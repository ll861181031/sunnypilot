import av
import cv2 as cv
import numpy as np
import os
import time
from av.video.reformatter import ColorRange, Interpolation, VideoReformatter

from openpilot.common.swaglog import cloudlog
from openpilot.system.camerad.cameras.nv12_info import get_nv12_info
from openpilot.system.camerad.webcam.v4l2 import V4L2Capture

# What the road streams publish when WEBCAM_OUTPUT_* is unset. 1344x760 is the
# smaller of the two sizes the road model is compiled for and what the desktop
# launcher publishes. The capture size is only the UVC mode: deriving the output
# from it made the published size follow whatever mode the camera happened to
# negotiate, which is not necessarily a size modeld was compiled for.
DEFAULT_ROAD_OUTPUT_SIZE = (1344, 760)
# Cabin frames feed the driver-monitoring warp, which is compiled for a fixed
# set of input sizes. The stock PC cabin resolution is 1928x1208, so the default
# cabin output must use it; overrides may request any compiled size.
DEFAULT_DRIVER_OUTPUT_SIZE = (1928, 1208)
# Aspect ratios closer than this count as matching. The long-standing
# 1920x1080 -> 1344x760 path differs by 0.5%, and cropping it would change
# today's pixels for no visible gain; a 4:3 sensor feeding a 16:10 model
# contract differs by 16% and must be cropped instead of squeezed.
CROP_ASPECT_TOLERANCE = 0.01


