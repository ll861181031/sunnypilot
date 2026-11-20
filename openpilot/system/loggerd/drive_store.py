"""Route inventories, incremental indexes, explicit pins and verified local exports."""

import contextlib
import datetime
import fcntl
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

SCHEMA = 1
PARSER_ID = hashlib.sha256(Path(__file__).with_name('drive_index.py').read_bytes()).hexdigest()
ROUTE_RE = re.compile(r'(?:[0-9a-f]{8}--[0-9a-f]{10}|\d{4}-\d{2}-\d{2}--\d{2}-\d{2}-\d{2})\Z')
LOG_NAMES = ('rlog.zst', 'rlog.bz2', 'qlog.zst', 'qlog.bz2')
VIDEO_NAMES = ('fcamera.hevc', 'ecamera.hevc', 'dcamera.hevc', 'qcamera.ts')


def route_name(value: str) -> str:
  if not ROUTE_RE.fullmatch(value):
    raise ValueError('invalid_route_name')
  return value


def split_segment(name: str) -> tuple[str, int] | None:
  route, _, number = name.rpartition('--')
  if ROUTE_RE.fullmatch(route) and number.isdigit() and int(number) < 100000:
    return route, int(number)
  return None


def store_root(root: Path) -> Path:
  if root.is_mount():
    return root / '.records'
  return root.parent / (root.name + '.records')


def atomic_json(path: Path, value) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  fd, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
  try:
    with os.fdopen(fd, 'w') as f:
      json.dump(value, f, ensure_ascii=False, indent=2, allow_nan=False)
      f.write('\n')
      f.flush()
      os.fsync(f.fileno())
    os.replace(temporary, path)
  finally:
    Path(temporary).unlink(missing_ok=True)


def load_json(path: Path, default=None):
  try:
    return json.loads(path.read_text())
  except (OSError, ValueError):
    return default


@contextlib.contextmanager
def store_lock(root: Path):
  directory = store_root(root)
  directory.mkdir(parents=True, exist_ok=True)
  with (directory / '.lock').open('a') as f:
    fcntl.flock(f, fcntl.LOCK_EX)
    yield


def groups(root: Path) -> dict[str, list[Path]]:
  found = {}
  if root.is_dir():
    for path in root.iterdir():
      pair = split_segment(path.name)
      if pair and path.is_dir() and not path.is_symlink():
        found.setdefault(pair[0], []).append(path)
  return {k: sorted(v, key=lambda p: split_segment(p.name)[1]) for k, v in sorted(found.items())}


def fingerprint(path: Path) -> dict:
  s = path.stat()
  return {'size': s.st_size, 'mtimeNs': s.st_mtime_ns, 'ctimeNs': s.st_ctime_ns, 'inode': s.st_ino, 'device': s.st_dev}


def files_in(path: Path) -> dict:
  return {p.name: fingerprint(p) for p in sorted(path.iterdir()) if p.is_file() and not p.is_symlink()}


def is_locked(paths: list[Path]) -> bool:
  return any(any(p.name.endswith('.lock') for p in d.iterdir()) for d in paths)


def pin_path(root: Path, route: str) -> Path:
  return store_root(root) / 'routes' / route_name(route) / 'pin.json'


def pinned(root: Path, route: str) -> bool:
  # Even an unreadable pin must prevent automatic removal.
  return pin_path(root, route).exists()


def set_pin(root: Path, route: str, enabled: bool, note: str = '') -> dict:
  route_name(route)
  with store_lock(root):
    if route not in groups(root):
      raise ValueError('route_not_found')
    path = pin_path(root, route)
    if enabled:
      atomic_json(path, {'route': route, 'pinnedAtNs': time.time_ns(), 'note': note[:2000]})
    else:
      path.unlink(missing_ok=True)
  return {'route': route, 'pinned': enabled}


def segment_record(root: Path, path: Path, inspect: bool = False, settle_seconds: float = 5., should_stop=None) -> tuple[dict, bool]:
  names = files_in(path)
  cache = store_root(root) / 'segments' / (path.name + '.json')
  old = load_json(cache, {})
  locked = any(name.endswith('.lock') for name in names)
  changing = any(time.time_ns() - f['mtimeNs'] < settle_seconds * 1e9 for f in names.values())
  record = {'segment': split_segment(path.name)[1], 'directory': str(path), 'files': names, 'schema': SCHEMA, 'parserId': PARSER_ID,
            'status': 'open' if locked or changing else 'unscanned'}
  if (old.get('schema') == SCHEMA and old.get('parserId') == PARSER_ID and old.get('files') == names
      and old.get('status') == 'indexed' and not locked and not changing):
    return old, False
  scanned = False
  if inspect and not locked and not changing:
    from openpilot.system.loggerd.drive_index import inspect_log
    name = next((n for n in LOG_NAMES if n in names), None)
    try:
      record['log'] = inspect_log(path / name, should_stop) if name else {'integrity': 'missing', 'issues': ['missing_rlog_and_qlog']}
    except InterruptedError:
      record['status'] = 'paused'
      return record, False
    if names != files_in(path):
      record['status'] = 'open'
      record.pop('log', None)
    else:
      record['status'], scanned = 'indexed', True
      record['indexedAtNs'] = time.time_ns()
      atomic_json(cache, record)
  return record, scanned


