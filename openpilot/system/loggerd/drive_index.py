"""Bounded, local-only rlog inspection. A readable prefix is not a complete log."""

import bz2
import hashlib
import math
from collections import Counter
from pathlib import Path

import capnp
import zstandard as zstd

from openpilot.cereal import log

MAX_COMPRESSED = 256 * 1024 * 1024
MAX_DECOMPRESSED = 512 * 1024 * 1024
REQUIRED_STREAMS = ('can', 'sendcan', 'carState', 'carControl', 'controlsState', 'pandaStates',
                    'vehicleParameters', 'deviceMotion', 'extrinsicsCalibration')
MODEL_STREAMS = ('modelV2', 'drivingModelData')


def read_log(path: Path, should_stop=None) -> tuple[bytes, str, list[str]]:
  if should_stop and should_stop():
    raise InterruptedError('indexing_paused')
  if path.stat().st_size > MAX_COMPRESSED:
    raise ValueError('compressed_log_too_large')
  packed = path.read_bytes()
  digest = hashlib.sha256(packed).hexdigest()
  make_decoder = bz2.BZ2Decompressor if path.suffix == '.bz2' else zstd.ZstdDecompressor().decompressobj
  decoder = make_decoder()
  parts, size, issues = [], 0, []
  for start in range(0, len(packed), 16384):
    if should_stop and should_stop():
      raise InterruptedError('indexing_paused')
    chunk = packed[start:start + 16384]
    while chunk:
      if should_stop and should_stop():
        raise InterruptedError('indexing_paused')
      if decoder.eof:
        decoder = make_decoder()
      try:
        part = decoder.decompress(chunk)
      except (OSError, EOFError, zstd.ZstdError):
        issues.append('invalid_compression_frame')
        return b''.join(parts), digest, issues
      size += len(part)
      if size > MAX_DECOMPRESSED:
        raise ValueError('decompressed_log_too_large')
      parts.append(part)
      chunk = decoder.unused_data if decoder.eof else b''
  if not decoder.eof:
    issues.append('truncated_compression_frame')
  return b''.join(parts), digest, issues


