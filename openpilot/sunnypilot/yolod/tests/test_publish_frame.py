#!/usr/bin/env python3
"""The publish glue: association result must reach the capnp message intact.

This is the one seam that nothing else covers. ``LightContext`` is tested on its own,
the planner's consumer is tested on its own, and the message schema round-trips - but
the three lines that copy ``evaluate()``'s tuple into ``yoloObjects`` were never
executed. A typo there would leave the planner permanently blind while every other
test stayed green.
"""


import numpy as np
import pytest

from openpilot.sunnypilot.yolod.detector import Detection
from openpilot.sunnypilot.yolod.yolod import _publish_frame


class _FakeDetector:
  class_names = ["traffic_light"]
  inference_ms = 14.7

  def __init__(self, detections):
    self._detections = detections

  def detect(self, bgr):
    return list(self._detections)


class _FakeLight:
  def __init__(self, result):
    self.result = result
    self.calls = []

  def evaluate(self, detections, bgr, width, height, light_ids):
    self.calls.append((len(detections), width, height, set(light_ids)))
    return self.result


class _FakePM:
  def __init__(self):
    self.sent = []

  def send(self, service, message):
    self.sent.append((service, message))


def _detection(cx=0.5, label="traffic_light"):
  return Detection(class_id=0, label=label, score=0.8, box=(cx - 0.01, 0.3, cx + 0.01, 0.5))


def _call(detections, light=None, light_ids=None, centre_gate=None, width=1344, height=760):
  pm = _FakePM()
  bgr = np.zeros((height, width, 3), dtype=np.uint8)
  published = _publish_frame(pm, _FakeDetector(detections), bgr, 7, width, height,
                             "traffic_light_det.onnx", light=light,
                             light_ids=light_ids, centre_gate=centre_gate)
  assert len(pm.sent) == 1
  service, message = pm.sent[0]
  assert service == "yoloObjects"
  return published, message.yoloObjects


def test_the_light_verdict_reaches_the_message():
  light = _FakeLight(("red", 5, 32.5))
  _, out = _call([_detection()], light=light, light_ids={0})
  assert out.egoLightState == "red"
  assert out.egoLightFrames == 5
  assert out.egoLightDistance == pytest.approx(32.5)
  assert light.calls == [(1, 1344, 760, {0})]


def test_without_a_light_the_fields_are_the_empty_defaults():
  """No association means "no information", never a guessed state."""
  _, out = _call([_detection()], light=None, light_ids={0})
  assert out.egoLightState == ""
  assert out.egoLightFrames == 0
  assert out.egoLightDistance == pytest.approx(-1.0)


def test_without_light_ids_the_context_is_not_consulted():
  light = _FakeLight(("red", 5, 32.5))
  _, out = _call([_detection()], light=light, light_ids=set())
  assert light.calls == []
  assert out.egoLightState == ""


def test_the_association_sees_every_detection_even_when_the_gate_hides_them():
  """The gate narrows the published list, not the input to the association.

  Filtering first would hide the neighbouring lane's light from the lane geometry
  that decides which light is ego's.
  """
  light = _FakeLight(("green", 4, 20.0))
  detections = [_detection(cx=0.5), _detection(cx=0.9)]
  published, out = _call(detections, light=light, light_ids={0}, centre_gate=0.1)
  assert light.calls[0][0] == 2, "association must see both lights"
  assert len(published) == 1, "the gate must drop the far one"
  assert len(out.objects) == 1


def test_the_verdict_survives_everything_being_gated_away():
  """A gated-out detection list must not clear a real verdict."""
  light = _FakeLight(("red", 5, 32.5))
  published, out = _call([_detection(cx=0.95)], light=light, light_ids={0}, centre_gate=0.05)
  assert published == []
  assert out.egoLightState == "red"
  assert out.egoLightFrames == 5
