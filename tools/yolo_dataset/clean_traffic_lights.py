#!/usr/bin/env python3
"""Second-pass cleanup of auto traffic_light boxes that are flags / glowing signs.

Traffic lights are small, mostly-dark housings with a lens. The false positives
seen on this corpus are:
  * red flags (large red rectangles, often with yellow stars),
  * glowing shop signs / billboards (bright, saturated blobs),
  * blue direction signs and yellow warning signs.

Rules (only applied to images that are NOT reviewed/edited by a human):
  area >= 0.003 | bright>=0.40 | blue>=0.25 | yellow>=0.25 |
  (red>=0.30 and yellow>=0.005)
"""
from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

import cv2
import numpy as np


def features(crop: np.ndarray) -> dict:
  hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
  h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
  red = (((h <= 10) | (h >= 170)) & (s >= 120) & (v >= 80)).mean()
  green = ((h >= 35) & (h <= 90) & (s >= 100) & (v >= 80)).mean()
  yellow = ((h >= 20) & (h <= 35) & (s >= 120) & (v >= 120)).mean()
  blue = ((h >= 90) & (h <= 130) & (s >= 100) & (v >= 80)).mean()
  bright = (v >= 200).mean()
  return {"red": float(red), "green": float(green), "yellow": float(yellow),
          "blue": float(blue), "bright": float(bright)}


def to_xyxy(line: list[str]) -> tuple[float, float, float, float]:
  cx, cy, w, h = map(float, line[1:5])
  return cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2


def match(a: tuple[float, ...], b: tuple[float, ...], tol: float = 1e-4) -> bool:
  return all(abs(x - y) <= tol for x, y in zip(a, b, strict=True))


def is_false_positive(area: float, feat: dict) -> bool:
  if area >= 0.003:
    return True
  if feat["bright"] >= 0.40:
    return True
  if feat["blue"] >= 0.25:
    return True
  if feat["yellow"] >= 0.25:
    return True
  return feat["red"] >= 0.30 and feat["yellow"] >= 0.005


def main(argv=None) -> int:
  parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
  parser.add_argument("dataset")
  parser.add_argument("--dry-run", action="store_true")
  parser.add_argument("--include-edited", action="store_true")
  parser.add_argument("--report", default=None)
  args = parser.parse_args(argv)
  root = Path(args.dataset)
  tax = json.loads((root / "taxonomy.json").read_text(encoding="utf-8"))
  tl = next(c["id"] for c in tax["classes"] if c["name"] == "traffic_light")
  review = json.loads((root / "review_state.json").read_text(encoding="utf-8")) if (root / "review_state.json").is_file() else {}
  auto_meta_path = root / "auto_meta.json"
  auto_meta = json.loads(auto_meta_path.read_text(encoding="utf-8")) if auto_meta_path.is_file() else {}
  stamp = time.strftime("%Y%m%d-%H%M%S")
  backup = root / f"labels_backup_tlclean_{stamp}"
  changed, removed, skipped = [], 0, 0
  for image_path in sorted((root / "images").glob("*.jpg")):
    stem = image_path.stem
    state = review.get(stem, {})
    if (state.get("reviewed") or state.get("edited")) and not args.include_edited:
      skipped += 1
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
    drop: list[tuple[float, ...]] = []
    for line in candidates:
      x1, y1, x2, y2 = to_xyxy(line.split())
      ix1, iy1 = max(0, int(x1 * w)), max(0, int(y1 * h))
      ix2, iy2 = min(w, int(x2 * w)), min(h, int(y2 * h))
      if ix2 - ix1 < 4 or iy2 - iy1 < 4:
        continue
      area = (x2 - x1) * (y2 - y1)
      if is_false_positive(area, features(img[iy1:iy2, ix1:ix2])):
        drop.append((x1, y1, x2, y2))
    if not drop:
      continue
    changed.append(stem)
    removed += len(drop)
    if args.dry_run:
      continue
    backup.mkdir(parents=True, exist_ok=True)
    shutil.copy2(label_path, backup / f"{stem}.labels.txt")
    shutil.copy2(auto_path, backup / f"{stem}.auto.txt")
    label_path.write_text("\n".join(ln for ln in label_lines
                                    if not (int(ln.split()[0]) == tl and any(match(to_xyxy(ln.split()), b) for b in drop)))
                          + ("\n" if label_lines else ""), encoding="utf-8")
    auto_path.write_text("\n".join(ln for ln in auto_lines
                                   if not (int(ln.split()[0]) == tl and any(match(to_xyxy(ln.split()), b) for b in drop)))
                         + ("\n" if auto_lines else ""), encoding="utf-8")
    if stem in auto_meta:
      auto_meta[stem] = [item for item in auto_meta[stem]
                         if not (item.get("cls") == tl and any(match(tuple(item.get("xyxy", ())), b) for b in drop))]
  if not args.dry_run and changed:
    auto_meta_path.write_text(json.dumps(auto_meta, ensure_ascii=False), encoding="utf-8")
  report = {"dataset": str(root), "dry_run": args.dry_run, "images_changed": len(changed),
            "boxes_removed": removed, "skipped_edited": skipped, "changed_ids": changed}
  out = Path(args.report) if args.report else root / f"tlclean_report_{stamp}.json"
  out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
  print(json.dumps({k: report[k] for k in ("images_changed", "boxes_removed", "skipped_edited", "dry_run")}, ensure_ascii=False))
  print(f"report: {out}")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
