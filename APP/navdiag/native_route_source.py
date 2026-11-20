from __future__ import annotations

import hashlib
import json
import math
import os
import re
import select
import shlex
import signal
import struct
import subprocess
import time
import uuid

if __package__:
  from .amap_adb_memory import process_identity
  from .amap_memory_probe import MAX_READ_BYTES, MAX_READ_CALLS, ProbeError, STATE_NAMES, SUPPORTED_PROFILE
  from .device import PACKAGE, Device, adb_client_argv
else:
  from amap_adb_memory import process_identity
  from amap_memory_probe import MAX_READ_BYTES, MAX_READ_CALLS, ProbeError, STATE_NAMES, SUPPORTED_PROFILE
  from device import PACKAGE, Device, adb_client_argv


PROBE_CLASS = PACKAGE + ".NativeRouteProbe"
MAX_RESULT_BYTES = 16384
MAX_GEOMETRY_BYTES = 8 * 1024 * 1024
_RESPONSE_TIMEOUT_S = 3.0
_GEOMETRY_TIMEOUT_S = 5.0
_START_TIMEOUT_S = 10.0
_STOP_TIMEOUT_S = 2.0
_MAX_WINDOW_NS = 3_000_000_000
_GEOMETRY_MAGIC = b"AMAP_NAV_GEO_V2\x00"
_GEOMETRY_HEADER_SIZE = 112
GEOMETRY_IDENTITY_KIND = "amap_native_window_except_path_count_v1"


MAX_ROUTE_SEGMENTS = 8192
MAX_ROUTE_LINKS = 100000
MAX_ROUTE_POINTS = 100000
COORDINATE_SCALE = 3_600_000
_APK_PATH = re.compile(r"/data/app/(?:~~[A-Za-z0-9_+=-]+/)?" + re.escape(PACKAGE)
                       + r"-[A-Za-z0-9_+=-]+/base\.apk")
_HEX = re.compile(r"[0-9a-f]{64}")
_REASON = re.compile(r"[a-z][a-z0-9_]{0,79}")
_BASE_FIELDS = {"schema_version", "profile", "status", "reason", "path_identity_hash", "navigation_state",
                "navigation_running_observed", "sample_consistent", "gps_navigation_observed",
                "active_route_verified", "control_input_allowed", "source", "implementation", "atomic_snapshot",
                "observation_clock_domain"}
_OBSERVATION_FIELDS = {"navi_type_raw", "navi_state_raw", "main_index", "path_count", "process_epoch_hash",
                       "read_calls", "read_bytes", "observation_started_monotonic_ns",
                       "observation_finished_monotonic_ns", "observation_window_ms"}
_OPTIONAL_FAILURE_FIELDS = {"failure_stage", "read_calls", "read_bytes", "observation_started_monotonic_ns",
                            "observation_finished_monotonic_ns", "observation_window_ms"}
_POSITION_FLAGS = {"coordinate_frame_verified", "source_freshness_verified", "raw_gps_verified",
                   "route_anchor_verified", "route_render_input_allowed", "control_input_allowed"}
_POSITION_BASE = {"status", "reason"} | _POSITION_FLAGS
_POSITION_OBSERVED = {"source", "source_type_raw", "coordinate_frame_candidate", "source_clock",
                      "native_st_pos_deg", "matched_st_pos_deg", "segment_index", "link_index", "point_index",
                      "point_index_source_field", "match_index", "match_count", "is_on_guide_road_raw",
                      "tick_time_raw", "input_tick_time_raw", "path_identity_hash"}
_SELECTION_DIAG_FIELDS = {"schema_version", "source", "changed_fields", "position_changed_fields",
                          "position_serialization_changed", "navi_info_changed_fields",
                          "navi_info_serialization_changed"}
_SELECTION_DIAG_V1_FIELDS = {"schema_version", "source", "changed_fields", "position_changed_fields",
                             "position_serialization_changed"}
_SELECTION_CHANGED_FIELDS = {"chain", "header", "nav_type", "nav_state", "path_count", "main_index", "path_id",
                             "position", "navi_info"}
_NAVI_INFO_FLAGS = {"diagnostic_only", "control_input_allowed", "route_render_input_allowed"}
_NAVI_INFO_BASE = {"schema_version", "status", "reason"} | _NAVI_INFO_FLAGS
_NAVI_INFO_OBSERVED = {"source", "path_identity_hash", "route_remain_time_raw", "route_remain_distance_raw",
                       "navigation_info_type_raw", "item_count", "item_index", "current_segment", "current_link",
                       "current_point", "branch_flag_raw", "branches"}
_NAVI_INFO_BRANCH_FIELDS = {"maneuver_id_raw", "maneuver_segment_raw", "maneuver_link_raw", "maneuver_point_raw",
                            "exit_number_raw", "exit_angle_raw", "obtain_supported_raw", "data_changed_raw",
                            "segment_remain_time_raw", "segment_remain_distance_raw", "next_road_name_segment_raw",
                            "next_road_name_link_raw", "split_raw", "segment_binding"}
_SEGMENT_BINDING_BASE = {"status", "reason"}
_SEGMENT_BINDING_VERIFIED = _SEGMENT_BINDING_BASE | {
  "segment_object_verified", "icon_id_getter_vtable_verified", "icon_getter_vtable_verified"}
