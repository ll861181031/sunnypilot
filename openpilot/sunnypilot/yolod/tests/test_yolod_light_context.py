#!/usr/bin/env python3
"""Tests for yolod's light-watching glue.

`LightContext` is the only place where images, the classifier and cereal meet, so
it is exactly where a failure would be silent. Every case here asserts the same
thing from a different angle: when anything is missing the published state is
empty, and an empty state is what keeps the planner's guard at stock behaviour.
"""

import numpy as np
import pytest

from openpilot.sunnypilot.yolod.detector import Detection
from openpilot.sunnypilot.yolod.yolod import LightContext, build_yolo_message, traffic_light_class_ids


class _FakeDetector:
  def __init__(self, names):
    self.class_names = names


class _FakePrediction:
  def __init__(self, label):
    self.label = label
    self.prob = 0.99


class _FakeClassifier:
  def __init__(self, label="red"):
    self.label = label
    self.calls = 0

  @property
  def available(self):
    return True

  def classify(self, crop):
    self.calls += 1
    return _FakePrediction(self.label) if crop.size else None


class _FakeLines:
  def __init__(self, xs, ys):
    self.x = list(xs)
    self.y = list(ys)
    self.z = [0.0] * len(xs)


class _FakeModel:
  def __init__(self, half_width=1.75):
    xs = list(np.arange(5.0, 101.0, 1.0))
    self.laneLines = [_FakeLines(xs, [half_width] * len(xs)),
                      _FakeLines(xs, [half_width] * len(xs)),
                      _FakeLines(xs, [-half_width] * len(xs)),
                      _FakeLines(xs, [-half_width] * len(xs))]


class _FakeCalib:
  rpyCalib = [0.0, 0.0646, 0.0094]


class _FakeSM(dict):
  def __init__(self, model=None, calib=None, valid=True, seen=True):
    super().__init__()
    self.valid = {'modelV2': valid and model is not None,
                  'extrinsicsCalibration': valid and calib is not None}
    self.seen = {'modelV2': seen and model is not None,
                 'extrinsicsCalibration': seen and calib is not None}
    if model is not None:
      self['modelV2'] = model
    if calib is not None:
      self['extrinsicsCalibration'] = calib

  def update(self):
    return None


def _context(label="red", sm=None, min_agree=1):
  from openpilot.sunnypilot.yolod.light_veto import EgoLightWatch
  context = LightContext.__new__(LightContext)
  context.classifier = _FakeClassifier(label)
  context.watch = EgoLightWatch(min_agree=min_agree)
  context.sm = sm
  context._rpy = None
  return context


def _frame(width=1344, height=760):
  return np.full((height, width, 3), 90, np.uint8)


def _light_box(width=1344, height=760, distance=40.0, lateral=0.0):
  focal = 1864.0 * width / 1344.0
  slope = -lateral / distance
  u = width / 2.0 + slope * focal
  h = focal * 1.0 / distance
  return Detection(class_id=0, label="traffic_light", score=0.9,
                   box=((u - 8.0) / width, (height / 2.0 - h / 2.0) / height,
                        (u + 8.0) / width, (height / 2.0 + h / 2.0) / height))


class TestClassIdMatching:
  def test_matches_the_usual_spellings(self):
    assert traffic_light_class_ids(_FakeDetector(["person", "traffic light", "car"])) == {1}
    assert traffic_light_class_ids(_FakeDetector(["traffic_light"])) == {0}
    assert traffic_light_class_ids(_FakeDetector(["TrafficLight"])) == {0}

  def test_a_detector_without_lights_matches_nothing(self):
    assert traffic_light_class_ids(_FakeDetector(["person", "car"])) == set()


class TestEvaluate:
  def test_returns_empty_without_lane_lines(self):
    context = _context(sm=_FakeSM(model=None))
    for _ in range(5):
      assert context.evaluate([_light_box()], _frame(), 1344, 760, {0}) == ("", 0, -1.0)

  def test_returns_empty_without_a_sub_master(self):
    context = _context(sm=None)
    assert context.evaluate([_light_box()], _frame(), 1344, 760, {0}) == ("", 0, -1.0)

  def test_returns_empty_when_the_model_is_invalid(self):
    context = _context(sm=_FakeSM(model=_FakeModel(), calib=_FakeCalib(), valid=False))
    assert context.evaluate([_light_box()], _frame(), 1344, 760, {0}) == ("", 0, -1.0)

  def test_returns_empty_without_lights(self):
    context = _context(sm=_FakeSM(model=_FakeModel(), calib=_FakeCalib()))
    car = Detection(class_id=1, label="car", score=0.9, box=(0.1, 0.1, 0.2, 0.2))
    assert context.evaluate([car], _frame(), 1344, 760, {0}) == ("", 0, -1.0)

  def test_reports_the_ego_light_once_settled(self):
    context = _context(sm=_FakeSM(model=_FakeModel(), calib=_FakeCalib()), min_agree=3)
    box = _light_box()
    assert context.evaluate([box], _frame(), 1344, 760, {0}) == ("", 0, -1.0)
    assert context.evaluate([box], _frame(), 1344, 760, {0}) == ("", 0, -1.0)
    state, frames, distance = context.evaluate([box], _frame(), 1344, 760, {0})
    assert state == "red"
    assert frames == 3
    assert distance == pytest.approx(40.0, rel=0.1)

  def test_skips_tiny_boxes_instead_of_crashing(self):
    context = _context(sm=_FakeSM(model=_FakeModel(), calib=_FakeCalib()))
    tiny = Detection(class_id=0, label="traffic_light", score=0.5, box=(0.5, 0.5, 0.5001, 0.5001))
    assert context.evaluate([tiny], _frame(), 1344, 760, {0}) == ("", 0, -1.0)

  def test_a_green_light_settles_as_green(self):
    context = _context(label="green", sm=_FakeSM(model=_FakeModel(), calib=_FakeCalib()),
                       min_agree=2)
    box = _light_box()
    context.evaluate([box], _frame(), 1344, 760, {0})
    state, _, _ = context.evaluate([box], _frame(), 1344, 760, {0})
    assert state == "green"


class TestPublishedMessage:
  def test_light_fields_round_trip(self):
    from openpilot.cereal import log
    message = build_yolo_message([_light_box()], ["traffic_light"], frame_id=1,
                                 frame_width=1344, frame_height=760, inference_ms=20.0,
                                 model_name="traffic_light_det.onnx")
    message.yoloObjects.egoLightState = "red"
    message.yoloObjects.egoLightFrames = 4
    message.yoloObjects.egoLightDistance = 42.5
    with log.Event.from_bytes(message.to_bytes()) as event:
      payload = event.yoloObjects
      assert payload.egoLightState == "red"
      assert payload.egoLightFrames == 4
      assert payload.egoLightDistance == pytest.approx(42.5, abs=1e-5)

  def test_default_message_has_no_light_state(self):
    from openpilot.cereal import log
    message = build_yolo_message([], ["traffic_light"], frame_id=0, frame_width=1344,
                                 frame_height=760, inference_ms=1.0, model_name="x.onnx")
    with log.Event.from_bytes(message.to_bytes()) as event:
      # Empty state is the "no information" signal the planner tests rely on.
      assert event.yoloObjects.egoLightState == ""
      assert event.yoloObjects.egoLightFrames == 0
