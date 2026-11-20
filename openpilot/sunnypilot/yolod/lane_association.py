"""Decide which detected traffic light governs ego's lane.

A junction usually shows several heads at once - a left-turn arrow, a through
light, a right-turn light - and at fHugh40 in this dataset one frame produced a
confident red and a confident green side by side. Acting on "any red anywhere"
would brake for the crossing lane's light. Only the head above ego's own lane may
veto the planner, so that is what this module answers.

The test runs in angle, not in image rows, for a reason worth writing down. A
traffic light sits ~5-6 m above the road, so it never appears at the image row
where its lane is drawn - comparing at "the same row" would compare a lamp
against a piece of tarmac 200 m away. What does carry over is the horizontal
angle: with ``view_x = -Y`` and ``view_z = X`` in the calib frame,

    rho := (u - cx) / f = -Y / X

so a lamp's horizontal pixel position is its lateral slope, independent of how
high it is mounted and (to first order) of camera pitch. The ego lane becomes a
band of slopes over the distances where lights actually matter, and a detection
belongs to it when its slope falls inside that band.

This also sidesteps needing the lamp's distance, which a monocular box cannot
give reliably. Distance is still estimated from apparent size, but only to
*rank* candidates and to gate implausible ones, never to decide membership.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np

# The ego lane is bounded by laneLines[1] and laneLines[2]; [0] and [3] are the
# neighbouring lanes' outer edges. Kept as constants rather than positional
# literals so the assumption is visible at the call site.
EGO_LEFT_INDEX = 1
EGO_RIGHT_INDEX = 2

# Distances over which a traffic light can plausibly be governing. Closer than
# the near bound the driver is already committed; beyond the far bound the lane
# band is so narrow in angle that a one-lane offset is unmeasurable.
NEAR_DISTANCE_M = 18.0
FAR_DISTANCE_M = 90.0

# Extra slack on each side of the lane band, in **metres**. Head geometry, lamp
# width and the model's own lane error all push a real ego-lane light slightly
# outside the pure road band, so a hard edge would reject correct assignments.
#
# It is deliberately a distance, not an angle. An angular margin converts to a
# lateral slack that grows with range: 0.030 rad is 0.6 m at 20 m but 2.4 m at
# 80 m, which is a whole extra lane on each side. That is what let lights over the
# neighbouring lane pass as ego's at long range.
#
# 1.0 m is set from measurement, not taste. On a real 1200-frame segment the lights
# the old logic accepted sat as close as 1.47 m from ego's lane centre; a 0.54 m
# margin rejected that one by 15 cm, which would be a missed red light. 1.0 m covers
# lamp housing width plus the model's own lane error, and still rejects a light one
# lane over (3.5 m) at every range from 20 m out.
MARGIN_METRES = 1.0

# Kept for callers that still think in radians at the near bound; equal to
# MARGIN_METRES / NEAR_DISTANCE_M.
ANGLE_MARGIN_RAD = MARGIN_METRES / NEAR_DISTANCE_M

# Hard ceiling on how far to the side a light may sit and still count as ego's,
# regardless of what the lane lines claim. The band above already lands near
# 2.3 m half-width with sane lane lines; this only bites when the model reports
# lanes that are too wide or drifting, where trusting them would put a
# neighbouring lane's light in charge of the car. 3.0 m is a 6 m lane - generous
# for any real road, and still short of the second lane over.
MAX_BAND_HALF_WIDTH_M = 3.0

# A vertical Chinese 3-lamp head is roughly 1.0 m tall including housing; a
# horizontal one is about 1.0 m wide. Only used for ranking, so being 30% off
# changes nothing important.
ASSUMED_HEAD_HEIGHT_M = 1.0

# Ranging sanity bounds. A 4 px tall blob implies 466 m at this focal length,
# which would give a band 0.0075 rad wide and reject lights that really are over
# ego's lane. Outside this window the size estimate carries no information, so it
# is reported as unknown and the caller falls back to the wide envelope.
MIN_RANGE_M = 8.0
MAX_RANGE_M = 150.0
MIN_RANGE_HEIGHT_PX = 6.0


@dataclass(frozen=True)
class LaneEvidence:
  """One detection with the geometry that decided its lane membership."""
  index: int
  slope: float
  distance_m: float | None
  in_ego_band: bool
  centre_offset: float      # |slope - band centre|; lower is more likely ego's


@dataclass(frozen=True)
class LaneAssignment:
  ego_index: int | None             # index into the input detections
  evidence: tuple[LaneEvidence, ...]
  band: tuple[float, float] | None  # (min, max) slope of the ego lane band


def _as_float_array(values) -> np.ndarray:
  return np.asarray(list(values) if not isinstance(values, np.ndarray) else values,
                    dtype=np.float64)


def lane_slope(x, y) -> np.ndarray:
  """rho = -y/x for every lane point, ignoring points behind or beside the car."""
  x = _as_float_array(x)
  y = _as_float_array(y)
  n = min(x.size, y.size)
  if n == 0:
    return np.empty(0)
  x, y = x[:n], y[:n]
  valid = (x > 1.0) & np.isfinite(x) & np.isfinite(y)
  if not np.any(valid):
    return np.empty(0)
  return -y[valid] / x[valid]


def _ego_boundaries(lane_lines):
  """The (left, right) boundary arrays bounding ego's lane, or None."""
  if lane_lines is None or len(lane_lines) == 0:
    return None
  if len(lane_lines) >= 4:
    left, right = lane_lines[EGO_LEFT_INDEX], lane_lines[EGO_RIGHT_INDEX]
  elif len(lane_lines) == 2:
    left, right = lane_lines
  else:
    return None
  left_x, left_y = _as_float_array(left[0]), _as_float_array(left[1])
  right_x, right_y = _as_float_array(right[0]), _as_float_array(right[1])
  n = min(left_x.size, left_y.size, right_x.size, right_y.size)
  if n == 0:
    return None
  return left_x[:n], left_y[:n], right_x[:n], right_y[:n]


