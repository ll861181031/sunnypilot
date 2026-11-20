#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
import re
import struct
import subprocess
from pathlib import Path
import time

if __package__:
  from .amap_adb_memory import NativeMemorySource
  from .amap_memory_probe import identity_hash
  from .device import Device, ROOT, choose_adb
  from .root_probe import write_private
  from .route_snapshot import CACHE_DIRECTORY, gather_window
else:
  from amap_adb_memory import NativeMemorySource
  from amap_memory_probe import identity_hash
  from device import Device, ROOT, choose_adb
  from root_probe import write_private
  from route_snapshot import CACHE_DIRECTORY, gather_window
from tools.cplink_shim.amap_route_cache import CachedRoute, SUPPORTED_PROFILE, decode_cache


MAX_OBSERVATION_NS = 2_500_000_000
MAX_WINDOW_NS = 8_000_000_000


class LiveRouteRejected(ValueError):
  pass


@dataclass(frozen=True)
class SelectedCacheRoute:
  route: CachedRoute = field(repr=False)
  cache_route_index: int
  source_path: str
  cache_sha256: str
  source_session_id: str
  identity_hash: str
  observed_started_ns: int
  observed_finished_ns: int

  def summary(self):
    return {"schema_version": 1, "route_identity_verified": True, "complete_route_verified": True,
            "cache_sha256": self.cache_sha256, "source_session_id": self.source_session_id,
            "path_identity_hash": self.identity_hash, "segments": len(self.route.segments),
            "cache_route_index": self.cache_route_index,
            "points": len(self.route.points_native), "distance_m": self.route.total_distance_m,
            "coordinate_frame": "amap_native_map", "coordinate_datum_verified": False,
            "observation_started_monotonic_ns": self.observed_started_ns,
            "observation_finished_monotonic_ns": self.observed_finished_ns,
            "atomic_snapshot": False, "map_input_ready": False, "control_input_allowed": False}


@dataclass(frozen=True)
class NativeRouteGeometry:
  points_native: tuple[tuple[int, int], ...] = field(repr=False)
  segments: tuple[tuple[tuple[int, int], ...], ...] = field(repr=False)
  segment_count: int
  link_count: int
  main_index: int
  path_count: int
  navi_type_raw: int
  navi_state_raw: int

  @property
  def points_degrees(self):
    from tools.cplink_shim.amap_route_cache import COORDINATE_SCALE
    return tuple((x / COORDINATE_SCALE, y / COORDINATE_SCALE) for x, y in self.points_native)


@dataclass(frozen=True)
class SelectedNativeRoute:
  route: NativeRouteGeometry = field(repr=False)
  source_path: str = field(repr=False)
  source_geometry_sha256: str
  source_session_id: str
  identity_hash: str
  observed_started_ns: int
  observed_finished_ns: int
  cache_route_index: int | None = None
  geometry_kind: str = "current_native_path_window"
  source_payload_sha256: str | None = None
  geometry_identity_kind: str = "native_payload_sha256_legacy"

  @property
  def cache_sha256(self):
    return self.source_geometry_sha256

  @property
  def complete_current_path_window_verified(self):
    return True

  @property
  def complete_route_verified(self):
    return False

  def summary(self):
    return {"schema_version": 1, "route_identity_verified": True,
            "complete_current_path_window_verified": True,
            "complete_route_verified": False,
            "source_geometry_sha256": self.source_geometry_sha256,
            "source_payload_sha256": self.source_payload_sha256,
            "geometry_identity_kind": self.geometry_identity_kind,
            "source_session_id": self.source_session_id, "path_identity_hash": self.identity_hash,
            "segments": len(self.route.segments), "links": self.route.link_count,
            "points": len(self.route.points_native), "coordinate_frame": "amap_native_map",
            "coordinate_datum_verified": False,
            "observation_started_monotonic_ns": self.observed_started_ns,
            "observation_finished_monotonic_ns": self.observed_finished_ns,
            "atomic_snapshot": False, "map_input_ready": False, "control_input_allowed": False}


