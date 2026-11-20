"""`driver_camera_available()` is the single answer for "is there a driver camera?".

camerad, the manager, selfdrived and both device pages each used to derive it from
`DRIVER_CAM` plus their own PC/webcam conditions, so a camera camerad had already
rejected still got a preview button and still got driver monitoring started. The
probe opens devices, so the tests isolate it: no real `/dev/video*` is touched.
"""

import importlib
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from openpilot.common import camera_config
from openpilot.system.camerad.webcam import driver_camera_setup


@pytest.fixture(autouse=True)
def fresh_cache():
  """The answer is cached, so each test has to start from an empty cache."""
  camera_config.driver_camera_available.cache_clear()
  yield
  camera_config.driver_camera_available.cache_clear()


@pytest.fixture
def pc(monkeypatch):
  """This desktop, with nothing configured and no camera switch flipped."""
  monkeypatch.setattr(camera_config.hardware, 'PC', True)
  monkeypatch.setattr(camera_config.platform, 'system', lambda: 'Linux')
  for key in ('SUNNYPILOT_DISABLE_DRIVER_CAMERA', 'DRIVER_CAM', 'NARROW_ROAD_CAM', 'ROAD_CAM'):
    monkeypatch.delenv(key, raising=False)
  return monkeypatch


def test_the_disable_switch_wins_over_a_working_camera(pc):
  pc.setenv('SUNNYPILOT_DISABLE_DRIVER_CAMERA', '1')
  pc.setenv('DRIVER_CAM', '2')
  with patch.object(driver_camera_setup, 'validate_driver_camera') as validate:
    assert camera_config.driver_camera_available() is False
  validate.assert_not_called()


def test_no_driver_camera_configured(pc):
  assert camera_config.driver_camera_available() is False


def test_a_rejected_driver_camera_reports_false_and_says_why_once(pc):
  # camerad drops a DRIVER_CAM it rejects (the road camera under another name, a
  # metadata node, a device that is gone), so the rest of the stack must agree with
  # that instead of waiting for cabin frames that will never arrive.
  pc.setenv('DRIVER_CAM', '/dev/video9')
  with patch.object(driver_camera_setup, 'validate_driver_camera',
                    side_effect=ValueError('Road and driver camera must be separate devices')), \
       patch.object(camera_config.cloudlog, 'error') as error:
    assert camera_config.driver_camera_available() is False
    assert camera_config.driver_camera_available() is False
  assert error.call_count == 1, "the probe is cached; one report per process is enough"
  assert 'separate devices' in error.call_args.args[0]


def test_a_validated_driver_camera_is_available_and_probed_once(pc):
  pc.setenv('DRIVER_CAM', '/dev/video2')
  pc.setenv('NARROW_ROAD_CAM', '/dev/v4l/by-id/usb-road-video-index0')
  with patch.object(driver_camera_setup, 'validate_driver_camera') as validate:
    assert camera_config.driver_camera_available() is True
    assert camera_config.driver_camera_available() is True
  validate.assert_called_once_with('/dev/v4l/by-id/usb-road-video-index0', '/dev/video2')


def test_other_platforms_keep_the_configured_camera_without_v4l2_validation(pc):
  # The separation check is Linux/V4L2-only, and camerad keeps DRIVER_CAM as-is on
  # macOS, so there is nothing here to disagree with.
  pc.setattr(camera_config.platform, 'system', lambda: 'Darwin')
  pc.setenv('DRIVER_CAM', 'Built-in FaceTime HD Camera')
  assert camera_config.driver_camera_available() is True


def test_a_comma_device_has_a_built_in_cabin_camera(monkeypatch):
  monkeypatch.setattr(camera_config.hardware, 'PC', False)
  monkeypatch.delenv('SUNNYPILOT_DISABLE_DRIVER_CAMERA', raising=False)
  monkeypatch.delenv('DRIVER_CAM', raising=False)
  assert camera_config.driver_camera_available() is True
  monkeypatch.setenv('SUNNYPILOT_DISABLE_DRIVER_CAMERA', '1')
  camera_config.driver_camera_available.cache_clear()
  assert camera_config.driver_camera_available() is False


def test_narrow_road_camera_is_resolved_exactly_like_camerad(pc):
  # If the two disagreed about which camera is the road camera, the separation check
  # would compare the driver camera against the wrong device.
  from openpilot.system.camerad.webcam import camerad
  try:
    for env in ({}, {'ROAD_CAM': '3'}, {'ROAD_CAM': '3', 'NARROW_ROAD_CAM': '5'}):
      with patch.dict(os.environ, env, clear=False):
        for key in ('ROAD_CAM', 'NARROW_ROAD_CAM'):
          if key not in env:
            os.environ.pop(key, None)
        importlib.reload(camerad)
        assert camera_config.narrow_road_cam() == camerad.NARROW_ROAD_CAM
  finally:
    importlib.reload(camerad)


def test_dmonitoring_follows_the_shared_answer():
  from openpilot.system.manager import process_config
  assert process_config.DRIVER_CAMERA_AVAILABLE == (
    camera_config.driver_camera_available() and (not process_config.PC or process_config.WEBCAM))
  for name in ('dmonitoringd', 'dmonitoringmodeld'):
    assert process_config.managed_processes[name].enabled == process_config.DRIVER_CAMERA_AVAILABLE


def test_the_driving_stack_uses_the_shared_answer():
  from openpilot.selfdrive.selfdrived import selfdrived
  src = Path(selfdrived.__file__).read_text()
  assert 'DM_ENABLED = driver_camera_available()' in src
  assert 'DRIVER_CAM = bool(os.getenv' not in src, "selfdrived must not re-derive the camera itself"


def test_the_settings_page_offers_exactly_one_preview():
  # sunnypilot's device page builds its own item list and replaces the base one, so
  # the base row must stay out of the way there; both use the same predicate.
  from openpilot.selfdrive.ui.layouts.settings import device as base_device
  from openpilot.selfdrive.ui.sunnypilot.layouts.settings import device as sp_device

  base_src = Path(base_device.__file__).read_text()
  sp_src = Path(sp_device.__file__).read_text()

  assert 'if not gui_app.sunnypilot_ui() and driver_camera_available():' in base_src
  assert 'set_visible(driver_camera_available)' in sp_src
  for src, name in ((base_src, 'base'), (sp_src, 'sunnypilot')):
    assert 'driver_camera_enabled' not in src, f"the {name} device page must use driver_camera_available()"
