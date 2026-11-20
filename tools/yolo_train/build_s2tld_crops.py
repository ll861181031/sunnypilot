#!/usr/bin/env python3
"""Cut S2TLD (VOC XML) into a folder-per-class crop set for the state classifier.

S2TLD ships Pascal-VOC annotations: ``Annotations/*.xml`` next to
``JPEGImages/*.jpg``, one ``<object>`` per traffic-light head with the state in
``<name>`` (red / yellow / green / off / wait_on). The state classifier does not
want whole photos, it wants the head crops - that is exactly what the YOLO
detector will hand it at runtime.

Two details matter for the result to be honest:

* **Split by image, never by crop.** Both halves of one photo show the same
  lights under the same exposure; splitting per crop would leak nearly identical
  images into validation and inflate the score.
* **Report per-class counts and refuse to be quiet about rare classes.** A class
  with a hundred examples cannot be learned, and that failure shows up as a
  silent hole exactly where the model must not have one (red).

Runs on the Windows training host; only needs Pillow, which ultralytics pulls in.
"""
from __future__ import annotations

import argparse
import random
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

CLASSES = ("red", "yellow", "green", "off", "wait_on")


def parse_args(argv=None):
  p = argparse.ArgumentParser(description="Build traffic-light state crops from S2TLD VOC data")
  p.add_argument("--data", action="append", required=True,
                 help="dataset root containing Annotations/ and JPEGImages/ (repeatable)")
  p.add_argument("--out", required=True, help="output root, gets train/ and val/")
  p.add_argument("--size", default="48x72", help="crop size WxH (default 48x72 = 0.67 aspect)")
  p.add_argument("--val-frac", type=float, default=0.15)
  p.add_argument("--min-side", type=int, default=6, help="skip boxes smaller than this in pixels")
  p.add_argument("--pad", type=float, default=0.08, help="fraction of box size to add as margin")
  p.add_argument("--seed", type=int, default=0)
  p.add_argument("--quality", type=int, default=95)
  return p.parse_args(argv)


def parse_size(text: str) -> tuple[int, int]:
  parts = text.lower().replace(" ", "").split("x")
  if len(parts) != 2:
    raise SystemExit(f"bad --size {text!r}, expected WxH")
  return int(parts[0]), int(parts[1])


def find_pairs(root: Path) -> list[tuple[Path, Path]]:
  """Locate every (xml, image) pair under root, tolerating a nested folder."""
  pairs = []
  for ann in root.rglob("*.xml"):
    if ann.parent.name.lower() != "annotations":
      continue
    image_dir = ann.parent.parent / "JPEGImages"
    if not image_dir.is_dir():
      continue
    image = image_dir / (ann.stem + ".jpg")
    if not image.is_file():
      for candidate in image_dir.glob(ann.stem + ".*"):
        image = candidate
        break
    if image.is_file():
      pairs.append((ann, image))
  return pairs


def read_boxes(xml_path: Path) -> list[tuple[str, tuple[float, float, float, float]]]:
  try:
    tree = ET.parse(str(xml_path))
  except (ET.ParseError, OSError):
    return []
  out = []
  for obj in tree.getroot().iter("object"):
    name_el = obj.find("name")
    box_el = obj.find("bndbox")
    if name_el is None or box_el is None:
      continue
    label = (name_el.text or "").strip().lower().replace(" ", "_")
    if label not in CLASSES:
      continue
    try:
      coords = tuple(float(box_el.find(tag).text) for tag in ("xmin", "ymin", "xmax", "ymax"))
    except (AttributeError, TypeError, ValueError):
      continue
    out.append((label, coords))
  return out


def main(argv=None) -> int:
  args = parse_args(argv)
  from PIL import Image

  width, height = parse_size(args.size)
  out_root = Path(args.out)
  rng = random.Random(args.seed)

  all_pairs: list[tuple[Path, Path]] = []
  for entry in args.data:
    root = Path(entry)
    if not root.is_dir():
      raise SystemExit(f"dataset root not found: {root}")
    found = find_pairs(root)
    print(f"  {root.name}: {len(found)} image/xml pairs")
    all_pairs.extend(found)
  if not all_pairs:
    raise SystemExit("no VOC pairs found (expected <root>/Annotations/*.xml + JPEGImages/)")

  all_pairs.sort(key=lambda pair: str(pair[0]))
  rng.shuffle(all_pairs)
  val_count = max(1, int(round(len(all_pairs) * args.val_frac)))
  val_pairs = all_pairs[:val_count]
  train_pairs = all_pairs[val_count:]

  made = {"train": Counter(), "val": Counter()}
  skipped = Counter()
  for split, pairs in (("train", train_pairs), ("val", val_pairs)):
    for xml_path, image_path in pairs:
      try:
        image = Image.open(str(image_path)).convert("RGB")
      except (OSError, ValueError):
        skipped["unreadable image"] += 1
        continue
      image_w, image_h = image.size
      boxes = read_boxes(xml_path)
      if not boxes:
        skipped["no usable object"] += 1
        continue
      for box_index, (label, (x1, y1, x2, y2)) in enumerate(boxes):
        box_w, box_h = x2 - x1, y2 - y1
        if box_w < args.min_side or box_h < args.min_side:
          skipped["box too small"] += 1
          continue
        # A little margin keeps the housing visible; the lamp position inside the
        # head is the cue that separates red from green on a monochrome crop.
        pad_x, pad_y = box_w * args.pad, box_h * args.pad
        left = max(0, int(round(x1 - pad_x)))
        top = max(0, int(round(y1 - pad_y)))
        right = min(image_w, int(round(x2 + pad_x)))
        bottom = min(image_h, int(round(y2 + pad_y)))
        if right - left < 4 or bottom - top < 4:
          skipped["degenerate after clamp"] += 1
          continue
        crop = image.crop((left, top, right, bottom)).resize((width, height), Image.BILINEAR)
        dest_dir = out_root / split / label
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{xml_path.stem}_{box_index}.jpg"
        crop.save(str(dest), quality=args.quality)
        made[split][label] += 1

  print(f"\n  crops written to {out_root}")
  header = "  " + "class".ljust(10) + "".join(f"{s:>10s}" for s in ("train", "val"))
  print(header)
  for label in CLASSES:
    print("  " + label.ljust(10) + "".join(f"{made[s][label]:>10d}" for s in ("train", "val")))
  total_train = sum(made["train"].values())
  total_val = sum(made["val"].values())
  print("  " + "total".ljust(10) + f"{total_train:>10d}{total_val:>10d}")

  if skipped:
    print("\n  skipped: " + ", ".join(f"{k}={v}" for k, v in skipped.items()))

  thin = [label for label in CLASSES if made["train"][label] < 200]
  if thin:
    print(f"\n  WARNING thin training classes (<200 crops): {', '.join(thin)}")
    print("  A class this small will not be learned reliably; red is safety critical.")
  return 0


if __name__ == "__main__":
  sys.exit(main())
