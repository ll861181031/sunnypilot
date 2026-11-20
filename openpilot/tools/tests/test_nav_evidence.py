import importlib
import importlib.util
import json

import pytest
import zstandard as zstd

from openpilot.cereal import messaging


def tool():
  assert importlib.util.find_spec('openpilot.tools.nav_evidence') is not None, 'navigation evidence reader is not implemented'
  return importlib.import_module('openpilot.tools.nav_evidence')


def row(t, *, valid=True, version=1, instruction=True, receipt=True, verified=True, reason='matched', **overrides):
  evidence = {
    'version': version, 'frameId': t, 'source': 'phone', 'instructionReason': 'accepted' if instruction else 'passed',
    'mapReason': 'phoneSuppressed', 'receiptAvailable': receipt, 'receiptVerified': verified,
    'receiptReason': reason, 'gpuSampled': False, 'gpuMatch': False, 'gpuError': False,
    'workerSession': 12, 'retryCount': 0, 'navigation': [0.0] * 47,
  }
  if instruction:
    evidence['navigation'][15] = 1.0
  evidence.update(overrides)
  return {'logMonoTime': t, 'messageValid': valid, 'route': 'route-a', 'navInstructionValid': instruction,
          'navMapValid': False, 'evidence': evidence if version else None}


def test_segments_accept_file_directory_prefix_and_numerically_sort(tmp_path):
  expected = []
  for index in (10, 2, 0):
    path = tmp_path / f'000001ae--abcdef--{index}' / 'rlog.zst'
    path.parent.mkdir()
    path.touch()
    expected.append(path)
  expected = [str(expected[i]) for i in (2, 1, 0)]
  assert tool().segments(str(tmp_path)) == expected
  assert tool().segments(str(tmp_path / '000001ae--abcdef')) == expected
  assert tool().segments(str(tmp_path / '000001ae--abcdef--2')) == expected[1:2]
  assert tool().segments(expected[0]) == expected[:1]
  assert tool().segments(str(tmp_path / 'missing')) == []


def test_missing_legacy_evidence_is_unknown_not_zero_navigation():
  summary = tool().summarize([row(10, version=0, instruction=False), row(20, version=0)])
  assert summary['evidence_status'] == 'unavailable'
  assert summary['missing_evidence_frames'] == 2
  assert summary['receipt']['coverage_ratio'] is None
  assert summary['gpu']['coverage_ratio'] is None
  assert summary['runtime_instruction_gaps'] == []
  assert summary['navigation_zero_intervals'] == []


def test_summary_separates_invalid_frames_receipts_samples_and_retries():
  records = [
    row(1_000_000_000, gpuSampled=True, gpuMatch=True),
    row(1_050_000_000, verified=False, reason='feedMismatch', retryCount=1),
    row(1_100_000_000, receipt=False, verified=False, reason='legacyNoReceipt'),
    row(1_150_000_000, valid=False, gpuSampled=True, gpuError=True),
    row(1_200_000_000, version=0),
    row(1_250_000_000, version=2),
  ]
  summary = tool().summarize(records)
  assert summary['frames'] == 6
  assert summary['valid_frames'] == 5
  assert summary['invalid_frames'] == 1
  assert summary['valid_evidence_frames'] == 3
  assert summary['unsupported_evidence_frames'] == 1
  assert summary['receipt']['available_frames'] == 2
  assert summary['receipt']['verified_frames'] == 1
  assert summary['receipt']['mismatch_frames'] == 1
  assert summary['receipt']['coverage_ratio'] == pytest.approx(2 / 3)
  assert summary['gpu']['sampled_frames'] == 1
  assert summary['gpu']['matching_frames'] == 1
  assert summary['gpu']['error_frames'] == 0
  assert summary['retries']['frames'] == 1
  assert summary['retries']['attempts'] == 1


def test_runtime_gap_does_not_claim_file_fallback_is_zero():
  fallback = [0.0] * 47
  fallback[10] = 1.0
  records = [row(1_000_000_000, instruction=False, navigation=fallback, instructionInputSource='files'),
             row(1_050_000_000, instruction=False), row(1_100_000_000)]
  summary = tool().summarize(records)
  assert summary['runtime_instruction_gaps'][0]['frames'] == 2
  assert summary['navigation_zero_intervals'][0]['frames'] == 1
  assert summary['runtime_instruction_gaps'][0]['observed_duration_s'] == pytest.approx(0.05)


def test_intervals_do_not_bridge_missing_evidence_or_route_boundaries():
  records = [row(1_000_000_000, instruction=False), row(1_050_000_000, version=0),
             row(1_100_000_000, instruction=False), row(2_000_000_000, instruction=False)]
  last = row(2_050_000_000, instruction=False)
  last['route'] = 'route-b'
  records.append(last)
  summary = tool().summarize(records)
  assert len(summary['runtime_instruction_gaps']) == 4
  assert all(gap['observed_duration_s'] == 0 for gap in summary['runtime_instruction_gaps'])


