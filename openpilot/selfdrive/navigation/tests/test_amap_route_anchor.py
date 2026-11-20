from dataclasses import replace
import math

import numpy as np
import pytest

from openpilot.selfdrive.navigation.amap_route_anchor import (METERS_PER_DEGREE, AmapRouteAnchorIndex,
                                                              AmapRouteAnchorRejected)
from openpilot.selfdrive.navigation.route import RouteTracker
from tools.cplink_shim.amap_route_cache import COORDINATE_SCALE, CachedRoute, RouteLink, RouteSegment


ORIGIN = (36_000_000, 72_000_000)


def point(x, y=0):
  return ORIGIN[0] + x, ORIGIN[1] + y


def degrees(native):
  return tuple(value / COORDINATE_SCALE for value in native)


def segment(points, links=None):
  if links is None:
    links = [(0, len(points))]
  return RouteSegment(0, 0, tuple(points), tuple(RouteLink(0, 1000, start, count) for start, count in links))


def route(*segments):
  return CachedRoute(987654321, 1000, 30, tuple(segments))


@pytest.fixture
def example():
  return route(segment([point(0), point(100), point(200), point(300)], [(0, 3), (2, 2)]),
               segment([point(300), point(400), point(500), point(600)], [(0, 2), (1, 3)]))


def test_same_segment_boundary_dedup_as_cache_with_nonzero_source_indices(example):
  index = AmapRouteAnchorIndex(example)
  assert index.points_native == example.points_native
  assert index.coordinates == example.points_degrees
  assert len(index.coordinates) == 7
  assert index.edge(0, 0, 0).global_segment == 0
  assert index.edge(0, 0, 1).global_segment == 1
  assert index.edge(0, 1, 0).global_segment == 2
  assert index.edge(1, 0, 0).global_segment == 3
  assert index.edge(1, 1, 0).global_segment == 4
  edge = index.edge(1, 1, 1)
  assert edge.global_segment == 5
  assert edge.start == degrees(point(500))
  assert edge.end == degrees(point(600))


def test_current_native_window_preserves_links_without_cache_length_evidence(example):
  spans = (((0, 3), (2, 2)), ((3, 2), (4, 3)))
  index = AmapRouteAnchorIndex.from_native(example.points_native, spans)
  assert index.points_native == example.points_native
  assert index.coordinates == example.points_degrees
  assert index._spans == spans
  assert len(index.coordinates) == 7
  edge = index.edge(1, 1, 1)
  assert edge.global_segment == 5
  assert edge.start == degrees(point(500))
  assert edge.end == degrees(point(600))


@pytest.mark.parametrize("spans", [
  (),
  (((0, 2),),),
  (((0, 3),),),
  (((0, 3), (3, 2)), ((3, 2), (4, 3))),
  (((0, 3), (2, 2)), ((4, 2), (4, 3))),
  (((0, 3), (2, 2)), ((3, 2), (5, 3))),
])
def test_current_native_window_rejects_incomplete_or_discontinuous_spans(spans):
  points = (point(0), point(100), point(200), point(300),
            point(400), point(500), point(600))
  with pytest.raises(AmapRouteAnchorRejected):
    AmapRouteAnchorIndex.from_native(points, spans)


def test_current_native_window_never_accepts_unsubstantiated_long_edge():
  with pytest.raises(AmapRouteAnchorRejected, match="^discontinuous_source_edge$"):
    AmapRouteAnchorIndex.from_native((point(0), point(1_000_000)), (((0, 2),),))


def test_midpoint_uses_only_selected_source_edge_and_exact_prefix_progress(example):
  index = AmapRouteAnchorIndex(example)
  result = index.project(1, 1, 1, degrees(point(550)))
  assert result.edge.global_segment == 5
  assert result.fraction == pytest.approx(0.5, abs=1e-10)
  assert result.progress_m == pytest.approx((index.distance_m[5] + index.distance_m[6]) / 2)
  assert result.lateral_m == pytest.approx(0)
  assert result.coordinate == pytest.approx(degrees(point(550)))
  assert result.global_segment == result.edge.global_segment
  assert result.pose[:2] == pytest.approx(result.coordinate)


