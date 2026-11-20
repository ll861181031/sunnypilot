import io
import subprocess
import threading
from types import SimpleNamespace

import pytest

from APP.navdiag import android_gps_source as gps
from APP.navdiag import amap_adb_memory as memory
from APP.navdiag import bridge_observation as bridge
from APP.navdiag import device as device_module
from APP.navdiag import native_route_source as native
from APP.navdiag import phone_route_producer as producer
from APP.navdiag import root_probe, route_snapshot


class FakeProcess:
  def __init__(self, command, returncode, stdout, stderr, *, server=False):
    self.args, self.returncode = command, returncode
    self.output, self.error, self.server = stdout, stderr, server
    self.stdin, self.stdout, self.stderr = io.BytesIO(), io.BytesIO(), io.BytesIO()

  def communicate(self, timeout):
    return self.output, self.error

  def poll(self):
    return self.returncode

  def wait(self, timeout):
    return self.returncode


@pytest.fixture
def transport(monkeypatch):
  state = SimpleNamespace(listening=False, daemon_starts=0, explicit_starts=0, commands=[], servers=[])

  def dispatch(command, text=False):
    state.commands.append(command)
    if command[-1] == "start-server":
      state.explicit_starts += 1
      state.listening = True
    elif not state.listening:
      if "-H" in command:
        assert command[command.index("-H") + 1] == "127.0.0.1"
        return 1, "" if text else b"", "server unavailable" if text else b"server unavailable"
      state.daemon_starts += 1
      state.listening = True
    if command[-1] == "kill-server":
      state.listening = False
    output = "List of devices attached\nsynthetic-phone\tdevice\n" if command[-1] == "devices" else ""
    return 0, output if text else output.encode(), "" if text else b""

  def popen(command, **kwargs):
    if command[-2:] == ["server", "nodaemon"]:
      state.commands.append(command)
      state.listening = True
      process = FakeProcess(command, None, b"", b"", server=True)
      state.servers.append(process)
      return process
    return FakeProcess(command, *dispatch(command, kwargs.get("text", False)))

  def run(command, **kwargs):
    result = subprocess.CompletedProcess(command, *dispatch(command, kwargs.get("text", False)))
    if kwargs.get("check"):
      result.check_returncode()
    return result

  def stop(process, **kwargs):
    if process.server:
      process.returncode = 1
      state.listening = False

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

  monkeypatch.setattr(subprocess, "Popen", popen)
  monkeypatch.setattr(subprocess, "run", run)
  monkeypatch.setattr(producer.socket, "socket", Socket)
  monkeypatch.setattr(producer, "_kill_local", stop)
  monkeypatch.setattr(producer.os, "killpg", lambda *a: pytest.fail("不能触碰真实进程"))
  state.stop = stop
  return state


def owned_device():
  return producer._InterruptibleDevice("/fixed/adb", 5039, "synthetic-phone", threading.Event())


@pytest.mark.parametrize("operation", ["host", "binary", "cleanup"])
def test_owned_client_fails_closed_after_owned_server_exits(transport, operation):
  device = owned_device()
  with device:
    transport.stop(device.server)
    if operation == "host":
      result = device.host("devices", check=False)
    elif operation == "binary":
      result = device.call_binary("shell", "-T", "true", check=False)
      assert isinstance(result.stdout, bytes)
    else:
      device.stop.set()
      result = device.cleanup_shell("-T", "true")
    assert result.returncode == 1
    assert transport.daemon_starts == 0 and not transport.listening
  with owned_device():
    assert transport.listening
  assert not transport.listening and len(transport.servers) == 2
  assert all(item.args == ["/fixed/adb", "-L", "tcp:localhost:5039", "server", "nodaemon"]
             for item in transport.servers)


def test_checked_owned_client_reports_transport_failure_without_daemon(transport):
  with pytest.raises(producer.PhoneRouteProducerRejected, match="^phone_adb_command_failed$"):
    owned_device().host("devices")
  assert transport.daemon_starts == 0 and not transport.listening


@pytest.mark.parametrize("source_type", [native.NativeRouteProbeSource, gps.AndroidGpsProbeSource,
                                         gps.AndroidGpsBridgeSource,
                                         producer._NativeSource, producer._GpsSource])
