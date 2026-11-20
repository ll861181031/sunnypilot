from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from openpilot.selfdrive.modeld.test_nav_root_instruction_gate import Model
from openpilot.selfdrive.navigation.amap_phone_route import AmapPhoneRouteAdapter
from openpilot.selfdrive.navigation import navd
from openpilot.selfdrive.navigation.tests.test_amap_phone_pipeline import (
  NOW_NS, make_sample, model_runtime, pipeline as pipeline, queue_frame,
)


def action_sample(code, exit_number=3, **kwargs):
  sample = make_sample(**kwargs)
  navi = sample.native_after['_navi_info']
  for branch in navi['branches']:
    branch.update(maneuver_id_raw=code, exit_number_raw=exit_number)
  sample.native_before['_navi_info'] = deepcopy(navi)
  sample.broadcast['records']['10001']['fields'].update(
    ICON=code, NEW_ICON=code, ROUNG_ABOUT_NUM=exit_number, ROUND_ALL_NUM=0)
  return sample


@pytest.fixture(autouse=True)
def root_mode(monkeypatch):
  monkeypatch.setenv('SUNNYPILOT_AMAP_ROOT_NAV', '1')


@pytest.mark.parametrize(('code', 'kind', 'modifier', 'command'), [
  (5, 'turn', 'slight right', 9), (7, 'turn', 'sharp right', 11),
  (11, 'roundabout', '', 26), (12, 'exit roundabout', '', 27),
])
def test_fixed_icon_semantics_reach_model(pipeline, monkeypatch, code, kind, modifier, command):
  publication = pipeline.adapter.accept(action_sample(code), now_ns=NOW_NS)
  assert publication is not None, pipeline.adapter.reason
  assert publication.instruction is not None, pipeline.adapter.instruction_reason
  assert (publication.instruction['type'], publication.instruction['modifier']) == (kind, modifier)
  pipeline.sm.publish(publication, NOW_NS)
  runtime = model_runtime(pipeline, monkeypatch)
  inputs = runtime.inputs_for_model(pipeline.sm, 0.0, Model())
  assert inputs['nav_map'].any() and runtime.map_valid
  assert runtime.instruction_valid, runtime.evidence()['instructionReason']
  assert np.flatnonzero(inputs['navigation'][0, :-1]).tolist() == [command]
  assert inputs['navigation'][0, -1] == pytest.approx(0.79)


@pytest.mark.parametrize(('kind', 'modifier', 'command'), [
  ('turn', 'sharp right', 11), ('roundabout', '', 26), ('exit roundabout', '', 27),
])
def test_model_independently_accepts_bound_new_semantics(pipeline, monkeypatch, kind, modifier, command):
  publication = replace(pipeline.publication, instruction={'type': kind, 'modifier': modifier, 'distance': 79.0})
  pipeline.sm.publish(publication, NOW_NS)
  runtime = model_runtime(pipeline, monkeypatch)
  vector = runtime.inputs_for_model(pipeline.sm, 0.0, Model())['navigation']
  assert np.flatnonzero(vector[0, :-1]).tolist() == [command]


@pytest.mark.parametrize(('code', 'kind'), [(11, 'roundabout'), (12, 'exit roundabout')])
def test_real_navd_publisher_keeps_roundabout_route_binding(pipeline, code, kind):
  sample = action_sample(code)
  events = []
  daemon = navd.Navd(SimpleNamespace(), SimpleNamespace(send=lambda name, msg: events.append((name, msg))),
                     phone_source=SimpleNamespace(latest=lambda: sample))
  daemon.step(NOW_NS / 1e9)
  assert [name for name, _ in events] == ['navRoute', 'navInstruction']
  route, instruction = events[0][1], events[1][1]
  assert route.valid and instruction.valid
  assert instruction.navInstruction.routeBinding.routeId == route.navRoute.routeBinding.routeId
  assert instruction.navInstruction.maneuverType == kind
  assert instruction.navInstruction.maneuverModifier == ''
  assert instruction.navInstruction.allManeuvers[0].type == kind


