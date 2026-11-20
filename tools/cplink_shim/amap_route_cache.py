#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import struct
import zlib

import zstandard


SUPPORTED_PROFILE = "amapauto-arm64-934396f73adf"
COORDINATE_SCALE = 3_600_000
MAX_CACHE_BYTES = 16 * 1024 * 1024
MAX_BLOCK_BYTES = 32 * 1024 * 1024
MAX_FIELDS = 500_000
MAX_ROUTES = 16
MAX_SEGMENTS = 8192
MAX_POINTS = 500_000


class RouteCacheDecodeError(ValueError):
  pass


@dataclass(frozen=True)
class RouteLink:
  link_id_delta: int
  distance_centi_m: int
  point_start: int
  point_count: int


@dataclass(frozen=True)
class RouteSegment:
  main_action: int
  assistant_action: int
  points_native: tuple[tuple[int, int], ...] = field(repr=False)
  links: tuple[RouteLink, ...]

  @property
  def distance_centi_m(self) -> int:
    return sum(link.distance_centi_m for link in self.links)


@dataclass(frozen=True)
class CachedRoute:
  path_id: int = field(repr=False)
  distance_centi_m: int
  travel_time_s: int
  segments: tuple[RouteSegment, ...]

  @property
  def total_distance_m(self) -> int:
    return self.distance_centi_m // 100

  @property
  def points_native(self) -> tuple[tuple[int, int], ...]:
    result = []
    for segment in self.segments:
      points = segment.points_native
      result.extend(points[1:] if result and points and result[-1] == points[0] else points)
    return tuple(result)

  @property
  def points_degrees(self) -> tuple[tuple[float, float], ...]:
    return tuple((x / COORDINATE_SCALE, y / COORDINATE_SCALE) for x, y in self.points_native)


@dataclass(frozen=True)
class DecodedCache:
  profile: str
  sha256: str
  routes: tuple[CachedRoute, ...]
  block_sizes: tuple[int, ...]
  coordinate_frame: str = "amap_native_map"
  coordinate_datum_verified: bool = False
  current_route_verified: bool = False


def _varint(data: bytes, offset: int) -> tuple[int, int]:
  value = 0
  for index in range(10):
    if offset >= len(data):
      raise RouteCacheDecodeError("truncated_varint")
    byte = data[offset]
    offset += 1
    if index == 9 and byte > 1:
      raise RouteCacheDecodeError("varint_overflow")
    value |= (byte & 127) << (7 * index)
    if byte < 128:
      if index and byte == 0:
        raise RouteCacheDecodeError("noncanonical_varint")
      return value, offset
  raise RouteCacheDecodeError("varint_overflow")


def _wire(data: bytes) -> dict[int, list[tuple[int, int | bytes]]]:
  fields = {}
  offset = 0
  count = 0
  while offset < len(data):
    count += 1
    if count > MAX_FIELDS:
      raise RouteCacheDecodeError("field_limit")
    key, offset = _varint(data, offset)
    tag, wire = key >> 3, key & 7
    if not 0 < tag < 1 << 29:
      raise RouteCacheDecodeError("invalid_field_number")
    if wire == 0:
      value, offset = _varint(data, offset)
    elif wire in (1, 5):
      size = 8 if wire == 1 else 4
      if offset + size > len(data):
        raise RouteCacheDecodeError("truncated_fixed_field")
      value = int.from_bytes(data[offset:offset + size], "little")
      offset += size
    elif wire == 2:
      size, offset = _varint(data, offset)
      if offset + size > len(data):
        raise RouteCacheDecodeError("truncated_bytes_field")
      value = data[offset:offset + size]
      offset += size
    else:
      raise RouteCacheDecodeError("unsupported_wire_type")
    fields.setdefault(tag, []).append((wire, value))
  return fields


def _one(fields: dict, tag: int, wire: int, *, default=None):
  values = fields.get(tag, [])
  if not values and default is not None:
    return default
  if len(values) != 1 or values[0][0] != wire:
    raise RouteCacheDecodeError("required_field_or_wire_type")
  return values[0][1]


def _messages(fields: dict, tag: int, limit: int) -> list[dict]:
  values = fields.get(tag, [])
  if not values or len(values) > limit or any(wire != 2 for wire, _ in values):
    raise RouteCacheDecodeError("message_count_or_wire_type")
  return [_wire(value) for _, value in values]


def _uint32(value: int) -> int:
  if not 0 <= value <= 0xFFFFFFFF:
    raise RouteCacheDecodeError("uint32_overflow")
  return value


def _sint32(value: int) -> int:
  _uint32(value)
  return (value >> 1) ^ -(value & 1)


