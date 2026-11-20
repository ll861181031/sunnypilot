#!/usr/bin/env python3
"""Train a YOLO detector from an annotated batch and export ONNX.

Designed to run on the Windows training host (RTX 4070 SUPER) but works on any
machine with ultralytics + torch installed. The dataset layout is the one
produced by ``tools/yolo_dataset`` and edited by ``tools/yolo_annotator``.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def parse_args(argv=None):
  p = argparse.ArgumentParser(description="Train YOLO on an annotated batch")
  p.add_argument("dataset", help="dataset root (contains images/, labels/, dataset.json)")
  p.add_argument("--model", default="yolo11n.pt", help="base weights (yolo11n.pt / yolo11s.pt / ...)")
  p.add_argument("--epochs", type=int, default=100)
  p.add_argument("--imgsz", type=int, default=640)
  p.add_argument("--batch", type=int, default=-1, help="-1 = auto")
  p.add_argument("--device", default="0")
  p.add_argument("--workers", type=int, default=8)
  p.add_argument("--project", default=None, help="runs directory (default <dataset>/runs)")
  p.add_argument("--name", default="train")
  p.add_argument("--export-onnx", action="store_true", default=True)
  p.add_argument("--no-export", dest="export_onnx", action="store_false")
  p.add_argument("--reviewed-only", action="store_true", help="train only on images marked reviewed in review_state.json")
  p.add_argument("--upload", action="store_true", help="upload best.onnx to the Linux host via paramiko")
  p.add_argument("--sync-config", default=None, help="JSON with host/user/key_path/remote_dir")
  return p.parse_args(argv)


def build_data_yaml(dataset: Path, reviewed_only: bool = False) -> Path:
  import yaml

  manifest = json.loads((dataset / "dataset.json").read_text(encoding="utf-8"))
  classes = manifest.get("classes") or []
  names = {int(c["id"]): c["name"] for c in classes}
  if manifest.get("taxonomy") is None and not names:
    raise SystemExit("dataset.json has no class list; refusing to guess")

  train_txt = dataset / "train.txt"
  val_txt = dataset / "val.txt"
  allowed: set[str] | None = None
  if reviewed_only:
    review = json.loads((dataset / "review_state.json").read_text(encoding="utf-8"))
    allowed = {image_id for image_id, state in review.items()
               if state.get("reviewed") or state.get("edited")}
    print(f"reviewed-only: {len(allowed)} images marked reviewed or edited")
    if not allowed:
      raise SystemExit("--reviewed-only requested but no image is marked reviewed/edited yet")
  if not train_txt.is_file() or not val_txt.is_file():
    # Fall back to a route-free 85/15 split over whatever images exist.
    import random
    images = sorted((dataset / "images").glob("*.jpg"))
    if not images:
      raise SystemExit(f"no images under {dataset / 'images'}")
    random.Random(0).shuffle(images)
    cut = max(1, int(len(images) * 0.85))
    train_txt.write_text("\n".join(p.as_posix() for p in images[:cut]) + "\n")
    val_txt.write_text("\n".join(p.as_posix() for p in images[cut:]) + "\n")

  def _absolutized(source: Path, target: Path) -> Path:
    """Ultralytics resolves .txt entries from the CWD; make them absolute."""
    lines = [line.strip() for line in source.read_text(encoding="utf-8").splitlines() if line.strip()]
    resolved: list[str] = []
    for line in lines:
      item = Path(line)
      if not item.is_absolute():
        item = dataset / item
      if allowed is not None and item.stem not in allowed:
        continue
      resolved.append(item.resolve().as_posix())
    target.write_text("\n".join(resolved) + "\n", encoding="utf-8")
    return target

  train_abs = _absolutized(train_txt, dataset / "train_abs.txt")
  val_abs = _absolutized(val_txt, dataset / "val_abs.txt")
  data = {
    "path": dataset.resolve().as_posix(),
    "train": train_abs.resolve().as_posix(),
    "val": val_abs.resolve().as_posix(),
    "names": names,
  }
  out = dataset / "data.yaml"
  out.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
  print(f"data.yaml -> {out} ({len(names)} classes)")
  return out


def upload_onnx(onnx_path: Path, config_path: str | None) -> None:
  import paramiko

  cfg = json.loads(Path(config_path).read_text(encoding="utf-8")) if config_path else {
    "host": "192.168.123.70", "port": 22, "user": "lsr",
    "key_path": str(Path.home() / ".ssh" / "id_ed25519_labeler"),
    "remote_dir": "/home/lsr/yolo_models",
  }
  client = paramiko.SSHClient()
  client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
  client.connect(cfg["host"], port=int(cfg.get("port", 22)), username=cfg["user"],
                 key_filename=cfg["key_path"], timeout=15, look_for_keys=False, allow_agent=False)
  sftp = client.open_sftp()
  try:
    sftp.stat(cfg["remote_dir"])
  except OSError:
    sftp.mkdir(cfg["remote_dir"])
  remote = f"{cfg['remote_dir']}/{onnx_path.name}"
  sftp.put(str(onnx_path), remote)
  print(f"uploaded {onnx_path} -> {remote}")
  sftp.close()
  client.close()


def main(argv=None) -> int:
  args = parse_args(argv)
  dataset = Path(args.dataset).resolve()
  if not (dataset / "dataset.json").is_file():
    raise SystemExit(f"not a dataset: {dataset}")
  data_yaml = build_data_yaml(dataset, reviewed_only=args.reviewed_only)

  from ultralytics import YOLO
  import torch

  print(f"torch {torch.__version__} cuda={torch.cuda.is_available()} device={args.device}")
  model = YOLO(args.model)
  project = Path(args.project) if args.project else dataset / "runs"
  results = model.train(data=str(data_yaml), epochs=args.epochs, imgsz=args.imgsz,
                        batch=args.batch, device=args.device, workers=args.workers,
                        project=str(project), name=args.name, exist_ok=True)
  best = Path(getattr(results, "save_dir", project / args.name)) / "weights" / "best.pt"
  print(f"best weights: {best}")
  if not best.is_file():
    print("WARNING: best.pt not found; skip export", file=sys.stderr)
    return 0
  if args.export_onnx:
    exported = YOLO(str(best)).export(format="onnx", imgsz=args.imgsz, opset=12)
    print(f"exported onnx: {exported}")
    if args.upload:
      upload_onnx(Path(exported), args.sync_config)
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
