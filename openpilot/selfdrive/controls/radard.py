#!/usr/bin/env python3
import math
import time
import numpy as np
from collections import deque
from typing import Any

import capnp
from openpilot.cereal import messaging, log, custom
from opendbc.car.structs import car
from opendbc.car.interfaces import ACCEL_MIN
from openpilot.common.filter_simple import FirstOrderFilter
from openpilot.common.params import Params
from openpilot.common.realtime import DT_MDL, Priority, config_realtime_process
from openpilot.common.swaglog import cloudlog
from openpilot.common.simple_kalman import KF1D

from opendbc.car import structs


# Default lead acceleration decay set to 50% at 1s
_LEAD_ACCEL_TAU = 1.5

# radar tracks
SPEED, ACCEL = 0, 1     # Kalman filter states enum

# stationary qualification parameters
V_EGO_STATIONARY = 4.   # no stationary object flag below this speed

RADAR_TO_CAMERA = 1.52  # RADAR is ~ 1.5m ahead from center of mesh frame

# Retention is timed from the last trustworthy model observation, never loop count.
LEAD_HOLD_MAX_TIME = 0.5
LEAD_HOLD_MAX_FRAMES = 10  # Kept for tools that describe the historical 20 Hz budget.
LEAD_HOLD_ENTER_DREL = 45.0
LEAD_HOLD_RELEASE_DREL = 50.0
LEAD_HOLD_MIN_DREL = 0.5
LEAD_OBSERVATION_MAX_AGE = 0.25
LEAD_UNCERTAIN_MAX_AGE = 1.25


class KalmanParams:
  def __init__(self, dt: float):
    # Lead Kalman Filter params, calculating K from A, C, Q, R requires the control library.
    # hardcoding a lookup table to compute K for values of radar_ts between 0.01s and 0.2s
    assert dt > .01 and dt < .2, "Radar time step must be between .01s and 0.2s"
    self.A = [[1.0, dt], [0.0, 1.0]]
    self.C = [1.0, 0.0]
    #Q = np.matrix([[10., 0.0], [0.0, 100.]])
    #R = 1e3
    #K = np.matrix([[ 0.05705578], [ 0.03073241]])
    dts = [i * 0.01 for i in range(1, 21)]
    K0 = [0.12287673, 0.14556536, 0.16522756, 0.18281627, 0.1988689,  0.21372394,
          0.22761098, 0.24069424, 0.253096,   0.26491023, 0.27621103, 0.28705801,
          0.29750003, 0.30757767, 0.31732515, 0.32677158, 0.33594201, 0.34485814,
          0.35353899, 0.36200124]
    K1 = [0.29666309, 0.29330885, 0.29042818, 0.28787125, 0.28555364, 0.28342219,
          0.28144091, 0.27958406, 0.27783249, 0.27617149, 0.27458948, 0.27307714,
          0.27162685, 0.27023228, 0.26888809, 0.26758976, 0.26633338, 0.26511557,
          0.26393339, 0.26278425]
    self.K = [[np.interp(dt, dts, K0)], [np.interp(dt, dts, K1)]]