_SNAPSHOT_RETRY_FIELDS = {"schema_version", "source", "extra_observations", "recovered", "initial_diagnosis"}
_SNAPSHOT_RETRY_WINDOW_NS = 50_000_000


def _validate_selection_diagnosis(value, *, include_position=False, include_navi_info=False):
  diagnosis = value["_selection_diagnosis"]
  expected_fields = _SELECTION_DIAG_FIELDS if include_navi_info else _SELECTION_DIAG_V1_FIELDS
  if (value.get("status") != "unavailable" or value.get("reason") != "selection_changed_during_read"
      or not isinstance(diagnosis, dict) or diagnosis.keys() != expected_fields
      or type(diagnosis.get("schema_version")) is not int
      or diagnosis["schema_version"] != (2 if include_navi_info else 1)
      or diagnosis.get("source") != "native_observation_pair"
      or type(diagnosis.get("position_serialization_changed")) is not bool):
    raise ProbeError("native_probe_selection_diagnosis_invalid")
  changed = diagnosis.get("changed_fields")
  position = diagnosis.get("position_changed_fields")
  for fields, allowed, minimum in ((changed, _SELECTION_CHANGED_FIELDS, 1),
                                    (position, _POSITION_BASE | _POSITION_OBSERVED, 0)):
    if (not isinstance(fields, list) or not minimum <= len(fields) <= len(allowed)
        or any(type(key) is not str or key not in allowed for key in fields)
        or len(set(fields)) != len(fields)):
      raise ProbeError("native_probe_selection_diagnosis_invalid")
  if (diagnosis["position_serialization_changed"] != ("position" in changed)
      or ("position" in changed and not include_position)
      or ("position" not in changed and position)):
    raise ProbeError("native_probe_selection_diagnosis_invalid")
  if include_navi_info:
    navi_info = diagnosis.get("navi_info_changed_fields")
    if (type(diagnosis.get("navi_info_serialization_changed")) is not bool
        or not isinstance(navi_info, list)
        # The action snapshot can change only in serialization while none of the
        # individually whitelisted fields changes (for example nested boundary
        # bytes). Keep that race visible without inventing a semantic field.
        or not 0 <= len(navi_info) <= len(_NAVI_INFO_BASE | _NAVI_INFO_OBSERVED)
        or any(type(key) is not str or key not in _NAVI_INFO_BASE | _NAVI_INFO_OBSERVED for key in navi_info)
        or len(set(navi_info)) != len(navi_info)
        or diagnosis["navi_info_serialization_changed"] != ("navi_info" in changed)
        or ("navi_info" not in changed and navi_info)):
      raise ProbeError("native_probe_selection_diagnosis_invalid")
  elif "navi_info" in changed:
    raise ProbeError("native_probe_selection_diagnosis_invalid")


def _validate_snapshot_retry(value, *, include_position=False, include_navi_info=False):
  retry = value["_snapshot_retry"]
  position = value.get("_position")
  if (not include_position or not isinstance(retry, dict) or retry.keys() != _SNAPSHOT_RETRY_FIELDS
      or type(retry.get("schema_version")) is not int or retry["schema_version"] != 1
      or retry.get("source") != "native_observation_pair"
      or type(retry.get("extra_observations")) is not int or retry["extra_observations"] != 1
      or type(retry.get("recovered")) is not bool
      or retry["recovered"] != (value.get("status") == "observed")
      or (retry["recovered"] and (not isinstance(position, dict) or position.get("status") != "observed"))):
    raise ProbeError("native_probe_snapshot_retry_invalid")
  initial = retry["initial_diagnosis"]
  try:
    _validate_selection_diagnosis({"status": "unavailable", "reason": "selection_changed_during_read",
                                  "_selection_diagnosis": initial}, include_position=True,
                                 include_navi_info=include_navi_info)
  except ProbeError:
    raise ProbeError("native_probe_snapshot_retry_invalid") from None
  if (initial["changed_fields"] != ["position"]
      or set(initial["position_changed_fields"]) not in ({"tick_time_raw"}, {"tick_time_raw", "native_st_pos_deg"},
                                                       {"tick_time_raw", "input_tick_time_raw"},
                                                       {"native_st_pos_deg", "tick_time_raw", "input_tick_time_raw"},
                                                       {"native_st_pos_deg", "matched_st_pos_deg", "tick_time_raw"},
                                                       {"native_st_pos_deg", "matched_st_pos_deg", "tick_time_raw", "input_tick_time_raw"})
      or (value.get("reason") == "selection_changed_during_read" and "_selection_diagnosis" not in value)):
    raise ProbeError("native_probe_snapshot_retry_invalid")
  if include_navi_info and (initial["navi_info_serialization_changed"]
                            or initial["navi_info_changed_fields"]):
    raise ProbeError("native_probe_snapshot_retry_invalid")


_PERFORMANCE_NS = {"maps_read_ns", "maps_parse_ns", "mapping_verification_ns", "identity_read_ns",
                   "memory_read_ns", "snapshot_maps_parse_ns", "snapshot_ns", "live_total_ns"}
