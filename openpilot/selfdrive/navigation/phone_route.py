"""Pure phone-route contracts, without publishing or changing instructions or controls.

A wire payload cannot establish its own provenance. Callers must independently supply
SourceProvenance from a trusted decoder and ActiveRouteBinding from an active-selection
observer, never deserialize either from payload verified/complete/version claims.
These consistency checks do not replace the decoder's format, ownership, or coverage checks.
"""
from dataclasses import dataclass, field, replace
import hashlib
import math
import re
import struct
from typing import Any

from openpilot.common.gcj import gcj02_to_wgs84
from tools.cplink_shim.amap_route_cache import SUPPORTED_PROFILE


class RouteRejected(ValueError):
  """Stable machine-readable rejection reason, without raw coordinates."""

  def __init__(self, reason: str):
    super().__init__(reason)
    self.reason = reason


def _text(value: Any, name: str, max_length: int = 256) -> str:
  if not isinstance(value, str) or not value.strip() or len(value) > max_length or "\x00" in value:
    raise RouteRejected(f"invalid_{name}")
  return value


def _integer(value: Any, name: str, minimum: int = 0) -> int:
  if type(value) is not int or not minimum <= value <= 2 ** 63 - 1:
    raise RouteRejected(f"invalid_{name}")
  return value


def _number(value: Any, name: str) -> float:
  if type(value) not in (int, float):
    raise RouteRejected(f"invalid_{name}")
  try:
    result = float(value)
  except (ValueError, OverflowError):
    raise RouteRejected(f"invalid_{name}") from None
  if not math.isfinite(result):
    raise RouteRejected(f"invalid_{name}")
  return result


def _sha256(value: Any, name: str) -> str:
  if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
    raise RouteRejected(f"invalid_{name}")
  return value


@dataclass(frozen=True)
class GeometryLimits:
  max_points: int = 100_000
  max_segment_m: float = 2_000.0

  def __post_init__(self):
    _integer(self.max_points, "max_points", 2)
    if _number(self.max_segment_m, "max_segment_m") <= 0:
      raise RouteRejected("invalid_max_segment_m")


DEFAULT_LIMITS = GeometryLimits()
COORDINATE_SYSTEMS = ("WGS84", "GCJ02", "AMAP_NATIVE_MAP")


@dataclass(frozen=True)
class RouteIdentity:
  provider: str
  source_session_id: str
  route_id: str
  revision: int
  cache_sha256: str

  def __post_init__(self):
    for name in ("provider", "source_session_id", "route_id"):
      _text(getattr(self, name), name)
    _integer(self.revision, "revision")
    _sha256(self.cache_sha256, "cache_sha256")


def _identity(data: dict) -> RouteIdentity:
  return RouteIdentity(*(data.get(name) for name in
    ("provider", "source_session_id", "route_id", "revision", "cache_sha256")))


def _edge_length(start: tuple[float, float], end: tuple[float, float]) -> float:
  """Great-circle length in metres; the single length algorithm used by every gate."""
  dlon = math.radians((end[0] - start[0] + 180) % 360 - 180)
  dlat = math.radians(end[1] - start[1])
  a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(start[1])) * math.cos(math.radians(end[1])) * math.sin(dlon / 2) ** 2
  return 2 * 6371008.8 * math.asin(math.sqrt(max(0.0, min(1.0, a))))


def _normalize_points(value: Any, limits: GeometryLimits) -> tuple[tuple[float, float], ...]:
  """Shape, range, finiteness, count and degeneracy only; no per-edge length gate."""
  if not isinstance(value, (list, tuple)):
    raise RouteRejected("invalid_points")
  if len(value) > limits.max_points:
    raise RouteRejected("point_limit")
  if len(value) < 2:
    raise RouteRejected("insufficient_points")
  result = []
  for point in value:
    if not isinstance(point, (list, tuple)) or len(point) != 2:
      raise RouteRejected("invalid_point")
    lon, lat = (_number(coordinate, "coordinate") for coordinate in point)
    if not -180 <= lon <= 180 or not -90 <= lat <= 90:
      raise RouteRejected("coordinate_out_of_range")
    result.append((lon, lat))
  if all(point == result[0] for point in result):
    raise RouteRejected("degenerate_geometry")
  return tuple(result)


