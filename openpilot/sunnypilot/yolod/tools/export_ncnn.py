#!/usr/bin/env python3
"""Convert the shipped YOLO ONNX export into ncnn param/bin with pnnx.

``yolod`` can run either the ONNX model (ONNX Runtime, CPU; the default) or the
ncnn version (Vulkan GPU, experimental). This tool performs the conversion and
writes the class-name JSON that ``NcnnDetector`` reads.

Requirements (the repo venv already has ncnn; pnnx provides the ``pnnx`` command):

    uv pip install ncnn pnnx

Typical use:

    cd /data/sunnypilot
    .venv/bin/python openpilot/sunnypilot/yolod/tools/export_ncnn.py \
        openpilot/sunnypilot/yolod/models/yolo11n.onnx --out-dir openpilot/sunnypilot/yolod/models

pnnx writes ``<stem>.ncnn.param`` / ``<stem>.ncnn.bin`` next to the ONNX file; they
are copied to ``--out-dir/<prefix>.ncnn.param`` / ``.bin`` and the ONNX metadata
(all 80 COCO names) is written to ``<prefix>.ncnn.names.json``. Use
``--skip-convert`` to only regenerate the names JSON, and ``--dry-run`` to print
the pnnx command without running it.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys

if __package__ in (None, ""):  # allow `python path/to/export_ncnn.py` from the repo root
  sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")))

from openpilot.sunnypilot.yolod.detector import parse_class_names, parse_imgsz
from openpilot.sunnypilot.yolod.paths import MODELS_DIR


def read_onnx_metadata(model_path: str) -> dict:
  """Read class names, ``imgsz`` metadata and the static input shape from an ONNX file."""
  try:
    import onnxruntime as ort
  except ImportError as error:  # pragma: no cover - the venv ships onnxruntime
    raise RuntimeError("onnxruntime is required to read the ONNX metadata") from error

  session = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
  metadata = session.get_modelmeta().custom_metadata_map
  input_shape = list(session.get_inputs()[0].shape)
  imgsz = parse_imgsz(metadata.get("imgsz"))
  if all(isinstance(dimension, int) and dimension > 0 for dimension in input_shape) and len(input_shape) == 4:
    shape = input_shape
  elif imgsz is not None:
    shape = [1, 3, imgsz[0], imgsz[1]]
  else:
    shape = [1, 3, 640, 640]
  return {
    "names": parse_class_names(metadata.get("names")),
    "imgsz": imgsz,
    "input_shape": [int(dimension) for dimension in shape],
  }


def format_input_shape(shape: list[int]) -> str:
  """``[1, 3, 640, 640]`` -> ``1,3,640,640`` (the pnnx inputshape value)."""
  return ",".join(str(int(dimension)) for dimension in shape)


def build_pnnx_command(pnnx: str, onnx_path: str, input_name: str = "in0",
                       input_shape: str = "1,3,640,640") -> list[str]:
  """pnnx takes the shape as ``inputshape=name=[N,C,H,W]``."""
  shape = input_shape.strip().strip("[]")
  return [pnnx, onnx_path, f"inputshape={input_name}=[{shape}]"]


def write_names_json(names: dict[int, str], path: str) -> dict:
  """Write ``{class_id: name}`` as a JSON object with string keys (Ultralytics style)."""
  payload = {str(class_id): name for class_id, name in sorted(names.items())}
  directory = os.path.dirname(os.path.abspath(path))
  os.makedirs(directory, exist_ok=True)
  with open(path, "w", encoding="utf-8") as file:
    json.dump(payload, file, indent=2)
    file.write("\n")
  return payload


def _copy_outputs(output_dir: str, stem: str, out_dir: str, prefix: str, force: bool) -> tuple[str, str]:
  sources = {
    "param": os.path.join(output_dir, f"{stem}.ncnn.param"),
    "bin": os.path.join(output_dir, f"{stem}.ncnn.bin"),
  }
  targets = {
    "param": os.path.join(out_dir, f"{prefix}.ncnn.param"),
    "bin": os.path.join(out_dir, f"{prefix}.ncnn.bin"),
  }
  for kind, source in sources.items():
    if not os.path.isfile(source):
      raise FileNotFoundError(f"pnnx did not produce {source}")
    if os.path.exists(targets[kind]) and not force:
      raise FileExistsError(f"{targets[kind]} already exists (use --force to overwrite)")
  for kind in sources:
    shutil.copy2(sources[kind], targets[kind])
  return targets["param"], targets["bin"]


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(description="Convert a YOLO ONNX export to ncnn param/bin (pnnx)")
  parser.add_argument("onnx", help="YOLO ONNX export (must carry names/imgsz metadata)")
  parser.add_argument("--out-dir", default=MODELS_DIR, help=f"output directory (default: {MODELS_DIR})")
  parser.add_argument("--prefix", default="yolo11n", help="output file prefix (default: yolo11n)")
  parser.add_argument("--input-name", default="in0", help="ONNX input name (default: in0)")
  parser.add_argument("--input-shape", default=None,
                      help="input shape as N,C,H,W (default: read from the ONNX input or imgsz metadata)")
  parser.add_argument("--pnnx", default=None, help="path to the pnnx executable (default: search PATH)")
  parser.add_argument("--skip-convert", action="store_true", help="only write the names JSON, do not run pnnx")
  parser.add_argument("--dry-run", action="store_true", help="print the pnnx command without running it")
  parser.add_argument("--force", action="store_true", help="overwrite existing output files")
  args = parser.parse_args(argv)

  if not os.path.isfile(args.onnx):
    print(f"error: ONNX model not found: {args.onnx}", file=sys.stderr)
    return 2

  try:
    metadata = read_onnx_metadata(args.onnx)
  except (OSError, RuntimeError) as error:
    print(f"error: cannot read ONNX metadata: {error}", file=sys.stderr)
    return 1
  names = metadata["names"]
  if not names:
    print("error: the ONNX file has no `names` metadata; pass a model exported by Ultralytics", file=sys.stderr)
    return 1

  names_path = os.path.join(args.out_dir, f"{args.prefix}.ncnn.names.json")
  write_names_json(names, names_path)
  print(f"names: {len(names)} classes -> {os.path.abspath(names_path)}")

  if args.skip_convert:
    print("skip-convert: pnnx not run")
    return 0

  pnnx = args.pnnx or shutil.which("pnnx")
  if pnnx is None and not args.dry_run:
    message = "pnnx is not installed. Install it with `uv pip install ncnn pnnx`, or use --skip-convert."
    print(f"error: {message}", file=sys.stderr)
    return 2
  input_shape = args.input_shape or format_input_shape(metadata["input_shape"])
  command = build_pnnx_command(pnnx or "pnnx", os.path.abspath(args.onnx), args.input_name, input_shape)
  print("pnnx:", " ".join(command))
  if args.dry_run:
    if pnnx is None:
      print("dry-run: pnnx not found, the command above is illustrative")
    return 0

  try:
    subprocess.run(command, cwd=os.path.dirname(os.path.abspath(args.onnx)), check=True)
  except (OSError, subprocess.CalledProcessError) as error:
    print(f"error: pnnx conversion failed: {error}", file=sys.stderr)
    return 1

  stem = os.path.splitext(os.path.basename(args.onnx))[0]
  try:
    param_path, bin_path = _copy_outputs(os.path.dirname(os.path.abspath(args.onnx)), stem,
                                         args.out_dir, args.prefix, args.force)
  except (FileNotFoundError, FileExistsError) as error:
    print(f"error: {error}", file=sys.stderr)
    return 1
  print(f"param: {param_path} ({os.path.getsize(param_path)} bytes)")
  print(f"bin:   {bin_path} ({os.path.getsize(bin_path)} bytes)")
  print("done. Use it with: YOLO_BACKEND=ncnn")
  return 0


if __name__ == "__main__":
  sys.exit(main())
