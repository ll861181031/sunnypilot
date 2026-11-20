#!/usr/bin/env python3
"""Build a combined training dataset with globally unique image IDs.

The two batches both use 6-digit IDs (000001...). Ultralytics labels caches and
some filename-based keys can collide across them, which made the tricycle
metrics unusable. This builder creates ``b1_000001`` / ``n_000001`` names and
hardlinks the source images/labels so no image bytes are duplicated.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path


def link_or_copy(src: Path, dst: Path) -> None:
  if dst.exists():
    return
  try:
    os.link(src, dst)
  except OSError:
    shutil.copy2(src, dst)


def main(argv=None) -> int:
  ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
  ap.add_argument("--out", required=True)
  ap.add_argument("--source", action="append", required=True,
                  help="PREFIX=dataset_dir (repeatable)")
  args = ap.parse_args(argv)
  out = Path(args.out)
  images = out / "images"
  labels = out / "labels"
  images.mkdir(parents=True, exist_ok=True)
  labels.mkdir(parents=True, exist_ok=True)
  sources = []
  for spec in args.source:
    prefix, directory = spec.split("=", 1)
    sources.append((prefix, Path(directory).resolve()))
  classes = None
  tax = None
  all_images = []
  train_lines: list[str] = []
  val_lines: list[str] = []
  split_counts = {"train": 0, "val": 0}
  for prefix, root in sources:
    manifest = json.loads((root / "dataset.json").read_text(encoding="utf-8"))
    if classes is None:
      classes = manifest["classes"]
      tax = root / "taxonomy.json"
    elif manifest["classes"] != classes:
      raise SystemExit(f"class mismatch in {root}")
    by_id = {im["id"]: im for im in manifest["images"]}
    train = {Path(p).stem for p in (root / "train.txt").read_text(encoding="utf-8").splitlines() if p.strip()}
    val = {Path(p).stem for p in (root / "val.txt").read_text(encoding="utf-8").splitlines() if p.strip()}
    if train & val:
      raise SystemExit(f"train/val overlap in {root}")
    for old_id, meta in by_id.items():
      split = "train" if old_id in train else ("val" if old_id in val else None)
      if split is None:
        continue
      new_id = f"{prefix}_{old_id}"
      src_img = root / "images" / f"{old_id}.jpg"
      src_lab = root / "labels" / f"{old_id}.txt"
      if not src_img.is_file() or not src_lab.is_file():
        raise SystemExit(f"missing {src_img} or {src_lab}")
      link_or_copy(src_img, images / f"{new_id}.jpg")
      link_or_copy(src_lab, labels / f"{new_id}.txt")
      item = dict(meta)
      item["id"] = new_id
      item["file"] = f"images/{new_id}.jpg"
      item["split"] = split
      all_images.append(item)
      (train_lines if split == "train" else val_lines).append(f"images/{new_id}.jpg")
      split_counts[split] += 1
  if tax is None:
    raise SystemExit("no source")
  (out / "taxonomy.json").write_bytes(tax.read_bytes())
  payload = {"schema": 1, "batch": out.name, "taxonomy": "road_users_detailed_v3",
             "taxonomy_version": 3, "classes": classes, "images": all_images,
             "counts": {}, "split": split_counts}
  (out / "dataset.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
  (out / "train.txt").write_text("\n".join(train_lines) + "\n", encoding="utf-8")
  (out / "val.txt").write_text("\n".join(val_lines) + "\n", encoding="utf-8")
  (out / "data.yaml").write_text(
      "path: " + out.as_posix() + "\n" + "train: train.txt\nval: val.txt\nnc: " +
      str(len(classes)) + "\nnames:\n" + "".join(f"  - {c['name']}\n" for c in classes),
      encoding="utf-8")
  print(json.dumps({"out": str(out), "images": len(all_images), "train": split_counts["train"],
                    "val": split_counts["val"], "classes": len(classes)}, ensure_ascii=False))
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
