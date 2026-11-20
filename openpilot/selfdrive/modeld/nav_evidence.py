"""Client-side navigation evidence; never change an inference result on a logging mismatch."""
from openpilot.selfdrive.modeld.nav_evidence_protocol import crc32_buffer


RECEIPT_FIELDS = {
  'protocol_version': 'receiptVersion', 'worker_session': 'workerSession', 'step_seq': 'workerSequence',
  'completed_mono_time': 'workerCompletedMonoTime', 'worker_step_ns': 'workerStepNs',
  'feed_nav_crc32': 'feedNavCrc32', 'feed_map_crc32': 'feedMapCrc32',
  'staging_nav_crc32': 'stagingNavCrc32', 'staging_map_crc32': 'stagingMapCrc32',
  'output_crc32': 'workerOutputCrc32', 'error_code': 'workerErrorCode',
  'diagnostic_duration_ns': 'diagnosticDurationNs', 'gpu_sample_mono_time': 'gpuSampleMonoTime',
  'gpu_sample_duration_ns': 'gpuSampleDurationNs', 'gpu_nav_crc32': 'gpuNavCrc32', 'gpu_map_crc32': 'gpuMapCrc32',
  'capture': 'capture', 'synchronized': 'synchronized', 'feed_valid': 'feedValid',
  'staging_valid': 'stagingValid', 'output_valid': 'outputValid', 'gpu_sampled': 'gpuSampled',
  'gpu_match': 'gpuMatch', 'gpu_error': 'gpuError', 'diagnostic_error': 'diagnosticError',
}


def finish_remote_evidence(evidence: dict, receipt: dict | None, output, previous_worker=None, invalid=False) -> dict:
  result = dict(evidence)
  result.update(receiptAvailable=receipt is not None, receiptVerified=False, outputCrc32=crc32_buffer(output))
  if receipt is None:
    result['receiptReason'] = 'invalidReceipt' if invalid else 'legacyNoReceipt'
    return result
  result.update({target: receipt[source] for source, target in RECEIPT_FIELDS.items()})
  worker = (result['workerSession'], result['workerSequence'])
  if result['diagnosticError']:
    reason = 'workerDiagnosticError'
  elif not worker[0] or not worker[1] or (previous_worker and worker[0] == previous_worker[0] and worker[1] <= previous_worker[1]):
    reason = 'sequenceMismatch'
  elif ('requestMonoTime' in result and
        not result['requestMonoTime'] <= result['workerCompletedMonoTime'] <= result['responseMonoTime']):
    reason = 'completionTimeMismatch'
  elif not result['feedValid']:
    reason = 'feedUnavailable'
  elif result['inputNavCrc32'] != result['feedNavCrc32'] or result['inputMapCrc32'] != result['feedMapCrc32']:
    reason = 'feedMismatch'
  elif not result['stagingValid'] or not result['synchronized']:
    reason = 'stagingUnavailable'
  elif result['inputNavCrc32'] != result['stagingNavCrc32'] or result['inputMapCrc32'] != result['stagingMapCrc32']:
    reason = 'stagingMismatch'
  elif not result['outputValid'] or result['outputCrc32'] != result['workerOutputCrc32']:
    reason = 'outputMismatch'
  else:
    reason = 'matched'
    result['receiptVerified'] = True
  result['receiptReason'] = reason
  # A stale/corrupt receipt cannot certify a GPU sample for the current request.
  if result['gpuSampled']:
    result['gpuMatch'] = bool(result['receiptVerified'] and not result['gpuError'] and result['gpuMatch']
                              and result['gpuNavCrc32'] == result['inputNavCrc32']
                              and result['gpuMapCrc32'] == result['inputMapCrc32'])
  else:
    result['gpuMatch'] = False
  return result
