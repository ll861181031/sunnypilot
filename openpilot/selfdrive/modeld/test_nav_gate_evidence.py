"""Offline evidence checks: observe navigation gates without changing their inputs."""
from types import SimpleNamespace

import numpy as np
import pytest

from openpilot.selfdrive.modeld import nav_inputs
from openpilot.selfdrive.navigation.helpers import navigation_input

MAP_SHAPE = (1, 1, 256, 256)
VEC_SHAPE = (1, 47)


class SM:
  def __init__(self, maneuvers=(), *, valid=True, received=99.9, produced=99.8):
    self.valid = {"navInstruction": valid}
    self.recv_time = {"navInstruction": received}
    self.logMonoTime = {"navInstruction": int(produced * 1e9)}
    self.instruction = SimpleNamespace(allManeuvers=list(maneuvers))

  def __getitem__(self, key):
    assert key == "navInstruction"
    return self.instruction


def maneuver(type_="turn", modifier="left", distance=30.0):
  return {"type": type_, "modifier": modifier, "distance": distance}


@pytest.fixture
def environment(monkeypatch):
  clock = [100.0]
  phone = [False]
  monkeypatch.setattr(nav_inputs.time, "monotonic", lambda: clock[0])
  monkeypatch.setattr(nav_inputs, "cplink_navi_active", lambda: phone[0])
  return clock, phone


def runtime():
  result = nav_inputs.NavRuntime()
  result._client_failed = True
  return result


def run(subject, sm, speed=10.0):
  return subject.update(sm, speed, MAP_SHAPE, VEC_SHAPE)


def client(image=None, *, produced=99.9, frame_id=23):
  if image is None:
    image = np.ones(MAP_SHAPE, np.uint8)
  buf = SimpleNamespace(data=image.tobytes(), width=image.shape[-1], height=image.shape[-2])
  return SimpleNamespace(is_connected=lambda: True, connect=lambda blocking: True,
                         recv=lambda timeout: buf, timestamp_eof=int(produced * 1e9), frame_id=frame_id)


def test_accepted_evidence_records_actual_selected_action_and_distance(environment):
  subject = runtime()
  sm = SM([maneuver(distance=0), maneuver("exit roundabout", "straight", 50)])
  _, vector = run(subject, sm)
  evidence = subject.evidence()
  expected = navigation_input(sm.instruction.allManeuvers, 10 * (100.0 - 99.8)).reshape(VEC_SHAPE)
  np.testing.assert_array_equal(vector, expected)
  assert evidence["instructionReason"] == "accepted"
  assert evidence["selectedManeuverIndex"] == 1
  assert evidence["selectedCommand"] == 27
  assert evidence["maneuverType"] == "exit roundabout"
  assert evidence["maneuverModifier"] == "straight"
  assert evidence["rawDistance"] == 50
  assert evidence["traveledDistance"] == pytest.approx(2.0)
  assert evidence["adjustedDistance"] == pytest.approx(48.0)
  assert evidence["instructionMonoTime"] == 99_800_000_000
  assert evidence["instructionReceivedMonoTime"] == 99_900_000_000


@pytest.mark.parametrize("first", [maneuver(distance=0), maneuver("roundabout", "straight", 0)])
def test_zero_distance_gap_is_distinguished_from_message_loss(environment, first):
  subject = runtime()
  _, vector = run(subject, SM([first, maneuver("continue", "straight", 0)]))
  evidence = subject.evidence()
  assert vector is None
  assert evidence["instructionReason"] == "passedManeuvers"
  assert evidence["maneuverCount"] == 2
  assert evidence["selectedManeuverIndex"] == -1
  assert evidence["selectedCommand"] == -1
  assert evidence["rawDistance"] == 0
  assert evidence["adjustedDistance"] == pytest.approx(-2.0)
  assert evidence["maneuverType"] == first["type"]
  assert evidence["nextManeuverType"] == "continue"
  assert evidence["nextManeuverModifier"] == "straight"
  assert evidence["nextManeuverDistance"] == 0


@pytest.mark.parametrize(("values", "reason"), [
  ({"valid": False}, "invalidMessage"),
  ({"valid": False, "received": 0, "produced": 0}, "missing"),
  ({"received": 99.4}, "staleReceived"),
  ({"produced": 99.4}, "staleProduced"),
  ({"received": 100.1}, "futureReceived"),
  ({"produced": 100.1}, "futureProduced"),
])
def test_rejection_reason_records_the_actual_gate(environment, values, reason):
  subject = runtime()
  _, vector = run(subject, SM([maneuver()], **values))
  assert vector is None
  assert subject.evidence()["instructionReason"] == reason
  assert subject.evidence()["selectedManeuverIndex"] == -1
  assert subject.evidence()["rawDistance"] == 30


@pytest.mark.parametrize(("maneuvers", "reason"), [
  ([], "emptyManeuvers"),
  ([maneuver("merge")], "unsupportedManeuver"),
  ([maneuver("depart")], "unsupportedManeuver"),
  ([maneuver(distance=float("nan"))], "nonfiniteDistance"),
  ([maneuver(distance=float("inf"))], "nonfiniteDistance"),
  ([maneuver(distance=-4)], "passedManeuvers"),
  ([maneuver("merge"), maneuver(distance=-4)], "noUsableManeuver"),
])
def test_unencodable_actions_are_distinguished(environment, maneuvers, reason):
  subject = runtime()
  _, vector = run(subject, SM(maneuvers))
  assert vector is None
  assert subject.evidence()["instructionReason"] == reason


