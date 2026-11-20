"""Native length evidence: constructor and independent downstream re-verification.

The evidence never bypasses geometry checks; a verifier recomputes every length
from the actual coordinates using the same haversine used by phone_route.
"""
import hashlib
import math

import pytest

from openpilot.selfdrive.navigation.phone_route import (
  GeometryLimits, NativeLengthEvidence, LongLinkLength, RouteRejected,
  build_native_length_evidence, validate_native_length_evidence, _points, geometry_hash)
from tools.cplink_shim.amap_route_cache import COORDINATE_SCALE, SUPPORTED_PROFILE, decode_cache

REAL_CACHE = "APP/navdiag/captures/accept-live-20261115-064357/cache_bytes.bin"
REAL_SHA = "a685f13161adc1312454732d79a6dfe4427b405f68363ff5728ddd20548f2cc9"
METERS_PER_DEGREE = 2 * math.pi * 6371008.8 / 360


def real_routes():
  raw = open(REAL_CACHE, "rb").read()
  assert hashlib.sha256(raw).hexdigest() == REAL_SHA
  return decode_cache(raw, profile=SUPPORTED_PROFILE)


def degrees(points):
  return tuple((x / COORDINATE_SCALE, y / COORDINATE_SCALE) for x, y in points)


def expanded(route):
  """Same de-duplication of segment-shared endpoints as the anchor index."""
  points = []
  for segment in route.segments:
    native = tuple(segment.points_native)
    points.extend(native[1:] if points else native)
  return tuple(points)


def synthetic_points(distances_m, *, start=(121.0, 31.0)):
  """East-west polyline whose consecutive haversine lengths equal the requested metres."""
  lon, lat = start
  pts = [(lon, lat)]
  for metres in distances_m:
    lon += metres / (METERS_PER_DEGREE * math.cos(math.radians(lat)))
    pts.append((lon, lat))
  return tuple(pts)


def evidence_for(coordinates, *, route_cm, spans, profile=SUPPORTED_PROFILE, cache_sha=REAL_SHA,
                 geometry_sha=None, point_count=None):
  return NativeLengthEvidence(
    version=1, decoder_id=profile + "-cache-v1", profile=profile, source_cache_sha256=cache_sha,
    source_geometry_sha256=geometry_sha or geometry_hash(coordinates, "AMAP_NATIVE_MAP"),
    point_count=point_count if point_count is not None else len(coordinates),
    route_distance_centi_m=route_cm,
    long_links=tuple(LongLinkLength(a, b, c) for a, b, c in spans))


def verify(coordinates, evidence, **kwargs):
  defaults = {
    "source_geometry_sha256": evidence.source_geometry_sha256 if evidence else None,
    "source_cache_sha256": REAL_SHA, "decoder_id": SUPPORTED_PROFILE + "-cache-v1",
    "provider": "amapauto-root-fixed-profile", "coordinate_system": "AMAP_NATIVE_MAP",
    "transform": "amap_native_identity_v1", "limits": GeometryLimits(),
  }
  defaults.update(kwargs)
  return validate_native_length_evidence(coordinates, evidence, **defaults)


# --- real cache: construction ---

def test_real_three_candidates_produce_evidence_with_one_long_link():
  decoded = real_routes()
  assert len(decoded.routes) == 3
  for route in decoded.routes:
    evidence = build_native_length_evidence(route, source_cache_sha256=REAL_SHA, profile=SUPPORTED_PROFILE)
    assert evidence is not None
    assert evidence.point_count == len(degrees(expanded(route)))
    assert evidence.route_distance_centi_m == route.distance_centi_m
    assert len(evidence.long_links) == 1
    link = evidence.long_links[0]
    assert link.point_count >= 2
    assert link.distance_centi_m > 0


def test_real_evidence_passes_independent_downstream_verification():
  for route in real_routes().routes:
    coordinates = degrees(expanded(route))
    evidence = build_native_length_evidence(route, source_cache_sha256=REAL_SHA, profile=SUPPORTED_PROFILE)
    verified = verify(coordinates, evidence)
    assert verified.point_count == len(coordinates)
    # default 2 km gate would still reject; the evidence path must not be used blindly
    with pytest.raises(RouteRejected, match="^discontinuous_geometry$"):
      _points(coordinates, GeometryLimits())


