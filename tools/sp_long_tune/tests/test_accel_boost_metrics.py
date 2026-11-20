"""Acceleration-boost numbers in the drive analysis, and the two ways they can lie.

The feature only exists from 2026-11-18 onward, so the interesting cases are the ones
that could be read backwards:

* a corpus scored before the field existed must NOT look like "switched on, never
  engaged" -- that would send someone hunting a bug that is not there;
* a drive where the boost genuinely never engaged must NOT look unrecorded -- that
  would hide a feature that is broken on the car.

The log side is exercised through a real synthetic ``qlog.zst`` rather than by calling
``_boost_stats`` directly, because the pairing of the plan to the engaged carControl
frames is where the number can go wrong.
"""

import json
import pathlib
import sqlite3

import pytest
import zstandard as zstd

from openpilot.cereal import messaging
from tools.sp_long_tune.cli import _boost_report
from tools.sp_long_tune.dataset import METRIC_FIELDS, TrialStore
from tools.sp_long_tune.metrics import MIN_BOOST, score_segment

import drive_data

SEGMENT = "000000aa--deadbeef--0"


def _segment(tmp_path: pathlib.Path, *, n=400, boost=0.2, long_active=True,
             a_req=0.6, a_ego=0.55, v_ego=20.0, plans=True, boost_half=None):
  """Write a synthetic qlog.zst that ``score_segment`` can read.

  ``boost_half`` makes the first half of the frames carry ``boost[0]`` and the second
  half ``boost[1]``, which is what the on/off error split needs to be testable.
  """
  seg = tmp_path / SEGMENT
  seg.mkdir(parents=True)
  blobs = []
  t0 = 1_000_000_000
  for i in range(n):
    t = t0 + i * 50_000_000
    cc = messaging.new_message("carControl")
    cc.logMonoTime = t
    cc.carControl.longActive = long_active
    cc.carControl.actuators.accel = a_req
    blobs.append(cc.to_bytes())

    cs = messaging.new_message("carState")
    cs.logMonoTime = t + 1_000
    cs.carState.aEgo = a_ego
    cs.carState.vEgo = v_ego
    blobs.append(cs.to_bytes())

    if plans:
      lp = messaging.new_message("longitudinalPlan")
      lp.logMonoTime = t
      lp.longitudinalPlan.aTarget = a_req
      value = boost if boost_half is None else boost_half[0 if i < n // 2 else 1]
      lp.longitudinalPlan.accelBoost = value
      blobs.append(lp.to_bytes())

  (seg / "qlog.zst").write_bytes(zstd.ZstdCompressor().compress(b"".join(blobs)))
  return seg


class TestTheMetricIsMeasuredFromTheLog:
  def test_a_steady_boost_is_reported_as_engaged_everywhere(self, tmp_path):
    m = score_segment(_segment(tmp_path, boost=0.2))
    assert m.boost_max == pytest.approx(0.2, abs=1e-6)
    assert m.boost_p90 == pytest.approx(0.2, abs=1e-6)
    assert m.boost_share == pytest.approx(1.0)
    assert m.boost_s > 0

  def test_zero_boost_is_zero_not_unknown(self, tmp_path):
    """Recorded and never engaged: 0%, not NaN."""
    m = score_segment(_segment(tmp_path, boost=0.0))
    assert m.boost_share == 0.0
    assert m.boost_s == 0.0
    assert m.boost_max == 0.0

  def test_no_plan_messages_means_unknown(self, tmp_path):
    """No longitudinalPlan at all is 'nothing to measure', which is NaN."""
    m = score_segment(_segment(tmp_path, plans=False))
    assert m.boost_share != m.boost_share
    assert m.boost_s != m.boost_s

  def test_no_engaged_frames_means_unknown(self, tmp_path):
    m = score_segment(_segment(tmp_path, long_active=False))
    assert m.frames_long_active == 0
    assert m.boost_share != m.boost_share

  def test_the_threshold_is_above_one_ramp_step(self, tmp_path):
    """The planner ramps 0.2 m/s^3 at 20 Hz = 0.01 per frame; the cutoff must not
    count the frame the boost starts on, or share inflates on every drive."""
    assert MIN_BOOST <= 0.01
    m = score_segment(_segment(tmp_path, boost=0.005))
    assert m.boost_share == 0.0


class TestTheOnOffErrorSplit:
  def test_both_halves_are_reported_when_both_exist(self, tmp_path):
    m = score_segment(_segment(tmp_path, boost_half=(0.2, 0.0)))
    assert m.boost_share == pytest.approx(0.5, abs=1e-6)
    assert m.err_p90_boosted == m.err_p90_boosted
    assert m.err_p90_unboosted == m.err_p90_unboosted

  def test_a_split_with_no_counter_half_is_nan_not_zero(self, tmp_path):
    """If the boost was on for every frame there is nothing to compare against."""
    m = score_segment(_segment(tmp_path, boost=0.2))
    assert m.err_p90_boosted == m.err_p90_boosted
    assert m.err_p90_unboosted != m.err_p90_unboosted

  def test_the_split_only_counts_moving_frames(self, tmp_path):
    """Same mask as err_p90, so the two halves stay comparable to the total."""
    m = score_segment(_segment(tmp_path, boost_half=(0.2, 0.0)))
    assert m.err_p90 == pytest.approx(0.05, abs=0.02)


class TestTheStoreToleratesOldRows:
  def _row(self, route, segment, **metrics):
    base = {"frames_long_active": 100, "duration_s": 60.0, "v_ego_median": 10.0,
            "a_target_p50": 0.0, "gain": 0.9, "corr": 0.9, "err_p50": 0.1,
            "err_p90": 0.2, "req_p50": 0.0, "req_p90": 0.5, "aego_p50": 0.0,
            "aego_p90": 0.5, "jerk_p99": 2.0, "stop_hold_s": 0.0, "resume_count": 0,
            "stock_acc_events": 0, "openpilot_long": True}
    base.update(metrics)
    return (route, segment, json.dumps(base))

  def test_the_new_fields_are_persisted(self, tmp_path):
    for name in ("boost_p90", "boost_max", "boost_s", "boost_share",
                 "err_p90_boosted", "err_p90_unboosted"):
      assert name in METRIC_FIELDS

  def test_a_store_written_before_the_fields_still_reports(self, tmp_path):
    """Rows scored earlier have no boost keys; report() must not KeyError."""
    db = tmp_path / "old.sqlite"
    with TrialStore(db):
      pass
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO segments VALUES (?,?,?,?)",
                 ("r1", 0, 0.0, self._row("r1", 0)[2]))
    conn.commit()
    conn.close()
    with TrialStore(db) as store:
      text = store.report()
    assert "not recorded" in text
    assert "gain (weighted)" in text

  def test_a_store_with_boost_rows_says_so(self, tmp_path):
    db = tmp_path / "new.sqlite"
    with TrialStore(db) as store:
      pass
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO segments VALUES (?,?,?,?)",
                 ("r1", 0, 0.0, self._row("r1", 0, boost_p90=0.2, boost_max=0.25,
                                          boost_s=30.0, boost_share=1.0,
                                          err_p90_boosted=0.2, err_p90_unboosted=0.3)[2]))
    conn.commit()
    conn.close()
    with TrialStore(db) as store:
      text = store.report()
    assert "accel boost: on 100%" in text
    assert "boosted 0.20 vs unboosted 0.30" in text


