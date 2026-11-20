#!/usr/bin/env python3
"""Report how much navigation the model actually received during a drive.

Reads `modelDataV2SP.navMapValid/navInstructionValid/...Age` (written by modeld) from one
segment or a whole route, so a drive can be audited from the log alone. Drives recorded
before those fields existed read as 0% — which is the honest answer, not a bug.

  python -m openpilot.tools.nav_status --route ~/.comma/media/0/realdata/<route>

A drive only counts as "navigated" when the map is valid for most frames and the
instruction age stays under a second: the model was trained with zeros for missing inputs,
so a low ratio means it drove on the fallback, not on the route.
"""
import argparse
import glob
import json
import os
import sys

import numpy as np

from openpilot.tools.lib.logreader import LogReader


def summarize(rows: list[tuple[bool, bool, float, float]]) -> dict:
  """rows: (map_valid, instruction_valid, map_age, instruction_age) per model frame."""
  if not rows:
    return {"frames": 0}
  map_valid = [bool(r[0]) for r in rows]
  instr_valid = [bool(r[1]) for r in rows]
  map_age = [float(r[2]) for r in rows]
  instr_age = [float(r[3]) for r in rows]
  return {
    "frames": len(rows),
    "map_valid_ratio": sum(map_valid) / len(rows),
    "instruction_valid_ratio": sum(instr_valid) / len(rows),
    "map_age": {"p50": float(np.percentile(map_age, 50)), "p95": float(np.percentile(map_age, 95)),
                "max": max(map_age)},
    "instruction_age": {"p50": float(np.percentile(instr_age, 50)), "p95": float(np.percentile(instr_age, 95)),
                        "max": max(instr_age)},
  }


def read_segment(path: str) -> list[tuple[bool, bool, float, float]]:
  rows = []
  for msg in LogReader(path):
    if msg.which() != "modelDataV2SP":
      continue
    md = msg.modelDataV2SP
    rows.append((md.navMapValid, md.navInstructionValid, md.navMapAge, md.navInstructionAge))
  return rows


def segments(route: str) -> list[str]:
  if os.path.isfile(route):
    return [route]
  found = sorted(glob.glob(os.path.join(route, "*/rlog.zst")))
  if not found and os.path.isfile(os.path.join(route, "rlog.zst")):
    found = [os.path.join(route, "rlog.zst")]
  return found


def main(argv=None) -> int:
  parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
  parser.add_argument("--route", required=True, help="route directory (segment subdirs) or one rlog path")
  parser.add_argument("--out", default=None, help="optional JSON output path")
  args = parser.parse_args(argv)

  rows = []
  for segment in segments(os.path.expanduser(args.route)):
    rows.extend(read_segment(segment))
  summary = summarize(rows)
  summary["route"] = args.route
  print(json.dumps(summary, indent=2))
  if args.out:
    with open(args.out, "w") as f:
      json.dump(summary, f, indent=2)
  return 0 if rows else 1


if __name__ == "__main__":
  sys.exit(main())
