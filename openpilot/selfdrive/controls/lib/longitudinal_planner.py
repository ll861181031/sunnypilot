#!/usr/bin/env python3
import collections
import math
import time

import numpy as np

import openpilot.cereal.messaging as messaging
from opendbc.car.interfaces import ACCEL_MIN, ACCEL_MAX
from openpilot.common.constants import CV
from openpilot.common.filter_simple import FirstOrderFilter
from openpilot.common.params import Params
from openpilot.common.realtime import DT_MDL
from openpilot.selfdrive.modeld.constants import ModelConstants
from openpilot.selfdrive.controls.lib.longcontrol import LongCtrlState
from openpilot.selfdrive.controls.lib.longitudinal_mpc_lib.long_mpc import LongitudinalMpc, LongitudinalPlanSource
from openpilot.selfdrive.controls.lib.longitudinal_mpc_lib.long_mpc import T_IDXS as T_IDXS_MPC
from openpilot.selfdrive.controls.lib.drive_helpers import CONTROL_N, get_accel_from_plan, should_stop
from openpilot.selfdrive.car.cruise import V_CRUISE_MAX, V_CRUISE_UNSET
from openpilot.common.swaglog import cloudlog

from openpilot.sunnypilot.selfdrive.controls.lib.longitudinal_planner import LongitudinalPlannerSP
from openpilot.selfdrive.controls.lib.accel_boost import AccelBoost
from openpilot.selfdrive.controls.lib.turn_speed_guard import TurnSpeedGuard
from openpilot.selfdrive.controls.lib.lead_uncertainty import LeadUncertaintyGuard

A_CRUISE_MAX_VALS = [1.6, 1.2, 0.8, 0.6]
A_CRUISE_MAX_BP = [0., 10.0, 25., 40.]
J_CRUISE_VALS = [1.6, 1.2, 0.8, 0.6]
A_CRUISE_MIN = -1.2
CONTROL_N_T_IDX = ModelConstants.T_IDXS[:CONTROL_N]
ALLOW_THROTTLE_THRESHOLD = 0.4
MIN_ALLOW_THROTTLE_SPEED = 2.5

# Lookup table for turns
_A_TOTAL_MAX_V = [1.7, 3.2]
_A_TOTAL_MAX_BP = [20., 40.]

def get_max_accel(v_ego):
  return np.interp(v_ego, A_CRUISE_MAX_BP, A_CRUISE_MAX_VALS)

def get_coast_accel(pitch):
  return np.sin(pitch) * -5.65 - 0.3  # fitted from data using xx/projects/allow_throttle/compute_coast_accel.py

def get_cruise_accel(e2e, v_cruise, v_ego, a_cruise_prev, angle_steers, CP, dt, accel_coast, allow_throttle):
  max_accel = ACCEL_MAX if e2e else get_max_accel(v_ego)

  if not e2e:
    a_total_max = np.interp(v_ego, _A_TOTAL_MAX_BP, _A_TOTAL_MAX_V)
    a_y = v_ego ** 2 * angle_steers * CV.DEG_TO_RAD / (CP.steerRatio * CP.wheelbase)
    a_x_allowed = math.sqrt(max(a_total_max ** 2 - a_y ** 2, 0.))
    max_accel = min(max_accel, a_x_allowed)
    if not allow_throttle:
      clipped_accel_coast = max(accel_coast, ACCEL_MIN)
      coast_limit = np.interp(v_ego, [MIN_ALLOW_THROTTLE_SPEED, MIN_ALLOW_THROTTLE_SPEED*2], [max_accel, clipped_accel_coast])
      max_accel = min(max_accel, coast_limit)

  target_accel = np.clip(v_cruise - v_ego, A_CRUISE_MIN, max_accel)
  j_cruise = np.interp(v_ego, A_CRUISE_MAX_BP, J_CRUISE_VALS)
  target_accel = float(np.clip(target_accel, a_cruise_prev - j_cruise * dt, a_cruise_prev + j_cruise * dt))

  return target_accel


# --- e2e "straight-line stop" guard -------------------------------------------------
# The e2e trajectory is a 10 s rollout of where the model expects the ego to go. Its
# terminal velocity collapsing to zero while the path stays straight, the model does
# not flag a stop, and nothing is within lead range means the rollout is reproducing a
# "traffic slows down around here" prior (crossings, junctions, side roads) rather than
# reacting to a concrete obstacle. Left unguarded that prior becomes commanded braking
# because the candidate pool is merged with min(), i.e. the most negative value wins.
#
# Measured on 557 segments (60,236 plan frames): the e2e candidate wins 49.8% of frames,
# and 3.92% of all frames are e2e wins with aTarget < -0.5, of which 60.9% have no lead
# within 50 m.
#
# DEFAULT OFF on purpose. The very same signature also shows up when stopping for a red
# light with no queue ahead (21/21 no-lead stops in the corpus had shouldStop == False
# while braking), and model outputs carry no signal light state, so enabling this trades
# "brakes at empty crossings" for "may roll into an empty signalised junction". Enable it
# per road only after checking both behaviours yourself.
E2E_STOP_GUARD_PARAM = "E2ECrosswalkGuard"
E2E_STOP_GUARD_MIN_BRAKE = 0.5      # only guard brakes stronger than -0.5 m/s^2
E2E_STOP_GUARD_TERMINAL_V = 1.0     # plan ends below this => the model is planning a stop
E2E_STOP_GUARD_STRAIGHT_YAW = 0.05  # rad; plan heading change below this => straight line
E2E_STOP_GUARD_NO_LEAD_DIST = 50.0  # m; nothing this close counts as a reason to stop
# The rollout's terminal velocity is extremely noisy frame to frame (measured swings of
# 2 -> 13 -> 3 m/s within 0.3 s), so the decision uses the minimum over the last 10 model
# frames instead of a single one. Without it the gate flickers on/off every other frame.
E2E_STOP_GUARD_VEL_WINDOW = 10

