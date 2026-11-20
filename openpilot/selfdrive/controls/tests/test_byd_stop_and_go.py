import pytest

from opendbc.car import gen_empty_fingerprint, structs
from opendbc.car.byd.interface import CarInterface
from opendbc.car.byd.values import CAR
from opendbc.car.byd.tests.test_longitudinal_command import ENGAGED_FRAME, template_from
from openpilot.selfdrive.controls.lib.longcontrol import LongControl, LongCtrlState, get_resume_request


@pytest.fixture
def rig():
  cp = CarInterface.get_params(CAR.BYD_SONG_PLUS_DMI_23_ALT, gen_empty_fingerprint(), [], True, False, False)
  cp_sp = structs.CarParamsSP()
  ci = CarInterface(cp, cp_sp)
  ci.CS.out = structs.CarState(canValid=True, standstill=True,
                             cruiseState={"enabled": True, "standstill": True})
  ci.CS.acc_cmd_msg = template_from(ENGAGED_FRAME)
  return ci, LongControl(cp, cp_sp)


def step(ci, control, target, should_stop, tick, active=True):
  accel = float(control.update(active, ci.CS.out, target, should_stop, (-3.5, 2.0)))
  cc = structs.CarControl(enabled=active, longActive=active, actuators={"accel": accel},
                         cruiseControl={"resume": get_resume_request(ci.CP, ci.CS.out, active, should_stop)})
  _, frames = ci.CC.update(cc.as_reader(), structs.CarControlSP(), ci.CS, 1_000_000_000 + tick * 10_000_000)
  return accel, [d for a, d, _ in frames if a == 0x32E]


def test_planner_can_start_from_stock_standstill(rig):
  ci, control = rig
  for tick in range(250):
    step(ci, control, 0.0, True, tick)
  frames = []
  for tick in range(250, 270):
    _, sent = step(ci, control, 0.8, False, tick)
    frames.extend(sent)
  assert frames
  # The frame that leaves the hold asks for 0.00 m/s^2 (raw 0x64) with
  # STANDSTILL_RESUME set, matching the stock capture (the longitudinal notes
  # Phase 5, 29.76 s); the planner's request resumes on the frames after it.
  assert frames[0][0] == 0x64 and frames[0][4] & 0x80
  assert all(d[0] == 116 and d[4] & 0x80 for d in frames[1:])
  assert control.long_control_state == LongCtrlState.pid


@pytest.mark.parametrize("stock_reports_motion", [False, True])
def test_unconfirmed_launch_times_out_and_stays_held(rig, stock_reports_motion):
  ci, control = rig
  for tick in range(20):
    step(ci, control, 0.0, True, tick)
  frames = []
  for tick in range(20, 620):
    if tick == 40 and stock_reports_motion:
      ci.CS.out.cruiseState.standstill = False
    _, sent = step(ci, control, 0.8, False, tick)
    frames.extend(sent)
  assert any(d[0] > 100 for d in frames[:20])
  assert all(d[0] == 0 and not d[4] & 0x80 for d in frames[-50:])
  assert control.long_control_state == LongCtrlState.stopping
  # A fluctuating planner must not renew an exhausted launch budget.
  for tick in range(620, 720):
    _, sent = step(ci, control, 0.8, tick % 2 == 0, tick)
    assert all(d[0] == 0 and not d[4] & 0x80 for d in sent)


def test_observed_motion_completes_launch(rig):
  ci, control = rig
  for tick in range(20):
    step(ci, control, 0.0, True, tick)
  for tick in range(20, 50):
    step(ci, control, 0.8, False, tick)
  ci.CS.out.vEgo = ci.CS.out.vEgoRaw = 1.2
  ci.CS.out.standstill = False
  ci.CS.out.cruiseState.standstill = False
  last_frames = []
  for tick in range(50, 700):
    accel, frames = step(ci, control, 0.8, False, tick)
    assert accel == pytest.approx(0.8)
    if frames:
      last_frames = frames
  # STANDSTILL_RESUME is held for the stock's 3 s after the hold is released and then
  # clears while the car keeps driving (the longitudinal notes Phase 5) -- it is no
  # longer gated on passing 1.0 m/s.
  assert last_frames
  assert all(d[0] == 116 for d in last_frames)
  assert not last_frames[-1][4] & 0x80


