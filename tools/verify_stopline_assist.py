#!/usr/bin/env python3
"""Verify the low-speed stop-line assist on a recorded drive.

Two independent pieces of evidence are required:

1. the cplink shim logged ``stop line <m>`` while driving, and
2. ``longitudinalPlan.longitudinalPlanSource == 5`` (trafficLight) actually won
   the planner merge and the car came to rest.

The shim logs to the user journal; save the relevant lines first:

    journalctl --user -u cplink-shim --since '2026-11-14 08:00' \
        > /tmp/cplink-shim.log
    .venv/bin/python tools/verify_stopline_assist.py <route-or-segment> \
        --journal /tmp/cplink-shim.log

This is read-only; it never starts a stack or sends anything.
"""
from __future__ import annotations

import argparse
import re
from collections import Counter
from pathlib import Path

SOURCE_TRAFFIC_LIGHT = 5
STOP_SPEED = 0.3

# ``stop line 15.6 m row=537 cov=0.99 v=8.0 lead=1e9``
STOP_LINE_RE = re.compile(r"stop line\s+(?P<distance>[0-9.]+)\s*m\s+row=(?P<row>\d+)\s+cov=(?P<coverage>[0-9.]+)\s+v=(?P<speed>[^ ]+)\s+lead=(?P<lead>[^ ]+)")


def parse_journal(path: str) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = STOP_LINE_RE.search(line)
            if m:
                rows.append({
                    "timestamp": " ".join(line.split()[:3]),
                    "distance_m": float(m.group("distance")),
                    "row": int(m.group("row")),
                    "coverage": float(m.group("coverage")),
                    "speed": m.group("speed"),
                    "lead": m.group("lead"),
                })
    return rows


def resolve_rlog(target: str) -> Path:
    p = Path(target).expanduser()
    if p.is_file() and p.name.startswith("rlog"):
        return p
    if p.is_dir() and (p / "rlog.zst").is_file():
        return p / "rlog.zst"
    media = Path.home() / ".comma" / "media" / "0" / "realdata"
    direct = media / target / "rlog.zst"
    if direct.is_file():
        return direct
    matches = sorted(media.glob(f"{target}--*/rlog.zst"))
    if matches:
        return matches[0]
    raise SystemExit(f"no rlog found for {target!r}")


def analyse_route(rlog: Path) -> dict:
    from openpilot.tools.lib.logreader import LogReader

    sources = Counter()
    stops = []
    last_source = 0
    prev_v = 0.0
    stop_start = None
    stop_source = 0
    for msg in LogReader(str(rlog), sort_by_time=True):
        which = msg.which()
        if which == "longitudinalPlan":
            src = msg.longitudinalPlan.longitudinalPlanSource
            last_source = src.raw if hasattr(src, "raw") else int(src)
            sources[last_source] += 1
        elif which == "carState":
            v = float(msg.carState.vEgo)
            if v < STOP_SPEED and prev_v >= STOP_SPEED:
                stop_start = True
                stop_source = last_source
            elif v >= STOP_SPEED and stop_start:
                stops.append(stop_source)
                stop_start = None
            prev_v = v
    if stop_start:
        stops.append(stop_source)
    return {
        "sources": dict(sources),
        "traffic_light_frames": sources.get(SOURCE_TRAFFIC_LIGHT, 0),
        "stops": len(stops),
        "traffic_light_stops": sum(1 for s in stops if s == SOURCE_TRAFFIC_LIGHT),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("route")
    ap.add_argument("--journal", help="saved journalctl output from cplink-shim")
    args = ap.parse_args()

    rlog = resolve_rlog(args.route)
    route = analyse_route(rlog)
    print(f"route              {rlog.parent.name}")
    print(f"trafficLight frames {route['traffic_light_frames']}")
    print(f"stops              {route['stops']}")
    print(f"stops on trafficLight {route['traffic_light_stops']}")
    print(f"plan sources       {route['sources']}")

    journal_rows = []
    if args.journal:
        journal_rows = parse_journal(args.journal)
    print(f"stop-line log rows {len(journal_rows)}")
    for row in journal_rows[:10]:
        print(f"  {row['timestamp']}  {row['distance_m']:.1f} m  row={row['row']} cov={row['coverage']:.2f} v={row['speed']}")

    verdict = []
    if journal_rows and route["traffic_light_frames"] == 0:
        verdict.append("source logged a stop line but the planner never chose trafficLight")
    if route["traffic_light_frames"] and not journal_rows:
        verdict.append("planner used trafficLight but no stop-line log was supplied/seen")
    if route["traffic_light_stops"]:
        verdict.append("at least one stop happened with plan source trafficLight")
    if not journal_rows and not route["traffic_light_frames"]:
        verdict.append("no stop-line evidence on this route (expected if no red/low-speed/no-lead approach)")
    print("verdict:", "; ".join(verdict) if verdict else "no red flags")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
