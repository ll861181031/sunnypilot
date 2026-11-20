"""The tinygrad modeld path must carry the same features as the stock one.

`modeld_v2` is a second implementation of the model loop, selected when the active
bundle asks for the tinygrad runner. It drifted once: the stock path grew the
latched-blinker gate, lane-marking gate, wide-camera indicator and Blendful
correction, and none of them reached the tinygrad loop. Nothing failed, because the
two loops are separate programs.

These are source-level assertions on purpose. Importing `modeld_v2.modeld` binds the
tinygrad device and loads a model; that is not something a unit test should do. The
strings pinned here are the wiring, and each one has a behavioural test on the stock
path already.
"""
from pathlib import Path

MODELD_V2 = Path(__file__).resolve().parents[2] / "sunnypilot/modeld_v2/modeld.py"


def source() -> str:
  return MODELD_V2.read_text()


def test_the_lane_change_inputs_reach_the_tinygrad_desire_helper():
  src = source()
  call = src[src.index("DH.update("):]
  call = call[:call.index(")")]
  for kwarg in ("blinker_latched", "left_lane_line", "right_lane_line",
                "lane_line_check", "blindspot_unavailable_left", "blindspot_unavailable_right"):
    assert kwarg in call, f"modeld_v2 lost {kwarg}; the stock path has it"


def test_it_subscribes_to_car_state_sp():
  assert '"carStateSP"' in source(), "modeld_v2 cannot read the fields it must forward"


def test_the_wide_indicator_follows_delivered_frames():
  src = source()
  assert "WideCameraActive" in src
  # The param must be driven by whether a wide frame actually reached the model,
  # not by whether the stream was configured.
  assert "extra_is_wide" in src
  assert "wide_on_frames" in src


def test_the_extra_stream_is_drained_without_blocking():
  src = source()
  assert "extra_frame_selector.select(vipc_client_extra" in src, "both model paths must use the arrived-frame selector"
  assert "EXTRA_SYNC_TOLERANCE_NS" in src
  assert "10000000" not in src, "the old 10 ms error threshold is below one frame at 20 fps"


def test_both_model_paths_preserve_arrived_wide_frames():
  # Conflation discards all but the newest frame before the selector can compare
  # their SOFs. Both clients must retain the small arrival queue instead.
  expected = 'VisionIpcClient("camerad", VisionStreamType.VISION_STREAM_WIDE_ROAD, False)'
  stock = Path(__file__).with_name("modeld.py").read_text()
  assert expected in stock
  assert expected in source()


def test_both_model_paths_validate_the_main_frame_too():
  expected = "{'img': meta_main.frame_id, 'big_img': meta_extra.frame_id}"
  stock = Path(__file__).with_name("modeld.py").read_text()
  assert expected in stock
  assert expected in source()


def test_blendful_is_wired_like_the_stock_path():
  src = source()
  assert "blendful=partial(blendful.update" in src
  assert "blendfulCurvature" in src and "blendfulLaneWidth" in src
