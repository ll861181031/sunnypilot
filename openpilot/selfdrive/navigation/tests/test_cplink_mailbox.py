"""Exercise the actual on-disk handoff with real cereal messages and no msgq sockets."""
from types import SimpleNamespace

import pytest

from openpilot.cereal import messaging
from openpilot.selfdrive.navigation import cplink, navd
from openpilot.sunnypilot.mapd.live_map_data import base_map_data
from openpilot.sunnypilot.mapd.live_map_data.osm_map_data import OsmMapData


@pytest.fixture
def mailbox(tmp_path, monkeypatch):
  path = tmp_path / "phone"
  monkeypatch.setattr(cplink, "CPLINK_GATE", path)
  monkeypatch.setattr(navd, "CPLINK_GATE", path)
  monkeypatch.setattr(base_map_data, "CPLINK_GATE", path)
  return cplink.CplinkMailbox(path)


@pytest.mark.parametrize("service", ["navInstruction", "liveMapDataSP"])
def test_mailbox_roundtrip_retains_validity_and_source_timestamp(mailbox, service):
  msg = messaging.new_message(service, valid=True)
  mailbox.send(service, msg)
  result = cplink.read_cplink_message(service)
  assert result is not None and result.valid
  assert result.logMonoTime == msg.logMonoTime
  assert result.which() == service
  assert not list(mailbox.gate_path.parent.glob(".*"))
  mailbox.send(service, messaging.new_message(service, valid=False))
  assert not cplink.read_cplink_message(service).valid


@pytest.mark.parametrize("service", ["navInstruction", "liveMapDataSP"])
def test_polling_cannot_extend_message_lifetime(mailbox, monkeypatch, service):
  clock = [100.0]
  monkeypatch.setattr(cplink.time, "monotonic", lambda: clock[0])
  msg = messaging.new_message(service, valid=True)
  mailbox.send(service, msg)
  assert cplink.read_cplink_message(service) is not None
  clock[0] += cplink.MESSAGE_TIMEOUT[service] + 0.1
  assert cplink.read_cplink_message(service) is None


@pytest.mark.parametrize("payload", [b"broken", b"", b"x" * 65537])
def test_bad_payload_is_unavailable(mailbox, payload):
  path = mailbox.gate_path.with_name(mailbox.gate_path.name + ".navInstruction")
  path.write_bytes(payload)
  assert cplink.read_cplink_message("navInstruction") is None


def test_missing_or_wrong_service_is_unavailable(mailbox):
  assert cplink.read_cplink_message("navInstruction") is None
  path = mailbox.gate_path.with_name(mailbox.gate_path.name + ".navInstruction")
  path.write_bytes(messaging.new_message("liveMapDataSP", valid=True).to_bytes())
  assert cplink.read_cplink_message("navInstruction") is None


def test_navd_step_forwards_phone_and_clears_missing_mailbox(mailbox):
  mailbox.gate_path.write_text("nav")
  sent = []
  d = navd.Navd(None, SimpleNamespace(send=lambda service, msg: sent.append((service, msg))))
  msg = messaging.new_message("navInstruction", valid=True)
  msg.navInstruction.maneuverModifier = "left"
  mailbox.send("navInstruction", msg)
  d.step(100.0)
  assert sent[0][0] == "navRoute" and len(sent[0][1].navRoute.coordinates) == 0
  assert sent[1][0] == "navInstruction" and sent[1][1].navInstruction.maneuverModifier == "left"
  mailbox.gate_path.with_name(mailbox.gate_path.name + ".navInstruction").unlink()
  d.step(100.3)
  assert not sent[-1][1].valid


def test_speed_source_uses_the_existing_publisher_and_clears_missing_mailbox(mailbox):
  mailbox.gate_path.write_text("speed")
  sent = []
  d = OsmMapData.__new__(OsmMapData)
  d.pm = SimpleNamespace(send=lambda service, msg: sent.append(msg))
  msg = messaging.new_message("liveMapDataSP", valid=True)
  msg.liveMapDataSP.speedLimitValid = True
  msg.liveMapDataSP.speedLimit = 60 / 3.6
  mailbox.send("liveMapDataSP", msg)
  d.publish()
  assert sent[-1].liveMapDataSP.speedLimit == pytest.approx(60 / 3.6)
  mailbox.gate_path.with_name(mailbox.gate_path.name + ".liveMapDataSP").unlink()
  d.publish()
  assert not sent[-1].valid and not sent[-1].liveMapDataSP.speedLimitValid