class Track:
  def __init__(self, identifier: int, v_lead: float, kalman_params: KalmanParams):
    self.identifier = identifier
    self.cnt = 0
    self.aLeadTau = FirstOrderFilter(_LEAD_ACCEL_TAU, 0.45, DT_MDL)
    self.K_A = kalman_params.A
    self.K_C = kalman_params.C
    self.K_K = kalman_params.K
    self.kf = KF1D([[v_lead], [0.0]], self.K_A, self.K_C, self.K_K)

  def update(self, d_rel: float, y_rel: float, v_rel: float, v_lead: float):
    # relative values, copy
    self.dRel = d_rel   # LONG_DIST
    self.yRel = y_rel   # -LAT_DIST
    self.vRel = v_rel   # REL_SPEED
    self.vLead = v_lead

    # computed velocity and accelerations
    if self.cnt > 0:
      self.kf.update(self.vLead)

    self.vLeadK = float(self.kf.x[SPEED][0])
    self.aLeadK = float(self.kf.x[ACCEL][0])

    # Learn if constant acceleration
    if abs(self.aLeadK) < 0.5:
      self.aLeadTau.x = _LEAD_ACCEL_TAU
    else:
      self.aLeadTau.update(0.0)

    self.cnt += 1

  def get_RadarState(self, model_prob: float = 0.0):
    return {
      "dRel": float(self.dRel),
      "yRel": float(self.yRel),
      "vRel": float(self.vRel),
      "vLead": float(self.vLead),
      "vLeadK": float(self.vLeadK),
      "aLeadK": float(self.aLeadK),
      "aLeadTau": float(self.aLeadTau.x),
      "present": True,
      "modelProb": model_prob,
      "radar": True,
      "radarTrackId": self.identifier,
    }

  def potential_low_speed_lead(self, v_ego: float):
    # stop for stuff in front of you and low speed, even without model confirmation
    # Radar points closer than 0.75, are almost always glitches on toyota radars
    return abs(self.yRel) < 1.0 and (v_ego < V_EGO_STATIONARY) and (0.75 < self.dRel < 25)

  def __str__(self):
    ret = f"x: {self.dRel:4.1f}  y: {self.yRel:4.1f}  v: {self.vRel:4.1f}  a: {self.aLeadK:4.1f}"
    return ret


def laplacian_pdf(x: float, mu: float, b: float):
  b = max(b, 1e-4)
  return math.exp(-abs(x-mu)/b)


def match_vision_to_track(v_ego: float, lead: capnp._DynamicStructReader, tracks: dict[int, Track]):
  offset_vision_dist = lead.x[0] - RADAR_TO_CAMERA

  def prob(c):
    prob_d = laplacian_pdf(c.dRel, offset_vision_dist, lead.xStd[0])
    prob_y = laplacian_pdf(c.yRel, -lead.y[0], lead.yStd[0])
    prob_v = laplacian_pdf(c.vRel + v_ego, lead.v[0], lead.vStd[0])

    # This isn't exactly right, but it's a good heuristic
    return prob_d * prob_y * prob_v

  track = max(tracks.values(), key=prob)

  # if no 'sane' match is found return -1
  # stationary radar points can be false positives
  dist_sane = abs(track.dRel - offset_vision_dist) < max([(offset_vision_dist)*.25, 5.0])
  vel_sane = (abs(track.vRel + v_ego - lead.v[0]) < 10) or (v_ego + track.vRel > 3)
  if dist_sane and vel_sane:
    return track
  else:
    return None


def get_RadarState_from_vision(lead_msg: capnp._DynamicStructReader, v_ego: float, model_v_ego: float, lead_prob: float):
  lead_v_rel_pred = lead_msg.v[0] - model_v_ego
  return {
    "dRel": float(lead_msg.x[0] - RADAR_TO_CAMERA),
    "yRel": float(-lead_msg.y[0]),
    "vRel": float(lead_v_rel_pred),
    "vLead": float(v_ego + lead_v_rel_pred),
    "vLeadK": float(v_ego + lead_v_rel_pred),
    "aLeadK": float(lead_msg.a[0]),
    "aLeadTau": 0.3,
    "modelProb": float(lead_prob),
    "present": True,
    "radar": False,
    "radarTrackId": -1,
  }


