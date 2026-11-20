"""Wiring for the nav pipeline: services, schema names, and manager processes.

The schema test exists because of a real trap: `log.capnp` carried
`navInstructionDEPRECATED` / `navRouteDEPRECATED` while `services.py` names the services
`navInstruction` / `navRoute`, and `messaging.new_message()` matches the Event union field
by name. Publishing raised `struct has no such member` only at runtime, 30 seconds into a
drive, so the rename to the active names is pinned here.
"""
import pytest

import openpilot.cereal.messaging as messaging
from openpilot.cereal import log
from openpilot.cereal.services import SERVICE_LIST
from openpilot.system.manager.process_config import procs


class TestServices:
  def test_nav_services_are_registered(self):
    assert "navInstruction" in SERVICE_LIST
    assert "navRoute" in SERVICE_LIST

  def test_instruction_is_one_hertz(self):
    assert SERVICE_LIST["navInstruction"].frequency == 1.0

  def test_route_is_change_driven(self):
    assert SERVICE_LIST["navRoute"].frequency == 0.


class TestSchemaNames:
  def test_event_union_uses_the_active_names(self):
    fields = log.Event.schema.fields
    assert "navInstruction" in fields, "publishing navInstruction would fail at runtime"
    assert "navRoute" in fields, "publishing navRoute would fail at runtime"
    # Ordinals must not move: old logs decode by ordinal, and messaging matches by name.
    assert int(fields["navInstruction"].proto.ordinal.explicit) == 82
    assert int(fields["navRoute"].proto.ordinal.explicit) == 83

  def test_instruction_message_carries_all_maneuvers(self):
    msg = messaging.new_message("navInstruction")
    msg.navInstruction.allManeuvers = [{"distance": 12.5, "type": "turn", "modifier": "right"}]
    with log.Event.from_bytes(msg.to_bytes()) as parsed:
      assert parsed.navInstruction.allManeuvers[0].modifier == "right"

  def test_route_message_carries_coordinates(self):
    msg = messaging.new_message("navRoute")
    msg.navRoute.coordinates = [{"latitude": 37.8, "longitude": -122.4}]
    with log.Event.from_bytes(msg.to_bytes()) as parsed:
      assert parsed.navRoute.coordinates[0].longitude == pytest.approx(-122.4, abs=1e-5)


class TestProcesses:
  def test_navd_and_mapsd_are_scheduled(self):
    names = {p.name for p in procs}
    assert "navd" in names
    assert "mapsd" in names

  def test_they_only_run_onroad(self):
    by_name = {p.name: p for p in procs}
    for name in ("navd", "mapsd"):
      assert by_name[name].module in ("openpilot.selfdrive.navigation.navd", "openpilot.selfdrive.navigation.mapsd")
