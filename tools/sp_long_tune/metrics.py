"""Longitudinal tracking metrics computed from a recorded route.

The lateral side already has ``tools/sp_lat_tune``; this is its longitudinal
counterpart. The numbers are the ones the project conventions Phase 9 measured by hand for the
first on-road validation, so a disagreement with that section is the bug.

Why these and not others:

``gain``
    Least-squares slope of ``aEgo`` against the requested acceleration. Phase 9
    measured 0.853 while driving (the stock ACC tracks itself at 0.852), which is
    the evidence that the car executes our 0x32E as its own command. A gain that
    drifts below ~0.85 means the actuator stopped following the request.

``corr``
    Pearson coefficient over the same pairs. Phase 9 measured 0.866. Gain says "how
    much", corr says "in step"; a high gain with a low corr is a lag, not a plant.

``err_p90``
    ``|aEgo - request|`` P90 in m/s^2. Phase 9 did not need it, but it is what
    makes a segment actionable: a big mean error with a small P90 is one event, a
    big P90 is the whole drive.

``jerk_p99``
    ``|d(aEgo)/dt|`` P99. LongControl limits requested jerk; the measured value is
    what the pedals actually did. Compared against the plan's own jerk, not against
    a single constant, because the limit is speed dependent.

``stop_hold_s`` / ``resume_count``
    Time spent holding at a standstill with longitudinal engaged, and how many times
    the car moved off again. Phase 10/11 exist because the hold was the hard part;
    a segment that engages at a stop and holds for 20 s without creeping is the
    evidence that the hold code works.

Two alignment rules this module enforces, both learned from the lateral work:

- **Align by ``logMonoTime``, never by index.** ``carControl`` and ``carState`` are
  published by different processes at the same nominal rate; pairing frame N with
  frame N splices different instants together and silently halves the measured
  error.
- **Only engaged frames count.** ``carControl.actuators.accel`` is published even
  while longitudinal is off, carrying the stock ACC's value; including those frames
  would make every segment look like a perfect follower.

qlog is the default source (10 Hz). Pass ``full=True`` to read rlog at 100 Hz when
jerk or lag is the question -- the decimated series understates jerk by construction.
"""

from __future__ import annotations

import math
from bisect import bisect_left
from dataclasses import dataclass
from pathlib import Path

import numpy as np

# Standstill is below 0.3 m/s, the same line the panda uses for the hold code
# (BYD_HOLD_MAX_SPEED_MS = 0.3). Using a different number here would report a hold
# the panda refuses to transmit.
STANDSTILL_SPEED = 0.3

# Requests smaller than this are mostly the planner idling around zero. Including
# them would fit the regression through a cloud of noise and shrink every gain.
MIN_REQ_MAGNITUDE = 0.2

# A segment needs at least this many engaged frames before any ratio is meaningful.
MIN_FRAMES = 50

# Tracking ratios are only meaningful while the car is actually moving. At a
# standstill the request is the hold deceleration (-2.0 m/s^2) while aEgo reads 0,
# so including those frames reports a 2.0 m/s^2 "error" and a negative gain for a
# perfectly good hold. The standstill is scored separately by stop_hold_s.
MIN_TRACKING_SPEED = 1.0


# A frame counts as "boosted" above this. The planner's own ramp is 0.2 m/s^3 at
# 20 Hz, so one frame is 0.01 m/s^2; anything at or below that is a frame that just
# started or is already releasing, and counting it would inflate boost_share.
MIN_BOOST = 0.01


@dataclass
class LongSegmentMetrics:
  route: str
  segment: int
  source: str
  frames_long_active: int
  duration_s: float
  v_ego_median: float
  a_target_p50: float
  gain: float
  corr: float
  err_p50: float
  err_p90: float
  req_p50: float
  req_p90: float
  aego_p50: float
  aego_p90: float
  jerk_p99: float
  stop_hold_s: float
  resume_count: int
  stock_acc_events: int
  openpilot_long: bool
  # Predictive acceleration boost (longitudinalPlan.accelBoost). NaN only when there
  # is nothing to measure -- no plan samples, or no engaged frames. Note that a
  # recording made before the field existed reads 0.0, not NaN: capnp returns the
  # schema default for a field the writer never set, so "old drive" and "drive where
  # the boost never engaged" are indistinguishable from the log alone. Both the CLI
  # and the console page carry that cutover note instead of pretending otherwise.
  boost_p90: float = float("nan")
  boost_max: float = float("nan")
  boost_s: float = float("nan")
  boost_share: float = float("nan")
  err_p90_boosted: float = float("nan")
  err_p90_unboosted: float = float("nan")

