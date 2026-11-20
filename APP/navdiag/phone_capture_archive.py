from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
import stat
import struct
import zipfile

if __package__:
  from .native_route_source import NativeRouteProbeSource, next_road_field_rejections
else:
  from native_route_source import NativeRouteProbeSource, next_road_field_rejections

MAX_BYTES = 256 * 1024 * 1024
MAX_FILES = 1025
SESSION = re.compile(r'[0-9]{13}-[0-9a-f]{32}')
RELATIVE = re.compile(r'(?:geometry/)?[a-zA-Z0-9_.-]{1,100}')
HEX = re.compile(r'[0-9a-f]{64}')


def _fail(reason):
  raise ValueError(reason)


def _pairs(items):
  result = {}
  for key, value in items:
    if key in result:
      _fail('duplicate_json_key')
    result[key] = value
  return result


def _json(data):
  return json.loads(data, object_pairs_hook=_pairs, parse_constant=lambda _: _fail('nonfinite_json'))


def _relative(name):
  if not isinstance(name, str) or not RELATIVE.fullmatch(name) or '..' in name:
    _fail('archive_path_invalid')
  return name


def _load(path):
  path = Path(path)
  files = {}
  total = 0
  if path.is_dir():
    session = path.name
    for file in path.rglob('*'):
      if file.is_symlink():
        _fail('archive_link_rejected')
      if file.is_dir():
        continue
      name = _relative(file.relative_to(path).as_posix())
      total += file.stat().st_size
      if total > MAX_BYTES or len(files) >= MAX_FILES:
        _fail('archive_size_limit')
      files[name] = file.read_bytes()
  else:
    session = None
    with zipfile.ZipFile(path) as archive:
      for info in archive.infolist():
        parts = PurePosixPath(info.filename).parts
        if info.is_dir() or len(parts) not in (2, 3) or info.filename.startswith('/'):
          _fail('archive_path_invalid')
        if session is None:
          session = parts[0]
        if parts[0] != session or stat.S_ISLNK(info.external_attr >> 16) or info.flag_bits & 1:
          _fail('archive_link_or_session_invalid')
        name = _relative('/'.join(parts[1:]))
        if name in files:
          _fail('duplicate_archive_file')
        total += info.file_size
        if total > MAX_BYTES or len(files) >= MAX_FILES:
          _fail('archive_size_limit')
        files[name] = archive.read(info)
  if not isinstance(session, str) or not SESSION.fullmatch(session):
    _fail('session_identity_invalid')
  return session, files


def decode_geometry(data):
  if len(data) < 112 or data[:16] != b'AMAP_NAV_GEO_V2\0':
    _fail('geometry_magic_invalid')
  version, navi_type, state, paths, main, segments, links, points = struct.unpack_from('<8I', data, 16)
  if (version != 1 or navi_type != 0 or state not in (1, 3) or not 1 <= paths <= 32
      or not 0 <= main < paths or not 1 <= segments <= 8192 or not 1 <= links <= 100000
      or not 2 <= points <= 100000 or len(data) != 112 + links * 12 + points * 8
      or data[48:80] == bytes(32) or data[80:112] == bytes(32)):
    _fail('geometry_header_invalid')
  previous_segment, previous_start, end = -1, -1, 0
  for i in range(links):
    segment, start, count = struct.unpack_from('<3I', data, 112 + 12 * i)
    if count < 2 or start + count > points or segment >= segments:
      _fail('geometry_link_invalid')
    if segment != previous_segment:
      if segment != previous_segment + 1 or start != (0 if previous_segment == -1 else end - 1):
        _fail('geometry_segment_discontinuity')
    elif not previous_start < start < end < start + count:
      _fail('geometry_link_discontinuity')
    previous_segment, previous_start, end = segment, start, start + count
  if previous_segment != segments - 1 or end != points:
    _fail('geometry_coverage_invalid')
  for x, y in struct.iter_unpack('<2i', data[112 + 12 * links:]):
    if (x == 0 and y == 0) or abs(x) > 180 * 3600000 or abs(y) > 90 * 3600000:
      _fail('geometry_coordinate_invalid')
  return {'path_identity_hash': data[48:80].hex(), 'process_epoch_hash': data[80:112].hex(),
          'points': points, 'links': links, 'segments': segments}


