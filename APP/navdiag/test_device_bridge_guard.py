from types import SimpleNamespace

import pytest

import device


class RunningBridgeDevice:
  def __init__(self, *_):
    self.calls = []

  def __enter__(self):
    return self

  def __exit__(self, *_):
    pass

  def shell(self, *args, **kwargs):
    self.calls.append(args)
    assert args[:3] == ("dumpsys", "activity", "services")
    return SimpleNamespace(stdout=f"ServiceRecord{{abc u0 {device.PACKAGE}/.BridgeService}}\n")


@pytest.mark.parametrize("command", ["install", "selftest", "bridge-selftest"])
def test_running_bridge_prevents_destructive_maintenance(monkeypatch, command):
  monkeypatch.setattr(device, "Device", RunningBridgeDevice)
  monkeypatch.setattr(device, "choose_adb", lambda _: "fake-adb")
  monkeypatch.setattr("sys.argv", ["device.py", command])
  with pytest.raises(RuntimeError, match="观测桥运行"):
    device.main()


def test_service_detection_keeps_capture_and_bridge_distinct():
  fake = RunningBridgeDevice()
  assert device.bridge_service_running(fake)
  assert not device.capture_service_running(fake)
