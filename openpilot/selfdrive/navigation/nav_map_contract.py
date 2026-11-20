"""Pure wire adapters and bounded map-frame correlation; no publishers or control gates.

WireRouteBinding describes a claim, not a trusted active selection. Every accepted frame
must independently match a validated RouteSnapshot, ActiveRouteBinding, and instruction
binding. Renderer instance IDs come from the receiver's current renderer lifecycle,
never by adopting the instance named by an arbitrary incoming frame.
"""
from collections import OrderedDict
from dataclasses import dataclass
import math
import zlib

from openpilot.selfdrive.navigation.phone_route import (DEFAULT_LIMITS, ActiveRouteBinding, GeometryLimits,
                                                      LongLinkLength, NativeLengthEvidence, _normalize_points,
                                                      binding_digest, validate_native_length_evidence,
                                                      RouteIdentity, RouteRejected, RouteSnapshot, _points, geometry_hash)
from openpilot.selfdrive.navigation.nav_phone_quality import (PHONE_MAP_MAX_ACCURACY_M,
  PHONE_MAP_MAX_PROJECTION_ERROR_M, PHONE_MAP_MAX_POSE_ERROR_M, PHONE_STRICT_ACCURACY_M, PHONE_STRICT_POSE_ERROR_M,
  QUALITY_DEGRADED_MAP, QUALITY_REASONS, QUALITY_STRICT)

FRAME_MAX_AGE_NS = 500_000_000
POSITION_MAX_AGE_NS = 2_000_000_000
BINDING_MAX_AGE_NS = 3_000_000_000
ROUTE_MAX_AGE_NS = 30_000_000_000
MAX_EVIDENCE_LINKS = 4096
INSTRUCTION_MAX_AGE_NS = 500_000_000


def _uint(value, name: str, bits: int = 64) -> int:
  if type(value) is not int or not 0 <= value < 2 ** bits:
    raise RouteRejected(f"invalid_{name}")
  return value


def _text(value, name: str, limit: int = 256) -> str:
  if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
    raise RouteRejected(f"invalid_{name}")
  return value


def _hash(value, name: str) -> str:
  if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
    raise RouteRejected(f"invalid_{name}")
  return value


def _finite(value, name: str) -> float:
  if type(value) not in (float, int):
    raise RouteRejected(f"invalid_{name}")
  try:
    value = float(value)
  except (ValueError, OverflowError):
    raise RouteRejected(f"invalid_{name}") from None
  if not math.isfinite(value):
    raise RouteRejected(f"invalid_{name}")
  return value


