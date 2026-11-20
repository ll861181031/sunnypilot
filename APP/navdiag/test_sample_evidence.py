"""Private evidence round trips and sampler integration; no phone, GPU or control process."""
from dataclasses import replace
import json
import stat
from types import SimpleNamespace

import numpy as np
import pytest

from APP.navdiag import live_pipeline_probe as probe
from APP.navdiag import sample_evidence as evidence
from APP.navdiag.android_gps_source import AndroidGpsBridgeFix, AndroidGpsFix
from APP.navdiag.live_route_snapshot import SelectedCacheRoute
from APP.navdiag.phone_route_producer import PhoneRouteSample, _freeze
from APP.navdiag.phone_pipeline_bench import ObservedPhoneRouteAdapter
from APP.navdiag.test_native_link_spans import native_geometry
from openpilot.selfdrive.navigation.amap_phone_route import AmapPhoneRouteAdapter
from openpilot.selfdrive.navigation.tests.test_amap_phone_pipeline import NOW_NS, make_sample


def sample_fixture(native=True, bridge=False):
  value = make_sample()
  if native:
    _, selected = native_geometry(((0, 0, 6), (0, 2, 5)), sample=value)
    cache_bytes = b''
    for side in ('native_before', 'native_after'):
      getattr(value, side)['_position']['point_index'] = 2
  else:
    selected = SelectedCacheRoute(value.selected.route, 0, value.selected.source_path, value.selected.cache_sha256,
                                   value.selected.source_session_id, value.selected.identity_hash,
                                   NOW_NS - 90_000_000, NOW_NS - 60_000_000)
    cache_bytes = value.cache_bytes
  gps = (AndroidGpsBridgeFix if bridge else AndroidGpsFix)(
    value.gps.source_token, value.gps.horizontal_accuracy_m, 0, value.gps.age_upper_ns,
    value.gps.host_started_ns, value.gps.host_finished_ns, value.gps.phone_boot_id,
    value.gps.elapsed_realtime_ns, value.gps.coordinates)
  return PhoneRouteSample(selected, cache_bytes, selected.source_path, value.epoch, value.sequence,
                          value.host_start_ns, gps, _freeze(value.native_before), _freeze(value.native_after),
                          value.transport_session, _freeze(value.broadcast))


@pytest.mark.parametrize('native,bridge', [(True, False), (True, True), (False, False), (False, True)])
def test_exact_round_trip_keeps_geometry_pairs_clocks_and_adapter_decision(tmp_path, native, bridge):
  sample = sample_fixture(native, bridge)
  archive = evidence.SampleEvidenceArchive(tmp_path)
  reference = archive.record(sample)
  restored = evidence.load_sample(tmp_path, reference)
  assert restored == sample
  assert type(restored.gps) is type(sample.gps)
  assert archive.record(sample) == reference
  assert archive.summary()['unique_samples'] == 1
  assert archive.summary()['files'] == 3
  first, second = AmapPhoneRouteAdapter(), AmapPhoneRouteAdapter()
  original = first.accept(sample, now_ns=NOW_NS)
  replayed = second.accept(restored, now_ns=NOW_NS)
  assert original is not None, first.reason
  assert replayed is not None, second.reason
  assert original.snapshot == replayed.snapshot
  assert original.active == replayed.active
  assert stat.S_IMODE(archive.output.stat().st_mode) == 0o700
  assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in archive.output.iterdir())


def test_sequence_conflict_is_preserved_as_error_without_replacing_first_sample(tmp_path):
  sample = sample_fixture()
  archive = evidence.SampleEvidenceArchive(tmp_path)
  reference = archive.record(sample)
  changed = replace(sample, gps=replace(sample.gps, age_upper_ns=sample.gps.age_upper_ns + 1))
  with pytest.raises(evidence.SampleEvidenceRejected, match='sequence_content_conflict'):
    archive.record(changed)
  assert evidence.load_sample(tmp_path, reference) == sample
  assert archive.summary()['unique_samples'] == 1


def test_new_sequence_reuses_geometry_without_dropping_new_observations(tmp_path):
  sample = sample_fixture()
  archive = evidence.SampleEvidenceArchive(tmp_path)
  first = archive.record(sample)
  changed = replace(sample, sequence=sample.sequence + 1, host_start_ns=sample.host_start_ns + 1)
  second = archive.record(changed)
  assert first != second and archive.summary()['unique_samples'] == 2
  assert archive.summary()['files'] == 4
  assert evidence.load_sample(tmp_path, second) == changed


