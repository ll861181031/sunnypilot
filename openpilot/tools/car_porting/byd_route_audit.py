#!/usr/bin/env python3
"""Audit a BYD Song Plus DM-i 23 ALT rlog against this port's CAN assumptions.

Read-only: it opens local rlogs and never touches CAN, USB, Params or the
panda. It re-checks, per route, the things that silently break control:

  * every message the C safety mode declares as required is present on the
    declared bus at the declared rate and length;
  * every message the host CarState parses is present the same way;
  * the 0x1E2 template the controller rebuilds is well-formed (symmetric jerk
    limits inside the observed range, reserved bits zero).

Passing is necessary but NOT sufficient: it cannot prove EPS handshakes,
actuator acceptance or takeover/release behaviour.

Usage:
  python -m openpilot.tools.car_porting.byd_route_audit ROUTE [ROUTE ...] [--json OUT]
"""

import argparse
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import opendbc


PLATFORM = "BYD_SONG_PLUS_DMI_23_ALT"
DBC_NAME = "byd_song_plus_dmi_23_alt"
VEHICLE_BUS, MPC_BUS = 0, 2

# address -> (bus, minimum Hz, source)
REQUIRED = {
  0x11F: (VEHICLE_BUS, 100, "safety+host"),
  0x1FC: (VEHICLE_BUS, 50, "safety+host"),
  0x1F0: (VEHICLE_BUS, 50, "safety+host"),
  0x220: (VEHICLE_BUS, 50, "safety+host"),
  0x342: (VEHICLE_BUS, 50, "safety+host"),
  0x32D: (MPC_BUS, 50, "safety+host"),
  0x3B0: (VEHICLE_BUS, 20, "safety+host"),
  0x294: (VEHICLE_BUS, 20, "host"),
  0x242: (VEHICLE_BUS, 50, "host"),
  0x133: (VEHICLE_BUS, 20, "host"),
  0x32E: (MPC_BUS, 50, "host"),
  0x316: (MPC_BUS, 50, "host"),
  0x1E2: (MPC_BUS, 50, "stock MPC template"),
}

# Tolerances: capture segments are short and the first/last second is noisy.
RATE_TOLERANCE = 0.8
EDGE_TRIM_S = 1.0
JERK_MIN, JERK_MAX = 300, 500


@dataclass
class Frame:
  address: int
  bus: int
  dat: bytes
  t_ns: int
  # Recorded host engagement at this instant (diagnostic only).
  enabled: bool = False
  lat_active: bool = False


@dataclass
class Stream:
  name: str
  times: list[int] = field(default_factory=list)
  dlcs: set[int] = field(default_factory=set)

  def window(self) -> list[int]:
    """Timestamps with the noisy first/last EDGE_TRIM_S of the capture removed."""
    if not self.times:
      return []
    trim = int(EDGE_TRIM_S * 1e9)
    lo, hi = self.times[0] + trim, self.times[-1] - trim
    return [t for t in self.times if lo <= t <= hi] or self.times

  def trimmed_rate_hz(self) -> float:
    w = self.window()
    span = (w[-1] - w[0]) / 1e9 if len(w) > 1 else 0.0
    return (len(w) - 1) / span if span > 0 else 0.0


def dbc_lengths() -> dict[int, int]:
  text = (Path(opendbc.__file__).parent / "dbc" / f"{DBC_NAME}.dbc").read_text()
  return {int(m.group(1)): int(m.group(3)) for m in re.finditer(r"^BO_ (\d+) (\w+): (\d+)", text, re.M)}


def is_real_rx(src: int) -> bool:
  """Panda sets 0x80 in src for frames it flagged (echo/rejected), not real RX."""
  return not (src & 0x80)


def _signed10(value: int) -> int:
  value &= 0x3FF
  return value - 1024 if value & 0x200 else value


def analyse_template(dat: bytes) -> str | None:
  """None when the stock 0x1E2 template is well-formed, else the reason."""
  if len(dat) != 8:
    return f"length {len(dat)}"
  upper = _signed10(dat[0] | ((dat[1] & 0x03) << 8))
  lower = _signed10((dat[1] >> 2) | ((dat[2] & 0x0F) << 6))
  if not (JERK_MIN <= upper <= JERK_MAX):
    return f"jerk upper {upper} outside [{JERK_MIN}, {JERK_MAX}]"
  if lower != -upper:
    return f"asymmetric jerk pair ({upper}, {lower})"
  if dat[2] & 0xC0 or dat[5] != 0 or dat[6] & 0x0F:
    return "reserved bits set"
  return None


