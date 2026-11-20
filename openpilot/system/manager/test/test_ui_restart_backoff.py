import importlib
from types import SimpleNamespace

import pytest

from openpilot.common import hardware
from openpilot.system.manager import process as process_mod
from openpilot.system.manager import process_config


@pytest.fixture
def process_clock(monkeypatch):
  clock = SimpleNamespace(now=100.0, started=[])

  class Child:
    def __init__(self, **kwargs):
      self.name = kwargs["name"]
      self.pid = 4242
      self.exitcode = None

    def start(self):
      clock.started.append((self.name, clock.now))

    def is_alive(self):
      return self.exitcode is None

  monkeypatch.setattr(process_mod, "Process", Child)
  monkeypatch.setattr(process_mod.time, "monotonic", lambda: clock.now)
  return clock


@pytest.fixture
def ui_process(monkeypatch, process_clock):
  try:
    with monkeypatch.context() as patch:
      patch.setattr(hardware, "PC", True)
      yield importlib.reload(process_config).managed_processes["ui"]
  finally:
    importlib.reload(process_config)


@pytest.mark.parametrize("exit_code", [75, -11, 1, 0])
def test_ui_waits_before_retry_and_reports_unavailable(ui_process, process_clock, exit_code):
  p, clock = ui_process, process_clock
  p.start()
  p.proc.exitcode = exit_code
  p.start()

  assert clock.started == [("ui", 100.0)]
  state = p.get_process_state_msg()
  assert not state.running
  assert state.shouldBeRunning
  assert state.exitCode == exit_code
  clock.now = 100.999
  p.start()
  assert len(clock.started) == 1
  clock.now = 101.0
  p.start()
  assert clock.started == [("ui", 100.0), ("ui", 101.0)]
  assert p.get_process_state_msg().running


def test_repeated_failures_back_off_but_keep_retrying(ui_process, process_clock):
  p, clock = ui_process, process_clock
  p.start()
  for delay in (1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0):
    count = len(clock.started)
    p.proc.exitcode = 75
    p.start()
    assert len(clock.started) == count
    clock.now += delay - 0.001
    p.start()
    assert len(clock.started) == count
    clock.now += 0.001
    p.start()
    assert len(clock.started) == count + 1
    assert p.get_process_state_msg().running


def test_healthy_run_resets_retry_delay(ui_process, process_clock):
  p, clock = ui_process, process_clock
  p.start()
  for delay in (1.0, 2.0):
    p.proc.exitcode = 75
    p.start()
    clock.now += delay
    p.start()
  assert len(clock.started) == 3

  clock.now += 30.0
  p.start()
  assert len(clock.started) == 3
  p.proc.exitcode = 75
  p.start()
  assert not p.get_process_state_msg().running
  clock.now += 1.0
  p.start()
  assert len(clock.started) == 4
  assert p.get_process_state_msg().running


def test_explicit_stop_cancels_retry_and_clears_backoff(ui_process, process_clock):
  p, clock = ui_process, process_clock
  p.start()
  for delay in (1.0, 2.0):
    p.proc.exitcode = 75
    p.start()
    clock.now += delay
    p.start()
  p.proc.exitcode = 75
  p.start()
  assert not p.get_process_state_msg().running

  p.stop(block=False)
  assert not p.get_process_state_msg().shouldBeRunning
  p.start()
  assert len(clock.started) == 4
  p.proc.exitcode = 75
  p.start()
  clock.now += 1.0
  p.start()
  assert len(clock.started) == 5


def test_intentional_stop_does_not_delay_next_start(monkeypatch, ui_process, process_clock):
  p, clock = ui_process, process_clock
  p.start()
  monkeypatch.setattr(p, "signal", lambda sig: setattr(p.proc, "exitcode", 0))
  p.stop(block=False)
  p.start()
  assert clock.started == [("ui", 100.0), ("ui", 100.0)]


def test_ui_retry_does_not_block_other_processes(ui_process, process_clock):
  p, clock = ui_process, process_clock
  p.start()
  p.proc.exitcode = 75
  p.start()
  other = process_mod.PythonProcess("other", "example.other", lambda *_: True)
  p.should_run = lambda *_: True
  process_mod.ensure_running({"ui": p, "other": other}.values(), True, None, None)
  assert clock.started == [("ui", 100.0), ("other", 100.0)]
  assert not p.get_process_state_msg().running
  assert other.get_process_state_msg().running


def test_non_pc_ui_keeps_existing_restart_policy(monkeypatch, process_clock):
  try:
    with monkeypatch.context() as patch:
      patch.setattr(hardware, "PC", False)
      p = importlib.reload(process_config).managed_processes["ui"]
      p.start()
      p.proc.exitcode = 1
      p.start()
      assert process_clock.started == [("ui", 100.0), ("ui", 100.0)]
  finally:
    importlib.reload(process_config)
