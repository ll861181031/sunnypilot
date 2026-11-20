import unittest

from openpilot.selfdrive.modeld.compile_modeld import nv12_copy_size
from openpilot.selfdrive.modeld.modeld import extra_camera_geometry_matches_model
from openpilot.system.camerad.cameras.nv12_info import get_nv12_info

# The geometry the desktop build actually ran with before 2026-10-13: the narrow
# camera published 1344x760 (the size the model is built for) while the wide camera
# published 1280x720. Both numbers come from modeld's own startup log.
NARROW = (1344, 760)
WIDE_OLD = (1280, 720)


def warp_max_index(main_size: tuple[int, int]) -> int:
  """Largest flat index the model's warp can read from a frame.

  Mirrors warp_perspective_tinygrad: it walks y over the main camera's y_height and
  x over the main camera's width, stepping rows by the main camera's stride. That is
  the whole problem - the stride and shape come from the main camera no matter which
  frame is being sampled.
  """
  stride, y_height, _uv_height, _size = get_nv12_info(*main_size)
  return (y_height - 1) * stride + (main_size[0] - 1)


def y_plane_bytes(size: tuple[int, int]) -> int:
  stride, y_height, _uv_height, _size = get_nv12_info(*size)
  return stride * y_height


class TestExtraCameraGeometry(unittest.TestCase):
  """Both camera feeds share one ModelState, so they must share one geometry.

  frame_copy_size and the jit's run_model key are derived from the main camera alone.
  A mismatched extra frame is not rejected by anything at runtime: the copy is capped
  at the main camera's copy size, which still fits inside the extra camera's larger
  buffer, so no exception is raised. What breaks is the content - the warp reads with
  the main camera's indexing and lands outside the extra frame's luma plane.
  """

  def test_matching_geometry_is_accepted(self):
    self.assertTrue(extra_camera_geometry_matches_model(NARROW, NARROW))
    self.assertTrue(extra_camera_geometry_matches_model((1928, 1208), (1928, 1208)))

  def test_differently_sized_geometry_is_rejected(self):
    for extra in (WIDE_OLD, (1920, 1080), (1344, 720), (1280, 760), (2688, 1520)):
      with self.subTest(extra=extra):
        self.assertFalse(extra_camera_geometry_matches_model(NARROW, extra))

  def test_argument_order_does_not_matter_for_equal_sizes(self):
    self.assertTrue(extra_camera_geometry_matches_model(WIDE_OLD, WIDE_OLD))

  def test_the_old_wide_config_would_have_read_past_the_luma_plane(self):
    """This is the failure the guard exists for, expressed in bytes.

    The warp's largest index must stay inside the frame's luma plane. With the wide
    camera at 1280x720 it did not: the index ran into the UV plane, so the model's
    second image was a mix of chroma and out-of-frame luma rather than a picture.
    """
    index = warp_max_index(NARROW)
    self.assertLess(index, y_plane_bytes(NARROW), "the main camera must stay in its own luma plane")
    self.assertGreater(index, y_plane_bytes(WIDE_OLD),
                       "with 1280x720 the same index left the luma plane - the original bug")

  def test_a_matching_geometry_keeps_the_warp_inside_the_luma_plane(self):
    for size in (NARROW, (1928, 1208)):
      with self.subTest(size=size):
        self.assertLess(warp_max_index(size), y_plane_bytes(size))

  def test_the_copy_never_raises_regardless_of_size(self):
    """Why nothing failed loudly: the copy size is the main camera's and the extra
    buffer is bigger, so np.frombuffer sees a large enough buffer either way."""
    main_copy = nv12_copy_size(*get_nv12_info(*NARROW)[:3])
    wide_buffer = get_nv12_info(*WIDE_OLD)[3]
    self.assertLess(main_copy, wide_buffer,
                    "a smaller copy than the buffer is exactly why this was silent")


if __name__ == "__main__":
  unittest.main()
