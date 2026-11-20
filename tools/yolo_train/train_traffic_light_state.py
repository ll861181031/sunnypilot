#!/usr/bin/env python3
"""Train the small 5-class traffic-light state classifier and export ONNX.

Runs on the Windows training host (torch is there, not on the car). The car only
needs the exported ONNX, which ``openpilot/sunnypilot/yolod/traffic_light_classifier.py``
loads with onnxruntime.

Dataset layout - one directory per class, either already split or to be split:

    <data>/red/*.jpg  <data>/yellow/*.jpg  <data>/green/*.jpg
    <data>/off/*.jpg  <data>/wait_on/*.jpg

    # ...or pre-split:
    <data>/train/{red,yellow,green,off,wait_on}/*.jpg
    <data>/val/{red,yellow,green,off,wait_on}/*.jpg

Two rules are built into the code because getting them wrong is worse than not
training at all:

1. **No vertical flip.** A lamp head encodes its state in vertical position
   (red on top, green at the bottom). A vertical flip silently turns every red
   light into a green one. Horizontal flips are fine - they change nothing about
   the state - and brightness/contrast jitter is kept mild because a strong hue
   shift can move yellow towards red.
2. **Per-class recall is reported, and red is the one that matters.** The
   earlier 21-class attempt reached mAP50 0.100 on red while looking acceptable
   overall; an aggregate accuracy number would have hidden exactly that.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

# Order matters: it is baked into the exported model metadata and must match what
# the inference module reads back. Alphabetical order would be actively confusing.
DEFAULT_CLASSES = ("red", "yellow", "green", "off", "wait_on")

# ImageNet-style normalisation is unnecessary at this size; a fixed, documented
# value is what matters so training and inference cannot drift apart.
NORM_MEAN = 0.5
NORM_STD = 0.5

# A model below this red recall is not usable regardless of how good it looks
# elsewhere; above it, ranking is by accuracy. Ordering by raw red recall instead
# would lock onto the first epoch that happened to score 1.000 exactly and ignore
# every later epoch that is better but scores 0.998.
RED_RECALL_GATE = 0.99


def parse_args(argv=None):
  p = argparse.ArgumentParser(description="Train the traffic-light state classifier")
  p.add_argument("data", help="dataset root (class subdirs, or train/ + val/)")
  p.add_argument("--classes", default=",".join(DEFAULT_CLASSES),
                 help="comma-separated class order (default: red,yellow,green,off,wait_on)")
  p.add_argument("--img-size", default="64x64", help="WxH fed to the network (default 64x64)")
  p.add_argument("--epochs", type=int, default=40)
  p.add_argument("--batch", type=int, default=128)
  p.add_argument("--lr", type=float, default=2e-3)
  p.add_argument("--weight-decay", type=float, default=1e-4)
  p.add_argument("--holdout", type=float, default=0.15,
                 help="validation fraction when the dataset is not pre-split")
  p.add_argument("--device", default="0", help="'0' for CUDA, 'cpu' to force CPU")
  p.add_argument("--workers", type=int, default=4)
  p.add_argument("--seed", type=int, default=0)
  p.add_argument("--out", default=None, help="output .onnx path (sidecar .json written next to it)")
  return p.parse_args(argv)


def parse_size(text: str) -> tuple[int, int]:
  parts = text.lower().replace(" ", "").split("x")
  if len(parts) == 1:
    value = int(parts[0])
    return value, value
  if len(parts) != 2:
    raise SystemExit(f"bad --img-size {text!r}, expected WxH such as 64x64")
  return int(parts[0]), int(parts[1])


class TrafficLightStateNet:
  """Factory for the tiny CNN; kept as a function so torch stays optional."""

  @staticmethod
  def build(num_classes: int):
    import torch.nn as nn

    def block(cin: int, cout: int) -> nn.Sequential:
      return nn.Sequential(
        nn.Conv2d(cin, cout, 3, padding=1, bias=False),
        nn.BatchNorm2d(cout),
        nn.ReLU(inplace=True),
        nn.MaxPool2d(2),
      )

    return nn.Sequential(
      block(3, 16),
      block(16, 32),
      block(32, 64),
      nn.AdaptiveAvgPool2d(1),
      nn.Flatten(),
      nn.Dropout(0.1),
      nn.Linear(64, num_classes),
    )


class CropFolder:
  """Map-style dataset over (path, label) pairs.

  Deliberately a plain class rather than a ``torch.utils.data.Dataset`` subclass:
  ``DataLoader`` only needs ``__len__``/``__getitem__``, and keeping torch out of
  module import means this file can at least be parsed on a machine without it.

  It also has to live at module scope - a class defined inside ``main()`` cannot be
  pickled, which breaks ``num_workers > 0``.
  """

  def __init__(self, items, transform):
    self.items = items
    self.transform = transform

  def __len__(self):
    return len(self.items)

  def __getitem__(self, index):
    from PIL import Image
    path, label = self.items[index]
    image = Image.open(path).convert("RGB")
    return self.transform(image), label


def collect_split(root: Path, classes: tuple[str, ...], holdout: float, seed: int):
  """Return (train_items, val_items, train_counts, val_counts)."""
  train_dir, val_dir = root / "train", root / "val"
  pre_split = train_dir.is_dir() and val_dir.is_dir()

  def gather(base: Path):
    items, counts = [], {}
    for index, name in enumerate(classes):
      class_dir = base / name
      if not class_dir.is_dir():
        continue
      files = sorted(p for p in class_dir.iterdir()
                     if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".bmp", ".webp"})
      if not files:
        continue
      counts[name] = len(files)
      items.extend((p, index) for p in files)
    return items, counts

  if pre_split:
    train_items, train_counts = gather(train_dir)
    val_items, val_counts = gather(val_dir)
    if not train_items:
      raise SystemExit(f"no images found under {train_dir}")
    if not val_items:
      raise SystemExit(f"no images found under {val_dir}")
    return train_items, val_items, train_counts, val_counts

  all_items, counts = gather(root)
  if not all_items:
    raise SystemExit(f"no images found under {root} (expected <class>/*.jpg subdirectories)")
  rng = random.Random(seed)
  # Split per class so rare states stay represented on both sides.
  train_items, val_items = [], []
  for index in range(len(classes)):
    group = [it for it in all_items if it[1] == index]
    rng.shuffle(group)
    cut = max(1, int(round(len(group) * holdout))) if len(group) > 1 else 0
    val_items.extend(group[:cut])
    train_items.extend(group[cut:])
  return train_items, val_items, counts, counts


def assert_no_vertical_flip(pipeline) -> None:
  """Refuse a transform pipeline that flips vertically.

  A lamp head encodes its state in vertical position - red on top, green at the
  bottom - so a vertical flip turns every red light into a green one. The mistake
  is invisible in aggregate accuracy, which is why the pipeline that will
  actually be used is inspected rather than trusting a comment.
  """
  from torchvision import transforms as T

  def offenders(container) -> list:
    found = []
    for step in getattr(container, "transforms", []):
      if isinstance(step, T.RandomVerticalFlip):
        found.append(step)
      elif isinstance(step, T.RandomApply):
        found.extend(s for s in getattr(step, "transforms", [])
                     if isinstance(s, T.RandomVerticalFlip))
    return found

  if offenders(pipeline):
    raise SystemExit("augmentation must not contain a vertical flip - it turns red lights green")


def build_transforms(img_hw: tuple[int, int]):
  from torchvision import transforms
  height, width = img_hw
  return transforms.Compose([
    transforms.Resize((height, width)),
    transforms.RandomHorizontalFlip(p=0.5),
    # Mild only: a strong colour shift can move yellow towards red.
    transforms.ColorJitter(brightness=0.25, contrast=0.25, saturation=0.15, hue=0.02),
    transforms.ToTensor(),
    transforms.Normalize(mean=[NORM_MEAN] * 3, std=[NORM_STD] * 3),
  ]), transforms.Compose([
    transforms.Resize((height, width)),
    transforms.ToTensor(),
    transforms.Normalize(mean=[NORM_MEAN] * 3, std=[NORM_STD] * 3),
  ])


def evaluate(model, loader, device, classes: tuple[str, ...]) -> tuple[float, dict[str, float], dict[str, int]]:
  """Return (accuracy, per-class recall, per-class support)."""
  import torch
  model.eval()
  correct = total = 0
  hit = {name: 0 for name in classes}
  support = {name: 0 for name in classes}
  with torch.no_grad():
    for images, targets in loader:
      images, targets = images.to(device), targets.to(device)
      pred = model(images).argmax(dim=1)
      correct += int((pred == targets).sum())
      total += int(targets.numel())
      for label, prediction in zip(targets.tolist(), pred.tolist()):
        if 0 <= label < len(classes):
          support[classes[label]] += 1
          if label == prediction:
            hit[classes[label]] += 1
  accuracy = correct / max(total, 1)
  recall = {c: (hit[c] / support[c]) if support[c] else float("nan") for c in classes}
  return accuracy, recall, support


def export_onnx(model, out_path: Path, img_hw: tuple[int, int], classes: tuple[str, ...]) -> None:
  import torch
  height, width = img_hw
  out_path.parent.mkdir(parents=True, exist_ok=True)
  # Export on CPU: the artifact is deployed on the car, where there is no CUDA,
  # and the tracing dummy has to live on the same device as the weights.
  model = model.to("cpu").eval()
  dummy = torch.zeros(1, 3, height, width)
  torch.onnx.export(model, dummy, str(out_path),
                    input_names=["input"], output_names=["logits"],
                    opset_version=17, do_constant_folding=True)
  sidecar = out_path.with_suffix(".json")
  sidecar.write_text(json.dumps({
    "labels": list(classes),
    "height": height,
    "width": width,
    "mean": NORM_MEAN,
    "std": NORM_STD,
    "input": "NCHW float32, RGB, /255 then (x-mean)/std",
  }, indent=2) + "\n", encoding="utf-8")
  print(f"\n  exported {out_path} ({out_path.stat().st_size/1024:.1f} KB)")
  print(f"  sidecar  {sidecar}")


def main(argv=None) -> int:
  args = parse_args(argv)
  # Checked structurally below, once the pipeline exists.

  import torch
  import torch.nn as nn
  from torch.utils.data import DataLoader

  classes = tuple(c.strip() for c in args.classes.split(",") if c.strip())
  if len(classes) != len(set(classes)):
    raise SystemExit("duplicate class names in --classes")
  width, height = parse_size(args.img_size)
  root = Path(args.data)
  if not root.is_dir():
    raise SystemExit(f"dataset root not found: {root}")

  random.seed(args.seed)
  torch.manual_seed(args.seed)

  train_items, val_items, train_counts, val_counts = collect_split(root, classes, args.holdout, args.seed)
  print(f"  classes  {', '.join(classes)}")
  print(f"  train    {len(train_items)} images  {train_counts}")
  print(f"  val      {len(val_items)} images  {val_counts}")
  present = {classes[i] for _, i in train_items}
  missing = [c for c in classes if c not in present]
  if missing:
    print(f"  WARNING  no training images for: {', '.join(missing)} - those classes cannot be learned")

  train_tf, val_tf = build_transforms((height, width))
  assert_no_vertical_flip(train_tf)
  assert_no_vertical_flip(val_tf)

  device = torch.device(f"cuda:{args.device}" if args.device not in ("cpu",) else "cpu")
  train_loader = DataLoader(CropFolder(train_items, train_tf), batch_size=args.batch,
                            shuffle=True, num_workers=args.workers, drop_last=False)
  val_loader = DataLoader(CropFolder(val_items, val_tf), batch_size=args.batch,
                          shuffle=False, num_workers=args.workers)

  model = TrafficLightStateNet.build(len(classes)).to(device)
  params = sum(p.numel() for p in model.parameters())
  print(f"  model    {params/1000:.1f}k parameters, input {width}x{height}")

  criterion = nn.CrossEntropyLoss()
  optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=args.weight_decay)
  scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=max(args.epochs, 1))

  best_score = (-1.0, -1.0)
  best_state = None
  best_epoch = 0
  for epoch in range(args.epochs):
    model.train()
    running = 0.0
    seen = 0
    for images, targets in train_loader:
      images, targets = images.to(device), targets.to(device)
      optimizer.zero_grad(set_to_none=True)
      loss = criterion(model(images), targets)
      loss.backward()
      optimizer.step()
      running += float(loss) * targets.numel()
      seen += int(targets.numel())
    scheduler.step()

    accuracy, recall, support = evaluate(model, val_loader, device, classes)
    # Red decides whether this model is usable at all, so selection is driven by
    # red recall rather than by accuracy.
    red_recall = recall.get("red", float("nan"))
    red_recall = -1.0 if red_recall != red_recall else red_recall
    line = "  ".join(f"{c} {recall[c]:.4f}" if c == "red" else f"{c} {recall[c]:.2f}"
                        for c in classes if support[c])
    print(f"  epoch {epoch+1:3d}  loss {running/max(seen,1):.4f}  acc {accuracy:.3f}  recall[{line}]")
    # Red recall is a gate, not a score. Once two epochs both clear it, prefer the
    # one that is better everywhere else - comparing red alone ties at 1.000 almost
    # immediately and keeps whatever early epoch got there first.
    score = (red_recall >= RED_RECALL_GATE, accuracy)
    if score > best_score:
      best_score = score
      best_epoch = epoch + 1
      best_state = {k: v.detach().clone() for k, v in model.state_dict().items()}

  if best_state is not None:
    model.load_state_dict(best_state)
  accuracy, recall, support = evaluate(model, val_loader, device, classes)
  print(f"\n  selected epoch {best_epoch} of {args.epochs}  (red gate {RED_RECALL_GATE} passed: {best_score[0]}, acc {best_score[1]:.4f})")
  print(f"  re-evaluated: acc {accuracy:.3f}")
  for name in classes:
    if support[name]:
      flag = "  <-- safety critical" if name == "red" else ""
      print(f"    {name:9s} recall {recall[name]:.3f}  ({support[name]} samples){flag}")
  if recall.get("red", 1.0) < 0.9:
    print("\n  WARNING red recall below 0.90 - do NOT wire this into longitudinal control yet.")

  out_path = Path(args.out) if args.out else Path("traffic_light_state.onnx")
  export_onnx(model, out_path, (height, width), classes)
  return 0


if __name__ == "__main__":
  sys.exit(main())
