"""Traffic-light state classifier: metadata handling, preprocessing and picking.

The model used here is synthetic on purpose - a linear colour-prototype head
exported with real ONNX metadata. That makes the test independent of any trained
weights while still exercising the whole path the car actually runs: metadata
parsing, resize/normalise, ONNX session, argmax and label lookup.
"""

import json

import numpy as np
import pytest

from openpilot.sunnypilot.yolod.traffic_light_classifier import (
  DEFAULT_MODEL,
  FALLBACK_LABELS,
  TrafficLightStateClassifier,
  StatePrediction,
  load_model_spec,
  most_dangerous,
)

# Prototype colour each label should win on, in normalised (x/255-0.5)/0.5 space.
PROTOTYPES = {
  "red": (1.0, -1.0, -1.0),
  "yellow": (1.0, 1.0, -1.0),
  "green": (-1.0, 1.0, -1.0),
  "off": (-1.0, -1.0, -1.0),
  "wait_on": (0.6, 0.6, 0.6),
}

# BGR, because crops arrive from OpenCV.
CROP_COLOURS = {
  "red": (0, 0, 255),
  "yellow": (0, 255, 255),
  "green": (0, 255, 0),
  "off": (0, 0, 0),
  "wait_on": (153, 153, 153),
}


def _build_onnx(path, labels, height=64, width=64, mean=0.5, std=0.5, with_metadata=True,
                with_softmax=True):
  """Reduce the image to per-channel means, score them against colour prototypes."""
  import onnx
  from onnx import TensorProto, helper

  # (num_classes, 3) prototypes transposed to (3, num_classes) so MatMul works
  # against the (1, 3) per-channel mean vector.
  weights = np.array([PROTOTYPES[label] for label in labels], dtype=np.float32).T

  nodes = [
    helper.make_node("ReduceMean", ["input"], ["means"], axes=[2, 3], keepdims=0),
    helper.make_node("MatMul", ["means", "weights"], ["logits"]),
  ]
  if with_softmax:
    # What a torch export looks like when the model is followed by Softmax.
    nodes.append(helper.make_node("Softmax", ["logits"], ["probs"], axis=1))
  else:
    # What torch.onnx.export produces for this project's classifier: the graph
    # ends at Linear, so `probs` here is really raw logits.
    nodes.append(helper.make_node("Identity", ["logits"], ["probs"]))
  graph = helper.make_graph(
    nodes,
    "colour_prototype",
    [helper.make_tensor_value_info("input", TensorProto.FLOAT, [1, 3, height, width])],
    [helper.make_tensor_value_info("probs", TensorProto.FLOAT, [1, len(labels)])],
    [helper.make_tensor("weights", TensorProto.FLOAT, list(weights.shape), weights.flatten().tolist())],
  )
  model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)])
  model.ir_version = 10

  if with_metadata:
    for key, value in (("labels", json.dumps(list(labels))), ("height", str(height)),
                       ("width", str(width)), ("mean", str(mean)), ("std", str(std))):
      entry = model.metadata_props.add()
      entry.key, entry.value = key, value

  onnx.save(model, str(path))
  return path


def _crop(colour, size=64):
  return np.full((size, size, 3), 0, dtype=np.uint8) + np.array(colour, dtype=np.uint8)


@pytest.fixture
def model_file(tmp_path):
  def make(labels=("red", "yellow", "green", "off", "wait_on"), **kwargs):
    path = tmp_path / f"tl_state_{'_'.join(labels)}.onnx"
    _build_onnx(path, labels, **kwargs)
    return path
  return make


class TestReadsTheStateOfALampCrop:
  def test_each_colour_maps_to_its_own_label(self, model_file):
    classifier = TrafficLightStateClassifier(model_file())
    for label, colour in CROP_COLOURS.items():
      prediction = classifier.classify(_crop(colour))
      assert prediction is not None, label
      assert prediction.label == label

  def test_confidence_is_a_probability(self, model_file):
    classifier = TrafficLightStateClassifier(model_file())
    prediction = classifier.classify(_crop(CROP_COLOURS["red"]))
    assert 0.0 <= prediction.prob <= 1.0

  def test_a_red_light_is_never_reported_as_green(self, model_file):
    """The one wrong answer that matters. Checked across many red shades."""
    classifier = TrafficLightStateClassifier(model_file())
    for value in (90, 140, 190, 230, 255):
      prediction = classifier.classify(_crop((0, 0, value)))
      assert prediction is not None
      assert prediction.label != "green"