def _points(value: Any, limits: GeometryLimits) -> tuple[tuple[float, float], ...]:
  """Normalized points plus the default per-edge length gate."""
  result = _normalize_points(value, limits)
  for start, end in zip(result[:-1], result[1:], strict=True):
    if _edge_length(start, end) > limits.max_segment_m:
      raise RouteRejected("discontinuous_geometry")
  return result


NATIVE_LENGTH_EVIDENCE_VERSION = 1
NATIVE_LENGTH_TOLERANCE = 0.02
NATIVE_PROVIDER = "amapauto-root-fixed-profile"
NATIVE_COORDINATE_SYSTEM = "AMAP_NATIVE_MAP"
NATIVE_TRANSFORM = "amap_native_identity_v1"


@dataclass(frozen=True)
class LongLinkLength:
  """One decoded link that contains at least one edge beyond the default limit.

  global_point_start/count index the fully expanded route point sequence;
  distance_centi_m is the link's own declared length in centimetres.
  """
  global_point_start: int
  point_count: int
  distance_centi_m: int

  def __post_init__(self):
    _integer(self.global_point_start, "global_point_start", 0)
    _integer(self.point_count, "point_count", 2)
    _uint32(self.distance_centi_m, "link_distance_centi_m")
    if self.distance_centi_m <= 0:
      raise RouteRejected("invalid_link_distance_centi_m")


@dataclass(frozen=True)
class NativeLengthEvidence:
  """Per-link declared lengths that let a verifier recompute long-edge agreement.

  This is integrity binding, not cryptographic authentication: a verifier must
  re-derive every length from the actual coordinates before trusting it.
  """
  version: int
  decoder_id: str
  profile: str
  source_cache_sha256: str
  source_geometry_sha256: str
  point_count: int
  route_distance_centi_m: int
  long_links: tuple[LongLinkLength, ...] = field(repr=False)

  def __post_init__(self):
    if type(self.version) is not int or self.version != NATIVE_LENGTH_EVIDENCE_VERSION:
      raise RouteRejected("unsupported_native_length_evidence")
    _text(self.decoder_id, "decoder_id")
    _text(self.profile, "profile")
    _sha256(self.source_cache_sha256, "source_cache_sha256")
    _sha256(self.source_geometry_sha256, "source_geometry_sha256")
    _uint32(self.point_count, "point_count")
    if self.point_count < 2:
      raise RouteRejected("invalid_point_count")
    _uint32(self.route_distance_centi_m, "route_distance_centi_m")
    if self.route_distance_centi_m <= 0:
      raise RouteRejected("invalid_route_distance_centi_m")
    if not isinstance(self.long_links, tuple) or not self.long_links:
      raise RouteRejected("invalid_long_links")
    for link in self.long_links:
      if not isinstance(link, LongLinkLength):
        raise RouteRejected("invalid_long_link")
      if link.distance_centi_m > self.route_distance_centi_m:
        raise RouteRejected("link_distance_exceeds_route")

  def digest(self) -> str:
    """Canonical digest over every evidence field, for binding into binding_id."""
    digest = hashlib.sha256(b"phone-native-length-evidence-v1\x00")
    for part in (str(self.version), self.decoder_id, self.profile, self.source_cache_sha256,
                 self.source_geometry_sha256, str(self.point_count), str(self.route_distance_centi_m)):
      raw = part.encode("utf-8")
      digest.update(struct.pack("<I", len(raw)))
      digest.update(raw)
    digest.update(struct.pack("<I", len(self.long_links)))
    for link in self.long_links:
      digest.update(struct.pack("<QQQ", link.global_point_start, link.point_count, link.distance_centi_m))
    return digest.hexdigest()


def _within(geometric_m: float, declared_m: float) -> bool:
  return declared_m > 0 and abs(geometric_m - declared_m) / declared_m <= NATIVE_LENGTH_TOLERANCE


