import json
from importlib import import_module

import pytest

from openpilot.cereal import log


def api():
  return import_module('openpilot.tools.car_porting.byd_longitudinal_feedback')


def event(ms, frames, valid=True, kind='can'):
  msg = log.Event.new_message(logMonoTime=round(ms * 1e6), valid=valid)
  for out, (address, raw, bus) in zip(msg.init(kind, len(frames)), frames, strict=True):
    out.address, out.dat, out.src = address, raw, bus
  return msg.as_reader()


def checksum(raw, counter, high=False):
  data = bytearray.fromhex(raw)
  data[6] = (data[6] & 15) | counter << 4 if high else (data[6] & 240) | counter
  data[7] = (255 - sum(data[:7])) & 255
  return bytes(data)


def frames(counter=0, *, ipb=True):
  result = [(0x32D, checksum('000000000030f000', counter), 2),
            (0x32E, checksum('5366669937dcf044', counter), 2),
            (0x1F0, checksum('64006440060600ff', counter, True), 0),
            (0x342, checksum('00001bf42628e0c2', counter, True), 0),
            (0x321, checksum('10c010f07017e9bf', counter, True), 0),
            (0x223, checksum('1eb33124c331fee7', counter), 0),
            (0x242, checksum('768447058c64fccd', counter, True), 0),
            (0x220, checksum('5532008c000020cc', counter), 0)]
  if ipb:
    result.append((0x123, checksum('b2c05dc05d800b88', counter), 0))
  return result


def replace_frame(items, address, raw, bus=0):
  return [f for f in items if f[0] != address] + [(address, raw, bus)]


def test_captured_fields_remain_candidates_with_literal_raw_evidence():
  result = api().analyze_events([event(1000, frames())])
  row, = result['rows']
  assert result['control_ready'] is False
  assert row['hud'] == 3
  assert row['values']['cmd_accel_candidate'] == pytest.approx(-.85)
  assert row['values']['brake_dbc_pedal_candidate'] == pytest.approx(-.262)
  assert row['values']['brake_raw16_at32'] == 6000
  assert row['values']['ipb_brake_candidate'] == 0
  assert row['values']['sensor_accel_candidate'] == pytest.approx(.054253472)
  assert row['raw']['brake'] == '10c010f07017099f'
  assert row['integrity']['ipb'].startswith('candidate_')
  assert row['integrity']['brake'] == 'dbc_checksum_counter'


def test_captured_override_sensor_is_not_replaced_with_command_or_vlc():
  captured = [(0x321, bytes.fromhex('10c010f07017099f'), 0),
              (0x32E, bytes.fromhex('78666699b7e8f98a'), 2),
              (0x242, bytes.fromhex('de7747248e64bc91'), 0),
              (0x32D, bytes.fromhex('7692042174500905'), 2),
              (0x342, bytes.fromhex('240027d42628a0f2'), 0),
              (0x223, bytes.fromhex('71b3311dc331f0a9'), 0),
              (0x220, bytes.fromhex('5536009f42313032'), 0),
              (0x1F0, bytes.fromhex('6e006ee0066e00cf'), 0)]
  row, = api().analyze_events([event(1000, captured)])['rows']
  assert (row['hud'], row['pedal']) == (5, 'gas')
  assert row['values']['cmd_accel_candidate'] == pytest.approx(1)
  assert row['values']['vlc_accel_candidate'] == pytest.approx(.95)
  assert row['values']['sensor_accel_candidate'] == pytest.approx(2.30577256)
  assert row['values']['brake_dbc_pedal_candidate'] == pytest.approx(-.262)


@pytest.mark.parametrize('bad', ['length', 'checksum', 'counter', 'duplicate', 'invalid'])
def test_bad_frame_removes_prior_feedback_instead_of_reusing_it(bad):
  middle = frames(1)
  raw = next(f[1] for f in middle if f[0] == 0x123)
  if bad == 'length':
    raw = raw[:-1]
  elif bad == 'checksum':
    raw = raw[:-1] + bytes([raw[-1] ^ 1])
  elif bad == 'counter':
    raw = checksum(raw.hex(), 4)
  middle = replace_frame(middle, 0x123, raw)
  if bad == 'duplicate':
    middle.append((0x123, raw, 0))
  result = api().analyze_events([event(1000, frames()), event(1020, middle, valid=bad != 'invalid'),
                               event(1040, frames(2, ipb=False))])
  assert 'ipb_brake_candidate' not in result['rows'][-1]['values']


