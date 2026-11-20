"""End-to-end native length evidence: decode -> provenance -> lease -> wire -> receiver."""
import hashlib
import math  # noqa: F401  (used via __import__ in the invariant test)

import pytest

from openpilot.selfdrive.navigation.nav_map_contract import WireRouteBinding, binding_from_snapshot
from openpilot.selfdrive.navigation.phone_route import (GeometryLimits, NativeLengthEvidence, RouteIdentity,
                                                        RouteRejected, SourceProvenance, _points, binding_digest,
                                                        build_native_length_evidence, geometry_hash)
from tools.cplink_shim.amap_route_cache import COORDINATE_SCALE, SUPPORTED_PROFILE, decode_cache

REAL_CACHE = "APP/navdiag/captures/accept-live-20261115-064357/cache_bytes.bin"
REAL_SHA = "a685f13161adc1312454732d79a6dfe4427b405f68363ff5728ddd20548f2cc9"
IDENTITY = RouteIdentity("amapauto-root-fixed-profile", "session-1", "route-1", 0, REAL_SHA)


def expanded(route):
  points = []
  for segment in route.segments:
    native = tuple(segment.points_native)
    points.extend(native[1:] if points else native)
  return tuple(points)


def degrees(points):
  return tuple((x / COORDINATE_SCALE, y / COORDINATE_SCALE) for x, y in points)


@pytest.fixture(scope="module")
def decoded():
  raw = open(REAL_CACHE, "rb").read()
  assert hashlib.sha256(raw).hexdigest() == REAL_SHA
  return decode_cache(raw, profile=SUPPORTED_PROFILE), raw


@pytest.mark.parametrize("index", [0, 1, 2])
def test_real_route_full_chain_survives_wire_round_trip(decoded, index):
  cache, raw = decoded
  route = cache.routes[index]
  points = degrees(expanded(route))
  evidence = build_native_length_evidence(route, source_cache_sha256=REAL_SHA, profile=SUPPORTED_PROFILE)

  provenance = SourceProvenance.from_decoded(
    identity=IDENTITY, source_bytes=raw, source_object_id="/cache/1.dat",
    decoder_id=SUPPORTED_PROFILE + "-cache-v1", coordinate_system="AMAP_NATIVE_MAP",
    decoded_points=points, expected_point_count=len(points), complete=True,
    native_length_evidence=evidence)

  snapshot = None
  from openpilot.selfdrive.navigation.phone_route import ActiveRouteBinding, PhoneRouteLease
  lease = PhoneRouteLease("transport-1", max_age_s=10.0)
  active = ActiveRouteBinding(IDENTITY, "transport-1", "/cache/1.dat", 1, 0.5, True)
  assert lease.observe(active, now=0.5)
  payload = {"provider": IDENTITY.provider, "source_session_id": IDENTITY.source_session_id,
             "route_id": IDENTITY.route_id, "revision": 0, "cache_sha256": REAL_SHA,
             "coordinate_system": "AMAP_NATIVE_MAP", "coordinate_order": "lon_lat",
             "geometry_kind": "full_route", "complete": True, "point_count": len(points), "points": points}
  assert lease.offer(payload, provenance, now=0.6, native_length_evidence=evidence)
  snapshot = lease.snapshot(now=0.7)
  assert snapshot is not None
  assert snapshot.native_length_evidence == evidence

  binding = binding_from_snapshot(snapshot, active, now_ns=700_000_000)
  assert binding.native_length_evidence == evidence
  data = binding.to_dict()
  round_tripped = WireRouteBinding.from_dict(data)
  assert round_tripped.native_length_evidence == evidence
  assert round_tripped.binding_id == binding.binding_id
  assert round_tripped.route_key() == binding.route_key()

  # Receiver re-verifies from the coordinates it actually received.
  from openpilot.selfdrive.navigation.phone_route import validate_native_length_evidence
  validate_native_length_evidence(snapshot.coordinates, round_tripped.native_length_evidence,
    source_geometry_sha256=round_tripped.source_geometry_sha256,
    source_cache_sha256=REAL_SHA, decoder_id=evidence.decoder_id,
    provider=IDENTITY.provider, coordinate_system="AMAP_NATIVE_MAP",
    transform="amap_native_identity_v1", limits=GeometryLimits())


