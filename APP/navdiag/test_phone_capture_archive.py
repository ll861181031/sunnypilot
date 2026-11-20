import hashlib
import json
import struct
import zipfile

import pytest

from phone_capture_archive import decode_geometry, verify
from build import check_manifest
import device


def encode(value):
  return (json.dumps(value) + '\n').encode()


def seal(directory):
  entries = []
  for file in sorted(directory.rglob('*')):
    if file.is_file() and file.name != 'completed.json':
      data = file.read_bytes()
      entries.append({'path': file.relative_to(directory).as_posix(), 'bytes': len(data),
                      'sha256': hashlib.sha256(data).hexdigest()})
  schema = json.loads((directory / 'manifest.json').read_text())['schema_version']
  (directory / 'completed.json').write_bytes(encode({
    'schema_version': schema, 'session_id': directory.name, 'archive_complete': True, 'files': entries}))


@pytest.fixture
def capture(tmp_path):
  directory = tmp_path / ('1794800000000-' + 'a' * 32)
  directory.mkdir()
  payload = (b'AMAP_NAV_GEO_V2\0' + struct.pack('<8I', 1, 0, 1, 1, 0, 1, 1, 3)
             + bytes.fromhex('a' * 64 + 'b' * 64) + struct.pack('<3I', 0, 0, 3)
             + struct.pack('<6i', 36000000, 72000000, 36000001, 72000001, 36000002, 72000002))
  geometry_name = 'geometry/' + hashlib.sha256(payload).hexdigest() + '.bin'
  (directory / 'geometry').mkdir()
  (directory / geometry_name).write_bytes(payload)
  flags = {'host_pipeline_executed': False, 'gpu_executed': False, 'control_input_allowed': False}
  manifest = {'schema_version': 1, 'session_id': directory.name, 'mode': 'phone_only_raw_navigation_evidence',
              'clock_domain': 'phone_elapsed_realtime', 'source_clock_translated': False,
              'phone_boot_id': 'test-boot', 'started_elapsed_realtime_ns': 10, **flags}
  frames = []
  for index in range(2):
    begin = 20 + index * 30
    def observation(start, point):
      return {'status': 'observed', 'reason': 'consistent_read_only_observation', 'sample_consistent': True,
              'path_identity_hash': 'a' * 64, 'process_epoch_hash': 'b' * 64,
              'observation_started_monotonic_ns': start, 'observation_finished_monotonic_ns': start + 2,
              '_position': {'status': 'observed', 'segment_index': 0, 'link_index': 0, 'point_index': point}}
    frames.append({'sequence': index + 1, 'sample_started_elapsed_realtime_ns': begin,
                   'sample_finished_elapsed_realtime_ns': begin + 20,
                   'native_before': observation(begin + 1, index),
                   'native_after': observation(begin + 12, index),
                   'gps': {'status': 'observed', 'provider': 'gps', 'mock': False,
                           'captured_elapsed_realtime_ns': begin + 10, 'source_elapsed_realtime_ns': begin + 8,
                           'horizontal_accuracy_m': 2, 'control_input_allowed': False, 'speed_mps': 2},
                   'geometry_reference': geometry_name, 'source_pair_same_selection': True, **flags})
  summary = {'session_id': directory.name, 'archive_complete': True, 'frames': 2, 'native_observed_frames': 2,
             'fresh_gps_frames': 2, 'paired_source_frames': 2, 'received_broadcasts': 0, 'dropped_broadcasts': 0,
             'incomplete_broadcasts': 0, 'raw_broadcast_lossless': True, 'probe_closed_cleanly': True, **flags}
  for name, value in [('manifest.json', manifest), ('summary.json', summary)]:
    (directory / name).write_bytes(encode(value))
  (directory / 'frames.jsonl').write_bytes(b''.join(encode(frame) for frame in frames))
  (directory / 'broadcasts.jsonl').write_bytes(b'')
  seal(directory)
  return directory


