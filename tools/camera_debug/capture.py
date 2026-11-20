"""Capture engine for the camera debug tool.

This is deliberately *not* the driving path (``system/camerad/webcam/camera.py``)
but it applies the same knobs in the same order - MJPG fourcc, size, capture fps,
``CAP_PROP_BUFFERSIZE``, then ``CAP_PROP_CONVERT_RGB=0`` - so that the numbers it
reports describe what the real stack would get, not an idealised read loop.

Two measurement rules this file follows:

* The read loop does nothing but ``cap.read()``. Preview encoding happens on the
  HTTP thread from the frame the loop last stored, so the measured read rate is
  not polluted by JPEG encoding.
* With MJPG the compressed frame is already a JPEG, so the preview is streamed
  byte-for-byte with no decode and no re-encode at all.
"""

import os
import random
import threading
import time
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime

import cv2 as cv
import numpy as np

from tools.camera_debug.devices import holders, usb_topology

# Live rate window. 1 s matches what the UI polls, short enough to see a stall.
WINDOW_S = 1.0
# Samples kept for percentiles (~60 s at 20 fps).
HISTORY = 1200
# Sparkline sampling period and length (60 s of history).
SERIES_PERIOD_S = 0.25
SERIES_LEN = 240
# Mirror camera.py: a UVC camera sharing a USB 2.0 bus can drop a frame, so a
# bounded grace period separates a hiccup from a camera that is really gone.
MAX_READ_MISSES = 30
READ_RETRY_INTERVAL = 0.05
# A window shorter than this is too small to divide by; report None instead of
# a wildly inflated rate for the first fraction of a second.
MIN_WINDOW_S = 0.2

FOURCC_CHOICES = ("MJPG", "YUYV")


def fourcc_int(name: str) -> int:
  return cv.VideoWriter_fourcc(*name) if len(name) == 4 else 0


def fourcc_to_str(value: int) -> str:
  """OpenCV packs the fourcc little-endian; unpack it for display."""
  if not value:
    return ""
  chars = "".join(chr((int(value) >> (8 * i)) & 0xFF) for i in range(4))
  return "".join(c for c in chars if c.isprintable()).strip()


def percentile(values, p):
  if not values:
    return None
  return round(float(np.percentile(np.fromiter(values, dtype=np.float64), p)), 3)


