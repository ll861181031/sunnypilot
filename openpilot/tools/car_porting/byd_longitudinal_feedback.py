"""Offline feedback hypotheses and counterexamples; no vehicle access or control."""

import argparse
import hashlib
import json
import math
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from itertools import groupby
from pathlib import Path

from opendbc import DBC_PATH
from openpilot.tools.car_porting.byd_longitudinal_audit import DBC_DEFINITION, decode_frame, local_log_paths, read_local_events

STREAMS = {(2, 0x32D): 'hud', (2, 0x32E): 'cmd', (0, 0x1F0): 'wheel', (0, 0x342): 'pedal',
           (0, 0x220): 'vlc', (0, 0x123): 'ipb', (0, 0x223): 'sensor', (0, 0x321): 'brake', (0, 0x242): 'drive'}
REQUIRED = {'cmd', 'hud', 'wheel', 'pedal'}
WINDOW_STREAMS = REQUIRED | {'sensor', 'vlc'}
# Hypotheses observed in captures, not additions to the runtime DBC or safety.
CANDIDATE_COUNTER_SHIFT = {'ipb': 0, 'sensor': 0, 'vlc': 0, 'pedal': 4, 'drive': 4}
ACCEL_FIELDS = {'cmd': 'cmd_accel_candidate', 'sensor': 'sensor_accel_candidate', 'vlc': 'vlc_accel_candidate'}
ACCEL_DBC_FIELDS = {'cmd': 'ACCEL_CMD', 'sensor': 'LONGITUDE', 'vlc': 'VLC_SENSOR'}
AGE_NS = 100_000_000  # Analysis freshness only, never an actuator deadline.


def _decode(stream, address, raw):
  values = decode_frame(address, raw)
  if stream in CANDIDATE_COUNTER_SHIFT:
    if sum(raw) % 256 != 255:
      raise ValueError('candidate_checksum')
    counter = (raw[6] >> CANDIDATE_COUNTER_SHIFT[stream]) & 15
    integrity = 'candidate_additive_checksum_counter_not_runtime_validated'
  else:
    counter = int(values['COUNTER'])
    integrity = 'dbc_checksum_counter'
  if stream == 'wheel' and any(values['FAULT_' + w] for w in ('FL', 'FR', 'BL', 'BR')):
    raise ValueError('wheel_fault')
  return values, counter, integrity


def _snapshot(nanos, fresh, chain):
  signals = {k: s['decoded'] for k, s in fresh.items()}
  values = {'cmd_accel_candidate': signals['cmd']['ACCEL_CMD'],
            'gas_pedal_candidate': signals['pedal']['GAS_PEDAL'], 'brake_pedal_candidate': signals['pedal']['BRAKE_PEDAL']}
  fields = {'ipb': {'ipb_brake_candidate': 'BRAKE_PRESSED'},
            'sensor': {'sensor_accel_candidate': 'LONGITUDE', 'sensor_offset_candidate': 'LONGITUDE_OFFSET'},
            'vlc': {'vlc_accel_candidate': 'VLC_SENSOR', 'vlc_brake_active_low': 'BRAKE_APPLIED_ACTIVE_LOW'},
            'brake': {'brake_dbc_pedal_candidate': 'BRAKE_PEDAL'},
            'drive': {'drive_throttle_candidate': 'RAW_THROTTLE', 'drive_brake_candidate': 'BRAKE_PRESSED'}}
  for stream, mapping in fields.items():
    if stream in fresh:
      values.update({label: signals[stream][name] for label, name in mapping.items()})
  if 'brake' in fresh:
    raw = fresh['brake']['raw']
    values.update(brake_raw16_at32=int.from_bytes(raw[4:6], 'little'), brake_raw_bit31=(raw[3] >> 7) & 1,
                  brake_raw12_at0=int.from_bytes(raw[:2], 'little') & 4095)
  gas, brake = values['gas_pedal_candidate'] > .01, values['brake_pedal_candidate'] > .01
  return {'logMonoTime': nanos, 'chain': chain, 'hud': int(signals['hud']['CRUISE_STATE']),
          'pedal': 'both' if gas and brake else 'gas' if gas else 'brake' if brake else 'none',
          'speed_mps': sum(signals['wheel']['WHEELSPEED_' + w] for w in ('FL', 'FR', 'BL', 'BR')) / 14.4,
          'values': values, 'raw': {k: s['raw'].hex() for k, s in fresh.items()},
          'signal_times_ns': {k: s['time'] for k, s in fresh.items() if k in ACCEL_FIELDS},
          'integrity': {k: s['integrity'] for k, s in fresh.items()},
          'ages_ms': {k: (nanos - s['time']) / 1e6 for k, s in fresh.items()}}


