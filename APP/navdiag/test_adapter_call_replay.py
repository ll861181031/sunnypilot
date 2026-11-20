from dataclasses import replace
import hashlib
import json
import time
from types import SimpleNamespace

import pytest

from APP.navdiag import sample_evidence as evidence
from APP.navdiag import live_pipeline_probe as probe
from APP.navdiag import phone_pipeline_bench as bench_module
from APP.navdiag.test_sample_evidence import sample_fixture
from openpilot.selfdrive.navigation.amap_phone_route import AmapPhoneRouteAdapter
from openpilot.selfdrive.navigation.tests.test_amap_phone_pipeline import NOW_NS

ObservedPhoneRouteAdapter = getattr(bench_module, 'ObservedPhoneRouteAdapter', AmapPhoneRouteAdapter)


def status(sample, **changes):
  return SimpleNamespace(running=True, connected=True, reason='phone_selection_refreshing',
                         transport_session=sample.transport_session, sequence=sample.sequence, **changes)


def timeline():
  first = sample_fixture()
  shift = 3_000_000_000
  next_sample = shift_sample(first, shift, sequence=2)
  return [(None, NOW_NS - 100_000_000, None), (first, NOW_NS, None),
          (None, NOW_NS + 100_000_000, status(first)),
          (None, NOW_NS + 2_100_000_000, status(first)),
          (None, NOW_NS + 2_200_000_000, status(first)), (next_sample, NOW_NS + shift, None)]


def shift_sample(sample, shift, *, sequence=None):
  before, after = (evidence._plain(getattr(sample, side)) for side in ('native_before', 'native_after'))
  for value in (before, after):
    for key in ('host_observation_started_monotonic_ns', 'host_observation_finished_monotonic_ns'):
      value[key] += shift
  return replace(sample, sequence=sample.sequence if sequence is None else sequence,
                 host_start_ns=sample.host_start_ns + shift,
                 gps=replace(sample.gps, host_started_ns=sample.gps.host_started_ns + shift,
                             host_finished_ns=sample.gps.host_finished_ns + shift,
                             elapsed_realtime_ns=sample.gps.elapsed_realtime_ns + shift,
                             source_token=hashlib.sha256(str(shift).encode()).hexdigest()),
                 native_before=evidence._freeze(before), native_after=evidence._freeze(after))


def write_run(directory, calls=None):
  directory.mkdir()
  archive = evidence.SampleEvidenceArchive(directory)
  adapter = ObservedPhoneRouteAdapter()
  frames = []
  for index, (sample, now, source_status) in enumerate(timeline() if calls is None else calls):
    publication = adapter.accept(sample, now_ns=now, source_status=source_status)
    reference = archive.record(sample)
    context = adapter.call_context()
    frame = {'index': index, 'has_sample': sample is not None, 'sequence': getattr(sample, 'sequence', None),
             'source_reason': adapter.reason, 'source_decision': adapter.decision_evidence(),
             'adapter_instruction_reason': adapter.instruction_reason,
             'map_valid': publication is not None, 'private_sample_evidence': reference,
             'private_adapter_call_evidence': archive.record_call(reference, context)}
    if sample is not None:
      selected = sample.selected
      frame.update(transport_session_hash=hashlib.sha256(sample.transport_session.encode()).hexdigest(),
                   gps_source_token=sample.gps.source_token,
                   sample_position_diagnostics={'source_anchors': {
                     side: {key: getattr(sample, side)['_position'][key]
                            for key in ('segment_index', 'link_index', 'point_index')}
                     for side in ('native_before', 'native_after')}},
                   route_identity_diagnostics={'selected': {
                     'path_identity_hash': selected.identity_hash, 'cache_sha256': selected.cache_sha256,
                     'source_session_id': selected.source_session_id,
                     'source_payload_sha256': selected.source_payload_sha256 or 'unknown',
                     'source_object_hash': hashlib.sha256(selected.source_path.encode()).hexdigest(),
                     'transport_session_hash': hashlib.sha256(sample.transport_session.encode()).hexdigest(),
                     'observed_sequence': sample.sequence, 'sample_host_start_ns': sample.host_start_ns}})
    if publication is not None:
      frame.setdefault('route_identity_diagnostics', {})['consumer_binding'] = evidence.publication_binding(publication, now)
    frames.append(frame)
  report = {'sample_evidence': archive.summary(), 'frames': len(frames), 'cleanup_verified': True,
            'error': None, 'interrupted': None,
            'source_sha256': evidence.replay_source_sha256()}
  (directory / 'report.json').write_text(json.dumps(report))
  save_frames(directory, frames)
  return frames


