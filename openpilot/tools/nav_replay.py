#!/usr/bin/env python3
"""Run the driving model over a recorded segment, directly, and summarize its output.

Why not the process-replay harness: replaying `modeld` through VisionIPC on this build hits
a tinygrad AMD MMU fault during input copies (the same artifact runs fine through
`ModelState`, see `openpilot/selfdrive/modeld/BIG_MODEL.md`), so this tool feeds frames to
`ModelState` directly. It is the offline "does the model produce sane output on my car's
footage" check before trusting it on the road.

  SUNNYPILOT_MODEL_DEVICE=AMD SUNNYPILOT_BIG_MODEL_ON_IGPU=1 DEV="KFD+AMD:LLVM" \\
    python -m openpilot.tools.nav_replay --segment ~/.comma/media/0/realdata/<seg> --frames 60

Navigation inputs are zeros here, which is exactly the state the model sees when navd/mapsd
have no route. Use `--pkl` to replay a specific artifact and compare two models on the same
frames.
"""
import argparse
import json
import os
import statistics
import subprocess
import sys
from pathlib import Path

import numpy as np

from openpilot.common.transformations.camera import DEVICE_CAMERAS
from openpilot.common.transformations.model import get_warp_matrix
from openpilot.system.camerad.cameras.nv12_info import get_nv12_info
from openpilot.tools.lib.logreader import LogReader


class SequentialVideoReader:
  """FrameReader-compatible nv12 reader for the fork's Matroska/H.264 camera files.

  `FrameReader` only indexes raw HEVC, while this build records `fcamera.hevc` as a
  Matroska file. Frames are read in order, which is all this tool needs."""

  def __init__(self, path: str):
    probe = json.loads(subprocess.check_output(
      ["ffprobe", "-v", "quiet", "-print_format", "json", "-select_streams", "v:0", "-show_streams", path], text=True))
    stream = probe["streams"][0]
    self.w, self.h = int(stream["width"]), int(stream["height"])
    self.pix_fmt = "nv12"
    self._size = self.w * self.h * 3 // 2
    self._proc = subprocess.Popen(["ffmpeg", "-v", "error", "-i", path, "-f", "rawvideo", "-pix_fmt", "nv12", "-"],
                                  stdout=subprocess.PIPE)
    self._next = 0

  def get(self, index: int):
    if index < self._next:
      raise RuntimeError(f"frame {index} was already passed (streaming reader is forward only)")
    while self._next <= index:
      data = self._proc.stdout.read(self._size)
      if not data or len(data) != self._size:
        raise EOFError(f"video ended before frame {index}")
      frame = np.frombuffer(data, dtype=np.uint8).copy()
      if self._next == index:
        return frame
      self._next += 1
    raise AssertionError("unreachable")

  def close(self):
    self._proc.kill()
    self._proc.wait()


