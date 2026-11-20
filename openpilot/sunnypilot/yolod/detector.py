"""YOLO object detector used by the informational ``yolod`` process.

The shipped model is YOLO11n (COCO, 640x640 input, ``[1, 84, 8400]`` output: four
box coordinates plus one score per class, no objectness). ONNX Runtime on the CPU
is the primary backend; ``cv2.dnn`` is the fallback so a missing/broken runtime
does not take object detection with it. ``backend`` may be set to ``cv2dnn`` to
force the OpenCV path (used to test the fallback); the optional ncnn + Vulkan
backend lives in ``detector_ncnn.py`` and is chosen by ``yolod`` through
``YOLO_BACKEND``.

Decoding also accepts the transposed prediction layout, a YOLOv5-style row with
an explicit objectness column, and an already-NMS'd ``[N, 6]`` export, so
replacing the model with another YOLO export usually needs no code change. Class
names and the model input size are read from the ONNX metadata; when a backend
cannot provide metadata (the cv2.dnn fallback has no metadata API) callers get
numeric labels and the 640x640 default instead of made-up names.
"""

import ast
import json
import os
import time
from dataclasses import dataclass
from collections.abc import Iterable, Sequence

import cv2
import numpy as np

DEFAULT_INPUT_SIZE = (640, 640)  # (width, height)
DEFAULT_CONF = 0.25
DEFAULT_IOU = 0.45
PAD_VALUE = 114  # letterbox fill; the Ultralytics convention
# "onnxruntime" prefers ONNX Runtime and falls back to cv2.dnn when the runtime
# cannot be loaded (the historical behaviour); "cv2dnn" forces the fallback so it
# can be exercised and selected on purpose.
BACKENDS = ("auto", "onnxruntime", "cv2dnn")


@dataclass(frozen=True)
class Detection:
  """One detection. ``box`` is (x1, y1, x2, y2) normalized to the input frame."""

  class_id: int
  label: str
  score: float
  box: tuple[float, float, float, float]

  @property
  def xywh(self) -> tuple[float, float, float, float]:
    """Top-left (x, y) and size (w, h), all normalized - the yoloObjects layout."""
    x1, y1, x2, y2 = self.box
    return x1, y1, x2 - x1, y2 - y1


@dataclass(frozen=True)
class LetterboxTransform:
  """Mapping between a source frame and the letterboxed model input.

  ``scale_x`` / ``scale_y`` are the scales actually applied after rounding the
  resized size, not the ideal aspect-preserving scale: mapping boxes back with
  the rounded factors keeps a point at the image corner exact.
  """

  src_w: int
  src_h: int
  dst_w: int
  dst_h: int
  scale_x: float
  scale_y: float
  pad_x: int
  pad_y: int

  def to_source(self, box: Sequence[float]) -> tuple[float, float, float, float]:
    """Map a box from letterboxed input pixels to source-image pixels."""
    x1, y1, x2, y2 = box
    return ((x1 - self.pad_x) / self.scale_x, (y1 - self.pad_y) / self.scale_y,
            (x2 - self.pad_x) / self.scale_x, (y2 - self.pad_y) / self.scale_y)

  def to_normalized(self, box: Sequence[float]) -> tuple[float, float, float, float]:
    """Map a box from letterboxed input pixels to [0, 1] source-frame coordinates."""
    x1, y1, x2, y2 = self.to_source(box)
    return (float(_clamp01(x1 / self.src_w)), float(_clamp01(y1 / self.src_h)),
            float(_clamp01(x2 / self.src_w)), float(_clamp01(y2 / self.src_h)))


def _clamp01(value: float) -> float:
  return min(1.0, max(0.0, value))


