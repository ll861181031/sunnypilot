#!/usr/bin/env python3
"""Stage 2: auto pre-label extracted frames with the shipped YOLO11n (COCO) model.

For every frame we run the same ONNX model ``yolod`` ships (ONNX Runtime, 2
threads per worker, ``conf=0.18`` to favour recall, ``iou=0.45``), map the COCO
class names onto the annotation taxonomy, and drop the rider duplicates: COCO
labels the rider of a motorcycle/bicycle as a separate ``person``, while the
taxonomy says the two-wheeler box *contains* its rider and ``person`` is only for
pedestrians. Those boxes go to the annotator as review candidates, so a low
threshold plus ``auto_meta.json`` confidences is the point: recall first, the
human fixes the class.

Outputs (under ``--work``)::

    _auto/<route>.json   {"route", "model", "conf", "iou", "dropped": {...},
                          "frames": {"<nnnn>": [{"cls", "conf", "xyxy", "coco"}]}}

Only classes present in ``coco_to_taxonomy`` survive; other COCO classes are
counted in ``dropped`` so a model swap does not silently lose detections.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common
sys.path.insert(0, "/data/sunnypilot/openpilot")

DEFAULT_CONF = 0.18
DEFAULT_IOU = 0.45
DEFAULT_THREADS = 2
# A rider is inside (or nearly inside) the two-wheeler box. Either test is enough:
# the centre test covers a person box smaller than the bike box, the IoU test
# covers a person box that is roughly the same size as the bike box.
RIDER_IOU = 0.1

_DETECTOR = None
_TAXONOMY = None


def _iou(a, b) -> float:
  inter_w = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
  inter_h = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
  inter = inter_w * inter_h
  if inter <= 0.0:
    return 0.0
  area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
  area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
  union = area_a + area_b - inter
  return inter / union if union > 0 else 0.0


def is_inside(box, outer) -> bool:
  """True when the centre of ``box`` lies inside ``outer``."""
  cx = (box[0] + box[2]) / 2.0
  cy = (box[1] + box[3]) / 2.0
  return outer[0] <= cx <= outer[2] and outer[1] <= cy <= outer[3]


def suppress_riders(detections: list[dict]) -> tuple[list[dict], int]:
  """Drop ``person`` detections that belong to a two-wheeler (its rider).

  ``detections`` entries need ``cls`` and ``xyxy``. Returns (kept, n_dropped).
  A person is dropped when its centre falls inside a two-wheeler box *or* the IoU
  with it exceeds ``RIDER_IOU``.
  """
  two_wheelers = [d["xyxy"] for d in detections if int(d["cls"]) in common.TWO_WHEELER_IDS]
  if not two_wheelers:
    return detections, 0
  kept, dropped = [], 0
  for det in detections:
    if int(det["cls"]) == common.PERSON_ID:
      box = det["xyxy"]
      if any(is_inside(box, tw) or _iou(box, tw) > RIDER_IOU for tw in two_wheelers):
        dropped += 1
        continue
    kept.append(det)
  return kept, dropped


def _init_worker(model: str, conf: float, iou: float, threads: int, taxonomy: str) -> None:
  """Build the detector once per worker process (ORT sessions are not fork-safe)."""
  global _DETECTOR, _TAXONOMY
  from sunnypilot.yolod.detector import Detector
  _DETECTOR = Detector(model, conf=conf, iou=iou, threads=threads)
  _DETECTOR.warmup()
  _TAXONOMY = common.load_taxonomy(taxonomy)


def autolabel_route(job: dict) -> dict:
  """Worker: label every frame of one route."""
  import cv2
  route = job["route"]
  work = job["work"]
  started = time.monotonic()
  meta_file = os.path.join(work, "_meta", f"{route}.json")
  out_file = os.path.join(work, "_auto", f"{route}.json")
  if not job["force"] and os.path.isfile(out_file):
    cached = common.read_json(out_file, {})
    if cached.get("frames") and cached.get("conf") == job["conf"] and cached.get("model") == job["model"]:
      return {"route": route, "status": "cached", "frames": len(cached["frames"]),
              "detections": sum(len(v) for v in cached["frames"].values()),
              "seconds": 0.0}

  meta = common.read_json(meta_file)
  if not meta or not meta.get("frames"):
    return {"route": route, "status": "no_frames", "frames": 0, "detections": 0, "seconds": 0.0}

  frame_dir = os.path.join(work, "_frames", route)
  taxonomy: common.Taxonomy = _TAXONOMY
  detector = _DETECTOR
  frames: dict[str, list[dict]] = {}
  unmapped: dict[str, int] = {}
  riders_dropped = 0
  inference_ms: list[float] = []
  for entry in meta["frames"]:
    stem = os.path.splitext(entry["file"])[0]
    image = cv2.imread(os.path.join(frame_dir, entry["file"]))
    if image is None:
      continue
    detections = []
    for det in detector.detect(image):
      cls = taxonomy.map_coco(det.label)
      if cls is None:
        unmapped[det.label] = unmapped.get(det.label, 0) + 1
        continue
      detections.append({"cls": cls, "conf": round(float(det.score), 4),
                         "xyxy": [round(float(v), 6) for v in det.box], "coco": det.label})
    inference_ms.append(detector.inference_ms)
    detections, dropped = suppress_riders(detections)
    riders_dropped += dropped
    detections.sort(key=lambda d: (-d["conf"], d["cls"]))
    frames[stem] = detections

  payload = {"route": route, "model": os.path.basename(job["model"]), "model_path": job["model"],
             "conf": job["conf"], "iou": job["iou"], "daylight": meta.get("daylight"),
             "hour": meta.get("hour"),
             "mean_inference_ms": round(sum(inference_ms) / len(inference_ms), 2) if inference_ms else 0.0,
             "dropped": {"unmapped_coco": unmapped, "rider_person": riders_dropped},
             "frames": frames}
  common.write_json(out_file, payload)
  return {"route": route, "status": "ok", "frames": len(frames),
          "detections": sum(len(v) for v in frames.values()),
          "riders": riders_dropped, "seconds": round(time.monotonic() - started, 2)}


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
  parser.add_argument("--work", default=common.DEFAULT_WORK_ROOT)
  parser.add_argument("--model", default=common.DEFAULT_MODEL)
  parser.add_argument("--taxonomy", default=common.DEFAULT_TAXONOMY)
  parser.add_argument("--conf", type=float, default=DEFAULT_CONF)
  parser.add_argument("--iou", type=float, default=DEFAULT_IOU)
  parser.add_argument("--threads", type=int, default=DEFAULT_THREADS, help="ORT threads per worker")
  parser.add_argument("--workers", type=int, default=8)
  parser.add_argument("--force", action="store_true")
  parser.add_argument("--route", action="append", default=[], help="limit to these routes")
  parser.add_argument("--limit-routes", type=int, default=0)
  args = parser.parse_args(argv)

  if not os.path.isfile(args.model):
    print(f"model not found: {args.model}", file=sys.stderr)
    return 1
  meta_root = os.path.join(args.work, "_meta")
  if not os.path.isdir(meta_root):
    print(f"no extracted frames under {args.work}; run extract.py first", file=sys.stderr)
    return 1
  routes = sorted(os.path.splitext(f)[0] for f in os.listdir(meta_root) if f.endswith(".json"))
  if args.route:
    wanted = set(args.route)
    routes = [r for r in routes if r in wanted]
  if args.limit_routes > 0:
    routes = routes[:args.limit_routes]
  if not routes:
    print("nothing to label", file=sys.stderr)
    return 1

  os.makedirs(os.path.join(args.work, "_auto"), exist_ok=True)
  jobs = [{"route": r, "work": args.work, "model": args.model, "conf": args.conf,
           "iou": args.iou, "force": args.force} for r in routes]
  started = time.monotonic()
  status_count: dict[str, int] = {}
  total_frames = total_detections = total_riders = 0
  done = 0
  with ProcessPoolExecutor(max_workers=max(1, args.workers),
                           initializer=_init_worker,
                           initargs=(args.model, args.conf, args.iou, args.threads,
                                     args.taxonomy)) as pool:
    futures = [pool.submit(autolabel_route, job) for job in jobs]
    for future in as_completed(futures):
      try:
        result = future.result()
      except Exception as error:
        result = {"route": "?", "status": "crash", "frames": 0, "detections": 0,
                  "seconds": 0.0, "error": f"{type(error).__name__}: {error}"}
      status_count[result["status"]] = status_count.get(result["status"], 0) + 1
      total_frames += result["frames"]
      total_detections += result["detections"]
      total_riders += result.get("riders", 0)
      done += 1
      if done % 50 == 0 or result["status"] not in ("ok", "cached"):
        head = f"[{done}/{len(jobs)}] {result['route']} {result['status']}"
        detail = f"frames={result['frames']} dets={result['detections']} {result['seconds']}s"
        print(f"{head} {detail} {result.get('error', '')}".rstrip(), flush=True)

  elapsed = time.monotonic() - started
  summary = {"routes": len(jobs), "frames": total_frames, "detections": total_detections,
             "rider_duplicates_removed": total_riders, "seconds": round(elapsed, 1),
             "ms_per_frame": round(elapsed * 1000.0 / total_frames, 1) if total_frames else 0,
             "status": status_count, "conf": args.conf, "iou": args.iou,
             "model": os.path.basename(args.model)}
  common.write_json(os.path.join(args.work, "_autolabel_summary.json"), summary)
  riders = f"{total_riders} rider duplicates removed"
  timing = f"{elapsed:.1f}s ({summary['ms_per_frame']} ms/frame)"
  print(f"autolabel: {total_frames} frames, {total_detections} detections, {riders} in {timing}")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
