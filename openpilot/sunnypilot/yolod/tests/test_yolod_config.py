"""yolod configuration parsing and stream selection tests."""

import pytest

from openpilot.cereal.visionipc import VisionStreamType
from openpilot.sunnypilot.yolod.paths import DEFAULT_NCNN_BIN, DEFAULT_NCNN_NAMES, DEFAULT_NCNN_PARAM
from openpilot.sunnypilot.yolod.yolod import DEFAULT_CLASSES, DEFAULT_MODEL_PATH, YoloConfig, create_detector, select_stream


def test_defaults():
  config = YoloConfig.from_env({})
  assert config.model_path == DEFAULT_MODEL_PATH
  assert config.conf == pytest.approx(0.25)
  assert config.iou == pytest.approx(0.45)
  assert config.rate == pytest.approx(10.0)
  assert config.threads == 2
  assert config.classes == DEFAULT_CLASSES
  assert config.stream == "narrow"
  assert config.backend == "onnxruntime"
  assert config.ncnn_param == DEFAULT_NCNN_PARAM
  assert config.ncnn_bin == DEFAULT_NCNN_BIN
  assert config.ncnn_names == DEFAULT_NCNN_NAMES
  assert config.warnings == ()


def test_environment_overrides():
  config = YoloConfig.from_env({
    "YOLO_MODEL_PATH": "/tmp/custom.onnx",
    "YOLO_CONF": "0.4",
    "YOLO_IOU": "0.6",
    "YOLO_RATE": "5",
    "YOLO_THREADS": "4",
    "YOLO_CLASSES": " person , car , motorcycle ",
    "YOLO_STREAM": "wide",
    "YOLO_BACKEND": "ncnn",
    "YOLO_NCNN_PARAM": "/tmp/custom.ncnn.param",
    "YOLO_NCNN_BIN": "/tmp/custom.ncnn.bin",
    "YOLO_NCNN_NAMES": "/tmp/custom.ncnn.names.json",
  })
  assert config.model_path == "/tmp/custom.onnx"
  assert config.conf == pytest.approx(0.4)
  assert config.iou == pytest.approx(0.6)
  assert config.rate == pytest.approx(5.0)
  assert config.threads == 4
  assert config.classes == ("person", "car", "motorcycle")
  assert config.stream == "wide"
  assert config.backend == "ncnn"
  assert config.ncnn_param == "/tmp/custom.ncnn.param"
  assert config.ncnn_bin == "/tmp/custom.ncnn.bin"
  assert config.ncnn_names == "/tmp/custom.ncnn.names.json"
  assert config.warnings == ()


def test_explicit_model_path_argument_wins_over_environment():
  config = YoloConfig.from_env({"YOLO_MODEL_PATH": "/tmp/env.onnx"}, model_path="/tmp/arg.onnx")
  assert config.model_path == "/tmp/arg.onnx"


def test_classes_all_means_no_filter():
  config = YoloConfig.from_env({"YOLO_CLASSES": "ALL"})
  assert config.classes is None


def test_classes_empty_uses_road_default():
  assert YoloConfig.from_env({"YOLO_CLASSES": "  "}).classes == DEFAULT_CLASSES
  config = YoloConfig.from_env({"YOLO_CLASSES": " , "})
  assert config.classes == DEFAULT_CLASSES
  assert config.warnings  # the empty explicit list is reported, not silently ignored


def test_invalid_numbers_fall_back_with_warnings():
  config = YoloConfig.from_env({"YOLO_CONF": "high", "YOLO_RATE": "", "YOLO_THREADS": "many"})
  assert config.conf == pytest.approx(0.25)
  assert config.rate == pytest.approx(10.0)
  assert config.threads == 2
  assert len(config.warnings) == 2  # empty strings are treated as unset, only two bad values


def test_out_of_range_numbers_are_clamped_with_warnings():
  config = YoloConfig.from_env({"YOLO_CONF": "5", "YOLO_IOU": "-1", "YOLO_RATE": "0", "YOLO_THREADS": "99"})
  assert config.conf == pytest.approx(0.99)
  assert config.iou == pytest.approx(0.01)
  assert config.rate == pytest.approx(0.5)
  assert config.threads == 32
  assert len(config.warnings) == 4


def test_invalid_backend_falls_back_to_onnxruntime():
  config = YoloConfig.from_env({"YOLO_BACKEND": "tensorrt"})
  assert config.backend == "onnxruntime"
  assert any("YOLO_BACKEND" in warning for warning in config.warnings)


def test_cv2dnn_backend_is_accepted():
  assert YoloConfig.from_env({"YOLO_BACKEND": "cv2dnn"}).backend == "cv2dnn"


