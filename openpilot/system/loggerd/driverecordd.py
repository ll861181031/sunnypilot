"""Observe new routes; index closed logs only while the manager is offroad."""

import signal
import threading
import time
from pathlib import Path

from openpilot.common.hardware.hw import Paths
from openpilot.common.params import Params
from openpilot.common.swaglog import cloudlog
from openpilot.system.loggerd.drive_provenance import capture, hash_artifacts_offroad, observe_configuration
from openpilot.system.loggerd.drive_store import atomic_json, groups, scan, store_lock, store_root


def step(root: Path, params, repo: Path, startup_wall_ns: int, stop=None):
  offroad = params.get_bool('IsOffroad')
  current = params.get('CurrentRoute')
  paths = groups(root).get(current, [])
  if not offroad and paths and max(p.stat().st_mtime_ns for p in paths) >= startup_wall_ns - 30_000_000_000:
    with store_lock(root):
      capture(root, current, params, repo)
      observe_configuration(root, current, params)
  result = {'updatedAtNs': time.time_ns(), 'offroad': offroad, 'currentRoute': current, 'indexedSegments': 0}
  if offroad:
    def interrupted():
      return bool((stop and stop()) or not params.get_bool('IsOffroad'))
    result.update(scan(root, limit=4, should_stop=interrupted))
    if not interrupted():
      hash_artifacts_offroad(root, interrupted)
  atomic_json(store_root(root) / 'worker-status.json', result)
  return result


def main():
  params, exit_event = Params(), threading.Event()
  signal.signal(signal.SIGTERM, lambda *_: exit_event.set())
  signal.signal(signal.SIGINT, lambda *_: exit_event.set())
  root, repo, startup = Path(Paths.log_root()), Path(__file__).resolve().parents[3], time.time_ns()
  while not exit_event.is_set():
    try:
      step(root, params, repo, startup, exit_event.is_set)
    except Exception:
      cloudlog.exception('drive record indexing failed')
    exit_event.wait(10)


if __name__ == '__main__':
  main()