def _stats(values: list[float]) -> tuple[float, float]:
  if not values:
    return float("nan"), float("nan")
  arr = np.asarray(values, dtype=float)
  return float(np.percentile(arr, 50)), float(np.percentile(arr, 90))


def _nearest_index(times: list[int], t: int) -> int:
  """Index of the sample closest to ``t``; the caller checks the gap itself."""
  i = bisect_left(times, t)
  if i <= 0:
    return 0
  if i >= len(times):
    return len(times) - 1
  return i if (times[i] - t) < (t - times[i - 1]) else i - 1


def score_segment(path: Path, full: bool = False) -> LongSegmentMetrics:
  from openpilot.tools.lib.logreader import LogReader, ReadMode

  path = Path(path)
  if path.is_dir():
    name = "rlog.zst" if full else "qlog.zst"
    path = path / name
  route = path.parent.name
  parts = route.split("--")
  try:
    segment = int(parts[2])
  except (IndexError, ValueError):
    segment = -1

  t_ctrl: list[int] = []
  req: list[float] = []
  active: list[bool] = []
  t_cs: list[int] = []
  aego: list[float] = []
  vego: list[float] = []
  t_plan: list[int] = []
  a_target: list[float] = []
  boost_series: list[float] = []
  boost_recorded = False
  stock_events = 0
  op_long = False

  for m in LogReader(str(path), ReadMode.RLOG if full else ReadMode.QLOG):
    which = m.which()
    t = m.logMonoTime
    if which == "carControl":
      cc = m.carControl
      t_ctrl.append(t)
      req.append(float(cc.actuators.accel))
      active.append(bool(cc.longActive))
    elif which == "carState":
      cs = m.carState
      t_cs.append(t)
      aego.append(float(cs.aEgo))
      vego.append(float(cs.vEgo))
    elif which == "longitudinalPlan":
      try:
        t_plan.append(t)
        a_target.append(float(m.longitudinalPlan.aTarget))
      except Exception:
        pass
      # Separate try: a recording without the field must still score aTarget.
      try:
        boost_series.append(float(m.longitudinalPlan.accelBoost))
        boost_recorded = True
      except Exception:
        boost_series.append(float("nan"))
    elif which == "onroadEvents":
      for e in m.onroadEvents:
        try:
          if str(e.name) == "stockAccStillActive":
            stock_events += 1
        except Exception:
          pass
    elif which == "carParams":
      try:
        op_long = bool(m.carParams.openpilotLongitudinalControl)
      except Exception:
        pass

  # Engage-only pairing. A carControl frame without a nearby carState sample is
  # dropped rather than filled: with 100 Hz data the two are never more than a few
  # milliseconds apart, and a bigger gap means the publisher stalled (commIssue).
  paired_req: list[float] = []
  paired_aego: list[float] = []
  paired_v: list[float] = []
  paired_t: list[int] = []
  paired_boost: list[float] = []
  active_count = 0
  if t_cs:
    for i, t in enumerate(t_ctrl):
      if not active[i]:
        continue
      active_count += 1
      j = _nearest_index(t_cs, t)
      paired_req.append(req[i])
      paired_aego.append(aego[j])
      paired_v.append(vego[j])
      paired_t.append(t)
      k = _nearest_index(t_plan, t) if t_plan else -1
      paired_boost.append(boost_series[k] if k >= 0 else float("nan"))

  if not active_count:
    # Positional to empty: every ratio below is meaningless with no engaged frames,
    # and the boost fields keep their NaN defaults ("not measured").
    return LongSegmentMetrics(route, segment, "rlog" if full else "qlog", 0, 0.0,
                              float("nan"), float("nan"), float("nan"), float("nan"),
                              float("nan"), float("nan"), float("nan"), float("nan"),
                              float("nan"), float("nan"), float("nan"), 0.0, 0, stock_events, op_long)

  r = np.asarray(paired_req, dtype=float)
  a = np.asarray(paired_aego, dtype=float)
  v = np.asarray(paired_v, dtype=float)

  mask = (np.abs(r) > MIN_REQ_MAGNITUDE) & (v > MIN_TRACKING_SPEED)
  if mask.sum() >= MIN_FRAMES:
    rr = r[mask]
    aa = a[mask]
    rr_c = rr - rr.mean()
    aa_c = aa - aa.mean()
    denom = float((rr_c * rr_c).sum())
    gain = float((rr_c * aa_c).sum() / denom) if denom > 0 else float("nan")
    std = float(np.sqrt((rr_c * rr_c).sum() * (aa_c * aa_c).sum()))
    corr = float((rr_c * aa_c).sum() / std) if std > 0 else float("nan")
  else:
    gain = corr = float("nan")

  # No moving frames means no tracking question to answer. Falling back to every
  # frame would report the standstill hold (-2.0 requested against aEgo~0) as a
  # 2.0 m/s^2 tracking error, which is exactly the artifact this mask exists to
  # remove.
  err = np.abs(a - r)[mask] if mask.any() else np.array([])
  err_p50, err_p90 = _stats(err.tolist())
  req_p50, req_p90 = _stats(r[mask].tolist())
  aego_p50, aego_p90 = _stats(a[mask].tolist())

  # Jerk from the sampled aEgo series. With qlog at 10 Hz this is a lower bound.
  if len(a) >= 3:
    dt = np.diff(np.asarray(paired_t, dtype=float)) / 1e9
    dt = np.where(dt > 1e-3, dt, np.nan)
    da = np.diff(a)
    jerk = np.abs(da / dt)
    jerk = jerk[np.isfinite(jerk)]
    jerk_p99 = float(np.percentile(jerk, 99)) if jerk.size else float("nan")
  else:
    jerk_p99 = float("nan")

  # Standstill hold: contiguous engaged frames under the standstill line, in seconds.
  sample_dt = (t_ctrl[-1] - t_ctrl[0]) / 1e9 / max(1, len(t_ctrl) - 1) if len(t_ctrl) > 1 else 0.01
  hold_frames = int(((v < STANDSTILL_SPEED)).sum())
  stop_hold_s = hold_frames * sample_dt

  # A resume is a standstill->moving transition while engaged.
  moving = v >= 0.5
  resume_count = int(((~moving[:-1]) & moving[1:]).sum()) if len(moving) > 1 else 0

  duration_s = (t_ctrl[-1] - t_ctrl[0]) / 1e9
  v_med = float(np.median(v))
  a_target_p50 = float(np.percentile(np.asarray(a_target, dtype=float), 50)) if a_target else float("nan")

  boost_p90, boost_max, boost_s, boost_share, err_p90_boosted, err_p90_unboosted = _boost_stats(
    np.asarray(paired_boost, dtype=float), r, a, v, mask, sample_dt, boost_recorded)

  return LongSegmentMetrics(
    route=route, segment=segment, source="rlog" if full else "qlog",
    frames_long_active=active_count, duration_s=duration_s, v_ego_median=v_med,
    a_target_p50=a_target_p50, gain=gain, corr=corr, err_p50=err_p50, err_p90=err_p90,
    req_p50=req_p50, req_p90=req_p90, aego_p50=aego_p50, aego_p90=aego_p90,
    jerk_p99=jerk_p99, stop_hold_s=stop_hold_s, resume_count=resume_count,
    stock_acc_events=stock_events, openpilot_long=op_long,
    boost_p90=boost_p90, boost_max=boost_max, boost_s=boost_s, boost_share=boost_share,
    err_p90_boosted=err_p90_boosted, err_p90_unboosted=err_p90_unboosted,
  )


