#!/usr/bin/env python3
"""Inspect remaining traffic_light labels (flags / glowing signs) with contact sheets."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np


def fractions(crop: np.ndarray) -> dict:
  hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
  h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
  red = (((h <= 10) | (h >= 170)) & (s >= 120) & (v >= 80)).mean()
  green = ((h >= 35) & (h <= 90) & (s >= 100) & (v >= 80)).mean()
  yellow = ((h >= 20) & (h <= 35) & (s >= 120) & (v >= 120)).mean()
  blue = ((h >= 90) & (h <= 130) & (s >= 100) & (v >= 80)).mean()
  bright = (v >= 200).mean()
  return {"red": float(red), "green": float(green), "yellow": float(yellow),
          "blue": float(blue), "bright": float(bright), "sat": float(s.mean() / 255),
          "val": float(v.mean() / 255)}


def main(argv=None) -> int:
  ap = argparse.ArgumentParser()
  ap.add_argument("dataset")
  ap.add_argument("--out", default="tl_probe")
  ap.add_argument("--limit", type=int, default=5000)
  ap.add_argument("--top", type=int, default=48)
  args = ap.parse_args(argv)
  root = Path(args.dataset)
  tax = json.loads((root / "taxonomy.json").read_text(encoding="utf-8"))
  tl = next(c["id"] for c in tax["classes"] if c["name"] == "traffic_light")
  review = json.loads((root / "review_state.json").read_text(encoding="utf-8")) if (root / "review_state.json").is_file() else {}
  rows = []
  for path in sorted((root / "images").glob("*.jpg"))[: args.limit]:
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
      x1, y1 = max(0, int((cx - bw / 2) * w)), max(0, int((cy - bh / 2) * h))
      x2, y2 = min(w, int((cx + bw / 2) * w)), min(h, int((cy + bh / 2) * h))
      if x2 - x1 < 4 or y2 - y1 < 4:
        continue
      crop = img[y1:y2, x1:x2]
      feat = fractions(crop)
      area = (x2 - x1) * (y2 - y1) / (w * h)
      rows.append({"id": path.stem, "box": [x1, y1, x2, y2], "area": area,
                   "aspect": (x2 - x1) / max(1, y2 - y1),
                   "reviewed": bool(review.get(path.stem, {}).get("reviewed")),
                   "edited": bool(review.get(path.stem, {}).get("edited")),
                   **feat})
  out = Path(args.out)
  out.mkdir(parents=True, exist_ok=True)
  (out / "rows.json").write_text(json.dumps(rows), encoding="utf-8")
  print(f"boxes={len(rows)} images={len({r['id'] for r in rows})}")

  def sheet(name, subset, sort_key):
    subset = sorted(subset, key=sort_key, reverse=True)[: args.top * args.top]
    tiles = []
    for r in subset[: args.top * args.top]:
      img = cv2.imread(str(root / "images" / f"{r['id']}.jpg"))
      x1, y1, x2, y2 = r["box"]
      crop = cv2.resize(img[y1:y2, x1:x2], (120, 120))
      cv2.putText(crop, f"a{r['area']:.4f}", (2, 12), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (0, 255, 0), 1)
      cv2.putText(crop, f"r{r['red']:.2f} g{r['green']:.2f}", (2, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.32, (0, 255, 0), 1)
      tiles.append(crop)
    cols = 8
    while len(tiles) % cols:
      tiles.append(np.zeros((120, 120, 3), np.uint8))
    if tiles:
      cv2.imwrite(str(out / f"{name}.jpg"), np.vstack([np.hstack(tiles[i:i + cols]) for i in range(0, len(tiles), cols)]))
    print(name, len(subset))

  sheet("largest", rows, lambda r: r["area"])
  sheet("brightest", rows, lambda r: r["bright"] * r["sat"])
  sheet("greenish", [r for r in rows if r["green"] > 0.25], lambda r: r["green"])
  sheet("red_mid", [r for r in rows if 0.35 <= r["red"] < 0.75], lambda r: r["red"])
  sheet("sample", rows[::max(1, len(rows) // (args.top * 2))], lambda r: 0)
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