def test_real_capnp_zstd_roundtrip_and_cli_streamed_frames(tmp_path, capsys):
  event = messaging.new_message('modelDataV2SP')
  event.valid = True
  event.logMonoTime = 1_000_000_000
  event.modelDataV2SP.navInstructionValid = True
  event.modelDataV2SP.navEvidence = {'version': 1, 'frameId': 42, 'source': 'phone',
                                   'instructionReason': 'accepted', 'navigation': [0.0] * 46 + [1.5],
                                   'receiptAvailable': True, 'receiptVerified': True, 'receiptReason': 'matched'}
  legacy = messaging.new_message('modelDataV2SP')
  legacy.valid = True
  legacy.logMonoTime = 1_050_000_000
  path = tmp_path / 'rlog.zst'
  path.write_bytes(zstd.compress(event.to_bytes() + legacy.to_bytes()))
  output, frames = tmp_path / 'summary.json', tmp_path / 'frames.jsonl'
  assert tool().main(['--route', str(path), '--out', str(output), '--frames', str(frames)]) == 0
  summary = json.loads(output.read_text())
  assert summary['frames'] == 2 and summary['missing_evidence_frames'] == 1
  detail = [json.loads(line) for line in frames.read_text().splitlines()]
  assert detail[0]['evidence']['frameId'] == 42
  assert detail[0]['evidence']['navigation'][-1] == 1.5
  assert detail[1]['evidence'] is None
  assert json.loads(capsys.readouterr().out)['frames'] == 2


def test_no_logs_returns_nonzero_without_claiming_navigation_missing(tmp_path, capsys):
  assert tool().main(['--route', str(tmp_path)]) == 1
  assert json.loads(capsys.readouterr().out)['evidence_status'] == 'unavailable'


@pytest.mark.parametrize('option', ['--frames', '--out'])
def test_output_cannot_overwrite_source_log(tmp_path, option):
  path = tmp_path / 'rlog.zst'
  path.write_bytes(b'original log bytes')
  with pytest.raises(SystemExit) as error:
    tool().main(['--route', str(path), option, str(path)])
  assert error.value.code == 2
  assert path.read_bytes() == b'original log bytes'


def test_corrupt_log_is_reported_as_read_issue(tmp_path, capsys):
  path = tmp_path / 'rlog.zst'
  path.write_bytes(b'not a valid zstd frame')
  assert tool().main(['--route', str(path)]) == 2
  result = json.loads(capsys.readouterr().out)
  assert result['evidence_status'] == 'unavailable'
  assert result['read_issues'][0]['path'] == str(path)


def test_failure_log_is_exported_without_changing_model_frame_denominator(tmp_path, capsys):
  model = messaging.new_message('modelDataV2SP')
  model.valid = True
  model.modelDataV2SP.navEvidence = {'version': 1, 'frameId': 50, 'receiptAvailable': True, 'receiptVerified': True}
  failure = messaging.new_message(None)
  failure.logMessage = json.dumps({'msg': {'event': 'navigationModelFailure', 'frameId': 51,
                                         'error': 'TimeoutError: worker', 'remote': {'clientSequence': 8}}})
  malformed = messaging.new_message(None)
  malformed.logMessage = '{broken json'
  ordinary = messaging.new_message(None)
  ordinary.logMessage = json.dumps({'msg': 'normal log'})
  path, frames = tmp_path / 'rlog.zst', tmp_path / 'frames.jsonl'
  path.write_bytes(zstd.compress(b''.join(event.to_bytes() for event in (model, failure, malformed, ordinary))))
  assert tool().main(['--route', str(path), '--frames', str(frames)]) == 0
  result = json.loads(capsys.readouterr().out)
  assert result['frames'] == 1 and result['receipt']['coverage_ratio'] == 1.0
  assert result['inference_failures']['count'] == 1 and result['read_issues'] == []
  records = [json.loads(line) for line in frames.read_text().splitlines()]
  assert len(records) == 2 and records[1]['kind'] == 'inferenceFailure'
  assert records[1]['failure']['frameId'] == 51
  assert records[1]['failure']['remote']['clientSequence'] == 8


def test_failure_details_are_bounded_and_interrupt_input_intervals():
  failures = [{'kind': 'inferenceFailure', 'logMonoTime': index, 'failure': {'frameId': index}} for index in range(105)]
  result = tool().summarize([row(1, instruction=False), *failures, row(106, instruction=False)])
  assert result['frames'] == 2
  assert result['inference_failures']['count'] == 105
  assert len(result['inference_failures']['details']) == 100
  assert result['inference_failures']['omitted'] == 5
  assert len(result['runtime_instruction_gaps']) == 2