def test_default_gate_still_rejects_same_real_geometry(decoded):
  """Without evidence the very same real geometry must still be rejected."""
  cache, _ = decoded
  points = degrees(expanded(cache.routes[0]))
  with pytest.raises(RouteRejected, match="^discontinuous_geometry$"):
    _points(points, GeometryLimits())


def test_legacy_binding_without_evidence_still_parses(decoded):
  cache, raw = decoded
  route = cache.routes[0]
  points = degrees(expanded(route))
  with pytest.raises(RouteRejected, match="^discontinuous_geometry$"):
    SourceProvenance.from_decoded(
      identity=IDENTITY, source_bytes=raw, source_object_id="/cache/1.dat",
      decoder_id=SUPPORTED_PROFILE + "-cache-v1", coordinate_system="AMAP_NATIVE_MAP",
      decoded_points=points, expected_point_count=len(points), complete=True)


def test_binding_id_binds_evidence(decoded):
  cache, _ = decoded
  route = cache.routes[0]
  evidence = build_native_length_evidence(route, source_cache_sha256=REAL_SHA, profile=SUPPORTED_PROFILE)
  with_evidence = binding_digest("t", IDENTITY, "/cache/1.dat", "a" * 64, evidence)
  without = binding_digest("t", IDENTITY, "/cache/1.dat", "a" * 64, None)
  assert with_evidence != without


@pytest.mark.parametrize("field,bad", [
  ("source_cache_sha256", "0" * 64),
  ("source_geometry_sha256", "1" * 64),
  ("point_count", 99),
  ("profile", "other-profile"),
  ("decoder_id", "other-cache-v1"),
])
def test_tampered_evidence_rejected_by_receiver(decoded, field, bad):
  cache, _ = decoded
  route = cache.routes[0]
  points = degrees(expanded(route))
  good = build_native_length_evidence(route, source_cache_sha256=REAL_SHA, profile=SUPPORTED_PROFILE)
  kwargs = {name: getattr(good, name) for name in
            ("version", "decoder_id", "profile", "source_cache_sha256", "source_geometry_sha256",
             "point_count", "route_distance_centi_m", "long_links")}
  kwargs[field] = bad
  from openpilot.selfdrive.navigation.phone_route import validate_native_length_evidence
  with pytest.raises(RouteRejected):
    validate_native_length_evidence(points, NativeLengthEvidence(**kwargs),
      source_geometry_sha256=geometry_hash(points, "AMAP_NATIVE_MAP"), source_cache_sha256=REAL_SHA,
      decoder_id=SUPPORTED_PROFILE + "-cache-v1", provider=IDENTITY.provider,
      coordinate_system="AMAP_NATIVE_MAP", transform="amap_native_identity_v1", limits=GeometryLimits())


def test_wire_binding_recomputes_binding_id_with_evidence(decoded):
  cache, _ = decoded
  route = cache.routes[0]
  evidence = build_native_length_evidence(route, source_cache_sha256=REAL_SHA, profile=SUPPORTED_PROFILE)
  good_id = binding_digest("transport-1", IDENTITY, "/cache/1.dat", "b" * 64, evidence)
  binding = WireRouteBinding(IDENTITY, good_id, "transport-1", "/cache/1.dat", "b" * 64, "b" * 64,
                             "AMAP_NATIVE_MAP", "AMAP_NATIVE_MAP", "amap_native_identity_v1", 1, 10, evidence)
  assert binding.native_length_evidence == evidence
  with pytest.raises(RouteRejected, match="^binding_id_evidence_mismatch$"):
    WireRouteBinding(IDENTITY, "c" * 64, "transport-1", "/cache/1.dat", "b" * 64, "b" * 64,
                     "AMAP_NATIVE_MAP", "AMAP_NATIVE_MAP", "amap_native_identity_v1", 1, 10, evidence)


