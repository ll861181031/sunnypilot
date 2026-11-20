#!/usr/bin/env python3
"""Route source for the nav driving model: publishes `navRoute` and `navInstruction`.

Upstream comma has this feeding off Mapbox Directions with comma's API token. This port
keeps the same message contract (and the same maneuver distances, measured from the
route start) but adds an offline route file so the model can be exercised without a
token or network:

  1. `SUNNYPILOT_NAV_ROUTE=/path/route.json`
  2. `$HOME/navigation/route.json`
  3. Mapbox Directions, when `MAPBOX_TOKEN` and `SUNNYPILOT_NAV_DESTINATION="lat,lon"`

Route file schema:

  {"coordinates": [[lon, lat], ...],
   "maneuvers": [{"distance": 120.5, "type": "turn", "modifier": "right",
                  "text": "Turn right onto Main St", "secondary": null}, ...]}

`distance` is meters from the route start to the maneuver, which is what
`navigation_input()` expects to subtract travel from.

Without a route the process publishes nothing: the model then gets zeros, which is a
state it was trained on, rather than a fabricated route.
"""
import json
import os
import re
import time
from collections.abc import Mapping
from pathlib import Path

import openpilot.cereal.messaging as messaging

from openpilot.common.realtime import Ratekeeper
from openpilot.common.swaglog import cloudlog
from openpilot.selfdrive.navigation.cplink import CPLINK_GATE, amap_root_nav_enabled, read_cplink_message, source_active
from openpilot.selfdrive.navigation.amap_phone_route import AmapPhoneRouteAdapter
from openpilot.selfdrive.navigation.nav_map_contract import binding_from_snapshot, route_fields
from openpilot.selfdrive.navigation.helpers import latest_gps_source
from openpilot.selfdrive.navigation.route import RouteTracker, along_path
from openpilot.selfdrive.navigation.routing import Route, fetch_mapbox_route, plan_route

ROUTE_RESEND_PERIOD = 5  # seconds; a restarted mapsd gets the route again soon (and this is the liveness ping)
PHONE_ADB_PORT_DEFAULT = 5039
PHONE_ADB_OWNERSHIP_DEFAULT = "dedicated"
_PHONE_INSTRUCTION_MAX_AGE_S = 2.0
_NAVI_INFO_LOG_PERIOD_S = 5.0
_NAVI_INFO_BRANCH_FIELDS = (
  "maneuver_id_raw", "maneuver_segment_raw", "maneuver_link_raw", "maneuver_point_raw",
  "exit_number_raw", "exit_angle_raw", "obtain_supported_raw", "data_changed_raw",
  "segment_remain_time_raw", "segment_remain_distance_raw", "next_road_name_segment_raw",
  "next_road_name_link_raw", "split_raw", "segment_binding",
)
_NAVI_INFO_SCALAR_FIELDS = (
  "route_remain_time_raw", "route_remain_distance_raw", "navigation_info_type_raw",
  "item_count", "item_index", "current_segment", "current_link", "current_point",
  "branch_flag_raw",
)
# Route sources live in routing.py: AMap Driving first (better Chinese data), OSRM as the
# no-token fallback. Both produce maneuvers in the Valhalla vocabulary the model was trained on.
MANEUVER_TRAVEL_LIMIT = 5000.0  # m; ignore maneuvers further than this from the ego
ON_ROUTE_LATERAL_MAX = 30.0  # m; beyond this the ego is not on the route, so give no maneuvers


def cplink_navi_active() -> bool:
  return amap_root_nav_enabled() or source_active("nav", CPLINK_GATE)


def default_route_path() -> Path:
  return Path(os.getenv("SUNNYPILOT_NAV_ROUTE", str(Path.home() / "navigation" / "route.json")))


def phone_source_settings() -> dict:
  """Resolve the phone attachment without turning a typo into a new daemon or port guess."""
  ownership = os.getenv("NAVDIAG_ADB_OWNERSHIP", PHONE_ADB_OWNERSHIP_DEFAULT)
  raw_port = os.getenv("NAVDIAG_ADB_PORT")
  if ownership not in ("dedicated", "attach") or (raw_port is not None
                                                   and re.fullmatch(r"[1-9][0-9]{0,4}", raw_port) is None):
    raise ValueError("phone_navigation_adb_configuration_invalid")
  port = PHONE_ADB_PORT_DEFAULT if raw_port is None else int(raw_port)
  if port > 65535:
    raise ValueError("phone_navigation_adb_configuration_invalid")
  return {"port": port, "adb_ownership": ownership, "serial": os.getenv("NAVDIAG_SERIAL") or None}


