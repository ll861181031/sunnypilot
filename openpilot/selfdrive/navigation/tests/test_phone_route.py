"""Offline contracts for phone geometry, independent provenance, and active leases."""
from dataclasses import FrozenInstanceError, replace
import hashlib
import math

import pytest

from openpilot.common.gcj import gcj02_to_wgs84
from openpilot.selfdrive.navigation import phone_route


SOURCE_BYTES = b"independently decoded complete route fixture"
POINTS = ((106.414900123456, 31.030100123456), (106.415000234567, 31.030100234567),
          (106.415100345678, 31.030200345678))


def payload():
  return {
    "provider": "amap", "source_session_id": "amap-instance-1", "route_id": "provider-route-42",
    "revision": 3, "cache_sha256": hashlib.sha256(SOURCE_BYTES).hexdigest(),
    "coordinate_system": "WGS84", "coordinate_order": "lon_lat", "geometry_kind": "full_route",
    "complete": True, "point_count": len(POINTS), "points": [list(point) for point in POINTS],
  }


def identity(data=None):
  data = payload() if data is None else data
  return phone_route.RouteIdentity(*(data[key] for key in
    ("provider", "source_session_id", "route_id", "revision", "cache_sha256")))


def provenance(data=None, **kwargs):
  data = payload() if data is None else data
  args = {"identity": identity(data), "source_bytes": SOURCE_BYTES, "source_object_id": "active-cache-object-1",
          "decoder_id": "fixture-decoder-v1", "coordinate_system": data["coordinate_system"],
          "decoded_points": data["points"], "expected_point_count": len(data["points"]), "complete": True}
  args.update(kwargs)
  return phone_route.SourceProvenance.from_decoded(**args)


def binding(data=None, **kwargs):
  args = {"identity": identity(data), "transport_session_id": "transport-1", "source_object_id": "active-cache-object-1",
          "observed_sequence": 1, "observed_mono_time": 10.0, "selected": True}
  args.update(kwargs)
  return phone_route.ActiveRouteBinding(**args)


def bind(data=None, source=None, active=None, **kwargs):
  data = payload() if data is None else data
  return phone_route.bind_geometry(data, provenance=provenance(data) if source is None else source,
                                   active_binding=binding(data) if active is None else active,
                                   transport_session_id="transport-1", now=10.1, **kwargs)


def test_payload_claims_cannot_replace_independent_provenance():
  data = payload() | {"verified": True, "source_verified": True, "schema_version": 999}
  with pytest.raises(phone_route.RouteRejected, match="missing_provenance"):
    phone_route.bind_geometry(data, provenance=None, active_binding=None,
                              transport_session_id="transport-1", now=10.1)


def test_geometry_source_does_not_prove_active_selection():
  with pytest.raises(phone_route.RouteRejected, match="missing_active_binding"):
    phone_route.bind_geometry(payload(), provenance=provenance(), active_binding=None,
                              transport_session_id="transport-1", now=10.1)


def test_snapshot_is_deeply_immutable_and_preserves_double_precision():
  data = payload()
  snapshot = bind(data)
  data["points"][0][0] = 0
  assert snapshot.coordinates == POINTS
  assert snapshot.coordinates[0][0] == 106.414900123456
  assert snapshot.coordinate_system == "WGS84"
  assert snapshot.source_coordinate_system == "WGS84"
  assert snapshot.transform == "identity"
  assert len(snapshot.binding_id) == 64
  with pytest.raises(FrozenInstanceError):
    snapshot.identity = identity()
  with pytest.raises(TypeError):
    snapshot.coordinates[0][0] = 0


@pytest.mark.parametrize("field,value", [("provider", "other"), ("source_session_id", "amap-instance-2"),
  ("route_id", "provider-route-43"), ("revision", 4), ("cache_sha256", "a" * 64)])
def test_each_route_identity_component_must_match_independent_source(field, value):
  data = payload()
  source = provenance(data)
  data[field] = value
  with pytest.raises(phone_route.RouteRejected, match="provenance_identity_mismatch"):
    bind(data, source=source)


