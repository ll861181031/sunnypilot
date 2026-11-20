"""yoloObjects message construction and cereal roundtrip tests."""

import pytest

from openpilot.cereal import log
from openpilot.sunnypilot.yolod.detector import Detection
from openpilot.sunnypilot.yolod.yolod import MAX_OBJECTS, build_yolo_message

CLASS_NAMES = ["person", "car", "motorcycle"]


def _detections() -> list[Detection]:
  return [
    Detection(class_id=2, label="motorcycle", score=0.75, box=(0.1, 0.2, 0.3, 0.5)),
    Detection(class_id=0, label="person", score=0.5, box=(0.4, 0.1, 0.5, 0.6)),
  ]


def test_build_yolo_message_roundtrips_as_a_log_event():
  message = build_yolo_message(_detections(), CLASS_NAMES, frame_id=42, frame_width=1928,
                               frame_height=1208, inference_ms=37.5, model_name="yolo11n.onnx")
  assert message.which() == "yoloObjects"
  raw = message.to_bytes()
  with log.Event.from_bytes(raw) as event:
    assert event.which() == "yoloObjects"
    assert event.valid is True
    payload = event.yoloObjects
    assert payload.frameId == 42
    assert payload.frameWidth == 1928
    assert payload.frameHeight == 1208
    assert payload.inferenceMs == pytest.approx(37.5, abs=1e-5)
    assert payload.model == "yolo11n.onnx"
    assert list(payload.classNames) == CLASS_NAMES
    assert len(payload.objects) == 2

    first = payload.objects[0]
    assert first.classId == 2
    assert first.confidence == pytest.approx(0.75, abs=1e-6)
    # The wire format carries top-left x/y plus w/h, not xyxy.
    assert (first.x, first.y, first.w, first.h) == pytest.approx((0.1, 0.2, 0.2, 0.3), abs=1e-6)
    second = payload.objects[1]
    assert second.classId == 0
    assert (second.x, second.y, second.w, second.h) == pytest.approx((0.4, 0.1, 0.1, 0.5), abs=1e-6)


def test_build_yolo_message_handles_empty_payloads():
  message = build_yolo_message([], [], frame_id=0, frame_width=1344, frame_height=760,
                               inference_ms=0.0, model_name="m.onnx")
  with log.Event.from_bytes(message.to_bytes()) as event:
    assert list(event.yoloObjects.classNames) == []
    assert len(event.yoloObjects.objects) == 0


def test_build_yolo_message_caps_object_count():
  detections = [Detection(class_id=0, label="person", score=0.9, box=(0.0, 0.0, 0.1, 0.1))] * (MAX_OBJECTS + 20)
  message = build_yolo_message(detections, CLASS_NAMES, frame_id=1, frame_width=640, frame_height=480,
                               inference_ms=1.0, model_name="m.onnx")
  with log.Event.from_bytes(message.to_bytes()) as event:
    assert len(event.yoloObjects.objects) == MAX_OBJECTS

  small = build_yolo_message(detections, CLASS_NAMES, frame_id=1, frame_width=640, frame_height=480,
                             inference_ms=1.0, model_name="m.onnx", max_objects=3)
  with log.Event.from_bytes(small.to_bytes()) as event:
    assert len(event.yoloObjects.objects) == 3


def test_build_yolo_message_keeps_full_class_names_with_filtered_objects():
  detections = [Detection(class_id=2, label="motorcycle", score=0.9, box=(0.0, 0.0, 0.1, 0.1))]
  message = build_yolo_message(detections, CLASS_NAMES, frame_id=5, frame_width=640, frame_height=480,
                               inference_ms=2.0, model_name="m.onnx")
  with log.Event.from_bytes(message.to_bytes()) as event:
    # classId indexes into classNames even when only one class is published.
    assert list(event.yoloObjects.classNames) == CLASS_NAMES
    assert event.yoloObjects.objects[0].classId == 2


def test_message_goes_through_msgq_as_a_log_event():
  """The real transport carries a log.Event, not a bare custom.YoloObjects."""
  import time

  from openpilot.cereal.messaging import PubMaster, sub_sock

  def message():
    return build_yolo_message(_detections(), CLASS_NAMES, frame_id=7, frame_width=1344,
                              frame_height=760, inference_ms=12.5, model_name="yolo11n.onnx")

  subscriber = sub_sock("yoloObjects", conflate=True, timeout=500)
  publisher = PubMaster(["yoloObjects"])
  raw = None
  deadline = time.monotonic() + 2.0
  while raw is None and time.monotonic() < deadline:
    # ZMQ connects asynchronously, so sending a few times is more reliable than
    # sleeping a fixed amount; the queue is conflated and only the last one matters.
    publisher.send("yoloObjects", message())
    raw = subscriber.receive()
  assert raw is not None, "no yoloObjects message arrived on msgq"
  with log.Event.from_bytes(raw) as event:
    assert event.valid is True
    assert event.yoloObjects.frameId == 7
    assert event.yoloObjects.classNames[2] == "motorcycle"
    assert len(event.yoloObjects.objects) == 2
