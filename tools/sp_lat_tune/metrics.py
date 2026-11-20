"""Lateral tracking metrics computed from a recorded route.

Every number here has a counterpart that was measured by hand before this module
existed. The definitions are those measurements, not new ones: if a number here
disagrees with the project conventions, the disagreement is the bug.

Two things are deliberately absent.

- **No estimate of what the car would have done under other parameters.** The
  lateral loop is closed. A recording only contains the response to the commands
  that were actually sent, so a parameter sweep cannot be evaluated offline. The
  only offline question this module can answer is "how did this drive go".
- **No optimisation.** This module reports; `optimize` decides.

Definitions, all taken from the project conventions:

``lead``
    ``|commanded angle - measured wheel angle|`` in degrees. This is the error
    signal that drives the EPS, not just a safety cap -- capping it at 4 deg was
    measured to hold the wheel to about 11 deg/s.

``undershoot``
    ``|desired lateral accel| / |actual lateral accel|``. 1.0 means the car did
    exactly what the model asked. the project conventions treats >1.2 as understeer and >2.0 as
    severe.

``sat_ratio``
    Fraction of active frames whose lead sits on the configured bound. This is the
    direct measure of "the bound is the thing holding the car back".

``jerk_p99``
    ``v * d(yaw_rate)/dt``. Compared against openpilot's MAX_LATERAL_JERK of 3.6.

``toofast_frames``
    Frames where the EPS itself reports a temperature fault (TooFast). This is a
    hard constraint, not a cost: it escalates to steerTempUnavailable, which is
    SOFT_DISABLE + NO_ENTRY.

Yaw rate comes from CAN 0x222 rather than ``carState.yawRate``. This port never
populates the latter -- it reads 0.0 in every recorded frame -- so a metric built
on it would silently report an undershoot of infinity.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

# Copy of the shipped commanded-angle lead bound, in the (speeds m/s, degrees)
# form used by opendbc/car/byd/values.py. It is only a fallback: the real table is
# imported below. A hard-coded copy silently kept the 2026-10 table (4.0/4.0/5.5/
# 5.5) after the 2026-11-06 inversion, so every sat_ratio this package reported
# between then and 2026-11-18 was measured against a bound the car no longer used.
_LEAD_TABLE_FALLBACK = ((0.0, 4.1667, 11.0, 20.0), (5.0, 5.0, 4.5, 1.5))


def _shipped_lead_table() -> tuple[tuple[float, ...], tuple[float, ...]]:
  """The table the controller actually ships, or the fallback when opendbc is absent."""
  try:
    from opendbc.car.byd.values import CarControllerParams
    bp, values = CarControllerParams.STEER_ANGLE_ERROR_MAX
    return (tuple(float(v) for v in bp), tuple(float(v) for v in values))
  except Exception:
    return _LEAD_TABLE_FALLBACK


DEFAULT_LEAD_TABLE = _shipped_lead_table()

# openpilot's own lateral jerk ceiling. Not a tuning knob.
MAX_LATERAL_JERK = 3.6

# Below this desired lateral acceleration the undershoot ratio is dominated by
# noise (the car is going straight and yaw_rate is near zero).
UNDERSHOOT_MIN_AY = 0.3

# A frame counts as "on the bound" when the lead is within this many degrees of it.
# A fault only counts against a parameter pair when the commanded lead was large
# enough to be plausible; see the toofast_attributed field.
TOOFAST_ATTRIBUTION_DEG = 1.0

BOUND_EPS_DEG = 0.05

# Yaw rate sensor. Address and bus were confirmed by decoding a recording: bus 0
# carries +-0.13 rad/s of real signal, bus 2 only ever shows the sensor's LSB.
YAW_ADDRESS = 0x222
YAW_BUS = 0
YAW_SIGNAL = "YAW_SENSOR"


def _percentile(values: np.ndarray, q: float) -> float:
  if values.size == 0:
    return float("nan")
  return float(np.percentile(values, q))


@dataclass
class SegmentMetrics:
  """Metrics for one contiguous stretch of active lateral control."""

  route: str
  segment: int
  # What was actually in force. Recovered from carParamsSP, which card.py logs
  # every 50 s; 0 means "the compiled default was used", not "zero".
  lead_tenths: int = 0
  rate_tenths: int = 0
  angle_max_tenths: int = 0
  # Exposure. A metric without these is not comparable to anything.
  frames_total: int = 0
  frames_active: int = 0
  duration_s: float = 0.0
  v_ego_min: float = float("nan")
  v_ego_median: float = float("nan")
  v_ego_max: float = float("nan")
  # Tracking.
  lead_p50: float = float("nan")
  lead_p90: float = float("nan")
  lead_p99: float = float("nan")
  sat_ratio: float = float("nan")
  # Undershoot. Three definitions, because the historical number was never
  # written down precisely enough to reproduce:
  #               likely meant, but dominated by frames with little yaw
  #   _aggregate  sum(ay_des)/sum(ay_act) -- frame-count independent, the least
  #               noisy, and the one the optimiser scores
  #   _deficit    mean(1 - ay_act/ay_des) -- "what fraction was not delivered"
  undershoot_p50: float = float("nan")
  undershoot_p75: float = float("nan")
  undershoot_p90: float = float("nan")
  undershoot_mean: float = float("nan")
  undershoot_aggregate: float = float("nan")
  undershoot_deficit: float = float("nan")
  severe_ratio: float = float("nan")
  undershoot_frames: int = 0
  # Dynamics.
  jerk_p99: float = float("nan")
  yaw_frames: int = 0
  # Faults -- hard constraints, never costs.
  toofast_frames: int = 0
  # Subset of toofast_frames that can plausibly be blamed on the commanded lead.
  # steerFaultTemporary also latches on a panda snapshot that is not fresh, which
  # is a startup artifact rather than an EPS refusal -- counting those in the
  # feasibility filter disqualified every segment in the corpus (812 frames across
  # 26 segments, while the same frames carried a measured lead of 0.0-0.5 deg).
  toofast_attributed: int = 0
  # Raw EPS pulses from carStateSP.epsTooFast. steerFaultTemporary debounces them
  # and also fires for panda problems, so this is the only count that names the EPS.
  eps_toofast_frames: int = 0
  steer_fault_permanent_frames: int = 0
  # Which bound table the sat_ratio was measured against.
  lead_table: list = field(default_factory=list)

  def to_dict(self) -> dict:
    return asdict(self)

  def feasible(self, *, jerk_limit: float = MAX_LATERAL_JERK) -> tuple[bool, str]:
    """Hard constraints. A metric set failing any of these is not a candidate.

    Kept separate from any objective on purpose: trading a TooFast frame for a
    lower undershoot ratio is how the 6.0 deg experiment went wrong.
    """
    if self.frames_active == 0:
      return False, "no active frames"
    if self.toofast_attributed > 0:
      return False, f"EPS reported TooFast under load in {self.toofast_attributed} frames"
    if self.steer_fault_permanent_frames > 0:
      return False, f"EPS reported a permanent fault in {self.steer_fault_permanent_frames} frames"
    if not math.isnan(self.jerk_p99) and self.jerk_p99 > jerk_limit:
      return False, f"jerk p99 {self.jerk_p99:.2f} > {jerk_limit}"
    return True, ""


def lead_bound_at(v_ego: float, table=DEFAULT_LEAD_TABLE) -> float:
  speeds, bounds = table
  return float(np.interp(v_ego, speeds, bounds))


def _as_route_dir(route: str | Path) -> Path:
  path = Path(route)
  if path.is_dir():
    return path
  candidates = sorted(p for p in path.parent.glob(path.name + "--*") if p.is_dir())
  if len(candidates) == 1:
    return candidates[0]
  if not candidates:
    raise FileNotFoundError(f"no route segments match {route}")
  raise ValueError(f"{route} matches {len(candidates)} segments; pass one segment")


def _yaw_parser():
  """CANParser for the yaw sensor, or None when the DBC cannot be loaded.

  Returning None rather than raising keeps ``score_segment`` usable on a machine
  without opendbc built; the metrics that do not need yaw still come out.
  """
  try:
    from opendbc.can.parser import CANParser
    from opendbc.car import Bus
    from opendbc.car.byd.values import DBC
    dbc = DBC[next(iter(DBC))][Bus.pt]
    return CANParser(dbc, [(YAW_SIGNAL, 0)], YAW_BUS)
  except Exception:
    return None


def score_segment(
  route_dir: str | Path,
  *,
  lead_table=DEFAULT_LEAD_TABLE,
  min_active_frames: int = 50,
) -> SegmentMetrics:
  """Score one route segment from its rlog.

  Reads carState / carControl / modelV2 / carParamsSP and walks them together on
  logMonoTime, with yaw rate decoded from CAN 0x222. Only frames with ``latActive``
  contribute to the tracking metrics; everything else still counts towards
  ``frames_total``.
  """
  from openpilot.tools.lib.logreader import LogReader

  path = _as_route_dir(route_dir)
  route = path.name.rsplit("--", 1)[0]
  try:
    segment = int(path.name.rsplit("--", 1)[1])
  except (IndexError, ValueError):
    segment = -1

  leads: list[float] = []
  undershoots: list[float] = []
  undershoot_des: list[float] = []
  undershoot_act: list[float] = []
  speeds: list[float] = []
  yaw_series: list[tuple[int, float, float]] = []  # (mono_ns, yaw_rate, v_ego)
  frames_total = 0
  frames_active = 0
  toofast = 0
  toofast_times: list[int] = []
  eps_toofast_times: list[int] = []
  ctrl_times: list[int] = []
  fault_permanent = 0
  lead_tenths = rate_tenths = angle_max_tenths = 0
  t_first = t_last = None

  yaw_parser = _yaw_parser()
  yaw_rate = 0.0
  yaw_seen = False

  cs = None
  mv = None

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
            # Sample the derivative at the sensor's own rate. Appending per
            # carState would repeat the same yaw value between updates and turn
            # quantisation into spikes: measured jerk p99 went 8.4 -> 1.x on the
            # same segment purely from this.
            yaw_series.append((msg.logMonoTime, yaw_rate, float(cs.vEgo) if cs is not None else 0.0))
          except (KeyError, IndexError):
            pass
    elif which == "carState":
      cs = msg.carState
      if cs.steerFaultTemporary:
        toofast += 1
        toofast_times.append(msg.logMonoTime)
      if cs.steerFaultPermanent:
        fault_permanent += 1
      if t_first is None:
        t_first = msg.logMonoTime
      t_last = msg.logMonoTime
    elif which == "modelV2":
      mv = msg.modelV2
    elif which == "carStateSP":
      # The raw EPS pulse, not steerFaultTemporary: the latter also latches on a
      # panda snapshot that is not fresh, and on this car that startup artifact was
      # 365 frames in one segment while 0x1FC carried zero TORQUE_TEMP_FAILED.
      # Judging a parameter pair on it would disqualify a segment the EPS never
      # complained about.
      try:
        if msg.carStateSP.epsTooFast:
          eps_toofast_times.append(msg.logMonoTime)
      except Exception:
        pass
    elif which == "carParamsSP":
      cp = msg.carParamsSP
      lead_tenths = int(getattr(cp, "steerAngleLeadTenths", 0) or 0)
      rate_tenths = int(getattr(cp, "steerRateTenths", 0) or 0)
      angle_max_tenths = int(getattr(cp, "steerAngleMaxTenths", 0) or 0)
    elif which == "carControl":
      frames_total += 1
      if not msg.carControl.latActive or cs is None:
        continue
      frames_active += 1
      ctrl_times.append(msg.logMonoTime)
      v = float(cs.vEgo)
      speeds.append(v)

      commanded = float(msg.carControl.actuators.steeringAngleDeg)
      measured = float(cs.steeringAngleDeg)
      leads.append(abs(commanded - measured))

      # Without a decoded yaw rate every undershoot ratio would be infinite; a
      # missing sensor must remove the metric, not corrupt it.
      if mv is not None and v > 1.0 and yaw_seen:
        ay_des = v * v * abs(float(mv.action.desiredCurvature))
        ay_act = v * abs(yaw_rate)
        if ay_des >= UNDERSHOOT_MIN_AY:
          undershoots.append(ay_des / max(ay_act, 0.05))
          undershoot_des.append(ay_des)
          undershoot_act.append(ay_act)

  jerk: list[float] = []
  for (t0, y0, v0), (t1, y1, _) in zip(yaw_series, yaw_series[1:], strict=False):
    dt = (t1 - t0) * 1e-9
    if 1e-4 < dt < 0.1:
      jerk.append(abs(v0 * (y1 - y0) / dt))

  # Attribute each fault to the lead that was on the wire at that instant. 1.0 deg
  # is the floor of the measured fault buckets: 0-2 deg sits at 0.03% across 2.24 M
  # active frames, so a fault below it cannot be the bound's doing.
  attributed = 0
  if ctrl_times and leads:
    t_arr = np.asarray(ctrl_times, dtype=np.int64)
    lead_arr_tmp = np.asarray(leads, dtype=float)
    for t in eps_toofast_times:
      i = int(np.searchsorted(t_arr, t))
      i = min(max(i, 0), len(t_arr) - 1)
      if lead_arr_tmp[i] > TOOFAST_ATTRIBUTION_DEG:
        attributed += 1

  lead_arr = np.asarray(leads, dtype=float)
  und_arr = np.asarray(undershoots, dtype=float)
  des_arr = np.asarray(undershoot_des, dtype=float)
  act_arr = np.asarray(undershoot_act, dtype=float)
  speed_arr = np.asarray(speeds, dtype=float)
  jerk_arr = np.asarray(jerk, dtype=float)

  sat = float("nan")
  if lead_arr.size:
    bounds = np.interp(speed_arr, lead_table[0], lead_table[1])
    sat = float(np.mean(lead_arr >= bounds - BOUND_EPS_DEG))

  out = SegmentMetrics(
    route=route,
    segment=segment,
    lead_tenths=lead_tenths,
    rate_tenths=rate_tenths,
    angle_max_tenths=angle_max_tenths,
    frames_total=frames_total,
    frames_active=frames_active,
    duration_s=0.0 if t_first is None or t_last is None else (t_last - t_first) * 1e-9,
    lead_p50=_percentile(lead_arr, 50),
    lead_p90=_percentile(lead_arr, 90),
    lead_p99=_percentile(lead_arr, 99),
    sat_ratio=sat,
    undershoot_p50=_percentile(und_arr, 50),
    undershoot_p75=_percentile(und_arr, 75),
    undershoot_p90=_percentile(und_arr, 90),
    undershoot_mean=float(und_arr.mean()) if und_arr.size else float("nan"),
    undershoot_aggregate=(float(des_arr.sum() / act_arr.sum())
                          if und_arr.size and act_arr.sum() > 0 else float("nan")),
    undershoot_deficit=(float(np.mean(1.0 - act_arr / np.maximum(des_arr, 1e-6)))
                        if und_arr.size else float("nan")),
    severe_ratio=float(np.mean(und_arr > 2.0)) if und_arr.size else float("nan"),
    undershoot_frames=int(und_arr.size),
    jerk_p99=_percentile(jerk_arr, 99),
    yaw_frames=len(yaw_series),
    toofast_frames=toofast,
    eps_toofast_frames=len(eps_toofast_times),
    toofast_attributed=attributed,
    steer_fault_permanent_frames=fault_permanent,
    lead_table=[list(lead_table[0]), list(lead_table[1])],
  )
  if out.frames_active < min_active_frames:
    out.sat_ratio = float("nan")
  if speed_arr.size:
    out.v_ego_min = float(speed_arr.min())
    out.v_ego_median = float(np.median(speed_arr))
    out.v_ego_max = float(speed_arr.max())
  return out


def route_segments(route: str | Path) -> list[Path]:
  path = Path(route)
  if path.is_dir() and "--" in path.name:
    return [path]
  return sorted(p for p in path.parent.glob(path.name + "--*") if p.is_dir())


def score_route(
  route: str | Path,
  *,
  lead_table=DEFAULT_LEAD_TABLE,
  min_active_frames: int = 50,
) -> dict:
  """Score every segment of a route and aggregate.

  Aggregation is frame-weighted, not segment-weighted: a 3-second segment must not
  carry the same weight as a 60-second one.
  """
  segments = route_segments(route)
  if not segments:
    raise ValueError(f"no route segments match {route}")
  scored = [score_segment(s, lead_table=lead_table, min_active_frames=min_active_frames) for s in segments]
  scored = [s for s in scored if s.frames_active > 0]
  if not scored:
    raise ValueError(f"no active lateral frames in {route}")

  total = sum(s.frames_active for s in scored)
  agg = {
    "route": scored[0].route,
    "segments": len(scored),
    "frames_active": total,
    "duration_s": sum(s.duration_s for s in scored),
    "toofast_frames": sum(s.toofast_frames for s in scored),
    "steer_fault_permanent_frames": sum(s.steer_fault_permanent_frames for s in scored),
    "undershoot_frames": sum(s.undershoot_frames for s in scored),
    "yaw_frames": sum(s.yaw_frames for s in scored),
  }
  # Frame-weighted mean of each per-segment figure.
  for name in ("lead_p50", "lead_p90", "lead_p99", "undershoot_p50", "undershoot_p75",
               "undershoot_p90", "undershoot_mean", "undershoot_aggregate",
               "undershoot_deficit", "sat_ratio", "jerk_p99", "severe_ratio"):
    pairs = [(getattr(s, name), s.frames_active) for s in scored if not math.isnan(getattr(s, name))]
    if pairs:
      weights = np.asarray([w for _, w in pairs], dtype=float)
      agg[name] = float(np.average([v for v, _ in pairs], weights=weights))
    else:
      agg[name] = float("nan")
  agg["params"] = {
    "lead_tenths": scored[0].lead_tenths,
    "rate_tenths": scored[0].rate_tenths,
    "angle_max_tenths": scored[0].angle_max_tenths,
  }
  return agg
