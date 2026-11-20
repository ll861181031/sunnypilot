import base64
import time
import zlib
from types import SimpleNamespace

import numpy as np
import pytest

from openpilot.cereal import log, messaging
from openpilot.selfdrive.modeld import modeld
from openpilot.selfdrive.modeld.nav_evidence_protocol import RECEIPT_MAGIC, encode_receipt
from openpilot.selfdrive.modeld.test_migraphx_recovery import ProtocolPeer, remote_state, run_frame


LEGACY_MODEL_DATA = ''.join([
  'AAAAABEAAAAAAAAAAgABANsgEeryCgAAcgAAAAAAAAAAAAAABQABAAAAEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACAPgAAAAAAAAAABAAD',
  'AAAAAAAAAAAAAAAAAAAAAAAqAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA',
])


def test_old_record_does_not_claim_worker_or_gpu_confirmation():
  with log.Event.from_bytes(base64.b64decode(LEGACY_MODEL_DATA)) as event:
    md = event.modelDataV2SP
    assert md.navInstructionValid and md.navInstructionAge == 0.25
    assert md.lateralDelayDebug.frameId == 42
    assert 'navEvidence' in md.schema.fields
    assert md.navEvidence.version == 0
    assert not md.navEvidence.receiptAvailable and not md.navEvidence.gpuSampled
    assert not md.navEvidence.navigationResetSupported
    assert not md.navEvidence.navigationSessionResetConfirmed
    assert md.navEvidence.mapResetRequestedEpoch == md.navEvidence.mapResetAppliedEpoch == 0
    assert md.navEvidence.instructionResetRequestedEpoch == md.navEvidence.instructionResetAppliedEpoch == 0
    assert md.navEvidence.phonePositionQuality == ""
    assert md.navEvidence.phoneGpsAccuracyM == 0
    assert md.navEvidence.phoneNativePoseErrorM == 0
    assert not md.navEvidence.controlInputAllowed


def test_gate_and_worker_evidence_roundtrip_keeps_frame_identity():
  fill = getattr(modeld, 'fill_nav_evidence', None)
  assert callable(fill), 'modeld must publish the per-frame evidence'
  event = messaging.new_message('modelDataV2SP')
  runtime = SimpleNamespace(evidence=lambda: {
    'source': 'phone', 'instructionReason': 'passed', 'mapReason': 'phoneActive',
    'instructionMonoTime': 1_000_000_000, 'instructionReceivedMonoTime': 1_050_000_000,
    'rawDistance': 0.0, 'adjustedDistance': -2.0, 'traveledDistance': 2.0,
    'selectedManeuverIndex': -1, 'selectedCommand': -1,
  })
  model = SimpleNamespace(last_nav_evidence={
    'clientSequence': 12, 'workerSequence': 101, 'workerSession': 9988,
    'receiptAvailable': True, 'receiptVerified': True, 'receiptReason': 'matched',
    'navigation': np.zeros(47, np.float32).tolist(), 'instructionInputSource': 'zeros',
  })
  fill(event.modelDataV2SP, runtime, model, 7, 8, 1_200_000_000)
  with log.Event.from_bytes(event.to_bytes()) as decoded:
    evidence = decoded.modelDataV2SP.navEvidence
    assert evidence.version == 1
    assert (evidence.frameId, evidence.frameIdExtra, evidence.timestampEof) == (7, 8, 1_200_000_000)
    assert evidence.instructionReason == 'passed' and evidence.adjustedDistance == -2.0
    assert evidence.receiptVerified and evidence.workerSession == 9988 and evidence.workerSequence == 101
    assert len(evidence.navigation) == 47 and not any(evidence.navigation)
    assert not evidence.gpuSampled


def test_other_backend_does_not_reuse_remote_confirmation():
  fill = getattr(modeld, 'fill_nav_evidence', None)
  assert callable(fill), 'modeld must distinguish uninstrumented backends'
  event = messaging.new_message('modelDataV2SP')
  fill(event.modelDataV2SP, None, SimpleNamespace(), 11, 11, 200)
  evidence = event.modelDataV2SP.navEvidence
  assert evidence.frameId == 11 and not evidence.receiptAvailable
  assert evidence.receiptReason == 'backendUnsupported'
  assert list(evidence.navigation) == []


