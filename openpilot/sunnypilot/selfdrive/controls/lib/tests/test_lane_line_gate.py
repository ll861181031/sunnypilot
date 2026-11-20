"""Tests for the optional lane-marking gate in DesireHelper.

The gate is off by default (``LaneLineCheck=0``), so every test that does not pass
the parameter must keep the old behavior. The enabled modes mirror CarrotPilot's
LaneLineCheck with one deliberate difference: mode 1 only blocks a *measured* solid
boundary, because the classifier answers unknown for whole stretches and failing
closed there silently disabled lane changes. Mode 2 keeps the strict behavior
(solid/unknown allowed only with driver torque).
"""
import pytest

from openpilot.common.test import OpenpilotTestCase
from openpilot.selfdrive.controls.lib.desire_helper import DesireHelper, LaneChangeState
from openpilot.sunnypilot.selfdrive.controls.lib.lane_turn_desire import LANE_CHANGE_SPEED_MIN


class _FakeCarState:
  """Only the fields DesireHelper.update touches on the lane change path."""

  def __init__(self, left_blinker: bool = True, right_blinker: bool = False) -> None:
    self.vEgo = LANE_CHANGE_SPEED_MIN + 5.0
    self.leftBlinker = left_blinker
    self.rightBlinker = right_blinker
    self.leftBlindspot = False
    self.rightBlindspot = False
    self.steeringPressed = False
    self.steeringTorque = 0.0
    self.brakePressed = False


def _run(left_lane_line, right_lane_line, lane_line_check, steering_pressed=False, steering_torque=0.0):
  dh = DesireHelper()
  cs = _FakeCarState()
  cs.steeringPressed = steering_pressed
  cs.steeringTorque = steering_torque
  dh.update(cs, lateral_active=True, lane_change_prob=0.0,
            left_lane_line=left_lane_line, right_lane_line=right_lane_line,
            lane_line_check=lane_line_check)
  return dh, cs


@pytest.mark.parametrize("line_type, check, entry_allowed, requires_torque", [
  (0, 0, True, False),    # check disabled: dashed, unchanged
  (1, 0, True, False),    # check disabled: solid, unchanged
  (0, 1, True, False),    # check 1: dashed allowed
  (1, 1, False, True),    # check 1: measured solid blocked
  (2, 1, False, True),    # check 1: any other measured marking blocks too
  (-1, 1, True, False),   # check 1: unknown is "no information", not blocked
  (5, 1, True, False),    # CarrotPilot DLM inner dashed counts as dashed
  (0, 2, True, False),    # check 2: dashed allowed without torque
  (1, 2, True, True),     # check 2: solid allowed, torque required
  (-1, 2, True, True),    # check 2: unknown allowed, torque required
])
def test_lane_line_gate_matrix(line_type, check, entry_allowed, requires_torque):
  assert DesireHelper.lane_line_gate(line_type, check) == (entry_allowed, requires_torque)


class TestLaneLineGate(OpenpilotTestCase):
  def test_default_keeps_solid_lines_working(self):
    """Without the parameter, a solid line must behave exactly as before."""
    dh, _ = _run(1, 1, 0)
    assert dh.lane_change_state == LaneChangeState.preLaneChange

  def test_check_one_blocks_a_solid_boundary(self):
    dh, _ = _run(1, 1, 1)
    assert dh.lane_change_state == LaneChangeState.off

  def test_check_one_allows_an_unmeasured_boundary(self):
    """Unknown must not disable the feature.

    Measured on route 00000057: 39 of 40 sampled frames came back unknown. Failing
    closed there blocked every lane change on that road without any alert, which is
    worse than falling back to the model geometry the car already had.
    """
    dh, _ = _run(-1, -1, 1)
    assert dh.lane_change_state == LaneChangeState.preLaneChange

  def test_check_one_still_blocks_a_measured_solid(self):
    dh, _ = _run(1, 1, 1)
    assert dh.lane_change_state == LaneChangeState.off

  def test_check_one_allows_a_dashed_boundary(self):
    dh, _ = _run(0, 0, 1)
    assert dh.lane_change_state == LaneChangeState.preLaneChange

  def test_check_two_enters_on_a_solid_but_waits_for_torque(self):
    dh, cs = _run(1, 1, 2)
    assert dh.lane_change_state == LaneChangeState.preLaneChange

    # No driver torque: automatic lane change must stay blocked on the solid line.
    dh.update(cs, lateral_active=True, lane_change_prob=0.0,
              left_lane_line=1, right_lane_line=1, lane_line_check=2)
    assert dh.lane_change_state == LaneChangeState.preLaneChange

    # Driver torque in the requested direction releases it.
    cs.steeringPressed = True
    cs.steeringTorque = 1.0  # positive is left in this signal convention
    dh.update(cs, lateral_active=True, lane_change_prob=0.0,
              left_lane_line=1, right_lane_line=1, lane_line_check=2)
    assert dh.lane_change_state == LaneChangeState.laneChangeStarting

  def test_check_two_enters_on_unknown_but_waits_for_torque(self):
    dh, cs = _run(-1, -1, 2)
    assert dh.lane_change_state == LaneChangeState.preLaneChange
    dh.update(cs, lateral_active=True, lane_change_prob=0.0,
              left_lane_line=-1, right_lane_line=-1, lane_line_check=2)
    assert dh.lane_change_state == LaneChangeState.preLaneChange
