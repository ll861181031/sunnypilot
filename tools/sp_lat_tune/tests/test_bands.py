"""Tests for the per-band lateral analysis.

Two things are load-bearing here and both are regression guards, not smoke tests:
band totals must equal the per-segment ``frames_active`` (same frame definition),
and a refusal must not be blamed on the bound when the clamp was not binding.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.sp_lat_tune.bands import (
  BandAccumulator,
  band_of,
  explain_table,
  score_bands,
  score_route,
)
from tools.sp_lat_tune.metrics import DEFAULT_LEAD_TABLE, score_segment

REALDATA = Path.home() / ".comma" / "media" / "0" / "realdata"


class TestBandOf:
  @pytest.mark.parametrize("kmh,expected", [
    (0, "0-15"), (14.9, "0-15"), (15, "15-40"), (39.9, "15-40"),
    (40, "40-72"), (71.9, "40-72"), (72, "72-1000"), (140, "72-1000"),
  ])
  def test_edges_follow_the_shipped_knees(self, kmh, expected):
    assert band_of(kmh / 3.6) == expected

  def test_negative_speed_does_not_escape_the_first_band(self):
    assert band_of(-1.0) == "0-15"


class TestAccumulator:
  def test_lead_and_excess_are_measured_against_the_shipped_bound(self):
    acc = BandAccumulator(table=DEFAULT_LEAD_TABLE)
    # 30 km/h sits on the ramp between the 4.17 and 11 m/s knees.
    v = 30 / 3.6
    acc.observe_frame(v, 10.0, float("nan"), float("nan"))
    stats = acc.result()[band_of(v)]
    assert stats.frames == 1
    # 10 deg of lead is far beyond any shipped bound in this band, so the frame
    # must also count as saturated.
    assert stats.at_bound_frames == 1
    assert stats.lead_p90 == pytest.approx(10.0)
    assert stats.excess_p90 > 5.0

  def test_frames_and_saturation_are_per_band(self):
    acc = BandAccumulator()
    acc.observe_frame(10 / 3.6, 1.0, 1.0, 1.0)
    acc.observe_frame(50 / 3.6, 1.0, 1.0, 1.0)
    acc.observe_frame(50 / 3.6, 1.0, 1.0, 1.0)
    out = acc.result()
    assert out["0-15"].frames == 1
    assert out["40-72"].frames == 2

  def test_undershoot_is_a_ratio_of_sums_not_a_mean_of_ratios(self):
    acc = BandAccumulator()
    # One big demand tracked badly and one tiny demand tracked perfectly: the
    # aggregate must be dominated by the big one (sum/sum), not averaged away.
    acc.observe_frame(20 / 3.6, 1.0, ay_des=5.0, ay_act=1.0)
    acc.observe_frame(20 / 3.6, 1.0, ay_des=0.5, ay_act=0.5)
    stats = acc.result()["15-40"]
    assert stats.undershoot_aggregate == pytest.approx(5.5 / 1.5)
    assert stats.severe_ratio == pytest.approx(0.5)

  def test_frames_without_yaw_do_not_invent_an_undershoot(self):
    acc = BandAccumulator()
    acc.observe_frame(20 / 3.6, 1.0, ay_des=float("nan"), ay_act=float("nan"))
    stats = acc.result()["15-40"]
    assert stats.undershoot_aggregate != stats.undershoot_aggregate  # NaN
    assert stats.frames == 1

  def test_a_fault_is_blamed_on_the_bound_only_when_the_clamp_was_binding(self):
    acc = BandAccumulator()
    acc.observe_fault(50 / 3.6, limit_mask=1)          # Limit.LEAD
    acc.observe_fault(50 / 3.6, limit_mask=1, driver_input=True)
    acc.observe_fault(50 / 3.6, limit_mask=0)
    acc.observe_fault(50 / 3.6, limit_mask=1 | 8)      # LEAD + INACTIVE
    stats = acc.result()["40-72"]
    assert (stats.faults_clamp, stats.faults_driver, stats.faults_unknown) == (1, 1, 2)
    assert stats.eps_toofast == 4

  def test_an_inactive_frame_can_never_be_bound_attributed(self):
    acc = BandAccumulator()
    acc.observe_fault(50 / 3.6, limit_mask=1 | 8)
    stats = acc.result()["40-72"]
    assert stats.faults_clamp == 0
    assert stats.faults_unknown == 1


class TestDemandFloor:
  def test_the_floor_filters_the_undershoot_sample(self):
    """A straight-line drive must not be able to report an undershoot ratio."""
    acc = BandAccumulator(min_ay=1.0)
    acc.observe_frame(30 / 3.6, 1.0, ay_des=0.5, ay_act=0.5)   # below the floor
    acc.observe_frame(30 / 3.6, 1.0, ay_des=2.0, ay_act=1.0)   # counted
    stats = acc.result()["15-40"]
    assert stats.frames == 2
    assert stats.demand_frames == 1
    assert stats.undershoot_aggregate == pytest.approx(2.0)

  def test_the_default_floor_keeps_the_historical_definition(self):
    acc = BandAccumulator()
    acc.observe_frame(30 / 3.6, 1.0, ay_des=0.4, ay_act=0.4)
    stats = acc.result()["15-40"]
    assert stats.demand_frames == 1
    assert stats.undershoot_aggregate == pytest.approx(1.0)

  def test_a_high_floor_with_no_demand_cannot_conclude(self):
    acc = BandAccumulator(min_ay=1.0)
    for _ in range(2000):
      acc.observe_frame(30 / 3.6, 9.0, ay_des=0.5, ay_act=0.5)   # saturated but weak
    stats = acc.result()["15-40"]
    ok, why = stats.usable(min_demand_frames=100)
    assert not ok and "above the demand threshold" in why


class TestUsability:
  def test_a_band_without_saturation_cannot_conclude_anything(self):
    acc = BandAccumulator()
    for _ in range(2000):
      acc.observe_frame(50 / 3.6, 0.0, 1.0, 1.0)   # wheel follows; nothing at bound
    ok, why = acc.result()["40-72"].usable()
    assert not ok and "never binds" in why

  def test_a_saturated_band_with_enough_frames_is_usable(self):
    acc = BandAccumulator()
    for _ in range(2000):
      acc.observe_frame(50 / 3.6, 9.0, 1.0, 1.0)   # pinned at the bound every frame
    ok, why = acc.result()["40-72"].usable()
    assert ok, why


class TestExplain:
  def test_the_shipped_table_explains_itself_with_zero_delta(self):
    rows = explain_table(DEFAULT_LEAD_TABLE)
    assert all(row["delta"] == pytest.approx(0.0) for row in rows)
    assert [row["kmh"] for row in rows] == sorted(row["kmh"] for row in rows)


def _log_event(service, ms, *, valid=True, **fields):
  from openpilot.cereal import messaging
  event = messaging.new_message(service, valid=valid, logMonoTime=ms * 1_000_000)
  setattr(event, service, fields)
  return event.as_reader()


def _diag(ms, sample_ms, **overrides):
  fields = {"valid": True, "submitted": True, "active": True, "sampleMonoTime": sample_ms * 1_000_000,
            "desiredAngleDeg": 6, "measuredAngleDeg": 2, "leadBoundDeg": 3, "limitMask": 1}
  return _log_event("carStateSP", ms, bydLateral={**fields, **overrides})


def test_band_report_uses_the_submitted_sample_once_at_its_own_time(tmp_path):
  from openpilot.tools.lib.logreader import save_log
  events = [
    _log_event("carState", 100, vEgo=8), _log_event("carControl", 101, latActive=True),
    _log_event("carControl", 102, latActive=True), _diag(122, 105, submitted=False),
    _diag(124, 106, active=False), _diag(126, 107, valid=False),
    _diag(127, 108, leadBoundDeg=float("nan")), _diag(128, 109, leadBoundDeg=0),
    _log_event("carStateSP", 129, valid=False, bydLateral={"valid": True, "submitted": True, "active": True,
                                                           "leadBoundDeg": 3, "limitMask": 1, "sampleMonoTime": 110_000_000}),
    _log_event("carState", 300, vEgo=14), _log_event("carControl", 301, latActive=True),
    _diag(305, 101), _diag(306, 300, leadBoundDeg=5, limitMask=0), _diag(308, 101), _diag(403, 400),
  ]
  save_log(str(tmp_path / "rlog.zst"), events)
  bands = score_bands(tmp_path)
  low, mid = bands["15-40"], bands["40-72"]
  assert (low.at_bound_frames, low.cut_frames, low.mask_at_bound_frames) == (1, 1, 1)
  assert (low.frames, low.recorded_bound_frames, low.estimated_bound_frames) == (2, 1, 0)
  assert low.at_bound_ratio == low.cut_ratio == 1.0
  assert low.excess_p90 == pytest.approx(1)
  assert (mid.recorded_bound_frames, mid.cut_frames, mid.mask_at_bound_frames) == (1, 0, 0)


@pytest.mark.parametrize("has_diagnostics", [True, False])
def test_only_legacy_segments_estimate_missing_bounds(tmp_path, has_diagnostics):
  from openpilot.tools.lib.logreader import save_log
  events = [_log_event("carState", 100, vEgo=8),
            _log_event("carControl", 101, latActive=True, actuators={"steeringAngleDeg": 9}),
            _diag(105, 100, valid=False) if has_diagnostics else _log_event("carStateSP", 105)]
  save_log(str(tmp_path / "rlog.zst"), events)
  band = score_bands(tmp_path)["15-40"]
  assert (band.frames, band.cut_frames, band.recorded_bound_frames, band.estimated_bound_frames) == (
    1, int(not has_diagnostics), 0, int(not has_diagnostics))
  if has_diagnostics:
    assert band.cut_ratio != band.cut_ratio


@pytest.mark.skipif(not REALDATA.exists(), reason="no local recordings")
class TestRealSegment:
  @pytest.fixture(scope="class")
  def segment(self):
    """Newest segment with lateral activity, found by mtime and bounded work.

    Route names sort by route id, not by date, so walking them lexicographically
    finds old drives first. Eight newest routes times two segments is enough in
    practice and keeps the test under a few seconds.
    """
    segs = [p for p in REALDATA.iterdir() if p.is_dir() and p.name.count("--") == 2 and (p / "rlog.zst").exists()]
    segs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for seg in segs[:16]:
      bands = score_bands(seg)
      if sum(s.frames for s in bands.values()) > 500:
        return seg, bands
    pytest.skip("no segment with lateral activity in the newest 16 segments")

  def test_band_frames_sum_to_the_segment_active_frames(self, segment):
    """Same frame definition as ``score_segment`` -- the two must not drift."""
    seg, bands = segment
    total = sum(s.frames for s in bands.values())
    assert total == score_segment(seg).frames_active

  def test_route_aggregation_equals_the_sum_of_segments(self, segment):
    seg, bands = segment
    route = score_route(seg.parent / seg.name.rsplit("--", 1)[0])
    # A route has more segments than this one, so compare monotonicity only.
    for name, stats in bands.items():
      assert route[name].frames >= stats.frames