def save_frames(directory, frames):
  (directory / 'frames.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in frames))


def test_observer_preserves_every_original_decision_and_does_not_copy_unrelated_state():
  original, observed = AmapPhoneRouteAdapter(), ObservedPhoneRouteAdapter()
  for sample, now, source_status in timeline():
    if source_status is not None:
      source_status.private_unrelated = object()
    first = original.accept(sample, now_ns=now, source_status=source_status)
    second = observed.accept(sample, now_ns=now, source_status=source_status)
    assert first == second
    assert original.decision_evidence() == observed.decision_evidence()
    assert original.instruction_reason == observed.instruction_reason
    call = observed.call_context()
    assert call['now_ns'] == now and call['sample_present'] is (sample is not None)
    if source_status is not None:
      assert 'private_unrelated' not in call['source_status']
      source_status.reason = 'changed_after_call'
      call['source_status']['sequence'] = -1
      assert observed.call_context()['source_status']['reason'] != source_status.reason
      assert observed.call_context()['source_status']['sequence'] >= 1


def test_full_replay_includes_initial_missing_hold_expiry_barriers_and_recovery(tmp_path):
  directory = tmp_path / 'run'
  frames = write_run(directory)
  assert [row['source_reason'] for row in frames] == [
    'phone_source_unavailable', 'accepted_strict', 'accepted_strict',
    'phone_transient_refresh_expired', 'phone_transient_refresh_expired', 'accepted_strict']
  actual = evidence.replay_run(directory)
  assert actual['all_adapter_calls_replayed'] is True
  assert actual['adapter_calls_replayed'] == len(frames)
  assert actual['missing_sample_calls_replayed'] == 4
  assert actual['held_publications_replayed'] == 1
  assert actual['publications_replayed'] == 3
  assert actual['clock_translated'] is False and actual['source_status_synthesized'] is False
  assert actual['consumer_bindings_equal'] is True
  assert actual['actual_dynamic_transition_verified'] is False
  assert actual['gpu_verified'] is False and actual['target_complete'] is False


@pytest.mark.parametrize('value', [True, 1.0, -1, 0, None])
def test_call_requires_exact_positive_integer_clock(tmp_path, value):
  archive = evidence.SampleEvidenceArchive(tmp_path)
  observer = ObservedPhoneRouteAdapter()
  observer.accept(None, now_ns=NOW_NS)
  call = observer.call_context() | {'now_ns': value}
  with pytest.raises(evidence.SampleEvidenceRejected, match='call_context_invalid'):
    archive.record_call(None, call)


def test_call_sample_presence_and_reference_are_linked(tmp_path):
  archive = evidence.SampleEvidenceArchive(tmp_path)
  observer = ObservedPhoneRouteAdapter()
  observer.accept(sample_fixture(), now_ns=NOW_NS)
  with pytest.raises(evidence.SampleEvidenceRejected, match='call_sample_mismatch'):
    archive.record_call(None, observer.call_context())


def test_calls_are_private_bounded_deduplicated_and_do_not_refresh_samples(tmp_path, monkeypatch):
  archive = evidence.SampleEvidenceArchive(tmp_path)
  observer = ObservedPhoneRouteAdapter()
  observer.accept(None, now_ns=NOW_NS)
  monkeypatch.setattr(evidence, 'MAX_CALLS', 1)
  reference = archive.record_call(None, observer.call_context())
  assert archive.record_call(None, observer.call_context()) == reference
  assert evidence.load_call(tmp_path, reference)['context']['now_ns'] == NOW_NS
  assert all(path.stat().st_mode & 0o777 == 0o600 for path in archive.output.iterdir())
  with pytest.raises(evidence.SampleEvidenceRejected, match='call_limit'):
    archive.record_call(None, observer.call_context() | {'now_ns': NOW_NS + 1})


@pytest.mark.parametrize('field', ['source_decision', 'source_reason', 'adapter_instruction_reason', 'consumer_binding', 'clock'])
def test_replay_rejects_changed_frame_decisions_or_binding(tmp_path, field):
  directory = tmp_path / 'run'
  frames = write_run(directory)
  target = frames[2]
  if field == 'source_decision':
    target[field]['publication_reused'] = False
  elif field == 'clock':
    target['source_decision']['accept_called_mono_s'] += 0.1
  elif field == 'consumer_binding':
    target['route_identity_diagnostics'][field]['observedSequence'] += 1
  else:
    target[field] = 'tampered'
  save_frames(directory, frames)
  with pytest.raises(evidence.SampleEvidenceRejected):
    evidence.replay_run(directory)


def test_replay_rejects_call_file_corruption(tmp_path):
  directory = tmp_path / 'run'
  frames = write_run(directory)
  path = directory / 'sample-evidence' / frames[2]['private_adapter_call_evidence']['file']
  path.write_bytes(path.read_bytes() + b' ')
  with pytest.raises(evidence.SampleEvidenceRejected, match='file_hash_mismatch'):
    evidence.replay_run(directory)


def test_replay_uses_archived_status_instead_of_inventing_a_refresh(tmp_path):
  directory = tmp_path / 'run'
  frames = write_run(directory)
  call = evidence.load_call(directory, frames[2]['private_adapter_call_evidence'])
  call['context']['source_status']['connected'] = False
  raw = evidence._encode(call)
  digest = hashlib.sha256(raw).hexdigest()
  reference = {'file': f'call-{digest}.bin', 'sha256': digest, 'bytes': len(raw)}
  (directory / 'sample-evidence' / reference['file']).write_bytes(raw)
  frames[2]['private_adapter_call_evidence'] = reference
  save_frames(directory, frames)
  with pytest.raises(evidence.SampleEvidenceRejected, match='replay_decision_mismatch'):
    evidence.replay_run(directory)


@pytest.mark.parametrize('kind', ['swap', 'gap'])
def test_replay_never_splices_calls_across_frame_order_or_gaps(tmp_path, kind):
  directory = tmp_path / 'run'
  frames = write_run(directory)
  if kind == 'swap':
    frames[0], frames[1] = frames[1], frames[0]
  else:
    frames[2]['index'] += 1
  save_frames(directory, frames)
  with pytest.raises(evidence.SampleEvidenceRejected, match='replay_frame_gap'):
    evidence.replay_run(directory)


def test_replay_rejects_source_drift_and_missing_frame_context(tmp_path):
  directory = tmp_path / 'run'
  frames = write_run(directory)
  report = json.loads((directory / 'report.json').read_text())
  report['source_sha256']['openpilot/selfdrive/navigation/amap_phone_route.py'] = 'f' * 64
  (directory / 'report.json').write_text(json.dumps(report))
  with pytest.raises(evidence.SampleEvidenceRejected, match='replay_source_changed'):
    evidence.replay_run(directory)
  report['source_sha256'] = evidence.replay_source_sha256()
  (directory / 'report.json').write_text(json.dumps(report))
  del frames[0]['private_adapter_call_evidence']
  save_frames(directory, frames)
  with pytest.raises(evidence.SampleEvidenceRejected, match='call_reference_missing'):
    evidence.replay_run(directory)


def test_rejected_terminal_sample_is_replayed_as_rejection(tmp_path):
  sample = sample_fixture()
  positions = [evidence._plain(getattr(sample, side)) for side in ('native_before', 'native_after')]
  for observation in positions:
    observation['_position']['point_index'] = 5
  sample = replace(sample, native_before=evidence._freeze(positions[0]), native_after=evidence._freeze(positions[1]))
  directory = tmp_path / 'run'
  write_run(directory, [(sample, NOW_NS, None)])
  result = evidence.replay_run(directory)
  assert result['reason_counts'] == {'phone_anchor_terminal_local_point_unverified': 1}
  assert result['publications_replayed'] == 0
  assert result['consumer_bindings_equal'] == 'not_observed'


@pytest.mark.parametrize('field,value', [('running', 1), ('connected', None), ('sequence', True),
                                       ('reason', '../private'), ('transport_session', 'x' * 257)])
def test_invalid_or_unbounded_source_context_is_not_replaced_with_a_guess(tmp_path, field, value):
  archive = evidence.SampleEvidenceArchive(tmp_path)
  sample = sample_fixture()
  observer = ObservedPhoneRouteAdapter()
  observer.accept(None, now_ns=NOW_NS, source_status=status(sample))
  call = observer.call_context()
  call['source_status'][field] = value
  with pytest.raises(evidence.SampleEvidenceRejected, match='call_context_invalid'):
    archive.record_call(None, call)


def test_legacy_archive_remains_verifiable_but_is_not_claimed_to_have_full_call_replay(tmp_path):
  directory = tmp_path / 'legacy'
  frames = write_run(directory)
  report = json.loads((directory / 'report.json').read_text())
  for key in ('adapter_call_schema_version', 'unique_adapter_calls'):
    del report['sample_evidence'][key]
  (directory / 'report.json').write_text(json.dumps(report))
  for frame in frames:
    del frame['private_adapter_call_evidence']
  save_frames(directory, frames)
  assert evidence.verify_run(directory)['archive_verified'] is True
  with pytest.raises(evidence.SampleEvidenceRejected, match='call_context_not_archived'):
    evidence.replay_run(directory)


def test_real_isolated_bench_captures_exact_at_call_status_and_full_sequence(tmp_path):
  directory = tmp_path / 'real-bench'
  directory.mkdir()
  archive = evidence.SampleEvidenceArchive(directory)
  frames = []
  sample = shift_sample(sample_fixture(), time.monotonic_ns() - NOW_NS)
  with bench_module.PhonePipelineBench() as bench:
    current = status(sample)
    bench.bind_source_status(lambda: current)
    for index, supplied in enumerate((None, sample, None, None)):
      if index == 3:
        current = SimpleNamespace(running=True, connected=False, reason='phone_source_disconnected',
                                  transport_session=sample.transport_session, sequence=sample.sequence)
      inputs, details = bench.step(supplied)
      frame = probe.frame_record(index, index * 250, supplied, details, inputs)
      frame['private_sample_evidence'] = archive.record(supplied)
      frame['private_adapter_call_evidence'] = archive.record_call(
        frame['private_sample_evidence'], details.get('adapter_call_context'))
      frames.append(frame)
    assert frames[1]['map_valid'] and frames[2]['map_valid']
    assert frames[2]['source_decision']['publication_reused'] is True
    assert not frames[3]['map_valid']
  save_frames(directory, frames)
  (directory / 'report.json').write_text(json.dumps({
    'frames': len(frames), 'sample_evidence': archive.summary(), 'source_sha256': evidence.replay_source_sha256(),
    'cleanup_verified': True, 'error': None, 'interrupted': None}))
  result = evidence.replay_run(directory)
  assert result['adapter_calls_replayed'] == 4
  assert result['valid_map_consumer_bindings_checked'] == 2
  final = evidence.load_call(directory, frames[3]['private_adapter_call_evidence'])
  assert final['context']['source_status']['connected'] is False