def letterbox(image: np.ndarray, size: tuple[int, int] = DEFAULT_INPUT_SIZE,
              pad_value: int = PAD_VALUE) -> tuple[np.ndarray, LetterboxTransform]:
  """Resize ``image`` with its aspect ratio intact and pad it to ``size`` (w, h)."""
  if image.ndim != 3 or image.shape[2] != 3:
    raise ValueError(f"expected a BGR image with 3 channels, got shape {image.shape}")
  src_h, src_w = image.shape[:2]
  dst_w, dst_h = (int(size[0]), int(size[1]))
  if src_w <= 0 or src_h <= 0 or dst_w <= 0 or dst_h <= 0:
    raise ValueError(f"invalid letterbox geometry: source {src_w}x{src_h}, target {dst_w}x{dst_h}")

  scale = min(dst_w / src_w, dst_h / src_h)
  new_w = min(dst_w, max(1, int(round(src_w * scale))))
  new_h = min(dst_h, max(1, int(round(src_h * scale))))
  if (new_w, new_h) != (src_w, src_h):
    resized = cv2.resize(image, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
  else:
    resized = image
  pad_x = (dst_w - new_w) // 2
  pad_y = (dst_h - new_h) // 2

  if new_w == dst_w and new_h == dst_h:
    padded = resized
  else:
    padded = np.full((dst_h, dst_w, 3), pad_value, dtype=np.uint8)
    padded[pad_y:pad_y + new_h, pad_x:pad_x + new_w] = resized

  transform = LetterboxTransform(src_w=src_w, src_h=src_h, dst_w=dst_w, dst_h=dst_h,
                                 scale_x=new_w / src_w, scale_y=new_h / src_h,
                                 pad_x=pad_x, pad_y=pad_y)
  return padded, transform


def preprocess(image: np.ndarray, size: tuple[int, int] = DEFAULT_INPUT_SIZE,
               pad_value: int = PAD_VALUE) -> tuple[np.ndarray, LetterboxTransform]:
  """Build the NCHW float32 RGB blob a YOLO export expects, plus its transform."""
  padded, transform = letterbox(image, size, pad_value)
  blob = padded[:, :, ::-1].transpose(2, 0, 1).astype(np.float32)
  blob /= 255.0
  return np.ascontiguousarray(blob[None]), transform


def _iou_one_to_many(box: np.ndarray, boxes: np.ndarray) -> np.ndarray:
  xx1 = np.maximum(box[0], boxes[:, 0])
  yy1 = np.maximum(box[1], boxes[:, 1])
  xx2 = np.minimum(box[2], boxes[:, 2])
  yy2 = np.minimum(box[3], boxes[:, 3])
  inter_w = np.maximum(0.0, xx2 - xx1)
  inter_h = np.maximum(0.0, yy2 - yy1)
  inter = inter_w * inter_h
  box_area = max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])
  boxes_area = np.maximum(0.0, boxes[:, 2] - boxes[:, 0]) * np.maximum(0.0, boxes[:, 3] - boxes[:, 1])
  union = box_area + boxes_area - inter
  return np.divide(inter, union, out=np.zeros_like(inter), where=union > 0)


def nms(boxes: np.ndarray, scores: np.ndarray, iou_threshold: float,
        class_ids: np.ndarray | None = None) -> list[int]:
  """Greedy non-maximum suppression over ``(N, 4)`` xyxy boxes.

  Suppression is class-wise when ``class_ids`` is given: two overlapping boxes of
  different classes are both kept, which is what the class score decoder needs.
  Returns indices into the input arrays, highest score first.
  """
  boxes = np.asarray(boxes, dtype=np.float32).reshape(-1, 4)
  scores = np.asarray(scores, dtype=np.float32).reshape(-1)
  if boxes.shape[0] != scores.shape[0]:
    raise ValueError("boxes and scores disagree on the number of detections")
  if not iou_threshold >= 0.0:
    raise ValueError(f"iou_threshold must be non-negative, got {iou_threshold}")
  if boxes.shape[0] == 0:
    return []
  if class_ids is not None:
    class_ids = np.asarray(class_ids).reshape(-1)
    if class_ids.shape[0] != boxes.shape[0]:
      raise ValueError("class_ids and boxes disagree on the number of detections")

  order = np.argsort(-scores, kind="stable")
  keep: list[int] = []
  while order.size:
    best = int(order[0])
    keep.append(best)
    rest = order[1:]
    if rest.size == 0:
      break
    overlaps = _iou_one_to_many(boxes[best], boxes[rest]) > iou_threshold
    if class_ids is not None:
      overlaps &= class_ids[rest] == class_ids[best]
    order = rest[~overlaps]
  return keep


