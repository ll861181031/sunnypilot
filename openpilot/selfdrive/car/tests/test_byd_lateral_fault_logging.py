from types import SimpleNamespace
from unittest.mock import patch

from openpilot.selfdrive.car import card
from opendbc.car import structs


class _SubMasterLike:
  """Mimics the real messaging.SubMaster closely enough to catch the class of bug.

  SubMaster defines __getitem__ but neither __contains__ nor __iter__, so Python
  falls back to the legacy integer protocol: ``"pandaStates" in sm`` evaluates
  sm[0], sm[1], ... and self.data[0] raises KeyError: 0. That is not IndexError,
  so it escapes instead of ending the iteration - and took card down on every
  start, which showed up on screen as "Unknown Vehicle Variant".

  The earlier tests in this file assign a plain dict, where ``in`` works fine, so
  they could never have caught it. This double exists so the real behaviour is
  exercised.
  """

  def __init__(self, messages, seen=True):
    self._data = dict(messages)
    self.seen = dict.fromkeys(messages, seen)

  def __getitem__(self, key):
    return self._data[key]


def make_card():
  obj = card.Car.__new__(card.Car)
  obj.CI = SimpleNamespace(CS=SimpleNamespace(
    lateral_control_fault="panda_tx_rejected",
    byd_lateral=structs.BydLateralDiagnostics(valid=True, finalAngleDeg=-7.7,
                                               leadBoundDeg=5.5, submitted=True),
  ))
  obj.sm = _SubMasterLike({"pandaStates": [SimpleNamespace(
    controlsAllowed=True, safetyTxBlocked=12, safetyRxInvalid=1,
    safetyRxChecksInvalid=False, faults=[], safetyModel="byd",
  )]})
  obj._prev_lateral_fault = False
  obj._lateral_fault_start = None
  obj._last_panda_tx_blocked = 10
  return obj


def make_state(fault: bool):
  return SimpleNamespace(
    steerFaultTemporary=fault,
    steerFaultPermanent=False,
    vEgo=12.5,
    steeringAngleDeg=-7.25,
    steeringRateDeg=4.0,
    steeringTorque=-20.0,
    steeringTorqueEps=-35.0,
    steeringPressed=False,
    canValid=True,
    byd_lateral=structs.BydLateralDiagnostics(valid=True, finalAngleDeg=-7.7,
                                               leadBoundDeg=5.5, submitted=True),
  )


def test_lateral_fault_is_logged_once_on_the_edge():
  obj = make_card()
  with patch.object(card.cloudlog, "event") as event:
    obj._log_lateral_fault(make_state(True))
    obj._log_lateral_fault(make_state(True))

  assert event.call_count == 1
  args, kwargs = event.call_args
  assert args[0] == "bydLateralFault"
  assert kwargs["error"] is True
  assert kwargs["epsTemporary"] is True
  assert kwargs["lateralControlFault"] == "panda_tx_rejected"
  assert kwargs["pandaTxBlockedDelta"] == 2
  assert kwargs["bydLateral"]["finalAngleDeg"] == -7.7
  assert kwargs["speedMs"] == 12.5


def test_lateral_fault_recovery_is_logged_and_resets_state():
  obj = make_card()
  with patch.object(card.cloudlog, "event") as event:
    obj._log_lateral_fault(make_state(True))
    obj._log_lateral_fault(make_state(False))
    obj._log_lateral_fault(make_state(False))

  assert event.call_count == 2
  recovery = event.call_args_list[1]
  assert recovery.args[0] == "bydLateralRecovered"
  assert "error" not in recovery.kwargs
  assert obj._lateral_fault_start is None
  assert obj._prev_lateral_fault is False


def test_membership_test_on_a_real_submaster_like_object_does_not_crash():
  """Regression: ``"pandaStates" in sm`` used to raise KeyError: 0."""
  obj = make_card()
  obj.sm = _SubMasterLike({"pandaStates": [SimpleNamespace(
    controlsAllowed=True, safetyTxBlocked=12, safetyRxInvalid=1,
    safetyRxChecksInvalid=False, faults=[], safetyModel="byd",
  )]})
  with patch.object(card.cloudlog, "event"):
    obj._log_lateral_fault(make_state(True))  # must not raise
  assert obj._last_panda_tx_blocked == 12


def test_missing_panda_states_is_tolerated():
  """card must survive a frame where pandad has not published yet."""
  obj = make_card()
  obj.sm = _SubMasterLike({})
  with patch.object(card.cloudlog, "event") as event:
    obj._log_lateral_fault(make_state(True))
  _, kwargs = event.call_args
  assert kwargs["panda"] is None


def test_a_seen_but_unpublished_service_is_tolerated():
  """seen True but no data yet is a real state at startup."""
  obj = make_card()
  obj.sm = _SubMasterLike({}, seen=True)
  obj.sm.seen["pandaStates"] = True
  with patch.object(card.cloudlog, "event"):
    obj._log_lateral_fault(make_state(True))
