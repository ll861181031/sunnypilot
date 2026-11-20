
from collections.abc import Callable, Mapping
from dataclasses import dataclass
import hashlib
import math
import re
import struct
import time


SUPPORTED_PROFILE = "amapauto-9.5.0.600013-arm64-v1"
GBL_SHA256 = "934396f73adf760af02e77ae8bd650c4e659522acd28db35f501d7633675a419"
AUTO_DICE_SHA256 = "5884adf676cafb883f92372af11c0e1c67b8b9cb7da8fdd59d885e5b441e1b71"
EXPECTED_HASHES = {"libGbl.so": GBL_SHA256, "libAutoDice.so": AUTO_DICE_SHA256}
MAX_ADDRESS = 1 << 48
MAX_READ_BYTES = 16384
MAX_READ_CALLS = 512
MAX_BUCKETS = 65536
MAX_CHAIN_NODES = 64
MAX_PATHS = 32
MAX_LOCATION_MATCHES = 3
STATE_NAMES = {1: "started", 2: "paused", 3: "resumed", 4: "stopped", 5: "arrived"}


class ProbeError(ValueError):
  pass


@dataclass(frozen=True)
class MemoryMap:
  start: int
  end: int
  permissions: str
  offset: int
  device: str
  inode: int
  path: str


def parse_maps(text: str) -> tuple[MemoryMap, ...]:
  if not isinstance(text, str) or len(text) > 2 * 1024 * 1024:
    raise ProbeError("invalid_maps")
  rows = []
  lines = text.splitlines()
  if not lines or len(lines) > 20000:
    raise ProbeError("invalid_maps")
  for line in lines:
    parts = line.split(None, 5)
    if len(parts) < 5:
      raise ProbeError("invalid_maps")
    try:
      bounds, permissions, offset, device, inode = parts[:5]
      first, last = bounds.split("-")
      start, end = int(first, 16), int(last, 16)
      file_offset, inode_number = int(offset, 16), int(inode)
    except (ValueError, TypeError):
      raise ProbeError("invalid_maps") from None
    if (not 0 < start < end <= MAX_ADDRESS or file_offset < 0 or inode_number < 0
        or re.fullmatch(r"[r-][w-][x-][ps]", permissions) is None
        or re.fullmatch(r"[0-9a-fA-F]+:[0-9a-fA-F]+", device) is None):
      raise ProbeError("invalid_maps")
    rows.append(MemoryMap(start, end, permissions, file_offset, device, inode_number,
                          parts[5] if len(parts) > 5 else ""))
  rows.sort(key=lambda row: row.start)
  if any(left.end > right.start for left, right in zip(rows, rows[1:], strict=False)):
    raise ProbeError("overlapping_maps")
  return tuple(rows)


def _library_base(maps: tuple[MemoryMap, ...], name: str) -> int:
  candidates = [row for row in maps if row.path.endswith("/" + name)
                and row.path.startswith("/data/app/")
                and re.search(r"/com\.autonavi\.amapauto-[^/]+/", row.path)
                and "/lib/arm64/" in row.path]
  if not candidates:
    raise ProbeError("library_mapping_missing")
  identities = {(row.path, row.device, row.inode) for row in candidates}
  roots = [row for row in candidates if row.offset == 0 and "r" in row.permissions]
  if len(identities) != 1 or len(roots) != 1:
    raise ProbeError("library_mapping_ambiguous")
  return roots[0].start


def identity_hash(path_id: int, process_epoch: str) -> str:
  if isinstance(path_id, bool) or not isinstance(path_id, int) or not 0 < path_id <= 0xffffffff:
    raise ProbeError("invalid_path_id")
  if not isinstance(process_epoch, str) or not process_epoch or len(process_epoch) > 256:
    raise ProbeError("invalid_process_epoch")
  value = b"amap-path-observation-v1\0" + process_epoch.encode("utf-8") + b"\0" + struct.pack("<I", path_id)
  return hashlib.sha256(value).hexdigest()