def analyze_events(events):
  cache, counters, previous_time = {}, {}, {}
  streams, metadata = defaultdict(Counter), Counter()
  rows, chain = [], 0
  observations = {key: [] for key in ACCEL_FIELDS}

  def can_events():
    last = -1
    for event in events:
      if event.which() != 'can':
        continue
      if event.logMonoTime < last:
        raise ValueError('CAN timestamp order reversed')
      last = event.logMonoTime
      yield event

  for nanos, group in groupby(can_events(), key=lambda e: e.logMonoTime):
    batch = list(group)
    metadata['can_envelopes'] += len(batch)
    if nanos <= 0 or any(not e.valid for e in batch):
      cache.clear()
      counters.clear()
      previous_time.clear()
      chain += 1
      metadata['invalid_batches'] += 1
      continue
    frames = defaultdict(list)
    for event in batch:
      for frame in event.can:
        stream = STREAMS.get((frame.src, frame.address))
        if stream:
          frames[stream].append(frame)
    accepted = set()
    for stream, items in sorted(frames.items()):
      prior_sample = cache.pop(stream, None)
      stats = streams[stream]
      stats['frames'] += len(items)
      if len(items) != 1:
        stats['ambiguous_batches'] += 1
        counters.pop(stream, None)
        previous_time.pop(stream, None)
        chain += int(stream in WINDOW_STREAMS)
        continue
      frame = items[0]
      raw = bytes(frame.dat)
      try:
        values, counter, integrity = _decode(stream, frame.address, raw)
      except ValueError as exc:
        stats[str(exc) + '_errors'] += 1
        counters.pop(stream, None)
        previous_time.pop(stream, None)
        chain += int(stream in WINDOW_STREAMS)
        continue
      last_counter = counters.get(stream)
      last_time = previous_time.get(stream)
      counters[stream], previous_time[stream] = counter, nanos
      if last_time is not None and nanos - last_time >= AGE_NS:
        stats['timing_gaps'] += 1
        chain += int(stream in WINDOW_STREAMS)
      if last_counter is not None and counter != (last_counter + 1) % 16:
        stats['counter_discontinuities'] += 1
        chain += int(stream in WINDOW_STREAMS)
        continue
      # Watch every accepted HUD/pedal observation, including short pulses
      # between command samples. Such pulses break a steady-mode window.
      if prior_sample is not None and stream in ('hud', 'pedal'):
        old = prior_sample['decoded']
        if stream == 'hud':
          changed = values['CRUISE_STATE'] != old['CRUISE_STATE']
        else:
          changed = any((values[key] > .01) != (old[key] > .01) for key in ('GAS_PEDAL', 'BRAKE_PEDAL'))
        chain += int(changed)
      cache[stream] = {'time': nanos, 'raw': raw, 'decoded': values, 'integrity': integrity}
      stats['eligible_samples'] += 1
      accepted.add(stream)
    # Retain every accepted acceleration sample, not only command-time joins.
    for stream in sorted(accepted & ACCEL_FIELDS.keys()):
      observations[stream].append({'logMonoTime': nanos, 'chain': chain,
                                   'value': cache[stream]['decoded'][ACCEL_DBC_FIELDS[stream]]})
    if 'cmd' not in accepted:
      continue
    fresh = {k: s for k, s in sorted(cache.items()) if 0 <= nanos - s['time'] < AGE_NS}
    if not REQUIRED.issubset(fresh):
      metadata['incomplete_command_context'] += 1
      continue
    rows.append(_snapshot(nanos, fresh, chain))
  return {'control_ready': False, 'metadata': dict(metadata), 'streams': {k: dict(v) for k, v in sorted(streams.items())},
          'rows': rows, 'observations': observations}