def mutate(directory, filename, fn, *, lines=False):
  file = directory / filename
  value = [json.loads(line) for line in file.read_text().splitlines()] if lines else json.loads(file.read_text())
  fn(value)
  file.write_bytes(b''.join(encode(v) for v in value) if lines else encode(value))
  seal(directory)


@pytest.fixture
def segmented_capture(capture):
  frames = (capture / 'frames.jsonl').read_bytes().splitlines(keepends=True)
  (capture / 'frames.jsonl').write_bytes(frames[0])
  (capture / 'frames-0002.jsonl').write_bytes(frames[1])
  (capture / 'broadcasts-0002.jsonl').write_bytes(b'')
  mutate(capture, 'manifest.json', lambda value: value.update(
    schema_version=2, segment_interval_ms=180000, max_duration_ms=7200000))
  mutate(capture, 'summary.json', lambda value: value.update(segments=2, finished_elapsed_realtime_ns=80))
  segments = [
    {'index': 1, 'frames_file': 'frames.jsonl', 'broadcasts_file': 'broadcasts.jsonl',
     'opened_elapsed_realtime_ns': 10, 'closed_elapsed_realtime_ns': 45, 'frames': 1, 'broadcasts': 0},
    {'index': 2, 'frames_file': 'frames-0002.jsonl', 'broadcasts_file': 'broadcasts-0002.jsonl',
     'opened_elapsed_realtime_ns': 45, 'closed_elapsed_realtime_ns': 80, 'frames': 1, 'broadcasts': 0},
  ]
  (capture / 'segments.json').write_bytes(encode({
    'schema_version': 1, 'partition': 'writer_rotation', 'interval_ms': 180000, 'segments': segments}))
  seal(capture)
  return capture


def test_segmented_trip_reassembles_continuous_sequences_and_motion(segmented_capture):
  result = verify(segmented_capture)
  assert result['segments'] == 2
  assert result['frames'] == 2
  assert result['within_link_point_transitions'] == 1
  assert result['host_pipeline_verified'] is False


def next_road_archive(directory, *, legacy=False):
  from test_native_route_source import private_navi_info

  def change(frames):
    for frame in frames:
      for side in ('native_before', 'native_after'):
        observation = frame[side]
        if legacy:
          observation.update(status='unavailable', reason='navi_info_next_segment_invalid',
                             sample_consistent=False, path_identity_hash=None)
          continue
        navi = private_navi_info(current_segment=0, current_link=0, current_point=0)
        navi['branches'][1]['next_road_name_segment_raw'] = 0xfffffffe
        navi['reason'] = 'navi_info_next_segment_invalid'
        observation.update(_navi_info=navi, gps_navigation_observed=True)
      if legacy:
        frame.update(geometry_reference=None, source_pair_same_selection=False)
  mutate(directory, 'frames.jsonl', change, lines=True)
  if legacy:
    mutate(directory, 'summary.json', lambda value: value.update(native_observed_frames=0, paired_source_frames=0))


def test_next_road_archive_preserves_raw_branch_and_original_times(capture):
  next_road_archive(capture)
  result = verify(capture)
  report = result['navi_info_field_diagnostics']
  assert report['observations_with_raw'] == 4
  assert report['missing_context_observations'] == 0
  assert report['affected_frames'] == 2
  assert report['source_snapshot_consistency_verified'] is False
  assert len(report['groups']) == 1
  group = report['groups'][0]
  assert group['field_rejections'] == [{'branch_index': 1, 'field': 'next_road_name_segment_raw',
                                      'reason': 'navi_info_next_segment_invalid',
                                      'raw_u32': 4294967294, 'raw_i32': -2}]
  assert (group['first_observation_started_ns'], group['last_observation_finished_ns']) == (21, 64)
  assert group['observations'] == 4
  assert result['stable_index_source_frames'] == 2
  assert 'path_identity_hash' not in json.dumps(report)


