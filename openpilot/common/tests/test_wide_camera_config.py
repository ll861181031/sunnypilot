"""Single/dual camera is decided by what is plugged in, not by a mode setting."""

import os

import pytest

from openpilot.common import camera_config


@pytest.fixture
def linux(monkeypatch):
  monkeypatch.setattr(camera_config.platform, 'system', lambda: 'Linux')


def test_no_wide_camera_configured(monkeypatch):
  monkeypatch.delenv(camera_config.WIDE_CAM_ENV, raising=False)
  assert not camera_config.wide_camera_configured()
  assert not camera_config.wide_camera_available()


def test_wide_camera_present(monkeypatch, tmp_path, linux):
  wide = tmp_path / 'wide-video'
  wide.write_bytes(b'')
  monkeypatch.setenv(camera_config.WIDE_CAM_ENV, str(wide))
  assert camera_config.wide_camera_configured()
  assert camera_config.wide_camera_available()


def test_missing_wide_camera_falls_back_to_single(monkeypatch, tmp_path, linux):
  monkeypatch.setenv(camera_config.WIDE_CAM_ENV, str(tmp_path / 'gone'))
  assert camera_config.wide_camera_configured()
  assert not camera_config.wide_camera_available()


def test_disable_switch_wins_over_a_present_camera(monkeypatch, tmp_path, linux):
  wide = tmp_path / 'wide-video'
  wide.write_bytes(b'')
  monkeypatch.setenv(camera_config.WIDE_CAM_ENV, str(wide))
  monkeypatch.setenv('SUNNYPILOT_DISABLE_WIDE_CAMERA', '1')
  assert not camera_config.wide_camera_configured()
  assert not camera_config.wide_camera_available()


def test_same_device_as_the_main_camera_is_refused(monkeypatch, tmp_path, linux):
  # This is the stale-path failure a lens/port swap produces: two stream names
  # pointing at one camera. Opening it twice fails with ENOSPC, and feeding the
  # model the same view twice is worse than running single camera.
  main = tmp_path / 'main-video'
  main.write_bytes(b'')
  link = tmp_path / 'wide-link'
  os.symlink(main, link)
  monkeypatch.setenv(camera_config.NARROW_ROAD_CAM_ENV, str(main))
  monkeypatch.setenv(camera_config.WIDE_CAM_ENV, str(link))
  assert not camera_config.wide_camera_available()


def test_numeric_id_is_resolved_to_the_video_node(monkeypatch, linux):
  checked = []

  def fake_exists(path):
    checked.append(path)
    return True

  monkeypatch.setenv(camera_config.WIDE_CAM_ENV, '3')
  monkeypatch.delenv(camera_config.NARROW_ROAD_CAM_ENV, raising=False)
  monkeypatch.setattr(camera_config.os.path, 'exists', fake_exists)
  assert camera_config.wide_camera_available()
  assert checked == ['/dev/video3']


def test_non_linux_trusts_the_configured_id(monkeypatch, tmp_path):
  # Device discovery is V4L2-only; elsewhere keep the historical behaviour.
  monkeypatch.setattr(camera_config.platform, 'system', lambda: 'Darwin')
  monkeypatch.setenv(camera_config.WIDE_CAM_ENV, str(tmp_path / 'not-v4l2'))
  assert camera_config.wide_camera_available()
