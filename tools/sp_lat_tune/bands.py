"""Per-speed-band lateral analysis over recorded segments.

Why this exists: the shipped lead bound is not one number, it is a curve, and the
settings page can only *tighten* it. A single per-segment score therefore cannot
say which part of the curve is actually holding the car back. This module splits
one segment (or a whole route) by speed band and reports, per band:

  * how far the commanded lead sits from the bound (``lead`` and ``excess``),
  * how often the bound is reached at all (``at_bound_ratio``) -- without
    saturation there is no information about the bound,
  * tracking quality (``undershoot_aggregate``, ``severe_ratio``),
  * EPS refusals split by cause (bound-driven vs driver-vs-EPS pull).

The split matters because of one measured fact: the deficit is not uniform. On the
2026-11-17 evening drive the low-speed band (15-30 km/h) held p90 lead 8.56 deg and
35.1% saturation while the rest of the drive was fine. Optimising one scalar lead
against such a drive would be dominated by whichever band the route happens to
spend its time in.

Fault attribution is deliberately stricter than ``metrics.toofast_attributed``: a
refusal only counts against the bound when ``limitMask`` says the clamp was
binding AND the driver was not pulling on the wheel. The evening drive's five
``TORQUE_TEMP_FAILED`` frames were all a driver-vs-EPS pull at lead <= 4.91 deg
with the clamp not binding, and treating those as "the bound is too wide" would
point the experiment in the wrong direction.

Read-only: opens rlog.zst files and never touches Params, CAN, USB or panda.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from bisect import bisect_left
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from .metrics import (
  BOUND_EPS_DEG,
  DEFAULT_LEAD_TABLE,
  UNDERSHOOT_MIN_AY,
  YAW_ADDRESS,
  YAW_SIGNAL,
  _as_route_dir,
  _percentile,
  _yaw_parser,
  lead_bound_at,
)

# Band edges in km/h. 15/40/72 are the shipped table's knees (4.17/11.1/20 m/s),
# so a band boundary is also where the bound changes slope.
BANDS_KMH = ((0.0, 15.0), (15.0, 40.0), (40.0, 72.0), (72.0, 1000.0))

# ``Limit.LEAD`` in opendbc/car/byd/carcontroller.py. Kept literal so this module
# still imports when opendbc is not built; the mask is only used for attribution.
LIMIT_LEAD = 1
LIMIT_INACTIVE = 8
MAX_SPEED_SKEW_NS = 50_000_000

# Raw torque above which the frame is treated as "the driver is pulling". The panda
# base threshold is 200 raw counts; the host's relative rule is higher, so this is
# deliberately the looser of the two -- over-attributing to the driver only loses
# information, while under-attributing puts a driver fight on the bound's account.
DRIVER_TORQUE_RAW = 200.0


def band_of(v_ego: float, bands=BANDS_KMH) -> str:
  """Band label for a speed in m/s, using the km/h edges above."""
  kmh = max(0.0, float(v_ego)) * 3.6
  for lo, hi in bands:
    if kmh < hi:
      return f"{lo:g}-{hi:g}"
  return f"{bands[-1][0]:g}-{bands[-1][1]:g}"


@dataclass
class BandStats:
  band: str = ""
  frames: int = 0
  recorded_bound_frames: int = 0
  estimated_bound_frames: int = 0
  at_bound_frames: int = 0
  mask_at_bound_frames: int = 0
  v_median: float = float("nan")
  lead_p50: float = float("nan")
  lead_p90: float = float("nan")
  lead_p95: float = float("nan")
  lead_p99: float = float("nan")
  excess_p50: float = float("nan")
  excess_p90: float = float("nan")
  excess_p95: float = float("nan")
  undershoot_aggregate: float = float("nan")
  severe_ratio: float = float("nan")
  ay_des_sum: float = 0.0
  ay_act_sum: float = 0.0
  cut_frames: int = 0          # lead beyond the bound: the clamp removed something
  demand_frames: int = 0       # frames whose desired lateral accel passed the threshold
  eps_toofast: int = 0
  faults_clamp: int = 0
  faults_driver: int = 0
  faults_unknown: int = 0

  @property
  def bound_frames(self) -> int:
    return self.recorded_bound_frames + self.estimated_bound_frames

  @property
  def at_bound_ratio(self) -> float:
    return self.at_bound_frames / self.bound_frames if self.bound_frames else float("nan")

  @property
  def cut_ratio(self) -> float:
    """请求超前量超过限值的比例；分母只包含可比较限值的样本。"""
    return self.cut_frames / self.bound_frames if self.bound_frames else float("nan")

  def to_dict(self) -> dict:
    out = asdict(self)
    out["at_bound_ratio"] = self.at_bound_ratio
    out["cut_ratio"] = self.cut_ratio
    return out

  def usable(self, *, min_frames: int = 1500, min_at_bound: float = 0.10,
             min_demand_frames: int = 0) -> tuple[bool, str]:
    """Whether this band can say anything about the bound.

    Both gates are about information, not safety: frames tell you the estimate is
    stable, ``at_bound_ratio`` tells you the estimator ever touched the quantity it
    is supposed to learn about.
    """
    if self.bound_frames < min_frames or not self.bound_frames:
      return False, f"可比较限值的样本数 {self.bound_frames} 不足（至少 {max(1, min_frames)}）"
    if not (self.at_bound_ratio >= min_at_bound):
      return False, f"at_bound {self.at_bound_ratio:.1%} < {min_at_bound:.0%} -- bound never binds"
    if self.demand_frames < min_demand_frames:
      return False, f"{self.demand_frames} frames above the demand threshold < {min_demand_frames}"
    return True, ""


class BandAccumulator:
  """Pure accumulator: feed it frames and faults, ask for per-band statistics.

  Kept free of any log/cereal type so the definitions can be tested without a
  recording and reused by the live web page later.
  """

  def __init__(self, table=DEFAULT_LEAD_TABLE, bands=BANDS_KMH, min_ay: float = UNDERSHOOT_MIN_AY):
    self.table = table
    self.bands = bands
    # Undershoot is only meaningful above a floor: at 0.3 m/s^2 a 0.005 rad/s yaw
    # sample turns a tracking ratio into a noise ratio. The default keeps the
    # historical 0.3 m/s^2; the experiment protocol raises it (see
    # LATLONG_PARAM_EXPERIMENT.md) so a straight-line drive cannot report "1.1".
    self.min_ay = float(min_ay)
    self._rows: dict[str, dict] = {}
    for lo, hi in bands:
      self._rows[f"{lo:g}-{hi:g}"] = {
        "leads": [], "excess": [], "speeds": [], "ay_des": [], "ay_act": [], "demand": 0,
        "at_bound": 0, "mask_at_bound": 0, "recorded": 0, "cuts": 0, "faults": [0, 0, 0],
      }

  # --- inputs ---------------------------------------------------------------

  def observe_frame(self, v_ego: float, lead_deg: float | None, ay_des: float, ay_act: float) -> None:
    """计入一次横向请求；只有无诊断的旧分段才用请求角估算限幅。"""
    row = self._rows[band_of(v_ego, self.bands)]
    row["speeds"].append(float(v_ego))
    if lead_deg is not None:
      self.observe_bound(v_ego, lead_deg, lead_bound_at(v_ego, self.table))
    if math.isfinite(ay_des) and math.isfinite(ay_act) and ay_des >= self.min_ay:
      row["ay_des"].append(float(ay_des))
      row["ay_act"].append(float(ay_act))
      row["demand"] += 1

  def observe_bound(self, v_ego: float, lead_deg: float, bound: float, limit_mask: int | None = None) -> None:
    row = self._rows[band_of(v_ego, self.bands)]
    row["leads"].append(float(lead_deg))
    row["excess"].append(max(0.0, float(lead_deg) - bound))
    row["at_bound"] += int(float(lead_deg) >= bound - BOUND_EPS_DEG)
    row["cuts"] += int(float(lead_deg) > bound + BOUND_EPS_DEG)
    if limit_mask is not None:
      row["recorded"] += 1
      row["mask_at_bound"] += int(bool(limit_mask & LIMIT_LEAD))

  def observe_fault(self, v_ego: float, *, limit_mask: int = 0, driver_input: bool = False) -> None:
    """One EPS ``TORQUE_TEMP_FAILED`` frame, classified by likely cause."""
    row = self._rows[band_of(v_ego, self.bands)]
    binding = bool(int(limit_mask) & LIMIT_LEAD) and not bool(int(limit_mask) & LIMIT_INACTIVE)
    if driver_input:
      row["faults"][1] += 1
    elif binding:
      row["faults"][0] += 1
    else:
      row["faults"][2] += 1

  # --- output ---------------------------------------------------------------

  def result(self) -> dict[str, BandStats]:
    out: dict[str, BandStats] = {}
    for name, row in self._rows.items():
      leads = np.asarray(row["leads"], dtype=float)
      excess = np.asarray(row["excess"], dtype=float)
      ay_des = float(np.sum(row["ay_des"])) if row["ay_des"] else 0.0
      ay_act = float(np.sum(row["ay_act"])) if row["ay_act"] else 0.0
      ratios = (np.asarray(row["ay_des"]) / np.maximum(np.asarray(row["ay_act"]), 0.05)
                if row["ay_des"] else np.asarray([]))
      out[name] = BandStats(
        band=name,
        frames=len(row["speeds"]),
        recorded_bound_frames=row["recorded"],
        estimated_bound_frames=int(leads.size) - row["recorded"],
        at_bound_frames=int(row["at_bound"]),
        mask_at_bound_frames=int(row["mask_at_bound"]),
        v_median=float(np.median(row["speeds"])) if row["speeds"] else float("nan"),
        lead_p50=_percentile(leads, 50),
        lead_p90=_percentile(leads, 90),
        lead_p95=_percentile(leads, 95),
        lead_p99=_percentile(leads, 99),
        excess_p50=_percentile(excess, 50),
        excess_p90=_percentile(excess, 90),
        excess_p95=_percentile(excess, 95),
        undershoot_aggregate=(ay_des / ay_act) if ay_act > 0 else float("nan"),
        severe_ratio=float(np.mean(ratios > 2.0)) if ratios.size else float("nan"),
        ay_des_sum=ay_des,
        ay_act_sum=ay_act,
        cut_frames=int(row["cuts"]),
        demand_frames=int(row["demand"]),
        faults_clamp=int(row["faults"][0]),
        faults_driver=int(row["faults"][1]),
        faults_unknown=int(row["faults"][2]),
        eps_toofast=int(sum(row["faults"])),
      )
    return out


def _feed(path: Path, acc: BandAccumulator) -> None:
  """Walk one rlog and push it into ``acc``."""
  from openpilot.tools.lib.logreader import LogReader

  yaw_parser = _yaw_parser()
  yaw_rate = 0.0
  yaw_seen = False
  cs = None
  mv = None
  speeds = []
  controls = []
  has_diagnostics = False
  submissions = {}

  for msg in LogReader(str(path / "rlog.zst")):
    which = msg.which()
    if which == "can":
      if yaw_parser is not None:
        frames = [(c.address, bytes(c.dat), c.src) for c in msg.can]
        if any(addr == YAW_ADDRESS for addr, _, _ in frames):
          yaw_parser.update([(msg.logMonoTime, frames)])
          try:
            yaw_rate = float(yaw_parser.vl[YAW_SIGNAL]["YAW_RATE"])
            yaw_seen = True
          except (KeyError, IndexError):
            pass
    elif which == "carState":
      cs = msg.carState
      if msg.valid and math.isfinite(cs.vEgo):
        speeds.append((msg.logMonoTime, float(cs.vEgo)))
    elif which == "modelV2":
      mv = msg.modelV2
    elif which == "carStateSP":
      sp = msg.carStateSP
      if sp._has("bydLateral"):
        has_diagnostics = True
        diag = sp.bydLateral
        if msg.valid and diag.valid and diag.submitted and diag.active and diag.sampleMonoTime > 0:
          bound = float(diag.leadBoundDeg)
          lead = abs(float(diag.desiredAngleDeg) - float(diag.measuredAngleDeg))
          if math.isfinite(bound) and bound > 0 and math.isfinite(lead):
            submissions.setdefault(diag.sampleMonoTime, (lead, bound, int(diag.limitMask)))
      try:
        if sp.epsTooFast and cs is not None:
          diag = sp.bydLateral
          acc.observe_fault(
            float(cs.vEgo),
            limit_mask=int(diag.limitMask) if diag.valid else 0,
            driver_input=bool(cs.steeringPressed or abs(float(cs.steeringTorque)) >= DRIVER_TORQUE_RAW),
          )
      except Exception:
        pass
    elif which == "carControl":
      cc = msg.carControl
      if not cc.latActive or cs is None:
        continue
      v = float(cs.vEgo)
      lead = abs(float(cc.actuators.steeringAngleDeg) - float(cs.steeringAngleDeg))
      ay_des = v * v * abs(float(mv.action.desiredCurvature)) if mv is not None else float("nan")
      ay_act = v * abs(yaw_rate) if yaw_seen else float("nan")
      controls.append((v, lead, ay_des, ay_act))

  for v, lead, ay_des, ay_act in controls:
    acc.observe_frame(v, None if has_diagnostics else lead, ay_des, ay_act)

  # 诊断在后续循环发布。按采样时间找最近的有效车速，避免跨速度段或跨数据缺口归因。
  speeds.sort()
  for ts, (lead, bound, mask) in submissions.items():
    i = bisect_left(speeds, ts, key=lambda sample: sample[0])
    nearest = min(speeds[max(0, i - 1):i + 1], key=lambda sample: abs(sample[0] - ts), default=None)
    if nearest is not None and abs(nearest[0] - ts) <= MAX_SPEED_SKEW_NS:
      acc.observe_bound(nearest[1], lead, bound, mask)


def score_bands(route_dir: str | Path, *, table=DEFAULT_LEAD_TABLE,
                min_ay: float = UNDERSHOOT_MIN_AY) -> dict[str, BandStats]:
  """Walk one rlog once and return per-band statistics."""
  acc = BandAccumulator(table, min_ay=min_ay)
  _feed(_as_route_dir(route_dir), acc)
  return acc.result()


def score_route(route: str | Path, *, table=DEFAULT_LEAD_TABLE,
                min_ay: float = UNDERSHOOT_MIN_AY) -> dict[str, BandStats]:
  """Aggregate every segment of a route (or a single segment) into one band table.

  One accumulator across segments, not a merge of finished results: the lead
  distributions have to stay pooled for the percentiles to mean anything.
  """
  path = Path(route).expanduser()
  single = path.is_dir() and "--" in path.name
  segments = [path] if single else sorted(p for p in path.parent.glob(path.name + "--*") if p.is_dir())
  if not segments:
    raise FileNotFoundError(f"no route segments match {path}")
  acc = BandAccumulator(table, min_ay=min_ay)
  for seg in segments:
    try:
      _feed(seg, acc)
    except FileNotFoundError:
      continue
  return acc.result()


def segment_dirs(root: str | Path, *, limit: int | None = None) -> list[Path]:
  """Segment directories under ``root``, newest first.

  Sorted by mtime rather than by name: route ids sort by drive number, and the
  drives worth re-scoring are the recent ones.
  """
  base = Path(root).expanduser()
  if base.is_dir() and "--" in base.name:
    return [base]
  segs = [p for p in base.iterdir() if p.is_dir() and "--" in p.name and (p / "rlog.zst").exists()]
  segs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
  return segs[:limit] if limit else segs


def scan(root: str | Path, *, limit: int | None = None, table=DEFAULT_LEAD_TABLE,
         min_ay: float = UNDERSHOOT_MIN_AY) -> dict[str, BandStats]:
  """Pool many segments into one per-band table (the E0 baseline)."""
  acc = BandAccumulator(table, min_ay=min_ay)
  for seg in segment_dirs(root, limit=limit):
    try:
      _feed(seg, acc)
    except FileNotFoundError:
      continue
  return acc.result()


def explain_table(table, speeds_kmh=(5, 15, 25, 40, 55, 72, 90)) -> list[dict]:
  """Shipped vs candidate bound at representative speeds, with the delta."""
  rows = []
  shipped = DEFAULT_LEAD_TABLE
  for kmh in speeds_kmh:
    v = kmh / 3.6
    rows.append({
      "kmh": kmh,
      "shipped": lead_bound_at(v, shipped),
      "candidate": lead_bound_at(v, table),
      "delta": lead_bound_at(v, table) - lead_bound_at(v, shipped),
    })
  return rows


def _fmt(value: float, digits: int = 2) -> str:
  if value != value:
    return "—"
  return f"{value:.{digits}f}"


def _print_bands(bands: dict[str, BandStats]) -> None:
  print("  ".join(("band", "请求帧", "实测/估算", "LEAD/实测", "at_bound", "cut", "lead p50/p90/p95",
                   "excess p90/p95", "ay_n", "under", "severe", "faults(c/d/?)", "v_med")))
  for name, s in bands.items():
    at_bound = f"{_fmt(s.at_bound_ratio * 100, 1):>6}%" if s.bound_frames else "待定"
    cut = f"{_fmt(s.cut_ratio * 100, 1):>5}%" if s.bound_frames else "待定"
    row = [
      f"{name:<9}",
      f"{s.frames:>7}",
      f"{s.recorded_bound_frames}/{s.estimated_bound_frames}",
      f"{s.mask_at_bound_frames}/{s.recorded_bound_frames}" if s.recorded_bound_frames else "无诊断样本",
      at_bound,
      cut,
      f"{_fmt(s.lead_p50)}/{_fmt(s.lead_p90)}/{_fmt(s.lead_p95)}",
      f"{_fmt(s.excess_p90)}/{_fmt(s.excess_p95)}",
      f"{s.demand_frames:>5}",
      _fmt(s.undershoot_aggregate),
      _fmt(s.severe_ratio, 2),
      f"{s.faults_clamp}/{s.faults_driver}/{s.faults_unknown}",
      _fmt(s.v_median, 1),
    ]
    print("  ".join(row))


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(prog="sp lat-bands", description="Per-speed-band lateral analysis")
  sub = parser.add_subparsers(dest="cmd", required=True)

  p = sub.add_parser("score", help="score one route/segment")
  p.add_argument("route")
  p.add_argument("--min-ay", type=float, default=UNDERSHOOT_MIN_AY,
                 help="desired lateral accel floor for the undershoot metric (m/s^2)")
  p.add_argument("--min-ay-frames", type=int, default=0,
                 help="require this many frames above --min-ay before a band may conclude")
  p.add_argument("--json", type=Path)
  p.set_defaults(func=_cmd_score)

  p = sub.add_parser("scan", help="pool many segments under a root directory")
  p.add_argument("root")
  p.add_argument("--limit", type=int, default=None)
  p.add_argument("--min-ay", type=float, default=UNDERSHOOT_MIN_AY)
  p.add_argument("--min-ay-frames", type=int, default=0)
  p.add_argument("--json", type=Path)
  p.set_defaults(func=_cmd_scan)

  p = sub.add_parser("table", help="show the shipped lead bound at representative speeds")
  p.set_defaults(func=_cmd_table)

  args = parser.parse_args(argv)
  return args.func(args)


def _cmd_score(args) -> int:
  bands = score_route(args.route, min_ay=args.min_ay)
  _print_bands(bands)
  if args.json:
    args.json.write_text(json.dumps({k: v.to_dict() for k, v in bands.items()}, indent=2))
  for name, s in bands.items():
    ok, why = s.usable(min_demand_frames=args.min_ay_frames)
    if not ok:
      print(f"{name}: 无结论 —— {why}", file=sys.stderr)
  return 0


def _cmd_scan(args) -> int:
  bands = scan(args.root, limit=args.limit, min_ay=args.min_ay)
  _print_bands(bands)
  if args.json:
    args.json.write_text(json.dumps({k: v.to_dict() for k, v in bands.items()}, indent=2))
  return 0


def _cmd_table(args) -> int:
  for row in explain_table(DEFAULT_LEAD_TABLE):
    print(f"{row['kmh']:>3} km/h: {row['shipped']:.2f} deg")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
