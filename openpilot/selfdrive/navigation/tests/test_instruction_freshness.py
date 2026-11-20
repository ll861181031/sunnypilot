from types import SimpleNamespace

import numpy as np
import pytest

from openpilot.selfdrive.modeld.nav_inputs import NavRuntime
from openpilot.selfdrive.navigation import navd
from openpilot.selfdrive.navigation.helpers import navigation_input
from openpilot.selfdrive.navigation.route import RouteTracker
from openpilot.selfdrive.navigation.routing import Route


@pytest.fixture(autouse=True)
def native_source(monkeypatch):
  monkeypatch.setattr(navd, "cplink_navi_active", lambda: False)
  monkeypatch.setattr("openpilot.selfdrive.modeld.nav_inputs.cplink_navi_active", lambda: False)


def test_passed_turns_are_removed_and_upcoming_turns_are_sorted():
  route = Route([[0, 0], [0, .03]], [
    {"distance": 800., "type": "turn", "modifier": "right"},
    {"distance": 10., "type": "turn", "modifier": "left"},
    {"distance": 600., "type": "roundabout", "modifier": ""},
  ])
  assert [m["distance"] for m in navd.relative_maneuvers(route, 500.)] == [100., 300.]


@pytest.mark.parametrize("on_route,maneuvers", [(False, []), (True, [{"distance": 10., "type": "turn"}])])
def test_unusable_route_publishes_invalid_empty_instruction(on_route, maneuvers):
  sent = []
  d = navd.Navd(None, SimpleNamespace(send=lambda _, msg: sent.append(msg)))
  d.route = Route([[0, 0], [0, .03]], maneuvers)
  d.tracker = RouteTracker(d.route.coordinates)
  d.on_route, d.progress = on_route, 3000.
  d.publish_instruction(100.)
  assert not sent[-1].valid
  assert not list(sent[-1].navInstruction.allManeuvers)


def test_current_banner_selects_the_next_turn():
  sent = []
  d = navd.Navd(None, SimpleNamespace(send=lambda _, msg: sent.append(msg)))
  d.route = Route([[0, 0], [0, .03]], [
    {"distance": 20., "type": "turn", "modifier": "right", "text": "past"},
    {"distance": 700., "type": "turn", "modifier": "left", "text": "next"},
  ])
  d.tracker = RouteTracker(d.route.coordinates)
  d.on_route, d.progress = True, 500.
  d.publish_instruction(100.)
  instruction = sent[-1].navInstruction
  assert instruction.maneuverPrimaryText == "next"
  assert instruction.maneuverDistance == 200.


def test_off_route_fix_does_not_poison_monotonic_progress(monkeypatch):
  d = navd.Navd(None, None)
  d.route = Route([[0, 0], [0, .03]], [])
  d.tracker = RouteTracker(d.route.coordinates)
  monkeypatch.setattr(d, "_position", lambda: (.001, .02))
  d._update_progress()
  assert not d.on_route
  monkeypatch.setattr(d, "_position", lambda: (0., .001))
  d._update_progress()
  assert d.on_route and d.progress == pytest.approx(111.195, abs=.01)


def test_planned_route_is_not_replaced_by_unchanged_fallback_file(monkeypatch, tmp_path):
  fallback = tmp_path / "route.json"
  fallback.write_text('{"coordinates":[[0,0],[0,0.01]],"maneuvers":[]}')
  monkeypatch.setenv("SUNNYPILOT_NAV_ROUTE", str(fallback))
  planned = Route([[0, 0], [.01, .01]], [{"distance": 100., "type": "turn", "modifier": "right"}])
  monkeypatch.delenv("MAPBOX_TOKEN", raising=False)
  monkeypatch.setattr(navd, "plan_route", lambda *args, **kwargs: (planned, "test"))
  d = navd.Navd(None, None)
  monkeypatch.setattr(d, "_destination", lambda: {"lat": .01, "lon": .01, "waypoints": []})
  monkeypatch.setattr(d, "_position", lambda: (0., 0.))
  d._maybe_load_route(100.)
  assert d.route is planned
  d.route_changed = False
  d._maybe_load_route(101.)
  assert d.route is planned


@pytest.mark.parametrize("maneuvers", [[], [{"type": "turn", "modifier": "right", "distance": -2508.67}],
                                    [{"type": "arrive", "modifier": "", "distance": 7.33}]])
def test_zero_instruction_vector_is_not_reported_as_valid(monkeypatch, maneuvers):
  monkeypatch.setattr("openpilot.selfdrive.modeld.nav_inputs.time.monotonic", lambda: 100.)
  runtime = NavRuntime()
  runtime._client_failed = True

  class SM:
    valid = {"navInstruction": True}
    recv_time = {"navInstruction": 100.}
    logMonoTime = {"navInstruction": 100_000_000_000}

    def __getitem__(self, key):
      return SimpleNamespace(allManeuvers=maneuvers)

  _, vector = runtime.update(SM(), 5., (1, 1, 256, 256), (1, 47))
  assert vector is None
  assert not runtime.status()["instruction_valid"]


def test_nonfinite_instruction_distance_is_not_sent_to_model():
  assert not np.any(navigation_input([{"type": "turn", "modifier": "right", "distance": float("inf")}], 0.))


@pytest.mark.parametrize("provider", [Route.from_osrm, Route.from_mapbox])
def test_maneuver_distance_is_at_start_of_step(provider):
  payload = {"routes": [{"geometry": {"coordinates": [[0, 0], [0, .01]]}, "legs": [{"steps": [
    {"distance": 100., "maneuver": {"type": "depart", "modifier": "straight"}},
    {"distance": 250., "maneuver": {"type": "turn", "modifier": "right"}},
    {"distance": 0., "maneuver": {"type": "arrive", "modifier": ""}},
  ]}]}]}
  assert [m["distance"] for m in provider(payload).maneuvers] == [0., 100., 350.]