def test_old_next_road_rejection_has_explicit_missing_context(capture):
  next_road_archive(capture, legacy=True)
  report = verify(capture)['navi_info_field_diagnostics']
  assert report['observations_with_raw'] == 0
  assert report['missing_context_observations'] == 4
  assert report['affected_frames'] == 2
  assert report['groups'] == []


def test_next_road_archive_bounds_unique_raw_groups_without_losing_counters(capture):
  from copy import deepcopy

  next_road_archive(capture)
  template = json.loads((capture / 'frames.jsonl').read_text().splitlines()[0])
  frames = []
  for index in range(130):
    frame = deepcopy(template)
    frame['sequence'] = index + 1
    for key in ('sample_started_elapsed_realtime_ns', 'sample_finished_elapsed_realtime_ns'):
      frame[key] += index * 30
    for side in ('native_before', 'native_after'):
      for key in ('observation_started_monotonic_ns', 'observation_finished_monotonic_ns'):
        frame[side][key] += index * 30
      frame[side]['_navi_info']['branches'][1]['next_road_name_segment_raw'] = 0x80000000 + index
    for key in ('captured_elapsed_realtime_ns', 'source_elapsed_realtime_ns'):
      frame['gps'][key] += index * 30
    frames.append(frame)
  (capture / 'frames.jsonl').write_bytes(b''.join(encode(frame) for frame in frames))
  mutate(capture, 'summary.json', lambda value: value.update(
    frames=130, native_observed_frames=130, fresh_gps_frames=130, paired_source_frames=130))
  report = verify(capture)['navi_info_field_diagnostics']
  assert report['observations_with_raw'] == 260 and report['affected_frames'] == 130
  assert len(report['groups']) == 128
  assert report['ungrouped_observations'] == 4


@pytest.mark.parametrize('change', [
  lambda navi: navi.update(reason='unverified_navi_info_semantics'),
  lambda navi: navi.update(control_input_allowed=True),
  lambda navi: navi.update(current_segment=-1),
  lambda navi: navi.update(path_identity_hash='c' * 64),
])
def test_next_road_archive_rejects_false_quarantine_even_when_resealed(capture, change):
  next_road_archive(capture)
  mutate(capture, 'frames.jsonl', lambda frames: change(frames[0]['native_before']['_navi_info']), lines=True)
  with pytest.raises(ValueError, match='navi_info_field_diagnosis_invalid'):
    verify(capture)


def test_next_road_archive_rejects_diagnostic_clock_outside_frame(capture):
  next_road_archive(capture)
  mutate(capture, 'frames.jsonl', lambda frames: frames[0]['native_before'].update(
    observation_started_monotonic_ns=1), lines=True)
  with pytest.raises(ValueError, match='native_pair_clock_invalid'):
    verify(capture)


def test_segmented_trip_zip_equals_directory(segmented_capture, tmp_path):
  archive = tmp_path / 'trip.zip'
  with zipfile.ZipFile(archive, 'w') as output:
    for file in segmented_capture.rglob('*'):
      if file.is_file():
        output.write(file, segmented_capture.name + '/' + file.relative_to(segmented_capture).as_posix())
  assert verify(archive) == verify(segmented_capture)


@pytest.mark.parametrize('change', [
  lambda value: value['segments'].reverse(),
  lambda value: value['segments'][1].update(frames_file='frames.jsonl'),
  lambda value: value['segments'][1].update(opened_elapsed_realtime_ns=44),
  lambda value: value['segments'][1].update(closed_elapsed_realtime_ns=81),
  lambda value: value['segments'][1].update(frames=2),
])
def test_segmented_trip_rejects_resealed_invalid_index(segmented_capture, change):
  mutate(segmented_capture, 'segments.json', change)
  with pytest.raises(ValueError, match='segment_'):
    verify(segmented_capture)


