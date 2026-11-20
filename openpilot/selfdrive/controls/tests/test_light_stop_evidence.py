import time

import pytest

from openpilot.selfdrive.controls.tests.test_light_stop import _FakeSM, _Light, _light, _planner


def nav(state='red', *, relevant=True, intersection='junction-a', target='line-a', frames=2, distance=15.0):
  light = _Light(state, frames, distance, True)
  light.egoLightSource = 'navigation'
  light.egoLightSession = 'session-a'
  light.egoLightSequence = 12
  light.egoLightReceivedMonoTime = int(time.monotonic() * 1e9)
  light.egoLightIntersectionId = intersection
  light.egoLightDirection = 'straight'
  light.egoLightRelevant = relevant
  light.egoLightConfirmations = frames
  light.stopLineConfirmations = 3
  light.stopLineTargetId = target
  light.stopLineCaptureMonoTime = int(time.monotonic() * 1e9)
  light.stopLineFrameId = 100
  return light


@pytest.mark.parametrize('state,frames', [('green', 1), ('off', 10), ('wait_on', 10)])
def test_unconfirmed_release_does_not_drop_committed_braking(state, frames):
  planner = _planner()
  planner._light_stop_candidate(_FakeSM(_light()), 8.0)
  candidate = planner._light_stop_candidate(_FakeSM(_Light(state, frames, 30.0)), 8.0)
  assert candidate is not None and candidate[0] < 0
  assert planner.light_stop_committed


def test_exhausted_committed_distance_does_not_remove_stop_candidate():
  planner = _planner()
  planner._light_stop_candidate(_FakeSM(_light(distance=2.0)), 8.0)
  for _ in range(10):
    candidate = planner._light_stop_candidate(_FakeSM(None, seen=False), 8.0)
    assert candidate is not None and candidate[0] <= 0


def test_navigation_without_road_association_only_pre_slows():
  planner = _planner()
  candidate = planner._light_stop_candidate(_FakeSM(nav(relevant=False, frames=6)), 13.9)
  assert candidate is not None and candidate[2] is False
  assert not planner.light_stop_committed
  assert planner.light_stop_state == 'slow_only'


def test_unassociated_red_preserves_model_braking_without_starting_a_nav_stop():
  planner = _planner()
  assert planner._ego_light_stop_reason(_FakeSM(nav(relevant=False)))
  planner._light_stop_candidate(_FakeSM(nav(relevant=False)), 8.0)
  assert not planner.light_stop_committed
  assert planner._ego_light_stop_reason(_FakeSM(nav()))


def test_independent_navigation_and_stopline_confirmations_allow_approach():
  planner = _planner()
  candidate = planner._light_stop_candidate(_FakeSM(nav()), 6.0)
  assert candidate is not None and candidate[0] < 0
  assert planner.light_stop_committed
  assert planner.light_stop_state == 'approach'


def test_wrong_intersection_green_does_not_release_stopped_car():
  planner = _planner()
  planner._light_stop_candidate(_FakeSM(nav(frames=6)), 0.0)
  candidate = planner._light_stop_candidate(_FakeSM(nav('green', intersection='other', frames=6)), 0.0)
  assert candidate is not None and candidate[2]
  assert planner.light_stop_committed


def test_new_stopline_target_cannot_jump_existing_stop_farther_away():
  planner = _planner()
  planner._light_stop_candidate(_FakeSM(nav(frames=6)), 5.0)
  planner._light_stop_candidate(_FakeSM(nav(target='other-line', frames=6, distance=50.0)), 5.0)
  assert planner.light_stop_distance < 15.0
  assert planner.light_stop_state == 'degraded'


@pytest.mark.parametrize('received', [float('nan'), float('inf'), time.monotonic() + 10])
def test_nonfinite_or_future_receive_time_is_not_light_evidence(received):
  sm = _FakeSM(_light())
  sm.recv_time = {'yoloObjects': received}
  assert _planner()._ego_light(sm) is None


@pytest.mark.parametrize('distance', [float('nan'), float('inf'), float('-inf')])
def test_nonfinite_distance_never_creates_control_candidate(distance):
  assert _planner()._light_stop_candidate(_FakeSM(_light(distance=distance)), 10.0) is None


def test_moving_evidence_timeout_releases_acceleration_gradually():
  planner = _planner()
  last = planner._light_stop_candidate(_FakeSM(_light(distance=60.0)), 8.0)[0]
  degraded = False
  for _ in range(300):
    candidate = planner._light_stop_candidate(_FakeSM(None, seen=False), 8.0)
    degraded |= getattr(planner, 'light_stop_state', '') == 'degraded'
    if candidate is None:
      assert degraded and last >= 0.8
      break
    assert candidate[0] - last <= 0.081
    last = candidate[0]
  else:
    pytest.fail('missing evidence kept a control constraint forever while moving')


def test_disengagement_clears_old_stop_before_reengagement():
  planner = _planner()
  planner._light_stop_candidate(_FakeSM(nav(frames=6)), 0.0)
  candidate = planner._light_stop_for_control(_FakeSM(nav(frames=6)), 0.0, enabled=False)
  assert candidate is None and not planner.light_stop_committed
  assert planner._light_stop_for_control(_FakeSM(None, seen=False), 0.0, enabled=True) is None


def test_degraded_release_that_reaches_standstill_keeps_stop_hold():
  planner = _planner()
  planner._light_stop_candidate(_FakeSM(_light(distance=60.0)), 8.0)
  for _ in range(61):
    planner._light_stop_candidate(_FakeSM(None, seen=False), 8.0)
  candidate = planner._light_stop_candidate(_FakeSM(None, seen=False), 0.0)
  assert candidate is not None and candidate[2]
  assert planner.light_stop_state == 'stopped_hold'


def test_finished_degraded_release_cannot_hold_a_later_unrelated_stop():
  planner = _planner()
  planner._light_stop_candidate(_FakeSM(_light(distance=60.0)), 8.0)
  for _ in range(300):
    planner._light_stop_candidate(_FakeSM(None, seen=False), 8.0)
  assert planner._light_stop_candidate(_FakeSM(None, seen=False), 0.0) is None


def test_wrong_junction_green_cannot_release_a_stop_reached_during_degradation():
  planner = _planner()
  planner._light_stop_candidate(_FakeSM(nav(distance=60.0)), 8.0)
  for _ in range(61):
    planner._light_stop_candidate(_FakeSM(None, seen=False), 8.0)
  candidate = planner._light_stop_candidate(_FakeSM(nav('green', intersection='other')), 0.0)
  assert candidate is not None and candidate[2]
  assert planner.light_stop_state == 'stopped_hold'