class EvidencePeer(ProtocolPeer):
  def __init__(self, modes):
    super().__init__()
    self.modes = iter(modes)
    self.frames = 0

  def recv_into(self, target, length):
    if not self.reply:
      self.frames += 1
      mode = next(self.modes)
      output = np.full(18580, 1.5, np.float32).tobytes()
      nav_crc = zlib.crc32(np.ones(47, np.float32).tobytes())
      map_crc = zlib.crc32(np.ones((256, 256), np.float32).tobytes())
      receipt = encode_receipt({
        'worker_session': 99, 'step_seq': self.frames, 'completed_mono_time': time.monotonic_ns(),
        'feed_nav_crc32': nav_crc ^ int(mode == 'corrupt'), 'feed_map_crc32': map_crc,
        'staging_nav_crc32': nav_crc, 'staging_map_crc32': map_crc, 'output_crc32': zlib.crc32(output),
        'synchronized': True, 'feed_valid': True, 'staging_valid': True, 'output_valid': True,
        'capture': True,
      })
      if mode == 'invalid':
        receipt = b'\xff\xff' + receipt[2:]
      self.reply.extend(b'MGX3' + output if mode == 'legacy' else RECEIPT_MAGIC + receipt + output)
    count = min(length, len(self.reply), 71)
    target[:count] = self.reply[:count]
    del self.reply[:count]
    return count


def test_worker_receipt_matches_actual_input_and_returned_output():
  peer = EvidencePeer(['ok'])
  state = remote_state(peer, [])
  output = run_frame(state)
  evidence = getattr(state, 'last_nav_evidence', {})
  assert evidence.get('receiptVerified'), 'worker acknowledgement must be logged for the exact request'
  assert evidence['receiptAvailable'] and evidence['workerSession'] == 99 and evidence['workerSequence'] == 1
  assert evidence['inputNavCrc32'] == evidence['stagingNavCrc32'] == evidence['feedNavCrc32']
  assert evidence['outputCrc32'] == zlib.crc32(output.tobytes())
  assert evidence['navigation'] == [1.0] * 47
  assert evidence['desire'][2] == 1 and sum(evidence['desire']) == 1
  assert evidence['responseMonoTime'] >= evidence['requestMonoTime'] > 0
  assert peer.sent[1][0] & 4
  message = messaging.new_message('modelDataV2SP')
  modeld.fill_nav_evidence(message.modelDataV2SP, None, state, 102, 103, 2_000_000_000)
  raw = message.to_bytes()
  assert len(raw) < 2048
  with log.Event.from_bytes(raw) as decoded:
    result = decoded.modelDataV2SP.navEvidence
    assert result.receiptVerified and result.workerOutputCrc32 == evidence['outputCrc32']
    assert result.frameId == 102 and result.receiptVersion == 1 and result.capture


def test_legacy_reply_is_unknown_and_does_not_replay_inference():
  peer = EvidencePeer(['legacy'])
  state = remote_state(peer, [])
  output = run_frame(state)
  evidence = getattr(state, 'last_nav_evidence', {})
  assert evidence.get('receiptReason') == 'legacyNoReceipt'
  assert not evidence['receiptAvailable'] and not evidence['receiptVerified']
  assert peer.frames == 1 and not peer.closed
  np.testing.assert_array_equal(output, np.full(18580, 1.5, np.float32))


def test_mismatched_receipt_is_visible_without_changing_model_output():
  state = remote_state(EvidencePeer(['corrupt']), [])
  output = run_frame(state)
  evidence = state.last_nav_evidence
  assert evidence['receiptAvailable'] and not evidence['receiptVerified']
  assert evidence['receiptReason'] == 'feedMismatch'
  np.testing.assert_array_equal(output, np.full(18580, 1.5, np.float32))


def test_invalid_receipt_does_not_repeat_inference_and_next_frame_is_fresh():
  peer = EvidencePeer(['ok', 'invalid', 'legacy'])
  state = remote_state(peer, [])
  run_frame(state)
  run_frame(state)
  assert state.last_nav_evidence['receiptReason'] == 'invalidReceipt'
  assert not state.last_nav_evidence['receiptVerified']
  assert state.last_nav_evidence.get('workerSession', 0) == 0
  run_frame(state)
  assert state.last_nav_evidence['clientSequence'] == 3
  assert state.last_nav_evidence['receiptReason'] == 'legacyNoReceipt'
  assert state.last_nav_evidence.get('workerSession', 0) == 0
  assert peer.frames == 3 and not peer.closed


