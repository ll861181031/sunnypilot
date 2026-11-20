"""Preserve per-plane warp pixels while sharing the chroma transform."""
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from openpilot.selfdrive.modeld import mgxd


@pytest.mark.parametrize('shape', [(1344, 760, 1408, 1081344), (640, 480, 672, 323584)])
@pytest.mark.parametrize('invert', [False, True])
@pytest.mark.parametrize('uv_mode', ['col2', 'same', 'elem'])
@pytest.mark.parametrize('shift', [(0., 0.), (0.5, 0.), (-0.25, 1.25)])
def test_warp_matches_independent_plane_reference(monkeypatch, shape, invert, uv_mode, shift):
  w, h, stride, uv_off = shape
  raw = np.random.default_rng(123).integers(0, 256, uv_off + stride * h // 2, dtype=np.uint8).tobytes()
  transform = np.array([[1.3125, .015, 90.], [-.01, 1.484375, 60.], [1e-5, -2e-5, 1.]], np.float32)
  monkeypatch.setattr(mgxd, 'INVERT', invert)
  monkeypatch.setattr(mgxd, 'SHIFT', shift)
  monkeypatch.setattr(mgxd, 'UV_MODE', uv_mode)
  matrix = np.linalg.inv(transform) if invert else transform.copy()
  matrix[:, 2] -= matrix[:, :2] @ np.array(shift, np.float32)
  if uv_mode == 'elem':
    scaled = transform * np.array([[1., 1., .5], [1., 1., .5], [2., 2., 1.]], np.float32)
    chroma_matrix = np.linalg.inv(scaled) if invert else scaled
  elif uv_mode == 'same':
    chroma_matrix = matrix
  else:
    chroma_matrix = matrix.copy()
    chroma_matrix[:, 2] *= .5
  y = np.frombuffer(raw, np.uint8, count=stride * h).reshape(h, stride)[:, :w]
  uv = np.frombuffer(raw, np.uint8, count=stride * h // 2, offset=uv_off).reshape(h // 2, stride)[:, :w]
  yw = cv2.warpPerspective(y, matrix, (512, 256), flags=cv2.INTER_LINEAR)
  uw = cv2.warpPerspective(uv[:, ::2], chroma_matrix, (256, 128), flags=cv2.INTER_LINEAR)
  vw = cv2.warpPerspective(uv[:, 1::2], chroma_matrix, (256, 128), flags=cv2.INTER_LINEAR)
  expected = np.stack([yw[::2, ::2], yw[1::2, ::2], yw[::2, 1::2], yw[1::2, 1::2], uw, vw])
  before = transform.copy()
  result = mgxd.warp_camera(cv2, raw, *shape, transform)
  np.testing.assert_array_equal(result, expected)
  np.testing.assert_array_equal(transform, before)
  assert result.shape == (6, 128, 256) and result.dtype == np.uint8 and result.flags.c_contiguous


def test_warp_transforms_interleaved_chroma_once():
  calls = []
  def warp(source, *args, **kwargs):
    calls.append(source.shape)
    return cv2.warpPerspective(source, *args, **kwargs)
  cv = SimpleNamespace(INTER_LINEAR=cv2.INTER_LINEAR, warpPerspective=warp)
  result = mgxd.warp_camera(cv, bytes(640 * 480 * 3 // 2), 640, 480, 640, 640 * 480,
                           np.eye(3, dtype=np.float32))
  assert np.count_nonzero(result) == 0
  assert calls == [(480, 640), (240, 320, 2)], 'U and V must share one chroma warp'
