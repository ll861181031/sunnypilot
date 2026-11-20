"""Detector tests: letterbox mapping, YOLO decoding, NMS and class filtering."""

import numpy as np
import pytest

from openpilot.sunnypilot.yolod.detector import (
  LetterboxTransform,
  decode_output,
  letterbox,
  nms,
  parse_class_names,
  parse_imgsz,
  preprocess,
  resolve_class_filter,
)

NAMES = {0: "person", 1: "bicycle", 2: "car", 3: "motorcycle"}


def _transform(width: int = 1928, height: int = 1208) -> LetterboxTransform:
  _, transform = letterbox(np.zeros((height, width, 3), dtype=np.uint8))
  return transform


def _prediction_output(entries, num_names: int = len(NAMES), num_boxes: int = 100,
                       objectness: float | None = None) -> np.ndarray:
  """Build a YOLO prediction tensor. Entries are ((cx, cy, w, h), class_id, score)."""
  attrs = 4 + (0 if objectness is None else 1) + num_names
  output = np.zeros((1, attrs, num_boxes), dtype=np.float32)
  for index, (box, class_id, score) in enumerate(entries):
    output[0, 0:4, index] = box
    if objectness is None:
      output[0, 4 + class_id, index] = score
    else:
      output[0, 4, index] = objectness
      output[0, 5 + class_id, index] = score
  return output


def _letterbox_box(x1: float, y1: float, x2: float, y2: float) -> tuple[float, float, float, float]:
  return (x1 + x2) / 2.0, (y1 + y2) / 2.0, x2 - x1, y2 - y1


def test_letterbox_keeps_aspect_ratio_and_pads_with_114():
  image = np.full((1208, 1928, 3), 200, dtype=np.uint8)
  padded, transform = letterbox(image)
  assert padded.shape == (640, 640, 3)
  assert transform.scale_x == pytest.approx(640 / 1928)
  assert transform.scale_y == pytest.approx(round(1208 * (640 / 1928)) / 1208)
  assert transform.pad_x == 0
  assert transform.pad_y == (640 - round(1208 * (640 / 1928))) // 2
  assert np.all(padded[:transform.pad_y] == 114)
  assert np.all(padded[transform.pad_y + round(1208 * transform.scale_y):] == 114)
  content = padded[transform.pad_y:transform.pad_y + round(1208 * transform.scale_y)]
  assert np.all(content == 200)


def test_letterbox_pads_horizontally_for_tall_input():
  image = np.zeros((640, 480, 3), dtype=np.uint8)
  padded, transform = letterbox(image)
  assert transform.pad_y == 0
  assert transform.pad_x == 80
  assert np.all(padded[:, :80] == 114)
  assert np.all(padded[:, 80 + 480:] == 114)


def test_letterbox_mapping_roundtrip():
  transform = _transform(1000, 500)
  for point in [(0.0, 0.0), (1.0, 1.0), (0.25, 0.75), (500.0, 250.0)]:
    x = point[0] * transform.scale_x + transform.pad_x
    y = point[1] * transform.scale_y + transform.pad_y
    assert transform.to_source((x, y, x, y)) == pytest.approx((point[0], point[1], point[0], point[1]), abs=1e-6)


def test_letterbox_normalized_corner_is_exact():
  transform = _transform()
  source = transform.to_source((transform.pad_x, transform.pad_y, transform.pad_x, transform.pad_y))
  assert source == pytest.approx((0.0, 0.0, 0.0, 0.0), abs=1e-6)
  corner = (transform.pad_x + round(1928 * transform.scale_x),
            transform.pad_y + round(1208 * transform.scale_y))
  source = transform.to_source((*corner, *corner))
  assert source == pytest.approx((1928.0, 1208.0, 1928.0, 1208.0), abs=1e-3)


def test_letterbox_rejects_non_bgr_input():
  with pytest.raises(ValueError):
    letterbox(np.zeros((100, 100), dtype=np.uint8))


