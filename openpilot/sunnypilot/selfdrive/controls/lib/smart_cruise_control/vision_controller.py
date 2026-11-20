"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import numpy as np

import openpilot.cereal.messaging as messaging
from openpilot.cereal import custom
from openpilot.common.params import Params
from openpilot.common.realtime import DT_MDL
from openpilot.selfdrive.car.cruise import V_CRUISE_UNSET
from openpilot.sunnypilot import PARAMS_UPDATE_PERIOD
from openpilot.sunnypilot.selfdrive.controls.lib.smart_cruise_control import MIN_V

VisionState = custom.LongitudinalPlanSP.SmartCruiseControl.VisionState

ACTIVE_STATES = (VisionState.entering, VisionState.turning, VisionState.leaving)
ENABLED_STATES = (VisionState.enabled, VisionState.overriding, *ACTIVE_STATES)

# Retuned 2026-11-14 in two passes.
#
# Pass 1 (route 000001a1, 2026-11-13 17:30 local, 864 s of active control, city only,
# 49 km/h max): the old 1.3 m/s^2 trigger was met in only 4.28% of frames above 20 km/h
# and `v_target = sqrt(_A_LAT_REG_MAX / max_curve)` with 2.0 evaluated above the car's
# own speed for a 70 m radius at 40 km/h, so geometry alone could never create a speed
# error. Threshold went to 0.9 and A_LAT to 1.6.
#
# Pass 2 (evening routes 000001a6/a8/a9, 2026-11-14 17:36-18:19, same car): the trigger
# was no longer the problem - every frame with a prediction >= 0.9 entered - but the
# ENTERING table was. Turns were taken at 16-30 km/h through 57-77 m radii, i.e. 0.5-1.25
# m/s^2 of predicted lateral acceleration; at 1.5 m/s^2 the old table asked for only
# -0.34 m/s^2, which the 4 s horizon turns into a 1.4 m/s target reduction while the
# geometry term still sits ABOVE the current speed. Net effect on route a9: 137 eligible
# frames, 9 demanded any speed reduction, average 0.51 m/s. The same replay with the
# numbers below: 117 frames and ~1.0 m/s, i.e. the 5-6 km/h per city turn the driver
# expects.
#
# The deceleration the planner can actually deliver is bounded by A_CRUISE_MIN (-1.2
# m/s^2) in longitudinal_planner.py, so these tables shape WHEN it starts and WHICH
# curves qualify, not how hard it brakes at the limit.
_ENTERING_PRED_LAT_ACC_TH = 0.9  # Predicted Lat Acc threshold to trigger entering turn state.
_ABORT_ENTERING_PRED_LAT_ACC_TH = 0.7  # Predicted Lat Acc threshold to abort entering state if speed drops.

_TURNING_LAT_ACC_TH = 1.3  # Lat Acc threshold to trigger turning state.

_LEAVING_LAT_ACC_TH = 1.1  # Lat Acc threshold to trigger leaving turn state.
_FINISH_LAT_ACC_TH = 0.9  # Lat Acc threshold to trigger the end of the turn cycle.

_A_LAT_REG_MAX = 1.4  # Maximum lateral acceleration

_NO_OVERSHOOT_TIME_HORIZON = 4.  # s. Time to use for velocity desired based on a_target when not overshooting.

# Lookup table for the minimum smooth deceleration during the ENTERING state
# depending on the actual maximum absolute lateral acceleration predicted on the turn ahead.
_ENTERING_SMOOTH_DECEL_V = [-0.5, -1.2]  # min decel value allowed on ENTERING state
_ENTERING_SMOOTH_DECEL_BP = [0.9, 2.2]  # absolute value of lat acc ahead

# Lookup table for the acceleration for the TURNING state
# depending on the current lateral acceleration of the vehicle.
_TURNING_ACC_V = [0.5, 0., -0.4]  # acc value
_TURNING_ACC_BP = [1.5, 2.3, 3.]  # absolute value of current lat acc