def analyse(frames: list[Frame], fingerprint: str | None) -> dict:
  problems: list[str] = []
  if fingerprint is not None and fingerprint != PLATFORM:
    problems.append(f"carFingerprint {fingerprint!r} is not {PLATFORM}")

  dlcs = dbc_lengths()
  streams: dict[int, Stream] = {}
  buses_seen: dict[int, set[int]] = defaultdict(set)
  template_values: dict[tuple[int, int], int] = defaultdict(int)
  template_problems: dict[str, int] = defaultdict(int)

  for f in frames:
    buses_seen[f.address].add(f.bus)
    if f.address not in REQUIRED:
      continue
    bus, _, name = REQUIRED[f.address]
    if f.bus != bus:
      continue
    s = streams.setdefault(f.address, Stream(name=name))
    s.times.append(f.t_ns)
    s.dlcs.add(len(f.dat))
    if f.address == 0x1E2:
      reason = analyse_template(f.dat)
      if reason is None:
        upper = _signed10(f.dat[0] | ((f.dat[1] & 0x03) << 8))
        template_values[(upper, -upper)] += 1
      else:
        template_problems[reason] += 1

  report_streams = {}
  for addr, (bus, min_rate, name) in REQUIRED.items():
    s = streams.get(addr)
    if s is None or not s.times:
      seen = sorted(buses_seen.get(addr, ()))
      problems.append(f"0x{addr:03X} ({name}) absent on bus {bus}" + (f"; seen on {seen}" if seen else ""))
      continue
    expected_dlc = dlcs.get(addr)
    bad_dlc = sorted(d for d in s.dlcs if d != expected_dlc)
    if bad_dlc:
      problems.append(f"0x{addr:03X} DLC {bad_dlc} != DBC length {expected_dlc}")
    rate = s.trimmed_rate_hz()
    if rate < min_rate * RATE_TOLERANCE:
      problems.append(f"0x{addr:03X} bus{bus} rate {rate:.1f}Hz < {min_rate}Hz")
    report_streams[f"0x{addr:03X}"] = {
      "bus": bus, "count": len(s.times), "rate_hz": round(rate, 1),
      "dlc": sorted(s.dlcs), "dbc_length": expected_dlc, "requirement": name,
    }

  if template_problems:
    problems.append(f"malformed 0x1E2 templates: {dict(template_problems)}")

  return {
    "platform": PLATFORM,
    "ok": not problems,
    "problems": problems,
    "streams": report_streams,
    "template_pairs": {f"{u}/{l}": c for (u, l), c in sorted(template_values.items())},
    "edge_trim_s": EDGE_TRIM_S,
  }


def read_route(path: str) -> tuple[list[Frame], str | None]:
  from openpilot.tools.lib.logreader import LogReader

  frames: list[Frame] = []
  fingerprint: str | None = None
  enabled = lat_active = False
  for msg in LogReader(path):
    which = msg.which()
    if which == "carParams":
      fingerprint = msg.carParams.carFingerprint
    elif which == "carControl":
      enabled = bool(msg.carControl.enabled)
      lat_active = bool(msg.carControl.latActive)
    elif which == "can":
      for c in msg.can:
        if not is_real_rx(c.src):
          continue
        frames.append(Frame(c.address, c.src, bytes(c.dat), msg.logMonoTime, enabled, lat_active))
  return frames, fingerprint


def template_builder_scan(frames: list[Frame]) -> dict:
  """Rebuild 0x1E2 from every stock frame using the real controller builder.

  This is the check that caught the old hard-coded "F4 31" template test: it asks
  whether the controller can build a frame the safety will relay for each stock
  template the car actually sends.
  """
  from opendbc.can import CANPacker, CANParser
  from opendbc.car.byd.bydcanfd import create_lkas_angle

  packer = CANPacker(DBC_NAME)
  parser = CANParser(DBC_NAME, [("MPC_LKAS_CMD_ANGLE", 50)], MPC_BUS)
  accepted = rejected = 0
  rejected_examples: list[str] = []
  for f in frames:
    if f.address != 0x1E2 or f.bus != MPC_BUS:
      continue
    parser.update([(f.t_ns, [(f.address, f.dat, f.bus)])])
    template = dict(parser.vl["MPC_LKAS_CMD_ANGLE"])
    for active in (False, True):
      address, _dat, _bus = create_lkas_angle(packer, template, 0.0, active, 0)
      if address == 0:
        rejected += 1
        # each frame is built twice (inactive + active); keep examples unique
        if f.dat.hex() not in rejected_examples and len(rejected_examples) < 3:
          rejected_examples.append(f.dat.hex())
      else:
        accepted += 1
  return {"accepted": accepted, "rejected": rejected, "rejected_examples": rejected_examples}


