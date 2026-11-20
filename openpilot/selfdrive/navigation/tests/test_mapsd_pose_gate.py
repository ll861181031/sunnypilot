from types import SimpleNamespace

import numpy as np
import pytest

from openpilot.selfdrive.navigation import mapsd
from openpilot.selfdrive.navigation.route import METERS_PER_DEGREE, RouteTracker


class PoseSM:
  def __init__(self, longitude, latitude):
    self.updated = {"navRoute": False}
    self.valid = {"gpsLocation": True, "gpsLocationExternal": False}
    self.recv_time = {"gpsLocation": 100.0}
    self.logMonoTime = {"gpsLocation": int(100e9)}
    self.gps = SimpleNamespace(hasFix=True, longitude=longitude, latitude=latitude, speed=0.0)

  def __getitem__(self, name):
    assert name == "gpsLocation"
    return self.gps


@pytest.fixture
def render(monkeypatch):
  monkeypatch.setattr(mapsd.time, "monotonic", lambda: 100.0)
  monkeypatch.setattr(mapsd, "cplink_navi_active", lambda: False)
  monkeypatch.setattr(mapsd, "make_renderer", lambda: None)
  instance = mapsd.MapsD()
  instance.route = np.array([[121.0, 31.0], [121.0, 31.002]])
  instance.tracker = RouteTracker(instance.route)
  instance.route_at = 100.0
  return instance


def displaced(meters):
  return 121.0 + meters / (METERS_PER_DEGREE * np.cos(np.radians(31.0)))


def test_raw_position_far_from_route_is_rejected_before_projection(render):
  image, stamp = render.update(PoseSM(displaced(200.0), 31.001))
  assert stamp == 0 and not image.any()
  assert render.tracker.progress == -np.inf
  assert render.pose is None


def test_leaving_route_revokes_map_and_keeps_previous_progress(render):
  image, stamp = render.update(PoseSM(121.0, 31.0001))
  assert stamp and image.any()
  progress = render.tracker.progress
  image, stamp = render.update(PoseSM(displaced(200.0), 31.0019))
  assert stamp == 0 and not image.any()
  assert render.tracker.progress == progress
  assert render.pose is None
  image, stamp = render.update(PoseSM(121.0, 31.0002))
  assert stamp and image.any()
  assert render.progress < 30.0


@pytest.mark.parametrize("lon,lat", [(float("nan"), 31.0), (121.0, float("inf")), (181.0, 31.0), (121.0, 91.0)])
def test_invalid_raw_coordinate_is_rejected_without_advancing_tracker(render, lon, lat):
  image, stamp = render.update(PoseSM(lon, lat))
  assert stamp == 0 and not image.any()
  assert render.tracker.progress == -np.inf


def test_near_route_position_remains_usable(render):
  image, stamp = render.update(PoseSM(displaced(10.0), 31.001))
  assert stamp and image.any()
  assert 100.0 < render.progress < 120.0
