"""The desktop camera's intrinsics must describe the buffer the model samples.

`modeld` builds its input warp from `DEVICE_CAMERAS[("pc", "unknown")]` in buffer
pixels, so borrowing the 1928x1208 device intrinsics while the webcam publishes
1344x760 left most of the model input outside the frame.
"""

import contextlib
import importlib
import math
import os
from unittest.mock import patch

import numpy as np
import pytest

from openpilot.common.transformations import camera as camera_mod
from openpilot.common.transformations.camera import DEVICE_CAMERAS
from openpilot.common.transformations.model import MEDMODEL_INPUT_SIZE, get_warp_matrix

STREAM_W, STREAM_H = 1344, 760


def out_of_frame_fraction(intrinsics, stream_w: int = STREAM_W, stream_h: int = STREAM_H) -> float:
  """Share of model-input samples whose source pixel falls outside the frame."""
  warp = get_warp_matrix(np.zeros(3), intrinsics, False)
  width, height = MEDMODEL_INPUT_SIZE
  xs, ys = np.meshgrid(np.arange(width) + 0.5, np.arange(height) + 0.5)
  pts = np.stack([xs.ravel(), ys.ravel(), np.ones(xs.size)])
  src = warp @ pts
  u, v = src[0] / src[2], src[1] / src[2]
  return float(((u < 0) | (u >= stream_w) | (v < 0) | (v >= stream_h)).mean())


class _EmptyParams:
  """No settings-page values, so the environment/default path is under test.

  The real Params store on this rig has BydCameraFovTenthsDeg=397 set (the
  measured lens field of view). Tests that pin the environment or built-in
  default must not inherit that, or they pass on a fresh checkout and fail on
  the car that actually configured the lens.
  """

  def __init__(self):
    pass

  def get(self, key, return_default=False):
    return 0


def with_stream_env(**extra):
  env = {"WEBCAM_OUTPUT_WIDTH": str(STREAM_W), "WEBCAM_OUTPUT_HEIGHT": str(STREAM_H), **extra}
  return _stream_env(env)


@contextlib.contextmanager
def _stream_env(env):
  with patch.dict(os.environ, env, clear=False), patch("openpilot.common.params.Params", _EmptyParams):
    yield


class TestPcCameraIntrinsics:
  def teardown_method(self):
    importlib.reload(camera_mod)

  def test_follows_the_published_stream_size(self):
    with with_stream_env():
      cam = importlib.reload(camera_mod).DEVICE_CAMERAS[("pc", "unknown")]
      assert (cam.narrow_road.width, cam.narrow_road.height) == (STREAM_W, STREAM_H)
      # Principal point is the buffer centre, as in the device configs.
      assert cam.narrow_road.intrinsics[0, 2] == STREAM_W / 2
      assert cam.narrow_road.intrinsics[1, 2] == STREAM_H / 2
      assert (cam.cabin.width, cam.cabin.height) == (STREAM_W, STREAM_H)
      assert (cam.wide_road.width, cam.wide_road.height) == (STREAM_W, STREAM_H)

  def test_field_of_view_matches_the_device_camera_by_default(self):
    with with_stream_env():
      pc = importlib.reload(camera_mod).DEVICE_CAMERAS[("pc", "unknown")]
      device = DEVICE_CAMERAS[("tici", "unknown")]

      def fov(cfg):
        return 2 * math.degrees(math.atan(cfg.width / 2 / cfg.focal_length))

      assert fov(pc.narrow_road) == pytest.approx(fov(device.narrow_road), abs=0.01)

  def test_focal_length_can_be_overridden(self):
    with with_stream_env(SUNNYPILOT_PC_CAMERA_FOCAL="2550", SUNNYPILOT_PC_CAMERA_WIDE_FOCAL="605"):
      pc = importlib.reload(camera_mod).DEVICE_CAMERAS[("pc", "unknown")]
      assert pc.narrow_road.focal_length == 2550.0
      assert pc.wide_road.focal_length == 605.0

  def test_focal_scales_with_the_stream_width(self):
    with with_stream_env(WEBCAM_OUTPUT_WIDTH="672", WEBCAM_OUTPUT_HEIGHT="380"):
      pc = importlib.reload(camera_mod).DEVICE_CAMERAS[("pc", "unknown")]
    device = DEVICE_CAMERAS[("tici", "unknown")].narrow_road
    assert pc.narrow_road.width == 672
    # Field of view is what the lens provides, so focal/width stays constant.
    assert pc.narrow_road.focal_length / pc.narrow_road.width == pytest.approx(device.focal_length / device.width, rel=1e-3)