def ego_band_at(lane_lines, distance: float,
                margin_m: float = MARGIN_METRES) -> tuple[float, float] | None:
  """Slope interval of ego's lane at one specific distance.

  This is the variant that matters. Taking the union over all distances instead
  would report the *near* field's width, and at 90 m a neighbouring lane sits only
  0.058 rad off centre - well inside the near-field envelope of 0.097 rad. Every
  far-away light would then look like it was over ego's lane. Narrowing the band
  to the light's own estimated distance is what makes the adjacent lanes
  separable at all.
  """
  if not np.isfinite(distance) or distance <= 0.0:
    return None
  bounds = _ego_boundaries(lane_lines)
  if bounds is None:
    return None
  left_x, left_y, _, right_y = bounds
  valid = np.isfinite(left_x) & np.isfinite(left_y) & np.isfinite(right_y) & (left_x > 1.0)
  if not np.any(valid):
    return None
  xs, ly, ry = left_x[valid], left_y[valid], right_y[valid]
  if xs.size < 2:
    return None
  order = np.argsort(xs)
  xs, ly, ry = xs[order], ly[order], ry[order]
  left_at = float(np.interp(distance, xs, ly))
  right_at = float(np.interp(distance, xs, ry))
  # Metres -> radians at this specific range, so the slack stays physically
  # constant instead of widening with distance.
  margin = margin_m / distance
  lo = min(-right_at, -left_at) / distance - margin
  hi = max(-right_at, -left_at) / distance + margin
  # Clamp in lateral metres so a bogus lane width cannot widen the gate.
  half_limit = MAX_BAND_HALF_WIDTH_M / distance
  centre = 0.5 * (lo + hi)
  lo = max(lo, centre - half_limit)
  hi = min(hi, centre + half_limit)
  return (lo, hi)


def ego_band(lane_lines, near: float = NEAR_DISTANCE_M, far: float = FAR_DISTANCE_M,
             margin_m: float = MARGIN_METRES) -> tuple[float, float] | None:
  """Widest slope interval ego's lane covers between ``near`` and ``far`` metres.

  Used only when a detection's distance cannot be estimated, where the honest
  answer is "somewhere in this envelope" rather than a specific width.
  """
  bounds = _ego_boundaries(lane_lines)
  if bounds is None:
    return None
  left_x, left_y, _, right_y = bounds
  valid = np.isfinite(left_x) & np.isfinite(left_y) & np.isfinite(right_y) & (left_x > 1.0)
  window = valid & (left_x >= near) & (left_x <= far)
  if not np.any(window):
    window = valid
  if not np.any(window):
    return None
  lx, ly, ry = left_x[window], left_y[window], right_y[window]
  slopes = np.concatenate([-ly / lx, -ry / lx])
  # Same physical slack as ego_band_at, applied at the nearest distance in the
  # window so the envelope stays the permissive one this function exists to give.
  margin = margin_m / max(float(lx.min()), 1.0)
  return (float(slopes.min()) - margin, float(slopes.max()) + margin)


