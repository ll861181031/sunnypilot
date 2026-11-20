import importlib
import inspect
from dataclasses import replace
from importlib.util import find_spec

import pytest
import zstandard

from opendbc.car.structs import car
from openpilot.cereal import log
from openpilot.selfdrive.controls.lib import longcontrol
from openpilot.tools.car_porting.byd_longitudinal_shadow import ShadowInputs
from openpilot.tools.car_porting.tests.test_byd_longitudinal_replay import bundle, can, message, params

from openpilot.tools.car_porting.runtime_support import (
  PLAN_TARGET_HELP, plan_target_fields_present,
)

pytestmark = pytest.mark.skipif(
  not plan_target_fields_present(), reason=PLAN_TARGET_HELP)



def module():
  name = 'openpilot.tools.car_porting.byd_longcontrol_replay'
  assert find_spec(name) is not None, 'Missing real LongControl offline replay'
  return importlib.import_module(name)


def cp_event():
  event = params()
  cp = event.carParams
  cp.longitudinalTuning.kpBP = [0.0]
  cp.longitudinalTuning.kpV = [0.5]
  cp.longitudinalTuning.kiBP = [0.0]
  cp.longitudinalTuning.kiV = [0.1]
  cp.longitudinalTuning.kf = 1.0
  cp.vEgoStarting = 0.5
  cp.stopAccel = -0.5
  cp.stoppingDecelRate = 0.8
  return event


def inputs(now=1_000_000_000, **changes):
  sample = ShadowInputs(now, now, now, now, now, now, now, True, 3, 10., 'drive',
                        False, False, False, False, .1, 11., 0.)
  return replace(sample, **changes)


def state_plan():
  cs = car.CarState.new_message(vEgo=10, aEgo=0, gearShifter='drive', canValid=True)
  plan = log.LongitudinalPlan.new_message(aTarget=.1, vTargetNow=11, jTargetNow=0, allowThrottle=True, allowBrake=True)
  return cs, plan, log.RadarState.new_message()


def bomb(*args, **kwargs):
  raise AssertionError('Offline evaluation accessed production Params')


def test_injected_params_avoid_default_storage(monkeypatch):
  assert 'params' in inspect.signature(longcontrol.LongControl).parameters, 'Missing isolated parameter dependency'
  monkeypatch.setattr(longcontrol, 'Params', bomb)
  cp = cp_event().carParams
  controller = longcontrol.LongControl(cp, params=object())
  cs, plan, radar = state_plan()
  assert controller.update(True, cs, plan, (-2, 1), 0, radar)[0] == pytest.approx(.601)


def test_real_pid_integrates_at_fixed_dt_without_mutating_recorded_cp(monkeypatch):
  m = module()
  monkeypatch.setattr(longcontrol, 'Params', bomb)
  cp = cp_event().carParams
  before = cp.to_dict()
  controller = m.OfflineLongControl(cp)
  cs, plan, radar = state_plan()
  assert controller.step(inputs(), cs, plan, radar).accel_mps2 is None
  assert controller.step(inputs(1_010_000_000), cs, plan, radar).accel_mps2 == pytest.approx(.601)
  assert controller.step(inputs(1_020_000_000), cs, plan, radar).accel_mps2 == pytest.approx(.602)
  for i in range(3, 120):
    result = controller.step(inputs(1_000_000_000 + i * 10_000_000), cs, plan, radar)
  assert result.accel_mps2 == pytest.approx(.719)
  assert cp.to_dict() == before and not cp.openpilotLongitudinalControl
  assert controller.controller.pid.i == pytest.approx(.119)