class TestModelWarpStaysInsideTheFrame:
  def test_pc_intrinsics_keep_the_model_input_inside_the_frame(self):
    with with_stream_env():
      pc = importlib.reload(camera_mod).DEVICE_CAMERAS[("pc", "unknown")]
    # The device intrinsics against a 1344x760 buffer is the bug this guards.
    device = DEVICE_CAMERAS[("tici", "unknown")]
    assert out_of_frame_fraction(device.narrow_road.intrinsics) > 0.5
    assert out_of_frame_fraction(pc.narrow_road.intrinsics) < 0.15

  def test_a_too_long_focal_pushes_the_sampled_region_out_of_frame(self):
    # This is why the focal length has to match the lens: the model input covers a
    # fixed angle, so a longer-than-real focal asks for more pixels than the frame
    # holds and the out-of-frame share grows again.
    with with_stream_env():
      default = importlib.reload(camera_mod).DEVICE_CAMERAS[("pc", "unknown")].narrow_road
    with with_stream_env(SUNNYPILOT_PC_CAMERA_FOCAL="3000"):
      narrow = importlib.reload(camera_mod).DEVICE_CAMERAS[("pc", "unknown")].narrow_road
    assert out_of_frame_fraction(narrow.intrinsics) > out_of_frame_fraction(default.intrinsics)


class TestSettingsPageFocalOverride:
  """The on-device settings page wins over the launcher environment variable.

  Order matters. The launcher value is the deployment default; the settings page
  is the driver changing their mind from the car. An unset settings entry (0)
  must leave the launcher value in charge, so the page cannot silently blank the
  configured lens on a device that never touched it.
  """

  def teardown_method(self):
    importlib.reload(camera_mod)

  @staticmethod
  def with_settings(values: dict[str, float]):
    # Patch the Params class rather than the module attribute: the tests reload
    # camera_mod to re-evaluate _pc_camera_config(), and a reload would undo a
    # patch applied to the module object. _param_override imports Params
    # inside the function, so patching the class survives the reload.
    fake = type("FakeParams", (), {
      "__init__": lambda self: None,
      "get": lambda self, key, return_default=False: values.get(key, 0),
    })
    return patch("openpilot.common.params.Params", fake)

  def test_settings_page_overrides_the_environment(self):
    with with_stream_env(SUNNYPILOT_PC_CAMERA_FOCAL="1864"), self.with_settings({"BydCameraFocalPx": 2000.0}):
      pc = importlib.reload(camera_mod).DEVICE_CAMERAS[("pc", "unknown")]
    assert pc.narrow_road.focal_length == 2000.0

  def test_unset_settings_entry_leaves_the_environment_in_charge(self):
    with with_stream_env(SUNNYPILOT_PC_CAMERA_FOCAL="1864"), self.with_settings({}):
      pc = importlib.reload(camera_mod).DEVICE_CAMERAS[("pc", "unknown")]
    assert pc.narrow_road.focal_length == 1864.0

  def test_narrow_and_wide_are_independent(self):
    env = {"SUNNYPILOT_PC_CAMERA_FOCAL": "1864", "SUNNYPILOT_PC_CAMERA_WIDE_FOCAL": "395"}
    with with_stream_env(**env), self.with_settings({"BydCameraWideFocalPx": 900.0}):
      pc = importlib.reload(camera_mod).DEVICE_CAMERAS[("pc", "unknown")]
    assert pc.narrow_road.focal_length == 1864.0
    assert pc.wide_road.focal_length == 900.0

  def test_settings_never_change_the_published_frame_size(self):
    # The focal length is an intrinsic; the buffer geometry comes from the stream
    # and modeld derives copy sizes from it. A settings entry must not touch it.
    with with_stream_env(), self.with_settings({"BydCameraFocalPx": 2000.0, "BydCameraWideFocalPx": 900.0}):
      pc = importlib.reload(camera_mod).DEVICE_CAMERAS[("pc", "unknown")]
    assert (pc.narrow_road.width, pc.narrow_road.height) == (STREAM_W, STREAM_H)
    assert (pc.wide_road.width, pc.wide_road.height) == (STREAM_W, STREAM_H)

  def test_a_broken_params_backend_falls_back_instead_of_raising(self):
    # camera.py is imported by the replay tools and by tests where the params
    # service is not running, so a missing backend must degrade to the
    # environment/default path rather than break the import.
    with patch("openpilot.common.params.Params", side_effect=RuntimeError("no params service")):
      assert camera_mod._param_override("BydCameraFocalPx") == 0.0



