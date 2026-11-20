"""Bounded lead prediction uses observation time, geometry and current ego speed."""
import importlib.util
import inspect
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from openpilot.selfdrive.controls import radard


@pytest.fixture
def module():
  source = os.getenv('LEAD_BASELINE_SOURCE')
  if not source:
    return SimpleNamespace(LeadHold=radard.LeadPrediction)
  spec = importlib.util.spec_from_file_location('lead_hold_baseline', Path(source))
  old = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(old)
  return old


def _lead(d_rel=20.0, v_lead=10.0, v_ego=10.0, y_rel=0.0, prob=0.9, std=1.0):
  return {'present': True, 'dRel': d_rel, 'yRel': y_rel, 'vRel': v_lead-v_ego, 'vLead': v_lead,
          'vLeadK': v_lead, 'aLeadK': 0.0, 'aLeadTau': 0.3, 'modelProb': prob, 'radar': False,
          'radarTrackId': -1, 'rawProb': prob, 'dRelStd': std, 'yRelStd': 0.2, 'vLeadStd': 0.5,
          'pathOffset': y_rel}


def _update(hold, lead, t, v_ego=10.0, now=None):
  # Load the saved baseline in isolation; never rewrite production source to reverse-test.
  if 'model_time' not in inspect.signature(hold.update).parameters:
    fields = {k: v for k, v in lead.items() if k not in ('rawProb', 'dRelStd', 'yRelStd', 'vLeadStd', 'pathOffset')}
    return hold.update(fields)
  return hold.update(lead, model_time=t, v_ego=v_ego, now=now)


def test_never_invent_lead(module):
  assert not _update(module.LeadHold(), {'present': False}, 1.0)['present']


def test_half_second_budget_uses_time_not_invocations(module):
  hold = module.LeadHold()
  _update(hold, _lead(), 1.0)
  assert _update(hold, {'present': False}, 1.49)['present']
  assert not _update(hold, {'present': False}, 1.51)['present']


def test_duplicate_measurement_cannot_refresh_budget(module):
  hold = module.LeadHold()
  _update(hold, _lead(), 1.0)
  _update(hold, _lead(), 1.0, now=1.45)
  assert not _update(hold, {'present': False}, 1.51)['present']


def test_out_of_order_measurement_cannot_refresh_budget(module):
  hold = module.LeadHold()
  _update(hold, _lead(), 1.0)
  _update(hold, _lead(), 0.9, now=1.45)
  assert not _update(hold, {'present': False}, 1.51)['present']


def test_stale_initial_observation_cannot_start_prediction(module):
  hold = module.LeadHold()
  _update(hold, _lead(), 1.0, now=1.8)
  assert not _update(hold, {'present': False}, 1.85)['present']


def test_prediction_recomputes_relative_motion_when_ego_brakes(module):
  hold = module.LeadHold()
  _update(hold, _lead(v_lead=10.0, v_ego=12.0), 1.0, v_ego=12.0)
  result = _update(hold, {'present': False}, 1.2, v_ego=8.0)
  assert result['vRel'] == pytest.approx(2.0)
  assert result['dRel'] == pytest.approx(20.0)


def test_low_raw_probability_and_drift_do_not_refresh_trusted_target(module):
  hold = module.LeadHold()
  _update(hold, _lead(), 1.0)
  poor = _lead(35.0, prob=.3, std=15.0)
  poor['modelProb'] = .65
  _update(hold, poor, 1.4)
  lost = _update(hold, {'present': False}, 1.51)
  assert not lost['present']


def test_good_probability_with_bad_variance_does_not_refresh_budget(module):
  hold = module.LeadHold()
  _update(hold, _lead(), 1.0)
  _update(hold, _lead(35.0, prob=.85, std=20.0), 1.4)
  assert not _update(hold, {'present': False}, 1.51)['present']


def test_obvious_cutout_releases_retention_after_two_fresh_observations(module):
  hold = module.LeadHold()
  _update(hold, _lead(y_rel=.3), 1.0)
  _update(hold, _lead(y_rel=3.0), 1.1)
  _update(hold, _lead(y_rel=3.0), 1.2)
  result = _update(hold, {'present': False}, 1.25)
  assert not result['present']


def test_far_lead_is_not_predicted(module):
  hold = module.LeadHold()
  _update(hold, _lead(46.0), 1.0)
  assert not _update(hold, {'present': False}, 1.1)['present']


def test_departing_lead_releases_at_distance_limit(module):
  hold = module.LeadHold()
  _update(hold, _lead(44.0, v_lead=30.0), 1.0)
  assert not _update(hold, {'present': False}, 1.4)['present']


