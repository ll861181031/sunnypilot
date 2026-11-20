import bz2
import hashlib
import json
import os
import time
from unittest.mock import Mock

import pytest
import zstandard as zstd

from openpilot.cereal import log
from openpilot.system.loggerd import deleter, drive_index, drive_store, driverecordd
from openpilot.system.loggerd.drive_provenance import capture

ROUTE = '00000004--0ac3964c96'
OTHER = '00000005--4c4e99b08b'


def event(kind, t=1_000_000_000, **values):
  m = log.Event.new_message()
  m.logMonoTime, m.valid = t, True
  if kind in ('can', 'sendcan', 'pandaStates'):
    m.init(kind, 0)
  else:
    body = m.init(kind)
    for k, v in values.items():
      setattr(body, k, v)
  return m.to_bytes()


def make_segment(root, number=0, route=ROUTE, start='startOfRoute', end='endOfRoute', model='modelV2', extras=(), qlog=False):
  path = root / f'{route}--{number}'
  path.mkdir(parents=True, exist_ok=True)
  messages = [event('initData', wallTimeNanos=123000000000), event('sentinel', type=start)]
  messages += [event(k, 2_000_000_000) for k in (*drive_index.REQUIRED_STREAMS, model)]
  messages += list(extras)
  if end:
    messages.append(event('sentinel', 3_000_000_000, type=end))
  dest = path / ('qlog.zst' if qlog else 'rlog.zst')
  dest.write_bytes(zstd.compress(b''.join(messages)))
  os.utime(dest, ns=(time.time_ns() - 1_000_000_000_000,) * 2)
  return dest


@pytest.fixture
def root(tmp_path):
  root = tmp_path / 'logs'
  root.mkdir()
  return root


@pytest.fixture
def pressure(root, monkeypatch):
  monkeypatch.setattr(deleter.Paths, 'log_root', lambda: str(root))
  monkeypatch.setattr(deleter.Paths, 'log_root_external', lambda: str(root.parent / 'external'))
  monkeypatch.setattr(deleter, 'get_available_bytes', lambda **_: 0)
  monkeypatch.setattr(deleter, 'get_available_percent', lambda **_: 0)


def test_complete_route_and_driving_model_data_supported(root):
  make_segment(root, end='endOfSegment')
  make_segment(root, 1, start='startOfSegment', model='drivingModelData')
  assert drive_store.scan(root)['indexedSegments'] == 2
  m = drive_store.manifest_for(root, ROUTE)
  assert m['completeLogs'] and m['analysisReady']
  assert not m['hasAnyVideo']
  assert m['provenance']['status'] == 'unknown_historical'


@pytest.mark.parametrize('numbers,ends', [([1], ['endOfRoute']), ([0, 2], ['endOfSegment', 'endOfRoute']), ([0], [None])])
def test_missing_beginning_middle_or_tail_never_complete(root, numbers, ends):
  for n, end in zip(numbers, ends, strict=True):
    make_segment(root, n, start='startOfRoute' if n == 0 else 'startOfSegment', end=end)
  drive_store.scan(root)
  assert not drive_store.manifest_for(root, ROUTE)['completeLogs']


def test_readable_truncated_zstd_is_not_complete(root):
  path = make_segment(root)
  path.write_bytes(path.read_bytes()[:-1])
  r = drive_index.inspect_log(path)
  assert r['integrity'] != 'complete'
  assert any('truncated' in x or 'parse_error' in x for x in r['issues'])


def test_corrupt_capnp_tail_is_not_complete(root):
  path = make_segment(root)
  packed = zstd.decompress(path.read_bytes()) + b'\xff' * 16
  path.write_bytes(zstd.compress(packed))
  assert drive_index.inspect_log(path)['integrity'] != 'complete'


def test_qlog_is_marked_as_partial_data(root):
  make_segment(root, qlog=True)
  drive_store.scan(root)
  m = drive_store.manifest_for(root, ROUTE)
  assert not m['completeLogs'] and not m['analysisReady']
  assert m['segments'][0]['log']['kind'] == 'qlog'


def test_incremental_scan_and_stale_cache(root, monkeypatch):
  path = make_segment(root)
  spy = Mock(wraps=drive_index.inspect_log)
  monkeypatch.setattr(drive_index, 'inspect_log', spy)
  assert drive_store.scan(root)['indexedSegments'] == 1
  assert drive_store.scan(root)['indexedSegments'] == 0
  path.write_bytes(zstd.compress(event('carState')))
  os.utime(path, ns=(time.time_ns() - 1_000_000_000_000,) * 2)
  assert drive_store.scan(root)['indexedSegments'] == 1
  assert spy.call_count == 2
  assert not drive_store.manifest_for(root, ROUTE)['completeLogs']


