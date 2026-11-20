"""Uncertain leads can request zero acceleration only inside a fixed loss window."""
import pytest

from openpilot.selfdrive.controls.lib.lead_uncertainty import LeadUncertaintyGuard


def lead(state='measured', age=0.0, track=1, d=20.0, reason=''):
  return {'present': state in ('measured', 'predicted'), 'observationState': state,
          'observationAge': age, 'visionTrackId': track, 'dRel': d, 'releaseReason': reason}


def update(guard, leads, t, **kwargs):
  return guard.update(leads, model_time=t, v_ego=kwargs.pop('v_ego', 10.0), enabled=kwargs.pop('enabled', True), **kwargs)


def test_missing_history_cannot_arm_guard():
  assert not update(LeadUncertaintyGuard(), [lead('uncertain', .6)], 1.6).active


def test_prediction_then_uncertainty_share_one_nonrenewable_budget():
  guard = LeadUncertaintyGuard()
  assert not update(guard, [lead()], 1.0).active
  assert update(guard, [lead('predicted', .2)], 1.2).active
  assert update(guard, [lead('uncertain', .6)], 1.6).active
  assert update(guard, [lead('uncertain', 1.25)], 2.25).active
  assert not update(guard, [lead('uncertain', 1.3)], 2.3).active


def test_uncertain_observation_cannot_reset_age_to_extend_guard():
  guard = LeadUncertaintyGuard()
  update(guard, [lead()], 1.0)
  assert not update(guard, [lead('uncertain', .05)], 2.3).active


def test_duplicate_measured_message_cannot_refresh_deadline():
  guard = LeadUncertaintyGuard()
  update(guard, [lead()], 1.0)
  assert not update(guard, [lead()], 1.0).active
  assert not update(guard, [lead('uncertain', .6)], 2.3).active


@pytest.mark.parametrize('reason', ['cutOut', 'away', 'far', 'invalidClock', 'invalidDistance'])
def test_positive_release_evidence_clears_guard(reason):
  guard = LeadUncertaintyGuard()
  update(guard, [lead()], 1.0)
  assert not update(guard, [lead('uncertain', .1, reason=reason)], 1.1).active
  assert not update(guard, [lead('uncertain', .2)], 1.2).active


@pytest.mark.parametrize('override', [{'enabled': False}, {'driver_override': True}, {'standstill': True}, {'v_ego': 0.0}])
def test_stop_takeover_and_disable_clear_history(override):
  guard = LeadUncertaintyGuard()
  update(guard, [lead()], 1.0)
  assert not update(guard, [lead('uncertain', .1)], 1.1, **override).active
  assert not update(guard, [lead('uncertain', .2)], 1.2).active


def test_restart_after_stop_requires_new_measured_lead():
  guard = LeadUncertaintyGuard()
  update(guard, [lead()], 1.0)
  update(guard, [lead()], 1.1, standstill=True)
  assert not update(guard, [lead('uncertain', .2)], 1.2).active
  update(guard, [lead()], 1.3)
  assert update(guard, [lead('predicted', .1)], 1.4).active


def test_slot_swap_and_two_lead_loss_keep_identity():
  guard = LeadUncertaintyGuard()
  update(guard, [lead(track=1), lead(track=2, d=35.0)], 1.0)
  result = update(guard, [lead(track=2, d=35.0), lead('predicted', .1, track=1)], 1.1)
  assert result.active and result.track_id == 1


def test_new_identity_does_not_inherit_old_guard():
  guard = LeadUncertaintyGuard()
  update(guard, [lead()], 1.0)
  assert not update(guard, [lead('uncertain', .1, track=2)], 1.1).active


def test_no_acceleration_candidate_keeps_more_negative_braking():
  guard = LeadUncertaintyGuard()
  update(guard, [lead()], 1.0)
  result = update(guard, [lead('uncertain', .6)], 1.6)
  assert result.active and result.acceleration == 0.0
  assert min(1.2, result.acceleration) == 0.0
  assert min(-2.0, result.acceleration) == -2.0


def test_far_unknown_or_old_schema_leads_cannot_arm():
  guard = LeadUncertaintyGuard()
  update(guard, [lead(d=46.0), {'present': True, 'dRel': 10.0}], 1.0)
  assert not update(guard, [lead('uncertain', .1)], 1.1).active


def test_stale_radar_observation_cannot_arm():
  guard = LeadUncertaintyGuard()
  update(guard, [lead(age=.4)], 1.0)
  assert not update(guard, [lead('uncertain', .5)], 1.1).active


@pytest.mark.parametrize('override', [{'enabled': False}, {'driver_override': True}, {'standstill': True}])
def test_override_clears_history_even_on_duplicate_timestamp(override):
  guard = LeadUncertaintyGuard()
  update(guard, [lead()], 1.0)
  update(guard, [lead()], 1.0, **override)
  assert not update(guard, [lead('uncertain', .2)], 1.2).active


def test_backward_clock_clears_credit():
  guard = LeadUncertaintyGuard()
  update(guard, [lead()], 1.0)
  update(guard, [lead()], .9)
  assert not update(guard, [lead('uncertain', .2)], 1.2).active


def test_duplicate_model_keeps_existing_guard_without_renewal_until_real_deadline():
  guard = LeadUncertaintyGuard()
  update(guard, [lead()], 1.0)
  assert update(guard, [lead('uncertain', .6)], 1.6).active
  assert update(guard, [lead('uncertain', .6)], 1.6, now=1.8).active
  assert not update(guard, [lead('uncertain', .6)], 1.6, now=2.3).active


def test_shadow_distance_does_not_borrow_actual_slot_distance():
  guard = LeadUncertaintyGuard()
  measured = dict(lead(d=10.0), observationShadow=True, observationDRel=60.0)
  update(guard, [measured], 1.0)
  assert not update(guard, [lead('uncertain', .2)], 1.2).active


@pytest.mark.parametrize('radar_delay', [0.0, .05, .1, .2])
def test_real_prediction_to_guard_does_not_count_processing_delay_twice(radar_delay):
  from openpilot.selfdrive.controls.radard import LeadPrediction

  observation = {'present': True, 'dRel': 20.0, 'yRel': 0.0, 'vRel': 0.0, 'vLead': 10.0,
                 'vLeadK': 10.0, 'aLeadK': 0.0, 'aLeadTau': .3, 'modelProb': .9, 'radar': False,
                 'radarTrackId': -1, 'rawProb': .9, 'dRelStd': 1.0, 'yRelStd': .2, 'vLeadStd': .5,
                 'pathOffset': 0.0}
  tracker = LeadPrediction(track_id=7)
  measured = tracker.update(observation, model_time=1.0, v_ego=10.0, now=1.0 + radar_delay)
  measured.update(observationShadow=True, observationDRel=measured['dRel'])
  guard = LeadUncertaintyGuard()
  update(guard, [measured], 1.0, now=1.2)
  predicted = tracker.update({'present': False}, model_time=1.3, v_ego=10.0, now=1.4)
  predicted.update(observationShadow=True, observationDRel=predicted['dRel'])
  decision = update(guard, [predicted], 1.3, now=1.4)
  assert decision.active and decision.age == pytest.approx(.4)
  assert update(guard, [predicted], 1.3, now=2.25).active
  assert not update(guard, [predicted], 1.3, now=2.26).active
