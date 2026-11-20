"""Lane dashes and blind-spot geometry shared by the onroad and mici displays.

Ported from CarrotPilot's ``openpilot/selfdrive/ui/road_markings.py``. The
functions are pure NumPy so the rendering code can be exercised without a
display, and both UI variants use the same dash spacing and barrier shape.
"""

import math

import numpy as np

LANE_DASH_LENGTH_M = 5.2
LANE_DASH_GAP_M = 4.2


def lane_type_is_dashed(lane_code: int) -> bool:
  """True when a CarStateSP lane-type code means a dashed marking.

  The code is ``-1`` unknown, ``0`` dashed, ``1`` solid, and may carry a colour
  decade (10/20/30) on cars that provide one. Matching CarrotPilot's renderer,
  only the last digit decides the dash pattern; negative/unknown stays solid so
  the legacy geometry remains visible.
  """
  return lane_code >= 0 and lane_code % 10 == 0


def lane_dash_segments(line: np.ndarray, max_distance: float) -> list[np.ndarray]:
  """Split a 3-D lane line into 5.2 m dashes with 4.2 m gaps."""
  if line.shape[0] < 2:
    return []

  x = line[:, 0]
  start_distance = max(0.0, float(x[0]))
  end_distance = min(max_distance, float(x[-1]))
  if end_distance <= start_distance:
    return []

  boundaries = []
  cycle_distance = LANE_DASH_LENGTH_M + LANE_DASH_GAP_M
  cursor = math.floor(start_distance / cycle_distance) * cycle_distance
  while cursor < end_distance:
    dash_start = max(cursor, start_distance)
    dash_end = min(cursor + LANE_DASH_LENGTH_M, end_distance)
    if dash_end > dash_start:
      boundaries.append((dash_start, dash_end))
    cursor += cycle_distance
  if not boundaries:
    return []

  # Interpolate all dash endpoints together instead of four np.interp calls per
  # short dash on every camera frame.
  distances = np.asarray(boundaries).ravel()
  endpoints = np.empty((len(distances), 3), dtype=line.dtype)
  endpoints[:, 0] = distances
  endpoints[:, 1] = np.interp(distances, x, line[:, 1])
  endpoints[:, 2] = np.interp(distances, x, line[:, 2])
  segments = []
  for i, (dash_start, dash_end) in enumerate(boundaries):
    inside = line[(x > dash_start) & (x < dash_end)]
    segments.append(np.concatenate((endpoints[2 * i:2 * i + 1], inside, endpoints[2 * i + 1:2 * i + 2])))
  return segments


def project_lane_segments(segments: list[np.ndarray], half_width: float, transform: np.ndarray, clip) -> list[np.ndarray]:
  """Project independent dash ribbons in one batch, preserving their gaps."""
  if not segments:
    return []
  lengths = [len(segment) for segment in segments]
  points = np.concatenate(segments)
  n = len(points)
  offsets = np.array([[0., -half_width, 0.], [0., half_width, 0.]], dtype=np.float32)
  sides = (points[None, :, :] + offsets[:, None, :]).reshape(2 * n, 3)
  projected = (transform @ sides.T).reshape(3, 2, n)
  depth_ok = np.abs(projected[2]) >= 1e-6
  xy = np.divide(projected[:2], projected[2:3], out=np.full_like(projected[:2], np.nan), where=depth_ok[None, :, :])
  valid = ((points[:, 0] >= 0) & depth_ok.all(axis=0)
           & ((xy[0] >= clip.x) & (xy[0] <= clip.x + clip.width)
              & (xy[1] >= clip.y) & (xy[1] <= clip.y + clip.height)).all(axis=0))
  result = []
  start = 0
  for length in lengths:
    end = start + length
    sides_xy = xy[:, :, start:end][:, :, valid[start:end]]
    if sides_xy.shape[2]:
      result.append(np.concatenate((sides_xy[:, 0].T, sides_xy[:, 1, ::-1].T)).astype(np.float32))
    start = end
  return result


