"""Opt-in private archives of the exact samples seen by the isolated navigation bench."""
from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Mapping
from dataclasses import asdict, fields
import hashlib
import json
from pathlib import Path
import re
import struct
from types import SimpleNamespace

from APP.navdiag.android_gps_source import AndroidGpsBridgeFix, AndroidGpsFix
from APP.navdiag.live_route_snapshot import NativeRouteGeometry, SelectedCacheRoute, SelectedNativeRoute
from APP.navdiag.native_route_source import GEOMETRY_IDENTITY_KIND, NativeRouteProbeSource, _GEOMETRY_MAGIC, geometry_identity_sha256
from APP.navdiag.phone_route_producer import PhoneRouteSample, _freeze
from APP.navdiag.root_probe import write_private
from tools.cplink_shim.amap_route_cache import SUPPORTED_PROFILE, decode_cache

MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_BLOB_BYTES = 16 * 1024 * 1024
MAX_SAMPLE_BYTES = 128 * 1024
MAX_SAMPLES = 1024
MAX_CALLS = 1024
MAX_CALL_BYTES = 8192
_SHA = re.compile(r'[0-9a-f]{64}')
ROOT = Path(__file__).resolve().parents[2]
REPLAY_SOURCE_FILES = (
  'APP/navdiag/sample_evidence.py', 'APP/navdiag/phone_pipeline_bench.py',
  'APP/navdiag/phone_route_producer.py', 'APP/navdiag/live_route_snapshot.py',
  'APP/navdiag/android_gps_source.py', 'APP/navdiag/native_route_source.py',
  'openpilot/selfdrive/navigation/amap_phone_route.py', 'openpilot/selfdrive/navigation/amap_route_anchor.py',
  'openpilot/selfdrive/navigation/phone_route.py', 'openpilot/selfdrive/navigation/nav_map_contract.py',
  'openpilot/selfdrive/navigation/nav_phone_quality.py', 'openpilot/common/gcj.py',
  'tools/cplink_shim/amap_route_cache.py',
)


class SampleEvidenceRejected(ValueError):
  pass


def _require(condition, reason):
  if not condition:
    raise SampleEvidenceRejected(reason)


def _plain(value, depth=0):
  _require(depth <= 20, 'sample_evidence_depth_limit')
  if isinstance(value, Mapping):
    _require(len(value) <= 256 and all(type(k) is str for k in value), 'sample_evidence_mapping_invalid')
    return {key: _plain(item, depth + 1) for key, item in value.items()}
  if isinstance(value, (tuple, list)):
    _require(len(value) <= 100000, 'sample_evidence_list_limit')
    return [_plain(item, depth + 1) for item in value]
  _require(value is None or type(value) in (str, int, bool, float), 'sample_evidence_value_invalid')
  return value


def _encode(value):
  try:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode()
  except (ValueError, TypeError, UnicodeError):
    raise SampleEvidenceRejected('sample_evidence_json_invalid') from None


def _native_payload(selected, epoch):
  route = selected.route
  header = (1, route.navi_type_raw, route.navi_state_raw, route.path_count, route.main_index,
            route.segment_count, route.link_count, len(route.points_native))
  try:
    payload = (_GEOMETRY_MAGIC + struct.pack('<8I', *header) + bytes.fromhex(selected.identity_hash)
               + hashlib.sha256(epoch.encode()).digest()
               + b''.join(struct.pack('<3I', segment, start, count)
                          for segment, links in enumerate(route.segments) for start, count in links)
               + b''.join(struct.pack('<2i', *point) for point in route.points_native))
  except (ValueError, TypeError, struct.error, OverflowError):
    raise SampleEvidenceRejected('sample_evidence_native_geometry_invalid') from None
  _require(len(payload) <= MAX_BLOB_BYTES, 'sample_evidence_blob_limit')
  parsed = NativeRouteProbeSource._parse_geometry(SimpleNamespace(epoch=epoch), payload)
  digest = hashlib.sha256(payload).hexdigest()
  if selected.geometry_identity_kind == GEOMETRY_IDENTITY_KIND:
    _require(selected.source_payload_sha256 == digest
             and selected.source_geometry_sha256 == geometry_identity_sha256(payload), 'sample_evidence_geometry_hash_mismatch')
  else:
    _require(selected.geometry_identity_kind == 'native_payload_sha256_legacy'
             and selected.source_geometry_sha256 == digest, 'sample_evidence_geometry_hash_mismatch')
  _require(parsed['path_identity_hash'] == selected.identity_hash, 'sample_evidence_path_mismatch')
  return payload


