"""Model smoke tests: metadata, backend construction and one real inference.

Skipped when the shipped AGPL-3.0 YOLO11n model or ONNX Runtime is missing, so the
rest of the suite still runs on a minimal machine.
"""

import os

import numpy as np
import pytest

pytest.importorskip("onnxruntime")

from openpilot.sunnypilot.yolod.detector import Detector
from openpilot.sunnypilot.yolod.yolod import DEFAULT_MODEL_PATH

pytestmark = pytest.mark.skipif(not os.path.isfile(DEFAULT_MODEL_PATH),
                                reason=f"shipped YOLO model not found at {DEFAULT_MODEL_PATH}")


@pytest.fixture(scope="module")
def detector() -> Detector:
  model = Detector(DEFAULT_MODEL_PATH, conf=0.25, iou=0.45, threads=2)
  model.warmup()
  return model


def test_model_metadata(detector):
  assert detector.backend == "onnxruntime"
  assert len(detector.names) == 80
  assert detector.names[0] == "person"
  assert detector.names[2] == "car"
  assert detector.names[3] == "motorcycle"
  assert detector.class_names == [detector.names[index] for index in range(80)]
  assert (detector.input_w, detector.input_h) == (640, 640)
  assert detector.class_filter is None


def test_detects_on_an_arbitrary_frame_and_times_inference(detector):
  frame = np.zeros((1208, 1928, 3), dtype=np.uint8)
  frame[300:800, 400:1200] = 180
  detections = detector.detect(frame)
  assert isinstance(detections, list)
  assert detector.inference_ms > 0.0
  for detection in detections:
    assert detection.label == detector.names[detection.class_id]
    assert 0.0 <= detection.score <= 1.0
    x1, y1, x2, y2 = detection.box
    assert 0.0 <= x1 < x2 <= 1.0
    assert 0.0 <= y1 < y2 <= 1.0


def test_class_filter_is_resolved_to_ids():
  model = Detector(DEFAULT_MODEL_PATH, threads=1, class_filter=["person", "car"])
  assert model.class_filter == [0, 2]


def test_unknown_class_name_is_an_error_by_default():
  with pytest.raises(ValueError, match="unicorn"):
    Detector(DEFAULT_MODEL_PATH, threads=1, class_filter=["unicorn"])


def test_lenient_class_filter_reports_unmatched_names():
  model = Detector(DEFAULT_MODEL_PATH, threads=1, class_filter=["person", "unicorn"], strict_filter=False)
  assert model.class_filter == [0]
  assert model.unmatched_classes == ["unicorn"]


def test_missing_model_raises():
  with pytest.raises(FileNotFoundError):
    Detector("/nonexistent/model.onnx")


def test_forced_cv2dnn_backend():
  model = Detector(DEFAULT_MODEL_PATH, threads=1, backend="cv2dnn")
  assert model.backend == "cv2.dnn"
  # cv2.dnn exposes no ONNX metadata, so labels fall back to class_<id>.
  assert model.names == {}
  detections = model.detect(np.zeros((480, 640, 3), dtype=np.uint8))
  assert isinstance(detections, list)
  assert model.inference_ms > 0.0


def test_unknown_backend_is_rejected():
  with pytest.raises(ValueError, match="backend"):
    Detector(DEFAULT_MODEL_PATH, backend="tensorrt")