def test_repeated_source_across_envelopes_is_order_independent():
  initial = event(1000, frames())
  left, right = event(1020, frames(1)), event(1020, [(0x123, checksum('b2c05dc05d880000', 2), 0)])
  follow = event(1040, frames(3))
  assert api().analyze_events([initial, left, right, follow]) == api().analyze_events([initial, right, left, follow])
  assert 'ipb_brake_candidate' not in api().analyze_events([initial, left, right])['rows'][-1]['values']


@pytest.mark.parametrize('ms,expected', [(1099, True), (1100, False), (1101, False)])
def test_feedback_age_boundary_excludes_stale_data(ms, expected):
  result = api().analyze_events([event(1000, frames()), event(ms, frames(1, ipb=False))])
  assert ('ipb_brake_candidate' in result['rows'][-1]['values']) is expected


def test_future_feedback_is_never_joined():
  result = api().analyze_events([event(1000, frames(ipb=False)), event(1001, [frames()[-1]])])
  assert 'ipb_brake_candidate' not in result['rows'][0]['values']


@pytest.mark.parametrize('bus,kind', [(2, 'can'), (128, 'can'), (192, 'can'), (0, 'sendcan')])
def test_mirrors_echoes_and_transmitted_frames_cannot_supply_feedback(bus, kind):
  raw = frames()[-1][1]
  result = api().analyze_events([event(1000, [(0x123, raw, bus)], kind=kind), event(1000, frames(ipb=False))])
  assert 'ipb_brake_candidate' not in result['rows'][0]['values']


def test_reversed_can_timestamp_is_rejected():
  with pytest.raises(ValueError, match='order'):
    api().analyze_events([event(1020, frames()), event(1000, frames(1))])


@pytest.mark.parametrize('address', [0x1F0, 0x342, 0x32D, 0x32E])
def test_invalid_required_context_suppresses_snapshot(address):
  source = next(f for f in frames() if f[0] == address)
  damaged = source[1][:-1] + bytes([source[1][-1] ^ 1])
  result = api().analyze_events([event(1000, replace_frame(frames(), address, damaged, source[2]))])
  assert result['rows'] == []


def test_wheel_fault_suppresses_snapshot():
  wheel = bytearray(next(f[1] for f in frames() if f[0] == 0x1F0))
  wheel[1] |= 16
  wheel = checksum(wheel.hex(), 0, True)
  assert api().analyze_events([event(1000, replace_frame(frames(), 0x1F0, wheel))])['rows'] == []


def test_counter_wrap_retains_snapshot():
  assert len(api().analyze_events([event(1000, frames(15)), event(1020, frames(0))])['rows']) == 2


def test_summary_does_not_promote_numeric_relationship_to_acceptance():
  result = api().analyze_events([event(1000, frames()), event(1020, frames(1))])
  summary = api().summarize_rows(result['rows'])
  assert summary['control_ready'] is False
  group, = summary['groups']
  assert group['samples'] == 2
  assert group['signals']['brake_dbc_pedal_candidate']['range'] == pytest.approx([-.262, -.262])
  assert group['signals']['brake_raw16_at32']['range'] == [6000, 6000]
  assert group['relationships']['cmd_vs_sensor']['correlation'] is None


def test_relationships_do_not_subtract_incompatible_units():
  group, = api().summarize_rows(api().analyze_events([event(1000, frames())])['rows'])['groups']
  assert 'difference_y_minus_x_range' not in group['relationships']['pedal_vs_brake_raw']
  assert 'difference_y_minus_x_median' not in group['relationships']['cmd_vs_drive_throttle']


@pytest.mark.parametrize('stream', ['pedal', 'hud'])
def test_short_mode_pulse_between_commands_breaks_derivative_history(stream):
  if stream == 'pedal':
    pulse = (0x342, checksum('00141bf426280000', 1, True), 0)
  else:
    pulse = (0x32D, checksum('000000000050f000', 1), 2)
  result = api().analyze_events([event(1000, frames()), event(1005, [pulse]),
                               event(1010, [f for f in frames(2) if f[0] == pulse[0]]),
                               event(1020, [f for f in frames(1) if f[0] != pulse[0]])])
  assert result['rows'][0]['hud'] == result['rows'][1]['hud']
  assert result['rows'][0]['pedal'] == result['rows'][1]['pedal']
  assert result['rows'][0]['chain'] != result['rows'][1]['chain']


def derivative_rows():
  return [{'logMonoTime': 1_000_000_000 + i * 20_000_000, 'chain': 0, 'hud': 5, 'pedal': 'gas',
           'speed_mps': 5 + i * .04, 'ages_ms': {'wheel': 0}, 'raw': {},
           'signal_times_ns': dict.fromkeys(('cmd', 'sensor', 'vlc'), 1_000_000_000 + i * 20_000_000),
           'values': {'sensor_accel_candidate': 2, 'cmd_accel_candidate': .3, 'vlc_accel_candidate': .3}}
          for i in range(101)]


