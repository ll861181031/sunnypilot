#!/usr/bin/env python3
"""Remove auto traffic_light boxes classified as flags / signs / lanterns by CLIP.

Reads the JSON produced by clip_tl_scan.py. Only images that are not reviewed or
edited are touched, so manual boxes are preserved. Conservative rules:
  flag/red-flag >=0.45, red-lantern >=0.65, street-light >=0.60,
  road-sign >=0.50, billboard/glow >=0.50.
Car-tail-light and uncertain classes are kept (a green/red light can look like
one), and boxes CLIP calls a traffic light are always kept.
"""
from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path


def to_xyxy(line: list[str]) -> tuple[float, float, float, float]:
  cx, cy, w, h = map(float, line[1:5])
  return cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2


def key(box) -> tuple[float, ...]:
  return tuple(round(float(v), 6) for v in box)


def should_remove(score: dict) -> bool:
  top, prob = score["top"], score["prob"]
  if top in ("a photo of a Chinese national flag", "a red flag with yellow stars"):
    return prob >= 0.45
  if top == "a photo of a red lantern":
    return prob >= 0.65
  if top == "a photo of a street light":
    return prob >= 0.60
  if top == "a photo of a road sign or warning sign":
    return prob >= 0.50
  if top in ("a photo of a glowing neon shop sign", "a photo of a billboard or advertising sign"):
    return prob >= 0.50
  return False


def main(argv=None) -> int:
  parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
  parser.add_argument("dataset")
  parser.add_argument("scores")
  parser.add_argument("--dry-run", action="store_true")
  parser.add_argument("--report", default=None)
  args = parser.parse_args(argv)
  root = Path(args.dataset)
  scores = json.loads(Path(args.scores).read_text(encoding="utf-8"))
  by_id: dict[str, list[dict]] = {}
  for item in scores:
    by_id.setdefault(item["id"], []).append(item)
  tax = json.loads((root / "taxonomy.json").read_text(encoding="utf-8"))
  tl = next(c["id"] for c in tax["classes"] if c["name"] == "traffic_light")
  review = json.loads((root / "review_state.json").read_text(encoding="utf-8")) if (root / "review_state.json").is_file() else {}
  auto_meta_path = root / "auto_meta.json"
  auto_meta = json.loads(auto_meta_path.read_text(encoding="utf-8")) if auto_meta_path.is_file() else {}
  stamp = time.strftime("%Y%m%d-%H%M%S")
  backup = root / f"labels_backup_clip_{stamp}"
  changed, removed = [], 0
  for label_path in sorted((root / "labels").glob("*.txt")):
    stem = label_path.stem
    state = review.get(stem, {})
    if state.get("reviewed") or state.get("edited"):
      continue
    lines = [ln for ln in label_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    drop = []
    for ln in lines:
      parts = ln.split()
      if len(parts) < 5 or int(parts[0]) != tl:
        continue
      # normalized xyxy from label line, rounded
      xyxy = key(to_xyxy(parts))
      score = None
      best_iou = 0.0
      for cand in by_id.get(stem, []):
        cbox = (cand["box"][0] / 1344.0, cand["box"][1] / 760.0,
                cand["box"][2] / 1344.0, cand["box"][3] / 760.0)
        ix1, iy1 = max(xyxy[0], cbox[0]), max(xyxy[1], cbox[1])
        ix2, iy2 = min(xyxy[2], cbox[2]), min(xyxy[3], cbox[3])
        inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
        union = ((xyxy[2] - xyxy[0]) * (xyxy[3] - xyxy[1])
                 + (cbox[2] - cbox[0]) * (cbox[3] - cbox[1]) - inter)
        value = inter / union if union > 0 else 0.0
        if value > best_iou:
          best_iou, score = value, cand
      if score and should_remove(score):
        drop.append(xyxy)
    if not drop:
      continue
    changed.append(stem)
    removed += len(drop)
    if args.dry_run:
      continue
    backup.mkdir(parents=True, exist_ok=True)
    shutil.copy2(label_path, backup / f"{stem}.labels.txt")
    auto_path = root / "auto_labels" / f"{stem}.txt"
    if auto_path.is_file():
      shutil.copy2(auto_path, backup / f"{stem}.auto.txt")
    new_lines = []
    for ln in lines:
      parts = ln.split()
      if len(parts) >= 5 and int(parts[0]) == tl and key(to_xyxy(parts)) in drop:
        continue
      new_lines.append(ln)
    label_path.write_text("\n".join(new_lines) + ("\n" if new_lines else ""), encoding="utf-8")
    if auto_path.is_file():
      auto_lines = [ln for ln in auto_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
      auto_lines = [ln for ln in auto_lines
                    if not (int(ln.split()[0]) == tl and key(to_xyxy(ln.split())) in drop)]
      auto_path.write_text("\n".join(auto_lines) + ("\n" if auto_lines else ""), encoding="utf-8")
    if stem in auto_meta:
      auto_meta[stem] = [item for item in auto_meta[stem]
                         if not (item.get("cls") == tl and key(item.get("xyxy", ())) in drop)]
  if not args.dry_run and changed:
    auto_meta_path.write_text(json.dumps(auto_meta, ensure_ascii=False), encoding="utf-8")
  report = {"dataset": str(root), "dry_run": args.dry_run, "images_changed": len(changed),
            "boxes_removed": removed, "changed_ids": changed}
  out = Path(args.report) if args.report else root / f"clip_filter_report_{stamp}.json"
  out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
  print(json.dumps({k: report[k] for k in ("images_changed", "boxes_removed", "dry_run")}, ensure_ascii=False))
  print(f"report: {out}")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