def test_segmented_trip_rejects_missing_chunk_even_when_resealed(segmented_capture):
  (segmented_capture / 'frames-0002.jsonl').unlink()
  seal(segmented_capture)
  with pytest.raises(ValueError, match='segment_'):
    verify(segmented_capture)


def test_segmented_trip_rejects_cross_chunk_sequence_reset(segmented_capture):
  mutate(segmented_capture, 'frames-0002.jsonl', lambda values: values[0].update(sequence=1), lines=True)
  with pytest.raises(ValueError, match='frame_sequence_or_clock_invalid'):
    verify(segmented_capture)


def test_segmented_trip_rejects_unindexed_chunk(segmented_capture):
  (segmented_capture / 'frames-0003.jsonl').write_bytes(b'')
  seal(segmented_capture)
  with pytest.raises(ValueError, match='segment_'):
    verify(segmented_capture)


def segment_broadcasts(directory):
  for number, name in enumerate(('broadcasts.jsonl', 'broadcasts-0002.jsonl'), 1):
    (directory / name).write_bytes(encode({
      'sequence': number, 'sender_verified': False, 'received_elapsed_realtime_ns': 5 + number,
      'raw_intent': {'complete': True}}))
  mutate(directory, 'segments.json', lambda value: [part.update(broadcasts=1) for part in value['segments']])
  mutate(directory, 'summary.json', lambda value: value.update(received_broadcasts=2))


def test_segmented_trip_keeps_broadcast_sequence_and_queued_source_clock(segmented_capture):
  segment_broadcasts(segmented_capture)
  result = verify(segmented_capture)
  assert result['archive_integrity'] == 'verified'
  assert result['dropped_broadcasts'] == 0
  assert json.loads((segmented_capture / 'broadcasts-0002.jsonl').read_text())['received_elapsed_realtime_ns'] == 7


def test_segmented_trip_rejects_broadcast_sequence_reset(segmented_capture):
  segment_broadcasts(segmented_capture)
  mutate(segmented_capture, 'broadcasts-0002.jsonl', lambda values: values[0].update(sequence=1), lines=True)
  with pytest.raises(ValueError, match='broadcast_sequence_invalid'):
    verify(segmented_capture)


def test_real_algorithm_reports_dynamic_point_progress_without_gpu_claim(capture):
  result = verify(capture)
  assert result['frames'] == 2
  assert result['within_link_point_transitions'] == 1
  assert result['gps_reported_motion_frames'] == 2
  assert result['source_clock_translated'] is False
  assert result['gpu_verified'] is False
  assert result['host_pipeline_verified'] is False


@pytest.mark.parametrize('position_change', [
  {'point_index': 7}, {'link_index': 2}, {'segment_index': 1}, {'status': 'unavailable'},
  {'point_index': True}, {'point_index': -1}, {'point_index': 1.0},
])
def test_dynamic_progress_requires_stable_before_after_indices(capture, position_change):
  mutate(capture, 'frames.jsonl', lambda rows: rows[1]['native_before']['_position'].update(position_change), lines=True)
  result = verify(capture)
  assert result['paired_source_frames'] == 2
  assert result['same_route_comparable_pairs'] == 0
  assert result['within_link_point_transitions'] == 0
  assert result['stable_index_source_frames'] == 1


def test_dynamic_progress_does_not_compare_different_geometry_revisions(capture):
  old = next((capture / 'geometry').glob('*.bin'))
  raw = bytearray(old.read_bytes())
  struct.pack_into('<i', raw, len(raw) - 8, 36000003)
  name = 'geometry/' + hashlib.sha256(raw).hexdigest() + '.bin'
  (capture / name).write_bytes(raw)
  mutate(capture, 'frames.jsonl', lambda rows: rows[1].update(geometry_reference=name), lines=True)
  result = verify(capture)
  assert result['stable_index_source_frames'] == 2
  assert result['same_route_comparable_pairs'] == 0


