#!/usr/bin/env python3
"""Tests for braking to the stop line on a red/yellow light.

The requirement is symmetric and both halves matter:

* red or yellow over ego's own lane -> brake to a stop short of the lamp
* before commitment, missing or unconfirmed evidence must not add a stop
* after commitment, missing evidence alone must not release a stopped vehicle

The closed-loop test is the one that actually pins the behaviour. Checking the
formula in isolation would pass even if the profile overshoots the stop line,
which is the one outcome that matters on the road.
"""

import time

import pytest

from openpilot.selfdrive.controls.lib.longitudinal_planner import (
  EG_LIGHT_MIN_FRAMES,
  EG_LIGHT_MIN_FRAMES_FAR,
  EG_LIGHT_NEAR_DISTANCE_M,
  LIGHT_STOP_COMMIT_TIMEOUT_S,
  LIGHT_STOP_HOLD_SPEED,
  LIGHT_STOP_LINE_OFFSET_M,
  LIGHT_STOP_MIN_DISTANCE_M,
  LongitudinalPlanner,
)
from opendbc.car.interfaces import ACCEL_MIN
from openpilot.selfdrive.controls.lib.longitudinal_mpc_lib.long_mpc import LongitudinalPlanSource


class _FakeSM(dict):
  def __init__(self, message=None, seen=True, valid=True):
    super().__init__()
    self.seen = {'yoloObjects': seen}
    self.valid = {'yoloObjects': valid}
    if message is not None:
      self['yoloObjects'] = message


class _Light:
  def __init__(self, state="", frames=0, distance=0.0, is_stop_line=False):
    self.egoLightState = state
    self.egoLightFrames = frames
    self.egoLightDistance = distance
    self.egoLightIsStopLine = is_stop_line


def _light(state="red", distance=30.0, frames=None, is_stop_line=False):
  """A light whose agreement count satisfies the far-range requirement."""
  if frames is None:
    frames = EG_LIGHT_MIN_FRAMES_FAR if distance > EG_LIGHT_NEAR_DISTANCE_M else EG_LIGHT_MIN_FRAMES
  return _Light(state, frames, distance, is_stop_line)


def _planner(light_stop=True, offset=LIGHT_STOP_LINE_OFFSET_M):
  """A bare planner with only the switch, offset and latch state set."""
  planner = object.__new__(LongitudinalPlanner)
  planner.light_stop = light_stop
  planner.light_stop_offset = offset
  planner.light_stop_committed = False
  planner.light_stop_distance = -1.0
  planner.light_stop_silent_s = 0.0
  planner.dt = 0.05
  return planner


def _candidate(sm, v_ego, light_stop=True):
  return _planner(light_stop)._light_stop_candidate(sm, v_ego)


class TestItBrakesForRedAndYellow:
  @pytest.mark.parametrize("state", ["red", "yellow"])
  def test_a_confirmed_light_produces_a_braking_candidate(self, state):
    candidate = _candidate(_FakeSM(_light(state, 40.0)), 12.0)
    assert candidate is not None
    accel, source, _ = candidate
    assert source == LongitudinalPlanSource.trafficLight
    assert accel < 0.0

  def test_braking_is_stronger_when_closer(self):
    far = _candidate(_FakeSM(_light("red", 50.0)), 12.0)[0]
    near = _candidate(_FakeSM(_light("red", 20.0)), 12.0)[0]
    assert near < far

  def test_braking_is_stronger_when_faster(self):
    slow = _candidate(_FakeSM(_light("red", 40.0)), 6.0)[0]
    fast = _candidate(_FakeSM(_light("red", 40.0)), 15.0)[0]
    assert fast < slow

  def test_it_never_commands_more_than_the_actuator_can_do(self):
    accel = _candidate(_FakeSM(_light("red", 2.0)), 25.0)[0]
    assert accel == pytest.approx(ACCEL_MIN)

  def test_it_never_commands_acceleration(self):
    for distance in (5.0, 20.0, 60.0):
      for speed in (0.5, 5.0, 20.0):
        accel = _candidate(_FakeSM(_light("red", distance)), speed)[0]
        assert accel <= 0.0, (distance, speed, accel)

  def test_below_the_hold_speed_it_asks_to_hold(self):
    accel, source, should_stop = _candidate(_FakeSM(_light("red", 8.0)), LIGHT_STOP_HOLD_SPEED - 0.1)
    assert accel == pytest.approx(ACCEL_MIN)
    assert should_stop is True
    assert source == LongitudinalPlanSource.trafficLight


