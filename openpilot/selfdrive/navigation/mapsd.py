#!/usr/bin/env python3
"""Renders the model's map input from the active route and publishes it on VisionIPC.

Contract preserved from comma's nav-model-integration `mapsd`: 256x256 uint8 grayscale
on `VISION_STREAM_MAP`, ego-three-quarters-down framing, route grey, ego arrow white,
and a zero timestamp whenever no usable map exists so the consumer can reject it.

Difference from upstream: this build has no access to comma's authenticated PMTiles
basemap, so only the route is drawn. The framing and the route are real; the surrounding
road graph is missing until a tile source is wired in (see map_draw docs).
"""
import math
import os
import time
import uuid
import zlib
from pathlib import Path

import numpy as np

import openpilot.cereal.messaging as messaging
from msgq.visionipc import VisionIpcServer
from openpilot.cereal.visionipc import VisionStreamType
from openpilot.common.realtime import Ratekeeper
from openpilot.common.swaglog import cloudlog
from openpilot.selfdrive.navigation.cplink import cplink_navi_active
from openpilot.selfdrive.navigation.helpers import latest_gps_source
from openpilot.selfdrive.navigation.local_tiles import HttpTiles, LocalTiles
from openpilot.selfdrive.navigation.map_draw import SIZE, render_route
from openpilot.selfdrive.navigation.nav_map_contract import (LocalNavdInstructionContext, LocalNavdPhonePoseContext, LocalNavdRouteContext, MapPixels,
                                                           ProjectionEvidence, RouteRejected, current_local_binding,
                                                           local_phone_observation, make_local_map_frame)
from openpilot.selfdrive.navigation.pmtiles import PMTiles
from openpilot.selfdrive.navigation.renderer import MapRenderer
from openpilot.selfdrive.navigation.route import RouteTracker, along_path

EMA_ALPHA = 0.2  # smooth the route pose; GPS jitter would otherwise shake the map frame
TILES_ENV = "SUNNYPILOT_NAV_TILES"
TILES_PARAM = "NavTiles"
# Protomaps publishes a daily planet build; only the ranges mapsd needs are fetched.
DEFAULT_TILES_URL = "https://build.protomaps.com/20261106.pmtiles"
DEFAULT_TILES = Path.home() / "navigation" / "basemap.pmtiles"
TILES_RETRY_SECONDS = 30.0
ROUTE_TIMEOUT = 30.0  # s, drop the route when navd stops publishing (3x its resend period)
ON_ROUTE_LATERAL_MAX = 30.0
TILES_INVALID_LIMIT = 3  # consecutive partial-tile frames before skipping the renderer
TILES_PARTIAL_BACKOFF = 600.0  # s, a partial archive rarely becomes complete at runtime


def param_tiles() -> str | None:
  """`NavTiles` from Params, or None. Kept separate so the precedence is testable."""
  try:
    from openpilot.common.params import Params
    value = Params().get(TILES_PARAM)
    if isinstance(value, bytes):
      value = value.decode()
    return (value or "").strip() or None
  except Exception:
    return None


def tiles_spec() -> str:
  """Archive to render: env var, then `NavTiles`, then a local file, then the URL.

  Precedence matters: an explicit env var is how an operator overrides everything, the
  Param is what the web page writes, a downloaded local archive works offline, and the
  URL is the zero-setup default."""
  env = os.getenv(TILES_ENV, "").strip()
  if env:
    return env
  from_param = (param_tiles() or "").strip()
  if from_param:
    return from_param
  if DEFAULT_TILES.is_file():
    return str(DEFAULT_TILES)
  return DEFAULT_TILES_URL


def make_renderer() -> MapRenderer | None:
  """A basemap renderer when a PMTiles archive is configured, else None (route-only frames).

  `SUNNYPILOT_NAV_TILES` may be a local file or an https:// URL (Protomaps daily build).
  A URL never blocks startup: only the header and directory are read here."""
  spec = tiles_spec()
  if spec.startswith(("http://", "https://")):
    return MapRenderer(PMTiles(HttpTiles(spec)))
  path = Path(spec)
  if not path.is_file():
    return None
  return MapRenderer(PMTiles(LocalTiles(path)))