def box_slope(box, focal: float, cx: float, axis_slope: float = 0.0) -> float:
  """Horizontal slope of a pixel box centre, relative to the direction of travel.

  ``axis_slope`` is the image slope of the calib frame's forward axis, which is
  not the image centre once the camera is yawed. On this device that offset is
  0.0094 rad - the same size as the ego lane band at 90 m, so leaving it out
  would bias every measurement toward one side.
  """
  x1, _, x2, _ = box
  return float((0.5 * (x1 + x2) - cx) / max(focal, 1e-6) - axis_slope)


def forward_slope(rpy_calib) -> float:
  """Image slope of the direction of travel, from calibration roll/pitch/yaw.

  Returns 0.0 for a camera that looks exactly where the car is going, which is
  also the safe answer when calibration is unavailable.
  """
  values = _as_float_array(rpy_calib)
  if values.size != 3 or not np.all(np.isfinite(values)):
    return 0.0
  # Imported here so the module stays usable without the transformation stack.
  from openpilot.common.transformations.camera import view_frame_from_device_frame
  from openpilot.common.transformations.orientation import rot_from_euler

  rotation = view_frame_from_device_frame @ rot_from_euler(values)
  ahead = rotation @ np.array([1.0, 0.0, 0.0])
  if ahead[2] <= 1e-6:
    return 0.0
  return float(ahead[0] / ahead[2])


def estimate_distance(box, focal: float, assumed_height_m: float = ASSUMED_HEAD_HEIGHT_M) -> float | None:
  """Rough range from apparent size, or None when the box is too small to range.

  A membership test built on a bad range is worse than no range at all, so
  anything outside the plausible window is reported as unknown on purpose.
  """
  _, y1, _, y2 = box
  h_px = abs(y2 - y1)
  if h_px < MIN_RANGE_HEIGHT_PX:
    return None
  distance = focal * assumed_height_m / h_px
  if not np.isfinite(distance) or not (MIN_RANGE_M <= distance <= MAX_RANGE_M):
    return None
  return float(distance)


def assign(detections, lane_lines, focal: float, cx: float,
           states=None, preferred_state: str | None = None,
           axis_slope: float = 0.0) -> LaneAssignment:
  """Pick the detection that sits over ego's lane.

  ``detections`` is a sequence of pixel boxes ``(x1, y1, x2, y2)``; ``states`` an
  optional parallel sequence of lamp states. ``preferred_state`` breaks ties in
  favour of a given state, which is how a caller asks "is *my* light red" rather
  than "is any light red".

  Each detection gets its own band at its own estimated distance. A detection too
  small to range falls back to the union band, which is permissive but never
  silently reports "no ego light" just because the head was far away.
  """
  boxes = list(detections)
  if lane_lines is None or not boxes:
    return LaneAssignment(ego_index=None, evidence=(), band=None)

  union = ego_band(lane_lines)
  if union is None:
    return LaneAssignment(ego_index=None, evidence=(), band=None)

  evidence = []
  for index, box in enumerate(boxes):
    slope = box_slope(box, focal, cx, axis_slope)
    distance = estimate_distance(box, focal)
    band = ego_band_at(lane_lines, distance) if distance is not None else None
    if band is None:
      # No range: use the widest envelope this lane can cover.
      band = union
    lo, hi = band
    centre = 0.5 * (lo + hi)
    evidence.append(LaneEvidence(index=index, slope=slope, distance_m=distance,
                                 in_ego_band=lo <= slope <= hi,
                                 centre_offset=abs(slope - centre)))

  candidates = [e for e in evidence if e.in_ego_band]
  if not candidates:
    return LaneAssignment(ego_index=None, evidence=tuple(evidence), band=union)

  def rank(item: LaneEvidence):
    state_penalty = 0.0
    if preferred_state is not None and states is not None:
      state = states[item.index] if item.index < len(states) else None
      state_penalty = 0.0 if state == preferred_state else 1.0
    # Right state first, then the NEAREST light over ego's lane, then the most
    # centred. Nearest first is deliberate: the light that governs this lane is the
    # one being approached, and the farther heads of the same gantry are either for
    # other lanes or for a junction beyond. Ranking by centre offset let a far head
    # win whenever it happened to sit a few pixels closer to the lane centre, which
    # on a real night approach made the reported range jump 10 m between frames and
    # flip which physical head was tracked.
    return (state_penalty,
            item.distance_m if item.distance_m is not None else 1e9,
            item.centre_offset)

  best = min(candidates, key=rank)
  return LaneAssignment(ego_index=best.index, evidence=tuple(evidence), band=union)


