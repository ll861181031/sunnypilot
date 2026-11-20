from openpilot.cereal import log
from opendbc.car.structs import car
from opendbc.car import DT_CTRL, structs
from opendbc.car.car_helpers import interfaces
from opendbc.car.interfaces import MAX_CTRL_SPEED

from openpilot.selfdrive.selfdrived.events import Events

ButtonType = structs.CarState.ButtonEvent.Type
GearShifter = structs.CarState.GearShifter
EventName = log.OnroadEvent.EventName

# Only a brake applied to a car that is still moving makes the stock ACC release.
# Measured over 80 recorded segments: of 21 cruise-disengage edges, 15 were
# brake-triggered and the stock ACC reported Passive. Every multi-second
# unconfirmed-handoff episode instead started from a car the stock ACC had already
# stopped - braking there is redundant, the ECU stays in its hold state, and the
# only clear condition (cruiseState.enabled turning False) never arrives. Latching
# in that case turned a redundant pedal press into a permanent NO_ENTRY alert that
# blocked re-engagement until the driver cancelled cruise manually.
BYD_CANCEL_LATCH_MIN_SPEED_MS = 0.3
# The latch exists to keep an unconfirmed handoff visible, not to outlive the drive.
# If the stock ECU never reports Passive, release it after this long so re-engagement
# is not blocked until the driver manually cancels cruise.
BYD_CANCEL_LATCH_TIMEOUT_S = 5.0