_PERFORMANCE_FIELDS = {"clock_domain", "identity_read_calls", "memory_read_calls"} | _PERFORMANCE_NS


def geometry_identity_sha256(payload: bytes) -> str:
  if not isinstance(payload, bytes) or len(payload) < _GEOMETRY_HEADER_SIZE:
    raise ProbeError("native_probe_geometry_size_invalid")
  return hashlib.sha256(b"AMAP_NATIVE_GEOMETRY_ID_V1\x00" + payload[:28] + payload[32:]).hexdigest()


def _unique_object(pairs):
  result = {}
  for key, value in pairs:
    if key in result:
      raise ProbeError("native_probe_duplicate_key")
    result[key] = value
  return result


def _json_constant(_):
  raise ProbeError("native_probe_nonfinite_json")


def _integer(value, minimum=0, maximum=(1 << 63) - 1):
  return type(value) is int and minimum <= value <= maximum


def _finite_number(value):
  try:
    return type(value) in (int, float) and math.isfinite(value)
  except OverflowError:
    return False


def next_road_field_rejections(branches):
  """Return bounded raw diagnostics; unknown negative indices never certify actions."""
  if not isinstance(branches, (list, tuple)) or len(branches) != 2:
    raise ProbeError("native_probe_navi_info_invalid")
  result = []
  for index, branch in enumerate(branches):
    for field, reason in (("next_road_name_segment_raw", "navi_info_next_segment_invalid"),
                          ("next_road_name_link_raw", "navi_info_next_link_invalid")):
      value = branch.get(field) if hasattr(branch, "get") else None
      if not _integer(value, 0, 0xffffffff):
        raise ProbeError("native_probe_navi_info_invalid")
      if 0x7fffffff < value < 0xffffffff:
        result.append({"branch_index": index, "field": field, "reason": reason,
                       "raw_u32": value, "raw_i32": value - 0x100000000})
  return result


def _start_script(apk_path: str, token: str) -> str:
  return "".join([
    "navdiag_stat=$(cat /proc/$$/stat) || exit 71; ",
    "navdiag_fields=${navdiag_stat##*) }; set -- $navdiag_fields; ",
    '[ "$#" -ge 20 ] || exit 71; shift 19; ',
    'case "$1" in \'\'|*[!0-9]*) exit 71;; esac; ',
    'printf \'NAVDIAG_ROUTE_V1 %s %s\\n\' "$$" "$1"; ',
    'IFS= read -r navdiag_ack || exit 72; [ "$navdiag_ack" = START ] || exit 72; ',
    "export NAVDIAG_PROBE_SESSION=" + shlex.quote(token) + "; ",
    "export CLASSPATH=" + shlex.quote(apk_path) + "; ",
    "exec /system/bin/app_process /system/bin " + PROBE_CLASS,
  ])


def _cleanup_script(pid: str, start_time: str, token: str) -> str:
  command = "/system/bin/app_process\n/system/bin\n" + PROBE_CLASS
  return "".join([
    "navdiag_pid=" + pid + "; ",
    "navdiag_owned() { ",
    '[ -d "/proc/$navdiag_pid" ] || return 3; ',
    'navdiag_stat=$(cat "/proc/$navdiag_pid/stat") || { [ ! -d "/proc/$navdiag_pid" ] && return 3; return 76; }; ',
    "navdiag_fields=${navdiag_stat##*) }; set -- $navdiag_fields; ",
    '[ "$#" -ge 20 ] || return 76; [ "$1" != Z ] || return 3; shift 19; ',
    '[ "$1" = ' + shlex.quote(start_time) + " ] || return 3; ",
    "tr '\\000' '\\n' < /proc/" + pid + "/environ | grep -F -x -q "
    + shlex.quote("NAVDIAG_PROBE_SESSION=" + token) + " || return 76; ",
    "navdiag_cmdline=$(tr '\\000' '\\n' < /proc/" + pid + "/cmdline) || return 76; ",
    'case "$navdiag_cmdline" in ' + shlex.quote(command) + "|" + shlex.quote(PROBE_CLASS)
    + ") return 0;; *) return 76;; esac; }; ",
    "navdiag_check() { navdiag_owned; navdiag_rc=$?; "
    + '[ "$navdiag_rc" = 3 ] && exit 0; [ "$navdiag_rc" = 0 ] || exit 76; }; ',
    "navdiag_check; ",
    'kill -TERM "$navdiag_pid" 2>/dev/null || { navdiag_check; exit 76; }; ',
    "for navdiag_attempt in 1 2 3 4 5 6 7 8 9 10; do navdiag_check; sleep 0.1; done; ",
    "navdiag_check; ",
    'kill -KILL "$navdiag_pid" 2>/dev/null || { navdiag_check; exit 76; }; ',
    "for navdiag_attempt in 1 2 3 4 5 6 7 8 9 10; do navdiag_check; sleep 0.1; done; ",
    "navdiag_check; exit 76",
  ])