def get_lead(v_ego: float, ready: bool, tracks: dict[int, Track], lead_msg: capnp._DynamicStructReader,
             model_v_ego: float, lead_prob: float, CP: structs.CarParams, CP_SP: structs.CarParamsSP,
             low_speed_override: bool = True) -> dict[str, Any]:
  # Determine leads, this is where the essential logic happens
  if len(tracks) > 0 and ready and lead_prob > .5:
    track = match_vision_to_track(v_ego, lead_msg, tracks)
  else:
    track = None

  lead_dict = {'present': False}
  if track is not None:
    lead_dict = track.get_RadarState(lead_prob)
  elif (track is None) and ready and (lead_prob > .5):
    lead_dict = get_RadarState_from_vision(lead_msg, v_ego, model_v_ego, lead_prob)

  if low_speed_override:
    low_speed_tracks = [c for c in tracks.values() if c.potential_low_speed_lead(v_ego)]
    if len(low_speed_tracks) > 0:
      closest_track = min(low_speed_tracks, key=lambda c: c.dRel)

      # Only choose new track if it is actually closer than the previous one
      if (not lead_dict['present']) or (closest_track.dRel < lead_dict['dRel']):
        lead_dict = closest_track.get_RadarState()

  return lead_dict


def _lead_fields(lead: dict[str, Any]) -> dict[str, Any]:
  return {key: value for key, value in lead.items() if key not in ('rawProb', 'yRelStd', 'vLeadStd', 'pathOffset')}


def _lead_is_finite(lead: dict[str, Any]) -> bool:
  return bool(lead.get('present', False) and all(math.isfinite(float(lead.get(key, math.nan)))
              for key in ('dRel', 'yRel', 'vRel', 'vLead', 'vLeadK', 'aLeadK', 'aLeadTau')))


def _lead_is_trusted(lead: dict[str, Any]) -> bool:
  if not _lead_is_finite(lead) or lead['dRel'] < LEAD_HOLD_MIN_DREL:
    return False
  if lead.get('radar', False):
    return True
  d_std, y_std, v_std = (float(lead.get(key, math.inf)) for key in ('dRelStd', 'yRelStd', 'vLeadStd'))
  return (float(lead.get('rawProb', 0.0)) > .5 and 0.0 <= d_std <= max(4.0, .25 * lead['dRel']) and
          0.0 <= y_std <= 1.5 and 0.0 <= v_std <= 8.0)


def _lead_obstacle(lead: dict[str, Any]) -> float:
  # Same stopped-equivalence ordering as the longitudinal MPC (COMFORT_BRAKE=2.5).
  return float(lead['dRel']) + max(0.0, float(lead['vLead'])) ** 2 / 5.0


def _conservative_lead(first: dict[str, Any], second: dict[str, Any], v_ego: float) -> dict[str, Any] | None:
  # Duplicate slots may cross in distance and velocity. Keeping just the nearer
  # one can weaken the later MPC obstacle, so retain the conservative envelope.
  result = dict(first)
  for key in ('dRel', 'vLead', 'vLeadK', 'aLeadK'):
    result[key] = min(first[key], second[key])
  result['vRel'] = min(first['vRel'], second['vRel'])
  taus = (first['aLeadTau'], second['aLeadTau'])
  result['aLeadTau'] = min(taus) if result['aLeadK'] < 0.0 else max(taus)
  # Below the MPC's collision-distance floor, reducing velocity can increase
  # the clamped distance. Keep the original constraints in that regime instead.
  minimum_distance = .5 * (v_ego ** 2 - max(0.0, result['vLead']) ** 2) / (-ACCEL_MIN * 2.0)
  if result['dRel'] < minimum_distance or min(taus) < 0.0:
    return None
  return result