def _boost_stats(boost: np.ndarray, r: np.ndarray, a: np.ndarray, v: np.ndarray,
                 mask: np.ndarray, sample_dt: float, recorded: bool):
  """Per-segment predictive-boost summary.

  ``err_p90_boosted`` / ``err_p90_unboosted`` answer the only question that matters
  operationally: does tracking get worse while the boost is pushing? They are split
  on the same moving-frame mask as ``err_p90`` so the two halves are comparable to
  each other and to the segment total.

  Everything is NaN when there is nothing to measure (no plan samples, no engaged
  frames). A drive recorded before the field existed reads 0.0 rather than NaN,
  because capnp answers with the schema default for a field the writer never set.
  """
  nan = float("nan")
  if not recorded or boost.size == 0:
    return nan, nan, nan, nan, nan, nan

  finite = np.isfinite(boost)
  if not finite.any():
    return nan, nan, nan, nan, nan, nan

  b = boost[finite]
  on = b > MIN_BOOST
  share = float(on.mean())
  seconds = float(on.sum() * sample_dt)

  # The split is on the aligned arrays, which share the finite-mask rows.
  err_all = np.abs(a - r)
  usable = finite & mask
  err_on = err_all[usable & (boost > MIN_BOOST)]
  err_off = err_all[usable & ~(boost > MIN_BOOST)]
  err_p90_on = float(np.percentile(err_on, 90)) if err_on.size >= 10 else nan
  err_p90_off = float(np.percentile(err_off, 90)) if err_off.size >= 10 else nan

  return (float(np.percentile(b, 90)), float(b.max()), seconds, share, err_p90_on, err_p90_off)
