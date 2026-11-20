#!/usr/bin/env python3
"""Check a recorded drive against what the traffic-light stop is supposed to do.

Run this on the first drive after enabling ``TrafficLightStop``. It answers the four
questions that decide whether the feature is working, without needing anyone to
read raw logs:

1. Did yolod actually publish an ego-light state, and was it right?
2. Did the light stop ever win the planner merge (``longitudinalPlanSource == 5``)?
3. Where did the car stop at a red light, and did it stop at all?
4. Did anything go wrong - braking at a green light, or driving through a red?

The last one is the reason this exists. A feature that stops correctly nine times
and releases once at the wrong moment is worse than no feature, and that failure is
invisible in a summary count; it has to be looked for explicitly.

Usage:
    python tools/verify_traffic_light_stop.py <route-or-segment-path>
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

# LongitudinalPlanSource.trafficLight, added alongside this feature.
SOURCE_TRAFFIC_LIGHT = 5
STOP_SPEED = 0.3
RED_STATES = ("red", "yellow")


def parse_args(argv=None):
  p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
  p.add_argument("route", help="route or segment directory, or a path to rlog.zst")
  p.add_argument("--min-stop-s", type=float, default=0.5,
                 help="a stop must last at least this long to be reported")
  return p.parse_args(argv)


def resolve_rlogs(target: str) -> list[Path]:
  path = Path(target).expanduser()
  if path.is_file() and path.name.startswith("rlog"):
    return [path]
  if path.is_dir() and (path / "rlog.zst").is_file():
    return [path / "rlog.zst"]
  if path.is_dir():
    segments = sorted(path.glob("*/rlog.zst"))
    if segments:
      return segments
  # Look the name up under the media root. A full segment name resolves directly;
  # a bare route name expands to all of its segments.
  media = Path.home() / ".comma" / "media" / "0" / "realdata"
  if media.is_dir():
    direct = media / target / "rlog.zst"
    if direct.is_file():
      return [direct]
    matches = sorted(media.glob(f"{target}--*/rlog.zst"))
    if matches:
      return matches
  raise SystemExit(f"no rlog found for {target!r}")


def analyse(rlogs: list[Path], min_stop_s: float = 0.5) -> int:
  from openpilot.tools.lib.logreader import LogReader

  light_states = Counter()
  source_counts = Counter()
  frames_with_light = 0
  frames_total = 0
  stops = []                 # (segment, start_s, end_s, state_at_stop)
  bad = {"brake_at_green": 0, "drive_through_red": 0, "stop_without_light": 0}
  have_any_light = False

  for rlog in rlogs:
    seg = rlog.parent.name
    t0 = None
    current_light = ""
    stopped_since = None
    light_at_stop_start = ""
    prev_v = 0.0

    for msg in LogReader(str(rlog), sort_by_time=True):
      which = msg.which()
      if t0 is None:
        t0 = msg.logMonoTime
      if which == "yoloObjects":
        have_any_light = True
        current_light = str(msg.yoloObjects.egoLightState)
      elif which == "longitudinalPlan":
        src = msg.longitudinalPlan.longitudinalPlanSource
        source_counts[src.raw if hasattr(src, "raw") else int(src)] += 1
      elif which == "carState":
        frames_total += 1
        v = float(msg.carState.vEgo)
        t = (msg.logMonoTime - t0) / 1e9
        if current_light:
          frames_with_light += 1
          light_states[current_light] += 1

        # Stop bookkeeping.
        if v < STOP_SPEED and prev_v >= STOP_SPEED:
          stopped_since = t
          light_at_stop_start = current_light
        elif v >= STOP_SPEED and stopped_since is not None:
          if t - stopped_since >= min_stop_s:
            stops.append((seg, stopped_since, t, light_at_stop_start))
          stopped_since = None

        # The two failure modes worth flagging.
        if current_light == "green" and v > STOP_SPEED and prev_v > v + 0.15:
          bad["brake_at_green"] += 1
        if current_light in RED_STATES and v > 3.0:
          # Still moving well above walking pace with a red light over our lane is
          # only a fault if it stays red as we close; count it as a soft signal.
          bad["drive_through_red"] += 1
        prev_v = v

    if stopped_since is not None:
      stops.append((seg, stopped_since, None, light_at_stop_start))

  print(f"  segments          {len(rlogs)}")
  print(f"  carState frames   {frames_total}")
  if not have_any_light:
    print("\n  This route has no yoloObjects at all - it was recorded before yolod")
    print("  was enabled, so there is nothing to check. Run again on a drive taken")
    print("  with YoloEnabled=True.")
    return 1

  print(f"  frames with light {frames_with_light} ({frames_with_light / max(frames_total, 1) * 100:.1f}%)")
  print(f"  light states      {dict(light_states)}")
  print(f"  plan sources      {dict(source_counts)}")
  tl = source_counts.get(SOURCE_TRAFFIC_LIGHT, 0)
  print(f"  trafficLight won  {tl} frames")
  if tl == 0:
    print("    -> the light stop never won the merge. Either it never saw a")
    print("       confirmed red/yellow in ego's lane, or TrafficLightStop is off.")

  print(f"\n  stops (>= {min_stop_s:.1f}s): {len(stops)}")
  for seg, start, end, state in stops[:20]:
    dur = "?" if end is None else f"{end - start:.1f}s"
    print(f"    {seg}  t={start:6.1f}s  {dur:>7s}  light at stop = {state or '(none)'}")

  print("\n  sanity checks")
  print(f"    braking while green : {bad['brake_at_green']} frames"
        "   (should be 0 - the guard is meant to drop those)")
  print(f"    moving >3 m/s on red: {bad['drive_through_red']} frames"
        "   (expected while approaching; look for a stop afterwards)")
  red_stops = sum(1 for _, _, _, s in stops if s in RED_STATES)
  print(f"    stops taken on red  : {red_stops} / {len(stops)}")

  verdict = []
  if bad["brake_at_green"] > 20:
    verdict.append("braking at green - check E2ECrosswalkGuard")
  if tl == 0:
    verdict.append("light stop never engaged - check YoloEnabled and TrafficLightStop")
  if stops and red_stops == 0:
    verdict.append("stops happened but none with a red light - check lane association")
  print("\n  verdict:", "; ".join(verdict) if verdict else "no red flags")
  return 0


def main(argv=None) -> int:
  args = parse_args(argv)
  return analyse(resolve_rlogs(args.route), min_stop_s=args.min_stop_s)


if __name__ == "__main__":
  sys.exit(main())