def rejection_diagnosis():
  return {'schema_version': 1, 'reason': 'geometry_point_discontinuity', 'diagnostic_only': True,
          'source_snapshot_consistent': False, 'control_input_allowed': False, 'route_render_input_allowed': False,
          'observation_clock_domain': 'phone_elapsed_realtime', 'path_identity_hash': 'a' * 64,
          'process_epoch_hash': 'b' * 64, 'geometry_read_pass': 1, 'source_coordinate_scale': 3600000,
          'previous_segment_index': 0, 'previous_link_index': 3, 'previous_link_local_start': 2,
          'previous_link_point_count': 2, 'next_segment_index': 1, 'next_link_index': 0,
          'next_link_local_start': 0, 'next_link_point_count': 2,
          'observation_started_monotonic_ns': 20, 'observation_finished_monotonic_ns': 21,
          'read_calls': 51, 'read_bytes': 680,
          'previous_link_tail_native': [[36000000, 72000000], [36000001, 72000001]],
          'next_link_head_native': [[36003600, 72000001], [36003601, 72000002]]}


def make_rejections(capture, *, include_context=True):
  def change(rows):
    for frame in rows:
      frame.update(geometry_reference=None, geometry_reason='geometry_point_discontinuity')
      if include_context:
        frame['native_before']['_geometry_rejection'] = rejection_diagnosis()
        frame['native_before']['_geometry_rejection'].update(
          observation_started_monotonic_ns=frame['sample_started_elapsed_realtime_ns'],
          observation_finished_monotonic_ns=frame['sample_started_elapsed_realtime_ns'] + 1)
  mutate(capture, 'frames.jsonl', change, lines=True)
  mutate(capture, 'summary.json', lambda row: row.update(paired_source_frames=0))


def test_rejection_context_reports_persistent_same_join_without_coordinates(capture):
  make_rejections(capture)
  report = verify(capture)['geometry_diagnostics']
  assert report['observations'] == report['bound_observations'] == 2
  assert report['missing_context_frames'] == report['unbound_observations'] == 0
  assert report['runs_total'] == 1
  run = report['runs'][0]
  assert (run['first_sequence'], run['last_sequence'], run['observations']) == (1, 2, 2)
  assert (run['previous_segment_index'], run['next_segment_index']) == (0, 1)
  assert run['sample_window_span_ns'] == 50
  assert 100 < run['endpoint_gap_approx_m'] < 110
  assert report['source_snapshot_consistency_verified'] is False
  serialized = json.dumps(report)
  assert '36000001' not in serialized and '72000001' not in serialized
  assert 'a' * 64 not in serialized and 'b' * 64 not in serialized


def test_legacy_discontinuity_does_not_invent_rejected_coordinates(capture):
  make_rejections(capture, include_context=False)
  report = verify(capture)['geometry_diagnostics']
  assert report['reason_frame_counts'] == {'geometry_point_discontinuity': 2}
  assert report['missing_context_frames'] == 2
  assert report['observations'] == report['runs_total'] == 0
  assert report['runs'] == []


def test_rejection_identity_change_is_reported_unbound_not_associated(capture):
  make_rejections(capture)
  mutate(capture, 'frames.jsonl', lambda rows: rows[1]['native_before']['_geometry_rejection'].update(
    path_identity_hash='c' * 64), lines=True)
  report = verify(capture)['geometry_diagnostics']
  assert report['bound_observations'] == report['unbound_observations'] == 1
  assert report['runs'][1]['frame_identity_matched'] is False
  assert report['runs_total'] == 2


def test_different_rejected_endpoints_are_separate_runs(capture):
  make_rejections(capture)
  def change(rows):
    rows[1]['native_before']['_geometry_rejection']['next_link_head_native'][0][0] += 1
  mutate(capture, 'frames.jsonl', change, lines=True)
  report = verify(capture)['geometry_diagnostics']
  assert report['runs_total'] == 2
  assert report['runs'][0]['fingerprint'] != report['runs'][1]['fingerprint']