def _packed(fields: dict, tag: int) -> list[int]:
  result = []
  for wire, value in fields.get(tag, []):
    if wire == 0:
      result.append(_sint32(value))
    elif wire == 2:
      offset = 0
      while offset < len(value):
        number, offset = _varint(value, offset)
        result.append(_sint32(number))
        if len(result) > MAX_POINTS:
          raise RouteCacheDecodeError("point_limit")
    else:
      raise RouteCacheDecodeError("coordinate_wire_type")
    if len(result) > MAX_POINTS:
      raise RouteCacheDecodeError("point_limit")
  return result


def _shape(fields: dict) -> tuple[tuple[int, int], ...]:
  xs, ys = _packed(fields, 1), _packed(fields, 2)
  if len(xs) < 2 or len(xs) != len(ys):
    raise RouteCacheDecodeError("coordinate_count")
  x = y = 0
  points = []
  for dx, dy in zip(xs, ys, strict=True):
    x, y = x + dx, y + dy
    if not -180 * COORDINATE_SCALE <= x <= 180 * COORDINATE_SCALE or not -90 * COORDINATE_SCALE <= y <= 90 * COORDINATE_SCALE:
      raise RouteCacheDecodeError("coordinate_range")
    points.append((x, y))
  return tuple(points)


def _segment(fields: dict) -> RouteSegment:
  points = _shape(_wire(_one(fields, 4, 2)))
  links = []
  covered = bytearray(len(points))
  for link in _messages(fields, 3, MAX_POINTS):
    span = _wire(_one(link, 8, 2))
    start = _uint32(_one(span, 1, 0, default=0))
    count = _uint32(_one(span, 2, 0))
    if start < 0 or count < 2 or start + count > len(points):
      raise RouteCacheDecodeError("link_point_range")
    covered[start:start + count] = b"\1" * count
    links.append(RouteLink(_sint64(_one(link, 1, 0, default=0)), _uint32(_one(link, 2, 0, default=0)), start, count))
  if not all(covered):
    raise RouteCacheDecodeError("uncovered_route_points")
  return RouteSegment(_uint32(_one(fields, 1, 0, default=0)), _uint32(_one(fields, 2, 0, default=0)), points, tuple(links))


def _sint64(value: int) -> int:
  return (value >> 1) ^ -(value & 1)


def _block(data: bytes, kind: int) -> tuple[bytes, dict]:
  fields = _wire(data)
  header = _wire(_one(fields, 1, 2))
  if _one(header, 3, 0, default=0) != 0:
    raise RouteCacheDecodeError("route_server_error")
  token = _one(header, 5, 2)
  if len(token) != 32:
    raise RouteCacheDecodeError("header_identity_format")
  return token, fields if kind == 3 else _wire(_one(fields, 2, 2))


def _packet(data: bytes) -> dict[int, bytes]:
  if len(data) < 32:
    raise RouteCacheDecodeError("packet_header_truncated")
  version, flags, count = struct.unpack_from("<HHH", data)
  total = struct.unpack_from("<I", data, 6)[0]
  if version != 31 or flags != 0 or not 1 <= count <= 3 or total != len(data) or any(data[10:32]):
    raise RouteCacheDecodeError("packet_header_mismatch")
  offset = 32
  result = {}
  expanded = 0
  for _ in range(count):
    if offset + 32 > len(data):
      raise RouteCacheDecodeError("chunk_header_truncated")
    compression, kind, size, output_size = struct.unpack_from("<HHII", data, offset)
    if compression != 2 or kind not in (1, 2, 3) or kind in result or any(data[offset + 12:offset + 32]):
      raise RouteCacheDecodeError("chunk_header_mismatch")
    expanded += output_size
    if not size or not output_size or expanded > MAX_BLOCK_BYTES:
      raise RouteCacheDecodeError("block_size_limit")
    offset += 32
    if offset + size > len(data):
      raise RouteCacheDecodeError("compressed_block_truncated")
    compressed = data[offset:offset + size]
    try:
      if zstandard.frame_content_size(compressed) != output_size:
        raise RouteCacheDecodeError("zstd_declared_size_mismatch")
      raw = zstandard.ZstdDecompressor().decompress(compressed, max_output_size=output_size, allow_extra_data=False)
    except zstandard.ZstdError:
      raise RouteCacheDecodeError("invalid_zstd_block") from None
    if len(raw) != output_size:
      raise RouteCacheDecodeError("zstd_decoded_size_mismatch")
    result[kind] = raw
    offset += size
  if offset != len(data):
    raise RouteCacheDecodeError("packet_trailing_bytes")
  return result


