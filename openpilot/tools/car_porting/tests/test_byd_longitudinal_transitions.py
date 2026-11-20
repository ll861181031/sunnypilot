import json
from importlib import import_module

import pytest

from openpilot.cereal import log


def api():
  return import_module('openpilot.tools.car_porting.byd_longitudinal_transitions')


def can_event(ms, frames, *, valid=True, kind='can'):
  msg = log.Event.new_message(logMonoTime=round(ms * 1e6), valid=valid)
  for out, (address, data, bus) in zip(msg.init(kind, len(frames)), frames, strict=True):
    out.address, out.dat, out.src = address, data, bus
  return msg.as_reader()


def checksummed(hex_text, counter, wheel=False):
  data = bytearray.fromhex(hex_text)
  data[6] = (data[6] & 15) | (counter << 4) if wheel else (data[6] & 240) | counter
  data[7] = (255 - sum(data[:7])) & 255
  return bytes(data)


def hud(state, counter=0):
  data = bytearray.fromhex('000000000020f000')
  data[5] = state << 4
  data[6] |= counter
  data[7] = (255 - sum(data[:7])) & 255
  return (0x32D, bytes(data), 2)


def command(kind='off', counter=0):
  raw = {'off': '6466669937d0f03f', 'on': '6466669937d8f037',
         'override': '6466669937e8f027', 'hold': '0066669937f0f083'}[kind]
  return (0x32E, checksummed(raw, counter), 2)


def vlc(mode=0, counter=0):
  # Manually specified raw samples; no production packer derives the expectation.
  raw = '5532008c000020cc' if mode == 0 else '5536008c00006088'
  return (0x220, checksummed(raw, counter), 0)


def pedal(gas=0, brake=0):
  return (0x342, bytes([gas, brake, 0, 0, 0, 0, 0, 0]), 0)


def stream_edges(result, stream, field):
  return [edge for edge in result['edges'] if edge['stream'] == stream and edge['field'] == field]


def test_observation_interval_uses_last_unchanged_sample_not_initial_state():
  result = api().analyze_events([can_event(1000, [hud(2)]), can_event(1020, [hud(2, 1)]), can_event(1040, [hud(3, 2)])])
  edge, = stream_edges(result, 'hud', 'state')
  assert (edge['old'], edge['new']) == (2, 3)
  assert edge['interval_ns'] == [1_020_000_000, 1_040_000_000]
  assert edge['old_payload_hex'] == '000000000020f1ee'
  assert edge['new_payload_hex'] == '000000000030f2dd'
  assert result['control_ready'] is False


def test_first_sample_is_not_an_engagement():
  result = api().analyze_events([can_event(1000, [hud(3), command('on'), vlc(1)])])
  assert result['edges'] == []
  assert result['episodes'] == []


@pytest.mark.parametrize('bad', ['checksum', 'length', 'counter', 'duplicate', 'invalid'])
def test_bad_or_ambiguous_frame_breaks_transition_history(bad):
  events = [can_event(1000, [hud(2)])]
  next_frame = hud(3, 1)
  if bad == 'checksum':
    next_frame = (0x32D, next_frame[1][:-1] + b'\x00', 2)
  elif bad == 'length':
    next_frame = (0x32D, next_frame[1][:-1], 2)
  elif bad == 'counter':
    next_frame = hud(3, 3)
  events.append(can_event(1020, [next_frame] * (2 if bad == 'duplicate' else 1), valid=bad != 'invalid'))
  events.append(can_event(1040, [hud(3, 4 if bad == 'counter' else 2)]))
  result = api().analyze_events(events)
  assert stream_edges(result, 'hud', 'state') == []


def test_counter_wrap_can_form_an_edge():
  result = api().analyze_events([can_event(1000, [hud(2, 15)]), can_event(1020, [hud(3, 0)])])
  assert len(stream_edges(result, 'hud', 'state')) == 1


def test_repeated_stream_in_different_envelopes_at_same_timestamp_is_ambiguous():
  result = api().analyze_events([can_event(1000, [hud(2)]), can_event(1020, [hud(3, 1)]),
                                 can_event(1020, [hud(2, 2)]), can_event(1040, [hud(3, 3)])])
  assert stream_edges(result, 'hud', 'state') == []
  assert result['streams']['hud']['ambiguous_batches'] == 1


@pytest.mark.parametrize('gap', [100, 101, 2000])
def test_gap_cannot_be_reported_as_observed_transition(gap):
  result = api().analyze_events([can_event(1000, [hud(2)]), can_event(1000 + gap, [hud(3, 1)])])
  assert stream_edges(result, 'hud', 'state') == []


def test_reversed_timestamp_fails_instead_of_sorting_into_fabricated_order():
  with pytest.raises(ValueError, match='order'):
    api().analyze_events([can_event(1020, [hud(2)]), can_event(1000, [hud(3, 1)])])