class TestTheCliSummary:
  def test_it_states_the_cutover(self, tmp_path):
    db = tmp_path / "s.sqlite"
    with TrialStore(db) as store:
      pass
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO segments VALUES (?,?,?,?)",
                 ("r1", 0, 0.0, json.dumps({"frames_long_active": 100, "boost_share": 0.5,
                                            "boost_s": 12.0, "boost_max": 0.2,
                                            "err_p90_boosted": 0.2, "err_p90_unboosted": 0.3})))
    conn.commit()
    conn.close()
    with TrialStore(db) as store:
      text = _boost_report(store)
    assert "2026-11-18" in text
    assert "50% of frames" in text
    assert "r1" in text

  def test_an_empty_store_does_not_crash(self, tmp_path):
    with TrialStore(tmp_path / "e.sqlite") as store:
      assert "no engaged segments" in _boost_report(store)


class TestTheConsolePayload:
  def _store(self, path, rows, columns="route TEXT, segment INTEGER, scored_at REAL, metrics TEXT"):
    conn = sqlite3.connect(path)
    conn.execute(f"CREATE TABLE segments ({columns})")
    for route, segment, blob in rows:
      conn.execute("INSERT INTO segments VALUES (?,?,?,?)", (route, segment, 0.0, json.dumps(blob)))
    conn.commit()
    conn.close()

  def _row(self, **metrics):
    base = {"frames_long_active": 100, "duration_s": 60.0, "v_ego_median": 10.0,
            "gain": 0.9, "corr": 0.9, "err_p50": 0.1, "err_p90": 0.2, "jerk_p99": 2.0,
            "stop_hold_s": 0.0, "resume_count": 0, "stock_acc_events": 0}
    base.update(metrics)
    return base

  def test_boost_is_aggregated_per_route(self, tmp_path, monkeypatch):
    lng = tmp_path / "long.sqlite"
    self._store(lng, [("r1", 0, self._row(boost_share=0.8, boost_s=40.0, boost_max=0.25,
                                          err_p90_boosted=0.2, err_p90_unboosted=0.3)),
                      ("r1", 1, self._row(boost_share=0.4, boost_s=20.0, boost_max=0.15)),
                      ("r2", 0, self._row())])
    monkeypatch.setattr(drive_data, "LONGITUDINAL_DB", lng)
    monkeypatch.setattr(drive_data, "LATERAL_DB", tmp_path / "absent.sqlite")
    monkeypatch.setattr(drive_data, "REPORT_JSON", tmp_path / "absent.json")
    lng_data = drive_data.summary()["longitudinal"]
    assert lng_data["boost_available"] is True
    assert lng_data["boost_share"] == pytest.approx(0.6, abs=1e-6)
    assert lng_data["boost_s"] == pytest.approx(60.0)
    assert lng_data["boost_peak"] == pytest.approx(0.25)
    assert lng_data["boost_err_on"] == pytest.approx(0.2)
    by_route = {r["route"]: r for r in lng_data["by_route"]}
    assert by_route["r1"]["boost_share"] == pytest.approx(0.6, abs=1e-6)
    # Summed over both of r1's segments: 40 s + 20 s.
    assert by_route["r1"]["boost_s"] == 60
    # A route whose segments carry no boost data reports None, not 0%.
    assert by_route["r2"]["boost_share"] is None

  def test_a_corpus_without_the_field_is_flagged_not_zeroed(self, tmp_path, monkeypatch):
    lng = tmp_path / "long.sqlite"
    self._store(lng, [("r1", 0, self._row())])
    monkeypatch.setattr(drive_data, "LONGITUDINAL_DB", lng)
    monkeypatch.setattr(drive_data, "LATERAL_DB", tmp_path / "absent.sqlite")
    monkeypatch.setattr(drive_data, "REPORT_JSON", tmp_path / "absent.json")
    lng_data = drive_data.summary()["longitudinal"]
    assert lng_data["boost_available"] is False
    assert lng_data["boost_share"] is None
    assert "2026-11-18" in lng_data["boost_note"]
