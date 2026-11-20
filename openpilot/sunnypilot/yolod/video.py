"""Video frame sources for offline YOLO evaluation and ``yolod --video``.

Recorded route segments store the road camera as ``fcamera.hevc`` (an HEVC or
H.264 bitstream, no container). PyAV is the primary decoder. When it cannot open
or decode the file - a missing build feature, an unusual codec, a corrupt file -
the reader falls back to an ``ffmpeg`` raw-video pipe, which decodes anything
this machine's ffmpeg does.
"""

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from collections.abc import Iterator

import numpy as np

ROUTE_VIDEO_CANDIDATES = ("fcamera.hevc", "fcamera.mp4", "video.hevc", "video.mp4")
DEFAULT_FPS = 20.0


class VideoDecodeError(RuntimeError):
  pass


@dataclass(frozen=True)
class VideoInfo:
  path: str
  width: int
  height: int
  fps: float
  frame_count: int | None


def find_video_path(path: str) -> str:
  """Resolve a video file, or the road-camera file inside a route directory."""
  if os.path.isdir(path):
    for name in ROUTE_VIDEO_CANDIDATES:
      candidate = os.path.join(path, name)
      if os.path.isfile(candidate):
        return candidate
    raise FileNotFoundError(f"no road camera video found in {path}, expected one of {ROUTE_VIDEO_CANDIDATES}")
  if not os.path.isfile(path):
    raise FileNotFoundError(f"video not found: {path}")
  return path


def _parse_rate(rate) -> float:
  if rate is None:
    return DEFAULT_FPS
  try:
    value = float(rate)
  except (TypeError, ValueError):
    return DEFAULT_FPS
  return value if value > 0 else DEFAULT_FPS


def _probe_with_ffprobe(path: str) -> VideoInfo | None:
  ffprobe = shutil.which("ffprobe")
  if ffprobe is None:
    return None
  command = [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height,avg_frame_rate,nb_frames", "-of", "json", path]
  try:
    result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=30)
  except (OSError, subprocess.TimeoutExpired):
    return None
  if result.returncode != 0:
    return None
  try:
    streams = json.loads(result.stdout).get("streams") or []
  except json.JSONDecodeError:
    return None
  if not streams:
    return None
  stream = streams[0]
  width, height = int(stream.get("width", 0)), int(stream.get("height", 0))
  if width <= 0 or height <= 0:
    return None
  fps = DEFAULT_FPS
  numerator, _, denominator = str(stream.get("avg_frame_rate", "")).partition("/")
  try:
    fps = float(numerator) / float(denominator) if denominator and float(denominator) else float(numerator)
  except (TypeError, ValueError, ZeroDivisionError):
    pass
  frame_count = None
  try:
    frame_count = int(stream["nb_frames"]) if stream.get("nb_frames") not in (None, "N/A") else None
  except (TypeError, ValueError):
    frame_count = None
  return VideoInfo(path=path, width=width, height=height, fps=_parse_rate(fps), frame_count=frame_count)


def probe_video(path: str) -> VideoInfo:
  """Read the video geometry/fps with PyAV, falling back to ffprobe."""
  try:
    import av
    with av.open(path) as container:
      if not container.streams.video:
        raise VideoDecodeError(f"no video stream in {path}")
      stream = container.streams.video[0]
      return VideoInfo(path=path,
                       width=int(stream.width),
                       height=int(stream.height),
                       fps=_parse_rate(stream.average_rate or stream.guessed_rate),
                       frame_count=int(stream.frames) if stream.frames else None)
  except VideoDecodeError:
    raise
  except Exception as error:
    info = _probe_with_ffprobe(path)
    if info is None:
      raise VideoDecodeError(f"cannot read video geometry of {path}: {error}") from error
    return info


def _iter_av(path: str, every: int, max_frames: int | None) -> Iterator[tuple[int, np.ndarray]]:
  import av
  container = av.open(path)
  try:
    if not container.streams.video:
      raise VideoDecodeError(f"no video stream in {path}")
    stream = container.streams.video[0]
    emitted = 0
    for index, frame in enumerate(container.decode(stream)):
      if index % every:
        continue
      yield index, frame.to_ndarray(format="bgr24")
      emitted += 1
      if max_frames is not None and emitted >= max_frames:
        break
  finally:
    container.close()


def _iter_ffmpeg(path: str, every: int, max_frames: int | None) -> Iterator[tuple[int, np.ndarray]]:
  ffmpeg = shutil.which("ffmpeg")
  if ffmpeg is None:
    raise VideoDecodeError("ffmpeg is not installed, cannot use the fallback decoder")
  info = _probe_with_ffprobe(path)
  if info is None:
    raise VideoDecodeError(f"ffprobe cannot read {path}, cannot use the fallback decoder")
  frame_size = info.width * info.height * 3
  command = [ffmpeg, "-v", "error", "-nostdin", "-i", path, "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"]
  process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
  emitted = 0
  try:
    index = 0
    while True:
      raw = process.stdout.read(frame_size)
      if raw is None or len(raw) < frame_size:
        break
      if index % every == 0:
        yield index, np.frombuffer(raw, dtype=np.uint8).reshape(info.height, info.width, 3).copy()
        emitted += 1
        if max_frames is not None and emitted >= max_frames:
          break
      index += 1
  finally:
    process.stdout.close()
    if process.poll() is None:
      process.kill()
    process.wait()
    stderr = process.stderr.read().decode("utf-8", "replace") if process.stderr is not None else ""
    if process.stderr is not None:
      process.stderr.close()
  if emitted == 0 and stderr.strip():
    raise VideoDecodeError(f"ffmpeg decoded no frames from {path}: {stderr.strip()[:500]}")


def iter_frames(path: str, every: int = 1, max_frames: int | None = None,
                backend: str = "auto") -> Iterator[tuple[int, np.ndarray]]:
  """Yield ``(source_frame_index, bgr)`` pairs from a video file.

  ``backend`` is ``auto`` (PyAV, then ffmpeg), ``av`` or ``ffmpeg``. ``every``
  keeps one frame out of N and ``max_frames`` caps the number of yielded frames.
  """
  if every < 1:
    raise ValueError(f"every must be >= 1, got {every}")
  if max_frames is not None and max_frames < 0:
    raise ValueError(f"max_frames must be non-negative, got {max_frames}")
  if backend not in ("auto", "av", "ffmpeg"):
    raise ValueError(f"unknown backend {backend!r}, expected auto/av/ffmpeg")

  if backend == "ffmpeg":
    yield from _iter_ffmpeg(path, every, max_frames)
    return

  generator = _iter_av(path, every, max_frames)
  try:
    first = next(generator)
  except StopIteration:
    return
  except Exception as error:
    generator.close()
    if backend == "av":
      raise VideoDecodeError(f"PyAV cannot decode {path}: {error}") from error
    yield from _iter_ffmpeg(path, every, max_frames)
    return
  try:
    yield first
    yield from generator
  finally:
    generator.close()