class LeadPrediction:
  """Keep prediction credit separate from current observations and their validity."""

  def __init__(self, track_id: int = 1):
    self.track_id = track_id
    self.reset()

  def reset(self) -> None:
    self._lead: dict[str, Any] | None = None
    self._observed_at = 0.0
    self._last_model_time = -math.inf
    self._last_time = -math.inf
    self._last_ego = 0.0
    self._distance = self._lateral = self._speed = 0.0
    self._lateral_speed = 0.0
    self._released = ''
    self._cutout_samples = 0

  def project(self, now: float, v_ego: float) -> tuple[float, float, float]:
    dt = max(0.0, now - self._last_time) if math.isfinite(self._last_time) else 0.0
    speed = self._speed
    if self._lead is not None:
      age = max(0.0, now - self._observed_at)
      previous_age = max(0.0, self._last_time - self._observed_at)
      a = self._lead['aLeadK']
      tau = max(0.0, self._lead['aLeadTau'])
      acceleration = .5 * a * (math.exp(-tau * age ** 2 / 2.0) + math.exp(-tau * previous_age ** 2 / 2.0))
      speed = max(0.0, speed + acceleration * dt)
    return (self._distance + .5 * (self._speed + speed - self._last_ego - v_ego) * dt,
            self._lateral + self._lateral_speed * dt, speed)

  def match_score(self, lead: dict[str, Any], now: float, v_ego: float) -> float:
    if self._lead is None or not _lead_is_finite(lead) or now - self._observed_at > LEAD_UNCERTAIN_MAX_AGE:
      return math.inf
    if lead.get('radar') and self._lead.get('radar') and lead.get('radarTrackId') != self._lead.get('radarTrackId'):
      return math.inf
    d, y, v = self.project(now, v_ego)
    distance_gate = max(4.0, .2 * self._lead['dRel'])
    delta = (abs(lead['dRel'] - d), abs(lead['yRel'] - y), abs(lead['vLead'] - v))
    if delta[0] > distance_gate or delta[1] > 3.5 or delta[2] > 8.0:
      return math.inf
    return delta[0] / distance_gate + delta[1] / 3.5 + delta[2] / 8.0

  def _diagnostic(self, lead: dict[str, Any], state: str, age: float, reason: str = '', matched: bool = False) -> dict[str, Any]:
    return dict(_lead_fields(lead), observationState=state, observationAge=max(0.0, age),
                visionTrackId=self.track_id if self._lead is not None else 0,
                observationMatched=matched, releaseReason=reason)

  def update(self, lead: dict[str, Any], model_time: float, v_ego: float, now: float | None = None) -> dict[str, Any]:
    now = model_time if now is None else now
    if not all(math.isfinite(value) for value in (model_time, now, v_ego)) or now < self._last_time or model_time < 0.0:
      self.reset()
      return self._diagnostic({'present': False}, 'unknown', 0.0, 'invalidClock')

    fresh = model_time > self._last_model_time and 0.0 <= now - model_time <= LEAD_OBSERVATION_MAX_AGE
    self._last_model_time = max(self._last_model_time, model_time)
    matched = math.isfinite(self.match_score(lead, now, v_ego))
    d, y, v = self.project(now, v_ego)
    self._distance, self._lateral, self._speed = d, y, v
    self._last_time, self._last_ego = now, v_ego
    trusted = fresh and _lead_is_trusted(lead) and (self._lead is None or matched)

    # Only clear a known in-path target with positive, precise cut-out evidence.
    # The offset is relative to the model path, not the camera axis on a bend.
    if trusted and self._lead is not None and not lead.get('radar', False):
      offset = float(lead.get('pathOffset', lead['yRel']))
      previous_offset = float(self._lead.get('pathOffset', self._lead['yRel']))
      margin = 2.0 * float(lead.get('yRelStd', 0.0))
      clear_outside = abs(offset) - margin > 2.3 and abs(lead['yRel']) - margin > 2.3
      moving_outside = abs(offset) > abs(previous_offset) + .5 and abs(lead['yRel']) > abs(self._lead['yRel']) + .5
      if clear_outside and (self._released == 'cutOut' or moving_outside):
        self._cutout_samples += 1
        if self._cutout_samples >= 2:
          self._released = 'cutOut'
        trusted = False
      else:
        self._cutout_samples = 0

    if trusted:
      previous = self._lead
      dt = model_time - self._observed_at
      self._lateral_speed = 0.0 if previous is None or dt <= 0.0 else float(np.clip((lead['yRel'] - previous['yRel']) / dt, -1.0, 1.0))
      self._lead = dict(lead)
      self._observed_at = model_time
      self._distance, self._lateral, self._speed = lead['dRel'], lead['yRel'], max(0.0, lead['vLead'])
      self._released = ''
      return self._diagnostic(lead, 'measured', now - model_time, matched=previous is not None)

    age = now - self._observed_at if self._lead is not None else 0.0
    reason = self._released or ('untrustedObservation' if fresh else 'staleObservation')
    prediction = None
    if self._lead is not None and not self._released:
      initial_std = float(self._lead.get('dRelStd', 0.0))
      speed_std = float(self._lead.get('vLeadStd', 0.0))
      d_std = math.sqrt(initial_std ** 2 + (speed_std * age) ** 2 + age ** 4)
      if self._lead['dRel'] > LEAD_HOLD_ENTER_DREL:
        reason = 'far'
      elif self._distance > LEAD_HOLD_RELEASE_DREL:
        reason = 'away'
      elif self._distance < LEAD_HOLD_MIN_DREL:
        reason = 'invalidDistance'
      elif age > LEAD_HOLD_MAX_TIME + 1e-9:
        reason = 'expired'
      elif d_std > max(4.0, .3 * self._lead['dRel']):
        reason = 'uncertainty'
      else:
        acceleration = self._lead['aLeadK'] * math.exp(-max(0.0, self._lead['aLeadTau']) * age ** 2 / 2.0)
        prediction = dict(self._lead, dRel=self._distance, yRel=self._lateral, vRel=self._speed-v_ego,
                          vLead=self._speed, vLeadK=self._speed, aLeadK=acceleration,
                          modelProb=min(float(self._lead.get('modelProb', 0.0)), .5), dRelStd=d_std)

    # Even a cut-out only cancels retention/guard credit: the current get_lead
    # observation keeps its original braking authority until the model drops it.
    current = lead if fresh and _lead_is_finite(lead) else None
    if prediction is not None:
      retained = prediction if current is None else _conservative_lead(prediction, current, v_ego)
      if retained is not None:
        return self._diagnostic(retained, 'predicted', age)
    if current is not None:
      return self._diagnostic(current, 'uncertain', age, reason)
    absent = dict(self._lead or {}, present=False)
    return self._diagnostic(absent, 'uncertain' if self._lead is not None else 'unknown', age, reason)