def select_native(geometry: dict, *, epoch: str) -> SelectedNativeRoute:
  """Bind one already validated native geometry window to its source identity."""
  path_hash = geometry.get("path_identity_hash")
  geometry_hash = geometry.get("source_geometry_sha256")
  if (geometry.get("status") != "observed"
      or geometry.get("geometry_kind") != "current_native_path_window"
      or geometry.get("complete_current_path_window_verified") is not True
      or geometry.get("active_route_verified") is not False
      or geometry.get("control_input_allowed") is not False
      or geometry.get("coordinate_frame") != "amap_native_map"
      or geometry.get("coordinate_datum_verified") is not False
      or not isinstance(path_hash, str) or re.fullmatch(r"[0-9a-f]{64}", path_hash) is None
      or not isinstance(geometry_hash, str) or re.fullmatch(r"[0-9a-f]{64}", geometry_hash) is None
      or geometry.get("epoch_sha256") != hashlib.sha256(epoch.encode()).hexdigest()):
    raise LiveRouteRejected("native_geometry_identity_invalid")
  points = geometry.get("points_native")
  segments = geometry.get("segments")
  point_count = geometry.get("point_count")
  link_count = geometry.get("link_count")
  segment_count = geometry.get("segment_count")
  if (not isinstance(points, (tuple, list)) or not isinstance(segments, (tuple, list))
      or type(point_count) is not int or len(points) != point_count
      or type(segment_count) is not int or len(segments) != segment_count
      or type(link_count) is not int or sum(len(link) for link in segments) != link_count):
    raise LiveRouteRejected("native_geometry_shape_invalid")
  route = NativeRouteGeometry(
    tuple(tuple(point) for point in points),
    tuple(tuple(tuple(link) for link in segment) for segment in segments),
    segment_count, link_count, geometry.get("main_index"), geometry.get("path_count"),
    geometry.get("navi_type_raw"), geometry.get("navi_state_raw"))
  started = geometry.get("host_observation_started_monotonic_ns")
  finished = geometry.get("host_observation_finished_monotonic_ns")
  if (geometry.get("host_observation_clock_domain") != "host_monotonic"
      or type(started) is not int or type(finished) is not int or not 0 < started <= finished):
    raise LiveRouteRejected("native_geometry_window_invalid")
  raw_payload_hash = geometry_hash
  identity_kind = "native_payload_sha256_legacy"
  if "geometry_identity_kind" in geometry or "geometry_identity_sha256" in geometry:
    if __package__:
      from . import native_route_source as native
    else:
      import native_route_source as native
    identity_kind = geometry.get("geometry_identity_kind")
    geometry_identity = geometry.get("geometry_identity_sha256")
    if (identity_kind != native.GEOMETRY_IDENTITY_KIND
        or not isinstance(geometry_identity, str) or re.fullmatch(r"[0-9a-f]{64}", geometry_identity) is None):
      raise LiveRouteRejected("native_geometry_identity_invalid")
    try:
      header = (1, route.navi_type_raw, route.navi_state_raw, route.path_count,
                route.main_index, segment_count, link_count, point_count)
      if (any(type(value) is not int for value in header)
          or route.navi_type_raw != 0 or route.navi_state_raw not in (1, 3)
          or not 1 <= route.path_count <= 32 or not 0 <= route.main_index < route.path_count
          or not 1 <= segment_count <= native.MAX_ROUTE_SEGMENTS
          or not 1 <= link_count <= native.MAX_ROUTE_LINKS or not 2 <= point_count <= native.MAX_ROUTE_POINTS
          or any(type(value) is not int for point in route.points_native for value in point)
          or any(type(value) is not int for links in route.segments for link in links for value in link)):
        raise ValueError("native_geometry_header_invalid")
      links = b"".join(struct.pack("<3I", segment, start, count)
                       for segment, spans in enumerate(route.segments) for start, count in spans)
      points = b"".join(struct.pack("<2i", *point) for point in route.points_native)
      payload = (native._GEOMETRY_MAGIC + struct.pack("<8I", *header)
                 + bytes.fromhex(path_hash) + hashlib.sha256(epoch.encode()).digest() + links + points)
      if (hashlib.sha256(payload).hexdigest() != raw_payload_hash
          or native.geometry_identity_sha256(payload) != geometry_identity):
        raise ValueError("native_geometry_hash_mismatch")
    except (ValueError, TypeError, struct.error, OverflowError):
      raise LiveRouteRejected("native_geometry_identity_invalid") from None
    geometry_hash = geometry_identity
  return SelectedNativeRoute(route, "native-drive-path:" + geometry_hash, geometry_hash, geometry_hash,
                             path_hash, started, finished, source_payload_sha256=raw_payload_hash,
                             geometry_identity_kind=identity_kind)


