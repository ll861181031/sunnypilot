import importlib
import json
from importlib.util import find_spec

import pytest
import zstandard

from openpilot.cereal import log
from opendbc.can import CANPacker

from openpilot.tools.car_porting.runtime_support import (
  PLAN_TARGET_HELP, plan_target_fields_present,
)

pytestmark = pytest.mark.skipif(
  not plan_target_fields_present(), reason=PLAN_TARGET_HELP)



def replay_module():
  name = 'openpilot.tools.car_porting.byd_longitudinal_replay'
  assert find_spec(name) is not None, 'Missing offline replay feature'
  return importlib.import_module(name)


def message(kind, now, **values):
  event = log.Event.new_message(logMonoTime=now, valid=True)
  out = event.init(kind)
  for name, value in values.items():
    setattr(out, name, value)
  return event


def params(fingerprint='BYD_SONG_PLUS_DMI_23_ALT'):
  return message('carParams', 990_000_000, carFingerprint=fingerprint, openpilotLongitudinalControl=False)


def can(now, counter=0, state=3, pedal=0, template='5366669937dcf044'):
  packer = CANPacker('byd_song_plus_dmi_23_alt')
  frames = [packer.make_can_msg('ACC_HUD_ADAS', 2, {'CRUISE_STATE': state, 'COUNTER': counter}),
            packer.make_can_msg('WHEEL_SPEED', 0, {**dict.fromkeys(('WHEELSPEED_FL', 'WHEELSPEED_FR', 'WHEELSPEED_BL',
                                                               'WHEELSPEED_BR'), 36), 'COUNTER': counter}),
            (0x342, bytes([pedal, 0, 0, 0, 0, 0, 0, 0]), 0)]
  raw = bytearray.fromhex(template)
  raw[6] = (raw[6] & 0xF0) | counter
  raw[7] = (255 - sum(raw[:7])) & 255
  frames.append((0x32E, bytes(raw), 2))
  event = log.Event.new_message(logMonoTime=now, valid=True)
  out = event.init('can', len(frames))
  for dst, (address, payload, bus) in zip(out, frames, strict=True):
    dst.address, dst.dat, dst.src = address, payload, bus
  return event


def bundle(now, counter=0, enabled=True):
  model = message('modelV2', now - 3_000_000)
  radar = message('radarState', now - 2_000_000, mdMonoTime=model.logMonoTime)
  plan = message('longitudinalPlan', now - 1_000_000, modelMonoTime=model.logMonoTime,
                 aTarget=0.8, vTargetNow=10, jTargetNow=0, shouldStop=False, allowThrottle=True, allowBrake=True)
  cs = message('carState', now - 1_000_000, canValid=True, vEgo=10, gearShifter='drive')
  cs.carState.cruiseState.enabled = enabled
  sd = message('selfdriveState', now - 1_000_000, enabled=enabled, active=enabled, state='enabled' if enabled else 'disabled')
  cc = message('carControl', now + 1_000_000, enabled=enabled, latActive=enabled, longActive=False)
  return [model, radar, plan, cs, sd, can(now, counter, state=3 if enabled else 2), cc]


def run(events):
  return replay_module().replay_events([e.as_reader() for e in events])


def test_recorded_plan_drives_offline_candidates_without_claiming_op_actuation():
  out = run([params(), *bundle(1_000_000_000), *bundle(1_020_000_000, 1), *bundle(1_040_000_000, 2)])
  assert out['candidate_count'] == 2
  assert out['calculation_ticks'] == 3
  assert out['recorded_long_active_ticks'] == 0
  assert out['candidate_examples'][0]['counter'] == 2
  assert out['candidate_examples'][1]['counter'] == 3
  assert out['control_ready'] is False
  assert out['states'] == {'arming': 1, 'tracking': 2}