# Signal-light evidence published by yolod. Kept as plain constants here rather
# than imported from the yolod package: plannerd must not depend on a daemon that
# may not be running, and a mismatch is caught by the tests instead of at runtime.
EG_LIGHT_REASON_STATES = ("red", "yellow")
EG_LIGHT_MIN_FRAMES = 3
# A detection summary older than this is "no information", not a stale red light.
EG_LIGHT_MAX_AGE_S = 0.3
EG_LIGHT_MAX_DISTANCE_M = 60.0

# Far lights need more agreement before they are acted on.
#
# The range is deliberately generous, because engaging late makes stopping harder:
# at 16 m/s a first sighting at 40 m would need 5 m/s^2, more than the car has.
# What the long range costs instead is exposure to a misdetection, and that is
# better paid for with evidence than with distance - a real light stays red for
# seconds, a false one rarely survives six agreeing frames.
EG_LIGHT_NEAR_DISTANCE_M = 40.0
EG_LIGHT_MIN_FRAMES_FAR = 6

# Braking to the stop line for a red/yellow light over ego's own lane.
#
# The stop target is the light's own distance minus an offset, not a detected
# crosswalk. Crosswalk detection was tried and rejected earlier: 82% detection
# with a 67% false-positive rate on ordinary frames, which is worse than useless
# as a braking trigger. Stopping a little short of the lamp is the safe direction -
# it puts the car before the stop line rather than into the junction.
LIGHT_STOP_PARAM = "TrafficLightStop"
LIGHT_STOP_OFFSET_PARAM = "TrafficLightStopOffset"
LIGHT_STOP_LINE_OFFSET_M = 5.0     # default; overridden by TrafficLightStopOffset
LIGHT_STOP_LINE_GAP_M = 0.5        # stop this far before a detected stop line
# Red/yellow without a range must still reduce speed.  The nav stream has no
# distance, so this is deliberately a *speed governor*, not a stop: 50 km/h down
# to the low-speed window where stop-line detection can range the line.  It never
# commands a full stop. A committed stop has separate confirmed-release handling.
LIGHT_SLOW_TARGET_MPS = 8.0        # 28.8 km/h, inside the stop-line speed gate
LIGHT_SLOW_GAIN = 0.7
LIGHT_SLOW_MAX_DECEL = -1.5        # comfortable; not the emergency profile
LIGHT_STOP_MIN_DISTANCE_M = 2.0    # never aim for a point behind us
LIGHT_STOP_REACTION_S = 0.3        # assume this much travel before the brakes bite
LIGHT_STOP_MARGIN = 1.2            # 20% extra decel so we actually reach zero
LIGHT_STOP_HOLD_SPEED = 0.3        # below this, ask to hold instead of decelerate
# A pure -v^2/(2d) profile is asymptotic: as v falls the command falls with it, and
# the last few metres are crawled at walking pace. Measured on this formula alone,
# a 12 m/s approach took 13.7 s and crept the final 3 m at about 1.2 m/s - enough
# to hold up everyone behind. A small constant floor brings it to 7.6 s and stops
# within 0.3 m of the intended point.
LIGHT_STOP_FLOOR = -0.6
# The floor only applies in the final approach. Applied everywhere it over-brakes
# at long range: a 5 m/s approach with 40 m to go needs 0.37 m/s^2 but the floor
# would command 0.6 and stop 19 m short of the light. Gated to the last 8 m it
# stops at 9.9 m instead, and every faster approach is unchanged.
LIGHT_STOP_FLOOR_DISTANCE_M = 8.0

# Once the car has committed to stopping for a red light, losing sight of the light
# must not release the brake. A dropped detection for a few frames would otherwise
# remove the candidate, hand control back to cruise, and accelerate the car towards
# a light that is still red. The commitment is held while the state is unknown and
# released on a confirmed, matching green with a bounded acceleration transition.
#
# The timeout applies **only while still moving**. Coming to rest is safe; creeping
# into a junction is not, and a red can easily last longer than any timeout worth
# setting. So once stopped, the hold is kept indefinitely and the driver's own
# escape is the accelerator - pressing it disengages longitudinal control in the
# same frame on this car. A dead camera therefore parks the car rather than
# releasing it into the junction.
LIGHT_STOP_COMMIT_TIMEOUT_S = 3.0
# Missing/off/waiting are not positive release evidence.
LIGHT_STOP_RELEASE_STATES = ("green",)
NAV_LIGHT_MIN_CONFIRMATIONS = 2
NAV_LIGHT_MAX_AGE_S = 3.0