class TestItAddsNothingOtherwise:
  """Every uncertain case must return None - that is what "green means go" is."""

  def test_green_adds_nothing(self):
    assert _candidate(_FakeSM(_Light("green", 10, 30.0)), 12.0) is None

  def test_wait_on_and_off_add_nothing(self):
    for state in ("off", "wait_on"):
      assert _candidate(_FakeSM(_Light(state, 10, 30.0)), 12.0) is None, state

  def test_an_empty_state_adds_nothing(self):
    assert _candidate(_FakeSM(_Light("", 0, 0.0)), 12.0) is None

  def test_too_few_agreeing_frames_add_nothing(self):
    assert _candidate(_FakeSM(_Light("red", 2, 30.0)), 12.0) is None

  def test_a_missing_or_invalid_service_adds_nothing(self):
    assert _candidate(_FakeSM(None, seen=False), 12.0) is None
    assert _candidate(_FakeSM(None, valid=False), 12.0) is None

  def test_an_unknown_distance_slows_but_does_not_stop(self):
    # -1 means "cannot be ranged"; there is no stop line to aim at, but the car
    # must still reduce speed towards the low-speed stop-line window.
    cand = _candidate(_FakeSM(_Light("red", 4, -1.0)), 12.0)
    assert cand is not None
    assert cand[0] > ACCEL_MIN
    assert cand[2] is False

  def test_a_light_beyond_the_useful_range_adds_nothing(self):
    assert _candidate(_FakeSM(_light("red", 200.0)), 12.0) is None

  def test_the_switch_being_off_adds_nothing(self):
    assert _candidate(_FakeSM(_light("red", 30.0)), 12.0, light_stop=False) is None

  def test_a_malformed_message_adds_nothing(self):
    class Broken:
      @property
      def egoLightState(self):
        raise ValueError("nope")

    assert _candidate(_FakeSM(Broken()), 12.0) is None