def inspect_log(path: Path, should_stop=None) -> dict:
  result = {'kind': 'rlog' if path.name.startswith('rlog') else 'qlog', 'integrity': 'incomplete', 'issues': [],
            'streams': {}, 'events': [], 'eventsTruncated': False, 'navigation': {'messages': 0, 'withManeuvers': 0, 'types': {}},
            'diagnostics': {'validLateral': 0, 'validDelay': 0}, 'videoFrames': {}, 'identity': {}, 'gpsFixSamples': 0,
            'activity': {'movingSamples': 0, 'latActiveSamples': 0, 'longActiveSamples': 0, 'maxSpeedKmh': None}}
  counts, invalid, previous, gaps, first, last, sentinels = Counter(), Counter(), {}, {}, None, None, Counter()
  event_last = {}

  def event(kind, t, detail=''):
    key = (kind, detail)
    entry = event_last.get(key)
    if entry is not None and 0 <= t - entry['endMonoNs'] <= 500_000_000:
      entry['endMonoNs'] = t
      entry['samples'] += 1
    elif len(result['events']) < 2000:
      entry = {'kind': kind, 'startMonoNs': t, 'endMonoNs': t, 'samples': 1, 'detail': detail}
      result['events'].append(entry)
      event_last[key] = entry
    else:
      result['eventsTruncated'] = True

  try:
    data, result['sha256'], issues = read_log(path, should_stop)
    result['issues'].extend(issues)
    for ordinal, m in enumerate(log.Event.read_multiple_bytes(data)):
      if ordinal % 256 == 0 and should_stop and should_stop():
        raise InterruptedError('indexing_paused')
      kind, t = m.which(), int(m.logMonoTime)
      counts[kind] += 1
      invalid[kind] += not m.valid
      if kind != 'initData':
        first = t if first is None else min(first, t)
        last = t if last is None else max(last, t)
      if kind in previous:
        delta = t - previous[kind]
        if delta < 0:
          result['issues'].append(f'nonmonotonic:{kind}')
        else:
          gaps[kind] = max(gaps.get(kind, 0), delta)
      previous[kind] = t
      if kind == 'initData':
        d = m.initData
        result['identity'] = {'gitCommit': d.gitCommit, 'dirty': d.dirty, 'version': d.version,
                              'wallTimeNs': int(d.wallTimeNanos), 'anchorMonoNs': t,
                              'modelIdentity': 'unknown', 'firmwareIdentity': 'unknown'}
      elif kind == 'sentinel':
        sentinels[str(m.sentinel.type)] += 1
      elif kind == 'carState' and m.valid and math.isfinite(m.carState.vEgo):
        speed = m.carState.vEgo
        result['activity']['movingSamples'] += speed > .3
        result['activity']['maxSpeedKmh'] = max(result['activity']['maxSpeedKmh'] or 0., speed * 3.6)
      elif kind == 'carControl' and m.valid:
        result['activity']['latActiveSamples'] += bool(m.carControl.latActive)
        result['activity']['longActiveSamples'] += bool(m.carControl.longActive)
      elif kind == 'navInstruction':
        n = m.navInstruction
        result['navigation']['messages'] += 1
        result['navigation']['withManeuvers'] += bool(m.valid and len(n.allManeuvers))
        name = n.maneuverType or 'empty'
        types = result['navigation']['types']
        types[name] = types.get(name, 0) + 1
        if n.maneuverType:
          event('navigation', t, f'{n.maneuverType}:{n.maneuverModifier}')
      elif kind == 'carStateSP':
        s = m.carStateSP
        if s.epsTooFast:
          event('epsTooFast', t)
        d = s.bydLateral
        if d.valid:
          result['diagnostics']['validLateral'] += 1
          sample = int(d.sampleMonoTime)
          if d.active and abs(d.desiredAngleDeg - d.measuredAngleDeg) > 8:
            event('targetAngleErrorOver8Deg', sample)
          if d.requestedActive and d.state != 0:
            event('recovery', sample, str(d.entryReason))
      elif kind == 'modelDataV2SP':
        result['diagnostics']['validDelay'] += bool(m.modelDataV2SP.lateralDelayDebug.valid)
      elif kind == 'selfdriveState' and m.selfdriveState.alertType:
        event('alert', t, m.selfdriveState.alertType)
      elif kind == 'userBookmark':
        event('bookmark', t)
      elif kind in ('gpsLocation', 'gpsLocationExternal'):
        result['gpsFixSamples'] += bool(m.valid and getattr(m, kind).hasFix)
      elif kind in ('narrowRoadEncodeIdx', 'wideRoadEncodeIdx', 'cabinEncodeIdx'):
        result['videoFrames'][kind] = result['videoFrames'].get(kind, 0) + 1
      elif kind == 'can':
        for p in m.can:
          if p.address == 0x1E2 and p.src in (192, 194):
            event('pandaTxRejected', t)
  except InterruptedError:
    raise
  except (OSError, ValueError, EOFError, zstd.ZstdError, capnp.KjException) as exc:
    result['issues'].append(f'parse_error:{type(exc).__name__}:{str(exc)[:200]}')

  if not any(sentinels[k] for k in ('startOfRoute', 'startOfSegment')):
    result['issues'].append('missing_start_sentinel')
  if not any(sentinels[k] for k in ('endOfRoute', 'endOfSegment')):
    result['issues'].append('missing_end_sentinel')
  result['issues'] = sorted(set(result['issues']))
  result.update(firstMonoNs=first, lastMonoNs=last, sentinels=dict(sentinels),
                streams={k: {'count': v, 'invalid': invalid[k], 'maxGapNs': gaps.get(k)} for k, v in sorted(counts.items())})
  result['missingRequiredStreams'] = [k for k in REQUIRED_STREAMS if not counts[k]]
  result['unusableRequiredStreams'] = [k for k in REQUIRED_STREAMS if counts[k] and counts[k] == invalid[k]]
  if not any(counts[k] for k in MODEL_STREAMS):
    result['missingRequiredStreams'].append('modelV2|drivingModelData')
  elif not any(counts[k] > invalid[k] for k in MODEL_STREAMS):
    result['unusableRequiredStreams'].append('modelV2|drivingModelData')
  result['integrity'] = 'complete' if not result['issues'] else 'incomplete'
  result['analysisReady'] = (result['kind'] == 'rlog' and result['integrity'] == 'complete'
                             and not result['missingRequiredStreams'] and not result['unusableRequiredStreams'])
  anchor = result['identity'].get('anchorMonoNs')
  wall = result['identity'].get('wallTimeNs')
  if anchor is not None and wall:
    for e in result['events']:
      e['routeTimeSeconds'] = (e['startMonoNs'] - anchor) / 1e9
      e['startWallTimeNs'] = wall + e['startMonoNs'] - anchor
  return result
