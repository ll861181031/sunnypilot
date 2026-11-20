import numpy as np
from opendbc.car.structs import car
from openpilot.common.realtime import DT_CTRL
from openpilot.selfdrive.controls.lib.drive_helpers import CONTROL_N
from openpilot.common.pid import PIDController
from openpilot.selfdrive.modeld.constants import ModelConstants

CONTROL_N_T_IDX = ModelConstants.T_IDXS[:CONTROL_N]

LongCtrlState = car.CarControl.Actuators.LongControlState
BYD_RESUME_MAX_TIME = 3.0


def get_resume_request(CP, CS, enabled, should_stop):
  own_standstill = (CP.carFingerprint == "BYD_SONG_PLUS_DMI_23_ALT" and CP.openpilotLongitudinalControl and
                   abs(CS.vEgoRaw) < 0.3)
  return enabled and not should_stop and (CS.cruiseState.standstill or own_standstill)


def long_control_state_trans(CP_SP, active, long_control_state,
                             should_stop, brake_pressed, cruise_standstill):
  # Gas Interceptor
  cruise_standstill = cruise_standstill and not CP_SP.enableGasInterceptor

  starting_condition = (not should_stop and
                        not cruise_standstill and
                        not brake_pressed)

  if not active:
    long_control_state = LongCtrlState.off

  else:
    if long_control_state == LongCtrlState.off:
      if not starting_condition:
        long_control_state = LongCtrlState.stopping
      else:
        long_control_state = LongCtrlState.pid

    elif long_control_state == LongCtrlState.stopping:
      if starting_condition:
        long_control_state = LongCtrlState.pid

    elif long_control_state == LongCtrlState.pid:
      if should_stop:
        long_control_state = LongCtrlState.stopping

  return long_control_state

class LongControl:
  def __init__(self, CP, CP_SP):
    self.CP = CP
    self.CP_SP = CP_SP
    self.long_control_state = LongCtrlState.off
    self.pid = PIDController(0.0, (CP.longitudinalTuning.kiBP, CP.longitudinalTuning.kiV),
                             rate=1 / DT_CTRL)
    self.last_output_accel = 0.0
    self.byd_stop_and_go = CP.carFingerprint == "BYD_SONG_PLUS_DMI_23_ALT" and CP.openpilotLongitudinalControl
    self.resume_from_hold = False
    self.resume_time = 0.0
    self.resume_blocked = False

  def reset(self):
    self.pid.reset()

  def byd_resume_inputs(self, active, CS, a_target, should_stop):
    stationary = abs(CS.vEgoRaw) < 0.3
    healthy = (CS.canValid and not CS.canTimeout and CS.cruiseState.enabled and
               not CS.gasPressed and not CS.brakePressed)
    driver_resume = any(b.pressed and b.type in (car.CarState.ButtonEvent.Type.accelCruise,
                                                car.CarState.ButtonEvent.Type.resumeCruise) for b in CS.buttonEvents)
    if not active or CS.gasPressed or CS.brakePressed or not stationary or (healthy and driver_resume):
      self.resume_from_hold = False
      self.resume_time = 0.0
      self.resume_blocked = False

    if active and healthy and stationary and self.long_control_state == LongCtrlState.stopping:
      self.resume_from_hold = True

    if not healthy or (stationary and self.resume_blocked):
      # An invalid/absent state is not an interrupted launch attempt: do not let
      # time spent here count towards the next attempt's timeout.
      self.resume_time = 0.0
      return True, CS.cruiseState.standstill

    if active and self.resume_from_hold and stationary and not should_stop and a_target > 0.0:
      # The stock MPC's standstill state is not an acknowledgement of our
      # acceleration command. Bound the attempt and confirm actual wheel motion.
      self.resume_time += DT_CTRL
      if self.resume_time >= BYD_RESUME_MAX_TIME:
        self.resume_blocked = True
        return True, CS.cruiseState.standstill
      return False, False
    else:
      # The attempt was interrupted (no request, should_stop, or not stationary):
      # start the next one from zero instead of carrying this attempt's time over.
      self.resume_time = 0.0

    return should_stop, CS.cruiseState.standstill

  def update(self, active, CS, a_target, should_stop, accel_limits):
    """Update longitudinal control. This updates the state machine and runs a PID loop"""
    self.pid.neg_limit = accel_limits[0]
    self.pid.pos_limit = accel_limits[1]

    cruise_standstill = CS.cruiseState.standstill
    if self.byd_stop_and_go:
      should_stop, cruise_standstill = self.byd_resume_inputs(active, CS, a_target, should_stop)
    self.long_control_state = long_control_state_trans(self.CP_SP, active, self.long_control_state,
                                                       should_stop, CS.brakePressed, cruise_standstill)
    if self.long_control_state == LongCtrlState.off:
      self.reset()
      output_accel = 0.

    elif self.long_control_state == LongCtrlState.stopping:
      output_accel = self.last_output_accel
      if output_accel > self.CP.stopAccel:
        output_accel = min(output_accel, 0.0)
        # TODO: can we just go straight to stopAccel?
        output_accel -= 1.0 * DT_CTRL  # m/s^2/s while trying to stop
      self.reset()

    else:  # LongCtrlState.pid
      error = a_target - CS.aEgo
      output_accel = self.pid.update(error, speed=CS.vEgo,
                                     feedforward=a_target)

    self.last_output_accel = np.clip(output_accel, accel_limits[0], accel_limits[1])
    return self.last_output_accel