def build_manifest(root: Path, route: str, segments: list[dict]) -> dict:
  numbers = [s['segment'] for s in segments]
  missing = sorted(set(range(max(numbers) + 1)) - set(numbers)) if numbers else []
  logs = [s.get('log', {}) for s in segments]
  complete = bool(segments and not missing and all(s['status'] == 'indexed' for s in segments)
                  and all(l.get('kind') == 'rlog' and l.get('integrity') == 'complete' for l in logs)
                  and logs[0].get('sentinels', {}).get('startOfRoute') and logs[-1].get('sentinels', {}).get('endOfRoute'))
  wall = next((l.get('identity', {}).get('wallTimeNs') for l in logs if l.get('identity', {}).get('wallTimeNs')), None)
  try:
    started = datetime.datetime.fromtimestamp(wall / 1e9, datetime.UTC).isoformat() if wall else None
  except (ValueError, OverflowError, OSError):
    started = None
  return {'schema': SCHEMA, 'route': route, 'root': str(root), 'updatedAtNs': time.time_ns(), 'segments': segments,
          'recordedStartUtc': started,
          'missingSegments': missing, 'completeLogs': complete, 'pinned': pinned(root, route),
          'analysisReady': bool(complete and all(l.get('analysisReady') for l in logs)),
          'hasAnyVideo': any(any(n in s['files'] for n in VIDEO_NAMES) for s in segments),
          'provenance': load_json(store_root(root) / 'routes' / route / 'provenance.json', {'status': 'unknown_historical'}),
          'events': [dict(e, segment=s['segment']) for s in segments for e in s.get('log', {}).get('events', [])]}


def scan(root: Path, route: str | None = None, limit: int | None = None, settle_seconds: float = 5., should_stop=None) -> dict:
  root = root.resolve()
  if route:
    route_name(route)
  all_groups = groups(root)
  if route and route not in all_groups:
    raise ValueError('route_not_found')
  examined, manifests = 0, []
  for name, paths in reversed(list(all_groups.items())):
    if route and name != route:
      continue
    with store_lock(root):
      segments = []
      for path in paths:
        inspect = (limit is None or examined < limit) and not (should_stop and should_stop())
        record, changed = segment_record(root, path, inspect, settle_seconds, should_stop)
        examined += changed
        segments.append(record)
      manifest = build_manifest(root, name, segments)
      atomic_json(store_root(root) / 'routes' / name / 'manifest.json', manifest)
      manifests.append({'route': name, 'segments': len(segments), 'completeLogs': manifest['completeLogs'],
                        'analysisReady': manifest['analysisReady'], 'pinned': manifest['pinned'], 'hasAnyVideo': manifest['hasAnyVideo'],
                        'missingSegments': manifest['missingSegments'], 'events': len(manifest['events'])})
    if should_stop and should_stop():
      break
    if limit is not None and limit > 0 and examined >= limit:
      break
  return {'root': str(root), 'indexedSegments': examined, 'routes': manifests}


def manifest_for(root: Path, route: str) -> dict:
  route_name(route)
  root = root.resolve()
  paths = groups(root).get(route)
  if not paths:
    raise ValueError('route_not_found')
  with store_lock(root):
    return build_manifest(root, route, [segment_record(root, p)[0] for p in paths])


def list_routes(root: Path) -> list[dict]:
  root = root.resolve()
  result = []
  with store_lock(root):
    for route, paths in reversed(list(groups(root).items())):
      m = build_manifest(root, route, [segment_record(root, p)[0] for p in paths])
      result.append({k: m[k] for k in ('route', 'recordedStartUtc', 'completeLogs', 'analysisReady', 'pinned', 'hasAnyVideo', 'missingSegments')}
                    | {'segments': len(m['segments']), 'events': len(m['events']),
                       'indexedSegments': sum(s['status'] == 'indexed' for s in m['segments']),
                       'openSegments': sum(s['status'] == 'open' for s in m['segments']),
                       'movingSamples': sum(s.get('log', {}).get('activity', {}).get('movingSamples', 0) for s in m['segments'])
                       if any(s['status'] == 'indexed' for s in m['segments']) else None})
  return result