def _verified_observation(observation: dict, epoch: str) -> tuple[int, int]:
  if observation.get("status") == "unavailable":
    reason = observation.get("reason")
    if isinstance(reason, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", reason):
      raise LiveRouteRejected(reason)
  if (observation.get("status") != "observed" or observation.get("sample_consistent") is not True
      or observation.get("gps_navigation_observed") is not True
      or observation.get("navi_type_raw") != 0 or observation.get("navi_state_raw") not in (1, 3)):
    raise LiveRouteRejected("gps_navigation_not_observed")
  if observation.get("process_epoch_hash") != hashlib.sha256(epoch.encode()).hexdigest():
    raise LiveRouteRejected("provider_epoch_mismatch")
  clock = observation.get("observation_clock_domain", "host_monotonic")
  if clock == "host_monotonic":
    prefix = ""
  elif clock == "phone_elapsed_realtime" and observation.get("host_observation_clock_domain") == "host_monotonic":
    prefix = "host_"
  else:
    raise LiveRouteRejected("unsupported_observation_clock")
  started = observation.get(prefix + "observation_started_monotonic_ns")
  finished = observation.get(prefix + "observation_finished_monotonic_ns")
  if type(started) is not int or type(finished) is not int or not 0 < started <= finished:
    raise LiveRouteRejected("invalid_observation_window")
  if finished - started > MAX_OBSERVATION_NS:
    raise LiveRouteRejected("observation_window_too_long")
  return started, finished


def associate(before: dict, snapshot: dict, files: dict[str, bytes], after: dict, *, epoch: str) -> SelectedCacheRoute:
  first_started, first_finished = _verified_observation(before, epoch)
  last_started, last_finished = _verified_observation(after, epoch)
  if before.get("path_identity_hash") != after.get("path_identity_hash"):
    raise LiveRouteRejected("main_route_changed_during_read")
  if not first_finished <= snapshot.get("read_started_mono_ns", 0) <= snapshot.get("read_finished_mono_ns", 0) <= last_started:
    raise LiveRouteRejected("cache_outside_observation_brackets")
  if last_finished - first_started > MAX_WINDOW_NS:
    raise LiveRouteRejected("collection_window_too_long")
  if snapshot.get("read_window_stable") is not True:
    raise LiveRouteRejected("cache_window_unstable")
  matches = []
  for item in snapshot.get("files", []):
    path = item.get("source", "")
    if not path.startswith(CACHE_DIRECTORY + "/"):
      continue
    data = files.get(path)
    if not isinstance(data, bytes) or hashlib.sha256(data).hexdigest() != item.get("sha256"):
      raise LiveRouteRejected("cache_hash_mismatch")
    decoded = decode_cache(data, profile=SUPPORTED_PROFILE)
    for index, route in enumerate(decoded.routes):
      if identity_hash(route.path_id, epoch) == after.get("path_identity_hash"):
        matches.append((path, decoded.sha256, route, index))
  if len(matches) != 1:
    raise LiveRouteRejected("selected_cache_missing_or_ambiguous")
  path, digest, route, index = matches[0]
  session = snapshot.get("source_app_session_id")
  if not isinstance(session, str) or len(session) != 64:
    raise LiveRouteRejected("source_session_missing")
  return SelectedCacheRoute(route, index, path, digest, session, after["path_identity_hash"], first_started, last_finished)


def collect_selected(device, memory: NativeMemorySource):
  before = memory.observe()
  _verified_observation(before, memory.epoch)
  snapshot, files = gather_window(device)
  after = memory.observe()
  result = associate(before, snapshot, files, after, epoch=memory.epoch)
  return result, {"before": before, "snapshot": snapshot, "after": after, "provider_epoch": memory.epoch}, files


def collect_native(source):
  observation = source.observe()
  _verified_observation(observation, source.epoch)
  geometry = source.observe_geometry()
  if geometry.get("path_identity_hash") != observation.get("path_identity_hash"):
    raise LiveRouteRejected("native_selected_identity_changed")
  selected = select_native(geometry, epoch=source.epoch)
  return selected, {"observation": observation, "geometry": geometry,
                    "provider_epoch": source.epoch}, {}


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--adb")
  parser.add_argument("--serial")
  parser.add_argument("--port", type=int, default=5039)
  parser.add_argument("--adb-ownership", choices=("dedicated", "attach"), default="dedicated",
                      help="attach 仅复用现有 ADB 服务，不启动或关闭它")
  parser.add_argument("--backend", choices=("python", "native"), default="python")
  parser.add_argument("--out", type=Path)
  args = parser.parse_args()
  output = args.out or ROOT / "captures" / ("live-route-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
  output.mkdir(mode=0o700, parents=True, exist_ok=False)
  try:
    with Device(choose_adb(args.adb), args.port, args.serial,
                ownership=args.adb_ownership) as device:
      if args.backend == "native":
        if __package__:
          from .native_route_source import NativeRouteProbeSource
        else:
          from native_route_source import NativeRouteProbeSource
        source_type = NativeRouteProbeSource
      else:
        source_type = NativeMemorySource
      with source_type(device) as source:
        if args.backend == "native":
          selected, evidence, files = collect_native(source)
        else:
          selected, evidence, files = collect_selected(device, source)
    if args.backend == "python":
      for item in evidence["snapshot"]["files"]:
        name = "blob-" + item["sha256"] + ".bin"
        if not (output / name).exists():
          write_private(output / name, files[item["source"]])
        item["file"] = name
    write_private(output / "evidence.json", (json.dumps(evidence, ensure_ascii=False, indent=2) + "\n").encode())
    report = selected.summary()
  except (ValueError, OSError, subprocess.SubprocessError) as error:
    reason = str(error) if re.fullmatch(r"[a-zA-Z0-9_]{1,80}", str(error)) else type(error).__name__
    report = {"schema_version": 1, "route_identity_verified": False, "reason": reason,
              "complete_route_verified": False, "map_input_ready": False, "control_input_allowed": False}
  report["report_written_monotonic_ns"] = time.monotonic_ns()
  write_private(output / "report.json", (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode())
  print(json.dumps({**report, "report": str((output / "report.json").resolve())}, ensure_ascii=False))


if __name__ == "__main__":
  main()
