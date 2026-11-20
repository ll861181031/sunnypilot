"""Pure pieces of the offline replay tool: nv12 packing and output summarization.

The full replay needs a recorded segment and a compiled artifact, so it is not part of the
suite; these tests pin the two places where a silent mistake would make every number wrong.
"""
import numpy as np

from openpilot.system.camerad.cameras.nv12_info import get_nv12_info
from openpilot.tools.nav_replay import pack_nv12, summarize


class TestPackNv12:
  def test_y_and_uv_planes_land_at_the_stride_offsets(self):
    w, h = 1344, 760
    flat = np.arange(w * h * 3 // 2, dtype=np.uint8)
    packed = pack_nv12(flat, w, h)
    stride, y_height, _, yuv_size = get_nv12_info(w, h)
    assert packed.shape == (yuv_size,)
    rows = packed[:stride * (y_height + h // 2)].reshape(-1, stride)
    assert np.array_equal(rows[:h, :w], flat[:w * h].reshape(h, w))
    assert np.array_equal(rows[y_height:y_height + h // 2, :w], flat[w * h:].reshape(h // 2, w))

  def test_padding_between_plane_rows_is_zero(self):
    w, h = 1344, 760
    packed = pack_nv12(np.full(w * h * 3 // 2, 255, dtype=np.uint8), w, h)
    stride, y_height, _, _ = get_nv12_info(w, h)
    rows = packed[:stride * (y_height + h // 2)].reshape(-1, stride)
    assert not rows[:h, w:].any(), "stride padding must stay zero, not repeat the last pixel"


class TestSummarize:
  def test_curvature_accel_and_finite_ratio(self):
    outs = [
      {"action": np.array([[0.02, -0.5]]), "plan": np.zeros((33, 15), dtype=np.float32)},
      {"action": np.array([[-0.04, 0.5]]), "plan": np.full((33, 15), np.nan, dtype=np.float32)},
    ]
    summary = summarize(outs)
    assert summary["frames"] == 2
    assert summary["desired_curvature"]["mean_abs"] == 0.03
    assert summary["desired_curvature"]["max_abs"] == 0.04
    assert summary["desired_acceleration"]["min"] == -0.5
    assert summary["plan_finite_ratio"] == 0.5

  def test_empty_output_is_reported_as_none(self):
    summary = summarize([])
    assert summary["frames"] == 0
    assert summary["desired_curvature"]["mean_abs"] is None
    assert summary["plan_finite_ratio"] is None
