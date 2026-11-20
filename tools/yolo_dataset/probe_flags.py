#!/usr/bin/env python3
"""Probe auto traffic-light boxes that look like red flags (diagnostic only)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np


def red_fraction(crop: np.ndarray) -> float:
  hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
  m1 = cv2.inRange(hsv, (0, 120, 80), (10, 255, 255))
  m2 = cv2.inRange(hsv, (170, 120, 80), (180, 255, 255))
  return float(((m1 | m2) > 0).mean())


def main(argv=None):
  parser = argparse.ArgumentParser()
  parser.add_argument("dataset")
  parser.add_argument("--out", default="flag_probe")
  parser.add_argument("--limit", type=int, default=4000)
  parser.add_argument("--top", type=int, default=40)
  args = parser.parse_args(argv)
  root = Path(args.dataset)
  tax = json.loads((root / "taxonomy.json").read_text(encoding="utf-8"))
  tl = next(c["id"] for c in tax["classes"] if c["name"] == "traffic_light")
  rows = []
  images = sorted((root / "images").glob("*.jpg"))[: args.limit]
  for path in images:
    label = root / "labels" / f"{path.stem}.txt"
    if not label.is_file():
      continue
    lines = [ln.split() for ln in label.read_text().splitlines() if ln.strip()]
    boxes = [ln for ln in lines if len(ln) >= 5 and int(ln[0]) == tl]
    if not boxes:
      continue
    img = cv2.imread(str(path))
    if img is None:
      continue
    h, w = img.shape[:2]
    for ln in boxes:
      cx, cy, bw, bh = map(float, ln[1:5])
      x1, y1 = int((cx - bw / 2) * w), int((cy - bh / 2) * h)
      x2, y2 = int((cx + bw / 2) * w), int((cy + bh / 2) * h)
      x1, y1 = max(0, x1), max(0, y1)
      x2, y2 = min(w, x2), min(h, y2)
      if x2 - x1 < 4 or y2 - y1 < 4:
        continue
      crop = img[y1:y2, x1:x2]
      rows.append((red_fraction(crop), (x2 - x1) * (y2 - y1) / (w * h), path.stem,
                   (x1, y1, x2, y2), crop))
  rows.sort(key=lambda r: (r[0], r[1]), reverse=True)
  out = Path(args.out)
  out.mkdir(parents=True, exist_ok=True)
  report = []
  tiles = []
  for _idx, (red, area, stem, box, crop) in enumerate(rows[: args.top]):
    tile = cv2.resize(crop, (160, 160))
    cv2.putText(tile, f"{red:.2f}/{area:.4f}", (3, 14), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 0), 1)
    tiles.append(tile)
    report.append({"id": stem, "red": red, "area": area, "box": box})
  if tiles:
    cols = 8
    while len(tiles) % cols:
      tiles.append(np.zeros((160, 160, 3), np.uint8))
    sheet = np.vstack([np.hstack(tiles[i:i + cols]) for i in range(0, len(tiles), cols)])
    cv2.imwrite(str(out / "sheet.jpg"), sheet)
  (out / "top.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
  print(f"traffic_light boxes={len(rows)} top={len(report)} sheet={out / 'sheet.jpg'}")
  print("red>=0.45", sum(1 for r in rows if r[0] >= 0.45), "red>=0.60", sum(1 for r in rows if r[0] >= 0.60))
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
