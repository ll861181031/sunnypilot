from types import SimpleNamespace

import pytest

from openpilot.cereal import log
from opendbc.car import structs
from opendbc.car.structs import car
from opendbc.car.byd.carstate import CarState
from openpilot.selfdrive.car import card


class Subscriptions(dict):
  frame = 1

  def update(self, timeout):
    pass

  def all_checks(self, services):
    return self.fresh


@pytest.mark.parametrize("fresh", [True, False])
def test_card_passes_panda_freshness_to_byd_state(monkeypatch, fresh):
  # Exercise the actual state-update integration without constructing live
  # sockets, publishers, USB connections, or touching production Params.
  cp = car.CarParams(brand="byd", carFingerprint="BYD_SONG_PLUS_DMI_23_ALT")
  cp_sp = structs.CarParamsSP()
  cs = CarState(cp, cp_sp)
  cs.out = car.CarState(canValid=True, cruiseState={"enabled": True})
  cs.panda_tx_blocked = 10
  panda = log.PandaState(controlsAllowed=True, safetyModel="byd", safetyTxBlocked=11)
  sm = Subscriptions(pandaStates=[panda], carControl=car.CarControl(), longitudinalPlanSP=None,
                     onroadEvents=[])
  sm.fresh = fresh
  obj = card.Car.__new__(card.Car)
  obj.CP = cp
  obj.CI = SimpleNamespace(CS=cs, update=lambda frames: (cs.out, structs.CarStateSP()))
  obj.RI = SimpleNamespace(update=lambda *args: None)
  obj.sm = sm
  obj.can_sock = None
  obj.can_rcv_cum_timeout_counter = 0
  obj.is_metric = True
  obj.experimental_mode = False
  obj.dynamic_experimental_control = False
  obj.CC_prev = car.CarControl()
  obj.v_cruise_helper = SimpleNamespace(
    update_speed_limit_assist=lambda *args: None,
    update_v_cruise=lambda *args: None, _paddle_decel_active=False,
    v_cruise_kph=30, v_cruise_cluster_kph=30, log="", _soft_hold_active=0,
    _activate_cruise=0, _lat_enabled=True, useLaneLineSpeedApply=0, carrot_cruise_active=False,
  )
  monkeypatch.setattr(card.messaging, "drain_sock_raw", lambda *args, **kwargs: [])
  state, _, _ = obj.state_update()
  # Two invariants that pull in opposite directions, and the fix has to hold both:
  #  - a snapshot we cannot read must never license steering, whatever it said the last
  #    time it was readable;
  #  - one stale sample is not a fault, because pandaStates arrives at 8.9 Hz here while
  #    its declared rate is 10 Hz, so all_checks(["pandaStates"]) trips on ordinary
  #    jitter. Latching on that sample used to disable lateral for the rest of the drive.
  assert not state.steerFaultTemporary, "one stale sample must not raise a fault"
  assert not cs.lateral_control_fault
  if fresh:
    assert cs.panda_controls_allowed, "a healthy snapshot allows steering"
  else:
    assert not cs.panda_controls_allowed, "an unreadable snapshot must not"


def test_sustained_staleness_becomes_a_fault(monkeypatch):
  """The debounce delays the fault, it does not remove it."""
  from opendbc.car.byd import carstate as byd_carstate
  cp = car.CarParams(brand="byd", carFingerprint="BYD_SONG_PLUS_DMI_23_ALT")
  cs = CarState(cp, structs.CarParamsSP())
  cs.update_panda_state([log.PandaState(controlsAllowed=True, safetyModel="byd")], True)
  assert cs.panda_controls_allowed
  for _ in range(byd_carstate.PANDA_UNAVAILABLE_LIMIT):
    cs.update_panda_state([], False)
  assert cs.lateral_control_fault == "panda_unavailable"
  assert not cs.panda_controls_allowed