def project_blindspot_barrier(points: np.ndarray, y_shift: float, transform: np.ndarray, clip) -> np.ndarray:
  """Project the two edges of a blind-spot barrier along the path."""
  points = points[points[:, 0] >= 0]
  if points.shape[0] == 0:
    return np.empty((0, 2), dtype=np.float32)

  offsets = np.array(
    [[0.0, y_shift, 1.15], [0.0, y_shift, 0.6]],
    dtype=np.float32,
  )
  points_3d = points[None, :, :] + offsets[:, None, :]
  projected = (
    transform[:, 0, None, None] * points_3d[None, :, :, 0] +
    transform[:, 1, None, None] * points_3d[None, :, :, 1] +
    transform[:, 2, None, None] * points_3d[None, :, :, 2]
  )
  upper_projected = projected[:, 0, :]
  lower_projected = projected[:, 1, :]

  valid_depth = (np.abs(upper_projected[2]) >= 1e-6) & (np.abs(lower_projected[2]) >= 1e-6)
  if not np.any(valid_depth):
    return np.empty((0, 2), dtype=np.float32)

  upper_screen = upper_projected[:2, valid_depth] / upper_projected[2, valid_depth][None, :]
  lower_screen = lower_projected[:2, valid_depth] / lower_projected[2, valid_depth][None, :]

  x_min, x_max = clip.x, clip.x + clip.width
  y_min, y_max = clip.y, clip.y + clip.height
  upper_in_clip = (
    (upper_screen[0] >= x_min) & (upper_screen[0] <= x_max) &
    (upper_screen[1] >= y_min) & (upper_screen[1] <= y_max)
  )
  lower_in_clip = (
    (lower_screen[0] >= x_min) & (lower_screen[0] <= x_max) &
    (lower_screen[1] >= y_min) & (lower_screen[1] <= y_max)
  )
  both_in_clip = upper_in_clip & lower_in_clip
  if not np.any(both_in_clip):
    return np.empty((0, 2), dtype=np.float32)

  upper_screen = upper_screen[:, both_in_clip]
  lower_screen = lower_screen[:, both_in_clip]

  # Match the old hill/inversion filter: keep a point only when its upper screen
  # Y does not increase relative to the last accepted point.
  if upper_screen.shape[1] > 1:
    keep = upper_screen[1] == np.minimum.accumulate(upper_screen[1])
    upper_screen = upper_screen[:, keep]
    lower_screen = lower_screen[:, keep]

  if upper_screen.shape[1] == 0:
    return np.empty((0, 2), dtype=np.float32)
  return np.vstack((upper_screen.T, lower_screen[:, ::-1].T)).astype(np.float32)


def blindspot_barrier_quads(points: np.ndarray) -> np.ndarray:
  """Split the projected barrier ribbon into drawable quads."""
  if points.size == 0:
    return np.empty((0, 4, 2), dtype=np.float32)

  count = points.shape[0]
  half = count // 2
  if half < 3:
    return np.empty((0, 4, 2), dtype=np.float32)

  starts = np.arange(0, half - 2, 2)
  if starts.size == 0:
    return np.empty((0, 4, 2), dtype=np.float32)

  quads = np.stack(
    (
      points[starts],
      points[starts + 1],
      points[count - starts - 3],
      points[count - starts - 2],
    ),
    axis=1,
  )
  centers = np.mean(quads, axis=1, keepdims=True)
  angles = np.arctan2(quads[:, :, 1] - centers[:, :, 1], quads[:, :, 0] - centers[:, :, 0])
  order = np.argsort(angles, axis=1)
  return np.take_along_axis(quads, order[:, :, None], axis=1)


def blind_spot_draw_state(car_state, radar_state, pre_lane_change: bool, lane_change_direction, v_ego: float):
  """Return ``(left_bsd, right_bsd, left_assist, right_assist)``.

  The amber BSD state comes from the vehicle (or xiaoge) blind-spot flag. Radar
  only adds a green assist hint while a lane change is pending in that direction
  and the side track is within three seconds of travel.
  """
  left_blindspot = bool(getattr(car_state, "leftBlindspot", False))
  right_blindspot = bool(getattr(car_state, "rightBlindspot", False))
  direction = str(lane_change_direction).lower()
  assist_distance = float(v_ego) * 3.0

  lead_left = getattr(radar_state, "leadLeft", None) if radar_state is not None else None
  lead_right = getattr(radar_state, "leadRight", None) if radar_state is not None else None

  left_assist = bool(
    pre_lane_change and "left" in direction and not left_blindspot and lead_left is not None
    and getattr(lead_left, "status", False) and float(getattr(lead_left, "dRel", 1e9)) < assist_distance
  )
  right_assist = bool(
    pre_lane_change and "right" in direction and not right_blindspot and lead_right is not None
    and getattr(lead_right, "status", False) and float(getattr(lead_right, "dRel", 1e9)) < assist_distance
  )
  return left_blindspot, right_blindspot, left_assist, right_assist
