import ast
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from openpilot.common.transformations.camera import CameraConfig, DeviceCameraConfig, DEVICE_CAMERAS, device_camera_config
from openpilot.common.transformations.model import get_warp_matrix
from openpilot.selfdrive.modeld.modeld import extra_camera_geometry_matches_model
from openpilot.sunnypilot.modeld_v2.camera_offset_helper import CameraOffsetHelper


SOURCE = Path(__file__).resolve().parents[2] / 'sunnypilot/modeld_v2/modeld.py'


class CameraMessages(dict):
  def __init__(self, rpy=(0.0, 0.0, 0.0), updated=True):
    super().__init__(deviceState=SimpleNamespace(deviceType='test'),
                     narrowRoadCameraState=SimpleNamespace(sensor='unknown'),
                     extrinsicsCalibration=SimpleNamespace(rpyCalib=list(rpy), height=[1.22]))
    self.updated = {'extrinsicsCalibration': updated}
    self.seen = {'deviceState': True, 'narrowRoadCameraState': True}


def calibration_block():
  # 执行生产标定块，避免导入 modeld_v2 时初始化 GPU 和加载模型。
  tree = ast.parse(SOURCE.read_text())
  main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'main')
  loop = next(node for node in main.body if isinstance(node, ast.While) and
              any(isinstance(child, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'buf_main' for t in child.targets)
                  for child in node.body))
  block = next(node for node in loop.body if isinstance(node, ast.If) and
               'extrinsicsCalibration' in ast.unparse(node.test))
  return compile(ast.Module(body=[block], type_ignores=[]), str(SOURCE), 'exec')


@pytest.fixture
def camera_state(monkeypatch):
  # 两种真实 CameraConfig 使用不同焦距，使错用内参产生可观测的 4 倍差异。
  monkeypatch.setitem(DEVICE_CAMERAS, ('test', 'unknown'), DeviceCameraConfig(
    CameraConfig(1344, 760, 1820.0), CameraConfig(1344, 760, 455.0), CameraConfig(1344, 760, 455.0)))
  return {
    'np': np, 'device_camera_config': device_camera_config, 'get_warp_matrix': get_warp_matrix,
    'camera_offset_helper': CameraOffsetHelper(), 'sm': CameraMessages(),
    'model_transform_main': np.zeros((3, 3), dtype=np.float32),
    'model_transform_extra': np.zeros((3, 3), dtype=np.float32),
    'model_transform_extra_fallback': np.zeros((3, 3), dtype=np.float32),
    'main_wide_camera': False, 'extra_is_wide': False, 'extra_wide_prev': None, 'live_calib_seen': False,
  }


def test_missing_wide_frame_uses_the_narrow_lens_geometry(camera_state):
  exec(calibration_block(), camera_state)
  assert camera_state['model_transform_main'][0, 0] == pytest.approx(2.0)
  assert camera_state['model_transform_extra'][0, 0] == pytest.approx(4.0)


def test_a_wide_frame_uses_the_wide_lens_geometry(camera_state):
  camera_state['extra_is_wide'] = True
  exec(calibration_block(), camera_state)
  assert camera_state['model_transform_extra'][0, 0] == pytest.approx(1.0)


def test_the_main_frame_fallback_has_its_own_geometry_ready(camera_state):
  camera_state['extra_is_wide'] = True
  exec(calibration_block(), camera_state)
  assert camera_state['model_transform_extra_fallback'][0, 0] == pytest.approx(4.0)


def test_an_unknown_previous_camera_state_forces_the_initial_warp(camera_state):
  camera_state['sm'] = CameraMessages(updated=False)
  camera_state['extra_is_wide'] = False
  camera_state['extra_wide_prev'] = None
  exec(calibration_block(), camera_state)
  assert camera_state['live_calib_seen'] is True
  assert camera_state['extra_wide_prev'] is False


