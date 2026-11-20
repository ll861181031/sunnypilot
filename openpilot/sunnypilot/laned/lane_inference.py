"""YOLOv8-Seg lane-marking classifier ported from CarrotPilot's xiaoge extension.

This model is deliberately independent of the driving model. The driving model
still owns lane geometry (``laneLines``); this classifier only answers "is the
marking on my left/right ego boundary solid, dashed, or unknown". It consumes the
grayscale Y plane of the road camera, so it does not need the NV12 chroma planes.

Model provenance: ``models/lane.onnx`` is the 416x416 YOLOv8-Seg model shipped in
CarrotPilot's ``xiaoge`` extension (``assets/lane.onnx``). Its embedded class map
is::

    0 divider-line      1 dotted-line      2 double-line
    3 random-line       4 road-sign-line   5 solid-line

Classes 0/2/5 are solid, 1 is dashed, and 3/4 are ignored (they are not lane
boundaries). The model returns one classification per side, not per lane line.
"""

from pathlib import Path
import numpy as np

try:
  import cv2
except ImportError:
  cv2 = None

DEFAULT_LANE_MODEL_PATH = Path(__file__).resolve().parent / "models" / "lane.onnx"

INPUT_SIZE = 416
PROTO_SIZE = 104
NUM_CLASSES = 6
MASK_COEFFICIENTS = 32
CONFIDENCE_THRESHOLD = 0.25
IOU_THRESHOLD = 0.5
CENTER_X_52 = PROTO_SIZE / 2.0

# Class mapping from the model's embedded names. Keep this in sync with
# CLASS_NAMES below; the tests assert the mapping is total over 0..5.
SOLID_CLASSES = {0, 2, 5}
DASHED_CLASSES = {1}
IGNORED_CLASSES = {3, 4}

CLASS_NAMES = {
  0: "divider-line",
  1: "dotted-line",
  2: "double-line",
  3: "random-line",
  4: "road-sign-line",
  5: "solid-line",
}

# Public lane types used by the rest of sunnypilot.
LANE_TYPE_UNKNOWN = -1
LANE_TYPE_DASHED = 0
LANE_TYPE_SOLID = 1


def class_to_type(class_id: int) -> int:
  """Map a YOLO class id to the public lane type (-1/0/1)."""
  if class_id in SOLID_CLASSES:
    return LANE_TYPE_SOLID
  if class_id in DASHED_CLASSES:
    return LANE_TYPE_DASHED
  return LANE_TYPE_UNKNOWN


def prepare_lane_image(image: np.ndarray, width: int, height: int) -> np.ndarray:
  """Return the centered square grayscale crop, resized to ``INPUT_SIZE``.

  ``image`` may be the 2-D NV12 Y plane or a BGR image. Rows beyond ``height``
  and columns beyond ``width`` are VisionIPC stride padding and are discarded
  before the crop, exactly as the CarrotPilot implementation does.
  """
  if width <= 0 or height <= 0:
    raise ValueError("lane image dimensions must be positive")
  if image.ndim not in (2, 3) or (image.ndim == 3 and image.shape[2] != 3):
    raise ValueError(f"unsupported lane image shape: {image.shape}")
  if image.shape[0] < height or image.shape[1] < width:
    raise ValueError("short lane image plane")

  gray = image[:height, :width]
  if image.ndim == 3:
    if cv2 is None:
      raise RuntimeError("OpenCV is required for BGR lane images")
    gray = cv2.cvtColor(gray, cv2.COLOR_BGR2GRAY)

  crop_size = min(width, height)
  start_x = (width - crop_size) // 2
  start_y = (height - crop_size) // 2
  gray = np.ascontiguousarray(gray[start_y:start_y + crop_size, start_x:start_x + crop_size])
  if gray.shape != (INPUT_SIZE, INPUT_SIZE):
    if cv2 is None:
      raise RuntimeError("OpenCV is required to resize lane images")
    gray = cv2.resize(gray, (INPUT_SIZE, INPUT_SIZE), interpolation=cv2.INTER_LINEAR)
  return gray


class LaneCandidate:
  """One kept detection, in the model's 104x104 prototype space."""

  def __init__(self, class_id: int, score: float, bottom: int, center_at_bottom: float):
    self.class_id = class_id
    self.score = score
    self.bottom = bottom
    self.center_at_bottom = center_at_bottom
    self.lane_type = class_to_type(class_id)