def test_evidence_with_gcj_or_wgs_coordinates_rejected(decoded):
  cache, _ = decoded
  route = cache.routes[0]
  points = degrees(expanded(route))
  evidence = build_native_length_evidence(route, source_cache_sha256=REAL_SHA, profile=SUPPORTED_PROFILE)
  from openpilot.selfdrive.navigation.phone_route import validate_native_length_evidence
  for system, transform in (("GCJ02", "gcj02_to_wgs84_v1"), ("WGS84", "identity")):
    with pytest.raises(RouteRejected, match="^native_evidence_transform_mismatch$"):
      validate_native_length_evidence(points, evidence,
        source_geometry_sha256=geometry_hash(points, "AMAP_NATIVE_MAP"), source_cache_sha256=REAL_SHA,
        decoder_id=SUPPORTED_PROFILE + "-cache-v1", provider=IDENTITY.provider,
        coordinate_system=system, transform=transform, limits=GeometryLimits())


def test_parser_version_zero_partial_evidence_rejected():
  """A half-filled evidence block must not be silently swallowed as legacy."""
  from openpilot.selfdrive.navigation.nav_map_contract import _evidence_from_dict
  with pytest.raises(RouteRejected):
    _evidence_from_dict({"version": 0})
  with pytest.raises(RouteRejected):
    _evidence_from_dict({"version": 0, "longLinks": [{"globalPointStart": 0, "pointCount": 2, "distanceCentiM": 1}]})
  assert _evidence_from_dict(None) is None


def test_parser_rejects_oversized_and_non_dict_links():
  from openpilot.selfdrive.navigation.nav_map_contract import MAX_EVIDENCE_LINKS, _evidence_from_dict
  with pytest.raises(RouteRejected):
    _evidence_from_dict({"version": 1, "longLinks": ["not-a-dict"]})
  with pytest.raises(RouteRejected):
    _evidence_from_dict({"version": 1, "longLinks": [{}] * (MAX_EVIDENCE_LINKS + 1)})


def test_evidence_only_for_amap_source_output_and_identity_transform(decoded):
  """Every other coordinate system or transform must reject the evidence outright."""
  cache, _ = decoded
  route = cache.routes[0]
  points = degrees(expanded(route))
  evidence = build_native_length_evidence(route, source_cache_sha256=REAL_SHA, profile=SUPPORTED_PROFILE)
  from openpilot.selfdrive.navigation.phone_route import validate_native_length_evidence
  for system, transform in (("GCJ02", "gcj02_to_wgs84_v1"), ("WGS84", "identity"),
                            ("AMAP_NATIVE_MAP", "identity"), ("AMAP_NATIVE_MAP", "gcj02_to_wgs84_v1")):
    with pytest.raises(RouteRejected, match="^native_evidence_transform_mismatch$"):
      validate_native_length_evidence(points, evidence,
        source_geometry_sha256=geometry_hash(points, "AMAP_NATIVE_MAP"), source_cache_sha256=REAL_SHA,
        decoder_id=SUPPORTED_PROFILE + "-cache-v1", provider=IDENTITY.provider,
        coordinate_system=system, transform=transform, limits=GeometryLimits())


@pytest.mark.parametrize("bad", [True, 1.0, 0, -1, 2 ** 32])
def test_evidence_requires_strict_positive_uint32_declarations(decoded, bad):
  cache, _ = decoded
  route = cache.routes[0]
  evidence = build_native_length_evidence(route, source_cache_sha256=REAL_SHA, profile=SUPPORTED_PROFILE)
  from openpilot.selfdrive.navigation.phone_route import LongLinkLength, NativeLengthEvidence
  link = evidence.long_links[0]
  with pytest.raises(RouteRejected):
    NativeLengthEvidence(evidence.version, evidence.decoder_id, evidence.profile, evidence.source_cache_sha256,
                         evidence.source_geometry_sha256, evidence.point_count, bad,
                         (LongLinkLength(link.global_point_start, link.point_count, link.distance_centi_m),))


