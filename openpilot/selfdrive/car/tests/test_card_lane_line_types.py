"""CardStateSP lane-line type merge.

The optional laned daemon publishes ``laneLineTypes``; card copies it into
CarStateSP. A missing or stale service must read as unknown (-1), never as
dashed (0), because a consumer enabling LaneLineCheck relies on that distinction.
"""
from types import SimpleNamespace

from openpilot.cereal import custom
from openpilot.selfdrive.car.card import Car


class _SM:
  def __init__(self, seen, alive, valid, payload=None):
    self.seen = {"laneLineTypes": seen}
    self.alive = {"laneLineTypes": alive}
    self.valid = {"laneLineTypes": valid}
    self.payload = payload

  def __getitem__(self, _key):
    return self.payload


def _merge(sm):
  cs = custom.CarStateSP.new_message()
  Car._update_lane_line_types(SimpleNamespace(sm=sm), cs)
  return cs


def test_fresh_result_is_copied():
  payload = SimpleNamespace(leftType=1, rightType=0)
  cs = _merge(_SM(seen=True, alive=True, valid=True, payload=payload))
  assert (cs.leftLaneLine, cs.rightLaneLine) == (1, 0)
  assert cs.laneLineTypesValid is True


def test_missing_service_is_unknown_not_dashed():
  cs = _merge(_SM(seen=False, alive=False, valid=False))
  assert (cs.leftLaneLine, cs.rightLaneLine) == (-1, -1)
  assert cs.laneLineTypesValid is False


def test_stale_result_is_unknown():
  payload = SimpleNamespace(leftType=1, rightType=0)
  cs = _merge(_SM(seen=True, alive=False, valid=True, payload=payload))
  assert (cs.leftLaneLine, cs.rightLaneLine) == (-1, -1)
  assert cs.laneLineTypesValid is False