@pytest.mark.parametrize('category', ['sample', 'selected', 'geometry'])
def test_tampered_archive_bytes_are_rejected(tmp_path, category):
  archive = evidence.SampleEvidenceArchive(tmp_path)
  reference = archive.record(sample_fixture())
  path = next(archive.output.glob(category + '-*.bin'))
  raw = bytearray(path.read_bytes())
  raw[-1] ^= 1
  path.write_bytes(raw)
  with pytest.raises(evidence.SampleEvidenceRejected, match='file_hash_mismatch'):
    evidence.load_sample(tmp_path, reference)


def test_reference_cannot_escape_archive_or_follow_symlink(tmp_path):
  archive = evidence.SampleEvidenceArchive(tmp_path)
  reference = archive.record(sample_fixture())
  with pytest.raises(evidence.SampleEvidenceRejected, match='reference_invalid'):
    evidence.load_sample(tmp_path, {**reference, 'file': '../outside'})
  path = archive.output / reference['file']
  outside = tmp_path / 'outside'
  outside.write_bytes(path.read_bytes())
  path.unlink()
  path.symlink_to(outside)
  with pytest.raises(evidence.SampleEvidenceRejected, match='file_invalid'):
    evidence.load_sample(tmp_path, reference)


@pytest.mark.parametrize('field', ['source_geometry_sha256', 'source_payload_sha256'])
def test_mismatched_native_geometry_hash_is_rejected(tmp_path, field):
  sample = sample_fixture()
  archive = evidence.SampleEvidenceArchive(tmp_path)
  with pytest.raises(evidence.SampleEvidenceRejected, match='geometry_hash_mismatch'):
    archive.record(replace(sample, selected=replace(sample.selected, **{field: 'e' * 64})))


def test_optional_limit_stops_recording_instead_of_silently_truncating(tmp_path, monkeypatch):
  archive = evidence.SampleEvidenceArchive(tmp_path)
  monkeypatch.setattr(evidence, 'MAX_TOTAL_BYTES', 1)
  with pytest.raises(evidence.SampleEvidenceRejected, match='total_limit'):
    archive.record(sample_fixture())
  assert archive.summary()['unique_samples'] == 0
  assert archive.summary()['files'] == 0


def test_sample_limit_counts_unique_sequences_not_repeated_frames(tmp_path, monkeypatch):
  archive = evidence.SampleEvidenceArchive(tmp_path)
  sample = sample_fixture()
  monkeypatch.setattr(evidence, 'MAX_SAMPLES', 1)
  first = archive.record(sample)
  assert archive.record(sample) == first
  with pytest.raises(evidence.SampleEvidenceRejected, match='sample_limit'):
    archive.record(replace(sample, sequence=2))


def test_none_or_rejected_adapter_sample_does_not_gain_authorization(tmp_path):
  archive = evidence.SampleEvidenceArchive(tmp_path)
  assert archive.record(None) is None
  sample = sample_fixture()
  changed = evidence._plain(sample.native_after)
  changed['_position'] = {**changed['_position'], 'point_index': 5}
  before = evidence._plain(sample.native_before)
  before['_position'] = {**before['_position'], 'point_index': 5}
  terminal = replace(sample, native_before=_freeze(before), native_after=_freeze(changed))
  reference = archive.record(terminal)
  adapter = AmapPhoneRouteAdapter()
  assert adapter.accept(evidence.load_sample(tmp_path, reference), now_ns=NOW_NS) is None
  assert 'terminal_local_point_unverified' in adapter.reason
  raw = json.loads((archive.output / reference['file']).read_text())
  assert raw['control_input_allowed'] is False and raw['raw_target_memory_bytes_included'] is False


