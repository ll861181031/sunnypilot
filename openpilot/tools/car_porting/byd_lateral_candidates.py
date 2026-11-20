"""Offline hypotheses only. Results deliberately have no transport interface."""

import inspect
import math
from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace

import numpy as np

from opendbc.can import CANParser
from opendbc.car import Bus, structs
from opendbc.car.byd import carcontroller
from opendbc.car.byd.carstate import CarState
from opendbc.car.vehicle_model import VehicleModel
from openpilot.selfdrive.controls.lib.drive_helpers import get_curvature_from_plan, smooth_value
from openpilot.tools.car_porting.byd_lateral_data import FINGERPRINT, VM_FIELDS


def result(status, reason, **values):
  return {"controlReady": False, "status": status, "reason": reason, **values}


def vehicle_model(row, metadata):
  values = metadata.get("vm", {})
  if any(not isinstance(values.get(k), (int, float)) or not math.isfinite(values[k]) for k in VM_FIELDS):
    raise ValueError("missing_vehicle_model")
  if min(values[k] for k in ("mass", "wheelbase", "steerRatio", "tireStiffnessFront", "tireStiffnessRear")) <= 0:
    raise ValueError("invalid_vehicle_model")
  live = row.get("vehicleParameters", {})
  if not live.get("valid") or not 0 <= row["t"] - live.get("time", -math.inf) <= 1:
    raise ValueError("missing_or_stale_vehicle_parameters")
  if any(not isinstance(live.get(k), (int, float)) or not math.isfinite(live[k]) for k in ("stiffness", "steerRatio", "roll", "offset")):
    raise ValueError("nonfinite_vehicle_parameters")
  vm = VehicleModel(SimpleNamespace(**values))
  vm.update_params(max(live["stiffness"], .1), max(live["steerRatio"], .1))
  return vm, live["roll"], live["offset"]


def valid_plan(row):
  plan = row.get("plan")
  if not plan or not row.get("planValid") or not 0 <= row["t"] - row.get("planTime", -math.inf) <= .15:
    raise ValueError("missing_or_stale_full_plan")
  arrays = {k: np.asarray(plan[k], dtype=float) for k in ("t", "yaw", "yawRate", "speed", "x", "y")}
  if len(arrays["t"]) < 3 or any(a.ndim != 1 or len(a) != len(arrays["t"]) or not np.all(np.isfinite(a)) for a in arrays.values()):
    raise ValueError("invalid_plan_arrays")
  if np.any(np.diff(arrays["t"]) <= 0):
    raise ValueError("nonmonotonic_plan_time")
  return arrays


