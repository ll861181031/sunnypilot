import pytest

from openpilot.cereal import log
from opendbc.car import gen_empty_fingerprint, structs
from opendbc.car.byd.interface import CarInterface
from opendbc.car.byd.values import CAR
from opendbc.car import DT_CTRL
from openpilot.selfdrive.car.car_events import BYD_CANCEL_LATCH_TIMEOUT_S, CarEvents
from openpilot.selfdrive.selfdrived.events import ET
from openpilot.selfdrive.selfdrived.state import State, StateMachine


@pytest.fixture
def rig():
  cp = CarInterface.get_params(CAR.BYD_SONG_PLUS_DMI_23_ALT, gen_empty_fingerprint(), [], True, False, False)
  cs = structs.CarState(canValid=True, gearShifter="drive", cruiseState={"enabled": True, "available": True})
  cc = structs.CarControl(enabled=True, longActive=True)
  events = CarEvents(cp)
  events.update(cs, cs, cc)
  return events, cs, cc


def test_cancel_alert_waits_for_valid_stock_disengagement(rig):
  events, cs, cc = rig
  cs.vEgoRaw = 10.0
  cc.enabled = cc.longActive = False
  cc.cruiseControl.cancel = True
  result = events.update(cs, cs, cc)
  name = log.OnroadEvent.EventName.stockAccStillActive
  assert name in result.names
  # Cancellation must remain visible when openpilot is already disabled.
  assert result.contains(ET.PERMANENT)
  cc.cruiseControl.cancel = False
  assert name in events.update(cs, cs, cc).names
  cs.cruiseState.enabled = False
  cs.canValid = False
  assert name in events.update(cs, cs, cc).names
  cs.canValid = True
  assert name not in events.update(cs, cs, cc).names


def test_cancel_alert_self_releases_after_timeout(rig):
    events, cs, cc = rig
    name = log.OnroadEvent.EventName.stockAccStillActive
    cs.vEgoRaw = 10.0
    cc.cruiseControl.cancel = True
    assert name in events.update(cs, cs, cc).names
    cc.cruiseControl.cancel = False
    result = None
    for _ in range(int(BYD_CANCEL_LATCH_TIMEOUT_S / DT_CTRL) + 2):
      result = events.update(cs, cs, cc)
    assert name not in result.names


def test_gas_override_does_not_request_cancellation(rig):
  events, cs, cc = rig
  cc.longActive = False
  cs.gasPressed = True
  assert log.OnroadEvent.EventName.stockAccStillActive not in events.update(cs, cs, cc).names


def test_continuous_cancel_warns_once_per_takeover(rig):
  events, cs, cc = rig
  cs.vEgoRaw = 10.0
  name = log.OnroadEvent.EventName.stockAccStillActive
  window = int(BYD_CANCEL_LATCH_TIMEOUT_S / DT_CTRL)
  for _ in range(2):
    cc.cruiseControl.cancel = True
    for i in range(window + 5):
      assert (name in events.update(cs, cs, cc).names) == (i < window)
      cc.enabled = cc.longActive = False
    cc.cruiseControl.cancel = False
    cc.enabled = cc.longActive = True
    assert name not in events.update(cs, cs, cc).names


def test_unconfirmed_cancel_prevents_reengagement_until_stock_acc_is_off(rig):
  events, cs, cc = rig
  cs.vEgoRaw = 10.0
  state = StateMachine()
  state.state = State.enabled
  cc.cruiseControl.cancel = True
  assert state.update(events.update(cs, cs, cc)) == (False, False)
  cc.enabled = cc.longActive = False
  cc.cruiseControl.cancel = False
  result = events.update(cs, cs, cc)
  result.add(log.OnroadEvent.EventName.pcmEnable)
  assert state.update(result) == (False, False)
  assert ET.NO_ENTRY in state.current_alert_types
  assert ET.PERMANENT in state.current_alert_types

  cs.cruiseState.enabled = False
  state.update(events.update(cs, cs, cc))
  previous = structs.CarState(canValid=True, gearShifter="drive", cruiseState={"enabled": False, "available": True})
  cs.cruiseState.enabled = True
  assert state.update(events.update(cs, previous, cc)) == (True, True)


