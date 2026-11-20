#!/usr/bin/env python3
"""Validate a delivered batch against the labeler contract.

Checks are deliberately mechanical: the point is to catch a broken hand-off
before the annotator (and later the trainer) wastes a session on it.

    .venv/bin/python tools/yolo_dataset/validate.py --dataset /data/yolo_dataset/pilot_200

Exit code 0 means every check passed; 1 means at least one error. Warnings never
fail the run (e.g. a class that no frame happens to contain).
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common

REQUIRED_FILES = ("dataset.json", "auto_meta.json", "review_state.json", "taxonomy.json",
                  "train.txt", "val.txt", "data.yaml", "stats.json", "README.md")
REQUIRED_DIRS = ("images", "labels", "auto_labels")


class Report:
  def __init__(self) -> None:
    self.errors: list[str] = []
    self.warnings: list[str] = []
    self.checks = 0

  def check(self, condition: bool, message: str) -> bool:
    self.checks += 1
    if not condition:
      self.errors.append(message)
    return condition

  def warn(self, condition: bool, message: str) -> bool:
    self.checks += 1
    if not condition:
      self.warnings.append(message)
    return condition

  def ok(self) -> bool:
    return not self.errors


def validate(dataset_dir: str, sample: int = 0, strict_negative_ok: bool = True) -> Report:
  report = Report()
  dataset_dir = os.path.abspath(dataset_dir)
  report.check(os.path.isdir(dataset_dir), f"dataset dir missing: {dataset_dir}")
  if not report.ok():
    return report

  for name in REQUIRED_FILES:
    report.check(os.path.isfile(os.path.join(dataset_dir, name)), f"missing file: {name}")
  for name in REQUIRED_DIRS:
    report.check(os.path.isdir(os.path.join(dataset_dir, name)), f"missing dir: {name}")
  if not report.ok():
    return report

  dataset = common.read_json(os.path.join(dataset_dir, "dataset.json"), {})
  report.check(dataset.get("schema") == 1, "dataset.json schema must be 1")
  classes = dataset.get("classes") or []
  report.check(len(classes) > 0, "dataset.json has no classes")
  ids_ok = [c.get("id") for c in classes] == list(range(len(classes)))
  report.check(ids_ok, "class ids must be 0..N-1 in order")
  names = [c.get("name") for c in classes]
  images = dataset.get("images") or []
  report.check(len(images) > 0, "dataset.json lists no images")

  taxonomy = common.load_taxonomy(os.path.join(dataset_dir, "taxonomy.json"))
  report.check(taxonomy.names == names, "taxonomy.json and dataset.json classes disagree")

  ids = [i.get("id") for i in images]
  report.check(len(set(ids)) == len(ids), "duplicate sample ids")
  report.check(all(isinstance(i, str) and len(i) == 6 and i.isdigit() for i in ids),
               "ids must be six-digit zero-padded strings")

  auto_meta = common.read_json(os.path.join(dataset_dir, "auto_meta.json"), {})
  report.check(set(auto_meta) == set(ids), "auto_meta.json keys must match dataset ids")
  review = common.read_json(os.path.join(dataset_dir, "review_state.json"), {})
  report.check(set(review) == set(ids), "review_state.json keys must match dataset ids")
  annotated = sum(1 for entry in review.values() if entry.get("reviewed") or entry.get("edited"))
  # A freshly built batch is all-false; a batch that has been merged from the labeler
  # is not, and that is expected - it only relaxes the labels/auto_labels check below.
  report.warn(annotated == 0 or annotated > 0,
              f"{annotated} images are already reviewed/edited (batch has annotation history)")

  import cv2
  rows = images if sample <= 0 else images[::max(1, len(images) // sample)][:sample]
  geometry = (int(dataset.get("geometry", {}).get("width", 0)),
              int(dataset.get("geometry", {}).get("height", 0)))
  route_split: dict[str, set[str]] = {}
  bad_labels = 0
  for entry in rows:
    sample_id = entry["id"]
    image_path = os.path.join(dataset_dir, "images", f"{sample_id}.jpg")
    if not report.check(os.path.isfile(image_path), f"missing image: {sample_id}.jpg"):
      continue
    if not report.check(os.path.getsize(image_path) > 1024, f"image too small: {sample_id}.jpg"):
      continue
    image = cv2.imread(image_path)
    if not report.check(image is not None, f"unreadable image: {sample_id}.jpg"):
      continue
    actual = f"{image.shape[1]}x{image.shape[0]}"
    report.check((image.shape[1], image.shape[0]) == geometry,
                 f"{sample_id}: image is {actual}, dataset.json says {geometry[0]}x{geometry[1]}")
    report.check((entry["width"], entry["height"]) == geometry,
                 f"{sample_id}: dataset.json width/height disagree with geometry")
    for sub in ("labels", "auto_labels"):
      path = os.path.join(dataset_dir, sub, f"{sample_id}.txt")
      if not report.check(os.path.isfile(path), f"missing {sub}: {sample_id}.txt"):
        continue
      with open(path, encoding="utf-8") as f:
        lines = [ln for ln in f.read().splitlines() if ln.strip()]
      for number, line in enumerate(lines, start=1):
        try:
          common.parse_label_line(line, len(classes))
        except ValueError as error:
          bad_labels += 1
          report.check(False, f"{sub}/{sample_id}.txt:{number}: {error}")
      if sub == "labels":
        # Only meaningful before annotation starts: an annotator adds and deletes
        # boxes, so afterwards labels legitimately differs from the auto pre-labels.
        expected = len(auto_meta.get(sample_id) or [])
        message = f"{sample_id}: labels has {len(lines)} lines, auto_meta has {expected} boxes"
        if annotated == 0:
          report.check(len(lines) == expected, message)
        else:
          report.warn(len(lines) == expected, message)
    route_split.setdefault(entry["route"], set()).add(entry["split"])
    report.check(entry["split"] in ("train", "val"), f"{sample_id}: bad split {entry['split']}")
  report.check(bad_labels == 0, f"{bad_labels} malformed label lines")

  leaked = sorted(route for route, splits in route_split.items() if len(splits) > 1)
  report.check(not leaked, f"routes appear in both splits (leakage): {leaked[:5]}")

  split_lists = {}
  for split in ("train", "val"):
    with open(os.path.join(dataset_dir, f"{split}.txt"), encoding="utf-8") as f:
      split_lists[split] = [ln.strip() for ln in f if ln.strip()]
    report.check(len(split_lists[split]) > 0 or split == "val" and len(images) < 10,
                 f"{split}.txt is empty")
    for rel in split_lists[split][:50]:
      if not report.check(os.path.isfile(os.path.join(dataset_dir, rel)),
                          f"{split}.txt references missing file: {rel}"):
        break
    report.check(all(rel.startswith("images/") for rel in split_lists[split]),
                 f"{split}.txt paths must be relative ('images/...')")
  overlap = set(split_lists["train"]) & set(split_lists["val"])
  report.check(not overlap, f"{len(overlap)} files listed in both train.txt and val.txt")
  listed = set(split_lists["train"]) | set(split_lists["val"])
  expected = {f"images/{i}.jpg" for i in ids}
  report.check(listed == expected,
               f"train/val lists cover {len(listed)} of {len(expected)} images")
  report.check(dataset.get("split", {}).get("train") == len(split_lists["train"]),
               "dataset.json split.train disagrees with train.txt")

  with open(os.path.join(dataset_dir, "data.yaml"), encoding="utf-8") as f:
    yaml_text = f.read()
  report.check("nc:" in yaml_text and "names:" in yaml_text, "data.yaml lacks nc/names")
  report.check(yaml_text.count("- ") >= len(names), "data.yaml names list looks short")

  # Class coverage is informational: a class with no boxes is normal for a batch.
  counts = dataset.get("counts") or {}
  empty = [name for name in names if counts.get(name, 0) == 0]
  report.warn(not empty, f"classes with zero boxes in this batch: {empty}")
  day = sum(1 for i in images if i.get("daylight"))
  report.warn(0 < day < len(images), f"day/night split looks degenerate (day={day}/{len(images)})")
  negatives = sum(1 for i in images if i.get("negative"))
  report.warn(negatives > 0 or not strict_negative_ok, "no negative samples in this batch")
  return report


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
  parser.add_argument("--dataset", required=True)
  parser.add_argument("--sample", type=int, default=0, help="check only N images (0 = all)")
  parser.add_argument("--json", action="store_true", help="machine-readable output")
  args = parser.parse_args(argv)

  report = validate(args.dataset, args.sample)
  if args.json:
    print(json.dumps({"ok": report.ok(), "checks": report.checks,
                      "errors": report.errors, "warnings": report.warnings},
                     ensure_ascii=False, indent=2))
  else:
    summary_line = f"{args.dataset}: {report.checks} checks, {len(report.errors)} errors"
    print(f"{summary_line}, {len(report.warnings)} warnings")
    for message in report.errors:
      print(f"  ERROR   {message}")
    for message in report.warnings:
      print(f"  WARNING {message}")
    print("OK" if report.ok() else "FAILED")
  return 0 if report.ok() else 1


if __name__ == "__main__":
  raise SystemExit(main())