def test_frame_is_not_mislabeled_as_global_datum_or_gps_evidence(example):
  index = AmapRouteAnchorIndex(example)
  assert index.coordinate_frame == "amap_native_map"
  assert index.coordinate_datum_verified is False
  assert "987654321" not in repr(index)
  assert "10.000" not in repr(index)
  assert str(ORIGIN[0]) not in repr(index)
  result = index.project(0, 0, 0, degrees(point(50)))
  assert "start=" not in repr(result.edge)
  assert "end=" not in repr(result.edge)
  assert "coordinate=" not in repr(result)


def test_loop_repeated_coordinates_keep_later_source_branch():
  native = [point(0), point(10_000), point(10_000, 10_000), point(0, 10_000),
            point(0), point(10_000), point(20_000)]
  index = AmapRouteAnchorIndex(route(segment(native)))
  position = degrees(point(5000))
  result = index.project(0, 0, 4, position)
  assert result.edge.global_segment == 4
  assert result.fraction == pytest.approx(0.5)
  legacy_progress = RouteTracker(np.asarray(index.coordinates)).project(*position)
  assert result.progress_m - legacy_progress > 1000
  assert result.progress_m > index.distance_m[4]


def test_crossing_later_source_edge_does_not_snap_to_first_intersection():
  native = [point(-10_000, -10_000), point(10_000, 10_000), point(-10_000, 10_000), point(10_000, -10_000)]
  index = AmapRouteAnchorIndex(route(segment(native)))
  position = degrees(point(0))
  result = index.project(0, 0, 2, position)
  assert result.edge.global_segment == 2
  assert result.fraction == pytest.approx(0.5, abs=1e-8)
  assert result.progress_m > index.distance_m[2]
  legacy_progress = RouteTracker(np.asarray(index.coordinates)).project(*position)
  assert result.progress_m - legacy_progress > 1000


def test_wrong_source_edge_rejected_even_when_position_is_on_another_route_edge():
  index = AmapRouteAnchorIndex(route(segment([point(0), point(10_000), point(10_000, 10_000)])))
  with pytest.raises(AmapRouteAnchorRejected, match="^position_outside_source_edge$"):
    index.project(0, 0, 0, degrees(point(10_000, 5000)), max_lateral_m=2)


def test_project_clamps_to_specified_edge_endpoint_without_choosing_next_edge(example):
  index = AmapRouteAnchorIndex(example)
  before = index.project(0, 0, 0, degrees(point(-1)))
  after = index.project(0, 0, 0, degrees(point(101)))
  assert before.fraction == 0
  assert before.progress_m == 0
  assert after.fraction == 1
  assert after.edge.global_segment == 0
  assert after.progress_m == index.distance_m[1]


def test_repeated_intralink_point_is_preserved_but_zero_length_edge_rejected():
  example = route(segment([point(0), point(100), point(100), point(200)]))
  index = AmapRouteAnchorIndex(example)
  assert index.points_native == example.points_native
  assert index.edge(0, 0, 2).global_segment == 2
  with pytest.raises(AmapRouteAnchorRejected, match="^degenerate_source_edge$"):
    index.project(0, 0, 1, degrees(point(100)))


def test_pose_matches_existing_training_chord_at_nonzero_progress_and_ends():
  index = AmapRouteAnchorIndex(route(segment([point(0), point(10_000), point(10_000, 10_000), point(20_000, 10_000)])))
  tracker = RouteTracker(np.asarray(index.coordinates))
  for progress in [-10, 0, 10, index.distance_m[1] - 20, index.distance_m[1] + 20, index.distance_m[-1], index.distance_m[-1] + 10]:
    assert index.pose(progress) == pytest.approx(tracker.pose(progress), abs=1e-9)


def test_pose_handles_repeated_trailing_points_without_nan():
  index = AmapRouteAnchorIndex(route(segment([point(0), point(100), point(100)])))
  assert index.pose(index.distance_m[-1])[:2] == degrees(point(100))
  assert all(math.isfinite(value) for value in index.pose(index.distance_m[-1]))


@pytest.mark.parametrize("progress", [False, math.nan, math.inf, 10**1000, "1"])
def test_pose_rejects_unusable_progress(example, progress):
  with pytest.raises(AmapRouteAnchorRejected, match="^invalid_progress$"):
    AmapRouteAnchorIndex(example).pose(progress)


def test_large_intralink_coordinate_gap_rejected():
  with pytest.raises(AmapRouteAnchorRejected, match="^discontinuous_source_edge$"):
    AmapRouteAnchorIndex(route(segment([point(0), point(1_000_000)])))