@pytest.mark.parametrize('code', [11, 12])
@pytest.mark.parametrize('exit_number', [1, 2, 3])
def test_roundabout_exit_is_preserved_without_using_zero_total_as_a_bound(code, exit_number):
  adapter = AmapPhoneRouteAdapter()
  publication = adapter.accept(action_sample(code, exit_number), now_ns=NOW_NS)
  assert publication.instruction is not None, adapter.instruction_reason
  assert publication.instruction['roundaboutExitRaw'] == exit_number


@pytest.mark.parametrize('code', [11, 12])
@pytest.mark.parametrize('fault', ['missing_exit', 'wrong_exit', 'zero_exit', 'negative_exit', 'bool_exit',
                                'float_exit', 'sentinel_exit', 'before_exit', 'before_action', 'before_missing',
                                'before_path', 'before_distance', 'stale_broadcast', 'old_icon', 'missing_binding'])
def test_roundabout_invalid_proof_keeps_geometry_but_blocks_action(code, fault):
  sample = action_sample(code)
  fields = sample.broadcast['records']['10001']['fields']
  before = sample.native_before['_navi_info']
  after = sample.native_after['_navi_info']
  if fault == 'missing_exit':
    fields.pop('ROUNG_ABOUT_NUM')
  elif fault == 'wrong_exit':
    fields['ROUNG_ABOUT_NUM'] = 2
  elif fault.endswith('_exit') and fault not in ('before_exit',):
    value = {'zero_exit': 0, 'negative_exit': -1, 'bool_exit': True,
             'float_exit': 3.0, 'sentinel_exit': 0xffffffff}[fault]
    fields['ROUNG_ABOUT_NUM'] = value
    for navi in (before, after):
      for branch in navi['branches']:
        branch['exit_number_raw'] = value
  elif fault == 'before_exit':
    before['branches'][0]['exit_number_raw'] = 2
  elif fault == 'before_action':
    before['branches'][0]['maneuver_id_raw'] = 12 if code == 11 else 11
  elif fault == 'before_missing':
    sample.native_before.pop('_navi_info')
  elif fault == 'before_path':
    before['path_identity_hash'] = 'different'
  elif fault == 'before_distance':
    before['branches'][0]['segment_remain_distance_raw'] += 1
  elif fault == 'stale_broadcast':
    sample.broadcast['records']['10001']['source_age_upper_ns'] = 2_000_000_001
  elif fault == 'old_icon':
    fields['ICON'] = 11 if code == 12 else 12
  elif fault == 'missing_binding':
    after['branches'][0]['segment_binding']['segment_object_verified'] = False
  adapter = AmapPhoneRouteAdapter()
  publication = adapter.accept(sample, now_ns=NOW_NS)
  assert publication is not None, adapter.reason
  assert publication.instruction is None


@pytest.mark.parametrize('code', [11, 12])
@pytest.mark.parametrize('change', ['none', 'exit_only', 'before_exit', 'action', 'route', 'stale'])
def test_roundabout_replay_cannot_change_accepted_exit_or_action(code, change):
  adapter = AmapPhoneRouteAdapter()
  first = adapter.accept(action_sample(code), now_ns=NOW_NS)
  assert first.instruction is not None, adapter.instruction_reason
  replay = action_sample(code, now_ns=NOW_NS + 200_000_000, sequence=2, path_id=456 if change == 'route' else 123)
  record = replay.broadcast['records']['10001']
  record.update(replayed=True, source_age_upper_ns=300_000_000)
  if change in ('exit_only', 'action'):
    for native in (replay.native_before, replay.native_after):
      for branch in native['_navi_info']['branches']:
        branch['exit_number_raw' if change == 'exit_only' else 'maneuver_id_raw'] = 2 if change == 'exit_only' else 12 if code == 11 else 11
    if change == 'exit_only':
      record['fields']['ROUNG_ABOUT_NUM'] = 2
    else:
      record['fields'].update(ICON=12 if code == 11 else 11, NEW_ICON=12 if code == 11 else 11)
  elif change == 'before_exit':
    replay.native_before['_navi_info']['branches'][0]['exit_number_raw'] = 2
  elif change == 'stale':
    record['source_age_upper_ns'] = 2_000_000_001
  publication = adapter.accept(replay, now_ns=NOW_NS + 200_000_000)
  assert publication is not None, adapter.reason
  if change == 'none':
    assert publication.instruction == first.instruction
    assert adapter.instruction_reason == 'accepted_fresh_replay_hold'
  else:
    assert publication.instruction is None