@pytest.mark.parametrize('changes', [
  {'schema_version': True}, {'schema_version': 2}, {'diagnostic_only': False},
  {'source_snapshot_consistent': True}, {'control_input_allowed': True}, {'route_render_input_allowed': True},
  {'observation_clock_domain': 'host_monotonic'}, {'source_coordinate_scale': 1},
  {'geometry_read_pass': 3}, {'geometry_read_pass': True}, {'path_identity_hash': 'unknown'},
  {'previous_link_point_count': 1}, {'previous_link_local_start': -1}, {'next_segment_index': 0},
  {'next_link_index': 1}, {'next_link_head_native': [[36003600, 72000001]]},
  {'next_link_head_native': [[True, 72000001], [36003601, 72000002]]},
  {'next_link_head_native': [[36000001, 72000001], [36003601, 72000002]]},
  {'next_link_head_native': [[650000000, 72000001], [36003601, 72000002]]},
  {'read_calls': True}, {'read_calls': 131073}, {'read_bytes': 16777217},
  {'observation_started_monotonic_ns': 22}, {'observation_finished_monotonic_ns': False},
  {'unknown_field': 1},
])
def test_resealed_invalid_geometry_diagnosis_cannot_be_certified(capture, changes):
  make_rejections(capture)
  mutate(capture, 'frames.jsonl', lambda rows: rows[0]['native_before']['_geometry_rejection'].update(changes), lines=True)
  with pytest.raises(ValueError, match='geometry_rejection_diagnosis_invalid'):
    verify(capture)


def test_geometry_context_requires_rejection_reason_and_no_active_geometry(capture):
  mutate(capture, 'frames.jsonl', lambda rows: rows[0]['native_before'].update(
    _geometry_rejection=rejection_diagnosis()), lines=True)
  with pytest.raises(ValueError, match='geometry_rejection_frame_invalid'):
    verify(capture)


def test_segmented_capture_preserves_rejection_run_across_chunks(segmented_capture):
  for name in ('frames.jsonl', 'frames-0002.jsonl'):
    def change(rows):
      for frame in rows:
        frame.update(geometry_reference=None, geometry_reason='geometry_point_discontinuity')
        frame['native_before']['_geometry_rejection'] = rejection_diagnosis()
        frame['native_before']['_geometry_rejection'].update(
          observation_started_monotonic_ns=frame['sample_started_elapsed_realtime_ns'],
          observation_finished_monotonic_ns=frame['sample_started_elapsed_realtime_ns'] + 1)
    mutate(segmented_capture, name, change, lines=True)
  mutate(segmented_capture, 'summary.json', lambda row: row.update(paired_source_frames=0))
  report = verify(segmented_capture)['geometry_diagnostics']
  assert report['runs_total'] == 1 and report['runs'][0]['observations'] == 2


def test_rejection_run_output_is_bounded_but_totals_are_complete():
  import copy
  from phone_capture_archive import MAX_DIAGNOSTIC_RUNS, _GeometryDiagnostics
  diagnostics = _GeometryDiagnostics()
  for index in range(MAX_DIAGNOSTIC_RUNS + 3):
    diagnosis = rejection_diagnosis()
    diagnosis['next_link_head_native'][0][0] += index
    diagnosis.update(observation_started_monotonic_ns=index * 10 + 1,
                     observation_finished_monotonic_ns=index * 10 + 2)
    frame = {'sequence': index + 1, 'sample_started_elapsed_realtime_ns': index * 10 + 1,
             'sample_finished_elapsed_realtime_ns': index * 10 + 9,
             'geometry_reason': 'geometry_point_discontinuity', 'geometry_reference': None,
             'native_before': {'path_identity_hash': 'a' * 64, 'process_epoch_hash': 'b' * 64,
                               '_geometry_rejection': copy.deepcopy(diagnosis)}, 'native_after': {}}
    diagnostics.observe(frame, True)
  report = diagnostics.report()
  assert report['observations'] == report['runs_total'] == MAX_DIAGNOSTIC_RUNS + 3
  assert len(report['runs']) == MAX_DIAGNOSTIC_RUNS and report['runs_dropped'] == 3


