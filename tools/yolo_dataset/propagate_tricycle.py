#!/usr/bin/env python3
"""Propagate manual tricycle corrections to neighbouring unedited frames.

The user corrected parked tricycles from ``car`` to ``tricycle`` in a few
frames. The same parked row appears in adjacent frames of the same route where
the auto-labeller still says ``car``. This script finds those frames, matches
auto car boxes against tricycle boxes from the nearest edited frames, and
relabels only unedited images (manual work is never touched).
"""
from __future__ import annotations

import argparse
import json
import shutil
import time
from collections import defaultdict
from pathlib import Path

TRICYCLE = 2
CAR = 3


def read_boxes(path: Path) -> list[tuple[int, tuple[float, ...]]]:
  out = []
  if not path.is_file():
    return out
  for line in path.read_text(encoding="utf-8").splitlines():
    parts = line.split()
    if len(parts) >= 5:
      out.append((int(parts[0]), tuple(float(v) for v in parts[1:5])))
  return out


def iou(a: tuple[float, ...], b: tuple[float, ...]) -> float:
  ax, ay, aw, ah = a
  bx, by, bw, bh = b
  ax1, ay1, ax2, ay2 = ax - aw / 2, ay - ah / 2, ax + aw / 2, ay + ah / 2
  bx1, by1, bx2, by2 = bx - bw / 2, by - bh / 2, bx + bw / 2, by + bh / 2
  ix1, iy1 = max(ax1, bx1), max(ay1, by1)
  ix2, iy2 = min(ax2, bx2), min(ay2, by2)
  inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
  union = (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - inter
  return inter / union if union > 0 else 0.0


def main(argv=None) -> int:
  ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
  ap.add_argument("dataset")
  ap.add_argument("--iou", type=float, default=0.45)
  ap.add_argument("--neighbours", type=int, default=5)
  ap.add_argument("--dry-run", action="store_true")
  ap.add_argument("--report", default=None)
  args = ap.parse_args(argv)
  root = Path(args.dataset)
  payload = json.loads((root / "dataset.json").read_text(encoding="utf-8"))
  images = {im["id"]: im for im in payload["images"]}
  review = json.loads((root / "review_state.json").read_text(encoding="utf-8"))
  edited = {i for i, state in review.items() if state.get("reviewed") or state.get("edited")}
  routes: dict[str, list[str]] = defaultdict(list)
  for image_id, image in images.items():
    routes[image["route"]].append(image_id)

  stamp = time.strftime("%Y%m%d-%H%M%S")
  backup = root / f"labels_backup_tricycle_prop_{stamp}"
  changed: list[str] = []
  boxes_changed = 0
  for _route, ids in routes.items():
    ed = [i for i in ids if i in edited]
    if not ed:
      continue
    tricycle_boxes = {i: [box for cls, box in read_boxes(root / "labels" / f"{i}.txt") if cls == TRICYCLE]
                       for i in ed}
    if not any(tricycle_boxes.values()):
      continue
    for image_id in ids:
      if image_id in edited:
        continue
      label_path = root / "labels" / f"{image_id}.txt"
      auto_path = root / "auto_labels" / f"{image_id}.txt"
      label_rows = [ln.split() for ln in label_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
      if not any(int(row[0]) == CAR for row in label_rows):
        continue
      t = images[image_id]["t"]
      near = sorted(ed, key=lambda j: abs(images[j]["t"] - t))[: args.neighbours]
      refs = [box for j in near for box in tricycle_boxes[j]]
      if not refs:
        continue
      hits = 0
      for row in label_rows:
        if int(row[0]) != CAR:
          continue
        box = tuple(float(v) for v in row[1:5])
        if max(iou(box, ref) for ref in refs) >= args.iou:
          row[0] = str(TRICYCLE)
          hits += 1
      if not hits:
        continue
      changed.append(image_id)
      boxes_changed += hits
      if args.dry_run:
        continue
      backup.mkdir(parents=True, exist_ok=True)
      shutil.copy2(label_path, backup / f"{image_id}.labels.txt")
      if auto_path.is_file():
        shutil.copy2(auto_path, backup / f"{image_id}.auto.txt")
        auto_rows = [ln.split() for ln in auto_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        for row in auto_rows:
          if int(row[0]) != CAR:
            continue
          box = tuple(float(v) for v in row[1:5])
          if max(iou(box, ref) for ref in refs) >= args.iou:
            row[0] = str(TRICYCLE)
        auto_path.write_text("\n".join(" ".join(r) for r in auto_rows) + ("\n" if auto_rows else ""),
                             encoding="utf-8")
      label_path.write_text("\n".join(" ".join(r) for r in label_rows) + ("\n" if label_rows else ""),
                            encoding="utf-8")
      auto_meta_path = root / "auto_meta.json"
      if auto_meta_path.is_file():
        meta = json.loads(auto_meta_path.read_text(encoding="utf-8"))
        for item in meta.get(image_id, []):
          if item.get("cls") == CAR:
            x1, y1, x2, y2 = (float(v) for v in item.get("xyxy", (0, 0, 0, 0)))
            box = ((x1 + x2) / 2, (y1 + y2) / 2, x2 - x1, y2 - y1)
            if max(iou(box, ref) for ref in refs) >= args.iou:
              item["cls"] = TRICYCLE
        auto_meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

  report = {"dataset": str(root), "dry_run": args.dry_run, "images_changed": len(changed),
            "boxes_changed": boxes_changed, "changed_ids": changed}
  out = Path(args.report) if args.report else root / f"tricycle_prop_report_{stamp}.json"
  out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
  print(json.dumps({k: report[k] for k in ("images_changed", "boxes_changed", "dry_run")}, ensure_ascii=False))
  print(f"report: {out}")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