class TestItActuallyStopsInTime:
  """Closed loop: drive the candidate from a realistic approach and check the stop.

  Checking the formula alone would pass even if the profile overshoots the stop
  line. This integrates the commanded acceleration forward until the car stops and
  asserts it stops before the target, for a range of speeds and distances.
  """

  @staticmethod
  def _run(v0, distance, dt=0.01):
    """Drive the candidate forward from an approach; returns (v, d_to_light, seconds)."""
    planner = _planner()
    v, d, t = v0, distance, 0.0
    for _ in range(8000):
      candidate = planner._light_stop_candidate(_FakeSM(_light("red", d)), v)
      if candidate is None:
        break
      v = max(0.0, v + candidate[0] * dt)
      d = max(0.0, d - v * dt)
      t += dt
      if v <= 0.0:
        break
    return v, d, t

  @pytest.mark.parametrize("v0,distance", [(8.0, 25.0), (12.0, 40.0), (17.0, 55.0)])
  def test_with_enough_room_it_stops_at_the_stop_line(self, v0, distance):
    """The cases where max braking is sufficient must stop near the 5 m offset."""
    v, d, _ = self._run(v0, distance)
    assert v == pytest.approx(0.0, abs=1e-6)
    # Stopped short of the lamp, close to the intended offset - not 10 m early.
    assert LIGHT_STOP_MIN_DISTANCE_M <= d <= 3 * LIGHT_STOP_LINE_OFFSET_M, (v0, distance, d)

  @pytest.mark.parametrize("v0,distance", [(12.0, 15.0), (17.0, 25.0), (17.0, 40.0)])
  def test_too_fast_too_close_it_brakes_as_hard_as_it_can(self, v0, distance):
    """When the room is not there, the car must spend everything it has.

    It cannot stop before the lamp - no controller can - and once the lamp is behind
    the car there is nothing left to stop for, so the candidate correctly goes away.
    What matters is that it used ACCEL_MIN getting there rather than easing off.
    """
    planner = _planner()
    v, d = v0, distance
    peak = 0.0
    for _ in range(8000):
      candidate = planner._light_stop_candidate(_FakeSM(_light("red", d)), v)
      if candidate is None:
        break
      peak = min(peak, candidate[0])
      v = max(0.0, v + candidate[0] * 0.01)
      d = max(0.0, d - v * 0.01)
      if v <= 0.0:
        break
    assert peak == pytest.approx(ACCEL_MIN, abs=0.01), (v0, distance, peak)
    assert d >= 0.0

  def test_it_does_not_creep_the_last_metres(self):
    """A pure -v^2/(2d) profile took 13.7 s and crawled the end at walking pace."""
    _, _, seconds = self._run(12.0, 40.0)
    assert seconds < 10.0, seconds

  def test_a_light_that_turns_green_mid_approach_stops_braking(self):
    """Confirmed green releases a moving stop with bounded acceleration change."""
    planner = _planner()
    v, d = 12.0, 30.0
    for _ in range(50):
      a = planner._light_stop_candidate(_FakeSM(_light("red", d)), v)[0]
      v = max(0.0, v + a * 0.01)
      d = max(0.0, d - v * 0.01)
    assert v < 12.0
    previous = a
    for _ in range(100):
      candidate = planner._light_stop_candidate(_FakeSM(_Light("green", 10, d)), v)
      if candidate is None:
        break
      assert 0 <= candidate[0] - previous <= 0.081
      previous = candidate[0]
    else:
      pytest.fail('confirmed green did not release the moving constraint')
    assert not planner.light_stop_committed