def decode_cache(data: bytes, *, profile: str) -> DecodedCache:
  if profile != SUPPORTED_PROFILE:
    raise RouteCacheDecodeError("unsupported_profile")
  if len(data) > MAX_CACHE_BYTES:
    raise RouteCacheDecodeError("cache_size_limit")
  if len(data) < 28 or data[:8] != b"BL-CRRD\0":
    raise RouteCacheDecodeError("cache_magic_or_size")
  size, checksum = struct.unpack_from("<II", data, 8)
  if size != len(data) - 16 or zlib.crc32(data[16:]) != checksum:
    raise RouteCacheDecodeError("cache_length_or_crc")
  parts = []
  offset = 16
  for _ in range(3):
    if offset + 4 > len(data):
      raise RouteCacheDecodeError("outer_part_truncated")
    size = struct.unpack_from("<I", data, offset)[0]
    offset += 4
    if offset + size > len(data):
      raise RouteCacheDecodeError("outer_part_truncated")
    parts.append(data[offset:offset + size])
    offset += size
  if offset != len(data) or parts[2]:
    raise RouteCacheDecodeError("unsupported_outer_parts")
  primary = parts[0]
  if len(primary) < 22:
    raise RouteCacheDecodeError("primary_header_truncated")
  magic, flags, version, reserved, first, second, third = struct.unpack_from("<IHHHIII", primary)
  if magic != 0x55EE55EE or flags or version != 1 or reserved or 22 + first + second + third != len(primary):
    raise RouteCacheDecodeError("primary_header_mismatch")
  if not first or not second or first % 16 or second % 16:
    raise RouteCacheDecodeError("metadata_envelope_size")
  main = _packet(primary[22 + first + second:])
  extra = _packet(parts[1])
  if set(main) != {1} or set(extra) != {2, 3}:
    raise RouteCacheDecodeError("route_block_set")
  blocks = {**main, **extra}
  if sum(len(value) for value in blocks.values()) > MAX_BLOCK_BYTES:
    raise RouteCacheDecodeError("block_size_limit")
  decoded = {kind: _block(value, kind) for kind, value in blocks.items()}
  if len({token for token, _ in decoded.values()}) != 1:
    raise RouteCacheDecodeError("block_identity_mismatch")
  routes = []
  for route in _messages(decoded[1][1], 7, MAX_ROUTES):
    segments = tuple(_segment(segment) for segment in _messages(route, 10, MAX_SEGMENTS))
    point_count = sum(len(segment.points_native) for segment in segments)
    if point_count > MAX_POINTS:
      raise RouteCacheDecodeError("point_limit")
    for previous, current in zip(segments, segments[1:], strict=False):
      if previous.points_native[-1] != current.points_native[0]:
        raise RouteCacheDecodeError("segment_discontinuity")
    routes.append(CachedRoute(_uint32(_one(route, 6, 5)), _uint32(_one(route, 1, 0)), _uint32(_one(route, 7, 0)), segments))
  ids = [route.path_id for route in routes]
  if len(set(ids)) != len(ids):
    raise RouteCacheDecodeError("duplicate_path_id")
  for kind in (2, 3):
    detail_ids = [_uint32(_one(route, 1, 5)) for route in _messages(decoded[kind][1], 1 if kind == 2 else 2, MAX_ROUTES)]
    if detail_ids != ids:
      raise RouteCacheDecodeError("detail_path_ids_mismatch")
  return DecodedCache(profile, hashlib.sha256(data).hexdigest(), tuple(routes), tuple(len(blocks[kind]) for kind in (1, 2, 3)))


def inspect_cache(data: bytes, *, profile: str) -> dict:
  cache = decode_cache(data, profile=profile)
  return {"schema_version": 1, "cache_valid": True, "cache_sha256": cache.sha256, "profile": profile,
          "coordinate_scale": COORDINATE_SCALE, "coordinate_frame": cache.coordinate_frame,
          "coordinate_datum_verified": cache.coordinate_datum_verified, "current_route_verified": False,
          "control_input_generated": False, "block_sizes": cache.block_sizes,
          "routes": [{"index": index, "path_id_sha256": hashlib.sha256(struct.pack("<I", route.path_id)).hexdigest(),
                      "distance_centi_m": route.distance_centi_m, "total_distance_m": route.total_distance_m,
                      "travel_time_s": route.travel_time_s, "segments": len(route.segments),
                      "links": sum(len(segment.links) for segment in route.segments),
                      "stored_points": sum(len(segment.points_native) for segment in route.segments),
                      "joined_points": len(route.points_native)} for index, route in enumerate(cache.routes)]}


def main() -> None:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("cache_file", type=Path)
  parser.add_argument("--profile", choices=[SUPPORTED_PROFILE], required=True)
  args = parser.parse_args()
  try:
    with args.cache_file.open("rb") as handle:
      data = handle.read(MAX_CACHE_BYTES + 1)
    report = inspect_cache(data, profile=args.profile)
  except (RouteCacheDecodeError, OSError) as error:
    reason = str(error) if isinstance(error, RouteCacheDecodeError) else "file_read_error"
    print(json.dumps({"cache_valid": False, "reason": reason, "control_input_generated": False}))
    raise SystemExit(1) from None
  print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
  main()
