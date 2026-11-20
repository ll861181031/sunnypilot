import importlib
import os
import signal
import time
import subprocess
from collections.abc import Callable, ValuesView
from abc import ABC, abstractmethod
from multiprocessing import Process

# A direct process-manager import can load cereal/KJ before the launcher has a
# chance to update PWD after changing directories.
os.environ.pop('PWD', None)

from setproctitle import setproctitle

from openpilot.cereal import log
from opendbc.car.structs import car
import openpilot.cereal.messaging as messaging
import openpilot.system.sentry as sentry
from openpilot.common.basedir import BASEDIR
from openpilot.common.params import Params
from openpilot.common.swaglog import cloudlog


def launcher(proc: str, name: str) -> None:
  try:
    # import the process
    mod = importlib.import_module(proc)

    # rename the process
    setproctitle(proc)

    # create new context since we forked
    messaging.reset_context()

    # add daemon name tag to logs
    cloudlog.bind(daemon=name)
    sentry.set_tag("daemon", name)

    # exec the process
    mod.main()
  except KeyboardInterrupt:
    cloudlog.warning(f"child {proc} got SIGINT")
  except Exception:
    # can't install the crash handler because sys.excepthook doesn't play nice
    # with threads, so catch it here.
    sentry.capture_exception()
    raise


def nativelauncher(pargs: list[str], cwd: str, name: str) -> None:
  os.environ['MANAGER_DAEMON'] = name

  # exec the process
  os.chdir(cwd)
  # Avoid inheriting a shell PWD that points at the manager directory after
  # this process switches to its own working directory.
  os.environ.pop('PWD', None)
  os.execvp(pargs[0], pargs)


def join_process(process: Process, timeout: float) -> None:
  # Process().join(timeout) will hang due to a python 3 bug: https://bugs.python.org/issue28382
  # We have to poll the exitcode instead
  t = time.monotonic()
  while time.monotonic() - t < timeout and process.exitcode is None:
    time.sleep(0.001)


class ManagerProcess(ABC):
  daemon = False
  sigkill = False
  should_run: Callable[[bool, Params, car.CarParams], bool]
  proc: Process | None = None
  enabled = True
  name = ""
  shutting_down = False

  @abstractmethod
  def start(self) -> None:
    pass

  def stop(self, retry: bool = True, block: bool = True, sig: signal.Signals | None = None) -> int | None:
    if self.proc is None:
      return None

    if self.proc.exitcode is None:
      if not self.shutting_down:
        cloudlog.info(f"killing {self.name}")
        if sig is None:
          sig = signal.SIGKILL if self.sigkill else signal.SIGINT
        self.signal(sig)
        self.shutting_down = True

        if not block:
          return None

      join_process(self.proc, 5)

      # If process failed to die send SIGKILL
      if self.proc.exitcode is None and retry:
        cloudlog.info(f"killing {self.name} with SIGKILL")
        self.signal(signal.SIGKILL)
        self.proc.join()

    ret = self.proc.exitcode
    cloudlog.info(f"{self.name} is dead with {ret}")

    if self.proc.exitcode is not None:
      self.shutting_down = False
      self.proc = None

    return ret

  def signal(self, sig: int) -> None:
    if self.proc is None:
      return

    # Don't signal if already exited
    if self.proc.exitcode is not None and self.proc.pid is not None:
      return

    # Can't signal if we don't have a pid
    if self.proc.pid is None:
      return

    cloudlog.info(f"sending signal {sig} to {self.name}")
    os.kill(self.proc.pid, sig)

  def get_process_state_msg(self):
    state = log.ManagerState.ProcessState.new_message()
    state.name = self.name
    if self.proc:
      state.running = self.proc.is_alive()
      state.shouldBeRunning = self.proc is not None and not self.shutting_down
      state.pid = self.proc.pid or 0
      state.exitCode = self.proc.exitcode or 0
    return state


class NativeProcess(ManagerProcess):
  def __init__(self, name, cwd, cmdline, should_run, enabled=True, sigkill=False):
    self.name = name
    self.cwd = cwd
    self.cmdline = cmdline
    self.should_run = should_run
    self.enabled = enabled
    self.sigkill = sigkill
    self.launcher = nativelauncher

  def start(self) -> None:
    # In case we only tried a non blocking stop we need to stop it before restarting
    if self.shutting_down:
      self.stop()

    if self.proc is not None:
      if self.proc.exitcode is None:
        return
      # A crashed child leaves proc set; without reaping it the manager would never
      # restart this process again (seen with ui after an early X-server race).
      self.stop(block=False)

    cwd = os.path.join(BASEDIR, self.cwd)
    cloudlog.info(f"starting process {self.name}")
    self.proc = Process(name=self.name, target=self.launcher, args=(self.cmdline, cwd, self.name))
    self.proc.start()
    self.shutting_down = False


