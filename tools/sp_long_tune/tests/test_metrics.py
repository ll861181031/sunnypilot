"""Longitudinal metric definitions, and the two artifacts they exist to prevent.

The tests that matter here are the ones about what must NOT be measured: a
standstill hold is not a tracking failure, and a frame with no request is not
evidence about the follower.
"""

from pathlib import Path

import pytest

from tools.sp_long_tune.metrics import (
  MIN_TRACKING_SPEED,
  STANDSTILL_SPEED,
  _nearest_index,
  _stats,
  score_segment,
)

REALDATA = Path.home() / ".comma" / "media" / "0" / "realdata"


class TestHelpers:
  def test_stats_of_nothing_is_nan(self):
    p50, p90 = _stats([])
    assert p50 != p50 and p90 != p90

  def test_nearest_index_picks_the_closer_sample(self):
    times = [0, 100, 200]
    assert _nearest_index(times, 40) == 0
    assert _nearest_index(times, 60) == 1
    assert _nearest_index(times, 1000) == 2
    assert _nearest_index(times, -5) == 0

  def test_standstill_line_matches_the_panda(self):
    """BYD_HOLD_MAX_SPEED_MS is 0.3; a different line here would score a hold the
    panda refuses to transmit."""
    assert STANDSTILL_SPEED == 0.3

  def test_tracking_only_counts_above_the_creep_band(self):
    assert MIN_TRACKING_SPEED > STANDSTILL_SPEED


@pytest.mark.skipif(not REALDATA.exists(), reason="no local recordings")
class TestRecordedSegments:
  def test_a_standstill_hold_is_not_reported_as_a_tracking_error(self):
    """The hold asks for -2.0 m/s^2 while aEgo reads ~0.

    Treating that as tracking error produces err_p90 = 2.01 and a negative gain on
    a perfectly good hold -- which is exactly what the first version of this module
    reported for 25 segments before the moving-frame mask went in.
    """
    seg = REALDATA / "000000bd--39d386ba54--20"
    if not seg.exists():
      pytest.skip("segment not present")
    m = score_segment(seg)
    assert m.frames_long_active > 0
    assert m.v_ego_median < MIN_TRACKING_SPEED
    assert m.err_p90 != m.err_p90  # no moving frames -> no ratio, not 2.0

  def test_a_moving_segment_tracks_with_a_plausible_gain(self):
    seg = REALDATA / "00000199--40b22824ea--2"
    if not seg.exists():
      pytest.skip("segment not present")
    m = score_segment(seg)
    assert m.frames_long_active > 0
    assert 0.5 < m.gain < 1.5, m.gain
    assert m.err_p90 < 1.0, m.err_p90
