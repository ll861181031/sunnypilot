import io
import os
from pathlib import Path
import signal
import subprocess
import threading
from types import SimpleNamespace

import pytest

from APP.navdiag import phone_route_producer
from openpilot.selfdrive.navigation import navd


@pytest.fixture
def lifecycle(monkeypatch):
  state = SimpleNamespace(enabled=True, stopped=True, cleanup_verified=True, starts=0, closes=0, errors=[])

  class Source:
    def start(self):
      state.starts += 1
      return self

    def close(self):
      state.closes += 1
      return state.stopped

    def status(self):
      return SimpleNamespace(cleanup_verified=state.cleanup_verified)

  state.source = Source()

  def step(_now):
    raise state.exit_error

  state.nav = SimpleNamespace(phone_source=None, step=step)
  monkeypatch.setattr(navd, "Ratekeeper", lambda _rate: SimpleNamespace(keep_time=lambda: None))
  monkeypatch.setattr(navd.messaging, "SubMaster", lambda _services: SimpleNamespace(update=lambda _timeout: None))
  monkeypatch.setattr(navd.messaging, "PubMaster", lambda _services: object())
  monkeypatch.setattr(navd, "Navd", lambda _sm, _pm: state.nav)
  monkeypatch.setattr(navd, "amap_root_nav_enabled", lambda: state.enabled)
  monkeypatch.setattr(phone_route_producer, "PhoneRouteProducer", lambda **_kwargs: state.source)
  monkeypatch.setattr(navd.cloudlog, "error", state.errors.append)
  return state


@pytest.mark.parametrize("exit_type", [KeyboardInterrupt, RuntimeError])
@pytest.mark.parametrize("stopped, verified", [(True, True), (True, False), (False, True), (False, False)])
def test_main_reports_incomplete_cleanup_even_after_thread_stops(lifecycle, exit_type, stopped, verified):
  lifecycle.stopped, lifecycle.cleanup_verified = stopped, verified
  lifecycle.exit_error = exit_type()
  with pytest.raises(exit_type) as raised:
    navd.main()
  assert raised.value is lifecycle.exit_error
  assert lifecycle.nav.phone_source is lifecycle.source
  assert lifecycle.starts == lifecycle.closes == 1
  assert lifecycle.errors == ([] if stopped and verified else ["navd.phone_source_cleanup_incomplete"])


@pytest.mark.parametrize("exit_type", [KeyboardInterrupt, RuntimeError])
def test_disabled_phone_source_does_not_start_or_close(lifecycle, exit_type):
  lifecycle.enabled = False
  lifecycle.exit_error = exit_type()
  with pytest.raises(exit_type) as raised:
    navd.main()
  assert raised.value is lifecycle.exit_error
  assert lifecycle.nav.phone_source is None
  assert lifecycle.starts == lifecycle.closes == 0
  assert lifecycle.errors == []


@pytest.mark.parametrize(("port", "ownership", "serial"), [
  ("5037", "attach", "WDHVB20806017806"),
  ("5041", "dedicated", ""),
])
def test_phone_source_settings_preserve_shared_attach_and_dedicated_modes(monkeypatch, port, ownership, serial):
  monkeypatch.setenv("NAVDIAG_ADB_PORT", port)
  monkeypatch.setenv("NAVDIAG_ADB_OWNERSHIP", ownership)
  monkeypatch.setenv("NAVDIAG_SERIAL", serial)
  assert navd.phone_source_settings() == {
    "port": int(port), "adb_ownership": ownership, "serial": serial or None}


@pytest.mark.parametrize(("port", "ownership"), [
  ("0", "attach"), ("65536", "attach"), ("5037x", "attach"), (None, "owned"),
  (None, "ATTACH"), (" 5037", "attach"),
])
def test_phone_source_settings_reject_invalid_values_without_guessing(monkeypatch, port, ownership):
  if port is None:
    monkeypatch.delenv("NAVDIAG_ADB_PORT", raising=False)
  else:
    monkeypatch.setenv("NAVDIAG_ADB_PORT", port)
  monkeypatch.setenv("NAVDIAG_ADB_OWNERSHIP", ownership)
  with pytest.raises(ValueError, match="^phone_navigation_adb_configuration_invalid$"):
    navd.phone_source_settings()


def test_main_passes_resolved_phone_transport_to_producer(lifecycle, monkeypatch):
  seen = {}
  monkeypatch.setenv("NAVDIAG_ADB_PORT", "5037")
  monkeypatch.setenv("NAVDIAG_ADB_OWNERSHIP", "attach")
  monkeypatch.setenv("NAVDIAG_SERIAL", "WDHVB20806017806")
  monkeypatch.setattr(phone_route_producer, "PhoneRouteProducer",
                      lambda **kwargs: seen.update(kwargs) or lifecycle.source)
  lifecycle.exit_error = KeyboardInterrupt()
  with pytest.raises(KeyboardInterrupt):
    navd.main()
  assert seen == {"port": 5037, "adb_ownership": "attach", "serial": "WDHVB20806017806"}


