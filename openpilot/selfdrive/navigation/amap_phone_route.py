"""Trusted local adapter for the fixed-profile, read-only phone producer."""
from dataclasses import dataclass
import hashlib
import math
import re
import struct

from openpilot.common.gcj import wgs84_to_gcj02
from openpilot.selfdrive.navigation.amap_route_anchor import AmapRouteAnchorIndex, AmapRouteAnchorRejected
from openpilot.selfdrive.navigation.nav_phone_quality import (PHONE_MAP_MAX_ACCURACY_M,
  PHONE_MAP_MAX_PROJECTION_ERROR_M, PHONE_MAP_MAX_POSE_ERROR_M, classify_phone_position_quality)
from openpilot.selfdrive.navigation.phone_route import (ActiveRouteBinding, PhoneRouteLease, RouteIdentity,
                                                        build_native_length_evidence,
                                                      RouteRejected, RouteSnapshot, SourceProvenance)
from tools.cplink_shim.amap_route_cache import SUPPORTED_PROFILE, RouteCacheDecodeError, decode_cache

_SAFE_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")

NATIVE_PROFILE = "amapauto-9.5.0.600013-arm64-v1"
_VERIFIED_ROOT_MANEUVERS = {
  2: ("turn", "left"),
  3: ("turn", "right"),
  5: ("turn", "slight right"),
  7: ("turn", "sharp right"),
  8: ("turn", "uturn"),
  11: ("roundabout", ""),
  12: ("exit roundabout", ""),
}
_NATIVE_GEOMETRY_KIND = "current_native_path_window"
_NATIVE_GEOMETRY_DECODER = "native-drive-path-v1"
_BROADCAST_AGE_NS = 2_000_000_000
_PUBLICATION_MAX_AGE_NS = 2_000_000_000
_PHONE_REFRESH_HOLD_REASONS = {
  "phone_selection_refreshing",
  "phone_selection_recovering",
  "selection_changed_during_read",
}
_PHONE_TRANSIENT_REFRESH_EXPIRED = "phone_transient_refresh_expired"


def _exact_integer(value, minimum=0, maximum=(1 << 63) - 1):
  return type(value) is int and minimum <= value <= maximum


def _verified_roundabout_exit(sample, selected, navi, branch, fields):
  exit_number = branch.get("exit_number_raw")
  if (not _exact_integer(exit_number, 1, 0xfffffffe)
      or not _exact_integer(fields.get("ROUNG_ABOUT_NUM"), 1, 0xfffffffe)
      or fields["ROUNG_ABOUT_NUM"] != exit_number):
    raise RouteRejected("phone_instruction_roundabout_exit_mismatch")
  before = sample.native_before.get("_navi_info") if hasattr(sample.native_before, "get") else None
  flag = navi.get("branch_flag_raw")
  if (not hasattr(before, "get") or not _exact_integer(flag, 0, 1)
      or navi.get("reason") != "unverified_navi_info_semantics"
      or navi.get("diagnostic_only") is not True or navi.get("control_input_allowed") is not False
      or before.get("status") != "observed" or before.get("path_identity_hash") != selected.identity_hash
      or any(before.get(key) != navi.get(key) for key in (
        "reason", "diagnostic_only", "control_input_allowed", "navigation_info_type_raw",
        "item_count", "item_index", "branch_flag_raw", "current_segment", "current_link", "current_point",
        "route_remain_distance_raw", "route_remain_time_raw"))):
    raise RouteRejected("phone_instruction_roundabout_snapshot_mismatch")
  branches = before.get("branches")
  if not isinstance(branches, (list, tuple)) or len(branches) != 2 or not hasattr(branches[flag], "get"):
    raise RouteRejected("phone_instruction_roundabout_snapshot_mismatch")
  first = branches[flag]
  binding = branch.get("segment_binding")
  if (not _exact_integer(first.get("exit_number_raw"), 1, 0xfffffffe)
      or not hasattr(binding, "get") or binding.get("status") != "verified"
      or binding.get("reason") != "segment_object_verified"
      or any(binding.get(key) is not True for key in (
        "segment_object_verified", "icon_id_getter_vtable_verified", "icon_getter_vtable_verified"))
      or branch.get("obtain_supported_raw") != 1
      or any(first.get(key) != branch.get(key) for key in (
        "maneuver_id_raw", "maneuver_segment_raw", "maneuver_link_raw", "maneuver_point_raw",
        "exit_number_raw", "exit_angle_raw", "obtain_supported_raw", "segment_binding",
        "segment_remain_distance_raw", "segment_remain_time_raw"))):
    raise RouteRejected("phone_instruction_roundabout_snapshot_mismatch")
  return exit_number