@pytest.mark.parametrize("source", [(0, 0, 2), (0, 1, 1), (1, 0, 1), (1, 1, 2)])
def test_terminal_local_point_never_guesses_next_node_or_wraps(example, source):
  with pytest.raises(AmapRouteAnchorRejected, match="^terminal_local_point_unverified$"):
    AmapRouteAnchorIndex(example).edge(*source)


@pytest.mark.parametrize("source", [(-1, 0, 0), (2, 0, 0), (False, 0, 0), (0.0, 0, 0),
                                    (0, -1, 0), (0, 2, 0), (0, True, 0), (0, 0.0, 0),
                                    (0, 0, -1), (0, 0, 3), (0, 0, False), (0, 0, 0.0),
                                    (2**100, 0, 0), (0, 2**100, 0), (0, 0, 2**100)])
def test_source_indices_are_strict_nonboolean_integer_bounds(example, source):
  with pytest.raises(AmapRouteAnchorRejected, match="^invalid_.*_index$"):
    AmapRouteAnchorIndex(example).edge(*source)


@pytest.mark.parametrize("position", [[True, 20], [10, False], [math.nan, 20], [10, math.inf],
                                      [10**1000, 20], [181, 20], [10, -91], [10], "10,20", None])
def test_unusable_position_rejected(example, position):
  with pytest.raises(AmapRouteAnchorRejected, match="^invalid_position$"):
    AmapRouteAnchorIndex(example).project(0, 0, 0, position)


@pytest.mark.parametrize("limit", [0, -1, True, math.nan, math.inf, 1001, 10**1000, "2"])
def test_invalid_lateral_limit_rejected(example, limit):
  with pytest.raises(AmapRouteAnchorRejected, match="^invalid_lateral_limit$"):
    AmapRouteAnchorIndex(example).project(0, 0, 0, degrees(point(50)), max_lateral_m=limit)


@pytest.mark.parametrize("links", [[(1, 2)], [(0, 2)], [(0, 3), (1, 3)], [(0, 2), (3, 2)],
                                   [(0, 2), (0, 4)], [(0, 5)], [(False, 4)], [(0, True)], [(0, 1)]])
def test_out_of_order_overlapping_gapped_uncovered_or_invalid_links_rejected(links):
  example = route(segment([point(0), point(100), point(200), point(300)], links))
  with pytest.raises(AmapRouteAnchorRejected, match="^(invalid|incomplete)_link_coverage$"):
    AmapRouteAnchorIndex(example)


def test_segment_join_must_be_exact_not_nearest_or_float_rounded():
  example = route(segment([point(0), point(100)]), segment([point(101), point(200)]))
  with pytest.raises(AmapRouteAnchorRejected, match="^discontinuous_segment_join$"):
    AmapRouteAnchorIndex(example)


@pytest.mark.parametrize("native", [[True, ORIGIN[1]], [ORIGIN[0], 0.0], [10**1000, 20], [0, 91 * COORDINATE_SCALE]])
def test_native_points_are_strict_and_bounded(native):
  example = route(segment([point(0), native]))
  with pytest.raises(AmapRouteAnchorRejected, match="^invalid_native_coordinate$"):
    AmapRouteAnchorIndex(example)


def test_empty_one_point_no_links_and_fully_degenerate_route_rejected():
  for example in [route(), route(segment([point(0)])), route(replace(segment([point(0), point(100)]), links=()))]:
    with pytest.raises(AmapRouteAnchorRejected):
      AmapRouteAnchorIndex(example)
  with pytest.raises(AmapRouteAnchorRejected, match="^degenerate_route$"):
    AmapRouteAnchorIndex(route(segment([point(0), point(0)])))


def test_link_point_limit_is_applied_before_unbounded_route_build(monkeypatch):
  monkeypatch.setattr("openpilot.selfdrive.navigation.amap_route_anchor.MAX_ROUTE_POINTS", 3)
  with pytest.raises(AmapRouteAnchorRejected, match="^route_point_limit$"):
    AmapRouteAnchorIndex(route(segment([point(0), point(100), point(200)]), segment([point(200), point(300)])))


# --- long native edge corroborated by declared link/route length ---

