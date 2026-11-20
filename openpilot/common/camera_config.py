"""Camera availability for capture and UI; this does not bypass driving checks."""
import functools
import os
import platform

from openpilot.common import hardware
from openpilot.common.swaglog import cloudlog


DRIVER_CAMERA_PARAMS = ('IsDriverViewEnabled', 'RecordFront', 'AlwaysOnDM')
# The names camerad.py reads to decide which camera feeds the cabin stream. Keep
# them in sync: the UI must not offer a preview for a camera that capture never
# opened, and DM must not wait on frames that will never arrive.
NARROW_ROAD_CAM_ENV = 'NARROW_ROAD_CAM'
ROAD_CAM_ENV = 'ROAD_CAM'
WIDE_CAM_ENV = 'WIDE_CAM'
DRIVER_CAM_ENV = 'DRIVER_CAM'


def driver_camera_enabled() -> bool:
  return os.getenv('SUNNYPILOT_DISABLE_DRIVER_CAMERA', '0') != '1'


def narrow_road_cam() -> str:
  """The road camera camerad uses, resolved exactly like camerad.py does."""
  return os.getenv(NARROW_ROAD_CAM_ENV, os.getenv(ROAD_CAM_ENV, '0'))


def camera_device_path(cam: str) -> str:
  """Resolve a camera id the same way camerad's `camera_device()` does.

  A bare index ("0") means /dev/video0 on Linux; a by-id path is kept verbatim.
  """
  return f"/dev/video{cam}" if platform.system() != 'Darwin' and cam.isdigit() else cam


def _same_device(a: str, b: str) -> bool:
  try:
    return os.path.realpath(camera_device_path(a)) == os.path.realpath(camera_device_path(b))
  except OSError:
    return False


def wide_camera_configured() -> bool:
  """Whether a second road camera was requested for this launch.

  Single/dual is decided from the hardware that is actually present, not from a
  settings switch: the launcher leaves WIDE_CAM set only when the configured
  device exists, and this environment check is what the capture process and the
  UI read. SUNNYPILOT_DISABLE_WIDE_CAMERA=1 remains the explicit force-single
  override.
  """
  if os.getenv('SUNNYPILOT_DISABLE_WIDE_CAMERA', '0') == '1':
    return False
  return bool(os.getenv(WIDE_CAM_ENV))


def wide_camera_available() -> bool:
  """Whether the configured second road camera exists and is a separate device.

  This is deliberately the same predicate the launcher and camerad use, so the
  settings page cannot offer wide-camera controls when capture will run single,
  and camerad never opens the narrow camera twice under two stream names.
  """
  if not wide_camera_configured():
    return False

  wide_cam = os.getenv(WIDE_CAM_ENV, '')
  narrow_cam = narrow_road_cam()
  # Same device under two names is the failure mode a lens/port swap produces:
  # opening it twice either fails with ENOSPC or, worse, feeds the model the same
  # view on both inputs.
  if narrow_cam and _same_device(wide_cam, narrow_cam):
    return False

  if platform.system() != 'Linux':
    # Device discovery is Linux/V4L2-only; elsewhere keep the historical
    # behaviour of trusting the configured id.
    return True
  return os.path.exists(camera_device_path(wide_cam))


def _probe_driver_camera() -> bool:
  """Whether a driver-facing camera is configured and can actually be opened.

  On a comma device the cabin camera is built in, so only the disable switch can
  take it away. On PC it is an ordinary USB camera chosen by DRIVER_CAM, and that
  camera is worthless if it is missing, if it is the road camera under another
  name, or if it cannot be queried - camerad drops a rejected DRIVER_CAM, so the
  rest of the stack has to agree with that decision instead of assuming cabin
  frames will appear.
  """
  if not driver_camera_enabled():
    return False
  # Read through the module so a test that patches openpilot.common.hardware.PC is
  # seen here too, the same way the manager and selfdrived tests do it.
  if not hardware.PC:
    return True

  driver_cam = os.getenv(DRIVER_CAM_ENV)
  if not driver_cam:
    return False
  if platform.system() != 'Linux':
    # The separation check is Linux/V4L2-only, and camerad keeps DRIVER_CAM as-is
    # on other platforms, so there is nothing here to disagree with.
    return True

  from openpilot.system.camerad.webcam.driver_camera_setup import validate_driver_camera
  validate_driver_camera(narrow_road_cam(), driver_cam)
  return True


@functools.lru_cache(maxsize=1)
def driver_camera_available() -> bool:
  """Cached: one answer for camerad, the manager, selfdrived and the UI.

  Every consumer used to re-derive this from DRIVER_CAM plus its own PC/webcam
  conditions, so a camera that camerad had already rejected still got a preview
  button and still got driver monitoring started. The probe opens devices, so it
  runs once and any failure is reported once and answers False - failing closed
  is what the rest of the stack already does when cabin frames are missing.
  """
  try:
    return _probe_driver_camera()
  except Exception as e:
    cloudlog.error(f"driver camera unavailable ({e}); driver monitoring disabled")
    return False


def apply_driver_camera_settings(params) -> None:
  if not driver_camera_enabled():
    for key in DRIVER_CAMERA_PARAMS:
      params.put_bool(key, False, block=True)