class MapsD:
  def __init__(self):
    self.route = np.empty((0, 2))
    self.tracker: RouteTracker | None = None
    self.progress = 0.0
    self.pose: tuple[float, float, float] | None = None
    self.renderer: MapRenderer | None = None
    self.tiles_failed_at = 0.0
    self.tiles_invalid_until = 0.0
    self.tiles_invalid = 0
    self.prefetched_route = None
    self.route_at = 0.0
    self._last_invalid_log = 0.0
    self._cplink_active = False
    self._route_after = 0.0
    self._last_update_at = 0.0
    self._phone_route_context: LocalNavdRouteContext | None = None
    self._phone_instruction_context: LocalNavdInstructionContext | None = None
    self._projection_evidence: ProjectionEvidence | None = None
    self._last_invalid_reason = "no_route"

  def _clear_route(self) -> None:
    self.route = np.empty((0, 2))
    self.tracker = None
    self.progress = 0.0
    self.pose = None
    self.prefetched_route = None
    self.route_at = 0.0
    self._phone_route_context = None
    self._phone_instruction_context = None
    self._projection_evidence = None

  def _invalid(self, reason: str, now: float) -> None:
    self._last_invalid_reason = reason
    if now - self._last_invalid_log >= 2.0:
      self._last_invalid_log = now
      cloudlog.warning(f"mapsd.invalid: {reason} route={len(self.route)} tracker={self.tracker is not None}")

  def _update_phone_route(self, sm, now: float) -> bool:
    now_ns = round(now * 1e9)
    if sm.updated.get("navRoute", False):
      previous = self._phone_route_context
      # A bad replacement must not leave the previous valid map available.
      self._phone_route_context = None
      try:
        candidate = LocalNavdRouteContext.from_submaster(sm, now_ns=now_ns)
        if min(candidate.produced_mono_time_ns, candidate.received_mono_time_ns) < round(self._route_after * 1e9):
          raise RouteRejected("route_before_source_switch")
      except (RouteRejected, AttributeError, KeyError, TypeError, ValueError) as error:
        self._clear_route()
        self._invalid(error.reason if isinstance(error, RouteRejected) else "malformed_phone_route", now)
        return False
      if previous is None or candidate.binding.route_key() != previous.binding.route_key():
        self._clear_route()
        self.route = np.array(candidate.coordinates, dtype=np.float64)
        self.tracker = RouteTracker(self.route)
      self._phone_route_context = candidate
      self.route_at = now
    try:
      instruction = local_phone_observation(sm, self._phone_route_context, now_ns=now_ns)
      if min(instruction.produced_mono_time_ns, instruction.received_mono_time_ns) < round(self._route_after * 1e9):
        raise RouteRejected("instruction_before_source_switch")
      current_local_binding(self._phone_route_context, instruction, now_ns=now_ns)
      self._phone_instruction_context = instruction
    except (RouteRejected, AttributeError, KeyError, TypeError, ValueError) as error:
      self._phone_instruction_context = None
      self.pose = None
      self._invalid(error.reason if isinstance(error, RouteRejected) else "malformed_phone_instruction", now)
      return False
    return True

  def update(self, sm) -> tuple[np.ndarray, int]:
    """(image, timestamp): a valid frame for the latest route, else black with timestamp 0."""
    now = time.monotonic()
    phone_active = cplink_navi_active()
    self._projection_evidence = None
    self._phone_instruction_context = None
    if phone_active != self._cplink_active:
      self._clear_route()
      # A new source can publish between our polls; reject everything from before
      # the last poll of the previous source, without waiting for a 10 s resend.
      self._route_after = self._last_update_at
    self._last_update_at = now
    self._cplink_active = phone_active
    if phone_active:
      if not self._update_phone_route(sm, now):
        return np.zeros((SIZE, SIZE), dtype=np.uint8), 0
    route_received = sm.recv_time.get("navRoute", 0.0)
    route_produced = getattr(sm, "logMonoTime", {}).get("navRoute", int(route_received * 1e9)) / 1e9
    if not phone_active and sm.updated["navRoute"] and min(route_received, route_produced) >= self._route_after:
      if not sm.valid.get("navRoute", False):
        self.route = np.empty((0, 2))
        self.tracker = None
        self.pose = None
        return np.zeros((SIZE, SIZE), dtype=np.uint8), 0
      route = np.array([(c.longitude, c.latitude) for c in sm["navRoute"].coordinates], dtype=np.float64).reshape((-1, 2))
      # Refresh on every resend: identical route messages are the liveness signal that
      # keeps ROUTE_TIMEOUT from expiring a route navd is still publishing.
      self.route_at = now
      if not np.array_equal(route, self.route):
        self.route = route
        self.tracker = RouteTracker(route) if len(route) >= 2 else None
        self.progress = 0.0
        self.pose = None
        self.prefetched_route = None
        self.route_at = now

    if self.tracker is not None and now - self.route_at > ROUTE_TIMEOUT:
      # navd stopped publishing (crashed/stopped): never keep guiding on a stale route.
      self.route = np.empty((0, 2))
      self.tracker = None
      self.pose = None

    if self.tracker is None:
      self._invalid("no_route", now)
      return np.zeros((SIZE, SIZE), dtype=np.uint8), 0
    if phone_active and isinstance(self._phone_instruction_context, LocalNavdPhonePoseContext):
      # Native-map geometry must never use WGS GPS, PMTiles or a global nearest search.
      try:
        observation = self._phone_instruction_context
        pose, projection = observation.map_pose(self.route)
        image, valid = render_route(self.route, *pose)
        current_local_binding(self._phone_route_context, observation, now_ns=time.monotonic_ns())
        if not valid or not cplink_navi_active():
          raise RouteRejected("phone_native_render_invalid")
        self.pose, self.progress = pose, projection.progress_m
        self._projection_evidence = projection
        self._last_invalid_reason = "accepted"
        return image, observation.position_mono_time_ns
      except (RouteRejected, ValueError, TypeError) as error:
        self.pose = None
        self._projection_evidence = None
        self._invalid(error.reason if isinstance(error, RouteRejected) else "phone_native_render_failed", now)
        return np.zeros((SIZE, SIZE), dtype=np.uint8), 0
    source = latest_gps_source(sm)
    if source is None:
      # No fix means no idea where we are on the route: never hand the model a map that
      # looks valid. Upstream gates the same way.
      self._invalid("no_gps", now)
      return np.zeros((SIZE, SIZE), dtype=np.uint8), 0
    if source is not None:
      gps = sm[source]
      if (not math.isfinite(gps.longitude) or not math.isfinite(gps.latitude)
          or not -180 <= gps.longitude <= 180 or not -90 <= gps.latitude <= 90):
        self.pose = None
        self._invalid("invalid_gps", now)
        return np.zeros((SIZE, SIZE), dtype=np.uint8), 0
      _, lateral = along_path(self.route, (gps.longitude, gps.latitude))
      if not math.isfinite(lateral) or lateral > ON_ROUTE_LATERAL_MAX:
        self.pose = None
        self._invalid("off_route", now)
        return np.zeros((SIZE, SIZE), dtype=np.uint8), 0
      if phone_active and not math.isfinite(gps.speed):
        self.pose = None
        self._invalid("invalid_gps_speed", now)
        return np.zeros((SIZE, SIZE), dtype=np.uint8), 0
      # Map frames are rendered on the route, not at the raw fix: project, then move ahead
      # by the distance driven since the fix (same rule the training data used).
      ahead = max(gps.speed, 0.0) * (now - sm.recv_time[source])
      self.progress = self.tracker.project(gps.longitude, gps.latitude)
      pose = self.tracker.pose(self.progress + ahead)
      if phone_active:
        segment = max(0, min(len(self.route) - 2, int(np.searchsorted(self.tracker.distance, self.progress, side="right") - 1)))
        bearing = getattr(gps, "bearingDeg", None)
        heading_valid = isinstance(bearing, (int, float)) and math.isfinite(bearing) and gps.speed >= 2.0
        heading_error = (bearing - pose[2] + 180.0) % 360.0 - 180.0 if heading_valid else 0.0
        self._projection_evidence = ProjectionEvidence(float(lateral), heading_error, segment, float(self.progress), heading_valid)
    else:
      # No fix yet: hold the route start so a parked/bench run still renders the route.
      pose = self.tracker.pose(self.progress)

    if self.pose is not None:
      lon, lat, bearing = pose
      plon, plat, pbearing = self.pose
      pose = (plon + EMA_ALPHA * (lon - plon), plat + EMA_ALPHA * (lat - plat),
              pbearing + EMA_ALPHA * ((bearing - pbearing + 180.0) % 360.0 - 180.0))
    self.pose = pose

    if self.renderer is None and now >= self.tiles_failed_at + TILES_RETRY_SECONDS:
      try:
        self.renderer = make_renderer()
      except Exception:
        cloudlog.exception("mapsd.tiles_failed")
        self.tiles_failed_at = now
    if self.renderer is not None and now >= self.tiles_invalid_until:
      try:
        if self.prefetched_route is None:
          self.renderer.prefetch(self.route)
          self.prefetched_route = self.route.copy()
        image, valid = self.renderer.render(*pose, self.route)
        if not valid:
          # The local archive does not cover every area (a partial planet build).
          # A route-only frame is still real guidance, so use it instead of zeros.
          self.tiles_invalid += 1
          if self.tiles_invalid >= TILES_INVALID_LIMIT:
            self.tiles_invalid_until = now + TILES_PARTIAL_BACKOFF
            cloudlog.warning("mapsd.partial_tiles: falling back to route-only frames")
          image, valid = render_route(self.route, *pose)
        else:
          self.tiles_invalid = 0
      except Exception:
        # A corrupt archive or a bad tile: drop the reader and fall back to the route-only
        # frame rather than publishing a half-drawn map.
        cloudlog.exception("mapsd.render_failed")
        self.renderer.close()
        self.renderer = None
        self.tiles_failed_at = now
        image, valid = render_route(self.route, *pose)
    else:
      image, valid = render_route(self.route, *pose)
    if not valid or cplink_navi_active() != phone_active:
      self._invalid("pose_far" if not valid else "source_changed_during_render", now)
      return np.zeros((SIZE, SIZE), dtype=np.uint8), 0
    if phone_active:
      try:
        current_local_binding(self._phone_route_context, self._phone_instruction_context, now_ns=time.monotonic_ns())
      except RouteRejected as error:
        self._invalid(error.reason, time.monotonic())
        return np.zeros((SIZE, SIZE), dtype=np.uint8), 0
    timestamp = sm.logMonoTime[source] if source is not None else time.monotonic_ns()
    self._last_invalid_reason = "accepted"
    return image, timestamp

  def publish_frame(self, server, pm, image: np.ndarray, timestamp: int, *, frame_id: int, renderer_instance_id: str) -> None:
    """Use one immutable pixel payload and exact frame timestamps for IPC and sidecar."""
    eof_ns = time.monotonic_ns()
    phone_now = cplink_navi_active()
    phone_frame = self._cplink_active or phone_now
    payload = image.tobytes()
    fields = None
    if phone_frame:
      try:
        if not phone_now or not self._cplink_active:
          raise RouteRejected("source_changed_before_publish")
        if not timestamp or not image.any():
          raise RouteRejected(self._last_invalid_reason)
        if not 0 <= eof_ns - timestamp < 2_000_000_000:
          raise RouteRejected("position_stale_or_future")
        if self._projection_evidence is None:
          raise RouteRejected("projection_evidence_missing")
        frame = MapPixels(frame_id, timestamp, eof_ns, SIZE, SIZE, payload)
        fields = make_local_map_frame(self._phone_route_context, self._phone_instruction_context, frame,
          renderer_instance_id=renderer_instance_id, location_mono_time_ns=timestamp, projection=self._projection_evidence).to_dict()
      except RouteRejected as error:
        timestamp = 0
        payload = bytes(SIZE * SIZE)
        fields = {"version": 1, "rendererInstanceId": renderer_instance_id, "frameId": frame_id,
                  "timestampSof": 0, "timestampEof": eof_ns, "locationMonoTime": 0,
                  "width": SIZE, "height": SIZE, "pixelFormat": "gray8", "imageCrc32": zlib.crc32(payload),
                  "valid": False, "reason": error.reason, "hasProjectionEvidence": False}
        if self._phone_route_context is not None:
          fields["routeBinding"] = self._phone_route_context.binding.to_dict()
      msg = messaging.new_message("navMapFrameSP", valid=True)
      msg.navMapFrameSP.from_dict(fields)
      pm.send("navMapFrameSP", msg)
    server.send(VisionStreamType.VISION_STREAM_MAP, payload, frame_id, timestamp, eof_ns)


def main():
  rk = Ratekeeper(20.0)
  sm = messaging.SubMaster(["gpsLocation", "gpsLocationExternal", "navRoute", "navInstruction"])
  pm = messaging.PubMaster(["navMapFrameSP"])
  server = VisionIpcServer("mapsd")
  server.create_buffers_with_sizes(VisionStreamType.VISION_STREAM_MAP, 4, SIZE, SIZE, SIZE * SIZE, SIZE, SIZE * SIZE)
  server.start_listener()
  mapsd = MapsD()
  renderer_instance_id = uuid.uuid4().hex
  cloudlog.warning("mapsd: publishing 256x256 route map on VISION_STREAM_MAP")
  while True:
    sm.update(0)
    image, timestamp = mapsd.update(sm)
    mapsd.publish_frame(server, pm, image, timestamp, frame_id=rk.frame, renderer_instance_id=renderer_instance_id)
    rk.keep_time()


if __name__ == "__main__":
  main()