@pytest.mark.parametrize('fault', ['brake', 'nan_accel', 'stale', 'aeb', 'gap', 'throttle_inhibited'])
def test_fault_withdraws_output_and_requires_healthy_disengagement(fault):
  controller = module().OfflineLongControl(cp_event().carParams)
  cs, plan, radar = state_plan()
  controller.step(inputs(), cs, plan, radar)
  controller.step(inputs(1_010_000_000), cs, plan, radar)
  sample = inputs(1_020_000_000)
  if fault == 'brake':
    cs.brakePressed = True
    sample = replace(sample, brake_pressed=True)
  elif fault == 'nan_accel':
    cs.aEgo = float('nan')
  elif fault == 'stale':
    sample = replace(sample, plan_ns=500_000_000)
  elif fault == 'aeb':
    sample = replace(sample, aeb_or_fcw=True)
  elif fault == 'gap':
    sample = inputs(1_030_000_000)
  else:
    sample = replace(sample, allow_throttle=False, accel_target=0)
    plan.aTarget = 0
    plan.allowThrottle = False
  result = controller.step(sample, cs, plan, radar)
  assert result.accel_mps2 is None
  assert controller.controller.pid.i == 0
  assert controller.controller.last_output_accel == 0
  cs, plan, radar = state_plan()
  now = sample.now_ns
  assert controller.step(inputs(now + 10_000_000), cs, plan, radar).accel_mps2 is None
  controller.step(inputs(now + 20_000_000, enabled=False, cruise_state=2), cs, plan, radar)
  assert controller.step(inputs(now + 30_000_000), cs, plan, radar).state == 'arming'
  assert controller.step(inputs(now + 40_000_000), cs, plan, radar).accel_mps2 == pytest.approx(.601)


@pytest.mark.parametrize('fault', ['empty_gains', 'mismatched', 'nan_gain', 'wrong_car', 'nan_stop'])
def test_invalid_recorded_cp_rejected(fault):
  cp = cp_event().carParams
  if fault == 'empty_gains':
    cp.longitudinalTuning.kpBP = []
    cp.longitudinalTuning.kpV = []
  elif fault == 'mismatched':
    cp.longitudinalTuning.kiBP = [0, 1]
  elif fault == 'nan_gain':
    cp.longitudinalTuning.kpV = [float('nan')]
  elif fault == 'wrong_car':
    cp.carFingerprint = 'different_vehicle'
  else:
    cp.stopAccel = float('nan')
  with pytest.raises(ValueError):
    module().OfflineLongControl(cp)


def replay(events):
  return module().replay_events([e.as_reader() for e in events])


def pid_bundle(now, counter=0, enabled=True):
  b = bundle(now, counter, enabled)
  b[2].longitudinalPlan.aTarget = .1
  b[2].longitudinalPlan.vTargetNow = 11
  return b


def test_resampling_uses_only_past_inputs_with_literal_pid_outputs():
  out = replay([cp_event(), *pid_bundle(1_000_000_000), *pid_bundle(1_020_000_000, 1), *pid_bundle(1_040_000_000, 2)])
  assert out['recorded_carcontrol_ticks'] == 3
  assert out['grid_ticks'] == 5
  assert out['pid_output_ticks'] == 4
  assert [e['accel_mps2'] for e in out['output_examples']] == pytest.approx([.601, .602, .603, .604])
  assert out['recorded_long_active_ticks'] == 0
  assert out['control_ready'] is False
  assert out['timing']['dt_ns'] == 10_000_000
  assert out['mode'] == 'historical_open_loop_longcontrol'


def test_future_plan_does_not_leak_to_earlier_grid_tick():
  first = pid_bundle(1_000_000_000)
  second = pid_bundle(1_020_000_000, 1)
  second[2].longitudinalPlan.aTarget = -.5
  out = replay([cp_event(), *first, *second])
  assert [e['accel_mps2'] for e in out['output_examples']] == pytest.approx([.601, .002])


def test_transient_fault_between_grid_ticks_is_not_overwritten():
  events = [cp_event(), *pid_bundle(1_000_000_000), *pid_bundle(1_020_000_000, 1)]
  bad = message('carState', 1_025_000_000, canValid=True, gearShifter='drive', brakePressed=True)
  good = message('carState', 1_026_000_000, canValid=True, vEgo=10, gearShifter='drive')
  good.carState.cruiseState.enabled = True
  out = replay([*events, bad, good, *pid_bundle(1_040_000_000, 2)])
  assert out['pid_output_ticks'] == 2
  assert out['states']['fault_latched'] == 1