class TestACommittedStopSurvivesLosingSight:
  """A dropped detection must not hand control back to cruise at a red light.

  Without this, a few frames of lost detection remove the candidate, the cruise
  candidate wins the merge again, and the car accelerates towards a light that is
  still red. The commitment is held while the state is unknown and released on an
  explicit green - or after a timeout, so a dead camera cannot trap the car.
  """

  def _confirmed(self, planner, distance=35.0, v=10.0):
    assert planner._light_stop_candidate(_FakeSM(_light("red", distance)), v) is not None

  def test_losing_the_light_keeps_the_brake_on(self):
    planner = _planner()
    self._confirmed(planner)
    for _ in range(10):
      assert planner._light_stop_candidate(_FakeSM(None, seen=False), 10.0) is not None

  def test_an_explicit_green_releases_the_commitment_then_ramps_out(self):
    planner = _planner()
    self._confirmed(planner)
    assert planner._light_stop_candidate(_FakeSM(_Light("green", 10, 30.0)), 10.0) is not None
    assert not planner.light_stop_committed
    for _ in range(100):
      candidate = planner._light_stop_candidate(_FakeSM(None, seen=False), 10.0)
      assert not planner.light_stop_committed
    assert candidate is None

  def test_off_and_wait_on_do_not_mean_confirmed_green(self):
    for state in ("off", "wait_on"):
      planner = _planner()
      self._confirmed(planner)
      candidate = planner._light_stop_candidate(_FakeSM(_Light(state, 10, 30.0)), 10.0)
      assert candidate is not None and candidate[0] < 0, state
      assert planner.light_stop_committed

  def test_while_still_moving_it_gives_up_after_the_timeout(self):
    """Braking blindly forever is not an option either; the timeout covers motion."""
    planner = _planner()
    self._confirmed(planner, distance=60.0, v=8.0)
    held = 0
    for _ in range(400):          # 400 * 0.05 s = 20 s of silence
      if planner._light_stop_candidate(_FakeSM(None, seen=False), 8.0) is None:
        break
      held += 1
    assert LIGHT_STOP_COMMIT_TIMEOUT_S < held * planner.dt < LIGHT_STOP_COMMIT_TIMEOUT_S + 5.0
    assert not planner.light_stop_committed

  def test_once_stopped_it_holds_indefinitely(self):
    """A red lasts longer than any timeout, and creeping into the junction is worse.

    Losing the camera while stopped must park the car, not release it. The driver's
    escape is the accelerator, which disengages longitudinal control on this car in
    the same frame.
    """
    planner = _planner()
    self._confirmed(planner, distance=30.0, v=10.0)
    for _ in range(2000):         # 100 s of silence at a standstill
      candidate = planner._light_stop_candidate(_FakeSM(None, seen=False), 0.0)
      assert candidate is not None
      assert candidate[0] == pytest.approx(ACCEL_MIN)
      assert candidate[2] is True

  def test_an_explicit_green_still_releases_a_stopped_car(self):
    planner = _planner()
    self._confirmed(planner, distance=30.0, v=10.0)
    for _ in range(100):
      planner._light_stop_candidate(_FakeSM(None, seen=False), 0.0)
    assert planner._light_stop_candidate(_FakeSM(_Light("green", 10, 30.0)), 0.0) is None
    assert planner.light_stop_committed is False

  def test_the_dead_reckoned_range_closes_with_our_own_travel(self):
    """Frozen range would make the profile stop early; it must keep converging."""
    planner = _planner()
    self._confirmed(planner, distance=40.0, v=10.0)
    start = planner.light_stop_distance
    for _ in range(20):           # 1 s of silence at 10 m/s
      planner._light_stop_candidate(_FakeSM(None, seen=False), 10.0)
    assert planner.light_stop_distance == pytest.approx(start - 10.0 * 20 * planner.dt, abs=0.5)

  def test_a_fresh_confirmation_restarts_the_timeout(self):
    planner = _planner()
    self._confirmed(planner)
    for _ in range(20):
      planner._light_stop_candidate(_FakeSM(None, seen=False), 10.0)
    assert planner.light_stop_silent_s > 0.0
    assert planner._light_stop_candidate(_FakeSM(_light("red", 25.0)), 10.0) is not None
    assert planner.light_stop_silent_s == pytest.approx(0.0)

  def test_the_switch_being_turned_off_releases_a_commitment(self):
    planner = _planner()
    self._confirmed(planner)
    planner.light_stop = False
    assert planner._light_stop_candidate(_FakeSM(_light("red", 20.0)), 10.0) is None
    assert planner.light_stop_committed is False


class TestYellowIsADecisionNotAnOrder:
  """A yellow that cannot be stopped for must be driven through, not slammed for.

  At 50-60 km/h the geometry demands more deceleration than the actuator can give at
  almost any distance. Braking anyway means hitting ACCEL_MIN, failing to stop, and
  coming to rest in the junction while inviting a rear-end collision.
  """

  def test_yellow_without_the_room_to_stop_adds_nothing(self):
    # 16.7 m/s with 20 m to the lamp needs about 9 m/s^2 - three times ACCEL_MIN.
    assert _candidate(_FakeSM(_light("yellow", 20.0)), 16.7) is None

  def test_an_infeasible_yellow_does_not_commit_on_the_first_frame(self):
    # The commit used to be set before the feasibility check; the next frame then
    # skipped it via already_committed and slammed the brake anyway.
    planner = _planner()
    sm = _FakeSM(_light("yellow", 20.0))
    assert planner._light_stop_candidate(sm, 16.7) is None
    assert planner.light_stop_committed is False
    assert planner._light_stop_candidate(sm, 16.7) is None
    assert planner.light_stop_committed is False

  def test_yellow_with_room_still_stops(self):
    assert _candidate(_FakeSM(_light("yellow", 60.0)), 16.7) is not None

  def test_red_always_tries_even_when_it_cannot_succeed(self):
    """Red is mandatory: attempting and failing beats not attempting."""
    candidate = _candidate(_FakeSM(_light("red", 20.0)), 16.7)
    assert candidate is not None
    assert candidate[0] == pytest.approx(ACCEL_MIN)

  def test_a_committed_stop_is_not_abandoned_when_it_becomes_infeasible(self):
    """Decide once. Releasing mid-stop would accelerate the car at the light."""
    planner = _planner()
    assert planner._light_stop_candidate(_FakeSM(_light("yellow", 60.0)), 16.7) is not None
    # Now far too close to stop - but we already committed, so keep braking.
    for distance in (30.0, 20.0, 12.0, 8.0):
      assert planner._light_stop_candidate(_FakeSM(_Light("yellow", 4, distance)), 16.7) is not None, distance

  def test_the_feasibility_boundary_matches_the_actuator(self):
    from openpilot.selfdrive.controls.lib.longitudinal_planner import LIGHT_STOP_YELLOW_FEASIBLE_DECEL
    assert LIGHT_STOP_YELLOW_FEASIBLE_DECEL == pytest.approx(ACCEL_MIN)


