"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
from abc import abstractmethod, ABC

import openpilot.cereal.messaging as messaging
from openpilot.common.params import Params
from openpilot.common.constants import CV
from openpilot.selfdrive.car.cruise import V_CRUISE_UNSET
from openpilot.selfdrive.navigation.cplink import CPLINK_GATE, read_cplink_message, source_active
from openpilot.sunnypilot.navd.helpers import coordinate_from_param

MAX_SPEED_LIMIT = V_CRUISE_UNSET * CV.KPH_TO_MS

def cplink_speed_active() -> bool:
  return source_active("speed", CPLINK_GATE)


class BaseMapData(ABC):
  def __init__(self):
    self.params = Params()

    self.sm = messaging.SubMaster(['liveLocationKalman'])
    self.pm = messaging.PubMaster(['liveMapDataSP'])

    self.localizer_valid = False
    self.last_bearing = None
    self.last_position = coordinate_from_param("LastGPSPositionLLK", self.params)

  @abstractmethod
  def update_location(self) -> None:
    pass

  @abstractmethod
  def get_current_speed_limit(self) -> float:
    pass

  @abstractmethod
  def get_next_speed_limit_and_distance(self) -> tuple[float, float]:
    pass

  @abstractmethod
  def get_current_road_name(self) -> str:
    pass

  def publish(self) -> None:
    if cplink_speed_active():
      msg = read_cplink_message("liveMapDataSP", CPLINK_GATE)
      if msg is None:
        msg = messaging.new_message("liveMapDataSP", valid=False)
      self.pm.send("liveMapDataSP", msg)
      return
    location_valid = (self.sm.alive['liveLocationKalman'] and self.sm.valid['liveLocationKalman'] and
                      self.localizer_valid and self.sm['liveLocationKalman'].gpsOK)
    speed_limit = self.get_current_speed_limit() if location_valid else 0.0
    next_speed_limit, next_speed_limit_distance = self.get_next_speed_limit_and_distance() if location_valid else (0.0, 0.0)

    mapd_sp_send = messaging.new_message('liveMapDataSP')
    mapd_sp_send.valid = location_valid
    live_map_data = mapd_sp_send.liveMapDataSP

    live_map_data.speedLimitValid = bool(MAX_SPEED_LIMIT > speed_limit > 0)
    live_map_data.speedLimit = speed_limit
    live_map_data.speedLimitAheadValid = bool(MAX_SPEED_LIMIT > next_speed_limit > 0)
    live_map_data.speedLimitAhead = next_speed_limit
    live_map_data.speedLimitAheadDistance = next_speed_limit_distance
    live_map_data.roadName = self.get_current_road_name() if location_valid else ""

    self.pm.send('liveMapDataSP', mapd_sp_send)

  def tick(self) -> None:
    self.sm.update(0)
    self.update_location()
    self.publish()