def _verify_unrenderable_uturn(sample, navi, branch):
  """Accept only the observed segment-level left U-turn shape from this profile.

  Fixed libGbl's ManeuverRender::isSupportIcon excludes icon 8 before checking
  segment drawing data. Its result writes NaviInfoData.isObtainSupported; zero
  is therefore expected for this icon, not an action-validity bit. Raw native
  pairs, broadcast fields and the selected segment must still agree.
  Evidence: APP/navdiag/captures/native-action-semantics-20261117-003358/.
  """
  before = sample.native_before.get("_navi_info") if hasattr(sample.native_before, "get") else None
  position = sample.native_after.get("_position", {})
  if (navi["branch_flag_raw"] != 0 or branch["obtain_supported_raw"] != 0
      or not _exact_integer(branch.get("maneuver_segment_raw"), 0, 0x7fffffff)
      or branch["maneuver_segment_raw"] != navi.get("current_segment")
      or any(not _exact_integer(navi.get("current_" + key), 0, 0x7fffffff)
             or navi["current_" + key] != position.get(key + "_index") for key in ("segment", "link", "point"))
      or any(type(branch.get(key)) is not int or branch[key] != expected for key, expected in (
        ("maneuver_link_raw", 0xffffffff), ("maneuver_point_raw", 0xffffffff),
        ("exit_number_raw", 0), ("exit_angle_raw", 0)))):
    raise RouteRejected("phone_instruction_uturn_shape_unverified")
  if (not hasattr(before, "get") or before != navi
      or any(type(before.get(key)) is not type(value) for key, value in navi.items() if key != "branches")):
    raise RouteRejected("phone_instruction_uturn_snapshot_mismatch")
  first = before["branches"][0]
  if (any(type(first.get(key)) is not type(value) for key, value in branch.items())
      or any(first.get("segment_binding", {}).get(key) is not True for key in (
        "segment_object_verified", "icon_id_getter_vtable_verified", "icon_getter_vtable_verified"))):
    raise RouteRejected("phone_instruction_uturn_snapshot_mismatch")


def _verified_root_action_source(sample, selected):
  """Validate the native action equally for new broadcasts and previous-action holds."""
  observation = sample.native_after
  navi = observation.get("_navi_info") if hasattr(observation, "get") else None
  if (not hasattr(navi, "get") or navi.get("status") != "observed"
      or navi.get("reason") != "unverified_navi_info_semantics"
      or navi.get("diagnostic_only") is not True or navi.get("control_input_allowed") is not False
      or navi.get("path_identity_hash") != selected.identity_hash
      or navi.get("navigation_info_type_raw") != 0
      or not _exact_integer(navi.get("item_count"), 1, 32)
      or not _exact_integer(navi.get("item_index"), 0, navi["item_count"] - 1)
      or not _exact_integer(navi.get("route_remain_time_raw"), 0, 0x7fffffff)
      or not _exact_integer(navi.get("route_remain_distance_raw"), 0, 0x7fffffff)
      or not _exact_integer(navi.get("branch_flag_raw"), 0, 1)):
    raise RouteRejected("phone_instruction_unavailable")
  branches = navi.get("branches")
  if not isinstance(branches, (tuple, list)) or len(branches) != 2:
    raise RouteRejected("phone_instruction_branches_invalid")
  for candidate in branches:
    if not hasattr(candidate, "get") or any(
      not _exact_integer(candidate.get(field), 0, 0x7fffffff)
      and not (type(candidate.get(field)) is int and candidate[field] == 0xffffffff)
      for field in ("next_road_name_segment_raw", "next_road_name_link_raw")
    ):
      raise RouteRejected("phone_instruction_next_road_unverified")
  branch = branches[navi["branch_flag_raw"]]
  if (not hasattr(branch, "get") or not _exact_integer(branch.get("obtain_supported_raw"), 0, 1)
      or not _exact_integer(branch.get("segment_remain_time_raw"), 0, 0x7fffffff)
      or not _exact_integer(branch.get("segment_remain_distance_raw"), 0, 0x7fffffff)):
    raise RouteRejected("phone_instruction_branch_unsupported")
  maneuver_id = branch.get("maneuver_id_raw")
  if type(maneuver_id) is not int:
    raise RouteRejected("phone_instruction_maneuver_unverified")
  if maneuver_id != 8 and branch["obtain_supported_raw"] != 1:
    raise RouteRejected("phone_instruction_branch_unsupported")
  binding = branch.get("segment_binding")
  if (not hasattr(binding, "get") or binding.get("status") != "verified"
      or binding.get("reason") != "segment_object_verified"
      or any(binding.get(key) is not True for key in (
        "segment_object_verified", "icon_id_getter_vtable_verified",
        "icon_getter_vtable_verified"))):
    raise RouteRejected("phone_instruction_segment_unverified")
  if maneuver_id == 8:
    _verify_unrenderable_uturn(sample, navi, branch)
  maneuver_type, modifier = _VERIFIED_ROOT_MANEUVERS.get(maneuver_id, (None, None))
  if maneuver_type is None:
    raise RouteRejected("phone_instruction_maneuver_unverified")
  return navi, branch, maneuver_type, modifier


