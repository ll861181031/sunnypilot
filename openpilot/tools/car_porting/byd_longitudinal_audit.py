"""Offline evidence collection; never authorizes or transmits vehicle commands."""

import argparse
import json
import statistics
import warnings
from collections import Counter, defaultdict
from pathlib import Path

import zstandard

from opendbc.can.dbc import DBC
from opendbc.can.parser import get_raw_value
from openpilot.tools.lib.logreader import LogReader


DBC_NAME = 'byd_song_plus_dmi_23_alt'
DBC_DEFINITION = DBC(DBC_NAME)
ADDRESSES = (0x32D, 0x32E, 0x32F, 0x1F0, 0x342)
CONTEXT = {(2, 0x32D): 'hud', (0, 0x1F0): 'wheel_speed', (0, 0x342): 'pedals'}
# Analysis join window only, not a validated control/actuator timeout.
CONTEXT_AGE_NS = 100_000_000
FLAGS = ('STANDSTILL_STATE', 'STANDSTILL_RESUME', 'ACC_CONTROLLABLE_AND_ON',
         'ACC_OVERRIDE_OR_STANDSTILL', 'ACC_REQ_NOT_STANDSTILL', 'BRAKE_BEHAVIOR', 'ESP_BEHAVIOR')


def decode_frame(address: int, payload: bytes) -> dict[str, float]:
  msg = DBC_DEFINITION.msgs[address]
  if len(payload) != msg.size:
    raise ValueError('length')
  values = {}
  for name, sig in msg.sigs.items():
    raw = get_raw_value(payload, sig)
    if sig.calc_checksum is not None and sig.calc_checksum(address, sig, bytearray(payload)) != raw:
      raise ValueError('checksum')
    if sig.is_signed and raw & (1 << (sig.size - 1)):
      raw -= 1 << sig.size
    values[name] = raw * sig.factor + sig.offset
  return values


def audit_events(events) -> dict:
  streams = {}
  context = {}
  ambiguous_context = Counter()
  patterns = {}
  transitions = []
  last_key = None
  last_time = 0
  invalid_events = 0
  fingerprints = set()
  longitudinal_flags = set()
  for event in events:
    if event.which() == 'carParams':
      fingerprints.add(event.carParams.carFingerprint)
      longitudinal_flags.add(event.carParams.openpilotLongitudinalControl)
    if event.which() != 'can':
      continue
    nanos = event.logMonoTime
    if not event.valid or nanos <= 0:
      invalid_events += 1
      context.clear()
      last_key = None
      continue
    if nanos < last_time:
      context.clear()
      last_key = None
    last_time = nanos
    commands = []
    batch_context_counts = Counter()
    for frame in event.can:
      address, bus, payload = frame.address, frame.src, frame.dat
      # Reject Panda echo/rejected-frame source flags, never mask them into RX.
      if address not in ADDRESSES or bus not in (0, 2):
        continue
      key = f'{bus}:{address:#x}'
      if key not in streams:
        streams[key] = {'frames': 0, 'length_errors': 0, 'checksum_errors': 0, 'counter_discontinuities': 0,
                        'nonincreasing_timestamps': 0, 'signal_ranges': {}, 'signal_counts': defaultdict(Counter),
                        '_periods': [], '_time': None, '_counter': None}
      stats = streams[key]
      checksum_available = any(sig.calc_checksum is not None for sig in DBC_DEFINITION.msgs[address].sigs.values())
      stats['checksum_available'] = checksum_available
      stats['frames'] += 1
      context_name = CONTEXT.get((bus, address))
      if context_name:
        batch_context_counts[context_name] += 1
      if stats['_time'] is not None:
        period = nanos - stats['_time']
        if period > 0:
          stats['_periods'].append(period)
        else:
          stats['nonincreasing_timestamps'] += 1
      stats['_time'] = nanos
      try:
        values = decode_frame(address, payload)
      except ValueError as exc:
        stats[f'{exc}_errors'] += 1
        context.pop(CONTEXT.get((bus, address)), None)
        stats['_counter'] = None
        if bus == 2 and address == 0x32E:
          last_key = None
        continue
      counter_continuous = True
      if 'COUNTER' in values:
        counter = int(values['COUNTER'])
        if stats['_counter'] is not None and counter != (stats['_counter'] + 1) % 16:
          stats['counter_discontinuities'] += 1
          counter_continuous = False
        stats['_counter'] = counter
      for signal, value in values.items():
        bounds = stats['signal_ranges'].setdefault(signal, [value, value])
        bounds[0], bounds[1] = min(bounds[0], value), max(bounds[1], value)
        if DBC_DEFINITION.msgs[address].sigs[signal].size <= 3:
          stats['signal_counts'][signal][str(int(value))] += 1
      if address == 0x32D:
        stats['signal_counts']['CRUISE_STATE'][str(int(values['CRUISE_STATE']))] += 1
      sample = {'logMonoTime': nanos, 'payload': payload.hex(), 'signals': values,
                'checksum_verified': checksum_available, 'counter_continuous': counter_continuous}
      if context_name:
        if address == 0x1F0:
          sample['speed_mps'] = sum(values[f'WHEELSPEED_{w}'] for w in ('FL', 'FR', 'BL', 'BR')) / 4 / 3.6
        context.pop(context_name, None)
        if counter_continuous:
          context[context_name] = sample
      if bus == 2 and address == 0x32E:
        commands.append(sample)

    # Multiple source samples in one envelope have no distinct wire timestamps.
    for name, count in batch_context_counts.items():
      if count > 1:
        ambiguous_context[name] += 1
        context.pop(name, None)
    for command in commands:
      fresh = {name: dict(sample, age_ms=(nanos - sample['logMonoTime']) / 1e6)
               for name, sample in context.items() if 0 <= nanos - sample['logMonoTime'] < CONTEXT_AGE_NS}
      values = command['signals']
      state = fresh['hud']['signals']['CRUISE_STATE'] if 'hud' in fresh else None
      key = (state, *(int(values[flag]) for flag in FLAGS))
      if key not in patterns:
        patterns[key] = {'cruise_state_candidate': state, 'flags': {f: int(values[f]) for f in FLAGS},
                         'frames': 0, 'accel_candidate_range': [values['ACCEL_CMD'], values['ACCEL_CMD']],
                         'wheel_speed_mps_range': None, 'pedal_pressed_frames': 0, 'pedal_known_frames': 0,
                         'first_logMonoTime': nanos, 'example_payload': command['payload']}
      pattern = patterns[key]
      pattern['frames'] += 1
      lo, hi = pattern['accel_candidate_range']
      pattern['accel_candidate_range'] = [min(lo, values['ACCEL_CMD']), max(hi, values['ACCEL_CMD'])]
      if 'wheel_speed' in fresh:
        speed = fresh['wheel_speed']['speed_mps']
        lo, hi = pattern['wheel_speed_mps_range'] or [speed, speed]
        pattern['wheel_speed_mps_range'] = [min(lo, speed), max(hi, speed)]
      if 'pedals' in fresh:
        pattern['pedal_known_frames'] += 1
        pedal = fresh['pedals']['signals']
        pattern['pedal_pressed_frames'] += int(pedal['GAS_PEDAL'] > 0.01 or pedal['BRAKE_PEDAL'] > 0.01)
      if key != last_key:
        transitions.append({'logMonoTime': nanos, 'payload': command['payload'], 'candidate': values, 'context': fresh,
                            'counter_continuous': command['counter_continuous']})
      last_key = key

  for stats in streams.values():
    periods = stats.pop('_periods')
    stats['median_period_ms'] = statistics.median(periods) / 1e6 if periods else None
    stats['max_period_ms'] = max(periods) / 1e6 if periods else None
    del stats['_counter'], stats['_time']
  return {'control_ready': False, 'dbc': DBC_NAME, 'context_join_window_ms': CONTEXT_AGE_NS / 1e6,
          'vehicle_fingerprints': sorted(fingerprints), 'recorded_op_longitudinal': sorted(longitudinal_flags),
          'invalid_can_events': invalid_events, 'ambiguous_context_batches': dict(ambiguous_context),
          'streams': streams, 'patterns': list(patterns.values()), 'transitions': transitions}


