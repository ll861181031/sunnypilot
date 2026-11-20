"""Pure geometry tests for the lane-type and blind-spot render helpers."""
from types import SimpleNamespace

import numpy as np

from openpilot.selfdrive.ui.sunnypilot.onroad.road_markings import (
  blind_spot_draw_state,
  blindspot_barrier_quads,
  lane_dash_segments,
  lane_type_is_dashed,
  project_lane_segments,
)


def test_lane_type_is_dashed():
  for code in (0, 10, 20, 30):
    assert lane_type_is_dashed(code)
  for code in (-1, 1, 5, 11, 21):
    assert not lane_type_is_dashed(code)


def test_lane_dash_segments_are_periodic():
  line = np.array([[0.0, 0.0, 0.0], [10.0, 0.0, 0.0], [20.0, 0.0, 0.0]], dtype=np.float32)
  segments = lane_dash_segments(line, 20.0)
  assert len(segments) == 3
  # First dash: 0..5.2, then 9.4..14.6, then 18.8..20.
  assert segments[0][0, 0] == 0.0
  assert segments[0][-1, 0] == 5.2
  assert segments[1][0, 0] == 9.4
  assert segments[1][-1, 0] == 14.6


def test_project_lane_segments_identity():
  line = np.array([[0.0, 0.0, 10.0], [10.0, 0.0, 10.0], [20.0, 0.0, 10.0]], dtype=np.float32)
  segments = lane_dash_segments(line, 20.0)
  clip = SimpleNamespace(x=-1000.0, y=-1000.0, width=2000.0, height=2000.0)
  projected = project_lane_segments(segments, 0.1, np.eye(3, dtype=np.float32), clip)
  assert len(projected) == len(segments)
  for polygon in projected:
    assert polygon.ndim == 2
    assert polygon.shape[1] == 2
    assert len(polygon) % 2 == 0
    assert len(polygon) >= 4


def test_blindspot_barrier_quads():
  # Two edges of three points each, already in the ribbon order used by the renderer.
  upper = np.array([[0.0, 0.0], [1.0, 1.0], [2.0, 2.0]], dtype=np.float32)
  lower = np.array([[0.0, 1.0], [1.0, 2.0], [2.0, 3.0]], dtype=np.float32)
  points = np.vstack((upper, lower[::-1]))
  quads = blindspot_barrier_quads(points)
  assert quads.shape[1:] == (4, 2)
  assert len(quads) >= 1


def test_blind_spot_draw_state_radar_assist():
  car_state = SimpleNamespace(leftBlindspot=False, rightBlindspot=False, vEgo=10.0)
  radar_state = SimpleNamespace(
    leadLeft=SimpleNamespace(status=True, dRel=10.0),
    leadRight=SimpleNamespace(status=True, dRel=10.0),
  )
  assert blind_spot_draw_state(car_state, radar_state, True, "left", 10.0) == (False, False, True, False)
  assert blind_spot_draw_state(car_state, radar_state, False, "left", 10.0) == (False, False, False, False)
  assert blind_spot_draw_state(car_state, None, True, "left", 10.0) == (False, False, False, False)


def test_blind_spot_draw_state_bsd_wins_over_assist():
  car_state = SimpleNamespace(leftBlindspot=True, rightBlindspot=False, vEgo=10.0)
  radar_state = SimpleNamespace(
    leadLeft=SimpleNamespace(status=True, dRel=10.0),
    leadRight=SimpleNamespace(status=False, dRel=10.0),
  )
  assert blind_spot_draw_state(car_state, radar_state, True, "left", 10.0) == (True, False, False, False)


def _straight_line(n=33, x_max=20.0):
  x = np.linspace(0.0, x_max, n, dtype=np.float32)
  return np.stack((x, np.zeros_like(x), np.full_like(x, 10.0)), axis=1)


class _FakeSM:
  def __init__(self, data, valid, alive):
    self.data = data
    self.valid = valid
    self.alive = alive

  def __getitem__(self, key):
    return self.data[key]


