"""Which archive mapsd renders from: env > NavTiles param > local file > URL."""
from pathlib import Path

from openpilot.selfdrive.navigation import mapsd


class TestTilesSpec:
  def test_env_wins_over_everything(self, monkeypatch, tmp_path):
    monkeypatch.setenv(mapsd.TILES_ENV, "/env/archive.pmtiles")
    monkeypatch.setattr(mapsd, "param_tiles", lambda: "/param/archive.pmtiles")
    monkeypatch.setattr(mapsd, "DEFAULT_TILES", tmp_path / "basemap.pmtiles")
    assert mapsd.tiles_spec() == "/env/archive.pmtiles"

  def test_param_second(self, monkeypatch, tmp_path):
    monkeypatch.delenv(mapsd.TILES_ENV, raising=False)
    monkeypatch.setattr(mapsd, "param_tiles", lambda: "https://example.com/tiles.pmtiles")
    monkeypatch.setattr(mapsd, "DEFAULT_TILES", tmp_path / "basemap.pmtiles")
    assert mapsd.tiles_spec() == "https://example.com/tiles.pmtiles"

  def test_local_default_file_when_no_override(self, monkeypatch, tmp_path):
    monkeypatch.delenv(mapsd.TILES_ENV, raising=False)
    monkeypatch.setattr(mapsd, "param_tiles", lambda: None)
    local = tmp_path / "basemap.pmtiles"
    local.write_bytes(b"x")
    monkeypatch.setattr(mapsd, "DEFAULT_TILES", local)
    assert mapsd.tiles_spec() == str(local)

  def test_url_is_the_last_resort(self, monkeypatch, tmp_path):
    monkeypatch.delenv(mapsd.TILES_ENV, raising=False)
    monkeypatch.setattr(mapsd, "param_tiles", lambda: None)
    monkeypatch.setattr(mapsd, "DEFAULT_TILES", tmp_path / "missing.pmtiles")
    assert mapsd.tiles_spec() == mapsd.DEFAULT_TILES_URL
    assert mapsd.DEFAULT_TILES_URL.startswith("https://")

  def test_blank_values_fall_through(self, monkeypatch, tmp_path):
    monkeypatch.setenv(mapsd.TILES_ENV, "   ")
    monkeypatch.setattr(mapsd, "param_tiles", lambda: "  ")
    monkeypatch.setattr(mapsd, "DEFAULT_TILES", Path(tmp_path / "missing.pmtiles"))
    assert mapsd.tiles_spec() == mapsd.DEFAULT_TILES_URL


class _StaleSM:
  """No new route and no GPS fix: only the age of the last route matters."""
  updated = {"navRoute": False}
  valid = {"gpsLocation": False, "gpsLocationExternal": False}
  recv_time = {"gpsLocation": 0.0, "gpsLocationExternal": 0.0, "navRoute": 0.0}
  logMonoTime = {"gpsLocation": 0, "gpsLocationExternal": 0, "navRoute": 0}


class TestRouteExpiry:
  def test_stale_route_renders_black(self):
    import numpy as np
    import time
    from openpilot.selfdrive.navigation.route import RouteTracker

    d = mapsd.MapsD()
    route = np.array([[121.47, 31.23], [121.48, 31.23]])
    d.route = route
    d.tracker = RouteTracker(route)
    d.route_at = time.monotonic() - mapsd.ROUTE_TIMEOUT - 1.0

    image, stamp = d.update(_StaleSM())
    assert stamp == 0
    assert not image.any()
    assert d.tracker is None