@pytest.mark.parametrize("field,value", [("provider", "other"), ("source_session_id", "amap-instance-2"),
  ("route_id", "provider-route-43"), ("revision", 4), ("cache_sha256", "b" * 64)])
def test_each_active_identity_component_must_match(field, value):
  active = binding(identity=replace(identity(), **{field: value}))
  with pytest.raises(phone_route.RouteRejected, match="active_identity_mismatch"):
    bind(active=active)


def test_source_object_and_transport_session_must_match():
  with pytest.raises(phone_route.RouteRejected, match="source_object_mismatch"):
    bind(active=binding(source_object_id="different-cache-object"))
  with pytest.raises(phone_route.RouteRejected, match="transport_session_mismatch"):
    bind(active=binding(transport_session_id="transport-2"))
  with pytest.raises(phone_route.RouteRejected, match="route_not_selected"):
    bind(active=binding(selected=False))


def test_source_bytes_are_hashed_independently():
  with pytest.raises(phone_route.RouteRejected, match="cache_hash_mismatch"):
    provenance(source_bytes=b"different cache contents")


def native_payload():
  data = payload() | {
    "coordinate_system": "AMAP_NATIVE_MAP",
    "geometry_kind": "current_native_path_window",
    "source_geometry_sha256": "f" * 64,
  }
  return data


def native_source(data=None):
  data = native_payload() if data is None else data
  return phone_route.SourceProvenance.from_native_window(
    identity=identity(data), source_geometry_sha256=data["source_geometry_sha256"],
    source_object_id="native-drive-path:" + data["source_geometry_sha256"],
    decoder_id="native-drive-path-v1", decoded_points=data["points"],
    expected_point_count=len(data["points"]))


def test_native_window_binds_exact_geometry_without_cache_length_evidence():
  data = native_payload()
  active = binding(data, source_object_id="native-drive-path:" + data["source_geometry_sha256"])
  snapshot = phone_route.bind_geometry(
    data, provenance=native_source(data), active_binding=active,
    transport_session_id="transport-1", now=10.1)
  assert snapshot.coordinate_system == "AMAP_NATIVE_MAP"
  assert snapshot.source_coordinate_system == "AMAP_NATIVE_MAP"
  assert snapshot.native_length_evidence is None
  assert snapshot.source_geometry_sha256 == "f" * 64
  assert snapshot.coordinates == POINTS


@pytest.mark.parametrize("field,value", [
  ("geometry_kind", "full_route"),
  ("source_geometry_sha256", "e" * 64),
  ("source_geometry_sha256", None),
])
def test_native_window_hash_and_kind_cannot_drift_from_provenance(field, value):
  data = native_payload()
  source = native_source()
  data[field] = value
  active = binding(native_payload(), source_object_id="native-drive-path:" + "f" * 64)
  with pytest.raises(phone_route.RouteRejected):
    phone_route.bind_geometry(data, provenance=source, active_binding=active,
                              transport_session_id="transport-1", now=10.1)


def test_same_valid_shape_but_different_points_fail_provenance_hash():
  data = payload()
  source = provenance(data)
  data["points"][1][0] += 0.00001
  with pytest.raises(phone_route.RouteRejected, match="geometry_hash_mismatch"):
    bind(data, source=source)


def test_same_points_cannot_be_relabelled_with_another_coordinate_system():
  data = payload()
  source = provenance(data)
  data["coordinate_system"] = "GCJ02"
  with pytest.raises(phone_route.RouteRejected, match="source_coordinate_system_mismatch"):
    bind(data, source=source)


@pytest.mark.parametrize("kwargs", [{"complete": False}, {"expected_point_count": 10},
  {"decoder_id": ""}, {"source_object_id": ""}])
def test_unproven_decoder_coverage_cannot_create_provenance(kwargs):
  with pytest.raises(phone_route.RouteRejected):
    provenance(**kwargs)


