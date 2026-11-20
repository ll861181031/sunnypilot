import numpy as np
import pytest

from openpilot.sunnypilot.laned.nv12 import nv12_y_plane


def test_nv12_y_plane_skips_stride_padding():
  width, height, stride = 4, 3, 6
  raw = np.full(stride * height, 255, dtype=np.uint8)
  for row in range(height):
    raw[row * stride:row * stride + width] = row * 10
  output = nv12_y_plane(raw.tobytes(), width, height, stride)
  assert output.shape == (height, width)
  assert output.tolist() == [[0, 0, 0, 0], [10, 10, 10, 10], [20, 20, 20, 20]]
  assert output.max() < 255  # padding was not included


def test_nv12_y_plane_accepts_a_numpy_buffer():
  raw = np.arange(4 * 3, dtype=np.uint8).reshape(3, 4)
  output = nv12_y_plane(raw, 4, 3, 4)
  assert np.array_equal(output, raw)


def test_nv12_y_plane_rejects_short_buffers():
  with pytest.raises(ValueError):
    nv12_y_plane(b"\x00" * 10, 4, 3, 4)


def test_nv12_y_plane_rejects_a_stride_smaller_than_width():
  with pytest.raises(ValueError):
    nv12_y_plane(b"\x00" * 100, 4, 3, 3)
