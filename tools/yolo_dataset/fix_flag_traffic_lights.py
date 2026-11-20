#!/usr/bin/env python3
"""Remove auto "traffic light" boxes that are actually red flags.

COCO YOLO11n sometimes labels Chinese national flags (red with yellow stars) as
traffic lights. This script removes only auto labels on images that have not been
reviewed/edited by a human, so manual work is never touched.

Metrics per traffic-light box: red-pixel fraction (HSV) and box area fraction.
A flag is a large saturated-red rectangle; a real traffic light crop is mostly
dark housing with a small lens, so its red fraction is much lower.
"""
from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

import cv2
import numpy as np

RED_HUE_1 = (0, 120, 80)
RED_HUE_2 = (170, 120, 80)
RED_HUE_3 = (10, 255, 255)
RED_HUE_4 = (180, 255, 255)


def red_fraction(crop: np.ndarray) -> float:
  hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
  mask = cv2.inRange(hsv, RED_HUE_1, RED_HUE_3) | cv2.inRange(hsv, RED_HUE_2, RED_HUE_4)
  return float((mask > 0).mean())


def cxcywh_to_xyxy(line: list[str]) -> tuple[float, float, float, float]:
  cx, cy, w, h = map(float, line[1:5])
  return cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2


def same_box(a: tuple[float, ...], b: tuple[float, ...], tol: float = 1e-4) -> bool:
  return all(abs(x - y) <= tol for x, y in zip(a, b, strict=True))


def main(argv=None) -> int:
  parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
  parser.add_argument("dataset")
  parser.add_argument("--red-threshold", type=float, default=0.75)
  parser.add_argument("--min-area", type=float, default=0.00010)
  parser.add_argument("--dry-run", action="store_true")
  parser.add_argument("--include-edited", action="store_true",
                      help="also clean images marked reviewed/edited (NOT recommended)")
  parser.add_argument("--report", default=None)
  args = parser.parse_args(argv)

  root = Path(args.dataset)
  tax = json.loads((root / "taxonomy.json").read_text(encoding="utf-8"))
  tl = next(c["id"] for c in tax["classes"] if c["name"] == "traffic_light")
  review_path = root / "review_state.json"
  review = json.loads(review_path.read_text(encoding="utf-8")) if review_path.is_file() else {}
  auto_meta_path = root / "auto_meta.json"
  auto_meta = json.loads(auto_meta_path.read_text(encoding="utf-8")) if auto_meta_path.is_file() else {}

  stamp = time.strftime("%Y%m%d-%H%M%S")
  backup = root / f"labels_backup_flagfix_{stamp}"
  changed: list[str] = []
  skipped_edited = 0
  removed_boxes = 0
  seen_images = 0

  for image_path in sorted((root / "images").glob("*.jpg")):
    stem = image_path.stem
    state = review.get(stem, {})
    if (state.get("reviewed") or state.get("edited")) and not args.include_edited:
      # Still count images whose manual labels contain traffic lights? No: manual untouched.
      skipped_edited += 1
      continue
    label_path = root / "labels" / f"{stem}.txt"
    auto_path = root / "auto_labels" / f"{stem}.txt"
    if not label_path.is_file() or not auto_path.is_file():
      continue
    label_lines = [ln for ln in label_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    auto_lines = [ln for ln in auto_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    candidates = [ln for ln in label_lines if int(ln.split()[0]) == tl]
    if not candidates:
      continue
    img = cv2.imread(str(image_path))
    if img is None:
      continue
    h, w = img.shape[:2]
    remove_boxes: list[tuple[float, ...]] = []
    for line in candidates:
      x1, y1, x2, y2 = cxcywh_to_xyxy(line.split())
      ix1, iy1 = max(0, int(x1 * w)), max(0, int(y1 * h))
      ix2, iy2 = min(w, int(x2 * w)), min(h, int(y2 * h))
      if ix2 - ix1 < 4 or iy2 - iy1 < 4:
        continue
      area = (x2 - x1) * (y2 - y1)
      if area < args.min_area:
        continue
      red = red_fraction(img[iy1:iy2, ix1:ix2])
      if red >= args.red_threshold:
        remove_boxes.append((x1, y1, x2, y2))
    if not remove_boxes:
      continue
    seen_images += 1
    changed.append(stem)
    if args.dry_run:
      removed_boxes += len(remove_boxes)
      continue
    backup.mkdir(parents=True, exist_ok=True)
    shutil.copy2(label_path, backup / f"{stem}.labels.txt")
    shutil.copy2(auto_path, backup / f"{stem}.auto.txt")
    new_labels = []
    for line in label_lines:
      if int(line.split()[0]) == tl and any(same_box(cxcywh_to_xyxy(line.split()), b) for b in remove_boxes):
        continue
      new_labels.append(line)
    new_auto = []
    for line in auto_lines:
      if int(line.split()[0]) == tl and any(same_box(cxcywh_to_xyxy(line.split()), b) for b in remove_boxes):
        continue
      new_auto.append(line)
    label_path.write_text("\n".join(new_labels) + ("\n" if new_labels else ""), encoding="utf-8")
    auto_path.write_text("\n".join(new_auto) + ("\n" if new_auto else ""), encoding="utf-8")
    if stem in auto_meta:
      kept = []
      for item in auto_meta[stem]:
        if item.get("cls") == tl and any(same_box(tuple(item.get("xyxy", ())), b) for b in remove_boxes):
          continue
        kept.append(item)
      auto_meta[stem] = kept
    removed_boxes += len(remove_boxes)

  report = {"dataset": str(root), "dry_run": args.dry_run, "images_changed": len(changed),
            "boxes_removed": removed_boxes, "skipped_edited": skipped_edited,
            "red_threshold": args.red_threshold, "min_area": args.min_area,
            "changed_ids": changed}
  if not args.dry_run and changed:
    auto_meta_path.write_text(json.dumps(auto_meta, ensure_ascii=False), encoding="utf-8")
  out = Path(args.report) if args.report else root / f"flagfix_report_{stamp}.json"
  out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
  print(json.dumps({k: report[k] for k in ("images_changed", "boxes_removed", "skipped_edited",
                                          "dry_run", "red_threshold", "min_area")}, ensure_ascii=False))
  print(f"report: {out}")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
