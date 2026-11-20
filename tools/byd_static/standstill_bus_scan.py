#!/usr/bin/env python3
"""Which CAN messages does this car emit at standstill that it does not emit while driving?

The mirror port refuses to transmit the standstill hold code (raw 0x00), so the
open question is whether the car can be held by some other request. That question
is only answerable by looking at what the stock ACC emits while the car is stopped
that it does not emit while moving: a message present only at standstill is part of
the holding handshake.

Reads rlog.zst, which records every bus frame. CAN messages in the log carry only
address/dat/src, so the DBC has to be applied here.

Speed comes from IPB_SPEED (0x121) because it was validated against carState.vEgo
over 2654 samples; WHEEL_SPEED (0x1F0) is used as a cross-check.
"""

import argparse
import glob
import os
import sys
from collections import Counter, defaultdict

from opendbc.can import CANParser
from opendbc.car import Bus
from opendbc.car.byd.values import CAR, DBC
from openpilot.tools.lib.logreader import LogReader

ACC_BUS = 2
PT_BUS = 0
STOP_KMH = 0.6
MOVE_KMH = 10.0


def parsers():
  pt = CANParser(DBC[CAR.BYD_SONG_PLUS_DMI_23_ALT][Bus.pt],
                 [("IPB_SPEED", 50), ("WHEEL_SPEED", 50)], PT_BUS)
  cam = CANParser(DBC[CAR.BYD_SONG_PLUS_DMI_23_ALT][Bus.pt],
                  [("ACC_CMD", 50)], ACC_BUS)
  return pt, cam


def main():
  ap = argparse.ArgumentParser()
  ap.add_argument("paths", nargs="+")
  ap.add_argument("--min-stop-frames", type=int, default=200)
  args = ap.parse_args()

  files = []
  for p in args.paths:
    files += glob.glob(os.path.join(p, "**", "rlog.zst"), recursive=True)
  files = sorted(set(files))
  print("rlog files: %d" % len(files), file=sys.stderr)

  stop_addr = Counter()
  move_addr = Counter()
  stop_vals = defaultdict(Counter)
  routes_with_stop = 0
  mismatch = 0

  for i, path in enumerate(files, 1):
    if i % 50 == 0:
      print("  scanned %d/%d" % (i, len(files)), file=sys.stderr)
    try:
      lr = LogReader(path)
      pt, cam = parsers()
    except Exception:
      continue

    stop_frames = 0
    t = 0
    try:
      for msg in lr:
        if msg.which() != "can":
          continue
        frames = [(c.address, bytes(c.dat), c.src) for c in msg.can]
        if not frames:
          continue
        t += 20000000
        pt.update([(t, frames)])
        cam.update([(t, frames)])

        speed = pt.vl["IPB_SPEED"].get("VEHICLE_SPEED")
        acc = dict(cam.vl["ACC_CMD"])
        if speed is None or not acc:
          continue

        wheel = pt.vl["WHEEL_SPEED"].get("WHEELSPEED_FL")
        if wheel is not None and abs(wheel - speed) > 1.0:
          mismatch += 1

        if not acc.get("ACC_CONTROLLABLE_AND_ON"):
          continue

        if speed < STOP_KMH:
          for addr, dat, _src in frames:
            stop_addr[addr] += 1
            stop_vals[addr][dat.hex()] += 1
          stop_frames += 1
        elif speed > MOVE_KMH:
          for addr, _dat, _src in frames:
            move_addr[addr] += 1
    except Exception:
      continue

    if stop_frames >= args.min_stop_frames:
      routes_with_stop += 1

  print("")
  print("routes with a usable standstill segment: %d" % routes_with_stop)
  print("standstill frames: %d   driving frames: %d" % (sum(stop_addr.values()), sum(move_addr.values())))
  if mismatch:
    print("IPB/WHEEL speed disagreements: %d" % mismatch)

  only_stop = sorted(a for a in stop_addr if a not in move_addr)
  print("")
  print("addresses emitted ONLY at standstill (%d):" % len(only_stop))
  for a in only_stop:
    print("  0x%03x   %8d frames, %4d distinct payloads" % (a, stop_addr[a], len(stop_vals[a])))

  print("")
  print("addresses relatively rarer at standstill (stop >= 50 frames, by ratio):")
  rows = []
  for a, c in stop_addr.items():
    m = move_addr.get(a, 0)
    if c >= 50:
      rows.append((c / m if m else float("inf"), a, c, m))
  rows.sort(key=lambda r: -(r[0] if r[0] != float("inf") else 1e9))
  print("  %6s %9s %9s %8s %9s" % ("addr", "stop", "move", "ratio", "distinct"))
  for ratio, a, c, m in rows[:25]:
    rs = "inf" if ratio == float("inf") else "%.2f" % ratio
    print("  0x%03x %9d %9d %8s %9d" % (a, c, m, rs, len(stop_vals[a])))

  const = sorted(a for a in stop_addr if stop_addr[a] >= 50 and len(stop_vals[a]) == 1)
  print("")
  print("constant while stopped (%d addresses):" % len(const))
  print("  " + ", ".join("0x%03x" % a for a in const[:40]))


if __name__ == "__main__":
  main()
