"""Offline contracts: these tests must never access vehicle hardware or Params."""

import importlib
from dataclasses import replace
from importlib.util import find_spec

import pytest


def modules():
  names = ('byd_longitudinal_shadow', 'byd_longitudinal_candidate')
  for name in names:
    assert find_spec(f'openpilot.tools.car_porting.{name}') is not None, f'Missing offline feature: {name}'
  return tuple(importlib.import_module(f'openpilot.tools.car_porting.{name}') for name in names)


def inputs(module, now=1_000_000_000, **kwargs):
  values = {'now_ns': now, 'plan_ns': now, 'model_ns': now, 'radar_ns': now, 'plan_model_ns': now,
            'radar_model_ns': now, 'vehicle_ns': now, 'enabled': True, 'cruise_state': 3, 'speed_mps': 10.0,
            'gear': 'drive', 'gas_pressed': False, 'brake_pressed': False, 'should_stop': False, 'aeb_or_fcw': False,
            'accel_target': 0.8, 'speed_target': 10.0, 'jerk_target': 0.0, 'input_fault': ''}
  return module.ShadowInputs(**(values | kwargs))


def test_acceleration_arms_at_zero_and_slews_by_elapsed_time():
  shadow, _ = modules()
  ctrl = shadow.ShadowController()
  first = ctrl.update(inputs(shadow))
  assert first.state == 'arming' and first.accel_mps2 is None
  second = ctrl.update(inputs(shadow, 1_020_000_000))
  assert second.state == 'tracking'
  assert second.accel_mps2 == pytest.approx(0.02)
  assert second.control_ready is False
  third = ctrl.update(inputs(shadow, 1_070_000_000, accel_target=-2.0))
  assert third.accel_mps2 == pytest.approx(-0.03)


@pytest.mark.parametrize('target,want', [(100.0, 1.0), (-100.0, -2.0)])
def test_offline_envelope_clamps_target_without_using_capture_extrema(target, want):
  shadow, _ = modules()
  ctrl = shadow.ShadowController()
  out = None
  for i in range(151):
    out = ctrl.update(inputs(shadow, 1_000_000_000 + i * 20_000_000, accel_target=target))
  assert out.accel_mps2 == pytest.approx(want)
  assert out.target_clipped


def test_optional_speed_feedback_is_explicit_and_has_no_integrator():
  shadow, _ = modules()
  ctrl = shadow.ShadowController(shadow.ShadowConfig(speed_kp=0.2))
  for i in range(101):
    out = ctrl.update(inputs(shadow, 1_000_000_000 + i * 20_000_000, accel_target=0, speed_target=12))
  assert out.accel_mps2 == pytest.approx(0.4)


@pytest.mark.parametrize('change,reason', [
  ({'brake_pressed': True}, 'brake_pressed'),
  ({'aeb_or_fcw': True}, 'aeb_or_fcw'),
  ({'gear': 'reverse'}, 'not_in_drive'),
  ({'input_fault': 'template_checksum'}, 'template_checksum'),
  ({'accel_target': float('nan')}, 'nonfinite_input'),
  ({'speed_target': float('inf')}, 'nonfinite_input'),
  ({'jerk_target': float('-inf')}, 'nonfinite_input'),
  ({'speed_mps': -1}, 'negative_speed'),
  ({'cruise_state': 15}, 'unsupported_cruise_state'),
])
def test_fault_suppresses_output_and_requires_healthy_disengagement(change, reason):
  shadow, _ = modules()
  ctrl = shadow.ShadowController()
  ctrl.update(inputs(shadow))
  ctrl.update(inputs(shadow, 1_020_000_000))
  bad = ctrl.update(inputs(shadow, 1_040_000_000, **change))
  assert bad.accel_mps2 is None and bad.reason == reason
  assert ctrl.update(inputs(shadow, 1_060_000_000)).state == 'fault_latched'
  assert ctrl.update(inputs(shadow, 1_080_000_000, enabled=False)).state == 'off'
  assert ctrl.update(inputs(shadow, 1_100_000_000)).state == 'arming'
  assert ctrl.update(inputs(shadow, 1_120_000_000)).accel_mps2 == pytest.approx(0.02)