class LeadTracker:
  """Associate both model slots before retention; model slot numbers are not IDs."""

  def __init__(self):
    self.tracks: list[LeadPrediction] = []
    self.next_id = 1
    self.last_model_time = -math.inf

  def reset(self) -> None:
    self.tracks.clear()
    self.last_model_time = -math.inf

  def update(self, leads: list[dict[str, Any]], model_time: float, v_ego: float,
             now: float | None = None) -> list[dict[str, Any]]:
    now = model_time if now is None else now
    candidates = [lead for lead in leads if _lead_is_finite(lead)]
    duplicate = False
    if len(candidates) == 2:
      a, b = candidates
      duplicate = (abs(a['dRel']-b['dRel']) < 2.5 and abs(a['yRel']-b['yRel']) < .8 and abs(a['vLead']-b['vLead']) < 3.0)
      if duplicate:
        # Prediction credit comes from the best trustworthy duplicate, while the
        # control fields preserve both current braking constraints.
        representative = min(candidates, key=lambda candidate: (not _lead_is_trusted(candidate), _lead_obstacle(candidate)))
        other = b if representative is a else a
        combined = _conservative_lead(representative, other, v_ego)
        if combined is not None:
          candidates = [combined]
        else:
          duplicate = False

    if model_time <= self.last_model_time:
      candidates = []
    self.last_model_time = max(self.last_model_time, model_time)

    # Exhaustively match the two slots. Greedy nearest-neighbor matching can
    # steal the only valid association from the second target during a slot swap.
    choices = [(None, None)]
    for i in range(len(candidates)):
      choices.extend([(i, None), (None, i)])
      for j in range(len(candidates)):
        if i != j:
          choices.append((i, j))
    best_assignment = (None, None)
    best_key = (0, math.inf)
    for assignment in choices:
      count, total = 0, 0.0
      for index, candidate in enumerate(assignment):
        if candidate is not None:
          if index >= len(self.tracks):
            total = math.inf
            break
          score = self.tracks[index].match_score(candidates[candidate], now, v_ego)
          total += score
          count += 1
      key = (-count, total)
      if math.isfinite(total) and key < best_key:
        best_key, best_assignment = key, assignment

    used, outputs = set(), []
    for index, track in enumerate(self.tracks):
      choice = best_assignment[index]
      observation = {'present': False} if choice is None else candidates[choice]
      if choice is not None:
        used.add(choice)
      outputs.append((track, track.update(observation, model_time, v_ego, now), choice is not None))
    for index, candidate in enumerate(candidates):
      if index not in used:
        track = LeadPrediction(self.next_id)
        self.next_id += 1
        outputs.append((track, track.update(candidate, model_time, v_ego, now), True))

    # Never displace a distinct current obstacle with a historical prediction.
    outputs.sort(key=lambda entry: (not entry[2], not entry[1]['present'], entry[1].get('dRel', math.inf)))
    outputs = outputs[:2]
    self.tracks = [track for track, lead, _ in outputs if lead['observationAge'] <= LEAD_UNCERTAIN_MAX_AGE]
    result = [lead for _, lead, _ in outputs]
    result.sort(key=lambda lead: (not lead['present'], lead.get('dRel', math.inf)))
    while len(result) < 2:
      result.append({'present': False, 'observationState': 'unknown', 'releaseReason': 'duplicate' if duplicate else 'noHistory'})
    return result


