"""NV12 (YUV420 semi-planar) frame helpers for the YOLO daemon.

camerad publishes NV12 frames through VisionIPC. Two details of that layout are
easy to get wrong and both shift the chroma, which tints the whole image:

* the Y and UV planes have a row stride that can be larger than the image width,
  so padding has to be skipped row by row instead of reshaping the plane;
* the UV plane starts at ``uv_offset``, not right after ``stride * height`` -
  camerad aligns the Y plane's height up, so the gap between the two can be
  several rows.

The conversion itself is OpenCV's I420 -> BGR, so the YUV maths is not
reimplemented here: this module only repacks the two strided planes into the
contiguous I420 buffer that ``cv2.cvtColor`` expects.
"""

import cv2
import numpy as np


def nv12_to_bgr(data: bytes | bytearray | memoryview | np.ndarray, width: int, height: int,
                stride: int | None = None, uv_offset: int | None = None) -> np.ndarray:
  """Convert an NV12 buffer to a BGR ``(height, width, 3)`` uint8 image.

  ``stride`` defaults to ``width`` and ``uv_offset`` to ``stride * height``, which
  is the unpadded layout. Both must be passed for real VisionIPC buffers.
  """
  if width <= 0 or height <= 0:
    raise ValueError(f"invalid frame size: {width}x{height}")
  if width % 2 != 0 or height % 2 != 0:
    raise ValueError(f"NV12 requires even dimensions, got {width}x{height}")

  stride = width if stride is None else int(stride)
  if stride < width:
    raise ValueError(f"stride {stride} is smaller than width {width}")
  uv_offset = stride * height if uv_offset is None else int(uv_offset)

  buf = np.frombuffer(data, dtype=np.uint8)
  y_end = stride * height
  uv_end = uv_offset + stride * (height // 2)
  if buf.size < uv_end:
    raise ValueError(f"NV12 buffer too small: need {uv_end} bytes, got {buf.size}")

  y_plane = buf[:y_end].reshape(height, stride)[:, :width]
  uv_plane = buf[uv_offset:uv_end].reshape(height // 2, stride)[:, :width]
  u_plane = uv_plane[:, 0::2]
  v_plane = uv_plane[:, 1::2]

  # I420 in a (height * 3 // 2, width) array: Y rows, then U, then V. The U and V
  # planes are (height // 2, width // 2) each, i.e. height * width // 4 bytes.
  i420 = np.empty((height * 3 // 2, width), dtype=np.uint8)
  i420[:height] = y_plane
  i420[height:] = np.concatenate((u_plane.ravel(), v_plane.ravel())).reshape(height // 2, width)
  return cv2.cvtColor(i420, cv2.COLOR_YUV2BGR_I420)
