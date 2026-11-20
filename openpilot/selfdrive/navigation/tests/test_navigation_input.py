"""The 47-float navigation vector the nav model consumes.

Ported from comma's nav-model-integration helpers; these tests pin the encoding the model
was trained on: 46 one-hot Valhalla command slots plus distance/100 in the last element.
"""
import numpy as np
import pytest

from openpilot.selfdrive.navigation.helpers import (
  VALHALLA_COMMAND_COUNT,
  navigation_input,
  valhalla_type,
)


def maneuver(type_, modifier, distance):
  return type("M", (), {"type": type_, "modifier": modifier, "distance": distance})()


class TestValhallaTypes:
  def test_right_turn(self):
    assert valhalla_type("turn", "right") == 10

  def test_left_turn(self):
    assert valhalla_type("turn", "left") == 15

  def test_straight_turn(self):
    assert valhalla_type("turn", "straight") == 8

  def test_merge_has_no_maneuver(self):
    """Valhalla emits nothing at a merge, so the model never saw that command. Inventing one
    would feed it an input it was not trained on."""
    assert valhalla_type("merge", "right") is None

  def test_sided_type_prefers_modifier(self):
    assert valhalla_type("off ramp", "right") == 20
    assert valhalla_type("off ramp", "left") == 21
    assert valhalla_type("off ramp", "straight") == 20

  def test_unknown_modifier_defaults_to_continue(self):
    assert valhalla_type("turn", "") == 8


class TestNavigationInput:
  def test_width_is_command_count_plus_distance(self):
    vector = navigation_input([], 0.0)
    assert vector.shape == (VALHALLA_COMMAND_COUNT + 1,)
    assert vector.dtype == np.float32
    assert not vector.any()

  def test_first_maneuver_becomes_the_one_hot(self):
    vector = navigation_input([maneuver("turn", "right", 250.0)], 0.0)
    assert vector[10] == 1.0
    assert vector[-1] == pytest.approx(2.5)  # distance / 100
    assert vector.sum() == pytest.approx(1.0 + 2.5)

  def test_travel_shortens_the_distance(self):
    vector = navigation_input([maneuver("turn", "right", 250.0)], 100.0)
    assert vector[-1] == pytest.approx(1.5)

  def test_passed_maneuvers_are_skipped(self):
    vector = navigation_input([maneuver("turn", "right", 10.0), maneuver("turn", "left", 300.0)], 50.0)
    assert vector[15] == 1.0
    assert vector[-1] == pytest.approx(2.5)

  def test_low_command_turns_are_filtered(self):
    """depart (1) and arrive (4) sit at or below the command>6 gate: no steerable command."""
    vector = navigation_input([maneuver("depart", "straight", 5.0)], 0.0)
    assert not vector.any()

  def test_merge_is_skipped_and_the_next_maneuver_wins(self):
    vector = navigation_input([maneuver("merge", "left", 20.0), maneuver("turn", "left", 200.0)], 0.0)
    assert vector[15] == 1.0
    assert vector[-1] == pytest.approx(2.0)

  def test_only_the_nearest_maneuver_is_encoded(self):
    vector = navigation_input([maneuver("turn", "right", 100.0), maneuver("turn", "left", 200.0)], 0.0)
    assert vector[10] == 1.0
    assert vector[15] == 0.0
