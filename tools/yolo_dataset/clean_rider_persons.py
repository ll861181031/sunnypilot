#!/usr/bin/env python3
"""Remove auto ``person`` boxes that are riders on a two-wheeler/tricycle.

The user deleted several person boxes that overlap two/three-wheelers. This
applies the same relation to unedited images only (manual frames untouched):
drop a person whose centre is inside a two-wheeler/tricycle box, or IoU>0.1.
"""
from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

PERSON = 0
RIDE = {1, 2}


def read(path: Path) -> list[list[str]]:
  return [line.split() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def xyxy(row: list[str]) -> tuple[float, ...]:
  cx, cy, w, h = map(float, row[1:5])
  return cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2


def iou(a: tuple[float, ...], b: tuple[float, ...]) -> float:
  ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
  ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
  inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
  ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
  return inter / ua if ua > 0 else 0.0


def inside(a: tuple[float, ...], b: tuple[float, ...]) -> bool:
  cx, cy = (a[0] + a[2]) / 2, (a[1] + a[3]) / 2
  return b[0] <= cx <= b[2] and b[1] <= cy <= b[3]


def main(argv=None) -> int:
  ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
  ap.add_argument("dataset")
  ap.add_argument("--dry-run", action="store_true")
  ap.add_argument("--report", default=None)
  args = ap.parse_args(argv)
  root = Path(args.dataset)
  payload = json.loads((root / "dataset.json").read_text(encoding="utf-8"))
  review = json.loads((root / "review_state.json").read_text(encoding="utf-8"))
  stamp = time.strftime("%Y%m%d-%H%M%S")
  backup = root / f"labels_backup_riders_{stamp}"
  changed, removed = [], 0
  for image in payload["images"]:
    image_id = image["id"]
    state = review.get(image_id, {})
    if state.get("reviewed") or state.get("edited"):
      continue
    label_path = root / "labels" / f"{image_id}.txt"
    auto_path = root / "auto_labels" / f"{image_id}.txt"
    if not label_path.is_file() or not auto_path.is_file():
      continue
    rows = read(label_path)
    rides = [xyxy(row) for row in rows if int(row[0]) in RIDE]
    if not rides:
      continue
    drop = []
    for row in rows:
      if int(row[0]) != PERSON:
        continue
      box = xyxy(row)
      if any(inside(box, r) or iou(box, r) > 0.1 for r in rides):
        drop.append(tuple(row))
    if not drop:
      continue
    changed.append(image_id)
    removed += len(drop)
    if args.dry_run:
      continue
    backup.mkdir(parents=True, exist_ok=True)
    shutil.copy2(label_path, backup / f"{image_id}.labels.txt")
    shutil.copy2(auto_path, backup / f"{image_id}.auto.txt")
    new = [row for row in rows if tuple(row) not in drop]
    label_path.write_text("\n".join(" ".join(r) for r in new) + ("\n" if new else ""), encoding="utf-8")
    auto_rows = read(auto_path)
    auto_new = [row for row in auto_rows if not (int(row[0]) == PERSON and tuple(row) in drop)]
    auto_path.write_text("\n".join(" ".join(r) for r in auto_new) + ("\n" if auto_new else ""), encoding="utf-8")
  report = {"dataset": str(root), "dry_run": args.dry_run,
            "images_changed": len(changed), "persons_removed": removed, "changed_ids": changed}
  out = Path(args.report) if args.report else root / f"rider_clean_report_{stamp}.json"
  out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
  print(json.dumps({k: report[k] for k in ("images_changed", "persons_removed", "dry_run")}, ensure_ascii=False))
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