@pytest.mark.parametrize('kind,bus', [('can', 0), ('can', 130), ('can', 194), ('sendcan', 2)])
def test_mirror_echo_rejected_and_tx_frames_do_not_form_hud_edges(kind, bus):
  frames = [hud(3, 1)]
  frames[0] = (frames[0][0], frames[0][1], bus)
  result = api().analyze_events([can_event(1000, [hud(2)]), can_event(1020, frames, kind=kind)])
  assert result['edges'] == []


def test_short_pedal_pulse_is_not_erased_by_next_control_sample():
  result = api().analyze_events([can_event(1000, [pedal()]), can_event(1020, [pedal(brake=20)]),
                                 can_event(1040, [pedal()])])
  assert [(e['old'], e['new']) for e in stream_edges(result, 'pedal', 'brake_pressed')] == [(False, True), (True, False)]


def test_vlc_candidate_checksum_and_counter_guard_analysis_only():
  damaged = list(vlc(1, 1))
  damaged[1] = damaged[1][:-1] + b'\x00'
  result = api().analyze_events([can_event(1000, [vlc(0)]), can_event(1020, [tuple(damaged)]),
                                 can_event(1040, [vlc(1, 2)])])
  assert stream_edges(result, 'vlc', 'mode_bit2') == []
  assert result['streams']['vlc']['candidate_checksum_errors'] == 1


def test_equal_batch_order_is_invariant_and_not_ecu_response_order():
  initial = [hud(2), command('off'), vlc(0), pedal()]
  changed = [hud(3, 1), command('on', 1), vlc(1, 1), pedal()]
  a = api().analyze_events([can_event(1000, initial), can_event(1020, changed)])
  b = api().analyze_events([can_event(1000, initial[::-1]), can_event(1020, changed[::-1])])
  assert a == b
  episode, = a['episodes']
  near = [e for e in episode['nearby_edges'] if e['stream'] == 'vlc']
  assert near[0]['relative_interval_ms'] == [-20, 20]
  assert near[0]['ordering'] == 'overlap_or_same_batch'


def test_intervals_do_not_convert_polling_spacing_into_actuator_latency():
  result = api().analyze_events([can_event(1000, [hud(2), vlc(0)]), can_event(1020, [hud(3, 1), vlc(0, 1)]),
                                 can_event(1040, [vlc(0, 2)]), can_event(1060, [vlc(1, 3)])])
  episode, = result['episodes']
  near = [e for e in episode['nearby_edges'] if e['stream'] == 'vlc']
  assert near[0]['relative_interval_ms'] == [20, 60]
  assert near[0]['ordering'] == 'observed_after'
  assert episode['coverage']['vlc']['complete_window'] is False


def test_two_nearby_changes_remain_two_not_one_chosen_confirmation():
  result = api().analyze_events([can_event(1000, [hud(2), vlc(0)]), can_event(1020, [hud(3, 1), vlc(1, 1)]),
                                 can_event(1040, [vlc(0, 2)])])
  episode, = result['episodes']
  edges = [e for e in episode['nearby_edges'] if e['stream'] == 'vlc' and e['field'] == 'mode_bit2']
  assert len(edges) == 2


def test_pedal_integrity_is_not_claimed_as_dbc_checksum_verified():
  result = api().analyze_events([can_event(1000, [hud(2), pedal()]), can_event(1020, [hud(3, 1), pedal(brake=10)])])
  edge, = stream_edges(result, 'pedal', 'brake_pressed')
  assert edge['integrity'] == 'unchecked'
  ctx = next(e for e in result['edges'] if e['stream'] == 'hud')['context']
  assert ctx['pedal']['age_ms'] == 0
  assert ctx['pedal']['integrity'] == 'unchecked'


def test_future_and_stale_context_not_attached_to_earlier_transition():
  result = api().analyze_events([can_event(900, [pedal()]), can_event(1000, [hud(2)]),
                                 can_event(1020, [hud(3, 1)]), can_event(1040, [vlc(1)])])
  edge, = stream_edges(result, 'hud', 'state')
  assert 'pedal' not in edge['context']
  assert 'vlc' not in edge['context']


def test_segment_boundary_cannot_generate_edge(tmp_path):
  paths = [tmp_path / 'a', tmp_path / 'b']
  for path, ev in zip(paths, [can_event(1000, [hud(2)]), can_event(1020, [hud(3, 1)])], strict=True):
    path.write_bytes(ev.as_builder().to_bytes())
  result = api().analyze_logs(paths)
  assert all(not s['edges'] for s in result['segments'])