@dataclass(frozen=True)
class WireRouteBinding:
  identity: RouteIdentity
  binding_id: str
  transport_session_id: str
  source_object_id: str
  source_geometry_sha256: str
  geometry_sha256: str
  source_coordinate_system: str
  coordinate_system: str
  transform: str
  observed_sequence: int
  observed_mono_time_ns: int
  native_length_evidence: NativeLengthEvidence | None = None

  def __post_init__(self):
    if not isinstance(self.identity, RouteIdentity):
      raise RouteRejected("invalid_wire_identity")
    for name in ("binding_id", "source_geometry_sha256", "geometry_sha256"):
      _hash(getattr(self, name), name)
    _text(self.transport_session_id, "transport_session_id")
    _text(self.source_object_id, "source_object_id", 4096)
    if self.coordinate_system == self.source_coordinate_system == "AMAP_NATIVE_MAP":
      expected_transform = "amap_native_identity_v1"
    elif self.coordinate_system == "WGS84" and self.source_coordinate_system in ("WGS84", "GCJ02"):
      expected_transform = "identity" if self.source_coordinate_system == "WGS84" else "gcj02_to_wgs84_v1"
    else:
      raise RouteRejected("invalid_wire_coordinate_system")
    if self.transform != expected_transform:
      raise RouteRejected("invalid_wire_transform")
    _uint(self.observed_sequence, "observed_sequence")
    _uint(self.observed_mono_time_ns, "observed_mono_time_ns")
    if not self.observed_sequence or not self.observed_mono_time_ns:
      raise RouteRejected("missing_observation")
    if self.native_length_evidence is not None:
      if not isinstance(self.native_length_evidence, NativeLengthEvidence):
        raise RouteRejected("invalid_native_length_evidence")
      expected = binding_digest(self.transport_session_id, self.identity, self.source_object_id,
                                self.geometry_sha256, self.native_length_evidence)
      if self.binding_id != expected:
        raise RouteRejected("binding_id_evidence_mismatch")

  def route_key(self) -> tuple:
    """Only real observation sequence/time may differ across same-route heartbeats."""
    evidence = self.native_length_evidence.digest() if self.native_length_evidence is not None else None
    return (self.identity, self.binding_id, self.transport_session_id, self.source_object_id,
            self.source_geometry_sha256, self.geometry_sha256, self.source_coordinate_system,
            self.coordinate_system, self.transform, evidence)

  def to_dict(self) -> dict:
    base = {"version": 1, "bindingId": self.binding_id, "transportSessionId": self.transport_session_id,
            "sourceSessionId": self.identity.source_session_id, "provider": self.identity.provider,
            "routeId": self.identity.route_id, "revision": self.identity.revision, "cacheSha256": self.identity.cache_sha256,
            "sourceObjectId": self.source_object_id, "sourceGeometrySha256": self.source_geometry_sha256,
            "geometrySha256": self.geometry_sha256, "sourceCoordinateSystem": self.source_coordinate_system,
            "coordinateSystem": self.coordinate_system, "transform": self.transform,
            "observedSequence": self.observed_sequence, "observedMonoTime": self.observed_mono_time_ns}
    if self.native_length_evidence is not None:
      base["nativeLengthEvidence"] = {
              "version": self.native_length_evidence.version, "decoderId": self.native_length_evidence.decoder_id,
              "profile": self.native_length_evidence.profile,
              "sourceCacheSha256": self.native_length_evidence.source_cache_sha256,
              "sourceGeometrySha256": self.native_length_evidence.source_geometry_sha256,
              "pointCount": self.native_length_evidence.point_count,
              "routeDistanceCentiM": self.native_length_evidence.route_distance_centi_m,
              "longLinks": [{"globalPointStart": link.global_point_start, "pointCount": link.point_count,
                             "distanceCentiM": link.distance_centi_m}
                            for link in self.native_length_evidence.long_links]}
    return base

  @classmethod
  def from_dict(cls, data: dict):
    if not isinstance(data, dict):
      raise RouteRejected("invalid_wire_binding")
    if data.get("version", 0) == 0:
      raise RouteRejected("legacy_binding")
    if type(data.get("version")) is not int or data["version"] != 1:
      raise RouteRejected("unsupported_binding_version")
    try:
      identity = RouteIdentity(*(data[name] for name in ("provider", "sourceSessionId", "routeId", "revision", "cacheSha256")))
      return cls(identity, *(data[name] for name in ("bindingId", "transportSessionId", "sourceObjectId", "sourceGeometrySha256",
                 "geometrySha256", "sourceCoordinateSystem", "coordinateSystem", "transform", "observedSequence", "observedMonoTime")),
                 _evidence_from_dict(data.get("nativeLengthEvidence")))
    except KeyError:
      raise RouteRejected("incomplete_wire_binding") from None


def _evidence_from_dict(data) -> NativeLengthEvidence | None:
  """Bounded parse; missing means legacy (no evidence), malformed is rejected."""
  if data is None:
    return None
  if not isinstance(data, dict) or not data:
    raise RouteRejected("invalid_native_length_evidence")
  links = data.get("longLinks")
  if not isinstance(links, list) or not links or len(links) > MAX_EVIDENCE_LINKS:
    raise RouteRejected("invalid_native_length_evidence")
  parsed = []
  for link in links:
    if not isinstance(link, dict):
      raise RouteRejected("invalid_native_length_evidence")
    parsed.append(LongLinkLength(link.get("globalPointStart"), link.get("pointCount"), link.get("distanceCentiM")))
  return NativeLengthEvidence(data.get("version"), data.get("decoderId"), data.get("profile"),
                              data.get("sourceCacheSha256"), data.get("sourceGeometrySha256"),
                              data.get("pointCount"), data.get("routeDistanceCentiM"), tuple(parsed))