def _proof_points(value: Any, limits: GeometryLimits, evidence: NativeLengthEvidence | None,
                  identity: RouteIdentity, coordinate_system: str, decoder_id: str,
                  source_cache_sha256: str | None = None):
  """Normalized points, gated either by evidence re-verification or the default 2 km rule."""
  if evidence is None:
    return _points(value, limits)
  validated = validate_native_length_evidence(
    value, evidence, source_geometry_sha256=geometry_hash(_normalize_points(value, limits), coordinate_system),
    source_cache_sha256=source_cache_sha256 or identity.cache_sha256, decoder_id=decoder_id,
    provider=identity.provider, coordinate_system=coordinate_system, transform=NATIVE_TRANSFORM, limits=limits)
  return _normalize_points(value, validated and limits or limits)


def build_native_length_evidence(route: Any, *, source_cache_sha256: str, profile: str) -> NativeLengthEvidence | None:
  """Build evidence only from one decoded route's own spans; None when no edge is long.

  source_cache_sha256 must come from the same identity that selected this route.
  """
  _sha256(source_cache_sha256, "source_cache_sha256")
  from tools.cplink_shim.amap_route_cache import COORDINATE_SCALE

  points: list[tuple[int, int]] = []
  spans: list[tuple[int, int, int]] = []
  for segment in route.segments:
    native = tuple(segment.points_native)
    offset = len(points) - 1 if points else 0
    for link in segment.links:
      spans.append((offset + link.point_start, link.point_count, link.distance_centi_m))
    points.extend(native[1:] if points else native)
  coordinates = tuple((x / COORDINATE_SCALE, y / COORDINATE_SCALE) for x, y in points)
  lengths = tuple(_edge_length(a, b) for a, b in zip(coordinates[:-1], coordinates[1:], strict=True))
  long_links = tuple(LongLinkLength(start, count, declared) for start, count, declared in spans
                     if any(length > GeometryLimits().max_segment_m
                            for length in lengths[start:start + count - 1]))
  if not long_links:
    return None
  return NativeLengthEvidence(NATIVE_LENGTH_EVIDENCE_VERSION, profile + "-cache-v1", profile,
                              source_cache_sha256, geometry_hash(coordinates, NATIVE_COORDINATE_SYSTEM),
                              len(coordinates), route.distance_centi_m, long_links)


def validate_native_length_evidence(coordinates, evidence, *, source_geometry_sha256, source_cache_sha256,
                                    decoder_id, provider, coordinate_system, transform,
                                    limits: GeometryLimits = DEFAULT_LIMITS) -> NativeLengthEvidence:
  """Recompute every length from `coordinates`; never trust the declared values alone."""
  if not isinstance(evidence, NativeLengthEvidence):
    raise RouteRejected("missing_native_length_evidence")
  if source_geometry_sha256 != evidence.source_geometry_sha256:
    raise RouteRejected("native_evidence_geometry_mismatch")
  if source_cache_sha256 != evidence.source_cache_sha256:
    raise RouteRejected("native_evidence_cache_mismatch")
  if decoder_id != evidence.decoder_id or evidence.decoder_id != NATIVE_DECODER_ID:
    raise RouteRejected("native_evidence_decoder_mismatch")
  if provider != NATIVE_PROVIDER or evidence.profile not in SUPPORTED_NATIVE_PROFILES:
    raise RouteRejected("native_evidence_profile_mismatch")
  if coordinate_system != NATIVE_COORDINATE_SYSTEM or transform != NATIVE_TRANSFORM:
    raise RouteRejected("native_evidence_transform_mismatch")
  # Shape/range/degeneracy unchanged; every long edge is judged below by evidence spans.
  points = _normalize_points(coordinates, limits)
  if len(points) != evidence.point_count:
    raise RouteRejected("native_evidence_point_count_mismatch")
  if geometry_hash(points, NATIVE_COORDINATE_SYSTEM) != evidence.source_geometry_sha256:
    raise RouteRejected("native_evidence_geometry_hash_mismatch")
  lengths = [_edge_length(a, b) for a, b in zip(points[:-1], points[1:], strict=True)]
  long_edges = {index for index, length in enumerate(lengths) if length > limits.max_segment_m}
  covered: set[int] = set()
  previous_end = -1
  for link in evidence.long_links:
    start, count = link.global_point_start, link.point_count
    if start < 0 or start + count > len(points):
      raise RouteRejected("native_evidence_link_out_of_bounds")
    if start < previous_end:
      raise RouteRejected("native_evidence_link_overlap")
    previous_end = start + count - 1
    link_edges = set(range(start, start + count - 1))
    if not link_edges & long_edges:
      raise RouteRejected("native_evidence_link_without_long_edge")
    if not _within(sum(lengths[start:start + count - 1]), link.distance_centi_m / 100):
      raise RouteRejected("native_evidence_link_length_mismatch")
    covered |= link_edges
  if long_edges - covered:
    raise RouteRejected("native_evidence_uncovered_long_edge")
  if not _within(sum(lengths), evidence.route_distance_centi_m / 100):
    raise RouteRejected("native_evidence_route_length_mismatch")
  return evidence