def _onroad_renderer():
  from openpilot.selfdrive.ui.onroad.model_renderer import ModelRenderer
  renderer = ModelRenderer.__new__(ModelRenderer)
  renderer._lane_lines = [
    SimpleNamespace(raw_points=_straight_line(), projected_points=np.zeros((4, 2), dtype=np.float32))
    for _ in range(4)
  ]
  renderer._lane_line_probs = np.ones(4, dtype=np.float32)
  renderer._lane_draw_distance = 20.0
  renderer._car_space_transform = np.eye(3, dtype=np.float32)
  renderer._clip_region = SimpleNamespace(x=-1000.0, y=-1000.0, width=2000.0, height=2000.0)
  renderer._rect = SimpleNamespace(x=0.0, y=0.0, width=1000.0, height=1000.0)
  renderer._road_edges = []
  return renderer


def test_onroad_dashed_ego_boundary_draws_segments(monkeypatch):
  from openpilot.selfdrive.ui.onroad import model_renderer as mr
  renderer = _onroad_renderer()
  car_state_sp = SimpleNamespace(laneLineTypesValid=True, leftLaneLine=0, rightLaneLine=1)
  sm = _FakeSM(
    data={'carStateSP': car_state_sp},
    valid={'carStateSP': True},
    alive={'carStateSP': True},
  )
  monkeypatch.setattr(mr, 'ui_state', SimpleNamespace(sm=sm))
  calls = []
  monkeypatch.setattr(mr, 'draw_polygon', lambda rect, pts, color: calls.append(np.asarray(pts)))
  renderer._draw_lane_lines()
  # Three solid lines (0, 2, 3) plus the left boundary split into dashes.
  assert len(calls) > 4
  assert any(np.ptp(poly[:, 0]) < 6.0 for poly in calls)


def test_onroad_blindspot_draws_amber_barrier(monkeypatch):
  from openpilot.cereal import log
  from openpilot.selfdrive.ui.onroad import model_renderer as mr
  renderer = _onroad_renderer()
  renderer._path = SimpleNamespace(raw_points=_straight_line(n=20, x_max=20.0))
  car_state = SimpleNamespace(leftBlindspot=True, rightBlindspot=False, vEgo=10.0)
  meta = SimpleNamespace(
    laneChangeState=log.LaneChangeState.preLaneChange,
    laneChangeDirection=log.LaneChangeDirection.left,
  )
  sm = _FakeSM(
    data={'carState': car_state, 'modelV2': SimpleNamespace(meta=meta)},
    valid={'carState': True, 'modelV2': True, 'radarState': False},
    alive={'carState': True, 'modelV2': True, 'radarState': False},
  )
  monkeypatch.setattr(mr, 'ui_state', SimpleNamespace(sm=sm))
  calls = []
  monkeypatch.setattr(mr, 'draw_polygon', lambda rect, pts, color: calls.append(color))
  renderer._draw_blind_spot(sm)
  assert calls
  assert all(color.r == 255 and color.g == 215 and color.b == 0 for color in calls)


def test_mici_dashed_ego_boundary_draws_segments(monkeypatch):
  from openpilot.selfdrive.ui.mici.onroad import model_renderer as mr
  renderer = mr.ModelRenderer.__new__(mr.ModelRenderer)
  renderer._lane_lines = [
    SimpleNamespace(raw_points=_straight_line(), projected_points=np.zeros((4, 2), dtype=np.float32))
    for _ in range(4)
  ]
  renderer._lane_line_probs = np.ones(4, dtype=np.float32)
  renderer._lane_draw_distance = 20.0
  renderer._car_space_transform = np.eye(3, dtype=np.float32)
  renderer._clip_region = SimpleNamespace(x=-1000.0, y=-1000.0, width=2000.0, height=2000.0)
  renderer._rect = SimpleNamespace(x=0.0, y=0.0, width=1000.0, height=1000.0)
  renderer._road_edges = []
  renderer._get_ll_color = lambda *args: mr.rl.Color(255, 255, 255, 255)

  car_state_sp = SimpleNamespace(laneLineTypesValid=True, leftLaneLine=0, rightLaneLine=1)
  sm = _FakeSM(
    data={'carStateSP': car_state_sp},
    valid={'carStateSP': True},
    alive={'carStateSP': True},
  )
  monkeypatch.setattr(mr, 'ui_state', SimpleNamespace(sm=sm))
  calls = []
  monkeypatch.setattr(mr, 'draw_polygon', lambda rect, pts, color: calls.append(np.asarray(pts)))
  renderer._draw_lane_lines()
  assert len(calls) > 4
  assert any(np.ptp(poly[:, 0]) < 6.0 for poly in calls)
