"""Offline stock longitudinal transitions; never transmits or authorizes control."""

import argparse
import hashlib
import json
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from itertools import groupby
from pathlib import Path

from openpilot.tools.car_porting.byd_longitudinal_audit import DBC_DEFINITION, decode_frame, local_log_paths, read_local_events

STREAMS = {(2, 0x32D): 'hud', (2, 0x32E): 'cmd', (2, 0x32F): 'aeb',
           (0, 0x220): 'vlc', (0, 0x342): 'pedal', (0, 0x1F0): 'wheel', (0, 0x3B0): 'buttons'}
MAX_AGE_NS = 100_000_000  # Analysis continuity/join window; not an ECU timeout.
WINDOW_NS = 1_000_000_000
ACTIVE_CANDIDATES = {3, 5, 6, 7}
CMD_FLAGS = ('STANDSTILL_STATE', 'STANDSTILL_RESUME', 'ACC_CONTROLLABLE_AND_ON',
             'ACC_OVERRIDE_OR_STANDSTILL', 'ACC_REQ_NOT_STANDSTILL', 'BRAKE_BEHAVIOR', 'ESP_BEHAVIOR')


def _decode(stream, address, payload):
  values = decode_frame(address, payload)
  counter = int(values['COUNTER']) if 'COUNTER' in values else None
  has_checksum = any(signal.calc_checksum is not None for signal in DBC_DEFINITION.msgs[address].sigs.values())
  integrity = 'dbc_checksum' if has_checksum else 'unchecked'
  if stream == 'vlc':
    if sum(payload) % 256 != 255:
      raise ValueError('candidate_checksum')
    counter = payload[6] & 15
    integrity = 'candidate_additive_checksum_not_dbc'
    fields = {'mode_bit2': (payload[1] >> 2) & 1, 'hold': int(values['BRAKE_HOLD_ACTIVE']),
              'brake_active_low': int(values['BRAKE_APPLIED_ACTIVE_LOW'])}
  elif stream == 'hud':
    fields = {'state': int(values['CRUISE_STATE']), 'aeb': int(values['AEB']), 'fcw': int(values['FCW'])}
  elif stream == 'cmd':
    fields = {key: int(values[key]) for key in CMD_FLAGS}
    fields['raw_zero'] = payload[0] == 0
    fields['byte5'] = payload[5]
  elif stream == 'pedal':
    fields = {'gas_pressed': values['GAS_PEDAL'] > .01, 'brake_pressed': values['BRAKE_PEDAL'] > .01}
  elif stream == 'wheel':
    if any(values['FAULT_' + wheel] for wheel in ('FL', 'FR', 'BL', 'BR')):
      raise ValueError('wheel_fault')
    values['speed_mps'] = sum(values['WHEELSPEED_' + wheel] for wheel in ('FL', 'FR', 'BL', 'BR')) / 14.4
    fields = {'stationary': values['speed_mps'] < .1}
  elif stream == 'buttons':
    fields = {key: int(values[key]) for key in ('RES_BTN', 'ACC_ON_BTN', 'ACC_SWITCH', 'LKAS_ON_BTN', 'BTN_ACC_DEC')}
  else:
    fields = {'first_six_bytes': payload[:6].hex()}
  return values, fields, counter, integrity


def _category(old, new):
  if old not in ACTIVE_CANDIDATES and new in ACTIVE_CANDIDATES:
    return 'entry_candidate'
  if old in ACTIVE_CANDIDATES and new not in ACTIVE_CANDIDATES:
    return 'exit_candidate'
  if new == 5:
    return 'gas_override_candidate'
  if old == 5 and new == 3:
    return 'override_return_candidate'
  if new == 6:
    return 'hold_candidate'
  if new == 7:
    return 'hold_wait_candidate'
  if old in (6, 7) and new == 3:
    return 'restart_candidate'
  return 'other_state_change'


def _coverage(samples, start, end):
  if not samples:
    return {'complete_window': False, 'reason': 'no_eligible_samples'}
  times = [s['logMonoTime'] for s in samples]
  lo, hi = bisect_right(times, start) - 1, bisect_left(times, end)
  # An edge window extending past segment boundaries is incomplete.
  if lo < 0 or hi >= len(samples):
    return {'complete_window': False, 'reason': 'window_not_bracketed'}
  selected = samples[lo:hi + 1]
  complete = len({s['chain'] for s in selected}) == 1
  return {'complete_window': complete, 'reason': 'continuous_observation' if complete else 'integrity_or_timing_gap',
          'bracket_ns': [times[lo], times[hi]], 'eligible_samples': len(selected)}


def _episodes(edges, samples):
  result = []
  for anchor in edges:
    if (anchor['stream'], anchor['field']) != ('hud', 'state'):
      continue
    left, right = anchor['interval_ns']
    start, end = left - WINDOW_NS, right + WINDOW_NS
    nearby = []
    for other in edges:
      if other is anchor:
        continue
      a, b = other['interval_ns']
      if b < start or a > end:
        continue
      delta = [(a - right) / 1e6, (b - left) / 1e6]
      order = 'observed_before' if delta[1] < 0 else 'observed_after' if delta[0] > 0 else 'overlap_or_same_batch'
      nearby.append(dict(other, relative_interval_ms=delta, ordering=order))
    result.append({'category': _category(anchor['old'], anchor['new']), 'anchor': anchor, 'nearby_edges': nearby,
                   'coverage': {k: _coverage(samples[k], start, end) for k in sorted(set(STREAMS.values()))},
                   'meaning': 'Observed RX change intervals, not causal order, ECU latency or OP acceptance'})
  return result


