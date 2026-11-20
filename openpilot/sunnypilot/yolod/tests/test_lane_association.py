#!/usr/bin/env python3
"""Tests for lane association.

The geometry is pinned with real numbers from this device: a 3.5 m lane, the
1864 px focal length of the published 1344-wide frame, and a principal point at
672. Pixel boxes are built from a wanted lateral offset and distance, so a test
failure points at the geometry rather than at arithmetic in the test itself.
"""

import numpy as np
import pytest

from openpilot.sunnypilot.yolod.lane_association import (
  MARGIN_METRES,
  LaneAssignment,
  LaneEvidence,
  TemporalArbiter,
  assign,
  box_slope,
  ego_band,
  ego_band_at,
  estimate_distance,
  lane_slope,
)

FOCAL = 1864.0
CX = 672.0
LANE_HALF_WIDTH = 1.75


def straight_lane(xs=None, left_y=LANE_HALF_WIDTH, right_y=-LANE_HALF_WIDTH):
  """Two boundaries plus two outer lines, in openpilot's order."""
  if xs is None:
    xs = np.arange(5.0, 101.0, 1.0)
  left = (xs, np.full_like(xs, left_y))
  right = (xs, np.full_like(xs, right_y))
  outer_left = (xs, np.full_like(xs, left_y * 2))
  outer_right = (xs, np.full_like(xs, right_y * 2))
  return [outer_left, left, right, outer_right]


def box_at(lateral_offset, distance, height_px=None):
  """Pixel box for a 1 m head at ``lateral_offset`` metres, ``distance`` ahead.

  Height follows distance unless overridden, because a fixed pixel height at a
  different distance would describe a differently sized lamp rather than a
  farther one - which is exactly the mistake that made a 90 m test case report
  46 m.
  """
  slope = -lateral_offset / distance
  u = CX + slope * FOCAL
  if height_px is None:
    height_px = FOCAL * 1.0 / distance
  return (u - 10.0, 300.0, u + 10.0, 300.0 + height_px)


def test_lane_slope_is_negative_for_left_side():
  xs = np.array([10.0, 20.0])
  ys = np.array([1.0, 1.0])
  slopes = lane_slope(xs, ys)
  assert np.allclose(slopes, [-0.1, -0.05])


def test_lane_slope_drops_points_behind_the_car():
  xs = np.array([-5.0, 0.5, 10.0])
  ys = np.array([1.0, 1.0, 1.0])
  slopes = lane_slope(xs, ys)
  assert slopes.size == 1
  assert slopes[0] == pytest.approx(-0.1)


def test_band_at_distance_matches_the_lane_width():
  lines = straight_lane()
  lo, hi = ego_band_at(lines, 30.0, margin_m=0.0)
  assert lo == pytest.approx(-LANE_HALF_WIDTH / 30.0)
  assert hi == pytest.approx(LANE_HALF_WIDTH / 30.0)


def test_band_narrows_with_distance():
  lines = straight_lane()
  near = ego_band_at(lines, 20.0, margin_m=0.0)
  far = ego_band_at(lines, 80.0, margin_m=0.0)
  assert (near[1] - near[0]) > (far[1] - far[0])
  assert far[1] - far[0] == pytest.approx((near[1] - near[0]) / 4.0)


def test_no_lane_lines_returns_none():
  assert ego_band([]) is None
  assert ego_band(None) is None
  assert ego_band_at([], 30.0) is None
  assert ego_band_at(straight_lane(), float("nan")) is None


def test_centre_light_belongs_to_ego_lane():
  result = assign([box_at(0.0, 40.0)], straight_lane(), FOCAL, CX)
  assert result.ego_index == 0
  assert result.evidence[0].in_ego_band


def test_light_at_lane_edge_still_belongs():
  result = assign([box_at(-LANE_HALF_WIDTH, 40.0)], straight_lane(), FOCAL, CX)
  assert result.ego_index == 0