def select_lane_results(candidates: list[LaneCandidate]) -> tuple[int, int, float, float]:
  """Select the nearest left and right boundary around image bottom-center.

  Returns ``(left_type, right_type, left_confidence, right_confidence)``. A side
  with no candidate is ``(-1, 0.0)``. The selection metric matches the CarrotPilot
  implementation: distance from the bottom-center, with vertical proximity to the
  bottom of the prototype grid weighted twice as much as horizontal distance.
  """
  left_candidates = [c for c in candidates if c.center_at_bottom < CENTER_X_52]
  right_candidates = [c for c in candidates if c.center_at_bottom >= CENTER_X_52]

  def nearest(side_candidates: list[LaneCandidate]) -> tuple[int, float]:
    if not side_candidates:
      return LANE_TYPE_UNKNOWN, 0.0
    best = min(
      side_candidates,
      key=lambda c: (c.center_at_bottom - CENTER_X_52) ** 2 + 2.0 * (c.bottom - (PROTO_SIZE - 1)) ** 2,
    )
    return best.lane_type, best.score

  left_type, left_conf = nearest(left_candidates)
  right_type, right_conf = nearest(right_candidates)
  return left_type, right_type, left_conf, right_conf


def non_maximum_suppression(boxes_left: np.ndarray, boxes_top: np.ndarray, boxes_right: np.ndarray,
                            boxes_bottom: np.ndarray, scores: np.ndarray, class_ids: np.ndarray,
                            iou_threshold: float) -> np.ndarray:
  """Keep the highest-scoring non-overlapping boxes independently per class."""
  areas = (boxes_right - boxes_left) * (boxes_bottom - boxes_top)
  kept_indices: list[int] = []
  for class_id in np.unique(class_ids):
    indices = np.flatnonzero(class_ids == class_id)
    order = indices[np.argsort(-scores[indices], kind="stable")]
    while len(order):
      current = order[0]
      kept_indices.append(int(current))
      remaining = order[1:]
      if not len(remaining):
        break
      intersection_width = np.maximum(
        0.0, np.minimum(boxes_right[current], boxes_right[remaining]) - np.maximum(boxes_left[current], boxes_left[remaining])
      )
      intersection_height = np.maximum(
        0.0, np.minimum(boxes_bottom[current], boxes_bottom[remaining]) - np.maximum(boxes_top[current], boxes_top[remaining])
      )
      intersection = intersection_width * intersection_height
      iou = intersection / (areas[current] + areas[remaining] - intersection + 1e-6)
      order = remaining[iou <= iou_threshold]
  return np.array(sorted(kept_indices, key=lambda index: scores[index], reverse=True), dtype=np.intp)


def _empty_result(valid: bool, error: str = "") -> dict:
  return {
    "leftLine": LANE_TYPE_UNKNOWN,
    "rightLine": LANE_TYPE_UNKNOWN,
    "leftConf": 0.0,
    "rightConf": 0.0,
    "valid": valid,
    "error": error,
    "candidatesCount": 0,
  }