# Yellow is a decision, not an order. Attempting a stop that the brakes cannot
# deliver means braking at the actuator limit, failing anyway, and stopping in the
# middle of the junction - while inviting a rear-end collision from behind. The rule
# used here is the physical one: start the stop only if the deceleration the
# geometry demands is within what the car can actually produce; otherwise carry on
# through. Red is different - it is mandatory, so it always tries, even when it
# cannot succeed.
LIGHT_STOP_YELLOW_FEASIBLE_DECEL = ACCEL_MIN


def e2e_stop_lacks_a_reason(a_e2e, should_stop_e2e, terminal_v, yaw_change, lead_present, lead_d_rel,
                            signal_light_reason=False):
  """True when the e2e brake is a straight-line stop with nothing in front of it.

  `terminal_v` is the model's planned terminal speed (callers smooth it over a short
  window), `yaw_change` is |plan yaw at horizon end - plan yaw now|. Any unknown input
  falls through to False, which keeps the model's candidate exactly as it was.

  `signal_light_reason` is the one piece of evidence the original gate could not see:
  a red or yellow light stably assigned to ego's own lane. It only ever makes the
  verdict False, i.e. it can only preserve a stop, never release one. A wrong or
  missing classification therefore degrades to the stock behaviour rather than
  inventing a new way to run a light - which is what made this guard default-off
  in the first place.
  """
  if a_e2e > -E2E_STOP_GUARD_MIN_BRAKE:
    return False
  if should_stop_e2e:
    return False
  if terminal_v is None or yaw_change is None:
    return False
  if terminal_v >= E2E_STOP_GUARD_TERMINAL_V:
    return False
  if yaw_change >= E2E_STOP_GUARD_STRAIGHT_YAW:
    return False
  if signal_light_reason:
    # The stop is justified after all: our own light is telling us to stop.
    return False
  return (not lead_present) or lead_d_rel > E2E_STOP_GUARD_NO_LEAD_DIST