class TestMetadataDecidesTheContract:
  def test_label_order_comes_from_the_file_not_from_a_constant(self, model_file, tmp_path):
    """A model exported with a different order must still be read correctly."""
    reversed_labels = tuple(reversed(FALLBACK_LABELS))
    classifier = TrafficLightStateClassifier(model_file(labels=reversed_labels))
    assert classifier.labels == reversed_labels
    assert classifier.classify(_crop(CROP_COLOURS["green"])).label == "green"

  def test_input_size_is_taken_from_the_file(self, model_file):
    classifier = TrafficLightStateClassifier(model_file(labels=FALLBACK_LABELS,
                                                        height=32, width=48))
    assert classifier.available
    assert classifier.classify(_crop(CROP_COLOURS["red"], size=64)) is not None

  def test_normalisation_is_taken_from_the_file(self, tmp_path):
    """mean=0/std=1 means the network sees raw 0..1 values; colours must still map."""
    path = _build_onnx(tmp_path / "raw.onnx", FALLBACK_LABELS, mean=0.0, std=1.0)
    classifier = TrafficLightStateClassifier(path)
    # With mean 0 the red channel is 1.0 and the others 0.0, which is not a
    # prototype colour, so only availability and shape handling is asserted here.
    assert classifier.available
    assert classifier.classify(_crop(CROP_COLOURS["red"])) is not None

  def test_refuses_to_guess_when_metadata_is_missing(self, tmp_path):
    """No labels/mean/std means no verdict - silently assuming defaults is worse."""
    path = _build_onnx(tmp_path / "bare.onnx", FALLBACK_LABELS, with_metadata=False)
    classifier = TrafficLightStateClassifier(path)
    assert not classifier.available
    assert classifier.load_error is not None
    assert classifier.classify(_crop(CROP_COLOURS["red"])) is None

  def test_sidecar_json_is_accepted_alone(self, tmp_path):
    path = _build_onnx(tmp_path / "sidecar.onnx", FALLBACK_LABELS, with_metadata=False)
    path.with_suffix(".json").write_text(json.dumps({
      "labels": list(FALLBACK_LABELS), "height": 64, "width": 64,
      "mean": 0.5, "std": 0.5,
    }), encoding="utf-8")
    classifier = TrafficLightStateClassifier(path)
    assert classifier.available
    assert classifier.classify(_crop(CROP_COLOURS["green"])).label == "green"

  def test_spec_loader_reports_none_for_a_missing_file(self, tmp_path):
    assert load_model_spec(tmp_path / "absent.onnx") is None


class TestBadInputIsRejected:
  def test_missing_model_file_is_not_an_error(self, tmp_path):
    classifier = TrafficLightStateClassifier(tmp_path / "absent.onnx")
    assert not classifier.available
    assert classifier.classify(_crop(CROP_COLOURS["red"])) is None

  def test_default_model_path_points_into_the_package(self):
    assert DEFAULT_MODEL.name == "traffic_light_state.onnx"
    assert DEFAULT_MODEL.parent.name == "models"

  @pytest.mark.parametrize("crop", [
    None,
    np.zeros((2, 2, 3), dtype=np.uint8),        # far too small to be a lamp
    np.zeros((64, 64), dtype=np.uint8),          # grayscale, not BGR
    np.zeros((64, 64, 4), dtype=np.uint8),       # RGBA
    np.zeros((0, 0, 3), dtype=np.uint8),
  ])
  def test_degenerate_crops_return_none(self, model_file, crop):
    classifier = TrafficLightStateClassifier(model_file())
    assert classifier.classify(crop) is None

  def test_batch_covers_every_item(self, model_file):
    classifier = TrafficLightStateClassifier(model_file())
    crops = [_crop(CROP_COLOURS["red"]), None, _crop(CROP_COLOURS["green"])]
    results = classifier.classify_batch(crops)
    assert len(results) == 3
    assert results[0].label == "red"
    assert results[1] is None
    assert results[2].label == "green"


class TestLogitsAreNormalisedNotDivided:
  """The exported classifier stops at Linear, so its output is raw logits."""

  def test_logits_become_probabilities(self, tmp_path):
    path = _build_onnx(tmp_path / "logits.onnx", FALLBACK_LABELS, with_softmax=False)
    classifier = TrafficLightStateClassifier(path)
    assert classifier.available
    prediction = classifier.classify(_crop(CROP_COLOURS["red"]))
    assert prediction.label == "red"
    assert 0.0 <= prediction.prob <= 1.0

  def test_a_confident_logit_is_not_reported_above_one(self, tmp_path):
    """Logits divided by their sum can exceed 1 - softmax cannot."""
    path = _build_onnx(tmp_path / "logits.onnx", FALLBACK_LABELS, with_softmax=False)
    classifier = TrafficLightStateClassifier(path)
    for colour in CROP_COLOURS.values():
      prediction = classifier.classify(_crop(colour))
      assert prediction is not None
      assert 0.0 <= prediction.prob <= 1.0, f"{colour} gave prob {prediction.prob}"

  def test_a_model_that_already_emits_probabilities_is_left_alone(self, model_file):
    classifier = TrafficLightStateClassifier(model_file())
    prediction = classifier.classify(_crop(CROP_COLOURS["green"]))
    assert prediction.label == "green"
    assert 0.0 <= prediction.prob <= 1.0


class TestWhichStateShouldStopTheCar:
  def test_red_wins_over_green_whatever_the_order(self):
    predictions = [StatePrediction("green", 0.9), StatePrediction("red", 0.6)]
    assert most_dangerous(predictions).label == "red"
    assert most_dangerous(list(reversed(predictions))).label == "red"

  def test_yellow_outranks_green(self):
    assert most_dangerous([StatePrediction("green", 0.95),
                           StatePrediction("yellow", 0.55)]).label == "yellow"

  def test_weak_guesses_are_dropped(self):
    assert most_dangerous([StatePrediction("red", 0.2)], min_prob=0.5) is None

  def test_nothing_to_report(self):
    assert most_dangerous([]) is None
    assert most_dangerous([None, None]) is None

  def test_green_and_off_are_not_treated_as_a_reason_to_stop(self):
    best = most_dangerous([StatePrediction("green", 0.8), StatePrediction("off", 0.7)])
    assert best.label == "green"