class NativeRouteProbeSource:
  def __init__(self, device: Device):
    self.device = device
    self.epoch = process_identity(device)
    if not isinstance(self.epoch, str) or re.fullmatch(r"[1-9][0-9]{0,9}:[0-9]{1,24}", self.epoch) is None:
      raise ProbeError("provider_identity_unavailable")
    self.process = None
    self._remote_identity = None
    self._token = uuid.uuid4().hex
    self._closed = False
    self._last_phone_finished = None
    self.cleanup_verified = False

  def __enter__(self):
    if self._closed or self.process is not None:
      raise ProbeError("native_probe_source_already_used")
    try:
      result = self.device.shell("-T", "pm path " + PACKAGE + " | head -c 2049", timeout=5)
      raw = result.stdout
      if (result.returncode != 0 or not isinstance(raw, str) or len(raw.encode("utf-8")) > 2048 or not raw.endswith("\n")
          or raw.count("\n") != 1 or not raw.startswith("package:")):
        raise ProbeError("native_probe_apk_path_invalid")
      apk_path = raw[len("package:"):-1]
      if _APK_PATH.fullmatch(apk_path) is None:
        raise ProbeError("native_probe_apk_path_invalid")
      script = _start_script(apk_path, self._token)
      self.process = subprocess.Popen(
        adb_client_argv(self.device, "-s", self.device.serial, "shell", "-T",
                        "su -c " + shlex.quote(script)), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, start_new_session=True)
      header = self._line(_START_TIMEOUT_S, 128)
      match = re.fullmatch(rb"NAVDIAG_ROUTE_V1 ([1-9][0-9]{0,9}) ([0-9]{1,24})", header)
      if match is None:
        raise ProbeError("native_probe_handshake_invalid")
      self._remote_identity = tuple(value.decode("ascii") for value in match.groups())
      self._write(b"START\n")
      return self
    except BaseException:
      self.close()
      raise

  def __exit__(self, *_):
    self.close()

  def _write(self, value: bytes):
    try:
      self.process.stdin.write(value)
      self.process.stdin.flush()
    except (OSError, ValueError, AttributeError):
      raise ProbeError("native_probe_channel_closed") from None

  def _line(self, timeout: float, maximum: int) -> bytes:
    data = bytearray()
    deadline = time.monotonic() + timeout
    while True:
      remaining = deadline - time.monotonic()
      if remaining <= 0 or not select.select([self.process.stdout], [], [], remaining)[0]:
        raise ProbeError("native_probe_channel_timeout")
      part = os.read(self.process.stdout.fileno(), maximum + 1 - len(data))
      if not part:
        raise ProbeError("native_probe_channel_closed")
      data.extend(part)
      if len(data) > maximum:
        raise ProbeError("native_probe_result_too_large")
      if b"\n" in data:
        line, suffix = bytes(data).split(b"\n", 1)
        if suffix:
          raise ProbeError("native_probe_unsolicited_output")
        return line

  def _geometry_header(self, timeout: float, maximum: int) -> bytes:
    data = bytearray()
    deadline = time.monotonic() + timeout
    while True:
      remaining = deadline - time.monotonic()
      if remaining <= 0 or not select.select([self.process.stdout], [], [], remaining)[0]:
        raise ProbeError("native_probe_channel_timeout")
      part = os.read(self.process.stdout.fileno(), 1)
      if not part:
        raise ProbeError("native_probe_channel_closed")
      if part == b"\n":
        return bytes(data)
      data.extend(part)
      if len(data) > maximum:
        raise ProbeError("native_probe_result_too_large")

  def _no_pending(self):
    if select.select([self.process.stdout], [], [], 0)[0]:
      part = os.read(self.process.stdout.fileno(), 1)
      raise ProbeError("native_probe_unsolicited_output" if part else "native_probe_channel_closed")

  def _read_exact(self, length: int, timeout: float) -> bytes:
    if type(length) is not int or not 0 <= length <= MAX_GEOMETRY_BYTES:
      raise ProbeError("native_probe_geometry_size_invalid")
    data = bytearray()
    deadline = time.monotonic() + timeout
    while len(data) < length:
      remaining = deadline - time.monotonic()
      if remaining <= 0 or not select.select([self.process.stdout], [], [], remaining)[0]:
        raise ProbeError("native_probe_geometry_timeout")
      part = os.read(self.process.stdout.fileno(), min(length - len(data), 65536))
      if not part:
        raise ProbeError("native_probe_channel_closed")
      data.extend(part)
    return bytes(data)

  def _validate_position(self, position: dict, parent: dict) -> None:
    if (not isinstance(position, dict) or not _POSITION_BASE <= position.keys()
        or any(position.get(key) is not False for key in _POSITION_FLAGS)
        or not isinstance(position.get("reason"), str) or _REASON.fullmatch(position["reason"]) is None):
      raise ProbeError("native_probe_position_invalid")
    if position.get("status") == "unavailable":
      if position.keys() != _POSITION_BASE:
        raise ProbeError("native_probe_position_invalid")
      return
    if (position.get("status") != "observed" or position.keys() != _POSITION_BASE | _POSITION_OBSERVED
        or parent.get("status") != "observed" or parent.get("gps_navigation_observed") is not True
        or position.get("reason") != "unverified_position_semantics"
        or position.get("path_identity_hash") != parent.get("path_identity_hash")
        or position.get("source") != "native_loc_info" or position.get("source_clock") != "unknown"
        or position.get("coordinate_frame_candidate") != "amap_native_map"
        or position.get("point_index_source_field") != "postCur"
        or not _integer(position.get("source_type_raw"), 0, 0xffffffff)
        or type(position.get("is_on_guide_road_raw")) is not int or position["is_on_guide_road_raw"] != 1
        or not _integer(position.get("match_count"), 1, 3)
        or not _integer(position.get("match_index"), 0, position["match_count"] - 1)
        or any(not _integer(position.get(key), 0, 0x7fffffff) for key in ("segment_index", "link_index", "point_index"))
        or any(not _integer(position.get(key), 1, 0xffffffffffffffff) for key in ("tick_time_raw", "input_tick_time_raw"))):
      raise ProbeError("native_probe_position_invalid")
    for key in ("native_st_pos_deg", "matched_st_pos_deg"):
      point = position.get(key)
      if (not isinstance(point, list) or len(point) != 2
          or any(not _finite_number(item) for item in point)
          or not -180 <= point[0] <= 180 or not -90 <= point[1] <= 90 or point == [0, 0]):
        raise ProbeError("native_probe_position_invalid")

  def _validate_navi_info(self, navi_info: dict, parent: dict) -> None:
    if (not isinstance(navi_info, dict) or not _NAVI_INFO_BASE <= navi_info.keys()
        or navi_info.get("schema_version") != 2 or navi_info.get("diagnostic_only") is not True
        or any(navi_info.get(key) is not False
               for key in ("control_input_allowed", "route_render_input_allowed"))
        or not isinstance(navi_info.get("reason"), str) or _REASON.fullmatch(navi_info["reason"]) is None):
      raise ProbeError("native_probe_navi_info_invalid")
    if navi_info.get("status") == "unavailable":
      if navi_info.keys() != _NAVI_INFO_BASE:
        raise ProbeError("native_probe_navi_info_invalid")
      return
    if (navi_info.get("status") != "observed" or navi_info.keys() != _NAVI_INFO_BASE | _NAVI_INFO_OBSERVED
        or parent.get("status") != "observed" or parent.get("gps_navigation_observed") is not True
        or navi_info.get("source") != "native_navi_info"
        or navi_info.get("path_identity_hash") != parent.get("path_identity_hash")
        or not _integer(navi_info.get("item_count"), 1, 32)
        or not _integer(navi_info.get("item_index"), 0, navi_info["item_count"] - 1)
        or not _integer(navi_info.get("route_remain_time_raw"), 0, 0x7fffffff)
        or not _integer(navi_info.get("route_remain_distance_raw"), 0, 0x7fffffff)
        or not _integer(navi_info.get("navigation_info_type_raw"), 0, 0xffffffff)
        or any(not _integer(navi_info.get(key), 0, 0x7fffffff)
               for key in ("current_segment", "current_link", "current_point"))
        or type(navi_info.get("branch_flag_raw")) is not int or navi_info["branch_flag_raw"] not in (0, 1)):
      raise ProbeError("native_probe_navi_info_invalid")
    branches = navi_info.get("branches")
    if not isinstance(branches, list) or len(branches) != 2:
      raise ProbeError("native_probe_navi_info_invalid")
    unsigned_fields = _NAVI_INFO_BRANCH_FIELDS - {
      "segment_binding",
      "obtain_supported_raw", "data_changed_raw"}
    for branch in branches:
      if (not isinstance(branch, dict) or branch.keys() != _NAVI_INFO_BRANCH_FIELDS
          or any(not _integer(branch.get(key), 0, 0xffffffff) for key in unsigned_fields)
          or type(branch.get("obtain_supported_raw")) is not int or branch["obtain_supported_raw"] not in (0, 1)
          or type(branch.get("data_changed_raw")) is not int or branch["data_changed_raw"] not in (0, 1)):
        raise ProbeError("native_probe_navi_info_invalid")
      binding = branch.get("segment_binding")
      if not isinstance(binding, dict) or not _SEGMENT_BINDING_BASE <= binding.keys():
        raise ProbeError("native_probe_navi_info_invalid")
      if (not isinstance(binding.get("reason"), str)
          or _REASON.fullmatch(binding["reason"]) is None):
        raise ProbeError("native_probe_navi_info_invalid")
      if binding.get("status") == "unavailable":
        if binding.keys() != _SEGMENT_BINDING_BASE:
          raise ProbeError("native_probe_navi_info_invalid")
      elif binding.get("status") == "verified":
        if (binding.keys() != _SEGMENT_BINDING_VERIFIED
            or binding.get("reason") != "segment_object_verified"
            or any(binding.get(key) is not True for key in (
              "segment_object_verified", "icon_id_getter_vtable_verified",
              "icon_getter_vtable_verified"))):
          raise ProbeError("native_probe_navi_info_invalid")
      else:
        raise ProbeError("native_probe_navi_info_invalid")

    rejected = next_road_field_rejections(branches)
    expected_reason = rejected[0]["reason"] if rejected else "unverified_navi_info_semantics"
    if navi_info["reason"] != expected_reason:
      raise ProbeError("native_probe_navi_info_invalid")

  def _validate(self, value: dict, *, include_position=False, include_navi_info=False) -> None:
    if not isinstance(value, dict) or not _BASE_FIELDS <= value.keys():
      raise ProbeError("native_probe_result_invalid")
    observed = value.get("status") == "observed"
    fields = _BASE_FIELDS | (_OBSERVATION_FIELDS if observed else _OPTIONAL_FAILURE_FIELDS)
    if "_performance" in value:
      fields = fields | {"_performance"}
    if "_selection_diagnosis" in value:
      _validate_selection_diagnosis(value, include_position=include_position,
                                    include_navi_info=include_navi_info)
      fields = fields | {"_selection_diagnosis"}
    if "_snapshot_retry" in value:
      _validate_snapshot_retry(value, include_position=include_position, include_navi_info=include_navi_info)
      fields = fields | {"_snapshot_retry"}
    if include_position:
      fields = fields | {"_position"}
      if "_position" not in value:
        raise ProbeError("native_probe_position_missing")
    if include_navi_info:
      fields = fields | {"_navi_info"}
      if "_navi_info" not in value:
        raise ProbeError("native_probe_navi_info_missing")
    elif "_navi_info" in value:
      raise ProbeError("native_probe_navi_info_unexpected")
    if (value.keys() - fields or (observed and not _OBSERVATION_FIELDS <= value.keys())
        or type(value.get("schema_version")) is not int or value["schema_version"] != 1
        or value.get("profile") != SUPPORTED_PROFILE or value.get("source") != "bounded_native_memory"
        or value.get("implementation") != "java_read_only_no_injection"
        or value.get("observation_clock_domain") != "phone_elapsed_realtime"
        or value.get("status") not in ("observed", "unavailable")
        or not isinstance(value.get("reason"), str) or _REASON.fullmatch(value["reason"]) is None
        or any(value.get(key) is not False for key in ("active_route_verified", "control_input_allowed", "atomic_snapshot"))
        or any(type(value.get(key)) is not bool for key in
               ("sample_consistent", "navigation_running_observed", "gps_navigation_observed"))):
      raise ProbeError("native_probe_result_invalid")
    if "failure_stage" in value and (not isinstance(value["failure_stage"], str)
                                     or _REASON.fullmatch(value["failure_stage"]) is None):
      raise ProbeError("native_probe_result_invalid")
    if observed:
      count, index, kind, state = (value.get(key) for key in
                                   ("path_count", "main_index", "navi_type_raw", "navi_state_raw"))
      path_hash = value["path_identity_hash"]
      if (value["reason"] != "consistent_read_only_observation" or value["sample_consistent"] is not True
          or not _integer(count, 0, 32) or not _integer(index, 0, 31)
          or not _integer(kind, 0, 0xffffffff) or not _integer(state, 0, 0xffffffff)
          or (count == 0 and (index != 0 or path_hash is not None))
          or (count > 0 and (index >= count or not isinstance(path_hash, str) or _HEX.fullmatch(path_hash) is None))
          or value["navigation_state"] != STATE_NAMES.get(state, "unknown")
          or value["navigation_running_observed"] != (count > 0 and state in (1, 3))
          or value["gps_navigation_observed"] != (count > 0 and state in (1, 3) and kind == 0)):
        raise ProbeError("native_probe_result_invalid")
      if value["process_epoch_hash"] != hashlib.sha256(self.epoch.encode()).hexdigest():
        raise ProbeError("process_epoch_changed")
    elif (value["path_identity_hash"] is not None or value["navigation_state"] != "unknown"
          or any(value[key] for key in ("sample_consistent", "navigation_running_observed", "gps_navigation_observed"))):
      raise ProbeError("native_probe_result_invalid")
    if include_position:
      self._validate_position(value["_position"], value)
    if include_navi_info:
      self._validate_navi_info(value["_navi_info"], value)
    if "_performance" in value:
      performance = value["_performance"]
      if (not isinstance(performance, dict) or performance.keys() != _PERFORMANCE_FIELDS
          or performance.get("clock_domain") != "phone_elapsed_realtime"
          or any(not _integer(performance.get(key), 0, 60_000_000_000) for key in _PERFORMANCE_NS)
          or not _integer(performance.get("identity_read_calls"), 0, 4 if "_snapshot_retry" in value else 3)
          or not _integer(performance.get("memory_read_calls"), 0, MAX_READ_CALLS)
          or ("read_calls" in value and performance["memory_read_calls"] != value["read_calls"])):
        raise ProbeError("native_probe_performance_invalid")
    diagnostics = {"read_calls", "read_bytes", "observation_started_monotonic_ns",
                   "observation_finished_monotonic_ns", "observation_window_ms"}
    if diagnostics & value.keys():
      if not diagnostics <= value.keys():
        raise ProbeError("native_probe_result_invalid")
      calls, size = value["read_calls"], value["read_bytes"]
      started, finished = value["observation_started_monotonic_ns"], value["observation_finished_monotonic_ns"]
      window = value["observation_window_ms"]
      if (not _integer(calls, 0, MAX_READ_CALLS) or not _integer(size, 0, MAX_READ_BYTES)
          or not calls <= size <= calls * 64 or (observed and calls < (29 if value["path_count"] else 17))
          or not _integer(started, 1) or not _integer(finished, started)
          or finished - started > _MAX_WINDOW_NS or not _finite_number(window)
          or abs(window - (finished - started) / 1_000_000) > 0.00051):
        raise ProbeError("native_probe_result_invalid")
      if observed and "_snapshot_retry" in value and finished - started >= _SNAPSHOT_RETRY_WINDOW_NS:
        raise ProbeError("native_probe_snapshot_retry_invalid")
      if self._last_phone_finished is not None and (started < self._last_phone_finished or finished <= self._last_phone_finished):
        raise ProbeError("native_probe_phone_time_regressed")
      self._last_phone_finished = finished

  def observe(self) -> dict:
    return self._observe(b"OBSERVE\n", include_position=False)

  def observe_position(self) -> dict:
    return self._observe(b"OBSERVE_POSITION\n", include_position=True)

  def observe_navi_info(self) -> dict:
    return self._observe(b"OBSERVE_NAVI_INFO\n", include_navi_info=True)

  def observe_position_navi_info(self) -> dict:
    return self._observe(b"OBSERVE_POSITION_NAVI_INFO\n", include_position=True, include_navi_info=True)

  def observe_geometry(self) -> dict:
    if self.process is None or self._closed:
      raise ProbeError("native_probe_source_not_started")
    try:
      started = time.monotonic_ns()
      self._no_pending()
      self._write(b"OBSERVE_GEOMETRY\n")
      raw_header = self._geometry_header(_GEOMETRY_TIMEOUT_S, 128)
      if raw_header.startswith(b"NAVDIAG_GEOMETRY_V2 ERROR "):
        try:
          reason = raw_header[len(b"NAVDIAG_GEOMETRY_V2 ERROR "):].decode("ascii")
        except UnicodeError:
          raise ProbeError("native_probe_geometry_error_invalid") from None
        if _REASON.fullmatch(reason) is None:
          raise ProbeError("native_probe_geometry_error_invalid")
        self._no_pending()
        if reason == "unsupported_command":
          raise ProbeError("native_probe_geometry_unsupported")
        raise ProbeError(reason)
      match = re.fullmatch(rb"NAVDIAG_GEOMETRY_V2 ([0-9]{1,8}) ([0-9a-f]{64})", raw_header)
      if match is None:
        raise ProbeError("native_probe_geometry_header_invalid")
      length = int(match.group(1))
      if not _GEOMETRY_HEADER_SIZE <= length <= MAX_GEOMETRY_BYTES:
        raise ProbeError("native_probe_geometry_size_invalid")
      payload = self._read_exact(length, _GEOMETRY_TIMEOUT_S)
      terminator = self._read_exact(1, _RESPONSE_TIMEOUT_S)
      if terminator != b"\n":
        raise ProbeError("native_probe_geometry_terminator_invalid")
      self._no_pending()
      if hashlib.sha256(payload).hexdigest() != match.group(2).decode("ascii"):
        raise ProbeError("native_probe_geometry_hash_mismatch")
      value = self._parse_geometry(payload)
      finished = time.monotonic_ns()
      if not 0 < started <= finished or finished - started > _MAX_WINDOW_NS:
        raise ProbeError("native_probe_host_window_invalid")
      value.update(host_observation_started_monotonic_ns=started,
                   host_observation_finished_monotonic_ns=finished,
                   host_observation_clock_domain="host_monotonic")
      return value
    except ProbeError as error:
      if str(error) != "native_probe_geometry_unsupported":
        self.close()
      raise
    except BaseException:
      self.close()
      raise

  def _parse_geometry(self, payload: bytes) -> dict:
    if len(payload) < _GEOMETRY_HEADER_SIZE or payload[:16] != _GEOMETRY_MAGIC:
      raise ProbeError("native_probe_geometry_magic_invalid")
    try:
      version, navi_type, navi_state, path_count, main_index, segment_count, link_count, point_count = (
        struct.unpack_from("<8I", payload, 16))
    except struct.error:
      raise ProbeError("native_probe_geometry_header_invalid") from None
    if (version != 1 or navi_type != 0 or navi_state not in (1, 3)
        or not 1 <= path_count <= 32 or not 0 <= main_index < path_count
        or not 1 <= segment_count <= MAX_ROUTE_SEGMENTS
        or not 1 <= link_count <= MAX_ROUTE_LINKS or not 2 <= point_count <= MAX_ROUTE_POINTS):
      raise ProbeError("native_probe_geometry_header_invalid")
    expected = _GEOMETRY_HEADER_SIZE + link_count * 12 + point_count * 8
    if len(payload) != expected:
      raise ProbeError("native_probe_geometry_size_invalid")
    path_hash, epoch_hash = payload[48:80], payload[80:112]
    if path_hash == b"\x00" * 32 or epoch_hash != hashlib.sha256(self.epoch.encode()).digest():
      raise ProbeError("native_probe_geometry_identity_invalid")
    spans, segment_order, records = [], [], []
    previous_segment = -1
    previous_end = 0
    for index in range(link_count):
      try:
        segment, start, count = struct.unpack_from("<3I", payload, _GEOMETRY_HEADER_SIZE + index * 12)
      except struct.error:
        raise ProbeError("native_probe_geometry_link_invalid") from None
      if (segment >= segment_count or count < 2 or start + count > point_count
          or segment < previous_segment):
        raise ProbeError("native_probe_geometry_link_invalid")
      if segment != previous_segment:
        if not spans and (segment != 0 or start != 0):
          raise ProbeError("native_probe_geometry_coverage_invalid")
        if spans and start != previous_end - 1:
          raise ProbeError("native_probe_geometry_segment_discontinuity")
        spans.append([])
        segment_order.append(segment)
      elif not records[-1][0] < start < previous_end < start + count:
        raise ProbeError("native_probe_geometry_link_discontinuity")
      spans[-1].append((start, count))
      records.append((start, count, segment))
      previous_segment, previous_end = segment, start + count
    if (not spans or len(spans) != segment_count or spans[0][0][0] != 0
        or segment_order != list(range(segment_count))
        or previous_end != point_count):
      raise ProbeError("native_probe_geometry_coverage_invalid")
    points_offset = _GEOMETRY_HEADER_SIZE + link_count * 12
    points = []
    for index in range(point_count):
      try:
        x, y = struct.unpack_from("<2i", payload, points_offset + index * 8)
      except struct.error:
        raise ProbeError("native_probe_geometry_point_invalid") from None
      if (x == 0 and y == 0 or abs(x) > 180 * COORDINATE_SCALE
          or abs(y) > 90 * COORDINATE_SCALE):
        raise ProbeError("native_probe_geometry_point_invalid")
      points.append((x, y))
    return {
      "schema_version": 1, "status": "observed", "reason": "consistent_read_only_geometry",
      "source": "native_drive_path_geometry", "geometry_kind": "current_native_path_window",
      "source_geometry_sha256": hashlib.sha256(payload).hexdigest(),
      "geometry_identity_kind": GEOMETRY_IDENTITY_KIND,
      "geometry_identity_sha256": geometry_identity_sha256(payload),
      "path_identity_hash": path_hash.hex(), "epoch_sha256": epoch_hash.hex(),
      "navi_type_raw": navi_type, "navi_state_raw": navi_state,
      "path_count": path_count, "main_index": main_index,
      "segment_count": segment_count, "link_count": link_count, "point_count": point_count,
      "segments": tuple(tuple(items) for items in spans), "points_native": tuple(points),
      "coordinate_frame": "amap_native_map", "coordinate_datum_verified": False,
      "complete_current_path_window_verified": True,
      "active_route_verified": False, "control_input_allowed": False,
    }

  def _on_unavailable(self, value):
    self.close()

  def _observe(self, command: bytes, *, include_position: bool = False, include_navi_info: bool = False) -> dict:
    if self.process is None or self._closed:
      raise ProbeError("native_probe_source_not_started")
    try:
      started = time.monotonic_ns()
      self._no_pending()
      self._write(command)
      raw = self._line(_RESPONSE_TIMEOUT_S, MAX_RESULT_BYTES)
      self._no_pending()
      try:
        value = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_json_constant)
      except (ValueError, UnicodeError, RecursionError):
        raise ProbeError("native_probe_json_invalid") from None
      self._validate(value, include_position=include_position, include_navi_info=include_navi_info)
      finished = time.monotonic_ns()
      if not 0 < started <= finished or finished - started > _MAX_WINDOW_NS:
        raise ProbeError("native_probe_host_window_invalid")
      value.update(host_observation_started_monotonic_ns=started,
                   host_observation_finished_monotonic_ns=finished,
                   host_observation_clock_domain="host_monotonic")
      if value["status"] == "unavailable":
        self._on_unavailable(value)
      return value
    except BaseException:
      self.close()
      raise

  def close(self):
    if self._closed:
      return
    self._closed = True
    process, self.process = self.process, None
    if process is None:
      self.cleanup_verified = True
      return
    clean_exit = False
    cleanup_failed = False
    try:
      if process.poll() is None:
        try:
          process.stdin.write(b"QUIT\n")
          process.stdin.flush()
          clean_exit = process.wait(timeout=_STOP_TIMEOUT_S) == 0
        except (OSError, ValueError, subprocess.TimeoutExpired):
          pass
      if not clean_exit and self._remote_identity is not None:
        try:
          pid, start_time = self._remote_identity
          result = self.device.shell("-T", "su -c " + shlex.quote(_cleanup_script(pid, start_time, self._token)),
                                     timeout=5, check=False)
          cleanup_failed = result.returncode != 0
        except (OSError, subprocess.SubprocessError):
          cleanup_failed = True
      if process.poll() is None:
        try:
          os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
          pass
        try:
          process.wait(timeout=_STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
          try:
            os.killpg(process.pid, signal.SIGKILL)
          except ProcessLookupError:
            pass
          process.wait(timeout=_STOP_TIMEOUT_S)
    finally:
      for stream in (process.stdin, process.stdout):
        if stream is not None:
          stream.close()
    self.cleanup_verified = not cleanup_failed
    if cleanup_failed:
      raise ProbeError("native_probe_cleanup_unverified")
