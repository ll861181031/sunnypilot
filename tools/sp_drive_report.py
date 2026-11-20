#!/usr/bin/env python3
"""Population report over every recorded segment: why the car took over, and where.

This is the missing middle layer between the raw logs and the tuning tools. The
existing tools each answer one question (`sp_lat_tune` scores lateral tracking,
`byd_lead_learn` replays the bound learner, `byd_route_audit` checks CAN
assumptions). None of them answers "across all 3000 segments, which feature
caused the takeovers, and how often" -- which is what decides where a fix goes.

Read-only: it opens qlog.zst files and never touches CAN, USB, Params or panda.

Definitions, and why they are these:

``disengage``
    ``selfdriveState.enabled`` going true -> false. That is the moment the wheel
    went back to the driver. The alert string present at that frame names the
    cause, but only if it is a disable alert; an override or a lane-change prompt
    also clears ``enabled`` under MADS, so the kind is kept separately.

``alert``
    ``selfdriveState.alertType`` while it is non-empty and different from the
    previous frame. A single soft disable repeats the same string for the whole
    hold, so counting frames would report one fault 200 times.

``qlog precision``
    qlog decimates ``selfdriveState`` to 10 Hz, so edges carry +/-100 ms. That is
    enough to count causes, not enough to measure hand-over latency; use an rlog
    for that.

Usage::

    python tools/sp_drive_report.py --root ~/.comma/media/0/realdata
    python tools/sp_drive_report.py --root ~/.comma/media/0/realdata --json out.json
    python tools/sp_drive_report.py --route 0000019d--a55a5e8b17
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field, asdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
  sys.path.insert(0, str(REPO))

# Alert kinds that mean "the feature withdrew". Anything else (a chime, an
# override prompt, a lane-change nudge) is recorded but never counted as a fault.
DISABLE_KINDS = ("softDisable", "immediateDisable", "userDisable", "startupNoCar",
                 "startupNoControl", "startupMaster")

# bydLateral limitMask bits, mirrored from lateral_diagnostics.py. Kept literal
# so this script does not fail when the enum is renamed.
LIMIT_LEAD = 1
LIMIT_RATE = 2


@dataclass
class SegmentStat:
  route: str = ""
  segment: int = -1
  mtime: float = 0.0
  duration_s: float = 0.0
  ss_frames: int = 0
  engaged_frames: int = 0
  disengages: int = 0
  alerts: dict[str, int] = field(default_factory=dict)
  disengage_alerts: dict[str, int] = field(default_factory=dict)
  onroad_events: dict[str, int] = field(default_factory=dict)
  lead_samples: int = 0
  lead_sum: float = 0.0
  lead_max: float = 0.0
  lead_limited_frames: int = 0
  rate_limited_frames: int = 0
  bound_sum: float = 0.0
  bound_samples: int = 0
  long_active_frames: int = 0
  lat_active_frames: int = 0
  panda_fault_frames: int = 0
  tx_blocked_max: int = 0
  tx_blocked_first: int = 0
  controls_allowed_drops: int = 0
  v_ego_sum: float = 0.0
  v_ego_max: float = 0.0
  v_ego_samples: int = 0
  gear: dict[str, int] = field(default_factory=dict)
  error: str = ""


def split_alert(alert_type: str) -> tuple[str, str]:
  """``steerTempUnavailable/softDisable`` -> (``steerTempUnavailable``, ``softDisable``).

  An alert with no suffix is its own base and has no kind. The suffix is what
  says whether the feature withdrew, so it is kept even when the base repeats.
  """
  if not alert_type:
    return "", ""
  if "/" in alert_type:
    base, kind = alert_type.rsplit("/", 1)
    return base, kind
  return alert_type, ""


def _get(obj, name, default=None):
  try:
    return getattr(obj, name)
  except Exception:
    return default


def scan_segment(qlog: Path) -> SegmentStat:
  from openpilot.tools.lib.logreader import LogReader, ReadMode

  st = SegmentStat()
  dirname = qlog.parent.name
  parts = dirname.split("--")
  st.route = "--".join(parts[:2]) if len(parts) >= 2 else dirname
  try:
    st.segment = int(parts[2])
  except (IndexError, ValueError):
    st.segment = -1
  try:
    st.mtime = qlog.stat().st_mtime
  except OSError:
    pass

  prev_enabled = False
  prev_alert = ""
  prev_controls = None
  t_first = t_last = None

  try:
    for m in LogReader(str(qlog), ReadMode.QLOG):
      t = m.logMonoTime
      t_first = t if t_first is None else t_first
      t_last = t
      which = m.which()

      if which == "selfdriveState":
        ss = m.selfdriveState
        st.ss_frames += 1
        enabled = bool(_get(ss, "enabled", False))
        if enabled:
          st.engaged_frames += 1
        if prev_enabled and not enabled:
          st.disengages += 1
          at = _get(ss, "alertType", "") or ""
          if at:
            base, kind = split_alert(at)
            st.disengage_alerts[at] = st.disengage_alerts.get(at, 0) + 1
        prev_enabled = enabled

        at = _get(ss, "alertType", "") or ""
        if at and at != prev_alert:
          st.alerts[at] = st.alerts.get(at, 0) + 1
        prev_alert = at

      elif which == "carStateSP":
        lat = _get(m.carStateSP, "bydLateral", None)
        if lat is not None and _get(lat, "valid", False):
          desired = float(_get(lat, "desiredAngleDeg", 0.0))
          measured = float(_get(lat, "measuredAngleDeg", 0.0))
          lead = abs(desired - measured)
          st.lead_samples += 1
          st.lead_sum += lead
          st.lead_max = max(st.lead_max, lead)
          mask = int(_get(lat, "limitMask", 0))
          if mask & LIMIT_LEAD:
            st.lead_limited_frames += 1
          if mask & LIMIT_RATE:
            st.rate_limited_frames += 1
          st.bound_sum += float(_get(lat, "leadBoundDeg", 0.0))
          st.bound_samples += 1

      elif which == "carControl":
        cc = m.carControl
        if _get(cc, "latActive", False):
          st.lat_active_frames += 1
        if _get(cc, "longActive", False):
          st.long_active_frames += 1

      elif which == "pandaStates":
        states = m.pandaStates
        blocked = 0
        allowed = True
        faulted = False
        for ps in states:
          if len(_get(ps, "faults", []) or []):
            faulted = True
          blocked = max(blocked, int(_get(ps, "safetyTxBlocked", 0) or 0))
          allowed = allowed and bool(_get(ps, "controlsAllowed", False))
        if faulted:
          st.panda_fault_frames += 1
        if st.tx_blocked_first == 0:
          st.tx_blocked_first = blocked
        st.tx_blocked_max = max(st.tx_blocked_max, blocked)
        if prev_controls is True and not allowed:
          st.controls_allowed_drops += 1
        prev_controls = allowed

      elif which == "carState":
        cs = m.carState
        v = float(_get(cs, "vEgo", 0.0))
        st.v_ego_samples += 1
        st.v_ego_sum += v
        st.v_ego_max = max(st.v_ego_max, v)
        gear = str(_get(cs, "gearShifter", "unknown"))
        st.gear[gear] = st.gear.get(gear, 0) + 1

      elif which == "onroadEvents":
        for e in m.onroadEvents:
          name = str(_get(e, "name", ""))
          if name:
            st.onroad_events[name] = st.onroad_events.get(name, 0) + 1
  except Exception as exc:  # a corrupt segment must not kill the sweep
    st.error = f"{type(exc).__name__}: {exc}"

  if t_first is not None and t_last is not None:
    st.duration_s = (t_last - t_first) / 1e9
  return st


def iter_segments(root: Path) -> list[Path]:
  out = []
  for qlog in sorted(root.glob("*/qlog.zst")):
    try:
      if qlog.stat().st_size == 0:
        continue
    except OSError:
      continue
    out.append(qlog)
  return out


def classify(alert_type: str) -> str:
  base, kind = split_alert(alert_type)
  if kind in ("softDisable", "immediateDisable", "startupNoCar", "startupNoControl", "startupMaster"):
    return "fault"
  if kind == "userDisable":
    return "driver"
  if base and not kind:
    return "info"
  return "other"


def aggregate(stats: list[SegmentStat]) -> dict:
  totals = {
    "segments": 0,
    "errors": 0,
    "duration_s": 0.0,
    "ss_frames": 0,
    "engaged_frames": 0,
    "disengages": 0,
    "lead_samples": 0,
    "lead_sum": 0.0,
    "lead_max": 0.0,
    "lead_limited_frames": 0,
    "rate_limited_frames": 0,
    "long_active_frames": 0,
    "lat_active_frames": 0,
    "panda_fault_frames": 0,
    "tx_blocked_segments": 0,
    "controls_allowed_drops": 0,
  }
  alerts: Counter = Counter()
  disengage_alerts: Counter = Counter()
  onroad_events: Counter = Counter()
  routes: dict[str, dict] = defaultdict(lambda: {"segments": 0, "disengages": 0, "faults": 0,
                                                 "engaged_s": 0.0, "alerts": Counter()})
  bounds: list[float] = []

  for st in stats:
    totals["segments"] += 1
    if st.error:
      totals["errors"] += 1
      continue
    totals["duration_s"] += st.duration_s
    totals["ss_frames"] += st.ss_frames
    totals["engaged_frames"] += st.engaged_frames
    totals["disengages"] += st.disengages
    totals["lead_samples"] += st.lead_samples
    totals["lead_sum"] += st.lead_sum
    totals["lead_max"] = max(totals["lead_max"], st.lead_max)
    totals["lead_limited_frames"] += st.lead_limited_frames
    totals["rate_limited_frames"] += st.rate_limited_frames
    totals["long_active_frames"] += st.long_active_frames
    totals["lat_active_frames"] += st.lat_active_frames
    totals["panda_fault_frames"] += st.panda_fault_frames
    totals["controls_allowed_drops"] += st.controls_allowed_drops
    if st.tx_blocked_max > st.tx_blocked_first:
      totals["tx_blocked_segments"] += 1
    if st.bound_samples:
      bounds.append(st.bound_sum / st.bound_samples)

    for name, n in st.alerts.items():
      alerts[name] += n
    for name, n in st.disengage_alerts.items():
      disengage_alerts[name] += n
    for name, n in st.onroad_events.items():
      onroad_events[name] += n

    r = routes[st.route]
    r["segments"] += 1
    r["disengages"] += st.disengages
    r["engaged_s"] += st.engaged_frames * 0.1  # qlog selfdriveState is 10 Hz
    r["faults"] += sum(n for a, n in st.alerts.items() if classify(a) == "fault")
    for name, n in st.alerts.items():
      r["alerts"][name] += n

  engaged_s = totals["engaged_frames"] * 0.1
  return {
    "totals": {**totals,
               "engaged_s": engaged_s,
               "hours_recorded": totals["duration_s"] / 3600.0,
               "lead_mean": (totals["lead_sum"] / totals["lead_samples"]) if totals["lead_samples"] else 0.0,
               "lead_limited_ratio": (totals["lead_limited_frames"] / totals["lead_samples"]) if totals["lead_samples"] else 0.0,
               "rate_limited_ratio": (totals["rate_limited_frames"] / totals["lead_samples"]) if totals["lead_samples"] else 0.0,
               "bound_mean": (sum(bounds) / len(bounds)) if bounds else 0.0},
    "alerts": dict(alerts.most_common()),
    "disengage_alerts": dict(disengage_alerts.most_common()),
    "onroad_events": dict(onroad_events.most_common(30)),
    "routes": {k: {"segments": v["segments"], "engaged_s": v["engaged_s"],
                   "disengages": v["disengages"], "faults": v["faults"],
                   "top_alerts": dict(v["alerts"].most_common(5))}
               for k, v in routes.items()},
  }


def _fmt_pct(n: float, total: float) -> str:
  return f"{100.0 * n / total:.1f}%" if total else "—"


def render_markdown(agg: dict, stats: list[SegmentStat], top: int = 20) -> str:
  t = agg["totals"]
  lines: list[str] = []
  a = lines.append

  a("# 行车接管 / 问题总表")
  a("")
  a(f"- 段数 **{t['segments']}**（解析失败 {t['errors']}），路线 **{len(agg['routes'])}**")
  a(f"- 接管时长 **{t['engaged_s'] / 3600:.2f} 小时**（selfdriveState 10 Hz 推算），"
    f"横向激活 **{t['lat_active_frames'] * 0.1 / 60:.0f} 分钟**")
  a(f"- 接管事件 **{t['disengages']}** 次，panda 故障帧 **{t['panda_fault_frames']}** 段")
  a(f"- 横向：激活帧 {t['lat_active_frames']}，超前均值 {t['lead_mean']:.2f}°、上限命中 "
    f"{_fmt_pct(t['lead_limited_frames'], t['lead_samples'])}、速率命中 "
    f"{_fmt_pct(t['rate_limited_frames'], t['lead_samples'])}")
  a(f"- 纵向：longActive 帧 {t['long_active_frames']}")
  a("")

  # Alerts that actually withdrew a feature: this is the ranking that matters.
  fault_rows = [(name, n) for name, n in agg["alerts"].items() if classify(name) == "fault"]
  fault_rows.sort(key=lambda x: -x[1])
  a("## 一、按告警类型排行（会真的撒手的功能故障）")
  a("")
  a("| # | 告警 | 分类 | 次数 | 主要功能 |")
  a("| --- | --- | --- | --- | --- |")
  for i, (name, n) in enumerate(fault_rows[:top], 1):
    base, kind = split_alert(name)
    a(f"| {i} | `{name}` | {kind or '—'} | {n} | {base} |")
  if not fault_rows:
    a("| — | 无 | — | 0 | — |")
  a("")

  driver_rows = [(name, n) for name, n in agg["alerts"].items() if classify(name) == "driver"]
  driver_rows.sort(key=lambda x: -x[1])
  a("## 二、司机主动接管（userDisable）")
  a("")
  a("| 告警 | 次数 |")
  a("| --- | --- |")
  for name, n in driver_rows[:top]:
    a(f"| `{name}` | {n} |")
  if not driver_rows:
    a("| 无 | 0 |")
  a("")

  # Routes ranked by fault density rather than raw count, so a long drive does
  # not automatically look worse than a short bad one.
  route_rows = []
  for route, v in agg["routes"].items():
    hours = v["engaged_s"] / 3600.0
    if hours <= 0.01:
      continue
    route_rows.append((route, v["faults"], v["disengages"], hours,
                       v["faults"] / hours, v["disengages"] / hours))
  route_rows.sort(key=lambda x: -x[4])
  a(f"## 三、问题最密集的路线（每接管小时故障数，前 {top}）")
  a("")
  a("| 路线 | 故障 | 接管 | 接管小时 | 故障/小时 | 接管/小时 | 主要告警 |")
  a("| --- | --- | --- | --- | --- | --- | --- |")
  for route, faults, dis, hours, fph, dph in route_rows[:top]:
    top_alerts = ", ".join(f"`{k}`×{v}" for k, v in
                           list(agg["routes"][route]["top_alerts"].items())[:2])
    a(f"| `{route}` | {faults} | {dis} | {hours:.2f} | {fph:.1f} | {dph:.1f} | {top_alerts} |")
  a("")

  ev_rows = list(agg["onroad_events"].items())[:top]
  a("## 四、onroadEvents 计数（原始事件，含瞬时）")
  a("")
  a("| 事件 | 次数 |")
  a("| --- | --- |")
  for name, n in ev_rows:
    a(f"| `{name}` | {n} |")
  if not ev_rows:
    a("| 无 | 0 |")
  a("")

  errs = [s for s in stats if s.error]
  if errs:
    a("## 五、解析失败的段")
    a("")
    for s in errs[:10]:
      a(f"- `{s.route}--{s.segment}`: {s.error}")
    a("")
  return "\n".join(lines)


def main() -> int:
  ap = argparse.ArgumentParser(description="Aggregate takeovers and faults from recorded routes")
  ap.add_argument("--root", default=str(Path.home() / ".comma" / "media" / "0" / "realdata"),
                  help="log root (default: %(default)s)")
  ap.add_argument("--route", action="append", default=None,
                  help="limit to a route prefix, repeatable")
  ap.add_argument("--json", default="", help="write machine-readable output here")
  ap.add_argument("--markdown", default="", help="write the report here (default: stdout)")
  ap.add_argument("--jobs", type=int, default=max(1, min(8, (os.cpu_count() or 4) - 1)))
  ap.add_argument("--top", type=int, default=20)
  args = ap.parse_args()

  root = Path(args.root).expanduser()
  if not root.is_dir():
    print(f"log root not found: {root}", file=sys.stderr)
    return 2
  segments = iter_segments(root)
  if args.route:
    segments = [p for p in segments if any(p.parent.name.startswith(r) for r in args.route)]
  if not segments:
    print("no segments matched", file=sys.stderr)
    return 2

  print(f"scanning {len(segments)} segment(s) with {args.jobs} worker(s)...", file=sys.stderr)
  stats: list[SegmentStat] = []
  if args.jobs > 1 and len(segments) > 8:
    with ProcessPoolExecutor(max_workers=args.jobs) as pool:
      for st in pool.map(scan_segment, segments, chunksize=4):
        stats.append(st)
  else:
    for p in segments:
      stats.append(scan_segment(p))

  agg = aggregate(stats)
  md = render_markdown(agg, stats, top=args.top)

  if args.markdown:
    Path(args.markdown).write_text(md, encoding="utf-8")
  else:
    print(md)

  if args.json:
    payload = {"aggregate": agg,
               "segments": [asdict(s) for s in stats]}
    Path(args.json).write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {args.json}", file=sys.stderr)
  return 0


if __name__ == "__main__":
  sys.exit(main())
