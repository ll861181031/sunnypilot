import json
import struct
import zlib

import pytest
import zstandard

from tools.cplink_shim import amap_route_cache as codec


def varint(value):
  result = bytearray()
  while value > 127:
    result.append((value & 127) | 128)
    value >>= 7
  return bytes(result + bytes([value]))


def scalar(tag, value, wire=0):
  return varint(tag << 3 | wire) + (varint(value) if wire == 0 else struct.pack("<I", value))


def message(tag, value):
  return varint(tag << 3 | 2) + varint(len(value)) + value


def sint(value):
  return varint((value << 1) ^ (value >> 31))


def shape(points, *, packed=True):
  xs, ys = [], []
  previous = (0, 0)
  for x, y in points:
    xs.append(x - previous[0])
    ys.append(y - previous[1])
    previous = x, y
  if packed:
    return message(1, b"".join(sint(v) for v in xs)) + message(2, b"".join(sint(v) for v in ys))
  return b"".join(varint(tag << 3) + sint(v) for tag, values in ((1, xs), (2, ys)) for v in values)


POINTS = ((36_000_000, 72_000_000), (36_000_360, 71_999_640), (36_000_720, 72_000_000))


def segment(points=POINTS, *, start=0, count=None, packed=True, include_link=True, shape_bytes=None):
  span = scalar(1, start) + scalar(2, len(points) if count is None else count)
  link = scalar(1, 0) + scalar(2, 30_001) + message(8, span)
  return (scalar(1, 2) + scalar(2, 7) + (message(3, link) if include_link else b"")
          + message(4, shape(points, packed=packed) if shape_bytes is None else shape_bytes))


def route(path_id=123, *, segments=None, distance=30_001, time=45):
  segments = [segment()] if segments is None else segments
  return (scalar(1, distance) + scalar(2, 0) + scalar(6, path_id, 5) + scalar(7, time)
          + b"".join(message(10, value) for value in segments))


def block(kind, routes, ids, *, identity=b"synthetic-route-identity-0000000"):
  header = scalar(1, 1) + scalar(2, 1) + scalar(3, 0) + message(5, identity)
  if kind == 1:
    content = message(2, b"".join(message(7, value) for value in routes))
  elif kind == 2:
    content = message(2, b"".join(message(1, scalar(1, value, 5)) for value in ids))
  else:
    content = b"".join(message(2, scalar(1, value, 5)) for value in ids)
  return message(1, header) + content


def packet(chunks):
  body = b""
  for kind, raw in chunks:
    compressed = zstandard.ZstdCompressor().compress(raw)
    body += struct.pack("<HHII20x", 2, kind, len(compressed), len(raw)) + compressed
  return struct.pack("<HHHI22x", 31, 0, len(chunks), 32 + len(body)) + body


def outer(first, second, third=b""):
  body = b"".join(struct.pack("<I", len(part)) + part for part in (first, second, third))
  return b"BL-CRRD\0" + struct.pack("<II", len(body), zlib.crc32(body)) + body


def cache(*, routes=None, ids=None, detail_ids=None, identities=None, chunks=None):
  routes = [route()] if routes is None else routes
  ids = [123] if ids is None else ids
  detail_ids = ids if detail_ids is None else detail_ids
  identities = {} if identities is None else identities
  chunks = {kind: block(kind, routes, ids if kind == 1 else detail_ids,
                       **({"identity": identities[kind]} if kind in identities else {})) for kind in (1, 2, 3)} if chunks is None else chunks
  first = packet([(1, chunks[1])])
  primary = struct.pack("<IHHHIII", 0x55EE55EE, 0, 1, 0, 16, 16, len(first)) + b"x" * 32 + first
  return outer(primary, packet([(2, chunks[2]), (3, chunks[3])]))


def decode(data):
  return codec.decode_cache(data, profile=codec.SUPPORTED_PROFILE)


def test_native_field_semantics_first_absolute_then_zigzag_deltas():
  decoded = decode(cache())
  result = decoded.routes[0]
  assert result.path_id == 123
  assert result.distance_centi_m == 30_001
  assert result.total_distance_m == 300
  assert result.travel_time_s == 45
  assert result.points_native == POINTS
  assert result.points_degrees == ((10., 20.), (10.0001, 19.9999), (10.0002, 20.))
  assert result.segments[0].main_action == 2
  assert result.segments[0].assistant_action == 7
  assert result.segments[0].distance_centi_m == 30_001
  assert decoded.coordinate_frame == "amap_native_map"
  assert not decoded.coordinate_datum_verified
  assert not decoded.current_route_verified


def test_unpacked_coordinates_and_segment_join():
  second = ((36_000_720, 72_000_000), (36_000_800, 72_000_080))
  result = decode(cache(routes=[route(segments=[segment(packed=False), segment(second)])])).routes[0]
  assert result.points_native == POINTS + second[1:]
  assert len(result.points_native) == 4


def test_report_and_repr_exclude_coordinates_and_raw_path_ids():
  data = cache(routes=[route(path_id=0x12345678)], ids=[0x12345678])
  result = decode(data)
  printed = json.dumps(codec.inspect_cache(data, profile=codec.SUPPORTED_PROFILE)) + repr(result)
  assert "36000360" not in printed
  assert "10.0001" not in printed
  assert str(0x12345678) not in printed
  assert "control_input_generated" in printed


@pytest.mark.parametrize("data", [b"", b"x", b"BL-CRRD\0", b"not a route cache" * 4])
def test_invalid_cache_envelope(data):
  with pytest.raises(codec.RouteCacheDecodeError):
    decode(data)