def cover_and_crop(src_w: int, src_h: int, out_w: int, out_h: int) -> tuple[int, int, int, int]:
  """Scale size and centred crop that fill `out_w x out_h` without distortion.

  Returns (scale_w, scale_h, crop_x, crop_y). A matching aspect ratio returns
  exactly the output size and a zero crop, so callers keep the historical
  single-resize path. Otherwise the frame is scaled to *cover* the output
  rectangle and the excess is cropped symmetrically - the same thing a camera's
  own 16:9 mode does to a 4:3 sensor. Crop offsets stay even because the UV
  plane is subsampled by two.
  """
  src_aspect = src_w / src_h
  out_aspect = out_w / out_h
  if abs(src_aspect / out_aspect - 1.0) <= CROP_ASPECT_TOLERANCE:
    return out_w, out_h, 0, 0
  scale = max(out_w / src_w, out_h / src_h)
  scale_w = max(out_w, int(round(src_w * scale)) & ~1)
  scale_h = max(out_h, int(round(src_h * scale)) & ~1)
  crop_x = ((scale_w - out_w) // 2) & ~1
  crop_y = ((scale_h - out_h) // 2) & ~1
  return scale_w, scale_h, crop_x, crop_y


# How long a camera may stop delivering before it counts as dead. Both read paths
# share one budget: MAX_READ_MISSES recoverable misses of at most READ_TIMEOUT_S
# each, plus the miss that trips the counter - about 1.8 s in total. A fallback to
# OpenCV therefore cannot silently change how long a dead camera takes to be
# noticed. That is long enough for a bandwidth hiccup or a replug to be survivable,
# and short enough that a camera that is actually gone still fails in under 2 s.
READ_TIMEOUT_S = 0.2
MAX_READ_MISSES = 8


class _TurboJpeg:
  """MJPEG -> NV12 via libjpeg-turbo, with the same range/crop contract as PyAV.

  Decodes at capture resolution, converts the camera's 4:2:2 to 4:2:0 (vertical
  chroma average), applies the JPEG(full) -> MPEG(limited) range conversion, then
  area-scales to the requested output size. Measured on this rig it is ~1.5x
  faster than the PyAV reformat path end to end.
  """
  _Y_LUT = np.clip(np.arange(256) * 219 / 255 + 16, 0, 255).astype(np.uint8)
  _UV_LUT = np.clip((np.arange(256) - 128) * 224 / 255 + 128, 0, 255).astype(np.uint8)

  def __init__(self, out_w, out_h, lib_path="libturbojpeg.so.0"):
    import ctypes
    self.lib = ctypes.CDLL(lib_path)
    self.lib.tjInitDecompress.restype = ctypes.c_void_p
    self.lib.tjDestroy.argtypes = [ctypes.c_void_p]
    self.lib.tjDecompressHeader3.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_ulong,
      ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)]
    self.lib.tjDecompressToYUV2.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_ulong,
      ctypes.c_char_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
    self.handle = self.lib.tjInitDecompress()
    if not self.handle:
      raise RuntimeError("tjInitDecompress failed")
    self.out_w, self.out_h = out_w, out_h
    self.w = self.h = self.sub = None
    self.buf = None
    # libyuv does 4:2:2->4:2:0, scaling and NV12 packing in C, which keeps the
    # per-frame Python work down to four ctypes calls (the GIL is released in each).
    self.yuv = None
    try:
      self.yuv = ctypes.CDLL("libyuv.so.0")
      self.yuv.I422ToI420.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_int,
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_int,
        ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int]
      self.yuv.I420Scale.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_int,
        ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, ctypes.c_int]
    except Exception:
      self.yuv = None
    self._full = None
    self._scaled = None

  def _decode_fast(self, jpeg):
    import ctypes
    lib = self.lib
    if self.w is None:
      w, h, sub, cs = ctypes.c_int(), ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
      if lib.tjDecompressHeader3(self.handle, jpeg, len(jpeg), ctypes.byref(w), ctypes.byref(h),
                                 ctypes.byref(sub), ctypes.byref(cs)) != 0:
        raise RuntimeError("tjDecompressHeader3 failed")
      self.w, self.h, self.sub = w.value, h.value, sub.value
      # Only 4:2:2 takes this path. Reject other subsampling BEFORE decoding:
      # TJSAMP_444 is 0 and needs 3*w*h bytes, while the old 1.5*w*h buffer let
      # libjpeg-turbo write past the end before this check ran.
      if self.sub != 1:
        return None
      self.buf = ctypes.create_string_buffer(self.w * self.h * 2)
    if lib.tjDecompressToYUV2(self.handle, jpeg, len(jpeg), self.buf, self.w, 1, self.h, 0) != 0:
      raise RuntimeError("tjDecompressToYUV2 failed")
    w, h = self.w, self.h
    if self._full is None:
      self._full = ctypes.create_string_buffer(w * h * 3 // 2)
      self._scaled = ctypes.create_string_buffer(self.out_w * self.out_h * 3 // 2)
    ysrc = ctypes.cast(ctypes.byref(self.buf, 0), ctypes.c_void_p)
    usrc = ctypes.cast(ctypes.byref(self.buf, w * h), ctypes.c_void_p)
    vsrc = ctypes.cast(ctypes.byref(self.buf, w * h + (w // 2) * h), ctypes.c_void_p)
    dy = ctypes.cast(ctypes.byref(self._full, 0), ctypes.c_void_p)
    du = ctypes.cast(ctypes.byref(self._full, w * h), ctypes.c_void_p)
    dv = ctypes.cast(ctypes.byref(self._full, w * h + (w // 2) * (h // 2)), ctypes.c_void_p)
    self.yuv.I422ToI420(ysrc, w, usrc, w // 2, vsrc, w // 2, dy, w, du, w // 2, dv, w // 2, w, h)
    oy = ctypes.cast(ctypes.byref(self._scaled, 0), ctypes.c_void_p)
    ou = ctypes.cast(ctypes.byref(self._scaled, self.out_w * self.out_h), ctypes.c_void_p)
    ov = ctypes.cast(ctypes.byref(self._scaled, self.out_w * self.out_h + (self.out_w // 2) * (self.out_h // 2)), ctypes.c_void_p)
    # kFilterBox = 3 (area-like), matches the AREA interpolation the PyAV path used.
    self.yuv.I420Scale(dy, w, du, w // 2, dv, w // 2, w, h, oy, self.out_w, ou, self.out_w // 2, ov, self.out_w // 2,
                       self.out_w, self.out_h, 3)
    arr = np.frombuffer(self._scaled, np.uint8)
    y = arr[:self.out_w * self.out_h].reshape(self.out_h, self.out_w)
    u = arr[self.out_w * self.out_h:self.out_w * self.out_h + (self.out_w // 2) * (self.out_h // 2)].reshape(self.out_h // 2, self.out_w // 2)
    v = arr[self.out_w * self.out_h + (self.out_w // 2) * (self.out_h // 2):].reshape(self.out_h // 2, self.out_w // 2)
    y = cv.LUT(y, self._Y_LUT)
    u = cv.LUT(u, self._UV_LUT)
    v = cv.LUT(v, self._UV_LUT)
    nv = np.empty((self.out_h * 3 // 2, self.out_w), np.uint8)
    nv[:self.out_h] = y
    nv[self.out_h:, 0::2] = u
    nv[self.out_h:, 1::2] = v
    return nv

  def decode(self, jpeg):
    import ctypes
    if self.yuv is not None:
      fast = self._decode_fast(jpeg)
      if fast is not None:
        return fast
    if self.w is None:
      w, h, sub, cs = ctypes.c_int(), ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
      if self.lib.tjDecompressHeader3(self.handle, jpeg, len(jpeg), ctypes.byref(w), ctypes.byref(h),
                                      ctypes.byref(sub), ctypes.byref(cs)) != 0:
        raise RuntimeError("tjDecompressHeader3 failed")
      self.w, self.h, self.sub = w.value, h.value, sub.value
      size = self.w * self.h * 3 // 2 if self.sub == 0 else self.w * self.h + 2 * (self.w // 2) * self.h
      self.buf = ctypes.create_string_buffer(size)

    if self.lib.tjDecompressToYUV2(self.handle, jpeg, len(jpeg), self.buf, self.w, 1, self.h, 0) != 0:
      raise RuntimeError("tjDecompressToYUV2 failed")

    a = np.frombuffer(self.buf, np.uint8)
    w, h = self.w, self.h
    y = a[:w * h].reshape(h, w)
    if self.sub == 1:  # 4:2:2 -> 4:2:0
      u = a[w * h:w * h + (w // 2) * h].reshape(h, w // 2)
      v = a[w * h + (w // 2) * h:].reshape(h, w // 2)
      u = ((u[0::2].astype(np.uint16) + u[1::2]) >> 1).astype(np.uint8)
      v = ((v[0::2].astype(np.uint16) + v[1::2]) >> 1).astype(np.uint8)
    else:
      u = a[w * h:w * h + (w // 2) * (h // 2)].reshape(h // 2, w // 2)
      v = a[w * h + (w // 2) * (h // 2):].reshape(h // 2, w // 2)

    y = cv.LUT(y, self._Y_LUT)
    u = cv.LUT(u, self._UV_LUT)
    v = cv.LUT(v, self._UV_LUT)

    if (w, h) != (self.out_w, self.out_h):
      y = cv.resize(y, (self.out_w, self.out_h), interpolation=cv.INTER_AREA)
      u = cv.resize(u, (self.out_w // 2, self.out_h // 2), interpolation=cv.INTER_AREA)
      v = cv.resize(v, (self.out_w // 2, self.out_h // 2), interpolation=cv.INTER_AREA)

    nv = np.empty((self.out_h * 3 // 2, self.out_w), np.uint8)
    nv[:self.out_h] = y
    nv[self.out_h:, 0::2] = u
    nv[self.out_h:, 1::2] = v
    return nv


class Camera:
  def __init__(self, cam_type_state, stream_type, camera_id, required: bool = True):
    try:
      camera_id = int(camera_id)
    except ValueError: # allow strings, ex: /dev/video0
      pass
    self.required = required
    self.cam_type_state = cam_type_state
    self.stream_type = stream_type
    self.cur_frame_id = 0
    # Cabin capture can have different resolution/orientation from the road.
    #
    # The wide camera gets a prefix of its own as well. It is a second road camera
    # that can be a different device with a different native mode, and it has to
    # share USB bandwidth with the narrow one. Without this, lowering its capture
    # size to fit that budget would drag the narrow camera - the model's main input -
    # down with it, because both read the same WEBCAM_WIDTH/HEIGHT.
    prefix = {'cabinCameraState': 'DRIVER_', 'wideRoadCameraState': 'WIDE_'}.get(cam_type_state, '')

    def setting(name, default):
      return os.getenv(prefix + name, os.getenv(name, default))

    self.fps = int(setting("WEBCAM_FPS", "20"))
    # MJPEG decode backend: libjpeg-turbo (default) with PyAV as the fallback.
    # Default stays PyAV: the TurboJPEG path measured 79% core vs 39% for PyAV at
    # 1080p in the live two-camera stack (ctypes/numpy work contends on the GIL),
    # despite being faster in a single-stream microbenchmark.
    self._use_turbo = os.getenv("WEBCAM_DECODER", "pyav") == "turbojpeg"
    self._turbo = None
    self._turbo_tried = False
    self.rotate_180 = setting("WEBCAM_ROTATE_180", "1") == "1"

    # Capture may run above the publication rate to leave MJPEG decode headroom - the
    # camera runner still paces outgoing frames at self.fps - but the wide camera must
    # not inherit that headroom from the road camera. The two are different devices
    # sharing one USB 2.0 bus, and asking the DECXIN for the narrow one's rate starves
    # it: measured on this machine it runs for a few seconds at 30 fps and then stops
    # delivering, which leaves modeld and selfdrived without the second input. Default
    # it to its own publication rate. Looked up without the usual fallback to the
    # unprefixed name: inheriting the road camera's headroom is exactly what starves it.
    #
    # This used to be applied only on the OpenCV fallback, which silently ignored it on
    # the raw V4L2 path the narrow camera actually uses, so the camera kept its 30 fps
    # power-on default and crowded the wide camera's REQBUFS into ENOSPC.
    if cam_type_state == 'wideRoadCameraState':
      self.capture_fps = int(os.getenv("WIDE_WEBCAM_CAPTURE_FPS", str(self.fps)))
    else:
      self.capture_fps = int(setting("WEBCAM_CAPTURE_FPS", str(self.fps)))

    print(f"Opening {cam_type_state} at {camera_id}")

    # Prefer a raw V4L2 handle: it is the only way to read the kernel's
    # start-of-frame timestamp, and that timestamp is what makes the two road
    # cameras comparable at all. Fall back to OpenCV when the raw path cannot be
    # set up - this is the main camera's input path, so a negotiation this code
    # does not expect must never cost the car its primary camera.
    requested_w = int(setting("WEBCAM_WIDTH", "1164"))
    requested_h = int(setting("WEBCAM_HEIGHT", "874"))
    # V4L2 cannot hold a camera at its advertised rate with a single buffer: the
    # driver needs one to fill while the application holds the current frame.
    # Measured on the DECXIN wide camera, BUFFERSIZE=1 halves it to 9.3 fps while
    # 2 or more give 18.7. The raw path therefore refuses to go below two; the
    # OpenCV path keeps its own shallow-queue defaults below.
    requested_buffers = int(setting("WEBCAM_BUFFERSIZE", "2"))
    self.raw_buffers = max(2, requested_buffers)
    if requested_buffers < 2:
      cloudlog.warning(f"{cam_type_state}: WEBCAM_BUFFERSIZE={requested_buffers} < 2; using {self.raw_buffers}")
    self.v4l2 = None
    self.cap = None
    try:
      self.v4l2 = V4L2Capture(camera_id, requested_w, requested_h,
                              self.raw_buffers, fps=self.capture_fps)
    except Exception as e:
      cloudlog.warning(f"{cam_type_state}: raw V4L2 unavailable ({e}); using OpenCV, no kernel timestamps")

    if self.v4l2 is not None:
      self.W, self.H = self.v4l2.width, self.v4l2.height
      self.mjpeg_decoder = av.CodecContext.create('mjpeg', 'r')
      self.mjpeg_decoder.thread_count = 1
      self.reformatter = VideoReformatter()
    else:
      self._open_opencv(camera_id, cam_type_state, setting, requested_w, requested_h)
    if self.v4l2 is None:
      self.W = int(self.cap.get(cv.CAP_PROP_FRAME_WIDTH))
      self.H = int(self.cap.get(cv.CAP_PROP_FRAME_HEIGHT))
    # Some UVC drivers only expose 1920x1080. Resize to the stock model
    # contract before publishing the VIPC buffer so modeld can initialize.
    # Publish the stock model's compiled narrow-road resolution. The physical
    # UVC mode may be different; resize before encoding the NV12 buffer.
    # The cabin output is consumed by the driver-monitoring warp, which is
    # compiled only for specific sizes, so it cannot follow the capture size.
    default_output = DEFAULT_DRIVER_OUTPUT_SIZE if cam_type_state == 'cabinCameraState' else DEFAULT_ROAD_OUTPUT_SIZE
    self.output_w = int(setting("WEBCAM_OUTPUT_WIDTH", str(default_output[0])))
    self.output_h = int(setting("WEBCAM_OUTPUT_HEIGHT", str(default_output[1])))
    if cam_type_state == 'wideRoadCameraState':
      # modeld pairs the narrow and wide frames and indexes one compiled
      # resolution for both, so a wide stream published at any other size cannot
      # be paired with anything. Read the narrow size here rather than at import
      # time: this runs after the environment is in place and keeps the two road
      # streams in lockstep even if only WIDE_WEBCAM_OUTPUT_* was overridden.
      narrow_w = int(os.getenv("WEBCAM_OUTPUT_WIDTH", str(DEFAULT_ROAD_OUTPUT_SIZE[0])))
      narrow_h = int(os.getenv("WEBCAM_OUTPUT_HEIGHT", str(DEFAULT_ROAD_OUTPUT_SIZE[1])))
      if (self.output_w, self.output_h) != (narrow_w, narrow_h):
        cloudlog.error(f"{cam_type_state} output {self.output_w}x{self.output_h} != narrow road {narrow_w}x{narrow_h}; using the narrow road size")
        self.output_w, self.output_h = narrow_w, narrow_h
    if self.W <= 0 or self.H <= 0 or self.W % 2 or self.H % 2:
      if self.v4l2 is not None:
        self.v4l2.release()
      elif self.cap is not None:
        self.cap.release()
      raise RuntimeError(f"Invalid camera dimensions for NV12: {self.W}x{self.H}")
    # A 4:3 sensor feeding the 16:10 model contract must be cropped, not
    # squeezed: scaling 2592x1944 straight to 1928x1208 compresses the image
    # vertically by 16% (25% for 16:9), which would bend every lane line the model sees.
    # Cabin keeps its historical stretch unless explicitly opted in - its warp
    # is compiled for 1928x1208 and was never validated with a cropped USB
    # image, so changing it here would be an unrelated, unmeasured change.
    # Cabin deliberately does not inherit the global WEBCAM_CROP_TO_OUTPUT:
    # the road launcher sets it to 1, and a fallback to the unprefixed name
    # would silently change the DM image as well.
    if cam_type_state == 'cabinCameraState':
      self.crop_to_output = os.getenv("DRIVER_WEBCAM_CROP_TO_OUTPUT", "0") == "1"
    else:
      self.crop_to_output = setting("WEBCAM_CROP_TO_OUTPUT", "1") == "1"
    if self.crop_to_output:
      self.scale_w, self.scale_h, self.crop_x, self.crop_y = cover_and_crop(
        self.W, self.H, self.output_w, self.output_h)
    else:
      self.scale_w, self.scale_h, self.crop_x, self.crop_y = self.output_w, self.output_h, 0, 0
    self.nv12_stride, self.nv12_y_height, self.nv12_uv_height, self.nv12_size = get_nv12_info(
      self.output_w, self.output_h)


  def _open_opencv(self, camera_id, cam_type_state, setting, requested_w, requested_h):
    """OpenCV fallback: the negotiation this port used before, minus the timestamps.

    Kept intact and separate so the raw V4L2 path can never take the primary
    camera down with it: anything this code does not anticipate lands here, and
    the result is exactly the behaviour the port shipped with.
    """
    self.cap = cv.VideoCapture(camera_id)
    if not self.cap.isOpened():
      self.cap.release()
      raise RuntimeError(f"Cannot open camera: {camera_id}")
    # Uncompressed YUYV is limited to 5 fps at 720p/1080p on this USB camera.
    self.cap.set(cv.CAP_PROP_FOURCC, cv.VideoWriter_fourcc(*"MJPG"))
    self.cap.set(cv.CAP_PROP_FRAME_WIDTH, requested_w)
    self.cap.set(cv.CAP_PROP_FRAME_HEIGHT, requested_h)
    # Same capture rate the raw V4L2 path asks for; see __init__ for why the wide
    # camera deliberately does not fall back to the unprefixed name.
    self.cap.set(cv.CAP_PROP_FPS, self.capture_fps)
    # The narrow camera is the driving input, where the extra frame of queueing
    # (see __init__ for the measurement) costs more than the 0.6 fps it buys, so
    # the OpenCV path keeps the shallow queue there and only the wide camera gets
    # the deeper one. The raw path has a hard floor of two instead.
    default_buffersize = 2 if cam_type_state == 'wideRoadCameraState' else 1
    self.cap.set(cv.CAP_PROP_BUFFERSIZE, int(setting("WEBCAM_BUFFERSIZE", str(default_buffersize))))

    # Keep MJPEG compressed until our decoder. The default OpenCV path first
    # expands 1080p to BGR, then resizes it and converts it back to YUV. Doing
    # the resize in YUV avoids those full-resolution RGB copies/conversions.
    self.mjpeg_decoder = None
    self.reformatter = VideoReformatter()
    if (self.cap.get(cv.CAP_PROP_FOURCC) == cv.VideoWriter_fourcc(*"MJPG") and
        self.cap.set(cv.CAP_PROP_CONVERT_RGB, 0)):
      self.mjpeg_decoder = av.CodecContext.create('mjpeg', 'r')
      self.mjpeg_decoder.thread_count = 1
    self._cap_is_mjpeg = self.mjpeg_decoder is not None

  def bgr2nv12(self, bgr):
    frame = av.VideoFrame.from_numpy_buffer(np.ascontiguousarray(bgr), format='bgr24')
    # The BGR frame is already output-sized (read_frames crops it), so there is
    # nothing left to crop here.
    return self.pack_nv12(frame.reformat(format='nv12'), crop=False)

  def mjpeg2nv12(self, jpeg):
    if self._use_turbo:
      if not self._turbo_tried:
        self._turbo_tried = True
        try:
          self._turbo = _TurboJpeg(self.scale_w, self.scale_h)
        except Exception as e:
          cloudlog.warning(f"{self.cam_type_state}: turbojpeg unavailable ({e}); using PyAV")
      if self._turbo is not None:
        try:
          return self._pack_nv12_array(self._turbo.decode(jpeg), self.rotate_180)
        except Exception as e:
          cloudlog.warning(f"{self.cam_type_state}: turbojpeg decode failed ({e}); falling back to PyAV")
          self._turbo = None

    frames = self.mjpeg_decoder.decode(av.Packet(jpeg))
    if len(frames) != 1:
      raise RuntimeError(f"Expected one MJPEG camera frame, received {len(frames)}")
    # JPEG is full-range; the existing BGR -> NV12 path produces limited-range
    # video. Specify both ranges to preserve black/white levels during scaling.
    # Scale to the covering size; pack_nv12 crops the centred output window, so
    # the optics stay square when capture and publish aspects differ.
    frame = self.reformatter.reformat(
      frames[0], width=getattr(self, "scale_w", self.output_w), height=getattr(self, "scale_h", self.output_h),
      format='nv12', interpolation=Interpolation.AREA,
      src_color_range=ColorRange.JPEG, dst_color_range=ColorRange.MPEG)
    return self.pack_nv12(frame, self.rotate_180)

  def _pack_nv12_array(self, nv, rotate_180=False):
    """Pack an NV12 numpy array produced by _TurboJpeg into the VIPC buffer."""
    crop_x = getattr(self, "crop_x", 0)
    crop_y = getattr(self, "crop_y", 0)
    out = bytearray(self.nv12_size)
    y = nv[:self.scale_h]
    uv = nv[self.scale_h:].reshape(self.scale_h // 2, self.scale_w // 2, 2)
    y = y[crop_y:crop_y + self.output_h, crop_x:crop_x + self.output_w]
    uv = uv[crop_y // 2:crop_y // 2 + self.output_h // 2, crop_x // 2:crop_x // 2 + self.output_w // 2]
    if rotate_180:
      y = y[::-1, ::-1]
      uv = uv[::-1, ::-1]
    dst_y = np.ndarray((self.output_h, self.output_w), np.uint8, buffer=out, strides=(self.nv12_stride, 1))
    dst_y[:] = y
    dst_uv = np.ndarray((self.output_h // 2, self.output_w // 2, 2), np.uint8, buffer=out,
                        offset=self.nv12_stride * self.nv12_y_height, strides=(self.nv12_stride, 2, 1))
    dst_uv[:] = uv
    return out

  def pack_nv12(self, frame, rotate_180=False, crop=True):
    # Copy strided plane views in native code. PyAV and VIPC can have different
    # row strides; all Venus alignment/kernel padding must remain zero.
    src_w, src_h = frame.width, frame.height
    crop_x = getattr(self, "crop_x", 0) if crop else 0
    crop_y = getattr(self, "crop_y", 0) if crop else 0
    out = bytearray(self.nv12_size)
    for plane, rows, offset, channels in (
      (frame.planes[0], self.output_h, 0, 1),
      (frame.planes[1], self.output_h // 2, self.nv12_stride * self.nv12_y_height, 2),
    ):
      # Reverse chroma pixels as U/V pairs, not as individual bytes.
      if channels == 1:
        src = np.ndarray((src_h, src_w), dtype=np.uint8, buffer=plane, strides=(plane.line_size, 1))
        window = src[crop_y:crop_y + rows, crop_x:crop_x + self.output_w]
        dst = np.ndarray((rows, self.output_w), dtype=np.uint8, buffer=out,
                         offset=offset, strides=(self.nv12_stride, 1))
      else:
        src = np.ndarray((src_h // 2, src_w // 2, channels), dtype=np.uint8, buffer=plane,
                         strides=(plane.line_size, channels, 1))
        window = src[crop_y // 2:crop_y // 2 + rows,
                     crop_x // 2:crop_x // 2 + self.output_w // channels]
        dst = np.ndarray((rows, self.output_w // channels, channels), dtype=np.uint8, buffer=out,
                         offset=offset, strides=(self.nv12_stride, channels, 1))
      dst[:] = window[::-1, ::-1] if rotate_180 else window
    return out

  def read_frames(self):
    # A UVC camera sharing a USB 2.0 bus can miss a frame when another device
    # claims bandwidth, and `cap.read()` reports that as a plain failure. Treating
    # one miss as a dead camera used to take down camerad and with it the whole
    # stack, even when the camera in question was the optional wide one. Give a
    # stalled stream a bounded grace period instead: long enough to ride out a
    # hiccup, short enough that a genuinely unplugged camera still fails. Both
    # paths use the same READ_TIMEOUT_S/MAX_READ_MISSES budget, so a fallback from
    # raw V4L2 to OpenCV cannot change how a dead camera is noticed.
    misses = 0
    try:
      while True:
        if self.v4l2 is not None:
          # The kernel timestamp belongs to this frame, read it before anything else.
          blob = self.v4l2.read(READ_TIMEOUT_S)
          if blob is None:
            misses += 1
            if misses > MAX_READ_MISSES:
              raise RuntimeError(f"Camera stopped delivering frames: {self.cam_type_state}")
            continue
          misses = 0
          yield self.mjpeg2nv12(blob), (self.v4l2.timestamp_ns if self.v4l2.timestamp_is_monotonic else None)
          continue
        ret, frame = self.cap.read()
        if not ret:
          misses += 1
          if misses > MAX_READ_MISSES:
            raise RuntimeError(f"Camera stopped delivering frames: {self.cam_type_state}")
          time.sleep(READ_TIMEOUT_S)
          continue
        misses = 0
        if self.mjpeg_decoder is not None and frame.ndim == 2:
          yield self.mjpeg2nv12(frame), None
          continue
        # Backends without raw MJPEG support retain the original BGR path.
        if self.rotate_180:
          frame = cv.flip(frame, -1)
        scale_w, scale_h = getattr(self, "scale_w", self.output_w), getattr(self, "scale_h", self.output_h)
        if frame.shape[1] != scale_w or frame.shape[0] != scale_h:
          frame = cv.resize(frame, (scale_w, scale_h), interpolation=cv.INTER_AREA)
        crop_x, crop_y = getattr(self, "crop_x", 0), getattr(self, "crop_y", 0)
        if crop_x or crop_y:
          frame = frame[crop_y:crop_y + self.output_h, crop_x:crop_x + self.output_w]
        yuv = self.bgr2nv12(frame)
        yield yuv, None
    finally:
      if self.v4l2 is not None:
        self.v4l2.release()
      elif self.cap is not None:
        self.cap.release()
