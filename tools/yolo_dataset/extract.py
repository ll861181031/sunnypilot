#!/usr/bin/env python3
"""Stage 1: extract evenly spaced frames from recorded road-camera segments.

Each route is a ~60 s ``fcamera.hevc``. Frames are sampled at ``--fps`` (default
0.5 Hz, i.e. one frame every 2 s) straight out of ffmpeg, scaled/letterboxed to
the dataset geometry, and written as high-quality JPEG. Alongside them we record
where every frame came from (route, timestamp inside the segment, wall-clock
hour, mean brightness) so the later stages can prioritise and split without
re-decoding anything.

Outputs (under ``--work``)::

    _frames/<route>/<nnnn>.jpg
    _meta/<route>.json     {"route", "hour", "daylight", "source_width", ...,
                            "frames": [{"file", "t", "brightness"}]}

The stage is idempotent: a route that already has a complete meta file and all of
its frames is skipped unless ``--force`` is given.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common

JPEG_QUALITY = 2  # ffmpeg -q:v, ~95/100: near-transparent for annotation/training


def probe_video(path: str) -> tuple[int, int, float]:
  """(width, height, duration_s) of the first video stream; 0s when unreadable."""
  cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,avg_frame_rate,nb_frames",
         "-of", "json", path]
  try:
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=True).stdout
  except (subprocess.SubprocessError, OSError):
    return 0, 0, 0.0
  try:
    import json
    stream = (json.loads(out).get("streams") or [{}])[0]
    width = int(stream.get("width") or 0)
    height = int(stream.get("height") or 0)
    num, _, den = str(stream.get("avg_frame_rate") or "0/0").partition("/")
    fps = float(num) / float(den or 1) if float(den or 1) else 0.0
    frames = int(stream.get("nb_frames") or 0)
  except (ValueError, TypeError, ZeroDivisionError):
    return 0, 0, 0.0
  return width, height, (frames / fps if fps > 0 and frames > 0 else 0.0)


def frames_dir(work: str, route: str) -> str:
  return os.path.join(work, "_frames", route)


def meta_path(work: str, route: str) -> str:
  return os.path.join(work, "_meta", f"{route}.json")


def extract_route(args_tuple: dict) -> dict:
  """Worker: extract one route. Returns a small result dict for the log/summary."""
  route_dir = args_tuple["route_dir"]
  work = args_tuple["work"]
  fps = args_tuple["fps"]
  size = tuple(args_tuple["size"])
  force = args_tuple["force"]
  route = common.route_id(route_dir)
  started = time.monotonic()

  video = common.camera_path(route_dir)
  if not video:
    return {"route": route, "status": "no_camera", "frames": 0, "seconds": 0.0}

  out_dir = frames_dir(work, route)
  meta_file = meta_path(work, route)
  if not force and os.path.isfile(meta_file):
    meta = common.read_json(meta_file, {})
    frames = meta.get("frames") or []
    if frames and all(os.path.isfile(os.path.join(out_dir, f["file"])) for f in frames):
      return {"route": route, "status": "cached", "frames": len(frames),
              "seconds": round(time.monotonic() - started, 2)}

  width, height, duration = probe_video(video)
  if width <= 0 or height <= 0:
    return {"route": route, "status": "unreadable", "frames": 0,
            "seconds": round(time.monotonic() - started, 2)}

  tmp_dir = out_dir + ".part"
  shutil.rmtree(tmp_dir, ignore_errors=True)
  os.makedirs(tmp_dir, exist_ok=True)
  cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", video,
         "-vf", common.ffmpeg_filter(fps, size),
         "-q:v", str(JPEG_QUALITY), "-vsync", "vfr",
         os.path.join(tmp_dir, "%04d.jpg")]
  try:
    subprocess.run(cmd, capture_output=True, check=True, timeout=args_tuple["timeout"])
  except (subprocess.SubprocessError, OSError) as error:
    shutil.rmtree(tmp_dir, ignore_errors=True)
    return {"route": route, "status": "ffmpeg_failed", "frames": 0,
            "seconds": round(time.monotonic() - started, 2),
            "error": str(getattr(error, "stderr", b"") or error)[:400]}

  files = sorted(f for f in os.listdir(tmp_dir) if f.endswith(".jpg"))
  if not files:
    shutil.rmtree(tmp_dir, ignore_errors=True)
    return {"route": route, "status": "empty", "frames": 0,
            "seconds": round(time.monotonic() - started, 2)}

  # Brightness is measured on the delivered JPEG, i.e. what the annotator sees.
  import cv2
  frames = []
  for name in files:
    image = cv2.imread(os.path.join(tmp_dir, name), cv2.IMREAD_GRAYSCALE)
    index = int(os.path.splitext(name)[0])
    frames.append({"file": name,
                   "t": round((index - 1) / fps, 3),
                   "brightness": round(float(image.mean()) if image is not None else 0.0, 2)})

  shutil.rmtree(out_dir, ignore_errors=True)
  os.replace(tmp_dir, out_dir)
  meta = common.new_route_meta(route_dir, source_width=width, source_height=height,
                               source_duration=round(duration, 2), fps=fps,
                               size=list(size), frames=frames)
  common.write_json(meta_file, meta)
  return {"route": route, "status": "ok", "frames": len(frames),
          "seconds": round(time.monotonic() - started, 2)}


def filter_by_hours(routes: list[str], mode: str) -> list[str]:
  """Keep only daylight (``day``) or non-daylight (``night``) routes; ``all`` is a no-op.

  Used to build the supplementary night batch without touching the sampling
  protocol of a full mixed batch.
  """
  if mode == "all":
    return routes
  want_day = mode == "day"
  return [route for route in routes if common.is_daylight(common.recorded_hour(route)) == want_day]


def select_routes(routes: list[str], count: int, day_frac: float, seed: int) -> list[str]:
  """Deterministically pick ``count`` routes with a day/night ratio near ``day_frac``.

  Used to bound a pilot run. The full run simply takes every route.
  """
  import random
  rng = random.Random(seed)
  day, night = [], []
  for route in routes:
    (day if common.is_daylight(common.recorded_hour(route)) else night).append(route)
  rng.shuffle(day)
  rng.shuffle(night)
  want_day = min(len(day), int(round(count * day_frac)))
  want_night = min(len(night), count - want_day)
  # Fill the shortfall of one bucket from the other so ``count`` is honoured.
  want_day = min(len(day), count - want_night)
  picked = day[:want_day] + night[:want_night]
  return sorted(picked)


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
  parser.add_argument("--data-root", default=common.DEFAULT_DATA_ROOT)
  parser.add_argument("--work", default=common.DEFAULT_WORK_ROOT)
  parser.add_argument("--fps", type=float, default=0.5, help="sample rate (0.5 = every 2 s)")
  parser.add_argument("--size", default="1344x760", help="WxH of the delivered frames")
  parser.add_argument("--workers", type=int, default=8)
  parser.add_argument("--routes", type=int, default=0, help="only N routes (pilot); 0 = all")
  parser.add_argument("--hours", choices=("all", "day", "night"), default="all",
                      help="restrict to daylight or night routes (hour from the camera mtime)")
  parser.add_argument("--route", action="append", default=[], help="explicit route dir/name")
  parser.add_argument("--day-frac", type=float, default=0.5, help="day share when --routes is used")
  parser.add_argument("--seed", type=int, default=1234)
  parser.add_argument("--timeout", type=int, default=600, help="per-route ffmpeg timeout (s)")
  parser.add_argument("--force", action="store_true", help="re-extract even if cached")
  parser.add_argument("--quiet", action="store_true")
  args = parser.parse_args(argv)

  width, height = (int(v) for v in args.size.lower().split("x"))
  size = (width, height)

  if args.route:
    routes = []
    for name in args.route:
      path = name if os.path.isdir(name) else os.path.join(args.data_root, name)
      if not os.path.isdir(path):
        parser.error(f"route not found: {name}")
      routes.append(path)
  else:
    routes = filter_by_hours(common.list_routes(args.data_root), args.hours)
    if args.routes > 0 and args.routes < len(routes):
      routes = select_routes(routes, args.routes, args.day_frac, args.seed)

  if not routes:
    print(f"no routes under {args.data_root}", file=sys.stderr)
    return 1

  os.makedirs(os.path.join(args.work, "_frames"), exist_ok=True)
  os.makedirs(os.path.join(args.work, "_meta"), exist_ok=True)

  started = time.monotonic()
  jobs = [{"route_dir": r, "work": args.work, "fps": args.fps, "size": size,
           "force": args.force, "timeout": args.timeout} for r in routes]
  status_count: dict[str, int] = {}
  total_frames = 0
  done = 0
  with ProcessPoolExecutor(max_workers=max(1, args.workers)) as pool:
    futures = {pool.submit(extract_route, job): job for job in jobs}
    for future in as_completed(futures):
      try:
        result = future.result()
      except Exception as error:  # keep the batch alive on a single bad route
        result = {"route": common.route_id(futures[future]["route_dir"]),
                  "status": "crash", "frames": 0, "error": f"{type(error).__name__}: {error}"}
      status_count[result["status"]] = status_count.get(result["status"], 0) + 1
      total_frames += result["frames"]
      done += 1
      if not args.quiet and (result["status"] != "ok" or done % 50 == 0):
        detail = result.get("error", "")
        head = f"[{done}/{len(jobs)}] {result['route']} {result['status']}"
        print(f"{head} frames={result['frames']} {result['seconds']}s {detail}".rstrip(), flush=True)

  elapsed = time.monotonic() - started
  summary = {"routes": len(jobs), "frames": total_frames, "seconds": round(elapsed, 1),
             "fps_per_worker": round(total_frames / elapsed, 1) if elapsed > 0 else 0,
             "status": status_count, "size": list(size), "sample_fps": args.fps,
             "work": os.path.abspath(args.work)}
  common.write_json(os.path.join(args.work, "_extract_summary.json"), summary)
  rate = f"{summary['fps_per_worker']} frames/s"
  print(f"extract: {total_frames} frames from {len(jobs)} routes in {elapsed:.1f}s ({rate}); status={status_count}")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
