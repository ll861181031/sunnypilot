"""MVT road decoding and PMTiles reading for the basemap path.

The fixtures are built here with ~30 lines of varint/protobuf encoding instead of shipping
binary blobs: a `.pmtiles` fixture in git would be opaque, and this way the test states
exactly which road and which archive layout the decoder is expected to accept.
"""
import gzip
import struct

import numpy as np
import pytest

from openpilot.selfdrive.navigation.local_tiles import LocalTiles
from openpilot.selfdrive.navigation.pmtiles import PMTiles, decode_directory, read_varint, tile_id
from openpilot.selfdrive.navigation.renderer import MAJOR, STREET, MapRenderer, road_style, stroke_widths
from openpilot.selfdrive.navigation.vector_tile import Road, decode_lines, decode_roads
from openpilot.selfdrive.navigation.map_draw import mercator, ROUTE_INTENSITY, SIZE

Z = 15


def varint(value: int) -> bytes:
  out = bytearray()
  while True:
    byte = value & 0x7F
    value >>= 7
    out.append(byte | (0x80 if value else 0))
    if not value:
      return bytes(out)


def zigzag(value: int) -> int:
  return (value << 1) ^ (value >> 31)


def keyed(field: int, wire: int) -> bytes:
  return varint((field << 3) | wire)


def message(field: int, payload: bytes) -> bytes:
  return keyed(field, 2) + varint(len(payload)) + payload


def text(field: int, value: str) -> bytes:
  return message(field, value.encode())


def packed(field: int, values) -> bytes:
  return message(field, b"".join(varint(v) for v in values))


def mvt_tile(x_frac=0.5, detail="motorway", kind="highway", bridge=False, tunnel=False, name="roads") -> bytes:
  keys = ["kind", "kind_detail", "is_bridge", "is_tunnel"]
  values = [text(1, kind), text(1, detail), keyed(7, 0) + varint(int(bridge)), keyed(7, 0) + varint(int(tunnel))]

  x = int(round(x_frac * 4096))
  geometry = packed(4, [9, zigzag(x), zigzag(0), 10, zigzag(0), zigzag(4096)])  # (x,0)->(x,4096)
  feature = packed(2, [0, 0, 1, 1, 2, 2, 3, 3]) + keyed(3, 0) + varint(2) + geometry
  layer = (text(1, name) + message(2, feature) +
           b"".join(text(3, k) for k in keys) +
           b"".join(message(4, v) for v in values) +
           keyed(5, 0) + varint(4096) + keyed(15, 0) + varint(2))
  return message(3, layer)


class TestVectorTile:
  def test_decodes_a_road_line(self):
    roads = decode_roads(mvt_tile())
    assert len(roads) == 1
    road = roads[0]
    assert road.kind == "highway" and road.detail == "motorway"
    assert road.points.shape == (2, 2)
    assert road.points[0] == pytest.approx([0.5, 0.0]) and road.points[1] == pytest.approx([0.5, 1.0])

  def test_reads_bridge_and_tunnel_flags(self):
    assert decode_roads(mvt_tile(bridge=True))[0].bridge is True
    assert decode_roads(mvt_tile(tunnel=True))[0].tunnel is True

  def test_ignores_other_layers(self):
    assert decode_roads(mvt_tile(name="other")) == []

  def test_rejects_a_broken_line_geometry(self):
    with pytest.raises(ValueError):
      decode_lines([9, zigzag(0), zigzag(0), 10, zigzag(1)])  # LineTo missing its y

  def test_road_styles_come_from_kind_detail(self):
    assert road_style(Road(np.zeros((2, 2)), "highway", "motorway", False, False)) == MAJOR
    assert road_style(Road(np.zeros((2, 2)), "highway", "residential", False, False)) == STREET
    assert road_style(Road(np.zeros((2, 2)), "highway", "platform", False, False)) is None

  def test_stroke_widths_widen_with_style_and_hide_tunnel_casings(self):
    ground = stroke_widths(16.0, np.array([MAJOR]), np.array([0]))
    tunnel = stroke_widths(16.0, np.array([MAJOR]), np.array([-1]))
    assert ground[0] > tunnel[0]