def pack_nv12(flat: np.ndarray, w: int, h: int) -> np.ndarray:
  """Flat nv12 -> the padded buffer layout camerad hands to modeld."""
  stride, y_height, _, yuv_size = get_nv12_info(w, h)
  uv_offset = stride * y_height
  padded = np.zeros(((uv_offset // stride) + (h // 2), stride), dtype=np.uint8)
  padded[:h, :w] = flat[:h * w].reshape(h, w)
  padded[uv_offset // stride:uv_offset // stride + h // 2, :w] = flat[h * w:].reshape(h // 2, w)
  out = np.zeros((yuv_size,), dtype=np.uint8)
  out[:padded.size] = padded.flatten()
  return out


def transforms_from_log(logs) -> tuple[np.ndarray, np.ndarray]:
  """(main tfm, extra tfm) using this device's own intrinsics from the log."""
  device_state = next((m.deviceState for m in logs if m.which() == "deviceState"), None)
  camera_state = next((m.narrowRoadCameraState for m in logs if m.which() == "narrowRoadCameraState"), None)
  if device_state is None or camera_state is None:
    raise SystemExit("segment has no deviceState/narrowRoadCameraState")
  config = DEVICE_CAMERAS[(str(device_state.deviceType), str(camera_state.sensor))]
  euler = np.zeros(3, dtype=np.float32)
  calib = next((m.extrinsicsCalibration for m in logs if m.which() == "extrinsicsCalibration"), None)
  if calib is not None and len(calib.rpyCalib) == 3:
    euler = np.array(calib.rpyCalib, dtype=np.float32)
  main = get_warp_matrix(euler, config.narrow_road.intrinsics, False).astype(np.float32)
  extra = get_warp_matrix(euler, config.wide_road.intrinsics, True).astype(np.float32)
  return main, extra


def summarize(outs: list[dict]) -> dict:
  curv = [float(o["action"][0, 0]) for o in outs if "action" in o]
  accel = [float(o["action"][0, 1]) for o in outs if "action" in o]
  plan_ok = sum(bool(np.isfinite(o["plan"]).all()) for o in outs if "plan" in o)
  return {
    "frames": len(outs),
    "desired_curvature": {
      "mean_abs": statistics.fmean(abs(v) for v in curv) if curv else None,
      "p95_abs": float(np.percentile(np.abs(curv), 95)) if curv else None,
      "max_abs": max((abs(v) for v in curv), default=None),
    },
    "desired_acceleration": {
      "min": min(accel, default=None), "max": max(accel, default=None),
      "mean": statistics.fmean(accel) if accel else None,
    },
    "plan_finite_ratio": plan_ok / len(outs) if outs else None,
  }


def main(argv=None) -> int:
  parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
  parser.add_argument("--segment", required=True, help="local segment directory (rlog.zst + fcamera.hevc)")
  parser.add_argument("--frames", type=int, default=60, help="frames to run (default 60 = 3 s at 20 Hz)")
  parser.add_argument("--pkl", default=None, help="replay a specific artifact instead of the installed one")
  parser.add_argument("--out", default=None, help="optional JSON output path")
  args = parser.parse_args(argv)

  segment = os.path.expanduser(args.segment)
  rlog = os.path.join(segment, "rlog.zst")
  fcamera = os.path.join(segment, "fcamera.hevc")
  ecamera = os.path.join(segment, "ecamera.hevc")
  if not os.path.isfile(rlog) or not os.path.isfile(fcamera):
    raise SystemExit(f"missing rlog.zst or fcamera.hevc under {segment}")

  logs = list(LogReader(rlog))
  main_tfm, extra_tfm = transforms_from_log(logs)
  main_reader = SequentialVideoReader(fcamera)
  w, h = main_reader.w, main_reader.h
  if args.pkl:
    import openpilot.selfdrive.modeld.helpers as modeld_helpers
    modeld_helpers.modeld_pkl_path = lambda chestnut: Path(args.pkl)

  from openpilot.selfdrive.modeld.modeld import ModelState

  state = ModelState(w, h, False)
  readers = {"img": main_reader}
  if os.path.isfile(ecamera):
    readers["big_img"] = SequentialVideoReader(ecamera)
  transforms = {"img": main_tfm, "big_img": extra_tfm}

  outs = []
  try:
    for index in range(args.frames):
      bufs = {name: pack_nv12(reader.get(index), w, h) for name, reader in readers.items()}
      if "big_img" not in bufs:  # single-video segment: the model still needs both slots
        bufs["big_img"] = bufs["img"]
      inputs = {
        "desire_pulse": np.zeros(8, dtype=np.float32),
        "traffic_convention": np.zeros(2, dtype=np.float32),
        "action_t": np.array([0.1, 0.2], dtype=np.float32),
      }
      outs.append(state.run(bufs, transforms, inputs))
  finally:
    for reader in readers.values():
      reader.close()

  summary = summarize(outs)
  summary.update({"segment": segment, "artifact": str(state.pkl_path), "nav": bool(state.nav),
                  "frame_shape": [w, h]})
  print(json.dumps(summary, indent=2))
  if args.out:
    with open(args.out, "w") as f:
      json.dump(summary, f, indent=2)
  return 0 if outs else 1


if __name__ == "__main__":
  sys.exit(main())
