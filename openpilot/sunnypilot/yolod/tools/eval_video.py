#!/usr/bin/env python3
"""Offline YOLO evaluation over a recorded video or a route directory.

Runs the same detector the daemon uses, frame by frame, and writes:

* an annotated mp4 with boxes, labels and scores;
* a JSON report with per-class counts, timing percentiles and throughput.

Examples:
  python openpilot/sunnypilot/yolod/tools/eval_video.py \
    ~/.comma/media/0/realdata/00000097--4f3f72a3b0--12 --max-frames 100 \
    --out /tmp/night_yolo.mp4 --json /tmp/night_yolo.json

The input may also be a single video file. Decoding uses PyAV and falls back to
an ffmpeg pipe when PyAV cannot read the file.
"""

import argparse
import json
import os
import sys
import time
from collections import Counter
from dataclasses import replace

import cv2
import numpy as np

if __package__ in (None, ""):  # allow `python path/to/eval_video.py` from the repo root
  sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")))

from openpilot.sunnypilot.yolod.detector import DEFAULT_CONF, DEFAULT_IOU, Detection
from openpilot.sunnypilot.yolod.paths import (
  DEFAULT_NCNN_BIN,
  DEFAULT_NCNN_NAMES,
  DEFAULT_NCNN_PARAM,
  DEFAULT_ONNX_MODEL,
)
from openpilot.sunnypilot.yolod.video import VideoInfo, find_video_path, iter_frames, probe_video
from openpilot.sunnypilot.yolod.yolod import YoloConfig, create_detector


def _class_color(class_id: int) -> tuple[int, int, int]:
  hue = (class_id * 37) % 180
  hsv = np.uint8([[[hue, 220, 255]]])
  return tuple(int(channel) for channel in cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)[0, 0])


def _draw_detection(image: np.ndarray, detection: Detection, width: int) -> None:
  height = image.shape[0]
  x1 = int(round(detection.box[0] * width))
  y1 = int(round(detection.box[1] * height))
  x2 = int(round(detection.box[2] * width))
  y2 = int(round(detection.box[3] * height))
  color = _class_color(detection.class_id)
  thickness = max(1, round(width / 960))
  cv2.rectangle(image, (x1, y1), (x2, y2), color, thickness)

  label = f"{detection.label} {detection.score:.2f}"
  font_scale = max(0.35, width / 2400.0)
  text_thickness = max(1, thickness - 1)
  (text_w, text_h), baseline = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, font_scale, text_thickness)
  top = max(0, y1 - text_h - baseline)
  cv2.rectangle(image, (x1, top), (x1 + text_w, top + text_h + baseline), color, -1)
  text_color = (0, 0, 0) if sum(color) > 380 else (255, 255, 255)
  cv2.putText(image, label, (x1, top + text_h), cv2.FONT_HERSHEY_SIMPLEX, font_scale,
              text_color, text_thickness, cv2.LINE_AA)