def test_same_type_after_calibration_does_not_rebuild_every_frame(camera_state):
  camera_state['sm'] = CameraMessages(updated=False)
  camera_state['extra_wide_prev'] = False
  camera_state['sm'].updated['extrinsicsCalibration'] = False
  before = camera_state['model_transform_extra'].copy()
  exec(calibration_block(), camera_state)
  np.testing.assert_array_equal(camera_state['model_transform_extra'], before)


def test_a_drop_and_recovery_refresh_both_warps_without_new_calibration(camera_state):
  camera_state['extra_is_wide'] = True
  block = calibration_block()
  exec(block, camera_state)
  camera_state['sm'].updated['extrinsicsCalibration'] = False
  camera_state['extra_is_wide'] = False
  exec(block, camera_state)
  assert camera_state['model_transform_extra'][0, 0] == pytest.approx(4.0)
  camera_state['extra_is_wide'] = True
  exec(block, camera_state)
  assert camera_state['model_transform_extra'][0, 0] == pytest.approx(1.0)


def test_a_single_wide_main_camera_keeps_wide_geometry_in_both_slots(camera_state):
  camera_state['main_wide_camera'] = True
  exec(calibration_block(), camera_state)
  assert camera_state['model_transform_main'][0, 0] == pytest.approx(0.5)
  assert camera_state['model_transform_extra'][0, 0] == pytest.approx(1.0)


@pytest.mark.parametrize('rpy', [(), (0.0,), (0.0, np.nan, 0.0)])
def test_unusable_calibration_starts_with_finite_neutral_warps(camera_state, rpy):
  camera_state['sm'] = CameraMessages(rpy=rpy)
  exec(calibration_block(), camera_state)
  assert np.isfinite(camera_state['model_transform_extra']).all()
  assert camera_state['model_transform_extra'][0, 0] == pytest.approx(4.0)


def test_initial_camera_messages_do_not_require_a_new_calibration_message(camera_state):
  camera_state['sm'] = CameraMessages(rpy=(), updated=False)
  exec(calibration_block(), camera_state)
  assert camera_state['model_transform_main'][0, 0] == pytest.approx(2.0)
  assert camera_state['model_transform_extra'][0, 0] == pytest.approx(4.0)


def test_offset_uses_the_actual_second_camera_horizon(camera_state):
  camera_state['sm'] = CameraMessages(rpy=(0.0, 0.05, 0.0))
  helper = camera_state['camera_offset_helper']
  helper.set_offset(0.1)
  exec(calibration_block(), camera_state)
  narrow = DEVICE_CAMERAS[('test', 'unknown')].narrow_road.intrinsics
  original = get_warp_matrix(np.array([0.0, 0.05, 0.0]), narrow, True)
  shear = np.eye(3)
  shear[0, 1] = 0.01 / 1.22
  shear[0, 2] = -(0.01 / 1.22) * (380.0 - 1820.0 * np.tan(0.05))
  np.testing.assert_allclose(camera_state['model_transform_extra'], shear @ original, rtol=1e-5, atol=1e-4)


@pytest.mark.parametrize('extra_size, expected', [((1344, 760), True), ((1280, 720), False)])
def test_mismatched_extra_geometry_cannot_reach_the_model(extra_size, expected):
  tree = ast.parse(SOURCE.read_text())
  main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'main')
  block = next(node for node in main.body if isinstance(node, ast.If) and
               isinstance(node.test, ast.Name) and node.test.id == 'use_extra_client')
  state = {'use_extra_client': True,
           'vipc_client_main': SimpleNamespace(width=1344, height=760),
           'vipc_client_extra': SimpleNamespace(width=extra_size[0], height=extra_size[1], buffer_len=9999999),
           'extra_camera_geometry_matches_model': extra_camera_geometry_matches_model,
           'cloudlog': SimpleNamespace(warning=lambda *args: None, error=lambda *args: None)}
  exec(compile(ast.Module(body=[block], type_ignores=[]), str(SOURCE), 'exec'), state)
  assert state['use_extra_client'] is expected