SUPPORTED_NATIVE_PROFILES = (SUPPORTED_PROFILE,)
NATIVE_DECODER_ID = SUPPORTED_PROFILE + "-cache-v1"


def _uint32(value: Any, name: str) -> int:
  if type(value) is not int or not 0 <= value <= 0xFFFFFFFF:
    raise RouteRejected(f"invalid_{name}")
  return value


def geometry_hash(points: tuple[tuple[float, float], ...], coordinate_system: str) -> str:
  """Canonical hash: version header, CRS, point count, and little-endian float64 lon/lat."""
  if coordinate_system not in COORDINATE_SYSTEMS:
    raise RouteRejected("unknown_coordinate_system")
  digest = hashlib.sha256(b"phone-route-geometry-v1\x00lon_lat\x00" + coordinate_system.encode("ascii") + b"\x00")
  digest.update(struct.pack("<Q", len(points)))
  for lon, lat in points:
    # Normalize signed zero so equivalent coordinates hash identically across languages.
    digest.update(struct.pack("<dd", lon if lon else 0.0, lat if lat else 0.0))
  return digest.hexdigest()


@dataclass(frozen=True)
class ValidatedGeometry:
  identity: RouteIdentity
  coordinate_system: str
  coordinates: tuple[tuple[float, float], ...]
  geometry_sha256: str


def validate_geometry(payload: Any, *, limits: GeometryLimits = DEFAULT_LIMITS,
                      native_length_evidence: NativeLengthEvidence | None = None,
                      geometry_kind: str = "full_route") -> ValidatedGeometry:
  """Validate full geometry shape without asserting provenance or active selection."""
  if not isinstance(payload, dict):
    raise RouteRejected("invalid_payload")
  identity = _identity(payload)
  if (payload.get("complete") is not True or
      payload.get("geometry_kind") not in ("full_route", "current_native_path_window")):
    raise RouteRejected("incomplete_geometry")
  if geometry_kind not in ("full_route", "current_native_path_window"):
    raise RouteRejected("unknown_geometry_kind")
  if geometry_kind == "current_native_path_window" and (
      payload.get("geometry_kind") != geometry_kind or native_length_evidence is not None
      or payload.get("coordinate_system") != NATIVE_COORDINATE_SYSTEM):
    raise RouteRejected("native_window_geometry_invalid")
  if payload.get("coordinate_order") != "lon_lat":
    raise RouteRejected("unknown_coordinate_order")
  system = payload.get("coordinate_system")
  if system not in COORDINATE_SYSTEMS:
    raise RouteRejected("unknown_coordinate_system")
  count = _integer(payload.get("point_count"), "point_count", 2)
  if count > limits.max_points:
    raise RouteRejected("point_limit")
  points = _normalize_points(payload.get("points"), limits)
  if geometry_kind == "current_native_path_window":
    _points(points, limits)
  elif native_length_evidence is not None:
    validate_native_length_evidence(points, native_length_evidence,
      source_geometry_sha256=geometry_hash(points, system), source_cache_sha256=identity.cache_sha256,
      decoder_id=native_length_evidence.decoder_id, provider=identity.provider, coordinate_system=system,
      transform=NATIVE_TRANSFORM, limits=limits)
  else:
    _points(points, limits)
  if count != len(points):
    raise RouteRejected("point_count_mismatch")
  digest = geometry_hash(points, system)
  if "geometry_sha256" in payload and _sha256(payload["geometry_sha256"], "geometry_sha256") != digest:
    raise RouteRejected("payload_geometry_hash_mismatch")
  return ValidatedGeometry(identity, system, points, digest)


