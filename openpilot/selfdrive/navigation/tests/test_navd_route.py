"""Offline route ingestion for navd: file schema, maneuver distances, Mapbox parsing."""
import json

import numpy as np
import pytest

from openpilot.selfdrive.navigation.navd import Route, relative_maneuvers


@pytest.fixture(autouse=True)
def isolate_route_source(monkeypatch):
  from openpilot.selfdrive.navigation import navd
  monkeypatch.setattr(navd, "cplink_navi_active", lambda: False)


LON, LAT = -122.4000, 37.8000

ROUTE = {
  "coordinates": [[-122.4000, 37.8000], [-122.3995, 37.8010], [-122.3990, 37.8020]],
  "maneuvers": [
    {"distance": 120.0, "type": "turn", "modifier": "right", "text": "Turn right onto Main St"},
    {"distance": 450.0, "type": "turn", "modifier": "left", "text": "Turn left onto Oak Ave"},
  ],
}


def write_route(tmp_path, payload) -> "object":
  path = tmp_path / "route.json"
  path.write_text(json.dumps(payload))
  return path


class TestRouteFile:
  def test_parses_coordinates_and_maneuvers(self, tmp_path):
    route = Route.from_file(write_route(tmp_path, ROUTE))
    assert route is not None
    assert route.coordinates.shape == (3, 2)
    assert [m["modifier"] for m in route.maneuvers] == ["right", "left"]

  def test_single_point_is_rejected(self, tmp_path):
    route = Route.from_file(write_route(tmp_path, {"coordinates": [[-122.4, 37.8]]}))
    assert route is None

  def test_broken_json_is_rejected(self, tmp_path):
    path = tmp_path / "route.json"
    path.write_text("{not json")
    assert Route.from_file(path) is None

  def test_missing_file_is_rejected(self, tmp_path):
    assert Route.from_file(tmp_path / "nope.json") is None


class TestRelativeManeuvers:
  def test_distances_are_measured_from_the_ego(self, tmp_path):
    route = Route.from_file(write_route(tmp_path, ROUTE))
    ahead = relative_maneuvers(route, 20.0)
    assert [m["distance"] for m in ahead] == pytest.approx([100.0, 430.0])

  def test_passed_maneuvers_are_removed(self):
    route = Route(np.array([[-122.4, 37.8], [-122.39, 37.81]]),
                  [{"distance": 10.0, "type": "turn", "modifier": "right"}])
    ahead = relative_maneuvers(route, 25.0)
    assert ahead == []

  def test_far_away_maneuvers_are_dropped(self):
    route = Route(np.array([[-122.4, 37.8], [-122.39, 37.81]]),
                  [{"distance": 10.0}, {"distance": 1e9}])
    assert [m["distance"] for m in relative_maneuvers(route, 0.0)] == pytest.approx([10.0])


class TestMapboxParsing:
  def test_steps_become_maneuvers_at_cumulative_distances(self):
    payload = {"routes": [{
      "geometry": {"coordinates": [[-122.4, 37.8], [-122.39, 37.81]]},
      "legs": [{"steps": [
        {"distance": 100.0, "maneuver": {"type": "depart", "modifier": "straight", "instruction": "Head north"},
         "bannerInstructions": [{"primary": {"text": "Head north"}}]},
        {"distance": 50.0, "maneuver": {"type": "turn", "modifier": "right", "instruction": "Turn right"},
         "bannerInstructions": [{"primary": {"text": "Turn right onto Main"}}]},
      ]}],
    }]}
    route = Route.from_mapbox(payload)
    assert route is not None
    assert [m["distance"] for m in route.maneuvers] == pytest.approx([0.0, 100.0])
    assert route.maneuvers[1]["text"] == "Turn right"

  def test_malformed_payload_is_rejected(self):
    assert Route.from_mapbox({}) is None


class _Gps:
  def __init__(self, longitude, latitude, speed=10.0):
    self.longitude, self.latitude, self.speed = longitude, latitude, speed
    self.hasFix = True


class _FakeSM:
  def __init__(self, gps):
    import time
    self._gps = gps
    self.valid = {"gpsLocation": gps is not None, "gpsLocationExternal": False}
    self.recv_time = {"gpsLocation": time.monotonic()}
    self.logMonoTime = {"gpsLocation": time.monotonic_ns()}

  def __getitem__(self, key):
    assert key == "gpsLocation"
    return self._gps


class _FakePM:
  def __init__(self):
    self.sent = []

  def send(self, name, msg):
    self.sent.append((name, msg))