def test_missing_context_breaks_repeated_join_run():
  from phone_capture_archive import _GeometryDiagnostics
  diagnostics = _GeometryDiagnostics()
  for index in range(3):
    before = {'path_identity_hash': 'a' * 64, 'process_epoch_hash': 'b' * 64}
    if index != 1:
      before['_geometry_rejection'] = rejection_diagnosis()
      before['_geometry_rejection'].update(observation_started_monotonic_ns=index * 10 + 1,
                                           observation_finished_monotonic_ns=index * 10 + 2)
    diagnostics.observe({'sequence': index + 1, 'sample_started_elapsed_realtime_ns': index * 10 + 1,
                         'sample_finished_elapsed_realtime_ns': index * 10 + 9,
                         'geometry_reason': 'geometry_point_discontinuity', 'geometry_reference': None,
                         'native_before': before, 'native_after': {}}, True)
  report = diagnostics.report()
  assert report['runs_total'] == report['observations'] == 2
  assert report['missing_context_frames'] == 1


def test_geometry_context_on_after_observation_is_rejected(capture):
  make_rejections(capture)
  mutate(capture, 'frames.jsonl', lambda rows: rows[0]['native_after'].update(
    _geometry_rejection=rejection_diagnosis()), lines=True)
  with pytest.raises(ValueError, match='geometry_rejection_frame_invalid'):
    verify(capture)


@pytest.mark.parametrize('start,finish', [(19, 20), (21, 22), (40, 41)])
def test_rejection_context_must_belong_to_current_frame(capture, start, finish):
  make_rejections(capture)
  mutate(capture, 'frames.jsonl', lambda rows: rows[0]['native_before']['_geometry_rejection'].update(
    observation_started_monotonic_ns=start, observation_finished_monotonic_ns=finish), lines=True)
  with pytest.raises(ValueError, match='geometry_rejection_frame_invalid'):
    verify(capture)


def test_same_join_different_read_timing_and_budget_still_groups(capture):
  make_rejections(capture)
  mutate(capture, 'frames.jsonl', lambda rows: rows[1]['native_before']['_geometry_rejection'].update(
    read_calls=52, read_bytes=700, observation_finished_monotonic_ns=50), lines=True)
  report = verify(capture)['geometry_diagnostics']
  assert report['runs_total'] == 1
  assert report['runs'][0]['max_read_calls'] == 52 and report['runs'][0]['max_read_bytes'] == 700


def test_phone_zip_and_directory_have_identical_verification(capture, tmp_path):
  target = tmp_path / 'capture.zip'
  with zipfile.ZipFile(target, 'w') as archive:
    for file in capture.rglob('*'):
      if file.is_file():
        archive.write(file, capture.name + '/' + file.relative_to(capture).as_posix())
  assert verify(target) == verify(capture)


@pytest.mark.parametrize('field', ['host_pipeline_executed', 'gpu_executed', 'control_input_allowed'])
def test_phone_capture_cannot_claim_host_or_vehicle_acceptance(capture, field):
  mutate(capture, 'manifest.json', lambda v: v.update({field: True}))
  with pytest.raises(ValueError, match='unsupported_acceptance_claim'):
    verify(capture)


def test_tampering_raw_samples_fails_hash(capture):
  (capture / 'frames.jsonl').write_bytes(b'{}\n')
  with pytest.raises(ValueError, match='archive_hash_mismatch'):
    verify(capture)


def test_missing_completion_not_reported_as_complete(capture):
  (capture / 'completed.json').unlink()
  with pytest.raises(ValueError, match='archive_incomplete'):
    verify(capture)


