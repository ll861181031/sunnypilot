"""``sp long-tune`` -- score longitudinal tracking from recorded routes. Read-only.

Usage::

    python -m tools.sp_long_tune.cli score ~/.comma/media/0/realdata/00000199--40b22824ea
    python -m tools.sp_long_tune.cli score ~/.comma/media/0/realdata --limit 30
    python -m tools.sp_long_tune.cli report

There is no ``next``/``apply`` yet on purpose: only one longitudinal configuration
ships, and the loop is closed, so an offline suggestion would be a guess dressed as
advice. What this tool answers is "how did the longitudinal path behave", which is
the input a human needs before changing stopAccel, ki or the actuator delay.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from .dataset import DEFAULT_DB, TrialStore
from .metrics import score_segment


def _segments(path: Path, limit: int) -> list[Path]:
  """Expand a root, a route prefix (``0000019d--a55a5e8b17``) or one segment.

  A route prefix is not itself a directory -- the segments are ``<prefix>--0``,
  ``<prefix>--1``, ... -- so testing only ``is_dir`` silently fell through to the
  parent and scored the root instead.
  """
  if re.search(r"--\d+$", path.name):
    return [path]
  if "--" in path.name:
    return sorted(p for p in path.parent.glob(path.name + "--*") if p.is_dir())
  if path.is_dir():
    routes = sorted({p.name.rsplit("--", 1)[0] for p in path.iterdir()
                     if p.is_dir() and "--" in p.name and not p.name.startswith("boot")})
    if limit:
      routes = routes[-limit:]
    out: list[Path] = []
    for r in routes:
      out.extend(sorted(p for p in path.glob(r + "--*") if p.is_dir()))
    return out
  return [path.parent]


def _boost_report(store: "TrialStore") -> str:
  """Acceleration-boost summary across every scored segment.

  Split out from ``report`` because the question is different: ``report`` asks how well
  longitudinal tracked, this asks whether the predictive boost was actually engaged and
  whether tracking got worse while it was. A segment scored before the field existed is
  counted as "not recorded" rather than as zero boost.
  """
  rows = [r for r in store.rows() if r.get("frames_long_active")]
  if not rows:
    return "no engaged segments recorded"
  def num(row: dict, name: str):
    v = row.get(name)
    return v if isinstance(v, (int, float)) and v == v else None
  scored = [r for r in rows if num(r, "boost_share") is not None]
  if not scored:
    return ("accel boost: no segment carries the field\n"
            "rescore a corpus that has longitudinalPlan.accelBoost:\n"
            "  python -m tools.sp_long_tune.cli score <root>")

  frames = sum(r.get("frames_long_active") or 0 for r in scored)
  seconds = sum(num(r, "boost_s") or 0.0 for r in scored)
  share = sum(num(r, "boost_share") or 0.0 for r in scored) / len(scored)
  peak = max(num(r, "boost_max") or 0.0 for r in scored)
  on = [num(r, "err_p90_boosted") for r in scored if num(r, "err_p90_boosted") is not None]
  off = [num(r, "err_p90_unboosted") for r in scored if num(r, "err_p90_unboosted") is not None]

  lines = [
    f"segments scored with boost data: {len(scored)}/{len(rows)}   engaged frames: {frames}",
    f"boost engaged: {share * 100:.0f}% of frames   total {seconds:.0f} s   peak {peak:.2f} m/s^2",
    "err P90 while boosted:   "
    + (f"{sum(on) / len(on):.3f} m/s^2 ({len(on)} segments)" if on else "n/a"),
    "err P90 while unboosted: "
    + (f"{sum(off) / len(off):.3f} m/s^2 ({len(off)} segments)" if off else "n/a"),
    # capnp answers a field the writer never set with the schema default, so a drive
    # recorded before this field existed reads as 0, not as "unknown".
    "note: accelBoost 自 2026-11-18 起写入；更早的录制恒读 0，不代表功能没生效。",
    "",
    f"{'route':>28} {'seg':>4} {'share':>6} {'peak':>6} {'errOn':>7} {'errOff':>7}",
  ]
  ranked = sorted(scored, key=lambda r: -(num(r, "boost_s") or 0.0))[:10]
  for r in ranked:
    def fmt(name: str, spec: str, row: dict = r) -> str:
      v = num(row, name)
      return format(float("nan") if v is None else v, spec)
    lines.append(f"{r['route']:>28} {r['segment']:>4} {fmt('boost_share', '6.2f')} "
                 f"{fmt('boost_max', '6.2f')} {fmt('err_p90_boosted', '7.2f')} {fmt('err_p90_unboosted', '7.2f')}")
  return "\n".join(lines)


def main() -> int:
  ap = argparse.ArgumentParser(description="longitudinal scoring")
  ap.add_argument("command", choices=("score", "report", "boost"))
  ap.add_argument("path", nargs="?", default=str(Path.home() / ".comma" / "media" / "0" / "realdata"))
  ap.add_argument("--db", default=str(DEFAULT_DB))
  ap.add_argument("--limit", type=int, default=0, help="keep only the last N routes")
  ap.add_argument("--full", action="store_true", help="read rlog (100 Hz) instead of qlog")
  args = ap.parse_args()

  if args.command == "report":
    with TrialStore(args.db) as store:
      print(store.report())
    return 0

  if args.command == "boost":
    with TrialStore(args.db) as store:
      print(_boost_report(store))
    return 0

  segs = _segments(Path(args.path).expanduser(), args.limit)
  if not segs:
    print("no segments found", file=sys.stderr)
    return 2
  print(f"scoring {len(segs)} segment(s) ({'rlog' if args.full else 'qlog'})", file=sys.stderr)
  with TrialStore(args.db) as store:
    n = 0
    for p in segs:
      try:
        m = score_segment(p, full=args.full)
      except Exception as exc:
        print(f"skip {p.name}: {type(exc).__name__}: {exc}", file=sys.stderr)
        continue
      store.add(m)
      n += 1
    print(f"scored {n} segment(s) into {args.db}")
  return 0


if __name__ == "__main__":
  sys.exit(main())
