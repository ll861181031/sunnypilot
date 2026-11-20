import ast
import inspect

import numpy as np
import pytest

from openpilot.selfdrive.modeld import modeld as stock
from openpilot.sunnypilot.modeld_v2.camera_offset_helper import CameraOffsetHelper

HEIGHT = 1.22  # metres, the fallback CameraOffsetHelper.update() uses when calibration omits it
ZERO_RPY = [0.0, 0.0, 0.0]


def intrinsics(fy=1864.0, cy=380.0):
  """A plausible pinhole matrix. cy is where the horizon sits when pitch is zero."""
  return np.array([[1864.0, 0.0, 672.0], [0.0, fy, cy], [0.0, 0.0, 1.0]], dtype=np.float32)


def transform():
  """Any invertible warp matrix works; the shear does not care where it came from."""
  return np.array([[1.0, 0.0, 100.0], [0.0, 1.0, 50.0], [0.0, 0.0, 1.0]], dtype=np.float32)


def sheared(offset, pairs, rpy=ZERO_RPY, height=HEIGHT):
  """Steady-state shear for a given offset.

  update_transforms() smooths at 0.9/0.1 per call, so a single call only applies a
  tenth of the target. These tests are about the steady-state geometry, not the ramp,
  so converge the smoothing first.
  """
  helper = CameraOffsetHelper()
  helper.set_offset(offset)
  for _ in range(500):
    helper.update_transforms((), rpy, height)
  return helper.update_transforms(pairs, rpy, height)


