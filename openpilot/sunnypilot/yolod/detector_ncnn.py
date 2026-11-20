"""Optional ncnn + Vulkan backend for ``yolod`` (experimental, not the default).

The shipped model is converted from the same ONNX export with pnnx (see
``tools/export_ncnn.py``), so decoding, letterboxing, class names and the
``Detection`` type are shared with the ONNX Runtime path in ``detector.py``.

The GPU is an iGPU shared with the desktop and with ``modeld`` (the driving model
runs on the same Radeon 780M), so this backend is opt-in through
``YOLO_BACKEND=ncnn`` and never becomes the default. A Vulkan failure is raised,
not silently downgraded to the CPU - a daemon that thinks it is on the GPU while
using the CPU would be worse than a clear error.

Only one Vulkan net is created per process on purpose: mixing an ncnn CPU net and
a Vulkan net in one process crashed during development, and coexistence with an
ONNX Runtime session is not validated either, so tests create this detector in a
subprocess.
"""

import os
import time
from collections.abc import Iterable, Sequence

import ncnn
import numpy as np

from openpilot.sunnypilot.yolod.detector import (
  DEFAULT_CONF,
  DEFAULT_INPUT_SIZE,
  DEFAULT_IOU,
  Detection,
  decode_output,
  letterbox,
  parse_class_names,
  resolve_class_filter,
)
from openpilot.sunnypilot.yolod.paths import DEFAULT_NCNN_BIN, DEFAULT_NCNN_NAMES, DEFAULT_NCNN_PARAM
DEFAULT_INPUT_NAME = "in0"
DEFAULT_OUTPUT_NAME = "out0"
DEFAULT_INPUT_SIZE_NCNN = DEFAULT_INPUT_SIZE
INPUT_SCALE = 1.0 / 255.0
SOFTWARE_VULKAN_NAMES = ("llvmpipe", "swiftshader")


def _device_name(index: int) -> str:
  try:
    return str(ncnn.get_gpu_info(index).device_name())
  except Exception:
    return f"device {index}"


def _select_vulkan_device() -> int:
  """Pick the first hardware Vulkan device, refusing software-only setups.

  ``ncnn.get_gpu_count()`` can include llvmpipe (Mesa's software rasterizer)
  which reports Vulkan support without giving any GPU speed. Selecting it would
  be a silent CPU fallback wearing a "GPU backend" label, so it is rejected.
  """
  try:
    count = ncnn.get_gpu_count()
  except Exception as error:
    raise RuntimeError(f"ncnn Vulkan backend requested but Vulkan is unavailable: {error}") from error
  if count <= 0:
    raise RuntimeError("ncnn Vulkan backend requested but no Vulkan device was found")

  software: list[str] = []
  for index in range(count):
    name = _device_name(index)
    if any(software_name in name.lower() for software_name in SOFTWARE_VULKAN_NAMES):
      software.append(name)
      continue
    return index
  raise RuntimeError(f"ncnn Vulkan backend requested but only software Vulkan devices are available: {software}")


def load_class_names(names_path: str | None) -> dict[int, str]:
  """Read the names JSON written by ``tools/export_ncnn.py``; empty when absent."""
  if not names_path or not os.path.isfile(names_path):
    return {}
  try:
    with open(names_path, encoding="utf-8") as file:
      return parse_class_names(file.read())
  except (OSError, ValueError):
    return {}


def _normalize_names(names: dict[int, str] | dict[str, str] | Sequence[str] | None) -> dict[int, str]:
  if not names:
    return {}
  if isinstance(names, dict):
    try:
      return {int(key): str(value) for key, value in names.items()}
    except (TypeError, ValueError):
      return {}
  return {index: str(name) for index, name in enumerate(names)}