class HostReplay:
  """Run the actual host controller on sampled inputs, never a live Car object.

  A 50 Hz log sample is held for two logical 100 Hz ticks. This is a sampling
  assumption, not reconstruction of every historical control-loop input.
  """

  def __init__(self, recovery_hold_seconds=None):
    self.controller_type = carcontroller.CarController
    self.hold_seconds = recovery_hold_seconds
    if recovery_hold_seconds is not None:
      if not math.isfinite(recovery_hold_seconds) or recovery_hold_seconds < 0:
        raise ValueError("invalid_recovery_hold_seconds")
      namespace = {"__name__": "offline_byd_controller"}
      exec(compile(inspect.getsource(carcontroller), "<offline recovery experiment>", "exec"), namespace)
      namespace["EPS_REARM_HOLD_NS"] = int(recovery_hold_seconds * 1e9)
      self.controller_type = namespace["CarController"]
    self.previous = None
    self.reset()

  def reset(self):
    cp = structs.CarParams(carFingerprint=FINGERPRINT)
    cp_sp = structs.CarParamsSP()
    self.cs = CarState(cp, cp_sp)
    self.cs.out = structs.CarState()
    self.cc = structs.CarControl()
    self.cc_sp = structs.CarControlSP()
    self.controller = self.controller_type({Bus.pt: "byd_song_plus_dmi_23_alt"}, cp, cp_sp)
    self.parser = CANParser("byd_song_plus_dmi_23_alt", [("MPC_LKAS_CMD_ANGLE", 50)], 2)

  def _tick(self, row, desired, time_ns):
    cs, cc = self.cs, self.cc
    cs.out.vEgo = row["speed"]
    cs.out.vEgoRaw = row.get("speedRaw", row["speed"])
    cs.out.steeringAngleDeg = row["rawAngle"]
    cs.out.steeringTorque = row.get("driverTorque", row.get("rawDriver", 0))
    cs.out.steeringPressed = row.get("driverPressed", False)
    cs.out.brakePressed = row.get("brake", False)
    cs.out.canValid = row.get("canValid", False)
    cs.out.steerFaultTemporary = row.get("fault", True) or bool(row.get("epsFault", 1))
    cs.out.cruiseState.enabled = row.get("cruiseEnabled", False)
    cs.panda_controls_allowed = row.get("pandaAllowed", False)
    cs.lkas_prepare_eps = row.get("epsPrepare", False)
    cs.eps_unknown_45 = row.get("epsBit45", False)
    cs.eps_angle_ctrl_state = row.get("epsAngleState", False)
    cs.eps_status_counter = row.get("epsCounter")
    self.parser.update([(time_ns, [(0x1E2, bytes.fromhex(row["template"]), 2)])])
    cs.lkas_angle_msg = dict(self.parser.vl["MPC_LKAS_CMD_ANGLE"])
    cs.lkas_angle_counter = int(cs.lkas_angle_msg["COUNTER"])
    cc.enabled = row.get("controlEnabled", False)
    cc.latActive = row.get("latActive", False)
    cc.actuators.steeringAngleDeg = desired
    self.cc_sp.mads.enabled = row.get("madsEnabled", False)
    self.cc_sp.mads.available = row.get("madsAvailable", False)
    self.cc_sp.mads.active = row.get("madsActive", False)
    self.controller.update(cc.as_reader(), self.cc_sp, cs, time_ns)

  def update(self, row, desired):
    finite = (desired, row.get("speed"), row.get("speedRaw"), row.get("rawAngle"), row.get("driverTorque"), row.get("t"))
    if (row.get("issues") or not row.get("segmentComplete", True) or not row.get("template") or
        any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in finite)):
      self.previous = None
      return result("invalid", "incomplete_controller_inputs")
    if (not row.get("pandaValid") or not row.get("controlSPValid") or row.get("pandaFault", True) or
        not 0 <= row["t"] - row.get("pandaTime", -math.inf) <= 1.5 or
        not 0 <= row["t"] - row.get("controlSPTime", -math.inf) <= .06):
      self.previous = None
      return result("invalid", "missing_or_stale_controller_gates")
    if not 0 <= row["t"] - row.get("templateTime", -math.inf) <= .15:
      self.previous = None
      return result("invalid", "stale_template")
    try:
      if len(bytes.fromhex(row["template"])) != 8:
        raise ValueError
    except (ValueError, TypeError):
      self.previous = None
      return result("invalid", "invalid_template")
    previous = self.previous
    continuous = previous is not None and previous[0]["route"] == row["route"] and previous[0]["segment"] == row["segment"]
    continuous = continuous and 0 < row["t"] - previous[0]["t"] <= .06
    now = int((row["t"] + 1) * 1e9)
    if not continuous:
      self.reset()
    else:
      self._tick(previous[0], previous[1], max(now - 10_000_000, int((previous[0]["t"] + 1) * 1e9) + 1))
    self._tick(row, desired, now)
    self.previous = row, desired
    diagnostic = asdict(self.cs.byd_lateral)
    return result("candidate", "sampled_host_replay", angleDeg=diagnostic["finalAngleDeg"], diagnostic=diagnostic,
                  recoveryTimingVerified=False, recoveryHoldSeconds=self.hold_seconds)


