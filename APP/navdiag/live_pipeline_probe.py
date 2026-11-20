#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime
import hashlib
import json
import math
from pathlib import Path
import re
import signal
import sys
import threading
import time

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from APP.navdiag.phone_pipeline_bench import PhonePipelineBench
from APP.navdiag.native_route_source import next_road_field_rejections
from APP.navdiag.phone_route_producer import PhoneRouteProducer
from APP.navdiag.sample_evidence import SampleEvidenceArchive, replay_source_sha256
from openpilot.common.gcj import wgs84_to_gcj02
from openpilot.selfdrive.navigation.amap_phone_route import _distance

MIN_DURATION, MAX_DURATION, DEFAULT_DURATION = 15, 180, 90
SAMPLE_INTERVAL_S = 0.25


class ObservedProducer(PhoneRouteProducer):

  def __init__(self, *args, **kwargs):
    self.observations = {"collector_calls": 0, "collector_ms": [], "recover_calls": 0,
                         "recover_hits": 0, "recover_ms": [], "recover_not_applicable": 0}
    self._observe_lock = threading.Lock()
    from APP.navdiag.live_route_snapshot import collect_selected
    base_collector = kwargs.pop("collector", collect_selected)

    def observed(device, native):
      started = time.monotonic()
      try:
        return base_collector(device, native)
      finally:
        with self._observe_lock:
          self.observations["collector_calls"] += 1
          self.observations["collector_ms"].append(round((time.monotonic() - started) * 1000, 3))

    kwargs["collector"] = observed
    super().__init__(*args, **kwargs)

  def _recover_inactive(self, slot, device, native, identity, generation=None):
    started = time.monotonic()
    result = PhoneRouteProducer._recover_inactive(self, slot, device, native, identity, generation)
    elapsed = round((time.monotonic() - started) * 1000, 3)
    with self._observe_lock:
      self.observations["recover_calls"] += 1
      if result is None:
        self.observations["recover_not_applicable"] += 1
      else:
        self.observations["recover_hits"] += 1
        self.observations["recover_ms"].append(elapsed)
    return result

  def summary(self):
    with self._observe_lock:
      data = {key: (list(value) if isinstance(value, list) else value)
              for key, value in self.observations.items()}
    for key in ("collector_ms", "recover_ms"):
      values = data[key]
      data[key + "_p50"] = round(sorted(values)[len(values) // 2], 3) if values else None
      data[key + "_max"] = max(values) if values else None
      data[key] = len(values)
    return data


def producer_transport_report(producer):
  ownership = getattr(producer, "connected_adb_ownership", None)
  port = getattr(producer, "connected_adb_port", None)
  return {"ownership": ownership if ownership in ("dedicated", "attach") else "unknown",
          "adb_port": port if type(port) is int and 1 <= port <= 65535 else None,
          "owned": ownership == "dedicated"}


def _diagnostic_integer(value, *, minimum=1, maximum=(1 << 63) - 1):
  return value if type(value) is int and minimum <= value <= maximum else "unknown"


def _diagnostic_window(started, finished):
  if (_diagnostic_integer(started) != "unknown" and _diagnostic_integer(finished) != "unknown"
      and started <= finished):
    return {"started": started, "finished": finished}
  return "unknown"


def _diagnostic_mapping(value):
  return value if isinstance(value, Mapping) else {}


def _diagnostic_distance(first, second):
  try:
    return _distance(first, second)
  except (ValueError, TypeError, AttributeError, KeyError, OverflowError):
    return "unknown"


def sample_position_diagnostics(sample):
  """Observe the adapter's metric and original clocks without exposing positions or changing gates."""
  gps = getattr(sample, "gps", None)
  observations = [_diagnostic_mapping(getattr(sample, side, None)) for side in ("native_before", "native_after")]
  positions = [_diagnostic_mapping(observation.get("_position")) for observation in observations]
  coordinates = [position.get("native_st_pos_deg") if position.get("status") == "observed" else None
                 for position in positions]
  matched = [position.get("matched_st_pos_deg") if position.get("status") == "observed" else None
             for position in positions]
  ego = getattr(gps, "coordinates", None)
  if _diagnostic_distance(ego, ego) != "unknown":
    try:
      ego = wgs84_to_gcj02(*ego)
    except (ValueError, TypeError, AttributeError, KeyError, OverflowError):
      ego = None
  else:
    ego = None

  selected = getattr(sample, "selected", None)
  route = getattr(selected, "route", None)
  indices = [tuple(position.get(name) for name in ("segment_index", "link_index", "point_index"))
             for position in positions]
  lateral = ["unknown", "unknown"]
  ego_lateral = "unknown"
  try:
    from openpilot.selfdrive.navigation.amap_route_anchor import AmapRouteAnchorIndex
    if getattr(selected, "geometry_kind", "full_route") == "current_native_path_window":
      index = AmapRouteAnchorIndex.from_native(route.points_native, route.segments)
    else:
      index = AmapRouteAnchorIndex(route)
    if indices[0] == indices[1]:
      for i, point in enumerate(matched):
        if point is not None:
          lateral[i] = round(index.project(*indices[i], point, max_lateral_m=1000.0).lateral_m, 3)
      if ego is not None:
        ego_lateral = round(index.project(*indices[0], ego, max_lateral_m=1000.0).lateral_m, 3)
  except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
    pass

  def tick_changed(name):
    ticks = [_diagnostic_integer(position.get(name), maximum=(1 << 64) - 1) for position in positions]
    return ticks[0] != ticks[1] if "unknown" not in ticks else "unknown"

  anchor_names = ("segment_index", "link_index", "point_index")
  anchors = [{name: _diagnostic_integer(position.get(name), minimum=0, maximum=0x7fffffff)
              if position.get("status") == "observed" else "unknown" for name in anchor_names}
             for position in positions]
  anchors_known = all(value != "unknown" for anchor in anchors for value in anchor.values())
  result = {
    "schema_version": 1, "clock": "host_monotonic_ns",
    "sample_host_start_ns": _diagnostic_integer(getattr(sample, "host_start_ns", None)),
    "gps_to_native_before_m": _diagnostic_distance(ego, coordinates[0]),
    "gps_to_native_after_m": _diagnostic_distance(ego, coordinates[1]),
    "native_before_to_after_m": _diagnostic_distance(*coordinates),
    "matched_to_source_edge_before_m": lateral[0],
    "matched_to_source_edge_after_m": lateral[1],
    "gps_to_source_edge_m": ego_lateral,
    "native_tick_time_changed": tick_changed("tick_time_raw"),
    "native_input_tick_time_changed": tick_changed("input_tick_time_raw"),
    "source_anchors": {"native_before": anchors[0], "native_after": anchors[1],
                       "changed_fields": ([name for name in anchor_names if anchors[0][name] != anchors[1][name]]
                                          if anchors_known else "unknown")},
    "gps": {
      "source_clock": "phone_elapsed_realtime_ns",
      "source_elapsed_realtime_ns": _diagnostic_integer(getattr(gps, "elapsed_realtime_ns", None)),
      "source_age_upper_at_host_finished_ns": _diagnostic_integer(getattr(gps, "age_upper_ns", None), minimum=0),
      "host_window_ns": _diagnostic_window(getattr(gps, "host_started_ns", None), getattr(gps, "host_finished_ns", None)),
    },
  }
  for side, observation in zip(("native_before", "native_after"), observations, strict=True):
    result[side + "_host_window_ns"] = (
      _diagnostic_window(observation.get("host_observation_started_monotonic_ns"),
                         observation.get("host_observation_finished_monotonic_ns"))
      if observation.get("host_observation_clock_domain") == "host_monotonic" else "unknown")
  return result


def anchor_pair_retry_diagnostics(sample):
  value = _diagnostic_mapping(getattr(sample, "anchor_pair_retry", None))
  fields = {"schema_version", "source", "extra_pair_observations", "recovered", "original_host_start_ns",
            "original_host_finished_ns", "recovery_host_start_ns", "recovery_host_finished_ns", "anchor_before", "anchor_after"}
  if (value.keys() != fields or type(value.get("schema_version")) is not int or value["schema_version"] != 1
      or value.get("source") != "phone_native_gps_native" or value.get("recovered") is not True
      or type(value.get("extra_pair_observations")) is not int or value["extra_pair_observations"] != 1):
    return None
  times = [value[key] for key in ("original_host_start_ns", "recovery_host_start_ns",
                                 "original_host_finished_ns", "recovery_host_finished_ns")]
  if (any(_diagnostic_integer(stamp) == "unknown" for stamp in times)
      or not times[0] < times[1] <= times[2] <= times[3] or times[3] - times[0] >= 2_000_000_000):
    return None
  names = {"segment_index", "link_index", "point_index"}
  anchors = [_diagnostic_mapping(value[key]) for key in ("anchor_before", "anchor_after")]
  if (any(anchor.keys() != names or any(_diagnostic_integer(item, minimum=0, maximum=0x7fffffff) == "unknown"
                                     for item in anchor.values()) for anchor in anchors)
      or anchors[0] == anchors[1]):
    return None
  return {**value, "anchor_before": dict(anchors[0]), "anchor_after": dict(anchors[1])}


def native_snapshot_retries(sample):
  """Copy only the bounded retry schema; consumers deduplicate using sequence and side."""
  records = []
  for side in ("native_before", "native_after"):
    observation = _diagnostic_mapping(getattr(sample, side, None))
    retry = _diagnostic_mapping(observation.get("_snapshot_retry"))
    if (retry.keys() != {"schema_version", "source", "extra_observations", "recovered", "initial_diagnosis"}
        or type(retry.get("schema_version")) is not int or retry["schema_version"] != 1
        or retry.get("source") != "native_observation_pair"
        or type(retry.get("extra_observations")) is not int or retry["extra_observations"] != 1
        or type(retry.get("recovered")) is not bool):
      continue
    diagnosis = _diagnostic_mapping(retry.get("initial_diagnosis"))
    version = diagnosis.get("schema_version")
    expected = {"schema_version", "source", "changed_fields", "position_changed_fields", "position_serialization_changed"}
    if type(version) is int and version == 2:
      expected |= {"navi_info_changed_fields", "navi_info_serialization_changed"}
      if (diagnosis.get("navi_info_serialization_changed") is not False
          or diagnosis.get("navi_info_changed_fields") not in ([], ())):
        continue
    if (diagnosis.keys() != expected
        or type(version) is not int or version not in (1, 2)
        or diagnosis.get("source") != "native_observation_pair"
        or diagnosis.get("position_serialization_changed") is not True):
      continue
    changed, position = diagnosis.get("changed_fields"), diagnosis.get("position_changed_fields")
    if (not isinstance(changed, (list, tuple)) or len(changed) != 1 or changed[0] != "position"
        or not isinstance(position, (list, tuple)) or not 1 <= len(position) <= 4
        or any(type(field) is not str for field in position)
        or tuple(position) not in (("tick_time_raw",), ("tick_time_raw", "input_tick_time_raw"),
                                  ("native_st_pos_deg", "tick_time_raw"),
                                  ("native_st_pos_deg", "tick_time_raw", "input_tick_time_raw"),
                                  ("native_st_pos_deg", "matched_st_pos_deg", "tick_time_raw"),
                                  ("native_st_pos_deg", "matched_st_pos_deg", "tick_time_raw", "input_tick_time_raw"))):
      continue
    records.append({"side": side, "schema_version": retry["schema_version"], "source": retry["source"],
                    "extra_observations": retry["extra_observations"], "recovered": retry["recovered"],
                    "initial_diagnosis": {"schema_version": diagnosis["schema_version"], "source": diagnosis["source"],
                                          "changed_fields": list(changed), "position_changed_fields": list(position),
                                          "position_serialization_changed": diagnosis["position_serialization_changed"]}})
    if version == 2:
      records[-1]["initial_diagnosis"].update(navi_info_changed_fields=[], navi_info_serialization_changed=False)
  return records


_NAVI_INFO_BASE = {"schema_version", "status", "reason", "diagnostic_only", "control_input_allowed",
                   "route_render_input_allowed"}
_NAVI_INFO_FIELDS = {"schema_version", "status", "reason", "diagnostic_only", "control_input_allowed",
                     "route_render_input_allowed", "source", "path_identity_hash", "route_remain_time_raw",
                     "route_remain_distance_raw", "navigation_info_type_raw", "item_count", "item_index",
                     "current_segment", "current_link", "current_point", "branch_flag_raw", "branches"}
_NAVI_INFO_BRANCH_FIELDS = {"maneuver_id_raw", "maneuver_segment_raw", "maneuver_link_raw", "maneuver_point_raw",
                            "exit_number_raw", "exit_angle_raw", "obtain_supported_raw", "data_changed_raw",
                            "segment_remain_time_raw", "segment_remain_distance_raw", "next_road_name_segment_raw",
                            "next_road_name_link_raw", "split_raw", "segment_binding"}
_SEGMENT_BINDING_BASE = {"status", "reason"}
_SEGMENT_BINDING_VERIFIED = _SEGMENT_BINDING_BASE | {
  "segment_object_verified", "icon_id_getter_vtable_verified", "icon_getter_vtable_verified"}
_NAVI_INFO_DIAGNOSTIC_FIELDS = ("route_remain_time_raw", "route_remain_distance_raw",
                                "navigation_info_type_raw", "item_count", "item_index", "current_segment",
                                "current_link", "current_point", "branch_flag_raw")


def _bounded_integer(value):
  return value if type(value) is int and 0 <= value <= (1 << 63) - 1 else None


def native_navi_info_shadow(sample, side):
  """Copy only semantic-neutral action samples; PathID, coordinates and raw addresses stay private."""
  observation = _diagnostic_mapping(getattr(sample, side, None))
  value = _diagnostic_mapping(observation.get("_navi_info"))
  if (not value or value.keys() not in (_NAVI_INFO_BASE, _NAVI_INFO_FIELDS)
      or value.get("schema_version") != 2):
    return {"status": "unavailable", "reason": "missing_or_malformed_observation"}
  if (value.get("diagnostic_only") is not True or value.get("control_input_allowed") is not False
      or value.get("route_render_input_allowed") is not False):
    return {"status": "unavailable", "reason": "unsafe_observation_contract"}
  if value.get("status") == "unavailable":
    if value.keys() != _NAVI_INFO_BASE:
      return {"status": "unavailable", "reason": "malformed_observation"}
    reason = value.get("reason")
    return {"status": "unavailable",
            "reason": reason if type(reason) is str and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", reason)
            else "unknown_reason"}
  if value.keys() != _NAVI_INFO_FIELDS or value.get("source") != "native_navi_info":
    return {"status": "unavailable", "reason": "unsafe_observation_contract"}
  if value.get("status") != "observed":
    return {"status": "unavailable", "reason": "malformed_observation"}
  path_hash = _diagnostic_hash(value.get("path_identity_hash"))
  if path_hash == "unknown" or _diagnostic_hash(observation.get("path_identity_hash")) != path_hash:
    return {"status": "unavailable", "reason": "path_identity_unbound"}
  result = {"status": "observed", "semantics": "unverified", "diagnostic_only": True,
            "control_input_allowed": False, "route_render_input_allowed": False,
            "path_identity_hash": path_hash}
  for key in _NAVI_INFO_DIAGNOSTIC_FIELDS:
    checked = _bounded_integer(value.get(key))
    if checked is None:
      return {"status": "unavailable", "reason": "malformed_observation"}
    result[key] = checked
  if value["branch_flag_raw"] not in (0, 1):
    return {"status": "unavailable", "reason": "malformed_observation"}
  branches = value.get("branches")
  if not isinstance(branches, (list, tuple)) or len(branches) != 2:
    return {"status": "unavailable", "reason": "malformed_observation"}
  copied = []
  for branch in branches:
    branch = _diagnostic_mapping(branch)
    if branch.keys() != _NAVI_INFO_BRANCH_FIELDS:
      return {"status": "unavailable", "reason": "malformed_observation"}
    row = {}
    for key in sorted(_NAVI_INFO_BRANCH_FIELDS - {"segment_binding"}):
      checked = _bounded_integer(branch.get(key))
      if checked is None:
        return {"status": "unavailable", "reason": "malformed_observation"}
      row[key] = checked
    binding = _diagnostic_mapping(branch.get("segment_binding"))
    if not _SEGMENT_BINDING_BASE <= binding.keys():
      return {"status": "unavailable", "reason": "malformed_observation"}
    status, reason = binding.get("status"), binding.get("reason")
    if type(reason) is not str or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", reason):
      return {"status": "unavailable", "reason": "malformed_observation"}
    if status == "unavailable":
      if binding.keys() != _SEGMENT_BINDING_BASE:
        return {"status": "unavailable", "reason": "malformed_observation"}
      row["segment_binding"] = {"status": status, "reason": reason}
    elif (status == "verified" and binding.keys() == _SEGMENT_BINDING_VERIFIED
          and reason == "segment_object_verified"
          and all(binding.get(key) is True for key in (
            "segment_object_verified", "icon_id_getter_vtable_verified", "icon_getter_vtable_verified"))):
      row["segment_binding"] = {
        "status": status, "reason": reason, "segment_object_verified": True,
        "icon_id_getter_vtable_verified": True, "icon_getter_vtable_verified": True}
    else:
      return {"status": "unavailable", "reason": "malformed_observation"}
    copied.append(row)
  result["branches"] = copied
  try:
    rejected = next_road_field_rejections(copied)
  except ValueError:
    return {"status": "unavailable", "reason": "malformed_observation"}
  expected_reason = rejected[0]["reason"] if rejected else "unverified_navi_info_semantics"
  if value.get("reason") != expected_reason:
    return {"status": "unavailable", "reason": "malformed_observation"}
  if rejected:
    result.update(action_semantics_reason=expected_reason, field_rejections=rejected)
  return result


def _diagnostic_hash(value):
  return (value if type(value) is str and len(value) == 64 and all(char in "0123456789abcdef" for char in value)
          else "unknown")


def _diagnostic_text_hash(value, *, limit):
  if (type(value) is not str or not 0 < len(value) <= limit
      or any(ord(char) < 32 or ord(char) == 127 for char in value)):
    return "unknown"
  try:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
  except UnicodeError:
    return "unknown"


def route_identity_diagnostics(sample, runtime, *, map_valid):
  """Compare independent source and accepted-consumer observations, never infer missing bindings."""
  selected = getattr(sample, "selected", None)
  source = {
    "path_identity_hash": _diagnostic_hash(getattr(selected, "identity_hash", None)),
    "cache_route_index": _diagnostic_integer(getattr(selected, "cache_route_index", None), minimum=0, maximum=(1 << 32) - 1),
    "source_session_id": _diagnostic_hash(getattr(selected, "source_session_id", None)),
    "cache_sha256": _diagnostic_hash(getattr(selected, "cache_sha256", None)),
    "source_payload_sha256": _diagnostic_hash(getattr(selected, "source_payload_sha256", None)),
    "transport_session_hash": _diagnostic_text_hash(getattr(sample, "transport_session", None), limit=256),
    "source_object_hash": _diagnostic_text_hash(getattr(selected, "source_path", None), limit=4096),
    "observed_sequence": _diagnostic_integer(getattr(sample, "sequence", None), maximum=(1 << 64) - 1),
    "sample_host_start_ns": _diagnostic_integer(getattr(sample, "host_start_ns", None), maximum=(1 << 64) - 1),
  }
  runtime = _diagnostic_mapping(runtime)
  observed = (map_valid is True and runtime.get("source") == "phone"
              and runtime.get("mapReason") == runtime.get("mapCorrelationReason") == "accepted")
  binding = _diagnostic_mapping(runtime.get("mapBinding")) if observed else {}
  if type(binding.get("version")) is not int or binding.get("version") != 1:
    binding = {}
  consumer = {key: _diagnostic_hash(binding.get(key)) for key in (
    "routeId", "cacheSha256", "sourceSessionId", "bindingId", "sourceGeometrySha256", "geometrySha256")}
  consumer.update({
    "provider": "amapauto-root-fixed-profile" if binding.get("provider") == "amapauto-root-fixed-profile" else "unknown",
    "revision": _diagnostic_integer(binding.get("revision"), minimum=0, maximum=(1 << 32) - 1),
    "transportSessionHash": _diagnostic_text_hash(binding.get("transportSessionId"), limit=256),
    "sourceObjectHash": _diagnostic_text_hash(binding.get("sourceObjectId"), limit=4096),
    "observedSequence": _diagnostic_integer(binding.get("observedSequence"), maximum=(1 << 64) - 1),
    "observedMonoTime": _diagnostic_integer(binding.get("observedMonoTime"), maximum=(1 << 64) - 1),
  })
  complete = all(value != "unknown" for value in consumer.values())

  def equal(*pairs):
    if not complete or any(source[left] == "unknown" for left, _ in pairs):
      return "unknown"
    return all(source[left] == consumer[right] for left, right in pairs)

  matches = {
    "route_identity": equal(("path_identity_hash", "routeId"), ("cache_sha256", "cacheSha256"),
                            ("source_session_id", "sourceSessionId")),
    "transport_session": equal(("transport_session_hash", "transportSessionHash")),
    "source_object": equal(("source_object_hash", "sourceObjectHash")),
    "observation_sequence": equal(("observed_sequence", "observedSequence")),
  }
  return {"schema_version": 1, "selected": source,
          "consumer_origin": "NavRuntime.evidence.mapBinding", "consumer_binding": consumer,
          "consumer_binding_status": "observed" if complete else "unknown", "matches": matches,
          "comparison_scope": ["route_identity", "transport_session", "source_object", "observation_sequence"],
          "source_consumer_binding_match": (all(matches.values()) if "unknown" not in matches.values() else "unknown")}


def frame_record(index, elapsed_ms, sample, details, inputs, *, producer_status=None):
  runtime = details["runtime"]
  image = inputs["nav_map"]
  vector = inputs["navigation"]
  gps = getattr(sample, "gps", None)
  nonzero_commands = [int(i) for i in np.flatnonzero(vector[0, :-1])]

  def broadcast_diagnostics(value):
    """Expose only fixed booleans and bounded ages used by the instruction gate."""
    broadcast = _diagnostic_mapping(value)
    record = _diagnostic_mapping(_diagnostic_mapping(broadcast.get("records")).get("10001"))
    source_age = record.get("source_age_upper_ns")
    return {
      "status": broadcast.get("status") if broadcast.get("status") in ("observed", "unavailable") else "unknown",
      "reason": (broadcast.get("reason")
                 if isinstance(broadcast.get("reason"), str)
                 and len(broadcast["reason"]) <= 80 else "unknown"),
      "bridge_transport_valid": broadcast.get("bridge_transport_valid"),
      "receiver_active": broadcast.get("receiver_active"),
      "record_fresh": record.get("fresh"),
      "record_replayed": record.get("replayed"),
      "record_sequence": _diagnostic_integer(record.get("sequence")),
      "source_age_upper_ns": _diagnostic_integer(source_age, minimum=0),
      "host_read_window": _diagnostic_window(
        broadcast.get("host_read_started_ns"), broadcast.get("host_read_finished_ns")),
    }

  return {
    "index": index, "since_start_ms": round(elapsed_ms, 3),
    "has_sample": sample is not None,
    "source_reason": details["source_reason"],
    "map_valid": details["map_valid"], "map_nonzero": bool(image.any()),
    "map_pixels_nonzero": int((image != 0).sum()),
    "instruction_valid": details["instruction_valid"],
    "reset_requested": bool(details["reset_requested"]),
    "map_reason": runtime.get("mapReason"), "map_frame_id": runtime.get("mapFrameId"),
    "map_correlation_reason": runtime.get("mapCorrelationReason"),
    "instruction_reason": runtime.get("instructionReason"),
    "adapter_instruction_reason": details.get("adapter_instruction_reason"),
    "selected_command": runtime.get("selectedCommand"),
    "selected_maneuver_index": runtime.get("selectedManeuverIndex"),
    "raw_distance_m": runtime.get("rawDistance"),
    "adjusted_distance_m": runtime.get("adjustedDistance"),
    "traveled_distance_m": runtime.get("traveledDistance"),
    "navigation_nonzero_commands": nonzero_commands,
    "navigation_distance_fraction": float(vector[0, -1]),
    "sequence": getattr(sample, "sequence", None),
    "transport_session_hash": (hashlib.sha256(sample.transport_session.encode()).hexdigest()
                               if getattr(sample, "transport_session", None) else None),
    "gps_source_token": getattr(gps, "source_token", None),
    "gps_age_upper_ms": (round((gps.age_upper_ns + time.monotonic_ns() - gps.host_finished_ns) / 1e6, 3)
                         if gps is not None else None),
    "gps_accuracy_m": getattr(gps, "horizontal_accuracy_m", None),
    "gps_live_source": getattr(gps, "live_source", None),
    "cache_sha256": getattr(getattr(sample, "selected", None), "cache_sha256", None),
    "route_points": len(getattr(getattr(sample, "selected", None), "route", None).points_native)
                    if getattr(getattr(sample, "selected", None), "route", None) else None,
    "navigation_finite": bool(np.isfinite(vector).all()) if vector.size else False,
    "source_decision": details.get("source_decision"),
    "broadcast_diagnostics": broadcast_diagnostics(getattr(sample, "broadcast", None)),
    "sample_position_diagnostics": sample_position_diagnostics(sample),
    "anchor_pair_retry": anchor_pair_retry_diagnostics(sample),
    "route_identity_diagnostics": route_identity_diagnostics(sample, runtime, map_valid=details["map_valid"]),
    "native_snapshot_retries": native_snapshot_retries(sample),
    "navi_info_shadow": {
      "host_monotonic_ns": time.monotonic_ns(),
      "native_before": native_navi_info_shadow(sample, "native_before"),
      "native_after": native_navi_info_shadow(sample, "native_after"),
    },
    # Producer-side rejection snapshot; refer to its own event id/time, which need not
    # belong to this frame's sample.
    "rejection": (getattr(producer_status, "rejection", None)
                  if producer_status is not None else None),
    "rejection_diagnosis": (getattr(producer_status, "rejection_diagnosis", None)
                            if producer_status is not None else None),
  }


GPS_FRESH_NS = 2_000_000_000
GPS_STRICT_ACCURACY_M = 5.0
GPS_MAP_MAX_ACCURACY_M = 25.0


def gps_counts(records):
  """Diagnostic schema v3: separate strict, degraded-map, and rejected accuracy.

  Accuracy is an independent partition; usable combines both existing gates.
  Invalid data takes precedence, then a known failing gate, then missing data.
  usable_but_map_rejected replaces v1's joint fresh_but_map_rejected meaning.
  """
  counts = {"schema_version": 3, "fresh": 0, "stale": 0, "unknown": 0, "invalid": 0,
            "accuracy_strict": 0, "accuracy_degraded_map": 0, "accuracy_rejected": 0,
            "accuracy_qualified": 0, "accuracy_unqualified": 0,
            "accuracy_unknown": 0, "accuracy_invalid": 0,
            "usable": 0, "unusable": 0, "usability_unknown": 0, "usability_invalid": 0,
            "fresh_but_map_rejected": 0, "stale_with_valid_map": 0,
            "usable_but_map_rejected": 0, "unusable_with_valid_map": 0}

  def numeric_state(value, *, allow_zero):
    if value is None:
      return "unknown"
    if (type(value) not in (int, float) or (type(value) is float and not math.isfinite(value))
        or value < 0 or (not allow_zero and value == 0)):
      return "invalid"
    return "known"

  for record in records:
    age, accuracy = record.get("gps_age_upper_ms"), record.get("gps_accuracy_m")
    age_state = numeric_state(age, allow_zero=True)
    accuracy_state = numeric_state(accuracy, allow_zero=False)
    if age_state == "known":
      age_state = "fresh" if age < GPS_FRESH_NS / 1e6 else "stale"
    if accuracy_state == "known":
      if accuracy <= GPS_STRICT_ACCURACY_M:
        accuracy_state, accuracy_partition = "qualified", "strict"
      elif accuracy <= GPS_MAP_MAX_ACCURACY_M:
        accuracy_state, accuracy_partition = "qualified", "degraded_map"
      else:
        accuracy_state, accuracy_partition = "unqualified", "rejected"
    counts[age_state] += 1
    counts["accuracy_" + accuracy_state] += 1
    if accuracy_state in ("qualified", "unqualified"):
      counts["accuracy_" + accuracy_partition] += 1
    if "invalid" in (age_state, accuracy_state):
      usability = "usability_invalid"
    elif age_state == "stale" or accuracy_state == "unqualified":
      usability = "unusable"
    elif "unknown" in (age_state, accuracy_state):
      usability = "usability_unknown"
    else:
      usability = "usable"
    counts[usability] += 1
    if age_state == "fresh" and record.get("map_valid") is False:
      counts["fresh_but_map_rejected"] += 1
    if age_state == "stale" and record.get("map_valid") is True:
      counts["stale_with_valid_map"] += 1
    if usability == "usable" and record.get("map_valid") is False:
      counts["usable_but_map_rejected"] += 1
    if usability == "unusable" and record.get("map_valid") is True:
      counts["unusable_with_valid_map"] += 1
  return counts


def align_actions(actions_path, started):
  if not actions_path.exists():
    return {"present": False, "aligned": None, "entries": None}
  entries, unaligned = [], 0
  for line in actions_path.read_text(encoding="utf-8").splitlines():
    if not line.strip():
      continue
    row = json.loads(line)
    if isinstance(row.get("monotonic_s"), (int, float)):
      row["since_start_ms"] = round((row["monotonic_s"] - started) * 1000, 3)
      entries.append(row)
    else:
      row["since_start_ms"] = None
      row["aligned"] = False
      entries.append(row)
      unaligned += 1
  return {"present": True, "aligned": unaligned == 0, "unaligned_entries": unaligned,
          "entries": entries,
          "contract": "每行必须含真实 time.monotonic 秒的 monotonic_s；缺失即 unaligned，不做换算。"}


def _drain_rejections(producer, sink, started, dropped_total):
  """Consume the producer's own event queue; never derived from a per-frame status read."""
  drain = getattr(producer, "drain_rejection_events", None)
  if drain is None:
    return
  batch = drain()
  if batch["dropped_count"]:
    dropped_total.append(batch["dropped_count"])
  if sink is None:
    return
  for record in batch["events"]:
    sink.write(json.dumps({"event": record,
                           "probe_seen_since_start_ms": round((time.monotonic() - started) * 1000, 3)},
                          ensure_ascii=False) + "\n")
  if batch["events"]:
    sink.flush()


def intervals(records):
  first_valid = next((r["since_start_ms"] for r in records if r["map_valid"]), None)
  gaps, open_loss, seen_valid = [], None, False
  for record in records:
    if not record["map_valid"]:
      if seen_valid and open_loss is None:
        open_loss = record["since_start_ms"]
    else:
      seen_valid = True
      if open_loss is not None:
        gaps.append({"lost_ms": open_loss, "recovered_ms": record["since_start_ms"],
                     "duration_ms": round(record["since_start_ms"] - open_loss, 3)})
        open_loss = None
  return {"first_available_ms": first_valid, "recovery_gaps": gaps,
          "unrecovered_loss_ms": open_loss,
          "startup_phase_ms": first_valid if first_valid is not None else max(
            (r["since_start_ms"] for r in records), default=None)}


def transport_lifecycle(records):
  """Summarize valid > loss > new-session recovery from one continuous observation."""
  accepted = [record for record in records if record["map_valid"]]
  if not accepted:
    return {"status": "not_observed", "reason": "no_valid_map",
            "initial_session_hash": None, "recovered_session_hash": None,
            "loss_started_ms": None, "recovered_ms": None,
            "loss_duration_ms": None, "stale_command_after_loss": "unknown"}

  initial = accepted[0]
  initial_sequence = initial.get("sequence")
  initial_session = initial.get("transport_session_hash")
  if type(initial_sequence) is not int or initial_sequence < 0 or initial_session is None:
    return {"status": "not_observed", "reason": "initial_observation_incomplete",
            "initial_session_hash": initial_session, "recovered_session_hash": None,
            "loss_started_ms": None, "recovered_ms": None,
            "loss_duration_ms": None, "stale_command_after_loss": "unknown"}

  loss_started = next((record for record in records
                       if record["since_start_ms"] > initial["since_start_ms"]
                       and not record["map_valid"]), None)
  if loss_started is None:
    return {"status": "not_observed", "reason": "map_loss_not_observed",
            "initial_session_hash": initial_session, "recovered_session_hash": None,
            "loss_started_ms": None, "recovered_ms": None,
            "loss_duration_ms": None, "stale_command_after_loss": "not_applicable"}

  loss_index = records.index(loss_started)
  recovered = next((record for record in records[loss_index + 1:] if record["map_valid"]), None)
  stale_commands = [
    record.get("selected_command") for record in records[loss_index:records.index(recovered)
                                                           if recovered is not None else loss_index + 1]
    if record.get("selected_command") not in (None, -1)
  ]
  stale_command_after_loss = "present" if stale_commands else "absent"
  if (recovered is None
      or type(recovered.get("sequence")) is not int or recovered["sequence"] < 0
      or recovered.get("transport_session_hash") in (None, initial_session)):
    return {"status": "unrecovered", "reason": "new_session_not_recovered",
            "initial_session_hash": initial_session,
            "recovered_session_hash": (recovered.get("transport_session_hash")
                                       if recovered is not None else None),
            "loss_started_ms": loss_started["since_start_ms"], "recovered_ms": None,
            "loss_duration_ms": None, "stale_command_after_loss": stale_command_after_loss}
  return {
    "status": "recovered",
    "reason": "new_transport_session_with_valid_sequence",
    "initial_session_hash": initial_session,
    "recovered_session_hash": recovered["transport_session_hash"],
    "loss_started_ms": loss_started["since_start_ms"],
    "recovered_ms": recovered["since_start_ms"],
    "loss_duration_ms": round(recovered["since_start_ms"] - loss_started["since_start_ms"], 3),
    "stale_command_after_loss": stale_command_after_loss,
  }


def main(argv=None):
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--out", type=Path, required=True)
  parser.add_argument("--duration", type=int, default=DEFAULT_DURATION)
  parser.add_argument("--port", type=int, default=5039)
  parser.add_argument("--serial")
  parser.add_argument("--adb", help="adb 可执行文件；可由 NAVDIAG_ADB 指定")
  parser.add_argument("--adb-ownership", choices=("dedicated", "attach"), default="dedicated",
                      help="dedicated 由本次运行独占新端口；attach 只连接已有服务")
  parser.add_argument("--capture-private-samples", action="store_true",
                      help="私有保存采样所见的完整几何、成对Native/GPS观察及原始时钟")
  args = parser.parse_args(argv)
  if not MIN_DURATION <= args.duration <= MAX_DURATION:
    parser.error(f"--duration 必须在 {MIN_DURATION}..{MAX_DURATION} 秒之间")
  args.out.mkdir(mode=0o700, parents=True, exist_ok=False)
  frames_path = args.out / "frames.jsonl"

  stop = threading.Event()

  def on_signal(signum, _frame):
    stop.set()
    raise KeyboardInterrupt(f"signal:{signum}")

  signal.signal(signal.SIGTERM, on_signal)
  signal.signal(signal.SIGINT, on_signal)

  started = time.monotonic()
  wall_anchor = datetime.now(UTC)
  report = {"schema_version": 1, "mode": "live_phone_source_continuous_actual_msgq_visionipc_no_gpu",
            "actual_msgq": True, "actual_visionipc": True, "gpu_inference_in_this_run": False,
            "control_stack_started": False, "source_clock_translated": False,
            "started_monotonic_s": started, "wall_clock_anchor_s": wall_anchor.timestamp(),
            "wall_clock_anchor_utc": wall_anchor.isoformat(),
            "requested_duration_s": args.duration, "host_resources": [], "phone_resources": [],
            "producer_observations": {}, "close": {}, "bench_entered": False,
            "interrupted": None, "error": None}
  producer = None
  sample_archive = None
  bench = None
  bench_entered = False
  errors = []
  records = []
  events_path = args.out / "rejections.jsonl"
  dropped_total = []
  sampling_start = sampling_end = None
  init_ms = cleanup_ms = None
  try:
    if args.capture_private_samples:
      sample_archive = SampleEvidenceArchive(args.out)
    producer = ObservedProducer(port=args.port, serial=args.serial, adb=args.adb,
                                adb_ownership=args.adb_ownership)
    report["host_resources"].append({"kind": "producer_thread", "name": "phone-route-producer"})
    producer.start()
    bench = PhonePipelineBench()
    bench.__enter__()
    bind_status = getattr(bench, "bind_source_status", None)
    if callable(bind_status):
      bind_status(producer.status)
    bench_entered = True
    report["bench_entered"] = True
    report["host_resources"].append({"kind": "openpilot_prefix", "prefix": str(bench.prefix),
                                     "owned": True, "isolates": "msgq+visionipc"})
    init_ms = round((time.monotonic() - started) * 1000, 3)
    report["init_ms"] = init_ms
    deadline = started + args.duration
    sampling_start = time.monotonic()
    index = 0
    prior_status = None
    with frames_path.open("w", encoding="utf-8") as sink, events_path.open("w", encoding="utf-8") as events:
      while time.monotonic() < deadline and not stop.is_set():
        status = producer.status()
        if prior_status != (status.reason, status.sequence, status.connected):
          report.setdefault("source_events", []).append(
            {"since_start_ms": round((time.monotonic() - started) * 1000, 3),
             "reason": status.reason, "sequence": status.sequence, "connected": status.connected})
          prior_status = (status.reason, status.sequence, status.connected)
        _drain_rejections(producer, events, started, dropped_total)
        sample = producer.latest()
        inputs, details = bench.step(sample)
        record = frame_record(index, (time.monotonic() - started) * 1000, sample, details, inputs,
                              producer_status=status)
        if sample_archive is not None:
          record["private_sample_evidence"] = sample_archive.record(sample)
          record["private_adapter_call_evidence"] = sample_archive.record_call(
            record["private_sample_evidence"], details.get("adapter_call_context"))
        records.append(record)
        sink.write(json.dumps(record, ensure_ascii=False) + "\n")
        sink.flush()
        index += 1
        time.sleep(SAMPLE_INTERVAL_S)
  except BaseException as error:
    errors.append(repr(error))
    report["error" if not isinstance(error, KeyboardInterrupt) else "interrupted"] = repr(error)
  finally:
    sampling_end = time.monotonic()
    cleanup_started = time.monotonic()
    producer_stopped = None
    producer_status = None
    if producer is not None:
      try:
        producer_stopped = producer.close()
      except BaseException as error:
        errors.append("producer_close:" + repr(error))
        producer_stopped = False
      try:
        with events_path.open("a", encoding="utf-8") as final_events:
          _drain_rejections(producer, final_events, started, dropped_total)
      except BaseException as error:
        errors.append("final_drain:" + repr(error))
      try:
        producer_status = producer.status()
        report["producer_observations"] = producer.summary()
      except BaseException as error:
        errors.append("producer_status:" + repr(error))
    if bench is not None and bench_entered:
      try:
        bench.__exit__(None, None, None)
      except BaseException as error:
        errors.append("bench_exit:" + repr(error))
        report["bench_cleanup_error"] = repr(error)
    cleanup_ms = round((time.monotonic() - cleanup_started) * 1000, 3)
    report["cleanup_ms"] = cleanup_ms
    if producer_status is not None:
      report["close"] = {"producer_thread_stopped": bool(producer_stopped),
                         "running": producer_status.running, "reason": producer_status.reason,
                         "cleanup_verified": producer_status.cleanup_verified,
                         "cleanup_diagnostics": list(producer_status.cleanup_diagnostics)}
    else:
      report["close"] = {"producer_thread_stopped": False, "cleanup_verified": False,
                         "cleanup_diagnostics": ["producer_unavailable"]}
    report["cleanup_verified"] = bool(
      producer_status is not None and producer_stopped and producer_status.cleanup_verified
      and report.get("bench_cleanup_error") is None)
    report["rejection_dropped_count"] = sum(dropped_total)
    report["total_wall_s"] = round(time.monotonic() - started, 3)
    report["sampling_window_s"] = (round(sampling_end - sampling_start, 3)
                                   if sampling_start is not None else 0.0)
    report["sampling_started_since_start_ms"] = (round((sampling_start - started) * 1000, 3)
                                                 if sampling_start is not None else None)
    report["sampling_ended_since_start_ms"] = (round((sampling_end - started) * 1000, 3)
                                               if sampling_end is not None else None)
    report["frames"] = len(records)
    report["accepted_frames"] = sum(1 for r in records if r["map_valid"])
    report["reason_counts"] = dict(Counter(r["source_reason"] for r in records))
    report["map_reason_counts"] = dict(Counter(str(r["map_reason"]) for r in records))
    report["unique_sequences"] = len({r["sequence"] for r in records if r["sequence"] is not None})
    report["unique_gps_tokens"] = len({r["gps_source_token"] for r in records if r["gps_source_token"]})
    report["source_sessions"] = len({r["transport_session_hash"] for r in records
                                     if r["transport_session_hash"]})
    report["adb_transport"] = producer_transport_report(producer)
    report["host_resources"].append({"kind": "adb_transport", "requested_port": args.port,
                                     "requested_ownership": args.adb_ownership,
                                     **report["adb_transport"]})
    report["timeline"] = intervals(records)
    report["transport_lifecycle"] = transport_lifecycle(records)
    report["gps_counts"] = gps_counts(records)
    if events_path is not None and events_path.exists():
      report["rejections"] = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()
                              if line.strip()]
    report["actions"] = align_actions(args.out / "actions.jsonl", started)
    observations = report["producer_observations"]
    report["recovery_evidence"] = {
      "whitelisted_recovery_observed": bool(observations.get("recover_hits")),
      "status": "observed" if observations.get("recover_hits") else "not_observed",
      "collector_calls": observations.get("collector_calls"),
      "note": "未遇到白名单异常时标记 not_observed；不以 0 次当作无缺口证明。"}
    report["errors"] = errors
    report["source_sha256"] = {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in (
      "APP/navdiag/live_pipeline_probe.py", "APP/navdiag/phone_pipeline_bench.py",
      "APP/navdiag/sample_evidence.py",
      "APP/navdiag/phone_route_producer.py", "APP/navdiag/android_gps_source.py",
      "APP/navdiag/native_route_source.py", "openpilot/selfdrive/navigation/navd.py",
      "openpilot/selfdrive/navigation/mapsd.py", "openpilot/selfdrive/modeld/nav_inputs.py")} | replay_source_sha256()
    report["sample_evidence"] = sample_archive.summary() if sample_archive is not None else {"enabled": False}
    target = args.out / "report.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    target.chmod(0o600)
    if not frames_path.exists():
      frames_path.touch()
    frames_path.chmod(0o600)
    if events_path.exists():
      events_path.chmod(0o600)
    print(json.dumps({"frames": report["frames"], "accepted": report["accepted_frames"],
                      "sessions": report["source_sessions"],
                      "cleanup_verified": report["cleanup_verified"],
                      "report": str(target.resolve())}, ensure_ascii=False))
  ok = (not errors and report["cleanup_verified"] and report["interrupted"] is None
        and report["error"] is None)
  return 0 if ok else 1


if __name__ == "__main__":
  sys.exit(main())
