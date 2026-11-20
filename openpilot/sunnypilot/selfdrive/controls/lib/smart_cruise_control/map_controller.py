import json
import math
import platform

from openpilot.cereal import custom
from openpilot.common.params import Params
from openpilot.common.realtime import DT_MDL
from openpilot.selfdrive.car.cruise import V_CRUISE_UNSET
from openpilot.sunnypilot import PARAMS_UPDATE_PERIOD
from openpilot.sunnypilot.navd.helpers import coordinate_from_param, Coordinate
from openpilot.sunnypilot.selfdrive.controls.lib.smart_cruise_control import MIN_V

MapState = VisionState = custom.LongitudinalPlanSP.SmartCruiseControl.MapState

ACTIVE_STATES = (MapState.turning, )
ENABLED_STATES = (MapState.enabled, MapState.overriding, *ACTIVE_STATES)

R = 6373000.0  # approximate radius of earth in meters
TO_RADIANS = math.pi / 180
TO_DEGREES = 180 / math.pi
TARGET_JERK = -0.6  # m/s^3 There's some jounce limits that are not consistent so we're fudging this some
TARGET_ACCEL = -1.2  # m/s^2 should match up with the long planner limit
TARGET_OFFSET = 1.0  # seconds - This controls how soon before the curve you reach the target velocity. It also helps
                     # reach the target velocity when inaccuracies in the distance modeling logic would cause overshoot.
                     # The value is multiplied against the target velocity to determine the additional distance. This is
                     # done to keep the distance calculations consistent but results in the offset actually being less
                     # time than specified depending on how much of a speed differential there is between v_ego and the
                     # target velocity.


# No road on earth is signed above this, so anything past it is bad map data, not a
# speed limit. The 2026-11-06 map blob carried entries up to 1243.7 m/s (4477 km/h)
# next to real ones, and the planner trusts the smallest value it finds.
MAX_PLAUSIBLE_VELOCITY = 60.0  # m/s == 216 km/h
CALC_PERIOD_S = 0.5


def velocities_from_param(param: str, params: Params):
  if params is None:
    params = Params()

  json_str = params.get(param)
  if json_str is None:
    return None

  try:
    velocities = json.loads(json_str)
  except (TypeError, ValueError):
    return None
  if not isinstance(velocities, list):
    return None

  # Drop entries that cannot be a speed limit instead of letting them reach the
  # distance maths, where one absurd value silently poisons the whole target.
  result = []
  for v in velocities:
    if not isinstance(v, dict):
      continue
    try:
      velocity = float(v.get("velocity", 0.0))
    except (TypeError, ValueError):
      continue
    if 0.0 < velocity <= MAX_PLAUSIBLE_VELOCITY:
      result.append(v)
  return result


def calculate_accel(t, target_jerk, a_ego):
  return a_ego + target_jerk * t


def calculate_velocity(t, target_jerk, a_ego, v_ego):
  return v_ego + a_ego * t + target_jerk/2 * (t ** 2)


def calculate_distance(t, target_jerk, a_ego, v_ego):
  return t * v_ego + a_ego/2 * (t ** 2) + target_jerk/6 * (t ** 3)


# points should be in radians
# output is meters
def distance_to_point(ax, ay, bx, by):
  a = math.sin((bx-ax)/2)*math.sin((bx-ax)/2) + math.cos(ax) * math.cos(bx)*math.sin((by-ay)/2)*math.sin((by-ay)/2)
  c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

  return R * c  # in meters