class RateStats:
  """Sliding-window read-rate and latency statistics for one camera.

  Everything is measured here; nothing is inferred from the requested settings.
  A UVC camera silently falls back to a different mode, and that difference is
  exactly what this tool exists to expose.
  """

  def __init__(self, window_s=WINDOW_S, history=HISTORY):
    self.window_s = window_s
    self.lock = threading.Lock()
    self.started_ns = time.monotonic_ns()
    self.frames = 0
    self.failures = 0
    self.bytes_total = 0
    self._read_times = deque()
    self._byte_samples = deque()
    self._intervals = deque(maxlen=history)
    self._read_ms = deque(maxlen=history)
    self._decode_ms = deque(maxlen=history)
    self._last_frame_ns = None
    self.series = deque(maxlen=SERIES_LEN)
    self._last_series_ns = 0
    self.cpu_pct = None
    self._last_cpu_check_ns = 0
    self._cpu_ns_at_check = 0

  def on_frame(self, now_ns, read_ms=None, decode_ms=None, nbytes=0):
    with self.lock:
      if self._last_frame_ns is not None and now_ns > self._last_frame_ns:
        self._intervals.append((now_ns - self._last_frame_ns) / 1e6)
      self._last_frame_ns = now_ns
      self._read_times.append(now_ns)
      self._byte_samples.append((now_ns, nbytes))
      if read_ms is not None:
        self._read_ms.append(read_ms)
      if decode_ms is not None:
        self._decode_ms.append(decode_ms)
      self.frames += 1
      self.bytes_total += nbytes
      self._sample_cpu(now_ns)
      self._maybe_sample_series(now_ns)

  def on_failure(self):
    with self.lock:
      self.failures += 1

  def _sample_cpu(self, now_ns):
    """Per-thread CPU usage, sampled every second. time.thread_time() counts
    only this capture thread, so it separates capture cost from preview cost."""
    if now_ns - self._last_cpu_check_ns < 1_000_000_000:
      return
    cpu_ns = time.thread_time_ns()
    if self._last_cpu_check_ns:
      wall = now_ns - self._last_cpu_check_ns
      if wall > 0:
        self.cpu_pct = round(100.0 * (cpu_ns - self._cpu_ns_at_check) / wall, 1)
    self._cpu_ns_at_check = cpu_ns
    self._last_cpu_check_ns = now_ns

  def _maybe_sample_series(self, now_ns):
    if now_ns - self._last_series_ns < SERIES_PERIOD_S * 1e9:
      return
    self._last_series_ns = now_ns
    fps = self._window_fps_locked(now_ns)
    self.series.append(None if fps is None else round(fps, 2))

  def _prune_locked(self, now_ns):
    cutoff = now_ns - int(self.window_s * 1e9)
    while self._read_times and self._read_times[0] < cutoff:
      self._read_times.popleft()
    while self._byte_samples and self._byte_samples[0][0] < cutoff:
      self._byte_samples.popleft()

  def _window_fps_locked(self, now_ns):
    span_s = min((now_ns - self.started_ns) / 1e9, self.window_s)
    if span_s < MIN_WINDOW_S:
      return None  # too early to divide by
    if len(self._read_times) < 2:
      # An empty window is a real reading: the camera is not delivering frames.
      return 0.0
    return round(len(self._read_times) / span_s, 2)

  def snapshot(self, now_ns=None):
    now_ns = now_ns or time.monotonic_ns()
    with self.lock:
      self._prune_locked(now_ns)
      elapsed = (now_ns - self.started_ns) / 1e9
      window_bytes = sum(n for _, n in self._byte_samples)
      span_s = min(elapsed, self.window_s)
      mbps = round(window_bytes / span_s / 1e6, 3) if span_s >= MIN_WINDOW_S else None
      intervals = list(self._intervals)
      return {
        "frames": self.frames,
        "failures": self.failures,
        "elapsed_s": round(elapsed, 2),
        "fps_window": self._window_fps_locked(now_ns),
        "fps_avg": round(self.frames / elapsed, 2) if elapsed > 0.5 else None,
        "dt_p50": percentile(intervals, 50),
        "dt_p90": percentile(intervals, 90),
        "dt_p99": percentile(intervals, 99),
        "dt_max": round(max(intervals), 3) if intervals else None,
        "jitter": round(float(np.std(intervals)), 3) if len(intervals) > 1 else None,
        "read_ms_p50": percentile(self._read_ms, 50),
        "read_ms_p99": percentile(self._read_ms, 99),
        "decode_ms_p50": percentile(self._decode_ms, 50),
        "decode_ms_p99": percentile(self._decode_ms, 99),
        "kb_avg": round(self.bytes_total / self.frames / 1024, 1) if self.frames else None,
        "mbps_window": mbps,
        "mb_total": round(self.bytes_total / 1e6, 2),
        "cpu_pct": self.cpu_pct,
        "series": list(self.series),
      }


@dataclass
class CameraSpec:
  device: str
  label: str = ""
  width: int = 1280
  height: int = 720
  fps: float = 30.0
  fourcc: str = "MJPG"
  # 2 rather than 1: with a single V4L2 buffer the driver cannot queue the next
  # frame while the app holds the current one, and the camera then delivers about
  # half the requested rate (measured 2026-10-16: 1920x1080 MJPG gave 30 fps at
  # buffersize 2 and 15 fps at 1). Callers can still ask for 1 explicitly.
  buffersize: int = 2
  decode: bool = False
  preview: bool = True
  fake: bool = False

  @staticmethod
  def from_dict(d: dict, slot: int) -> "CameraSpec":
    return CameraSpec(
      device=str(d.get("device", "")).strip(),
      label=str(d.get("label") or f"相机{slot + 1}"),
      width=int(d.get("width") or 1280),
      height=int(d.get("height") or 720),
      fps=float(d.get("fps") or 30),
      fourcc=str(d.get("fourcc") or "MJPG").upper(),
      buffersize=int(d.get("buffersize") or 2),
      decode=bool(d.get("decode")),
      preview=bool(d.get("preview", True)),
      fake=bool(d.get("fake")),
    )


