"""The configured longitudinal path produces commands this car can execute.

Runs the production LongControl against the BYD CarParams that CarInterface builds,
then feeds its output through the same clamp the car-side builder applies. What this
pins down is the configuration, not the driving: whether the parameters the port
declares are the ones the controller actually needs, and whether the values it emits
stay inside the envelope the panda accepts.

The on-road question - does the car hold itself on continuous brake pressure - cannot
be answered here and is not claimed by these tests.
"""

from types import SimpleNamespace

import pytest

from opendbc.can import CANPacker
from opendbc.car import Bus, gen_empty_fingerprint
from opendbc.car.byd.bydcanfd import ACC_ACCEL_MAX, ACC_ACCEL_MIN, ACC_CMD_ADDR, ACC_STANDSTILL_HOLD, create_acc_command
from opendbc.car.byd.interface import CarInterface
from opendbc.car.byd.values import CAR, DBC, BydSafetyFlags
from opendbc.car.structs import CarParams, CarParamsSP
from opendbc.safety.tests.libsafety import libsafety_py

from openpilot.selfdrive.controls.lib.longcontrol import LongControl, LongCtrlState


DT = 0.01


def car_params():
  # get_params(candidate, fingerprint, car_fw, alpha_long, is_release, docs)
  return CarInterface.get_params(CAR.BYD_SONG_PLUS_DMI_23_ALT, gen_empty_fingerprint(), [], True, False, False)


def state(v_ego, a_ego=0.0, brake_pressed=False, standstill=False):
  # LongControl.byd_resume_inputs (added 2026-10-14) reads vEgoRaw, canValid,
  # canTimeout, gasPressed, buttonEvents and cruiseState.enabled as well. Without
  # them every test in this file raises AttributeError instead of exercising the
  # code, so the fake has to carry the whole surface that method touches.
  return SimpleNamespace(vEgo=v_ego, vEgoRaw=v_ego, aEgo=a_ego, brakePressed=brake_pressed,
                         gasPressed=False, canValid=True, canTimeout=False, buttonEvents=[],
                         cruiseState=SimpleNamespace(standstill=standstill, enabled=True))


def test_longitudinal_params_are_configured_not_left_at_defaults():
  cp = car_params()
  assert cp.openpilotLongitudinalControl
  assert cp.safetyConfigs[-1].safetyParam & BydSafetyFlags.LONGITUDINAL.value
  # stopAccel is what the car sees at a standstill, so it has to sit inside the
  # envelope and well clear of the raw 0x00 hold code.
  assert ACC_ACCEL_MIN <= cp.stopAccel < 0
  assert cp.stopAccel > ACC_STANDSTILL_HOLD
  assert cp.longitudinalActuatorDelay > 0
  assert len(cp.longitudinalTuning.kiBP) == len(cp.longitudinalTuning.kiV)


def test_lateral_only_build_keeps_the_flag_clear():
  cp = CarInterface.get_params(CAR.BYD_SONG_PLUS_DMI_23_ALT, gen_empty_fingerprint(), [], False, False, False)
  assert not cp.openpilotLongitudinalControl
  assert cp.safetyConfigs[-1].safetyParam == 0


def run_commands(targets, cycles_each=20):
  """Feed a range of planner targets through LongControl and collect its output."""
  cp = car_params()
  cp_sp = CarParamsSP()
  lc = LongControl(cp, cp_sp)
  limits = (ACC_ACCEL_MIN, ACC_ACCEL_MAX)

  outputs = []
  for target in targets:
    for _ in range(cycles_each):
      cs = state(6.0, 0.0)
      lc.update(True, cs, target, False, limits)
      outputs.append(float(lc.last_output_accel))
  return lc, outputs


def run_stopping_hold(cycles=400):
  """Hold the controller in its stopping state from a standstill.

  This is the state that matters for stop-and-go: the car sits here for the whole
  stop, so whatever it emits is the command the car holds itself with.
  """
  cp = car_params()
  cp_sp = CarParamsSP()
  lc = LongControl(cp, cp_sp)
  limits = (ACC_ACCEL_MIN, ACC_ACCEL_MAX)

  outputs = []
  for _ in range(cycles):
    cs = state(0.0, 0.0, standstill=True)
    lc.update(True, cs, -1.0, True, limits)
    outputs.append(float(lc.last_output_accel))
  return lc, outputs


def test_every_output_stays_inside_the_envelope():
  targets = [-9.0, ACC_ACCEL_MIN, -2.0, -0.5, 0.0, 0.5, ACC_ACCEL_MAX, 9.0]
  _lc, outputs = run_commands(targets)
  assert outputs
  for accel in outputs:
    assert ACC_ACCEL_MIN - 1e-6 <= accel <= ACC_ACCEL_MAX + 1e-6, f"{accel} is outside the wire envelope"


def test_the_hold_code_is_never_produced():
  # The controller must not be able to emit raw 0x00 even if asked to: the envelope
  # floor sits above the hold code by design, so a planner request cannot reach it.
  _lc, commands = run_commands([-9.0])
  _lc2, stopping = run_stopping_hold()
  for accel in commands + stopping:
    assert accel > ACC_STANDSTILL_HOLD


def test_stopping_state_converges_to_the_configured_stop_accel():
  cp = car_params()
  lc, outputs = run_stopping_hold()
  assert lc.long_control_state == LongCtrlState.stopping
  # The ramp is 1 m/s^2/s from zero, so it needs about two seconds to reach -2.0.
  assert outputs[0] == pytest.approx(0.0, abs=0.05)
  assert outputs[-1] == pytest.approx(cp.stopAccel, abs=1e-6), \
    "the value held at a standstill must be the configured stopAccel"


def test_stop_accel_is_representable_and_accepted_by_the_panda():
  # The standstill value is the one the car sits on for the whole stop, so it is the
  # most important single frame in the stream.
  cp = car_params()
  packer = CANPacker(DBC[CAR.BYD_SONG_PLUS_DMI_23_ALT][Bus.pt])
  template = {"ACCEL_CMD": 0.0, "ACC_CONTROLLABLE_AND_ON": 1.0, "ACC_REQ_NOT_STANDSTILL": 1.0,
              "ACC_OVERRIDE_OR_STANDSTILL": 0.0, "BRAKE_BEHAVIOR": 0.0, "COUNTER": 0.0,
              "CHECKSUM": 0.0, "ComfortBandLower": 0.1, "ComfortBandUpper": 0.1,
              "ESP_BEHAVIOR": 3.0, "JerkLowerLimit": -4.6, "JerkUpperLimit": 4.4,
              "SET_ME_1": 1.0, "SET_ME_XF": 15.0, "STANDSTILL_RESUME": 0.0, "STANDSTILL_STATE": 0.0}

  safety = libsafety_py.libsafety
  assert 0 == safety.set_safety_hooks(CarParams.SafetyModel.byd, BydSafetyFlags.LONGITUDINAL.value)
  safety.set_controls_allowed(True)

  addr, dat, bus = create_acc_command(packer, template, cp.stopAccel, 3)
  assert addr == ACC_CMD_ADDR
  assert dat[0] != 0x00
  assert safety.safety_tx_hook(libsafety_py.make_CANPacket(addr, bus, dat)), \
    "the panda rejects the acceleration the car is held with"
