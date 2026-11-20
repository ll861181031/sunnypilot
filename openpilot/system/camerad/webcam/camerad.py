#!/usr/bin/env python3
import threading
import os
import platform
import time
import math
import queue
import cv2 as cv
from contextlib import closing
from collections import namedtuple

from openpilot.cereal.visionipc import VisionStreamType
from msgq.visionipc import VisionIpcServer
from openpilot.cereal import messaging
from openpilot.common.camera_config import driver_camera_enabled, wide_camera_available
from openpilot.common.swaglog import cloudlog

from openpilot.system.camerad.webcam.camera import Camera

NARROW_ROAD_CAM = os.getenv("NARROW_ROAD_CAM", os.getenv("ROAD_CAM", "0"))
# Single vs dual is decided by what is plugged in, not by a mode setting: the
# launcher exports WIDE_CAM only when the configured device exists, and this
# guard covers the other launch paths (and the swapped-device mistake where the
# wide id points at the narrow camera). A wide camera that is configured but
# missing therefore costs only the second model input, never the stack.
WIDE_CAM = os.getenv("WIDE_CAM") if wide_camera_available() else None
if os.getenv("WIDE_CAM") and WIDE_CAM is None:
  cloudlog.warning("wide camera configured but unavailable or same as the road camera; running single camera")
DRIVER_CAM = os.getenv("DRIVER_CAM") if driver_camera_enabled() else None

# The driver camera must be separate hardware from the road camera. Without
# this the user could point DRIVER_CAM at the road camera, and driver
# monitoring would silently watch the road instead of the driver. Validation is
# Linux/V4L2-only; a rejected camera is dropped so DM fails closed (the
# monitoring processes then see no cabin frames and block engagement) rather
# than monitoring the wrong scene.
if DRIVER_CAM and platform.system() == "Linux":
  try:
    from openpilot.system.camerad.webcam.driver_camera_setup import validate_driver_camera
    validate_driver_camera(NARROW_ROAD_CAM, DRIVER_CAM)
  except (OSError, ValueError, ImportError) as e:
    cloudlog.error(f"DRIVER_CAM rejected ({e}); driver monitoring disabled")
    DRIVER_CAM = None

CameraType = namedtuple("CameraType", ["msg_name", "stream_type", "cam_id", "required"])

# Only the narrow road camera is required. It is the model's main input: without it
# there is nothing to drive from, so its failure must stop camerad. The wide camera
# is a continuous second input with no gating (modeld uses it whenever frames are
# present), and the cabin camera feeds driver monitoring, which fails closed on its
# own when frames are missing. Losing either of those should cost that one feature,
# not the whole stack - a camera sharing a USB 2.0 bus can lose its bandwidth to
# another device at any moment. Optional cameras are therefore tolerated both at
# construction and while running.

CAMERAS = [
  CameraType("narrowRoadCameraState", VisionStreamType.VISION_STREAM_NARROW_ROAD, NARROW_ROAD_CAM, True)
]
if WIDE_CAM:
  CAMERAS.append(CameraType("wideRoadCameraState", VisionStreamType.VISION_STREAM_WIDE_ROAD, WIDE_CAM, False))
if DRIVER_CAM:
  CAMERAS.append(CameraType("cabinCameraState", VisionStreamType.VISION_STREAM_CABIN, DRIVER_CAM, False))


def camera_device(cam_id: str) -> str:
  return f"/dev/video{cam_id}" if platform.system() != "Darwin" and cam_id.isdigit() else cam_id


