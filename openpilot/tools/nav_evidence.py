#!/usr/bin/env python3
"""Inspect navigation input evidence in local driving logs.

  python -m openpilot.tools.nav_evidence --route /path/to/000001ae--abcdef \
    --out /tmp/nav-summary.json --frames /tmp/nav-frames.jsonl

Version zero means that detailed evidence was not recorded. Receipt and GPU counters
use valid messages with the supported evidence version, not all historical frames.
Input gaps describe the current vector; recurrent navigation history is not cleared
by a zero vector. This tool does not establish navigation's causal effect on steering.
"""
import argparse
from collections import Counter
from collections.abc import Iterable, Iterator
from contextlib import nullcontext
import json
from pathlib import Path
import warnings

from openpilot.tools.lib.logreader import LogReader

EVIDENCE_VERSION = 1
MAX_CONTIGUOUS_GAP_NS = 500_000_000
MAX_FAILURE_DETAILS = 100


def _segment_key(path: Path) -> tuple[str, int, str]:
  name = path.parent.name
  route, separator, index = name.rpartition('--')
  if separator and index.isdigit():
    return route, int(index), str(path)
  if name.isdigit():
    return str(path.parent.parent), int(name), str(path)
  return name, -1, str(path)


def segments(route: str) -> list[str]:
  path = Path(route).expanduser().absolute()
  if path.is_file():
    return [str(path)]
  names = ('rlog.zst', 'rlog.bz2', 'rlog')
  if path.is_dir():
    direct = next((path / name for name in names if (path / name).is_file()), None)
    if direct is not None:
      return [str(direct)]
    directories = [child for child in path.iterdir() if child.is_dir()]
  elif path.parent.is_dir():
    prefix = path.name + '--'
    directories = [child for child in path.parent.iterdir()
                   if child.is_dir() and child.name.startswith(prefix) and child.name[len(prefix):].isdigit()]
  else:
    return []
  found = []
  for directory in directories:
    source = next((directory / name for name in names if (directory / name).is_file()), None)
    if source is not None:
      found.append(source)
  return [str(source) for source in sorted(found, key=_segment_key)]


def read_records(paths: Iterable[str], issues: list[dict]) -> Iterator[dict]:
  for path in paths:
    with warnings.catch_warnings(record=True) as captured:
      warnings.simplefilter('always')
      try:
        for event in LogReader(path):
          if event.which() == 'logMessage':
            try:
              payload = json.loads(str(event.logMessage))
              failure = payload.get('msg') if isinstance(payload, dict) else None
            except (ValueError, TypeError):
              continue
            if isinstance(failure, dict) and failure.get('event') == 'navigationModelFailure':
              yield {'kind': 'inferenceFailure', 'segment': path, 'route': _segment_key(Path(path))[0],
                     'logMonoTime': int(event.logMonoTime), 'failure': failure}
            continue
          if event.which() != 'modelDataV2SP':
            continue
          data = event.modelDataV2SP
          evidence = data.navEvidence.to_dict()
          yield {
            'segment': path, 'route': _segment_key(Path(path))[0],
            'logMonoTime': int(event.logMonoTime), 'messageValid': bool(event.valid),
            'navInstructionValid': bool(data.navInstructionValid), 'navMapValid': bool(data.navMapValid),
            'navInstructionAge': float(data.navInstructionAge), 'navMapAge': float(data.navMapAge),
            'modelCurvature': float(data.modelCurvature), 'blendfulCurvature': float(data.blendfulCurvature),
            'laneTurnDirection': str(data.laneTurnDirection),
            'evidence': evidence if evidence.get('version', 0) else None,
          }
      except Exception as error:
        issues.append({'path': path, 'error': f'{type(error).__name__}: {error}'})
      finally:
        issues.extend({'path': path, 'warning': str(warning.message)} for warning in captured)


