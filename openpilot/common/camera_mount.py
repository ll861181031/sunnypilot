"""Camera mount geometry shared by the calibration alerts and the aiming guide.

`rpyCalib` uses the same sign convention as the device settings page: positive
yaw means the camera points left and positive pitch means it points down. Both
the wording and the on-screen marker are therefore expressed in the direction
the camera has to be moved, which is the opposite of where it currently points.
"""

import math

# Below this the camera is pointing close enough that telling the driver to move
# it would be noise rather than guidance.
MOUNT_ADJUST_MIN_DEG = 0.2


def camera_mount_adjustment(yaw: float, pitch: float) -> str:
  """How far to turn the camera, e.g. 'Turn camera left 4.2°, tilt up 0.5°'.

  Takes the correction still required in radians, not the mount angle, so an
  axis calibrationd already accepts contributes nothing.
  """
  parts = []
  for axis, degrees in camera_mount_adjustment_parts(yaw, pitch):
    if axis == 'left':
      parts.append(f"Turn camera left {degrees:.1f}°")
    elif axis == 'right':
      parts.append(f"Turn camera right {degrees:.1f}°")
    else:
      parts.append(f"tilt {axis} {degrees:.1f}°")
  return ", ".join(parts)


def camera_mount_adjustment_parts(yaw: float, pitch: float) -> list[tuple[str, float]]:
  """Axis corrections still required as ('left'|'right'|'up'|'down', degrees).

  Structured form so the UI can rebuild the sentence from translated catalog
  templates: Chinese text composed server-side would miss the UI's loaded glyph
  set, while catalog translations are covered by the font preparation step.
  """
  parts: list[tuple[str, float]] = []
  yaw_deg = math.degrees(yaw)
  if abs(yaw_deg) > MOUNT_ADJUST_MIN_DEG:
    parts.append(('right' if yaw_deg > 0 else 'left', abs(yaw_deg)))
  pitch_deg = math.degrees(pitch)
  if abs(pitch_deg) > MOUNT_ADJUST_MIN_DEG:
    parts.append(('up' if pitch_deg > 0 else 'down', abs(pitch_deg)))
  return parts


def aim_offset_px(yaw: float, pitch: float, focal_px: float) -> tuple[float, float]:
  """Pixel offset of the straight-ahead direction from the image centre.

  Positive x is right and positive y is down. A camera pointing left shows the
  road ahead to the right of centre, and a camera pointing down shows it above
  centre, so the marker moves the same way the camera has to be turned and the
  driver can simply chase it until it reaches the fixed crosshair.
  """
  return focal_px * math.tan(yaw), -focal_px * math.tan(pitch)
