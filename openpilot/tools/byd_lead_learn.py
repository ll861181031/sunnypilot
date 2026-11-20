"""Replay the lead-bound learner over recorded routes. Read-only.

Feeds `carStateSP.bydLateral` (what actually went on the bus, what the wheel did,
and whether the command was pinned to the bound) plus the EPS refusal flag into
`opendbc.car.byd.lead_learn.LeadBoundLearner`, so the number it converges to can be
checked against the measured fault buckets before anything is wired to the car.

No stack, no sockets, no Params, no hardware. Only reads files under the log root.

  python -m openpilot.tools.byd_lead_learn --route 000000e9--4979179d0e
"""

import argparse
import glob
import os
import sys
from bisect import bisect_left

import numpy as np

from opendbc.car.byd.lead_learn import LeadBoundLearner
from opendbc.car.byd.values import CarControllerParams
from openpilot.tools.lib.logreader import LogReader

REALDATA = os.path.expanduser("~/.comma/media/0/realdata")
FAULT_WINDOW_S = 0.060
LEAD = 1          # lateral_diagnostics.Limit.LEAD
INACTIVE = 8      # lateral_diagnostics.Limit.INACTIVE


def segments(route, root=REALDATA):
  found = glob.glob(os.path.join(root, f"{route}--*"))
  return sorted(found, key=lambda p: int(p.rsplit("--", 1)[1]))


def extract(path):
  """Per-segment samples: (t, vEgo, desired, measured, bound, limitMask) + fault times."""
  samples, faults = [], []
  cs = None
  for msg in LogReader(path, sort_by_time=True):
    which = msg.which()
    if which == "carState":
      cs = msg.carState
    elif which == "can":
      # The raw EPS pulse, NOT CarState.steerFaultTemporary: that one is debounced
      # (EPS_TEMP_FAULT_SAMPLES) precisely so a 1-4 frame pulse does not latch a
      # takeover prompt, so it hides exactly the events the bound is responsible for.
      for can in msg.can:
        if can.address == 0x1FC and (bytes(can.dat)[5] >> 6) & 0x3 == 2:
          faults.append(msg.logMonoTime / 1e9)
    elif which == "carStateSP" and cs is not None:
      diag = msg.carStateSP.bydLateral
      if not diag.valid:
        continue
      samples.append((msg.logMonoTime / 1e9, cs.vEgo, diag.desiredAngleDeg,
                      diag.measuredAngleDeg, diag.leadBoundDeg, int(diag.limitMask),
                      diag.leadLimitedAngleDeg))
  return samples, faults


def replay(routes, root=REALDATA, verbose=True, allow_up=False):
  bp, values = CarControllerParams.STEER_ANGLE_ERROR_MAX
  # Off-policy: the recording carries the lead the car actually sent, so a raise
  # would never show the refusals it would have caused. Down-side only by default.
  learner = LeadBoundLearner(bp=bp, values=values, allow_up=allow_up)
  initial = [b.theta for b in learner.buckets]
  totals = {"samples": 0, "pinned": 0, "starved": 0, "faults": 0}

  for route in routes:
    for directory in segments(route, root):
      path = os.path.join(directory, "rlog.zst")
      if not os.path.isfile(path):
        continue
      samples, faults = extract(path)
      if not samples:
        continue
      fault_t = np.asarray(faults) if faults else np.zeros(0)
      for t, v, des, meas, bound, mask, _limited in samples:
        totals["samples"] += 1
        if mask & INACTIVE:
          continue
        pinned = bool(mask & LEAD)
        if not pinned:
          continue
        totals["pinned"] += 1
        starved = abs(des - meas) > bound + learner.DEMAND_MARGIN
        totals["starved"] += int(starved)
        # A refusal counts only if it lands inside the window right after this
        # pinned sample - the same 60 ms attribution the corpus buckets use.
        lo = bisect_left(fault_t, t)
        refused = lo < len(fault_t) and fault_t[lo] <= t + FAULT_WINDOW_S
        totals["faults"] += int(refused)
        learner.observe(v, pinned, starved, refused)

  if verbose:
    report(learner, initial, totals)
  return learner, totals


def report(learner, initial, totals):
  centres = [f"{lo:.0f}-{hi:.0f}km/h" for lo, hi in zip(learner.EDGES_KMH[:-1], learner.EDGES_KMH[1:], strict=True)]
  print(f"{'band':>14} {'start':>6} {'learned':>7} {'moved':>6} {'pinned':>7} {'faults':>6} {'up':>4} {'down':>4}")
  for name, was, bucket in zip(centres, initial, learner.buckets, strict=True):
    print(f"{name:>14} {was:6.2f} {bucket.theta:7.2f} {bucket.theta - was:+6.2f} "
          + f"{bucket.observations:7d} {bucket.fault_observations:6d} "
          + f"{bucket.increases:4d} {bucket.decreases:4d}")
  speeds, bounds = learner.table()
  print()
  print("table (m/s -> deg): " + " ".join(f"{s:.2f}:{b:.2f}" for s, b in zip(speeds, bounds, strict=True)))
  print("param text        : " + learner.to_param())
  print()
  print(f"frames {totals['samples']}  pinned {totals['pinned']} "
        + f"({100.0 * totals['pinned'] / max(totals['samples'], 1):.1f}%)  "
        + f"starved {totals['starved']}  refusals {totals['faults']}  "
        + f"changes {len(learner.changes)}")