def _parse_metadata_value(raw: str | None):
  if not raw:
    return None
  for parser in (ast.literal_eval, json.loads):
    try:
      return parser(raw)
    except (ValueError, SyntaxError, TypeError):
      continue
  return None


def parse_class_names(raw: str | None) -> dict[int, str]:
  """Parse the ``names`` metadata field into ``{class_id: name}``.

  Ultralytics writes Python-dict text (``"{0: 'person', ...}"``); JSON exports use
  string keys. Both are accepted, as is a plain list of names.
  """
  value = _parse_metadata_value(raw)
  if isinstance(value, dict):
    try:
      return {int(k): str(v) for k, v in value.items()}
    except (TypeError, ValueError):
      return {}
  if isinstance(value, (list, tuple)):
    return {i: str(name) for i, name in enumerate(value)}
  return {}


def parse_imgsz(raw: str | None) -> tuple[int, int] | None:
  """Parse the ``imgsz`` metadata field into ``(height, width)``."""
  value = _parse_metadata_value(raw)
  if isinstance(value, int) and value > 0:
    return value, value
  if isinstance(value, (list, tuple)) and len(value) == 2:
    try:
      height, width = int(value[0]), int(value[1])
    except (TypeError, ValueError):
      return None
    if height > 0 and width > 0:
      return height, width
  return None


def resolve_class_filter(class_filter: Iterable[str | int] | None,
                         names: dict[int, str]) -> tuple[list[int], list[str]]:
  """Map names and/or ids to class ids. Returns ``(ids, unmatched names)``.

  An empty or unset filter means "no filter" (all classes). Unknown names are
  reported to the caller instead of raising so a daemon can degrade gracefully;
  the detector itself is strict and turns them into an error.
  """
  if not class_filter:
    return [], []
  ids: set[int] = set()
  unmatched: list[str] = []
  by_name = {name: class_id for class_id, name in names.items()}
  for entry in class_filter:
    if isinstance(entry, int) or (isinstance(entry, str) and entry.strip().isdigit()):
      ids.add(int(entry))
    elif isinstance(entry, str):
      name = entry.strip()
      if name in by_name:
        ids.add(by_name[name])
      else:
        unmatched.append(name)
  return sorted(ids), unmatched