def _selected_parts(sample):
  selected = sample.selected
  _require(isinstance(selected, (SelectedNativeRoute, SelectedCacheRoute)), 'sample_evidence_selected_type_invalid')
  values = asdict(selected)
  del values['route']
  if isinstance(selected, SelectedNativeRoute):
    payload = _native_payload(selected, sample.epoch)
    return {'kind': 'native', 'fields': values,
            'payload_origin': 'reconstructed_from_selected_and_hash_verified'}, payload
  _require(isinstance(sample.cache_bytes, bytes) and 0 < len(sample.cache_bytes) <= MAX_BLOB_BYTES,
           'sample_evidence_cache_invalid')
  _require(hashlib.sha256(sample.cache_bytes).hexdigest() == selected.cache_sha256, 'sample_evidence_geometry_hash_mismatch')
  decoded = decode_cache(sample.cache_bytes, profile=SUPPORTED_PROFILE)
  _require(type(selected.cache_route_index) is int and 0 <= selected.cache_route_index < len(decoded.routes),
           'sample_evidence_cache_index_invalid')
  _require(decoded.routes[selected.cache_route_index] == selected.route, 'sample_evidence_cache_route_mismatch')
  return {'kind': 'cache', 'fields': values, 'payload_origin': 'original_cache_bytes'}, sample.cache_bytes


def _rebuild_selected(value, payload, epoch):
  metadata = dict(value['fields'])
  if value['kind'] == 'native':
    _require(value['payload_origin'] == 'reconstructed_from_selected_and_hash_verified', 'sample_evidence_origin_invalid')
    raw = NativeRouteProbeSource._parse_geometry(SimpleNamespace(epoch=epoch), payload)
    route = NativeRouteGeometry(raw['points_native'], raw['segments'], raw['segment_count'], raw['link_count'],
                                raw['main_index'], raw['path_count'], raw['navi_type_raw'], raw['navi_state_raw'])
    selected = SelectedNativeRoute(route=route, **metadata)
    _native_payload(selected, epoch)
    return selected
  _require(value['kind'] == 'cache', 'sample_evidence_kind_invalid')
  _require(value['payload_origin'] == 'original_cache_bytes', 'sample_evidence_origin_invalid')
  decoded = decode_cache(payload, profile=SUPPORTED_PROFILE)
  index = metadata['cache_route_index']
  _require(type(index) is int and 0 <= index < len(decoded.routes), 'sample_evidence_cache_index_invalid')
  _require(decoded.sha256 == metadata['cache_sha256'], 'sample_evidence_geometry_hash_mismatch')
  return SelectedCacheRoute(route=decoded.routes[index], **metadata)


def _call_context(value):
  _require(isinstance(value, dict) and set(value) == {'schema_version', 'now_ns', 'sample_present', 'source_status'}
           and type(value['schema_version']) is int and value['schema_version'] == 1
           and type(value['now_ns']) is int and 0 < value['now_ns'] < (1 << 63)
           and type(value['sample_present']) is bool, 'sample_evidence_call_context_invalid')
  status = value['source_status']
  if status is not None:
    _require(isinstance(status, dict) and set(status) == {'running', 'connected', 'reason', 'transport_session', 'sequence'}
             and all(type(status[key]) is bool for key in ('running', 'connected'))
             and type(status['sequence']) is int and 0 <= status['sequence'] < (1 << 63)
             and type(status['reason']) is str and re.fullmatch('[a-z][a-z0-9_]{0,79}', status['reason'])
             and (status['transport_session'] is None or type(status['transport_session']) is str
                  and len(status['transport_session']) <= 256), 'sample_evidence_call_context_invalid')
  return _plain(value)