def test_candidate_counter_wrap_and_output_cadence():
  out = run([params(), *bundle(1_000_000_000, 13), *bundle(1_020_000_000, 14),
             *bundle(1_030_000_000, 15), *bundle(1_040_000_000, 0)])
  assert out['candidate_count'] == 2
  assert [v['counter'] for v in out['candidate_examples']] == [15, 0]


@pytest.mark.parametrize('kind', ['modelV2', 'radarState', 'longitudinalPlan', 'carState', 'selfdriveState'])
def test_missing_required_service_does_not_fabricate_targets(kind):
  events = [params(), *bundle(1_000_000_000), *bundle(1_020_000_000, 1)]
  out = run([e for e in events if e.which() != kind])
  assert out['candidate_count'] == 0
  assert out['reasons'][f'missing_{kind}'] > 0


@pytest.mark.parametrize('fault', ['invalid_plan', 'future_model', 'stale_model', 'can_invalid',
                                  'radar_fault', 'soft_disabling', 'brake', 'checksum', 'counter_gap',
                                  'duplicate_hud', 'invalid_then_valid_template', 'wheel_fault'])
def test_invalid_inputs_latch_after_tracking(fault):
  first = [params(), *bundle(1_000_000_000), *bundle(1_020_000_000, 1)]
  bad = bundle(1_040_000_000, 2)
  if fault == 'invalid_plan':
    bad[2].valid = False
  elif fault == 'future_model':
    bad[2].longitudinalPlan.modelMonoTime = 2_000_000_000
  elif fault == 'stale_model':
    bad[2].longitudinalPlan.modelMonoTime = 500_000_000
  elif fault == 'can_invalid':
    bad[3].carState.canValid = False
  elif fault == 'radar_fault':
    bad[1].radarState.radarErrors.radarFault = True
  elif fault == 'soft_disabling':
    bad[4].selfdriveState.state = 'softDisabling'
  elif fault == 'brake':
    bad[3].carState.brakePressed = True
  elif fault == 'checksum':
    bad[5].can[3].dat = bytes.fromhex('5366669937dcf200')
  elif fault == 'counter_gap':
    bad[5] = can(1_040_000_000, 4)
  elif fault in ('duplicate_hud', 'invalid_then_valid_template'):
    frames = [f.to_dict() for f in bad[5].can]
    idx = 0 if fault == 'duplicate_hud' else 3
    frames.append(frames[idx].copy())
    if fault == 'invalid_then_valid_template':
      frames[3]['dat'] = bytes.fromhex('5366669937dcf200')
    bad[5].can = frames
  elif fault == 'wheel_fault':
    raw = bytearray(bad[5].can[1].dat)
    raw[1] |= 0x10
    raw[7] = (255 - sum(raw[:7])) & 255
    bad[5].can[1].dat = bytes(raw)
  recovered = [*bundle(1_060_000_000, 3), *bundle(1_080_000_000, 4)]
  out = run([*first, *bad, *recovered])
  assert out['candidate_count'] == 1
  assert out['states'].get('fault_latched', 0) >= 1


@pytest.mark.parametrize('src,kind', [(0, 'can'), (130, 'can'), (2, 'sendcan')])
def test_wrong_bus_echo_or_sendcan_cannot_supply_stock_template(src, kind):
  events = [params()]
  for i in range(2):
    part = bundle(1_000_000_000 + i * 20_000_000, i)
    part[5].can[3].src = src
    if kind == 'sendcan':
      f = [frame.to_dict() for frame in part[5].can]
      replacement = log.Event.new_message(logMonoTime=part[5].logMonoTime, valid=True)
      replacement.init('sendcan', len(f))
      replacement.sendcan = f
      part[5] = replacement
    events.extend(part)
  assert run(events)['candidate_count'] == 0


def test_can_only_capture_reports_observations_without_synthetic_plan():
  out = run([can(1_000_000_000), can(1_020_000_000, 1, state=7, template='00666699b7f0f7fc')])
  assert out['can_only'] is True
  assert out['candidate_count'] == 0
  assert out['stock_command_frames'] == 2
  assert out['cruise_state_frames'] == {'3': 1, '7': 1}