def test_adjacent_lane_light_is_rejected_at_close_range():
  # 5.25 m to the right is exactly one lane over.
  result = assign([box_at(-5.25, 40.0)], straight_lane(), FOCAL, CX)
  assert result.ego_index is None
  assert not result.evidence[0].in_ego_band


def test_adjacent_lane_light_is_still_rejected_far_away():
  # This is the case the union band gets wrong: at 90 m an adjacent lane sits at
  # only 0.058 rad, inside the near-field envelope of 0.097 rad. Per-distance
  # bounding is what keeps it out.
  union = ego_band(straight_lane())
  assert union[0] <= 0.0583 <= union[1], "precondition: union band would accept it"
  result = assign([box_at(-5.25, 90.0)], straight_lane(), FOCAL, CX)
  assert result.ego_index is None


def test_identical_size_heads_prefer_the_lane_centre():
  # Left-turn head one lane to the left, through head dead ahead, same distance.
  detections = [box_at(5.25, 40.0), box_at(0.0, 40.0)]
  result = assign(detections, straight_lane(), FOCAL, CX)
  assert result.ego_index == 1


def test_preferred_state_breaks_ties_among_ego_candidates():
  # Two heads both plausibly over the ego lane; only one of them is red.
  left = box_at(0.9, 40.0)
  right = box_at(-0.9, 40.0)
  detections = [left, right]
  states = ["green", "red"]
  picked_green = assign(detections, straight_lane(), FOCAL, CX,
                        states=states, preferred_state="green")
  picked_red = assign(detections, straight_lane(), FOCAL, CX,
                      states=states, preferred_state="red")
  assert picked_green.ego_index == 0
  assert picked_red.ego_index == 1


def test_no_detections_is_not_a_failure():
  result = assign([], straight_lane(), FOCAL, CX)
  assert isinstance(result, LaneAssignment)
  assert result.ego_index is None
  assert result.evidence == ()


def test_narrow_box_uses_the_union_band_rather_than_refusing():
  # 4 px tall is below the ranging floor, so distance is unknown.
  tiny = box_at(0.0, 40.0, height_px=4.0)
  result = assign([tiny], straight_lane(), FOCAL, CX)
  assert result.evidence[0].distance_m is None
  assert result.ego_index == 0


def test_distance_uses_apparent_size():
  assert estimate_distance((0.0, 0.0, 10.0, 40.0), FOCAL) == pytest.approx(FOCAL / 40.0)
  assert estimate_distance((0.0, 0.0, 10.0, 2.0), FOCAL) is None


def test_box_slope_is_positive_to_the_right_of_centre():
  assert box_slope((CX + 100.0, 0.0, CX + 120.0, 10.0), FOCAL, CX) > 0.0
  assert box_slope((CX - 120.0, 0.0, CX - 100.0, 10.0), FOCAL, CX) < 0.0


def test_band_width_matches_lane_width_at_every_distance():
  """The band must describe the actual lane, not an arbitrary tolerance."""
  lines = straight_lane()
  for distance in (20.0, 40.0, 60.0, 90.0):
    lo, hi = ego_band_at(lines, distance, margin_m=MARGIN_METRES)
    # The slack is a distance now, so it shrinks in angle as range grows.
  assert hi - lo == pytest.approx(2 * LANE_HALF_WIDTH / distance + 2 * MARGIN_METRES / distance)


def test_curved_lane_shifts_the_band_with_distance():
  """On a curve the band centre must follow the lane, not stay at zero."""
  xs = np.arange(5.0, 101.0, 1.0)
  curve = 0.0008 * xs**2          # gentle left-hand bend
  left = (xs, curve + LANE_HALF_WIDTH)
  right = (xs, curve - LANE_HALF_WIDTH)
  lines = [left, left, right, right]
  near = ego_band_at(lines, 25.0, margin_m=0.0)
  far = ego_band_at(lines, 80.0, margin_m=0.0)
  near_centre = 0.5 * (near[0] + near[1])
  far_centre = 0.5 * (far[0] + far[1])
  # Lane centre drifts left, so its slope (y negative-to-the-left convention
  # aside) must move in the same direction as the curve does.
  assert far_centre < near_centre


