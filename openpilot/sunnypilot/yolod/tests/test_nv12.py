"""NV12 -> BGR tests, including the strided/padded layout camerad really uses."""

import cv2
import numpy as np
import pytest

from openpilot.sunnypilot.yolod.nv12 import nv12_to_bgr


def _gradient_bgr(width: int, height: int) -> np.ndarray:
  x = np.linspace(0, 255, width, dtype=np.uint8)[None, :]
  y = np.linspace(0, 255, height, dtype=np.uint8)[:, None]
  image = np.empty((height, width, 3), dtype=np.uint8)
  image[..., 0] = x
  image[..., 1] = y
  image[..., 2] = 128
  return image


def _i420_from_bgr(image: np.ndarray) -> np.ndarray:
  return cv2.cvtColor(image, cv2.COLOR_BGR2YUV_I420)


def _i420_planes(i420: np.ndarray, width: int, height: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
  chroma = (height // 2, width // 2)
  flat = i420.reshape(-1)
  y_plane = flat[:height * width].reshape(height, width)
  u_plane = flat[height * width:height * width + height * width // 4].reshape(chroma)
  v_plane = flat[height * width + height * width // 4:].reshape(chroma)
  return y_plane, u_plane, v_plane


def _pack_nv12(i420: np.ndarray, width: int, height: int, stride: int, uv_offset: int | None,
               fill: int = 0xAB) -> np.ndarray:
  """Independently pack I420 into NV12 with row padding and a Y-plane gap."""
  y_plane, u_plane, v_plane = _i420_planes(i420, width, height)
  uv_offset = stride * (height + 8) if uv_offset is None else uv_offset
  buffer = np.full(uv_offset + stride * (height // 2), fill, dtype=np.uint8)
  buffer[:height * stride].reshape(height, stride)[:, :width] = y_plane
  uv = np.empty((height // 2, width), dtype=np.uint8)
  uv[:, 0::2] = u_plane
  uv[:, 1::2] = v_plane
  buffer[uv_offset:uv_offset + stride * (height // 2)].reshape(height // 2, stride)[:, :width] = uv
  return buffer


@pytest.mark.parametrize("width,height,stride", [
  (16, 10, 16),
  (1928, 1208, 2048),
  (8, 6, 12),
  (640, 480, 640),
])
def test_matches_opencv_i420_conversion(width, height, stride):
  image = _gradient_bgr(width, height)
  i420 = _i420_from_bgr(image)
  expected = cv2.cvtColor(i420, cv2.COLOR_YUV2BGR_I420)

  buffer = _pack_nv12(i420, width, height, stride, uv_offset=None)
  actual = nv12_to_bgr(buffer, width, height, stride, stride * (height + 8))
  assert actual.shape == (height, width, 3)
  assert actual.dtype == np.uint8
  assert np.array_equal(actual, expected)


def test_padding_and_gap_contents_are_ignored():
  width, height, stride = 1928, 1208, 2048
  i420 = _i420_from_bgr(_gradient_bgr(width, height))
  uv_offset = stride * (height + 8)
  first = _pack_nv12(i420, width, height, stride, uv_offset, fill=0x00)
  second = _pack_nv12(i420, width, height, stride, uv_offset, fill=0xFF)
  expected = cv2.cvtColor(i420, cv2.COLOR_YUV2BGR_I420)
  assert np.array_equal(nv12_to_bgr(first, width, height, stride, uv_offset), expected)
  assert np.array_equal(nv12_to_bgr(second, width, height, stride, uv_offset), expected)


def test_contiguous_layout_matches_padded_layout():
  width, height = 320, 240
  i420 = _i420_from_bgr(_gradient_bgr(width, height))
  contiguous = _pack_nv12(i420, width, height, width, width * height)
  padded = _pack_nv12(i420, width, height, 384, 384 * (height + 16))
  expected = cv2.cvtColor(i420, cv2.COLOR_YUV2BGR_I420)
  assert np.array_equal(nv12_to_bgr(contiguous, width, height), expected)
  assert np.array_equal(nv12_to_bgr(padded, width, height, 384, 384 * (height + 16)), expected)


def test_accepts_memoryview_and_bytearray():
  width, height = 64, 48
  i420 = _i420_from_bgr(_gradient_bgr(width, height))
  uv_offset = width * (height + 8)
  buffer = _pack_nv12(i420, width, height, width, uv_offset)
  expected = cv2.cvtColor(i420, cv2.COLOR_YUV2BGR_I420)
  assert np.array_equal(nv12_to_bgr(memoryview(buffer), width, height, width, uv_offset), expected)
  assert np.array_equal(nv12_to_bgr(bytearray(buffer.tobytes()), width, height, width, uv_offset), expected)


@pytest.mark.parametrize("width,height,stride,uv_offset", [
  (0, 10, None, None),
  (16, 0, None, None),
  (15, 10, None, None),  # odd width
  (16, 9, None, None),   # odd height
  (16, 10, 8, None),     # stride smaller than width
])
def test_rejects_invalid_geometry(width, height, stride, uv_offset):
  with pytest.raises(ValueError):
    nv12_to_bgr(bytes(width * height * 2 if width > 0 and height > 0 else 16), width, height, stride, uv_offset)


def test_rejects_short_buffer():
  with pytest.raises(ValueError, match="too small"):
    nv12_to_bgr(bytes(100), 64, 48)


def test_matches_camerad_nv12_packing():
  """Cross-check against the layout camerad actually publishes (PyAV NV12 planes)."""
  av = pytest.importorskip("av")
  width, height = 320, 240
  bgr = _gradient_bgr(width, height)
  frame = av.VideoFrame.from_numpy_buffer(np.ascontiguousarray(bgr), format="bgr24").reformat(format="nv12")
  stride = 384
  y_height = height + 16  # camerad pads the Y plane height, uv_offset uses the padded value
  uv_offset = stride * y_height
  buffer = np.zeros(uv_offset + stride * (height // 2), dtype=np.uint8)

  y_plane = frame.planes[0]
  uv_plane = frame.planes[1]
  y_src = np.ndarray((height, width), dtype=np.uint8, buffer=y_plane, strides=(y_plane.line_size, 1))
  uv_src = np.ndarray((height // 2, width // 2, 2), dtype=np.uint8, buffer=uv_plane,
                      strides=(uv_plane.line_size, 2, 1))
  buffer[:height * stride].reshape(height, stride)[:, :width] = y_src
  buffer[uv_offset:uv_offset + stride * (height // 2)].reshape(height // 2, stride)[:, :width] = \
    uv_src.reshape(height // 2, width)

  actual = nv12_to_bgr(buffer, width, height, stride, uv_offset)
  assert actual.shape == bgr.shape
  # U/V order, stride handling and the Y-plane gap all influence the result: a U/V
  # swap alone would push the mean error far above this bound.
  mean_error = np.abs(actual.astype(np.int16) - bgr.astype(np.int16)).mean()
  assert mean_error < 3.0, f"mean absolute error {mean_error:.2f} is too high"