@pytest.mark.parametrize("field,value", [("complete", False), ("complete", 1), ("geometry_kind", "remaining_route"),
  ("point_count", 4), ("point_count", True), ("coordinate_order", "lat_lon"),
  ("coordinate_system", "unknown"), ("coordinate_system", ""), ("revision", True),
  ("revision", -1), ("cache_sha256", "not-a-hash"), ("source_session_id", "")])
def test_malformed_or_incomplete_payload_is_rejected(field, value):
  data = payload()
  source = provenance(data)
  data[field] = value
  with pytest.raises(phone_route.RouteRejected):
    bind(data, source=source)


@pytest.mark.parametrize("point", [(math.nan, 31.0), (106.0, math.inf), (True, 31.0), ("106", 31.0),
  (181.0, 31.0), (106.0, 91.0), (10 ** 400, 31.0), (106.0,), (106.0, 31.0, 1.0)])
def test_invalid_coordinates_are_rejected_before_conversion(point):
  data = payload()
  data["points"][1] = point
  with pytest.raises(phone_route.RouteRejected):
    phone_route.validate_geometry(data)


@pytest.mark.parametrize("points", [[], [POINTS[0]], [POINTS[0]] * 3,
  [(106.0, 31.0), (107.0, 31.0), (108.0, 31.0)]])
def test_empty_degenerate_or_discontinuous_geometry_is_rejected(points):
  data = payload() | {"points": points, "point_count": len(points)}
  with pytest.raises(phone_route.RouteRejected):
    phone_route.validate_geometry(data)


def test_point_limit_is_checked_before_iterating_payload():
  class Oversized(list):
    def __iter__(self):
      raise AssertionError("oversized payload must not be iterated")

  data = payload() | {"points": Oversized(POINTS), "point_count": 3}
  with pytest.raises(phone_route.RouteRejected, match="point_limit"):
    phone_route.validate_geometry(data, limits=phone_route.GeometryLimits(max_points=2))


def test_gcj_conversion_happens_once_and_source_hash_stays_original(monkeypatch):
  data = payload() | {"coordinate_system": "GCJ02"}
  source = provenance(data)
  calls = []

  def counted(lon, lat):
    calls.append((lon, lat))
    return gcj02_to_wgs84(lon, lat)

  monkeypatch.setattr(phone_route, "gcj02_to_wgs84", counted)
  snapshot = bind(data, source=source)
  assert calls == list(POINTS)
  assert snapshot.coordinates == tuple(gcj02_to_wgs84(*point) for point in POINTS)
  assert snapshot.source_geometry_sha256 == source.geometry_sha256
  assert snapshot.source_coordinate_system == "GCJ02"
  assert snapshot.coordinate_system == "WGS84"
  assert snapshot.transform == "gcj02_to_wgs84_v1"
  with pytest.raises(phone_route.RouteRejected, match="invalid_payload"):
    phone_route.validate_geometry(snapshot)


def test_wgs84_is_never_run_through_gcj_conversion(monkeypatch):
  def forbidden(*args):
    raise AssertionError("WGS84 must not be converted again")

  monkeypatch.setattr(phone_route, "gcj02_to_wgs84", forbidden)
  assert bind().coordinates == POINTS


@pytest.mark.parametrize("observed,now", [(11.0, 10.0), (6.9, 10.0), (math.nan, 10.0),
  (10.0, math.inf), (10.0, 9.0), (-0.1, 0.1), (0.0, -0.1)])
def test_binding_requires_fresh_nonfuture_local_monotonic_observation(observed, now):
  with pytest.raises(phone_route.RouteRejected):
    phone_route.bind_geometry(payload(), provenance=provenance(),
                              active_binding=binding(observed_mono_time=observed),
                              transport_session_id="transport-1", now=now)


def lease():
  result = phone_route.PhoneRouteLease("transport-1", max_age_s=3.0)
  assert result.observe(binding(), now=10.1)
  assert result.offer(payload(), provenance(), now=10.1)
  return result