class _BoundedReader:
  def __init__(self, maps: tuple[MemoryMap, ...], read: Callable[[int, int], bytes]):
    self.maps = maps
    self.read_callback = read
    self.calls = 0
    self.bytes = 0
    self.stage = "initializing"
    self.immutable_cache: dict[tuple[int, int], bytes] = {}

  def check(self, address: int, size: int, permission: str = "r") -> None:
    if not 0 < address < MAX_ADDRESS or size <= 0 or address + size > MAX_ADDRESS:
      raise ProbeError("invalid_address")
    if not any(row.start <= address and address + size <= row.end and permission in row.permissions
               for row in self.maps):
      raise ProbeError("unmapped_read" if permission == "r" else "non_executable_target")

  def read(self, address: int, size: int) -> bytes:
    if size > 64 or self.calls >= MAX_READ_CALLS or self.bytes + size > MAX_READ_BYTES:
      raise ProbeError("read_budget_exceeded")
    self.check(address, size)
    self.calls += 1
    self.bytes += size
    try:
      raw = self.read_callback(address, size)
    except Exception:
      raise ProbeError("read_failed") from None
    if not isinstance(raw, bytes) or len(raw) != size:
      raise ProbeError("short_read")
    return raw

  def pointer(self, address: int, *, nullable: bool = False) -> int:
    value = struct.unpack("<Q", self.read(address, 8))[0]
    return self.validate_pointer(value, nullable=nullable)

  def validate_pointer(self, value: int, *, nullable: bool = False) -> int:
    if not value and nullable:
      return 0
    if not value:
      raise ProbeError("null_pointer")
    if value & 7:
      raise ProbeError("unaligned_pointer")
    self.check(value, 8)
    return value

  def immutable_u64(self, address: int) -> int:
    valid = any(row.start <= address and address + 8 <= row.end
                and "r" in row.permissions and "w" not in row.permissions
                and any(row.path.endswith("/" + name) for name in EXPECTED_HASHES)
                for row in self.maps)
    if not valid:
      raise ProbeError("immutable_mapping_not_read_only")
    key = (address, 8)
    if key not in self.immutable_cache:
      self.immutable_cache[key] = self.read(address, 8)
    return struct.unpack("<Q", self.immutable_cache[key])[0]

  def u64(self, address: int) -> int:
    return struct.unpack("<Q", self.read(address, 8))[0]

  def u32(self, address: int) -> int:
    return struct.unpack("<I", self.read(address, 4))[0]


def _manager(reader: _BoundedReader, gbl: int) -> tuple[int, tuple[int, ...]]:
  reader.stage = "registry_root"
  registry = reader.pointer(gbl + 0x2aad120)
  reader.stage = "registry_buckets"
  buckets, count = struct.unpack("<QQ", reader.read(registry + 0x48, 16))
  reader.validate_pointer(buckets)
  if not 1 <= count <= MAX_BUCKETS:
    raise ProbeError("registry_bucket_count")
  key = gbl + 0x7e6024
  previous = reader.pointer(buckets + (key % count) * 8, nullable=True)
  if not previous:
    raise ProbeError("manager_not_registered")
  node = reader.pointer(previous, nullable=True)
  visited = set()
  for _ in range(MAX_CHAIN_NODES):
    reader.stage = "registry_node"
    if not node:
      raise ProbeError("manager_not_registered")
    if node in visited:
      raise ProbeError("registry_cycle")
    visited.add(node)
    next_node, node_hash, type_info, _, wrapper = struct.unpack("<QQQQQ", reader.read(node, 40))
    if node_hash % count != key % count:
      raise ProbeError("manager_not_registered")
    if node_hash == key:
      reader.validate_pointer(type_info)
      type_name = reader.immutable_u64(type_info + 8)
      if type_name == key:
        if type_info != gbl + 0x2985828:
          raise ProbeError("manager_type_mismatch")
        reader.stage = "manager_instance"
        reader.validate_pointer(wrapper)
        manager = reader.pointer(wrapper + 0x130)
        return manager, (registry, buckets, count, previous, node, wrapper, manager)
    node = reader.validate_pointer(next_node, nullable=True)
  raise ProbeError("registry_chain_limit")


def _position_unavailable(reason: str) -> dict:
  return {"status": "unavailable", "reason": reason,
          "coordinate_frame_verified": False, "source_freshness_verified": False,
          "raw_gps_verified": False, "route_anchor_verified": False,
          "route_render_input_allowed": False, "control_input_allowed": False}