LONG_EDGE_NATIVE = 2_174.7
LONG_EDGE_TAIL_M = 600.0
# The single link covers both edges of this synthetic route.
DECLARED_LINK_CM = round((LONG_EDGE_NATIVE + LONG_EDGE_TAIL_M) * 100)
DECLARED_ROUTE_CM = DECLARED_LINK_CM


def _native_for_meters(meters):
  """Native x units that project to the requested east-west distance at ORIGIN latitude."""
  import math
  return round(meters * COORDINATE_SCALE / (METERS_PER_DEGREE * math.cos(math.radians(ORIGIN[1] / COORDINATE_SCALE))))


def long_edge_route(edge_native=None, link_cm=DECLARED_LINK_CM, route_cm=DECLARED_ROUTE_CM, tail=600):
  """One link containing a >2 km native edge, declared length corroborating it."""
  if edge_native is None:
    edge_native = _native_for_meters(LONG_EDGE_NATIVE)
  pts = [point(0), point(edge_native), point(edge_native + _native_for_meters(tail))]
  seg = RouteSegment(0, 0, tuple(pts), (RouteLink(0, link_cm, 0, 3),))
  return CachedRoute(987654321, route_cm, 30, (seg,))


def test_native_long_edge_accepted_when_link_and_route_corroborate():
  index = AmapRouteAnchorIndex(long_edge_route())
  assert len(index.points_native) == 3
  assert index.distance_m[-1] == pytest.approx(LONG_EDGE_NATIVE + len(index.coordinates) * 0 + 600, abs=5.0)


def test_native_long_edge_preserves_geometry_and_native_binding():
  route_obj = long_edge_route()
  index = AmapRouteAnchorIndex(route_obj)
  assert index.points_native == route_obj.segments[0].points_native
  assert index.coordinates == tuple((x / COORDINATE_SCALE, y / COORDINATE_SCALE) for x, y in index.points_native)
  assert index._spans == (((0, 3),),)
  mid = tuple((a + b) / 2 for a, b in zip(index.coordinates[0], index.coordinates[1], strict=True))
  projected = index.project(0, 0, 0, mid)
  assert projected.edge.segment_index == 0
  assert projected.edge.link_index == 0
  assert projected.fraction == pytest.approx(0.5, abs=1e-6)
  assert index.edge(0, 0, 0).length_m == pytest.approx(LONG_EDGE_NATIVE, abs=5.0)


@pytest.mark.parametrize("kwargs", [
  {"link_cm": 0},
  {"link_cm": -546_800},
  {"link_cm": True},
  {"link_cm": 546_800.0},
  {"link_cm": 2 ** 32},
  {"route_cm": 0},
  {"route_cm": -1},
])
def test_declared_length_must_be_strict_positive_uint32(kwargs):
  with pytest.raises(AmapRouteAnchorRejected, match="^discontinuous_source_edge$"):
    AmapRouteAnchorIndex(long_edge_route(**kwargs))


def test_declared_link_longer_than_route_rejected():
  with pytest.raises(AmapRouteAnchorRejected, match="^discontinuous_source_edge$"):
    AmapRouteAnchorIndex(long_edge_route(link_cm=DECLARED_ROUTE_CM + 100, route_cm=DECLARED_ROUTE_CM))


@pytest.mark.parametrize("link_cm", [2_000_00, 3_600_00])
def test_link_declared_length_outside_two_percent_rejected(link_cm):
  with pytest.raises(AmapRouteAnchorRejected, match="^discontinuous_source_edge$"):
    AmapRouteAnchorIndex(long_edge_route(link_cm=link_cm, route_cm=route_cm_for(link_cm)))


def test_route_declared_length_outside_two_percent_rejected():
  with pytest.raises(AmapRouteAnchorRejected, match="^discontinuous_source_edge$"):
    AmapRouteAnchorIndex(long_edge_route(route_cm=DECLARED_ROUTE_CM * 3))


def route_cm_for(link_cm):
  """Route declaration consistent with the given link so only the link check fails."""
  return round((LONG_EDGE_NATIVE + LONG_EDGE_TAIL_M) * 100)


def test_short_link_keeps_old_behaviour_despite_inaccurate_declaration():
  seg = segment([point(0), point(100), point(200), point(300)], [(0, 3), (2, 2)])
  index = AmapRouteAnchorIndex(route(seg))
  assert len(index.points_native) == 4