def relative_maneuvers(route: Route, progress: float) -> list[dict]:
  """Maneuvers ahead of the ego, distance measured from the ego, nearest first."""
  ahead = []
  for maneuver in route.maneuvers:
    remaining = float(maneuver.get("distance", 0.0)) - progress
    if 0.0 <= remaining <= MANEUVER_TRAVEL_LIMIT:
      ahead.append({**maneuver, "distance": remaining})
  return sorted(ahead, key=lambda maneuver: maneuver["distance"])


def _phone_log_error(message: str) -> None:
  try:
    cloudlog.error(message)
  except Exception:
    # Logging must never prevent navigation or source cleanup.
    pass


def _bounded_log_integer(value):
  return value if type(value) is int and 0 <= value <= (1 << 63) - 1 else None


def _bounded_log_hash(value):
  return value if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) else None


def _log_mapping(value):
  return value if isinstance(value, Mapping) else {}


def navi_info_evidence(sample) -> dict | None:
  """Return a coordinate-free action snapshot for low-rate diagnostic logging."""
  observation = getattr(sample, "native_after", None)
  if not isinstance(observation, Mapping):
    return None
  value = _log_mapping(observation.get("_navi_info"))
  if not isinstance(value, Mapping):
    return None
  required = {"schema_version", "status", "reason", "diagnostic_only", "control_input_allowed",
              "route_render_input_allowed"}
  if (not required <= value.keys() or value.get("schema_version") != 2
      or value.get("diagnostic_only") is not True
      or value.get("control_input_allowed") is not False
      or value.get("route_render_input_allowed") is not False):
    return {"status": "unavailable", "reason": "unsafe_observation_contract"}
  reason = value.get("reason")
  if not isinstance(reason, str) or re.fullmatch(r"[a-z][a-z0-9_]{0,79}", reason) is None:
    reason = "unknown_reason"
  if value.get("status") == "unavailable":
    return {"status": "unavailable", "reason": reason}
  path_hash = _bounded_log_hash(value.get("path_identity_hash"))
  if (value.get("status") != "observed" or reason != "unverified_navi_info_semantics"
      or value.get("source") != "native_navi_info" or path_hash is None
      or path_hash != _bounded_log_hash(observation.get("path_identity_hash"))):
    return {"status": "unavailable", "reason": "path_identity_unbound"}
  evidence = {"status": "observed", "semantics": "unverified", "diagnostic_only": True,
              "control_input_allowed": False, "route_render_input_allowed": False,
              "path_identity_hash": path_hash}
  for key in _NAVI_INFO_SCALAR_FIELDS:
    checked = _bounded_log_integer(value.get(key))
    if checked is None:
      return {"status": "unavailable", "reason": "malformed_observation"}
    evidence[key] = checked
  if evidence["branch_flag_raw"] not in (0, 1):
    return {"status": "unavailable", "reason": "malformed_observation"}
  branches = value.get("branches")
  if not isinstance(branches, (list, tuple)) or len(branches) != 2:
    return {"status": "unavailable", "reason": "malformed_observation"}
  rows = []
  for branch in branches:
    branch = _log_mapping(branch)
    if branch.keys() != set(_NAVI_INFO_BRANCH_FIELDS):
      return {"status": "unavailable", "reason": "malformed_observation"}
    row = {}
    for key in _NAVI_INFO_BRANCH_FIELDS[:-1]:
      checked = _bounded_log_integer(branch.get(key))
      if checked is None:
        return {"status": "unavailable", "reason": "malformed_observation"}
      row[key] = checked
    binding = _log_mapping(branch.get("segment_binding"))
    status, binding_reason = binding.get("status"), binding.get("reason")
    if (not {"status", "reason"} <= binding.keys()
        or not isinstance(binding_reason, str)
        or re.fullmatch(r"[a-z][a-z0-9_]{0,79}", binding_reason) is None):
      return {"status": "unavailable", "reason": "malformed_observation"}
    if status == "unavailable":
      if binding.keys() != {"status", "reason"}:
        return {"status": "unavailable", "reason": "malformed_observation"}
      row["segment_binding"] = {"status": status, "reason": binding_reason}
    elif (status == "verified" and binding.keys() == {
      "status", "reason", "segment_object_verified", "icon_id_getter_vtable_verified",
      "icon_getter_vtable_verified"} and binding_reason == "segment_object_verified"
          and all(binding.get(key) is True for key in (
            "segment_object_verified", "icon_id_getter_vtable_verified",
            "icon_getter_vtable_verified"))):
      row["segment_binding"] = {
        "status": status, "reason": binding_reason, "segment_object_verified": True,
        "icon_id_getter_vtable_verified": True, "icon_getter_vtable_verified": True}
    else:
      return {"status": "unavailable", "reason": "malformed_observation"}
    rows.append(row)
  evidence["branches"] = rows
  return evidence