def test_cli_reads_local_completed_logs_and_never_overwrites_output(tmp_path):
  path, output = tmp_path / 'rlog', tmp_path / 'evidence.json'
  path.write_bytes(can_event(1000, [hud(2)]).as_builder().to_bytes() + can_event(1020, [hud(3, 1)]).as_builder().to_bytes())
  assert api().main([str(path), '--output', str(output)]) == 0
  saved = output.read_bytes()
  result = json.loads(saved)
  assert result['segments'][0]['episodes'][0]['category'] == 'entry_candidate'
  with pytest.raises(SystemExit):
    api().main([str(path), '--output', str(output)])
  assert output.read_bytes() == saved


@pytest.mark.parametrize('path', ['https://example.invalid/rlog', 'missing/rlog'])
def test_no_remote_or_missing_log_input(path):
  with pytest.raises(ValueError):
    api().analyze_logs([path])


def test_locked_log_rejected(tmp_path):
  path = tmp_path / 'rlog'
  path.write_bytes(can_event(1000, [hud(2)]).as_builder().to_bytes())
  (tmp_path / 'rlog.lock').touch()
  with pytest.raises(ValueError, match='lock'):
    api().analyze_logs([path])

def test_complete_window_requires_continuous_samples_before_and_after():
  events = [can_event(1000 + i * 20, [hud(2 if i < 60 else 3, i % 16), vlc(0, i % 16)]) for i in range(122)]
  result = api().analyze_events(events)
  episode, = result['episodes']
  assert episode['coverage']['vlc']['complete_window'] is True
  assert not [e for e in episode['nearby_edges'] if e['stream'] == 'vlc']


def test_single_bad_frame_inside_window_prevents_negative_evidence():
  events = [can_event(1000 + i * 20, [hud(2 if i < 60 else 3, i % 16), vlc(0, i % 16)]) for i in range(122)]
  events[65] = can_event(2300, [hud(3, 1), (0x220, bytes.fromhex('5532008c00002100'), 0)])
  result = api().analyze_events(events)
  episode, = result['episodes']
  assert episode['coverage']['vlc']['complete_window'] is False
  assert episode['coverage']['vlc']['reason'] == 'integrity_or_timing_gap'


def test_hold_special_value_does_not_get_classified_as_acceleration_edge():
  result = api().analyze_events([can_event(1000, [command('on'), vlc(1)]),
                                 can_event(1020, [command('hold', 1), vlc(1, 1)])])
  edge, = stream_edges(result, 'cmd', 'raw_zero')
  assert (edge['old'], edge['new']) == (False, True)
  assert 'acceleration_applied' not in result


def test_ambiguous_batch_counter_order_cannot_change_recovery_or_later_edges():
  ambiguous = [hud(2, 1), hud(3, 2)]
  def run(frames):
    return api().analyze_events([can_event(1000, [hud(2)]), can_event(1020, frames),
                                 can_event(1040, [hud(2, 3)]), can_event(1060, [hud(3, 4)])])
  forward, reverse = run(ambiguous), run(ambiguous[::-1])
  assert forward == reverse
  edge, = stream_edges(forward, 'hud', 'state')
  assert edge['interval_ns'] == [1_040_000_000, 1_060_000_000]


def test_ambiguous_batch_with_bad_checksum_cannot_change_recovery():
  bad = (0x32D, bytes.fromhex('000000000030f200'), 2)
  ambiguous = [hud(2, 1), bad]
  def run(frames):
    return api().analyze_events([can_event(1000, [hud(2)]), can_event(1020, frames),
                                 can_event(1040, [hud(2, 3)]), can_event(1060, [hud(3, 4)])])
  assert run(ambiguous) == run(ambiguous[::-1])


def test_wheel_fault_breaks_stationary_edge_history():
  stopped = checksummed('0000000000000000', 0, wheel=True)
  fault = checksummed('0020000000000000', 1, wheel=True)
  moving = checksummed('6400644006640000', 2, wheel=True)
  result = api().analyze_events([can_event(1000, [(0x1F0, stopped, 0)]), can_event(1020, [(0x1F0, fault, 0)]),
                                 can_event(1040, [(0x1F0, moving, 0)])])
  assert stream_edges(result, 'wheel', 'stationary') == []
  assert result['streams']['wheel']['wheel_fault_errors'] == 1


def test_vlc_candidate_counter_gap_breaks_mode_edge_history():
  result = api().analyze_events([can_event(1000, [vlc(0)]), can_event(1020, [vlc(1, 3)]),
                                 can_event(1040, [vlc(1, 4)])])
  assert stream_edges(result, 'vlc', 'mode_bit2') == []
  assert result['streams']['vlc']['counter_discontinuities'] == 1


def test_invalid_can_batch_breaks_previously_continuous_coverage():
  events = [can_event(1000 + i * 20, [hud(2 if i < 60 else 3, i % 16), vlc(0, i % 16)]) for i in range(122)]
  events[65] = can_event(2300, [], valid=False)
  episode, = api().analyze_events(events)['episodes']
  assert episode['coverage']['vlc']['complete_window'] is False
  assert episode['coverage']['hud']['complete_window'] is False
