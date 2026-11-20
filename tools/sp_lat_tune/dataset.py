"""Trial store for lateral parameter sweeps.

One row per scored route segment, keyed by the parameter pair that was in force.
The parameters come from ``carParamsSP`` inside the recording, so a row can never
disagree with the drive it describes.

SQLite rather than CSV because the same route gets rescored as metrics change, and
because "which parameters produced this" is a join, not a column you trust.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path

DEFAULT_DB = Path.home() / ".comma" / "sp_lat_tune.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS segments (
  route        TEXT NOT NULL,
  segment      INTEGER NOT NULL,
  lead_tenths  INTEGER NOT NULL,
  rate_tenths  INTEGER NOT NULL,
  angle_max_tenths INTEGER NOT NULL,
  scored_at    REAL NOT NULL,
  metrics      TEXT NOT NULL,
  PRIMARY KEY (route, segment)
);
CREATE INDEX IF NOT EXISTS idx_params ON segments (lead_tenths, rate_tenths);
"""

# Every field the optimiser reads. Kept explicit so a schema change in
# SegmentMetrics cannot silently feed NaN into the objective.
METRIC_FIELDS = (
  "frames_active", "duration_s", "v_ego_median",
  "lead_p50", "lead_p90", "lead_p99", "sat_ratio",
  "undershoot_p50", "undershoot_p75", "undershoot_p90",
  "undershoot_mean", "undershoot_aggregate", "undershoot_deficit",
  "severe_ratio", "undershoot_frames", "jerk_p99",
  "toofast_frames", "eps_toofast_frames", "toofast_attributed", "steer_fault_permanent_frames",
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

  def __enter__(self) -> TrialStore:
    return self

  def __exit__(self, *exc) -> None:
    self.close()

  def add(self, metrics) -> None:
    """Insert or replace one segment's metrics."""
    payload = {k: getattr(metrics, k) for k in METRIC_FIELDS}
    self.conn.execute(
      "INSERT OR REPLACE INTO segments VALUES (?,?,?,?,?,?,?)",
      (metrics.route, metrics.segment, metrics.lead_tenths, metrics.rate_tenths,
       metrics.angle_max_tenths, time.time(), json.dumps(payload)),  # noqa: TID251 - a record timestamp, not a control-loop clock
    )
    self.conn.commit()

  def rows(self) -> list[dict]:
    out = []
    query = "SELECT route, segment, lead_tenths, rate_tenths, angle_max_tenths, scored_at, metrics FROM segments ORDER BY scored_at"
    for route, segment, lead, rate, angle_max, scored_at, blob in self.conn.execute(query):
      row = json.loads(blob)
      row.update(route=route, segment=segment, lead_tenths=lead, rate_tenths=rate,
                 angle_max_tenths=angle_max, scored_at=scored_at)
      out.append(row)
    return out

  def trials(self) -> dict[tuple[int, int], dict]:
    """Aggregate segments into one entry per parameter pair.

    Aggregation is frame-weighted. Segments with no active lateral frames are
    skipped -- they carry no information about tracking and would drag every mean
    towards zero.
    """
    grouped: dict[tuple[int, int], list[dict]] = {}
    for row in self.rows():
      if not row["frames_active"]:
        continue
      grouped.setdefault((row["lead_tenths"], row["rate_tenths"]), []).append(row)

    out: dict[tuple[int, int], dict] = {}
    for key, rows in grouped.items():
      weight = sum(r["frames_active"] for r in rows)
      agg = {"lead_tenths": key[0], "rate_tenths": key[1], "segments": len(rows),
             "frames_active": weight,
             "toofast_frames": sum(r.get("toofast_frames", 0) for r in rows),
             "eps_toofast_frames": sum(r.get("eps_toofast_frames", 0) for r in rows),
             "toofast_attributed": sum(r.get("toofast_attributed", r.get("toofast_frames", 0)) for r in rows),
             "steer_fault_permanent_frames": sum(r.get("steer_fault_permanent_frames", 0) for r in rows)}
      for name in ("undershoot_aggregate", "undershoot_mean", "undershoot_deficit",
                   "severe_ratio", "sat_ratio", "jerk_p99", "lead_p90", "lead_p99"):
        pairs = [(r[name], r["frames_active"]) for r in rows if r[name] == r[name]]
        agg[name] = (sum(v * w for v, w in pairs) / sum(w for _, w in pairs)) if pairs else float("nan")
      # Aggregate the undershoot ratio by magnitude, not by mean-of-ratios: summing
      # the per-segment ratios would let a 2-second segment weigh as much as a
      # minute-long one.
      out[key] = agg
    return out

  def report(self) -> str:
    trials = self.trials()
    if not trials:
      return "no trials recorded"
    header = f"{'lead':>5} {'rate':>5} {'segs':>5} {'frames':>7} {'under':>6} {'severe':>7} {'sat':>5} {'jerk':>5} {'toofast':>7}  feasible"
    lines = [header]
    for key in sorted(trials):
      t = trials[key]
      ok = t["toofast_frames"] == 0 and t["steer_fault_permanent_frames"] == 0 and t["jerk_p99"] <= 3.6
      # 0 means "the compiled default was in force", not "zero degrees". Showing it
      # as 0.0 would read as a measurement, which it is not.
      lead_cell = "dflt" if not t["lead_tenths"] else f"{t['lead_tenths']/10:.1f}"
      rate_cell = "dflt" if not t["rate_tenths"] else f"{t['rate_tenths']/10:.1f}"
      cells = [
        f"{lead_cell:>5}",
        f"{rate_cell:>5}",
        f"{t['segments']:5d}",
        f"{t['frames_active']:7d}",
        f"{t['undershoot_aggregate']:6.2f}",
        f"{100*t['severe_ratio']:6.1f}%",
        f"{100*t['sat_ratio']:4.0f}%",
        f"{t['jerk_p99']:5.2f}",
        f"{t['toofast_frames']:7d}",
        "yes" if ok else "NO",
      ]
      lines.append(" ".join(cells))
    return "\n".join(lines)


def metrics_to_row(metrics) -> dict:
  row = {k: getattr(metrics, k) for k in METRIC_FIELDS}
  row.update(asdict(metrics))
  return row
