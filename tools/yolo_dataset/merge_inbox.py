#!/usr/bin/env python3
"""Stage 4: merge the labeler's uploads back into the batch.

The Windows labeler uploads an incremental slice of its work over SFTP::

    /home/lsr/yolo_label_inbox/<batch>/
      labels/<id>.txt        only the files touched since the last sync
      review_state.json      {"<id>": {"reviewed", "edited", "updated"}}  (full)
      sync_manifest.json     informational: last_sync / pending / counts

Merging is validated before anything is written: an id that is not in the batch,
a malformed line, an out-of-range class or coordinate rejects the *whole* file
for that id (its current labels stay untouched) and is reported. Changed files are
backed up to ``labels_backup/<timestamp>/`` first, so a bad merge is always
reversible with a plain copy. Empty label files are legitimate negatives.

    .venv/bin/python tools/yolo_dataset/merge_inbox.py --batch-dir /data/yolo_dataset/batch_001
    .venv/bin/python tools/yolo_dataset/merge_inbox.py --batch-dir ... --dry-run
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common

DEFAULT_INBOX_ROOT = "/home/lsr/yolo_label_inbox"


def read_label_file(path: str, num_classes: int) -> list[tuple[int, tuple[float, ...]]]:
  """Parse and validate a YOLO label file; raises ValueError on the first problem."""
  with open(path, encoding="utf-8") as f:
    raw = f.read()
  parsed = []
  for number, line in enumerate(raw.splitlines(), start=1):
    if not line.strip():
      continue
    try:
      parsed.append(common.parse_label_line(line, num_classes))
    except ValueError as error:
      raise ValueError(f"line {number}: {error}") from error
  return parsed


def to_lines(parsed: list[tuple[int, tuple[float, ...]]]) -> list[str]:
  return [common.format_yolo_line(cls, (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2))
          for cls, (cx, cy, w, h) in parsed]


def normalized_text(path: str, num_classes: int) -> str | None:
  """Canonical form of a label file (six decimals, single trailing newline).

  Returns None when the file does not exist or cannot be parsed, which lets the
  caller treat "unparsable current file" as "different" and merge over it.
  """
  if not os.path.isfile(path):
    return ""
  try:
    parsed = read_label_file(path, num_classes)
  except ValueError:
    return None
  return "\n".join(to_lines(parsed)) + ("\n" if parsed else "")


def count_classes(path: str, num_classes: int) -> list[int]:
  counts = [0] * num_classes
  if not os.path.isfile(path):
    return counts
  try:
    parsed = read_label_file(path, num_classes)
  except ValueError:
    return counts
  for cls, _box in parsed:
    counts[cls] += 1
  return counts


def merge(batch_dir: str, inbox: str, dry_run: bool, backup_root: str | None = None,
          allow_inbox_mismatch: bool = False) -> dict:
  dataset_dir = os.path.abspath(batch_dir)
  dataset_path = os.path.join(dataset_dir, "dataset.json")
  if not os.path.isfile(dataset_path):
    raise FileNotFoundError(f"not a dataset: {dataset_path}")
  dataset = common.read_json(dataset_path, {})
  classes = dataset.get("classes") or []
  num_classes = len(classes)
  valid_ids = sorted({image["id"] for image in dataset.get("images") or []})

  inbox = os.path.abspath(inbox)
  # Guard rail: sample ids are six-digit counters, so an inbox belonging to another
  # batch would collide id-for-id and silently overwrite real labels. The inbox
  # directory must be named after the batch unless the caller insists.
  batch_name = str(dataset.get("batch") or "")
  inbox_name = os.path.basename(inbox.rstrip("/"))
  if batch_name and inbox_name and inbox_name != batch_name and not allow_inbox_mismatch:
    detail = f"inbox {inbox!r} does not belong to batch {batch_name!r}"
    raise ValueError(f"{detail} (pass --force-inbox to merge anyway)")
  inbox_labels = os.path.join(inbox, "labels")
  manifest = common.read_json(os.path.join(inbox, "sync_manifest.json"), {})
  before = [0] * num_classes
  for sample_id in valid_ids:
    counts = count_classes(os.path.join(dataset_dir, "labels", f"{sample_id}.txt"), num_classes)
    before = [a + b for a, b in zip(before, counts, strict=True)]
  report = {
    "batch": dataset.get("batch"),
    "inbox": inbox,
    "dry_run": dry_run,
    "inbox_present": os.path.isdir(inbox),
    "manifest_last_sync": manifest.get("last_sync"),
    "manifest_pending": manifest.get("pending"),
    "files": 0,
    "files_merged": 0,
    "files_rejected": 0,
    "files_unchanged": 0,
    "unknown_ids": [],
    "rejected": [],
    "reviewed": 0,
    "edited": 0,
    "empty_negative_files": 0,
    "backup_dir": None,
    "class_counts_before": before,
    "class_counts_after": list(before),
  }
  if not os.path.isdir(inbox_labels):
    return report

  incoming: dict[str, str] = {}
  deltas = [0] * num_classes
  for name in sorted(os.listdir(inbox_labels)):
    if not name.endswith(".txt"):
      continue
    sample_id = os.path.splitext(name)[0]
    report["files"] += 1
    if sample_id not in set(valid_ids):
      report["unknown_ids"].append(sample_id)
      continue
    try:
      parsed = read_label_file(os.path.join(inbox_labels, name), num_classes)
    except ValueError as error:
      report["rejected"].append({"id": sample_id, "reason": str(error)})
      continue
    if not parsed:
      report["empty_negative_files"] += 1
    text = "\n".join(to_lines(parsed)) + ("\n" if parsed else "")
    current_path = os.path.join(dataset_dir, "labels", f"{sample_id}.txt")
    if normalized_text(current_path, num_classes) == text:
      report["files_unchanged"] += 1
      continue
    incoming[sample_id] = text
    old = count_classes(current_path, num_classes)
    new = [0] * num_classes
    for cls, _box in parsed:
      new[cls] += 1
    deltas = [d + n - o for d, n, o in zip(deltas, new, old, strict=True)]
  report["files_merged"] = len(incoming)
  report["files_rejected"] = len(report["rejected"])

  # Review bookkeeping from the labeler, cross-checked against real content:
  # "edited" also means "differs from the auto pre-labels", which we can verify.
  inbox_review = common.read_json(os.path.join(inbox, "review_state.json"), {})
  review_path = os.path.join(dataset_dir, "review_state.json")
  review = common.read_json(review_path, {})
  for sample_id in valid_ids:
    review.setdefault(sample_id, {"reviewed": False, "edited": False, "updated": 0})
  for sample_id, entry in inbox_review.items():
    if sample_id not in set(valid_ids) or not isinstance(entry, dict):
      continue
    state = review[sample_id]
    state["reviewed"] = bool(entry.get("reviewed", state.get("reviewed", False)))
    state["edited"] = bool(entry.get("edited", state.get("edited", False)))
    state["updated"] = int(entry.get("updated") or 0)
  for sample_id in valid_ids:
    auto_path = os.path.join(dataset_dir, "auto_labels", f"{sample_id}.txt")
    auto_text = normalized_text(auto_path, num_classes) or ""
    label_text = incoming.get(sample_id)
    if label_text is None:
      label_text = normalized_text(os.path.join(dataset_dir, "labels", f"{sample_id}.txt"),
                                   num_classes) or ""
    if label_text != auto_text:
      review[sample_id]["edited"] = True
  report["reviewed"] = sum(1 for state in review.values() if state.get("reviewed"))
  report["edited"] = sum(1 for state in review.values() if state.get("edited"))

  if not dry_run:
    if incoming:
      stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
      backup_dir = os.path.join(backup_root or os.path.join(dataset_dir, "labels_backup"), stamp)
      os.makedirs(backup_dir, exist_ok=True)
      for sample_id in incoming:
        current_path = os.path.join(dataset_dir, "labels", f"{sample_id}.txt")
        if os.path.isfile(current_path):
          shutil.copy2(current_path, os.path.join(backup_dir, f"{sample_id}.txt"))
      for sample_id, text in incoming.items():
        common.write_labels(os.path.join(dataset_dir, "labels", f"{sample_id}.txt"),
                            text.splitlines())
      report["backup_dir"] = backup_dir
    common.write_json(review_path, review)
  report["class_counts_after"] = [b + d for b, d in zip(before, deltas, strict=True)]
  return report


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
  parser.add_argument("--batch-dir", default=os.path.join(common.DEFAULT_WORK_ROOT, "batch_001"))
  parser.add_argument("--inbox", default="", help="default: <inbox-root>/<batch>")
  parser.add_argument("--inbox-root", default=DEFAULT_INBOX_ROOT)
  parser.add_argument("--batch", default="", help="batch name; default from dataset.json")
  parser.add_argument("--dry-run", action="store_true")
  parser.add_argument("--force-inbox", action="store_true",
                      help="merge even though the inbox folder is not named after the batch")
  parser.add_argument("--json", action="store_true")
  args = parser.parse_args(argv)

  dataset_path = os.path.join(os.path.abspath(args.batch_dir), "dataset.json")
  if not os.path.isfile(dataset_path):
    print(f"not a dataset: {dataset_path}", file=sys.stderr)
    return 1
  batch = args.batch or common.read_json(dataset_path, {}).get("batch") or "batch_001"
  inbox = args.inbox or os.path.join(args.inbox_root, batch)

  try:
    report = merge(args.batch_dir, inbox, args.dry_run,
                   allow_inbox_mismatch=args.force_inbox)
  except ValueError as error:
    print(f"refusing to merge: {error}", file=sys.stderr)
    return 1
  names = [c["name"] for c in common.read_json(dataset_path, {}).get("classes", [])]
  if args.json:
    import json
    print(json.dumps(report, ensure_ascii=False, indent=2))
  else:
    mode = "dry-run" if report["dry_run"] else "merge"
    print(f"batch={report['batch']} inbox={report['inbox']} ({mode})")
    if not report["inbox_present"]:
      print("  inbox not present - nothing uploaded yet")
    print(f"  manifest: last_sync={report['manifest_last_sync']} pending={report['manifest_pending']}")
    files = f"files={report['files']} merged={report['files_merged']}"
    rest = f"unchanged={report['files_unchanged']} rejected={report['files_rejected']}"
    print(f"  {files} {rest} negatives={report['empty_negative_files']}")
    if report["unknown_ids"]:
      print(f"  unknown ids ({len(report['unknown_ids'])}): {report['unknown_ids'][:10]}")
    for rejected in report["rejected"]:
      print(f"  REJECTED {rejected['id']}: {rejected['reason']}")
    print(f"  reviewed={report['reviewed']} edited={report['edited']}")
    before, after = report["class_counts_before"], report["class_counts_after"]
    for index, name in enumerate(names):
      if before[index] or after[index]:
        delta = after[index] - before[index]
        marker = f" ({delta:+d})" if delta else ""
        print(f"    {index:2d} {name:18s} {before[index]:7d} -> {after[index]:7d}{marker}")
    if report["backup_dir"]:
      print(f"  backup: {report['backup_dir']}")
    elif report["dry_run"]:
      print("  dry-run: nothing written")
  return 0 if not report["rejected"] else 0


if __name__ == "__main__":
  raise SystemExit(main())