class _Gaps:
  def __init__(self):
    self.current = None
    self.intervals = []

  def close(self, boundary: str) -> None:
    if self.current is not None:
      current = self.current
      current['observed_duration_s'] = (current['endMonoTime'] - current['startMonoTime']) / 1e9
      current['reasons'] = dict(current['reasons'])
      current['boundary'] = boundary
      self.intervals.append(current)
      self.current = None

  def update(self, record: dict, missing: bool, reason: str) -> None:
    t = record['logMonoTime']
    route = record.get('route', '')
    if self.current is not None:
      elapsed = t - self.current['endMonoTime']
      if route != self.current['route'] or not 0 <= elapsed <= MAX_CONTIGUOUS_GAP_NS:
        self.close('dataGapOrRouteChange')
    if not missing:
      self.close('inputRecovered')
      return
    if self.current is None:
      self.current = {'route': route, 'startMonoTime': t, 'endMonoTime': t, 'frames': 0, 'reasons': Counter()}
    self.current['endMonoTime'] = t
    self.current['frames'] += 1
    self.current['reasons'][reason] += 1


def summarize(records: Iterable[dict]) -> dict:
  counts = Counter()
  instruction_reasons, map_reasons, map_poll_reasons = Counter(), Counter(), Counter()
  sources, input_sources, receipt_reasons = Counter(), Counter(), Counter()
  receipt, gpu, retries = Counter(), Counter(), Counter()
  sessions = set()
  last_session = None
  instruction_gaps, zero_gaps = _Gaps(), _Gaps()
  failures = []
  for record in records:
    if record.get('kind') == 'inferenceFailure':
      counts['inference_failures'] += 1
      if len(failures) < MAX_FAILURE_DETAILS:
        failures.append(record)
      instruction_gaps.close('inferenceFailure')
      zero_gaps.close('inferenceFailure')
      continue
    counts['frames'] += 1
    valid = bool(record['messageValid'])
    counts['valid_frames' if valid else 'invalid_frames'] += 1
    evidence = record.get('evidence')
    version = int(evidence.get('version', 0)) if evidence else 0
    if version != EVIDENCE_VERSION:
      counts['missing_evidence_frames' if version == 0 else 'unsupported_evidence_frames'] += 1
      instruction_gaps.close('evidenceUnavailable')
      zero_gaps.close('evidenceUnavailable')
      continue
    counts['evidence_frames'] += 1
    if not valid:
      counts['invalid_evidence_frames'] += 1
      instruction_gaps.close('invalidMessage')
      zero_gaps.close('invalidMessage')
      continue
    counts['valid_evidence_frames'] += 1
    instruction_reason = evidence.get('instructionReason') or 'unknown'
    instruction_reasons[instruction_reason] += 1
    map_reasons[evidence.get('mapReason') or 'unknown'] += 1
    map_poll_reasons[evidence.get('mapPollReason') or 'unknown'] += 1
    sources[evidence.get('source') or 'unknown'] += 1
    input_sources[evidence.get('instructionInputSource') or 'unknown'] += 1
    instruction_gaps.update(record, not record['navInstructionValid'], instruction_reason)
    vector = evidence.get('navigation', [])
    if len(vector) == 47:
      zero = all(value == 0 for value in vector)
      counts['zero_vector_frames' if zero else 'nonzero_vector_frames'] += 1
      zero_gaps.update(record, zero, instruction_reason)
    else:
      counts['unrecorded_vector_frames'] += 1
      zero_gaps.close('vectorUnavailable')
    available = bool(evidence.get('receiptAvailable'))
    verified = available and bool(evidence.get('receiptVerified'))
    receipt['available_frames' if available else 'unavailable_frames'] += 1
    receipt['verified_frames'] += int(verified)
    receipt['unverified_frames'] += int(available and not verified)
    reason = evidence.get('receiptReason') or 'unknown'
    receipt_reasons[reason] += 1
    receipt['mismatch_frames'] += int(available and 'mismatch' in reason.lower())
    sampled, error = bool(evidence.get('gpuSampled')), bool(evidence.get('gpuError'))
    gpu['sampled_frames'] += int(sampled)
    gpu['error_frames'] += int(error)
    gpu['matching_frames'] += int(sampled and not error and bool(evidence.get('gpuMatch')))
    gpu['mismatch_frames'] += int(sampled and not error and not evidence.get('gpuMatch'))
    retry_count = int(evidence.get('retryCount', 0))
    retries['frames'] += int(retry_count > 0)
    retries['attempts'] += retry_count
    session = int(evidence.get('workerSession', 0))
    if available and session:
      sessions.add(session)
      retries['worker_session_changes'] += int(last_session is not None and last_session != session)
      last_session = session
  instruction_gaps.close('endOfInput')
  zero_gaps.close('endOfInput')
  total = counts['valid_evidence_frames']
  status = ('unavailable' if not total else 'available' if total == counts['frames'] else 'partial')
  return {
    'evidence_version': EVIDENCE_VERSION, 'evidence_status': status,
    'inference_failures': {'count': counts['inference_failures'], 'details': failures,
                          'omitted': counts['inference_failures'] - len(failures)},
    **{key: counts[key] for key in ('frames', 'valid_frames', 'invalid_frames', 'evidence_frames', 'valid_evidence_frames',
                                  'invalid_evidence_frames', 'missing_evidence_frames', 'unsupported_evidence_frames',
                                  'zero_vector_frames', 'nonzero_vector_frames', 'unrecorded_vector_frames')},
    'metric_scope': 'valid messages with evidence version 1',
    'instruction_reasons': dict(instruction_reasons), 'map_reasons': dict(map_reasons), 'map_poll_reasons': dict(map_poll_reasons),
    'sources': dict(sources), 'instruction_input_sources': dict(input_sources),
    'receipt': {**{key: receipt[key] for key in ('available_frames', 'unavailable_frames', 'verified_frames',
                                              'unverified_frames', 'mismatch_frames')},
                'coverage_ratio': receipt['available_frames'] / total if total else None, 'reasons': dict(receipt_reasons)},
    'gpu': {**{key: gpu[key] for key in ('sampled_frames', 'matching_frames', 'mismatch_frames', 'error_frames')},
            'coverage_ratio': gpu['sampled_frames'] / total if total else None},
    'retries': {**{key: retries[key] for key in ('frames', 'attempts', 'worker_session_changes')}, 'worker_sessions': len(sessions)},
    'runtime_instruction_gaps': instruction_gaps.intervals, 'navigation_zero_intervals': zero_gaps.intervals,
  }