class SampleEvidenceArchive:
  """Store original parsed observations and geometry; never refresh clocks or authorize inputs."""

  def __init__(self, output):
    self.output = Path(output) / 'sample-evidence'
    self.output.mkdir(mode=0o700, exist_ok=False)
    self.bytes = 0
    self._seen = {}
    self._geometry = {}
    self._blobs = set()
    self._calls = set()

  def _store(self, category, payload, limit):
    _require(len(payload) <= limit, 'sample_evidence_file_limit')
    digest = hashlib.sha256(payload).hexdigest()
    relative = f'{category}-{digest}.bin'
    if relative not in self._blobs:
      _require(self.bytes + len(payload) <= MAX_TOTAL_BYTES, 'sample_evidence_total_limit')
      write_private(self.output / relative, payload)
      self._blobs.add(relative)
      self.bytes += len(payload)
    return {'file': relative, 'sha256': digest, 'bytes': len(payload)}

  def record(self, sample):
    if sample is None:
      return None
    _require(isinstance(sample, PhoneRouteSample), 'sample_evidence_sample_type_invalid')
    _require(type(sample.sequence) is int and sample.sequence > 0 and type(sample.host_start_ns) is int
             and sample.host_start_ns > 0, 'sample_evidence_sequence_invalid')
    _require(isinstance(sample.gps, AndroidGpsFix), 'sample_evidence_gps_type_invalid')
    _require(sample.source_object == sample.selected.source_path, 'sample_evidence_source_object_mismatch')
    _require(type(sample.epoch) is str and 0 < len(sample.epoch) <= 128
             and type(sample.transport_session) is str and 0 < len(sample.transport_session) <= 256,
             'sample_evidence_identity_invalid')
    key = (sample.epoch, sample.transport_session, sample.sequence)
    _require(key in self._seen or len(self._seen) < MAX_SAMPLES, 'sample_evidence_sample_limit')
    selected_key = (id(sample.selected), sample.epoch)
    cached = self._geometry.get(selected_key)
    if cached is not None and cached[0] is sample.selected:
      selected_ref = cached[1]
    else:
      metadata, payload = _selected_parts(sample)
      metadata['payload'] = self._store('geometry', payload, MAX_BLOB_BYTES)
      selected_ref = self._store('selected', _encode(metadata), MAX_SAMPLE_BYTES)
      self._geometry[selected_key] = (sample.selected, selected_ref)
    record = {'schema_version': 1, 'evidence_kind': 'parsed_phone_route_sample', 'selected': selected_ref,
              'source_object': sample.source_object, 'epoch': sample.epoch, 'sequence': sample.sequence,
              'host_start_ns': sample.host_start_ns, 'transport_session': sample.transport_session,
              'gps_type': 'bridge' if isinstance(sample.gps, AndroidGpsBridgeFix) else 'root',
              'gps': asdict(sample.gps), 'native_before': _plain(sample.native_before),
              'native_after': _plain(sample.native_after), 'broadcast': _plain(sample.broadcast),
              'anchor_pair_retry': _plain(sample.anchor_pair_retry),
              'raw_target_memory_bytes_included': False, 'control_input_allowed': False}
    encoded = _encode(record)
    digest = hashlib.sha256(encoded).hexdigest()
    _require(key not in self._seen or self._seen[key]['sha256'] == digest, 'sample_evidence_sequence_content_conflict')
    reference = self._store('sample', encoded, MAX_SAMPLE_BYTES)
    self._seen[key] = reference
    return dict(reference)

  def record_call(self, sample_reference, context):
    context = _call_context(context)
    _require(context['sample_present'] is (sample_reference is not None), 'sample_evidence_call_sample_mismatch')
    if sample_reference is not None:
      _load(self.output, sample_reference, 'sample', MAX_SAMPLE_BYTES)
    payload = _encode({'schema_version': 1, 'context': context, 'sample_reference': sample_reference,
                       'control_input_allowed': False})
    digest = hashlib.sha256(payload).hexdigest()
    _require(digest in self._calls or len(self._calls) < MAX_CALLS, 'sample_evidence_call_limit')
    reference = self._store('call', payload, MAX_CALL_BYTES)
    self._calls.add(digest)
    return reference

  def summary(self):
    result = {'enabled': True, 'unique_samples': len(self._seen), 'files': len(self._blobs),
              'bytes': self.bytes, 'max_bytes': MAX_TOTAL_BYTES, 'raw_target_memory_bytes_included': False}
    if self._calls:
      result.update(adapter_call_schema_version=1, unique_adapter_calls=len(self._calls))
    return result