def binding_from_snapshot(snapshot: RouteSnapshot, active: ActiveRouteBinding, *, now_ns: int,
                          max_age_ns: int = BINDING_MAX_AGE_NS) -> WireRouteBinding:
  """Serialize an already validated snapshot only against independent active selection."""
  _uint(now_ns, "now_ns")
  if not isinstance(snapshot, RouteSnapshot) or not isinstance(active, ActiveRouteBinding):
    raise RouteRejected("missing_trusted_context")
  if not active.selected:
    raise RouteRejected("route_not_selected")
  if (snapshot.identity != active.identity or snapshot.transport_session_id != active.transport_session_id or
      snapshot.source_object_id != active.source_object_id):
    raise RouteRejected("active_snapshot_mismatch")
  observed_ns = round(active.observed_mono_time * 1e9)
  if not 0 <= now_ns - observed_ns < max_age_ns:
    raise RouteRejected("active_binding_stale_or_future")
  if active.observed_sequence < snapshot.observed_sequence or active.observed_mono_time < snapshot.observed_mono_time:
    raise RouteRejected("observation_before_snapshot")
  return WireRouteBinding(snapshot.identity, snapshot.binding_id, snapshot.transport_session_id, snapshot.source_object_id,
                          snapshot.source_geometry_sha256, snapshot.geometry_sha256, snapshot.source_coordinate_system,
                          snapshot.coordinate_system, snapshot.transform, active.observed_sequence, observed_ns,
                          snapshot.native_length_evidence)


def route_fields(snapshot: RouteSnapshot, active: ActiveRouteBinding, *, now_ns: int) -> dict:
  binding = binding_from_snapshot(snapshot, active, now_ns=now_ns)
  return {"coordinates64": [{"longitude": lon, "latitude": lat} for lon, lat in snapshot.coordinates],
          "routeBinding": binding.to_dict()}


def _message_times(produced: int, received: int, now_ns: int, max_age_ns: int, service: str) -> None:
  _uint(now_ns, "now_ns")
  for value, name in ((produced, "produced"), (received, "received")):
    _uint(value, name)
    if not value or not 0 <= now_ns - value < max_age_ns:
      raise RouteRejected(f"{service}_{name}_stale_or_future")


def _submaster_times(sm, service: str, now_ns: int, max_age_ns: int) -> tuple[int, int]:
  if not sm.valid.get(service, False):
    raise RouteRejected(f"{service}_invalid")
  produced = sm.logMonoTime.get(service, 0)
  received = round(_finite(sm.recv_time.get(service, 0.0), "received_mono_time") * 1e9)
  _message_times(produced, received, now_ns, max_age_ns, service)
  return produced, received


@dataclass(frozen=True)
class LocalNavdRouteContext:
  """Geometry anchor from the sole local navd publisher, not a phone wire assertion.

  Build only when the actual SubMaster reports a new navRoute. navd owns source
  verification; downstream processes verify local wire consistency and freshness.
  A newer instruction observation may renew route liveness without retransmitting
  the full geometry, but cannot change this anchor's stable route identity.
  """
  binding: WireRouteBinding
  coordinates: tuple[tuple[float, float], ...]
  produced_mono_time_ns: int
  received_mono_time_ns: int

  @classmethod
  def from_submaster(cls, sm, *, now_ns: int, limits: GeometryLimits = DEFAULT_LIMITS):
    if not sm.updated.get("navRoute", False):
      raise RouteRejected("navRoute_not_updated")
    produced, received = _submaster_times(sm, "navRoute", now_ns, ROUTE_MAX_AGE_NS)
    route = sm["navRoute"]
    binding = WireRouteBinding.from_dict(route.routeBinding.to_dict())
    if len(route.coordinates64) > limits.max_points:
      raise RouteRejected("point_limit")
    raw_points = [(point.longitude, point.latitude) for point in route.coordinates64]
    if binding.native_length_evidence is None:
      coordinates = _points(raw_points, limits)
    else:
      # Recompute every length from the received coordinates; a profile string or
      # any "verified" flag is never sufficient on its own.
      validate_native_length_evidence(raw_points, binding.native_length_evidence,
        source_geometry_sha256=binding.source_geometry_sha256,
        source_cache_sha256=binding.identity.cache_sha256, decoder_id=binding.native_length_evidence.decoder_id,
        provider=binding.identity.provider, coordinate_system=binding.source_coordinate_system,
        transform=binding.transform, limits=limits)
      coordinates = _normalize_points(raw_points, limits)
    if geometry_hash(coordinates, binding.coordinate_system) != binding.geometry_sha256:
      raise RouteRejected("local_geometry_hash_mismatch")
    return cls(binding, coordinates, produced, received)

  def check_fresh(self, now_ns: int) -> None:
    _message_times(self.produced_mono_time_ns, self.received_mono_time_ns, now_ns, ROUTE_MAX_AGE_NS, "navRoute")


