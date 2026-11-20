"""Root route geometry does not authenticate the legacy maneuver mailbox."""
from types import SimpleNamespace

import pytest

from openpilot.cereal import messaging
from openpilot.selfdrive.navigation import navd


@pytest.mark.parametrize("mailbox_valid", [False, True])
def test_root_publisher_never_reads_or_forwards_legacy_instruction(monkeypatch, mailbox_valid):
  monkeypatch.setenv("SUNNYPILOT_AMAP_ROOT_NAV", "1")
  original = messaging.new_message("navInstruction", valid=mailbox_valid)
  original.navInstruction.allManeuvers = [{"distance": 50.0, "type": "turn", "modifier": "right"}]
  reads, sent = [], []

  def read(*args):
    reads.append(args)
    return original

  monkeypatch.setattr(navd, "read_cplink_message", read)
  daemon = navd.Navd(None, SimpleNamespace(send=lambda name, message: sent.append((name, message))))
  daemon.publish_instruction(100.0)
  assert not reads
  assert len(sent) == 1 and sent[0][0] == "navInstruction"
  assert not sent[0][1].valid
  assert not list(sent[0][1].navInstruction.allManeuvers)
  assert sent[0][1].navInstruction.routeBinding.version == 0
  assert daemon.sent_instruction_at == 100.0


def test_nonroot_phone_publisher_preserves_legacy_message_and_timestamp(monkeypatch):
  monkeypatch.setenv("SUNNYPILOT_AMAP_ROOT_NAV", "0")
  monkeypatch.setattr(navd, "cplink_navi_active", lambda: True)
  original = messaging.new_message("navInstruction", valid=True)
  original.logMonoTime = 99_900_000_000
  original.navInstruction.allManeuvers = [{"distance": 50.0, "type": "turn", "modifier": "right"}]
  monkeypatch.setattr(navd, "read_cplink_message", lambda *_: original)
  sent = []
  daemon = navd.Navd(None, SimpleNamespace(send=lambda name, message: sent.append(message)))
  daemon.publish_instruction(100.0)
  assert sent == [original]
  assert sent[0].valid and sent[0].logMonoTime == 99_900_000_000


def test_root_instruction_publication_does_not_mutate_route_adapter(monkeypatch):
  monkeypatch.setenv("SUNNYPILOT_AMAP_ROOT_NAV", "1")
  monkeypatch.setattr(navd, "read_cplink_message", lambda *_: pytest.fail("Root instruction must not read the mailbox"))
  daemon = navd.Navd(None, SimpleNamespace(send=lambda *_: None))
  adapter = daemon.phone_adapter
  route = daemon.route = object()
  daemon.publish_instruction(100.0)
  assert daemon.phone_adapter is adapter and daemon.route is route