@pytest.mark.parametrize('field,age', [('plan_ns', 500_000_000), ('model_ns', 500_000_000),
                                    ('radar_ns', 500_000_000), ('plan_model_ns', 500_000_000),
                                    ('radar_model_ns', 500_000_000), ('vehicle_ns', 100_000_000)])
def test_freshness_boundary_is_exclusive(field, age):
  shadow, _ = modules()
  out = shadow.ShadowController().update(inputs(shadow, **{field: 1_000_000_000 - age}))
  assert out.accel_mps2 is None and out.reason == f'stale_or_future_{field}'


@pytest.mark.parametrize('stamp', [0, 1_000_000_001])
def test_missing_or_future_source_timestamp_is_not_healthy(stamp):
  shadow, _ = modules()
  out = shadow.ShadowController().update(inputs(shadow, model_ns=stamp))
  assert out.reason == 'stale_or_future_model_ns'


@pytest.mark.parametrize('delta', [0, -1, 100_000_001])
def test_invalid_tick_spacing_never_creates_a_large_acceleration_step(delta):
  shadow, _ = modules()
  ctrl = shadow.ShadowController()
  ctrl.update(inputs(shadow))
  out = ctrl.update(inputs(shadow, 1_000_000_000 + delta))
  assert out.accel_mps2 is None and out.reason == 'tick_discontinuity'


@pytest.mark.parametrize('change', [{'gas_pressed': True}, {'cruise_state': 5}])
def test_gas_override_removes_candidate_and_release_rearms_at_zero(change):
  shadow, _ = modules()
  ctrl = shadow.ShadowController()
  ctrl.update(inputs(shadow))
  ctrl.update(inputs(shadow, 1_020_000_000))
  assert ctrl.update(inputs(shadow, 1_040_000_000, **change)).state == 'override'
  assert ctrl.update(inputs(shadow, 1_060_000_000)).state == 'arming'
  assert ctrl.update(inputs(shadow, 1_080_000_000)).accel_mps2 == pytest.approx(0.02)


@pytest.mark.parametrize('change', [{'speed_mps': 0.0}, {'cruise_state': 6}, {'cruise_state': 7}, {'should_stop': True}])
def test_unknown_stop_and_resume_protocol_never_generates_hold_command(change):
  shadow, _ = modules()
  ctrl = shadow.ShadowController()
  ctrl.update(inputs(shadow))
  ctrl.update(inputs(shadow, 1_020_000_000))
  out = ctrl.update(inputs(shadow, 1_040_000_000, **change))
  assert out.state == 'protocol_blocked' and out.accel_mps2 is None
  assert ctrl.update(inputs(shadow, 1_060_000_000)).state == 'fault_latched'


@pytest.mark.parametrize('change', [{'enabled': False}, {'cruise_state': 2}, {'cruise_state': 0}])
def test_disable_has_no_zero_acceleration_packet(change):
  shadow, _ = modules()
  out = shadow.ShadowController().update(inputs(shadow, **change))
  assert out.state == 'off' and out.accel_mps2 is None


@pytest.mark.parametrize('change', [{'jerk_up': 0}, {'jerk_down': -1}, {'accel_min': -5},
                                  {'accel_max': float('nan')}, {'speed_kp': -1}, {'accel_min': 1}])
def test_bad_simulation_configuration_is_rejected(change):
  shadow, _ = modules()
  with pytest.raises(ValueError):
    replace(shadow.ShadowConfig(), **change)


def test_literal_captured_template_and_checksum_round_trip():
  _, candidate = modules()
  raw = bytes.fromhex('5366669937dcf044')
  result = candidate.encode_candidate(raw, -0.85, 0)
  assert result.payload_hex == '5366669937dcf044'
  assert result.quantized_accel_mps2 == pytest.approx(-0.85)
  assert result.control_ready is False
  # Counter 15 and -1 m/s2: only bytes 0, 6 and 7 may change.
  changed = candidate.encode_candidate(raw, -1.0, 15)
  assert changed.payload_hex == '5066669937dcff38'
  assert not hasattr(changed, 'bus')