@pytest.mark.parametrize('mutation,reason', [
  (lambda v: v[0].update(sequence=2), 'frame_sequence_or_clock_invalid'),
  (lambda v: v[1].update(sample_started_elapsed_realtime_ns=1), 'frame_sequence_or_clock_invalid'),
  (lambda v: v[0]['gps'].update(source_elapsed_realtime_ns=100), 'gps_freshness_claim_invalid'),
  (lambda v: v[0]['gps'].update(mock=True), 'gps_freshness_claim_invalid'),
  (lambda v: v[0]['gps'].update(horizontal_accuracy_m=26), 'gps_freshness_claim_invalid'),
  (lambda v: v[0]['gps'].update(captured_elapsed_realtime_ns=999), 'gps_capture_clock_invalid'),
  (lambda v: v[0]['native_after'].update(path_identity_hash='c' * 64), 'source_pair_claim_invalid'),
  (lambda v: v[0].update(geometry_reference='geometry/missing.bin'), 'geometry_source_binding_invalid'),
  (lambda v: v[0]['native_before'].update(observation_finished_monotonic_ns=99), 'native_pair_clock_invalid'),
])
def test_semantic_failures_rejected_even_with_recomputed_hash(capture, mutation, reason):
  mutate(capture, 'frames.jsonl', mutation, lines=True)
  with pytest.raises(ValueError, match=reason):
    verify(capture)


def test_forged_broadcast_counter_rejected(capture):
  mutate(capture, 'summary.json', lambda v: v.update(received_broadcasts=1))
  with pytest.raises(ValueError, match='broadcast_counter_mismatch'):
    verify(capture)


def test_no_files_outside_manifest(capture):
  (capture / 'unknown.json').write_bytes(b'{}')
  with pytest.raises(ValueError, match='manifest_file_set_invalid'):
    verify(capture)


def test_zero_dynamic_progress_is_reported_as_zero(capture):
  def stationary(frames):
    frames[1]['native_after']['_position']['point_index'] = 0
  mutate(capture, 'frames.jsonl', stationary, lines=True)
  assert verify(capture)['within_link_point_transitions'] == 0


def test_geometry_binary_shape_rejected(capture):
  geometry = next((capture / 'geometry').iterdir()).read_bytes()
  with pytest.raises(ValueError, match='geometry_header_invalid'):
    decode_geometry(geometry[:-1])


def test_private_complete_service_manifest_is_accepted(tmp_path):
  path = tmp_path / 'AndroidManifest.xml'
  path.write_text('<manifest xmlns:android="http://schemas.android.com/apk/res/android" '
                  + 'package="com.jixiexiaoge.drivingassist.navdiag"><application>'
                  + '<service android:name=".PhoneCaptureService" android:exported="false" '
                  + 'android:foregroundServiceType="location"/></application></manifest>')
  check_manifest(path)


def test_phone_capture_blocks_install_and_selftest(monkeypatch):
  from types import SimpleNamespace
  class Fake:
    def __init__(self, *args):
      pass
    def __enter__(self):
      return self
    def __exit__(self, *args):
      pass
    def shell(self, *args):
      return SimpleNamespace(stdout='ServiceRecord{abc u0 ' + device.PACKAGE + '/.PhoneCaptureService}\n')
  monkeypatch.setattr(device, 'Device', Fake)
  monkeypatch.setattr(device, 'choose_adb', lambda _: 'fake-adb')
  for command in ('install', 'selftest', 'phone-selftest', 'phone-pull'):
    monkeypatch.setattr('sys.argv', ['device.py', command])
    with pytest.raises(RuntimeError, match='运行'):
      device.main()


def test_phone_service_is_distinct_from_raw_capture_service():
  from types import SimpleNamespace
  class Fake:
    def shell(self, *args):
      return SimpleNamespace(stdout='ServiceRecord{abc u0 ' + device.PACKAGE + '/.PhoneCaptureService}\n')
  assert device.phone_capture_service_running(Fake())
  assert not device.capture_service_running(Fake())
