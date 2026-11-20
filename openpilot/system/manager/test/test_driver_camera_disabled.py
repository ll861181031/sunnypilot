import asyncio
import importlib
import json
import os
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from openpilot.common.camera_config import apply_driver_camera_settings, DRIVER_CAMERA_PARAMS
from openpilot.system.manager import process_config


def test_disabled_camera_clears_only_camera_preferences():
  params = Mock()
  with patch.dict(os.environ, {'SUNNYPILOT_DISABLE_DRIVER_CAMERA': '1'}):
    apply_driver_camera_settings(params)
  assert [c.args for c in params.put_bool.call_args_list] == [(key, False) for key in DRIVER_CAMERA_PARAMS]
  assert all(c.kwargs == {'block': True} for c in params.put_bool.call_args_list)
  params.remove.assert_not_called()


def test_enabled_camera_keeps_existing_preferences():
  params = Mock()
  with patch.dict(os.environ, {'SUNNYPILOT_DISABLE_DRIVER_CAMERA': '0'}):
    apply_driver_camera_settings(params)
  params.put_bool.assert_not_called()


def test_stale_preview_request_cannot_start_offroad_processes_when_disabled():
  params = Mock()
  params.get_bool.return_value = True
  with patch.dict(os.environ, {'SUNNYPILOT_DISABLE_DRIVER_CAMERA': '1', 'PANDA_USB_READONLY': '0'}):
    assert not process_config.driverview(False, params, None)
    assert not process_config.webcam(False, params, None)
    assert process_config.driverview(True, params, None)
    assert process_config.webcam(True, params, None)


def test_disable_wins_over_configured_cabin_camera_and_keeps_road_model():
  try:
    with patch.dict(os.environ, {'SUNNYPILOT_DISABLE_DRIVER_CAMERA': '1', 'DRIVER_CAM': '/dev/video2', 'USE_WEBCAM': '1'}):
      procs = importlib.reload(process_config).managed_processes
      assert not procs['dmonitoringd'].enabled
      assert not procs['dmonitoringmodeld'].enabled
      assert procs['webcamerad'].enabled
      assert procs['modeld'].enabled
  finally:
    importlib.reload(process_config)


def test_explicit_configured_driver_camera_restores_monitoring_processes():
  # A configured DRIVER_CAM is no longer enough on its own: the manager now asks
  # camera_config.driver_camera_available(), which validates that the device exists
  # and is not the road camera. This test is about the gating, so the validation is
  # stubbed; the validation itself is covered by test_driver_camera_available.py.
  from openpilot.common import camera_config
  try:
    with patch.dict(os.environ, {'SUNNYPILOT_DISABLE_DRIVER_CAMERA': '0', 'DRIVER_CAM': '/dev/video2', 'USE_WEBCAM': '1'}), \
         patch('openpilot.system.camerad.webcam.driver_camera_setup.validate_driver_camera'):
      camera_config.driver_camera_available.cache_clear()
      procs = importlib.reload(process_config).managed_processes
      assert procs['dmonitoringd'].enabled
      assert procs['dmonitoringmodeld'].enabled
      assert procs['webcamerad'].enabled
      assert procs['modeld'].enabled
  finally:
    camera_config.driver_camera_available.cache_clear()
    importlib.reload(process_config)


def test_webcam_constructs_only_road_captures_when_cabin_disabled():
  from openpilot.system.camerad.webcam import camerad

  camera = Mock(output_w=128, output_h=64, nv12_size=12288, nv12_stride=128, nv12_y_height=64)
  try:
    with patch.dict(os.environ, {'SUNNYPILOT_DISABLE_DRIVER_CAMERA': '1', 'DRIVER_CAM': '/dev/video2',
                                'NARROW_ROAD_CAM': '/dev/video0', 'WIDE_CAM': '/dev/video1'}), \
         patch('openpilot.common.camera_config.wide_camera_available', return_value=True):
      importlib.reload(camerad)
      with patch.object(camerad, 'Camera', return_value=camera) as factory, \
           patch.object(camerad, 'VisionIpcServer'), patch.object(camerad.messaging, 'PubMaster') as pub:
        camerad.Camerad()
      assert [call.args[2] for call in factory.call_args_list] == ['/dev/video0', '/dev/video1']
      pub.assert_called_once_with(['narrowRoadCameraState', 'wideRoadCameraState'])
  finally:
    importlib.reload(camerad)


def test_remote_cabin_request_does_not_disturb_existing_road_session():
  from openpilot.system.webrtc.webrtcd import handle_get_stream

  existing = Mock()
  # Reject before taking a lock or closing a working road-video connection.
  state = SimpleNamespace(streams={'road': existing}, stream_lock=None)
  body = json.dumps({'sdp': '', 'cameras': ['driver'], 'enabled': True}).encode()
  with patch.dict(os.environ, {'SUNNYPILOT_DISABLE_DRIVER_CAMERA': '1'}):
    status, response, _ = asyncio.run(handle_get_stream(state, body, 'application/json'))
  assert status == 403
  assert 'disabled' in json.loads(response)['error']
  existing.stop.assert_not_called()


def test_stream_switch_to_disabled_cabin_preserves_road_socket():
  from openpilot.system.webrtc.device.video import LiveStreamVideoStreamTrack

  track = LiveStreamVideoStreamTrack.__new__(LiveStreamVideoStreamTrack)
  old_socket = track._sock = object()
  with patch.dict(os.environ, {'SUNNYPILOT_DISABLE_DRIVER_CAMERA': '1'}):
    with pytest.raises(ValueError, match='disabled'):
      track.switch_camera('driver')
  assert track._sock is old_socket


def test_road_stream_subscription_works_with_cabin_disabled():
  from openpilot.system.webrtc.device import video

  track = video.LiveStreamVideoStreamTrack.__new__(video.LiveStreamVideoStreamTrack)
  with patch.dict(os.environ, {'SUNNYPILOT_DISABLE_DRIVER_CAMERA': '1'}), patch.object(video.messaging, 'sub_sock') as subscribe:
    track._make_sock('road')
  subscribe.assert_called_once_with('livestreamNarrowRoadEncodeData', conflate=True)