class TestMeasuredFieldOfView:
  """A tape-measured field of view outranks a focal length.

  The focal in millimetres cancels out of the conversion, so the driver-facing
  number is the one they can actually check: park facing a wall and measure the
  span the frame edges cover. These tests pin the arithmetic and the precedence.
  """

  def teardown_method(self):
    importlib.reload(camera_mod)

  @staticmethod
  def with_settings(values: dict[str, float]):
    fake = type("FakeParams", (), {
      "__init__": lambda self: None,
      "get": lambda self, key, return_default=False: values.get(key, 0),
    })
    return patch("openpilot.common.params.Params", fake)

  def test_the_shipped_field_of_view_reproduces_the_shipped_focal(self):
    # 39.65 deg at 1344 px is the value the launcher comment derives from the
    # lens, so the inverse must land back on the 1864 px already in use.
    assert camera_mod.focal_from_hfov(39.65, STREAM_W) == pytest.approx(1864.0, abs=0.01)

  def test_focal_scales_with_the_published_width(self):
    # Same lens, narrower stream: fewer pixels across the same angle.
    assert camera_mod.focal_from_hfov(39.65, 672) == pytest.approx(932.0, abs=0.01)

  def test_field_of_view_wins_over_the_focal_length(self):
    with with_stream_env(), self.with_settings({"BydCameraFocalPx": 2000, "BydCameraFovTenthsDeg": 397}):
      pc = importlib.reload(camera_mod).DEVICE_CAMERAS[("pc", "unknown")]
    assert pc.narrow_road.focal_length == pytest.approx(camera_mod.focal_from_hfov(39.7, STREAM_W))

  def test_focal_is_used_when_no_field_of_view_is_set(self):
    with with_stream_env(), self.with_settings({"BydCameraFocalPx": 2000}):
      pc = importlib.reload(camera_mod).DEVICE_CAMERAS[("pc", "unknown")]
    assert pc.narrow_road.focal_length == 2000.0

  def test_the_two_cameras_are_independent(self):
    values = {"BydCameraFovTenthsDeg": 397, "BydCameraWideFocalPx": 900}
    with with_stream_env(), self.with_settings(values):
      pc = importlib.reload(camera_mod).DEVICE_CAMERAS[("pc", "unknown")]
    assert pc.narrow_road.focal_length == pytest.approx(camera_mod.focal_from_hfov(39.7, STREAM_W))
    assert pc.wide_road.focal_length == 900.0

  def test_a_field_of_view_past_ninety_degrees_is_flagged_by_the_maths(self):
    # The UI clamps the entry, but the formula itself is the reason: tan diverges
    # at 90 deg, so 185 - the fisheye on this bench - yields a NEGATIVE focal.
    # This is why "the wide camera just needs the right number" is not true.
    assert camera_mod.focal_from_hfov(185.0, STREAM_W) < 0
    # A wider view is a SHORTER focal, so the curve must fall, not rise.
    assert camera_mod.focal_from_hfov(89.0, STREAM_W) < camera_mod.focal_from_hfov(45.0, STREAM_W)