def analyze_events(events):
  """Keep each stream's change interval; do not fabricate edges over evidence gaps."""
  stats, previous, counters, chains, cache = defaultdict(Counter), {}, {}, Counter(), {}
  samples, edges, metadata = defaultdict(list), [], Counter()
  fingerprints, recorded_long = set(), set()

  def can_events():
    last_time = -1
    for event in events:
      if event.which() == 'carParams':
        fingerprints.add(event.carParams.carFingerprint)
        recorded_long.add(event.carParams.openpilotLongitudinalControl)
      if event.which() != 'can':
        continue
      if event.logMonoTime < last_time:
        raise ValueError('CAN timestamps out of order; will not infer transitions after sorting')
      last_time = event.logMonoTime
      yield event

  def reset(stream):
    previous.pop(stream, None)
    cache.pop(stream, None)
    chains[stream] += 1

  for nanos, group in groupby(can_events(), key=lambda e: e.logMonoTime):
    batch = list(group)
    metadata['can_envelopes'] += len(batch)
    if nanos <= 0 or any(not event.valid for event in batch):
      metadata['invalid_timestamp_batches'] += 1
      for stream in sorted(set(STREAMS.values())):
        reset(stream)
      counters.clear()
      continue
    frames = defaultdict(list)
    for event in batch:
      for frame in event.can:
        stream = STREAMS.get((frame.src, frame.address))
        if stream:
          frames[stream].append(frame)
    pending_edges = []
    for stream, items in sorted(frames.items()):
      stats[stream]['frames'] += len(items)
      decoded = []
      for frame in items:
        try:
          values, fields, counter, integrity = _decode(stream, frame.address, bytes(frame.dat))
        except ValueError as exc:
          stats[stream][str(exc) + '_errors'] += 1
          reset(stream)
          counters.pop(stream, None)
          continue
        # Multiple source frames have no separate wire times. Do not update or
        # compare their counters in host iteration order, including bad frames.
        if counter is not None and len(items) == 1:
          last_counter = counters.get(stream)
          counters[stream] = counter
          if last_counter is not None and counter != (last_counter + 1) % 16:
            stats[stream]['counter_discontinuities'] += 1
            reset(stream)
            continue
        decoded.append({'logMonoTime': nanos, 'payload_hex': bytes(frame.dat).hex(),
                        'values': values, 'fields': fields, 'integrity': integrity})
      if len(items) != 1:
        stats[stream]['ambiguous_batches'] += 1
        reset(stream)
        counters.pop(stream, None)
        continue
      if len(decoded) != 1:
        continue
      sample = decoded[0]
      prior = previous.get(stream)
      if prior is not None and nanos - prior['logMonoTime'] >= MAX_AGE_NS:
        stats[stream]['timing_gaps'] += 1
        reset(stream)
        prior = None
      sample['chain'] = chains[stream]
      if prior is not None:
        for field, value in sorted(sample['fields'].items()):
          if value != prior['fields'][field]:
            pending_edges.append({'stream': stream, 'field': field, 'old': prior['fields'][field], 'new': value,
                                  'interval_ns': [prior['logMonoTime'], nanos],
                                  'old_payload_hex': prior['payload_hex'], 'new_payload_hex': sample['payload_hex'],
                                  'integrity': sample['integrity']})
      samples[stream].append(sample)
      cache[stream] = sample
      previous[stream] = sample
      stats[stream]['eligible_samples'] += 1

    fresh = {k: {'logMonoTime': v['logMonoTime'], 'age_ms': (nanos - v['logMonoTime']) / 1e6,
                 'payload_hex': v['payload_hex'], 'values': v['values'], 'fields': v['fields'], 'integrity': v['integrity']}
             for k, v in sorted(cache.items()) if 0 <= nanos - v['logMonoTime'] < MAX_AGE_NS}
    edges.extend(dict(edge, context=fresh) for edge in pending_edges)

  episodes = _episodes(edges, samples)
  # Stable cross-stream snapshots are observations, not a permitted command table.
  snapshots = Counter()
  for sample in samples['hud']:
    snapshots[str(sample['fields']['state'])] += 1
  return {'control_ready': False, 'mode': 'passive_transition_analysis',
          'max_analysis_gap_ms': MAX_AGE_NS / 1e6, 'episode_window_ms': WINDOW_NS / 1e6,
          'time_meaning': 'Host receive intervals; equal timestamps do not establish wire order',
          'vehicle_fingerprints': sorted(fingerprints), 'recorded_op_longitudinal': sorted(recorded_long),
          'metadata': dict(metadata), 'streams': {k: dict(v) for k, v in sorted(stats.items())},
          'hud_sample_counts': dict(snapshots), 'edge_counts': dict(Counter(f"{e['stream']}:{e['field']}" for e in edges)),
          'episode_counts': dict(Counter(e['category'] for e in episodes)), 'edges': edges, 'episodes': episodes}


def analyze_logs(paths):
  local = local_log_paths(paths)
  segments = []
  for path in local:
    segment = analyze_events(read_local_events(path))
    segment['source'] = str(path)
    segment['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    segments.append(segment)
  return {'control_ready': False, 'meaning': 'Stock RX observations only; not verified OP control, ownership or AEB arbitration',
          'segments': segments}


def main(argv=None):
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('logs', nargs='+', help='Completed local log files only')
  parser.add_argument('--output', type=Path, help='Create a new JSON file; never overwrite an existing file')
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