def _relationship(pairs, comparable_units=False):
  if not pairs:
    return {'n': 0, 'correlation': None}
  x, y = zip(*pairs, strict=True)
  n = len(x)
  mx, my = sum(x) / n, sum(y) / n
  xx, yy = sum((v - mx) ** 2 for v in x), sum((v - my) ** 2 for v in y)
  xy = sum((a - mx) * (b - my) for a, b in pairs)
  result = {'n': n, 'correlation': xy / math.sqrt(xx * yy) if xx > 1e-12 and yy > 1e-12 else None}
  if comparable_units:
    differences = sorted(b - a for a, b in pairs)
    result.update(difference_y_minus_x_range=[differences[0], differences[-1]],
                  difference_y_minus_x_median=(differences[(n - 1) // 2] + differences[n // 2]) / 2)
  return result


def _time_average(index, start, end):
  if index is None:
    return None
  times, points = index
  left, right = bisect_right(times, start) - 1, bisect_left(times, end)
  if left < 0 or right >= len(times):
    return None
  integral = 0.
  for i in range(left, right):
    t0, t1 = times[i], times[i + 1]
    if not 0 < t1 - t0 < AGE_NS:
      return None
    a, b = max(start, t0), min(end, t1)
    if b <= a:
      continue
    y0, y1 = points[i]['value'], points[i + 1]['value']
    ya = y0 + (y1 - y0) * ((a - t0) / (t1 - t0))
    yb = y0 + (y1 - y0) * ((b - t0) / (t1 - t0))
    integral += (ya + yb) * .5 * (b - a)
  return integral / (end - start)


def _wheel_windows(rows, observations=None):
  windows, chunk = [], []
  grouped = defaultdict(list)
  if observations is None:
    # Useful for callers with already-normalized diagnostic snapshots.
    seen = set()
    for row in rows:
      for stream, field in ACCEL_FIELDS.items():
        nanos = row.get('signal_times_ns', {}).get(stream)
        key = (row.get('source'), row['chain'], stream)
        if nanos is not None and field in row['values'] and (*key, nanos) not in seen:
          grouped[key].append({'logMonoTime': nanos, 'value': row['values'][field]})
          seen.add((*key, nanos))
  else:
    for stream, points in observations.items():
      for point in points:
        grouped[(point.get('source'), point['chain'], stream)].append(point)
  indexes = {key: ([p['logMonoTime'] for p in points], points) for key, points in grouped.items()}

  def flush():
    times = [r['logMonoTime'] for r in chunk]
    left = 0
    while left < len(chunk):
      right = bisect_right(times, times[left] + 1_000_000_000 - 1)
      if right >= len(chunk):
        break
      a, b = chunk[left], chunk[right]
      ta = a['logMonoTime'] - round(a['ages_ms']['wheel'] * 1e6)
      tb = b['logMonoTime'] - round(b['ages_ms']['wheel'] * 1e6)
      seconds = (tb - ta) / 1e9
      if .9 <= seconds <= 1.1:
        means = {field: _time_average(indexes.get((a.get('source'), a['chain'], stream)), ta, tb)
                 for stream, field in ACCEL_FIELDS.items()}
        if all(value is not None for value in means.values()):
          windows.append({'hud': a['hud'], 'pedal': a['pedal'], 'source': a.get('source'),
                          'interval_ns': [ta, tb], 'wheel_derivative': (b['speed_mps'] - a['speed_mps']) / seconds,
                          'values': means, 'comparison': 'time_weighted_piecewise_linear_mean_over_same_host_interval',
                          'raw_example_at_start': a['raw'], 'raw_example_logMonoTime': a['logMonoTime']})
      left = right + 1

  for row in rows:
    eligible = (row['speed_mps'] >= 1 and row['ages_ms']['wheel'] <= 25 and
                {'sensor_accel_candidate', 'vlc_accel_candidate'}.issubset(row['values']))
    if chunk:
      previous = chunk[-1]
      changed = any(row.get(k) != previous.get(k) for k in ('chain', 'source', 'hud', 'pedal'))
      if not eligible or changed or not 0 < row['logMonoTime'] - previous['logMonoTime'] < AGE_NS:
        flush()
        chunk = []
    if eligible:
      chunk.append(row)
  flush()
  return windows


def summarize_rows(rows, observations=None):
  groups = defaultdict(list)
  for row in rows:
    groups[(row['hud'], row['pedal'])].append(row)
  summaries = []
  for (hud, pedal), group in sorted(groups.items()):
    signals = defaultdict(list)
    for row in group:
      for key, value in row['values'].items():
        signals[key].append(value)
    stats = {k: {'n': len(v), 'range': [min(v), max(v)], 'negative': sum(x < 0 for x in v),
                 'zero': sum(x == 0 for x in v), 'top_values': Counter(v).most_common(8)} for k, v in sorted(signals.items())}
    pairs = {'cmd_vs_sensor': ('cmd_accel_candidate', 'sensor_accel_candidate'),
             'pedal_vs_brake_raw': ('brake_pedal_candidate', 'brake_raw16_at32'),
             'pedal_vs_ipb': ('brake_pedal_candidate', 'ipb_brake_candidate'),
             'cmd_vs_drive_throttle': ('cmd_accel_candidate', 'drive_throttle_candidate')}
    relationships = {label: _relationship([(r['values'][a], r['values'][b]) for r in group if a in r['values'] and b in r['values']],
                                         comparable_units=label == 'cmd_vs_sensor')
                     for label, (a, b) in pairs.items()}
    summaries.append({'hud': hud, 'pedal': pedal, 'samples': len(group), 'signals': stats,
                      'relationships': relationships, 'examples': group[:2]})
  by_window = defaultdict(list)
  for window in _wheel_windows(rows, observations):
    by_window[(window['hud'], window['pedal'])].append(window)
  derivatives = []
  for (hud, pedal), group in sorted(by_window.items()):
    relationships = {label: _relationship([(w['wheel_derivative'], w['values'][key]) for w in group], comparable_units=True)
                     for label, key in [('wheel_vs_sensor', 'sensor_accel_candidate'), ('wheel_vs_vlc', 'vlc_accel_candidate'),
                                       ('wheel_vs_cmd', 'cmd_accel_candidate')]}
    derivatives.append({'hud': hud, 'pedal': pedal, 'windows': len(group), 'relationships': relationships, 'examples': group[:3]})
  return {'control_ready': False, 'samples': len(rows), 'groups': summaries, 'wheel_windows': derivatives}


def analyze_logs(paths):
  segments, all_rows = [], []
  all_observations = {key: [] for key in ACCEL_FIELDS}
  for path in local_log_paths(paths):
    result = analyze_events(read_local_events(path))
    rows = result.pop('rows')
    observations = result.pop('observations')
    for stream, points in observations.items():
      for point in points:
        point['source'] = str(path)
      all_observations[stream].extend(points)
    for row in rows:
      row['source'] = str(path)
    result.update(source=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest(), summary=summarize_rows(rows, observations))
    segments.append(result)
    all_rows.extend(rows)
  return {'control_ready': False, 'meaning': 'Passive stock observations; no OP acceptance, withdrawal, ownership or AEB validation',
          'max_context_age_ms': AGE_NS / 1e6, 'candidate_counter_shifts': CANDIDATE_COUNTER_SHIFT,
          'dbc_sha256': hashlib.sha256((Path(DBC_PATH) / (DBC_DEFINITION.name + '.dbc')).read_bytes()).hexdigest(),
          'segments': segments, 'summary': summarize_rows(all_rows, all_observations)}


def main(argv=None):
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('logs', nargs='+')
  parser.add_argument('--output', type=Path, help='Create a new report; never overwrite')
  args = parser.parse_args(argv)
  try:
    result = analyze_logs(args.logs)
    text = json.dumps(result, indent=2, allow_nan=False) + '\n'
    if args.output:
      with args.output.open('x') as output:
        output.write(text)
    else:
      print(text, end='')
  except (ValueError, OSError, RuntimeWarning) as exc:
    parser.error(str(exc))
  return 0


if __name__ == '__main__':
  raise SystemExit(main())