@pytest.mark.parametrize("guard", ["gasPressed", "brakePressed", "canTimeout", "invalid_can", "cruise_off"])
def test_byd_resume_requires_healthy_inputs(rig, guard):
  ci, control = rig
  for tick in range(20):
    step(ci, control, 0.0, True, tick)
  if guard == "invalid_can":
    ci.CS.out.canValid = False
  elif guard == "cruise_off":
    ci.CS.out.cruiseState.enabled = False
  else:
    setattr(ci.CS.out, guard, True)
  for tick in range(20, 50):
    accel, frames = step(ci, control, 0.8, False, tick)
    assert accel <= 0
    assert not frames


@pytest.mark.parametrize("change", ["other_car", "stock_longitudinal"])
def test_resume_override_does_not_change_other_configurations(rig, change):
  ci, _ = rig
  if change == "other_car":
    ci.CP.carFingerprint = "OTHER_CAR"
  else:
    ci.CP.openpilotLongitudinalControl = False
  control = LongControl(ci.CP, ci.CP_SP)
  for _ in range(100):
    assert control.update(True, ci.CS.out, 0.8, False, (-3.5, 2.0)) <= 0
  assert control.long_control_state == LongCtrlState.stopping


def test_cancel_stops_transmitting_even_with_stale_long_active(rig):
  ci, control = rig
  ci.CS.out.vEgo = ci.CS.out.vEgoRaw = 10.0
  ci.CS.out.cruiseState.standstill = False
  for tick in range(4):
    step(ci, control, 0.8, False, tick)
  cc = structs.CarControl(enabled=True, longActive=True, actuators={"accel": 0.8}, cruiseControl={"cancel": True})
  for tick in range(4, 10):
    _, frames = ci.CC.update(cc.as_reader(), structs.CarControlSP(), ci.CS, 1_000_000_000 + tick * 10_000_000)
    assert not any(a == 0x32E for a, _, _ in frames)


def test_driver_resume_can_retry_after_timeout(rig):
  ci, control = rig
  for tick in range(20):
    step(ci, control, 0.0, True, tick)
  for tick in range(20, 620):
    step(ci, control, 0.8, False, tick)
  ci.CS.out.buttonEvents = [structs.CarState.ButtonEvent(type="accelCruise", pressed=True)]
  accel, frames = step(ci, control, 0.8, False, 620)
  assert accel > 0
  # The retry's first frame is the stock's 0.00 m/s^2 entry (raw 100 = 0x64), then
  # the planner's positive request; both carry STANDSTILL_RESUME.
  assert frames and all(d[0] >= 100 and d[4] & 0x80 for d in frames)


def test_hold_clears_a_stale_resume_bit_from_the_template(rig):
  ci, control = rig
  ci.CS.acc_cmd_msg["STANDSTILL_RESUME"] = 1
  _, frames = step(ci, control, 0.0, True, 0)
  assert frames and all(d[0] == 0 and not d[4] & 0x80 for d in frames)


def test_interrupted_launch_does_not_carry_its_timer(rig):
  # A launch attempt that stops being valid (should_stop) must not leave its
  # elapsed time behind; otherwise the next attempt is cut short.
  ci, control = rig
  control.long_control_state = LongCtrlState.stopping
  control.resume_from_hold = True
  control.resume_time = 2.0
  control.byd_resume_inputs(True, ci.CS.out, 0.8, True)
  assert control.resume_time == 0.0


def test_resume_request_uses_our_standstill_only_for_byd(rig):
  ci, _ = rig
  ci.CS.out.cruiseState.standstill = False
  assert get_resume_request(ci.CP, ci.CS.out, True, False)
  assert not get_resume_request(ci.CP, ci.CS.out, False, False)
  assert not get_resume_request(ci.CP, ci.CS.out, True, True)
  ci.CP.openpilotLongitudinalControl = False
  assert not get_resume_request(ci.CP, ci.CS.out, True, False)
  ci.CP.openpilotLongitudinalControl = True
  ci.CP.carFingerprint = "OTHER_CAR"
  assert not get_resume_request(ci.CP, ci.CS.out, True, False)
  ci.CS.out.cruiseState.standstill = True
  assert get_resume_request(ci.CP, ci.CS.out, True, False)
