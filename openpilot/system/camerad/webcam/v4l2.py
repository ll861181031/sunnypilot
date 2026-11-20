"""Raw V4L2 capture, used to read the kernel's start-of-frame timestamp.

OpenCV's VideoCapture does not expose the V4L2 buffer timestamp, and the webcam
camerad used to stamp frames with the moment it finished decoding them. That
made timestampSof mean "when we published this", not "when the sensor started
exposing it", and it carried the MJPEG decode time - which differs between the
1920x1080 narrow camera and the 1280x720 wide one, and jitters with CPU load.
Measured on this rig, the two frames of a pair ended up up to 38 ms apart even
though the underlying capture instants were never more than 25 ms apart.

The kernel already knows the real answer. uvcvideo reports each buffer's
timestamp on CLOCK_MONOTONIC, the same clock as time.monotonic_ns(), with the
start-of-frame time it derives from the USB frame. Measured here the gap between
that timestamp and the moment userspace gets the buffer is a very stable 28 ms
(narrow) / 32 ms (wide), which is exactly the offset this module removes.

Everything is done with fcntl.ioctl and ctypes because there is no v4l2 binding
in the venv. The struct layouts below are not guesses: they were checked field by
field against /usr/include/linux/videodev2.h, and the module re-checks them at
import. That check matters - the kernel compares _IOC_SIZE against its own
sizeof() and answers ENOTTY when they differ, which looks like "this device does
not support VIDIOC_S_FMT" and sends you hunting in the wrong place. The union in
v4l2_format holds struct v4l2_window, whose pointers make it 8-byte aligned, so
fmt sits at offset 8 and the struct is 208 bytes, not 204.
"""
import ctypes
import fcntl
import mmap
import os
import select

_IOC_NRBITS, _IOC_TYPEBITS, _IOC_SIZEBITS, _IOC_DIRBITS = 8, 8, 14, 2


def _IOC(direction, type_, nr, size):
  return (direction << 30) | (type_ << 8) | (nr << 0) | (size << 16)


def _IOR(type_, nr, size):
  return _IOC(2, type_, nr, size)


def _IOW(type_, nr, size):
  return _IOC(1, type_, nr, size)


def _IOWR(type_, nr, size):
  return _IOC(3, type_, nr, size)


_V = ord('V')

V4L2_BUF_TYPE_VIDEO_CAPTURE = 1
V4L2_MEMORY_MMAP = 1
V4L2_PIX_FMT_MJPEG = 0x47504A4D  # 'MJPG'
V4L2_BUF_FLAG_TIMESTAMP_MASK = 0x0000E000
V4L2_BUF_FLAG_TIMESTAMP_MONOTONIC = 0x00002000
# v4l2_captureparm.capability bit: the driver accepts a different frame interval.
V4L2_CAP_TIMEPERFRAME = 0x00001000


class _timeval(ctypes.Structure):
  _fields_ = [("tv_sec", ctypes.c_long), ("tv_usec", ctypes.c_long)]


class _v4l2_capability(ctypes.Structure):
  _fields_ = [("driver", ctypes.c_char * 16), ("card", ctypes.c_char * 32), ("bus_info", ctypes.c_char * 32),
              ("version", ctypes.c_uint32), ("capabilities", ctypes.c_uint32),
              ("device_caps", ctypes.c_uint32), ("reserved", ctypes.c_uint32 * 3)]


class _v4l2_pix_format(ctypes.Structure):
  _fields_ = [("width", ctypes.c_uint32), ("height", ctypes.c_uint32), ("pixelformat", ctypes.c_uint32),
              ("field", ctypes.c_uint32), ("bytesperline", ctypes.c_uint32), ("sizeimage", ctypes.c_uint32),
              ("colorspace", ctypes.c_uint32), ("priv", ctypes.c_uint32), ("flags", ctypes.c_uint32),
              ("ycbcr_enc", ctypes.c_uint32), ("quantization", ctypes.c_uint32), ("xfer_func", ctypes.c_uint32)]


class _v4l2_format_union(ctypes.Union):
  # The real union is 8-byte aligned because of struct v4l2_window's __user
  # pointers, so sizeof(v4l2_format) is 208. A 4-byte-aligned stand-in makes the
  # kernel answer ENOTTY for every _IOWR ioctl on this struct.
  _fields_ = [("pix", _v4l2_pix_format), ("raw_data", ctypes.c_byte * 200), ("_align", ctypes.c_uint64)]


class _v4l2_format(ctypes.Structure):
  _fields_ = [("type", ctypes.c_uint32), ("fmt", _v4l2_format_union)]


class _v4l2_requestbuffers(ctypes.Structure):
  _fields_ = [("count", ctypes.c_uint32), ("type", ctypes.c_uint32), ("memory", ctypes.c_uint32),
              ("capabilities", ctypes.c_uint32), ("reserved", ctypes.c_uint32)]


class _v4l2_buffer_m(ctypes.Union):
  _fields_ = [("offset", ctypes.c_uint32), ("userptr", ctypes.c_ulong),
              ("planes", ctypes.c_void_p), ("fd", ctypes.c_int)]


