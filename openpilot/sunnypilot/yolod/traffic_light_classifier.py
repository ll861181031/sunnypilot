"""Read the red / yellow / green / off / wait_on state of a traffic-light crop.

This is the lightweight half of the two-stage pipeline: a YOLO detector finds the
lamp head, this module names what it is doing. The driving models have no such
output head, and the 21-class combined detector was both too slow (283 ms at 16
threads) and unreliable on red, so state is deliberately kept separate: a 24k
parameter CNN on a ~20x40 crop costs well under a millisecond.

Design notes that matter:

* **No vertical flip anywhere.** A traffic light encodes its state in lamp
  position - red on top, green at the bottom. Mirroring vertically would turn a
  red light into a green one, which is the one error that must never happen.
  Horizontal flips are harmless, brightness/contrast jitter is mild on purpose
  (a strong hue shift can turn yellow into red).
* **Preprocessing is dictated by the model file, not by this module.** The mean,
  standard deviation, input size and label order are read from the ONNX metadata
  (or the sidecar JSON written by ``tools/yolo_train/train_traffic_light_state.py``).
  Hardcoding them here would silently produce garbage if the model were retrained
  with different settings, so nothing falls back to a guessed default.
* **Absent model is not an error.** The classifier is optional; callers get
  ``None`` and keep whatever they did before.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

DEFAULT_MODEL = Path(__file__).resolve().parent / "models" / "traffic_light_state.onnx"

# Label order used when a model file does not record one. Kept in sync with the
# training script; the model file's own metadata always wins when present.
FALLBACK_LABELS = ("red", "yellow", "green", "off", "wait_on")

# Crops smaller than this carry no usable signal - a distant lamp is a few pixels
# wide and classifying sensor noise produces confident nonsense.
MIN_CROP_SIDE = 4


@dataclass(frozen=True)
class StatePrediction:
  label: str
  prob: float


@dataclass(frozen=True)
class _ModelSpec:
  labels: tuple[str, ...]
  height: int
  width: int
  mean: float
  std: float


def _parse_labels(value) -> tuple[str, ...] | None:
  """Accept a list, a JSON array string, or a comma-separated string.

  ONNX metadata only stores text, so a label list always arrives as a string and
  iterating over it directly would yield single characters.
  """
  if isinstance(value, (list, tuple)):
    return tuple(str(x) for x in value) or None
  if not isinstance(value, str):
    return None
  text = value.strip()
  if not text:
    return None
  if text.startswith("["):
    try:
      parsed = json.loads(text)
    except ValueError:
      return None
    return _parse_labels(parsed) if isinstance(parsed, list) else None
  parts = [p.strip() for p in text.split(",") if p.strip()]
  return tuple(parts) or None


def _spec_from_metadata(meta: dict) -> _ModelSpec | None:
  """Build a spec from a metadata mapping, or None if it is not complete."""
  try:
    labels = _parse_labels(meta["labels"])
    height = int(meta["height"])
    width = int(meta["width"])
    mean = float(meta["mean"])
    std = float(meta["std"])
  except (KeyError, TypeError, ValueError):
    return None
  if not labels or height <= 0 or width <= 0 or std <= 0:
    return None
  return _ModelSpec(labels=labels, height=height, width=width, mean=mean, std=std)


def _sidecar_path(model_path: Path) -> Path:
  return model_path.with_suffix(".json")


def _spec_from_sidecar(model_path: Path) -> _ModelSpec | None:
  """Read the sidecar JSON written by the training script, if it is usable."""
  sidecar = _sidecar_path(model_path)
  if not sidecar.is_file():
    return None
  try:
    return _spec_from_metadata(json.loads(sidecar.read_text(encoding="utf-8")))
  except (OSError, ValueError, KeyError, TypeError):
    return None


def load_model_spec(model_path: Path) -> _ModelSpec | None:
  """Read the spec from the sidecar JSON, then from ONNX metadata."""
  model_path = Path(model_path)
  spec = _spec_from_sidecar(model_path)
  if spec is not None:
    return spec
  if not model_path.is_file():
    return None
  try:
    import onnxruntime as ort
    session = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
    raw = session.get_modelmeta().custom_metadata_map
  except Exception:
    return None
  return _spec_from_metadata(dict(raw)) if raw else None


class TrafficLightStateClassifier:
  """ONNX wrapper for the 5-class lamp-state CNN.

  ``available`` is False when the model file or onnxruntime is missing; every
  call then returns None and the caller keeps its previous behaviour.
  """

  def __init__(self, model_path: str | Path | None = None, threads: int = 1):
    self.model_path = Path(model_path) if model_path is not None else DEFAULT_MODEL
    self.threads = threads
    self._session = None
    self._spec: _ModelSpec | None = None
    self._input_name = ""
    self._load_error: str | None = None
    self._load()

  def _load(self) -> None:
    if not self.model_path.is_file():
      self._load_error = f"model not found: {self.model_path}"
      return
    try:
      import onnxruntime as ort
    except Exception as error:  # pragma: no cover - depends on the host image
      self._load_error = f"onnxruntime unavailable: {error}"
      return

    options = ort.SessionOptions()
    options.intra_op_num_threads = max(1, int(self.threads))
    options.inter_op_num_threads = 1
    try:
      session = ort.InferenceSession(str(self.model_path), sess_options=options,
                                     providers=["CPUExecutionProvider"])
    except Exception as error:
      self._load_error = f"session failed: {error}"
      return

    # The sidecar wins because a retrain without re-exporting metadata would
    # otherwise silently keep reading stale labels from the previous export.
    spec = _spec_from_sidecar(self.model_path)
    if spec is None:
      raw = session.get_modelmeta().custom_metadata_map
      spec = _spec_from_metadata(dict(raw)) if raw else None
    if spec is None:
      # Refusing is deliberate: guessing the label order or normalisation would
      # turn every later decision into an unverifiable coin flip.
      self._load_error = "model metadata incomplete (labels/height/width/mean/std)"
      return

    self._session = session
    self._spec = spec
    self._input_name = session.get_inputs()[0].name

  @property
  def available(self) -> bool:
    return self._session is not None and self._spec is not None

  @property
  def load_error(self) -> str | None:
    return self._load_error

  @property
  def labels(self) -> tuple[str, ...]:
    return self._spec.labels if self._spec is not None else FALLBACK_LABELS

  def _preprocess(self, crop_bgr: np.ndarray) -> np.ndarray | None:
    spec = self._spec
    if spec is None:
      return None
    if crop_bgr is None or crop_bgr.ndim != 3 or crop_bgr.shape[2] != 3:
      return None
    if crop_bgr.shape[0] < MIN_CROP_SIDE or crop_bgr.shape[1] < MIN_CROP_SIDE:
      return None

    import cv2
    # Straight resize, not letterbox: the crop is already the tight box around a
    # lamp head, and distorting it does not move the lamp up or down, which is
    # the cue the map in the classifier relies on.
    resized = cv2.resize(crop_bgr, (spec.width, spec.height), interpolation=cv2.INTER_AREA)
    rgb = resized[:, :, ::-1].astype(np.float32)
    normalized = (rgb / 255.0 - spec.mean) / spec.std
    return np.ascontiguousarray(normalized.transpose(2, 0, 1)[None])

  def classify(self, crop_bgr: np.ndarray) -> StatePrediction | None:
    """Return the most likely state for one crop, or None if it cannot be read."""
    if not self.available:
      return None
    x = self._preprocess(crop_bgr)
    if x is None:
      return None
    try:
      out = self._session.run(None, {self._input_name: x})[0]
    except Exception:
      return None
    raw = np.asarray(out, dtype=np.float32).reshape(-1)
    if raw.size == 0:
      return None
    # The exported graph stops at the classifier's Linear layer, so it emits raw
    # logits. A model exported with a trailing Softmax would already be a
    # distribution. Which one it is gets detected rather than assumed - dividing
    # logits by their sum yields numbers that look like confidence and are not.
    if np.all(raw >= 0.0) and abs(float(raw.sum()) - 1.0) < 1e-3:
      probs = raw
    else:
      shifted = raw - float(raw.max())      # keeps exp() from overflowing
      exp = np.exp(shifted)
      probs = exp / float(exp.sum())
    index = int(np.argmax(probs))
    labels = self.labels
    if index >= len(labels):
      return None
    return StatePrediction(label=labels[index], prob=float(probs[index]))

  def classify_batch(self, crops: list[np.ndarray]) -> list[StatePrediction | None]:
    return [self.classify(c) for c in crops]


def most_dangerous(predictions: list[StatePrediction | None], min_prob: float = 0.5) -> StatePrediction | None:
  """Pick the prediction that should most stop the car, ignoring weak ones.

  Used to answer "is there a red light anywhere ahead". Red outranks yellow, and
  a low-confidence guess is dropped rather than treated as evidence - a 0.2 red
  is noise, and acting on it would brake the car for nothing.
  """
  order = {"red": 0, "yellow": 1, "wait_on": 2, "green": 3, "off": 4}
  best = None
  for p in predictions:
    if p is None or p.prob < min_prob:
      continue
    if best is None or order.get(p.label, 9) < order.get(best.label, 9):
      best = p
  return best
