from openpilot.cereal import log, custom
from openpilot.common.constants import CV
from openpilot.common.realtime import DT_MDL
from openpilot.sunnypilot.selfdrive.controls.lib.auto_lane_change import AutoLaneChangeController, AutoLaneChangeMode
from openpilot.sunnypilot.selfdrive.controls.lib.lane_turn_desire import LaneTurnController

LaneChangeState = log.LaneChangeState
LaneChangeDirection = log.LaneChangeDirection
TurnDirection = custom.ModelDataV2SP.TurnDirection

LANE_CHANGE_SPEED_MIN = 20 * CV.MPH_TO_MS
LANE_CHANGE_TIME_MAX = 10.
LANE_CHANGE_START_TIME = 0.5

TURN_DESIRES = {
  TurnDirection.none: log.Desire.none,
  TurnDirection.turnLeft: log.Desire.turnLeft,
  TurnDirection.turnRight: log.Desire.turnRight,
}

class DesireHelper:
  def __init__(self):
    self.lane_change_state = LaneChangeState.off
    self.lane_change_direction = LaneChangeDirection.none
    self.lane_change_timer = 0.0
    self.prev_one_blinker = False
    self.solid_line = False
    self.desire = log.Desire.none
    self.alc = AutoLaneChangeController(self)
    self.lane_turn_controller = LaneTurnController(self)
    self.lane_turn_direction = TurnDirection.none

  @staticmethod
  def get_lane_change_direction(CS):
    return LaneChangeDirection.left if CS.leftBlinker else LaneChangeDirection.right

  @staticmethod
  def lane_line_gate(line_type: int, lane_line_check: int) -> tuple[bool, bool]:
    """Return ``(entry_allowed, requires_torque)`` for the target boundary.

    ``lane_line_check`` mirrors CarrotPilot's LaneLineCheck parameter:
      0: disabled, current behavior
      1: a **measured** solid boundary blocks the sequence; an unmeasured one
         (``-1``) does not
      2: solid/unknown may start, but only with a driver torque override

    ``line_type`` is -1 unknown, 0 dashed, 1 solid. 5 is CarrotPilot's "DLM inner
    dashed" code and is treated as dashed for cars that provide it.

    Mode 1 used to fail closed on unknown as well. That was wrong in practice: the
    classifier reports unknown for whole stretches (39 of 40 frames sampled from one
    recorded drive, where the marking was simply not resolvable), so failing closed
    did not prevent crossing a solid line - it silently disabled every lane change
    on those roads, with no alert telling the driver why. Unknown now means "no
    information" and falls back to the previous behavior (model geometry + driver
    intent); mode 2 stays available for drivers who want the strict version.
    """
    if lane_line_check <= 0 or line_type in (0, 5):
      return True, False
    if lane_line_check == 1:
      # Only a measured, non-dashed boundary blocks.
      return (True, False) if line_type < 0 else (False, True)
    return True, True

  def update(self, carstate, lateral_active, lane_change_prob, left_edge_detected=False, right_edge_detected=False,
             blinker_latched=False, left_lane_line=-1, right_lane_line=-1, lane_line_check=0,
             blindspot_unavailable_left=False, blindspot_unavailable_right=False,
             navi_turn_direction=None):
    self.alc.update_params()
    self.lane_turn_controller.update_params()
    v_ego = carstate.vEgo
    one_blinker = carstate.leftBlinker != carstate.rightBlinker
    below_lane_change_speed = v_ego < LANE_CHANGE_SPEED_MIN

    # A latched turn signal means the driver is about to turn, not to change lanes.
    # Only cars whose stalk reports the difference set this (currently BYD, where the
    # light tap and the press are separate values of TURN_SIGNAL_SWITCH); everyone
    # else leaves it false and behaves exactly as before. It gates entry only - once
    # a tap has started the sequence, switching to a held signal does not cancel it,
    # because holding the signal through a lane change is normal.
    blinker_requests_lane_change = not blinker_latched

    # Lane turn controller update
    self.lane_turn_controller.update_lane_turn(blindspot_left=carstate.leftBlindspot, blindspot_right=carstate.rightBlindspot,
                                               left_blinker=carstate.leftBlinker, right_blinker=carstate.rightBlinker, v_ego=v_ego)
    self.lane_turn_direction = self.lane_turn_controller.get_turn_direction()

    if not lateral_active or self.lane_change_timer > LANE_CHANGE_TIME_MAX or self.alc.lane_change_set_timer == AutoLaneChangeMode.OFF:
      self.lane_change_state = LaneChangeState.off
      self.lane_change_direction = LaneChangeDirection.none
      self.lane_change_timer = 0.0
      self.solid_line = False
    else:
      if self.lane_change_state == LaneChangeState.off and one_blinker and blinker_requests_lane_change \
         and not self.prev_one_blinker and not below_lane_change_speed:
        direction = self.get_lane_change_direction(carstate)
        target_line = left_lane_line if direction == LaneChangeDirection.left else right_lane_line
        entry_allowed, requires_torque = self.lane_line_gate(target_line, lane_line_check)
        # "Unknown is not clear": a blind-spot monitor that is expected but cannot be
        # read blocks starting the sequence, exactly as a detected car would. It only
        # gates entry - a sequence already in preLaneChange is not cancelled, and a
        # manual lane change (driver torque) is untouched. Cars without BSM leave both
        # flags false, so their behavior is unchanged.
        blindspot_unknown = (blindspot_unavailable_left if direction == LaneChangeDirection.left
                             else blindspot_unavailable_right)
        if entry_allowed and not blindspot_unknown:
          self.lane_change_state = LaneChangeState.preLaneChange
          self.lane_change_timer = 0.0
          # Initialize lane change direction to prevent UI alert flicker
          self.lane_change_direction = direction
          self.solid_line = requires_torque

      elif self.lane_change_state == LaneChangeState.preLaneChange:
        # Update lane change direction
        self.lane_change_direction = self.get_lane_change_direction(carstate)

        torque_applied = carstate.steeringPressed and \
                         ((carstate.steeringTorque > 0 and self.lane_change_direction == LaneChangeDirection.left) or
                          (carstate.steeringTorque < 0 and self.lane_change_direction == LaneChangeDirection.right))

        blindspot_detected = (((carstate.leftBlindspot or left_edge_detected) and self.lane_change_direction == LaneChangeDirection.left) or
                              ((carstate.rightBlindspot or right_edge_detected) and self.lane_change_direction == LaneChangeDirection.right))

        self.alc.update_lane_change(blindspot_detected, carstate.brakePressed)

        if not one_blinker or below_lane_change_speed:
          self.lane_change_state = LaneChangeState.off
          self.lane_change_direction = LaneChangeDirection.none
          self.lane_change_timer = 0.0
          self.solid_line = False
        elif (torque_applied or (self.alc.auto_lane_change_allowed and not self.solid_line)) and not blindspot_detected:
          self.lane_change_state = LaneChangeState.laneChangeStarting
          self.lane_change_timer = 0.0

      elif self.lane_change_state == LaneChangeState.laneChangeStarting:
        self.lane_change_timer += DT_MDL

        if lane_change_prob < 0.02 and self.lane_change_timer >= LANE_CHANGE_START_TIME:
          self.lane_change_timer = 0.0
          if one_blinker:
            self.lane_change_state = LaneChangeState.preLaneChange
            self.lane_change_direction = self.get_lane_change_direction(carstate)
          else:
            self.lane_change_state = LaneChangeState.off
            self.lane_change_direction = LaneChangeDirection.none
            self.solid_line = False

    self.prev_one_blinker = one_blinker and lateral_active

    if navi_turn_direction is not None and navi_turn_direction != TurnDirection.none:
      # CPlink navigation turn, gated in modeld (file flag + distance + speed).  It is
      # deliberately below the driver's blinker: a manual signal still wins.
      self.desire = TURN_DESIRES[navi_turn_direction]
    elif self.lane_turn_direction != TurnDirection.none:
      self.desire = TURN_DESIRES[self.lane_turn_direction]
    else:
      self.desire = log.Desire.none
      if self.lane_change_state == LaneChangeState.laneChangeStarting:
        if self.lane_change_direction == LaneChangeDirection.left:
          self.desire = log.Desire.laneChangeLeft
        elif self.lane_change_direction == LaneChangeDirection.right:
          self.desire = log.Desire.laneChangeRight

    self.alc.update_state()