def forwarding_scan(frames: list[Frame]) -> dict:
  """Diagnostic: how much stock 0x1E2 the current forward hook would suppress.

  Counts stock angle frames blocked by the *current* C forward hook given the
  recorded engagement, and how many of those happened while lateral was recorded
  inactive (i.e. openpilot was not steering but the stock command was withheld).
  This does not change behaviour; it sizes the open question in section 2 of
  the integration notes.
  """
  from opendbc.car import structs
  from opendbc.safety.tests.libsafety import libsafety_py

  lib = libsafety_py.libsafety
  if lib.set_safety_hooks(structs.CarParams.SafetyModel.byd, 0) != 0:
    return {"available": False, "reason": "SAFETY_BYD could not be selected"}
  lib.init_tests()

  stock = blocked = blocked_inactive = 0
  for f in frames:
    if f.address != 0x1E2 or f.bus != MPC_BUS:
      continue
    stock += 1
    lib.set_controls_allowed(f.enabled)
    if lib.safety_fwd_hook(MPC_BUS, f.address) == -1:
      blocked += 1
      if not f.lat_active:
        blocked_inactive += 1
  return {
    "available": True,
    "stock_lkas_frames": stock,
    "forward_blocked": blocked,
    "blocked_with_lateral_inactive": blocked_inactive,
  }


def safety_scan(frames: list[Frame]) -> dict:
  """Replay the route through the real C safety mode (no CAN, no panda).

  This is the authoritative check for "would this traffic satisfy our safety
  mode": it exercises the actual counters, checksums and frequency checks.
  """
  from opendbc.car import structs
  from opendbc.safety.tests.libsafety import libsafety_py

  lib = libsafety_py.libsafety
  if lib.set_safety_hooks(structs.CarParams.SafetyModel.byd, 0) != 0:
    return {"available": False, "reason": "SAFETY_BYD could not be selected"}
  lib.init_tests()

  fed: dict[int, int] = defaultdict(int)
  rejected: dict[int, int] = defaultdict(int)
  t0 = frames[0].t_ns if frames else 0
  for f in frames:
    if f.address not in REQUIRED:
      continue
    lib.set_timer((f.t_ns - t0) // 1000)
    fed[f.address] += 1
    if not lib.safety_rx_hook(libsafety_py.make_CANPacket(f.address, f.bus, f.dat)):
      rejected[f.address] += 1
  lib.safety_tick_current_safety_config()

  return {
    "available": True,
    "config_valid": bool(lib.safety_config_valid()),
    "fed": {f"0x{a:03X}": c for a, c in sorted(fed.items())},
    "rejected": {f"0x{a:03X}": c for a, c in sorted(rejected.items())},
  }


def main(argv=None) -> int:
  parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
  parser.add_argument("routes", nargs="+", help="local rlog.zst files or segment directories")
  parser.add_argument("--json", help="write the report to this path")
  args = parser.parse_args(argv)

  exit_code = 0
  reports = {}
  for route in args.routes:
    try:
      frames, fingerprint = read_route(route)
    except Exception as e:  # LogReader asserts on bad paths / corrupt segments
      reports[route] = {"ok": False, "problems": [f"could not read route: {type(e).__name__}: {e}"]}
      print(f"FAIL {route}  could not read: {type(e).__name__}: {e}")
      exit_code = 1
      continue
    if not frames:
      reports[route] = {"ok": False, "problems": ["no CAN frames read"]}
      exit_code = 1
      continue
    report = analyse(frames, fingerprint)
    report["template_builder"] = template_builder_scan(frames)
    builder = report["template_builder"]
    if builder["rejected"]:
      report["problems"].append(" ".join([
        f"controller could not build 0x1E2 from {builder['rejected']} stock templates",
        f"(e.g. {builder['rejected_examples']})",
      ]))
    report["forwarding"] = forwarding_scan(frames)  # diagnostic only, does not fail the route
    scan = safety_scan(frames)
    report["safety"] = scan
    if scan.get("available"):
      if not scan["config_valid"]:
        report["problems"].append("C safety reports the safety config invalid for this route")
      for addr, count in scan["rejected"].items():
        report["problems"].append(f"C safety rejected {count} frames of {addr}")
    else:
      report["problems"].append(f"C safety check unavailable: {scan.get('reason')}")
    report["ok"] = not report["problems"]
    reports[route] = report
    status = "OK  " if report["ok"] else "FAIL"
    print(f"{status} {route}  frames={len(frames)}")
    for problem in report["problems"]:
      print(f"      ! {problem}")
    if not report["ok"]:
      exit_code = 1

  if args.json:
    Path(args.json).write_text(json.dumps(reports, indent=2, sort_keys=True))
    print(f"report written to {args.json}")
  return exit_code


if __name__ == "__main__":
  sys.exit(main())