class LongitudinalPlanner(LongitudinalPlannerSP):
  def __init__(self, CP, CP_SP, init_v=0.0, init_a=0.0, dt=DT_MDL):
    self.CP = CP
    self.mpc = LongitudinalMpc(dt=dt)
    LongitudinalPlannerSP.__init__(self, self.CP, CP_SP, self.mpc)
    self.fcw = False
    self.dt = dt
    self.allow_throttle = True
    # Replaced by LongitudinalPlannerSP.update() with the predictive AccelBoostSP when
    # that parameter is on and this car runs openpilot longitudinal control.
    self.accel_boost = AccelBoost()

    self.v_desired_filter = FirstOrderFilter(init_v, 2.0, self.dt)
    self.a_cruise = init_a
    self.output_a_target = init_a
    self.output_should_stop = False
    self.turn_guard = TurnSpeedGuard()
    self.turn_guard_result = None
    self.lead_guard = LeadUncertaintyGuard()
    self.lead_guard_result = None
    self.lead_guard_enforce = False

    self.params = None
    self.e2e_stop_guard = False
    self.light_stop = False
    self.light_stop_offset = LIGHT_STOP_LINE_OFFSET_M
    self.light_stop_committed = False
    self.light_stop_is_stop_line = False
    self.light_stop_distance = -1.0
    self.light_stop_silent_s = 0.0
    self._reset_light_stop('initializing')
    self.switch_frames = 0
    self.e2e_stop_guard_frames = 0
    self.e2e_stop_guard_held = 0
    self.plan_terminal_vels = collections.deque(maxlen=E2E_STOP_GUARD_VEL_WINDOW)

    self.v_desired_trajectory = np.zeros(CONTROL_N)
    self.a_desired_trajectory = np.zeros(CONTROL_N)
    self.j_desired_trajectory = np.zeros(CONTROL_N)

  def _ego_light(self, sm):
    """(state, agreeing_frames, distance_m, is_stop_line) or None.

    ``is_stop_line`` means the distance was measured to the painted stop line by
    the low-speed stop-line source instead of to the lamp itself; the stop point
    then uses a small gap rather than the lamp-distance offset.

    Reads the summary published by yolod or the phone bridge. Missing, stale or
    malformed evidence returns None. Existing stop commitments handle that loss
    separately. The phone bridge does not require YoloEnabled.
    """
    try:
      if not sm.seen.get('yoloObjects', False) or not sm.valid.get('yoloObjects', False):
        return None
      # SubMaster.valid is the last message's valid flag; it is not recomputed when
      # the publisher stops. Gate on the receive age so a dead yolod cannot keep a
      # frozen red light (and its frozen distance) commanding a stop.
      recv_time = getattr(sm, "recv_time", None)
      if recv_time is not None:
        received = recv_time.get('yoloObjects')
        age = math.inf if received is None else time.monotonic() - received
        if not math.isfinite(age) or not 0.0 <= age <= EG_LIGHT_MAX_AGE_S:
          return None
      light = sm['yoloObjects']
      state = str(light.egoLightState)
      if not state:
        return None
      is_stop_line = bool(getattr(light, 'egoLightIsStopLine', False))
      distance = float(light.egoLightDistance)
      if not math.isfinite(distance):
        return None
      if str(getattr(light, 'egoLightSource', '')) == 'navigation':
        age = time.monotonic() - int(light.egoLightReceivedMonoTime) / 1e9
        if not 0.0 <= age <= NAV_LIGHT_MAX_AGE_S:
          return None
        frames = int(light.egoLightConfirmations)
      else:
        frames = int(light.egoLightFrames)
      return state, frames, distance, is_stop_line
    except (KeyError, AttributeError, TypeError, ValueError):
      return None

  def _ego_light_stop_reason(self, sm) -> bool:
    """True when our own signal light justifies the model's stop."""
    info = self._ego_light(sm)
    if info is None:
      return False
    state, frames, distance, _is_stop_line = info
    if state not in EG_LIGHT_REASON_STATES:
      return False
    nav = str(getattr(sm['yoloObjects'], 'egoLightSource', '')) == 'navigation'
    # This only preserves the model's existing brake. Association gates the
    # separate full-stop candidate; missing navigation IDs must not make the
    # optional E2E guard remove braking that was previously retained.
    if frames < (NAV_LIGHT_MIN_CONFIRMATIONS if nav else EG_LIGHT_MIN_FRAMES):
      return False
    # Negative encodes "distance unknown"; unknown still counts as a reason.
    return distance < 0.0 or 0.0 < distance <= EG_LIGHT_MAX_DISTANCE_M

  def _refresh_switches(self) -> None:
    """Re-read the two opt-in switches about once a second.

    Params reads hit disk, so this is rate limited; the very first call always
    reads, so a freshly started process honours the setting immediately.
    """
    self.switch_frames += 1
    if self.params is not None and self.switch_frames % 100 != 0:
      return
    try:
      if self.params is None:
        self.params = Params()
      self.e2e_stop_guard = self.params.get_bool(E2E_STOP_GUARD_PARAM)
      self.light_stop = self.params.get_bool(LIGHT_STOP_PARAM)
      try:
        offset = float(self.params.get(LIGHT_STOP_OFFSET_PARAM) or LIGHT_STOP_LINE_OFFSET_M)
        # A nonsensical offset would aim the stop behind the car or inside the
        # junction; clamp instead of trusting whatever is stored.
        self.light_stop_offset = float(np.clip(offset, 1.0, 20.0))
      except (TypeError, ValueError):
        self.light_stop_offset = LIGHT_STOP_LINE_OFFSET_M
    except Exception:
      # An unreadable param means "off". Never guess a switch that can brake the car.
      self.e2e_stop_guard = False
      self.light_stop = False

  def _reset_light_stop(self, reason):
    self.light_stop_committed = False
    self.light_stop_is_stop_line = False
    self.light_stop_distance = -1.0
    self.light_stop_silent_s = 0.0
    self.light_stop_context = None
    self.light_stop_target_id = ''
    self.light_stop_measurement = None
    self.light_stop_release_accel = None
    self.light_stop_release_needs_hold = False
    self.light_stop_last_accel = 0.0
    self.light_stop_state = 'unknown'
    self.light_stop_reason = reason

  def _light_stop_for_control(self, sm, v_ego, *, enabled):
    if not enabled:
      self._reset_light_stop('control_inactive_or_override')
      return None
    return self._light_stop_candidate(sm, v_ego)

  def _control_guard_candidates(self, sm, v_ego, *, enabled, overriding):
    now = time.monotonic()

    def fresh(service):
      received = getattr(sm, 'recv_time', {}).get(service, math.inf)
      age = now - received
      return (getattr(sm, 'seen', {}).get(service, False) and getattr(sm, 'valid', {}).get(service, False)
              and math.isfinite(age) and 0.0 <= age <= EG_LIGHT_MAX_AGE_S)

    try:
      car_state_sp = sm['carStateSP']
    except KeyError:
      car_state_sp = None
    self.turn_guard_result = self.turn_guard.update(
      car_state_sp, now_ns=int(now * 1e9), message_alive=car_state_sp is not None and fresh('carStateSP'),
      message_valid=getattr(sm, 'valid', {}).get('carStateSP', False), long_active=enabled,
      lateral_active=sm['carControl'].latActive,
      overriding=overriding or sm['carState'].steeringPressed, v_ego=v_ego)
    radar = sm['radarState']
    model_time = radar.mdMonoTime / 1e9
    radar_fresh = fresh('radarState') and 0.0 <= now - model_time <= EG_LIGHT_MAX_AGE_S
    self.lead_guard_result = self.lead_guard.update(
      [radar.leadOne, radar.leadTwo], model_time=model_time, v_ego=v_ego,
      enabled=enabled and radar_fresh, standstill=sm['carState'].standstill, driver_override=overriding, now=now)
    candidates = []
    if math.isfinite(self.turn_guard_result.accel_max):
      candidates.append((self.turn_guard_result.accel_max, LongitudinalPlanSource.turnGuard, False))
    if self.lead_guard_enforce and self.lead_guard_result.active:
      candidates.append((self.lead_guard_result.acceleration, LongitudinalPlanSource.leadGuard, False))
    return candidates

  def _light_stop_release(self, v_ego):
    accel = getattr(self, 'light_stop_release_accel', None)
    if accel is None:
      return None
    accel += float(np.interp(v_ego, A_CRUISE_MAX_BP, J_CRUISE_VALS)) * self.dt
    self.light_stop_release_accel = accel
    if accel >= get_max_accel(v_ego):
      self.light_stop_release_accel = None
      self.light_stop_release_needs_hold = False
      return None
    return (accel, LongitudinalPlanSource.trafficLight, False)

  def _light_stop_candidate(self, sm, v_ego):
    candidate = self._compute_light_stop_candidate(sm, v_ego)
    if candidate is not None:
      self.light_stop_last_accel = candidate[0]
    return candidate

  def _compute_light_stop_candidate(self, sm, v_ego):
    """(accel, source, should_stop) to stop for a red/yellow light, or None.

    Range and association are required to start a full stop; a confirmed signal
    without a usable target only governs speed. A committed stop survives brief
    loss, holds once stationary, and requires matching green evidence to release.
    Moving evidence loss has a bounded degraded transition instead of an abrupt
    return to cruise or an unlimited blind stop.
    """
    if not self.light_stop:
      self._reset_light_stop('disabled')
      return None

    info = self._ego_light(sm)
    state = "" if info is None else info[0]
    frames = 0 if info is None else info[1]
    distance = -1.0 if info is None else info[2]
    is_stop_line = False if info is None else info[3]
    light = sm['yoloObjects'] if info is not None else None
    nav = light is not None and (str(getattr(light, 'egoLightSource', '')) == 'navigation'
                                 or str(getattr(light, 'model', '')) == 'cplink-navi')
    context = ('vision', '', '', '')
    related = True
    target_id = ''
    measurement = None
    if nav:
      context = ('navigation', str(getattr(light, 'egoLightSession', '')),
                 str(getattr(light, 'egoLightIntersectionId', '')), str(getattr(light, 'egoLightDirection', '')))
      related = bool(getattr(light, 'egoLightRelevant', False)) and all(context[1:])
      target_id = str(getattr(light, 'stopLineTargetId', ''))
      measurement = (context, int(getattr(light, 'stopLineCaptureMonoTime', 0)),
                     int(getattr(light, 'stopLineFrameId', 0)))
    pending_stop = self.light_stop_committed or getattr(self, 'light_stop_release_needs_hold', False)
    matches_commit = (not pending_stop
                      or context == getattr(self, 'light_stop_context', ('vision', '', '', '')))
    self.light_stop_state = 'unknown'
    self.light_stop_reason = 'no_confirmed_evidence'

    min_frames = NAV_LIGHT_MIN_CONFIRMATIONS if nav else EG_LIGHT_MIN_FRAMES
    if info is not None and state in LIGHT_STOP_RELEASE_STATES and frames >= min_frames and related and matches_commit:
      was_committed = self.light_stop_committed
      self.light_stop_committed = False
      self.light_stop_is_stop_line = False
      self.light_stop_state = 'release_confirmed'
      self.light_stop_reason = 'confirmed_matching_green'
      self.light_stop_release_needs_hold = False
      if was_committed and v_ego >= LIGHT_STOP_HOLD_SPEED:
        self.light_stop_release_accel = min(0.0, getattr(self, 'light_stop_last_accel', 0.0))
      return self._light_stop_release(v_ego)

    if getattr(self, 'light_stop_release_needs_hold', False) and v_ego < LIGHT_STOP_HOLD_SPEED:
      self.light_stop_committed = True
      self.light_stop_release_accel = None
      self.light_stop_state = 'stopped_hold'
      self.light_stop_reason = 'degraded_stop_reached'
      return (ACCEL_MIN, LongitudinalPlanSource.trafficLight, True)

    required = min_frames
    if not nav and distance > EG_LIGHT_NEAR_DISTANCE_M:
      required = EG_LIGHT_MIN_FRAMES_FAR
    confirmed = (info is not None and state in EG_LIGHT_REASON_STATES
                 and frames >= required and related and matches_commit
                 and 0.0 < distance <= EG_LIGHT_MAX_DISTANCE_M)
    if nav:
      age = time.monotonic() - measurement[1] / 1e9
      confirmed = (confirmed and is_stop_line and bool(target_id)
                   and int(getattr(light, 'stopLineConfirmations', 0)) >= EG_LIGHT_MIN_FRAMES
                   and 0.0 <= age <= EG_LIGHT_MAX_AGE_S)
      if confirmed and self.light_stop_committed:
        predicted = max(0.0, self.light_stop_distance - v_ego * self.dt)
        confirmed = (target_id == getattr(self, 'light_stop_target_id', '')
                     and abs(distance - predicted) <= max(2.0, predicted * 0.2)
                     and measurement != getattr(self, 'light_stop_measurement', None))

    already_committed = self.light_stop_committed

    # State-only red/yellow: the phone navigation knows the light but not the
    # range.  Slow to the stop-line window instead of doing nothing; the stop-line
    # source (or the driver) can finish the stop once a range exists.  This block
    # never runs after a stop has been committed, and never commands zero speed.
    if (not confirmed and not already_committed and info is not None
        and state in EG_LIGHT_REASON_STATES and frames >= min_frames
        and (distance < 0.0 or nav)):
      self.light_stop_state = 'slow_only'
      self.light_stop_reason = 'no_verified_stop_target'
      releasing = self._light_stop_release(v_ego)
      if v_ego > LIGHT_SLOW_TARGET_MPS:
        slow = (LIGHT_SLOW_TARGET_MPS - v_ego) * LIGHT_SLOW_GAIN
        slow = float(np.clip(slow, LIGHT_SLOW_MAX_DECEL, 0.0))
        now = time.monotonic()
        if now - getattr(self, 'light_slow_log_at', 0.0) > 1.0:
          self.light_slow_log_at = now
          cloudlog.info({"trafficLightSlow": True, "vEgo": round(v_ego, 2),
                         "target": LIGHT_SLOW_TARGET_MPS, "accel": round(slow, 2)})
        return (min(slow, releasing[0]) if releasing else slow, LongitudinalPlanSource.trafficLight, False)
      # At/below the target, keep the cruise from re-accelerating through a red.
      # This is a hold, not a stop: the stop-line source or the driver finishes it.
      now = time.monotonic()
      if now - getattr(self, 'light_slow_log_at', 0.0) > 1.0:
        self.light_slow_log_at = now
        cloudlog.info({"trafficLightHold": True, "vEgo": round(v_ego, 2),
                       "target": LIGHT_SLOW_TARGET_MPS})
      return (min(0.0, releasing[0]) if releasing else 0.0, LongitudinalPlanSource.trafficLight, False)

    # Decide on a candidate distance WITHOUT committing the stop yet. The yellow
    # feasibility check below has to run before the latch is set, otherwise the
    # first confirmed frame commits and every later frame skips the check via
    # already_committed.
    if confirmed:
      candidate_distance = distance
      self.light_stop_is_stop_line = is_stop_line
      self.light_stop_context = context
      self.light_stop_target_id = target_id
      self.light_stop_measurement = measurement
      self.light_stop_state = 'approach'
      self.light_stop_reason = 'confirmed_target'
      self.light_stop_release_accel = None
      self.light_stop_release_needs_hold = False
    elif self.light_stop_committed:
      # No usable information this frame. Dead-reckon the range with our own travel
      # so the profile keeps converging instead of freezing at a stale distance.
      self.light_stop_silent_s += self.dt
      stopped = v_ego < LIGHT_STOP_HOLD_SPEED
      if not stopped and self.light_stop_silent_s > LIGHT_STOP_COMMIT_TIMEOUT_S:
        # Release a blind moving constraint gradually, with an explicit degraded state.
        self.light_stop_committed = False
        self.light_stop_is_stop_line = False
        self.light_stop_state = 'degraded'
        self.light_stop_reason = 'evidence_timeout'
        self.light_stop_release_needs_hold = True
        self.light_stop_release_accel = min(0.0, getattr(self, 'light_stop_last_accel', 0.0))
        return self._light_stop_release(v_ego)
      if stopped:
        # Hold the car where it is. Losing the light while stationary must never
        # turn into creeping forward; the driver releases it with the accelerator.
        self.light_stop_state = 'stopped_hold'
        self.light_stop_reason = 'holding_without_release_evidence'
        return (ACCEL_MIN, LongitudinalPlanSource.trafficLight, True)
      self.light_stop_state = 'degraded'
      self.light_stop_reason = 'target_unavailable_or_inconsistent'
      if self.light_stop_distance > 0.0:
        self.light_stop_distance = max(0.0, self.light_stop_distance - v_ego * self.dt)
      candidate_distance = self.light_stop_distance
      # Keep the source of the committed stop: a stop-line commit must not
      # silently switch to the 5 m lamp offset when the range disappears.
      is_stop_line = self.light_stop_is_stop_line
    else:
      candidate = self._light_stop_release(v_ego)
      if candidate is not None:
        self.light_stop_state = 'degraded'
        self.light_stop_reason = 'bounded_release'
      return candidate

    if candidate_distance <= 0.0 and not self.light_stop_committed:
      return None

    offset = LIGHT_STOP_LINE_GAP_M if is_stop_line else self.light_stop_offset
    d_stop = max(candidate_distance - offset, LIGHT_STOP_MIN_DISTANCE_M)
    if v_ego < LIGHT_STOP_HOLD_SPEED:
      self.light_stop_state = 'stopped_hold'
      self.light_stop_reason = 'target_stop_reached'
      if confirmed:
        self.light_stop_committed = True
        self.light_stop_distance = candidate_distance
        self.light_stop_silent_s = 0.0
      return (ACCEL_MIN, LongitudinalPlanSource.trafficLight, True)

    # Constant decel to reach zero at d_stop, plus a reaction-time allowance and a
    # margin, because a profile that just barely reaches zero overshoots on any delay.
    d_eff = max(d_stop - v_ego * LIGHT_STOP_REACTION_S, 0.5)
    required = -(v_ego * v_ego) / (2.0 * d_eff)
    if state == "yellow" and not already_committed and required < LIGHT_STOP_YELLOW_FEASIBLE_DECEL:
      # Cannot stop for it; carrying on is safer than a guaranteed-failed hard stop.
      return None

    # Feasibility passed (or this stop was already committed): latch it now.
    if confirmed:
      self.light_stop_committed = True
      self.light_stop_distance = candidate_distance
      self.light_stop_silent_s = 0.0
    accel = required * LIGHT_STOP_MARGIN
    if d_stop < LIGHT_STOP_FLOOR_DISTANCE_M:
      accel = min(accel, LIGHT_STOP_FLOOR)
    accel = float(np.clip(accel, ACCEL_MIN, 0.0))
    return (accel, LongitudinalPlanSource.trafficLight, should_stop(v_ego, accel))

  def e2e_stop_guard_blocks(self, sm, a_e2e, should_stop_e2e):
    """Keep an implausible e2e straight-line stop out of the candidate pool.

    Returns False whenever anything is uncertain, so the model keeps its vote.
    """
    self.e2e_stop_guard_frames += 1
    if not self.e2e_stop_guard:
      return False

    try:
      model = sm['modelV2']
      plan_vels = model.velocity.x
      plan_yaws = model.orientation.z
      lead = sm['radarState'].leadOne
      lead_present = bool(lead.present)
      lead_d_rel = float(lead.dRel)
    except (KeyError, AttributeError, TypeError):
      return False

    if len(plan_vels) < 1 or len(plan_yaws) < 2:
      return False

    # Smooth the rollout's terminal speed; a single frame is far too noisy to gate on.
    self.plan_terminal_vels.append(float(plan_vels[-1]))
    terminal_v = min(self.plan_terminal_vels)
    yaw_change = abs(plan_yaws[-1] - plan_yaws[0])

    if not e2e_stop_lacks_a_reason(a_e2e, should_stop_e2e, terminal_v, yaw_change,
                                  lead_present, lead_d_rel,
                                  signal_light_reason=self._ego_light_stop_reason(sm)):
      return False

    self.e2e_stop_guard_held += 1
    return True

  def update(self, sm):
    LongitudinalPlannerSP.update(self, sm)

    # The two opt-in switches (TrafficLightStop / E2ECrosswalkGuard) must be read on
    # every control cycle, not only while the e2e guard is active: the light-stop
    # candidate is evaluated unconditionally below.
    self._refresh_switches()

    if len(sm['carControl'].orientationNED) == 3:
      accel_coast = get_coast_accel(sm['carControl'].orientationNED[1])
    else:
      accel_coast = ACCEL_MAX

    v_ego = sm['carState'].vEgo
    v_cruise_kph = min(sm['carState'].vCruise, V_CRUISE_MAX)
    v_cruise = v_cruise_kph * CV.KPH_TO_MS
    if sm['controlsState'].forceDecel:
      v_cruise = 0.0

    long_control_off = sm['controlsState'].longControlState == LongCtrlState.off

    # Reset current state when not engaged, or user is controlling the speed
    reset_state = long_control_off if self.CP.openpilotLongitudinalControl else not sm['selfdriveState'].enabled
    # PCM cruise speed may be updated a few cycles later, check if initialized
    v_cruise_initialized = sm['carState'].vCruise != V_CRUISE_UNSET
    reset_state = reset_state or not v_cruise_initialized

    throttle_probs = sm['modelV2'].meta.disengagePredictions.gasPressProbs
    throttle_prob = throttle_probs[1] if len(throttle_probs) > 1 else 1.0
    self.allow_throttle = throttle_prob > ALLOW_THROTTLE_THRESHOLD or v_ego <= MIN_ALLOW_THROTTLE_SPEED

    steer_angle_without_offset = sm['carState'].steeringAngleDeg - sm['vehicleParameters'].angleOffsetDeg

    if reset_state:
      self.v_desired_filter.x = v_ego
      self.output_a_target = np.clip(sm['carState'].aEgo, ACCEL_MIN, ACCEL_MAX)
      self.a_cruise = self.output_a_target

    # Prevent divergence, smooth in current v_ego
    self.v_desired_filter.x = max(0.0, self.v_desired_filter.update(v_ego))

    # No change cost when user is controlling the speed, or when standstill
    prev_accel_constraint = not (reset_state or sm['carState'].standstill)

    # Get new v_cruise and a_target from Smart Cruise Control and Speed Limit Assist
    v_cruise, self.output_a_target = LongitudinalPlannerSP.update_targets(self, sm, self.v_desired_filter.x, self.output_a_target, v_cruise)

    self.mpc.set_weights(prev_accel_constraint, personality=sm['selfdriveState'].personality)
    self.mpc.set_cur_state(self.v_desired_filter.x, self.output_a_target)
    self.mpc.update(sm['radarState'], personality=sm['selfdriveState'].personality)

    self.v_desired_trajectory = np.interp(CONTROL_N_T_IDX, T_IDXS_MPC, self.mpc.v_solution)
    self.a_desired_trajectory = np.interp(CONTROL_N_T_IDX, T_IDXS_MPC, self.mpc.a_solution)
    self.j_desired_trajectory = np.interp(CONTROL_N_T_IDX, T_IDXS_MPC[:-1], self.mpc.j_solution)

    # TODO counter is only needed because radar is glitchy, remove once radar is gone
    self.fcw = self.mpc.crash_cnt > 2 and not sm['carState'].standstill
    if self.fcw:
      cloudlog.info("FCW triggered")

    # Save starting point for next iteration
    a_prev = self.output_a_target

    action_t =  self.CP.longitudinalActuatorDelay + DT_MDL
    output_a_target_mpc = get_accel_from_plan(self.v_desired_trajectory, self.a_desired_trajectory, CONTROL_N_T_IDX,
                                              action_t=action_t)
    output_should_stop_mpc = should_stop(v_ego, output_a_target_mpc)
    output_a_target_e2e = sm['modelV2'].action.desiredAcceleration
    output_should_stop_e2e = sm['modelV2'].action.shouldStop

    is_e2e = self.is_e2e(sm)

    self.a_cruise = get_cruise_accel(is_e2e, v_cruise, v_ego,
                                     self.a_cruise, steer_angle_without_offset, self.CP, self.dt,
                                     accel_coast, self.allow_throttle)
    cruise_should_stop = should_stop(v_ego, self.a_cruise)

    # Boost is applied to the e2e candidate *before* the min() below, so the MPC (lead
    # following) and the cruise accel (set speed) still cap it. The predictive boost can
    # therefore never push past the set speed and never outrank a lead vehicle.
    output_a_target_e2e = self.accel_boost.update(sm, output_a_target_e2e, output_a_target_mpc, self.a_cruise)

    candidates = [(output_a_target_mpc, self.mpc.source, output_should_stop_mpc),
                  (self.a_cruise, LongitudinalPlanSource.cruise, cruise_should_stop)]
    if is_e2e and not self.e2e_stop_guard_blocks(sm, output_a_target_e2e, output_should_stop_e2e):
      candidates.append((output_a_target_e2e, LongitudinalPlanSource.e2e, output_should_stop_e2e))

    # A confirmed red/yellow light adds its own braking candidate. min() below
    # picks whichever source brakes hardest, so the light stop never weakens a
    # lead-vehicle or model stop - it can only add braking, never remove it.
    driver_override = (sm['carState'].gasPressed or sm['carState'].brakePressed
                       or sm['carControl'].cruiseControl.override)
    control_active = not reset_state and sm['carControl'].longActive and not driver_override
    light_candidate = self._light_stop_for_control(sm, v_ego, enabled=control_active)
    if light_candidate is not None:
      candidates.append(light_candidate)
    candidates.extend(self._control_guard_candidates(sm, v_ego, enabled=control_active, overriding=driver_override))

    output_a_target, self.mpc.source, _ = min(candidates, key=lambda c: c[0])
    self.output_should_stop = any(should_stop for _, _, should_stop in candidates)
    self.output_a_target = np.clip(output_a_target, ACCEL_MIN, ACCEL_MAX)

    self.v_desired_filter.x = self.v_desired_filter.x + self.dt * (self.output_a_target + a_prev) / 2.0

  def publish(self, sm, pm):
    plan_send = messaging.new_message('longitudinalPlan')

    plan_send.valid = sm.all_checks()

    longitudinalPlan = plan_send.longitudinalPlan
    longitudinalPlan.modelMonoTime = sm.logMonoTime['modelV2']
    longitudinalPlan.processingDelay = (plan_send.logMonoTime / 1e9) - sm.logMonoTime['modelV2']
    longitudinalPlan.solverExecutionTime = self.mpc.solve_time

    longitudinalPlan.speeds = self.v_desired_trajectory.tolist()
    longitudinalPlan.accels = self.a_desired_trajectory.tolist()
    longitudinalPlan.jerks = self.j_desired_trajectory.tolist()

    longitudinalPlan.hasLead = sm['radarState'].leadOne.present
    longitudinalPlan.longitudinalPlanSource = self.mpc.source
    longitudinalPlan.fcw = self.fcw

    longitudinalPlan.aTarget = float(self.output_a_target)
    longitudinalPlan.accelBoost = float(self.accel_boost.total_boost)
    longitudinalPlan.shouldStop = bool(self.output_should_stop)
    longitudinalPlan.allowBrake = True
    longitudinalPlan.allowThrottle = bool(self.allow_throttle)

    pm.send('longitudinalPlan', plan_send)

    self.publish_longitudinal_plan_sp(sm, pm)
