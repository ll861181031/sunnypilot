"""A crashed child must be reaped, otherwise the manager can never restart it.

The ui process died once during an early X-server race and the manager kept it
"running=False, shouldBeRunning=True" forever, because `start()` returned on a
non-None proc with a non-None exitcode.
"""
from openpilot.system.manager import process as process_mod


class _FakeProc:
  def __init__(self, **kwargs):
    self.kwargs = kwargs
    self.exitcode = None
    self.pid = 4242

  def start(self):
    pass

  def join(self, timeout=None):
    pass

  def is_alive(self):
    return self.exitcode is None


def _patch_process(monkeypatch):
  created = []
  monkeypatch.setattr(process_mod, "Process", lambda **kw: created.append(_FakeProc(**kw)) or created[-1])
  return created


def test_crashed_python_process_is_restarted(monkeypatch):
  created = _patch_process(monkeypatch)
  p = process_mod.PythonProcess("ui", "openpilot.selfdrive.ui.ui", lambda *a: True)
  p.proc = _FakeProc()
  p.proc.exitcode = -11

  p.start()

  assert len(created) == 1
  assert p.proc is created[0]


def test_live_python_process_is_not_restarted(monkeypatch):
  created = _patch_process(monkeypatch)
  p = process_mod.PythonProcess("ui", "openpilot.selfdrive.ui.ui", lambda *a: True)
  p.proc = _FakeProc()

  p.start()

  assert created == []
  assert p.proc is not None


def test_crashed_native_process_is_restarted(monkeypatch):
  created = _patch_process(monkeypatch)
  p = process_mod.NativeProcess("modeld", ".", ["true"], lambda *a: True)
  p.proc = _FakeProc()
  p.proc.exitcode = 1

  p.start()

  assert len(created) == 1
  assert p.proc is created[0]


def test_limited_process_stops_after_repeated_crashes(monkeypatch):
  created = _patch_process(monkeypatch)
  clock = [0.0]
  monkeypatch.setattr(process_mod.time, "monotonic", lambda: clock[0])
  p = process_mod.PythonProcess("modeld", "example.modeld", lambda *a: True, restart_limit=(3, 300.0))
  p.start()
  for moment in (10.0, 20.0, 30.0):
    clock[0] = moment
    p.proc.exitcode = 1
    p.start()

  assert len(created) == 3
  assert p.proc is None
  state = p.get_process_state_msg()
  assert not state.running
  assert state.shouldBeRunning
  assert state.exitCode == 1
  clock[0] = 1000.0
  p.start()
  assert len(created) == 3

  # An explicit stop, or leaving onroad, arms the process for a new session.
  p.stop(block=False)
  assert not p.get_process_state_msg().shouldBeRunning
  p.start()
  assert len(created) == 4


def test_crashes_outside_window_do_not_disable_healthy_process(monkeypatch):
  created = _patch_process(monkeypatch)
  clock = [0.0]
  monkeypatch.setattr(process_mod.time, "monotonic", lambda: clock[0])
  p = process_mod.PythonProcess("modeld", "example.modeld", lambda *a: True, restart_limit=(3, 300.0))
  p.start()
  for moment in (10.0, 400.0, 800.0, 1200.0):
    clock[0] = moment
    p.proc.exitcode = 1
    p.start()
  assert len(created) == 5
  assert p.proc is created[-1]


def test_offroad_transition_rearms_a_blocked_process(monkeypatch):
  created = _patch_process(monkeypatch)
  p = process_mod.PythonProcess("modeld", "example.modeld", lambda started, *_: started, restart_limit=(1, 300.0))
  procs = {p.name: p}
  process_mod.ensure_running(procs.values(), True, None, None)
  p.proc.exitcode = -11
  process_mod.ensure_running(procs.values(), True, None, None)
  assert len(created) == 1
  assert p.get_process_state_msg().shouldBeRunning
  process_mod.ensure_running(procs.values(), False, None, None)
  assert not p.get_process_state_msg().shouldBeRunning
  process_mod.ensure_running(procs.values(), True, None, None)
  assert len(created) == 2


def test_intentional_nonblocking_stop_does_not_count_as_crash(monkeypatch):
  created = _patch_process(monkeypatch)
  p = process_mod.PythonProcess("modeld", "example.modeld", lambda *_: True, restart_limit=(1, 300.0))
  p.start()
  signals = []
  monkeypatch.setattr(p, "signal", signals.append)
  p.stop(block=False)
  p.proc.exitcode = 0
  p.start()
  assert len(signals) == 1
  assert len(created) == 2
  assert p.get_process_state_msg().running


def test_unlimited_python_process_keeps_restarting(monkeypatch):
  created = _patch_process(monkeypatch)
  p = process_mod.PythonProcess("ui", "example.ui", lambda *_: True)
  p.start()
  for _ in range(10):
    p.proc.exitcode = 1
    p.start()
  assert len(created) == 11
  assert p.get_process_state_msg().running