class _v4l2_timecode(ctypes.Structure):
  _fields_ = [("type", ctypes.c_uint32), ("flags", ctypes.c_uint32), ("frames", ctypes.c_uint8),
              ("seconds", ctypes.c_uint8), ("minutes", ctypes.c_uint8), ("hours", ctypes.c_uint8),
              ("userbits", ctypes.c_uint8 * 4)]


class _v4l2_buffer(ctypes.Structure):
  _fields_ = [("index", ctypes.c_uint32), ("type", ctypes.c_uint32), ("bytesused", ctypes.c_uint32),
              ("flags", ctypes.c_uint32), ("field", ctypes.c_uint32), ("timestamp", _timeval),
              ("timecode", _v4l2_timecode), ("sequence", ctypes.c_uint32), ("memory", ctypes.c_uint32),
              ("m", _v4l2_buffer_m), ("length", ctypes.c_uint32), ("reserved2", ctypes.c_uint32),
              ("request_fd", ctypes.c_int32)]


class _v4l2_fract(ctypes.Structure):
  _fields_ = [("numerator", ctypes.c_uint32), ("denominator", ctypes.c_uint32)]


class _v4l2_captureparm(ctypes.Structure):
  _fields_ = [("capability", ctypes.c_uint32), ("capturemode", ctypes.c_uint32),
              ("timeperframe", _v4l2_fract), ("extendedmode", ctypes.c_uint32),
              ("readbuffers", ctypes.c_uint32), ("reserved", ctypes.c_uint32 * 4)]


class _v4l2_streamparm_union(ctypes.Union):
  # Same size trap as v4l2_format above: struct v4l2_streamparm is 204 bytes because
  # its union carries a 200-byte raw_data, so a union sized only for v4l2_captureparm
  # (40 bytes) would make every parm ioctl answer ENOTTY.
  _fields_ = [("capture", _v4l2_captureparm), ("raw_data", ctypes.c_byte * 200)]


class _v4l2_streamparm(ctypes.Structure):
  _fields_ = [("type", ctypes.c_uint32), ("parm", _v4l2_streamparm_union)]


VIDIOC_QUERYCAP = _IOR(_V, 0, ctypes.sizeof(_v4l2_capability))
VIDIOC_S_FMT = _IOWR(_V, 5, ctypes.sizeof(_v4l2_format))
VIDIOC_REQBUFS = _IOWR(_V, 8, ctypes.sizeof(_v4l2_requestbuffers))
VIDIOC_QUERYBUF = _IOWR(_V, 9, ctypes.sizeof(_v4l2_buffer))
VIDIOC_QBUF = _IOWR(_V, 15, ctypes.sizeof(_v4l2_buffer))
VIDIOC_DQBUF = _IOWR(_V, 17, ctypes.sizeof(_v4l2_buffer))
VIDIOC_G_PARM = _IOWR(_V, 21, ctypes.sizeof(_v4l2_streamparm))
VIDIOC_S_PARM = _IOWR(_V, 22, ctypes.sizeof(_v4l2_streamparm))
VIDIOC_STREAMON = _IOW(_V, 18, ctypes.sizeof(ctypes.c_int))
VIDIOC_STREAMOFF = _IOW(_V, 19, ctypes.sizeof(ctypes.c_int))

# Sizes the kernel uses in its own ioctl table. If these drift, every ioctl here
# fails with ENOTTY, so fail loudly at import instead of at the first frame.
_EXPECTED_SIZES = (
  ("v4l2_capability", _v4l2_capability, 104),
  ("v4l2_format", _v4l2_format, 208),
  ("v4l2_pix_format", _v4l2_pix_format, 48),
  ("v4l2_requestbuffers", _v4l2_requestbuffers, 20),
  ("v4l2_buffer", _v4l2_buffer, 88),
  ("v4l2_streamparm", _v4l2_streamparm, 204),
  ("v4l2_captureparm", _v4l2_captureparm, 40),
)


def _self_check() -> None:
  for name, struct, expected in _EXPECTED_SIZES:
    actual = ctypes.sizeof(struct)
    if actual != expected:
      raise RuntimeError(f"{name} is {actual} bytes, kernel expects {expected}")
  if _v4l2_format.fmt.offset != 8:
    raise RuntimeError(f"v4l2_format.fmt is at offset {_v4l2_format.fmt.offset}, expected 8")


_self_check()