_LEAVING_ACC = 0.5  # Conformable acceleration to regain speed while leaving a turn.
_STRAIGHT_CONFIRM_TIME = 0.3  # s. Reject isolated prediction/current-curvature dips.
_RELEASE_ACCEL_RATE = (_LEAVING_ACC - min(_ENTERING_SMOOTH_DECEL_V)) / _NO_OVERSHOOT_TIME_HORIZON


class SmartCruiseControlVision:
  v_target: float = 0
  a_target: float = 0.
  v_ego: float = 0.
  a_ego: float = 0.
  output_v_target: float = V_CRUISE_UNSET
  output_a_target: float = 0.

  def __init__(self):
    self.params = Params()
    self.frame = -1
    self.long_enabled = False
    self.long_override = False
    self.is_enabled = False
    self.is_active = False
    self.enabled = self.params.get_bool("SmartCruiseControlVision")
    self.v_cruise_setpoint = 0.

    self.state = VisionState.disabled
    self.current_lat_acc = 0.
    self.max_pred_lat_acc = 0.
    self._straight_time = 0.
    self._release_confirmed = False
    self._control_v_target = V_CRUISE_UNSET

  def get_a_target_from_control(self) -> float:
    return self.a_target

  def get_v_target_from_control(self) -> float:
    if self.is_active:
      # The horizon offset is applied AFTER the floor, and the floor must survive it:
      # in a sharp turn the geometry term already sits at MIN_V and a -1.2 m/s^2
      # entering term would otherwise ask for ~0 m/s, i.e. "stop inside the curve".
      return self._control_v_target

    return V_CRUISE_UNSET

  def _update_params(self) -> None:
    if self.frame % int(PARAMS_UPDATE_PERIOD / DT_MDL) == 0:
      self.enabled = self.params.get_bool("SmartCruiseControlVision")

  def _update_calculations(self, sm: messaging.SubMaster) -> None:
    if not self.long_enabled:
      return
    else:
      rate_plan = np.array(np.abs(sm['modelV2'].orientationRate.z))
      vel_plan = np.array(sm['modelV2'].velocity.x)

      self.current_lat_acc = self.v_ego ** 2 * abs(sm['controlsState'].curvature)

      # get the maximum lat accel from the model
      predicted_lat_accels = rate_plan * vel_plan
      self.max_pred_lat_acc = np.percentile(predicted_lat_accels, 97)

      # get the maximum curve based on the current velocity
      v_ego = max(self.v_ego, 0.1)  # ensure a value greater than 0 for calculations
      max_curve = self.max_pred_lat_acc / (v_ego**2)

      # Get the target velocity for the maximum curve
      self.v_target = (_A_LAT_REG_MAX / max_curve) ** 0.5 if max_curve > 0. else V_CRUISE_UNSET

  def _update_state_machine(self) -> tuple[bool, bool]:
    clear_turn = (self.max_pred_lat_acc < _ABORT_ENTERING_PRED_LAT_ACC_TH and
                  self.current_lat_acc < _FINISH_LAT_ACC_TH)
    if self.state in ACTIVE_STATES and clear_turn:
      self._straight_time += DT_MDL
    else:
      self._straight_time = 0.
      self._release_confirmed = False
    if self._straight_time >= _STRAIGHT_CONFIRM_TIME:
      self._release_confirmed = True

    # ENABLED, ENTERING, TURNING, LEAVING, OVERRIDING
    if self.state != VisionState.disabled:
      # longitudinal and feature disable always have priority in a non-disabled state
      if not self.long_enabled or not self.enabled:
        self.state = VisionState.disabled
      elif self.long_override:
        self.state = VisionState.overriding

      else:
        # ENABLED
        if self.state == VisionState.enabled:
          # Do not enter a turn control cycle if the speed is low.
          if self.v_ego <= MIN_V:
            pass
          # If significant lateral acceleration is predicted ahead, then move to Entering turn state.
          elif self.max_pred_lat_acc >= _ENTERING_PRED_LAT_ACC_TH:
            self.state = VisionState.entering

        # OVERRIDING
        elif self.state == VisionState.overriding:
          if not self.long_override:
            self.state = VisionState.enabled

        # ENTERING
        elif self.state == VisionState.entering:
          # Transition to Turning if current lateral acceleration is over the threshold.
          if self.current_lat_acc >= _TURNING_LAT_ACC_TH:
            self.state = VisionState.turning
          # A short prediction dip must not revoke the speed constraint. Confirm
          # both low current and predicted curvature before beginning release.
          elif self._release_confirmed:
            self.state = VisionState.leaving

        # TURNING
        elif self.state == VisionState.turning:
          # Transition to Leaving if current lateral acceleration drops below a threshold.
          if self.current_lat_acc <= _LEAVING_LAT_ACC_TH:
            self.state = VisionState.leaving

        # LEAVING
        elif self.state == VisionState.leaving:
          # Transition back to Turning if current lateral acceleration goes back over the threshold.
          if self.current_lat_acc >= _TURNING_LAT_ACC_TH:
            self.state = VisionState.turning
          elif self.max_pred_lat_acc >= _ENTERING_PRED_LAT_ACC_TH:
            self.state = VisionState.entering
          # Keep publishing the releasing constraint until it reaches cruise.
          elif self._release_confirmed and self.output_v_target >= max(self.v_cruise_setpoint, MIN_V):
            self.state = VisionState.enabled

    # DISABLED
    elif self.state == VisionState.disabled:
      if self.long_enabled and self.enabled:
        if self.long_override:
          self.state = VisionState.overriding
        else:
          self.state = VisionState.enabled

    enabled = self.state in ENABLED_STATES
    active = self.state in ACTIVE_STATES
    if not active:
      self._straight_time = 0.
      self._release_confirmed = False

    return enabled, active

  def _update_solution(self) -> float:
    # DISABLED, ENABLED, OVERRIDING
    if self.state not in ACTIVE_STATES:
      # when not overshooting, calculate v_turn as the speed at the prediction horizon when following
      # the smooth deceleration.
      a_target = self.a_ego
    # ENTERING
    elif self.state == VisionState.entering:
      # when not overshooting, target a smooth deceleration in preparation for a sharp turn to come.
      a_target = np.interp(self.max_pred_lat_acc, _ENTERING_SMOOTH_DECEL_BP, _ENTERING_SMOOTH_DECEL_V)
    # TURNING
    elif self.state == VisionState.turning:
      # When turning, we provide a target acceleration that is comfortable for the lateral acceleration felt.
      a_target = np.interp(self.current_lat_acc, _TURNING_ACC_BP, _TURNING_ACC_V)
    # LEAVING
    elif self.state == VisionState.leaving:
      # When leaving, we provide a comfortable acceleration to regain speed.
      a_target = _LEAVING_ACC
    else:
      raise NotImplementedError(f"SCC-V state not supported: {self.state}")

    return a_target

  def update(self, sm: messaging.SubMaster, long_enabled: bool, long_override: bool, v_ego: float, a_ego: float,
             v_cruise_setpoint: float) -> None:
    previously_active = self.is_active
    previous_a_target = self.a_target
    self.long_enabled = long_enabled
    self.long_override = long_override
    self.v_ego = v_ego
    self.a_ego = a_ego
    self.v_cruise_setpoint = v_cruise_setpoint

    self._update_params()
    self._update_calculations(sm)

    self.is_enabled, self.is_active = self._update_state_machine()
    self.a_target = self._update_solution()
    if self.is_active:
      v_target = max(self.v_target + self.a_target * _NO_OVERSHOOT_TIME_HORIZON, MIN_V)
      if previously_active:
        waiting_for_clear = (self.state == VisionState.leaving or
                             self.max_pred_lat_acc < _ABORT_ENTERING_PRED_LAT_ACC_TH) and not self._release_confirmed
        if waiting_for_clear:
          v_target = min(v_target, self.output_v_target)
          self.a_target = min(self.a_target, previous_a_target)
        else:
          v_target = min(v_target, self.output_v_target + _LEAVING_ACC * DT_MDL)
          self.a_target = min(self.a_target, previous_a_target + _RELEASE_ACCEL_RATE * DT_MDL)
      self._control_v_target = v_target
    else:
      self._control_v_target = V_CRUISE_UNSET

    self.output_v_target = self.get_v_target_from_control()
    self.output_a_target = self.get_a_target_from_control()

    self.frame += 1