def preview_target(row, metadata, delay_seconds=None):
  debug = row.get("delayDebug", {})
  debug_fresh = debug.get("valid") and 0 <= row["t"] - row.get("delayDebugTime", -math.inf) <= .15
  output_type = debug.get("modelOutputType", "unknown") if debug_fresh else "unknown"
  if output_type == "action":
    return result("unsupported", "action_head_requires_model_input_replay")
  if output_type != "plan":
    return result("unsupported", "unknown_model_output_type")
  if row.get("issues") or not row.get("segmentComplete", True):
    return result("invalid", "incomplete_inputs")
  if debug.get("actionExtractor") != "planYawV1":
    return result("unsupported", "unreplayable_action_extractor")
  if debug.get("frameId") != row.get("planFrameId"):
    return result("unsupported", "plan_and_action_frame_mismatch")
  try:
    plan = valid_plan(row)
    vm, roll, offset = vehicle_model(row, metadata)
    if delay_seconds is None:
      if not debug_fresh:
        return result("unsupported", "missing_recorded_action_time")
      action_time = debug["actionTime"]
    else:
      if not math.isfinite(delay_seconds) or delay_seconds <= 0:
        raise ValueError("invalid_preview_delay")
      if not debug_fresh:
        return result("unsupported", "missing_recorded_smoothing_and_action_time")
      action_time = debug["actionTime"] - debug["effectiveDelay"] + delay_seconds
    if action_time > plan["t"][-1] or action_time <= 0:
      raise ValueError("preview_outside_plan")
    model_speed = debug["modelSpeed"]
    if not all(math.isfinite(debug[k]) for k in ("actionTime", "effectiveDelay", "smoothSeconds", "previousCurvature", "modelSpeed")):
      raise ValueError("nonfinite_delay_diagnostics")
    curvature = float(get_curvature_from_plan(plan["yaw"], plan["yawRate"], plan["t"], model_speed, action_time))
    if debug.get("curvatureSmoothing"):
      curvature = (float(smooth_value(curvature, debug["previousCurvature"], debug["smoothSeconds"]))
                   if model_speed > debug["minControlSpeed"] else debug["previousCurvature"])
    angle = math.degrees(vm.get_steer_from_curvature(-curvature, row["speed"], roll)) + offset
    return result("candidate", "plan_preview_hypothesis", desiredAngleDeg=angle, curvature=curvature,
                  actionTime=action_time, curvatureSmoothingReplayed=True)
  except (ValueError, KeyError, TypeError, ZeroDivisionError) as exc:
    return result("invalid", str(exc))


class PreviewReplay:
  """Advance candidate smoothing once per model frame; seed from the log."""
  def __init__(self):
    self.key, self.curvature, self.frame, self.last_time = None, None, None, None

  def update(self, row, metadata, delay_seconds=None):
    debug = row.get("delayDebug", {})
    key = (row["route"], row["segment"])
    continuous = self.key == key and self.last_time is not None and 0 < row["t"] - self.last_time <= .15
    copied = row
    if continuous and self.curvature is not None:
      copied = {**row, "delayDebug": deepcopy(debug)}
      if debug.get("frameId") == self.frame:
        # The same model frame can feed multiple 50 Hz controller requests.
        # Reuse the smoothed curvature but still convert using current live data.
        copied["delayDebug"].update(curvatureSmoothing=True, minControlSpeed=math.inf, previousCurvature=self.curvature)
      else:
        copied["delayDebug"]["previousCurvature"] = self.curvature
    outcome = preview_target(copied, metadata, delay_seconds)
    self.curvature = outcome.get("curvature")
    self.key, self.frame, self.last_time = key, debug.get("frameId"), row["t"]
    return outcome