@dataclass(frozen=True)
class LocalNavdInstructionContext:
  """Fresh instruction binding from navd; missing binding never disables the 47-vector."""
  binding: WireRouteBinding
  produced_mono_time_ns: int
  received_mono_time_ns: int

  @classmethod
  def from_submaster(cls, sm, *, now_ns: int):
    produced, received = _submaster_times(sm, "navInstruction", now_ns, INSTRUCTION_MAX_AGE_NS)
    result = cls(WireRouteBinding.from_dict(sm["navInstruction"].routeBinding.to_dict()), produced, received)
    result.check_fresh(now_ns)
    return result

  def check_fresh(self, now_ns: int) -> None:
    _message_times(self.produced_mono_time_ns, self.received_mono_time_ns, now_ns, INSTRUCTION_MAX_AGE_NS, "navInstruction")
    if not 0 <= now_ns - self.binding.observed_mono_time_ns < BINDING_MAX_AGE_NS:
      raise RouteRejected("instruction_observation_stale_or_future")
    if self.binding.observed_mono_time_ns > self.produced_mono_time_ns:
      raise RouteRejected("instruction_observation_after_message")


@dataclass(frozen=True)
class LocalNavdPhonePoseContext(LocalNavdInstructionContext):
  """Independent phone route/pose lease; never asserts the legacy instruction's identity."""
  position_mono_time_ns: int
  gps_source_token: str
  longitude: float
  latitude: float
  horizontal_accuracy_m: float
  global_segment: int
  matched_longitude: float
  matched_latitude: float
  native_pose_error_m: float
  quality_reason: str

  @classmethod
  def from_submaster(cls, sm, *, now_ns: int):
    produced, received = _submaster_times(sm, "navRoute", now_ns, POSITION_MAX_AGE_NS)
    route = sm["navRoute"]
    binding = WireRouteBinding.from_dict(route.routeBinding.to_dict())
    data = route.phonePose.to_dict()
    if (type(data.get("version")) is not int or data["version"] != 1
        or binding.coordinate_system != "AMAP_NATIVE_MAP"
        or data.get("coordinateSystem") != binding.coordinate_system
        or data.get("positionTransform") != "wgs84_to_gcj02_approx_v1_crosschecked"):
      raise RouteRejected("phone_pose_frame_unverified")
    try:
      values = [data[name] for name in ("positionMonoTime", "gpsSourceToken", "longitude", "latitude", "horizontalAccuracyM",
                                      "globalSegment", "matchedLongitude", "matchedLatitude", "nativePoseErrorM", "reason")]
    except KeyError:
      raise RouteRejected("phone_pose_incomplete") from None
    result = cls(binding, produced, received, *values)
    _hash(result.gps_source_token, "gps_source_token")
    _uint(result.position_mono_time_ns, "position_mono_time")
    _uint(result.global_segment, "global_segment", 32)
    for lon, lat in ((result.longitude, result.latitude), (result.matched_longitude, result.matched_latitude)):
      if not -180 <= _finite(lon, "longitude") <= 180 or not -90 <= _finite(lat, "latitude") <= 90:
        raise RouteRejected("phone_pose_coordinate_invalid")
    if not 0 < _finite(result.horizontal_accuracy_m, "accuracy") <= PHONE_MAP_MAX_ACCURACY_M:
      raise RouteRejected("phone_pose_accuracy_unusable")
    if not 0 <= _finite(result.native_pose_error_m, "native_pose_error") <= PHONE_MAP_MAX_POSE_ERROR_M:
      raise RouteRejected("phone_pose_space_mismatch")
    if result.quality_reason not in QUALITY_REASONS:
      raise RouteRejected("phone_pose_quality_unverified")
    expected_quality = (QUALITY_STRICT
                        if result.horizontal_accuracy_m <= PHONE_STRICT_ACCURACY_M
                        and result.native_pose_error_m <= PHONE_STRICT_POSE_ERROR_M
                        else QUALITY_DEGRADED_MAP)
    if result.quality_reason != expected_quality:
      raise RouteRejected("phone_pose_quality_mismatch")
    result.check_fresh(now_ns)
    return result

  def check_fresh(self, now_ns: int) -> None:
    _message_times(self.produced_mono_time_ns, self.received_mono_time_ns, now_ns, POSITION_MAX_AGE_NS, "phone_pose")
    if not 0 < self.position_mono_time_ns <= self.produced_mono_time_ns:
      raise RouteRejected("phone_position_future_or_missing")
    if now_ns - self.position_mono_time_ns >= POSITION_MAX_AGE_NS:
      raise RouteRejected("phone_position_stale")
    if not 0 <= now_ns - self.binding.observed_mono_time_ns < BINDING_MAX_AGE_NS:
      raise RouteRejected("phone_observation_stale_or_future")
    if self.binding.observed_mono_time_ns > self.produced_mono_time_ns:
      raise RouteRejected("phone_observation_after_message")

  def map_pose(self, coordinates):
    """Project the actual transformed GPS on the source-selected edge, never nearest globally."""
    import numpy as np
    from openpilot.selfdrive.navigation.route import RouteTracker, local_meters

    route = np.asarray(coordinates, dtype=np.float64)
    if not 0 <= self.global_segment < len(route) - 1:
      raise RouteRejected("phone_anchor_out_of_range")
    edge = route[self.global_segment:self.global_segment + 2]
    delta = local_meters(edge[1], edge[0])
    squared = float(delta @ delta)
    if not math.isfinite(squared) or squared <= 1e-8:
      raise RouteRejected("phone_anchor_degenerate")
    def project(point):
      local = local_meters(point, edge[0])
      fraction = float(np.clip(local @ delta / squared, 0.0, 1.0))
      return fraction, float(np.linalg.norm(local - fraction * delta))
    _, matched_lateral = project((self.matched_longitude, self.matched_latitude))
    fraction, lateral = project((self.longitude, self.latitude))
    if matched_lateral > 1.0:
      raise RouteRejected("phone_source_anchor_mismatch")
    if lateral > PHONE_MAP_MAX_PROJECTION_ERROR_M:
      raise RouteRejected("phone_off_route")
    tracker = RouteTracker(route)
    start, end = tracker.distance[self.global_segment:self.global_segment + 2]
    progress = float(start + fraction * (end - start))
    return tracker.pose(progress), ProjectionEvidence(lateral, 0.0, self.global_segment, progress, False)