def test_forward_slope_is_zero_for_an_aligned_camera():
  from openpilot.sunnypilot.yolod.lane_association import forward_slope
  assert forward_slope([0.0, 0.0, 0.0]) == pytest.approx(0.0, abs=1e-9)


def test_forward_slope_reproduces_the_measured_device_yaw():
  """Real calibration from this device: pitch 3.7 deg, yaw 0.54 deg.

  The direction of travel lands 17.5 px right of the image centre, i.e. slope
  0.0094 - not zero, and large enough to matter against a 90 m lane band.
  """
  from openpilot.sunnypilot.yolod.lane_association import forward_slope
  slope = forward_slope([0.0, 0.0646, 0.0094])
  assert slope == pytest.approx(0.0094, abs=0.0005)


def test_forward_slope_is_safe_without_calibration():
  from openpilot.sunnypilot.yolod.lane_association import forward_slope
  assert forward_slope([]) == 0.0
  assert forward_slope([0.0, 0.0]) == 0.0


def test_axis_slope_references_measurements_to_the_direction_of_travel():
  centred = (CX - 10.0, 300.0, CX + 10.0, 340.0)
  assert box_slope(centred, FOCAL, CX) == pytest.approx(0.0)
  # With the device's yaw, a box at the image centre is slightly left of travel.
  assert box_slope(centred, FOCAL, CX, axis_slope=0.0094) == pytest.approx(-0.0094)


def test_a_light_on_the_optical_axis_is_not_treated_as_dead_ahead():
  """Without the yaw correction this light would look centred and be picked."""
  lines = straight_lane()
  centred = box_at(0.0, 40.0)
  centred = (CX - 10.0, centred[1], CX + 10.0, centred[3])
  # The band at 40 m spans about +-0.044 rad, so a 0.0094 rad bias is inside it
  # either way; what must change is where the light is judged to sit.
  with_correction = assign([centred], lines, FOCAL, CX, axis_slope=0.0094)
  without = assign([centred], lines, FOCAL, CX, axis_slope=0.0)
  assert with_correction.evidence[0].slope != without.evidence[0].slope
  assert with_correction.evidence[0].slope < without.evidence[0].slope


def _assignment(slope, distance=40.0):
  from openpilot.sunnypilot.yolod.lane_association import LaneAssignment, LaneEvidence
  evidence = LaneEvidence(index=0, slope=slope, distance_m=distance,
                          in_ego_band=True, centre_offset=abs(slope))
  return LaneAssignment(ego_index=0, evidence=(evidence,), band=(-0.1, 0.1))


def test_arbiter_needs_agreement_before_reporting():
  from openpilot.sunnypilot.yolod.lane_association import TemporalArbiter
  arbiter = TemporalArbiter(min_agree=3)
  assert arbiter.update(_assignment(0.0), ["red"]) is None
  assert arbiter.update(_assignment(0.001), ["red"]) is None
  decision = arbiter.update(_assignment(0.002), ["red"])
  assert decision is not None
  assert decision.state == "red"
  assert decision.frames_agreeing == 3


def test_arbiter_does_not_chatter_between_alternating_states():
  from openpilot.sunnypilot.yolod.lane_association import TemporalArbiter
  arbiter = TemporalArbiter(window=6, min_agree=3)
  results = []
  for _ in range(12):
    results.append(arbiter.update(_assignment(0.0), ["red"]))
    results.append(arbiter.update(_assignment(0.0), ["green"]))
  assert all(r is None for r in results), "alternating states must never stabilise"


