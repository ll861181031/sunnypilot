import numpy as np
import pytest

from openpilot.sunnypilot.laned.lane_inference import (
  CLASS_NAMES,
  DASHED_CLASSES,
  DEFAULT_LANE_MODEL_PATH,
  IGNORED_CLASSES,
  SOLID_CLASSES,
  LaneCandidate,
  LaneInference,
  class_to_type,
  non_maximum_suppression,
  prepare_lane_image,
  select_lane_results,
)

try:
  import cv2
except ImportError:
  cv2 = None


def test_every_model_class_has_a_public_type():
  assert set(CLASS_NAMES) == set(range(6))
  assert SOLID_CLASSES | DASHED_CLASSES | IGNORED_CLASSES == set(range(6))
  assert not (SOLID_CLASSES & DASHED_CLASSES)
  assert not (SOLID_CLASSES & IGNORED_CLASSES)
  assert not (DASHED_CLASSES & IGNORED_CLASSES)


@pytest.mark.parametrize("class_id, expected", [
  (0, 1),  # divider-line
  (1, 0),  # dotted-line
  (2, 1),  # double-line
  (3, -1),  # random-line
  (4, -1),  # road-sign-line
  (5, 1),  # solid-line
])
def test_class_to_type(class_id, expected):
  assert class_to_type(class_id) == expected


def test_prepare_lane_image_keeps_a_416_square_unchanged():
  image = np.arange(416 * 416, dtype=np.uint16).reshape(416, 416).astype(np.uint8)
  output = prepare_lane_image(image, 416, 416)
  assert output.shape == (416, 416)
  assert np.array_equal(output, image)


def test_prepare_lane_image_center_crops_a_wide_plane():
  image = np.zeros((760, 1344), dtype=np.uint8)
  image[:, 100] = 255  # outside the centered 760-wide crop
  image[:, 672] = 255  # inside the crop
  output = prepare_lane_image(image, 1344, 760)
  assert output.shape == (416, 416)
  # Resizing a one-pixel line spreads it, so check presence rather than 255.
  assert output.max() > 100
  assert output[:, 0].max() == 0


def test_prepare_lane_image_ignores_stride_padding():
  image = np.zeros((760, 1408), dtype=np.uint8)
  image[:, 1344:] = 255  # padding beyond the visible width
  output = prepare_lane_image(image, 1344, 760)
  assert output.shape == (416, 416)
  assert output.max() == 0


def test_prepare_lane_image_rejects_short_planes():
  with pytest.raises(ValueError):
    prepare_lane_image(np.zeros((100, 100), dtype=np.uint8), 200, 100)


def test_select_lane_results_uses_one_type_per_side():
  candidates = [
    LaneCandidate(1, 0.9, 100, 40.0),  # dashed, left
    LaneCandidate(5, 0.8, 100, 70.0),  # solid, right
  ]
  left, right, left_conf, right_conf = select_lane_results(candidates)
  assert (left, right) == (0, 1)
  assert (left_conf, right_conf) == (0.9, 0.8)


def test_select_lane_results_picks_the_boundary_nearest_to_bottom_center():
  candidates = [
    LaneCandidate(5, 0.3, 100, 30.0),  # solid, far left
    LaneCandidate(1, 0.9, 100, 50.0),  # dashed, closest to center
  ]
  left, right, left_conf, right_conf = select_lane_results(candidates)
  assert (left, right) == (0, -1)
  assert (left_conf, right_conf) == (0.9, 0.0)


def test_select_lane_results_without_candidates_is_unknown():
  assert select_lane_results([]) == (-1, -1, 0.0, 0.0)


def test_nms_keeps_the_best_box_per_class():
  left = np.array([0.0, 1.0])
  top = np.array([0.0, 1.0])
  right = np.array([10.0, 11.0])
  bottom = np.array([10.0, 11.0])
  scores = np.array([0.9, 0.8])
  classes = np.array([1, 1])
  kept = non_maximum_suppression(left, top, right, bottom, scores, classes, 0.5)
  assert len(kept) == 1
  assert int(kept[0]) == 0


def test_nms_does_not_suppress_different_classes():
  left = np.array([0.0, 1.0])
  top = np.array([0.0, 1.0])
  right = np.array([10.0, 11.0])
  bottom = np.array([10.0, 11.0])
  scores = np.array([0.9, 0.8])
  classes = np.array([1, 5])
  kept = non_maximum_suppression(left, top, right, bottom, scores, classes, 0.5)
  assert len(kept) == 2


def test_missing_model_reports_invalid_without_raising():
  inference = LaneInference("/nonexistent/lane.onnx")
  assert not inference.load()
  result = inference.infer(np.zeros((416, 416), dtype=np.uint8), 416, 416)
  assert result["valid"] is False
  assert result["leftLine"] == -1
  assert result["rightLine"] == -1


@pytest.mark.skipif(cv2 is None, reason="OpenCV is not installed")
@pytest.mark.skipif(not DEFAULT_LANE_MODEL_PATH.is_file(), reason="lane.onnx is missing")
def test_model_smoke_on_a_synthetic_road():
  image = np.full((760, 1344), 90, dtype=np.uint8)
  cv2.line(image, (500, 760), (620, 200), 220, 6)
  cv2.line(image, (840, 760), (720, 200), 220, 6)

  inference = LaneInference(DEFAULT_LANE_MODEL_PATH)
  assert inference.load(), inference.error
  result = inference.infer(image, image.shape[1], image.shape[0])
  assert result["valid"] is True, result["error"]
  assert result["leftLine"] in (-1, 0, 1)
  assert result["rightLine"] in (-1, 0, 1)
  assert result["candidatesCount"] >= 2