def local_phone_observation(sm, route_context: LocalNavdRouteContext | None, *, now_ns: int) -> LocalNavdInstructionContext:
  if route_context is not None and route_context.binding.coordinate_system == "AMAP_NATIVE_MAP":
    return LocalNavdPhonePoseContext.from_submaster(sm, now_ns=now_ns)
  return LocalNavdInstructionContext.from_submaster(sm, now_ns=now_ns)


def current_local_binding(route_context: LocalNavdRouteContext | None,
                          instruction_context: LocalNavdInstructionContext | None, *, now_ns: int) -> WireRouteBinding:
  if not isinstance(route_context, LocalNavdRouteContext):
    raise RouteRejected("local_route_missing")
  route_context.check_fresh(now_ns)
  if not isinstance(instruction_context, LocalNavdInstructionContext):
    raise RouteRejected("local_instruction_missing")
  instruction_context.check_fresh(now_ns)
  if route_context.binding.route_key() != instruction_context.binding.route_key():
    raise RouteRejected("instruction_route_mismatch")
  if (instruction_context.binding.observed_sequence < route_context.binding.observed_sequence or
      instruction_context.binding.observed_mono_time_ns < route_context.binding.observed_mono_time_ns):
    raise RouteRejected("instruction_observation_before_route")
  return instruction_context.binding


@dataclass(frozen=True)
class ProjectionEvidence:
  raw_gps_lateral_m: float
  raw_gps_heading_error_deg: float
  segment: int
  progress_m: float
  heading_valid: bool = False

  def __post_init__(self):
    if _finite(self.raw_gps_lateral_m, "raw_gps_lateral_m") < 0 or _finite(self.progress_m, "progress_m") < 0:
      raise RouteRejected("invalid_projection_distance")
    if abs(_finite(self.raw_gps_heading_error_deg, "raw_gps_heading_error_deg")) > 180:
      raise RouteRejected("invalid_heading_error")
    _uint(self.segment, "projection_segment", 32)
    if type(self.heading_valid) is not bool:
      raise RouteRejected("invalid_heading_valid")