def run_fake_sampler(tmp_path, monkeypatch, *, enabled=True, boom=False):
  sample = sample_fixture()
  positions = [evidence._plain(getattr(sample, side)) for side in ('native_before', 'native_after')]
  for position in positions:
    position['_position']['point_index'] = 5
  sample = replace(sample, native_before=_freeze(positions[0]), native_after=_freeze(positions[1]))
  closed = []

  class Producer:
    def __init__(self, **kwargs):
      pass
    def start(self):
      pass
    def latest(self):
      return sample
    def status(self):
      return SimpleNamespace(reason='accepted', sequence=sample.sequence, connected=True, running=False,
                             cleanup_verified=True, cleanup_diagnostics=())
    def close(self):
      closed.append('producer')
      return True
    def summary(self):
      return {}

  class Bench:
    prefix = 'sample_evidence_fake'
    def __enter__(self):
      return self
    def __exit__(self, *_):
      closed.append('bench')
    def step(self, supplied):
      assert supplied is sample
      if boom:
        raise RuntimeError('sample_evidence_test_failure')
      adapter = ObservedPhoneRouteAdapter()
      adapter.accept(sample, now_ns=NOW_NS)
      return ({'nav_map': np.zeros((1, 1, 256, 256), np.uint8), 'navigation': np.zeros((1, 47), np.float32)},
              {'source_reason': adapter.reason, 'source_decision': adapter.decision_evidence(),
               'adapter_call_context': adapter.call_context(), 'map_valid': False, 'instruction_valid': False,
               'reset_requested': False, 'runtime': {'mapReason': 'terminal_local_point_unverified'}})

  monkeypatch.setattr(probe, 'ObservedProducer', Producer)
  monkeypatch.setattr(probe, 'PhonePipelineBench', Bench)
  clock = iter(float(i) for i in range(100))
  monkeypatch.setattr(probe.time, 'monotonic', lambda: next(clock, 999.0))
  monkeypatch.setattr(probe.time, 'sleep', lambda _: None)
  out = tmp_path / 'capture'
  arguments = ['--out', str(out), '--duration', '15']
  if enabled:
    arguments.append('--capture-private-samples')
  code = probe.main(arguments)
  return code, out, closed


def test_sampler_opt_in_archives_rejected_sample_and_closes_both_resources(tmp_path, monkeypatch):
  code, out, closed = run_fake_sampler(tmp_path, monkeypatch)
  assert code == 0 and closed == ['producer', 'bench']
  report = json.loads((out / 'report.json').read_text())
  assert report['sample_evidence']['unique_samples'] == 1
  frames = [json.loads(line) for line in (out / 'frames.jsonl').read_text().splitlines()]
  assert frames and all(not frame['map_valid'] and frame['private_sample_evidence'] for frame in frames)
  assert all(frame.get('private_adapter_call_evidence') for frame in frames)
  verified = evidence.verify_run(out)
  assert verified['archive_verified'] and verified['capture_finished_without_error']
  assert verified['adapter_call_archive_verified'] is True
  assert not verified['actual_dynamic_transition_verified']
  assert not verified['gpu_verified'] and not verified['target_complete']


def test_sampler_defaults_to_no_private_sample_archive(tmp_path, monkeypatch):
  code, out, closed = run_fake_sampler(tmp_path, monkeypatch, enabled=False)
  assert code == 0 and len(closed) == 2
  assert json.loads((out / 'report.json').read_text())['sample_evidence'] == {'enabled': False}
  assert not (out / 'sample-evidence').exists()
  assert all('private_sample_evidence' not in json.loads(line) for line in (out / 'frames.jsonl').read_text().splitlines())


def test_sampler_exception_still_closes_resources_and_records_error(tmp_path, monkeypatch):
  code, out, closed = run_fake_sampler(tmp_path, monkeypatch, boom=True)
  assert code == 1 and closed == ['producer', 'bench']
  report = json.loads((out / 'report.json').read_text())
  assert report['sample_evidence']['enabled'] and 'sample_evidence_test_failure' in report['error']


@pytest.mark.parametrize('field', ['sequence', 'route', 'gps', 'sample_clock'])
def test_frame_reference_substitution_is_rejected(tmp_path, monkeypatch, field):
  _, out, _ = run_fake_sampler(tmp_path, monkeypatch)
  path = out / 'frames.jsonl'
  rows = [json.loads(line) for line in path.read_text().splitlines()]
  if field == 'sequence':
    rows[0]['sequence'] += 1
  elif field == 'route':
    rows[0]['route_identity_diagnostics']['selected']['path_identity_hash'] = 'e' * 64
  elif field == 'gps':
    rows[0]['gps_source_token'] = 'e' * 64
  else:
    rows[0]['route_identity_diagnostics']['selected']['sample_host_start_ns'] += 1
  path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
  with pytest.raises(evidence.SampleEvidenceRejected, match='frame_identity_mismatch'):
    evidence.verify_run(out)
