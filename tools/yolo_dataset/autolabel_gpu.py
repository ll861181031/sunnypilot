#!/usr/bin/env python3
"""GPU auto-labelling for the YOLO dataset pipeline (Windows/NVIDIA host).

Runs Ultralytics YOLO11n on CUDA instead of the ONNX Runtime CPU path in
``autolabel.py``. Input is an existing dataset (images/ + taxonomy.json) or a
plain image directory; output is ``auto_labels/<id>.txt`` + ``auto_meta.json``
(matching the Linux pipeline) and optionally ``labels/`` for unreviewed images.

This is the tool that keeps heavy CPU/GPU work on the LAN training host; the
car PC only edits labels and merges uploads.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


def load_taxonomy(path: Path) -> tuple[dict, dict]:
  data = json.loads(path.read_text(encoding="utf-8"))
  by_name = {c["name"]: int(c["id"]) for c in data["classes"]}
  coco_map = data.get("coco_to_taxonomy", {})
  return by_name, coco_map


def inside(box: tuple, outer: tuple) -> bool:
  x1, y1, x2, y2 = box
  ox1, oy1, ox2, oy2 = outer
  cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
  return ox1 <= cx <= ox2 and oy1 <= cy <= oy2


def iou(a: tuple, b: tuple) -> float:
  x1, y1 = max(a[0], b[0]), max(a[1], b[1])
  x2, y2 = min(a[2], b[2]), min(a[3], b[3])
  inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
  ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
  return inter / ua if ua > 0 else 0.0


def convert(result, names, class_ids: dict, coco_map: dict) -> tuple[list[str], list[dict]]:
  raw: list[dict] = []
  for box in result.boxes:
    coco_name = names[int(box.cls[0])]
    target = coco_map.get(coco_name)
    if target is None:
      continue
    x1, y1, x2, y2 = [float(v) for v in box.xyxyn[0]]
    raw.append({"cls": int(class_ids[target]), "conf": float(box.conf[0]), "xyxy": [x1, y1, x2, y2],
                "coco": coco_name})
  # Rider dedup: drop person boxes whose centre is inside a two-wheeler box.
  two = [d for d in raw if d["coco"] in ("motorcycle", "bicycle")]
  kept = []
  for det in raw:
    if det["coco"] == "person":
      box = tuple(det["xyxy"])
      if any(inside(box, tuple(t["xyxy"])) or iou(box, tuple(t["xyxy"])) > 0.1 for t in two):
        continue
    kept.append(det)
  lines = []
  meta = []
  for det in kept:
    x1, y1, x2, y2 = det["xyxy"]
    cx, cy, w, h = (x1 + x2) / 2, (y1 + y2) / 2, x2 - x1, y2 - y1
    lines.append(f"{det['cls']} {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}")
    meta.append({"cls": det["cls"], "conf": det["conf"], "xyxy": det["xyxy"]})
  return lines, meta


def main(argv=None) -> int:
  parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
  parser.add_argument("dataset", help="batch dir with images/ + taxonomy.json (or dataset.json)")
  parser.add_argument("--images", help="override image directory")
  parser.add_argument("--out-dir", default=None, help="output dir (default: <dataset>/gpu_auto)")
  parser.add_argument("--limit", type=int, default=0, help="process first N images only (0=all)")
  parser.add_argument("--batch", type=int, default=32)
  parser.add_argument("--conf", type=float, default=0.18)
  parser.add_argument("--iou", type=float, default=0.45)
  parser.add_argument("--imgsz", type=int, default=640)
  parser.add_argument("--device", default="0")
  parser.add_argument("--weights", default="yolo11n.pt")
  parser.add_argument("--update-labels", action="store_true",
                      help="also write labels/ for images that are not reviewed/edited")
  args = parser.parse_args(argv)

  root = Path(args.dataset)
  tax_path = root / "taxonomy.json"
  if not tax_path.is_file():
    raise SystemExit(f"missing taxonomy: {tax_path}")
  class_ids, coco_map = load_taxonomy(tax_path)
  img_dir = Path(args.images) if args.images else root / "images"
  out_dir = Path(args.out_dir) if args.out_dir else root / "gpu_auto"
  labels_dir = out_dir / "labels"
  labels_dir.mkdir(parents=True, exist_ok=True)

  review = {}
  if (root / "review_state.json").is_file():
    review = json.loads((root / "review_state.json").read_text(encoding="utf-8"))

  images = sorted(img_dir.glob("*.jpg"))
  if args.limit:
    images = images[: args.limit]
  from ultralytics import YOLO
  import torch
  print(f"torch {torch.__version__} cuda={torch.cuda.is_available()} device={args.device} images={len(images)}")
  model = YOLO(args.weights)
  names = model.names
  meta_all = {}
  counts = Counter()
  processed = 0
  for result in model.predict(source=[str(p) for p in images], stream=True, batch=args.batch,
                              imgsz=args.imgsz, conf=args.conf, iou=args.iou,
                              device=args.device, verbose=False):
    stem = Path(result.path).stem
    lines, meta = convert(result, names, class_ids, coco_map)
    (labels_dir / f"{stem}.txt").write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    meta_all[stem] = meta
    counts.update(item["cls"] for item in meta)
    processed += 1
    if processed % 500 == 0:
      print(f"  {processed}/{len(images)}", flush=True)
  (out_dir / "auto_meta.json").write_text(json.dumps(meta_all), encoding="utf-8")
  (out_dir / "class_counts.json").write_text(json.dumps(dict(sorted(counts.items()))), encoding="utf-8")
  print(f"done {processed} images -> {out_dir}; boxes={sum(counts.values())}")

  if args.update_labels:
    n = 0
    for stem, meta in meta_all.items():
      state = review.get(stem, {})
      if state.get("reviewed") or state.get("edited"):
        continue
      lines = []
      for item in meta:
        x1, y1, x2, y2 = item["xyxy"]
        lines.append(f"{item['cls']} {(x1+x2)/2:.6f} {(y1+y2)/2:.6f} {x2-x1:.6f} {y2-y1:.6f}")
      (root / "labels" / f"{stem}.txt").write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
      n += 1
    print(f"updated labels for {n} unreviewed images")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