class TestInstructionGating:
  """Off-route or fix-less publishes must carry no maneuvers: a bogus "turn here" is worse
  than silence, because the model was trained to treat an empty list as "no guidance"."""

  def setup_method(self):
    from openpilot.selfdrive.navigation.navd import Navd
    from openpilot.selfdrive.navigation.route import RouteTracker
    self.coords = np.array([[LON, LAT], [LON, LAT + 0.01]])
    self.route = Route(self.coords, [{"distance": 300.0, "type": "turn", "modifier": "right"}])
    self._Navd = Navd
    self._Tracker = RouteTracker

  def _run(self, gps):
    navd = self._Navd(_FakeSM(gps), _FakePM())
    navd.route, navd.tracker = self.route, self._Tracker(self.route.coordinates)
    navd._update_progress()
    navd.publish_instruction(now=1.0)
    return navd.pm.sent[-1][1].navInstruction

  def test_no_fix_gives_no_maneuvers(self):
    instruction = self._run(None)
    assert len(instruction.allManeuvers) == 0

  def test_off_route_gives_no_maneuvers(self):
    instruction = self._run(_Gps(LON + 0.01, LAT))  # ~900 m east of the route
    assert len(instruction.allManeuvers) == 0

  def test_on_route_publishes_the_maneuver(self):
    instruction = self._run(_Gps(LON, LAT))
    assert len(instruction.allManeuvers) == 1
    assert instruction.allManeuvers[0].modifier == "right"
    assert instruction.allManeuvers[0].distance == pytest.approx(300.0, abs=1.0)


OSRM_PAYLOAD = {
  "routes": [{
    "distance": 350.0,
    "geometry": {"coordinates": [[-122.4194, 37.7749], [-122.4180, 37.7760], [-122.4160, 37.7775]]},
    "legs": [{"steps": [
      {"distance": 100.0, "name": "Market Street",
       "maneuver": {"type": "depart", "modifier": "straight"}},
      {"distance": 250.0, "name": "10th Street",
       "maneuver": {"type": "turn", "modifier": "right"}},
    ]}],
  }],
}


class TestOsrmParsing:
  def test_steps_become_maneuvers_at_cumulative_distances(self):
    route = Route.from_osrm(OSRM_PAYLOAD)
    assert route is not None
    assert [m["distance"] for m in route.maneuvers] == pytest.approx([0.0, 100.0])
    assert route.maneuvers[1]["type"] == "turn" and route.maneuvers[1]["modifier"] == "right"
    assert "10th Street" in route.maneuvers[1]["text"]

  def test_malformed_payload_is_rejected(self):
    assert Route.from_osrm({}) is None

  def test_osrm_route_is_consumable_by_navigation_input(self):
    from openpilot.selfdrive.navigation.helpers import navigation_input
    route = Route.from_osrm(OSRM_PAYLOAD)
    vector = navigation_input(route.maneuvers, 0.0)
    assert vector[10] == 1.0  # right turn at the second step (first is `depart`, filtered)

  def test_fetch_uses_the_public_server_by_default(self, monkeypatch):
    from openpilot.selfdrive.navigation import routing
    seen = {}

    class _Response:
      def raise_for_status(self): pass
      def json(self): return OSRM_PAYLOAD

    def fake_get(url, params=None, timeout=None):
      seen["url"] = url
      seen["params"] = params
      return _Response()

    monkeypatch.setattr(routing.requests, "get", fake_get)
    route = routing.fetch_osrm_route((-122.4194, 37.7749), (-122.4160, 37.7775))
    assert route is not None and seen["url"].startswith("https://router.project-osrm.org/route/v1/driving/")
    assert "overview=full" in seen["params"] or seen["params"]["overview"] == "full"
    assert seen["url"].count(";") == 1


class _FakeParams:
  def __init__(self, value=None):
    self.value = value
    self.removed = False

  def get(self, key):
    return self.value

  def put(self, key, value):
    self.value = value

  def remove(self, key):
    self.value = None
    self.removed = True


class TestDestination:
  def test_params_destination_wins(self):
    navd = __import__("openpilot.selfdrive.navigation.navd", fromlist=["Navd"]).Navd(None, None,
                                                                                  params=_FakeParams({"latitude": 37.8, "longitude": -122.4}))
    destination = navd._destination()
    assert (destination["lon"], destination["lat"]) == (-122.4, 37.8)

  def test_env_destination_is_a_fallback(self, monkeypatch):
    monkeypatch.setenv("SUNNYPILOT_NAV_DESTINATION", "37.8,-122.4")
    navd = __import__("openpilot.selfdrive.navigation.navd", fromlist=["Navd"]).Navd(None, None,
                                                                                  params=_FakeParams({"latitude": 1.0}))
    destination = navd._destination()
    assert (destination["lon"], destination["lat"]) == (-122.4, 37.8)

  def test_broken_destination_is_ignored(self):
    navd = __import__("openpilot.selfdrive.navigation.navd", fromlist=["Navd"]).Navd(None, None,
                                                                                  params=_FakeParams({"latitude": 37.8}))
    assert navd._destination() is None