class NcnnDetector:
  """YOLO detector running on ncnn's Vulkan backend (AMD iGPU in this device)."""

  def __init__(self, model_param: str = DEFAULT_NCNN_PARAM, model_bin: str = DEFAULT_NCNN_BIN,
               names: dict[int, str] | dict[str, str] | Sequence[str] | None = None,
               names_path: str | None = DEFAULT_NCNN_NAMES, conf: float = DEFAULT_CONF,
               iou: float = DEFAULT_IOU, threads: int = 1,
               class_filter: Iterable[str | int] | None = None, strict_filter: bool = True,
               gpu_index: int | None = None, imgsz: int | None = None):
    self.model_param = os.fspath(model_param)
    self.model_bin = os.fspath(model_bin)
    for path in (self.model_param, self.model_bin):
      if not os.path.isfile(path):
        raise FileNotFoundError(f"ncnn model file not found: {path}")

    self.conf = float(conf)
    self.iou = float(iou)
    self.threads = max(1, int(threads))
    self.gpu_index = gpu_index
    self.inference_ms = 0.0
    self.backend = "ncnn-vulkan"
    # An ncnn param file does not record its input shape, so the letterbox size has
    # to be supplied. Feeding a 640 tensor to a 960 graph does not raise - it
    # produces confident garbage (one 960 export reported 1255 detections), which
    # is why this is explicit rather than inferred.
    if imgsz is not None and int(imgsz) > 0:
      self.input_w = self.input_h = int(imgsz)
    else:
      self.input_w, self.input_h = DEFAULT_INPUT_SIZE_NCNN
    self.input_name = DEFAULT_INPUT_NAME
    self.output_name = DEFAULT_OUTPUT_NAME
    self._net = None

    self.names = _normalize_names(names) or load_class_names(names_path)
    self.names_source = "explicit" if names else ("file" if self.names else "none")
    self.class_names = [self.names[class_id] for class_id in sorted(self.names)]
    class_ids, unmatched = resolve_class_filter(class_filter, self.names)
    if unmatched and strict_filter:
      available = ", ".join(self.class_names[:20]) + (" ..." if len(self.class_names) > 20 else "")
      raise ValueError(f"unknown class name(s) {unmatched}; model knows: {available}")
    self.unmatched_classes = unmatched
    self.class_filter: list[int] | None = class_ids or None

    self.device_index = -1
    self.device_name = ""
    self._init_net()

  def _init_net(self) -> None:
    if self.gpu_index is None:
      device_index = _select_vulkan_device()
    else:
      try:
        count = ncnn.get_gpu_count()
      except Exception as error:
        raise RuntimeError(f"ncnn Vulkan backend requested but Vulkan is unavailable: {error}") from error
      if not 0 <= int(self.gpu_index) < count:
        raise RuntimeError(f"ncnn Vulkan device index {self.gpu_index} is out of range (found {count} devices)")
      device_index = int(self.gpu_index)
    self.device_index = device_index
    self.device_name = _device_name(device_index)

    net = ncnn.Net()
    net.opt.use_vulkan_compute = True
    net.opt.num_threads = self.threads
    net.set_vulkan_device(device_index)
    try:
      param_status = net.load_param(self.model_param)
      model_status = net.load_model(self.model_bin)
    except Exception as error:
      raise RuntimeError(f"ncnn failed to load {self.model_param} / {self.model_bin}: {error}") from error
    if param_status != 0:
      raise RuntimeError(f"ncnn could not parse {self.model_param} (status {param_status})")
    if model_status != 0:
      raise RuntimeError(f"ncnn could not load weights from {self.model_bin} (status {model_status})")

    inputs = list(net.input_names())
    outputs = list(net.output_names())
    self.input_name = inputs[0] if inputs else DEFAULT_INPUT_NAME
    self.output_name = outputs[0] if outputs else DEFAULT_OUTPUT_NAME
    self._net = net

  def _run(self, padded_bgr: np.ndarray) -> np.ndarray:
    """Run the network on an already letterboxed BGR uint8 image."""
    if self._net is None:
      raise RuntimeError("ncnn detector is closed")
    # from_pixels applies the BGR->RGB conversion and the float conversion; building
    # a float HWC Mat directly produced wrong detections (a bogus 0.5 person box).
    mat = ncnn.Mat.from_pixels(np.ascontiguousarray(padded_bgr), ncnn.Mat.PixelType.PIXEL_BGR2RGB,
                               self.input_w, self.input_h)
    mat.substract_mean_normalize([], [INPUT_SCALE, INPUT_SCALE, INPUT_SCALE])
    with self._net.create_extractor() as extractor:
      if extractor.input(self.input_name, mat) != 0:
        raise RuntimeError("ncnn failed to set the input blob on the Vulkan backend")
      status, output = extractor.extract(self.output_name)
      if status != 0:
        raise RuntimeError("ncnn inference failed on the Vulkan backend")
      return np.array(output).astype(np.float32, copy=False)

  def detect(self, bgr: np.ndarray) -> list[Detection]:
    """Run one frame. ``inference_ms`` covers letterbox, GPU inference and decoding."""
    started = time.perf_counter()
    padded, transform = letterbox(bgr, (self.input_w, self.input_h))
    output = self._run(padded)
    detections = decode_output(output, transform, self.conf, self.iou, self.class_filter, self.names)
    self.inference_ms = (time.perf_counter() - started) * 1000.0
    return detections

  def warmup(self) -> None:
    """Compile the Vulkan shaders and warm the allocator before the first real frame."""
    self.detect(np.zeros((self.input_h, self.input_w, 3), dtype=np.uint8))

  def close(self) -> None:
    self._net = None

  def __repr__(self) -> str:
    param = os.path.basename(self.model_param)
    return f"NcnnDetector(param={param!r}, backend={self.backend!r}, device={self.device_index}:{self.device_name!r}, classes={len(self.class_names)})"