def test_disjoint_wheel_windows_expose_override_difference():
  group, = api().summarize_rows(derivative_rows())['wheel_windows']
  assert group['windows'] == 1
  assert group['relationships']['wheel_vs_sensor']['difference_y_minus_x_range'] == pytest.approx([0, 0])
  assert group['relationships']['wheel_vs_vlc']['difference_y_minus_x_range'] == pytest.approx([-1.7, -1.7])


@pytest.mark.parametrize('kind', ['irregular_linear', 'uniform_quadratic'])
def test_wheel_and_candidate_compare_time_averages_not_midpoint_samples(kind):
  times = [i / 100 for i in range(51)] + [.59, .68, .77, .86, .95, 1] if kind == 'irregular_linear' else [i / 50 for i in range(51)]
  rows = []
  for t in times:
    row = derivative_rows()[0]
    row['logMonoTime'] = 1_000_000_000 + round(t * 1e9)
    row['signal_times_ns'] = dict.fromkeys(('cmd', 'sensor', 'vlc'), row['logMonoTime'])
    row['speed_mps'] = 5 + 2 * t ** 2 if kind == 'irregular_linear' else 5 + 4 * t ** 3
    row['values']['sensor_accel_candidate'] = 4 * t if kind == 'irregular_linear' else 12 * t ** 2
    rows.append(row)
  group, = api().summarize_rows(rows)['wheel_windows']
  # Linear interpolation is exact for linear a(t); trapezoidal quadratic error < .001.
  assert group['relationships']['wheel_vs_sensor']['difference_y_minus_x_range'] == pytest.approx([0, 0], abs=.001)


@pytest.mark.parametrize('boundary', ['start', 'end'])
def test_time_average_requires_source_samples_bracketing_both_wheel_endpoints(boundary):
  rows = derivative_rows()[:51]
  for row in rows:
    row['signal_times_ns']['sensor'] += 1_000_000 if boundary == 'start' else -1_000_000
  assert api().summarize_rows(rows)['wheel_windows'] == []


def test_average_preserves_between_command_source_observations():
  rows = derivative_rows()[:51]
  observations = {key: [] for key in ('cmd', 'sensor', 'vlc')}
  for i in range(101):
    nanos = 1_000_000_000 + i * 10_000_000
    for stream in observations:
      value = 4 if stream == 'sensor' and i % 2 else 2
      observations[stream].append({'logMonoTime': nanos, 'chain': 0, 'value': value})
  group, = api().summarize_rows(rows, observations)['wheel_windows']
  assert group['relationships']['wheel_vs_sensor']['difference_y_minus_x_range'] == pytest.approx([1, 1])


@pytest.mark.parametrize('defect', ['chain', 'mode', 'age', 'gap'])
def test_wheel_windows_do_not_cross_invalid_observation_chain(defect):
  rows = derivative_rows()[:51]
  for row in rows[25:]:
    if defect == 'chain':
      row['chain'] = 1
    elif defect == 'mode':
      row['hud'] = 3
    elif defect == 'age':
      row['ages_ms']['wheel'] = 100
    else:
      row['logMonoTime'] += 200_000_000
  assert api().summarize_rows(rows)['wheel_windows'] == []


def test_logs_never_share_feedback_cache(tmp_path):
  a, b = tmp_path / 'a.rlog', tmp_path / 'b.rlog'
  a.write_bytes(event(1000, frames()).as_builder().to_bytes())
  b.write_bytes(event(1020, frames(1, ipb=False)).as_builder().to_bytes())
  result = api().analyze_logs([a, b])
  assert 'ipb_brake_candidate' not in result['segments'][1]['summary']['groups'][0]['signals']
  assert len(result['segments'][0]['sha256']) == 64


def test_cli_refuses_overwrite_and_locked_input(tmp_path):
  path, output = tmp_path / 'input.rlog', tmp_path / 'result.json'
  path.write_bytes(event(1000, frames()).as_builder().to_bytes())
  assert api().main([str(path), '--output', str(output)]) == 0
  original = output.read_bytes()
  assert json.loads(original)['control_ready'] is False
  with pytest.raises(SystemExit):
    api().main([str(path), '--output', str(output)])
  assert output.read_bytes() == original
  (tmp_path / 'rlog.lock').touch()
  with pytest.raises(SystemExit):
    api().main([str(path)])
