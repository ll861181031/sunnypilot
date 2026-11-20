"""Tests for the trial store.

The property that matters is that a row can never disagree with the drive it
describes, and that aggregation is frame-weighted so a 3-second segment cannot
outvote a minute-long one.
"""

from __future__ import annotations

from tools.sp_lat_tune.dataset import METRIC_FIELDS, TrialStore
from tools.sp_lat_tune.metrics import SegmentMetrics


def _metrics(route="r", segment=0, *, lead=55, rate=24, frames=1000,
             under=1.2, severe=0.1, toofast=0, jerk=1.0) -> SegmentMetrics:
  m = SegmentMetrics(route=route, segment=segment, lead_tenths=lead, rate_tenths=rate,
                     frames_total=frames, frames_active=frames,
                     undershoot_aggregate=under, severe_ratio=severe,
                     toofast_frames=toofast, jerk_p99=jerk, sat_ratio=0.2,
                     undershoot_mean=under, undershoot_deficit=0.1, lead_p90=3.0)
  return m


def test_round_trip_preserves_the_parameters(tmp_path):
  with TrialStore(tmp_path / "t.sqlite") as store:
    store.add(_metrics(lead=70, rate=18))
    rows = store.rows()
  assert len(rows) == 1
  assert rows[0]["lead_tenths"] == 70 and rows[0]["rate_tenths"] == 18


def test_rescoring_replaces_rather_than_duplicates(tmp_path):
  """Metrics change; the drive does not. A rescore must not double-count it."""
  with TrialStore(tmp_path / "t.sqlite") as store:
    store.add(_metrics(under=1.5))
    store.add(_metrics(under=1.1))
    rows = store.rows()
  assert len(rows) == 1
  assert rows[0]["undershoot_aggregate"] == 1.1


def test_trials_group_by_parameter_pair(tmp_path):
  with TrialStore(tmp_path / "t.sqlite") as store:
    store.add(_metrics("a", 0, lead=55))
    store.add(_metrics("a", 1, lead=55))
    store.add(_metrics("b", 0, lead=70))
    trials = store.trials()
  assert set(trials) == {(55, 24), (70, 24)}
  assert trials[(55, 24)]["segments"] == 2


def test_aggregation_is_frame_weighted(tmp_path):
  """A long segment must dominate a short one, or the report lies."""
  with TrialStore(tmp_path / "t.sqlite") as store:
    store.add(_metrics("long", 0, frames=10_000, under=1.0))
    store.add(_metrics("short", 0, frames=100, under=3.0))
    trial = store.trials()[(55, 24)]
  assert trial["undershoot_aggregate"] < 1.1


def test_segments_without_active_frames_are_skipped(tmp_path):
  """A parked segment carries no information about tracking."""
  with TrialStore(tmp_path / "t.sqlite") as store:
    store.add(_metrics("parked", 0, frames=0))
    assert store.trials() == {}


def test_toofast_is_summed_not_averaged(tmp_path):
  """One fault is disqualifying, so the sum is the number that matters."""
  with TrialStore(tmp_path / "t.sqlite") as store:
    store.add(_metrics("a", 0, toofast=2))
    store.add(_metrics("a", 1, toofast=3))
    assert store.trials()[(55, 24)]["toofast_frames"] == 5


def test_report_lists_one_line_per_pair_and_flags_infeasible(tmp_path):
  with TrialStore(tmp_path / "t.sqlite") as store:
    store.add(_metrics("a", 0, lead=55, toofast=0))
    store.add(_metrics("b", 0, lead=70, toofast=4))
    text = store.report()
  # Skip the header row; the data rows are the ones that carry a feasibility verdict.
  lines = [ln for ln in text.splitlines()[1:] if ln.strip()]
  assert len(lines) == 2
  assert any("NO" in ln for ln in lines)
  assert any("yes" in ln for ln in lines)


def test_report_on_an_empty_store_says_so(tmp_path):
  with TrialStore(tmp_path / "t.sqlite") as store:
    assert store.report() == "no trials recorded"


def test_every_scored_field_is_persisted(tmp_path):
  """A field missing from METRIC_FIELDS would silently vanish from the objective."""
  with TrialStore(tmp_path / "t.sqlite") as store:
    store.add(_metrics())
    row = store.rows()[0]
  for name in METRIC_FIELDS:
    assert name in row, name