def test_parser_revision_invalidates_cached_results(root, monkeypatch):
  make_segment(root)
  drive_store.scan(root)
  monkeypatch.setattr(drive_store, 'PARSER_ID', 'new-parser')
  assert drive_store.scan(root)['indexedSegments'] == 1


def test_onroad_transition_cancels_indexing_without_caching_failure(root):
  make_segment(root)
  calls = 0

  def stop():
    nonlocal calls
    calls += 1
    return calls >= 2

  assert drive_store.scan(root, should_stop=stop)['indexedSegments'] == 0
  assert not (drive_store.store_root(root) / 'segments' / f'{ROUTE}--0.json').exists()
  assert drive_store.scan(root)['indexedSegments'] == 1


def test_decompression_stops_before_parsing_when_driving_starts(root, monkeypatch):
  path = make_segment(root)
  stop = Mock(side_effect=[False, True])
  parse = Mock(side_effect=AssertionError('must cancel before parsing'))
  monkeypatch.setattr(drive_index.log.Event, 'read_multiple_bytes', parse)
  with pytest.raises(InterruptedError, match='indexing_paused'):
    drive_index.inspect_log(path, stop)
  parse.assert_not_called()


def test_locked_segment_not_read_or_marked_complete(root, monkeypatch):
  path = make_segment(root)
  (path.parent / 'rlog.lock').touch()
  spy = Mock(side_effect=AssertionError('must not read active log'))
  monkeypatch.setattr(drive_index, 'inspect_log', spy)
  drive_store.scan(root)
  m = drive_store.manifest_for(root, ROUTE)
  assert m['segments'][0]['status'] == 'open' and not m['completeLogs']


def test_valid_empty_navigation_is_not_a_maneuver(root):
  path = make_segment(root, extras=[event('navInstruction', 2_500_000_000)])
  nav = drive_index.inspect_log(path)['navigation']
  assert nav['messages'] == 1 and nav['withManeuvers'] == 0 and nav['types'] == {'empty': 1}


def test_missing_streams_distinguished_from_file_completeness(root):
  path = make_segment(root)
  path.write_bytes(zstd.compress(event('sentinel', type='startOfRoute') + event('sentinel', type='endOfRoute')))
  result = drive_index.inspect_log(path)
  assert result['integrity'] == 'complete' and not result['analysisReady']
  assert 'can' in result['missingRequiredStreams']


def test_all_invalid_required_stream_is_not_analysis_ready(root):
  path = make_segment(root)
  items = []
  for msg in log.Event.read_multiple_bytes(zstd.decompress(path.read_bytes())):
    item = msg.as_builder()
    if item.which() == 'carState':
      item.valid = False
    items.append(item.to_bytes())
  path.write_bytes(zstd.compress(b''.join(items)))
  result = drive_index.inspect_log(path)
  assert result['integrity'] == 'complete'
  assert not result['analysisReady'] and result['unusableRequiredStreams'] == ['carState']


def test_export_copies_and_verifies_logs_without_mutating_source(root, tmp_path):
  source = make_segment(root)
  before = source.read_bytes()
  output = tmp_path / 'export'
  result = drive_store.export_route(root, ROUTE, output)
  digest = json.loads((output / 'checksums.json').read_text())
  for name, expected in digest.items():
    assert hashlib.sha256((output / name).read_bytes()).hexdigest() == expected
  assert source.read_bytes() == before
  replay = json.loads((output / 'replay-manifest.json').read_text())
  assert replay[0]['root'] == str(output) and replay[0]['segments'] == [0]
  manifest = json.loads((output / 'export.json').read_text())
  assert manifest['completeLogs']
  assert manifest['files'] == result['files'] == len(digest)
  assert manifest['logFiles'] == 1


def test_export_qlog_only_route_uses_sampled_replay_mode(root, tmp_path):
  make_segment(root, qlog=True)
  output = tmp_path / 'export'
  drive_store.export_route(root, ROUTE, output)
  replay = json.loads((output / 'replay-manifest.json').read_text())
  assert replay[0]['logType'] == 'qlog' and replay[0]['segments'] == [0]
  assert not json.loads((output / 'export.json').read_text())['completeLogs']


def test_export_bz2_keeps_log_but_marks_replay_format_unsupported(root, tmp_path):
  source = make_segment(root)
  replacement = source.with_suffix('.bz2')
  replacement.write_bytes(bz2.compress(zstd.decompress(source.read_bytes())))
  source.unlink()
  os.utime(replacement, (1, 1))
  output = tmp_path / 'export'
  drive_store.export_route(root, ROUTE, output)
  assert (output / replacement.parent.name / replacement.name).read_bytes() == replacement.read_bytes()
  assert json.loads((output / 'replay-manifest.json').read_text()) == []
  manifest = json.loads((output / 'export.json').read_text())
  assert manifest['replayUnsupportedSegments'] == [{'segment': 0, 'reason': 'replay_reader_requires_zst'}]


