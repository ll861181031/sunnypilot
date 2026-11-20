"""Trial store for longitudinal scoring.

One row per scored segment. Unlike the lateral store there is no parameter pair
yet: openpilot longitudinal currently ships exactly one configuration
(stopAccel=-2.0, ki=0, actuatorDelay=0.15), so a row records the configuration in
force (``stop_accel`` etc. read from the recording's carParams) rather than a swept
pair. If a sweep is ever run, the same join answers "which configuration produced
this".

SQLite for the same reason as the lateral store: the same segment gets rescored as
metric definitions change, and the primary key makes that an upsert.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

DEFAULT_DB = Path.home() / ".comma" / "sp_long_tune.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS segments (
  route        TEXT NOT NULL,
  segment      INTEGER NOT NULL,
  scored_at    REAL NOT NULL,
  metrics      TEXT NOT NULL,
  PRIMARY KEY (route, segment)
);
"""

METRIC_FIELDS = (
  "frames_long_active", "duration_s", "v_ego_median", "a_target_p50",
  "gain", "corr", "err_p50", "err_p90", "req_p50", "req_p90",
  "aego_p50", "aego_p90", "jerk_p99", "stop_hold_s", "resume_count",
  "stock_acc_events", "openpilot_long",
  # Added when predictive acceleration boost was ported. Rows scored before that
  # simply lack these keys, which is why every reader below goes through .get().
  "boost_p90", "boost_max", "boost_s", "boost_share",
  "err_p90_boosted", "err_p90_unboosted",
)


class TrialStore:
  def __init__(self, path: str | Path = DEFAULT_DB):
    self.path = Path(path)
    self.path.parent.mkdir(parents=True, exist_ok=True)
    self.conn = sqlite3.connect(self.path)
    self.conn.executescript(SCHEMA)
    self.conn.commit()

  def close(self) -> None:
    self.conn.close()

  def __enter__(self) -> "TrialStore":
    return self

  def __exit__(self, *exc) -> None:
    self.close()

  def add(self, metrics) -> None:
    payload = {k: getattr(metrics, k) for k in METRIC_FIELDS}
    self.conn.execute(
      "INSERT OR REPLACE INTO segments VALUES (?,?,?,?)",
      (metrics.route, metrics.segment, time.time(), json.dumps(payload)),  # noqa: TID251 - record timestamp only
    )
    self.conn.commit()

  def rows(self) -> list[dict]:
    out = []
    for route, segment, scored_at, blob in self.conn.execute(
        "SELECT route, segment, scored_at, metrics FROM segments ORDER BY route, segment"):
      row = json.loads(blob)
      row.update(route=route, segment=segment, scored_at=scored_at)
      out.append(row)
    return out

  def report(self) -> str:
    rows = [r for r in self.rows() if r.get("frames_long_active")]
    if not rows:
      return "no engaged segments recorded"
    def _num(row: dict, name: str):
      v = row.get(name)
      return v if isinstance(v, (int, float)) and v == v else None
    def wavg(name: str) -> float:
      # .get(), not []: rows scored before the boost fields existed have no key.
      pairs = [(v, r.get("frames_long_active") or 0) for r in rows
               if (v := _num(r, name)) is not None and (r.get("frames_long_active") or 0)]
      return sum(v * w for v, w in pairs) / sum(w for _, w in pairs) if pairs else float("nan")
    frames = sum(r["frames_long_active"] for r in rows)
    lines = [
      f"segments with longitudinal engaged: {len(rows)}   frames: {frames}",
      f"gain (weighted): {wavg('gain'):.3f}   corr: {wavg('corr'):.3f}",
      f"err P50: {wavg('err_p50'):.3f}   P90: {wavg('err_p90'):.3f} m/s^2",
      f"jerk P99: {wavg('jerk_p99'):.2f} m/s^3   stock ACC events: {sum(r.get('stock_acc_events') or 0 for r in rows)}",
      f"standstill hold: {sum(r.get('stop_hold_s') or 0.0 for r in rows):.1f} s   resumes: {sum(r.get('resume_count') or 0 for r in rows)}",
      _boost_line(rows),
    ]
    worst = sorted(rows, key=lambda r: -(_num(r, "err_p90") if _num(r, "err_p90") is not None else -1))[:10]
    lines.append("")
    lines.append(f"{'route':>28} {'seg':>4} {'v':>5} {'gain':>6} {'corr':>6} {'errP90':>7} {'jerk':>6}")
    for r in worst:
      cells = [_num(r, k) for k in ("v_ego_median", "gain", "corr", "err_p90", "jerk_p99")]
      v_med, gain, corr, err90, jerk = (float("nan") if c is None else c for c in cells)
      lines.append(f"{r['route']:>28} {r['segment']:>4} {v_med:>5.1f} "
                   f"{gain:>6.2f} {corr:>6.2f} {err90:>7.2f} {jerk:>6.2f}")
    return "\n".join(lines)


def _boost_line(rows: list[dict]) -> str:
  """One line for the predictive boost, or an explicit "not recorded"."""
  scored = [r for r in rows if isinstance(r.get("boost_share"), (int, float)) and r["boost_share"] == r["boost_share"]]
  if not scored:
    return "accel boost: not recorded (segments predate longitudinalPlan.accelBoost)"
  share = sum(r["boost_share"] for r in scored) / len(scored)
  seconds = sum(r.get("boost_s") or 0.0 for r in scored)
  peak = max((r.get("boost_max") or 0.0) for r in scored)
  on = [r["err_p90_boosted"] for r in scored if r.get("err_p90_boosted") == r.get("err_p90_boosted")]
  off = [r["err_p90_unboosted"] for r in scored if r.get("err_p90_unboosted") == r.get("err_p90_unboosted")]
  on_s = f"{sum(on) / len(on):.2f}" if on else "n/a"
  off_s = f"{sum(off) / len(off):.2f}" if off else "n/a"
  return (f"accel boost: on {share * 100:.0f}% of engaged frames, {seconds:.0f} s, peak {peak:.2f} m/s^2 "
          f"| err P90 boosted {on_s} vs unboosted {off_s} m/s^2 ({len(scored)}/{len(rows)} segments)")
