"""Tests for the lateral metrics.

The metric definitions are the contract. Each test below pins one of them, and the
docstrings say which measurement from the project conventions the definition is meant to
reproduce, so a future edit that quietly changes a definition fails here instead of
silently changing every number in the report.
"""

from __future__ import annotations

import glob
import os
from pathlib import Path

import pytest

from tools.sp_lat_tune.metrics import (
  BOUND_EPS_DEG,
  DEFAULT_LEAD_TABLE,
  MAX_LATERAL_JERK,
  SegmentMetrics,
  lead_bound_at,
  route_segments,
  score_segment,
)

REALDATA = Path.home() / ".comma" / "media" / "0" / "realdata"


def _metrics(**overrides) -> SegmentMetrics:
  base = {"route": "r", "segment": 0, "frames_total": 100, "frames_active": 50,
          "toofast_frames": 0, "steer_fault_permanent_frames": 0, "jerk_p99": 1.0,
          "undershoot_aggregate": 1.2, "severe_ratio": 0.1}
  base.update(overrides)
  return SegmentMetrics(**base)


class TestLeadBound:
  def test_the_default_table_is_the_one_the_car_ships(self):
    """A local copy drifted once (2026-10 table kept after the 2026-11 inversion).

    Every sat_ratio in this package is measured against ``DEFAULT_LEAD_TABLE``, so
    a table that is not the shipped one makes the coverage metric meaningless.
    """
    opendbc = pytest.importorskip("opendbc.car.byd.values")
    bp, values = opendbc.CarControllerParams.STEER_ANGLE_ERROR_MAX
    assert DEFAULT_LEAD_TABLE == (tuple(float(v) for v in bp), tuple(float(v) for v in values))

  def test_low_speed_is_flat_at_the_shipped_value(self):
    """0-15 km/h holds 5.0 deg: the EPS is tolerant here (2026-11-06 inversion)."""
    for v in (0.0, 2.0, 4.0):
      assert lead_bound_at(v) == pytest.approx(5.0)

  def test_high_speed_is_flat_at_the_shipped_floor(self):
    assert lead_bound_at(20.0) == pytest.approx(1.5)
    assert lead_bound_at(30.0) == pytest.approx(1.5)

  def test_the_ramp_between_the_knees_falls_with_speed(self):
    """The ship table shrinks with speed; the old one grew. This pins the direction."""
    values = [lead_bound_at(v) for v in (4.17, 6.0, 8.0, 11.0, 15.0, 20.0)]
    assert values == sorted(values, reverse=True)

  def test_a_custom_table_is_honoured(self):
    assert lead_bound_at(15.0, ((0.0, 30.0), (7.0, 7.0))) == 7.0


class TestFeasibility:
  def test_a_clean_trial_is_feasible(self):
    ok, why = _metrics().feasible()
    assert ok, why

  def test_one_attributed_toofast_frame_disqualifies(self):
    """Not a cost: the fault escalates to steerTempUnavailable, which hands back.

    This is the 6.0 deg experiment. A weighted objective kept it for a day; a
    constraint kills it on the spot.
    """
    ok, why = _metrics(toofast_frames=1, toofast_attributed=1).feasible()
    assert not ok and "TooFast" in why

  def test_a_fault_with_no_lead_to_blame_does_not_disqualify(self):
    """``steerFaultTemporary`` also latches on a panda snapshot that is not fresh.

    Scanning the corpus with the unattributed count made every trial infeasible:
    812 fault frames across 26 segments, while the same frames carried 0.0-0.5 deg
    of commanded lead -- the startup artifact the project conventions describes under
    "panda_unavailable". Judging a parameter pair on frames it never touched is
    how the sweep gets stuck with no suggestion at all.
    """
    ok, why = _metrics(toofast_frames=12, toofast_attributed=0).feasible()
    assert ok, why

  def test_a_permanent_eps_fault_disqualifies(self):
    ok, why = _metrics(steer_fault_permanent_frames=1).feasible()
    assert not ok and "permanent" in why

  def test_jerk_above_the_openpilot_limit_disqualifies(self):
    ok, why = _metrics(jerk_p99=MAX_LATERAL_JERK + 0.1).feasible()
    assert not ok and "jerk" in why

  def test_a_trial_without_active_frames_disqualifies(self):
    ok, why = _metrics(frames_active=0).feasible()
    assert not ok and "active" in why

  def test_nan_jerk_does_not_disqualify(self):
    """A segment with no usable yaw must not be thrown away for lack of a number."""
    ok, _ = _metrics(jerk_p99=float("nan")).feasible()
    assert ok


class TestBoundEpsilon:
  def test_epsilon_is_small_enough_to_mean_pinned(self):
    """sat_ratio claims the command sits ON the bound; a loose epsilon would lie."""
    assert 0 < BOUND_EPS_DEG <= 0.1


class TestRouteDiscovery:
  def test_a_segment_directory_resolves_to_itself(self, tmp_path):
    seg = tmp_path / "00000051--201b6e1171--3"
    seg.mkdir()
    assert route_segments(seg) == [seg]

  def test_a_route_prefix_resolves_to_every_segment(self, tmp_path):
    for i in range(3):
      (tmp_path / f"00000051--201b6e1171--{i}").mkdir()
    assert len(route_segments(tmp_path / "00000051--201b6e1171")) == 3

  def test_an_unknown_route_yields_nothing(self, tmp_path):
    assert route_segments(tmp_path / "nope") == []


@pytest.mark.skipif(not REALDATA.exists(), reason="no recordings on this machine")
class TestAgainstRealRecording:
  """One real segment, to prove the plumbing actually reads an rlog."""

  @pytest.fixture(scope="class")
  def segment(self):
    candidates = sorted(glob.glob(os.path.join(str(REALDATA), "00000051--*")))
    if not candidates:
      pytest.skip("route 51 not recorded on this machine")
    return candidates[0]

  def test_yaw_rate_is_decoded_from_can_not_carstate(self, segment):
    """carState.yawRate is 0.0 in every frame of this port.

    A metric built on it would report an infinite undershoot for every frame, so
    this asserts the CAN path produced samples at all.
    """
    m = score_segment(segment)
    assert m.yaw_frames > 0

  def test_metrics_are_in_physical_range(self, segment):
    m = score_segment(segment)
    if m.frames_active == 0:
      pytest.skip("segment has no active lateral frames")
    assert 0 <= m.lead_p50 <= 180
    assert m.lead_p99 >= m.lead_p50
    assert 0.0 <= m.sat_ratio <= 1.0
    if m.undershoot_frames:
      assert 0.0 < m.undershoot_aggregate < 100.0
    assert m.toofast_frames >= 0

  def test_undershoot_aggregate_is_the_ratio_of_sums(self, segment):
    """The optimiser scores this one, so it must be the robust definition."""
    m = score_segment(segment)
    if not m.undershoot_frames:
      pytest.skip("no undershoot samples")
    # mean-of-ratios and ratio-of-sums are different statistics; the aggregate is
    # the one that does not blow up when the car is barely turning.
    assert m.undershoot_aggregate != m.undershoot_mean or m.undershoot_frames < 5