def pmtiles_archive(path, tiles, x: int = 100, y: int = 100) -> None:
  """A PMTiles v3 archive holding `tiles`: either one tile (placed at x, y) or {(x, y): tile}."""
  if not isinstance(tiles, dict):
    tiles = {(x, y): tiles}
  entries = sorted((tile_id(Z, tx, ty), gzip.compress(tile)) for (tx, ty), tile in tiles.items())
  ids = [entry[0] for entry in entries]
  lengths = [len(entry[1]) for entry in entries]
  offsets, running = [], 0
  for length in lengths:
    offsets.append(running + 1)  # stored value 0 means "implicitly after the previous entry"
    running += length
  deltas, previous = [], 0
  for value in ids:
    deltas.append(value - previous)
    previous = value
  directory = varint(len(entries))
  for column in (deltas, [1] * len(entries), lengths, offsets):
    directory += b"".join(varint(value) for value in column)
  root = gzip.compress(directory)
  data = b"".join(entry[1] for entry in entries)
  header = bytearray(127)
  header[0:8] = b"PMTiles\x03"
  struct.pack_into("<8Q", header, 8, 127, len(root), 0, 0, 0, 0, 127 + len(root), len(data))
  header[97:99] = b"\x02\x02"
  header[99] = 1
  header[100] = Z
  header[101] = Z
  path.write_bytes(bytes(header) + root + data)


class TestPMTiles:
  def test_tile_ids_are_unique_per_zoom(self):
    ids = {tile_id(1, x, y) for x in range(2) for y in range(2)}
    assert len(ids) == 4
    assert tile_id(0, 0, 0) == 0

  def test_directory_round_trip(self):
    # count=2; ids (delta): 0, 2; runs: 1, 1; lengths: 2, 7; offsets (stored +1): 1, 1
    encoded = (varint(2) + varint(0) + varint(2) + varint(1) + varint(1) +
               varint(2) + varint(7) + varint(1) + varint(1))
    ids, entries = decode_directory(encoded)
    assert ids == [0, 2]
    assert entries[0] == (1, 2, 0) and entries[1] == (1, 7, 0)

  def test_reads_a_tile_from_a_local_archive(self, tmp_path):
    path = tmp_path / "base.pmtiles"
    pmtiles_archive(path, mvt_tile())
    archive = PMTiles(LocalTiles(path))
    try:
      assert archive.min_zoom == archive.max_zoom == Z
      tile = archive.get(Z, 100, 100)
      assert tile is not None and decode_roads(tile)
      assert archive.get(Z, 0, 0) is None
    finally:
      archive.close()

  def test_rejects_a_truncated_archive(self, tmp_path):
    path = tmp_path / "bad.pmtiles"
    path.write_bytes(b"PMTiles\x03" + b"\x00" * 20)
    with pytest.raises(ValueError):
      PMTiles(LocalTiles(path))

  def test_varint_is_little_endian_groups(self):
    value, offset = read_varint(varint(300), 0)
    assert value == 300 and offset == 2


class _FakeArchive:
  def __init__(self, tile: bytes | None):
    self.tile = tile
    self.min_zoom = self.max_zoom = Z
    self.prefetched = []
    self.closed = False

  def get(self, z, x, y):
    return self.tile

  def prefetch(self, tiles):
    self.prefetched.extend(tiles)

  def close(self):
    self.closed = True


def tile_and_fraction(longitude: float, latitude: float) -> tuple[int, int, float, float]:
  """(tile x, tile y, fractional x, fractional y) of a position at the renderer's zoom."""
  mx, my = mercator(np.array([[longitude, latitude]]))[0]
  scaled_x, scaled_y = mx * (1 << Z), my * (1 << Z)
  return int(scaled_x), int(scaled_y), float(scaled_x % 1), float(scaled_y % 1)


EGO_LON, EGO_LAT = -122.4, 37.8
_, _, FX, _ = tile_and_fraction(EGO_LON, EGO_LAT)


ROUTE = np.array([[-122.4, 37.8], [-122.4, 37.8005]])