def annotate(image: np.ndarray, detections: list[Detection]) -> np.ndarray:
  annotated = image.copy()
  width = annotated.shape[1]
  for detection in detections:
    _draw_detection(annotated, detection, width)
  cv2.putText(annotated, f"objects: {len(detections)}", (10, max(24, annotated.shape[0] // 40)),
              cv2.FONT_HERSHEY_SIMPLEX, max(0.5, width / 2000.0), (0, 255, 0), max(1, round(width / 1200)),
              cv2.LINE_AA)
  return annotated


def _percentile(values: list[float], quantile: float) -> float:
  if not values:
    return 0.0
  return float(np.percentile(np.asarray(values, dtype=np.float64), quantile))


def _model_name(config: YoloConfig) -> str:
  return config.ncnn_param if config.backend == "ncnn" else config.model_path


def evaluate(video: str, config: YoloConfig, every: int, max_frames: int | None,
             classes: list[str] | None, out_path: str, json_path: str) -> int:
  path = find_video_path(video)
  info: VideoInfo = probe_video(path)
  detector = create_detector(config, classes, strict_filter=True)
  detector.warmup()
  device = getattr(detector, "device_name", "")
  device_text = f" ({device})" if device else ""
  model_name = os.path.basename(_model_name(config))
  print(f"input: {path} ({info.width}x{info.height} @ {info.fps:.1f} fps), backend={detector.backend}{device_text}, model={model_name}")

  writer = None
  if out_path:
    fps = max(1.0, info.fps / every)
    writer = cv2.VideoWriter(out_path, cv2.VideoWriter_fourcc(*"mp4v"), fps, (info.width, info.height))
    if not writer.isOpened():
      print(f"warning: cannot open {out_path} for writing; continuing without the annotated video",
            file=sys.stderr)
      writer = None

  counts: Counter[str] = Counter()
  frames_with: Counter[str] = Counter()
  inference_ms: list[float] = []
  per_frame_counts: list[int] = []
  frames = 0
  started = time.monotonic()

  try:
    for _, image in iter_frames(path, every=every, max_frames=max_frames):
      detections = detector.detect(image)
      inference_ms.append(detector.inference_ms)
      counts.update(detection.label for detection in detections)
      frames_with.update({detection.label for detection in detections})
      per_frame_counts.append(len(detections))
      frames += 1
      if writer is not None:
        writer.write(annotate(image, detections))
      if frames % 100 == 0:
        print(f"  {frames} frames, {frames / (time.monotonic() - started):.1f} fps, last inference {detector.inference_ms:.1f} ms")
  finally:
    if writer is not None:
      writer.release()

  elapsed = max(1e-6, time.monotonic() - started)
  if frames == 0:
    raise RuntimeError(f"no frames decoded from {path}")

  objects_total = sum(counts.values())
  report = {
    "input": path,
    "video": {"width": info.width, "height": info.height, "fps": info.fps, "frame_count": info.frame_count},
    "model": os.path.abspath(_model_name(config)),
    "backend": detector.backend,
    "device": device,
    "conf": config.conf,
    "iou": config.iou,
    "every": every,
    "class_names": detector.class_names,
    "class_filter": classes,
    "frames_processed": frames,
    "objects_total": objects_total,
    "objects_per_frame": objects_total / frames,
    "max_objects_in_frame": max(per_frame_counts) if per_frame_counts else 0,
    "counts": dict(counts.most_common()),
    "frames_with": dict(frames_with.most_common()),
    "inference_ms": {
      "mean": float(np.mean(inference_ms)) if inference_ms else 0.0,
      "p50": _percentile(inference_ms, 50),
      "p90": _percentile(inference_ms, 90),
      "max": max(inference_ms) if inference_ms else 0.0,
    },
    "elapsed_s": elapsed,
    "fps": frames / elapsed,
    "annotated_video": os.path.abspath(out_path) if out_path and writer is not None else None,
  }

  if json_path:
    with open(json_path, "w", encoding="utf-8") as file:
      json.dump(report, file, indent=2, ensure_ascii=False)
    print(f"json: {os.path.abspath(json_path)}")
  if report["annotated_video"]:
    print(f"video: {report['annotated_video']}")

  timing = report["inference_ms"]
  print(f"frames={frames} objects={objects_total} objects_per_frame={report['objects_per_frame']:.2f} fps={report['fps']:.2f}")
  print(f"inference_ms mean={timing['mean']:.1f} p50={timing['p50']:.1f} p90={timing['p90']:.1f} max={timing['max']:.1f}")
  if counts:
    summary = ", ".join(f"{name}={count}" for name, count in counts.most_common())
    print(f"counts: {summary}")
  else:
    print("counts: (no detections)")
  return frames


def _parse_classes(raw: str | None) -> list[str] | None:
  if raw is None or not raw.strip() or raw.strip().lower() == "all":
    return None
  return [name.strip() for name in raw.split(",") if name.strip()]


def _default_paths(video: str, out_path: str | None, json_path: str | None) -> tuple[str, str]:
  # A route directory is named after the route, which is a better report name than
  # the generic "fcamera" every route file shares.
  resolved = find_video_path(video)
  stem = os.path.basename(os.path.normpath(video)) if os.path.isdir(video) else os.path.splitext(os.path.basename(resolved))[0]
  return (out_path if out_path is not None else os.path.join(os.getcwd(), f"{stem}.yolo.mp4"),
          json_path if json_path is not None else os.path.join(os.getcwd(), f"{stem}.yolo.json"))


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(description="Offline YOLO evaluation over a recorded video or route")
  parser.add_argument("video", help="video file or route directory (uses fcamera.hevc)")
  parser.add_argument("--backend", choices=("onnxruntime", "cv2dnn", "ncnn"), default="onnxruntime",
                      help="detector backend: onnxruntime CPU (default), cv2dnn, or ncnn Vulkan GPU")
  parser.add_argument("--model", default=None, help="ONNX model path (default: shipped yolo11n.onnx)")
  parser.add_argument("--ncnn-param", default=DEFAULT_NCNN_PARAM, help="ncnn .param file (with --backend ncnn)")
  parser.add_argument("--ncnn-bin", default=DEFAULT_NCNN_BIN, help="ncnn .bin file (with --backend ncnn)")
  parser.add_argument("--ncnn-names", default=DEFAULT_NCNN_NAMES, help="ncnn class names JSON")
  parser.add_argument("--conf", type=float, default=DEFAULT_CONF, help=f"confidence threshold (default {DEFAULT_CONF})")
  parser.add_argument("--iou", type=float, default=DEFAULT_IOU, help=f"NMS IoU threshold (default {DEFAULT_IOU})")
  parser.add_argument("--threads", type=int, default=2, help="ONNX Runtime intra-op threads (default 2)")
  parser.add_argument("--every", type=int, default=1, help="process one frame out of N (default 1)")
  parser.add_argument("--max-frames", type=int, default=None, help="stop after N processed frames")
  parser.add_argument("--classes", default=None,
                      help="comma-separated class names, or 'all' (default: all model classes)")
  parser.add_argument("--out", default=None, help="annotated mp4 path (default: <cwd>/<stem>.yolo.mp4)")
  parser.add_argument("--json", default=None, help="JSON report path (default: <cwd>/<stem>.yolo.json)")
  args = parser.parse_args(argv)

  if args.every < 1:
    print("error: --every must be >= 1", file=sys.stderr)
    return 2
  classes = _parse_classes(args.classes)
  config = replace(YoloConfig.from_env({}), model_path=args.model or DEFAULT_ONNX_MODEL,
                   conf=args.conf, iou=args.iou, threads=args.threads, backend=args.backend,
                   ncnn_param=args.ncnn_param, ncnn_bin=args.ncnn_bin, ncnn_names=args.ncnn_names)

  try:
    out_path, json_path = _default_paths(args.video, args.out, args.json)
    evaluate(args.video, config, args.every, args.max_frames, classes, out_path, json_path)
  except (OSError, RuntimeError, ValueError) as error:
    print(f"error: {error}", file=sys.stderr)
    return 1
  return 0


if __name__ == "__main__":
  sys.exit(main())