def _load(directory, reference, category, limit):
  digest = reference.get('sha256')
  _require(type(digest) is str and _SHA.fullmatch(digest), 'sample_evidence_reference_invalid')
  _require(reference.get('file') == f'{category}-{digest}.bin', 'sample_evidence_reference_invalid')
  _require(type(reference.get('bytes')) is int and 0 < reference['bytes'] <= limit, 'sample_evidence_reference_invalid')
  path = directory / reference['file']
  _require(not path.is_symlink() and path.stat().st_size <= limit, 'sample_evidence_file_invalid')
  payload = path.read_bytes()
  _require(len(payload) == reference.get('bytes') and hashlib.sha256(payload).hexdigest() == digest,
           'sample_evidence_file_hash_mismatch')
  return payload


def load_sample(output, reference):
  """Rebuild an offline diagnostic sample with its original timestamps, without publishing it."""
  directory = Path(output) / 'sample-evidence'
  record = json.loads(_load(directory, reference, 'sample', MAX_SAMPLE_BYTES))
  _require(record['schema_version'] == 1 and record['evidence_kind'] == 'parsed_phone_route_sample'
           and record['control_input_allowed'] is False, 'sample_evidence_schema_invalid')
  selected = json.loads(_load(directory, record['selected'], 'selected', MAX_SAMPLE_BYTES))
  payload = _load(directory, selected['payload'], 'geometry', MAX_BLOB_BYTES)
  route = _rebuild_selected(selected, payload, record['epoch'])
  _require(record['source_object'] == route.source_path, 'sample_evidence_source_object_mismatch')
  gps_fields = {field.name for field in fields(AndroidGpsFix)}
  _require(set(record['gps']) == gps_fields, 'sample_evidence_gps_fields_invalid')
  gps = dict(record['gps'])
  gps['coordinates'] = tuple(gps['coordinates'])
  _require(record['gps_type'] in ('root', 'bridge'), 'sample_evidence_gps_type_invalid')
  gps = (AndroidGpsBridgeFix if record['gps_type'] == 'bridge' else AndroidGpsFix)(**gps)
  return PhoneRouteSample(route, b'' if selected['kind'] == 'native' else payload, record['source_object'],
                          record['epoch'], record['sequence'], record['host_start_ns'], gps,
                          _freeze(record['native_before']), _freeze(record['native_after']), record['transport_session'],
                          _freeze(record['broadcast']), _freeze(record['anchor_pair_retry']))


def load_call(output, reference):
  record = json.loads(_load(Path(output) / 'sample-evidence', reference, 'call', MAX_CALL_BYTES))
  _require(set(record) == {'schema_version', 'context', 'sample_reference', 'control_input_allowed'}
           and type(record['schema_version']) is int and record['schema_version'] == 1
           and record['control_input_allowed'] is False, 'sample_evidence_call_schema_invalid')
  context = _call_context(record['context'])
  _require(context['sample_present'] is (record['sample_reference'] is not None), 'sample_evidence_call_sample_mismatch')
  return record


