"""Serial selection tests without real ADB, sockets, phones, or worker threads."""
import threading
from types import SimpleNamespace

import pytest

from APP.navdiag import phone_route_producer as producer


@pytest.fixture(autouse=True)
def no_hardware(monkeypatch):
  monkeypatch.delenv("NAVDIAG_SERIAL", raising=False)
  monkeypatch.setattr(producer, "choose_adb", lambda _: "/fixed/adb")
  monkeypatch.setattr(producer.subprocess, "Popen", lambda *a, **k: pytest.fail("unexpected subprocess"))
  monkeypatch.setattr(producer.socket, "socket", lambda *a, **k: pytest.fail("unexpected socket"))
  monkeypatch.setattr(producer.threading.Thread, "start", lambda *a, **k: pytest.fail("unexpected worker"))


def _device(**kwargs):
  worker = producer.PhoneRouteProducer(**kwargs)
  return worker._device_factory(threading.Event())


@pytest.mark.parametrize("value", ["synthetic-phone", "ABC_0123", "emulator-5554", "192.0.2.1:5555", "A" * 128])
def test_environment_serial_reaches_device_without_changing_port(monkeypatch, value):
  monkeypatch.setenv("NAVDIAG_SERIAL", value)
  device = _device()
  assert device.serial == value
  assert device.port == 5039
  assert device.server is None and device.started is False


def test_attach_ownership_reaches_configured_shared_port_without_starting_server():
  device = _device(serial="explicit-phone", port=5037, adb_ownership="attach")
  assert device.serial == "explicit-phone" and device.port == 5037
  assert device.ownership == "attach"
  assert device.server is None and device.started is False


@pytest.mark.parametrize("value", ["", "owned", "attach ", None, True, 7])
def test_invalid_adb_ownership_is_rejected_before_ready(value):
  with pytest.raises(ValueError, match="^phone_producer_configuration_invalid$"):
    producer.PhoneRouteProducer(adb_ownership=value)


@pytest.mark.parametrize("environment", ["other-phone", "", "invalid env; never used"])
def test_explicit_serial_overrides_environment(monkeypatch, environment):
  monkeypatch.setenv("NAVDIAG_SERIAL", environment)
  device = _device(serial="explicit-phone", port=5041)
  assert device.serial == "explicit-phone" and device.port == 5041


@pytest.mark.parametrize("environment", [None, ""])
def test_unconfigured_or_empty_environment_preserves_auto_selection(monkeypatch, environment):
  if environment is not None:
    monkeypatch.setenv("NAVDIAG_SERIAL", environment)
  assert _device().serial is None


def test_environment_is_bound_at_construction_not_at_first_connect(monkeypatch):
  monkeypatch.setenv("NAVDIAG_SERIAL", "first-phone")
  worker = producer.PhoneRouteProducer()
  monkeypatch.setenv("NAVDIAG_SERIAL", "second-phone")
  assert worker._device_factory(threading.Event()).serial == "first-phone"


INVALID_SERIALS = [" ", "\t", "phone\n", "phone name", "phone;id", "$(id)", "-s", "手机", "phone/id", "phone\\id",
                   "A" * 129]


@pytest.mark.parametrize("value", INVALID_SERIALS)
@pytest.mark.parametrize("source", ["environment", "explicit"])
def test_invalid_serial_fails_before_adb_factory_or_thread(monkeypatch, value, source):
  monkeypatch.setattr(producer, "choose_adb", lambda _: pytest.fail("invalid serial reached ADB selection"))
  kwargs = {}
  if source == "environment":
    monkeypatch.setenv("NAVDIAG_SERIAL", value)
  else:
    monkeypatch.setenv("NAVDIAG_SERIAL", "valid-environment-phone")
    kwargs["serial"] = value
  with pytest.raises(ValueError, match="^phone_producer_serial_invalid$") as error:
    producer.PhoneRouteProducer(**kwargs)
  assert str(error.value) == "phone_producer_serial_invalid"


@pytest.mark.parametrize("value", ["", True, 7, b"phone", ["phone"], "phone\x00id"])
def test_explicit_invalid_types_or_empty_serial_do_not_fall_back(monkeypatch, value):
  monkeypatch.setenv("NAVDIAG_SERIAL", "valid-environment-phone")
  with pytest.raises(ValueError, match="^phone_producer_serial_invalid$"):
    producer.PhoneRouteProducer(serial=value)


def _fake_device_transport(monkeypatch, device, ready, *, stop_after_devices=False):
  calls, launches, killed = [], [], []
  connections = []
  process = SimpleNamespace(poll=lambda: None)

  class Socket:
    def __enter__(self):
      return self
    def __exit__(self, *_):
      pass
    def settimeout(self, _):
      pass
    def connect_ex(self, address):
      connections.append(address)
      return 111 if len(connections) == 1 else 0

  def popen(command, **kwargs):
    launches.append(command)
    return process

  def host(*args, **kwargs):
    calls.append(args)
    if stop_after_devices:
      device.stop.set()
    return SimpleNamespace(stdout="List of devices attached\n" + "".join(serial + "\tdevice\n" for serial in ready))

  monkeypatch.setattr(producer.socket, "socket", Socket)
  monkeypatch.setattr(producer.subprocess, "Popen", popen)
  monkeypatch.setattr(producer, "_kill_local", lambda item: killed.append(item))
  device.host = host
  return calls, launches, killed, process


def test_configured_serial_selects_exact_device_among_two_ready_devices(monkeypatch):
  monkeypatch.setenv("NAVDIAG_SERIAL", "phone")
  device = _device()
  calls, launches, killed, process = _fake_device_transport(monkeypatch, device, ["headunit", "phone"])
  with device:
    assert device.serial == "phone"
  assert calls == [("devices",)]
  assert launches == [["/fixed/adb", "-L", "tcp:localhost:5039", "server", "nodaemon"]]
  assert killed == [process] and device.cleanup_verified


@pytest.mark.parametrize("environment", [None, ""])
def test_unconfigured_multiple_ready_devices_still_reject(monkeypatch, environment):
  if environment is not None:
    monkeypatch.setenv("NAVDIAG_SERIAL", environment)
  device = _device()
  calls, _, killed, process = _fake_device_transport(monkeypatch, device, ["headunit", "phone"])
  with pytest.raises(producer.PhoneRouteProducerRejected, match="^phone_multiple_devices_require_serial$"):
    with device:
      pytest.fail("must not choose a device implicitly")
  assert calls == [("devices",)] and killed == [process] and device.cleanup_verified


def test_unconfigured_single_ready_device_still_selects_automatically(monkeypatch):
  device = _device()
  calls, _, killed, process = _fake_device_transport(monkeypatch, device, ["phone"])
  with device:
    assert device.serial == "phone"
  assert calls == [("devices",)] and killed == [process]


def test_configured_missing_device_never_falls_back_to_other_ready_device(monkeypatch):
  monkeypatch.setenv("NAVDIAG_SERIAL", "phone")
  device = _device()
  calls, _, killed, process = _fake_device_transport(monkeypatch, device, ["headunit"], stop_after_devices=True)
  with pytest.raises(producer._Stopped):
    with device:
      pytest.fail("must not select the other device")
  assert device.serial == "phone" and calls == [("devices",)] and killed == [process]