def curve_speed(row, metadata, config):
  required = ("lateral_accel_max", "steer_rate_max_deg_s", "decel_max", "accel_max", "jerk_max", "cruise_speed_mps")
  if config is None or any(k not in config for k in required):
    return result("unavailable", "missing_explicit_capability_parameters")
  if any(not isinstance(config[k], (int, float)) or not math.isfinite(config[k]) or config[k] <= 0 for k in required):
    return result("invalid", "invalid_capability_parameters")
  if row.get("issues") or not row.get("segmentComplete", True):
    return result("invalid", "incomplete_inputs")
  try:
    plan = valid_plan(row)
    vm, roll, offset = vehicle_model(row, metadata)
    if row["speed"] <= 2 or np.any(plan["speed"] <= 1):
      return result("unsupported", "low_speed_or_stopping_plan")
    distance = np.r_[0., np.cumsum(np.hypot(np.diff(plan["x"]), np.diff(plan["y"])))]
    if np.any(np.diff(distance) <= 1e-3):
      raise ValueError("nonincreasing_path_distance")
    curvature = plan["yawRate"] / plan["speed"]
    angles = np.array([math.degrees(vm.get_steer_from_curvature(-k, v, roll)) + offset
                       for k, v in zip(curvature, plan["speed"], strict=True)])
    slope = np.abs(np.gradient(angles, distance))
    lateral_cap = np.sqrt(config["lateral_accel_max"] / np.maximum(abs(curvature), 1e-9))
    rate_cap = config["steer_rate_max_deg_s"] / np.maximum(slope, 1e-9)
    caps = np.minimum(np.minimum(lateral_cap, rate_cap), config["cruise_speed_mps"])
    profile = caps.copy()
    for i in range(len(profile) - 2, -1, -1):
      profile[i] = min(profile[i], math.sqrt(profile[i + 1] ** 2 + 2 * config["decel_max"] * (distance[i + 1] - distance[i])))
    authority = bool(metadata.get("openpilotLongitudinalControl") and row.get("longActive"))
    values = {"distanceMeters": distance.tolist(), "speedCapsMps": caps.tolist(), "speedProfileMps": profile.tolist(),
              "constraint": ["steerRate" if r < min(a, config["cruise_speed_mps"]) else
                             "lateralAccel" if a < config["cruise_speed_mps"] else "cruise"
                             for a, r in zip(lateral_cap, rate_cap, strict=True)],
              "recordedLongitudinalAuthority": authority,
              "note": "基于记录路径和车速的能力假设；降低车速后的车辆模型变化未作闭环验证。"}
    if row["speed"] > profile[0] + 1e-6:
      return result("infeasible", "insufficient_braking_distance", **values)
    profile[0] = row["speed"]
    for i in range(1, len(profile)):
      profile[i] = min(profile[i], math.sqrt(profile[i - 1] ** 2 + 2 * config["accel_max"] * (distance[i] - distance[i - 1])))
    dt = 2 * np.diff(distance) / (profile[1:] + profile[:-1])
    acceleration = np.diff(profile) / dt
    initial_accel = row.get("aEgo", 0.)
    jerk = np.diff(np.r_[initial_accel, acceleration]) / dt
    profile_angles = np.array([math.degrees(vm.get_steer_from_curvature(-k, v, roll)) + offset
                               for k, v in zip(curvature, profile, strict=True)])
    steer_rates = np.diff(profile_angles) / dt
    values.update(speedProfileMps=profile.tolist(), accelerationMps2=acceleration.tolist(), jerkMps3=jerk.tolist(),
                  steeringAngleDeg=profile_angles.tolist(), steeringRateDegS=steer_rates.tolist())
    if np.any(abs(steer_rates) > config["steer_rate_max_deg_s"] + 1e-6):
      return result("infeasible", "steer_rate_at_candidate_speed", **values)
    if np.any(acceleration > config["accel_max"] + 1e-6) or np.any(acceleration < -config["decel_max"] - 1e-6):
      return result("infeasible", "acceleration_at_candidate_speed", **values)
    if np.any(abs(jerk) > config["jerk_max"] + 1e-6):
      return result("infeasible", "jerk_limit", **values)
    return result("candidate", "offline_speed_profile", **values)
  except (ValueError, KeyError, TypeError, ZeroDivisionError) as exc:
    return result("invalid", str(exc))