class LaneInference:
  """Thin wrapper around the OpenCV DNN YOLOv8-Seg session."""

  def __init__(self, model_path: Path | str = DEFAULT_LANE_MODEL_PATH):
    self.model_path = Path(model_path)
    self.net = None
    self.valid = False
    self.error = ""

  def load(self) -> bool:
    if cv2 is None:
      self.error = "OpenCV (cv2) is missing"
      self.valid = False
      return False
    if not self.model_path.is_file():
      self.error = f"Model file not found: {self.model_path}"
      self.valid = False
      return False
    try:
      self.net = cv2.dnn.readNetFromONNX(str(self.model_path))
      self.valid = True
      self.error = ""
      return True
    except Exception as exc:  # cv2.error and backend failures both land here
      self.error = str(exc)
      self.valid = False
      return False

  def warmup(self) -> None:
    """Run one zero-input pass so the first real frame is not the slow one."""
    if not self.valid or self.net is None:
      return
    blob = np.zeros((1, 3, INPUT_SIZE, INPUT_SIZE), dtype=np.float32)
    self.net.setInput(blob)
    self.net.forward(self.net.getUnconnectedOutLayersNames())

  def preprocess_image(self, image: np.ndarray, width: int, height: int) -> np.ndarray:
    """Center-crop, resize, normalize, and expand to NCHW float32."""
    gray = prepare_lane_image(image, width, height)
    normalized = gray.astype(np.float32) / 255.0
    return np.broadcast_to(normalized, (3, INPUT_SIZE, INPUT_SIZE)).copy()[np.newaxis, ...]

  def _split_outputs(self, outputs: dict[str, np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
    """Return ``(predictions, prototypes)`` from the two YOLOv8-Seg heads.

    The shipped model names them ``output0`` and ``output1``. The shape fallback
    keeps the code usable if a future export renames them; it deliberately does
    not guess between two same-rank tensors.
    """
    predictions = outputs.get("output0")
    prototypes = outputs.get("output1")
    if predictions is None or prototypes is None:
      predictions = None
      prototypes = None
      for output in outputs.values():
        if output.ndim == 3 and output.shape[0] == 1 and output.shape[1] >= 4 + NUM_CLASSES:
          predictions = output
        elif output.ndim == 4 and output.shape[0] == 1:
          prototypes = output
    if predictions is None or prototypes is None:
      raise ValueError("lane model did not expose output0/output1")
    return predictions[0], prototypes[0]

  def infer(self, image: np.ndarray, width: int, height: int,
            conf_thresh: float = CONFIDENCE_THRESHOLD, iou_thresh: float = IOU_THRESHOLD) -> dict:
    if not self.valid or self.net is None:
      return _empty_result(False, self.error or "Model not loaded")

    try:
      blob = self.preprocess_image(image, width, height)
      self.net.setInput(blob)
      out_names = self.net.getUnconnectedOutLayersNames()
      outputs = dict(zip(out_names, self.net.forward(out_names), strict=True))
      predictions, prototypes = self._split_outputs(outputs)

      if predictions.shape[0] < 4 + NUM_CLASSES + MASK_COEFFICIENTS:
        raise ValueError(f"unexpected lane model prediction shape: {predictions.shape}")

      # YOLOv8 output rows: cx, cy, w, h, class scores..., mask coefficients...
      class_scores = predictions[4:4 + NUM_CLASSES, :]
      max_scores = np.max(class_scores, axis=0)
      class_ids = np.argmax(class_scores, axis=0)

      mask_valid = max_scores >= conf_thresh
      if not np.any(mask_valid):
        return _empty_result(True)

      valid_indices = np.where(mask_valid)[0]
      boxes_cx = predictions[0, valid_indices]
      boxes_cy = predictions[1, valid_indices]
      boxes_w = predictions[2, valid_indices]
      boxes_h = predictions[3, valid_indices]
      scores = max_scores[valid_indices]
      c_ids = class_ids[valid_indices]
      coeffs = predictions[4 + NUM_CLASSES:4 + NUM_CLASSES + MASK_COEFFICIENTS, valid_indices].T

      boxes_left = boxes_cx - boxes_w * 0.5
      boxes_top = boxes_cy - boxes_h * 0.5
      boxes_right = boxes_cx + boxes_w * 0.5
      boxes_bottom = boxes_cy + boxes_h * 0.5
      kept_indices = non_maximum_suppression(
        boxes_left, boxes_top, boxes_right, boxes_bottom, scores, c_ids, iou_thresh,
      )

      candidates: list[LaneCandidate] = []
      for k_idx in kept_indices:
        cid = int(c_ids[k_idx])
        if cid in IGNORED_CLASSES:
          continue

        score = float(scores[k_idx])
        coeff = coeffs[k_idx]

        left_p = int(np.clip((boxes_left[k_idx] / INPUT_SIZE) * PROTO_SIZE, 0, PROTO_SIZE - 1))
        top_p = int(np.clip((boxes_top[k_idx] / INPUT_SIZE) * PROTO_SIZE, 0, PROTO_SIZE - 1))
        right_p = int(np.clip((boxes_right[k_idx] / INPUT_SIZE) * PROTO_SIZE, 0, PROTO_SIZE - 1))
        bottom_p = int(np.clip((boxes_bottom[k_idx] / INPUT_SIZE) * PROTO_SIZE, 0, PROTO_SIZE - 1))

        if right_p <= left_p or bottom_p <= top_p:
          continue

        proto_crop = prototypes[:, top_p:bottom_p + 1, left_p:right_p + 1]
        mask_crop = np.tensordot(coeff, proto_crop, axes=(0, 0)) > 0.0
        if not np.any(mask_crop):
          continue

        row_has_mask = np.any(mask_crop, axis=1)
        nonzero_rows = np.where(row_has_mask)[0]
        if len(nonzero_rows) == 0:
          continue

        lowest_row_in_crop = nonzero_rows[-1]
        bottom_y = top_p + lowest_row_in_crop
        if bottom_y < PROTO_SIZE // 2:
          continue

        lowest_row_cols = np.where(mask_crop[lowest_row_in_crop])[0]
        center_x_in_crop = np.mean(lowest_row_cols)
        center_at_bottom = left_p + center_x_in_crop

        candidates.append(LaneCandidate(cid, score, bottom_y, center_at_bottom))

      left_type, right_type, left_conf, right_conf = select_lane_results(candidates)
      return {
        "leftLine": left_type,
        "rightLine": right_type,
        "leftConf": round(left_conf, 3),
        "rightConf": round(right_conf, 3),
        "valid": True,
        "error": "",
        "candidatesCount": len(candidates),
      }

    except Exception as exc:
      return _empty_result(False, str(exc))