PRIOR_BIN_DEG = 0.5
PRIOR_TARGET = 0.01


def prior(routes, root=REALDATA, target=PRIOR_TARGET):
  """Cold-start table: the largest lead whose measured refusal rate stays <= target.

  This is the honest offline half. A one-pass replay is OFF-POLICY - the recorded
  frames carry the lead the car actually sent, so raising the estimate changes
  nothing in the recording and can never show the extra refusals a raise would
  cause. What a recording CAN answer is the converse question: at each speed band,
  how often did a lead of x degrees get refused? Take the largest x that stays
  under target and that is the prior; the live learner is then only allowed to
  fine-tune around it.
  """
  samples = 0
  per_band = {}
  for route in routes:
    for directory in segments(route, root):
      path = os.path.join(directory, "rlog.zst")
      if not os.path.isfile(path):
        continue
      rows, faults = extract(path)
      if not rows:
        continue
      fault_t = np.asarray(faults) if faults else np.zeros(0)
      for t, v, _des, meas, _bound, mask, limited in rows:
        if mask & INACTIVE:
          continue
        lead = abs(limited - meas)
        lo = bisect_left(fault_t, t)
        refused = bool(lo < len(fault_t) and fault_t[lo] <= t + FAULT_WINDOW_S)
        band = int(max(0.0, v) * 3.6 // 10) * 10
        per_band.setdefault(band, {}).setdefault(round(lead / PRIOR_BIN_DEG) * PRIOR_BIN_DEG, [0, 0])
        per_band[band][round(lead / PRIOR_BIN_DEG) * PRIOR_BIN_DEG][0] += 1
        per_band[band][round(lead / PRIOR_BIN_DEG) * PRIOR_BIN_DEG][1] += int(refused)
        samples += 1

  print(f"active frames {samples}")
  print(f"{'band':>10} {'lead':>5} {'frames':>7} {'refused':>8} {'rate':>7}")
  table = {}
  for band in sorted(per_band):
    best = None
    for lead in sorted(per_band[band]):
      n, k = per_band[band][lead]
      if n < 20:
        continue
      rate = k / n
      print(f"{band:>7}-{band + 10:<3} {lead:5.1f} {n:7d} {k:8d} {100 * rate:6.2f}%")
      if rate <= target:
        best = lead
    table[band] = best
  print()
  print("prior per band (largest lead under target, deg):")
  for band in sorted(table):
    print(f"  {band:>3}-{band + 10:<3} km/h -> {table[band]}")

  # A curve the car can actually take: unsampled bands inherit the nearest measured
  # one, then the table is projected monotone and clamped to the range the Param
  # parser accepts. This is the cold-start value for BydLeadBoundTable.
  bands = sorted(table)
  curve, last = [], None
  for band in bands:
    if table[band] is not None:
      last = table[band]
    curve.append(last)
  first_known = next((v for v in curve if v is not None), 5.0)
  curve = [first_known if v is None else v for v in curve]
  for i in range(1, len(curve)):
    curve[i] = min(curve[i], curve[i - 1])
  low, high = CarControllerParams.STEER_ANGLE_ERROR_MAX[1][-1], 6.0
  curve = [min(high, max(low, v)) for v in curve]
  points = [(0.0, curve[0])] + [((band + 5) / 3.6, v) for band, v in zip(bands, curve, strict=True)]
  text = ",".join(f"{v * 3.6:.1f}:{b:.2f}" for v, b in points)
  print()
  print("cold-start param  : " + text)
  return table


def main(argv=None):
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--route", action="append", default=[], help="route id, repeatable (default: all)")
  parser.add_argument("--root", default=REALDATA)
  parser.add_argument("--limit", type=int, default=0, help="cap the number of routes")
  parser.add_argument("--prior", action="store_true", help="compute the cold-start prior instead of replaying")
  parser.add_argument("--target", type=float, default=PRIOR_TARGET, help="fault rate the prior aims at")
  parser.add_argument("--allow-up", action="store_true",
                      help="let the replay raise the bound (off-policy: the raise is not validated by the recording)")
  args = parser.parse_args(argv)

  if args.route:
    routes = args.route
  else:
    names = {os.path.basename(p).rsplit("--", 1)[0] for p in glob.glob(os.path.join(args.root, "000000??--*"))}
    routes = sorted(names)
  if args.limit:
    routes = routes[-args.limit:]
  print(f"routes: {len(routes)}  ({routes[0]} .. {routes[-1]})" if routes else "routes: none")
  if args.prior:
    prior(routes, args.root, args.target)
  else:
    replay(routes, args.root, allow_up=args.allow_up)
  return 0


if __name__ == "__main__":
  sys.exit(main())
