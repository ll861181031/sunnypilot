#!/usr/bin/env python3
"""Merge labeler inboxes into their datasets, skipping unchanged manifests.

The labeler writes ``sync_manifest.json`` on every upload. A merge over a 12k
batch takes several CPU seconds, so this watcher remembers the last processed
``last_sync`` per batch and does nothing when nothing new arrived. Use --force
to merge regardless.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

INBOX_ROOT = Path("/home/lsr/yolo_label_inbox")
DATASET_ROOT = Path("/data/yolo_dataset")
MERGE = Path("/data/sunnypilot/tools/yolo_dataset/merge_inbox.py")
STATE = INBOX_ROOT / ".watch_state.json"


def _load_state() -> dict:
  try:
    return json.loads(STATE.read_text(encoding="utf-8"))
  except (OSError, ValueError):
    return {}


def _save_state(state: dict) -> None:
  STATE.parent.mkdir(parents=True, exist_ok=True)
  fd, tmp = tempfile.mkstemp(dir=str(STATE.parent), prefix=".watch_state.")
  with os.fdopen(fd, "w", encoding="utf-8") as handle:
    json.dump(state, handle, indent=1, sort_keys=True)
  os.replace(tmp, STATE)


def main(argv=None) -> int:
  parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
  parser.add_argument("--force", action="store_true", help="merge even when last_sync did not change")
  args = parser.parse_args(argv)
  if not MERGE.is_file():
    print(f"merge_inbox.py not found: {MERGE}", file=sys.stderr)
    return 2

  state = _load_state()
  changed = False
  for inbox in sorted(INBOX_ROOT.iterdir()) if INBOX_ROOT.is_dir() else []:
    if not inbox.is_dir():
      continue
    manifest_path = inbox / "sync_manifest.json"
    dataset = DATASET_ROOT / inbox.name
    if not manifest_path.is_file() or not (dataset / "dataset.json").is_file():
      continue
    try:
      manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
      print(f"[watch_inbox] {inbox.name}: unreadable manifest ({error})", file=sys.stderr)
      continue
    stamp = str(manifest.get("last_sync") or "")
    if not args.force and stamp and state.get(inbox.name) == stamp:
      continue
    result = subprocess.run([sys.executable, str(MERGE), "--batch-dir", str(dataset),
                             "--inbox", str(inbox)],
                            capture_output=True, text=True)
    if result.returncode != 0:
      print(f"[watch_inbox] merge failed for {inbox.name}:\n{result.stderr}", file=sys.stderr)
      continue
    tail = (result.stdout or "").strip().splitlines()
    print(f"[watch_inbox] {inbox.name}: {tail[-1] if tail else 'merged'}")
    if stamp:
      state[inbox.name] = stamp
      changed = True
  if changed:
    _save_state(state)
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