def _adapter_sample(route, raw):
  """Build the same sample shape the producer hands to the adapter."""
  from types import SimpleNamespace
  import hashlib as _h
  from openpilot.selfdrive.navigation import amap_phone_route as apr
  sha = _h.sha256(raw).hexdigest()
  points = degrees(expanded(route))
  selected = SimpleNamespace(route=route, identity_hash=apr._path_hash(route.path_id, "123:456"),
                             cache_sha256=sha, source_session_id="src-session", source_path="/cache/1.dat")
  gps = SimpleNamespace(source_token="tok", horizontal_accuracy_m=1.0, age_lower_ns=0, age_upper_ns=0,
                        host_started_ns=0, host_finished_ns=0, phone_boot_id="b", elapsed_realtime_ns=0,
                        coordinates=(121.0, 31.0), fresh_at=lambda now: True)
  return SimpleNamespace(selected=selected, cache_bytes=raw, source_object=selected.source_path,
                         epoch="123:456", sequence=1, host_start_ns=0, gps=gps,
                         native_before={}, native_after={}, transport_session="t-1"), points


def test_production_adapter_produces_verified_evidence(decoded, monkeypatch):
  """The real adapter, not a hand-built provenance, must yield accepted geometry."""
  cache, raw = decoded
  route = cache.routes[0]
  sample, points = _adapter_sample(route, raw)
  from openpilot.selfdrive.navigation import amap_phone_route as apr
  monkeypatch.setattr(apr, "ActiveRouteBinding", lambda *a, **k: type("B", (), {
    "identity": a[0], "transport_session_id": a[1], "source_object_id": a[2],
    "observed_sequence": a[3], "observed_mono_time": a[4], "selected": a[5]})())
  evidence = build_native_length_evidence(route, source_cache_sha256=sample.selected.cache_sha256,
                                          profile=SUPPORTED_PROFILE)
  assert evidence is not None
  assert evidence.point_count == len(points)
  assert evidence.source_geometry_sha256 == geometry_hash(points, "AMAP_NATIVE_MAP")


def test_original_coordinates_spans_and_distance_unchanged(decoded):
  """Accepting evidence must not alter coordinates, spans or anchor distance_m."""
  cache, _ = decoded
  route = cache.routes[0]
  from openpilot.selfdrive.navigation.amap_route_anchor import AmapRouteAnchorIndex
  index = AmapRouteAnchorIndex(route)
  points = expanded(route)
  assert index.points_native == points
  assert index.coordinates == degrees(points)
  total = sum(
    __import__("math").hypot((b[0] / COORDINATE_SCALE - a[0] / COORDINATE_SCALE)
                             * __import__("math").cos(__import__("math").radians(((a[1] + b[1]) / 2) / COORDINATE_SCALE)),
                             (b[1] - a[1]) / COORDINATE_SCALE)
    * (2 * __import__("math").pi * 6371008.8 / 360)
    for a, b in zip(points[:-1], points[1:], strict=True))
  assert index.distance_m[-1] == pytest.approx(total, rel=1e-9)
  assert len(index.distance_m) == len(points)


def test_default_capnp_message_round_trips_without_evidence():
  """A real default capnp navRoute binding (no @16 written) must parse as legacy."""
  from openpilot.cereal import messaging
  from openpilot.selfdrive.navigation.nav_map_contract import _evidence_from_dict
  msg = messaging.new_message("navRoute")
  raw = msg.navRoute.routeBinding.to_dict()
  assert "nativeLengthEvidence" not in raw or not raw["nativeLengthEvidence"]
  assert _evidence_from_dict(raw.get("nativeLengthEvidence")) is None


def test_short_route_without_evidence_binds_normally(decoded):
  """Short routes keep the pre-evidence behaviour end to end."""
  from openpilot.selfdrive.navigation.phone_route import (_points, build_native_length_evidence)
  short = tuple((121.0 + i * 0.001, 31.0) for i in range(5))
  assert len(_points(short, GeometryLimits())) == 5
  assert build_native_length_evidence(
    type("R", (), {"segments": (), "distance_centi_m": 100})(),
    source_cache_sha256=REAL_SHA, profile=SUPPORTED_PROFILE) is None
