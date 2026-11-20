"""Tests for the lead-bound learner's control-loop glue.

The estimator's own behaviour is covered in opendbc; what is tested here is the part
that only exists in the car: the 60 ms attribution window, the mode switch, and the
promise that shadow mode cannot move the curve in force.
"""

import pytest

from opendbc.car.byd.values import CarControllerParams, parse_lead_bound_table
from openpilot.sunnypilot.selfdrive.controls.lib.lead_bound_learn import (
  LeadBoundLearning,
  MODE_APPLY,
  MODE_OFF,
  MODE_SHADOW,
)


def learning(mode=MODE_SHADOW):
  bp, values = CarControllerParams.STEER_ANGLE_ERROR_MAX
  return LeadBoundLearning(bp, values, mode)


def feed(learner, frames, v_kmh=30.0, at_bound=True, starved=True, too_fast=False):
  for i in range(frames):
    learner.step(v_kmh / 3.6, at_bound, starved, too_fast and i == 0)


class TestModes:
  def test_off_changes_nothing(self):
    learner = learning(MODE_OFF)
    before = learner.table_text()
    feed(learner, 2000, too_fast=True)
    assert learner.table_text() == before
    assert learner.step(30 / 3.6, True, True, True) is False

  def test_shadow_learns_but_never_asks_to_apply(self):
    """Learning is observable as a NARROWING, not as a widening.

    The learner may never exceed the shipped curve - `parse_lead_bound_table`
    rejects a wider table outright, so a widening "learn" would be discarded by
    the car. Drive the band down with a refusal, then let it climb back; the text
    must move either way while shadow mode still refuses to apply it.
    """
    learner = learning(MODE_SHADOW)
    before = learner.table_text()
    # The refusal is attributed to the frames BEFORE it, so the window has to be
    # filled first: a fault on the very first frame has no predecessor to land on.
    feed(learner, 6)
    feed(learner, 1, too_fast=True)
    narrowed = learner.table_text()
    assert narrowed != before, "a refusal must narrow the proposal"
    feed(learner, 2000)
    assert learner.table_text() != narrowed, "starved demand must raise it again"
    assert learner.wants_apply() is False

  def test_apply_mode_wants_to_apply(self):
    assert learning(MODE_APPLY).wants_apply() is True


class TestAttributionWindow:
  def test_a_refusal_lands_on_a_frame_sixty_milliseconds_earlier(self):
    learn = learning()
    # Six frames of clean pinned demand, then a refusal. The estimator must only see
    # that refusal after the buffer has walked past the frame it belongs to, i.e.
    # the accounting lags by LOOKAHEAD_FRAMES, never leads.
    for _ in range(6):
      learn.step(30 / 3.6, True, True, False)
    assert learn.learner.frames == 0, "nothing is attributed before the window fills"

    learn.step(30 / 3.6, True, True, True)
    assert learn.learner.frames == 1
    index = learn.learner.bucket_of(30 / 3.6)
    assert learn.learner.buckets[index].fault_observations == 1

  def test_a_clean_window_never_books_a_refusal(self):
    learn = learning()
    feed(learn, 300)
    index = learn.learner.bucket_of(30 / 3.6)
    assert learn.learner.buckets[index].fault_observations == 0


class TestPersistence:
  def test_the_recorded_curve_is_a_valid_param(self):
    learn = learning()
    feed(learn, 1500)
    text = learn.table_text()
    parsed = parse_lead_bound_table(text)
    assert parsed is not None, f"the learner emitted something the parser rejects: {text}"
    bp, values = parsed
    assert values[0] >= values[-1], "the curve must not widen with speed"
    assert max(values) <= 6.0

  def test_summary_carries_the_mode_and_the_curve(self):
    learn = learning(MODE_SHADOW)
    feed(learn, 10)
    summary = learn.summary()
    assert summary["mode"] == MODE_SHADOW
    assert summary["table"] == learn.table_text()
    assert summary["frames"] + 6 == learn.learner.frames + 6  # frames counted pre-window


class TestParamDomain:
  """The settings page reserves 0 for "unset", so the stored value is 1-based."""

  @pytest.mark.parametrize("raw,expected", [
    (0, MODE_SHADOW), ("0", MODE_SHADOW),   # never touched -> the shipped default
    (1, MODE_OFF), ("1", MODE_OFF),
    (2, MODE_SHADOW), ("2", MODE_SHADOW),
    (3, MODE_APPLY), ("3", MODE_APPLY),
  ])
  def test_every_stored_value_maps_to_a_mode(self, raw, expected):
    from openpilot.sunnypilot.selfdrive.controls.lib.lead_bound_learn import mode_from_param
    assert mode_from_param(raw) == expected

  @pytest.mark.parametrize("raw", [None, "", "junk", 7, 99])
  def test_garbage_falls_back_to_shadow_not_apply(self, raw):
    from openpilot.sunnypilot.selfdrive.controls.lib.lead_bound_learn import mode_from_param
    assert mode_from_param(raw) == MODE_SHADOW
