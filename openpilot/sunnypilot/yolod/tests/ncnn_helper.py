#!/usr/bin/env python3
"""Subprocess helper for the ncnn backend tests.

An ncnn Vulkan net is created in its own process on purpose: ncnn crashed when a
CPU net and a Vulkan net were mixed in one process during development, and running
it next to an ONNX Runtime session (which the test suite also uses) has not been
validated. The helper decodes the same frames as the parent test and dumps the
detections as JSON.
"""

import argparse
import json
import os
import sys

if __package__ in (None, ""):  # allow running this file directly
  sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")))

from openpilot.sunnypilot.yolod.detector import letterbox
from openpilot.sunnypilot.yolod.detector_ncnn import (
  DEFAULT_NCNN_BIN,
  DEFAULT_NCNN_NAMES,
  DEFAULT_NCNN_PARAM,
  NcnnDetector,
)
from openpilot.sunnypilot.yolod.video import find_video_path, iter_frames


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(description="Run the ncnn Vulkan detector and dump detections as JSON")
  parser.add_argument("video", help="video file or route directory")
  parser.add_argument("--out", required=True, help="JSON output path")
  parser.add_argument("--max-frames", type=int, default=4)
  parser.add_argument("--param", default=DEFAULT_NCNN_PARAM)
  parser.add_argument("--bin", default=DEFAULT_NCNN_BIN)
  parser.add_argument("--names", default=DEFAULT_NCNN_NAMES)
  parser.add_argument("--conf", type=float, default=0.25)
  parser.add_argument("--iou", type=float, default=0.45)
  parser.add_argument("--threads", type=int, default=1)
  args = parser.parse_args(argv)

  detector = NcnnDetector(args.param, args.bin, names_path=args.names, conf=args.conf, iou=args.iou,
                          threads=args.threads)
  detector.warmup()
  path = find_video_path(args.video)

  frames = []
  output_meta = None
  for frame_index, image in iter_frames(path, max_frames=args.max_frames):
    detections = detector.detect(image)
    if output_meta is None:
      padded, _ = letterbox(image, (detector.input_w, detector.input_h))
      raw = detector._run(padded)
      output_meta = {"shape": list(raw.shape), "dtype": str(raw.dtype)}
    frames.append({
      "index": frame_index,
      "inference_ms": detector.inference_ms,
      "detections": [{
        "class_id": detection.class_id,
        "label": detection.label,
        "score": detection.score,
        "box": list(detection.box),
      } for detection in detections],
    })

  payload = {
    "backend": detector.backend,
    "device": detector.device_name,
    "device_index": detector.device_index,
    "input": [detector.input_w, detector.input_h],
    "class_names": detector.class_names,
    "output": output_meta,
    "frames": frames,
  }
  with open(args.out, "w", encoding="utf-8") as file:
    json.dump(payload, file, indent=2)
  print(f"ncnn helper: {len(frames)} frames, backend={payload['backend']}, device={payload['device']}")
  return 0


if __name__ == "__main__":
  sys.exit(main())
