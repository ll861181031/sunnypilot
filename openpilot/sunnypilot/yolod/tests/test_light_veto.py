#!/usr/bin/env python3
"""Tests for the signal-light veto.

The contract under test is deliberately one-sided: a light may only ever add a
reason to stop. So most of these tests assert that nothing happens - wrong state,
stale evidence, missing lane lines, unreadable crops - because "nothing happens"
is what keeps the guard at its stock behaviour.
"""

import numpy as np

from openpilot.sunnypilot.yolod.light_veto import (
  MAX_REASON_DISTANCE_M,
  REASON_STATES,
  EgoLight,
  EgoLightWatch,
  justifies_stop,
)

FOCAL = 1864.0
CX = 672.0
LANE_HALF_WIDTH = 1.75


def straight_lane(xs=None):
  if xs is None:
    xs = np.arange(5.0, 101.0, 1.0)
  left = (xs, np.full_like(xs, LANE_HALF_WIDTH))
  right = (xs, np.full_like(xs, -LANE_HALF_WIDTH))
  return [left, left, right, right]


def box_at(lateral_offset, distance, height_px=None):
  slope = -lateral_offset / distance
  u = CX + slope * FOCAL
  if height_px is None:
    height_px = FOCAL * 1.0 / distance
  return (u - 10.0, 300.0, u + 10.0, 300.0 + height_px)


def light(state="red", distance=40.0, frames=3):
  return EgoLight(state=state, slope=0.0, distance_m=distance, frames_agreeing=frames)


class TestJustifiesStop:
  def test_red_within_range_is_a_reason(self):
    assert justifies_stop(light("red", 40.0))

  def test_yellow_counts_as_a_reason(self):
    # Asymmetry decides this: withholding the reason at a yellow light means
    # suppressing a stop for it, which is the dangerous direction.
    assert "yellow" in REASON_STATES
    assert justifies_stop(light("yellow", 25.0))

  def test_green_is_never_a_reason(self):
    assert not justifies_stop(light("green", 10.0))

  def test_unlit_head_is_never_a_reason(self):
    # An unlit head must not hold the car still at every dark lamp over the road.
    assert not justifies_stop(light("off", 10.0))
    assert not justifies_stop(light("wait_on", 10.0))

  def test_far_light_is_not_what_the_stop_is_about(self):
    assert not justifies_stop(light("red", MAX_REASON_DISTANCE_M + 1.0))

  def test_distance_boundary_is_inclusive(self):
    assert justifies_stop(light("red", MAX_REASON_DISTANCE_M))

  def test_unknown_distance_still_counts(self):
    # The light is already stably assigned to our lane; erring toward "there is a
    # reason" is the safe side.
    assert justifies_stop(light("red", None))

  def test_no_light_is_not_a_reason(self):
    assert not justifies_stop(None)


class TestEgoLightWatch:
  def test_needs_several_frames_before_reporting(self):
    watch = EgoLightWatch(min_agree=3)
    lines = straight_lane()
    boxes = [box_at(0.0, 40.0)]
    assert watch.observe(boxes, ["red"], lines, 1344) is None
    assert watch.observe(boxes, ["red"], lines, 1344) is None
    settled = watch.observe(boxes, ["red"], lines, 1344)
    assert settled is not None
    assert settled.state == "red"
    assert settled.frames_agreeing == 3

  def test_reports_nothing_without_lane_lines(self):
    watch = EgoLightWatch(min_agree=1)
    for _ in range(5):
      assert watch.observe([box_at(0.0, 40.0)], ["red"], None, 1344) is None

  def test_reports_nothing_without_detections(self):
    watch = EgoLightWatch(min_agree=1)
    assert watch.observe([], [], straight_lane(), 1344) is None

  def test_unreadable_crop_does_not_become_a_state(self):
    watch = EgoLightWatch(min_agree=1)
    for _ in range(5):
      assert watch.observe([box_at(0.0, 40.0)], [None], straight_lane(), 1344) is None

  def test_adjacent_lane_light_is_ignored(self):
    watch = EgoLightWatch(min_agree=1)
    # One lane to the right: 5.25 m off centre.
    assert watch.observe([box_at(-5.25, 40.0)], ["red"], straight_lane(), 1344) is None

  def test_stop_reason_follows_the_policy(self):
    watch = EgoLightWatch(min_agree=1)
    boxes = [box_at(0.0, 40.0)]
    green = None
    for _ in range(3):
      green = watch.observe(boxes, ["green"], straight_lane(), 1344)
    assert green is not None
    assert not watch.stop_reason(green)

    watch.reset()
    red = None
    for _ in range(3):
      red = watch.observe(boxes, ["red"], straight_lane(), 1344)
    assert red is not None
    assert watch.stop_reason(red)

  def test_reset_clears_accumulated_agreement(self):
    watch = EgoLightWatch(min_agree=3)
    boxes = [box_at(0.0, 40.0)]
    watch.observe(boxes, ["red"], straight_lane(), 1344)
    watch.observe(boxes, ["red"], straight_lane(), 1344)
    watch.reset()
    assert watch.observe(boxes, ["red"], straight_lane(), 1344) is None

  def test_focal_scales_with_frame_width(self):
    """A 1928-wide frame must use the matching intrinsic, not the 1344 one."""
    watch = EgoLightWatch(min_agree=1)
    wide = 1928
    slope = -0.0 / 40.0
    u = 1928 / 2.0 + slope * (1864.0 * 1928 / 1344.0)
    box = (u - 10.0, 300.0, u + 10.0, 300.0 + FOCAL * 1928 / 1344.0 / 40.0)
    result = watch.observe([box], ["red"], straight_lane(), wide)
    assert result is not None, "a centred light must still be found on a wider frame"
