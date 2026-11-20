from openpilot.cereal import custom
from openpilot.selfdrive.car import card
from openpilot.selfdrive.car.helpers import convert_to_capnp
from opendbc.car import structs
from opendbc.car.byd.tests.test_lateral import rig as rig, update
from opendbc.car.byd.carstate import CarState


def test_diagnostic_conversion_and_unchanged_snapshot(rig):
  controller, cs, cc = rig
  update(controller, cc, cs, 2_000_000_000)
  snapshot = structs.CarStateSP(bydLateral=cs.byd_lateral)
  message = convert_to_capnp(snapshot)
  update(controller, cc, cs, 2_010_000_000)
  with custom.CarStateSP.from_bytes(message.to_bytes()) as restored:
    assert restored.bydLateral.sampleMonoTime == 2_000_000_000
    assert restored.bydLateral.frame == 0
    assert restored.bydLateral.prepare
    assert restored.bydLateral.finalAngleDeg == controller.last_submitted_angle
  assert snapshot.bydLateral.sampleMonoTime != cs.byd_lateral.sampleMonoTime


def test_other_cars_and_legacy_empty_struct_have_no_diagnostics():
  assert not convert_to_capnp(structs.CarStateSP()).bydLateral.valid
  # An empty root struct is also a valid old message with no extension pointers.
  legacy_empty = bytes.fromhex("00000000010000000000000000000000")
  with custom.CarStateSP.from_bytes(legacy_empty) as restored:
    assert not restored.bydLateral.valid
  with custom.ModelDataV2SP.from_bytes(legacy_empty) as restored:
    assert not restored.lateralDelayDebug.valid


def test_actual_carstate_carries_previous_control_cycle(rig):
  controller, cs, cc = rig
  parsers = CarState.get_can_parsers(cs.CP)
  _, before = cs.update(parsers)
  assert not before.bydLateral.valid
  update(controller, cc, cs, 2_000_000_000)
  _, after = cs.update(parsers)
  assert after.bydLateral.sampleMonoTime == 2_000_000_000
  assert after.bydLateral.frame == 0
  update(controller, cc, cs, 2_010_000_000)
  assert after.bydLateral.sampleMonoTime == 2_000_000_000


def test_card_publishes_before_applying_next_control():
  from types import SimpleNamespace
  order = []
  obj = card.Car.__new__(card.Car)
  obj.state_update = lambda: ("state", "diagnostic", "radar")
  obj.state_publish = lambda *args: order.append("publish")
  obj.controls_update = lambda *args: order.append("apply")
  # step() runs two observability hooks between update and publish. They need a real
  # CI/learner, which a Car built through __new__ does not have, and they are not the
  # subject here - the assertion is the publish/apply ordering.
  obj._log_lateral_fault = lambda *args: None
  obj._lead_learn_step = lambda *args: None
  obj.CP = SimpleNamespace(passive=False)
  class Subscriptions(dict):
    seen = {"onroadEvents": True}
  obj.sm = Subscriptions(onroadEvents=[], carControl=None, carControlSP=None)
  obj.step()
  assert order == ["publish", "apply"]
