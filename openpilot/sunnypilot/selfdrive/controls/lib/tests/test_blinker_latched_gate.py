"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

A latched turn signal means the driver is turning, not changing lanes, so it must not
start the lane change sequence. Only cars whose stalk reports the difference set
CarStateSP.blinkerLatched (BYD, via bit0 of TURN_SIGNAL_SWITCH); every other car leaves
it false, which is why the default here has to keep lane changes working.
"""
from openpilot.common.test import OpenpilotTestCase
from openpilot.selfdrive.controls.lib.desire_helper import DesireHelper, LaneChangeState
from openpilot.sunnypilot.selfdrive.controls.lib.lane_turn_desire import LANE_CHANGE_SPEED_MIN


class _FakeCarState:
  """Only the fields DesireHelper.update touches on the lane change path."""

  def __init__(self, left_blinker: bool = False, right_blinker: bool = False) -> None:
    # Comfortably above LANE_CHANGE_SPEED_MIN, which is 20 mph.
    self.vEgo = LANE_CHANGE_SPEED_MIN + 5.0
    self.leftBlinker = left_blinker
    self.rightBlinker = right_blinker
    self.leftBlindspot = False
    self.rightBlindspot = False
    # The driver is not fighting the wheel; the lane change has to come from the
    # blinker alone for this test to say anything.
    self.steeringPressed = False
    self.steeringTorque = 0.0
    self.brakePressed = False


def _run(blinker_latched, left_blinker=True):
  dh = DesireHelper()
  cs = _FakeCarState(left_blinker=left_blinker, right_blinker=not left_blinker)
  dh.update(cs, lateral_active=True, lane_change_prob=0.0, blinker_latched=blinker_latched)
  return dh


class TestBlinkerLatchedGate(OpenpilotTestCase):
  def test_a_light_tap_starts_the_lane_change_sequence(self):
    dh = _run(blinker_latched=False)
    assert dh.lane_change_state == LaneChangeState.preLaneChange

  def test_a_latched_press_does_not(self):
    dh = _run(blinker_latched=True)
    assert dh.lane_change_state == LaneChangeState.off

  def test_the_default_keeps_other_cars_working(self):
    """A car that cannot tell tap from press omits the argument entirely."""
    dh = DesireHelper()
    cs = _FakeCarState(left_blinker=True)
    dh.update(cs, lateral_active=True, lane_change_prob=0.0)
    assert dh.lane_change_state == LaneChangeState.preLaneChange

  def test_a_press_after_a_tap_does_not_cancel_a_started_sequence(self):
    """Holding the signal through the manoeuvre is normal, so the gate is entry only."""
    dh = DesireHelper()
    cs = _FakeCarState(left_blinker=True)
    dh.update(cs, lateral_active=True, lane_change_prob=0.0, blinker_latched=False)
    assert dh.lane_change_state == LaneChangeState.preLaneChange
    # The driver keeps holding it: the state must not fall back to off.
    for _ in range(10):
      dh.update(cs, lateral_active=True, lane_change_prob=0.0, blinker_latched=True)
      assert dh.lane_change_state != LaneChangeState.off