def test_carcontrol_dropout_does_not_create_extra_active_steps():
  events = [cp_event()]
  for i in range(21):
    b = pid_bundle(1_000_000_000 + 20_000_000 * i, i % 16)
    events.extend(b if i in (0, 20) else b[:-1])
  out = replay(events)
  assert out['pid_output_ticks'] == 9
  assert out['reasons']['stale_carControl'] >= 1
  assert out['states']['fault_latched'] >= 1


def test_can_only_and_missing_metadata_never_fabricate_a_controller():
  out = replay([can(1_000_000_000), can(1_020_000_000, 1)])
  assert out['can_only'] and out['grid_ticks'] == 0 and out['pid_output_ticks'] == 0
  out = replay(pid_bundle(1_000_000_000))
  assert out['pid_output_ticks'] == 0 and out['identity_valid'] is False


def test_replay_segments_are_isolated_and_output_never_overwrites(tmp_path):
  m = module()
  first, second, output = tmp_path / 'first.zst', tmp_path / 'second.zst', tmp_path / 'result.json'
  first.write_bytes(zstandard.compress(b''.join(e.to_bytes() for e in [cp_event(), *pid_bundle(1_000_000_000),
                                                                      *pid_bundle(1_020_000_000, 1)])))
  second.write_bytes(cp_event().to_bytes() + message('carControl', 1_040_000_000, enabled=True).to_bytes())
  result = m.replay_logs([first, second])
  assert [s['pid_output_ticks'] for s in result['segments']] == [2, 0]
  assert m.main([str(first), '--output', str(output)]) == 0
  before = output.read_bytes()
  with pytest.raises(SystemExit):
    m.main([str(second), '--output', str(output)])
  assert output.read_bytes() == before
  (tmp_path / 'rlog.lock').touch()
  with pytest.raises(ValueError, match='lock'):
    m.replay_logs([first])


def test_conflicting_metadata_and_invalid_control_envelope_are_not_accepted():
  bad_cp = cp_event()
  bad_cp.carParams.longitudinalTuning.kpV = [2]
  out = replay([cp_event(), bad_cp, *pid_bundle(1_000_000_000), *pid_bundle(1_020_000_000, 1)])
  assert not out['identity_valid'] and out['pid_output_ticks'] == 0
  bad = message('carControl', 1_015_000_000, enabled=True)
  bad.valid = False
  out = replay([cp_event(), *pid_bundle(1_000_000_000), bad, *pid_bundle(1_020_000_000, 1), *pid_bundle(1_040_000_000, 2)])
  assert out['pid_output_ticks'] == 1


@pytest.mark.parametrize('field,value', [('softHoldActive', 1), ('aEgo', float('nan'))])
def test_transient_hold_or_invalid_acceleration_is_retained(field, value):
  events = [cp_event(), *pid_bundle(1_000_000_000), *pid_bundle(1_020_000_000, 1)]
  bad = message('carState', 1_025_000_000, canValid=True, vEgo=10, gearShifter='drive')
  setattr(bad.carState, field, value)
  bad.carState.cruiseState.enabled = True
  good = message('carState', 1_026_000_000, canValid=True, vEgo=10, gearShifter='drive')
  good.carState.cruiseState.enabled = True
  out = replay([*events, bad, good, *pid_bundle(1_040_000_000, 2)])
  assert out['pid_output_ticks'] == 2
  assert out['states']['fault_latched'] == 1