def _verified_root_instruction(sample, selected, positions):
  """Return a coordinate-free action only when native and broadcast describe one state."""
  navi, branch, maneuver_type, modifier = _verified_root_action_source(sample, selected)
  broadcast = getattr(sample, "broadcast", None)
  record = _broadcast_record(broadcast)
  fields = record.get("fields") if hasattr(record, "get") else None
  host_started = broadcast.get("host_read_started_ns") if hasattr(broadcast, "get") else None
  host_finished = broadcast.get("host_read_finished_ns") if hasattr(broadcast, "get") else None
  if (not hasattr(broadcast, "get") or not hasattr(record, "get")
      or broadcast.get("status") != "observed" or broadcast.get("bridge_transport_valid") is not True
      or broadcast.get("receiver_active") is not True or record.get("fresh") is not True
      or not hasattr(fields, "get")
      or not _exact_integer(host_started, 1) or not _exact_integer(host_finished, host_started)
      or not _exact_integer(record.get("source_age_upper_ns"), 0, _BROADCAST_AGE_NS)):
    raise RouteRejected("phone_instruction_broadcast_unverified")
  times = (
    (fields.get("TYPE"), 0),
    (fields.get("ICON"), branch["maneuver_id_raw"]),
    (fields.get("NEW_ICON"), branch["maneuver_id_raw"]),
    (fields.get("SEG_REMAIN_DIS"), branch["segment_remain_distance_raw"]),
    (fields.get("SEG_REMAIN_TIME"), branch["segment_remain_time_raw"]),
    (fields.get("ROUTE_REMAIN_DIS"), navi["route_remain_distance_raw"]),
    (fields.get("ROUTE_REMAIN_TIME"), navi["route_remain_time_raw"]),
    (fields.get("CUR_SEG_NUM"), navi["current_segment"]),
  )
  if any(type(actual) is not int or actual != expected for actual, expected in times):
    raise RouteRejected("phone_instruction_broadcast_mismatch")
  extra = ({"roundaboutExitRaw": _verified_roundabout_exit(sample, selected, navi, branch, fields)}
           if maneuver_type in ("roundabout", "exit roundabout") else {})
  distance = float(branch["segment_remain_distance_raw"])
  return {"distance": distance, "type": maneuver_type, "modifier": modifier,
          "segmentRemainTimeRaw": branch["segment_remain_time_raw"],
          "routeRemainDistanceRaw": navi["route_remain_distance_raw"],
          "routeRemainTimeRaw": navi["route_remain_time_raw"], **extra}


def _broadcast_record(broadcast):
  if not hasattr(broadcast, "get"):
    return None
  records = broadcast.get("records")
  return records.get("10001") if hasattr(records, "get") else None


def _require_new_broadcast(broadcast):
  """A replayed source may only be used by the explicit previous-action hold path."""
  record = _broadcast_record(broadcast)
  if not hasattr(record, "get") or record.get("replayed") is not True:
    return
  raise RouteRejected("phone_instruction_broadcast_replayed")


def _fresh_replayed_root_instruction(sample, selected, positions, previous):
  """Hold only the previous verified action while its exact broadcast is replayed.

  The bridge may publish 10001 less often than the producer samples it. In this
  narrow case a matching, fresh replay is stale for the action update but not for
  the route geometry or GPS lease. The caller only enters this path after the full
  native/geometry/GPS validation succeeded.
  """
  if not isinstance(previous, dict):
    raise RouteRejected("phone_instruction_unavailable")
  broadcast = getattr(sample, "broadcast", None)
  record = _broadcast_record(broadcast)
  fields = record.get("fields") if hasattr(record, "get") else None
  if (not hasattr(broadcast, "get") or not hasattr(fields, "get")
      or broadcast.get("status") not in ("observed", "unavailable")
      or record.get("replayed") is not True
      or not _exact_integer(record.get("source_age_upper_ns"), 0, _BROADCAST_AGE_NS)
      or not _exact_integer(record.get("sequence"), 0)):
    raise RouteRejected("phone_instruction_broadcast_unverified")
  # A replayed bridge record is expected to be the source of the previous action.
  # Reconstructing it from the current native snapshot protects against a stale
  # 10001 being paired with a new maneuver or route state.
  navi, branch, maneuver_type, modifier = _verified_root_action_source(sample, selected)
  times = (
    (fields.get("TYPE"), 0),
    (fields.get("ICON"), branch.get("maneuver_id_raw")),
    (fields.get("NEW_ICON"), branch.get("maneuver_id_raw")),
    (fields.get("SEG_REMAIN_DIS"), branch.get("segment_remain_distance_raw")),
    (fields.get("SEG_REMAIN_TIME"), branch.get("segment_remain_time_raw")),
    (fields.get("ROUTE_REMAIN_DIS"), navi.get("route_remain_distance_raw")),
    (fields.get("ROUTE_REMAIN_TIME"), navi.get("route_remain_time_raw")),
    (fields.get("CUR_SEG_NUM"), navi.get("current_segment")),
  )
  if any(type(actual) is not int or actual != expected for actual, expected in times):
    raise RouteRejected("phone_instruction_broadcast_mismatch")
  expected = maneuver_type, modifier
  previous_pair = previous.get("type"), previous.get("modifier")
  if previous_pair != expected:
    raise RouteRejected("phone_instruction_maneuver_unverified")
  if expected[0] in ("roundabout", "exit roundabout"):
    exit_number = _verified_roundabout_exit(sample, selected, navi, branch, fields)
    if type(previous.get("roundaboutExitRaw")) is not int or previous["roundaboutExitRaw"] != exit_number:
      raise RouteRejected("phone_instruction_roundabout_exit_changed")
  return {**previous,
          "distance": float(branch["segment_remain_distance_raw"]),
          "segmentRemainTimeRaw": branch["segment_remain_time_raw"],
          "routeRemainDistanceRaw": navi["route_remain_distance_raw"],
          "routeRemainTimeRaw": navi["route_remain_time_raw"]}