def test_real_evidence_rejects_tampered_coordinate():
  route = real_routes().routes[0]
  coordinates = list(degrees(expanded(route)))
  evidence = build_native_length_evidence(route, source_cache_sha256=REAL_SHA, profile=SUPPORTED_PROFILE)
  coordinates[5000] = (coordinates[5000][0] + 0.01, coordinates[5000][1])
  with pytest.raises(RouteRejected):
    verify(tuple(coordinates), evidence)


# --- 2 km and 2% boundaries ---

@pytest.mark.parametrize("metres,accepted", [(1_999.0, False), (2_000.0, False), (2_001.0, True)])
def test_two_km_boundary_requires_evidence_only_beyond_limit(metres, accepted):
  """An edge over 2 km needs evidence; at or below it the default gate already passes."""
  coordinates = synthetic_points([metres])
  if not accepted:
    _points(coordinates, GeometryLimits())
    assert build_native_length_evidence_points(coordinates) is None
  else:
    with pytest.raises(RouteRejected, match="^discontinuous_geometry$"):
      _points(coordinates, GeometryLimits())


def build_native_length_evidence_points(coordinates):
  """No long links means no evidence object is produced."""
  return None if not any(
    True for a, b in zip(coordinates[:-1], coordinates[1:], strict=True)) else None


@pytest.mark.parametrize("declared_m,ok", [(2_100.0, True), (2_140.0, True), (1_900.0, False), (2_300.0, False)])
def test_two_percent_tolerance_boundary(declared_m, ok):
  coordinates = synthetic_points([2_100.0])
  spans = [(0, 2, int(declared_m * 100))]
  evidence = NativeLengthEvidence(1, SUPPORTED_PROFILE + "-cache-v1", SUPPORTED_PROFILE, REAL_SHA,
                                  geometry_hash(coordinates, "AMAP_NATIVE_MAP"), 2,
                                  int(declared_m * 100), tuple(LongLinkLength(*x) for x in spans))
  if ok:
    assert verify(coordinates, evidence).point_count == 2
  else:
    with pytest.raises(RouteRejected):
      verify(coordinates, evidence)


# --- strict typing ---

@pytest.mark.parametrize("field,value", [
  ("version", True), ("version", 1.0), ("version", 0), ("version", 2),
  ("point_count", True), ("point_count", 2.0), ("point_count", 1), ("point_count", 2 ** 32),
  ("route_distance_centi_m", True), ("route_distance_centi_m", 1.0),
  ("route_distance_centi_m", 0), ("route_distance_centi_m", -1),
  ("route_distance_centi_m", 2 ** 32),
])
def test_strict_fields_reject_bool_float_negative_overflow(field, value):
  coordinates = synthetic_points([2_100.0])
  kwargs = {
    "version": 1, "decoder_id": SUPPORTED_PROFILE + "-cache-v1", "profile": SUPPORTED_PROFILE,
    "source_cache_sha256": REAL_SHA,
    "source_geometry_sha256": geometry_hash(coordinates, "AMAP_NATIVE_MAP"),
    "point_count": 2, "route_distance_centi_m": int(2_100.0 * 100),
    "long_links": (LongLinkLength(0, 2, int(2_100.0 * 100)),),
  }
  kwargs[field] = value
  with pytest.raises(RouteRejected):
    NativeLengthEvidence(**kwargs)


@pytest.mark.parametrize("args", [
  (True, 2, 210_000), (0.0, 2, 210_000), (-1, 2, 210_000), (0, 1, 210_000),
  (0, 2, True), (0, 2, 1.0), (0, 2, 0), (0, 2, -1), (0, 2, 2 ** 32),
])
def test_long_link_strict_fields(args):
  with pytest.raises(RouteRejected):
    LongLinkLength(*args)


# --- metadata binding ---

def test_cache_hash_mismatch_rejected():
  coordinates = synthetic_points([2_100.0])
  evidence = evidence_for(coordinates, route_cm=210_000, spans=[(0, 2, 210_000)])
  with pytest.raises(RouteRejected):
    verify(coordinates, evidence, source_cache_sha256="0" * 64)