class PythonProcess(ManagerProcess):
  def __init__(self, name, module, should_run, enabled=True, sigkill=False, restart_limit: tuple[int, float] | None = None,
               restart_backoff: tuple[float, float] | None = None):
    self.name = name
    self.module = module
    self.should_run = should_run
    self.enabled = enabled
    self.sigkill = sigkill
    self.launcher = launcher
    self.restart_limit = restart_limit
    self._crash_times: list[float] = []
    self._restart_blocked = False
    self._last_exit_code = 0
    self.restart_backoff = restart_backoff
    self._restart_delay = 0.0
    self._restart_at: float | None = None
    self._started_at = 0.0

  def start(self) -> None:
    # In case we only tried a non blocking stop we need to stop it before restarting
    if self.shutting_down:
      self.stop()

    if self.proc is not None:
      if self.proc.exitcode is None:
        if self.restart_backoff is not None and time.monotonic() - self._started_at >= self.restart_backoff[1]:
          self._restart_delay = 0.0
        return
      self._last_exit_code = self.proc.exitcode
      # Reap an unexpected exit without clearing the crash history.
      super().stop(block=False)
      if self.restart_limit is not None:
        limit, window = self.restart_limit
        now = time.monotonic()
        self._crash_times = [t for t in self._crash_times if now - t < window]
        self._crash_times.append(now)
        if len(self._crash_times) >= limit:
          self._restart_blocked = True
          cloudlog.error("%s crashed %d times within %.0fs; automatic restart disabled until stopped/offroad",
                         self.name, limit, window)
      if self.restart_backoff is not None:
        initial_delay, max_delay = self.restart_backoff
        self._restart_delay = min(max_delay, max(initial_delay, self._restart_delay * 2))
        self._restart_at = time.monotonic() + self._restart_delay
        cloudlog.warning("%s exited with %d; retrying in %.1fs", self.name, self._last_exit_code, self._restart_delay)

    if self._restart_blocked:
      return
    if self._restart_at is not None:
      # Do not sleep in the manager: other processes must keep being supervised.
      if time.monotonic() < self._restart_at:
        return
      self._restart_at = None

    cloudlog.info(f"starting python {self.module}")
    self.proc = Process(name=self.name, target=self.launcher, args=(self.module, self.name))
    self.proc.start()
    self.shutting_down = False
    if self.restart_backoff is not None:
      self._started_at = time.monotonic()

  def stop(self, retry: bool = True, block: bool = True, sig: signal.Signals | None = None) -> int | None:
    # An intentional stop (including an offroad transition) starts a new session.
    self._crash_times.clear()
    self._restart_blocked = False
    self._restart_delay = 0.0
    self._restart_at = None
    return super().stop(retry=retry, block=block, sig=sig)

  def get_process_state_msg(self):
    state = super().get_process_state_msg()
    if self._restart_blocked or self._restart_at is not None:
      # Keep the failure visible to selfdrived's processNotRunning check.
      state.shouldBeRunning = True
      state.exitCode = self._last_exit_code
    return state


class DaemonProcess(ManagerProcess):
  """Python process that has to stay running across manager restart.
  This is used for athena so you don't lose SSH access when restarting manager."""
  def __init__(self, name, module, param_name, enabled=True):
    self.name = name
    self.module = module
    self.param_name = param_name
    self.enabled = enabled
    self.params = None

  @staticmethod
  def should_run(started, params, CP):
    return True

  def start(self) -> None:
    if self.params is None:
      self.params = Params()

    pid = self.params.get(self.param_name)
    if pid is not None:
      try:
        os.kill(int(pid), 0)
        with open(f'/proc/{pid}/cmdline') as f:
          if self.module in f.read():
            # daemon is running
            return
      except (OSError, FileNotFoundError):
        # process is dead
        pass

    cloudlog.info(f"starting daemon {self.name}")
    proc = subprocess.Popen(['python', '-m', self.module],
                               stdin=open('/dev/null'),
                               stdout=open('/dev/null', 'w'),
                               stderr=open('/dev/null', 'w'),
                               preexec_fn=os.setpgrp)

    self.params.put(self.param_name, proc.pid, block=True)

  def stop(self, retry=True, block=True, sig=None) -> None:
    pass


def ensure_running(procs: ValuesView[ManagerProcess], started: bool, params: Params, CP: car.CarParams,
                   not_run: list[str] | None=None) -> list[ManagerProcess]:
  if not_run is None:
    not_run = []

  running = []
  for p in procs:
    if p.enabled and p.name not in not_run and p.should_run(started, params, CP):
      running.append(p)
    else:
      p.stop(block=False)

  for p in running:
    p.start()

  return running