class LeadHold:
  """Production retention: preserve acquisition, but use time and ego motion."""

  def __init__(self):
    self.reset()

  def reset(self) -> None:
    self._lead: dict[str, Any] | None = None
    self._observed_at = 0.0
    self._last_model_time = -math.inf
    self._last_time = -math.inf
    self._last_ego = 0.0
    self._distance = 0.0

  def update(self, lead: dict[str, Any], model_time: float, v_ego: float, now: float | None = None) -> dict[str, Any]:
    now = model_time if now is None else now
    if not all(math.isfinite(value) for value in (model_time, now, v_ego)) or now < self._last_time or model_time < 0.0:
      self.reset()
      return {'present': False}
    fresh = model_time > self._last_model_time and 0.0 <= now - model_time <= LEAD_OBSERVATION_MAX_AGE
    self._last_model_time = max(self._last_model_time, model_time)
    if fresh and lead.get('present', False):
      self._lead = dict(lead)
      self._observed_at = model_time
      self._last_time, self._last_ego, self._distance = now, v_ego, lead['dRel']
      return lead
    if self._lead is None:
      return {'present': False}
    dt = max(0.0, now - self._last_time)
    self._distance += (self._lead['vLead'] - .5 * (self._last_ego + v_ego)) * dt
    self._last_time, self._last_ego = now, v_ego
    age = now - self._observed_at
    if (self._lead['dRel'] > LEAD_HOLD_ENTER_DREL or age > LEAD_HOLD_MAX_TIME + 1e-9 or
        not LEAD_HOLD_MIN_DREL <= self._distance <= LEAD_HOLD_RELEASE_DREL):
      self._lead = None
      return {'present': False}
    held = dict(self._lead, dRel=self._distance, vRel=self._lead['vLead'] - v_ego)
    held['modelProb'] = min(float(held.get('modelProb', 0.0)), .5)
    return held