def _distance(a, b):
  for point in (a, b):
    if (not isinstance(point, (tuple, list)) or len(point) != 2
        or any(type(value) not in (int, float) or not math.isfinite(value) for value in point)
        or not -180 <= point[0] <= 180 or not -90 <= point[1] <= 90):
      raise RouteRejected("phone_position_coordinate_invalid")
  return math.hypot((a[0] - b[0]) * math.cos(math.radians((a[1] + b[1]) / 2)), a[1] - b[1]) * 111195.0802335329


def _path_hash(path_id, epoch):
  return hashlib.sha256(b"amap-path-observation-v1\0" + epoch.encode() + b"\0" + struct.pack("<I", path_id)).hexdigest()


@dataclass(frozen=True)
class PhoneRoutePublication:
  snapshot: RouteSnapshot
  active: ActiveRouteBinding
  phone_pose: dict
  instruction: dict | None = None


class AmapPhoneRouteAdapter:
  """Only producer-owned observations establish provenance; never accept phone verified flags."""
  def __init__(self):
    self._session = None
    self._lease = None
    self._key = None
    self._index = None
    self._identity = None
    self._source_object = None
    self._last_sequence = 0
    self._last_observation_ns = 0
    self._last_gps = None
    self._publication = None
    self._publication_host_start_ns = None
    self._publication_gps = None
    self._missing_source_barrier_armed = False
    self._expired_transient_refresh = None
    self.reason = "phone_source_unavailable"
    self._instruction_reason = "phone_instruction_unavailable"
    self._decision = self._empty_decision()
    self._last_verified_instruction_key = None

  def withdraw(self, now_ns, reason="phone_source_unavailable", *, advance_barrier=True):
    if self._lease is not None and advance_barrier:
      self._lease.withdraw(now=now_ns / 1e9)
      self._missing_source_barrier_armed = True
    if advance_barrier:
      self._expired_transient_refresh = None
    self._key = self._index = self._identity = self._source_object = None
    self._publication = None
    self._publication_host_start_ns = None
    self._publication_gps = None
    self.reason = reason

  @staticmethod
  def _error_code(error) -> str:
    """Fixed, coordinate-free code from a rejection, or empty when unsuitable."""
    code = error.args[0] if error.args else None
    return code if type(code) is str and _SAFE_CODE.fullmatch(code) else ""

  def decision_evidence(self) -> dict:
    """Same-decision record; every time is host-monotonic seconds and unknown is explicit."""
    record = self._decision
    return {"clock": "host_monotonic_s", "reason": self.reason, **record}

  @property
  def instruction_reason(self) -> str:
    return self._instruction_reason

  @staticmethod
  def _empty_decision() -> dict:
    """Every field exists from construction; unknown is explicit, never a call time."""
    return {"accept_called_mono_s": "unknown", "sample_observed_mono_s": "unknown",
            "sample_sequence": "unknown", "accept_begin_withdrawn_at_mono_s": "unknown",
            "accept_final_withdrawn_at_mono_s": "unknown", "after_observe_withdrawn_at_mono_s": "unknown",
            "lease_last_observed_mono_s": "unknown", "decision_reason": "unknown",
            "same_gps_token": "unknown", "sample_host_age_ns": "unknown",
            "gps_source_age_upper_ns": "unknown", "gps_position_age_upper_ns": "unknown",
            "gps_host_window_ns": "unknown", "position_publish_clock": "host_monotonic_ns",
            "publication_reused": False, "source_status_reason": "unknown",
            "held_publication_age_ns": "unknown"}

  def accept(self, sample, *, now_ns, source_status=None):
    self._decision = self._empty_decision() | {"accept_called_mono_s": now_ns / 1e9}
    self._decision["accept_begin_withdrawn_at_mono_s"] = getattr(
      self._lease, "_withdrawn_at", "unknown")
    try:
      publication = self._accept(sample, now_ns=now_ns, source_status=source_status)
      self._missing_source_barrier_armed = False
      self._expired_transient_refresh = None
      return publication
    except (ValueError, TypeError, AttributeError, KeyError, OverflowError) as error:
      if isinstance(error, RouteRejected):
        reason = error.reason
      elif isinstance(error, (AmapRouteAnchorRejected, RouteCacheDecodeError)):
        # Keep the fixed sub-reason so a rejected anchor or cache stays diagnosable.
        prefix = "phone_anchor_" if isinstance(error, AmapRouteAnchorRejected) else "phone_cache_decode_"
        reason = prefix + (self._error_code(error) or "unspecified")
      else:
        reason = "phone_sample_rejected"
      expired_refresh = None
      if reason == "phone_source_unavailable":
        expired_refresh = self._transient_refresh_expired(now_ns, source_status)
        if expired_refresh is None and self._expired_transient_refresh is not None:
          expired_refresh = self._transient_refresh_expired(
            now_ns, source_status, expired=True)
      if expired_refresh is not None:
        reason = _PHONE_TRANSIENT_REFRESH_EXPIRED
        self._expired_transient_refresh = expired_refresh
        self._decision["source_status_reason"] = getattr(source_status, "reason", "unknown")
        self._decision["held_publication_age_ns"] = (
          now_ns - expired_refresh[3]
          if type(now_ns) is int and type(expired_refresh[3]) is int
          else "unknown")
        self._decision["sample_observed_mono_s"] = expired_refresh[2]
        self._decision["sample_sequence"] = expired_refresh[1]
      # A delayed pre-withdrawal observation must not keep moving its own recovery barrier.
      # Repeated missing-source polls during one producer refresh must also preserve the
      # first barrier. The new sample starts after that withdrawal and may finish later.
      missing_after_withdrawal = (
        reason == "phone_source_unavailable"
        and self._missing_source_barrier_armed)
      self.withdraw(now_ns, reason, advance_barrier=(
        reason not in ("observation_before_withdrawal", _PHONE_TRANSIENT_REFRESH_EXPIRED)
        and not missing_after_withdrawal))
      if reason == _PHONE_TRANSIENT_REFRESH_EXPIRED:
        self._expired_transient_refresh = expired_refresh
      self._decision["decision_reason"] = reason
      return None
    finally:
      # Real barrier after every exit path of this decision; never a synthetic field.
      self._decision["accept_final_withdrawn_at_mono_s"] = getattr(
        self._lease, "_withdrawn_at", "unknown")
      if self._decision["decision_reason"] == "unknown":
        self._decision["decision_reason"] = self.reason

  def _hold_verified_publication(self, now_ns, source_status):
    """Only bridge one known same-session producer refresh; never renew its clocks."""
    publication = self._publication
    if (publication is None or self._lease is None or self._session is None
        or self._publication_host_start_ns is None or self._publication_gps is None):
      raise RouteRejected("phone_source_unavailable")
    if not hasattr(source_status, "running") or source_status.running is not True:
      raise RouteRejected("phone_source_unavailable")
    if not hasattr(source_status, "connected") or source_status.connected is not True:
      raise RouteRejected("phone_source_unavailable")
    reason = getattr(source_status, "reason", None)
    if reason not in _PHONE_REFRESH_HOLD_REASONS:
      raise RouteRejected("phone_source_unavailable")
    session = getattr(source_status, "transport_session", None)
    sequence = getattr(source_status, "sequence", None)
    if (session != publication.snapshot.transport_session_id or session != self._session
        or type(sequence) is not int or sequence != publication.active.observed_sequence):
      raise RouteRejected("phone_source_unavailable")
    if type(now_ns) is not int:
      raise RouteRejected("phone_source_unavailable")
    host_age_ns = now_ns - self._publication_host_start_ns
    if not 0 <= host_age_ns < _PUBLICATION_MAX_AGE_NS:
      raise RouteRejected("phone_source_unavailable")
    gps = self._publication_gps
    if not gps.fresh_at(now_ns):
      raise RouteRejected("phone_source_unavailable")
    snapshot = self._lease.snapshot(now=now_ns / 1e9)
    if (snapshot is None or snapshot.binding_id != publication.snapshot.binding_id
        or snapshot.identity != publication.snapshot.identity
        or snapshot.transport_session_id != publication.snapshot.transport_session_id
        or snapshot.source_object_id != publication.snapshot.source_object_id
        or snapshot.observed_sequence != publication.active.observed_sequence
        or snapshot.observed_mono_time != publication.active.observed_mono_time):
      raise RouteRejected("phone_source_unavailable")
    self._decision.update(
      publication_reused=True, source_status_reason=reason, held_publication_age_ns=host_age_ns,
      sample_observed_mono_s=publication.active.observed_mono_time,
      sample_sequence=publication.active.observed_sequence,
      lease_last_observed_mono_s=self._lease._last_observed,
      same_gps_token=True,
      sample_host_age_ns=host_age_ns,
      gps_source_age_upper_ns=(gps.age_upper_ns + now_ns - gps.host_finished_ns
                               if type(gps.host_finished_ns) is int and type(gps.age_upper_ns) is int
                               and now_ns >= gps.host_finished_ns else "unknown"),
      gps_position_age_upper_ns=now_ns - publication.phone_pose["positionMonoTime"],
      gps_host_window_ns=(gps.host_finished_ns - gps.host_started_ns
                          if type(gps.host_finished_ns) is int and type(gps.host_started_ns) is int
                          else "unknown"),
      position_publish_clock="host_monotonic_ns",
      decision_reason="accepted_transient_source_refresh")
    return publication

  def _transient_refresh_expired(self, now_ns, source_status, *, expired=False):
    publication = self._publication
    if (self._lease is None or self._session is None
        or not hasattr(source_status, "running") or source_status.running is not True
        or not hasattr(source_status, "connected") or source_status.connected is not True
        or getattr(source_status, "reason", None) not in _PHONE_REFRESH_HOLD_REASONS):
      return None
    status_session = getattr(source_status, "transport_session", None)
    status_sequence = getattr(source_status, "sequence", None)
    if (status_session != self._session
        or type(status_sequence) is not int
        or type(now_ns) is not int):
      return None
    if expired:
      identity = (status_session, status_sequence)
      return (self._expired_transient_refresh
              if identity == self._expired_transient_refresh[:2] else None)
    if (publication is None or self._publication_host_start_ns is None or self._publication_gps is None
        or status_session != publication.snapshot.transport_session_id
        or status_sequence != publication.active.observed_sequence):
      return None
    host_age_ns = now_ns - self._publication_host_start_ns
    if host_age_ns >= _PUBLICATION_MAX_AGE_NS or not self._publication_gps.fresh_at(now_ns):
      return (status_session, status_sequence, publication.active.observed_mono_time,
              self._publication_host_start_ns)
    return None

  def _accept(self, sample, *, now_ns, source_status=None):
    if sample is None:
      return self._hold_verified_publication(now_ns, source_status)
    if (type(now_ns) is not int or type(sample.host_start_ns) is not int
        or not 0 <= now_ns - sample.host_start_ns < 2_000_000_000):
      raise RouteRejected("phone_observation_stale_or_future")
    gps = sample.gps
    self._decision["sample_host_age_ns"] = now_ns - sample.host_start_ns
    self._decision["gps_host_window_ns"] = gps.host_finished_ns - gps.host_started_ns
    self._decision["gps_source_age_upper_ns"] = (
      gps.age_upper_ns + now_ns - gps.host_finished_ns
      if type(gps.host_finished_ns) is int and type(gps.age_upper_ns) is int and now_ns >= gps.host_finished_ns
      else "unknown")
    if not gps.fresh_at(now_ns) or not 0 < gps.horizontal_accuracy_m <= PHONE_MAP_MAX_ACCURACY_M:
      raise RouteRejected("phone_gps_stale_or_inaccurate")
    if self._session != sample.transport_session:
      if sample.host_start_ns <= self._last_observation_ns:
        raise RouteRejected("phone_retired_transport_observation")
      self.withdraw(now_ns)
      self._session = sample.transport_session
      self._lease = PhoneRouteLease(self._session, max_age_s=2.0)
      self._last_sequence = 0
      self._last_gps = None
    if type(sample.sequence) is not int or sample.sequence < self._last_sequence:
      raise RouteRejected("phone_sequence_rollback")
    if (sample.sequence == self._last_sequence and sample.host_start_ns != self._last_observation_ns
        or sample.sequence > self._last_sequence and sample.host_start_ns <= self._last_observation_ns):
      raise RouteRejected("phone_observation_not_advancing")
    selected = sample.selected
    epoch_hash = hashlib.sha256(sample.epoch.encode()).hexdigest()
    positions = []
    windows = []
    for observation in (sample.native_before, sample.native_after):
      if (observation.get("status") != "observed" or observation.get("sample_consistent") is not True
          or observation.get("gps_navigation_observed") is not True or observation.get("profile") != NATIVE_PROFILE
          or type(observation.get("navi_type_raw")) is not int or observation["navi_type_raw"] != 0
          or type(observation.get("navi_state_raw")) is not int or observation["navi_state_raw"] not in (1, 3)
          or observation.get("process_epoch_hash") != epoch_hash
          or observation.get("path_identity_hash") != selected.identity_hash):
        raise RouteRejected("phone_active_identity_mismatch")
      position = observation.get("_position", {})
      if (position.get("status") != "observed" or type(position.get("is_on_guide_road_raw")) is not int
          or position["is_on_guide_road_raw"] != 1
          or position.get("path_identity_hash") != selected.identity_hash):
        raise RouteRejected("phone_source_anchor_unavailable")
      if any(type(position.get(name)) is not int or position[name] < 0
             for name in ("segment_index", "link_index", "point_index")):
        raise RouteRejected("phone_source_anchor_index_invalid")
      started = observation.get("host_observation_started_monotonic_ns")
      finished = observation.get("host_observation_finished_monotonic_ns")
      if (observation.get("host_observation_clock_domain") != "host_monotonic" or type(started) is not int
          or type(finished) is not int or not 0 < started <= finished <= now_ns):
        raise RouteRejected("phone_observation_window_invalid")
      windows.append((started, finished))
      positions.append(position)
    if not (sample.host_start_ns == windows[0][0] and windows[0][1] <= gps.host_started_ns
            <= gps.host_finished_ns <= windows[1][0]):
      raise RouteRejected("phone_gps_outside_observation_bracket")
    before, position = positions
    indices = tuple(position[name] for name in ("segment_index", "link_index", "point_index"))
    if tuple(before[name] for name in ("segment_index", "link_index", "point_index")) != indices:
      raise RouteRejected("phone_anchor_changed_during_read")
    same_gps_token = False
    if self._last_gps is not None:
      boot, elapsed, token, coordinates = self._last_gps
      same_gps_token = (
        gps.phone_boot_id == boot
        and gps.elapsed_realtime_ns == elapsed)
      self._decision["same_gps_token"] = same_gps_token
      if gps.phone_boot_id != boot or gps.elapsed_realtime_ns < elapsed:
        raise RouteRejected("phone_gps_epoch_or_time_rollback")
      if gps.elapsed_realtime_ns == elapsed and gps.source_token != token:
        raise RouteRejected("phone_gps_token_conflict")
      if gps.elapsed_realtime_ns == elapsed:
        if gps.coordinates != coordinates:
          raise RouteRejected("phone_gps_source_content_conflict")
    # positionMonoTime is the local publication instant consumed by mapsd and
    # NavRuntime. The phone fix's lifetime is owned by gps.fresh_at(now_ns) above:
    # it advances with the original source age, so polling the same token cannot
    # renew the phone GPS lease. Do not compare a source-derived timestamp against
    # the host monotonic clock a second time.
    position_ns = now_ns
    ego = wgs84_to_gcj02(*gps.coordinates)
    pose_errors = []
    for item in positions:
      error = _distance(ego, item["native_st_pos_deg"])
      if error > PHONE_MAP_MAX_POSE_ERROR_M:
        raise RouteRejected("phone_pose_space_mismatch")
      pose_errors.append(error)
    key = (sample.transport_session, selected.source_session_id, selected.cache_sha256, selected.identity_hash)
    if key != self._key:
      geometry_kind = getattr(selected, "geometry_kind", "full_route")
      source_geometry_sha256 = getattr(selected, "source_geometry_sha256", None)
      if geometry_kind == _NATIVE_GEOMETRY_KIND:
        if (source_geometry_sha256 != selected.cache_sha256
            or getattr(selected, "complete_current_path_window_verified", True) is not True
            or getattr(selected, "complete_route_verified", True) is not False):
          raise RouteRejected("phone_native_geometry_binding_invalid")
        self._index = AmapRouteAnchorIndex.from_native(selected.route.points_native, selected.route.segments)
        self._identity = RouteIdentity("amapauto-root-fixed-profile", selected.source_session_id,
                                       selected.identity_hash, 0, selected.cache_sha256)
        self._source_object = selected.source_path
        points = self._index.coordinates
        provenance = SourceProvenance.from_native_window(
          identity=self._identity, source_geometry_sha256=source_geometry_sha256,
          source_object_id=self._source_object, decoder_id=_NATIVE_GEOMETRY_DECODER,
          decoded_points=points, expected_point_count=len(selected.route.points_native))
        payload = {"provider": self._identity.provider, "source_session_id": self._identity.source_session_id,
                   "route_id": self._identity.route_id, "revision": 0, "cache_sha256": selected.cache_sha256,
                   "source_geometry_sha256": source_geometry_sha256,
                   "coordinate_system": "AMAP_NATIVE_MAP", "coordinate_order": "lon_lat",
                   "geometry_kind": _NATIVE_GEOMETRY_KIND,
                   "complete": True, "point_count": len(points), "points": points}
        evidence = None
      else:
        decoded = decode_cache(sample.cache_bytes, profile=SUPPORTED_PROFILE)
        if decoded.sha256 != selected.cache_sha256:
          raise RouteRejected("phone_cache_hash_mismatch")
        matches = [route for route in decoded.routes if _path_hash(route.path_id, sample.epoch) == selected.identity_hash]
        if len(matches) != 1 or matches[0] != selected.route:
          raise RouteRejected("phone_decoded_selection_mismatch")
        self._index = AmapRouteAnchorIndex(matches[0])
        self._identity = RouteIdentity("amapauto-root-fixed-profile", selected.source_session_id,
                                       selected.identity_hash, 0, selected.cache_sha256)
        self._source_object = selected.source_path
        points = self._index.coordinates
        evidence = build_native_length_evidence(matches[0], source_cache_sha256=selected.cache_sha256,
                                                profile=SUPPORTED_PROFILE)
        provenance = SourceProvenance.from_decoded(identity=self._identity, source_bytes=sample.cache_bytes,
          source_object_id=self._source_object, decoder_id=SUPPORTED_PROFILE + "-cache-v1",
          coordinate_system="AMAP_NATIVE_MAP", decoded_points=points,
          expected_point_count=len(matches[0].points_native), complete=True,
          native_length_evidence=evidence)
        payload = {"provider": self._identity.provider, "source_session_id": self._identity.source_session_id,
                   "route_id": self._identity.route_id, "revision": 0, "cache_sha256": selected.cache_sha256,
                   "coordinate_system": "AMAP_NATIVE_MAP", "coordinate_order": "lon_lat",
                   "geometry_kind": "full_route", "complete": True, "point_count": len(points),
                   "points": points}
    active = ActiveRouteBinding(self._identity, sample.transport_session, self._source_object, sample.sequence,
                                 sample.host_start_ns / 1e9, True)
    # Capture the exact values the lease is about to compare, from the same binding.
    self._decision.update(sample_observed_mono_s=active.observed_mono_time,
                          sample_sequence=active.observed_sequence,
                          lease_last_observed_mono_s=self._lease._last_observed)
    if sample.sequence > self._last_sequence:
      accepted = self._lease.observe(active, now=now_ns / 1e9)
      self._decision["after_observe_withdrawn_at_mono_s"] = self._lease._withdrawn_at
      if not accepted:
        raise RouteRejected(self._lease.last_reason)
      self._last_sequence = sample.sequence
    if key != self._key:
      if not self._lease.offer(payload, provenance, now=now_ns / 1e9,
                               native_length_evidence=evidence):
        raise RouteRejected(self._lease.last_reason)
      self._key = key
    for item in positions:
      self._index.project(*indices, item["matched_st_pos_deg"], max_lateral_m=1.0)
    projection = self._index.project(*indices, ego, max_lateral_m=PHONE_MAP_MAX_PROJECTION_ERROR_M)
    snapshot = self._lease.snapshot(now=now_ns / 1e9)
    if snapshot is None:
      raise RouteRejected(self._lease.last_reason)
    self._last_gps = (gps.phone_boot_id, gps.elapsed_realtime_ns, gps.source_token, gps.coordinates)
    self._last_observation_ns = sample.host_start_ns
    self._decision["same_gps_token"] = same_gps_token
    self._decision["gps_position_age_upper_ns"] = now_ns - position_ns
    quality = classify_phone_position_quality(gps.horizontal_accuracy_m, max(pose_errors))
    self.reason = quality
    try:
      instruction = _verified_root_instruction(sample, selected, positions)
      # Direct verification may pass with a fresh replay whose native snapshot moved.
      # That would turn a held old action into a new action without a new broadcast;
      # use the explicit replay branch instead, or fail closed.
      _require_new_broadcast(getattr(sample, "broadcast", None))
      self._instruction_reason = "accepted"
      record = _broadcast_record(getattr(sample, "broadcast", None))
      navi = sample.native_after.get("_navi_info") if hasattr(sample.native_after, "get") else {}
      self._last_verified_instruction_key = (
        selected.identity_hash,
        record.get("sequence") if hasattr(record, "get") else None,
        navi.get("route_remain_distance_raw") if hasattr(navi, "get") else None,
      )
    except RouteRejected as error:
      try:
        record = _broadcast_record(getattr(sample, "broadcast", None))
        key = (selected.identity_hash,
               record.get("sequence") if hasattr(record, "get") else None,
               sample.native_after.get("_navi_info", {}).get("route_remain_distance_raw")
               if hasattr(sample.native_after, "get") else None)
        if key != self._last_verified_instruction_key:
          raise RouteRejected("phone_instruction_source_changed")
        instruction = _fresh_replayed_root_instruction(
          sample, selected, positions, getattr(self, "_publication", None).instruction
          if getattr(self, "_publication", None) is not None else None)
        self._instruction_reason = "accepted_fresh_replay_hold"
      except RouteRejected:
        instruction = None
        self._instruction_reason = error.reason
    publication = PhoneRoutePublication(snapshot, active, {
      "version": 1, "positionMonoTime": position_ns, "gpsSourceToken": gps.source_token,
      "coordinateSystem": "AMAP_NATIVE_MAP", "positionTransform": "wgs84_to_gcj02_approx_v1_crosschecked",
      "longitude": ego[0], "latitude": ego[1], "horizontalAccuracyM": gps.horizontal_accuracy_m,
      "globalSegment": projection.global_segment, "matchedLongitude": position["matched_st_pos_deg"][0],
      "matchedLatitude": position["matched_st_pos_deg"][1], "nativePoseErrorM": max(pose_errors),
      "reason": quality, "sourceSegment": indices[0], "sourceLink": indices[1], "sourcePoint": indices[2]},
      instruction)
    self._publication = publication
    self._publication_host_start_ns = sample.host_start_ns
    self._publication_gps = gps
    return publication