class TestMapRenderer:
  def test_renders_roads_and_route(self):
    route = np.array([[EGO_LON, EGO_LAT], [EGO_LON, EGO_LAT + 0.0005]])
    renderer = MapRenderer(_FakeArchive(mvt_tile(x_frac=FX)))
    image, valid = renderer.render(EGO_LON, EGO_LAT, 0.0, route)
    assert image.shape == (SIZE, SIZE)
    assert (image == 255).any(), "the basemap road must be drawn"
    assert (image == ROUTE_INTENSITY).any(), "the route must be drawn over it"
    assert valid is True

  def test_missing_tile_is_not_valid(self):
    renderer = MapRenderer(_FakeArchive(None))
    _, valid = renderer.render(EGO_LON, EGO_LAT, 0.0, ROUTE)
    assert valid is False

  def test_a_road_far_from_the_ego_is_not_valid(self):
    # The archive puts the road 15% of a tile (~145 m) east of the ego: past the 100 m gate.
    renderer = MapRenderer(_FakeArchive(mvt_tile(x_frac=FX + 0.15)))
    _, valid = renderer.render(EGO_LON, EGO_LAT, 0.0, ROUTE)
    assert valid is False

  def test_zoom_is_clamped_to_the_archive(self):
    archive = _FakeArchive(mvt_tile())
    assert MapRenderer(archive).zoom == Z

  def test_prefetch_and_close_reach_the_archive(self):
    archive = _FakeArchive(mvt_tile())
    renderer = MapRenderer(archive)
    renderer.prefetch(ROUTE)
    renderer.close()
    assert archive.prefetched and archive.closed


class TestMapsdTiles:
  """mapsd picks up a local archive when it exists and falls back to route-only when not."""

  def test_no_archive_means_route_only(self, tmp_path, monkeypatch):
    from openpilot.selfdrive.navigation import mapsd
    monkeypatch.setenv(mapsd.TILES_ENV, str(tmp_path / "missing.pmtiles"))
    assert mapsd.make_renderer() is None

  def test_local_archive_is_loaded(self, tmp_path, monkeypatch):
    from openpilot.selfdrive.navigation import mapsd
    path = tmp_path / "base.pmtiles"
    tx, ty, _, _ = tile_and_fraction(EGO_LON, EGO_LAT)
    pmtiles_archive(path, {(tx + dx, ty + dy): mvt_tile(x_frac=FX) for dx in (-2, -1, 0, 1, 2) for dy in (-2, -1, 0, 1, 2)})
    monkeypatch.setenv(mapsd.TILES_ENV, str(path))
    renderer = mapsd.make_renderer()
    assert renderer is not None
    renderer.close()

  def test_mapsd_renders_the_basemap_when_available(self, tmp_path, monkeypatch):
    import time
    from openpilot.selfdrive.navigation import mapsd
    monkeypatch.setattr(mapsd, "cplink_navi_active", lambda: False)

    path = tmp_path / "base.pmtiles"
    tx, ty, _, _ = tile_and_fraction(EGO_LON, EGO_LAT)
    pmtiles_archive(path, {(tx + dx, ty + dy): mvt_tile(x_frac=FX) for dx in (-2, -1, 0, 1, 2) for dy in (-2, -1, 0, 1, 2)})
    monkeypatch.setenv(mapsd.TILES_ENV, str(path))

    class _Gps:
      hasFix = True
      longitude, latitude, speed = EGO_LON, EGO_LAT, 8.0

    class _Route:
      coordinates = [type("C", (), {"longitude": EGO_LON, "latitude": EGO_LAT + i * 0.0002})() for i in range(5)]

    class _SM:
      updated = {"navRoute": True}
      valid = {"gpsLocation": True, "gpsLocationExternal": False, "navRoute": True}
      recv_time = {"gpsLocation": time.monotonic(), "navRoute": time.monotonic()}
      logMonoTime = {"gpsLocation": time.monotonic_ns()}

      def __getitem__(self, key):
        return _Route() if key == "navRoute" else _Gps()

    md = mapsd.MapsD()
    image, timestamp = md.update(_SM())
    assert timestamp != 0
    assert (image == 255).any(), "basemap road pixels expected once the archive is present"
    md.renderer.close() if md.renderer is not None else None


class _FakeResponse:
  def __init__(self, status, content=b"", headers=None):
    self.status_code = status
    self.content = content
    self.headers = headers or {}


