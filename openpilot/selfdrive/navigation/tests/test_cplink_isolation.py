"""Phone instructions must never be combined with the native route geometry."""
from types import SimpleNamespace

import numpy as np
import pytest

from openpilot.selfdrive.modeld import nav_inputs
from openpilot.selfdrive.navigation import mapsd, navd
from openpilot.selfdrive.navigation.route import RouteTracker


@pytest.fixture
def source(monkeypatch):
  state = [False]
  for module in (nav_inputs, mapsd, navd):
    monkeypatch.setattr(module, "cplink_navi_active", lambda: state[0], raising=False)
  return state


def test_phone_nav_clears_native_route_publication(source):
  source[0] = True
  sent = []
  d = navd.Navd(None, SimpleNamespace(send=lambda name, msg: sent.append(msg)))
  d.route = navd.Route(np.array([[121.0, 31.0], [121.001, 31.0]]), [])
  d.publish_route(100.0)
  assert not sent or len(sent[-1].navRoute.coordinates) == 0


def test_mapsd_drops_route_cache_while_phone_owns_navigation(source):
  d = mapsd.MapsD()
  d.route = np.array([[121.0, 31.0], [121.001, 31.0]])
  d.tracker = RouteTracker(d.route)
  d.route_at = mapsd.time.monotonic()
  sm = SimpleNamespace(updated={"navRoute": False}, valid={"gpsLocation": False, "gpsLocationExternal": False}, recv_time={})
  source[0] = True
  image, stamp = d.update(sm)
  assert stamp == 0 and not image.any()
  assert d.tracker is None and len(d.route) == 0


def test_model_immediately_drops_native_map_on_phone_takeover(source, monkeypatch):
  clock = [100.0]
  monkeypatch.setattr(nav_inputs.time, "monotonic", lambda: clock[0])
  runtime = nav_inputs.NavRuntime()
  runtime._client_failed = True
  runtime.map_image = np.ones((1, 1, 256, 256), dtype=np.uint8)
  runtime.map_at = 100.0
  sm = SimpleNamespace(valid={}, recv_time={})
  source[0] = True
  image, _ = runtime.update(sm, 0.0, (1, 1, 256, 256), (1, 47))
  assert image is None and not runtime.map_valid
  source[0] = False
  clock[0] += 0.1
  image, _ = runtime.update(sm, 0.0, (1, 1, 256, 256), (1, 47))
  assert image is None


def test_black_frame_revokes_previously_cached_map(source):
  runtime = nav_inputs.NavRuntime()
  runtime.map_image = np.ones((1, 1, 256, 256), dtype=np.uint8)
  runtime.map_at = nav_inputs.time.monotonic()
  runtime.client = SimpleNamespace(is_connected=lambda: True,
                                  recv=lambda _: SimpleNamespace(width=256, height=256, data=bytes(256 * 256)))
  image, _ = runtime.update(SimpleNamespace(valid={}, recv_time={}), 0.0, (1, 1, 256, 256), (1, 47))
  assert image is None and not runtime.map_valid


class InstructionSM:
  def __init__(self, received, produced=None):
    self.valid = {"navInstruction": True}
    self.recv_time = {"navInstruction": received}
    self.logMonoTime = {"navInstruction": int((produced if produced is not None else received) * 1e9)}

  def __getitem__(self, key):
    return SimpleNamespace(allManeuvers=[SimpleNamespace(type="turn", modifier="right", distance=20.0)])


def test_source_transition_rejects_queued_instructions_in_both_directions(source, monkeypatch):
  clock = [100.0]
  monkeypatch.setattr(nav_inputs.time, "monotonic", lambda: clock[0])
  runtime = nav_inputs.NavRuntime()
  runtime._client_failed = True
  for active in (True, False):
    source[0] = active
    _, vector = runtime.update(InstructionSM(clock[0]), 0.0, (1, 1, 256, 256), (1, 47))
    assert vector is None
    previous = clock[0]
    clock[0] += 0.6
    _, vector = runtime.update(InstructionSM(clock[0], previous), 0.0, (1, 1, 256, 256), (1, 47))
    assert vector is None
    image, vector = runtime.update(InstructionSM(clock[0]), 0.0, (1, 1, 256, 256), (1, 47))
    assert image is None and vector is not None
    assert vector[0, 10] == 1.0
    clock[0] += 0.1