def lane_lines_from_model(model) -> list[tuple[np.ndarray, np.ndarray]]:
  """(x, y) arrays for each lane line of a ``modelV2`` message."""
  out = []
  for line in model.laneLines:
    out.append((_as_float_array(line.x), _as_float_array(line.y)))
  return out


@dataclass(frozen=True)
class LightDecision:
  """A light decision stable enough to act on."""
  state: str
  slope: float
  distance_m: float | None
  frames_agreeing: int


class TemporalArbiter:
  """Turns per-frame lane assignments into a decision that stops chattering.

  A single frame is not evidence enough to brake for. Two heads a few pixels
  apart trade places between frames as the boxes jitter, and acting on each flip
  would produce a light that alternates red/green at 20 Hz. This keeps a short
  history and only reports a state once the same light - same state, slope within
  ``slope_tolerance`` - has been chosen ``min_agree`` times.

  ``slope_tolerance`` is 0.05 because that is what the heads actually look like.
  On a real night drive the two neighbouring heads of one gantry differ by a
  median 0.049 rad; the previous 0.012 covered only 13% of such pairs, so the
  assignment alternated between two heads that read the *same* state and the
  trailing run never reached ``min_agree``. One segment produced a single verdict
  from 202 frames with a light in view. The state still has to match exactly, so
  a red and a green head can never be merged into one agreement - the tolerance
  only lets two heads that already agree count as one.

  It never invents a state: with no agreement it reports ``None``, which callers
  must treat as "no light information", not as "green".
  """

  def __init__(self, window: int = 6, min_agree: int = 3,
               slope_tolerance: float = 0.05,
               distance_tolerance: float = 0.2,
               distance_tolerance_m: float = 3.0):
    if window < 1 or min_agree < 1:
      raise ValueError("window and min_agree must be positive")
    if min_agree > window:
      raise ValueError("min_agree cannot exceed window")
    self.window = window
    self.min_agree = min_agree
    self.slope_tolerance = slope_tolerance
    self.distance_tolerance = distance_tolerance
    self.distance_tolerance_m = distance_tolerance_m
    self._history: deque[tuple[str, float, float | None]] = deque(maxlen=window)

  def reset(self) -> None:
    self._history.clear()

  def _distance_agrees(self, a, b) -> bool:
    """Whether two ranges are close enough to be the same physical head.

    Slope alone cannot separate two heads of one gantry: they sit 0.03-0.04 rad
    apart, which is inside the slope tolerance, so a gantry's near and far heads
    read as one light and their ranges get medianed into a number that matches
    neither. On a real night approach that produced a steady "59 m" while the car
    closed from 74 m to 25 m. Range separates them cleanly - the two heads differed
    by 66% while the same head moved 5% over the three agreeing frames.

    A missing range is not a disagreement: the caller already treats it as unknown.
    """
    if a is None or b is None:
      return True
    tolerance = max(self.distance_tolerance_m, self.distance_tolerance * max(a, b))
    return abs(a - b) <= tolerance

  def update(self, assignment: LaneAssignment, states=None) -> LightDecision | None:
    """Feed one frame's assignment; returns a decision only when it is stable."""
    if assignment.ego_index is None:
      self._history.append(("none", float("nan"), None))
      return None

    index = assignment.ego_index
    evidence = next((e for e in assignment.evidence if e.index == index), None)
    if evidence is None:
      return None
    state = None
    if states is not None and index < len(states):
      state = states[index]
    if state is None:
      # A light with no readable state cannot support a decision, but it also
      # must not be remembered as a conflicting one.
      return None

    self._history.append((state, evidence.slope, evidence.distance_m))

    # Agreement must be the *trailing run*, not a count inside the window.
    # Counting inside a window of 6 with min_agree 3 lets a perfect red/green
    # alternation qualify - three of each are always present - which is exactly
    # the chatter this class exists to suppress.
    recent = list(self._history)[-self.min_agree:]
    if len(recent) < self.min_agree:
      return None
    if any(h[0] != state or abs(h[1] - evidence.slope) > self.slope_tolerance
           or not self._distance_agrees(h[2], evidence.distance_m)
           for h in recent):
      return None
    slopes = [h[1] for h in recent]
    distances = [h[2] for h in recent if h[2] is not None]
    return LightDecision(state=state,
                         slope=float(np.median(slopes)),
                         distance_m=float(np.median(distances)) if distances else None,
                         frames_agreeing=len(recent))