def test_state_age_and_identity_survive_prediction():
  hold = radard.LeadPrediction(track_id=7)
  measured = _update(hold, _lead(), 1.0)
  predicted = _update(hold, {'present': False}, 1.2)
  uncertain = _update(hold, {'present': False}, 1.6)
  assert measured['observationState'] == 'measured'
  assert predicted['observationState'] == 'predicted'
  assert uncertain['observationState'] == 'uncertain'
  assert predicted['observationAge'] == pytest.approx(.2)
  assert uncertain['observationAge'] == pytest.approx(.6)
  assert {x['visionTrackId'] for x in (measured, predicted, uncertain)} == {7}


def test_slot_swap_preserves_identity_and_two_distinct_targets():
  tracker = radard.LeadTracker()
  first = tracker.update([_lead(20.0), _lead(35.0)], 1.0, 10.0)
  swapped = tracker.update([_lead(35.0), _lead(20.0)], 1.05, 10.0)
  assert [l['visionTrackId'] for l in first] == [l['visionTrackId'] for l in swapped]
  assert len({l['visionTrackId'] for l in swapped}) == 2
  assert all(l['observationMatched'] for l in swapped)


def test_duplicate_slots_publish_only_one_target():
  tracker = radard.LeadTracker()
  leads = tracker.update([_lead(20.0), _lead(20.1)], 1.0, 10.0)
  assert sum(l['present'] for l in leads) == 1
  assert leads[1]['releaseReason'] == 'duplicate'


def test_lead_two_is_predicted_and_closer_cut_in_reorders_slots():
  tracker = radard.LeadTracker()
  previous = tracker.update([_lead(20.0), _lead(35.0)], 1.0, 10.0)
  leads = tracker.update([_lead(20.0), {'present': False}], 1.1, 10.0)
  assert leads[1]['observationState'] == 'predicted'
  assert leads[1]['visionTrackId'] == previous[1]['visionTrackId']
  leads = tracker.update([_lead(12.0), _lead(20.0)], 1.2, 10.0)
  assert leads[0]['dRel'] == 12.0
  assert leads[0]['visionTrackId'] not in {l['visionTrackId'] for l in previous}
  assert leads[1]['visionTrackId'] == previous[0]['visionTrackId']


def test_malformed_variance_cannot_seed_prediction():
  hold = radard.LeadPrediction()
  _update(hold, _lead(std=float('nan')), 1.0)
  assert not _update(hold, {'present': False}, 1.1)['present']


def test_current_uncertain_observation_does_not_remove_existing_braking_target():
  hold = radard.LeadPrediction()
  observed = _update(hold, _lead(12.0, v_lead=2.0, std=12.0), 1.0)
  assert observed['present']
  assert observed['dRel'] == 12.0
  assert observed['observationState'] == 'uncertain'
  assert not _update(hold, {'present': False}, 1.1)['present']


@pytest.mark.parametrize('d1,d2,v1,v2,a1,a2,ego', [
  (20.0, 21.0, 10.0, 8.0, -1.0, -3.0, 10.0),
  (20.0, 19.0, 8.0, 10.0, 2.0, -4.0, 10.0),
  (1.0, 1.5, 2.0, 0.0, -8.0, 0.0, 14.0),
  (4.0, 5.0, 9.0, 7.0, -8.0, 2.0, 15.0),
])
def test_duplicate_merging_never_weakens_real_mpc_obstacle(d1, d2, v1, v2, a1, a2, ego):
  import numpy as np
  from openpilot.cereal import log
  from openpilot.selfdrive.controls.lib.longitudinal_mpc_lib.long_mpc import LongitudinalMpc, get_stopped_equivalence_factor

  mpc = object.__new__(LongitudinalMpc)
  mpc.x0 = np.array([0.0, ego, 0.0])
  first, second = _lead(d1, v1, ego), _lead(d2, v2, ego)
  first['aLeadK'], second['aLeadK'] = a1, a2
  first['aLeadTau'], second['aLeadTau'] = .3, 1.5
  result = radard.LeadTracker().update([first, second], 1.0, ego)

  def obstacle(lead):
    msg = log.RadarState.LeadData.new_message(**radard._lead_fields(lead))
    trajectory = mpc.process_lead(msg)
    return trajectory[:, 0] + get_stopped_equivalence_factor(trajectory[:, 1])

  before = np.minimum(obstacle(first), obstacle(second))
  after = np.minimum.reduce([obstacle(lead) for lead in result if lead['present']])
  np.testing.assert_array_less(after, before + 1e-5)


