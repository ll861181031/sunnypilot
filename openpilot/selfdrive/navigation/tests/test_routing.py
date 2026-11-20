"""Routing: AMap first, OSRM fallback, and the maneuver translation both need."""
import pytest

from openpilot.selfdrive.navigation import routing


AMAP_PAYLOAD = {
  "status": "1", "info": "OK",
  "route": {"paths": [{
    "distance": "350", "duration": "120",
    "steps": [
      {"instruction": "沿人民路向西行驶200米", "action": "", "distance": "200",
       "polyline": "106.414900,31.030100;106.416000,31.030100"},
      {"instruction": "右转进入滨江路", "action": "右转", "distance": "150",
       "polyline": "106.416000,31.030100;106.416000,31.031000"},
    ],
  }]},
}


class TestAmapManeuvers:
  @pytest.mark.parametrize("action,instruction,expected", [
    ("左转", "", ("turn", "left")),
    ("右转", "", ("turn", "right")),
    ("掉头", "", ("turn", "uturn")),
    ("", "沿S204行驶", ("turn", "straight")),
    ("靠左", "", ("fork", "slight left")),
    ("靠右", "", ("fork", "slight right")),
    ("向左前方行驶", "", ("fork", "slight left")),
    ("进入环岛", "", ("roundabout", "")),
    ("驶出环岛", "", ("exit roundabout", "")),
    ("上高速", "", ("on ramp", "")),
    ("下高速", "", ("off ramp", "")),
    ("", "到达目的地", ("arrive", "")),
    ("", "未知动作", ("turn", "straight")),
  ])
  def test_action_table(self, action, instruction, expected):
    assert routing.amap_maneuver(action, instruction) == expected

  def test_navigation_input_accepts_the_translation(self):
    from openpilot.selfdrive.navigation.helpers import navigation_input
    maneuvers = [{"distance": 100.0, "type": "turn", "modifier": "left"}]
    assert navigation_input(maneuvers, 0.0)[15] == 1.0  # Valhalla left turn


class TestFromAmap:
  def test_geometry_is_converted_to_wgs84(self):
    route = routing.Route.from_amap(AMAP_PAYLOAD)
    assert route is not None
    # GCJ 106.4149/31.0301 -> WGS is ~0.004 west and ~0.003 north of the input.
    assert route.coordinates[0][0] == pytest.approx(106.4107, abs=2e-3)
    assert route.coordinates[0][1] == pytest.approx(31.0325, abs=2e-3)
    assert route.source == "amap"
    assert route.distance_m == 350.0 and route.duration_s == 120.0

  def test_maneuvers_accumulate_distance(self):
    route = routing.Route.from_amap(AMAP_PAYLOAD)
    assert [m["distance"] for m in route.maneuvers] == [200.0, 350.0]
    assert route.maneuvers[1]["type"] == "turn" and route.maneuvers[1]["modifier"] == "right"

  def test_duplicate_points_are_dropped(self):
    payload = {"route": {"paths": [{"steps": [
      {"distance": "10", "polyline": "106.4,31.0;106.4,31.0"},
      {"distance": "10", "polyline": "106.4,31.0;106.5,31.0"},
    ]}]}}
    route = routing.Route.from_amap(payload)
    assert len(route.coordinates) == 2

  def test_garbage_is_rejected(self):
    assert routing.Route.from_amap({}) is None
    assert routing.Route.from_amap({"route": {"paths": []}}) is None


class TestPlanRoute:
  def test_amap_wins_when_it_answers(self, monkeypatch):
    monkeypatch.setattr(routing, "amap_key", lambda handle=None: "k")
    monkeypatch.setattr(routing, "fetch_amap_route", lambda *a, **k: "AMAP")
    monkeypatch.setattr(routing, "fetch_osrm_route", lambda *a, **k: "OSRM")
    assert routing.plan_route((0, 0), (1, 1)) == ("AMAP", "amap")

  def test_osrm_is_the_fallback(self, monkeypatch):
    monkeypatch.setattr(routing, "amap_key", lambda handle=None: "k")
    monkeypatch.setattr(routing, "fetch_amap_route", lambda *a, **k: None)
    monkeypatch.setattr(routing, "fetch_osrm_route", lambda *a, **k: "OSRM")
    assert routing.plan_route((0, 0), (1, 1)) == ("OSRM", "osrm")

  def test_no_key_goes_straight_to_osrm(self, monkeypatch):
    monkeypatch.setattr(routing, "amap_key", lambda handle=None: None)
    called = {"amap": False}

    def fake_amap(*args, **kwargs):
      called["amap"] = True
      return None

    monkeypatch.setattr(routing, "fetch_amap_route", fake_amap)
    monkeypatch.setattr(routing, "fetch_osrm_route", lambda *a, **k: "OSRM")
    assert routing.plan_route((0, 0), (1, 1)) == ("OSRM", "osrm")
    assert called["amap"] is False

  def test_nothing_available(self, monkeypatch):
    monkeypatch.setattr(routing, "amap_key", lambda handle=None: None)
    monkeypatch.setattr(routing, "fetch_osrm_route", lambda *a, **k: None)
    assert routing.plan_route((0, 0), (1, 1)) == (None, "none")


class TestAmapRequest:
  def test_waypoints_are_converted_and_limited(self, monkeypatch):
    seen = {}

    class Response:
      def raise_for_status(self):
        pass

      def json(self):
        return AMAP_PAYLOAD

    def fake_get(url, params=None, timeout=None):
      seen.update(params)
      return Response()

    monkeypatch.setattr(routing.requests, "get", fake_get)
    routing.fetch_amap_route((106.4149, 31.0301), (106.42, 31.04),
                             waypoints=[(106.416, 31.035)] * 20, key="k")
    assert seen["key"] == "k"
    assert seen["origin"].startswith("106.419")  # GCJ-02, not the raw WGS value
    assert seen["waypoints"].count("|") == routing.AMAP_WAYPOINTS_MAX - 1

  def test_without_a_key_nothing_is_sent(self, monkeypatch):
    monkeypatch.setattr(routing, "amap_key", lambda handle=None: None)
    assert routing.fetch_amap_route((0, 0), (1, 1)) is None