def main(argv=None) -> int:
  parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
  parser.add_argument('--route', required=True, help='本地 rlog、分段目录、路线前缀或包含分段的目录')
  parser.add_argument('--out', help='汇总 JSON 文件')
  parser.add_argument('--frames', help='逐帧 JSONL 文件（流式写入）')
  args = parser.parse_args(argv)
  paths, issues = segments(args.route), []
  destinations = [Path(value).expanduser().resolve() for value in (args.out, args.frames) if value]
  inputs = [Path(value).resolve() for value in paths]
  for index, destination in enumerate(destinations):
    for source in inputs + destinations[:index]:
      if destination == source or (destination.exists() and source.exists() and destination.samefile(source)):
        parser.error('输出文件不能覆盖原始日志，汇总和逐帧输出也必须使用不同文件')
  with Path(args.frames).expanduser().open('w') if args.frames else nullcontext(None) as output:
    def records():
      for record in read_records(paths, issues):
        if output is not None:
          output.write(json.dumps(record, ensure_ascii=False) + '\n')
        yield record
    result = summarize(records())
  result.update({'route': args.route, 'segments': paths, 'read_issues': issues})
  text = json.dumps(result, ensure_ascii=False, indent=2)
  print(text)
  if args.out:
    Path(args.out).expanduser().write_text(text + '\n')
  return 2 if issues else 0 if result['frames'] or result['inference_failures']['count'] else 1


if __name__ == '__main__':
  raise SystemExit(main())
