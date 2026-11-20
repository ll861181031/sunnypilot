from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from openpilot.selfdrive.modeld.test_nav_root_instruction_gate import Model
from openpilot.selfdrive.navigation import navd
from openpilot.selfdrive.navigation.amap_phone_route import AmapPhoneRouteAdapter
from openpilot.selfdrive.navigation.tests.test_amap_phone_pipeline import (
  NOW_NS, make_sample, model_runtime, pipeline as pipeline,
)


def uturn_sample(**kwargs):
  sample = make_sample(**kwargs)
  navi = sample.native_after['_navi_info']
  for branch in navi['branches']:
    branch.update(maneuver_id_raw=8, obtain_supported_raw=0)
  sample.native_before['_navi_info'] = deepcopy(navi)
  sample.broadcast['records']['10001']['fields'].update(ICON=8, NEW_ICON=8)
  return sample


@pytest.fixture(autouse=True)
def root_mode(monkeypatch):
  monkeypatch.setenv('SUNNYPILOT_AMAP_ROOT_NAV', '1')


def test_verified_unrenderable_uturn_reaches_left_uturn_slot(pipeline, monkeypatch):
  publication = pipeline.adapter.accept(uturn_sample(), now_ns=NOW_NS)
  assert publication is not None, pipeline.adapter.reason
  assert publication.instruction is not None, pipeline.adapter.instruction_reason
  assert (publication.instruction['type'], publication.instruction['modifier']) == ('turn', 'uturn')
  pipeline.sm.publish(publication, NOW_NS)
  runtime = model_runtime(pipeline, monkeypatch)
  inputs = runtime.inputs_for_model(pipeline.sm, 0.0, Model())
  assert inputs['nav_map'].any() and runtime.map_valid
  assert runtime.instruction_valid, runtime.evidence()['instructionReason']
  assert np.flatnonzero(inputs['navigation'][0, :-1]).tolist() == [13]
  assert inputs['navigation'][0, -1] == pytest.approx(.79)


def test_model_independently_accepts_bound_left_uturn(pipeline, monkeypatch):
  publication = replace(pipeline.publication, instruction={'type': 'turn', 'modifier': 'uturn', 'distance': 79.0})
  pipeline.sm.publish(publication, NOW_NS)
  runtime = model_runtime(pipeline, monkeypatch)
  inputs = runtime.inputs_for_model(pipeline.sm, 0.0, Model())
  assert inputs['nav_map'].any()
  assert np.flatnonzero(inputs['navigation'][0, :-1]).tolist() == [13]


def test_real_navd_binds_uturn_to_same_route(pipeline):
  events = []
  daemon = navd.Navd(SimpleNamespace(), SimpleNamespace(send=lambda name, msg: events.append((name, msg))),
                     phone_source=SimpleNamespace(latest=uturn_sample))
  daemon.step(NOW_NS / 1e9)
  assert [name for name, _ in events] == ['navRoute', 'navInstruction']
  route, instruction = events[0][1], events[1][1]
  assert route.valid and instruction.valid
  assert instruction.navInstruction.routeBinding.routeId == route.navRoute.routeBinding.routeId
  assert instruction.navInstruction.maneuverType == 'turn'
  assert instruction.navInstruction.maneuverModifier == 'uturn'


