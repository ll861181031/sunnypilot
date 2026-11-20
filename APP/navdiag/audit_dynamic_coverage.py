#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
MAX_INPUT_BYTES = 128 * 1024 * 1024
INDEX_FIELDS = ('segment_index', 'link_index', 'point_index')
IDENTITY_FIELDS = ('path_identity_hash', 'transport_session_hash', 'source_object_hash')


def anchor(value):
  if not isinstance(value, dict):
    return None
  values = tuple(value.get(key) for key in INDEX_FIELDS)
  return values if all(type(x) is int and x >= 0 for x in values) else None


def mapping(value):
  return value if isinstance(value, dict) else {}


def scan_records(records):
  counts = Counter()
  indices = Counter()
  changes = []
  identities = set()
  previous = None
  for line_number, record in enumerate(records, 1):
    if not isinstance(record, dict):
      raise ValueError('frame_record_not_object')
    counts['total_frames'] += 1
    position = mapping(record.get('sample_position_diagnostics'))
    anchors = mapping(position.get('source_anchors'))
    before, after = (anchor(anchors.get(key)) for key in ('native_before', 'native_after'))
    if before is None or after is None:
      counts['frames_without_usable_anchor_pair'] += 1
      previous = None
      continue
    counts['source_anchor_pair_frames'] += 1
    if before != after:
      counts['source_anchor_changed_during_read_frames'] += 1
      previous = None
      continue
    counts['stable_source_anchor_pair_frames'] += 1
    indices[before] += 1
    if record.get('map_valid') is True:
      counts['map_valid_source_anchor_frames'] += 1
    diagnostics = mapping(record.get('route_identity_diagnostics'))
    selected = mapping(diagnostics.get('selected'))
    identity = tuple(selected.get(key) for key in IDENTITY_FIELDS)
    sequence, index = record.get('sequence'), record.get('index')
    clock = record.get('since_start_ms')
    qualified = (record.get('has_sample') is True and record.get('map_valid') is True
                 and diagnostics.get('source_consumer_binding_match') is True
                 and all(isinstance(x, str) and re.fullmatch('[0-9a-f]{64}', x) for x in identity)
                 and record.get('transport_session_hash') == identity[1]
                 and type(sequence) is int and sequence >= 0
                 and type(selected.get('observed_sequence')) is int and selected.get('observed_sequence') == sequence
                 and type(index) is int and index >= 0 and type(clock) in (int, float)
                 and math.isfinite(clock) and clock >= 0)
    if not qualified:
      counts['stable_but_binding_or_order_unknown_frames'] += 1
      previous = None
      continue
    counts['qualified_bound_frames'] += 1
    identities.add(identity)
    current = (identity, sequence, index, clock, before, line_number)
    if previous is not None:
      prior_identity, prior_sequence, prior_index, prior_clock, prior_anchor, prior_line = previous
      continuous = (identity == prior_identity and index == prior_index + 1
                    and clock > prior_clock and sequence >= prior_sequence)
      if continuous:
        counts['comparable_adjacent_frame_pairs'] += 1
        if before != prior_anchor:
          if sequence == prior_sequence:
            counts['same_sequence_anchor_inconsistencies'] += 1
          else:
            counts['observed_index_change_events'] += 1
            category = ('segment' if before[0] != prior_anchor[0] else
                        'link' if before[1] != prior_anchor[1] else 'point')
            counts[category + '_change_events'] += 1
            changes.append({'previous_line': prior_line, 'line': line_number, 'category': category,
                            'before': list(prior_anchor), 'after': list(before)})
      else:
        counts['identity_gap_or_order_breaks'] += 1
    previous = current
  for key in ('total_frames', 'source_anchor_pair_frames', 'stable_source_anchor_pair_frames',
              'map_valid_source_anchor_frames', 'qualified_bound_frames',
              'source_anchor_changed_during_read_frames', 'observed_index_change_events',
              'segment_change_events', 'link_change_events', 'point_change_events',
              'same_sequence_anchor_inconsistencies'):
    counts[key] += 0
  return {'counts': dict(counts), 'indices': [{'indices': list(key), 'frames': value}
                                            for key, value in sorted(indices.items())],
          'qualified_route_transport_object_identities': len(identities), 'events': changes}


def audit(paths):
  files, total, indices = [], Counter(), Counter()
  for path in sorted(set(p.resolve() for p in paths)):
    before = path.stat()
    if before.st_size > MAX_INPUT_BYTES:
      raise ValueError('input_file_too_large')
    raw = path.read_bytes()
    after = path.stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
      raise ValueError('input_changed_during_read')
    rows = (json.loads(line) for line in raw.splitlines() if line.strip())
    result = scan_records(rows)
    total.update(result['counts'])
    indices.update({tuple(row['indices']): row['frames'] for row in result['indices']})
    files.append({'path': str(path), 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(), **result})
  return {'schema_version': 1, 'scope': 'explicit_frames_jsonl_only', 'files_scanned': len(files),
          'counts': dict(total), 'indices': [{'indices': list(key), 'frames': value}
                                           for key, value in sorted(indices.items())],
          'files': files, 'actual_motion_verified': False,
          'terminal_behavior_verified': False, 'roundabout_behavior_verified': False,
          'gpu_verified': False, 'target_complete': False,
          'limits': ['仅统计明确传入的逐帧文件，不代表全部历史行车数据',
                     '静态非零segment和选路身份变化不是动态跨segment证据',
                     '相邻帧推进也只能称索引变化，不能单凭索引认证真实运动或控制效果',
                     '缺失、不稳定、绑定未知及失序记录切断相邻比较，不跨缺口拼接']}


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--frames', nargs='+', type=Path)
  parser.add_argument('--out', type=Path)
  parser.add_argument('--verify', type=Path, help='只读复核已有审计目录中的输入SHA和统计')
  args = parser.parse_args()
  if args.verify:
    if args.frames or args.out:
      parser.error('--verify 不能和 --frames/--out 同时使用')
    saved = json.loads((args.verify / 'audit.private.json').read_text())
    actual = audit([Path(row['path']) for row in saved['files']])
    if actual != saved:
      raise ValueError('input_or_audit_changed')
    print(json.dumps({'audit_verified': True, 'files': actual['files_scanned'],
                      'counts': actual['counts'], 'indices': actual['indices'],
                      'target_complete': False}, ensure_ascii=False, indent=2))
    return
  if not args.frames or not args.out:
    parser.error('需要 --frames 和尚不存在的 --out 目录')
  result = audit(args.frames)
  args.out.mkdir(mode=0o700, parents=True, exist_ok=False)
  path = args.out / 'audit.private.json'
  path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
  path.chmod(0o600)
  print(json.dumps({k: result[k] for k in ('files_scanned', 'counts', 'indices', 'target_complete')},
                   ensure_ascii=False, indent=2))


if __name__ == '__main__':
  main()