@pytest.mark.parametrize('value,want', [(-0.875, -0.85), (-0.826, -0.85), (-0.824, -0.8), (0, 0)])
def test_candidate_quantization_has_explicit_half_up_rounding(value, want):
  _, candidate = modules()
  result = candidate.encode_candidate(bytes.fromhex('5366669937dcf044'), value, 0)
  assert result.quantized_accel_mps2 == pytest.approx(want)


@pytest.mark.parametrize('payload', ['00666699b7f0f7fc', '5766669937ddf738', '64666699b7d8f8af',
                                   '5366669937dcf0', '5366669937dcf000', '5367669937dcf043'])
def test_special_or_malformed_templates_are_refused(payload):
  _, candidate = modules()
  with pytest.raises(ValueError):
    candidate.encode_candidate(bytes.fromhex(payload), 0, 0)


@pytest.mark.parametrize('value', [-5, -5.01, 7.751, float('nan'), float('inf')])
def test_reserved_zero_overflow_and_nonfinite_acceleration_are_refused(value):
  _, candidate = modules()
  with pytest.raises(ValueError):
    candidate.encode_candidate(bytes.fromhex('5366669937dcf044'), value, 0)


@pytest.mark.parametrize('counter', [-1, 16, 1.5, True])
def test_counter_must_be_explicit_four_bit_integer(counter):
  _, candidate = modules()
  with pytest.raises(ValueError):
    candidate.encode_candidate(bytes.fromhex('5366669937dcf044'), 0, counter)


@pytest.mark.parametrize('changes,reason', [({'allow_throttle': False, 'accel_target': 0.8}, 'plan_throttle_inhibited'),
                                          ({'allow_brake': False, 'accel_target': -1.0}, 'plan_brake_inhibited')])
def test_plan_permissions_do_not_leave_previous_acceleration_running(changes, reason):
  shadow, _ = modules()
  ctrl = shadow.ShadowController()
  ctrl.update(inputs(shadow))
  ctrl.update(inputs(shadow, 1_020_000_000))
  out = ctrl.update(inputs(shadow, 1_040_000_000, **changes))
  assert out.accel_mps2 is None and out.reason == reason


def test_computed_target_overflow_is_not_saturated_into_valid_command():
  shadow, _ = modules()
  ctrl = shadow.ShadowController(shadow.ShadowConfig(speed_kp=1e308))
  ctrl.update(inputs(shadow))
  out = ctrl.update(inputs(shadow, 1_020_000_000, speed_target=1e308))
  assert out.accel_mps2 is None and out.reason == 'nonfinite_computed_target'


def test_simultaneous_gas_and_stop_does_not_bypass_protocol_latch():
  shadow, _ = modules()
  ctrl = shadow.ShadowController()
  ctrl.update(inputs(shadow))
  ctrl.update(inputs(shadow, 1_020_000_000))
  out = ctrl.update(inputs(shadow, 1_040_000_000, gas_pressed=True, should_stop=True))
  assert out.state == 'protocol_blocked'
  assert ctrl.update(inputs(shadow, 1_060_000_000)).state == 'fault_latched'


@pytest.mark.parametrize('payload', [
  '646666903f48fabe',  # 2026-09-24 14:27:57.373: HUD 3->1, request bit remains 1.
  '646666903fd0f937',  # 14:27:17.679: HUD 3->2, on bit remains 1, request bit 0.
  '6a66669639e8f31f',  # 14:29:29.706: driver gas override, candidate request nonzero.
  '00666699b7f0f7fc',  # Recorded hold/resume candidate, raw acceleration zero.
])
def test_captured_withdrawal_override_and_hold_cannot_seed_moving_candidate(payload):
  _, candidate = modules()
  with pytest.raises(ValueError):
    candidate.encode_candidate(bytes.fromhex(payload), -1.0, 0)


def test_captured_brake_exit_request_bit_is_not_alone_a_validity_gate():
  _, candidate = modules()
  observed = candidate.decode_frame(0x32E, bytes.fromhex('646666903f48fabe'))
  assert observed['ACC_REQ_NOT_STANDSTILL'] == 1
  assert observed['ACC_CONTROLLABLE_AND_ON'] == 0
  assert observed['ACCEL_CMD'] == 0
  with pytest.raises(ValueError):
    candidate.validate_template(bytes.fromhex('646666903f48fabe'))