@pytest.mark.parametrize('replayed', [False, True])
@pytest.mark.parametrize('fault', [
  'before_missing', 'before_code', 'before_support', 'before_distance', 'before_bool_distance',
  'before_reason', 'before_path', 'before_exit', 'render_supported', 'support_bool',
  'link_action', 'point_action', 'future_segment', 'navi_position', 'exit_number', 'exit_angle',
  'branch_one', 'binding', 'null_binding', 'float_code', 'next_road', 'reason', 'broadcast_code', 'broadcast_stale',
])
def test_unverified_uturn_clears_only_action(replayed, fault):
  adapter = AmapPhoneRouteAdapter()
  first = adapter.accept(uturn_sample(), now_ns=NOW_NS)
  assert first is not None and first.instruction is not None, adapter.instruction_reason
  sample = uturn_sample(now_ns=NOW_NS + 200_000_000, sequence=2)
  before, after = sample.native_before['_navi_info'], sample.native_after['_navi_info']
  branch = after['branches'][0]
  record = sample.broadcast['records']['10001']
  record.update(sequence=119 if replayed else 120, replayed=replayed, source_age_upper_ns=300_000_000)
  if fault == 'before_missing':
    sample.native_before.pop('_navi_info')
  elif fault == 'before_code':
    before['branches'][0]['maneuver_id_raw'] = 9
  elif fault == 'before_support':
    before['branches'][0]['obtain_supported_raw'] = 1
  elif fault == 'before_distance':
    before['branches'][0]['segment_remain_distance_raw'] += 1
  elif fault == 'before_bool_distance':
    for navi in (before, after):
      navi['branches'][0]['segment_remain_distance_raw'] = 1
    before['branches'][0]['segment_remain_distance_raw'] = True
    record['fields']['SEG_REMAIN_DIS'] = 1
  elif fault == 'before_reason':
    before['reason'] = 'unavailable'
  elif fault == 'before_path':
    before['path_identity_hash'] = 'other'
  elif fault == 'before_exit':
    before['branches'][0]['exit_number_raw'] = 1
  elif fault in ('render_supported', 'support_bool'):
    branch['obtain_supported_raw'] = 1 if fault == 'render_supported' else False
  elif fault == 'link_action':
    branch['maneuver_link_raw'] = 0
  elif fault == 'point_action':
    branch['maneuver_point_raw'] = 0
  elif fault == 'future_segment':
    branch['maneuver_segment_raw'] = 1
  elif fault == 'navi_position':
    after['current_point'] = 1
  elif fault == 'exit_number':
    branch['exit_number_raw'] = 1
  elif fault == 'exit_angle':
    branch['exit_angle_raw'] = 1
  elif fault == 'branch_one':
    after['branch_flag_raw'] = 1
  elif fault == 'binding':
    branch['segment_binding']['segment_object_verified'] = False
  elif fault == 'null_binding':
    branch['segment_binding'] = None
  elif fault == 'float_code':
    branch.update(maneuver_id_raw=8.0, obtain_supported_raw=1)
  elif fault == 'next_road':
    branch['next_road_name_segment_raw'] = 0xfffffffe
  elif fault == 'reason':
    after['reason'] = 'navi_info_next_segment_invalid'
  elif fault == 'broadcast_code':
    record['fields']['NEW_ICON'] = 9
  elif fault == 'broadcast_stale':
    record['source_age_upper_ns'] = 2_000_000_001
  if not fault.startswith('before_'):
    sample.native_before['_navi_info'] = deepcopy(after)
  publication = adapter.accept(sample, now_ns=NOW_NS + 200_000_000)
  assert publication is not None, adapter.reason
  assert publication.instruction is None


def test_fresh_uturn_replay_holds_then_bad_snapshot_clears_and_new_record_recovers():
  adapter = AmapPhoneRouteAdapter()
  for sequence, mode, wanted in ((1, 'new', True), (2, 'replay', True), (3, 'bad', False),
                                 (4, 'replay', False), (5, 'new', True)):
    now = NOW_NS + (sequence - 1) * 100_000_000
    sample = uturn_sample(now_ns=now, sequence=sequence)
    record = sample.broadcast['records']['10001']
    record.update(sequence=120 if sequence == 5 else 119, replayed=mode != 'new',
                  source_age_upper_ns=190_000_000 + (sequence - 1) * 100_000_000)
    if mode == 'bad':
      sample.native_before['_navi_info']['branches'][0]['maneuver_id_raw'] = 3
    publication = adapter.accept(sample, now_ns=now)
    assert publication is not None
    assert (publication.instruction is not None) == wanted, (mode, adapter.instruction_reason)
    if wanted:
      assert publication.instruction['modifier'] == 'uturn'


@pytest.mark.parametrize('code', [2, 3, 5, 7, 9, 11, 12, 15, 16, 99])
def test_other_unrenderable_codes_do_not_inherit_uturn_exception(code):
  sample = uturn_sample()
  for navi in (sample.native_before['_navi_info'], sample.native_after['_navi_info']):
    for branch in navi['branches']:
      branch['maneuver_id_raw'] = code
  sample.broadcast['records']['10001']['fields'].update(ICON=code, NEW_ICON=code)
  adapter = AmapPhoneRouteAdapter()
  publication = adapter.accept(sample, now_ns=NOW_NS)
  assert publication is not None and publication.instruction is None