def test_export_rejects_recent_unlocked_logs(root, tmp_path):
  source = make_segment(root)
  os.utime(source, None)
  with pytest.raises(ValueError, match='route_missing_or_open'):
    drive_store.export_route(root, ROUTE, tmp_path / 'export')


def test_export_detects_new_file_in_existing_segment(root, tmp_path, monkeypatch):
  source = make_segment(root)
  original = drive_store.sha256_file

  def add_file(path):
    (source.parent / 'qlog.zst').write_bytes(source.read_bytes())
    return original(path)

  monkeypatch.setattr(drive_store, 'sha256_file', add_file)
  with pytest.raises(ValueError, match='route_changed'):
    drive_store.export_route(root, ROUTE, tmp_path / 'export')
  assert source.exists() and not (tmp_path / 'export').exists()


def test_export_never_calls_video_only_data_a_log_export(root, tmp_path):
  source = make_segment(root)
  video = source.with_name('fcamera.hevc')
  source.rename(video)
  with pytest.raises(ValueError, match='no_log_files'):
    drive_store.export_route(root, ROUTE, tmp_path / 'export', include_video=True)


def test_export_refuses_existing_destination_and_path_traversal(root, tmp_path):
  make_segment(root)
  with pytest.raises(ValueError, match='exists'):
    drive_store.export_route(root, ROUTE, tmp_path)
  with pytest.raises(ValueError, match='invalid_route'):
    drive_store.set_pin(root, '../elsewhere', True)
  with pytest.raises(ValueError, match='outside'):
    drive_store.export_route(root, ROUTE, root / 'nested')


def test_export_failure_removes_partial_output_but_keeps_source(root, tmp_path, monkeypatch):
  source = make_segment(root)
  monkeypatch.setattr(drive_store, 'sha256_file', lambda p: str(p))
  with pytest.raises(ValueError, match='source_changed'):
    drive_store.export_route(root, ROUTE, tmp_path / 'export')
  assert source.exists() and not (tmp_path / 'export').exists()
  assert not list(tmp_path.glob('.export-*'))


def test_pin_prevents_cleanup_of_entire_route(root, pressure):
  first = make_segment(root, end='endOfSegment')
  second = make_segment(root, 1, start='startOfSegment')
  drive_store.set_pin(root, ROUTE, True, '问题弯道')
  assert deleter.deleter_step() == (True, None)
  assert first.exists() and second.exists()
  drive_store.set_pin(root, ROUTE, False)
  assert deleter.deleter_step()[1] == str(first.parent)
  assert not first.exists() and not second.exists()


def test_locked_tail_protects_closed_head_of_same_route(root, pressure):
  first = make_segment(root, end='endOfSegment')
  second = make_segment(root, 1, start='startOfSegment')
  (second.parent / 'rlog.lock').touch()
  assert deleter.deleter_step() == (True, None)
  assert first.exists()


def test_retention_deletes_whole_old_route_not_one_segment(root, pressure):
  first = make_segment(root, end='endOfSegment')
  second = make_segment(root, 1, start='startOfSegment')
  newer = make_segment(root, route=OTHER)
  deleter.deleter_step()
  assert not first.exists() and not second.exists() and newer.exists()


def test_recently_closed_route_and_unknown_directories_are_not_deleted(root, pressure):
  source = make_segment(root)
  os.utime(source, None)
  unrelated = root / 'not-a-route'
  unrelated.mkdir()
  assert deleter.deleter_step() == (True, None)
  assert source.exists() and unrelated.exists()


def test_corrupt_pin_fails_closed(root, pressure):
  source = make_segment(root)
  pin = drive_store.pin_path(root, ROUTE)
  pin.parent.mkdir(parents=True)
  pin.write_text('broken')
  assert deleter.deleter_step() == (True, None)
  assert source.exists()


def test_external_archive_verifies_all_segments(root, tmp_path):
  sources = [make_segment(root), make_segment(root, 1)]
  external = tmp_path / 'external'
  external.mkdir()
  expected = {str(p.relative_to(root)): p.read_bytes() for p in sources}
  deleter.archive_group(root, external, [p.parent for p in sources])
  assert all((external / n).read_bytes() == data for n, data in expected.items())
  assert all(not p.exists() for p in sources)


