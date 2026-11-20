import json

import pytest
import zstandard

from openpilot.cereal import log
from openpilot.tools.car_porting.byd_longitudinal_audit import audit_events, audit_logs, decode_frame, main


# Raw bus-2 frames from the owner's routes, not produced by the decoder/packer.
HOLD = bytes.fromhex('00666699b7f0f102')
RESUME = bytes.fromhex('64666699b7d8f8af')
APPROACH_STOP = bytes.fromhex('5766669937ddf738')
# Hand-checked synthetic HUD: state 6, counter 0, additive checksum.
HUD = bytes.fromhex('000000000060009f')
WHEELS_STOPPED = bytes.fromhex('00000000000000ff')
PEDALS_RELEASED = bytes.fromhex('0000000000000000')


def event(nanos, frames, kind='can', valid=True):
  msg = log.Event.new_message(logMonoTime=nanos, valid=valid)
  msgs = msg.init(kind, len(frames))
  for out, (address, payload, bus) in zip(msgs, frames, strict=True):
    out.address, out.dat, out.src = address, payload, bus
  return msg.as_reader()


def frame(nanos, payload=HOLD, address=0x32E, bus=2):
  return event(nanos, [(address, payload, bus)])


def context(nanos=1_000_000_000):
  return event(nanos, [(0x32D, HUD, 2), (0x1F0, WHEELS_STOPPED, 0), (0x342, PEDALS_RELEASED, 0)])


def test_real_hold_payload_is_not_interpreted_as_measured_deceleration():
  result = decode_frame(0x32E, HOLD)
  assert result['ACCEL_CMD'] == -5.0
  assert result['STANDSTILL_RESUME'] == 1
  assert result['STANDSTILL_STATE'] == 0
  assert result['ACC_OVERRIDE_OR_STANDSTILL'] == 1


@pytest.mark.parametrize('payload,expected', [(APPROACH_STOP, (1, 0, 2)), (RESUME, (0, 1, 0))])
def test_real_stop_and_restart_fields(payload, expected):
  values = decode_frame(0x32E, payload)
  assert tuple(values[s] for s in ('STANDSTILL_STATE', 'STANDSTILL_RESUME', 'BRAKE_BEHAVIOR')) == expected


@pytest.mark.parametrize('payload', [HOLD[:-1], HOLD + b'\x00', HOLD[:-1] + b'\x03'])
def test_malformed_frames_are_not_decoded(payload):
  with pytest.raises(ValueError):
    decode_frame(0x32E, payload)


def test_report_keeps_raw_evidence_and_separates_candidate_from_physical_state():
  result = audit_events([context(), frame(1_020_000_000)])
  sample = result['transitions'][0]
  assert sample['payload'] == HOLD.hex()
  assert sample['context']['hud']['signals']['CRUISE_STATE'] == 6
  assert sample['context']['wheel_speed']['speed_mps'] == 0
  assert sample['context']['pedals']['signals']['BRAKE_PEDAL'] == 0
  assert sample['candidate']['ACCEL_CMD'] == -5
  assert result['control_ready'] is False


@pytest.mark.parametrize('age', [100_000_000, 100_000_001, 500_000_000])
def test_stale_context_is_not_used_to_infer_command_meaning(age):
  result = audit_events([context(), frame(1_000_000_000 + age)])
  assert result['transitions'][0]['context'] == {}


def test_receive_and_transmit_buses_cannot_be_mixed():
  result = audit_events([context(), frame(1_010_000_000, bus=0), frame(1_020_000_000, bus=130),
                         event(1_030_000_000, [(0x32E, HOLD, 2)], kind='sendcan')])
  assert result['transitions'] == []
  assert result['streams']['0:0x32e']['frames'] == 1
  assert '130:0x32e' not in result['streams']


def test_invalid_can_event_clears_correlated_context():
  result = audit_events([context(), event(1_010_000_000, [], valid=False), frame(1_020_000_000)])
  assert result['transitions'][0]['context'] == {}
  assert result['invalid_can_events'] == 1


def test_bad_checksum_clears_previous_hud_sample():
  bad = HUD[:-1] + b'\x00'
  result = audit_events([context(), frame(1_010_000_000, bad, 0x32D), frame(1_020_000_000)])
  assert 'hud' not in result['transitions'][0]['context']
  assert result['streams']['2:0x32d']['checksum_errors'] == 1


def test_counter_wrap_is_valid_but_repeated_counter_is_reported():
  # Counter 15 -> 0 -> 0; checksum changes with the low nibble of byte 6.
  messages = [bytes.fromhex(p) for p in ('6466669937d0ff30', '6466669937d0f03f', '6466669937d0f03f')]
  result = audit_events([frame(1_000_000_000 + i * 20_000_000, p) for i, p in enumerate(messages)])
  stats = result['streams']['2:0x32e']
  assert stats['checksum_errors'] == 0
  assert stats['counter_discontinuities'] == 1
  assert stats['median_period_ms'] == 20


def test_corrupt_command_is_counted_without_creating_a_state_transition():
  result = audit_events([frame(1_000_000_000, HOLD[:-1] + b'\x03')])
  assert result['streams']['2:0x32e']['checksum_errors'] == 1
  assert result['transitions'] == []


def test_out_of_order_time_does_not_pair_future_context():
  result = audit_events([context(2_000_000_000), frame(1_000_000_000)])
  assert result['transitions'][0]['context'] == {}