@dataclass(frozen=True)
class MapPixels:
  frame_id: int
  timestamp_sof_ns: int
  timestamp_eof_ns: int
  width: int
  height: int
  pixels: bytes

  def __post_init__(self):
    _uint(self.frame_id, "frame_id", 32)
    _uint(self.timestamp_sof_ns, "timestamp_sof_ns")
    _uint(self.timestamp_eof_ns, "timestamp_eof_ns")
    _uint(self.width, "width", 16)
    _uint(self.height, "height", 16)
    if not self.width or not self.height or not isinstance(self.pixels, bytes) or len(self.pixels) != self.width * self.height:
      raise RouteRejected("invalid_pixel_buffer")


@dataclass(frozen=True)
class MapFrameMetadata:
  renderer_instance_id: str
  frame_id: int
  timestamp_sof_ns: int
  timestamp_eof_ns: int
  route_binding: WireRouteBinding
  location_mono_time_ns: int
  width: int
  height: int
  pixel_format: str
  image_crc32: int
  valid: bool
  reason: str
  projection: ProjectionEvidence | None

  def __post_init__(self):
    _text(self.renderer_instance_id, "renderer_instance_id")
    _uint(self.frame_id, "frame_id", 32)
    for name in ("timestamp_sof_ns", "timestamp_eof_ns", "location_mono_time_ns"):
      _uint(getattr(self, name), name)
    _uint(self.width, "width", 16)
    _uint(self.height, "height", 16)
    _uint(self.image_crc32, "image_crc32", 32)
    if type(self.valid) is not bool or not isinstance(self.route_binding, WireRouteBinding):
      raise RouteRejected("invalid_frame_metadata")
    _text(self.reason, "frame_reason")
    _text(self.pixel_format, "pixel_format")
    if self.projection is not None and not isinstance(self.projection, ProjectionEvidence):
      raise RouteRejected("invalid_projection_evidence")

  def to_dict(self) -> dict:
    projection = self.projection
    return {"version": 1, "rendererInstanceId": self.renderer_instance_id, "frameId": self.frame_id,
            "timestampSof": self.timestamp_sof_ns, "timestampEof": self.timestamp_eof_ns,
            "routeBinding": self.route_binding.to_dict(), "locationMonoTime": self.location_mono_time_ns,
            "width": self.width, "height": self.height, "pixelFormat": self.pixel_format, "imageCrc32": self.image_crc32,
            "valid": self.valid, "reason": self.reason, "hasProjectionEvidence": projection is not None,
            "rawGpsLateralM": projection.raw_gps_lateral_m if projection else 0.0,
            "rawGpsHeadingErrorDeg": projection.raw_gps_heading_error_deg if projection else 0.0,
            "projectionSegment": projection.segment if projection else 0, "progressM": projection.progress_m if projection else 0.0,
            "rawGpsHeadingValid": projection.heading_valid if projection else False}

  @classmethod
  def from_dict(cls, data: dict):
    if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1:
      raise RouteRejected("legacy_or_unsupported_map_metadata")
    try:
      if type(data["hasProjectionEvidence"]) is not bool:
        raise RouteRejected("invalid_projection_flag")
      projection = (ProjectionEvidence(data["rawGpsLateralM"], data["rawGpsHeadingErrorDeg"], data["projectionSegment"], data["progressM"],
                                       data.get("rawGpsHeadingValid", False))
                    if data["hasProjectionEvidence"] else None)
      return cls(data["rendererInstanceId"], data["frameId"], data["timestampSof"], data["timestampEof"],
                 WireRouteBinding.from_dict(data["routeBinding"]), data["locationMonoTime"], data["width"], data["height"],
                 data["pixelFormat"], data["imageCrc32"], data["valid"], data["reason"], projection)
    except KeyError:
      raise RouteRejected("incomplete_map_metadata") from None


def make_map_frame(snapshot: RouteSnapshot, active: ActiveRouteBinding, frame: MapPixels, *, renderer_instance_id: str,
                   location_mono_time_ns: int, projection: ProjectionEvidence | None) -> MapFrameMetadata:
  binding = binding_from_snapshot(snapshot, active, now_ns=frame.timestamp_eof_ns)
  return MapFrameMetadata(renderer_instance_id, frame.frame_id, frame.timestamp_sof_ns, frame.timestamp_eof_ns, binding,
                          location_mono_time_ns, frame.width, frame.height, "gray8", zlib.crc32(frame.pixels), True, "accepted", projection)


