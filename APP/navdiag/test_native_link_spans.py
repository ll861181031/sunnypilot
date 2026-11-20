from copy import deepcopy
from dataclasses import replace
import hashlib
import struct
from types import SimpleNamespace

import numpy as np
import pytest

from APP.navdiag.live_route_snapshot import select_native
from APP.navdiag.native_route_source import NativeRouteProbeSource, ProbeError, _GEOMETRY_MAGIC
from openpilot.selfdrive.modeld import nav_inputs
from openpilot.selfdrive.modeld.nav_reset import NavigationResetMixin
from openpilot.selfdrive.navigation import mapsd
from openpilot.selfdrive.navigation.amap_phone_route import AmapPhoneRouteAdapter
from openpilot.selfdrive.navigation.amap_route_anchor import AmapRouteAnchorIndex, AmapRouteAnchorRejected
from openpilot.selfdrive.navigation.tests.test_amap_phone_pipeline import (
  MAP_SHAPE, NATIVE_POSITION, NATIVE_X, NATIVE_Y, NOW_NS, VEC_SHAPE, PhoneSM, make_sample, model_runtime,
)


POINTS = tuple((NATIVE_X + offset, NATIVE_Y) for offset in (-1800, -900, 0, 900, 1800, 2700, 3600))


def native_geometry(spans, *, points=POINTS, segment_count=1, sample=None):
  if sample is None:
    sample = make_sample()
  payload = (_GEOMETRY_MAGIC + struct.pack('<8I', 1, 0, 1, 1, 0, segment_count, len(spans), len(points))
             + bytes.fromhex(sample.selected.identity_hash) + hashlib.sha256(sample.epoch.encode()).digest()
             + b''.join(struct.pack('<3I', *span) for span in spans)
             + b''.join(struct.pack('<2i', *point) for point in points))
  source = NativeRouteProbeSource.__new__(NativeRouteProbeSource)
  source.epoch = sample.epoch
  geometry = source._parse_geometry(payload)
  geometry.update(host_observation_clock_domain='host_monotonic',
                  host_observation_started_monotonic_ns=NOW_NS - 90_000_000,
                  host_observation_finished_monotonic_ns=NOW_NS - 60_000_000)
  return geometry, select_native(geometry, epoch=sample.epoch)


@pytest.mark.parametrize('overlap', [1, 2, 3, 4, 5])
def test_shared_native_window_preserves_every_source_edge_and_prefix(overlap):
  start = 6 - overlap
  geometry, selected = native_geometry(((0, 0, 6), (0, start, 7 - start)))
  index = AmapRouteAnchorIndex.from_native(selected.route.points_native, selected.route.segments)
  assert index.points_native == POINTS
  assert index._spans == (((0, 6), (start, 7 - start)),)
  assert geometry['source_geometry_sha256'] == selected.source_payload_sha256
  assert selected.complete_route_verified is False
  for link, (offset, count) in enumerate(index._spans[0]):
    for local in range(count - 1):
      edge = index.edge(0, link, local)
      assert edge.global_segment == offset + local
      assert edge.start == index.coordinates[offset + local]
      assert edge.end == index.coordinates[offset + local + 1]
      assert edge.start_progress_m == index.distance_m[offset + local]
    with pytest.raises(AmapRouteAnchorRejected, match='^terminal_local_point_unverified$'):
      index.edge(0, link, count - 1)


def test_multiple_shared_points_do_not_change_segment_boundary_contract():
  _, selected = native_geometry(((0, 0, 4), (0, 1, 4), (1, 4, 3)), segment_count=2)
  index = AmapRouteAnchorIndex.from_native(selected.route.points_native, selected.route.segments)
  assert index._spans == (((0, 4), (1, 4)), ((4, 3),))
  assert index.edge(0, 1, 2).global_segment == 3
  assert index.edge(1, 0, 0).global_segment == 4
  assert index.points_native == POINTS


def test_repeated_loop_coordinates_keep_the_source_occurrence():
  points = (POINTS[0], POINTS[1], POINTS[2], (NATIVE_X, NATIVE_Y + 900),
            POINTS[1], POINTS[2], POINTS[3])
  _, selected = native_geometry(((0, 0, 6), (0, 1, 6)), points=points)
  index = AmapRouteAnchorIndex.from_native(selected.route.points_native, selected.route.segments)
  first = index.edge(0, 1, 0)
  repeated = index.edge(0, 1, 3)
  assert first.start == repeated.start and first.end == repeated.end
  assert first.global_segment == 1 and repeated.global_segment == 4
  assert first.start_progress_m < repeated.start_progress_m
  assert index.points_native == points