class TestFarLightsNeedMoreEvidence:
  """Range is generous on purpose; the cost is paid in evidence, not distance.

  Engaging late makes stopping harder - at 16 m/s a first sighting at 40 m needs
  5 m/s^2, more than the car has - so the max range stays at 60 m. What that costs
  is exposure to a misdetection far away, and a real light stays red for seconds
  while a false one rarely survives six agreeing frames.
  """

  def test_three_frames_is_enough_up_close(self):
    assert _candidate(_FakeSM(_Light("red", 3, 30.0)), 10.0) is not None

  def test_three_frames_is_not_enough_far_away(self):
    assert _candidate(_FakeSM(_Light("red", 3, 55.0)), 10.0) is None

  def test_six_frames_is_enough_far_away(self):
    assert _candidate(_FakeSM(_Light("red", 6, 55.0)), 10.0) is not None

  def test_the_boundary_is_the_declared_near_distance(self):
    assert _candidate(_FakeSM(_Light("red", 3, EG_LIGHT_NEAR_DISTANCE_M)), 10.0) is not None
    assert _candidate(_FakeSM(_Light("red", 3, EG_LIGHT_NEAR_DISTANCE_M + 1.0)), 10.0) is None

  def test_a_committed_stop_is_not_released_by_the_extra_requirement(self):
    """Already braking is already committed; the frame count only gates entry."""
    planner = _planner()
    assert planner._light_stop_candidate(_FakeSM(_Light("red", 6, 55.0)), 10.0) is not None
    for _ in range(5):
      assert planner._light_stop_candidate(_FakeSM(_Light("red", 3, 55.0)), 10.0) is not None


class TestAnUnknownRangeIsNotTheSameAsNoVerdict:
  """``-1`` means "the light is there but I cannot range it", not "no light".

  The two are separated by the state string, and each consumer wants the opposite
  answer: a stop profile needs a range, but a *reason to keep the model's stop*
  does not. Getting these two backwards would either stop the car on a light it
  cannot measure, or release the brake at a red light it merely failed to range.
  """

  def test_an_unranged_red_does_not_start_a_full_stop(self):
    cand = _candidate(_FakeSM(_Light("red", 6, -1.0)), 10.0)
    assert cand is not None
    assert cand[0] > ACCEL_MIN
    assert cand[2] is False
    # At or below the low-speed target, hold instead of letting cruise re-accelerate.
    hold = _candidate(_FakeSM(_Light("red", 6, -1.0)), 7.0)
    assert hold is not None and hold[0] == 0.0 and hold[2] is False

  def test_an_unranged_red_still_keeps_an_existing_commitment(self):
    planner = _planner()
    assert planner._light_stop_candidate(_FakeSM(_Light("red", 6, 30.0)), 10.0) is not None
    # The range disappears but the verdict stays: keep braking.
    for _ in range(10):
      assert planner._light_stop_candidate(_FakeSM(_Light("red", 6, -1.0)), 10.0) is not None

  def test_an_unranged_red_still_counts_as_a_reason_for_the_model_stop(self):
    from openpilot.selfdrive.controls.lib.longitudinal_planner import LongitudinalPlanner
    planner = object.__new__(LongitudinalPlanner)
    assert planner._ego_light_stop_reason(_FakeSM(_Light("red", 6, -1.0))) is True

  def test_no_state_at_all_is_not_a_reason(self):
    from openpilot.selfdrive.controls.lib.longitudinal_planner import LongitudinalPlanner
    planner = object.__new__(LongitudinalPlanner)
    assert planner._ego_light_stop_reason(_FakeSM(_Light("", 0, -1.0))) is False

  def test_the_two_meanings_are_told_apart_by_the_state_string(self):
    from openpilot.selfdrive.controls.lib.longitudinal_planner import LongitudinalPlanner
    planner = object.__new__(LongitudinalPlanner)
    # Same distance, different state: one is information, the other is silence.
    assert planner._ego_light(_FakeSM(_Light("red", 6, -1.0))) == ("red", 6, -1.0, False)
    assert planner._ego_light(_FakeSM(_Light("", 6, -1.0))) is None