def _position(reader: _BoundedReader, manager: int, path_id: int | None, nav_type: int, nav_state: int) -> dict:
  if path_id is None or nav_type != 0 or nav_state not in (1, 3):
    return _position_unavailable("gps_navigation_not_observed")
  reader.stage = "position_flags"
  if reader.read(manager + 0x38, 1) != b"\x01":
    return _position_unavailable("position_missing")
  usable, simulated, source_type = struct.unpack("<III", reader.read(manager + 0x40, 12))
  if usable != 1:
    return _position_unavailable("position_not_usable")
  if simulated != 0:
    return _position_unavailable("position_simulated")
  reader.stage = "position_match_header"
  count = reader.u32(manager + 0x360)
  if not 1 <= count <= MAX_LOCATION_MATCHES:
    return _position_unavailable("position_match_count")
  matches = []
  for index in range(count):
    item = manager + 0x1c8 + index * 0x88
    segment, link, point, on_guide_road, match_path = struct.unpack("<iiiII", reader.read(item + 0x68, 20))
    if match_path == path_id:
      matches.append((index, item, segment, link, point, on_guide_road))
  if not matches:
    return _position_unavailable("position_main_path_match_missing")
  if len(matches) != 1:
    return _position_unavailable("position_main_path_match_ambiguous")
  index, item, segment, link, point, on_guide_road = matches[0]
  if on_guide_road != 1:
    return _position_unavailable("position_not_on_route")
  if min(segment, link, point) < 0:
    return _position_unavailable("position_index_invalid")
  reader.stage = "position_coordinates"
  native = struct.unpack("<ddd", reader.read(manager + 0x60, 24))
  matched = struct.unpack("<ddd", reader.read(item, 24))
  for coordinate in (native, matched):
    if (not all(math.isfinite(value) for value in coordinate)
        or not -180 <= coordinate[0] <= 180 or not -90 <= coordinate[1] <= 90
        or coordinate[:2] == (0.0, 0.0)):
      return _position_unavailable("position_coordinate_invalid")
  reader.stage = "position_source_ticks"
  tick, input_tick = struct.unpack("<QQ", reader.read(manager + 0x1b0, 16))
  if tick == 0 or input_tick == 0:
    return _position_unavailable("position_source_tick_missing")
  return {**_position_unavailable("unverified_position_semantics"), "status": "observed",
          "source": "native_loc_info", "source_type_raw": source_type,
          "coordinate_frame_candidate": "amap_native_map", "source_clock": "unknown",
          "native_st_pos_deg": native[:2], "matched_st_pos_deg": matched[:2],
          "segment_index": segment, "link_index": link, "point_index": point,
          "point_index_source_field": "postCur", "match_index": index, "match_count": count,
          "is_on_guide_road_raw": on_guide_road,
          "tick_time_raw": tick, "input_tick_time_raw": input_tick}


def _observe(reader: _BoundedReader, gbl: int, dice: int, *, include_position: bool = False) -> dict:
  manager, chain = _manager(reader, gbl)
  reader.stage = "main_path_header"
  header = reader.read(manager + 0xb00, 36)
  paths, = struct.unpack_from("<Q", header)
  count, = struct.unpack_from("<I", header, 12)
  main_index, = struct.unpack_from("<I", header, 32)
  nav_type, nav_state = struct.unpack("<II", reader.read(manager + 0x1138, 8))
  if count > MAX_PATHS:
    raise ProbeError("path_count_limit")
  result = {"chain": chain, "header": header, "navi_type": nav_type, "navi_state": nav_state,
            "path_count": count, "main_index": main_index, "path_id": None}
  if include_position:
    result["position"] = _position_unavailable("gps_navigation_not_observed")
  if not count:
    if main_index != 0:
      raise ProbeError("main_index_out_of_bounds")
    return result
  if main_index >= count:
    raise ProbeError("main_index_out_of_bounds")
  if not paths or paths & 7:
    raise ProbeError("unaligned_pointer")
  reader.check(paths, count * 0x50)
  info = paths + main_index * 0x50
  reader.stage = "path_info"
  a = reader.pointer(info + 0x48)
  b = reader.pointer(a + 8)
  drive_path = reader.pointer(b + 0x48)
  reader.stage = "drive_path"
  path_vtable, implementation = struct.unpack("<QQ", reader.read(drive_path, 16))
  if path_vtable != dice + 0x3d28040:
    raise ProbeError("drive_path_vtable_mismatch")
  if reader.immutable_u64(dice + 0x3d28040 + 0x20) != dice + 0x37709a4:
    raise ProbeError("drive_path_getter_mismatch")
  reader.check(dice + 0x37709a4, 4, "x")
  reader.validate_pointer(implementation)
  reader.stage = "drive_path_implementation"
  implementation_header = reader.read(implementation, 24)
  impl_vtable, = struct.unpack_from("<Q", implementation_header)
  if impl_vtable != dice + 0x3d29828:
    raise ProbeError("drive_impl_vtable_mismatch")
  if reader.immutable_u64(dice + 0x3d29828 + 0x20) != dice + 0x379237c:
    raise ProbeError("drive_impl_getter_mismatch")
  reader.check(dice + 0x379237c, 4, "x")
  path_id, = struct.unpack_from("<I", implementation_header, 0x14)
  if not path_id:
    raise ProbeError("invalid_path_id")
  result["path_id"] = path_id
  result["chain"] += (info, a, b, drive_path, implementation)
  if include_position:
    result["position"] = _position(reader, manager, path_id, nav_type, nav_state)
  return result