@dataclass(frozen=True)
class SourceProvenance:
  """Construct only in an independent trusted decoder, never from a same-named wire object.

  The factory's complete flag means that decoder already verified full route coverage.
  Source bytes, decoded geometry, point counts, and source object are bound together;
  these still cannot establish which route is currently selected.
  """
  identity: RouteIdentity
  source_object_id: str
  decoder_id: str
  coordinate_system: str
  geometry_sha256: str
  decoded_point_count: int
  expected_point_count: int
  native_length_evidence: NativeLengthEvidence | None = None
  geometry_kind: str = "full_route"
  source_geometry_sha256: str | None = None

  def __post_init__(self):
    if self.native_length_evidence is not None:
      if not isinstance(self.native_length_evidence, NativeLengthEvidence):
        raise RouteRejected("invalid_native_length_evidence")
      if self.native_length_evidence.source_geometry_sha256 != self.geometry_sha256:
        raise RouteRejected("native_evidence_geometry_mismatch")
      if self.native_length_evidence.source_cache_sha256 != self.identity.cache_sha256:
        raise RouteRejected("native_evidence_cache_mismatch")
      if self.native_length_evidence.point_count != self.decoded_point_count:
        raise RouteRejected("native_evidence_point_count_mismatch")
      if self.coordinate_system != NATIVE_COORDINATE_SYSTEM:
        raise RouteRejected("native_evidence_transform_mismatch")
    if not isinstance(self.identity, RouteIdentity):
      raise RouteRejected("invalid_provenance_identity")
    _text(self.source_object_id, "source_object_id", 4096)
    _text(self.decoder_id, "decoder_id")
    if self.coordinate_system not in COORDINATE_SYSTEMS:
      raise RouteRejected("unknown_coordinate_system")
    if self.geometry_kind not in ("full_route", "current_native_path_window"):
      raise RouteRejected("unknown_geometry_kind")
    if self.geometry_kind == "current_native_path_window" and (
        self.coordinate_system != NATIVE_COORDINATE_SYSTEM or self.native_length_evidence is not None):
      raise RouteRejected("native_window_provenance_invalid")
    if self.source_geometry_sha256 is not None:
      _sha256(self.source_geometry_sha256, "source_geometry_sha256")
      if self.geometry_kind != "current_native_path_window":
        raise RouteRejected("unexpected_source_geometry_sha256")
    elif self.geometry_kind == "current_native_path_window":
      raise RouteRejected("native_window_provenance_invalid")
    _sha256(self.geometry_sha256, "geometry_sha256")
    _integer(self.decoded_point_count, "decoded_point_count", 2)
    _integer(self.expected_point_count, "expected_point_count", 2)
    if self.decoded_point_count != self.expected_point_count:
      raise RouteRejected("incomplete_decoder_coverage")

  @classmethod
  def from_decoded(cls, *, identity: RouteIdentity, source_bytes: bytes, source_object_id: str,
                   decoder_id: str, coordinate_system: str, decoded_points: Any,
                   expected_point_count: int, complete: bool, limits: GeometryLimits = DEFAULT_LIMITS,
                   native_length_evidence: NativeLengthEvidence | None = None):
    if complete is not True:
      raise RouteRejected("incomplete_decoder_coverage")
    if not isinstance(source_bytes, bytes) or not source_bytes:
      raise RouteRejected("invalid_source_bytes")
    if hashlib.sha256(source_bytes).hexdigest() != identity.cache_sha256:
      raise RouteRejected("cache_hash_mismatch")
    points = _proof_points(decoded_points, limits, native_length_evidence, identity, coordinate_system, decoder_id)
    return cls(identity, source_object_id, decoder_id, coordinate_system, geometry_hash(points, coordinate_system),
               len(points), expected_point_count, native_length_evidence, "full_route", None)

  @classmethod
  def from_native_window(cls, *, identity: RouteIdentity, source_geometry_sha256: str,
                         source_object_id: str, decoder_id: str, decoded_points: Any,
                         expected_point_count: int):
    """Bind a validated current IDrivePath window without claiming destination coverage."""
    points = _points(decoded_points, DEFAULT_LIMITS)
    _sha256(source_geometry_sha256, "source_geometry_sha256")
    return cls(identity, source_object_id, decoder_id, NATIVE_COORDINATE_SYSTEM,
               geometry_hash(points, NATIVE_COORDINATE_SYSTEM), len(points), expected_point_count,
               None, "current_native_path_window", source_geometry_sha256)


