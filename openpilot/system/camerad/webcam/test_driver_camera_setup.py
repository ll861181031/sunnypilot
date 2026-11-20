import stat
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from openpilot.system.camerad.webcam import driver_camera_setup as setup


@pytest.mark.parametrize('capabilities,device_caps,accepted', [
  (setup.V4L2_CAP_VIDEO_CAPTURE | setup.V4L2_CAP_STREAMING, 0, True),
  (setup.V4L2_CAP_DEVICE_CAPS, setup.V4L2_CAP_VIDEO_CAPTURE | setup.V4L2_CAP_STREAMING, True),
  (setup.V4L2_CAP_DEVICE_CAPS | setup.V4L2_CAP_VIDEO_CAPTURE, setup.V4L2_CAP_STREAMING | 0x00800000, False),
  (setup.V4L2_CAP_VIDEO_CAPTURE, 0, False),
])
def test_query_uses_node_capabilities_and_rejects_metadata(capabilities, device_caps, accepted):
  def query(fd, request, data):
    assert request == setup.VIDIOC_QUERYCAP
    data[:] = setup.CAPABILITY.pack(b'uvcvideo', b'camera', b'usb-1-2', 0, capabilities, device_caps, bytes(12))

  with patch.object(setup.Path, 'stat', return_value=SimpleNamespace(st_mode=stat.S_IFCHR)), \
       patch.object(setup.os, 'open', return_value=123) as open_device, patch.object(setup.os, 'close') as close, \
       patch.object(setup.fcntl, 'ioctl', side_effect=query):
    if accepted:
      assert setup.capture_bus('/dev/video2') == 'usb-1-2'
    else:
      with pytest.raises(ValueError, match='capture node'):
        setup.capture_bus('/dev/video2')
    open_device.assert_called_once_with(setup.Path('/dev/video2'), setup.os.O_RDONLY | setup.os.O_NONBLOCK)
    close.assert_called_once_with(123)


def test_regular_files_and_missing_driver_are_rejected(tmp_path):
  file = tmp_path / 'video'
  file.write_bytes(b'not a camera')
  with pytest.raises(ValueError, match='video device'):
    setup.capture_bus(str(file))
  with pytest.raises(ValueError, match='DRIVER_CAM'):
    setup.validate_driver_camera('/dev/video0', '')


@pytest.mark.parametrize('road_bus,driver_bus,accepted', [
  ('usb-road', 'usb-driver', True), ('usb-road', 'usb-road', False), ('usb-road', '', False),
])
def test_both_cameras_must_be_separate_hardware(road_bus, driver_bus, accepted):
  road, driver = Mock(), Mock()
  road.stat.return_value.st_rdev = 1
  driver.stat.return_value.st_rdev = 2
  with patch.object(setup.Path, 'resolve', side_effect=[road, driver]), \
       patch.object(setup, 'capture_bus', side_effect=[road_bus, driver_bus]):
    if accepted:
      setup.validate_driver_camera('0', '2')
    else:
      with pytest.raises(ValueError, match='separate'):
        setup.validate_driver_camera('0', '2')


def test_alias_of_road_device_is_rejected_before_opening():
  road, alias = Mock(), Mock()
  road.stat.return_value.st_rdev = alias.stat.return_value.st_rdev = 1
  with patch.object(setup.Path, 'resolve', side_effect=[road, alias]), patch.object(setup, 'capture_bus') as query:
    with pytest.raises(ValueError, match='separate devices'):
      setup.validate_driver_camera('/dev/video0', '/dev/v4l/by-id/road')
    query.assert_not_called()
