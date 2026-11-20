from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from openpilot.sunnypilot.mapd.live_map_data.osm_map_data import OsmMapData


@pytest.mark.parametrize("alive,valid,localizer_valid,gps_ok", [
  (True, True, True, True),
  (False, True, True, True),
  (True, False, True, True),
  (True, True, False, True),
  (True, True, True, False),
])
def test_map_output_requires_fresh_valid_location(alive, valid, localizer_valid, gps_ok):
  class LocationState(dict):
    pass

  bridge = OsmMapData.__new__(OsmMapData)
  bridge.sm = LocationState(liveLocationKalman=SimpleNamespace(gpsOK=gps_ok))
  bridge.sm.alive = {"liveLocationKalman": alive}
  bridge.sm.valid = {"liveLocationKalman": valid}
  bridge.localizer_valid = localizer_valid
  bridge.pm = Mock()
  bridge.get_current_speed_limit = lambda: 20.0
  bridge.get_next_speed_limit_and_distance = lambda: (10.0, 100.0)
  bridge.get_current_road_name = lambda: "Test road"
  bridge.publish()

  channel, message = bridge.pm.send.call_args.args
  assert channel == "liveMapDataSP"
  expected = alive and valid and localizer_valid and gps_ok
  assert message.valid == expected
  assert message.liveMapDataSP.speedLimitValid == expected
  assert message.liveMapDataSP.speedLimitAheadValid == expected
  if not expected:
    assert message.liveMapDataSP.speedLimit == 0.0
    assert message.liveMapDataSP.speedLimitAhead == 0.0
    assert message.liveMapDataSP.roadName == ""
