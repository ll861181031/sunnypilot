#!/usr/bin/env python3
"""Turn a reviewed label set into a fine-tuning dataset for the state classifier.

Reads the ``labels.json`` exported by ``review.html`` (which ``pick_domain_samples.py``
generates), rebuilds every kept crop from its recorded source box at the
classifier's own input size, and writes ``train/`` and ``val/`` in the layout
``train_traffic_light_state.py`` already expects.

Three rules that keep the resulting set honest:

* **``reject`` is never folded into a lamp class.** A crop that is a road sign or
  a street lamp teaches nothing about lamp state, and forcing it into the nearest
  class would reinforce exactly the red bias this exercise exists to remove.
  Under ``--keep-rejects-as`` those crops become their own class instead of being
  dropped - which is usually what you want, because the current model calls a
  third of them "red" with confidence 1.00, and the only way to teach it
  "warm blob is not a red light" is to give those examples a label.
* **The split is by source image.** Several crops can come from one frame; letting
  them straddle train and val would leak near-identical images into validation.
* **Crops are rebuilt from the source, not upscaled from the review thumbnail.**
  Review images are JPEG at native size, but rebuilding through the original
  pixels keeps the training input free of the review re-encode.

Also reports the confusion between the model's pre-label and the human verdict,
which is the number that says whether the domain gap actually shrank.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

CLASSES = ("red", "yellow", "green", "off", "wait_on")
DROP = {"reject", "skip", None, ""}


def parse_args(argv=None):
  p = argparse.ArgumentParser(description="Build a fine-tune dataset from reviewed crops")
  p.add_argument("pick_dir", help="output directory from pick_domain_samples.py")
  p.add_argument("--labels", required=True, help="labels.json exported by review.html")
  p.add_argument("--out", required=True, help="output dataset root (gets train/ and val/)")
  p.add_argument("--size", default="48x72", help="crop size WxH; must match the classifier input")
  p.add_argument("--val-frac", type=float, default=0.2)
  p.add_argument("--pad", type=float, default=0.08,
                 help="margin around the box, matching build_s2tld_crops.py")
  p.add_argument("--seed", type=int, default=0)
  p.add_argument("--keep-rejects-as", default=None, metavar="CLASS",
                 help="write 'reject' crops into CLASS instead of dropping them, e.g. not_light. "
                      "Recommended: the detector hands the classifier signs and street lamps, and "
                      "without negatives it will keep calling them red")
  return p.parse_args(argv)


def parse_size(text: str) -> tuple[int, int]:
  parts = text.lower().replace(" ", "").split("x")
  if len(parts) != 2:
    raise SystemExit(f"bad --size {text!r}, expected WxH")
  return int(parts[0]), int(parts[1])


def main(argv=None) -> int:
  args = parse_args(argv)
  import cv2
  import random

  pick_dir = Path(args.pick_dir)
  manifest = json.loads((pick_dir / "manifest.json").read_text(encoding="utf-8"))
  seed = json.loads((pick_dir / "seed_labels.json").read_text(encoding="utf-8"))
  verdicts = json.loads(Path(args.labels).read_text(encoding="utf-8"))

  width, height = parse_size(args.size)
  unknown = sorted(set(verdicts) - set(manifest))
  if unknown:
    print(f"  ignoring {len(unknown)} entries with no manifest row (e.g. {unknown[0]})")

  extra_class = args.keep_rejects_as.strip().lower() if args.keep_rejects_as else None
  allowed = set(CLASSES) | ({extra_class} if extra_class else set())

  kept, dropped = [], Counter()
  for name, verdict in sorted(verdicts.items()):
    if name not in manifest:
      continue
    label = None if verdict is None else str(verdict).strip().lower()
    if label == "reject" and args.keep_rejects_as:
      label = args.keep_rejects_as.strip().lower()
    elif label in DROP:
      dropped["reject" if label == "reject" else "skip/unset"] += 1
      continue
    if label not in allowed:
      dropped[f"bad value {label!r}"] += 1
      continue
    kept.append((name, label))

  if not kept:
    raise SystemExit("nothing left after dropping reject/skip - did you export labels.json?")

  # Group by source image so the split can be done per image, not per crop.
  by_source = {}
  for name, label in kept:
    by_source.setdefault(manifest[name]["source"], []).append((name, label))

  sources = sorted(by_source)
  random.Random(args.seed).shuffle(sources)
  val_count = max(1, int(round(len(sources) * args.val_frac)))
  val_sources = set(sources[:val_count])

  out = Path(args.out)
  written = {"train": Counter(), "val": Counter()}
  missing = Counter()
  for source, entries in by_source.items():
    split = "val" if source in val_sources else "train"
    image = cv2.imread(source)
    if image is None:
      missing["source unreadable"] += 1
      continue
    image_h, image_w = image.shape[:2]
    for name, label in entries:
      x1, y1, x2, y2 = manifest[name]["box"]
      box_w, box_h = x2 - x1, y2 - y1
      if box_w < 4 or box_h < 4:
        missing["box too small"] += 1
        continue
      pad_x, pad_y = box_w * args.pad, box_h * args.pad
      left = max(0, int(round(x1 - pad_x)))
      top = max(0, int(round(y1 - pad_y)))
      right = min(image_w, int(round(x2 + pad_x)))
      bottom = min(image_h, int(round(y2 + pad_y)))
      if right - left < 4 or bottom - top < 4:
        missing["degenerate after clamp"] += 1
        continue
      crop = cv2.resize(image[top:bottom, left:right], (width, height),
                        interpolation=cv2.INTER_AREA)
      dest_dir = out / split / label
      dest_dir.mkdir(parents=True, exist_ok=True)
      cv2.imwrite(str(dest_dir / f"domain_{Path(name).stem}.jpg"), crop,
                  [cv2.IMWRITE_JPEG_QUALITY, 95])
      written[split][label] += 1

  print(f"\n  crops written to {out}")
  all_classes = list(CLASSES) + ([extra_class] if extra_class else [])
  print("  " + "class".ljust(10) + "".join(f"{s:>9s}" for s in ("train", "val")) + "   changed by review")
  for label in all_classes:
    agreed = sum(1 for n, v in kept if v == label and seed.get(n, {}).get("pred") == label)
    disputed = sum(1 for n, v in kept if v == label and seed.get(n, {}).get("pred") != label)
    print("  " + label.ljust(10) + "".join(f"{written[s][label]:>9d}" for s in ("train", "val"))
          + f"   {disputed:>4d} corrected / {agreed} kept")
  print("  " + "total".ljust(10)
        + f"{sum(written['train'].values()):>9d}{sum(written['val'].values()):>9d}")

  if dropped:
    print("\n  dropped: " + ", ".join(f"{k}={v}" for k, v in dropped.items()))
  if missing:
    print("  unusable: " + ", ".join(f"{k}={v}" for k, v in missing.items()))

  total = sum(written["train"].values()) + sum(written["val"].values())
  print(f"\n  {total} labelled crops from {len(by_source)} source frames")
  thin = [c for c in all_classes if written["train"][c] < 50]
  if thin:
    print(f"  NOTE thin classes (<50 train crops): {', '.join(thin)}"
          " - fine-tuning will barely move them, by design")

  print("\n  fine-tune with:")
  print(f"    python tools/yolo_train/train_traffic_light_state.py {out} \\")
  classes_arg = "" if not extra_class else f"      --classes {','.join(all_classes)} \\\n"
  print(classes_arg + f"      --img-size {args.size} --epochs 20 --lr 5e-4 --device 0 \\")
  print("      --out traffic_light_state_ft.onnx")
  return 0


if __name__ == "__main__":
  sys.exit(main())