def test_a_stale_detection_is_treated_as_no_information():
    # SubMaster.valid keeps the last message's flag when the publisher dies; the
    # age gate is what stops a frozen red light from commanding a stop forever.
    from openpilot.selfdrive.controls.lib.longitudinal_planner import LongitudinalPlanner
    planner = object.__new__(LongitudinalPlanner)
    sm = _FakeSM(_Light("red", 6, 20.0))
    sm.recv_time = {'yoloObjects': time.monotonic() - 1.0}
    assert planner._ego_light(sm) is None


class TestStopLineDistanceUsesASmallGap:
  """A stop-line distance is not a lamp distance: the car should stop at the line,
  not five metres before it. The adapter marks the message so the planner can pick
  the 0.5 m gap without changing the user's TrafficLightStopOffset."""

  def test_stop_line_distance_brakes_later_than_lamp_distance(self):
    v = 8.0
    lamp = _planner()._light_stop_candidate(_FakeSM(_light("red", 20.0, 6)), v)
    line = _planner()._light_stop_candidate(
      _FakeSM(_light("red", 20.0, 6, is_stop_line=True)), v)
    assert lamp is not None and line is not None
    # The stop-line target is 19.5 m ahead instead of 15 m, so it needs less
    # deceleration; the command must be correspondingly gentler.
    assert line[0] > lamp[0]

  def test_stop_line_commit_keeps_the_small_gap(self):
    planner = _planner()
    assert planner._light_stop_candidate(
      _FakeSM(_light("red", 30.0, 6, is_stop_line=True)), 10.0) is not None
    assert planner.light_stop_committed is True
    assert planner.light_stop_distance == 30.0


def test_stopline_commit_keeps_the_gap_when_the_range_disappears():
    planner = _planner()
    assert planner._light_stop_candidate(
      _FakeSM(_light("red", 30.0, 6, is_stop_line=True)), 10.0) is not None
    assert planner.light_stop_is_stop_line is True
    # The stop-line source drops out; the committed stop must keep its 0.5 m gap.
    assert planner._light_stop_candidate(_FakeSM(_Light("red", 6, -1.0)), 10.0) is not None
    assert planner.light_stop_is_stop_line is True


def test_state_only_red_slows_a_50_kmh_approach():
    cand = _candidate(_FakeSM(_Light("red", 6, -1.0)), 13.9)
    assert cand is not None
    assert cand[0] < 0.0
    assert cand[0] >= -1.5
    assert cand[2] is False


def test_state_only_green_does_not_slow():
    assert _candidate(_FakeSM(_Light("green", 6, -1.0)), 13.9) is None


def test_state_only_red_holds_at_or_below_the_speed_window():
    cand = _candidate(_FakeSM(_Light("red", 6, -1.0)), 0.0)
    assert cand is not None
    assert cand[0] == 0.0
    assert cand[2] is False