class _FakeSession:
  """Stands in for requests.Session: serves byte ranges from an in-memory blob."""

  def __init__(self, blob: bytes):
    self.blob = blob
    self.calls = []
    self.closed = False

  def get(self, url, headers=None, timeout=None):
    span = headers["Range"].split("=")[1]
    start, end = (int(v) for v in span.split("-"))
    self.calls.append((start, end))
    if start == 0 and end == 0 and len(self.blob) > 1:
      return _FakeResponse(206, self.blob[0:1], {"Content-Range": f"bytes 0-0/{len(self.blob)}"})
    return _FakeResponse(206, self.blob[start:end + 1])

  def close(self):
    self.closed = True


class TestHttpTiles:
  def _tiles(self, tmp_path, blob):
    from openpilot.selfdrive.navigation import local_tiles
    session = _FakeSession(blob)
    tiles = local_tiles.HttpTiles.__new__(local_tiles.HttpTiles)
    tiles.url = "https://example.invalid/base.pmtiles"
    tiles.session = session
    tiles.size = len(blob)
    tiles.cache_dir = tmp_path
    return tiles, session

  def test_read_fetches_and_then_serves_from_cache(self, tmp_path):
    blob = bytes(range(64))
    tiles, session = self._tiles(tmp_path, blob)
    assert tiles.read(0, 8) == blob[0:8]
    calls = len(session.calls)
    assert tiles.read(0, 8) == blob[0:8]
    assert len(session.calls) == calls, "the second read must hit the shared direct-mapped cache"

  def test_read_rejects_a_short_response(self, tmp_path):
    tiles, session = self._tiles(tmp_path, b"x" * 4)
    with pytest.raises(OSError):
      tiles.read(0, 8)

  def test_prefetch_is_bounded(self, tmp_path):
    from openpilot.selfdrive.navigation import local_tiles
    blob = bytes(range(64))
    tiles, session = self._tiles(tmp_path, blob)
    tiles.prefetch([(i, 1) for i in range(1000)])
    assert len(session.calls) <= local_tiles.MAX_PREFETCH_RANGES + 1

  def test_close_closes_the_session(self, tmp_path):
    tiles, session = self._tiles(tmp_path, b"x" * 8)
    tiles.close()
    assert session.closed

  def test_mapsd_accepts_an_https_url(self, monkeypatch):
    from openpilot.selfdrive.navigation import mapsd
    made = {}
    class _FakeTiles:
      def __init__(self, url): made["url"] = url
      def close(self): pass
      def read(self, offset, length): return b""
      def prefetch(self, ranges): pass
    class _FakePMTiles:
      def __init__(self, source):
        self.min_zoom = self.max_zoom = 0
        self.source = source
      def get(self, z, x, y): return None
      def prefetch(self, tiles): pass
      def close(self): pass
    monkeypatch.setattr(mapsd, "HttpTiles", _FakeTiles)
    monkeypatch.setattr(mapsd, "PMTiles", _FakePMTiles)
    monkeypatch.setenv(mapsd.TILES_ENV, "https://example.invalid/base.pmtiles")
    renderer = mapsd.make_renderer()
    assert renderer is not None and made["url"].startswith("https://")


@pytest.mark.skipif(not __import__("os").environ.get("NAV_ONLINE_TEST"), reason="set NAV_ONLINE_TEST=1 for the public-archive check")
class TestOnlineArchive:
  """Opt-in: reads one real tile from Protomaps' public daily build."""

  URL = __import__("os").environ.get("NAV_ONLINE_URL", "https://build.protomaps.com/20261106.pmtiles")

  def test_real_archive_yields_roads(self, tmp_path):
    from openpilot.selfdrive.navigation.local_tiles import HttpTiles
    archive = PMTiles(HttpTiles(self.URL, cache_dir=tmp_path))
    try:
      longitude, latitude = -122.4194, 37.7749
      mx, my = mercator(np.array([[longitude, latitude]]))[0]
      tile = archive.get(15, int(mx * (1 << 15)), int(my * (1 << 15)))
      assert tile is not None
      assert decode_roads(tile), "expected real roads in a San Francisco tile"
    finally:
      archive.close()