def probe_snapshot(maps_text: str, read: Callable[[int, int], bytes], *,
                   library_sha256: Mapping[str, str], process_epoch: Callable[[], str],
                   profile: str = SUPPORTED_PROFILE, include_position: bool = False) -> dict:
  started = time.monotonic_ns()
  output = {"schema_version": 1, "profile": SUPPORTED_PROFILE, "status": "unavailable",
            "reason": "unknown", "path_identity_hash": None, "navigation_state": "unknown",
            "navigation_running_observed": False, "sample_consistent": False,
            "gps_navigation_observed": False,
            "active_route_verified": False, "control_input_allowed": False,
            "source": "bounded_native_memory", "atomic_snapshot": False}
  if include_position is True:
    output["_position"] = _position_unavailable("snapshot_unavailable")
  reader = None
  try:
    if type(include_position) is not bool:
      raise ProbeError("invalid_position_request")
    if profile != SUPPORTED_PROFILE or any(library_sha256.get(key) != value for key, value in EXPECTED_HASHES.items()):
      raise ProbeError("unsupported_binary_profile")
    maps = parse_maps(maps_text)
    gbl, dice = _library_base(maps, "libGbl.so"), _library_base(maps, "libAutoDice.so")
    try:
      before = process_epoch()
    except Exception:
      raise ProbeError("process_epoch_failed") from None
    if not isinstance(before, str) or not before or len(before) > 256:
      raise ProbeError("invalid_process_epoch")
    reader = _BoundedReader(maps, read)
    first = _observe(reader, gbl, dice, include_position=include_position)
    second = _observe(reader, gbl, dice, include_position=include_position)
    try:
      after = process_epoch()
    except Exception:
      raise ProbeError("process_epoch_failed") from None
    if before != after:
      raise ProbeError("process_epoch_changed")
    if first != second:
      raise ProbeError("selection_changed_during_read")
    state = STATE_NAMES.get(second["navi_state"], "unknown")
    path_id = second["path_id"]
    output.update(status="observed", reason="consistent_read_only_observation", sample_consistent=True,
                  navigation_state=state, navi_type_raw=second["navi_type"], navi_state_raw=second["navi_state"],
                  main_index=second["main_index"], path_count=second["path_count"],
                  navigation_running_observed=path_id is not None and second["navi_state"] in (1, 3),
                  gps_navigation_observed=path_id is not None and second["navi_state"] in (1, 3) and second["navi_type"] == 0,
                  path_identity_hash=identity_hash(path_id, before) if path_id is not None else None,
                  process_epoch_hash=hashlib.sha256(before.encode("utf-8")).hexdigest())
    if include_position:
      output["_position"] = second["position"]
      if output["_position"]["status"] == "observed":
        output["_position"]["path_identity_hash"] = output["path_identity_hash"]
  except ProbeError as error:
    output["reason"] = str(error)
    output["failure_stage"] = reader.stage if reader is not None else "profile_or_maps"
  output["read_calls"] = reader.calls if reader is not None else 0
  output["read_bytes"] = reader.bytes if reader is not None else 0
  finished = time.monotonic_ns()
  output["observation_started_monotonic_ns"] = started
  output["observation_finished_monotonic_ns"] = finished
  output["observation_window_ms"] = round((finished - started) / 1_000_000, 3)
  return output
