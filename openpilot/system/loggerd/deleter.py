#!/usr/bin/env python3
import os
import time
import shutil
import threading
import tempfile
from pathlib import Path
from openpilot.common.hardware.hw import Paths
from openpilot.common.swaglog import cloudlog
from openpilot.system.loggerd.config import get_available_bytes, get_available_percent
from openpilot.system.loggerd.uploader import listdir_by_creation
from openpilot.system.loggerd.xattr_cache import getxattr
from openpilot.system.loggerd.drive_store import (atomic_json, files_in, fingerprint, groups, is_locked, pinned,
                                                sha256_file, split_segment, store_lock, store_root)

MIN_BYTES = 5 * 1024 * 1024 * 1024
MIN_PERCENT = 10

DELETE_LAST = ['boot', 'crash']

PRESERVE_ATTR_NAME = 'user.preserve'
PRESERVE_ATTR_VALUE = b'1'
PRESERVE_COUNT = 5
DELETE_GRACE_SECONDS = 120


def has_preserve_xattr(d: str, root: str | None = None) -> bool:
  return getxattr(os.path.join(root or Paths.log_root(), d), PRESERVE_ATTR_NAME) == PRESERVE_ATTR_VALUE


def get_preserved_segments(dirs_by_creation: list[str], root: str | None = None) -> set[str]:
  # skip deleting most recent N preserved segments (and their prior segment)
  preserved = set()
  for n, d in enumerate(filter(lambda name: has_preserve_xattr(name, root), reversed(dirs_by_creation))):
    if n == PRESERVE_COUNT:
      break
    date_str, _, seg_str = d.rpartition("--")

    # ignore non-segment directories
    if not date_str:
      continue
    try:
      seg_num = int(seg_str)
    except ValueError:
      continue

    # preserve segment and two prior
    for _seg_num in range(max(0, seg_num - 2), seg_num + 1):
      preserved.add(f"{date_str}--{_seg_num}")

  return preserved


def candidates(root: Path) -> list[list[Path]]:
  preserved = get_preserved_segments(listdir_by_creation(str(root)), str(root))
  routes = groups(root)
  ordered = sorted(routes, key=lambda route: (any(p.name in preserved for p in routes[route]), route))
  selections = [routes[route] for route in ordered if not pinned(root, route)]
  selections += [[root / name] for name in DELETE_LAST if (root / name).is_dir() and not (root / name).is_symlink()]
  return [paths for paths in selections if not is_locked(paths)
          and all(time.time_ns() - f['mtimeNs'] >= DELETE_GRACE_SECONDS * 1e9 for p in paths for f in files_in(p).values())]


def journal(root: Path, paths: list[Path], state: str, **details) -> None:
  record = {'wallTimeNs': time.time_ns(), 'state': state, 'segments': [p.name for p in paths], **details}
  atomic_json(store_root(root) / 'retention' / (paths[0].name + '.json'), record)
  atomic_json(store_root(root) / 'retention-status.json', record)


def remove_group(root: Path, paths: list[Path]) -> str:
  journal(root, paths, 'deleting')
  try:
    for path in paths:
      shutil.rmtree(path)
  except OSError:
    journal(root, paths, 'delete_failed', remaining=[p.name for p in paths if p.exists()])
    raise
  journal(root, paths, 'deleted')
  return str(paths[0])


def archive_group(root: Path, external: Path, paths: list[Path]) -> str:
  # Keep originals until every destination byte is verified. A failed archive is
  # never permission to delete the source; existing destinations are not merged.
  if any((external / p.name).exists() for p in paths):
    raise OSError('archive destination already exists')
  snapshots = {p: files_in(p) for p in paths}
  staging = Path(tempfile.mkdtemp(prefix='.drive-archive-', dir=external))
  published = []
  try:
    for source, files in snapshots.items():
      if any(p.is_symlink() or not p.is_file() for p in source.iterdir()):
        raise OSError('non-regular archive input')
      target = staging / source.name
      shutil.copytree(source, target)
      for name, before in files.items():
        copied = target / name
        if sha256_file(copied) != sha256_file(source / name) or fingerprint(source / name) != before:
          raise OSError('archive source changed or copy verification failed')
        with copied.open('rb') as f:
          os.fsync(f.fileno())
    if is_locked(paths) or any(files_in(p) != f for p, f in snapshots.items()):
      raise OSError('archive route is changing')
    pair = split_segment(paths[0].name)
    if pair:
      source_metadata = store_root(root) / 'routes' / pair[0]
      if source_metadata.exists():
        shutil.copytree(source_metadata, store_root(external) / 'routes' / pair[0], dirs_exist_ok=True)
      snapshot_dir = store_root(root) / 'snapshots'
      if snapshot_dir.exists():
        # Snapshots are content-addressed and small; never copy model weights.
        shutil.copytree(snapshot_dir, store_root(external) / 'snapshots', dirs_exist_ok=True)
    for source in paths:
      os.rename(staging / source.name, external / source.name)
      published.append(source.name)
    journal(root, paths, 'archived_verified', destination=str(external))
  except Exception:
    for name in published:
      os.rename(external / name, staging / name)
    raise
  finally:
    shutil.rmtree(staging)
  for source in paths:
    shutil.rmtree(source)
  journal(root, paths, 'moved', destination=str(external))
  return str(paths[0])


def deleter_step() -> tuple[bool, str | None]:
  if not (get_available_bytes(default=MIN_BYTES + 1) < MIN_BYTES or get_available_percent(default=MIN_PERCENT + 1) < MIN_PERCENT):
    return False, None
  root, external = Path(Paths.log_root()), Path(Paths.log_root_external())
  with store_lock(root):
    available = candidates(root)
    if not available:
      atomic_json(store_root(root) / 'retention-status.json', {'state': 'blocked', 'wallTimeNs': time.time_ns(),
                  'reason': 'all_remaining_routes_pinned_open_or_recent'})
      return True, None
    paths = available[0]
    try:
      if external.is_mount():
        with store_lock(external):
          needed = sum(f['size'] for p in paths for f in files_in(p).values())
          free = get_available_bytes(default=0, path_type='external')
          if free < MIN_BYTES + needed or get_available_percent(default=0, path_type='external') < MIN_PERCENT:
            external_candidates = candidates(external)
            if external_candidates:
              return True, remove_group(external, external_candidates[0])
            return True, None
          return True, archive_group(root, external, paths)
      cloudlog.info(f'deleting complete route group starting at {paths[0]}')
      return True, remove_group(root, paths)
    except OSError as exc:
      cloudlog.exception('route retention operation failed; remaining sources kept')
      journal(root, paths, 'operation_failed', reason=str(exc)[:300])
      return True, None


def deleter_thread(exit_event: threading.Event):
  while not exit_event.is_set():
    try:
      pressure, changed = deleter_step()
      exit_event.wait(.1 if pressure and changed else 30)
    except OSError:
      cloudlog.exception('retention scan failed')
      exit_event.wait(30)


def main():
  deleter_thread(threading.Event())


if __name__ == "__main__":
  main()