@dataclass(frozen=True)
class ActiveRouteBinding:
  """Independent selection observation using receiver-host monotonic time.

  Advance observed_sequence only after actually observing the active-selection source.
  Forwarding, serializing, or periodically resending a snapshot cannot advance it.
  Source-session changes require a new transport-session lease.
  """
  identity: RouteIdentity
  transport_session_id: str
  source_object_id: str
  observed_sequence: int
  observed_mono_time: float
  selected: bool

  def __post_init__(self):
    if not isinstance(self.identity, RouteIdentity):
      raise RouteRejected("invalid_active_identity")
    _text(self.transport_session_id, "transport_session_id")
    _text(self.source_object_id, "source_object_id", 4096)
    _integer(self.observed_sequence, "observed_sequence", 1)
    if _number(self.observed_mono_time, "observed_mono_time") < 0:
      raise RouteRejected("invalid_observed_mono_time")
    if type(self.selected) is not bool:
      raise RouteRejected("invalid_selected")


def _check_binding(binding: ActiveRouteBinding, session: str, now: float, max_age_s: float) -> None:
  if not isinstance(binding, ActiveRouteBinding):
    raise RouteRejected("missing_active_binding")
  _text(session, "transport_session_id")
  now = _number(now, "now")
  if now < 0:
    raise RouteRejected("invalid_now")
  if _number(max_age_s, "max_age_s") <= 0:
    raise RouteRejected("invalid_max_age_s")
  if binding.transport_session_id != session:
    raise RouteRejected("transport_session_mismatch")
  age = now - binding.observed_mono_time
  if age < 0:
    raise RouteRejected("binding_future")
  if age >= max_age_s:
    raise RouteRejected("binding_stale")


@dataclass(frozen=True)
class RouteSnapshot:
  identity: RouteIdentity
  transport_session_id: str
  source_object_id: str
  coordinates: tuple[tuple[float, float], ...]
  coordinate_system: str
  source_coordinate_system: str
  source_geometry_sha256: str
  geometry_sha256: str
  transform: str
  binding_id: str
  observed_sequence: int
  observed_mono_time: float
  native_length_evidence: NativeLengthEvidence | None = None


def binding_digest(transport_session_id: str, identity: RouteIdentity, source_object_id: str,
                   geometry_sha256: str, evidence: NativeLengthEvidence | None) -> str:
  """Single canonical digest shared by producer and every consumer."""
  # No evidence must stay byte-identical to the legacy v1 algorithm so old bindings
  # keep verifying; evidence gets its own v2 domain instead of extending v1 bytes.
  domain = b"phone-route-binding-v1\x00" if evidence is None else b"phone-route-binding-v2\x00"
  digest = hashlib.sha256(domain)
  for part in (transport_session_id, identity.provider, identity.source_session_id, identity.route_id,
               str(identity.revision), identity.cache_sha256, source_object_id, geometry_sha256):
    raw = part.encode("utf-8")
    digest.update(struct.pack("<I", len(raw)))
    digest.update(raw)
  if evidence is not None:
    digest.update(evidence.digest().encode("ascii"))
  return digest.hexdigest()