@pytest.mark.parametrize('fingerprint', [None, 'BYD_ATTO3', ''])
def test_missing_or_other_vehicle_identity_is_not_assumed(fingerprint):
  events = [] if fingerprint is None else [params(fingerprint)]
  out = run([*events, *bundle(1_000_000_000), *bundle(1_020_000_000, 1)])
  assert out['candidate_count'] == 0
  assert out['reasons']['unconfirmed_vehicle_identity'] == 2


def test_file_replay_keeps_segments_isolated_and_report_is_exclusive(tmp_path):
  module = replay_module()
  first, second = tmp_path / 'first.zst', tmp_path / 'second.zst'
  first.write_bytes(zstandard.compress(b''.join(e.to_bytes() for e in [params(), *bundle(1_000_000_000), *bundle(1_020_000_000, 1)])))
  second.write_bytes(params().to_bytes() + message('carControl', 1_040_000_000, enabled=True).to_bytes())
  result = module.replay_logs([first, second])
  assert result['segments'][0]['candidate_count'] == 1
  assert result['segments'][1]['candidate_count'] == 0
  assert result['segments'][1]['reasons']['missing_modelV2'] == 1
  output = tmp_path / 'result.json'
  assert module.main([str(first), '--output', str(output)]) == 0
  original = output.read_bytes()
  assert json.loads(original)['control_ready'] is False
  with pytest.raises(SystemExit) as exc:
    module.main([str(second), '--output', str(output)])
  assert exc.value.code == 2 and output.read_bytes() == original


def test_locked_corrupt_and_duplicate_inputs_are_rejected(tmp_path):
  module = replay_module()
  path = tmp_path / 'rlog.zst'
  raw = zstandard.compress(params().to_bytes())
  path.write_bytes(raw[:-1])
  with pytest.raises(ValueError, match='Incomplete'):
    module.replay_logs([path])
  path.write_bytes(raw)
  with pytest.raises(ValueError, match='distinct'):
    module.replay_logs([path, path])
  (tmp_path / 'rlog.lock').touch()
  with pytest.raises(ValueError, match='lock'):
    module.replay_logs([path])


def test_remote_paths_rejected_without_fetch():
  with pytest.raises(ValueError, match='local'):
    replay_module().replay_logs(['https://example.com/rlog.zst'])


def test_observed_hud_error_four_is_opaque_not_automatically_a_fault():
  events = [params()]
  for i in range(3):
    b = bundle(1_000_000_000 + i * 20_000_000, i)
    # Captured HUD state 3 from route 7b38267791 segment 37; ERROR=4.
    raw = bytearray.fromhex('54100401743008ea')
    raw[6] = (raw[6] & 0xF0) | i
    raw[7] = (255 - sum(raw[:7])) & 255
    b[5].can[0].dat = bytes(raw)
    events.extend(b)
  result = run(events)
  assert result['candidate_count'] == 2
  assert result['opaque_hud_error_counts'] == {'4': 3}


@pytest.mark.parametrize('service', ['can', 'carState'])
def test_transient_brake_before_control_tick_is_not_hidden_by_release(service):
  events = [params(), *bundle(1_000_000_000), *bundle(1_020_000_000, 1)]
  if service == 'carState':
    transient = message('carState', 1_025_000_000, canValid=True, brakePressed=True, gearShifter='drive')
  else:
    transient = can(1_025_000_000, 2)
    transient.can[2].dat = bytes.fromhex('000a000000000000')
  next_counter = 3 if service == 'can' else 2
  events.extend([transient, *bundle(1_040_000_000, next_counter), *bundle(1_060_000_000, next_counter + 1)])
  result = run(events)
  assert result['candidate_count'] == 1
  assert result['states']['fault_latched'] == 1