@pytest.mark.parametrize('kind', ['gas', 'brake', 'hold', 'stop', 'aeb', 'throttle', 'stopped_carstate'])
def test_adapter_does_not_trust_disagreement_with_real_controller_inputs(kind):
  controller = module().OfflineLongControl(cp_event().carParams)
  cs, plan, radar = state_plan()
  controller.step(inputs(), cs, plan, radar)
  controller.step(inputs(1_010_000_000), cs, plan, radar)
  if kind == 'gas':
    cs.gasPressed = True
  elif kind == 'brake':
    cs.brakePressed = True
  elif kind == 'hold':
    cs.softHoldActive = 1
  elif kind == 'stop':
    plan.shouldStop = True
  elif kind == 'aeb':
    cs.stockAeb = True
  elif kind == 'stopped_carstate':
    cs.vEgo = 0
  else:
    plan.allowThrottle = False
  assert controller.step(inputs(1_020_000_000), cs, plan, radar).accel_mps2 is None


def test_inhibited_pid_result_does_not_advance_controller_clock_twice():
  controller = module().OfflineLongControl(cp_event().carParams)
  cs, plan, radar = state_plan()
  plan.aTarget = 0
  controller.step(inputs(accel_target=0), cs, plan, radar)
  controller.step(inputs(1_010_000_000, accel_target=0), cs, plan, radar)
  plan.allowThrottle = False
  result = controller.step(inputs(1_020_000_000, accel_target=0, allow_throttle=False), cs, plan, radar)
  assert result.accel_mps2 is None
  assert controller.controller.readParamCount == 3


def test_starting_state_is_not_reported_as_a_tracking_command():
  cp = cp_event().carParams
  cp.startingState = True
  cp.startAccel = .5
  controller = module().OfflineLongControl(cp)
  cs, plan, radar = state_plan()
  controller.step(inputs(), cs, plan, radar)
  result = controller.step(inputs(1_010_000_000), cs, plan, radar)
  assert result.accel_mps2 is None
  assert result.reason == 'stop_resume_unverified'


def test_successful_file_replay_uses_no_params_sockets_or_usb(tmp_path, monkeypatch):
  import socket
  import usb1
  from openpilot.cereal import messaging

  m = module()
  source = tmp_path / 'rlog.zst'
  source.write_bytes(zstandard.compress(b''.join(e.to_bytes() for e in [cp_event(), *pid_bundle(1_000_000_000),
                                                                        *pid_bundle(1_020_000_000, 1)])))
  monkeypatch.setattr(longcontrol, 'Params', bomb)
  monkeypatch.setattr(socket, 'socket', bomb)
  monkeypatch.setattr(usb1, 'USBContext', bomb)
  for name in ('PubMaster', 'SubMaster', 'pub_sock', 'sub_sock'):
    monkeypatch.setattr(messaging, name, bomb)
  result = m.replay_logs([source])
  assert result['pid_output_ticks'] == 2
  assert result['segments'][0]['recorded_cp']['openpilotLongitudinalControl'] is False


def test_default_runtime_parameter_dependency_is_unchanged(monkeypatch):
  sentinel = object()
  monkeypatch.setattr(longcontrol, 'Params', lambda: sentinel)
  assert longcontrol.LongControl(cp_event().carParams).params is sentinel


@pytest.mark.parametrize('fault', ['remote', 'corrupt', 'duplicate', 'backwards', 'huge_span'])
def test_bad_replay_inputs_fail_closed_without_output(tmp_path, fault):
  m = module()
  output = tmp_path / 'result.json'
  path = tmp_path / 'log'
  if fault == 'remote':
    with pytest.raises(SystemExit):
      m.main(['https://example.com/rlog.zst', '--output', str(output)])
  elif fault == 'corrupt':
    path.write_bytes(zstandard.compress(cp_event().to_bytes())[:-1])
    with pytest.raises(SystemExit):
      m.main([str(path), '--output', str(output)])
  elif fault == 'duplicate':
    path.write_bytes(cp_event().to_bytes())
    with pytest.raises(SystemExit):
      m.main([str(path), str(path), '--output', str(output)])
  elif fault == 'backwards':
    with pytest.raises(ValueError, match='monotonic'):
      replay(list(reversed(pid_bundle(1_000_000_000))))
  else:
    with pytest.raises(ValueError, match='600 s'):
      replay([cp_event(), *pid_bundle(1_000_000_000), *pid_bundle(602_000_000_000)])
  assert not output.exists()