def test_cancel_during_can_failure_still_warns(rig):
  events, cs, cc = rig
  cs.vEgoRaw = 10.0
  cs.canValid = False
  cc.enabled = cc.longActive = False
  cc.cruiseControl.cancel = True
  assert log.OnroadEvent.EventName.stockAccStillActive in events.update(cs, cs, cc).names


def test_startup_cancel_does_not_prevent_pcm_engagement(rig):
  _, cs, cc = rig
  cp = CarInterface.get_params(CAR.BYD_SONG_PLUS_DMI_23_ALT, gen_empty_fingerprint(), [], True, False, False)
  events = CarEvents(cp)
  cc.enabled = cc.longActive = False
  cc.cruiseControl.cancel = True
  previous = structs.CarState(cruiseState={"enabled": False})
  result = events.update(cs, previous, cc)
  assert log.OnroadEvent.EventName.pcmEnable in result.names
  assert log.OnroadEvent.EventName.stockAccStillActive not in result.names


def test_no_cancel_alert_in_stock_longitudinal_mode(rig):
  events, cs, cc = rig
  events.CP.openpilotLongitudinalControl = False
  cc.enabled = cc.longActive = False
  cc.cruiseControl.cancel = True
  assert log.OnroadEvent.EventName.stockAccStillActive not in events.update(cs, cs, cc).names


@pytest.mark.parametrize("stock_standstill", [False, True])
def test_failed_resume_prompts_for_driver_action(rig, stock_standstill):
  events, cs, cc = rig
  cs.cruiseState.standstill = stock_standstill
  cc.cruiseControl.resume = True
  cc.actuators.accel = -2.0
  for _ in range(25):
    result = events.update(cs, cs, cc)
  assert log.OnroadEvent.EventName.resumeRequired in result.names
  cc.actuators.accel = 0.8
  assert log.OnroadEvent.EventName.resumeRequired not in events.update(cs, cs, cc).names


def test_a_cancel_while_stopped_does_not_latch_the_handoff_alert(rig):
  """Braking a car the stock ACC already stopped is redundant, not a handoff.

  Measured over 80 recorded segments: every multi-second unconfirmed-handoff
  episode began from a car that was already at standstill under ACC hold, where
  the ECU keeps reporting Active and cruiseState.enabled never goes False. The
  alert therefore latched permanently and blocked re-engagement.
  """
  events, cs, cc = rig
  cs.vEgoRaw = 0.0
  cc.enabled = cc.longActive = False
  cc.cruiseControl.cancel = True
  name = log.OnroadEvent.EventName.stockAccStillActive
  assert name not in events.update(cs, cs, cc).names
  cc.cruiseControl.cancel = False
  assert name not in events.update(cs, cs, cc).names


def test_a_cancel_while_stopped_leaves_engagement_available(rig):
  """The same case must not turn into NO_ENTRY, which is what blocked re-engagement."""
  events, cs, cc = rig
  cs.vEgoRaw = 0.0
  state = StateMachine()
  state.state = State.enabled
  cc.cruiseControl.cancel = True
  cc.enabled = cc.longActive = False
  result = events.update(cs, cs, cc)
  result.add(log.OnroadEvent.EventName.pcmEnable)
  assert ET.NO_ENTRY not in state.current_alert_types
  assert state.update(result) == (True, True)


def test_the_latch_threshold_sits_between_standstill_and_creep(rig):
  """0.3 m/s is the port's existing standstill boundary; verify both sides."""
  from openpilot.selfdrive.car.car_events import BYD_CANCEL_LATCH_MIN_SPEED_MS
  assert BYD_CANCEL_LATCH_MIN_SPEED_MS == pytest.approx(0.3)
  events, cs, cc = rig
  cc.enabled = cc.longActive = False
  cc.cruiseControl.cancel = True
  name = log.OnroadEvent.EventName.stockAccStillActive
  cs.vEgoRaw = BYD_CANCEL_LATCH_MIN_SPEED_MS - 0.01
  assert name not in events.update(cs, cs, cc).names
  cs.vEgoRaw = BYD_CANCEL_LATCH_MIN_SPEED_MS + 0.01
  assert name in events.update(cs, cs, cc).names