def test_prediction_retains_previously_measured_braking():
  hold = radard.LeadPrediction()
  measured = _lead(v_lead=8.0)
  measured['aLeadK'] = -2.0
  _update(hold, measured, 1.0)
  predicted = _update(hold, {'present': False}, 1.2)
  assert predicted['vLead'] < 8.0
  assert predicted['aLeadK'] < -1.9


def test_path_relative_cutout_does_not_confuse_normal_bend():
  hold = radard.LeadPrediction()
  first = _lead(y_rel=2.5)
  first['pathOffset'] = .2
  _update(hold, first, 1.0)
  turning = _lead(y_rel=3.2)
  turning['pathOffset'] = .3
  assert _update(hold, turning, 1.1)['present']


def test_lead_tracker_expires_with_no_new_model_timestamp():
  tracker = radard.LeadTracker()
  tracker.update([_lead()], 1.0, 10.0)
  result = tracker.update([_lead()], 1.0, 10.0, now=1.6)
  assert not any(lead['present'] for lead in result)


def test_cutout_stays_released_until_target_returns_to_path():
  tracker = radard.LeadTracker()
  tracker.update([_lead()], 1.0, 10.0)
  first = tracker.update([_lead(y_rel=3.0)], 1.1, 10.0)
  second = tracker.update([_lead(y_rel=3.0)], 1.2, 10.0)
  lost = tracker.update([], 1.25, 10.0)
  assert first[0]['present'] and second[0]['present']
  assert second[0]['releaseReason'] == lost[0]['releaseReason'] == 'cutOut'
  assert not lost[0]['present']


def test_path_jump_alone_cannot_release_a_physical_near_lead():
  tracker = radard.LeadTracker()
  tracker.update([_lead(3.0, y_rel=.6)], 1.0, 0.0)
  path_jump = _lead(3.0, y_rel=.7)
  path_jump['pathOffset'] = 4.0
  tracker.update([path_jump], 1.05, 0.0)
  result = tracker.update([path_jump], 1.1, 0.0)
  assert result[0]['present']
  assert result[0]['releaseReason'] != 'cutOut'


def test_radar_update_publishes_diagnostics_on_both_slots():
  from openpilot.cereal import log

  class SubMaster(dict):
    seen = {'modelV2': True}
    logMonoTime = {'modelV2': 1000000000, 'carState': 1000000000}
    recv_frame = {'carState': 1}

    def all_checks(self):
      return True

  model = log.ModelDataV2.new_message()
  model.velocity.x = [10.0]
  model.position.x, model.position.y = [0.0, 50.0], [0.0, 0.0]
  model.init('leadsV3', 2)
  for index, lead in enumerate(model.leadsV3):
    lead.prob = .9
    lead.x, lead.y, lead.v, lead.a = [21.52 + index * 15.0], [0.0], [10.0], [0.0]
    lead.xStd, lead.yStd, lead.vStd = [1.0], [.2], [.5]
  cp = SimpleNamespace(brand='byd', flags=0)
  radar = radard.RadarD(cp, SimpleNamespace(flags=0))
  sm = SubMaster(modelV2=model, carState=SimpleNamespace(vEgo=10.0))
  radar.update(sm, SimpleNamespace(points=[], errors={}), now=1.0)
  assert radar.radar_state.leadOne.observationState == 'measured'
  assert radar.radar_state.leadTwo.observationState == 'measured'
  assert radar.radar_state.leadOne.visionTrackId != radar.radar_state.leadTwo.visionTrackId


@pytest.fixture
def production_module():
  source = os.getenv('LEAD_BASELINE_SOURCE')
  if not source:
    return radard
  spec = importlib.util.spec_from_file_location('lead_production_baseline', Path(source))
  old = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(old)
  return old


def test_production_retention_does_not_shorten_existing_acquisition(production_module):
  hold = production_module.LeadHold()
  _update(hold, _lead(), 1.0)
  accepted = _lead(25.0, prob=.3, std=15.0)
  accepted['modelProb'] = .65
  _update(hold, accepted, 1.4)
  assert _update(hold, {'present': False}, 1.8)['present']


def test_production_retention_uses_real_elapsed_time(production_module):
  hold = production_module.LeadHold()
  _update(hold, _lead(), 1.0)
  assert not _update(hold, {'present': False}, 1.51)['present']


def test_production_duplicate_does_not_refresh_credit(production_module):
  hold = production_module.LeadHold()
  _update(hold, _lead(), 1.0)
  _update(hold, _lead(), 1.0, now=1.4)
  assert not _update(hold, {'present': False}, 1.51)['present']