@pytest.mark.parametrize('spans', [
  ((0, 0, 5), (0, 1, 3), (0, 3, 4)),
  ((0, 0, 5), (0, 1, 4), (0, 4, 3)),
  ((0, 0, 5), (0, 0, 7)),
  ((0, 0, 5), (0, 5, 2)),
  ((0, 0, 4), (0, 5, 2)),
])
def test_wire_parser_rejects_nonadvancing_or_disconnected_native_links(spans):
  with pytest.raises(ProbeError, match='^native_probe_geometry_link_discontinuity$'):
    native_geometry(spans)


@pytest.mark.parametrize('spans', [
  (((0, 5), (1, 3), (3, 4)),),
  (((0, 5), (1, 4), (4, 3)),),
  (((0, 5), (0, 7)),),
  (((0, 5), (5, 2)),),
  (((0, 4), (5, 2)),),
  (((0, 4),), ((2, 5),)),
])
def test_anchor_rechecks_malformed_spans_independently(spans):
  with pytest.raises(AmapRouteAnchorRejected):
    AmapRouteAnchorIndex.from_native(POINTS, spans)


def test_deep_overlap_reaches_model_map_without_reindexing(monkeypatch):
  monkeypatch.setattr(mapsd.time, 'monotonic_ns', lambda: NOW_NS)
  monkeypatch.setattr(nav_inputs.time, 'monotonic_ns', lambda: NOW_NS)
  monkeypatch.setattr(nav_inputs.time, 'monotonic', lambda: NOW_NS / 1e9)
  monkeypatch.setattr(mapsd, 'cplink_navi_active', lambda: True)
  monkeypatch.setattr(nav_inputs, 'cplink_navi_active', lambda: True)
  sample = make_sample()
  _, sample.selected = native_geometry(((0, 0, 4), (0, 1, 6)), sample=sample)
  for observation in (sample.native_before, sample.native_after):
    observation['_position'].update(link_index=1, point_index=1,
                                    native_st_pos_deg=list(NATIVE_POSITION),
                                    matched_st_pos_deg=list(NATIVE_POSITION))
  sample.broadcast = None
  sample.native_after.pop('_navi_info')
  adapter = AmapPhoneRouteAdapter()
  publication = adapter.accept(sample, now_ns=NOW_NS)
  assert publication is not None, adapter.reason
  assert publication.instruction is None
  assert adapter._index.edge(0, 1, 1).global_segment == 2
  state = SimpleNamespace(clock=[NOW_NS], sample=sample, adapter=adapter, publication=publication,
                          sm=PhoneSM(publication), renderer=mapsd.MapsD())
  subject = model_runtime(state, monkeypatch)
  class Model(NavigationResetMixin):
    input_shapes = {'nav_map': MAP_SHAPE, 'navigation': VEC_SHAPE}

  inputs = subject.inputs_for_model(state.sm, 0.0, Model())
  assert inputs['nav_map'].any()
  assert subject.status()['map_valid'] is True
  assert subject.status()['instruction_valid'] is False
  np.testing.assert_array_equal(inputs['navigation'], np.zeros(VEC_SHAPE, dtype=np.float32))
  np.testing.assert_array_equal(state.renderer.route, sample.selected.route.points_degrees)
  assert state.renderer._projection_evidence.segment == 2


@pytest.mark.parametrize('fault', ['gps_stale', 'source_off_edge', 'terminal', 'changed_anchor'])
def test_shared_windows_never_bypass_existing_position_guards(fault):
  sample = make_sample()
  _, sample.selected = native_geometry(((0, 0, 4), (0, 1, 6)), sample=sample)
  for observation in (sample.native_before, sample.native_after):
    observation['_position'].update(link_index=1, point_index=1)
  if fault == 'gps_stale':
    sample.gps = replace(sample.gps, age_upper_ns=2_000_000_000)
  elif fault == 'source_off_edge':
    for observation in (sample.native_before, sample.native_after):
      observation['_position']['matched_st_pos_deg'][1] += 0.001
  elif fault == 'terminal':
    for observation in (sample.native_before, sample.native_after):
      observation['_position']['point_index'] = 5
  else:
    sample.native_after = deepcopy(sample.native_after)
    sample.native_after['_position']['point_index'] = 2
  adapter = AmapPhoneRouteAdapter()
  assert adapter.accept(sample, now_ns=NOW_NS) is None
  expected = {'gps_stale': 'phone_gps_stale_or_inaccurate',
              'source_off_edge': 'phone_anchor_position_outside_source_edge',
              'terminal': 'phone_anchor_terminal_local_point_unverified',
              'changed_anchor': 'phone_anchor_changed_during_read'}
  assert adapter.reason == expected[fault]