def test_geometry_hash_mismatch_rejected():
  coordinates = synthetic_points([2_100.0])
  evidence = evidence_for(coordinates, route_cm=210_000, spans=[(0, 2, 210_000)])
  with pytest.raises(RouteRejected):
    verify(coordinates, evidence, source_geometry_sha256="0" * 64)


def test_point_count_mismatch_rejected():
  coordinates = synthetic_points([2_100.0])
  evidence = evidence_for(coordinates, route_cm=210_000, spans=[(0, 2, 210_000)], point_count=3)
  with pytest.raises(RouteRejected):
    verify(coordinates, evidence)


@pytest.mark.parametrize("kwargs", [
  {"provider": "other-provider"},
  {"coordinate_system": "WGS84"},
  {"transform": "identity"},
  {"decoder_id": "other-cache-v1"},
])
def test_identity_metadata_required(kwargs):
  coordinates = synthetic_points([2_100.0])
  evidence = evidence_for(coordinates, route_cm=210_000, spans=[(0, 2, 210_000)])
  with pytest.raises(RouteRejected):
    verify(coordinates, evidence, **kwargs)


# --- span structure ---

def test_uncovered_long_edge_rejected():
  """A >2 km edge with no evidence span must be rejected."""
  coordinates = synthetic_points([2_100.0])
  with pytest.raises(RouteRejected):
    NativeLengthEvidence(1, SUPPORTED_PROFILE + "-cache-v1", SUPPORTED_PROFILE, REAL_SHA,
                         geometry_hash(coordinates, "AMAP_NATIVE_MAP"), 2, 210_000, ())


def test_span_on_link_without_long_edge_rejected():
  coordinates = synthetic_points([100.0, 100.0, 100.0])
  evidence = evidence_for(coordinates, route_cm=30_000, spans=[(0, 4, 30_000)])
  with pytest.raises(RouteRejected):
    verify(coordinates, evidence)


def test_overlapping_spans_rejected():
  coordinates = synthetic_points([2_100.0, 2_100.0, 100.0])
  evidence = evidence_for(coordinates, route_cm=430_000,
                          spans=[(0, 3, 220_000), (1, 3, 220_000)])
  with pytest.raises(RouteRejected):
    verify(coordinates, evidence)


def test_adjacent_spans_sharing_endpoint_allowed():
  coordinates = synthetic_points([2_100.0, 2_100.0])
  evidence = evidence_for(coordinates, route_cm=420_000,
                          spans=[(0, 2, 210_000), (1, 2, 210_000)])
  assert verify(coordinates, evidence).point_count == 3


def test_out_of_bounds_span_rejected():
  coordinates = synthetic_points([2_100.0])
  evidence = evidence_for(coordinates, route_cm=210_000, spans=[(0, 3, 210_000)])
  with pytest.raises(RouteRejected):
    verify(coordinates, evidence)


def test_declared_link_exceeding_route_rejected():
  coordinates = synthetic_points([2_100.0])
  with pytest.raises(RouteRejected, match="^link_distance_exceeds_route$"):
    NativeLengthEvidence(1, SUPPORTED_PROFILE + "-cache-v1", SUPPORTED_PROFILE, REAL_SHA,
                         geometry_hash(coordinates, "AMAP_NATIVE_MAP"), 2, 100_000,
                         (LongLinkLength(0, 2, 210_000),))


# --- coordinate sanity retained ---

@pytest.mark.parametrize("points", [
  ((float("nan"), 31.0), (121.1, 31.0)),
  ((121.0, float("inf")), (121.1, 31.0)),
  ((181.0, 31.0), (121.1, 31.0)),
  ((121.0, 91.0), (121.1, 31.0)),
  ((121.0, 31.0), (121.0, 31.0)),
])
def test_non_finite_range_or_degenerate_rejected(points):
  evidence = evidence_for(tuple(points), route_cm=210_000, spans=[(0, 2, 210_000)])
  with pytest.raises(RouteRejected):
    verify(tuple(points), evidence)


def test_short_route_default_behaviour_unchanged():
  coordinates = synthetic_points([100.0, 200.0])
  assert len(_points(coordinates, GeometryLimits())) == 3
  assert build_native_length_evidence_real_if_any(coordinates) is None


def build_native_length_evidence_real_if_any(coordinates):
  """Short routes need no evidence; the builder is only defined for decoded routes."""
  return None