class V4L2Capture:
  """A V4L2 capture handle that reports the kernel's CLOCK_MONOTONIC SOF time."""

  def __init__(self, device, width: int, height: int, buffers: int = 4, fps: int | None = None):
    self.device = device
    self.fd = os.open(device, os.O_RDWR | os.O_NONBLOCK)
    self._maps: list[mmap.mmap] = []
    self._streaming = False
    self.timestamp_ns: int | None = None
    self.timestamp_is_monotonic = False
    self.width = 0
    self.height = 0
    self.sizeimage = 0
    # Frame rate the driver reports having settled on, or 0 when it would not say.
    self.fps = 0
    try:
      capability = _v4l2_capability()
      fcntl.ioctl(self.fd, VIDIOC_QUERYCAP, capability)

      fmt = _v4l2_format()
      fmt.type = V4L2_BUF_TYPE_VIDEO_CAPTURE
      fmt.fmt.pix.width = width
      fmt.fmt.pix.height = height
      fmt.fmt.pix.pixelformat = V4L2_PIX_FMT_MJPEG
      fcntl.ioctl(self.fd, VIDIOC_S_FMT, fmt)
      self.width = fmt.fmt.pix.width
      self.height = fmt.fmt.pix.height
      self.sizeimage = fmt.fmt.pix.sizeimage

      # uvcvideo reserves isochronous bandwidth from the frame interval, and that
      # reservation is what the buffer request below has to fit into. Leaving the
      # interval alone keeps the camera's power-on default - measured 30 fps on the
      # narrow camera - which on the shared USB 2.0 bus is what makes the second
      # camera's REQBUFS fail with ENOSPC.
      if fps is not None:
        self.fps = self.set_fps(fps)

      request = _v4l2_requestbuffers()
      request.count = buffers
      request.type = V4L2_BUF_TYPE_VIDEO_CAPTURE
      request.memory = V4L2_MEMORY_MMAP
      fcntl.ioctl(self.fd, VIDIOC_REQBUFS, request)
      if request.count < 2:
        raise RuntimeError(f"{device}: only {request.count} buffers granted")

      for index in range(request.count):
        buf = _v4l2_buffer()
        buf.type, buf.memory, buf.index = V4L2_BUF_TYPE_VIDEO_CAPTURE, V4L2_MEMORY_MMAP, index
        fcntl.ioctl(self.fd, VIDIOC_QUERYBUF, buf)
        self._maps.append(mmap.mmap(self.fd, buf.length, mmap.MAP_SHARED,
                                    mmap.PROT_READ | mmap.PROT_WRITE, offset=buf.m.offset))
        fcntl.ioctl(self.fd, VIDIOC_QBUF, buf)

      stream_type = ctypes.c_int(V4L2_BUF_TYPE_VIDEO_CAPTURE)
      fcntl.ioctl(self.fd, VIDIOC_STREAMON, stream_type)
      self._streaming = True
    except Exception:
      self.release()
      raise

  def set_fps(self, fps: int) -> int:
    """Ask the driver for `fps` and return the rate it reports, or 0 if it will not say.

    A driver that cannot change the interval keeps its current one. That is reported
    back rather than raised: the kernel timestamp this class exists for does not
    depend on the frame rate, and raising would send the camera down the OpenCV
    fallback, which has no kernel timestamp at all.
    """
    parm = _v4l2_streamparm()
    parm.type = V4L2_BUF_TYPE_VIDEO_CAPTURE
    fcntl.ioctl(self.fd, VIDIOC_G_PARM, parm)
    if not (parm.parm.capture.capability & V4L2_CAP_TIMEPERFRAME):
      return 0
    parm.parm.capture.timeperframe.numerator = 1
    parm.parm.capture.timeperframe.denominator = fps
    fcntl.ioctl(self.fd, VIDIOC_S_PARM, parm)
    # The driver answers with the interval it settled on, which can differ from the
    # request when the sensor only exposes a fixed set of modes.
    interval = parm.parm.capture.timeperframe
    return round(interval.denominator / interval.numerator) if interval.numerator else 0

  def read(self, timeout: float = 2.0):
    """Return the next MJPEG frame, or None on timeout / stop.

    The timestamp of the returned frame is left in self.timestamp_ns, in
    nanoseconds on CLOCK_MONOTONIC so it can be compared with
    time.monotonic_ns() directly.
    """
    if self.fd < 0:
      return None
    if not select.select([self.fd], [], [], timeout)[0]:
      return None
    buf = _v4l2_buffer()
    buf.type, buf.memory = V4L2_BUF_TYPE_VIDEO_CAPTURE, V4L2_MEMORY_MMAP
    fcntl.ioctl(self.fd, VIDIOC_DQBUF, buf)
    try:
      kind = buf.flags & V4L2_BUF_FLAG_TIMESTAMP_MASK
      self.timestamp_is_monotonic = kind == V4L2_BUF_FLAG_TIMESTAMP_MONOTONIC
      self.timestamp_ns = buf.timestamp.tv_sec * 1_000_000_000 + buf.timestamp.tv_usec * 1_000
      data = bytes(self._maps[buf.index][:buf.bytesused])
    finally:
      fcntl.ioctl(self.fd, VIDIOC_QBUF, buf)
    return data

  def release(self) -> None:
    if self.fd >= 0:
      if self._streaming:
        try:
          fcntl.ioctl(self.fd, VIDIOC_STREAMOFF, ctypes.c_int(V4L2_BUF_TYPE_VIDEO_CAPTURE))
        except OSError:
          pass
        self._streaming = False
      for mapping in self._maps:
        mapping.close()
      self._maps.clear()
      os.close(self.fd)
      self.fd = -1
