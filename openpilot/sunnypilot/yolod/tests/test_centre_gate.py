#!/usr/bin/env python3
"""The optional lateral gate on published detections.

The detector reports every traffic light in the frame, including the ones over
the neighbouring lanes and across the junction. That is deliberate - the lane
association needs them to decide which light is ego's - but sometimes you want the
published list itself narrowed to what is straight ahead. This gate does only
that; it must never influence the ego assignment.
"""

import pytest

from openpilot.sunnypilot.yolod.detector import Detection
from openpilot.sunnypilot.yolod.yolod import centre_gate_filter


def detection(cx: float, width: float = 0.02) -> Detection:
  """A box centred at normalised ``cx``."""
  return Detection(class_id=0, label="traffic_light", score=0.9,
                   box=(cx - width / 2, 0.3, cx + width / 2, 0.5))


def test_off_keeps_everything():
  items = [detection(0.5), detection(0.05), detection(0.95)]
  assert centre_gate_filter(items, None) == items


def test_only_near_centre_survives():
  items = [detection(0.50), detection(0.56), detection(0.62), detection(0.80)]
  kept = centre_gate_filter(items, 0.10)
  assert [d.box[0] + d.box[2] for d in kept] == pytest.approx([1.00, 1.12])


def test_the_gate_is_symmetric():
  left = centre_gate_filter([detection(0.38)], 0.10)
  right = centre_gate_filter([detection(0.62)], 0.10)
  assert left == [] and right == []


def test_a_gate_of_zero_keeps_only_the_exact_middle():
  assert centre_gate_filter([detection(0.5)], 0.0) != []
  assert centre_gate_filter([detection(0.51)], 0.0) == []


def test_everything_can_be_gated_away():
  assert centre_gate_filter([detection(0.1), detection(0.9)], 0.05) == []
