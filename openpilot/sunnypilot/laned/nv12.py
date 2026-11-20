"""NV12 helpers for the lane daemon.

The lane classifier only needs luma, so unlike ``yolod.nv12`` this module does not
build a BGR image. VisionIPC's Y plane has a row stride that may exceed the
visible width; those padding columns are skipped here.
"""

import numpy as np


def nv12_y_plane(data: bytes | bytearray | memoryview | np.ndarray, width: int, height: int,
                 stride: int) -> np.ndarray:
  """Return the visible ``(height, width)`` luma plane of an NV12 buffer."""
  if width <= 0 or height <= 0:
    raise ValueError(f"invalid frame size: {width}x{height}")
  if stride < width:
    raise ValueError(f"stride {stride} is smaller than width {width}")

  if isinstance(data, np.ndarray):
    raw = np.ascontiguousarray(data, dtype=np.uint8).reshape(-1)
  else:
    raw = np.frombuffer(data, dtype=np.uint8)

  y_size = stride * height
  if raw.size < y_size:
    raise ValueError(f"short NV12 Y plane: need {y_size} bytes, got {raw.size}")
  return raw[:y_size].reshape(height, stride)[:, :width]