class NaviInfoEvidenceLogger:
  """Emit action snapshots on identity/payload change and a slow heartbeat only."""

  def __init__(self):
    self._last_key = None
    self._last_log_at = 0.0
    self._last_reason = None

  def step(self, source, now: float) -> None:
    latest = getattr(source, "latest", None)
    if not callable(latest):
      return
    try:
      sample = latest()
      evidence = navi_info_evidence(sample)
    except Exception:
      _phone_log_error("navd.navi_info_evidence_read_failed")
      return
    if evidence is None:
      return
    key = json.dumps(evidence, sort_keys=True, separators=(",", ":"))
    reason_changed = evidence.get("reason") != self._last_reason
    changed = key != self._last_key
    if not changed and now - self._last_log_at < _NAVI_INFO_LOG_PERIOD_S:
      return
    try:
      cloudlog.event("navd.navi_info_shadow", version=1, sequence=getattr(sample, "sequence", None),
                     changed=changed, reason_changed=reason_changed, evidence=evidence)
      self._last_key, self._last_log_at = key, now
      self._last_reason = evidence.get("reason")
    except Exception:
      _phone_log_error("navd.navi_info_shadow_log_failed")


def drain_phone_rejections(source) -> None:
  """Publish each producer-owned rejection to the existing logMessage path."""
  drain = getattr(source, "drain_rejection_events", None)
  if not callable(drain):
    return
  try:
    batch = drain()
    events, dropped_count = batch["events"], batch["dropped_count"]
  except Exception:
    _phone_log_error("navd.phone_source_rejection_drain_failed")
    return
  for record in events:
    try:
      # Only the bounded, coordinate-free queue contract is made public.
      fields = {key: record[key] for key in ("event_id", "reason", "generation", "sequence",
                                             "host_monotonic_ns", "diagnosis", "diagnosis_present",
                                             "snapshot_retry", "snapshot_retry_present") if key in record}
      cloudlog.event("navd.phone_source_rejection", version=1, **fields)
    except Exception:
      _phone_log_error("navd.phone_source_rejection_log_failed")
  if dropped_count:
    try:
      cloudlog.event("navd.phone_source_rejections_dropped", version=1, dropped_count=dropped_count)
    except Exception:
      _phone_log_error("navd.phone_source_rejection_log_failed")