def test_preprocess_builds_rgb_nchw_blob():
  image = np.zeros((240, 320, 3), dtype=np.uint8)
  image[..., 0] = 255  # pure blue in BGR
  blob, transform = preprocess(image, (320, 240))
  assert blob.shape == (1, 3, 240, 320)
  assert blob.dtype == np.float32
  assert blob.flags["C_CONTIGUOUS"]
  assert transform.pad_x == 0 and transform.pad_y == 0
  assert np.allclose(blob[0, 2], 1.0)   # blue ended up in the RGB blue channel
  assert np.allclose(blob[0, 0], 0.0)   # and not in red
  assert np.allclose(blob[0, 1], 0.0)


def test_decode_maps_box_back_to_normalized_source_coordinates():
  transform = _transform()
  box = _letterbox_box(100, 200, 300, 400)
  output = _prediction_output([(box, 2, 0.9)], num_boxes=20)
  detections = decode_output(output, transform, conf=0.25, names=NAMES)
  assert len(detections) == 1
  detection = detections[0]
  assert detection.class_id == 2
  assert detection.label == "car"
  assert detection.score == pytest.approx(0.9, abs=1e-6)
  expected = transform.to_normalized((100, 200, 300, 400))
  assert detection.box == pytest.approx(expected, abs=1e-6)
  assert 0.0 <= detection.box[0] < detection.box[2] <= 1.0
  assert detection.xywh[2] == pytest.approx(detection.box[2] - detection.box[0], abs=1e-7)


def test_decode_respects_confidence_threshold():
  transform = _transform()
  box = _letterbox_box(10, 200, 110, 300)
  output = _prediction_output([(box, 0, 0.30), (box, 1, 0.20)], num_boxes=10)
  assert len(decode_output(output, transform, conf=0.25, names=NAMES)) == 1
  assert len(decode_output(output, transform, conf=0.30, names=NAMES)) == 1  # >= is kept
  assert len(decode_output(output, transform, conf=0.31, names=NAMES)) == 0


def test_decode_class_filter_keeps_only_requested_classes():
  transform = _transform()
  output = _prediction_output([(_letterbox_box(10, 200, 110, 300), 0, 0.9),
                               (_letterbox_box(200, 200, 300, 300), 2, 0.8)], num_boxes=10)
  detections = decode_output(output, transform, conf=0.25, names=NAMES, class_filter=[2])
  assert [detection.label for detection in detections] == ["car"]


def test_decode_suppresses_overlapping_boxes_of_the_same_class():
  transform = _transform()
  output = _prediction_output([(_letterbox_box(10, 200, 110, 300), 2, 0.9),
                               (_letterbox_box(20, 210, 120, 310), 2, 0.8)], num_boxes=10)
  detections = decode_output(output, transform, conf=0.25, iou=0.45, names=NAMES)
  assert len(detections) == 1
  assert detections[0].score == pytest.approx(0.9, abs=1e-6)


def test_decode_keeps_overlapping_boxes_of_different_classes():
  transform = _transform()
  output = _prediction_output([(_letterbox_box(10, 200, 110, 300), 2, 0.9),
                               (_letterbox_box(20, 210, 120, 310), 3, 0.8)], num_boxes=10)
  detections = decode_output(output, transform, conf=0.25, names=NAMES)
  assert sorted(detection.label for detection in detections) == ["car", "motorcycle"]


def test_decode_keeps_separate_boxes_of_the_same_class():
  transform = _transform()
  output = _prediction_output([(_letterbox_box(10, 200, 110, 300), 2, 0.9),
                               (_letterbox_box(400, 300, 500, 400), 2, 0.8)], num_boxes=10)
  assert len(decode_output(output, transform, conf=0.25, names=NAMES)) == 2


def test_decode_handles_transposed_prediction_layout():
  transform = _transform()
  box = _letterbox_box(100, 200, 300, 400)
  output = _prediction_output([(box, 2, 0.9)], num_boxes=20)
  transposed = np.ascontiguousarray(output.transpose(0, 2, 1))
  regular = decode_output(output, transform, conf=0.25, names=NAMES)
  flipped = decode_output(transposed, transform, conf=0.25, names=NAMES)
  assert len(regular) == len(flipped) == 1
  assert regular[0].box == pytest.approx(flipped[0].box, abs=1e-6)


def test_decode_handles_objectness_layout():
  transform = _transform()
  small_names = {0: "a", 1: "b"}
  box = _letterbox_box(100, 200, 300, 400)
  output = _prediction_output([(box, 1, 0.8)], num_names=len(small_names), num_boxes=10, objectness=0.5)
  detections = decode_output(output, transform, conf=0.25, names=small_names)
  assert len(detections) == 1
  assert detections[0].label == "b"
  assert detections[0].score == pytest.approx(0.4, abs=1e-6)  # objectness * class score