def test_repeated_snapshot_or_send_time_does_not_renew_lease():
  state = lease()
  assert not state.observe(binding(observed_mono_time=12.9), now=12.9)
  assert state.offer(payload(), provenance(), now=12.9)
  assert state.snapshot(now=13.01) is None
  assert state.last_reason == "binding_stale"


def test_new_sequence_without_new_observation_cannot_renew_lease():
  state = lease()
  assert not state.observe(binding(observed_sequence=2), now=12.0)
  assert state.snapshot(now=13.01) is None


def test_advancing_real_observation_renews_without_reconverting(monkeypatch):
  state = lease()
  monkeypatch.setattr(phone_route, "validate_geometry", lambda *a, **k: pytest.fail("heartbeat must not reinterpret geometry"))
  assert state.observe(binding(observed_sequence=2, observed_mono_time=12.5), now=12.6)
  assert state.snapshot(now=15.4) is not None
  assert state.snapshot(now=15.51) is None


def test_route_change_clears_previous_geometry_before_new_payload():
  state = lease()
  next_data = payload() | {"route_id": "provider-route-43", "revision": 4}
  assert state.observe(binding(next_data, observed_sequence=2, observed_mono_time=11.0), now=11.1)
  assert state.snapshot(now=11.1) is None
  assert not state.offer(payload(), provenance(), now=11.1)
  assert state.snapshot(now=11.1) is None
  assert state.offer(next_data, provenance(next_data), now=11.2)
  assert state.snapshot(now=11.2).identity.route_id == "provider-route-43"


def test_withdrawal_invalidates_and_blocks_old_observations():
  state = lease()
  state.withdraw(now=11.0)
  assert state.snapshot(now=11.1) is None
  assert not state.observe(binding(), now=11.1)
  assert not state.offer(payload(), provenance(), now=11.1)
  assert state.observe(binding(observed_sequence=2, observed_mono_time=11.2), now=11.3)
  assert state.snapshot(now=11.3) is None
  assert state.offer(payload(), provenance(), now=11.3)


def test_explicit_unselected_observation_withdraws_geometry():
  state = lease()
  assert state.observe(binding(selected=False, observed_sequence=2, observed_mono_time=11.0), now=11.1)
  assert state.snapshot(now=11.2) is None
  assert not state.offer(payload(), provenance(), now=11.2)


def test_cache_object_change_clears_geometry_even_with_same_route_identity():
  state = lease()
  assert state.observe(binding(source_object_id="cache-object-2", observed_sequence=2, observed_mono_time=11.0), now=11.1)
  assert state.snapshot(now=11.1) is None
  assert not state.offer(payload(), provenance(), now=11.1)
  assert state.offer(payload(), provenance(source_object_id="cache-object-2"), now=11.2)


def test_new_session_has_no_old_geometry_and_rejects_other_session_observation():
  state = phone_route.PhoneRouteLease("transport-2")
  assert not state.observe(binding(), now=10.1)
  assert not state.offer(payload(), provenance(), now=10.1)
  assert state.snapshot(now=10.1) is None


def test_source_instance_change_requires_explicit_transport_session_restart():
  state = lease()
  next_data = payload() | {"source_session_id": "amap-instance-2"}
  assert not state.observe(binding(next_data, observed_sequence=2, observed_mono_time=11.0), now=11.1)
  assert state.snapshot(now=11.1) is None


def test_clock_rollback_and_nonfinite_time_invalidate_cached_geometry():
  state = lease()
  assert state.snapshot(now=9.9) is None
  assert state.snapshot(now=10.2) is None
  state = lease()
  assert state.snapshot(now=math.nan) is None


def test_invalid_current_geometry_replaces_no_cached_map():
  state = lease()
  data = payload()
  data["points"][1][0] += 0.00001
  assert not state.offer(data, provenance(), now=10.2)
  assert state.snapshot(now=10.2) is None


def test_binding_id_tracks_route_identity_and_not_heartbeat():
  first = bind()
  assert first.binding_id == bind(active=binding(observed_sequence=2, observed_mono_time=10.05)).binding_id
  assert first.binding_id != bind(payload() | {"revision": 4}).binding_id