def test_factory_passes_backend_and_filter_to_detector(monkeypatch):
  import openpilot.sunnypilot.yolod.yolod as yolod

  calls = {}

  class _FakeDetector:
    def __init__(self, path, **kwargs):
      calls["path"] = path
      calls.update(kwargs)

  monkeypatch.setattr(yolod, "Detector", _FakeDetector)
  config = YoloConfig.from_env({"YOLO_BACKEND": "onnxruntime"})
  detector = create_detector(config, config.classes)
  assert detector is not None
  assert calls["backend"] == "onnxruntime"
  assert calls["class_filter"] == config.classes

  calls.clear()
  config = YoloConfig.from_env({"YOLO_BACKEND": "cv2dnn"})
  create_detector(config, ("person",))
  assert calls["backend"] == "cv2dnn"
  assert calls["class_filter"] == ("person",)


def test_factory_passes_ncnn_paths(monkeypatch):
  pytest.importorskip("ncnn")
  from openpilot.sunnypilot.yolod import detector_ncnn

  calls = {}

  class _FakeNcnnDetector:
    def __init__(self, param, binary, **kwargs):
      calls["param"] = param
      calls["bin"] = binary
      calls.update(kwargs)

  monkeypatch.setattr(detector_ncnn, "NcnnDetector", _FakeNcnnDetector)
  config = YoloConfig.from_env({"YOLO_BACKEND": "ncnn"})
  create_detector(config, ("person", "car"))
  assert calls["param"] == config.ncnn_param
  assert calls["bin"] == config.ncnn_bin
  assert calls["names_path"] == config.ncnn_names
  assert calls["class_filter"] == ("person", "car")


def test_invalid_stream_falls_back_to_narrow():
  config = YoloConfig.from_env({"YOLO_STREAM": "ultra-wide"})
  assert config.stream == "narrow"
  assert any("YOLO_STREAM" in warning for warning in config.warnings)


def test_threads_are_rounded_to_integers():
  assert YoloConfig.from_env({"YOLO_THREADS": "3.6"}).threads == 4


def test_select_stream_prefers_requested_and_falls_back():
  streams = {VisionStreamType.VISION_STREAM_NARROW_ROAD, VisionStreamType.VISION_STREAM_WIDE_ROAD}
  assert select_stream(streams, "narrow") == VisionStreamType.VISION_STREAM_NARROW_ROAD
  assert select_stream(streams, "wide") == VisionStreamType.VISION_STREAM_WIDE_ROAD
  assert select_stream({VisionStreamType.VISION_STREAM_WIDE_ROAD}, "narrow") == VisionStreamType.VISION_STREAM_WIDE_ROAD
  assert select_stream({VisionStreamType.VISION_STREAM_NARROW_ROAD}, "wide") == VisionStreamType.VISION_STREAM_NARROW_ROAD
  assert select_stream({VisionStreamType.VISION_STREAM_CABIN}, "narrow") is None
  assert select_stream(set(), "narrow") is None


def test_imgsz_defaults_to_the_backend_size():
  """Unset means "let the backend decide" - the ONNX path reads the graph itself."""
  assert YoloConfig.from_env({}).imgsz is None


def test_imgsz_is_parsed_when_given():
  assert YoloConfig.from_env({"YOLO_IMGSZ": "960"}).imgsz == 960


def test_a_bad_imgsz_falls_back_instead_of_crashing():
  """A typo must not take the daemon down; it warns and keeps the default.

  This matters because feeding a 640 tensor to a 960 ncnn graph does not raise -
  it returns confident nonsense (one 960 export reported 1255 detections on a
  single frame), so silently accepting a broken value would be worse than a crash.
  """
  for raw in ("abc", "0", "-640"):
    config = YoloConfig.from_env({"YOLO_IMGSZ": raw})
    assert config.imgsz is None, raw
    assert any("YOLO_IMGSZ" in warning for warning in config.warnings), raw


def test_centre_gate_is_off_by_default():
  """Publishing every light is the safe default: side lights are context."""
  assert YoloConfig.from_env({}).centre_gate is None


def test_centre_gate_is_parsed():
  assert YoloConfig.from_env({"YOLO_CENTRE_GATE": "0.12"}).centre_gate == pytest.approx(0.12)


def test_a_bad_centre_gate_is_ignored_with_a_warning():
  for raw in ("abc", "0", "-0.1", "0.7"):
    config = YoloConfig.from_env({"YOLO_CENTRE_GATE": raw})
    assert config.centre_gate is None, raw
    assert any("YOLO_CENTRE_GATE" in w for w in config.warnings), raw
