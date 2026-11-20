"""Unknown is not clear: an unreadable blind-spot monitor blocks lane-change entry.

The flags are "unavailable" rather than "valid" on purpose. Every car that does not
fill them leaves them false, which must keep its lane changes working exactly as
before - that is the default-behavior test at the bottom.
"""
from openpilot.common.test import OpenpilotTestCase
from openpilot.selfdrive.controls.lib.desire_helper import DesireHelper, LaneChangeState
from openpilot.sunnypilot.selfdrive.controls.lib.lane_turn_desire import LANE_CHANGE_SPEED_MIN


class _FakeCarState:
  def __init__(self, left_blinker: bool = True, right_blinker: bool = False) -> None:
    self.vEgo = LANE_CHANGE_SPEED_MIN + 5.0
    self.leftBlinker = left_blinker
    self.rightBlinker = right_blinker
    self.leftBlindspot = False
    self.rightBlindspot = False
    self.steeringPressed = False
    self.steeringTorque = 0.0
    self.brakePressed = False


def _run(left_unavailable=False, right_unavailable=False, right_blinker=False):
  dh = DesireHelper()
  cs = _FakeCarState(left_blinker=not right_blinker, right_blinker=right_blinker)
  dh.update(cs, lateral_active=True, lane_change_prob=0.0,
            blindspot_unavailable_left=left_unavailable,
            blindspot_unavailable_right=right_unavailable)
  return dh, cs


class TestBlindspotUnavailableGate(OpenpilotTestCase):
  def test_an_unreadable_left_monitor_blocks_a_left_change(self):
    dh, _ = _run(left_unavailable=True)
    assert dh.lane_change_state == LaneChangeState.off

  def test_an_unreadable_right_monitor_blocks_a_right_change(self):
    dh, _ = _run(right_unavailable=True, right_blinker=True)
    assert dh.lane_change_state == LaneChangeState.off

  def test_only_the_requested_side_matters(self):
    dh, _ = _run(left_unavailable=True, right_blinker=True)
    assert dh.lane_change_state == LaneChangeState.preLaneChange

  def test_a_clear_monitor_still_enters(self):
    dh, _ = _run()
    assert dh.lane_change_state == LaneChangeState.preLaneChange

  def test_entering_does_not_cancel_an_ongoing_change(self):
    """It gates entry only: holding the signal through a change is normal."""
    dh, cs = _run()
    assert dh.lane_change_state == LaneChangeState.preLaneChange

    dh.update(cs, lateral_active=True, lane_change_prob=0.0,
              blindspot_unavailable_left=True)
    assert dh.lane_change_state == LaneChangeState.preLaneChange
