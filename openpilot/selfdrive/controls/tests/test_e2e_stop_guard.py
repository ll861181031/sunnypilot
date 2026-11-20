#!/usr/bin/env python3
"""Tests for the e2e straight-line-stop guard in the longitudinal planner.

The guard exists because the candidate pool is merged with min(), so the model's raw
action.desiredAcceleration wins whenever it is the most negative value. The model rolls
its 10 s plan out to a standstill at crossings and junctions even when there is nothing
to stop for, and that prior used to be commanded as braking. These tests pin down which
combinations count as "no reason to stop" and -- just as important -- that every
uncertain case keeps the model's vote.
"""

from openpilot.selfdrive.controls.lib.longitudinal_planner import (
  E2E_STOP_GUARD_NO_LEAD_DIST,
  E2E_STOP_GUARD_STRAIGHT_YAW,
  E2E_STOP_GUARD_TERMINAL_V,
  e2e_stop_lacks_a_reason,
)


class TestTheBlockedCombination:
  """The exact signature measured on the empty-crossing cases."""

  def test_a_straight_stop_with_nothing_ahead_is_flagged(self):
    assert e2e_stop_lacks_a_reason(-3.10, False, 0.0, 0.002, False, 0.0)

  def test_a_far_lead_does_not_count_as_a_reason(self):
    assert e2e_stop_lacks_a_reason(-1.77, False, 0.2, 0.012, True, E2E_STOP_GUARD_NO_LEAD_DIST + 1.0)

  def test_the_measured_cases_are_all_flagged(self):
    # (a_e2e, smoothed terminal_v, |yaw| change, lead dRel or None) from the corpus scan.
    measured = [
      (-1.71, 0.0, 0.008, None),
      (-1.77, 0.1, 0.012, None),
      (-1.70, 0.2, 0.009, None),
      (-2.43, 0.2, 0.003, None),
      (-1.77, 0.1, 0.005, None),
      (-1.58, 0.2, 0.000, 68.0),
      (-1.78, 0.0, 0.002, None),
      (-3.10, 0.3, 0.002, 54.0),
    ]
    for a, v, yaw, lead in measured:
      assert e2e_stop_lacks_a_reason(a, False, v, yaw, lead is not None, lead or 0.0), (a, v, yaw, lead)


class TestEveryUncertainCaseKeepsTheModelsVote:
  def test_a_mild_brake_is_left_alone(self):
    assert not e2e_stop_lacks_a_reason(-0.3, False, 0.0, 0.002, False, 0.0)

  def test_a_throttle_request_is_left_alone(self):
    assert not e2e_stop_lacks_a_reason(0.8, False, 0.0, 0.002, False, 0.0)

  def test_an_explicit_stop_decision_is_trusted(self):
    assert not e2e_stop_lacks_a_reason(-3.10, True, 0.0, 0.002, False, 0.0)

  def test_a_plan_that_keeps_moving_is_not_a_stop(self):
    assert not e2e_stop_lacks_a_reason(-1.5, False, E2E_STOP_GUARD_TERMINAL_V + 0.5, 0.002, False, 0.0)

  def test_a_turn_is_a_reason_to_slow_down(self):
    assert not e2e_stop_lacks_a_reason(-1.5, False, 0.0, E2E_STOP_GUARD_STRAIGHT_YAW + 0.05, False, 0.0)

  def test_a_near_lead_is_a_reason_to_stop(self):
    assert not e2e_stop_lacks_a_reason(-1.5, False, 0.0, 0.002, True, E2E_STOP_GUARD_NO_LEAD_DIST - 1.0)

  def test_missing_model_data_falls_through(self):
    # A missing rollout must not be read as "the plan ends at zero velocity".
    assert not e2e_stop_lacks_a_reason(-3.0, False, None, 0.002, False, 0.0)
    assert not e2e_stop_lacks_a_reason(-3.0, False, 0.0, None, False, 0.0)


class TestTheSmoothingWindow:
  """The rollout terminal speed swings by metres per second between frames."""

  def _gate_series(self, raw):
    from collections import deque
    from openpilot.selfdrive.controls.lib.longitudinal_planner import E2E_STOP_GUARD_VEL_WINDOW as W
    window = deque(maxlen=W)
    out = []
    for v in raw:
      window.append(v)
      out.append(e2e_stop_lacks_a_reason(-2.5, False, min(window), 0.003, False, 0.0))
    return out

  def test_the_measured_flicker_does_not_toggle_the_gate(self):
    # Around the green-light crossing the rollout ended at 13.75 / 2.07 / 13.50 / 3.33 m/s
    # within a quarter second. Gating on a single frame would open and close every other
    # frame; once the smoothed minimum drops it must stay down.
    gated = self._gate_series([13.75, 12.59, 2.07, 7.96, 13.50, 3.33, 2.52, 5.05, 3.23, 1.18, 0.46])
    assert all(not gated[i] or gated[i + 1] for i in range(len(gated) - 1)), gated
    assert gated[-1]

  def test_the_gate_releases_once_the_low_samples_roll_out(self):
    gated = self._gate_series([0.0] + [20.0] * 12)
    assert gated[0]
    # The single low sample leaves the window after E2E_STOP_GUARD_VEL_WINDOW frames.
    from openpilot.selfdrive.controls.lib.longitudinal_planner import E2E_STOP_GUARD_VEL_WINDOW as W
    assert not gated[W]
    assert not gated[-1]