class SmartCruiseControlMap:
  v_target: float = 0
  a_target: float = 0.
  v_ego: float = 0.
  a_ego: float = 0.
  output_v_target: float = V_CRUISE_UNSET
  output_a_target: float = 0.

  def __init__(self):
    self.params = Params()
    self.mem_params = Params("/dev/shm/params") if platform.system() != "Darwin" else self.params
    self.enabled = self.params.get_bool("SmartCruiseControlMap")
    self.long_enabled = False
    self.long_override = False
    self.is_enabled = False
    self.is_active = False
    self.state = MapState.disabled
    self.v_cruise = 0
    self.target_lat = 0.0
    self.target_lon = 0.0
    self.frame = -1

    self.last_position = coordinate_from_param("LastGPSPosition", self.mem_params) or Coordinate(0.0, 0.0)
    self.target_velocities = velocities_from_param("MapTargetVelocities", self.mem_params) or []

  def get_v_target_from_control(self) -> float:
    if self.is_active:
      return max(self.v_target, MIN_V)

    return V_CRUISE_UNSET

  def get_a_target_from_control(self) -> float:
    return self.a_ego

  def update_params(self):
    if self.frame % int(PARAMS_UPDATE_PERIOD / DT_MDL) == 0:
      self.enabled = self.params.get_bool("SmartCruiseControlMap")

  def update_calculations(self) -> None:
    # Do NOT fall back to Coordinate(0.0, 0.0). That is the Gulf of Guinea, and the
    # nearest map point to it can be any road on earth. The old fallback also made the
    # `is None` guard below unreachable, because (0, 0) is never None -- on 2026-11-06
    # the car was doing 102 km/h while this code produced a 58 km/h target from a stale
    # position, so the planner sat on the -1.2 m/s^2 floor and braked continuously.
    try:
      self.last_position = coordinate_from_param("LastGPSPosition", self.mem_params)
    except (TypeError, ValueError, KeyError):
      self.last_position = None
    self.target_velocities = velocities_from_param("MapTargetVelocities", self.mem_params) or []

    if self.last_position is None or not self.target_velocities:
      # No position (no GPS fix) or no map data for it: clear any stale target so
      # get_v_target_from_control() cannot keep commanding an old speed.
      self.v_target = 0.0
      self.target_lat = 0.0
      self.target_lon = 0.0
      return

    lat = self.last_position.latitude
    lon = self.last_position.longitude

    min_dist = 1000
    min_idx = 0
    distances = []

    # find our location in the path
    for i in range(len(self.target_velocities)):
      target_velocity = self.target_velocities[i]
      tlat = target_velocity["latitude"]
      tlon = target_velocity["longitude"]
      d = distance_to_point(lat * TO_RADIANS, lon * TO_RADIANS, tlat * TO_RADIANS, tlon * TO_RADIANS)
      distances.append(d)
      if d < min_dist:
        min_dist = d
        min_idx = i

    # only look at values from our current position forward
    forward_points = self.target_velocities[min_idx:]
    forward_distances = distances[min_idx:]

    # find velocities that we are within the distance we need to adjust for
    valid_velocities = []
    for i in range(len(forward_points)):
      target_velocity = forward_points[i]
      tlat = target_velocity["latitude"]
      tlon = target_velocity["longitude"]
      tv = target_velocity["velocity"]
      if tv > self.v_ego:
        continue

      d = forward_distances[i]

      a_diff = (self.a_ego - TARGET_ACCEL)
      accel_t = abs(a_diff / TARGET_JERK)
      min_accel_v = calculate_velocity(accel_t, TARGET_JERK, self.a_ego, self.v_ego)

      max_d = 0
      if tv > min_accel_v:
        # calculate time needed based on target jerk
        a = 0.5 * TARGET_JERK
        b = self.a_ego
        c = self.v_ego - tv
        t_a = -1 * ((b**2 - 4 * a * c) ** 0.5 + b) / (2 * a)
        t_b = ((b**2 - 4 * a * c) ** 0.5 - b) / (2 * a)
        if not isinstance(t_a, complex) and t_a > 0:
          t = t_a
        else:
          t = t_b
        if isinstance(t, complex):
          continue

        max_d = max_d + calculate_distance(t, TARGET_JERK, self.a_ego, self.v_ego)
      else:
        t = accel_t
        max_d = calculate_distance(t, TARGET_JERK, self.a_ego, self.v_ego)

        # calculate additional time needed based on target accel
        t = abs((min_accel_v - tv) / TARGET_ACCEL)
        max_d += calculate_distance(t, 0, TARGET_ACCEL, min_accel_v)

      if d < max_d + tv * TARGET_OFFSET:
        valid_velocities.append((float(tv), tlat, tlon))

    # Find the smallest velocity we need to adjust for
    min_v = 100.0
    target_lat = 0.0
    target_lon = 0.0
    for tv, lat, lon in valid_velocities:
      if tv < min_v:
        min_v = tv
        target_lat = lat
        target_lon = lon

    if self.v_target < min_v and not (self.target_lat == 0 and self.target_lon == 0):
      for i in range(len(forward_points)):
        target_velocity = forward_points[i]
        tlat = target_velocity["latitude"]
        tlon = target_velocity["longitude"]
        tv = target_velocity["velocity"]
        if tv > self.v_ego:
          continue

        if tlat == self.target_lat and tlon == self.target_lon and tv == self.v_target:
          return

      # not found so let's reset
      self.v_target = 0.0
      self.target_lat = 0.0
      self.target_lon = 0.0

    self.v_target = min_v
    self.target_lat = target_lat
    self.target_lon = target_lon

  def _update_state_machine(self) -> tuple[bool, bool]:
    # ENABLED, TURNING
    if self.state != MapState.disabled:
      if not self.long_enabled or not self.enabled:
        self.state = MapState.disabled
      elif self.long_override:
        self.state = MapState.overriding

      else:
        # ENABLED
        if self.state == MapState.enabled:
          if self.v_cruise > self.v_target != 0:
            self.state = MapState.turning

        # TURNING
        elif self.state == MapState.turning:
          if self.v_cruise <= self.v_target or self.v_target == 0:
            self.state = MapState.enabled

        # OVERRIDING
        elif self.state == MapState.overriding:
          if not self.long_override:
            if self.v_cruise > self.v_target != 0:
              self.state = MapState.turning
            else:
              self.state = MapState.enabled

    # DISABLED
    elif self.state == MapState.disabled:
      if self.long_enabled and self.enabled:
        if self.long_override:
          self.state = MapState.overriding
        else:
          self.state = MapState.enabled

    enabled = self.state in ENABLED_STATES
    active = self.state in ACTIVE_STATES

    return enabled, active

  def update(self, long_enabled: bool, long_override: bool, v_ego, a_ego, v_cruise) -> None:
    self.long_enabled = long_enabled
    self.long_override = long_override
    self.v_ego = v_ego
    self.a_ego = a_ego
    self.v_cruise = v_cruise

    self.update_params()
    # The map blob changes at most every few seconds; parsing and scanning it at
    # the planner's 100 Hz rate is pure waste and makes a malformed Param fatal
    # 100 times a second.
    # frame starts at -1 so the first update always calculates.
    if self.frame < 0 or self.frame % max(1, int(CALC_PERIOD_S / DT_MDL)) == 0:
      self.update_calculations()

    self.is_enabled, self.is_active = self._update_state_machine()

    self.output_v_target = self.get_v_target_from_control()
    self.output_a_target = self.get_a_target_from_control()

    self.frame += 1