class CarEvents:
  def __init__(self, CP: structs.CarParams):
    self.CP = CP

    self.steering_unpressed = 0
    self.no_steer_warning = False
    self.silent_steer_warning = True
    self.byd_longitudinal_seen = False
    self.byd_cancel_pending = False
    self.byd_cancel_pending_frames = 0
    self.byd_resume_wait_frames = 0

  def update(self, CS: car.CarState, CS_prev: car.CarState, CC: car.CarControl):
    if self.CP.brand == 'mock':
      return Events()

    events = self.create_common_events(CS, CS_prev)

    if self.CP.brand == 'byd' and self.CP.openpilotLongitudinalControl:
      valid = CS.canValid and not CS.canTimeout
      if valid and not CS.cruiseState.enabled:
        self.byd_cancel_pending = False
        self.byd_cancel_pending_frames = 0
        self.byd_longitudinal_seen = False
      elif self.byd_longitudinal_seen and CC.cruiseControl.cancel:
        # See BYD_CANCEL_LATCH_MIN_SPEED_MS: a brake on a car the stock ACC has
        # already stopped does not release it, so there is no handoff to confirm.
        if abs(CS.vEgoRaw) >= BYD_CANCEL_LATCH_MIN_SPEED_MS:
          if not self.byd_cancel_pending:
            self.byd_cancel_pending_frames = 0
          self.byd_cancel_pending = True
          self.byd_longitudinal_seen = False
      if self.byd_cancel_pending:
        # No verified stock cancel command is available. Keep the unconfirmed
        # handoff visible until fresh CAN feedback confirms ACC has disengaged,
        # but do not let it block re-engagement forever if the ECU never does.
        self.byd_cancel_pending_frames += 1
        if self.byd_cancel_pending_frames > int(BYD_CANCEL_LATCH_TIMEOUT_S / DT_CTRL):
          self.byd_cancel_pending = False
          self.byd_cancel_pending_frames = 0
        else:
          events.add(EventName.stockAccStillActive)
      if valid and CS.cruiseState.enabled and CC.enabled and CC.longActive and not CC.cruiseControl.cancel and not self.byd_cancel_pending:
        self.byd_longitudinal_seen = True

      resume_waiting = (valid and CC.enabled and CC.longActive and CC.cruiseControl.resume and
                        abs(CS.vEgoRaw) < 0.3 and CC.actuators.accel <= 0.0 and
                        not CS.gasPressed and not CS.brakePressed)
      self.byd_resume_wait_frames = self.byd_resume_wait_frames + 1 if resume_waiting else 0
      if self.byd_resume_wait_frames >= int(0.2 / DT_CTRL):
        events.add(EventName.resumeRequired)

    return events

  def create_common_events(self, CS: structs.CarState, CS_prev: car.CarState):
    events = Events()

    CI = interfaces[self.CP.carFingerprint]
    if CS.doorOpen:
      events.add(EventName.doorOpen)
    if CS.seatbeltUnlatched:
      events.add(EventName.seatbeltNotLatched)
    if CS.gearShifter != GearShifter.drive and CS.gearShifter not in CI.DRIVABLE_GEARS:
      events.add(EventName.wrongGear)
    if CS.gearShifter == GearShifter.reverse:
      events.add(EventName.reverseGear)
    if not CS.cruiseState.available:
      events.add(EventName.wrongCarMode)
    if CS.espDisabled:
      events.add(EventName.espDisabled)
    if CS.espActive:
      events.add(EventName.espActive)
    if CS.stockFcw:
      events.add(EventName.stockFcw)
    if CS.stockAeb:
      events.add(EventName.stockAeb)
    if CS.stockLkas:
      events.add(EventName.stockLkas)
    if CS.vEgo > MAX_CTRL_SPEED:
      events.add(EventName.speedTooHigh)
    if CS.cruiseState.nonAdaptive:
      events.add(EventName.wrongCruiseMode)
    if CS.brakeHoldActive and self.CP.openpilotLongitudinalControl:
      events.add(EventName.brakeHold)
    if CS.parkingBrake:
      events.add(EventName.parkBrake)
    if CS.accFaulted:
      events.add(EventName.accFaulted)
    if CS.carNotReady:
      events.add(EventName.carNotReady)
    if CS.steeringPressed:
      events.add(EventName.steerOverride)
    if CS.steeringDisengage and not CS_prev.steeringDisengage:
      events.add(EventName.steerDisengage)
    if CS.brakePressed and CS.standstill:
      events.add(EventName.preEnableStandstill)
    if CS.gasPressed:
      events.add(EventName.gasPressedOverride)
    if CS.vehicleSensorsInvalid:
      events.add(EventName.vehicleSensorsInvalid)
    if CS.invalidLkasSetting:
      events.add(EventName.invalidLkasSetting)
    if CS.lowSpeedAlert:
      events.add(EventName.belowSteerSpeed)
    if CS.buttonEnable:
      events.add(EventName.buttonEnable)

    # Handle cancel button presses
    for b in CS.buttonEvents:
      # Disable on rising and falling edge of cancel for both stock and OP long
      # TODO: only check the cancel button with openpilot longitudinal on all brands to match panda safety
      if b.type == ButtonType.cancel:
        events.add(EventName.buttonCancel)

    # Handle permanent and temporary steering faults
    self.steering_unpressed = 0 if CS.steeringPressed else self.steering_unpressed + 1
    if CS.steerFaultTemporary:
      if CS.steeringPressed and (not CS_prev.steerFaultTemporary or self.no_steer_warning):
        self.no_steer_warning = True
      else:
        self.no_steer_warning = False

        # if the user overrode recently, show a less harsh alert
        if self.silent_steer_warning or CS.standstill or self.steering_unpressed < int(1.5 / DT_CTRL):
          self.silent_steer_warning = True
          events.add(EventName.steerTempUnavailableSilent)
        else:
          events.add(EventName.steerTempUnavailable)
    else:
      self.no_steer_warning = False
      self.silent_steer_warning = False
    if CS.steerFaultPermanent:
      events.add(EventName.steerUnavailable)

    # we engage when pcm is active (rising edge)
    # enabling can optionally be blocked by the car interface
    if self.CP.pcmCruise:
      if CS.cruiseState.enabled and not CS_prev.cruiseState.enabled and not CS.blockPcmEnable:
        events.add(EventName.pcmEnable)
      elif not CS.cruiseState.enabled:
        events.add(EventName.pcmDisable)

    return events