def _launcher_example_settings(monkeypatch, **overrides):
  root = Path(__file__).resolve().parents[4]
  config = root / "APP/navdiag/phone-navigation.conf.example"
  env = os.environ.copy()
  for key in ("NAVDIAG_ADB_PORT", "NAVDIAG_ADB_OWNERSHIP", "NAVDIAG_SERIAL"):
    env.pop(key, None)
  env.update(overrides)
  command = r'''source "$1"
printf '%s\n%s\n%s' "$NAVDIAG_ADB_PORT" "$NAVDIAG_ADB_OWNERSHIP" "$NAVDIAG_SERIAL"'''
  result = subprocess.run(
    ["bash", "-c", command, "_", str(config)],
    capture_output=True, text=True, check=True, env=env,
  )
  for key, value in zip(("NAVDIAG_ADB_PORT", "NAVDIAG_ADB_OWNERSHIP", "NAVDIAG_SERIAL"), result.stdout.split("\n"), strict=True):
    monkeypatch.setenv(key, value)
  return navd.phone_source_settings()


def _adb_boundaries(monkeypatch, *, listening):
  state = SimpleNamespace(listening=listening, launches=[], kills=[], server=None)

  class Socket:
    def __enter__(self):
      return self
    def __exit__(self, *_):
      pass
    def settimeout(self, _):
      pass
    def connect_ex(self, address):
      assert address == ("127.0.0.1", 5039)
      return 0 if state.listening else 111

  class Process:
    def __init__(self, server):
      self.pid = 123456 if server else 123457
      self.returncode = None if server else 0
      self.stdout = None if server else io.StringIO()
      self.stderr = None if server else io.StringIO()
    def poll(self):
      return self.returncode
    def wait(self, timeout):
      return self.returncode
    def communicate(self, timeout):
      return "List of devices attached\nsynthetic-phone\tdevice\n", ""

  def popen(command, **kwargs):
    assert kwargs["start_new_session"] is True
    state.launches.append(command)
    if command == ["/synthetic/adb", "-L", "tcp:localhost:5039", "server", "nodaemon"]:
      assert not state.listening
      state.listening = True
      state.server = Process(True)
      return state.server
    assert command == ["/synthetic/adb", "-H", "127.0.0.1", "-P", "5039", "devices"]
    assert state.listening
    return Process(False)

  def killpg(pid, sig):
    assert state.server is not None and pid == state.server.pid
    state.kills.append((pid, sig))
    state.server.returncode = -sig
    state.listening = False

  monkeypatch.setattr(phone_route_producer.socket, "socket", Socket)
  monkeypatch.setattr(phone_route_producer.subprocess, "Popen", popen)
  monkeypatch.setattr(phone_route_producer.os, "killpg", killpg)
  return state


def _configured_device(settings):
  return phone_route_producer._InterruptibleDevice(
    "/synthetic/adb", settings["port"], settings["serial"], threading.Event(), ownership=settings["adb_ownership"])


@pytest.mark.parametrize("abort", [False, True])
def test_launcher_default_starts_and_cleans_its_cold_adb_server(monkeypatch, abort):
  settings = _launcher_example_settings(monkeypatch)
  state = _adb_boundaries(monkeypatch, listening=False)
  device = _configured_device(settings)
  try:
    with device:
      assert device.serial == "synthetic-phone"
      assert device.started and not device.cleanup_verified and device.server is state.server
      if abort:
        raise KeyboardInterrupt()
  except KeyboardInterrupt:
    assert abort
  assert not state.listening and state.kills == [(123456, signal.SIGTERM)]
  assert state.launches == [
    ["/synthetic/adb", "-L", "tcp:localhost:5039", "server", "nodaemon"],
    ["/synthetic/adb", "-H", "127.0.0.1", "-P", "5039", "devices"],
  ]
  assert device.cleanup_verified and not device.started and device.server is None


def test_launcher_explicit_attach_preserves_external_adb_server(monkeypatch):
  settings = _launcher_example_settings(monkeypatch, NAVDIAG_ADB_OWNERSHIP="attach")
  state = _adb_boundaries(monkeypatch, listening=True)
  device = _configured_device(settings)
  with device:
    assert device.serial == "synthetic-phone"
    assert not device.started and device.server is None
  assert state.listening and not state.kills
  assert state.launches == [["/synthetic/adb", "-H", "127.0.0.1", "-P", "5039", "devices"]]
  assert device.cleanup_verified and not device.started and device.server is None


def test_launcher_default_refuses_external_adb_port_without_takeover(monkeypatch):
  settings = _launcher_example_settings(monkeypatch)
  state = _adb_boundaries(monkeypatch, listening=True)
  device = _configured_device(settings)
  with pytest.raises(phone_route_producer.PhoneRouteProducerRejected, match="^phone_adb_port_owned_elsewhere$"):
    with device:
      pytest.fail("默认独占模式不能接管已占用端口")
  assert state.listening and not state.kills and not state.launches
  assert device.cleanup_verified and not device.started and device.server is None