def bind_geometry(payload: Any, *, provenance: SourceProvenance | None, active_binding: ActiveRouteBinding | None,
                  transport_session_id: str, now: float, max_age_s: float = 3.0,
                  limits: GeometryLimits = DEFAULT_LIMITS,
                  native_length_evidence: NativeLengthEvidence | None = None) -> RouteSnapshot:
  """Convert once to an immutable WGS84 snapshot after source, selection, and session match."""
  if not isinstance(provenance, SourceProvenance):
    raise RouteRejected("missing_provenance")
  if provenance.geometry_kind == "current_native_path_window" and native_length_evidence is not None:
    raise RouteRejected("native_window_evidence_unexpected")
  _check_binding(active_binding, transport_session_id, now, max_age_s)
  if not active_binding.selected:
    raise RouteRejected("route_not_selected")
  geometry = validate_geometry(payload, limits=limits, native_length_evidence=native_length_evidence,
                               geometry_kind=provenance.geometry_kind)
  if geometry.identity != provenance.identity:
    raise RouteRejected("provenance_identity_mismatch")
  if geometry.identity != active_binding.identity:
    raise RouteRejected("active_identity_mismatch")
  if provenance.source_object_id != active_binding.source_object_id:
    raise RouteRejected("source_object_mismatch")
  if geometry.coordinate_system != provenance.coordinate_system:
    raise RouteRejected("source_coordinate_system_mismatch")
  if payload.get("geometry_kind") != provenance.geometry_kind:
    raise RouteRejected("geometry_kind_mismatch")
  if (provenance.geometry_kind == "current_native_path_window"
      and payload.get("source_geometry_sha256") != provenance.source_geometry_sha256):
    raise RouteRejected("source_geometry_hash_mismatch")
  if len(geometry.coordinates) != provenance.decoded_point_count:
    raise RouteRejected("source_point_count_mismatch")
  if geometry.geometry_sha256 != provenance.geometry_sha256:
    raise RouteRejected("geometry_hash_mismatch")
  if native_length_evidence is not None and native_length_evidence != provenance.native_length_evidence:
    raise RouteRejected("native_evidence_provenance_mismatch")
  if native_length_evidence is not None and geometry.coordinate_system != NATIVE_COORDINATE_SYSTEM:
    raise RouteRejected("native_evidence_transform_mismatch")
  converted = geometry.coordinate_system == "GCJ02"
  points = tuple(gcj02_to_wgs84(*point) for point in geometry.coordinates) if converted else geometry.coordinates
  # Evidence already re-verified these exact points; only normalization is re-run.
  points = _normalize_points(points, limits) if native_length_evidence is not None else _points(points, limits)
  digest = binding_digest(transport_session_id, geometry.identity, provenance.source_object_id,
                          geometry.geometry_sha256, native_length_evidence)
  output_system = "AMAP_NATIVE_MAP" if geometry.coordinate_system == "AMAP_NATIVE_MAP" else "WGS84"
  transform = "amap_native_identity_v1" if output_system == "AMAP_NATIVE_MAP" else "gcj02_to_wgs84_v1" if converted else "identity"
  source_geometry_sha256 = (provenance.source_geometry_sha256
                            if provenance.geometry_kind == "current_native_path_window"
                            else geometry.geometry_sha256)
  return RouteSnapshot(geometry.identity, transport_session_id, provenance.source_object_id, points, output_system,
                       geometry.coordinate_system, source_geometry_sha256, geometry_hash(points, output_system),
                       transform, digest,
                       active_binding.observed_sequence, active_binding.observed_mono_time,
                       native_length_evidence)