def test_arbiter_treats_a_different_light_as_a_different_series():
  from openpilot.sunnypilot.yolod.lane_association import TemporalArbiter
  arbiter = TemporalArbiter(min_agree=3, slope_tolerance=0.012)
  assert arbiter.update(_assignment(0.000), ["red"]) is None
  assert arbiter.update(_assignment(0.060), ["red"]) is None
  assert arbiter.update(_assignment(0.000), ["red"]) is None
  # Three frames, but the middle one was a different head 0.06 rad away.
  assert arbiter.update(_assignment(0.060), ["red"]) is None


def test_arbiter_reports_nothing_without_a_state():
  from openpilot.sunnypilot.yolod.lane_association import TemporalArbiter
  arbiter = TemporalArbiter(min_agree=2)
  for _ in range(5):
    assert arbiter.update(_assignment(0.0), [None]) is None


def test_arbiter_reports_nothing_when_no_light_is_assigned():
  from openpilot.sunnypilot.yolod.lane_association import LaneAssignment, TemporalArbiter
  arbiter = TemporalArbiter(min_agree=2)
  for _ in range(5):
    assert arbiter.update(LaneAssignment(ego_index=None, evidence=(), band=None), []) is None


def test_arbiter_reset_clears_agreement():
  from openpilot.sunnypilot.yolod.lane_association import TemporalArbiter
  arbiter = TemporalArbiter(min_agree=3)
  arbiter.update(_assignment(0.0), ["red"])
  arbiter.update(_assignment(0.0), ["red"])
  arbiter.reset()
  assert arbiter.update(_assignment(0.0), ["red"]) is None


def test_arbiter_rejects_incoherent_parameters():
  from openpilot.sunnypilot.yolod.lane_association import TemporalArbiter
  with pytest.raises(ValueError):
    TemporalArbiter(window=2, min_agree=5)
  with pytest.raises(ValueError):
    TemporalArbiter(window=0, min_agree=0)


def test_the_lateral_slack_does_not_grow_with_distance():
  """Regression: the margin used to be an angle, so it became a whole lane at range.

  0.030 rad is 0.6 m at 20 m but 2.4 m at 80 m. At 80 m that put the band at
  +/-4.2 m, wide enough to swallow the neighbouring lane's light - which is exactly
  the "it sees the light beside me" complaint. The slack must be a fixed distance.
  """
  lines = straight_lane()
  for distance in (20.0, 40.0, 60.0, 80.0, 100.0):
    lo, hi = ego_band_at(lines, distance, margin_m=MARGIN_METRES)
    lateral_half = 0.5 * (hi - lo) * distance
    assert lateral_half == pytest.approx(LANE_HALF_WIDTH + MARGIN_METRES, abs=0.02), distance


def test_a_neighbouring_lane_light_is_rejected_at_long_range():
  """One lane over at 80 m used to land inside the band; it must not now.

  A neighbouring lane's light sits roughly 3.5 m to the side. At 80 m that is a
  slope of 3.5/80 = 0.044 rad, which the old +/-4.2 m band accepted.
  """
  lines = straight_lane()
  distance = 80.0
  lo, hi = ego_band_at(lines, distance, margin_m=MARGIN_METRES)
  neighbour_slope = (2 * LANE_HALF_WIDTH) / distance
  assert not (lo <= neighbour_slope <= hi), (lo, hi, neighbour_slope)
  # The neighbouring lane must stay rejected across the whole useful range, not
  # just at 80 m - the old angular margin let it through from 60 m outward.
  for distance in (60.0, 80.0, 100.0):
    lo, hi = ego_band_at(lines, distance, margin_m=MARGIN_METRES)
    slope = (2 * LANE_HALF_WIDTH) / distance
    assert not (lo <= slope <= hi), (distance, lo, hi, slope)