def _same(before, after):
  return (all(item.get('status') == 'observed' and item.get('sample_consistent') is True
              for item in (before, after))
          and all(isinstance(before.get(key), str) and HEX.fullmatch(before[key])
                  and before[key] == after.get(key) for key in ('path_identity_hash', 'process_epoch_hash')))


_GEOMETRY_DIAG_FIELDS = {
  'schema_version', 'reason', 'diagnostic_only', 'source_snapshot_consistent', 'control_input_allowed',
  'route_render_input_allowed', 'observation_clock_domain', 'path_identity_hash', 'process_epoch_hash',
  'geometry_read_pass', 'source_coordinate_scale', 'previous_segment_index', 'previous_link_index',
  'previous_link_local_start', 'previous_link_point_count', 'next_segment_index', 'next_link_index',
  'next_link_local_start', 'next_link_point_count', 'previous_link_tail_native', 'next_link_head_native',
  'observation_started_monotonic_ns', 'observation_finished_monotonic_ns', 'read_calls', 'read_bytes',
}
MAX_DIAGNOSTIC_RUNS = 128


class _NaviInfoFieldDiagnostics:
  """Summarize quarantined raw scalars without publishing route identity or coordinates."""

  def __init__(self):
    self.observations = self.missing = self.frames = self.dropped = 0
    self.groups = {}
    self.validator = NativeRouteProbeSource.__new__(NativeRouteProbeSource)

  def observe(self, frame):
    affected = False
    reasons = {'navi_info_next_segment_invalid', 'navi_info_next_link_invalid'}
    for side in ('native_before', 'native_after'):
      observation = frame[side]
      if observation.get('status') == 'unavailable' and observation.get('reason') in reasons:
        self.missing += 1
        affected = True
        continue
      navi = observation.get('_navi_info')
      if not isinstance(navi, dict) or navi.get('status') != 'observed':
        continue
      try:
        rejected = next_road_field_rejections(navi.get('branches'))
        if not rejected and navi.get('reason') not in reasons:
          continue
        self.validator._validate_navi_info(navi, observation)
      except ValueError:
        _fail('navi_info_field_diagnosis_invalid')
      if (observation.get('sample_consistent') is not True
          or any(not isinstance(observation.get(key), str) or not HEX.fullmatch(observation[key])
                 for key in ('path_identity_hash', 'process_epoch_hash'))):
        _fail('navi_info_field_diagnosis_invalid')
      self.observations += 1
      affected = True
      context = {key: navi[key] for key in ('item_count', 'item_index', 'current_segment',
                                           'current_link', 'current_point', 'branch_flag_raw')}
      identity = [observation['path_identity_hash'], observation['process_epoch_hash'], context, rejected]
      fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
      start, end = (observation[key] for key in ('observation_started_monotonic_ns', 'observation_finished_monotonic_ns'))
      group = self.groups.get(fingerprint)
      if group is None:
        if len(self.groups) >= MAX_DIAGNOSTIC_RUNS:
          self.dropped += 1
          continue
        group = {'field_rejections': rejected, **context, 'first_sequence': frame['sequence'],
                 'last_sequence': frame['sequence'], 'first_observation_started_ns': start,
                 'last_observation_finished_ns': end, 'observations': 0}
        self.groups[fingerprint] = group
      group.update(last_sequence=frame['sequence'], last_observation_finished_ns=end,
                   observations=group['observations'] + 1)
    self.frames += affected

  def report(self):
    return {'observations_with_raw': self.observations, 'missing_context_observations': self.missing,
            'affected_frames': self.frames, 'groups': list(self.groups.values()),
            'group_limit': MAX_DIAGNOSTIC_RUNS, 'ungrouped_observations': self.dropped,
            'source_snapshot_consistency_verified': False, 'control_input_allowed': False,
            'route_render_input_allowed': False, 'clock_domain': 'phone_elapsed_realtime'}