class FakeCapture:
  """Synthetic camera used by ``--fake``.

  It implements the sliver of the ``cv.VideoCapture`` interface the engine
  touches, so the whole tool - API, rates, preview, comparison table - can be
  exercised and tested without occupying the real cameras (which the driving
  stack may be using).
  """

  def __init__(self, width, height, fps, fourcc="MJPG"):
    self.width, self.height = int(width), int(height)
    self.fps = max(1.0, float(fps))
    self.fourcc_name = fourcc
    self.convert_rgb = 1
    self.buffersize = 1
    self.opened = True
    self._next_ns = time.monotonic_ns()
    self._n = 0

  def isOpened(self):
    return self.opened

  def set(self, prop, value):
    if prop == cv.CAP_PROP_FOURCC:
      pass
    elif prop == cv.CAP_PROP_FRAME_WIDTH:
      self.width = int(value)
    elif prop == cv.CAP_PROP_FRAME_HEIGHT:
      self.height = int(value)
    elif prop == cv.CAP_PROP_FPS:
      self.fps = max(1.0, float(value))
    elif prop == cv.CAP_PROP_BUFFERSIZE:
      self.buffersize = int(value)
    elif prop == cv.CAP_PROP_CONVERT_RGB:
      self.convert_rgb = int(value)
    return True

  def get(self, prop):
    if prop == cv.CAP_PROP_FOURCC:
      return fourcc_int(self.fourcc_name)
    if prop == cv.CAP_PROP_FRAME_WIDTH:
      return float(self.width)
    if prop == cv.CAP_PROP_FRAME_HEIGHT:
      return float(self.height)
    if prop == cv.CAP_PROP_FPS:
      return float(self.fps)
    if prop == cv.CAP_PROP_CONVERT_RGB:
      return float(self.convert_rgb)
    return 0.0

  def read(self):
    # Pace to the requested rate, with a little jitter so the interval
    # percentiles have something real to describe.
    self._next_ns += int(1e9 / self.fps)
    delay = (self._next_ns - time.monotonic_ns()) / 1e9
    if delay > 0:
      time.sleep(min(delay, 0.5) * random.uniform(0.9, 1.1))
    frame = np.zeros((self.height, self.width, 3), dtype=np.uint8)
    frame[:, :, 0] = (self._n * 3) % 256
    frame[:, :, 1] = (self._n * 5) % 256
    cv.putText(frame, f"FAKE {self.width}x{self.height} #{self._n}",
               (30, max(40, self.height // 8)), cv.FONT_HERSHEY_SIMPLEX,
               max(0.6, self.width / 1400), (255, 255, 255), 2)
    self._n += 1
    if self.fourcc_name == "MJPG" and not self.convert_rgb:
      ok, buf = cv.imencode(".jpg", frame, [int(cv.IMWRITE_JPEG_QUALITY), 70])
      return ok, buf.reshape(-1, 1)
    return True, frame

  def release(self):
    self.opened = False


class CameraStream(threading.Thread):
  """One capture thread: open, configure, read, measure, publish latest frame."""

  def __init__(self, spec: CameraSpec, slot: int):
    super().__init__(name=f"cam{slot}", daemon=True)
    self.spec = spec
    self.slot = slot
    self.stats = RateStats()
    self.state = "idle"
    self.error = ""
    self.negotiated = {}
    self._passthrough = False
    self._decoder = None
    self.lock = threading.Lock()
    self.preview_cond = threading.Condition()
    self._preview = None       # ("jpeg", bytes) or ("bgr", ndarray)
    self._preview_id = 0
    # Read once: the card shows which USB bus the camera is on, so a "second
    # stream never starts" failure can be recognised as a shared-bus problem.
    self.usb = usb_topology(spec.device)
    # Named _stop_event: threading.Thread already owns _stop(), and a
    # same-named attribute breaks join() with "'Event' object is not callable".
    self._stop_event = threading.Event()
    self._cap = None

  # -- lifecycle ----------------------------------------------------------
  def stop(self):
    self._stop_event.set()

  def run(self):
    self.state = "starting"
    try:
      self._open()
    except Exception as exc:
      self.error = str(exc)
      self.state = "failed"
      return
    self.state = "running"
    misses = 0
    try:
      while not self._stop_event.is_set():
        t0 = time.monotonic_ns()
        ok, frame = self._cap.read()
        t1 = time.monotonic_ns()
        if not ok or frame is None:
          misses += 1
          self.stats.on_failure()
          if misses > MAX_READ_MISSES:
            self.error = "相机停止供帧（连续读取失败超过 1.5 秒）；两路同时采集时先查 USB 带宽：journalctl -k | grep -i bandwidth"
            self.state = "failed"
            break
          time.sleep(READ_RETRY_INTERVAL)
          continue
        misses = 0
        decode_ms = self._decode(frame) if self.spec.decode else None
        self._publish(frame)
        self.stats.on_frame(t1, read_ms=(t1 - t0) / 1e6, decode_ms=decode_ms,
                            nbytes=int(getattr(frame, "size", 0)))
    except Exception as exc:
      self.error = f"{type(exc).__name__}: {exc}"
      self.state = "failed"
    finally:
      if self._cap is not None:
        try:
          self._cap.release()
        except Exception:
          pass
      if self.state == "running":
        self.state = "stopped"

  def _open(self):
    spec = self.spec
    if spec.fake:
      cap = FakeCapture(spec.width, spec.height, spec.fps, spec.fourcc)
    else:
      cap = cv.VideoCapture(spec.device)
      if not cap.isOpened():
        cap.release()
        pids = holders(spec.device)
        if pids:
          raise RuntimeError(f"无法打开 {spec.device}（被 PID {', '.join(map(str, pids))} 占用）")
        if not os.path.exists(spec.device):
          # Seen on this machine: the camera dropped off the USB bus and its
          # /dev node disappeared, so every open fails with ENOENT.
          raise RuntimeError(f"无法打开 {spec.device}（设备节点不存在：相机可能已从 USB 总线掉线，需重新插拔）")
        raise RuntimeError(f"无法打开 {spec.device}")
    self._cap = cap

    # Same order as the driving path: format first, then the size and rates.
    cap.set(cv.CAP_PROP_FOURCC, fourcc_int(spec.fourcc))
    cap.set(cv.CAP_PROP_FRAME_WIDTH, spec.width)
    cap.set(cv.CAP_PROP_FRAME_HEIGHT, spec.height)
    cap.set(cv.CAP_PROP_FPS, spec.fps)
    cap.set(cv.CAP_PROP_BUFFERSIZE, spec.buffersize)

    # Keep MJPEG compressed: the frame the driver hands us is already a JPEG,
    # which is both what the real pipeline relies on and free to preview. The
    # PyAV decoder mirrors camera.py and is only built when decode cost is
    # actually being measured.
    self._passthrough = False
    if cap.get(cv.CAP_PROP_FOURCC) == fourcc_int("MJPG") and cap.set(cv.CAP_PROP_CONVERT_RGB, 0):
      self._passthrough = True
      if spec.decode:
        import av
        self._decoder = av.CodecContext.create("mjpeg", "r")
        self._decoder.thread_count = 1

    self.negotiated = {
      "width": int(cap.get(cv.CAP_PROP_FRAME_WIDTH)),
      "height": int(cap.get(cv.CAP_PROP_FRAME_HEIGHT)),
      "fps": round(float(cap.get(cv.CAP_PROP_FPS)), 2),
      "fourcc": fourcc_to_str(int(cap.get(cv.CAP_PROP_FOURCC))),
    }
    if self.negotiated["width"] <= 0 or self.negotiated["height"] <= 0:
      raise RuntimeError(f"{spec.device} 报告了无效分辨率 {self.negotiated}")

  def _decode(self, frame):
    """Decode the MJPEG frame with PyAV, the way the driving path does."""
    if self._decoder is None:
      return None
    import av
    t0 = time.monotonic_ns()
    self._decoder.decode(av.Packet(np.ascontiguousarray(frame)))
    return (time.monotonic_ns() - t0) / 1e6

  def _publish(self, frame):
    if not self.spec.preview:
      return
    if self._passthrough and getattr(frame, "ndim", 1) == 2:
      item = ("jpeg", frame.tobytes())
    else:
      item = ("bgr", frame)
    with self.preview_cond:
      self._preview = item
      self._preview_id += 1
      self.preview_cond.notify_all()

  # -- preview for the HTTP thread ---------------------------------------
  def encode_preview(self):
    """Return (frame_id, jpeg_bytes) for the newest frame, or (id, None)."""
    with self.preview_cond:
      item = self._preview
      frame_id = self._preview_id
    if item is None:
      return frame_id, None
    kind, payload = item
    if kind == "jpeg":
      return frame_id, payload
    # BGR fallback (YUYV capture): encode here, on the HTTP thread, so the read
    # loop's timings stay clean.
    ok, buf = cv.imencode(".jpg", payload, [int(cv.IMWRITE_JPEG_QUALITY), 70])
    return frame_id, (buf.tobytes() if ok else None)

  def wait_preview(self, last_id, timeout=1.0):
    with self.preview_cond:
      if self._preview_id == last_id:
        self.preview_cond.wait(timeout)
      return self._preview_id

  def snapshot(self):
    return {
      "slot": self.slot,
      "label": self.spec.label,
      "device": self.spec.device,
      "state": self.state,
      "error": self.error,
      "requested": {"width": self.spec.width, "height": self.spec.height,
                    "fps": self.spec.fps, "fourcc": self.spec.fourcc,
                    "buffersize": self.spec.buffersize,
                    "decode": self.spec.decode, "preview": self.spec.preview},
      "negotiated": self.negotiated,
      "usb": self.usb,
      "passthrough": bool(self._passthrough),
      "metrics": self.stats.snapshot(),
    }


@dataclass
class SessionSummary:
  started_at: float
  duration_s: float
  cameras: list = field(default_factory=list)
  combined: dict = field(default_factory=dict)

  def as_dict(self):
    return asdict(self)


class SessionLog:
  """Archived runs, so "one camera alone" can be compared with "both at once"."""

  def __init__(self, maxlen=50):
    self.sessions = deque(maxlen=maxlen)

  def add(self, summary: SessionSummary):
    self.sessions.append(summary)

  def clear(self):
    self.sessions.clear()

  def all(self):
    return [s.as_dict() for s in self.sessions]

  @staticmethod
  def _key(cam):
    neg = cam.get("negotiated") or {}
    return (cam.get("device"), neg.get("width"), neg.get("height"),
            (neg.get("fourcc") or cam.get("requested", {}).get("fourcc") or ""))

  def comparisons(self):
    """Per device+mode: the last run alone against the last run alongside others.

    This is the question the tool exists to answer on this machine: the two
    cameras share one USB 2.0 bus, so the rate of each depends on whether the
    other one is streaming.
    """
    alone, together = {}, {}
    for sess in self.sessions:
      cams = sess.cameras
      for cam in cams:
        key = self._key(cam)
        if len(cams) == 1:
          alone[key] = (sess, cam)
        else:
          together[key] = (sess, cam)
    out = []
    for key in sorted(set(alone) | set(together), key=lambda k: str(k)):
      a = alone.get(key)
      t = together.get(key)
      row = {
        "device": key[0],
        "width": key[1],
        "height": key[2],
        "fourcc": key[3],
        "label": (t or a)[1].get("label"),
        "alone": None,
        "together": None,
        "delta_pct": None,
      }
      if a is not None:
        row["alone"] = {k: a[1]["metrics"].get(k) for k in ("fps_avg", "fps_window", "mbps_window")}
        row["alone"]["at"] = a[0].started_at
      if t is not None:
        row["together"] = {k: t[1]["metrics"].get(k) for k in ("fps_avg", "fps_window", "mbps_window")}
        row["together"]["at"] = t[0].started_at
        row["together"]["n_cameras"] = len(t[0].cameras)
      if a is not None and t is not None and a[1]["metrics"].get("fps_avg") and t[1]["metrics"].get("fps_avg"):
        base = a[1]["metrics"]["fps_avg"]
        row["delta_pct"] = round(100.0 * (t[1]["metrics"]["fps_avg"] - base) / base, 1)
      out.append(row)
    return out


class CaptureSession:
  """A run of 1..2 camera streams plus the aggregate numbers across them."""

  def __init__(self, specs: list, log: SessionLog):
    if not 1 <= len(specs) <= 2:
      raise ValueError("只支持同时采集 1 或 2 个摄像头")
    devices = [s.device for s in specs]
    if len(set(devices)) != len(devices):
      raise ValueError("不能对同一个设备开两路采集")
    self.specs = specs
    self.log = log
    self.streams = [CameraStream(s, i) for i, s in enumerate(specs)]
    # Wall-clock label for the report ("this run happened at 18:20"), while every
    # duration in this file is measured with the monotonic clock.
    self.started_at = datetime.now().timestamp()
    self.started_ns = time.monotonic_ns()
    self._archived = False

  def start(self):
    for s in self.streams:
      s.start()

  def stop(self):
    for s in self.streams:
      s.stop()
    for s in self.streams:
      s.join(timeout=3)
    if not self._archived:
      self._archived = True
      self.log.add(self.summary())

  def summary(self) -> SessionSummary:
    now_ns = time.monotonic_ns()
    snaps = self._snapshots()
    cameras = []
    for snap in snaps:
      cameras.append({
        "label": snap["label"],
        "device": snap["device"],
        "requested": snap["requested"],
        "negotiated": snap["negotiated"],
        "metrics": {k: v for k, v in snap["metrics"].items() if k != "series"},
        "error": snap["error"],
      })
    return SessionSummary(
      started_at=self.started_at,
      duration_s=round((now_ns - self.started_ns) / 1e9, 2),
      cameras=cameras,
      combined=self._combined(snaps),
    )

  def _snapshots(self):
    """One snapshot per stream, taken once per call.

    The combined numbers are the sum of these exact per-camera numbers: two
    separate snapshots a few ms apart would disagree by a frame, and a tool
    whose whole purpose is measurement cannot show numbers that do not add up.
    """
    return [s.snapshot() for s in self.streams]

  def _combined(self, snaps):
    def total(key):
      vals = [s["metrics"].get(key) for s in snaps]
      vals = [v for v in vals if v is not None]
      return round(sum(vals), 2) if vals else None
    return {
      "n_cameras": len(snaps),
      "devices": [s["device"] for s in snaps],
      "read_fps_window": total("fps_window"),
      "read_fps_avg": total("fps_avg"),
      "mbps_window": total("mbps_window"),
      "mb_total": total("mb_total"),
      "decode_ms_p50_max": max([s["metrics"].get("decode_ms_p50") or 0 for s in snaps], default=0) or None,
      "failures": sum(s["metrics"].get("failures") or 0 for s in snaps),
    }

  def snapshot(self):
    now_ns = time.monotonic_ns()
    snaps = self._snapshots()
    return {
      "running": any(s.is_alive() for s in self.streams),
      "elapsed_s": round((now_ns - self.started_ns) / 1e9, 2),
      "cameras": snaps,
      "combined": self._combined(snaps),
    }