@pytest.mark.parametrize('code', [11, 12])
@pytest.mark.parametrize('transition', ['cancel', 'expire', 'change_route', 'gps_invalid', 'geometry_invalid'])
def test_roundabout_revoke_clears_model_input(pipeline, monkeypatch, code, transition):
  publication = pipeline.adapter.accept(action_sample(code), now_ns=NOW_NS)
  assert publication.instruction is not None
  pipeline.sm.publish(publication, NOW_NS)
  runtime = model_runtime(pipeline, monkeypatch)
  model = Model()
  assert runtime.inputs_for_model(pipeline.sm, 0.0, model)['navigation'].any()
  model._commit_nav_reset(model.pending_nav_resets())
  pipeline.clock[0] += 2_100_000_000 if transition == 'expire' else 100_000_000
  if transition == 'cancel':
    pipeline.adapter.withdraw(pipeline.clock[0], 'phone_navigation_stopped')
    pipeline.sm.publish(None, pipeline.clock[0])
  elif transition != 'expire':
    sample = action_sample(code, now_ns=pipeline.clock[0], sequence=2, elapsed_ns=10_100_000_000,
                           path_id=456 if transition == 'change_route' else 123)
    if transition == 'gps_invalid':
      sample.gps = replace(sample.gps, age_upper_ns=3_000_000_000)
    elif transition == 'geometry_invalid':
      sample.cache_bytes = b'broken'
      sample.selected = deepcopy(sample.selected)
      sample.selected.cache_sha256 = '0' * 64
    fresh = pipeline.adapter.accept(sample, now_ns=pipeline.clock[0])
    old = pipeline.sm['navInstruction']
    pipeline.sm.publish(fresh, pipeline.clock[0])
    if transition == 'change_route':
      pipeline.sm.data['navInstruction'] = old
      runtime._update_phone_route(pipeline.sm, pipeline.clock[0] / 1e9)
      queue_frame(runtime, pipeline, frame_id=43)
    else:
      assert fresh is None
  assert not runtime.inputs_for_model(pipeline.sm, 0.0, model)['navigation'].any()


@pytest.mark.parametrize('code', [4, 6, 8, 9, 10, 17, 18, 21, 22, 23, 24, 65, 66, 999])
def test_other_codes_remain_unverified(code):
  adapter = AmapPhoneRouteAdapter()
  publication = adapter.accept(action_sample(code), now_ns=NOW_NS)
  assert publication is not None
  assert publication.instruction is None


@pytest.mark.parametrize(('kind', 'modifier'), [('roundabout', 'right'), ('roundabout', 'left'),
                                               ('exit roundabout', '3'), ('rotary', ''), ('turn', '')])
def test_model_rejects_roundabout_aliases_or_invented_modifiers(pipeline, monkeypatch, kind, modifier):
  publication = replace(pipeline.publication, instruction={'type': kind, 'modifier': modifier, 'distance': 79.0})
  pipeline.sm.publish(publication, NOW_NS)
  runtime = model_runtime(pipeline, monkeypatch)
  assert not runtime.inputs_for_model(pipeline.sm, 0.0, Model())['navigation'].any()