def verify_run(output):
  output = Path(output)
  report = json.loads((output / 'report.json').read_text())
  _require(report.get('sample_evidence', {}).get('enabled') is True, 'sample_evidence_not_enabled')
  samples = set()
  calls = set()
  has_calls = report.get('sample_evidence', {}).get('adapter_call_schema_version') == 1
  referenced = absent = 0
  for line in (output / 'frames.jsonl').read_text().splitlines():
    frame = json.loads(line)
    reference = frame.get('private_sample_evidence')
    if has_calls:
      call_reference = frame.get('private_adapter_call_evidence')
      _require(isinstance(call_reference, dict), 'sample_evidence_call_reference_missing')
      call = load_call(output, call_reference)
      _require(call['sample_reference'] == reference and call['context']['sample_present'] is frame.get('has_sample'),
               'sample_evidence_call_sample_mismatch')
      now_ns = call['context']['now_ns']
      decision = frame.get('source_decision', {})
      _require(now_ns / 1e9 == decision.get('accept_called_mono_s'), 'sample_evidence_call_clock_mismatch')
      calls.add(call_reference['sha256'])
    if not frame.get('has_sample'):
      _require(reference is None, 'sample_evidence_absent_frame_has_reference')
      absent += 1
      continue
    _require(isinstance(reference, dict), 'sample_evidence_frame_reference_missing')
    sample = load_sample(output, reference)
    if has_calls and type(decision.get('sample_host_age_ns')) is int:
      _require(now_ns - sample.host_start_ns == decision['sample_host_age_ns'], 'sample_evidence_call_clock_mismatch')
    _require(type(frame['sequence']) is int and sample.sequence == frame['sequence']
             and hashlib.sha256(sample.transport_session.encode()).hexdigest() == frame['transport_session_hash'],
             'sample_evidence_frame_identity_mismatch')
    source = frame.get('route_identity_diagnostics', {}).get('selected', {})
    expected = {'path_identity_hash': sample.selected.identity_hash, 'cache_sha256': sample.selected.cache_sha256,
                'source_session_id': sample.selected.source_session_id,
                'source_payload_sha256': getattr(sample.selected, 'source_payload_sha256', None) or 'unknown',
                'source_object_hash': hashlib.sha256(sample.selected.source_path.encode()).hexdigest(),
                'transport_session_hash': hashlib.sha256(sample.transport_session.encode()).hexdigest(),
                'observed_sequence': sample.sequence, 'sample_host_start_ns': sample.host_start_ns}
    _require(all(source.get(key) == value for key, value in expected.items())
             and frame.get('gps_source_token') == sample.gps.source_token,
             'sample_evidence_frame_identity_mismatch')
    for side in ('native_before', 'native_after'):
      actual = getattr(sample, side).get('_position', {})
      logged = frame.get('sample_position_diagnostics', {}).get('source_anchors', {}).get(side, {})
      _require(all(actual.get(key) == logged.get(key) for key in ('segment_index', 'link_index', 'point_index')),
               'sample_evidence_frame_anchor_mismatch')
    samples.add(reference['sha256'])
    referenced += 1
  _require(len(samples) == report['sample_evidence']['unique_samples'] and referenced + absent == report['frames'],
           'sample_evidence_summary_mismatch')
  if has_calls:
    _require(type(report['sample_evidence'].get('unique_adapter_calls')) is int
             and len(calls) == report['sample_evidence']['unique_adapter_calls'], 'sample_evidence_call_summary_mismatch')
  result = {'archive_verified': True, 'frames_with_samples': referenced, 'frames_without_samples': absent,
            'unique_samples': len(samples),
            'capture_finished_without_error': (report.get('error') is None and report.get('interrupted') is None
                                               and report.get('cleanup_verified') is True),
            'actual_dynamic_transition_verified': False,
            'gpu_verified': False, 'target_complete': False}
  if has_calls:
    result.update(adapter_call_archive_verified=True, unique_adapter_calls=len(calls))
  return result


def replay_source_sha256():
  return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in REPLAY_SOURCE_FILES}


