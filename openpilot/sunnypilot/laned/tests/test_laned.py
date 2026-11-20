import pytest

from openpilot.cereal import messaging
from openpilot.cereal.visionipc import VisionStreamType
from openpilot.sunnypilot.laned.laned import LaneConfig, build_lane_message, select_stream


def test_select_stream_prefers_the_configured_stream():
  available = {VisionStreamType.VISION_STREAM_NARROW_ROAD, VisionStreamType.VISION_STREAM_WIDE_ROAD}
  assert select_stream(available, "narrow") == VisionStreamType.VISION_STREAM_NARROW_ROAD
  assert select_stream(available, "wide") == VisionStreamType.VISION_STREAM_WIDE_ROAD


def test_select_stream_falls_back_to_the_other_road_stream():
  assert select_stream({VisionStreamType.VISION_STREAM_WIDE_ROAD}, "narrow") == VisionStreamType.VISION_STREAM_WIDE_ROAD
  assert select_stream({VisionStreamType.VISION_STREAM_NARROW_ROAD}, "wide") == VisionStreamType.VISION_STREAM_NARROW_ROAD


def test_select_stream_without_a_road_stream_returns_none():
  assert select_stream(set(), "narrow") is None
  assert select_stream({VisionStreamType.VISION_STREAM_CABIN}, "narrow") is None


def test_lane_config_defaults_are_safe():
  config = LaneConfig.from_env({})
  assert config.conf == 0.25
  assert config.rate == 2.5
  assert config.stream == "narrow"
  assert config.model_path.endswith("lane.onnx")


def test_lane_config_clamps_bad_environment_values():
  config = LaneConfig.from_env({"LANE_CONF": "9", "LANE_RATE": "0.01", "LANE_STREAM": "sideways"})
  assert config.conf == 0.99
  assert config.rate == 0.2
  assert config.stream == "narrow"


def test_lane_config_accepts_the_wide_stream():
  assert LaneConfig.from_env({"LANE_STREAM": "wide"}).stream == "wide"


def test_lane_message_round_trips():
  result = {
    "leftLine": 1,
    "rightLine": 0,
    "leftConf": 0.875,
    "rightConf": 0.5,
    "valid": True,
    "error": "",
    "candidatesCount": 3,
  }
  message = build_lane_message(result, frame_id=42, timestamp_eof=123456789,
                               inference_ms=12.5, model_name="lane.onnx")
  assert message.valid is True
  reader = messaging.log_from_bytes(message.to_bytes())
  payload = reader.laneLineTypes
  assert payload.frameId == 42
  assert payload.timestampEof == 123456789
  assert payload.leftType == 1
  assert payload.rightType == 0
  assert payload.leftConfidence == pytest.approx(0.875)
  assert payload.rightConfidence == pytest.approx(0.5)
  assert payload.inferenceMs == pytest.approx(12.5)
  assert payload.candidatesCount == 3
  assert payload.model == "lane.onnx"


def test_lane_message_marks_invalid_results():
  result = {"leftLine": -1, "rightLine": -1, "leftConf": 0.0, "rightConf": 0.0,
            "valid": False, "error": "boom", "candidatesCount": 0}
  message = build_lane_message(result, 0, 0, 0.0, "lane.onnx")
  assert message.valid is False
  reader = messaging.log_from_bytes(message.to_bytes())
  assert reader.laneLineTypes.leftType == -1
  assert reader.laneLineTypes.rightType == -1
