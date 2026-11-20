"""Read-only validation before opting a separate Linux webcam into driver monitoring."""
import argparse
import fcntl
import os
import stat
import struct
from pathlib import Path


VIDIOC_QUERYCAP = 0x80685600
V4L2_CAP_VIDEO_CAPTURE = 0x00000001
V4L2_CAP_DEVICE_CAPS = 0x80000000
V4L2_CAP_STREAMING = 0x04000000
CAPABILITY = struct.Struct('=16s32s32sIII12s')


def capture_bus(device: str) -> str:
  path = Path(f'/dev/video{device}' if device.isdigit() else device)
  if not stat.S_ISCHR(path.stat().st_mode):
    raise ValueError(f'Not a video device: {path}')
  fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
  try:
    data = bytearray(CAPABILITY.size)
    fcntl.ioctl(fd, VIDIOC_QUERYCAP, data)
    _, _, bus, _, capabilities, device_caps, _ = CAPABILITY.unpack(data)
    caps = device_caps if capabilities & V4L2_CAP_DEVICE_CAPS else capabilities
    if not caps & V4L2_CAP_VIDEO_CAPTURE or not caps & V4L2_CAP_STREAMING:
      raise ValueError(f'Not a streaming video capture node (possibly metadata): {path}')
    return bus.partition(bytes([0]))[0].decode('utf-8', errors='replace')
  finally:
    os.close(fd)


def validate_driver_camera(road: str, driver: str) -> None:
  if not driver:
    raise ValueError('DRIVER_CAM must identify a separate driver-facing camera')

  def device_path(device):
    return Path(f'/dev/video{device}' if device.isdigit() else device).resolve(strict=True)

  road_path, driver_path = device_path(road), device_path(driver)
  if road_path == driver_path or road_path.stat().st_rdev == driver_path.stat().st_rdev:
    raise ValueError('Road and driver camera must be separate devices')
  road_bus, driver_bus = capture_bus(str(road_path)), capture_bus(str(driver_path))
  if not road_bus or not driver_bus or road_bus == driver_bus:
    raise ValueError('Cannot verify separate road and driver camera hardware')


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--road', required=True)
  parser.add_argument('--driver', required=True)
  args = parser.parse_args()
  try:
    validate_driver_camera(args.road, args.driver)
  except (OSError, ValueError) as exc:
    parser.exit(1, f'Driver camera configuration error: {exc}' + chr(10))
  print('Separate video capture device verified; mounting and monitoring still require validation.')


if __name__ == '__main__':
  main()