class TestCameraOffsetShear:
  """The stock modeld had no lateral-origin compensation at all; the tinygrad path did.

  These cover the shear that was added to the stock path. They do not claim the offset
  fixes the measured lane bias - that needs a drive - only that it is wired in, is the
  identity at the default, and bends the projection the way a lateral camera shift does.
  """

  def test_the_default_offset_is_the_identity(self):
    # The safety property: a fresh helper with the param at its 0.0 default must not
    # touch the transform, so an upgrade cannot change how the car drives by itself.
    helper = CameraOffsetHelper()
    main, extra = helper.update_transforms(
      ((transform(), intrinsics()), (transform(), intrinsics(fy=900.0, cy=400.0))), ZERO_RPY, HEIGHT)
    np.testing.assert_array_equal(main, transform())
    np.testing.assert_array_equal(extra, transform())

  def test_only_the_horizontal_row_of_the_transform_changes(self):
    (out,) = sheared(0.10, ((transform(), intrinsics()),))
    np.testing.assert_array_equal(out[1], transform()[1])
    np.testing.assert_array_equal(out[2], transform()[2])
    assert not np.array_equal(out[0], transform()[0])

  def test_the_shift_is_zero_on_the_horizon_line_and_grows_below_it(self):
    # A lateral camera shift moves the projection by f * offset / distance, which in
    # row space means a horizontal shift proportional to (row - horizon). So the
    # horizon line must not move at all and the shift must grow linearly below it.
    # An identity warp matrix is used so the input row equals the image row; with the
    # translation in transform() the horizon lands on a different input row and the
    # arithmetic below would no longer describe the geometry.
    v_horizon = 380.0
    k = 0.10 / HEIGHT
    (out,) = sheared(0.10, ((np.eye(3, dtype=np.float32), intrinsics(cy=v_horizon)),))

    def shift_at(v):
      point = np.array([0.0, float(v), 1.0])
      return float((out @ point)[0] - point[0])

    assert shift_at(v_horizon) == pytest.approx(0.0, abs=1e-3)
    assert shift_at(v_horizon + 100.0) == pytest.approx(k * 100.0, abs=1e-2)
    assert shift_at(v_horizon + 200.0) == pytest.approx(k * 200.0, abs=1e-2)

  def test_the_shift_scales_linearly_with_the_offset(self):
    intr = intrinsics()
    k = {offset: float((sheared(offset, ((transform(), intr),))[0] - transform())[0, 1])
         for offset in (0.05, 0.10, 0.20)}
    assert k[0.10] == pytest.approx(2 * k[0.05])
    assert k[0.20] == pytest.approx(4 * k[0.05])
    assert k[0.10] == pytest.approx(0.10 / HEIGHT)

  def test_each_transform_is_sheared_around_its_own_horizon(self):
    # This is why update_transforms() exists instead of reusing update(): the stock
    # path builds its extra transform from the narrow intrinsics when no wide stream
    # exists, so the two transforms have different horizon rows.
    k = 0.10 / HEIGHT
    main, extra = sheared(0.10, ((transform(), intrinsics(cy=380.0)),
                                 (transform(), intrinsics(fy=900.0, cy=400.0))))
    # Row 0 of the shear is [1, k, -k * v_horizon], so row 0 of the product moves by
    # k * M[1] - k * v_horizon * M[2]. With this M that is k * (50 - v_horizon).
    assert float((main - transform())[0, 2]) == pytest.approx(k * (50.0 - 380.0), abs=1e-3)
    assert float((extra - transform())[0, 2]) == pytest.approx(k * (50.0 - 400.0), abs=1e-3)
    assert float((main - transform())[0, 2]) != pytest.approx(float((extra - transform())[0, 2]))

  def test_pitch_moves_the_horizon_row(self):
    # get_v_horizon() subtracts fy * tan(pitch), so a calibrated pitch must change the
    # pivot too. Guards against the shear being computed with a fixed cy.
    intr = intrinsics(fy=1864.0, cy=380.0)
    assert CameraOffsetHelper.get_v_horizon(intr, [0.0, 0.05, 0.0]) == pytest.approx(
      380.0 - 1864.0 * np.tan(0.05), abs=1e-3)

  def test_a_hand_edited_param_cannot_produce_an_unbounded_shear(self):
    # Params is writable from anywhere, and the shear scales with the offset. The
    # settings page already limits itself, but that is not a guarantee about the value
    # the modeld reads back.
    helper = CameraOffsetHelper()
    helper.set_offset(999.0)
    assert helper.camera_offset == pytest.approx(CameraOffsetHelper.MAX_OFFSET_M)
    helper.set_offset(-999.0)
    assert helper.camera_offset == pytest.approx(-CameraOffsetHelper.MAX_OFFSET_M)
    helper.set_offset(0.10)
    assert helper.camera_offset == pytest.approx(0.10)

  def test_the_smoothing_converges_on_the_target(self):
    helper = CameraOffsetHelper()
    helper.set_offset(0.20)
    for _ in range(500):
      helper.update_transforms((), ZERO_RPY, HEIGHT)
    assert helper.actual_camera_offset == pytest.approx(0.20, abs=1e-6)

  def test_the_first_step_is_a_tenth_of_the_target(self):
    # 0.9/0.1 smoothing: one call must not jump the full way, or changing the value
    # while driving would be a step change in the model's view.
    helper = CameraOffsetHelper()
    helper.set_offset(0.20)
    helper.update_transforms((), ZERO_RPY, HEIGHT)
    assert helper.actual_camera_offset == pytest.approx(0.02)


class TestStockModeldWiring:
  """The class above only proves the math; this proves the modeld calls it."""

  def test_the_calibration_block_applies_the_offset_to_both_transforms(self):
    tree = ast.parse(inspect.getsource(stock.main))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and n.func.attr == "update_transforms"]
    assert len(calls) == 1, "expected exactly one update_transforms() call in the model loop"
    pairs = calls[0].args[0]
    assert isinstance(pairs, ast.Tuple), "must pass (transform, intrinsics) pairs"
    assert len(pairs.elts) == 3, "main, wide extra, and main-frame fallback must all be sheared"

  def test_the_offset_is_read_from_the_param_store_once_in_the_loop(self):
    tree = ast.parse(inspect.getsource(stock.main))
    reads = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and n.func.attr == "set_offset"]
    assert len(reads) == 1, "the offset must be read from the param store exactly once"
    assert 'params.get("CameraOffset"' in inspect.getsource(stock.main)