def test_return_to_native_requires_new_map_timestamp(source, monkeypatch):
  clock = [100.0]
  monkeypatch.setattr(nav_inputs.time, "monotonic", lambda: clock[0])
  runtime = nav_inputs.NavRuntime()
  runtime.client = SimpleNamespace(is_connected=lambda: True, timestamp_eof=int(99e9),
                                  recv=lambda _: SimpleNamespace(width=256, height=256, data=bytes([1]) * 256 * 256))
  sm = SimpleNamespace(valid={}, recv_time={})
  source[0] = True
  runtime.update(sm, 0.0, (1, 1, 256, 256), (1, 47))
  source[0] = False
  clock[0] += 0.1
  runtime.update(sm, 0.0, (1, 1, 256, 256), (1, 47))
  clock[0] += 0.6
  image, _ = runtime.update(sm, 0.0, (1, 1, 256, 256), (1, 47))
  assert image is None
  runtime.client.timestamp_eof = int(clock[0] * 1e9)
  runtime.update(sm, 0.0, (1, 1, 256, 256), (1, 47))
  image, _ = runtime.update(sm, 0.0, (1, 1, 256, 256), (1, 47))
  assert image is not None


def test_navd_resends_native_route_immediately_on_release(source, monkeypatch):
  d = navd.Navd(None, SimpleNamespace(send=lambda *_: None))
  routes = []
  monkeypatch.setattr(d, "_maybe_load_route", lambda _: None)
  monkeypatch.setattr(d, "_update_progress", lambda: None)
  monkeypatch.setattr(d, "publish_route", lambda now: (routes.append(now), setattr(d, "route_changed", False)))
  monkeypatch.setattr(d, "publish_instruction", lambda _: None)
  source[0] = True
  d.step(100.0)
  d.step(100.1)
  source[0] = False
  d.step(100.2)
  assert routes == [100.0, 100.2]


def test_mapsd_waits_for_fresh_native_route_after_release(source, monkeypatch):
  clock = [100.0]
  monkeypatch.setattr(mapsd.time, "monotonic", lambda: clock[0])

  class SM:
    updated = {"navRoute": True}
    valid = {"navRoute": True, "gpsLocation": False, "gpsLocationExternal": False}
    recv_time = {"navRoute": 99.0}

    def __getitem__(self, key):
      return SimpleNamespace(coordinates=[SimpleNamespace(longitude=121.0, latitude=31.0),
                                          SimpleNamespace(longitude=121.001, latitude=31.0)])

  sm = SM()
  d = mapsd.MapsD()
  source[0] = True
  d.update(sm)
  source[0] = False
  clock[0] += 0.1
  d.update(sm)
  assert d.tracker is None
  clock[0] += 0.1
  sm.recv_time["navRoute"] = clock[0]
  d.update(sm)
  assert d.tracker is not None


def test_mapsd_accepts_native_resend_between_source_polls(source, monkeypatch):
  clock = [100.0]
  monkeypatch.setattr(mapsd.time, "monotonic", lambda: clock[0])

  class SM:
    updated = {"navRoute": True}
    valid = {"navRoute": True, "gpsLocation": False, "gpsLocationExternal": False}
    recv_time = {"navRoute": 100.05}
    logMonoTime = {"navRoute": int(100.05e9)}

    def __getitem__(self, key):
      return SimpleNamespace(coordinates=[SimpleNamespace(longitude=121.0, latitude=31.0),
                                          SimpleNamespace(longitude=121.001, latitude=31.0)])

  d = mapsd.MapsD()
  source[0] = True
  d.update(SM())
  source[0] = False
  clock[0] += 0.1
  d.update(SM())
  assert d.tracker is not None