def test_same_batch_frame_order_does_not_change_context_join():
  frames = [(0x32E, HOLD, 2), (0x32D, HUD, 2), (0x1F0, WHEELS_STOPPED, 0), (0x342, PEDALS_RELEASED, 0)]
  result = audit_events([event(1_000_000_000, frames)])
  assert result['transitions'][0]['context']['hud']['signals']['CRUISE_STATE'] == 6


def test_local_log_integration_and_output(tmp_path):
  path = tmp_path / 'rlog'
  path.write_bytes(context().as_builder().to_bytes() + frame(1_020_000_000).as_builder().to_bytes())
  output = tmp_path / 'audit.json'
  assert main([str(path), '--output', str(output)]) == 0
  result = json.loads(output.read_text())
  assert result['segments'][0]['streams']['2:0x32e']['frames'] == 1
  assert result['control_ready'] is False


@pytest.mark.parametrize('path', ['https://example.com/rlog.zst', 'missing/rlog.zst'])
def test_missing_or_remote_input_is_rejected_before_logreader(path):
  with pytest.raises(ValueError, match='local'):
    audit_logs([path])


def test_locked_recording_is_not_read(tmp_path):
  path = tmp_path / 'rlog'
  path.touch()
  (tmp_path / 'rlog.lock').touch()
  with pytest.raises(ValueError, match='lock'):
    audit_logs([str(path)])


def test_segments_never_share_stale_context(tmp_path):
  a, b = tmp_path / 'first', tmp_path / 'second'
  a.write_bytes(context().as_builder().to_bytes())
  b.write_bytes(frame(1_020_000_000).as_builder().to_bytes())
  result = audit_logs([str(a), str(b)])
  assert result['segments'][1]['transitions'][0]['context'] == {}


def test_multiple_hud_states_in_one_batch_are_not_treated_as_an_exact_join():
  hud_next = bytes.fromhex('000000000070008f')
  frames = [(0x32D, HUD, 2), (0x32E, HOLD, 2), (0x32D, hud_next, 2)]
  result = audit_events([event(1_000_000_000, frames), frame(1_020_000_000)])
  assert 'hud' not in result['transitions'][0]['context']
  assert result['ambiguous_context_batches']['hud'] == 1


def test_report_distinguishes_unchecked_pedals_from_checksum_verified_hud():
  result = audit_events([context(), frame(1_020_000_000)])
  ctx = result['transitions'][0]['context']
  assert ctx['pedals']['checksum_verified'] is False
  assert ctx['hud']['checksum_verified'] is True
  assert result['streams']['0:0x342']['checksum_available'] is False


def test_repeated_hud_counter_cannot_supply_trusted_context():
  result = audit_events([context(), frame(1_010_000_000, HUD, 0x32D), frame(1_020_000_000)])
  assert 'hud' not in result['transitions'][0]['context']
  assert result['streams']['2:0x32d']['counter_discontinuities'] == 1


def test_existing_report_is_never_overwritten(tmp_path):
  path, output = tmp_path / 'rlog', tmp_path / 'report.json'
  path.write_bytes(frame(1_000_000_000).as_builder().to_bytes())
  output.write_text('keep this report')
  with pytest.raises(SystemExit) as exc:
    main([str(path), '--output', str(output)])
  assert exc.value.code == 2
  assert output.read_text() == 'keep this report'


def test_truncated_log_is_not_reported_as_complete(tmp_path):
  path = tmp_path / 'rlog'
  path.write_bytes(frame(1_000_000_000).as_builder().to_bytes() + b'\xff' * 16)
  with pytest.raises(RuntimeWarning, match='Corrupted'):
    audit_logs([str(path)])


@pytest.mark.parametrize('missing', [1, 4, 12])
def test_truncated_zstd_frame_is_rejected_even_when_events_look_complete(tmp_path, missing):
  path = tmp_path / 'rlog.zst'
  raw = context().as_builder().to_bytes() + frame(1_020_000_000).as_builder().to_bytes()
  compressed = zstandard.ZstdCompressor(write_checksum=True).compress(raw)
  path.write_bytes(compressed[:-missing])
  with pytest.raises(ValueError, match='[Zz]standard'):
    audit_logs([str(path)])


def test_concatenated_complete_zstd_frames_are_supported(tmp_path):
  path = tmp_path / 'rlog.zst'
  compressor = zstandard.ZstdCompressor()
  path.write_bytes(compressor.compress(context().as_builder().to_bytes()) +
                   compressor.compress(frame(1_020_000_000).as_builder().to_bytes()))
  result = audit_logs([str(path)])
  assert result['segments'][0]['streams']['2:0x32e']['frames'] == 1


def test_symlink_cannot_bypass_recording_lock(tmp_path):
  recorder = tmp_path / 'recorder'
  recorder.mkdir()
  path = recorder / 'rlog'
  path.write_bytes(frame(1_000_000_000).as_builder().to_bytes())
  (recorder / 'rlog.lock').touch()
  link = tmp_path / 'alias'
  link.symlink_to(path)
  with pytest.raises(ValueError, match='lock'):
    audit_logs([str(link)])


def test_local_filename_never_enters_network_identifier_router(tmp_path, monkeypatch):
  path = tmp_path / 'useradmin.comma.ai?onebox=https%3A%2F%2Fexample.invalid%2Frlog.zst'
  path.write_bytes(frame(1_000_000_000).as_builder().to_bytes())

  def reject_identifier_routing(_):
    raise AssertionError('Local bytes must not enter the route/URL identifier parser')

  monkeypatch.setattr('openpilot.tools.lib.logreader.parse_indirect', reject_identifier_routing)
  result = audit_logs([str(path)])
  assert result['segments'][0]['streams']['2:0x32e']['frames'] == 1