class PhoneRouteLease:
  """One transport session's geometry cache, with explicit clocks for offline validation.

  observe accepts independent selection observations; offer cannot renew the lease.
  snapshot is valid at read time only: downstream map frames still need freshness gates.
  """

  def __init__(self, transport_session_id: str, *, max_age_s: float = 3.0, limits: GeometryLimits = DEFAULT_LIMITS):
    self.transport_session_id = _text(transport_session_id, "transport_session_id")
    self.max_age_s = _number(max_age_s, "max_age_s")
    if self.max_age_s <= 0:
      raise RouteRejected("invalid_max_age_s")
    self.limits = limits
    self._binding: ActiveRouteBinding | None = None
    self._snapshot: RouteSnapshot | None = None
    self._source_session: tuple[str, str] | None = None
    self._last_sequence = -1
    self._last_observed = -math.inf
    self._last_now = -math.inf
    self._withdrawn_at = -math.inf
    self._closed = False
    self.last_reason = "no_active_binding"

  def _reject(self, reason: str, *, clear_binding: bool = False) -> bool:
    self.last_reason = reason
    self._snapshot = None
    if clear_binding:
      self._binding = None
    return False

  def _clock(self, now: float) -> bool:
    try:
      now = _number(now, "now")
      if now < 0:
        raise RouteRejected("invalid_now")
    except RouteRejected:
      self._closed = True
      return self._reject("clock_abnormal", clear_binding=True)
    if now < self._last_now:
      self._closed = True
      return self._reject("clock_rollback", clear_binding=True)
    self._last_now = now
    if self._closed:
      return self._reject("session_restart_required", clear_binding=True)
    return True

  def observe(self, binding: ActiveRouteBinding, *, now: float) -> bool:
    if not self._clock(now):
      return False
    try:
      _check_binding(binding, self.transport_session_id, now, self.max_age_s)
    except RouteRejected as error:
      return self._reject(error.reason, clear_binding=True)
    if binding.observed_sequence <= self._last_sequence or binding.observed_mono_time <= self._last_observed:
      self.last_reason = "observation_not_advancing"
      return False
    if binding.observed_mono_time <= self._withdrawn_at:
      return self._reject("observation_before_withdrawal", clear_binding=True)
    source_session = (binding.identity.provider, binding.identity.source_session_id)
    if self._source_session is not None and source_session != self._source_session:
      self._closed = True
      return self._reject("source_session_changed", clear_binding=True)
    self._source_session = source_session
    self._last_sequence = binding.observed_sequence
    self._last_observed = binding.observed_mono_time
    if not binding.selected:
      self._withdrawn_at = now
      self._reject("route_withdrawn", clear_binding=True)
      return True
    if (self._binding is None or binding.identity != self._binding.identity or
        binding.source_object_id != self._binding.source_object_id):
      self._snapshot = None
    self._binding = binding
    self.last_reason = "accepted_observation"
    return True

  def offer(self, payload: Any, provenance: SourceProvenance, *, now: float,
            native_length_evidence: NativeLengthEvidence | None = None) -> bool:
    if not self._clock(now):
      return False
    try:
      self._snapshot = bind_geometry(payload, provenance=provenance, active_binding=self._binding,
                                     transport_session_id=self.transport_session_id, now=now,
                                     max_age_s=self.max_age_s, limits=self.limits,
                                     native_length_evidence=native_length_evidence)
    except RouteRejected as error:
      return self._reject(error.reason)
    self.last_reason = "accepted_geometry"
    return True

  def snapshot(self, *, now: float) -> RouteSnapshot | None:
    if not self._clock(now):
      return None
    try:
      _check_binding(self._binding, self.transport_session_id, now, self.max_age_s)
    except RouteRejected as error:
      self._reject(error.reason, clear_binding=True)
      return None
    if self._snapshot is None:
      self.last_reason = "no_geometry"
      return None
    self.last_reason = "accepted"
    return replace(self._snapshot, observed_sequence=self._binding.observed_sequence,
                   observed_mono_time=self._binding.observed_mono_time)

  def withdraw(self, *, now: float) -> None:
    if self._clock(now):
      self._withdrawn_at = now
    self._reject("route_withdrawn", clear_binding=True)
