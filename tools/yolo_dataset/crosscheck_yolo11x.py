#!/usr/bin/env python3
"""High-precision YOLO11x cross-check of the current labels (Windows GPU).

Runs COCO YOLO11x on every image and compares predictions with the human /
auto labels. It does NOT modify labels: it writes a report of
``missing`` (model found, label absent), ``class_mismatch`` (same object,
different class) and ``model_missed`` (label class that the model knows but
did not find). COCO cannot represent tricycle/e-bike, so labels of those
classes are never reported as mismatches and model bicycle/motorcycle
predictions are mapped to ``two_wheeler``.

The images are processed in chunks with an explicit CUDA cache clear between
chunks: a single stream over 17k images leaks GPU memory and OOMs on 12 GB.
"""
from __future__ import annotations

import argparse
import gc
import json
import time
from collections import Counter
from pathlib import Path

COCO_TO_TAXONOMY = {
  "person": 0,
  "bicycle": 1,
  "motorcycle": 1,
  "car": 3,
  "truck": 5,
  "bus": 6,
  "traffic light": 14,
  "stop sign": 15,
}
MODEL_MISS_CHECK = {0, 3, 5, 6, 14, 15}


def iou(a: tuple[float, ...], b: tuple[float, ...]) -> float:
  ax1, ay1, ax2, ay2 = a
  bx1, by1, bx2, by2 = b
  ix1, iy1 = max(ax1, bx1), max(ay1, by1)
  ix2, iy2 = min(ax2, bx2), min(ay2, by2)
  inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
  union = (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - inter
  return inter / union if union > 0 else 0.0


def read_labels(path: Path) -> list[tuple[int, tuple[float, ...]]]:
  out = []
  if not path.is_file():
    return out
  for line in path.read_text(encoding="utf-8").splitlines():
    parts = line.split()
    if len(parts) < 5:
      continue
    cx, cy, w, h = map(float, parts[1:5])
    out.append((int(parts[0]), (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)))
  return out


def process_result(result, root: Path, class_names: list[str], records: list[dict], summary: Counter) -> dict | None:
  stem = Path(result.path).stem
  labels = read_labels(root / "labels" / f"{stem}.txt")
  label_boxes = [box for _, box in labels]
  preds = []
  for box in result.boxes:
    coco_name = result.names[int(box.cls[0])]
    cls = COCO_TO_TAXONOMY.get(coco_name)
    if cls is None:
      continue
    x1, y1, x2, y2 = (float(v) for v in box.xyxyn[0])
    preds.append({"cls": cls, "conf": float(box.conf[0]), "box": (x1, y1, x2, y2)})
  used = set()
  missing, mismatch = [], []
  for pred in preds:
    best_iou, best_idx = 0.0, -1
    for idx, label_box in enumerate(label_boxes):
      if idx in used:
        continue
      value = iou(pred["box"], label_box)
      if value > best_iou:
        best_iou, best_idx = value, idx
    if best_iou >= 0.5 and best_idx >= 0:
      used.add(best_idx)
      label_cls = labels[best_idx][0]
      if pred["cls"] != label_cls and label_cls not in (1, 2):
        mismatch.append({"label_cls": class_names[label_cls], "pred_cls": class_names[pred["cls"]],
                         "conf": pred["conf"], "box": pred["box"]})
    elif pred["conf"] >= 0.35:
      if any(iou(pred["box"], box) > 0.3 for cls, box in labels if cls in (1, 2)):
        continue
      missing.append({"cls": class_names[pred["cls"]], "conf": pred["conf"], "box": pred["box"]})
  model_missed = []
  predicted_known = {p["cls"] for p in preds if p["conf"] >= 0.20}
  for idx, (cls, box) in enumerate(labels):
    if idx in used or cls not in MODEL_MISS_CHECK:
      continue
    if cls not in predicted_known:
      model_missed.append({"cls": class_names[cls], "box": box})
  if not (missing or mismatch or model_missed):
    return None
  rec = {"dataset": root.name, "id": stem, "missing": missing,
         "class_mismatch": mismatch, "model_missed": model_missed}
  records.append(rec)
  for item in missing:
    summary[f"missing:{item['cls']}"] += 1
  for item in mismatch:
    summary[f"mismatch:{item['label_cls']}->{item['pred_cls']}"] += 1
  for item in model_missed:
    summary[f"model_missed:{item['cls']}"] += 1
  return rec


def main(argv=None) -> int:
  ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
  ap.add_argument("datasets", nargs="+")
  ap.add_argument("--out", required=True)
  ap.add_argument("--weights", default="yolo11x.pt")
  ap.add_argument("--imgsz", type=int, default=640)
  ap.add_argument("--conf", type=float, default=0.20)
  ap.add_argument("--iou", type=float, default=0.5)
  ap.add_argument("--batch", type=int, default=4)
  ap.add_argument("--chunk", type=int, default=500)
  ap.add_argument("--device", default="0")
  ap.add_argument("--half", action="store_true")
  ap.add_argument("--limit", type=int, default=0)
  args = ap.parse_args(argv)
  out_dir = Path(args.out)
  out_dir.mkdir(parents=True, exist_ok=True)
  from ultralytics import YOLO
  import torch
  print(f"torch {torch.__version__} cuda={torch.cuda.is_available()} device={args.device}", flush=True)
  model = YOLO(args.weights)
  summary = Counter()
  records = []
  started = time.monotonic()
  processed = 0
  total_images = sum(len(list((Path(dataset) / "images").glob("*.jpg"))) for dataset in args.datasets)
  for dataset in args.datasets:
    root = Path(dataset)
    payload = json.loads((root / "dataset.json").read_text(encoding="utf-8"))
    class_names = [c["name"] for c in payload["classes"]]
    images = sorted((root / "images").glob("*.jpg"))
    if args.limit:
      images = images[: args.limit]
    print(f"{root}: {len(images)} images", flush=True)
    for chunk_start in range(0, len(images), args.chunk):
      chunk = images[chunk_start:chunk_start + args.chunk]
      for result in model.predict(
          source=[str(p) for p in chunk], imgsz=args.imgsz, conf=args.conf, iou=args.iou,
          batch=args.batch, device=args.device, half=args.half, stream=True, verbose=False):
        process_result(result, root, class_names, records, summary)
        processed += 1
        if processed % 1000 == 0:
            elapsed = int(time.monotonic() - started)
            print(f"  {processed}/{total_images} elapsed={elapsed}s records={len(records)}", flush=True)
      gc.collect()
      torch.cuda.empty_cache()
  with (out_dir / "crosscheck_records.jsonl").open("w", encoding="utf-8") as handle:
    for rec in records:
      handle.write(json.dumps(rec, ensure_ascii=False) + "\n")
  report = {"datasets": args.datasets, "images_processed": processed,
            "images_with_findings": len(records), "elapsed_seconds": round(time.monotonic() - started, 1),
            "summary": dict(sorted(summary.items(), key=lambda kv: -kv[1]))}
  summary_text = json.dumps(report, ensure_ascii=False, indent=1) + "\n"
  (out_dir / "crosscheck_summary.json").write_text(summary_text, encoding="utf-8")
  print(json.dumps(report["summary"], ensure_ascii=False, indent=1))
  print(f"processed={processed} findings={len(records)} elapsed={report['elapsed_seconds']}s")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