class TestTheThresholdsAreTheMeasuredOnes:
  def test_no_lead_distance_covers_the_measured_far_leads(self):
    # The flagged cases had their nearest vehicle between 51 and 103 m away.
    assert E2E_STOP_GUARD_NO_LEAD_DIST <= 51.0

  def test_straight_yaw_still_admits_the_measured_curves(self):
    # 6c-4 and 7c-0 measured 0.108 / 0.113 rad; those must stay trusted.
    assert E2E_STOP_GUARD_STRAIGHT_YAW < 0.108

  def test_terminal_velocity_admits_plans_that_still_roll(self):
    # 47-10 / 4c-0 / 65-7 ended at 5.2 / 11.4 / 12.3 m/s and must not be flagged.
    assert E2E_STOP_GUARD_TERMINAL_V < 5.2


# --- signal-light veto -------------------------------------------------------
# The guard's own comment records why it is default-off: the same "straight stop,
# nothing ahead" signature appears when stopping for a red light, and the model
# carries no signal state. These tests pin the one-directional fix - a light can
# only ever preserve a stop, never release one.


def test_a_signal_light_reason_preserves_the_stop():
  # Everything about this frame says "suppress", except that our light is red.
  assert e2e_stop_lacks_a_reason(-3.10, False, 0.0, 0.002, False, 0.0)
  assert not e2e_stop_lacks_a_reason(-3.10, False, 0.0, 0.002, False, 0.0,
                                     signal_light_reason=True)


def test_the_reason_never_creates_a_stop_that_was_not_flagged():
  """It must be impossible for the veto to flip False -> True."""
  for a in (-0.3, 0.8, -3.10):
    for should_stop in (False, True):
      for v in (0.0, E2E_STOP_GUARD_TERMINAL_V + 0.5):
        for yaw in (0.002, E2E_STOP_GUARD_STRAIGHT_YAW + 0.01):
          for lead in (None, 5.0, E2E_STOP_GUARD_NO_LEAD_DIST + 10.0):
            base = e2e_stop_lacks_a_reason(a, should_stop, v, yaw, lead is not None, lead or 0.0)
            with_reason = e2e_stop_lacks_a_reason(a, should_stop, v, yaw, lead is not None,
                                                  lead or 0.0, signal_light_reason=True)
            assert not (with_reason and not base), (a, should_stop, v, yaw, lead)


def test_default_keeps_the_original_verdict():
  assert e2e_stop_lacks_a_reason(-3.10, False, 0.0, 0.002, False, 0.0,
                                 signal_light_reason=False) == \
         e2e_stop_lacks_a_reason(-3.10, False, 0.0, 0.002, False, 0.0)


class _FakeSM(dict):
  def __init__(self, message=None, seen=True, valid=True):
    super().__init__()
    self.seen = {'yoloObjects': seen}
    self.valid = {'yoloObjects': valid}
    if message is not None:
      self['yoloObjects'] = message


class _Light:
  def __init__(self, state="", frames=0, distance=0.0):
    self.egoLightState = state
    self.egoLightFrames = frames
    self.egoLightDistance = distance


def _reason(sm):
  from openpilot.selfdrive.controls.lib.longitudinal_planner import LongitudinalPlanner
  return LongitudinalPlanner._ego_light_stop_reason(object.__new__(LongitudinalPlanner), sm)


def test_planner_reads_a_red_light_as_a_reason():
  assert _reason(_FakeSM(_Light("red", 4, 42.0)))


def test_planner_reads_a_yellow_light_as_a_reason():
  assert _reason(_FakeSM(_Light("yellow", 3, 30.0)))


def test_planner_does_not_treat_green_as_a_reason():
  assert not _reason(_FakeSM(_Light("green", 6, 20.0)))


def test_planner_requires_a_settled_light():
  from openpilot.selfdrive.controls.lib.longitudinal_planner import EG_LIGHT_MIN_FRAMES
  assert not _reason(_FakeSM(_Light("red", EG_LIGHT_MIN_FRAMES - 1, 40.0)))


def test_planner_ignores_a_far_light():
  from openpilot.selfdrive.controls.lib.longitudinal_planner import EG_LIGHT_MAX_DISTANCE_M
  assert not _reason(_FakeSM(_Light("red", 5, EG_LIGHT_MAX_DISTANCE_M + 5.0)))


def test_planner_accepts_unknown_distance():
  assert _reason(_FakeSM(_Light("red", 5, -1.0)))


def test_planner_is_silent_when_yolod_is_absent():
  assert not _reason(_FakeSM(None, seen=False))
  assert not _reason(_FakeSM(_Light("red", 5, 20.0), valid=False))


def test_planner_is_silent_on_a_malformed_message():
  class Broken:
    egoLightState = None
    egoLightFrames = "many"
    egoLightDistance = "far"
  assert not _reason(_FakeSM(Broken()))


def test_planner_and_yolod_agree_on_the_policy_constants():
  """The planner keeps its own copy to avoid depending on an optional daemon."""
  from openpilot.selfdrive.controls.lib.longitudinal_planner import (
    EG_LIGHT_MAX_DISTANCE_M,
    EG_LIGHT_MIN_FRAMES,
    EG_LIGHT_REASON_STATES,
  )
  from openpilot.sunnypilot.yolod.light_veto import (
    MAX_REASON_DISTANCE_M,
    MIN_AGREEING_FRAMES,
    REASON_STATES,
  )
  assert tuple(EG_LIGHT_REASON_STATES) == tuple(REASON_STATES)
  assert EG_LIGHT_MIN_FRAMES == MIN_AGREEING_FRAMES
  assert EG_LIGHT_MAX_DISTANCE_M == MAX_REASON_DISTANCE_M