def _geometry_diagnosis(value):
  def invalid():
    _fail('geometry_rejection_diagnosis_invalid')

  if (not isinstance(value, dict) or value.keys() != _GEOMETRY_DIAG_FIELDS
      or type(value.get('schema_version')) is not int or value['schema_version'] != 1
      or value.get('reason') != 'geometry_point_discontinuity'
      or value.get('diagnostic_only') is not True
      or any(value.get(key) is not False for key in (
        'source_snapshot_consistent', 'control_input_allowed', 'route_render_input_allowed'))
      or value.get('observation_clock_domain') != 'phone_elapsed_realtime'
      or type(value.get('source_coordinate_scale')) is not int or value['source_coordinate_scale'] != 3600000
      or type(value.get('geometry_read_pass')) is not int or value['geometry_read_pass'] not in (1, 2)
      or any(not isinstance(value.get(key), str) or not HEX.fullmatch(value[key])
             for key in ('path_identity_hash', 'process_epoch_hash'))):
    invalid()
  for key, maximum in (('observation_started_monotonic_ns', (1 << 63) - 1),
                        ('observation_finished_monotonic_ns', (1 << 63) - 1),
                        ('read_calls', 131072), ('read_bytes', 16 * 1024 * 1024)):
    if type(value[key]) is not int or not 0 < value[key] <= maximum:
      invalid()
  if value['observation_started_monotonic_ns'] > value['observation_finished_monotonic_ns']:
    invalid()
  for side in ('previous', 'next'):
    for suffix, maximum in (('segment_index', 8191), ('link_index', 99999),
                            ('link_local_start', 499999), ('link_point_count', 500000)):
      number = value[side + '_' + suffix]
      if type(number) is not int or not 0 <= number <= maximum:
        invalid()
    count = value[side + '_link_point_count']
    if count < 2 or value[side + '_link_local_start'] + count > 500000:
      invalid()
    points = value['previous_link_tail_native' if side == 'previous' else 'next_link_head_native']
    if not isinstance(points, list) or len(points) != min(4, count):
      invalid()
    for point in points:
      if (not isinstance(point, list) or len(point) != 2 or any(type(n) is not int for n in point)
          or abs(point[0]) > 180 * 3600000 or abs(point[1]) > 90 * 3600000 or point == [0, 0]):
        invalid()
  previous, following = value['previous_link_tail_native'][-1], value['next_link_head_native'][0]
  if (value['next_segment_index'] != value['previous_segment_index'] + 1
      or value['next_link_index'] != 0 or previous == following):
    invalid()
  scale = math.pi / (180 * 3600000)
  latitude = (previous[1] + following[1]) / 2 * scale
  gap = math.hypot((following[0] - previous[0]) * math.cos(latitude), following[1] - previous[1]) * scale * 6371000
  stable = {key: item for key, item in value.items() if key not in (
    'observation_started_monotonic_ns', 'observation_finished_monotonic_ns', 'read_calls', 'read_bytes')}
  fingerprint = hashlib.sha256(json.dumps(stable, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
  fields = {key: value[key] for key in ('geometry_read_pass', 'previous_segment_index', 'previous_link_index',
                                      'previous_link_local_start', 'previous_link_point_count', 'next_segment_index',
                                      'next_link_index', 'next_link_local_start', 'next_link_point_count')}
  return {'fingerprint': fingerprint, 'endpoint_gap_approx_m': round(gap, 3), **fields,
          'max_read_calls': value['read_calls'], 'max_read_bytes': value['read_bytes'],
          'max_observation_window_ns': value['observation_finished_monotonic_ns'] - value['observation_started_monotonic_ns']}


class _GeometryDiagnostics:
  def __init__(self):
    self.reason_counts = Counter()
    self.observations = self.bound = self.missing = self.total_runs = 0
    self.runs = []
    self.current = None
    self.started = 0

  def observe(self, frame, same):
    before, after = frame['native_before'], frame['native_after']
    reason = frame.get('geometry_reason', 'unrecorded')
    if not isinstance(reason, str) or not re.fullmatch('[a-z][a-z0-9_]{0,99}', reason):
      _fail('geometry_rejection_frame_invalid')
    self.reason_counts[reason] += 1
    if '_geometry_rejection' in after:
      _fail('geometry_rejection_frame_invalid')
    if '_geometry_rejection' not in before:
      self.missing += reason == 'geometry_point_discontinuity'
      self.current = None
      return
    if reason != 'geometry_point_discontinuity' or frame.get('geometry_reference') is not None:
      _fail('geometry_rejection_frame_invalid')
    diagnosis = before['_geometry_rejection']
    sanitized = _geometry_diagnosis(diagnosis)
    if (not frame['sample_started_elapsed_realtime_ns'] <= diagnosis['observation_started_monotonic_ns']
            <= diagnosis['observation_finished_monotonic_ns'] <= frame['sample_finished_elapsed_realtime_ns']
        or (before.get('status') == 'observed'
            and diagnosis['observation_finished_monotonic_ns'] > before['observation_started_monotonic_ns'])
        or ('gps' in frame and diagnosis['observation_finished_monotonic_ns'] > frame['gps']['captured_elapsed_realtime_ns'])):
      _fail('geometry_rejection_frame_invalid')
    bound = same and all(diagnosis[key] == before[key] for key in ('path_identity_hash', 'process_epoch_hash'))
    self.observations += 1
    self.bound += bound
    sequence = frame['sequence']
    if (self.current is None or self.current['fingerprint'] != sanitized['fingerprint']
        or self.current['frame_identity_matched'] is not bound or self.current['last_sequence'] != sequence - 1):
      self.total_runs += 1
      self.started = frame['sample_started_elapsed_realtime_ns']
      self.current = {**sanitized, 'frame_identity_matched': bound, 'first_sequence': sequence,
                      'last_sequence': sequence, 'observations': 0, 'sample_window_span_ns': 0}
      if len(self.runs) < MAX_DIAGNOSTIC_RUNS:
        self.runs.append(self.current)
    self.current.update(last_sequence=sequence, observations=self.current['observations'] + 1,
                        sample_window_span_ns=frame['sample_finished_elapsed_realtime_ns'] - self.started)
    for key in ('max_read_calls', 'max_read_bytes', 'max_observation_window_ns'):
      self.current[key] = max(self.current[key], sanitized[key])

  def report(self):
    return {'reason_frame_counts': dict(self.reason_counts), 'observations': self.observations,
            'bound_observations': self.bound, 'unbound_observations': self.observations - self.bound,
            'missing_context_frames': self.missing, 'runs_total': self.total_runs,
            'runs_dropped': self.total_runs - len(self.runs), 'runs': self.runs,
            'source_snapshot_consistency_verified': False}


def _stable_indices(before, after):
  positions = [item.get('_position', {}) for item in (before, after)]
  indices = []
  for side, position in zip(('before', 'after'), positions, strict=True):
    if not isinstance(position, dict) or position.get('status') != 'observed':
      return None, side + '_position_unavailable'
    values = tuple(position.get(key) for key in ('segment_index', 'link_index', 'point_index'))
    if any(type(value) is not int or value < 0 for value in values):
      return None, side + '_indices_invalid'
    indices.append(values)
  return (None, 'indices_changed_during_pair') if indices[0] != indices[1] else (indices[0], None)


def _record_streams(files, manifest, summary):
  if manifest['schema_version'] == 1:
    return ['frames.jsonl'], ['broadcasts.jsonl']
  if 'segments.json' not in files:
    _fail('segment_index_missing')
  index = _json(files['segments.json'])
  segments = index.get('segments')
  if (index.get('schema_version') != 1 or index.get('partition') != 'writer_rotation'
      or index.get('interval_ms') != 180000 or manifest.get('segment_interval_ms') != 180000
      or manifest.get('max_duration_ms') != 7200000 or not isinstance(segments, list)
      or not 1 <= len(segments) <= 40 or summary.get('segments') != len(segments)):
    _fail('segment_contract_invalid')
  frames, broadcasts = [], []
  opened = manifest.get('started_elapsed_realtime_ns')
  for number, segment in enumerate(segments, 1):
    suffix = '' if number == 1 else f'-{number:04d}'
    frame_name, broadcast_name = f'frames{suffix}.jsonl', f'broadcasts{suffix}.jsonl'
    closed = segment.get('closed_elapsed_realtime_ns')
    if (segment.get('index') != number or segment.get('frames_file') != frame_name
        or segment.get('broadcasts_file') != broadcast_name or frame_name not in files or broadcast_name not in files
        or type(opened) is not int or type(closed) is not int or not 0 < opened <= closed
        or segment.get('opened_elapsed_realtime_ns') != opened):
      _fail('segment_order_or_file_invalid')
    for name, key in ((frame_name, 'frames'), (broadcast_name, 'broadcasts')):
      if segment.get(key) != len(files[name].splitlines()):
        _fail('segment_counter_invalid')
    for line in files[frame_name].splitlines():
      frame = _json(line)
      begin, finish = frame.get('sample_started_elapsed_realtime_ns'), frame.get('sample_finished_elapsed_realtime_ns')
      if type(begin) is not int or type(finish) is not int or not opened <= begin <= finish <= closed:
        _fail('segment_frame_clock_invalid')
    frames.append(frame_name)
    broadcasts.append(broadcast_name)
    opened = closed
  if opened != summary.get('finished_elapsed_realtime_ns'):
    _fail('segment_end_clock_invalid')
  record_files = {name for name in files if name.endswith('.jsonl')}
  if record_files != set(frames + broadcasts):
    _fail('segment_unindexed_stream')
  return frames, broadcasts


def verify(path):
  session, files = _load(path)
  if 'completed.json' not in files:
    _fail('archive_incomplete')
  completed = _json(files['completed.json'])
  if (completed.get('schema_version') not in (1, 2) or completed.get('archive_complete') is not True
      or completed.get('session_id') != session):
    _fail('completion_invalid')
  seen = set()
  for item in completed.get('files', []):
    name = _relative(item.get('path'))
    if name in seen or name == 'completed.json' or name not in files:
      _fail('manifest_file_set_invalid')
    data = files[name]
    if len(data) != item.get('bytes') or hashlib.sha256(data).hexdigest() != item.get('sha256'):
      _fail('archive_hash_mismatch')
    seen.add(name)
  if seen != files.keys() - {'completed.json'} or not {
      'manifest.json', 'summary.json', 'frames.jsonl', 'broadcasts.jsonl'} <= seen:
    _fail('manifest_file_set_invalid')
  manifest, summary = (_json(files[name]) for name in ('manifest.json', 'summary.json'))
  if (manifest.get('schema_version') != completed.get('schema_version') or manifest.get('session_id') != session
      or manifest.get('mode') != 'phone_only_raw_navigation_evidence'
      or manifest.get('clock_domain') != 'phone_elapsed_realtime'
      or manifest.get('source_clock_translated') is not False
      or summary.get('session_id') != session or summary.get('archive_complete') is not True):
    _fail('archive_contract_invalid')
  for item in (manifest, summary):
    if any(item.get(flag) is not False for flag in ('host_pipeline_executed', 'gpu_executed', 'control_input_allowed')):
      _fail('unsupported_acceptance_claim')
  frame_files, broadcast_files = _record_streams(files, manifest, summary)
  geometry = {}
  for name, data in files.items():
    if name.startswith('geometry/'):
      if name != 'geometry/' + hashlib.sha256(data).hexdigest() + '.bin':
        _fail('geometry_filename_mismatch')
      geometry[name] = decode_geometry(data)
  counts = Counter(dict.fromkeys(('frames', 'native_observed_frames', 'fresh_gps_frames', 'paired_source_frames',
                                 'gps_reported_motion_frames', 'same_route_comparable_pairs', 'segment_transitions',
                                 'link_transitions', 'within_link_point_transitions', 'stable_index_source_frames'), 0))
  reasons = Counter()
  index_rejections = Counter()
  geometry_diagnostics = _GeometryDiagnostics()
  navi_field_diagnostics = _NaviInfoFieldDiagnostics()
  prior = None
  last_time = manifest.get('started_elapsed_realtime_ns', 0)
  for sequence, line in enumerate((line for name in frame_files for line in files[name].splitlines()), 1):
    frame = _json(line)
    before, after, gps = (frame[key] for key in ('native_before', 'native_after', 'gps'))
    begin, finish = (frame[key] for key in ('sample_started_elapsed_realtime_ns', 'sample_finished_elapsed_realtime_ns'))
    if (frame.get('sequence') != sequence or not isinstance(begin, int) or not isinstance(finish, int)
        or not 0 < last_time <= begin <= finish or frame.get('control_input_allowed') is not False
        or frame.get('host_pipeline_executed') is not False):
      _fail('frame_sequence_or_clock_invalid')
    last_time = finish
    same = _same(before, after)
    if frame.get('source_pair_same_selection') is not same:
      _fail('source_pair_claim_invalid')
    if gps.get('clock_domain') not in (None, 'phone_elapsed_realtime'):
      _fail('gps_clock_invalid')
    captured = gps.get('captured_elapsed_realtime_ns')
    if not isinstance(captured, int) or not begin <= captured <= finish:
      _fail('gps_capture_clock_invalid')
    for observation, bound in ((before, 'before'), (after, 'after')):
      if observation.get('status') == 'observed':
        start = observation.get('observation_started_monotonic_ns')
        end = observation.get('observation_finished_monotonic_ns')
        if (not isinstance(start, int) or not isinstance(end, int) or not begin <= start <= end <= finish
            or (bound == 'before' and end > captured) or (bound == 'after' and start < captured)):
          _fail('native_pair_clock_invalid')
    reference = frame.get('geometry_reference')
    navi_field_diagnostics.observe(frame)
    if reference is not None:
      if not same or reference not in geometry or any(geometry[reference][key] != before[key]
          for key in ('path_identity_hash', 'process_epoch_hash')):
        _fail('geometry_source_binding_invalid')
    fresh = gps.get('status') == 'observed'
    if fresh:
      source = gps.get('source_elapsed_realtime_ns')
      accuracy = gps.get('horizontal_accuracy_m', 0)
      if (not isinstance(source, int) or not 0 < source <= captured or captured - source >= 2000000000
          or not isinstance(accuracy, (int, float)) or not 0 < accuracy <= 25
          or gps.get('provider') != 'gps' or gps.get('mock') is not False
          or gps.get('control_input_allowed') is not False or gps.get('phone_boot_id') not in (None, manifest.get('phone_boot_id'))):
        _fail('gps_freshness_claim_invalid')
    counts['frames'] += 1
    counts['native_observed_frames'] += before.get('status') == 'observed'
    counts['fresh_gps_frames'] += fresh
    counts['paired_source_frames'] += bool(same and fresh and reference)
    counts['gps_reported_motion_frames'] += fresh and gps.get('speed_mps', 0) > 1
    reasons[before.get('reason', 'unknown')] += 1
    geometry_diagnostics.observe(frame, same)
    current = None
    if same and fresh and reference:
      indices, rejection = _stable_indices(before, after)
      if rejection:
        index_rejections[rejection] += 1
      else:
        counts['stable_index_source_frames'] += 1
        current = (after['path_identity_hash'], after['process_epoch_hash'], reference, indices)
    if prior and current and prior[:3] == current[:3]:
      a, b = prior[3], current[3]
      counts['same_route_comparable_pairs'] += 1
      counts['segment_transitions'] += a[0] != b[0]
      counts['link_transitions'] += a[:2] != b[:2]
      counts['within_link_point_transitions'] += a[:2] == b[:2] and a[2] != b[2]
    prior = current
  for key in ('frames', 'native_observed_frames', 'fresh_gps_frames', 'paired_source_frames'):
    if summary.get(key) != counts[key]:
      _fail('summary_counter_mismatch')
  written, incomplete, previous = 0, 0, 0
  for line in (line for name in broadcast_files for line in files[name].splitlines()):
    broadcast = _json(line)
    sequence = broadcast.get('sequence')
    if type(sequence) is not int or sequence <= previous or broadcast.get('sender_verified') is not False:
      _fail('broadcast_sequence_invalid')
    previous = sequence
    written += 1
    incomplete += broadcast.get('raw_intent', {}).get('complete') is not True
  dropped = summary.get('dropped_broadcasts')
  if (type(dropped) is not int or dropped < 0 or summary.get('received_broadcasts') != written + dropped
      or summary.get('incomplete_broadcasts') != incomplete
      or summary.get('raw_broadcast_lossless') is not (dropped == 0 and incomplete == 0)):
    _fail('broadcast_counter_mismatch')
  return {'archive_integrity': 'verified', 'session_id': session, 'files': len(files), 'segments': len(frame_files),
          **dict(counts), 'geometry_files': len(geometry), 'source_reason_counts': dict(reasons),
          'index_pair_rejection_counts': dict(index_rejections), 'geometry_diagnostics': geometry_diagnostics.report(),
          'navi_info_field_diagnostics': navi_field_diagnostics.report(),
          'probe_closed_cleanly': summary.get('probe_closed_cleanly') is True,
          'capture_failure': summary.get('failure'), 'dropped_broadcasts': dropped,
          'host_pipeline_verified': False, 'gpu_verified': False, 'road_control_verified': False,
          'source_clock_translated': False}


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('archive', type=Path)
  args = parser.parse_args()
  print(json.dumps(verify(args.archive), ensure_ascii=False, indent=2))


if __name__ == '__main__':
  main()