def test_external_archive_keeps_metadata_on_the_mounted_disk(root, tmp_path, monkeypatch):
  source = make_segment(root)
  drive_store.scan(root)
  external = tmp_path / 'external'
  external.mkdir()
  monkeypatch.setattr(type(external), 'is_mount', lambda p: p == external)
  assert drive_store.store_root(external) == external / '.records'
  deleter.archive_group(root, external, [source.parent])
  assert (external / '.records' / 'routes' / ROUTE / 'manifest.json').exists()
  assert not (tmp_path / 'external.records').exists()


def test_failed_archive_never_removes_original(root, tmp_path, monkeypatch):
  source = make_segment(root)
  external = tmp_path / 'external'
  external.mkdir()
  monkeypatch.setattr(deleter.shutil, 'copytree', Mock(side_effect=OSError('disk full')))
  with pytest.raises(OSError, match='disk full'):
    deleter.archive_group(root, external, [source.parent])
  assert source.exists() and not list(external.iterdir())


def test_live_deleter_archive_failure_has_no_delete_fallback(root, pressure, monkeypatch):
  source = make_segment(root)
  external = root.parent / 'external'
  external.mkdir()
  monkeypatch.setattr(type(external), 'is_mount', lambda p: p == external)
  monkeypatch.setattr(deleter, 'get_available_bytes', lambda **kw: 100 * 1024 ** 3 if kw.get('path_type') == 'external' else 0)
  monkeypatch.setattr(deleter, 'get_available_percent', lambda **kw: 50 if kw.get('path_type') == 'external' else 0)
  monkeypatch.setattr(deleter.shutil, 'copytree', Mock(side_effect=OSError('disk full')))
  assert deleter.deleter_step() == (True, None)
  assert source.exists()


def test_external_locked_or_pinned_route_cannot_be_reclaimed(root, pressure, monkeypatch):
  source = make_segment(root)
  external = root.parent / 'external'
  older = make_segment(external, route=OTHER)
  monkeypatch.setattr(type(external), 'is_mount', lambda p: p == external)
  drive_store.set_pin(external, OTHER, True)
  assert deleter.deleter_step() == (True, None)
  assert source.exists() and older.exists()


def test_existing_archive_not_overwritten(root, tmp_path):
  source = make_segment(root)
  external = tmp_path / 'external'
  previous = external / source.parent.name
  previous.mkdir(parents=True)
  with pytest.raises(OSError, match='already exists'):
    deleter.archive_group(root, external, [source.parent])
  assert source.exists() and previous.exists()


def test_worker_never_indexes_onroad_or_relabels_old_route(root, tmp_path, monkeypatch):
  make_segment(root)
  params = Mock()
  params.get_bool.return_value = False
  params.get.return_value = ROUTE
  index = Mock(side_effect=AssertionError('onroad parsing'))
  capture_mock = Mock()
  monkeypatch.setattr(driverecordd, 'scan', index)
  monkeypatch.setattr(driverecordd, 'capture', capture_mock)
  driverecordd.step(root, params, tmp_path, time.time_ns() + 100_000_000_000)
  capture_mock.assert_not_called()
  params.put.assert_not_called()


def test_worker_captures_current_route_and_does_not_touch_recording_preference(root, tmp_path, monkeypatch):
  make_segment(root)
  params = Mock()
  params.get_bool.return_value = False
  params.get.return_value = ROUTE
  capture_mock, observe = Mock(), Mock()
  monkeypatch.setattr(driverecordd, 'capture', capture_mock)
  monkeypatch.setattr(driverecordd, 'observe_configuration', observe)
  driverecordd.step(root, params, tmp_path, time.time_ns())
  capture_mock.assert_called_once()
  observe.assert_called_once()
  params.put.assert_not_called()
  params.put_bool.assert_not_called()


def test_provenance_freezes_actual_source_without_overwriting_history(root, tmp_path):
  repo = tmp_path / 'repo'
  source = repo / 'openpilot/selfdrive/controls/example.py'
  source.parent.mkdir(parents=True)
  source.write_text('value = 1\n')
  params = Mock()
  params.all_keys.return_value = [b'DisableEncoderd', b'SecretUnrelated']
  params.get.return_value = True
  first = capture(root, ROUTE, params, repo, {'MODELD_BACKEND': 'test'})
  source.write_text('value = 2\n')
  second = capture(root, ROUTE, params, repo)
  assert first == second
  assert first['sourceHashes']['openpilot/selfdrive/controls/example.py'] == hashlib.sha256(b'value = 1\n').hexdigest()
  assert first['parameters'] == {'DisableEncoderd': True}
  params.put.assert_not_called()


def test_worker_is_managed_without_new_boot_service_or_control_publisher():
  from openpilot.system.manager.process_config import managed_processes
  proc = managed_processes['driverecordd']
  assert proc.module == 'openpilot.system.loggerd.driverecordd'
  assert proc.should_run(False, None, None) and proc.should_run(True, None, None)