@pytest.mark.parametrize(('change', 'reason'), [
  ({'feed_valid': False}, 'feedUnavailable'),
  ({'staging_valid': False}, 'stagingUnavailable'),
  ({'synchronized': False}, 'stagingUnavailable'),
  ({'staging_nav_crc32': 0}, 'stagingMismatch'),
  ({'output_crc32': 0}, 'outputMismatch'),
  ({'diagnostic_error': True}, 'workerDiagnosticError'),
  ({'step_seq': 0}, 'sequenceMismatch'),
])
def test_unverified_receipt_never_certifies_gpu_sample(change, reason):
  from openpilot.selfdrive.modeld.nav_evidence import finish_remote_evidence
  from openpilot.selfdrive.modeld.nav_evidence_protocol import decode_receipt
  output = np.ones(18580, np.float32)
  receipt = decode_receipt(encode_receipt({
    'worker_session': 5, 'step_seq': 1, 'feed_nav_crc32': 12, 'feed_map_crc32': 34,
    'staging_nav_crc32': 12, 'staging_map_crc32': 34, 'output_crc32': zlib.crc32(output.tobytes()),
    'feed_valid': True, 'staging_valid': True, 'synchronized': True, 'output_valid': True,
    'gpu_sampled': True, 'gpu_match': True, 'gpu_nav_crc32': 12, 'gpu_map_crc32': 34,
  }))
  receipt.update(change)
  result = finish_remote_evidence({'inputNavCrc32': 12, 'inputMapCrc32': 34}, receipt, output)
  assert not result['receiptVerified'] and not result['gpuMatch']
  assert result['receiptReason'] == reason


def test_reused_worker_sequence_is_not_accepted_for_next_camera_frame():
  from openpilot.selfdrive.modeld.nav_evidence import finish_remote_evidence
  from openpilot.selfdrive.modeld.nav_evidence_protocol import decode_receipt
  output = np.zeros(18580, np.float32)
  receipt = decode_receipt(encode_receipt({'worker_session': 99, 'step_seq': 5}))
  result = finish_remote_evidence({'inputNavCrc32': 0, 'inputMapCrc32': 0}, receipt, output, (99, 5))
  assert not result['receiptVerified'] and result['receiptReason'] == 'sequenceMismatch'


def test_receipt_outside_request_window_cannot_certify_this_frame():
  from openpilot.selfdrive.modeld.nav_evidence import finish_remote_evidence
  from openpilot.selfdrive.modeld.nav_evidence_protocol import decode_receipt
  receipt = decode_receipt(encode_receipt({'worker_session': 99, 'step_seq': 10, 'completed_mono_time': 50}))
  result = finish_remote_evidence({'inputNavCrc32': 0, 'inputMapCrc32': 0, 'requestMonoTime': 60,
                                  'responseMonoTime': 80}, receipt, np.zeros(18580, np.float32))
  assert not result['receiptVerified'] and result['receiptReason'] == 'completionTimeMismatch'


def test_sequence_regression_cannot_lower_client_high_water_mark(monkeypatch):
  peer = EvidencePeer(['ok', 'ok', 'ok'])
  state = remote_state(peer, [])
  original = modeld.decode_receipt
  sequences = iter((100, 99, 100))
  def decode(payload):
    return {**original(payload), 'step_seq': next(sequences)}
  monkeypatch.setattr(modeld, 'decode_receipt', decode)
  run_frame(state)
  assert state.last_nav_evidence['receiptVerified']
  run_frame(state)
  assert state.last_nav_evidence['receiptReason'] == 'sequenceMismatch'
  run_frame(state)
  assert state.last_nav_evidence['receiptReason'] == 'sequenceMismatch'


def test_failure_log_contains_current_camera_and_input_evidence(monkeypatch):
  emit = getattr(modeld, 'log_nav_model_failure', None)
  assert callable(emit), 'failed inference also needs an identifiable evidence record'
  logged = []
  monkeypatch.setattr(modeld.cloudlog, 'event', lambda name, **data: logged.append((name, data)))
  model = SimpleNamespace(last_nav_evidence={'clientSequence': 9, 'receiptReason': 'pending'})
  runtime = SimpleNamespace(evidence=lambda: {'source': 'phone', 'instructionReason': 'accepted'})
  emit(runtime, model, 71, 73, 500, TimeoutError('worker unavailable'))
  assert logged[0][0] == 'navigationModelFailure'
  assert logged[0][1]['frameId'] == 71 and logged[0][1]['frameIdExtra'] == 73
  assert logged[0][1]['runtime']['source'] == 'phone'
  assert logged[0][1]['remote']['clientSequence'] == 9
  assert 'TimeoutError' in logged[0][1]['error']


def test_terminal_rpc_failure_records_attempt_count_without_old_receipt():
  state = remote_state(ProtocolPeer('receive'), [ProtocolPeer('receive')])
  with pytest.raises(TimeoutError):
    run_frame(state)
  evidence = state.last_nav_evidence
  assert evidence.get('retryCount') == 1 and evidence['receiptReason'] == 'rpcFailure'
  assert not evidence['receiptVerified'] and not evidence['receiptAvailable']
  assert evidence['responseMonoTime'] >= evidence['requestMonoTime']