def test_disallowed_plan_throttle_never_creates_positive_candidate():
  events = [params()]
  for i in range(4):
    b = bundle(1_000_000_000 + i * 20_000_000, i)
    b[2].longitudinalPlan.allowThrottle = False
    events.extend(b)
  result = run(events)
  assert result['candidate_count'] == 0
  assert result['reasons']['observed_plan_fault_or_inhibition'] > 0


@pytest.mark.parametrize('change', ['inactive', 'enable_disagreement', 'cruise_disagreement'])
def test_inactive_or_disagreement_does_not_clear_fault_latch(change):
  fault = bundle(1_040_000_000, 2)
  fault[2].valid = False
  middle = bundle(1_060_000_000, 3)
  if change == 'inactive':
    middle[4].selfdriveState.active = False
  elif change == 'enable_disagreement':
    middle[4].selfdriveState.enabled = False
  else:
    middle[3].carState.cruiseState.enabled = False
  result = run([params(), *bundle(1_000_000_000), *bundle(1_020_000_000, 1), *fault, *middle,
                *bundle(1_080_000_000, 4), *bundle(1_100_000_000, 5)])
  assert result['candidate_count'] == 1
  assert result['states']['fault_latched'] >= 2


@pytest.mark.parametrize('service', ['longitudinalPlan', 'radarState'])
def test_source_timestamp_cannot_be_after_its_own_envelope(service):
  events = [params()]
  for i in range(3):
    part = bundle(1_000_000_000 + i * 20_000_000, i)
    e = part[2 if service == 'longitudinalPlan' else 1]
    setattr(getattr(e, service), 'modelMonoTime' if service == 'longitudinalPlan' else 'mdMonoTime', e.logMonoTime + 1)
    events.extend(part)
  assert run(events)['candidate_count'] == 0


def test_same_timestamp_reordered_services_do_not_change_candidates():
  first = [params()]
  second = [params()]
  for i in range(3):
    b = bundle(1_000_000_000 + i * 20_000_000, i)
    for e in b:
      e.logMonoTime = 1_000_000_000 + i * 20_000_000
    first.extend(b)
    second.extend(reversed(b))
  assert run(first)['candidate_examples'] == run(second)['candidate_examples']


def test_file_corruption_never_writes_success_report(tmp_path):
  module = replay_module()
  path, output = tmp_path / 'bad', tmp_path / 'result.json'
  path.write_bytes(params().to_bytes() + b'\xff' * 16)
  with pytest.raises(SystemExit) as exc:
    module.main([str(path), '--output', str(output)])
  assert exc.value.code == 2 and not output.exists()


def test_symlink_to_locked_recording_is_rejected(tmp_path):
  module = replay_module()
  real = tmp_path / 'real'
  real.mkdir()
  p = real / 'log'
  p.write_bytes(params().to_bytes())
  (real / 'rlog.lock').touch()
  link = tmp_path / 'link'
  link.symlink_to(p)
  with pytest.raises(ValueError, match='lock'):
    module.replay_logs([link])


def test_nonmonotonic_input_does_not_reuse_previous_templates():
  result = run([params(), *bundle(1_000_000_000), *bundle(1_020_000_000, 1),
                *bundle(1_010_000_000, 2), *bundle(1_040_000_000, 3)])
  assert result['candidate_count'] == 1
  assert result['reasons']['nonmonotonic_event_time'] >= 1