def test_probe_direct_popen_cannot_restart_missing_server(transport, monkeypatch, source_type):
  device = owned_device()
  monkeypatch.setattr(native, "process_identity", lambda _: "123:456")
  monkeypatch.setattr(device, "shell", lambda *a, **k: SimpleNamespace(
    returncode=0, stdout="package:/data/app/" + device_module.PACKAGE + "-fixture/base.apk\n"))
  source = source_type(device, threading.Event()) if source_type in (producer._NativeSource, producer._GpsSource) else source_type(device)
  if isinstance(source, gps.AndroidGpsBridgeSource):
    with pytest.raises(gps.GpsBridgeRejected, match="gps_bridge_start_failed"):
      source.__enter__()
  else:
    def closed(*args):
      raise RuntimeError("synthetic_closed_channel")
    monkeypatch.setattr(source, "_line", closed)
    with pytest.raises(RuntimeError, match="synthetic_closed_channel"):
      source.__enter__()
  assert source.process is None and source.cleanup_verified
  assert transport.daemon_starts == 0 and not transport.listening


@pytest.mark.parametrize("production", [False, True])
def test_binary_cache_read_cannot_create_server(transport, production):
  device = owned_device() if production else device_module.Device("/fixed/adb", 5039, "synthetic-phone")
  with pytest.raises(ValueError, match="^navigation_file_read_failed$"):
    route_snapshot.read_file(device, route_snapshot.CACHE_DIRECTORY + "/1.dat")
  assert transport.daemon_starts == 0 and not transport.listening


def test_diagnostic_device_explicit_start_is_preserved_and_clients_are_connect_only(transport):
  with device_module.Device("/fixed/adb", 5039, "synthetic-phone") as device:
    assert device.started and transport.listening
  assert transport.explicit_starts == 1 and transport.daemon_starts == 0 and not transport.listening
  assert transport.commands[0] == ["/fixed/adb", "-P", "5039", "start-server"]
  assert all(command[1:5] == ["-H", "127.0.0.1", "-P", "5039"] for command in transport.commands[1:])


def test_attach_device_only_queries_existing_server(transport):
  transport.listening = True
  with device_module.Device("/fixed/adb", 5039, "synthetic-phone", ownership="attach") as device:
    assert not device.started and transport.listening
  assert transport.explicit_starts == 0
  assert transport.daemon_starts == 0
  assert transport.listening
  assert transport.commands == [["/fixed/adb", "-H", "127.0.0.1", "-P", "5039", "devices"]]


def test_attach_device_fails_without_starting_missing_server(transport):
  with pytest.raises(ValueError, match="^adb_server_unavailable$"):
    device_module.Device("/fixed/adb", 5039, "synthetic-phone", ownership="attach").__enter__()
  assert transport.commands == []
  assert transport.explicit_starts == 0 and transport.daemon_starts == 0
  assert not transport.listening


def test_attach_device_does_not_kill_server_after_device_error(transport, monkeypatch):
  transport.listening = True
  monkeypatch.setattr(device_module.time, "sleep", lambda _: None)
  with pytest.raises(RuntimeError, match="ADB 尚未连接或授权"):
    with device_module.Device("/fixed/adb", 5039, "missing-phone", ownership="attach"):
      pytest.fail("missing phone must not enter")
  assert transport.explicit_starts == 0 and transport.daemon_starts == 0
  assert transport.listening
  assert all(command[-1] != "kill-server" for command in transport.commands)


def test_attach_uses_connect_only_command_without_implicit_daemon(transport):
  transport.listening = True
  with device_module.Device("/fixed/adb", 5039, "synthetic-phone", ownership="attach") as device:
    result = device.shell("getprop", "ro.build.version.release")
  assert result.returncode == 0
  assert transport.explicit_starts == 0 and transport.daemon_starts == 0
  assert transport.listening


def test_legacy_memory_client_cannot_create_server(transport):
  source = memory.NativeMemorySource.__new__(memory.NativeMemorySource)
  source.device, source.pid, source.process = owned_device(), "123", None
  with source:
    assert source.process.poll() == 1
  assert transport.daemon_starts == 0 and not transport.listening


def test_bridge_and_root_diagnostic_clients_cannot_create_server(transport):
  device = owned_device()
  with pytest.raises(bridge.BridgeError, match="^bridge_read_failed$"):
    bridge.read_latest(device, runner=subprocess.run)
  event, body = root_probe.read_once(device, 1)
  assert event["status"] == "command_error" and body is None
  assert transport.daemon_starts == 0 and not transport.listening