def test_decode_handles_end_to_end_export():
  transform = _transform()
  output = np.zeros((1, 300, 6), dtype=np.float32)
  output[0, 0] = [100, 200, 300, 400, 0.85, 2]
  output[0, 1] = [400, 200, 500, 400, 0.55, 3]
  detections = decode_output(output, transform, conf=0.25, names=NAMES)
  assert [detection.label for detection in detections] == ["car", "motorcycle"]
  assert detections[0].box == pytest.approx(transform.to_normalized((100, 200, 300, 400)), abs=1e-6)


def test_decode_clamps_boxes_to_the_frame():
  transform = _transform()
  box = _letterbox_box(-100, 200, 100, 400)
  output = _prediction_output([(box, 2, 0.9)], num_boxes=4)
  detections = decode_output(output, transform, conf=0.25, names=NAMES)
  assert len(detections) == 1
  x1, y1, x2, y2 = detections[0].box
  assert x1 == 0.0  # left edge was outside the frame and got clamped
  assert y1 == pytest.approx(transform.to_normalized((0, 200, 0, 200))[1], abs=1e-6)
  assert x2 > x1 and y2 > y1


def test_decode_rejects_unexpected_shapes():
  transform = _transform()
  with pytest.raises(ValueError):
    decode_output(np.zeros((2, 84, 8400), dtype=np.float32), transform, names=NAMES)
  with pytest.raises(ValueError):
    decode_output(np.zeros((1, 84, 8400), dtype=np.float32), transform, names=NAMES)  # 84 vs 4 names


def test_decode_rejects_conflicting_class_count():
  transform = _transform()
  output = np.zeros((1, 4 + len(NAMES) - 1, 10), dtype=np.float32)
  output[0, 4, 0] = 0.9
  with pytest.raises(ValueError, match="class scores"):
    decode_output(output, transform, names=NAMES)


def test_nms_is_class_wise_and_thresholded():
  boxes = np.array([[0, 0, 10, 10], [1, 1, 11, 11], [20, 20, 30, 30], [0, 0, 10, 10]], dtype=np.float32)
  scores = np.array([0.9, 0.8, 0.7, 0.6], dtype=np.float32)
  class_ids = np.array([0, 0, 0, 1])
  kept = nms(boxes, scores, 0.45, class_ids)
  assert kept == [0, 2, 3]
  keepless = nms(np.zeros((0, 4)), np.zeros((0,)), 0.45)
  assert keepless == []
  with pytest.raises(ValueError):
    nms(np.zeros((2, 4)), np.zeros((3,)), 0.45)
  with pytest.raises(ValueError):
    nms(boxes, scores, -1.0)


def test_parse_class_names_accepts_python_and_json_metadata():
  assert parse_class_names("{0: 'person', 1: 'car'}") == {0: "person", 1: "car"}
  assert parse_class_names('{"0": "person", "1": "car"}') == {0: "person", 1: "car"}
  assert parse_class_names("['person', 'car']") == {0: "person", 1: "car"}
  assert parse_class_names(None) == {}
  assert parse_class_names("not metadata") == {}


def test_parse_imgsz_accepts_ultralytics_formats():
  assert parse_imgsz("[640, 640]") == (640, 640)
  assert parse_imgsz("[480, 640]") == (480, 640)
  assert parse_imgsz("640") == (640, 640)
  assert parse_imgsz(None) is None
  assert parse_imgsz("nonsense") is None


def test_resolve_class_filter_reports_unknown_names():
  ids, unmatched = resolve_class_filter(["person", "car"], NAMES)
  assert ids == [0, 2]
  assert unmatched == []
  ids, unmatched = resolve_class_filter(["person", "unicorn"], NAMES)
  assert ids == [0]
  assert unmatched == ["unicorn"]
  ids, unmatched = resolve_class_filter([1, "3"], NAMES)
  assert ids == [1, 3]
  assert unmatched == []
  assert resolve_class_filter(None, NAMES) == ([], [])
  assert resolve_class_filter([], NAMES) == ([], [])