@pytest.mark.parametrize('kind', ['gas', 'reverse', 'stop', 'nan', 'hold_can', 'gas_can', 'resume_template'])
def test_transient_semantic_states_between_ticks_are_not_overwritten(kind):
  events = [params(), *bundle(1_000_000_000), *bundle(1_020_000_000, 1)]
  t = 1_025_000_000
  counter = 2
  if kind in ('gas', 'reverse'):
    e = message('carState', t, canValid=True, gearShifter='reverse' if kind == 'reverse' else 'drive', gasPressed=kind == 'gas')
  elif kind in ('stop', 'nan'):
    e = message('longitudinalPlan', t, modelMonoTime=t - 1, aTarget=float('nan') if kind == 'nan' else 0,
                shouldStop=kind == 'stop', allowThrottle=True, allowBrake=True)
  else:
    e = can(t, 2, state=7 if kind == 'hold_can' else 5 if kind == 'gas_can' else 3,
            template='00666699b7f0f7fc' if kind != 'gas_can' else '5366669937dcf044')
    counter = 3
  out = run([*events, e, *bundle(1_040_000_000, counter), *bundle(1_060_000_000, counter + 1),
             *bundle(1_080_000_000, counter + 2)])
  if kind in ('gas', 'gas_can'):
    assert out['states']['override'] == 1
    assert out['states']['arming'] == 2
    assert out['candidate_count'] == 2
    assert out['candidate_examples'][-1]['calculated_accel_mps2'] == pytest.approx(0.02)
  else:
    assert out['candidate_count'] == 1
    assert out['states']['fault_latched'] >= 1


def test_contradictory_disabled_state_cannot_clear_fault_latch():
  bad = bundle(1_040_000_000, 2)
  bad[2].valid = False
  false_off = bundle(1_060_000_000, 3, enabled=False)
  false_off[4].selfdriveState.state = 'enabled'
  false_off[4].selfdriveState.active = True
  result = run([params(), *bundle(1_000_000_000), *bundle(1_020_000_000, 1), *bad, *false_off,
                *bundle(1_080_000_000, 4), *bundle(1_100_000_000, 5)])
  assert result['candidate_count'] == 1
  assert result['reasons']['inconsistent_selfdrive_state'] == 1


def test_healthy_explicit_disengagement_rearms_after_fault():
  bad = bundle(1_040_000_000, 2)
  bad[2].valid = False
  result = run([params(), *bundle(1_000_000_000), *bundle(1_020_000_000, 1), *bad,
                *bundle(1_060_000_000, 3, enabled=False), *bundle(1_080_000_000, 4), *bundle(1_100_000_000, 5)])
  assert result['candidate_count'] == 2
  assert result['states']['off'] == 1
  assert result['states']['arming'] == 2


@pytest.mark.parametrize('template', ['5366669937fcf024', '5366669937ccf054', '5367669937dcf043'])
def test_transient_unsupported_template_is_not_overwritten(template):
  result = run([params(), *bundle(1_000_000_000), *bundle(1_020_000_000, 1),
                can(1_025_000_000, 2, template=template), *bundle(1_040_000_000, 3), *bundle(1_060_000_000, 4)])
  assert result['candidate_count'] == 1
  assert result['states']['fault_latched'] == 1


@pytest.mark.parametrize('source', ['hud', 'selfdriveState'])
def test_transient_disengagement_resets_ramp_without_clearing_fault(source):
  def interrupted(latched):
    events = [params(), *bundle(1_000_000_000), *bundle(1_020_000_000, 1)]
    if latched:
      bad = message('longitudinalPlan', 1_023_000_000)
      bad.valid = False
      events.append(bad)
      events.append(message('carControl', 1_024_000_000, enabled=True))
    if source == 'hud':
      events.append(can(1_025_000_000, 2, state=2))
      counter = 3
    else:
      events.append(message('selfdriveState', 1_025_000_000, enabled=False, active=False, state='disabled'))
      counter = 2
    return run([*events, *bundle(1_040_000_000, counter), *bundle(1_060_000_000, counter + 1),
                *bundle(1_080_000_000, counter + 2)])
  reset = interrupted(False)
  assert reset['states']['interrupted'] == 1
  assert reset['states']['arming'] == 2
  assert reset['candidate_examples'][-1]['calculated_accel_mps2'] == pytest.approx(0.02)
  fault = interrupted(True)
  assert fault['candidate_count'] == 1
  assert fault['states']['fault_latched'] >= 2