def make_local_map_frame(route_context: LocalNavdRouteContext, instruction_context: LocalNavdInstructionContext,
                         frame: MapPixels, *, renderer_instance_id: str, location_mono_time_ns: int,
                         projection: ProjectionEvidence | None) -> MapFrameMetadata:
  """mapsd-side adapter after navd has already verified and published the current route."""
  binding = current_local_binding(route_context, instruction_context, now_ns=frame.timestamp_eof_ns)
  return MapFrameMetadata(renderer_instance_id, frame.frame_id, frame.timestamp_sof_ns, frame.timestamp_eof_ns, binding,
                          location_mono_time_ns, frame.width, frame.height, "gray8", zlib.crc32(frame.pixels), True, "accepted", projection)


@dataclass(frozen=True)
class FrameMatch:
  accepted: bool
  reason: str
  metadata: MapFrameMetadata | None = None


class MapFrameMatcher:
  """Bounded sidecar cache, keyed by renderer instance, frame ID, and exact render time.

  A missing sidecar may arrive later; the caller may retry the same immutable MapPixels
  within its freshness window. No arbitrary latest sidecar or image is substituted.
  """

  def __init__(self, renderer_instance_id: str, *, max_entries: int = 16):
    self.renderer_instance_id = _text(renderer_instance_id, "renderer_instance_id")
    if type(max_entries) is not int or not 1 <= max_entries <= 256:
      raise RouteRejected("invalid_max_entries")
    self.max_entries = max_entries
    self._metadata: OrderedDict[tuple, tuple[MapFrameMetadata | None, int]] = OrderedDict()
    self._link_epoch: str | None = None
    self._connected_mono_time_ns = 0
    self.requires_reconnect = False

  @classmethod
  def for_link(cls, link_epoch: str, *, connected_mono_time_ns: int, max_entries: int = 16):
    """Create after a successful VisionIPC connect, with a receiver-created link epoch.

    No sidecar instance is trusted at creation. It can bind this link only after exact
    pixels/timestamps and the independent local route/instruction anchors all match.
    A different fully matching instance requires clearing/reconnecting the IPC client.
    """
    _text(link_epoch, "link_epoch")
    _uint(connected_mono_time_ns, "connected_mono_time_ns")
    result = cls("pending-link", max_entries=max_entries)
    result.renderer_instance_id = None
    result._link_epoch = link_epoch
    result._connected_mono_time_ns = connected_mono_time_ns
    return result

  @property
  def pending_count(self) -> int:
    return len(self._metadata)

  def switch_renderer(self, renderer_instance_id: str) -> None:
    if self._link_epoch is not None:
      raise RouteRejected("new_link_context_required")
    renderer_instance_id = _text(renderer_instance_id, "renderer_instance_id")
    if renderer_instance_id != self.renderer_instance_id:
      self._metadata.clear()
      self.renderer_instance_id = renderer_instance_id

  def add_metadata(self, metadata: MapFrameMetadata, *, received_mono_time_ns: int) -> bool:
    _uint(received_mono_time_ns, "received_mono_time_ns")
    if not isinstance(metadata, MapFrameMetadata):
      return False
    if self._link_epoch is None and metadata.renderer_instance_id != self.renderer_instance_id:
      return False
    key = (metadata.renderer_instance_id, metadata.frame_id, metadata.timestamp_eof_ns)
    previous = self._metadata.get(key)
    if previous is not None:
      if previous[0] != metadata:
        self._metadata[key] = (None, previous[1])
        return False
      return True  # Repeated delivery never refreshes the original receipt time.
    self._metadata[key] = (metadata, received_mono_time_ns)
    while len(self._metadata) > self.max_entries:
      self._metadata.popitem(last=False)
    return True

  def _frame_metadata(self, frame: MapPixels, now_ns: int) -> MapFrameMetadata:
    _uint(now_ns, "now_ns")
    if not isinstance(frame, MapPixels):
      raise RouteRejected("invalid_frame")
    if now_ns < frame.timestamp_eof_ns:
      raise RouteRejected("future_frame")
    if now_ns - frame.timestamp_eof_ns >= FRAME_MAX_AGE_NS:
      raise RouteRejected("stale_frame")
    if self._link_epoch is not None:
      if self.requires_reconnect:
        raise RouteRejected("renderer_reconnect_required")
      if frame.timestamp_eof_ns < self._connected_mono_time_ns:
        raise RouteRejected("frame_before_connection")
      candidates = [item for key, item in self._metadata.items() if key[1:] == (frame.frame_id, frame.timestamp_eof_ns)]
      if len(candidates) > 1:
        raise RouteRejected("renderer_frame_ambiguous")
      found = candidates[0] if candidates else None
    else:
      found = self._metadata.get((self.renderer_instance_id, frame.frame_id, frame.timestamp_eof_ns))
    if found is None:
      raise RouteRejected("metadata_missing")
    metadata, receipt = found
    if metadata is None:
      raise RouteRejected("metadata_conflict")
    if not 0 <= now_ns - receipt < FRAME_MAX_AGE_NS:
      raise RouteRejected("metadata_stale_or_future")
    if frame.timestamp_sof_ns != metadata.timestamp_sof_ns or not 0 < frame.timestamp_sof_ns <= frame.timestamp_eof_ns:
      raise RouteRejected("timestamp_mismatch")
    if (frame.width, frame.height) != (256, 256) or (frame.width, frame.height) != (metadata.width, metadata.height):
      raise RouteRejected("pixel_shape_mismatch")
    if metadata.pixel_format != "gray8":
      raise RouteRejected("pixel_format_mismatch")
    if not metadata.valid:
      raise RouteRejected(f"map_invalid:{metadata.reason}")
    if not 0 < metadata.location_mono_time_ns <= frame.timestamp_eof_ns:
      raise RouteRejected("position_future_or_missing")
    if now_ns - metadata.location_mono_time_ns >= POSITION_MAX_AGE_NS:
      raise RouteRejected("stale_position")
    if metadata.projection is None:
      raise RouteRejected("projection_evidence_missing")
    if zlib.crc32(frame.pixels) != metadata.image_crc32:
      raise RouteRejected("pixel_crc_mismatch")
    if not any(frame.pixels):
      raise RouteRejected("empty_map")
    return metadata

  @staticmethod
  def _compare_bindings(metadata: MapFrameMetadata, expected: WireRouteBinding,
                        instruction_binding: WireRouteBinding | None, now_ns: int) -> None:
    if expected.route_key() != metadata.route_binding.route_key():
      raise RouteRejected("map_route_mismatch")
    if not isinstance(instruction_binding, WireRouteBinding):
      raise RouteRejected("instruction_binding_missing")
    if expected.route_key() != instruction_binding.route_key():
      raise RouteRejected("instruction_route_mismatch")
    for bound in (metadata.route_binding, instruction_binding):
      if (bound.observed_sequence > expected.observed_sequence or bound.observed_mono_time_ns > expected.observed_mono_time_ns):
        raise RouteRejected("observation_ahead_of_active")
      if not 0 <= now_ns - bound.observed_mono_time_ns < BINDING_MAX_AGE_NS:
        raise RouteRejected("wire_observation_stale_or_future")
    if metadata.route_binding.observed_mono_time_ns > metadata.timestamp_eof_ns:
      raise RouteRejected("observation_after_render")

  def _accepted(self, metadata: MapFrameMetadata) -> FrameMatch:
    if self._link_epoch is not None:
      if self.renderer_instance_id is None:
        self.renderer_instance_id = metadata.renderer_instance_id
      elif self.renderer_instance_id != metadata.renderer_instance_id:
        self.requires_reconnect = True
        return FrameMatch(False, "renderer_instance_changed")
    return FrameMatch(True, "accepted", metadata)

  def match(self, frame: MapPixels, *, snapshot: RouteSnapshot, active_binding: ActiveRouteBinding,
            instruction_binding: WireRouteBinding | None, now_ns: int) -> FrameMatch:
    try:
      metadata = self._frame_metadata(frame, now_ns)
      expected = binding_from_snapshot(snapshot, active_binding, now_ns=now_ns)
      self._compare_bindings(metadata, expected, instruction_binding, now_ns)
      return self._accepted(metadata)
    except RouteRejected as error:
      return FrameMatch(False, error.reason)

  def match_local(self, frame: MapPixels, *, route_context: LocalNavdRouteContext | None,
                  instruction_context: LocalNavdInstructionContext | None, now_ns: int) -> FrameMatch:
    """Downstream path: navd already verified raw provenance; never recreate that proof."""
    try:
      metadata = self._frame_metadata(frame, now_ns)
      expected = current_local_binding(route_context, instruction_context, now_ns=now_ns)
      self._compare_bindings(metadata, expected, instruction_context.binding, now_ns)
      return self._accepted(metadata)
    except RouteRejected as error:
      return FrameMatch(False, error.reason)