def decode_output(output: np.ndarray, transform: LetterboxTransform, conf: float = DEFAULT_CONF,
                  iou: float = DEFAULT_IOU, class_filter: Sequence[int] | None = None,
                  names: dict[int, str] | None = None) -> list[Detection]:
  """Decode a raw YOLO output into detections normalized to the source frame."""
  names = names or {}
  arr = np.asarray(output, dtype=np.float32)
  while arr.ndim > 2:
    if arr.shape[0] != 1:
      raise ValueError(f"expected a single-image batch, got shape {arr.shape}")
    arr = arr[0]
  if arr.ndim == 1:
    arr = arr[None, :]
  if arr.ndim != 2:
    raise ValueError(f"unexpected YOLO output shape {arr.shape}")
  if arr.shape[0] == 0 or arr.shape[1] == 0:
    return []

  # End-to-end exports already contain finished boxes: x1, y1, x2, y2, score, class.
  # A two-class raw export also has six columns, so only use this interpretation
  # when the metadata rules out the raw layout.
  num_names = len(names)
  raw_layout_sizes = (num_names + 4, num_names + 5) if num_names else ()
  if arr.shape[1] == 6 and 6 not in raw_layout_sizes:
    return _decode_end2end(arr, transform, conf, iou, class_filter, names)

  # Fix the orientation when it is unambiguous: the attribute dimension is always
  # the smaller one, and with metadata it is exactly 4 (+1) + number of classes.
  if num_names:
    if arr.shape[1] not in raw_layout_sizes and arr.shape[0] in raw_layout_sizes:
      arr = arr.T
    elif arr.shape[1] not in raw_layout_sizes and arr.shape[0] < arr.shape[1]:
      arr = arr.T
  elif arr.shape[0] < arr.shape[1]:
    arr = arr.T

  attributes = arr.shape[1]
  has_objectness = bool(num_names) and attributes == num_names + 5
  scores = arr[:, 5:] * arr[:, 4:5] if has_objectness else arr[:, 4:]
  if scores.shape[1] == 0:
    raise ValueError(f"output has no class scores: {arr.shape}")
  if num_names and scores.shape[1] != num_names:
    raise ValueError(f"output has {scores.shape[1]} class scores, metadata names {num_names} classes")

  class_ids = scores.argmax(axis=1)
  best = scores[np.arange(scores.shape[0]), class_ids]
  selected = best >= conf
  if class_filter is not None:
    selected &= np.isin(class_ids, np.asarray(list(class_filter), dtype=np.int64))
  rows = np.flatnonzero(selected)
  if rows.size == 0:
    return []

  centers = arr[rows, 0:2]
  sizes = arr[rows, 2:4]
  boxes = np.concatenate((centers - sizes / 2.0, centers + sizes / 2.0), axis=1)
  scores = best[rows]
  class_ids = class_ids[rows]

  keep = nms(boxes, scores, iou, class_ids)
  return _build_detections(boxes, scores, class_ids, keep, transform, names)


def _decode_end2end(arr: np.ndarray, transform: LetterboxTransform, conf: float, iou: float,
                    class_filter: Sequence[int] | None, names: dict[int, str]) -> list[Detection]:
  selected = arr[:, 4] >= conf
  if class_filter is not None:
    selected &= np.isin(arr[:, 5].astype(np.int64), np.asarray(list(class_filter), dtype=np.int64))
  rows = np.flatnonzero(selected)
  if rows.size == 0:
    return []
  boxes = arr[rows, 0:4]
  scores = arr[rows, 4]
  class_ids = arr[rows, 5].astype(np.int64)
  keep = nms(boxes, scores, iou, class_ids)
  return _build_detections(boxes, scores, class_ids, keep, transform, names)


def _build_detections(boxes: np.ndarray, scores: np.ndarray, class_ids: np.ndarray, keep: Sequence[int],
                      transform: LetterboxTransform, names: dict[int, str]) -> list[Detection]:
  detections: list[Detection] = []
  for position in keep:
    box = transform.to_normalized(boxes[int(position)])
    if box[2] <= box[0] or box[3] <= box[1]:
      continue
    class_id = int(class_ids[int(position)])
    detections.append(Detection(class_id=class_id,
                                label=names.get(class_id, f"class_{class_id}"),
                                score=float(scores[int(position)]),
                                box=box))
  detections.sort(key=lambda detection: detection.score, reverse=True)
  return detections