def test_source_switch_and_phone_map_suppression_have_separate_reasons(environment):
  clock, phone = environment
  subject = runtime()
  subject.client = client()
  run(subject, SM([maneuver()]))
  phone[0] = True
  image, vector = run(subject, SM([maneuver()]))
  evidence = subject.evidence()
  assert image is None and vector is None
  assert evidence["source"] == "phone"
  assert evidence["sourceChangedMonoTime"] == 100_000_000_000
  assert evidence["acceptAfterMonoTime"] == 100_500_000_000
  assert evidence["sourceTransition"]
  assert evidence["instructionReason"] == "sourceSwitch"
  assert evidence["mapReason"] == "phoneMapSuppressed"
  clock[0] = 100.6
  _, vector = run(subject, SM([maneuver()], received=100.55, produced=100.51))
  assert vector is not None
  assert not subject.evidence()["sourceTransition"]
  assert subject.evidence()["instructionReason"] == "accepted"
  phone[0] = False
  run(subject, SM([maneuver()], received=100.6, produced=100.6))
  assert subject.evidence()["source"] == "native"
  assert subject.evidence()["mapReason"] == "sourceSwitch"


def test_accepted_map_has_capture_and_receive_identifiers(environment):
  subject = runtime()
  subject.client = client()
  image, _ = run(subject, SM())
  assert image is not None
  evidence = subject.evidence()
  assert evidence["mapReason"] == evidence["mapPollReason"] == "accepted"
  assert evidence["mapMonoTime"] == 99_900_000_000
  assert evidence["mapReceivedMonoTime"] == 100_000_000_000
  assert evidence["mapFrameId"] == 23


@pytest.mark.parametrize(("image", "produced", "reason"), [
  (np.zeros(MAP_SHAPE, np.uint8), 99.9, "emptyMap"),
  (np.ones((1, 1, 128, 128), np.uint8), 99.9, "shapeMismatch"),
  (np.ones(MAP_SHAPE, np.uint8), 99.4, "staleProduced"),
  (np.ones(MAP_SHAPE, np.uint8), 100.1, "futureProduced"),
])
def test_map_poll_rejections_are_explicit(environment, image, produced, reason):
  subject = runtime()
  subject.client = client(image, produced=produced)
  image, _ = run(subject, SM())
  assert image is None
  assert subject.evidence()["mapReason"] == subject.evidence()["mapPollReason"] == reason


def test_rejected_incoming_map_does_not_mislabel_usable_cached_map(environment):
  subject = runtime()
  subject.client = client()
  first, _ = run(subject, SM())
  subject.client.timestamp_eof = 99_000_000_000
  subject._map_poll = 1
  second, _ = run(subject, SM())
  assert second is first
  evidence = subject.evidence()
  assert evidence["mapReason"] == "accepted"
  assert evidence["mapPollReason"] == "staleProduced"
  assert evidence["mapMonoTime"] == 99_900_000_000


def test_cleared_cache_cannot_inherit_an_old_accepted_map_reason(environment):
  clock, phone = environment
  subject = runtime()
  subject.client = client()
  run(subject, SM())
  phone[0] = True
  run(subject, SM())
  phone[0] = False
  run(subject, SM())
  clock[0] = 100.6
  subject._map_poll = 0
  image, _ = run(subject, SM())
  assert image is None
  assert subject.evidence()["mapPollReason"] == "accepted"
  assert subject.evidence()["mapReason"] == "missing"


@pytest.mark.parametrize("reason", ["notConnected", "receiveError", "noFrame", "clientUnavailable"])
def test_map_connection_states_are_distinguished(environment, reason):
  subject = runtime()
  if reason != "clientUnavailable":
    subject.client = client()
    if reason == "notConnected":
      subject.client.is_connected = lambda: False
      subject.client.connect = lambda blocking: False
    elif reason == "noFrame":
      subject.client.recv = lambda timeout: None
    else:
      def failed_receive(timeout):
        raise OSError("no frame")
      subject.client.recv = failed_receive
  image, _ = run(subject, SM())
  assert image is None
  assert subject.evidence()["mapPollReason"] == reason


def test_map_source_transition_discards_queued_frame_with_explicit_reason(environment):
  subject = runtime()
  subject._source_changed_at = 99.4
  subject._accept_after = 99.9
  subject.client = client(produced=99.8)
  image, _ = run(subject, SM())
  assert image is None
  assert subject.evidence()["mapPollReason"] == "sourceSwitch"


def test_failed_diagnostic_does_not_discard_valid_inputs_or_status(environment, monkeypatch):
  subject = runtime()
  def failure(*args):
    raise ValueError("diagnostic failure")
  monkeypatch.setattr(subject, "_record_evidence", failure)
  _, vector = run(subject, SM([maneuver()]))
  assert vector is not None
  assert subject.status()["instruction_valid"]
  assert subject.evidence()["instructionReason"] == "diagnosticError"


def test_evidence_cannot_be_mutated_by_its_caller(environment):
  subject = runtime()
  run(subject, SM([maneuver()]))
  subject.evidence()["instructionReason"] = "changed"
  assert subject.evidence()["instructionReason"] == "accepted"
  assert set(subject.status()) == {"map_valid", "instruction_valid", "map_age", "instruction_age"}