@pytest.mark.parametrize("content,nav,speed", [
  ("nav", True, False), ("speed", False, True), ("nav+speed", True, True),
  ("", False, False), ("garbage", False, False),
])
def test_all_readers_agree_on_service_ownership(tmp_path, monkeypatch, content, nav, speed):
  from openpilot.selfdrive.navigation import cplink
  from openpilot.sunnypilot.mapd.live_map_data import base_map_data
  path = tmp_path / "gate"
  path.write_text(content)
  monkeypatch.setattr(cplink, "CPLINK_GATE", path)
  monkeypatch.setattr(navd, "CPLINK_GATE", path)
  monkeypatch.setattr(base_map_data, "CPLINK_GATE", path)
  assert navd.cplink_navi_active() is nav
  assert cplink.cplink_navi_active() is nav
  assert base_map_data.cplink_speed_active() is speed


def test_expired_lease_releases_both_services(tmp_path):
  import os
  from openpilot.selfdrive.navigation.cplink import source_active
  path = tmp_path / "gate"
  path.write_text("nav+speed")
  os.utime(path, (1.0, 1.0))
  assert not source_active("nav", path) and not source_active("speed", path)


def test_navd_is_the_only_publisher_and_forwards_phone_message(source, monkeypatch):
  from openpilot.cereal import messaging
  original = messaging.new_message("navInstruction", valid=True)
  original.navInstruction.maneuverModifier = "right"
  monkeypatch.setattr(navd, "read_cplink_message", lambda *_: original, raising=False)
  source[0] = True
  sent = []
  d = navd.Navd(None, SimpleNamespace(send=lambda name, msg: sent.append(msg)))
  d.publish_instruction(100.0)
  assert len(sent) == 1 and sent[0].navInstruction.maneuverModifier == "right"
  assert sent[0].logMonoTime == original.logMonoTime


def test_native_speed_publisher_forwards_phone_message(monkeypatch):
  from openpilot.cereal import messaging
  from openpilot.sunnypilot.mapd.live_map_data import base_map_data
  from openpilot.sunnypilot.mapd.live_map_data.osm_map_data import OsmMapData
  original = messaging.new_message("liveMapDataSP", valid=True)
  original.liveMapDataSP.speedLimitValid = True
  original.liveMapDataSP.speedLimit = 60 / 3.6
  monkeypatch.setattr(base_map_data, "cplink_speed_active", lambda: True)
  monkeypatch.setattr(base_map_data, "read_cplink_message", lambda *_: original, raising=False)
  sent = []
  d = OsmMapData.__new__(OsmMapData)
  d.pm = SimpleNamespace(send=lambda name, msg: sent.append(msg))
  d.publish()
  assert len(sent) == 1 and sent[0].liveMapDataSP.speedLimit == pytest.approx(60 / 3.6)
  assert sent[0].logMonoTime == original.logMonoTime


def test_forwarding_cannot_renew_instruction_age(source, monkeypatch):
  monkeypatch.setattr(nav_inputs.time, "monotonic", lambda: 100.0)
  runtime = nav_inputs.NavRuntime()
  runtime._client_failed = True
  _, vector = runtime.update(InstructionSM(100.0, 99.4), 10.0, (1, 1, 256, 256), (1, 47))
  assert vector is None
  _, vector = runtime.update(InstructionSM(100.0, 99.8), 10.0, (1, 1, 256, 256), (1, 47))
  assert vector[0, -1] == pytest.approx(0.18)


def test_receiving_queued_map_cannot_renew_its_age(source, monkeypatch):
  monkeypatch.setattr(nav_inputs.time, "monotonic", lambda: 100.0)
  runtime = nav_inputs.NavRuntime()
  runtime.client = SimpleNamespace(is_connected=lambda: True, timestamp_eof=int(99.4e9),
                                  recv=lambda _: SimpleNamespace(width=256, height=256, data=bytes([1]) * 256 * 256))
  image, _ = runtime.update(SimpleNamespace(valid={}, recv_time={}), 0.0, (1, 1, 256, 256), (1, 47))
  assert image is None
