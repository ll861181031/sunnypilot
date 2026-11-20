"""Golden compatibility for the legacy no-evidence binding digest (v1)."""
import hashlib
import struct

import pytest

from openpilot.selfdrive.navigation.phone_route import RouteIdentity, binding_digest


def legacy_digest(transport_session_id, identity, source_object_id, geometry_sha256):
  """The exact pre-evidence algorithm: v1 domain, eight length-prefixed parts, no flag byte."""
  digest = hashlib.sha256(b"phone-route-binding-v1\x00")
  for part in (transport_session_id, identity.provider, identity.source_session_id, identity.route_id,
               str(identity.revision), identity.cache_sha256, source_object_id, geometry_sha256):
    raw = part.encode("utf-8")
    digest.update(struct.pack("<I", len(raw)))
    digest.update(raw)
  return digest.hexdigest()


WGS_ID = RouteIdentity("amapauto-root-fixed-profile", "sess-wgs", "route-wgs", 0, "a" * 64)
GCJ_ID = RouteIdentity("amapauto-root-fixed-profile", "sess-gcj", "route-gcj", 3, "b" * 64)
AMAP_ID = RouteIdentity("amapauto-root-fixed-profile", "sess-amap", "route-amap", 0, "c" * 64)


@pytest.mark.parametrize("identity,geometry", [
  (WGS_ID, "d" * 64), (GCJ_ID, "e" * 64), (AMAP_ID, "f" * 64),
])
def test_no_evidence_digest_matches_legacy_bytes(identity, geometry):
  """Without evidence the digest must be byte-identical to the legacy v1 algorithm."""
  assert binding_digest("transport-x", identity, "/cache/x.dat", geometry, None) == \
    legacy_digest("transport-x", identity, "/cache/x.dat", geometry)


def test_evidence_digest_uses_separate_domain():
  """Evidence digests live in a different domain and never collide with v1 bytes."""
  legacy = legacy_digest("transport-x", AMAP_ID, "/cache/x.dat", "f" * 64)
  assert binding_digest("transport-x", AMAP_ID, "/cache/x.dat", "f" * 64, None) == legacy


def test_gcj_bind_geometry_uses_source_geometry_sha(monkeypatch):
  """GCJ binding must hash the source-coordinate geometry, exactly as before evidence existed."""
  from openpilot.selfdrive.navigation import phone_route as pr
  from openpilot.common.gcj import wgs84_to_gcj02

  base = wgs84_to_gcj02(121.5, 31.2)
  coordinates = ((base[0], base[1]), (base[0] + 0.01, base[1]))
  provenance = pr.SourceProvenance(GCJ_ID, "/cache/gcj.dat", "decoder", "GCJ02",
                                   pr.geometry_hash(coordinates, "GCJ02"), 2, 2)
  active = pr.ActiveRouteBinding(GCJ_ID, "t-gcj", "/cache/gcj.dat", 1, 0.0, True)
  payload = {"provider": GCJ_ID.provider, "source_session_id": GCJ_ID.source_session_id,
             "route_id": GCJ_ID.route_id, "revision": GCJ_ID.revision, "cache_sha256": GCJ_ID.cache_sha256,
             "coordinate_system": "GCJ02", "coordinate_order": "lon_lat", "geometry_kind": "full_route",
             "complete": True, "point_count": 2,
             "points": [list(p) for p in coordinates]}
  snapshot = pr.bind_geometry(payload, provenance=provenance, active_binding=active,
                              transport_session_id="t-gcj", now=0.0)
  expected = legacy_digest("t-gcj", GCJ_ID, "/cache/gcj.dat", pr.geometry_hash(coordinates, "GCJ02"))
  assert snapshot.binding_id == expected
  assert snapshot.transform == "gcj02_to_wgs84_v1"
  assert snapshot.source_geometry_sha256 == pr.geometry_hash(coordinates, "GCJ02")