class Camerad:
  def __init__(self):
    self.pm = messaging.PubMaster([c.msg_name for c in CAMERAS])
    self.vipc_server = VisionIpcServer("camerad")
    self._next_vipc_frame_id = {c.stream_type: 0 for c in CAMERAS}

    self.cameras = []
    for c in CAMERAS:
      cam_device = camera_device(c.cam_id)
      try:
        cam = Camera(c.msg_name, c.stream_type, cam_device, required=c.required)
      except Exception as e:
        # An optional camera that cannot be opened at all must not take the rest of
        # the stack with it - that includes the narrow road camera, which is
        # constructed first. A required camera still raises: a missing model input
        # is a hard failure, and starting anyway would only move it to modeld.
        if c.required:
          raise
        cloudlog.error(f"{c.msg_name} unavailable: {e}; continuing without it")
        # No VIPC buffer for a stream nothing will ever publish to: modeld would
        # otherwise wait on a stream that can never produce a frame.
        continue
      self.cameras.append(cam)
      self.vipc_server.create_buffers_with_sizes(
        c.stream_type, 20, cam.output_w, cam.output_h, cam.nv12_size,
        cam.nv12_stride, cam.nv12_stride * cam.nv12_y_height)

    self.vipc_server.start_listener()

  def _send_yuv(self, yuv, frame_id, pub_type, yuv_type, sof_ns=None, repeat=False):
    # timestampSof must be when the sensor started exposing this frame. The raw
    # V4L2 path supplies exactly that from the kernel - uvcvideo reports it on
    # CLOCK_MONOTONIC, the same clock time.monotonic_ns() uses. The OpenCV
    # fallback cannot see it, and this function used to stamp every frame with
    # the moment decoding finished, which folded MJPEG decode time into the
    # timestamp - and that differs between the 1920x1080 narrow camera and the
    # 1280x720 wide one. Measured on this rig it put the two frames of a pair up
    # to 38 ms apart when their capture instants were never more than 25 ms apart.
    eof = time.monotonic_ns()
    sof = sof_ns if sof_ns is not None else eof
    # Frame IDs are stream-local. They used to be a shared counter, which made
    # the wide stream's first id depend on how many narrow frames had arrived.
    counters = getattr(self, '_next_vipc_frame_id', None)
    if counters is None:
      counters = self._next_vipc_frame_id = {}
    vipc_frame_id = counters.get(yuv_type, 0)
    if repeat:
      vipc_frame_id = (vipc_frame_id - 1) & 0xFFFFFFFF
    else:
      counters[yuv_type] = vipc_frame_id + 1
    self.vipc_server.send(yuv_type, yuv, vipc_frame_id, sof, eof)
    dat = messaging.new_message(pub_type, valid=True)
    msg = {
      "frameId": vipc_frame_id,
      "timestampSof": sof,
      "timestampEof": eof,
      "transform": [1.0, 0.0, 0.0,
                    0.0, 1.0, 0.0,
                    0.0, 0.0, 1.0]
    }
    setattr(dat, pub_type, msg)
    if repeat:
      # A repeat is a publication placeholder, not a newly captured image.
      # Keep the source frame id for debugging and clear its acquisition time.
      getattr(dat, pub_type).frameIdSensor = vipc_frame_id
      getattr(dat, pub_type).timestampSof = 0
      getattr(dat, pub_type).timestampEof = 0
    self.pm.send(pub_type, dat)

  def camera_runner(self, cam):
    # Capture rate and publication rate are separate contracts. The sensor may
    # deliver above or below its requested interval, but modeld and recording need
    # a predictable stream. One monotonic clock owns the publication schedule.
    # The camera driver is not required to honor the requested interval exactly.
    # Fill missing publication slots with the previous frame so downstream sees
    # an exact clock; repeats are marked and modeld ignores them.
    last_yuv = None
    last_sof_ns = None
    last_source_frame_id = 0
    next_publication_time = time.monotonic()
    # The two camera threads must begin on the same monotonic clock epoch, not
    # merely at the same nominal rate. Otherwise one stream's publication slots
    # are offset from the other by an arbitrary fraction of a frame.
    slot_phase = getattr(self, '_slot_phase', None)
    if slot_phase is None:
      slot_phase = self._slot_phase = next_publication_time
    period = 1.0 / cam.fps
    slots_behind = math.ceil((time.monotonic() - slot_phase) / period)
    next_publication_time = slot_phase + max(0, slots_behind) * period
    with closing(cam.read_frames()) as frames:
      for yuv, sof_ns in frames:
        if self._stop_event.is_set():
          break
        now = time.monotonic()
        if last_yuv is not None:
          # One slot per missed interval; never replay several historical slots.
          while next_publication_time + 0.5 / cam.fps <= now:
            self._send_yuv(last_yuv, last_source_frame_id, cam.cam_type_state,
                           cam.stream_type, last_sof_ns, repeat=True)
            next_publication_time += 1.0 / cam.fps
        source_frame_id = cam.cur_frame_id
        self._send_yuv(yuv, source_frame_id, cam.cam_type_state, cam.stream_type, sof_ns)
        cam.cur_frame_id += 1
        last_yuv, last_sof_ns, last_source_frame_id = yuv, sof_ns, source_frame_id
        next_publication_time = max(next_publication_time + 1.0 / cam.fps,
                                    time.monotonic())
        delay = next_publication_time - time.monotonic()
        if delay > 0:
          time.sleep(delay)

  def _worker(self, cam, errors):
    """Run one camera; an optional camera's failure must not kill the process."""
    try:
      self.camera_runner(cam)
    except Exception as exc:
      if cam.required:
        errors.put(exc)
      else:
        # Report and let the rest of the stack run without this input. The model
        # already has a single-camera path, and driver monitoring fails closed on
        # missing cabin frames, so neither needs camerad to die.
        # No stop event here: that would take the other cameras down with it.
        # This camera's own thread has already exited and the run loop keeps
        # going while any other thread is alive.
        cloudlog.error(f"{cam.cam_type_state} stopped: {exc}; continuing without it")

  def run(self):
    self._stop_event = threading.Event()
    errors = queue.Queue()

    threads = []
    # self.cameras holds only the cameras that were constructed successfully, so a
    # stream that never opened has no thread and no VIPC buffer to fill.
    for cam in self.cameras:
      cam_thread = threading.Thread(target=self._worker, args=(cam, errors), daemon=True)
      cam_thread.start()
      threads.append(cam_thread)

    try:
      while any(t.is_alive() for t in threads) or not errors.empty():
        try:
          error = errors.get(timeout=0.1)
        except queue.Empty:
          continue
        raise error
    except KeyboardInterrupt:
      pass
    finally:
      self._stop_event.set()
      for t in threads:
        t.join(timeout=2)


def main():
  # A large OpenCV pool adds CPU contention with modeld/encoderd on desktop.
  # This only limits the webcam process and its BGR fallback path.
  cv.setNumThreads(min(4, os.cpu_count() or 1))
  camerad = Camerad()
  camerad.run()


if __name__ == "__main__":
  main()