def publication_binding(publication, now_ns):
  from openpilot.selfdrive.navigation.nav_map_contract import binding_from_snapshot
  binding = binding_from_snapshot(publication.snapshot, publication.active, now_ns=now_ns).to_dict()
  result = {key: binding[key] for key in (
    'routeId', 'cacheSha256', 'sourceSessionId', 'bindingId', 'sourceGeometrySha256',
    'geometrySha256', 'provider', 'revision', 'observedSequence', 'observedMonoTime')}
  result.update(transportSessionHash=hashlib.sha256(binding['transportSessionId'].encode()).hexdigest(),
                sourceObjectHash=hashlib.sha256(binding['sourceObjectId'].encode()).hexdigest())
  return result


def replay_run(output):
  from openpilot.selfdrive.navigation.amap_phone_route import AmapPhoneRouteAdapter
  output = Path(output)
  archive = verify_run(output)
  _require(archive.get('adapter_call_archive_verified') is True, 'sample_evidence_call_context_not_archived')
  report = json.loads((output / 'report.json').read_text())
  sources = report.get('source_sha256', {})
  _require(all(sources.get(name) == digest for name, digest in replay_source_sha256().items()),
           'sample_evidence_replay_source_changed')
  adapter = AmapPhoneRouteAdapter()
  counts, reasons = Counter(), Counter()
  previous_ns = 0
  for index, line in enumerate((output / 'frames.jsonl').read_text().splitlines()):
    frame = json.loads(line)
    _require(type(frame.get('index')) is int and frame['index'] == index, 'sample_evidence_replay_frame_gap')
    call = load_call(output, frame['private_adapter_call_evidence'])
    context = call['context']
    now_ns = context['now_ns']
    _require(now_ns > previous_ns, 'sample_evidence_replay_clock_not_advancing')
    previous_ns = now_ns
    sample = load_sample(output, call['sample_reference']) if context['sample_present'] else None
    status = SimpleNamespace(**context['source_status']) if context['source_status'] is not None else None
    publication = adapter.accept(sample, now_ns=now_ns, source_status=status)
    _require(adapter.decision_evidence() == frame.get('source_decision'), 'sample_evidence_replay_decision_mismatch')
    _require(adapter.reason == frame.get('source_reason') and adapter.instruction_reason == frame.get('adapter_instruction_reason'),
             'sample_evidence_replay_reason_mismatch')
    _require(publication is not None or frame.get('map_valid') is False, 'sample_evidence_replay_publication_mismatch')
    if frame.get('map_valid') is True:
      expected = frame.get('route_identity_diagnostics', {}).get('consumer_binding')
      _require(expected == publication_binding(publication, now_ns), 'sample_evidence_replay_consumer_binding_mismatch')
      counts['valid_map_consumer_bindings_checked'] += 1
    counts['adapter_calls_replayed'] += 1
    counts['publications_replayed'] += int(publication is not None)
    counts['missing_sample_calls_replayed'] += int(sample is None)
    counts['held_publications_replayed'] += int(adapter.decision_evidence()['publication_reused'] is True)
    reasons[adapter.reason] += 1
  _require(counts['adapter_calls_replayed'] > 0, 'sample_evidence_replay_empty')
  return {'all_adapter_calls_replayed': True, **dict(counts), 'reason_counts': dict(reasons),
          'archive': archive, 'consumer_bindings_equal': (True if counts['valid_map_consumer_bindings_checked'] else 'not_observed'),
          'clock_translated': False,
          'source_status_synthesized': False, 'phone_connected': False, 'messages_published': False,
          'replay_scope': 'adapter calls and recorded valid-map bindings; mapsd/NavRuntime/GPU are not replayed',
          'actual_dynamic_transition_verified': False, 'gpu_verified': False, 'target_complete': False}


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  mode = parser.add_mutually_exclusive_group(required=True)
  mode.add_argument('--verify', type=Path)
  mode.add_argument('--replay', type=Path, help='只读重放含实际调用上下文的新归档')
  args = parser.parse_args()
  result = replay_run(args.replay) if args.replay else verify_run(args.verify)
  print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
  main()
