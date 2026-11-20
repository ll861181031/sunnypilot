"""Turn ego-lane light detection into the single bit the planner needs.

`e2e_stop_lacks_a_reason` suppresses a brake when nothing justifies it, and its
own comment records the reason it is default-off: the same signature appears when
stopping for a red light with no queue ahead, and the model outputs carry no
signal state, so enabling the guard trades "brakes at empty crossings" for "may
roll into an empty signalised junction".

This module supplies the missing bit. The wiring is deliberately one-directional:
a light can only ever be a **reason to stop**, never a reason to release the
brakes. So a wrong classification degrades to the stock behaviour - the car
brakes the way it always did - and cannot create a new way to run a light.

Yellow counts as a reason even though stopping on yellow is not always right.
The asymmetry decides it: wrongly withholding the reason at a red or yellow light
means suppressing a correct stop, while wrongly granting it merely reproduces the
braking that happens without this feature at all.
"""

from __future__ import annotations

from dataclasses import dataclass

from openpilot.sunnypilot.yolod.lane_association import (
  TemporalArbiter,
  assign,
  forward_slope,
)

# States that justify the model's stop. `off` and `wait_on` deliberately do not:
# an unlit head is not telling us to stop, and claiming otherwise would hold the
# car still at every dark lamp over the road.
REASON_STATES = ("red", "yellow")

# A light further than this is not what the stop is about. Measured junction
# stops in this corpus happen inside 60 m.
MAX_REASON_DISTANCE_M = 60.0

# Matches the arbiter's default: three consecutive agreeing frames.
MIN_AGREEING_FRAMES = 3


@dataclass(frozen=True)
class EgoLight:
  """One frame's settled view of the light over ego's lane."""
  state: str
  slope: float
  distance_m: float | None
  frames_agreeing: int


def justifies_stop(light: EgoLight | None,
                   max_distance: float = MAX_REASON_DISTANCE_M) -> bool:
  """True when this light is itself a reason for the model to be stopping.

  Unknown distance counts as a reason: the light is already stably assigned to
  ego's lane, and erring toward "there is a reason" is the safe side.
  """
  if light is None:
    return False
  if light.state not in REASON_STATES:
    return False
  if light.distance_m is None:
    return True
  return 0.0 < light.distance_m <= max_distance


class EgoLightWatch:
  """Accumulates frames until a light over ego's lane is trustworthy.

  Deliberately stateless about images: the caller crops and classifies, this keeps
  only the arbitration history, so the whole policy is testable without a camera.
  """

  def __init__(self, min_agree: int = MIN_AGREEING_FRAMES,
               max_distance: float = MAX_REASON_DISTANCE_M):
    self._arbiter = TemporalArbiter(min_agree=min_agree)
    self.max_distance = max_distance

  def reset(self) -> None:
    self._arbiter.reset()

  def observe(self, boxes_px, states, lane_lines, frame_width: int,
              rpy=None, axis_slope: float | None = None,
              focal: float | None = None) -> EgoLight | None:
    """Feed one frame. ``boxes_px`` are pixel boxes in the published frame.

    ``rpy`` is the calibration roll/pitch/yaw; when given, the direction-of-travel
    offset is derived from it. ``focal`` defaults to the intrinsic that matches a
    1344-wide published frame scaled to ``frame_width``.
    """
    if focal is None:
      focal = 1864.0 * frame_width / 1344.0
    if axis_slope is None:
      axis_slope = forward_slope(rpy) if rpy is not None else 0.0

    assignment = assign(boxes_px, lane_lines, focal=focal, cx=frame_width / 2.0,
                        states=states, axis_slope=axis_slope)
    decision = self._arbiter.update(assignment, states)
    if decision is None:
      return None
    return EgoLight(state=decision.state, slope=decision.slope,
                    distance_m=decision.distance_m,
                    frames_agreeing=decision.frames_agreeing)

  def stop_reason(self, light: EgoLight | None) -> bool:
    return justifies_stop(light, self.max_distance)