class TestSetDestinationCli:
  def test_apply_writes_a_dict(self):
    from openpilot.selfdrive.navigation.set_destination import apply_destination
    params = _FakeParams()
    destination = apply_destination(params, 37.7749, -122.4194, "Ferry Building")
    assert params.value == destination
    assert destination["latitude"] == pytest.approx(37.7749)

  def test_out_of_range_is_rejected(self):
    from openpilot.selfdrive.navigation.set_destination import apply_destination
    with pytest.raises(ValueError):
      apply_destination(_FakeParams(), 91.0, 0.0)
    with pytest.raises(ValueError):
      apply_destination(_FakeParams(), 0.0, 181.0)

  def test_clear_removes_the_param(self):
    """The real param store must not be touched by tests: a leftover destination would make
    navd start routing on every later stack run."""
    from openpilot.selfdrive.navigation.set_destination import main
    params = _FakeParams({"latitude": 1.0, "longitude": 2.0})
    assert main(["--clear"], params=params) == 0
    assert params.removed is True


class _Sm:
  valid = {"gpsLocation": True, "gpsLocationExternal": False}
  recv_time = {"gpsLocation": 0.0, "gpsLocationExternal": 0.0}
  logMonoTime = {"gpsLocation": 1, "gpsLocationExternal": 0}

  def __init__(self):
    import time
    self.recv_time = {"gpsLocation": time.monotonic(), "gpsLocationExternal": 0.0}

  def __getitem__(self, key):
    return type("G", (), {"latitude": 31.0, "longitude": 106.0, "hasFix": True})()


class _Pm:
  def send(self, name, msg):
    pass


class TestWaypoints:
  def test_destination_waypoints_reach_the_planner(self, monkeypatch):
    import time
    from openpilot.selfdrive.navigation import navd as navd_module
    calls = {}

    def fake_plan(position, target, waypoints=(), handle=None):
      calls["waypoints"] = list(waypoints)
      calls["target"] = target
      return None, "none"

    monkeypatch.setattr(navd_module, "plan_route", fake_plan)
    params = _FakeParams({"latitude": 31.0, "longitude": 106.0,
                          "waypoints": [{"latitude": 31.01, "longitude": 106.01},
                                        {"latitude": 31.02, "longitude": 106.02}]})
    navd_instance = navd_module.Navd(_Sm(), _Pm(), params=params)
    navd_instance._maybe_load_route(time.monotonic())
    assert calls["waypoints"] == [(106.01, 31.01), (106.02, 31.02)]
    assert calls["target"] == (106.0, 31.0)

  def test_waypoint_order_changes_reload_the_route(self, monkeypatch):
    import time
    from openpilot.selfdrive.navigation import navd as navd_module
    calls = []

    def fake_plan(position, target, waypoints=(), handle=None):
      calls.append(list(waypoints))
      return None, "none"

    monkeypatch.setattr(navd_module, "plan_route", fake_plan)
    params = _FakeParams({"latitude": 31.0, "longitude": 106.0,
                          "waypoints": [{"latitude": 31.01, "longitude": 106.01}]})
    navd_instance = navd_module.Navd(_Sm(), _Pm(), params=params)
    navd_instance._maybe_load_route(time.monotonic())
    params.value["waypoints"] = [{"latitude": 31.02, "longitude": 106.02}]
    navd_instance._maybe_load_route(time.monotonic())
    assert len(calls) == 2 and calls[1] == [(106.02, 31.02)]


class _PM:
  def __init__(self):
    self.sent = []

  def send(self, name, msg):
    self.sent.append((name, msg))


class _SM:
  valid = {"gpsLocation": False, "gpsLocationExternal": False}
  recv_time = {"gpsLocation": 0.0, "gpsLocationExternal": 0.0}
  logMonoTime = {"gpsLocation": 0, "gpsLocationExternal": 0}


class _Params:
  def get(self, key, *args, **kwargs):
    return None


class TestNavdPublishing:
  """The model only accepts messages with the message-level `valid` flag set."""

  def _navd(self, monkeypatch, tmp_path):
    from openpilot.selfdrive.navigation import navd as navd_mod
    path = write_route(tmp_path, ROUTE)
    monkeypatch.setenv("SUNNYPILOT_NAV_ROUTE", str(path))
    pm = _PM()
    return navd_mod.Navd(_SM(), pm, params=_Params()), pm, path

  def test_loaded_route_is_published_immediately_and_marked_valid(self, monkeypatch, tmp_path):
    navd, pm, _ = self._navd(monkeypatch, tmp_path)
    navd.step(1.0)
    name, msg = next((n, m) for n, m in reversed(pm.sent) if n == "navRoute")
    assert name == "navRoute"
    assert msg.valid is True
    assert len(msg.navRoute.coordinates) == 3

  def test_removed_route_publishes_an_empty_valid_route(self, monkeypatch, tmp_path):
    navd, pm, path = self._navd(monkeypatch, tmp_path)
    navd.step(1.0)
    path.unlink()
    navd.step(2.0)
    name, msg = next((n, m) for n, m in reversed(pm.sent) if n == "navRoute")
    assert name == "navRoute"
    assert msg.valid is True
    assert len(msg.navRoute.coordinates) == 0