def local_log_paths(paths) -> list[Path]:
  """Resolve distinct completed local recordings, without identifier routing."""
  local_paths = []
  for path in paths:
    local = Path(path)
    if '://' in str(path) or not local.is_file():
      raise ValueError(f'Expected an existing local log file: {path}')
    resolved = local.resolve()
    if any((parent / 'rlog.lock').exists() for parent in (local.parent, resolved.parent)):
      raise ValueError(f'Recording lock present: {local.parent}')
    local_paths.append(resolved)
  if not local_paths or len(set(local_paths)) != len(local_paths):
    raise ValueError('Provide distinct local log files')
  return local_paths


def read_local_events(path: Path):
  """Strict local deserialization shared by audit and offline replay."""
  path = local_log_paths([path])[0]
  # The identifier router can interpret even a local filename as a URL.
  # Read local bytes directly; require a complete compressed stream first.
  raw = path.read_bytes()
  if raw.startswith(b'\x28\xb5\x2f\xfd'):
    chunks = []
    try:
      while raw:
        decoder = zstandard.ZstdDecompressor().decompressobj()
        chunks.append(decoder.decompress(raw))
        if not decoder.eof:
          raise ValueError(f'Incomplete Zstandard frame: {path}')
        raw = decoder.unused_data
    except zstandard.ZstdError as exc:
      raise ValueError(f'Invalid Zstandard stream: {path}') from exc
    raw = b''.join(chunks)
  if not raw:
    raise ValueError(f'Empty local log: {path}')
  # Never accept LogReader's partial recovery as a complete protocol capture.
  with warnings.catch_warnings():
    warnings.simplefilter('error', RuntimeWarning)
    return list(LogReader.from_bytes(raw))


def audit_logs(paths) -> dict:
  local_paths = local_log_paths(paths)
  segments = []
  for path in local_paths:
    result = audit_events(sorted(read_local_events(path), key=lambda event: event.logMonoTime))
    result['source'] = str(path)
    segments.append(result)
  return {'control_ready': False, 'meaning': 'DBC hypotheses and RX observations only; not ECU acceptance or actuator validation',
          'segments': segments}


def main(argv=None):
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('logs', nargs='+', help='Existing local rlog files; no device access or network sources')
  parser.add_argument('--output', type=Path, help='New JSON report file (existing files are never overwritten)')
  args = parser.parse_args(argv)
  try:
    result = audit_logs(args.logs)
    text = json.dumps(result, indent=2, allow_nan=False) + '\n'
    if args.output:
      with args.output.open('x') as report:
        report.write(text)
    else:
      print(text, end='')
  except (ValueError, OSError, RuntimeWarning) as exc:
    parser.error(str(exc))
  return 0


if __name__ == '__main__':
  raise SystemExit(main())