class RadarD:
  def __init__(self, CP: structs.CarParams, CP_SP: structs.CarParams, delay: float = 0.0):
    self.CP = CP
    self.CP_SP = CP_SP

    self.current_time = 0.0
    self.tracks: dict[int, Track] = {}
    self.kalman_params = KalmanParams(DT_MDL)
    self.lead_prob_filters = [FirstOrderFilter(0.0, 0.2, DT_MDL) for _ in range(2)]
    self.lead_hold = LeadHold()
    self.lead_tracker_shadow = LeadTracker()
    self.last_shadow_ids = [0, 0]
    self.last_model_time = -1

    self.v_ego = 0.0
    self.v_ego_hist = deque([0.0], maxlen=int(round(delay / DT_MDL))+1)
    self.last_v_ego_frame = -1

    self.radar_state: capnp._DynamicStructBuilder | None = None
    self.radar_state_valid = False

    self.ready = False

  def update(self, sm: messaging.SubMaster, rr: car.RadarData, now: float | None = None):
    now = time.monotonic() if now is None else now
    self.ready = sm.seen['modelV2']
    if not self.ready:
      self.lead_hold.reset()
      self.lead_tracker_shadow.reset()
      self.last_shadow_ids = [0, 0]

    if sm.recv_frame['carState'] != self.last_v_ego_frame:
      self.v_ego = sm['carState'].vEgo
      self.v_ego_hist.append(self.v_ego)
      self.last_v_ego_frame = sm.recv_frame['carState']

    ar_pts = {pt.trackId: [pt.dRel, pt.yRel, pt.vRel] for pt in rr.points}

    # *** remove missing points from meta data ***
    for ids in list(self.tracks.keys()):
      if ids not in ar_pts:
        self.tracks.pop(ids, None)

    # *** compute the tracks ***
    for ids in ar_pts:
      rpt = ar_pts[ids]

      # align v_ego by a fixed time to align it with the radar measurement
      v_lead = rpt[2] + self.v_ego_hist[0]

      # create the track if it doesn't exist or it's a new track
      if ids not in self.tracks:
        self.tracks[ids] = Track(ids, v_lead, self.kalman_params)
      self.tracks[ids].update(rpt[0], rpt[1], rpt[2], v_lead)

    # *** publish radarState ***
    model_time = sm.logMonoTime['modelV2'] / 1e9
    self.radar_state_valid = sm.all_checks() and 0.0 <= now - model_time <= LEAD_OBSERVATION_MAX_AGE
    if not self.radar_state_valid:
      self.lead_hold.reset()
      self.lead_tracker_shadow.reset()
      self.last_shadow_ids = [0, 0]
    self.radar_state = log.RadarState.new_message()
    self.radar_state.mdMonoTime = sm.logMonoTime['modelV2']
    self.radar_state.radarErrors = rr.errors

    if len(sm['modelV2'].velocity.x):
      model_v_ego = sm['modelV2'].velocity.x[0]
    else:
      model_v_ego = self.v_ego
    leads_v3 = sm['modelV2'].leadsV3
    if len(leads_v3) > 1:
      for i in range(2):
        # Asymmetric filter on lead prob to keep lead when uncertain
        lead_prob = leads_v3[i].prob
        if sm.logMonoTime['modelV2'] > self.last_model_time:
          if lead_prob > self.lead_prob_filters[i].x:
            self.lead_prob_filters[i].x = lead_prob
          else:
            self.lead_prob_filters[i].update(lead_prob)

      observations = []
      for i in range(2):
        raw = leads_v3[i]
        lead = get_lead(self.v_ego, self.ready, self.tracks, raw, model_v_ego, self.lead_prob_filters[i].x,
                        self.CP, self.CP_SP, low_speed_override=i == 0)
        if lead.get('present', False):
          path = sm['modelV2'].position
          path_valid = len(path.x) > 1 and len(path.x) == len(path.y) and bool(np.all(np.diff(path.x) > 0.0))
          path_y = float(np.interp(raw.x[0], path.x, path.y)) if path_valid else math.nan
          lead.update(rawProb=float(raw.prob), dRelStd=float(raw.xStd[0]) if len(raw.xStd) else math.inf,
                      yRelStd=float(raw.yStd[0]) if len(raw.yStd) else math.inf,
                      vLeadStd=float(raw.vStd[0]) if len(raw.vStd) else math.inf, pathOffset=float(-raw.y[0] + path_y))
        observations.append(lead)
      model_time = sm.logMonoTime['modelV2'] / 1e9
    else:
      observations = [{'present': False}, {'present': False}]
      model_time = sm.logMonoTime['modelV2'] / 1e9
    self.last_model_time = max(self.last_model_time, sm.logMonoTime['modelV2'])

    # Acquisition and both current MPC constraints stay unchanged in the first
    # deployment. Association, uncertainty gates and cut-out decisions are shadow.
    actual = [_lead_fields(lead) for lead in observations]
    if self.ready and self.radar_state_valid:
      actual[0] = self.lead_hold.update(actual[0], model_time, self.v_ego, now)
      shadow = self.lead_tracker_shadow.update(observations, model_time, self.v_ego, now)
    else:
      shadow = []
      actual = [{'present': False}, {'present': False}]
    for index, lead in enumerate(actual):
      candidates = [entry for entry in shadow if entry.get('visionTrackId', 0) == self.last_shadow_ids[index]
                    and self.last_shadow_ids[index] != 0]
      if observations[index].get('present', False):
        observed = observations[index]
        candidates = [entry for entry in shadow if entry.get('dRel', 0.0) > 0.0 and
                      abs(entry['dRel'] - observed['dRel']) <= max(4.0, .25 * observed['dRel'])]
        candidates.sort(key=lambda entry: abs(entry['dRel']-observed['dRel']) + abs(entry.get('yRel', 0.0)-observed['yRel']))
      diagnostic = candidates[0] if candidates else {}
      lead.update(observationState=diagnostic.get('observationState', 'unknown'),
                  observationAge=diagnostic.get('observationAge', 0.0),
                  visionTrackId=diagnostic.get('visionTrackId', 0),
                  observationMatched=diagnostic.get('observationMatched', False),
                  releaseReason=diagnostic.get('releaseReason', 'noHistory'),
                  dRelStd=diagnostic.get('dRelStd', 0.0), observationShadow=True,
                  observationDRel=diagnostic.get('dRel', -1.0))
      self.last_shadow_ids[index] = lead['visionTrackId']
    self.radar_state.leadOne, self.radar_state.leadTwo = actual

  def publish(self, pm: messaging.PubMaster):
    assert self.radar_state is not None

    radar_msg = messaging.new_message("radarState")
    radar_msg.valid = self.radar_state_valid
    radar_msg.radarState = self.radar_state
    pm.send("radarState", radar_msg)


# fuses camera and radar data for best lead detection
def main() -> None:
  config_realtime_process(5, Priority.CTRL_LOW)

  # wait for stats about the car to come in from controls
  cloudlog.info("radard is waiting for CarParams")
  CP = messaging.log_from_bytes(Params().get("CarParams", block=True), car.CarParams)
  cloudlog.info("radard got CarParams")

  cloudlog.info("radard is waiting for CarParamsSP")
  CP_SP = messaging.log_from_bytes(Params().get("CarParamsSP", block=True), custom.CarParamsSP)
  cloudlog.info("radard got CarParamsSP")

  # *** setup messaging
  sm = messaging.SubMaster(['modelV2', 'carState', 'radarTracks'], poll='modelV2')
  pm = messaging.PubMaster(['radarState'])

  RD = RadarD(CP, CP_SP, CP.radarDelay)

  while 1:
    sm.update()

    RD.update(sm, sm['radarTracks'])
    RD.publish(pm)


if __name__ == "__main__":
  main()