class TestPhysicalFocalNeedsSensorWidth:
  """A focal length in mm is not a camera parameter on its own.

    f_px = f_mm * width_px / sensor_width_mm

  Both halves are required, and neither does anything alone. This matters
  because the two spec-sheet numbers a datasheet gives are often approximate:
  on this lens (8 mm, 1/2.7 inch) they resolve to ~2029 px, while the tape
  measured field of view resolves to 1864 px - a 9% disagreement, which is why
  the measured value outranks them.
  """

  def teardown_method(self):
    importlib.reload(camera_mod)

  @staticmethod
  def with_settings(values: dict[str, float]):
    fake = type("FakeParams", (), {
      "__init__": lambda self: None,
      "get": lambda self, key, return_default=False: values.get(key, 0),
    })
    return patch("openpilot.common.params.Params", fake)

  def test_datasheet_pair_gives_a_focal(self):
    # 8.0 mm on a 5.27 mm wide (1/2.7 inch) sensor at 1344 px.
    values = {"BydCameraFocalTenthMm": 80, "BydCameraSensorWidthTenthMm": 53}
    with with_stream_env(), self.with_settings(values):
      pc = importlib.reload(camera_mod).DEVICE_CAMERAS[("pc", "unknown")]
    assert pc.narrow_road.focal_length == pytest.approx(8.0 * STREAM_W / 5.3, rel=1e-6)

  def test_focal_alone_is_ignored(self):
    # Entering only the focal must not silently blank a working configuration.
    with with_stream_env(SUNNYPILOT_PC_CAMERA_FOCAL="1864"), self.with_settings({"BydCameraFocalTenthMm": 80}):
      pc = importlib.reload(camera_mod).DEVICE_CAMERAS[("pc", "unknown")]
    assert pc.narrow_road.focal_length == 1864.0

  def test_sensor_width_alone_is_ignored(self):
    with with_stream_env(SUNNYPILOT_PC_CAMERA_FOCAL="1864"), self.with_settings({"BydCameraSensorWidthTenthMm": 53}):
      pc = importlib.reload(camera_mod).DEVICE_CAMERAS[("pc", "unknown")]
    assert pc.narrow_road.focal_length == 1864.0

  def test_datasheet_pair_outranks_a_direct_focal(self):
    values = {"BydCameraFocalTenthMm": 80, "BydCameraSensorWidthTenthMm": 53, "BydCameraFocalPx": 3000}
    with with_stream_env(), self.with_settings(values):
      pc = importlib.reload(camera_mod).DEVICE_CAMERAS[("pc", "unknown")]
    assert pc.narrow_road.focal_length == pytest.approx(8.0 * STREAM_W / 5.3, rel=1e-6)

  def test_measured_field_of_view_outranks_the_datasheet_pair(self):
    values = {"BydCameraFocalTenthMm": 80, "BydCameraSensorWidthTenthMm": 53, "BydCameraFovTenthsDeg": 397}
    with with_stream_env(), self.with_settings(values):
      pc = importlib.reload(camera_mod).DEVICE_CAMERAS[("pc", "unknown")]
    assert pc.narrow_road.focal_length == pytest.approx(camera_mod.focal_from_hfov(39.7, STREAM_W))

  def test_the_datasheet_and_the_measurement_really_do_disagree(self):
    # Not a nitpick: this is the 9% gap that motivates the precedence order.
    # If a future change makes these agree, the ordering rationale needs revisiting.
    datasheet_px = 8.0 * STREAM_W / 5.27
    measured_px = camera_mod.focal_from_hfov(39.65, STREAM_W)
    assert datasheet_px / measured_px == pytest.approx(1.09, abs=0.02)

  def test_the_two_cameras_are_independent(self):
    values = {"BydCameraFocalTenthMm": 80, "BydCameraSensorWidthTenthMm": 53,
              "BydCameraWideFocalTenthMm": 17, "BydCameraWideSensorWidthTenthMm": 72}
    with with_stream_env(), self.with_settings(values):
      pc = importlib.reload(camera_mod).DEVICE_CAMERAS[("pc", "unknown")]
    assert pc.narrow_road.focal_length == pytest.approx(8.0 * STREAM_W / 5.3, rel=1e-6)
    assert pc.wide_road.focal_length == pytest.approx(1.7 * STREAM_W / 7.2, rel=1e-6)


def test_unknown_device_sensor_falls_back_instead_of_raising():
  # modeld must not die on a new device or an unknown sensor string; the fallback
  # is the AR/OX config the pre-deviceType path already used.
  # Use the module's own dict: earlier tests reload the module, so the top-level
  # import of DEVICE_CAMERAS can belong to a previous class generation.
  fallback = camera_mod.DEVICE_CAMERAS[("tici", "unknown")]
  assert camera_mod.device_camera_config("future-device", "future-sensor") == fallback
  assert camera_mod.device_camera_config("tici", "ar0231") == camera_mod.DEVICE_CAMERAS[("tici", "ar0231")]
