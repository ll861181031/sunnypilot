"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""

from openpilot.common.params import Params
from opendbc.car import structs
from opendbc.safety import ALTERNATIVE_EXPERIENCE
# BYD Song Plus DM-i 23 ALT port: settings-page param -> CarParamsSP field.
#
# opendbc is not allowed to import openpilot, so every value the settings page
# owns has to travel across in CarParamsSP. Keeping the mapping in one table
# means adding a knob is a one-line change here plus a field on both sides,
# instead of another hand-written assignment that can silently go stale.
BYD_SETTINGS_PARAMS = (
  ("steerAngleLeadTenths", "BydSteerLeadMaxDeg"),
  ("steerRateTenths", "BydSteerRateMax"),
  ("steerAngleMaxTenths", "BydSteerAngleMaxTenths"),
  ("overrideTorqueRelease", "BydOverrideTorqueRelease"),
  ("releaseStableFrames", "BydReleaseStableFrames"),
  ("rearmAngleErrorTenths", "BydRearmAngleErrorTenths"),
  ("epsRearmHoldMs", "BydEpsRearmHoldMs"),
  ("epsRearmTimeoutMs", "BydEpsRearmTimeoutMs"),
  ("driverTorqueBase", "BydDriverTorqueBase"),
  ("driverTorqueEpsMargin", "BydDriverTorqueEpsMargin"),
  ("driverTorqueFactorHundredths", "BydDriverTorqueFactorHundredths"),
  ("driverTorqueSamples", "BydDriverTorqueSamples"),
  ("pandaTxRejectLimit", "BydPandaTxRejectLimit"),
  ("pandaTxRejectDecay", "BydPandaTxRejectDecay"),
  ("pandaUnavailableLimit", "BydPandaUnavailableLimit"),
  ("pandaFaultClearCycles", "BydPandaFaultClearCycles"),
  ("blinkerHoldCycles", "BydBlinkerHoldCycles"),
  ("bydHmaRequest", "BydHmaRequest"),
)

# Same contract, one field: the learned lead-bound curve is TEXT (a breakpoint per
# speed band at 0.01 deg), so it cannot ride in the int table above.
BYD_SETTINGS_TEXT_PARAMS = (
  ("leadBoundTable", "BydLeadBoundTable"),
)


class MadsSteeringModeOnBrake:
  REMAIN_ACTIVE = 0
  PAUSE = 1
  DISENGAGE = 2


def read_steering_mode_param(CP: structs.CarParams, CP_SP: structs.CarParamsSP, params: Params):
  return params.get("MadsSteeringMode", return_default=True)


def set_alternative_experience(CP: structs.CarParams, CP_SP: structs.CarParamsSP, params: Params):
  enabled = params.get_bool("Mads")
  steering_mode = read_steering_mode_param(CP, CP_SP, params)

  if enabled:
    CP.alternativeExperience |= ALTERNATIVE_EXPERIENCE.ENABLE_MADS

    if steering_mode == MadsSteeringModeOnBrake.DISENGAGE:
      CP.alternativeExperience |= ALTERNATIVE_EXPERIENCE.MADS_DISENGAGE_LATERAL_ON_BRAKE
    elif steering_mode == MadsSteeringModeOnBrake.PAUSE:
      CP.alternativeExperience |= ALTERNATIVE_EXPERIENCE.MADS_PAUSE_LATERAL_ON_BRAKE


def set_car_specific_params(CP: structs.CarParams, CP_SP: structs.CarParamsSP, params: Params):
  # BYD Song Plus DM-i 23 ALT port: carry the settings page values that tune the
  # lateral envelope into opendbc, which is not allowed to read Params itself.
  # 0 means "unset", so the platform defaults in opendbc/car/byd/values.py apply
  # unchanged and the porting tests keep describing the shipped behaviour.
  if CP.brand == "byd":
    for field_name, param_name in BYD_SETTINGS_PARAMS:
      setattr(CP_SP, field_name, int(params.get(param_name, return_default=True) or 0))

    for field_name, param_name in BYD_SETTINGS_TEXT_PARAMS:
      raw = params.get(param_name, return_default=True)
      setattr(CP_SP, field_name, raw.decode("utf-8", "replace") if isinstance(raw, bytes) else (raw or ""))

    # These two are seed values for the vehicle model rather than envelope knobs,
    # so they belong on CarParams itself. paramsd overwrites them online once it
    # has learnt the car, which is why the page labels them as seed-only.
    steer_ratio = int(params.get("BydCarSpecsSteerRatioTenths", return_default=True) or 0)
    stiffness = int(params.get("BydCarSpecsStiffnessHundredths", return_default=True) or 0)
    if steer_ratio > 0:
      CP.steerRatio = steer_ratio / 10.0
    if stiffness > 0:
      CP.tireStiffnessFactor = stiffness / 100.0