class Navd:
  def __init__(self, sm, pm, params=None, phone_source=None):
    self.sm = sm
    self.pm = pm
    self.params = params
    self.route: Route | None = None
    self.tracker: RouteTracker | None = None
    self.route_stamp: float | None = None
    self.route_changed = True
    self.progress = 0.0
    self.on_route = False
    self._cplink_active = False
    self.sent_route_at = 0.0
    self.sent_instruction_at = 0.0
    self.destination_key: tuple | None = None
    self.phone_source = phone_source
    self.phone_adapter = AmapPhoneRouteAdapter()
    self._phone_publication = None
    self._phone_publication_at = 0.0
    self.navi_info_logger = NaviInfoEvidenceLogger()

  def _destination(self) -> dict | None:
    """Destination from Params NavDestination, else the env override.

    Shape: {"lat", "lon", "waypoints": [{"lat", "lon"}], "name"}. Waypoints were added for
    multi-stop routes; older values simply have no list."""
    raw = None
    try:
      from openpilot.common.params import Params
      raw = (self.params or Params()).get("NavDestination")
    except Exception:
      raw = None  # params key missing on an old lib: keep the env path working
    if isinstance(raw, dict):
      try:
        waypoints = []
        for point in raw.get("waypoints") or []:
          if isinstance(point, dict):
            waypoints.append({"lat": float(point["latitude"]), "lon": float(point["longitude"])})
        return {"lat": float(raw["latitude"]), "lon": float(raw["longitude"]),
                "waypoints": waypoints, "name": raw.get("place_name")}
      except (KeyError, TypeError, ValueError):
        pass  # malformed destination: fall through to the env override
    spec = os.environ.get("SUNNYPILOT_NAV_DESTINATION")
    if spec:
      try:
        lat, lon = (float(v) for v in spec.split(","))
        return {"lat": lat, "lon": lon, "waypoints": [], "name": None}
      except ValueError:
        return None
    return None

  def _maybe_load_route(self, now: float) -> None:
    path = default_route_path()
    try:
      stamp = path.stat().st_mtime
    except OSError:
      stamp = None
    if stamp != self.route_stamp:
      self.route_stamp = stamp
      self.route = Route.from_file(path) if stamp is not None else None
      self.route_changed = True
      self.tracker = RouteTracker(self.route.coordinates) if self.route is not None and len(self.route.coordinates) >= 2 else None
      if self.route is not None:
        cloudlog.warning(f"navd.route_loaded: {len(self.route.coordinates)} points, {len(self.route.maneuvers)} maneuvers")

    destination = self._destination()
    if destination is None:
      self.destination_key = None
      return
    key = (round(destination["lat"], 7), round(destination["lon"], 7),
           tuple((round(point["lat"], 7), round(point["lon"], 7)) for point in destination["waypoints"]))
    if key == self.destination_key and self.tracker is not None:
      return
    self.destination_key = key
    position = self._position()
    if position is None:
      return  # route requests without a fix would start from a guessed location
    target = (destination["lon"], destination["lat"])
    waypoints = [(point["lon"], point["lat"]) for point in destination["waypoints"]]
    token = os.environ.get("MAPBOX_TOKEN")
    route = fetch_mapbox_route(token, position, target) if token else None
    source = "mapbox" if route is not None else "none"
    if route is None:
      route, source = plan_route(position, target, waypoints, handle=self.params)
    if route is not None:
      self.route = route
      self.tracker = RouteTracker(route.coordinates)
      self.route_changed = True
      summary = f"{len(route.coordinates)} points, {len(route.maneuvers)} maneuvers, {len(waypoints)} waypoints"
      cloudlog.warning(f"navd.route_from_{source}: {summary}")

  def _position(self) -> tuple[float, float] | None:
    source = latest_gps_source(self.sm)
    if source is None:
      return None
    gps = self.sm[source]
    # Last position is (latitude, longitude); navRoute/RouteTracker work in (lon, lat).
    return gps.longitude, gps.latitude

  def _update_progress(self) -> None:
    position = self._position()
    if position is None or self.tracker is None:
      self.on_route = False
      return
    _, lateral = along_path(self.route.coordinates, position)
    # Off-route maneuvers are worse than none: the model would be told to turn when the
    # route is somewhere else entirely.
    self.on_route = bool(lateral <= ON_ROUTE_LATERAL_MAX)
    if self.on_route:
      self.progress = float(self.tracker.project(*position))

  def publish_route(self, now: float) -> None:
    phone_active = cplink_navi_active()
    msg = messaging.new_message("navRoute", valid=not phone_active)
    if phone_active and self.phone_source is not None:
      sample = self.phone_source.latest()
      source_status = None
      if sample is None and hasattr(self.phone_source, "status"):
        source_status = self.phone_source.status()
      publication = self.phone_adapter.accept(
        sample, now_ns=round(now * 1e9), source_status=source_status)
      if publication is not None:
        msg.valid = True
        reused = publication is self._phone_publication
        self._phone_publication = publication
        if not reused:
          self._phone_publication_at = float(now)
        fields = route_fields(publication.snapshot, publication.active, now_ns=round(now * 1e9))
        fields["phonePose"] = publication.phone_pose
        fields["phonePose"]["reason"] = publication.phone_pose["reason"]
        msg.navRoute.from_dict(fields)
      else:
        self._phone_publication = None
        self._phone_publication_at = 0.0
        msg.navRoute.phonePose.version = 1
        msg.navRoute.phonePose.reason = self.phone_adapter.reason
        if self.phone_adapter.reason == "phone_source_unavailable" and hasattr(self.phone_source, "status"):
          reason = self.phone_source.status().reason
          if isinstance(reason, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", reason):
            msg.navRoute.phonePose.reason = reason
    if self.route is not None and not phone_active:
      msg.navRoute.coordinates = [{"latitude": float(lat), "longitude": float(lon)}
                                  for lon, lat in self.route.coordinates]
    self.pm.send("navRoute", msg)
    self.sent_route_at = now
    self.route_changed = False

  def publish_instruction(self, now: float) -> None:
    if amap_root_nav_enabled():
      publication = self._phone_publication
      observed = self._phone_publication_at
      msg = messaging.new_message("navInstruction", valid=False)
      if (publication is not None and publication.instruction is not None
          and 0 <= now - observed < _PHONE_INSTRUCTION_MAX_AGE_S):
        try:
          binding = binding_from_snapshot(publication.snapshot, publication.active,
                                          now_ns=round(now * 1e9))
          instruction = publication.instruction
          distance = float(instruction["distance"])
          msg.valid = True
          msg.navInstruction.allManeuvers = [{
            "distance": distance, "type": instruction["type"], "modifier": instruction["modifier"]}]
          msg.navInstruction.maneuverDistance = distance
          msg.navInstruction.maneuverType = instruction["type"]
          msg.navInstruction.maneuverModifier = instruction["modifier"]
          msg.navInstruction.distanceRemaining = float(instruction["routeRemainDistanceRaw"])
          msg.navInstruction.timeRemaining = float(instruction["routeRemainTimeRaw"])
          msg.navInstruction.routeBinding.from_dict(binding.to_dict())
        except (KeyError, TypeError, ValueError, OverflowError):
          msg = messaging.new_message("navInstruction", valid=False)
      self.sent_instruction_at = now
      self.pm.send("navInstruction", msg)
      return
    if cplink_navi_active():
      msg = read_cplink_message("navInstruction", CPLINK_GATE)
      if msg is None:
        msg = messaging.new_message("navInstruction", valid=False)
      self.pm.send("navInstruction", msg)
      self.sent_instruction_at = now
      return
    msg = messaging.new_message("navInstruction", valid=True)
    instruction = msg.navInstruction
    instruction.maneuverPrimaryText = ""
    instruction.maneuverSecondaryText = ""
    instruction.maneuverType = ""
    instruction.maneuverModifier = ""
    instruction.speedLimit = 0.0
    if self.route is None or not self.on_route:
      msg.valid = False
      instruction.allManeuvers = []
      instruction.distanceRemaining = 0.0
      self.pm.send("navInstruction", msg)
      self.sent_instruction_at = now
      return
    ahead = relative_maneuvers(self.route, self.progress)
    msg.valid = bool(ahead)
    instruction.allManeuvers = [{"distance": float(m["distance"]), "type": m.get("type", ""),
                                 "modifier": m.get("modifier", "")} for m in ahead]
    if ahead:
      nearest = ahead[0]
      instruction.maneuverPrimaryText = nearest.get("text") or ""
      instruction.maneuverSecondaryText = nearest.get("secondary") or ""
      instruction.maneuverType = nearest.get("type", "")
      instruction.maneuverModifier = nearest.get("modifier", "")
      instruction.maneuverDistance = float(nearest["distance"])
    total = float(self.tracker.distance[-1]) if self.tracker is not None else 0.0
    instruction.distanceRemaining = float(max(total - self.progress, 0.0))
    self.pm.send("navInstruction", msg)
    self.sent_instruction_at = now

  def step(self, now: float) -> None:
    phone_active = cplink_navi_active()
    if phone_active != self._cplink_active:
      self._cplink_active = phone_active
      self.route_changed = True
    if phone_active:
      if self.route_changed or (self.phone_source is not None and now - self.sent_route_at >= 0.2):
        self.publish_route(now)
      if now - self.sent_instruction_at >= 0.2:
        self.publish_instruction(now)
      self.navi_info_logger.step(self.phone_source, now)
      return
    self._maybe_load_route(now)
    self._update_progress()
    if self.route_changed or (self.route is not None and now - self.sent_route_at >= ROUTE_RESEND_PERIOD):
      self.publish_route(now)
    if now - self.sent_instruction_at >= 0.2:
      self.publish_instruction(now)


def main():
  rk = Ratekeeper(20.0)
  sm = messaging.SubMaster(["gpsLocation", "gpsLocationExternal"])
  pm = messaging.PubMaster(["navRoute", "navInstruction"])
  navd = Navd(sm, pm)
  source = None
  try:
    if amap_root_nav_enabled():
      from APP.navdiag.phone_route_producer import PhoneRouteProducer
      source = PhoneRouteProducer(**phone_source_settings()).start()
      navd.phone_source = source
    while True:
      sm.update(0)
      navd.step(time.monotonic())
      drain_phone_rejections(source)
      rk.keep_time()
  finally:
    if source is not None:
      try:
        stopped = source.close()
      finally:
        # close() joins the producer before the final queue drain.
        drain_phone_rejections(source)
      if not stopped or not source.status().cleanup_verified:
        _phone_log_error("navd.phone_source_cleanup_incomplete")


if __name__ == "__main__":
  main()
