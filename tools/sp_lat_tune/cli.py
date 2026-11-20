"""``sp lat-tune`` -- score drives, pick the next parameter pair, record trials.

Usage::

    sp lat-tune score ~/.comma/media/0/realdata/00000051--201b6e1171--0
    sp lat-tune report
    sp lat-tune next -n 3
    sp lat-tune apply 55 24

``apply`` writes the two settings-page params. With the hot-swap in card.py those
take effect on the next lateral re-engagement rather than at the next stack start,
so a single drive can cover several pairs.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import optimize
from .dataset import DEFAULT_DB, TrialStore
from .bands import scan as scan_bands
from .metrics import score_segment


def _cmd_score(args) -> int:
  with TrialStore(args.db) as store:
    count = 0
    for route in args.routes:
      path = Path(route).expanduser()
      if path.is_dir() and "--" in path.name:
        m = score_segment(path)
        store.add(m)
        count += 1
        print(f"{m.route}--{m.segment}: {m.frames_active} active frames, under {m.undershoot_aggregate:.2f}, toofast {m.toofast_frames}")
      else:
        for seg in sorted(p for p in path.parent.glob(path.name + "--*") if p.is_dir()):
          m = score_segment(seg)
          store.add(m)
          count += 1
          if args.verbose:
            print(f"{m.route}--{m.segment}: {m.frames_active} active, under {m.undershoot_aggregate:.2f}, toofast {m.toofast_frames}")
    print(f"scored {count} segment(s) into {args.db}")
  return 0


def _cmd_bands(args) -> int:
  from .bands import _print_bands
  _print_bands(scan_bands(args.root, limit=args.limit))
  return 0


def _cmd_report(args) -> int:
  with TrialStore(args.db) as store:
    print(store.report())
    best = optimize.best(store.trials())
    if best:
      print(f"\nbest feasible: lead={best['lead_tenths']/10:.1f} deg, rate={best['rate_tenths']/10:.1f}, objective={optimize.objective(best):.3f}")
    else:
      print("\nno feasible trial yet")
  return 0


def _cmd_next(args) -> int:
  with TrialStore(args.db) as store:
    picks = optimize.suggest(store, n=args.n)
    if not picks:
      print("search space exhausted")
      return 0
    print(json.dumps(picks, indent=2))
    print("\napply with: sp lat-tune apply <lead> <rate>", file=sys.stderr)
  return 0


def _cmd_apply(args) -> int:
  lead = int(round(args.lead * 10))
  rate = int(round(args.rate * 10))
  if not (optimize.LEAD_RANGE[0] <= lead <= optimize.LEAD_HARD_MAX):
    print(f"lead {args.lead} deg outside {optimize.LEAD_RANGE[0]/10}-{optimize.LEAD_HARD_MAX/10}", file=sys.stderr)
    return 2
  if not (optimize.RATE_RANGE[0] <= rate <= optimize.RATE_HARD_MAX):
    print(f"rate {args.rate} outside {optimize.RATE_RANGE[0]/10}-{optimize.RATE_HARD_MAX/10}", file=sys.stderr)
    return 2
  try:
    from openpilot.common.params import Params
  except Exception as exc:  # pragma: no cover - depends on a built openpilot
    print(f"cannot reach Params: {exc}", file=sys.stderr)
    return 3
  params = Params()
  params.put("BydSteerLeadMaxDeg", lead)
  params.put("BydSteerRateMax", rate)
  print(f"applied lead={lead/10:.1f} deg, rate={rate/10:.1f}; takes effect on the next lateral re-engagement")
  return 0


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(prog="sp lat-tune",
                                   description="Lateral parameter sweep over recorded drives")
  parser.add_argument("--db", default=str(DEFAULT_DB))
  sub = parser.add_subparsers(dest="cmd", required=True)

  p = sub.add_parser("score", help="score one or more routes into the trial store")
  p.add_argument("routes", nargs="+")
  p.add_argument("-v", "--verbose", action="store_true")
  p.set_defaults(func=_cmd_score)

  p = sub.add_parser("bands", help="per-speed-band analysis of a route or a corpus sample")
  p.add_argument("root", help="route/segment directory, or a realdata root")
  p.add_argument("--limit", type=int, default=None)
  p.set_defaults(func=_cmd_bands)

  p = sub.add_parser("report", help="show every trial recorded so far")
  p.set_defaults(func=_cmd_report)

  p = sub.add_parser("next", help="suggest the next parameter pair(s) to drive")
  p.add_argument("-n", type=int, default=1)
  p.set_defaults(func=_cmd_next)

  p = sub.add_parser("apply", help="write a parameter pair to Params")
  p.add_argument("lead", type=float, help="degrees, 0.5-3.4 (only tightens)")
  p.add_argument("rate", type=float, help="0.5-1.0")
  p.set_defaults(func=_cmd_apply)

  args = parser.parse_args(argv)
  return args.func(args)


if __name__ == "__main__":
  raise SystemExit(main())