class Detector:
  """YOLO detector with an ONNX Runtime backend and a cv2.dnn fallback."""

  def __init__(self, model_path: str, conf: float = DEFAULT_CONF, iou: float = DEFAULT_IOU,
               threads: int = 2, class_filter: Iterable[str | int] | None = None,
               strict_filter: bool = True, backend: str = "auto"):
    backend = str(backend).lower()
    if backend not in BACKENDS:
      raise ValueError(f"unknown backend {backend!r}, expected one of {BACKENDS}")
    self.backend_request = backend
    self.model_path = os.fspath(model_path)
    if not os.path.isfile(self.model_path):
      raise FileNotFoundError(f"model file not found: {self.model_path}")
    self.conf = float(conf)
    self.iou = float(iou)
    self.threads = max(1, int(threads))
    self.inference_ms = 0.0
    self.backend = "uninitialized"
    self.names: dict[int, str] = {}
    self.class_names: list[str] = []
    self.input_w, self.input_h = DEFAULT_INPUT_SIZE
    self.session = None
    self.net = None
    self.input_name = ""
    self.output_names: list[str] = []
    self._init_backend()

    self.class_names = [self.names[class_id] for class_id in sorted(self.names)]
    class_ids, unmatched = resolve_class_filter(class_filter, self.names)
    if unmatched and strict_filter:
      available = ", ".join(self.class_names[:20]) + (" ..." if len(self.class_names) > 20 else "")
      raise ValueError(f"unknown class name(s) {unmatched}; model knows: {available}")
    self.unmatched_classes = unmatched
    self.class_filter: list[int] | None = class_ids or None

  def _init_backend(self) -> None:
    errors: list[str] = []
    ort = None
    if self.backend_request != "cv2dnn":
      try:
        import onnxruntime as ort
      except ImportError as error:  # pragma: no cover - the venv ships onnxruntime
        ort = None
        errors.append(f"onnxruntime import failed: {error}")

    if ort is not None:
      try:
        options = ort.SessionOptions()
        options.intra_op_num_threads = self.threads
        options.inter_op_num_threads = 1
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.session = ort.InferenceSession(self.model_path, sess_options=options,
                                            providers=["CPUExecutionProvider"])
        model_input = self.session.get_inputs()[0]
        self.input_name = model_input.name
        self.output_names = [output.name for output in self.session.get_outputs()]
        metadata = self.session.get_modelmeta().custom_metadata_map
        self.names = parse_class_names(metadata.get("names"))
        self._apply_input_geometry(model_input.shape, parse_imgsz(metadata.get("imgsz")))
        self.backend = "onnxruntime"
        return
      except Exception as error:
        errors.append(f"onnxruntime failed: {type(error).__name__}: {error}")
        self.session = None

    try:
      self.net = cv2.dnn.readNetFromONNX(self.model_path)
      self._apply_input_geometry(None, None)
      self.backend = "cv2.dnn"
    except Exception as error:
      errors.append(f"cv2.dnn failed: {type(error).__name__}: {error}")
      raise RuntimeError("no usable YOLO inference backend: " + " | ".join(errors)) from error

  def _apply_input_geometry(self, shape, imgsz: tuple[int, int] | None) -> None:
    """Prefer the static input shape, fall back to metadata, then to 640x640."""
    width = height = None
    if shape is not None and len(shape) == 4:
      if isinstance(shape[2], int) and shape[2] > 0:
        height = shape[2]
      if isinstance(shape[3], int) and shape[3] > 0:
        width = shape[3]
    if (width is None or height is None) and imgsz is not None:
      height, width = imgsz
    if width is None or height is None:
      width, height = DEFAULT_INPUT_SIZE
    self.input_w, self.input_h = int(width), int(height)

  def detect(self, bgr: np.ndarray) -> list[Detection]:
    """Run one frame. ``inference_ms`` covers preprocessing, inference and decoding."""
    started = time.perf_counter()
    blob, transform = preprocess(bgr, (self.input_w, self.input_h))
    output = self._infer(blob)
    detections = decode_output(output, transform, self.conf, self.iou, self.class_filter, self.names)
    self.inference_ms = (time.perf_counter() - started) * 1000.0
    return detections

  def _infer(self, blob: np.ndarray) -> np.ndarray:
    if self.session is not None:
      return self.session.run(self.output_names, {self.input_name: blob})[0]
    # cv2.dnn: the NCHW float32 blob built by preprocess() is exactly its input format.
    self.net.setInput(blob)
    return np.asarray(self.net.forward())

  def warmup(self) -> None:
    """Run one throwaway inference so the first published frame is not the slow one."""
    self.detect(np.zeros((self.input_h, self.input_w, 3), dtype=np.uint8))