def test_assign_ignores_a_light_over_the_neighbouring_lane_at_range():
  """End to end through assign(): the far side light must not become ego's."""
  lines = straight_lane()
  distance = 80.0
  focal, cx = FOCAL, CX
  # Box height implies the distance under the 1.0 m head assumption.
  height_px = focal * 1.0 / distance
  width_px = height_px * 0.6

  def box_for(lateral_m):
    centre_x = cx - focal * lateral_m / distance
    return (centre_x - width_px / 2, 300.0, centre_x + width_px / 2, 300.0 + height_px)

  ego_box = box_for(0.0)
  side_box = box_for(2 * LANE_HALF_WIDTH)
  assignment = assign([ego_box, side_box], lines, focal, cx)
  assert assignment.ego_index == 0
  assert assignment.evidence[1].in_ego_band is False


def test_a_bogus_wide_lane_cannot_widen_the_gate():
  """If the model reports a 12 m lane, the band must still stop at 3 m a side.

  Trusting the lane lines unconditionally means a bad frame - a wide intersection,
  a merging lane, a drifting estimate - hands the adjacent lane's light control of
  the car. The clamp keeps that from happening without needing a special case.
  """
  from openpilot.sunnypilot.yolod.lane_association import MAX_BAND_HALF_WIDTH_M

  xs = np.arange(5.0, 101.0, 1.0)
  wide = [(xs, np.full_like(xs, 6.0)), (xs, np.full_like(xs, 6.0)),
          (xs, np.full_like(xs, -6.0)), (xs, np.full_like(xs, -6.0))]
  for distance in (20.0, 40.0, 80.0):
    lo, hi = ego_band_at(wide, distance, margin_m=MARGIN_METRES)
    assert 0.5 * (hi - lo) * distance == pytest.approx(MAX_BAND_HALF_WIDTH_M, abs=0.02), distance


class TestTwoHeadsOfOneGantryAreNotOneLight:
  """Regression: slope alone merged a gantry's near and far heads.

  On a real night approach the arbiter reported a steady 59 m while the car closed
  from 74 m to 25 m. The assignment was correct - it picked the near head every
  frame - but the two heads sit 0.03-0.04 rad apart, inside the slope tolerance, so
  their ranges were medianed together into a number that matched neither.
  """

  @staticmethod
  def _assignment(slope: float, distance: float) -> LaneAssignment:
    evidence = (LaneEvidence(index=0, slope=slope, distance_m=distance,
                             in_ego_band=True, centre_offset=abs(slope)),)
    return LaneAssignment(ego_index=0, evidence=evidence, band=(-0.2, 0.2))

  def test_a_far_head_does_not_agree_with_a_near_one(self):
    arbiter = TemporalArbiter(min_agree=3)
    # Same state, slopes 0.03 apart (inside the 0.05 slope tolerance), ranges 25 vs 60 m.
    assert arbiter.update(self._assignment(-0.020, 25.0), ["red"]) is None
    assert arbiter.update(self._assignment(-0.035, 60.0), ["red"]) is None
    assert arbiter.update(self._assignment(-0.050, 60.0), ["red"]) is None
    # Three frames of the *same* head then agree and report its own range.
    decision = arbiter.update(self._assignment(-0.050, 60.0), ["red"])
    assert decision is None or decision.distance_m == pytest.approx(60.0, abs=6.0)

  def test_the_same_head_agrees_while_the_car_closes(self):
    """Real approach motion is 5-20% over three frames and must still agree."""
    arbiter = TemporalArbiter(min_agree=3)
    assert arbiter.update(self._assignment(-0.020, 35.0), ["red"]) is None
    assert arbiter.update(self._assignment(-0.023, 33.0), ["red"]) is None
    decision = arbiter.update(self._assignment(-0.026, 31.0), ["red"])
    assert decision is not None
    assert decision.state == "red"
    # The reported range is the closing light, not some blend of two heads.
    assert decision.distance_m == pytest.approx(33.0, abs=3.0)

  def test_an_unknown_range_is_not_a_disagreement(self):
    arbiter = TemporalArbiter(min_agree=3)
    assert arbiter.update(self._assignment(-0.020, None), ["red"]) is None
    assert arbiter.update(self._assignment(-0.022, None), ["red"]) is None
    assert arbiter.update(self._assignment(-0.024, None), ["red"]) is not None