def test_production_prediction_uses_current_ego_motion(production_module):
  hold = production_module.LeadHold()
  _update(hold, _lead(v_lead=10.0, v_ego=12.0), 1.0, v_ego=12.0)
  result = _update(hold, {'present': False}, 1.2, v_ego=8.0)
  assert result['vRel'] == pytest.approx(2.0)
  assert result['dRel'] == pytest.approx(20.0)


def _radar_fixture():
  from openpilot.cereal import log

  class SubMaster(dict):
    seen = {'modelV2': True}
    logMonoTime = {'modelV2': 1000000000, 'carState': 1000000000}
    recv_frame = {'carState': 1}
    valid = True

    def all_checks(self):
      return self.valid

  model = log.ModelDataV2.new_message()
  model.velocity.x = [10.0]
  model.position.x, model.position.y = [0.0, 50.0], [0.0, 0.0]
  model.init('leadsV3', 2)
  for lead in model.leadsV3:
    lead.prob = .9
    lead.x, lead.y, lead.v, lead.a = [21.52], [0.0], [10.0], [0.0]
    lead.xStd, lead.yStd, lead.vStd = [1.0], [.2], [.5]
  cp = SimpleNamespace(brand='byd', flags=0)
  return radard.RadarD(cp, cp), SubMaster(modelV2=model, carState=SimpleNamespace(vEgo=10.0)), SimpleNamespace(points=[], errors={})


def test_production_preserves_both_current_slots_and_labels_shadow():
  radar, sm, rr = _radar_fixture()
  radar.update(sm, rr, now=1.0)
  assert radar.radar_state.leadOne.present and radar.radar_state.leadTwo.present
  assert radar.radar_state.leadOne.observationShadow and radar.radar_state.leadTwo.observationShadow
  assert radar.radar_state.leadOne.visionTrackId == radar.radar_state.leadTwo.visionTrackId
  assert radar.radar_state.leadOne.observationDRel == pytest.approx(20.0)


def test_production_invalid_submaster_clears_prediction_credit():
  radar, sm, rr = _radar_fixture()
  radar.update(sm, rr, now=1.0)
  sm.valid = False
  radar.update(sm, rr, now=1.1)
  assert radar.lead_hold._lead is None
  assert not radar.lead_tracker_shadow.tracks
  assert not radar.radar_state.leadOne.present


def test_production_uses_live_clock_when_messages_freeze(monkeypatch):
  radar, sm, rr = _radar_fixture()
  monkeypatch.setattr(radard.time, 'monotonic', lambda: 1.0)
  radar.update(sm, rr)
  assert radar.radar_state.leadOne.present
  monkeypatch.setattr(radard.time, 'monotonic', lambda: 1.6)
  radar.update(sm, rr)
  assert not radar.radar_state_valid
  assert radar.lead_hold._lead is None
  assert not radar.radar_state.leadOne.present


def test_offline_replay_leads_passes_real_timestamp_ego_and_absolute_speed(monkeypatch):
  from openpilot.tools.car_porting import byd_scc_lead_ab as current_tool

  tool = current_tool
  source = os.getenv('LEAD_TOOL_BASELINE_SOURCE')
  if source:
    spec = importlib.util.spec_from_file_location('lead_tool_baseline', Path(source))
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)

  def message(kind, timestamp, value):
    return SimpleNamespace(logMonoTime=int(timestamp * 1e9), which=lambda: kind, **{kind: value})

  messages = [message('carState', .99, SimpleNamespace(vEgo=10.0)),
              message('radarState', 1.0, SimpleNamespace(leadOne=SimpleNamespace(present=True)))]
  times = [1.0, 1.05, 1.1, 1.3, 1.7]
  for index, timestamp in enumerate(times):
    model_lead = SimpleNamespace(prob=.9 if index == 0 else 0.0, x=[21.52], v=[12.0])
    model = SimpleNamespace(leadsV3=[model_lead], velocity=SimpleNamespace(x=[10.0]))
    messages.append(message('modelV2', timestamp, model))
  observed = []

  class ProbeHold(radard.LeadHold):
    def update(self, lead, model_time, v_ego, now=None):
      observed.append((model_time, v_ego, lead.get('vLead')))
      return super().update(lead, model_time, v_ego, now)

  monkeypatch.setattr(tool, 'LeadHold', ProbeHold)
  monkeypatch.setattr(tool, 'segments', lambda route: ['synthetic'])
  monkeypatch.setattr(tool, 'LogReader', lambda path: messages)
  result = tool.replay_leads('synthetic')
  assert result['before']['present'] == 3
  assert result['after']['present'] == 4
  assert [sample[0] for sample in observed] == times
  assert all(sample[1] == 10.0 for sample in observed)
  assert [sample[2] for sample in observed[:3]] == [12.0] * 3