@pytest.mark.parametrize("offset", [0, 8, 12, 20, -1])
def test_modified_or_truncated_outer_cache_fails(offset):
  data = bytearray(cache())
  data[offset] ^= 1
  with pytest.raises(codec.RouteCacheDecodeError):
    decode(bytes(data))


def test_unknown_profile_rejected_before_parsing():
  with pytest.raises(codec.RouteCacheDecodeError, match="unsupported_profile"):
    codec.decode_cache(b"", profile="unknown")


@pytest.mark.parametrize("ids", [[456], [123, 456], []])
def test_all_three_blocks_must_have_identical_path_order(ids):
  with pytest.raises(codec.RouteCacheDecodeError):
    decode(cache(detail_ids=ids))


def test_duplicate_route_ids_fail():
  with pytest.raises(codec.RouteCacheDecodeError, match="duplicate_path_id"):
    decode(cache(routes=[route(), route()], ids=[123, 123]))


def test_block_header_identity_must_match():
  with pytest.raises(codec.RouteCacheDecodeError, match="block_identity_mismatch"):
    decode(cache(identities={2: b"a" * 32}))


@pytest.mark.parametrize("segments", [[segment(POINTS[:1])], [segment(include_link=False)],
                                    [segment(count=1)], [segment(start=1, count=3)], [segment(start=0xFFFFFFFF)],
                                    [segment(count=2)], [segment(), segment(POINTS)]] )
def test_partial_or_discontinuous_geometry_is_rejected(segments):
  with pytest.raises(codec.RouteCacheDecodeError):
    decode(cache(routes=[route(segments=segments)]))


@pytest.mark.parametrize("points", [((648_000_001, 0), (648_000_002, 0)),
                                   ((0, 324_000_001), (1, 324_000_002)),
                                   ((-648_000_001, 0), (-648_000_002, 0))])
def test_world_coordinate_bounds(points):
  with pytest.raises(codec.RouteCacheDecodeError, match="coordinate_range"):
    decode(cache(routes=[route(segments=[segment(points)])]))


@pytest.mark.parametrize("raw", [b"\x00", b"\x08\x80", b"\x08" + b"\x80" * 10 + b"\x00",
                                b"\x08" + b"\xff" * 9 + b"\x02", b"\x0a\x05x", b"\x0dxx", b"\x0b",
                                b"\x08\x80\x00"])
def test_wire_parser_is_bounded_and_rejects_malformed_fields(raw):
  with pytest.raises(codec.RouteCacheDecodeError):
    codec._wire(raw)


def test_duplicate_required_field_is_rejected():
  with pytest.raises(codec.RouteCacheDecodeError, match="required_field"):
    decode(cache(routes=[route() + scalar(6, 123, 5)]))


def test_coordinate_axis_count_mismatch_is_rejected():
  bad = message(1, sint(0) + sint(10)) + message(2, sint(0))
  with pytest.raises(codec.RouteCacheDecodeError, match="coordinate_count"):
    decode(cache(routes=[route(segments=[segment(shape_bytes=bad)])]))


def test_coordinate_sint32_overflow_is_rejected():
  bad = message(1, varint(1 << 32) + sint(1)) + message(2, sint(0) + sint(1))
  with pytest.raises(codec.RouteCacheDecodeError, match="uint32_overflow"):
    decode(cache(routes=[route(segments=[segment(shape_bytes=bad)])]))


@pytest.mark.parametrize("offset,value", [(0, 30), (2, 1), (4, 0), (6, 0), (10, 1), (32, 1), (34, 9), (44, 1)])
def test_packet_headers_reject_unsupported_or_inconsistent_values(offset, value):
  data = bytearray(packet([(1, block(1, [route()], [123]))]))
  data[offset] = value
  with pytest.raises(codec.RouteCacheDecodeError):
    codec._packet(bytes(data))


def test_zstd_declared_size_mismatch_is_rejected():
  data = bytearray(packet([(1, b"x" * 100)]))
  struct.pack_into("<I", data, 40, 99)
  with pytest.raises(codec.RouteCacheDecodeError, match="zstd_declared"):
    codec._packet(bytes(data))


def test_zstd_trailing_bytes_are_rejected():
  data = bytearray(packet([(1, b"x" * 100)])) + b"extra"
  struct.pack_into("<I", data, 6, len(data))
  struct.pack_into("<I", data, 36, len(data) - 64)
  with pytest.raises(codec.RouteCacheDecodeError, match="zstd"):
    codec._packet(bytes(data))


def test_limits_checked_before_large_allocations(monkeypatch):
  data = cache()
  monkeypatch.setattr(codec, "MAX_CACHE_BYTES", 1)
  with pytest.raises(codec.RouteCacheDecodeError, match="cache_size_limit"):
    decode(data)
  monkeypatch.setattr(codec, "MAX_CACHE_BYTES", len(data))
  monkeypatch.setattr(codec, "MAX_BLOCK_BYTES", 16)
  with pytest.raises(codec.RouteCacheDecodeError, match="block_size_limit"):
    decode(data)


def test_point_limit(monkeypatch):
  data = cache()
  monkeypatch.setattr(codec, "MAX_POINTS", 2)
  with pytest.raises(codec.RouteCacheDecodeError, match="point_limit"):
    decode(data)


def test_empty_route_set_is_rejected():
  with pytest.raises(codec.RouteCacheDecodeError):
    decode(cache(routes=[], ids=[]))
