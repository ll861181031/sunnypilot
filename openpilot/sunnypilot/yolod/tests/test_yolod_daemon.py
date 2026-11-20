"""Daemon loop test: Params gate, VisionIPC input and msgq publishing, all offline.

Uses an in-process VisionIpcServer named after a throw-away endpoint instead of the
real camerad, and fakes Params, so no part of the driving stack is started.
"""

import gc
import threading
import time
import uuid

import numpy as np
import pytest

pytest.importorskip("av")
pytest.importorskip("onnxruntime")

import av
from msgq.visionipc import VisionIpcServer

from openpilot.cereal import log
from openpilot.cereal.messaging import sub_sock
from openpilot.cereal.visionipc import VisionStreamType
from openpilot.sunnypilot.yolod import yolod

WIDTH, HEIGHT, STRIDE, UV_ALIGN = 1344, 760, 1408, 768
UV_OFFSET = STRIDE * UV_ALIGN


class _FakeParams:
  def __init__(self, enabled: bool):
    self.enabled = enabled

  def get_bool(self, key: str) -> bool:
    assert key == "YoloEnabled"
    return self.enabled


def _nv12_frame() -> np.ndarray:
  image = np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)
  image[..., 2] = 160
  frame = av.VideoFrame.from_numpy_buffer(image, format="bgr24").reformat(format="nv12")
  buffer = np.zeros(UV_OFFSET + STRIDE * (HEIGHT // 2), dtype=np.uint8)
  y_plane, uv_plane = frame.planes[0], frame.planes[1]
  y = np.ndarray((HEIGHT, WIDTH), dtype=np.uint8, buffer=y_plane, strides=(y_plane.line_size, 1))
  uv = np.ndarray((HEIGHT // 2, WIDTH // 2, 2), dtype=np.uint8, buffer=uv_plane,
                  strides=(uv_plane.line_size, 2, 1))
  buffer[:HEIGHT * STRIDE].reshape(HEIGHT, STRIDE)[:, :WIDTH] = y
  buffer[UV_OFFSET:UV_OFFSET + STRIDE * (HEIGHT // 2)].reshape(HEIGHT // 2, STRIDE)[:, :WIDTH] = \
    uv.reshape(HEIGHT // 2, WIDTH)
  return buffer


def test_daemon_exits_immediately_when_disabled(monkeypatch):
  monkeypatch.setattr(yolod, "Params", lambda: _FakeParams(False))
  stop = threading.Event()
  started = time.monotonic()
  yolod.run_daemon(yolod.YoloConfig.from_env({}), stop)
  assert time.monotonic() - started < 5.0


def test_daemon_publishes_yolo_objects_from_visionipc(monkeypatch):
  # No other publisher may be alive for this endpoint in the same process.
  gc.collect()
  name = "yolod_test_" + uuid.uuid4().hex
  stream = VisionStreamType.VISION_STREAM_NARROW_ROAD
  server = VisionIpcServer(name)
  server.create_buffers_with_sizes(stream, 2, WIDTH, HEIGHT, UV_OFFSET + STRIDE * (HEIGHT // 2), STRIDE, UV_OFFSET)
  server.start_listener()

  monkeypatch.setattr(yolod, "CAMERA", name)
  monkeypatch.setattr(yolod, "Params", lambda: _FakeParams(True))
  monkeypatch.setattr(yolod, "config_realtime_process", lambda *args, **kwargs: None)

  stop = threading.Event()
  frame = _nv12_frame()
  sender_stop = threading.Event()

  def sender() -> None:
    index = 0
    while not sender_stop.is_set():
      server.send(stream, frame, index, index * 50_000_000, index * 50_000_000 + 40_000_000)
      index += 1
      time.sleep(0.02)

  sender_thread = threading.Thread(target=sender, daemon=True)
  sender_thread.start()

  subscriber = sub_sock("yoloObjects", conflate=True, timeout=500)
  daemon_thread = threading.Thread(target=yolod.run_daemon, args=(yolod.YoloConfig.from_env({}), stop), daemon=True)
  daemon_thread.start()
  try:
    raw = None
    deadline = time.monotonic() + 10.0
    while raw is None and time.monotonic() < deadline:
      raw = subscriber.receive()
    assert raw is not None, "yolod never published yoloObjects"
    with log.Event.from_bytes(raw) as event:
      assert event.valid is True
      payload = event.yoloObjects
      assert payload.frameWidth == WIDTH
      assert payload.frameHeight == HEIGHT
      assert len(payload.classNames) == 80
      assert payload.classNames[0] == "person"
      assert payload.inferenceMs > 0.0
      assert len(payload.objects) <= yolod.MAX_OBJECTS
      for obj in payload.objects:
        assert 0.0 <= obj.x <= 1.0 and 0.0 <= obj.y <= 1.0
        assert 0.0 <= obj.w <= 1.0 and 0.0 <= obj.h <= 1.0
        # classNames is the full model list, and the default filter only lets
        # road-relevant classes through.
        assert payload.classNames[obj.classId] in yolod.DEFAULT_CLASSES
  finally:
    stop.set()
    sender_stop.set()
    sender_thread.join(timeout=5)
    daemon_thread.join(timeout=5)
  assert not daemon_thread.is_alive(), "yolod did not stop on the stop event"