def storage_status(root: Path) -> dict:
  root = root.resolve()
  usage = shutil.disk_usage(root)
  routes = groups(root)
  worker = load_json(store_root(root) / 'worker-status.json', {})
  return {'root': str(root), 'indexRoot': str(store_root(root)), 'routes': len(routes),
          'segments': sum(map(len, routes.values())), 'freeBytes': usage.free, 'freePercent': 100 * usage.free / usage.total,
          'retentionPressure': usage.free < 5 * 1024 ** 3 or usage.free / usage.total < .10,
          'pinnedRoutes': [r for r in routes if pinned(root, r)], 'worker': worker,
          'workerStatusAgeSeconds': (time.time_ns() - worker['updatedAtNs']) / 1e9 if worker.get('updatedAtNs') else None,
          'retentionStatus': load_json(store_root(root) / 'retention-status.json', {})}


def sha256_file(path: Path, should_stop=None) -> str:
  digest = hashlib.sha256()
  with path.open('rb') as f:
    for block in iter(lambda: f.read(1024 * 1024), b''):
      if should_stop and should_stop():
        raise InterruptedError('hashing_paused')
      digest.update(block)
  return digest.hexdigest()


def export_route(root: Path, route: str, output: Path, include_video: bool = False) -> dict:
  root, output = root.resolve(), output.resolve()
  route_name(route)
  if output == root or root in output.parents or output == store_root(root) or store_root(root) in output.parents:
    raise ValueError('export_must_be_outside_log_and_index_roots')
  if output.exists():
    raise ValueError('export_destination_exists')
  scan(root, route)
  with store_lock(root):
    paths = groups(root).get(route)
    if not paths or is_locked(paths):
      raise ValueError('route_missing_or_open')
    records = [segment_record(root, p)[0] for p in paths]
    if any(record['status'] == 'open' for record in records):
      raise ValueError('route_missing_or_open')
    snapshots = {p: files_in(p) for p in paths}
    source_files = [(d, n, f) for d, files in snapshots.items() for n, f in files.items()
                    if n in LOG_NAMES or (include_video and n in VIDEO_NAMES)]
    if not any(n in LOG_NAMES for _, n, _ in source_files):
      raise ValueError('no_log_files')
    output.parent.mkdir(parents=True, exist_ok=True)
    required = sum(f['size'] for _, _, f in source_files)
    if shutil.disk_usage(output.parent).free < required + 512 * 1024 * 1024:
      raise ValueError('insufficient_export_space')
    temporary = Path(tempfile.mkdtemp(prefix='.' + output.name + '-', dir=output.parent))
    try:
      checksums = {}
      for directory, name, before in source_files:
        source, dest = directory / name, temporary / directory.name / name
        dest.parent.mkdir(exist_ok=True)
        shutil.copy2(source, dest)
        digest = sha256_file(dest)
        if digest != sha256_file(source) or fingerprint(source) != before:
          raise ValueError('source_changed_during_export')
        checksums[str(dest.relative_to(temporary))] = digest
      metadata = store_root(root) / 'routes' / route
      if metadata.exists():
        shutil.copytree(metadata, temporary / 'metadata')
        provenance = load_json(metadata / 'provenance.json', {})
        snapshot = provenance.get('sourceSnapshot')
        if snapshot and Path(snapshot).name == snapshot:
          source = store_root(root) / 'snapshots' / snapshot
          if source.is_file():
            shutil.copy2(source, temporary / 'metadata' / snapshot)
      replay_segments, unsupported = {'rlog': [], 'qlog': []}, []
      for path, names in snapshots.items():
        segment = split_segment(path.name)[1]
        if 'rlog.zst' in names:
          replay_segments['rlog'].append(segment)
        elif 'qlog.zst' in names:
          replay_segments['qlog'].append(segment)
        else:
          unsupported.append({'segment': segment, 'reason': 'replay_reader_requires_zst'})
      atomic_json(temporary / 'replay-manifest.json', [{'root': str(output), 'route': route,
                  'segments': segments, 'logType': kind, 'source': 'local-export'}
                  for kind, segments in replay_segments.items() if segments])
      verified_files = sum(p.is_file() for p in temporary.rglob('*')) + 1
      atomic_json(temporary / 'export.json', {'route': route, 'sourceRoot': str(root), 'includeVideo': include_video,
                  'files': verified_files, 'logFiles': sum(n in LOG_NAMES for _, n, _ in source_files),
                  'bytes': required, 'exportedAtNs': time.time_ns(), 'replayUnsupportedSegments': unsupported,
                  'completeLogs': build_manifest(root, route, records)['completeLogs']})
      for metadata_file in temporary.rglob('*'):
        if metadata_file.is_file():
          name = str(metadata_file.relative_to(temporary))
          if name not in checksums:
            checksums[name] = sha256_file(metadata_file)
      atomic_json(temporary / 'checksums.json', checksums)
      if groups(root).get(route) != paths or is_locked(paths) or any(files_in(d) != files for d, files in snapshots.items()):
        raise ValueError('route_changed_during_export')
      os.rename(temporary, output)
    finally:
      if temporary.exists():
        shutil.rmtree(temporary)
  return {'route': route, 'output': str(output), 'files': len(checksums), 'bytes': required}
