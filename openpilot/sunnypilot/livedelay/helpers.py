"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
from openpilot.common.params import Params


def get_lat_delay(params: Params, stock_lat_delay: float) -> float:
# live learning on: use what lagd publishes.
# off: use the fixed steerActuatorDelay + software delay sum that LagdToggle caches.

  return select_lat_delay(params, stock_lat_delay)[0]


def select_lat_delay(params: Params, stock_lat_delay: float) -> tuple[float, str]:
  if params.get_bool("LagdToggle"):
    return stock_lat_delay, "published"
  return float(params.get("LagdValueCache", return_default=True)), "fixedCache"


def lateral_action_time(effective_delay: float, smooth_seconds: float, frame_delay: float, action_delay: float) -> float:
  return effective_delay + smooth_seconds + frame_delay + action_delay


def fill_lateral_delay_debug(message, model, smooth_seconds: float, action_time: float, frame_id: int, model_output: dict,
                            previous_curvature: float = 0., model_speed: float = 0., *, stock: bool = False) -> None:
  debug = message.lateralDelayDebug
  debug.valid = True
  debug.source = model.lat_delay_source
  debug.effectiveDelay = float(model.lat_delay)
  debug.smoothSeconds = float(smooth_seconds)
  debug.actionTime = float(action_time)
  debug.frameId = int(frame_id)
  debug.modelOutputType = "action" if "action" in model_output else "plan"
  replayable = stock or ("desired_curvature" not in model_output and
                        ("planplus" not in model_output or getattr(model, "PLANPLUS_CONTROL", 1.) == 1.))
  debug.actionExtractor = "planYawV1" if replayable else "unsupported"
  debug.previousCurvature = float(previous_curvature)
  debug.modelSpeed = float(model_speed)
  debug.minControlSpeed = 0.3 if stock else float(getattr(model, "MIN_LAT_CONTROL_SPEED", 0.3))
  generation = getattr(model, "generation", None)
  debug.curvatureSmoothing = stock or (generation is not None and generation >= 10)
