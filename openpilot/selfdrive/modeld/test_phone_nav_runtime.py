from dataclasses import replace

import numpy as np
import pytest

from openpilot.selfdrive.modeld import nav_inputs
from openpilot.selfdrive.navigation.nav_map_contract import MapFrameMatcher
from openpilot.cereal import custom
from openpilot.selfdrive.navigation.tests.test_nav_map_contract import frame_data, local_sm


MAP_SHAPE = (1, 1, 256, 256)
VEC_SHAPE = (1, 47)


@pytest.fixture
def phone(monkeypatch):
  monkeypatch.setattr(nav_inputs.time, "monotonic", lambda: 10.2)
  monkeypatch.setattr(nav_inputs, "cplink_navi_active", lambda: True)
  runtime = nav_inputs.NavRuntime()
  runtime._cplink_active = True
  runtime._client_failed = True
  _, _, frame, metadata = frame_data()
  runtime._phone_matcher = MapFrameMatcher(metadata.renderer_instance_id)
  runtime._phone_matcher.add_metadata(metadata, received_mono_time_ns=10_110_000_000)
  runtime._phone_frames = {(frame.frame_id, frame.timestamp_eof_ns): frame}
  return runtime, local_sm(), frame


def test_verified_phone_pixels_and_independent_vector_are_both_accepted(phone):
  runtime, sm, _ = phone
  image, vector = runtime.update(sm, 0.0, MAP_SHAPE, VEC_SHAPE)
  assert image is not None and image.shape == MAP_SHAPE
  assert vector is not None and vector.shape == VEC_SHAPE
  assert runtime.evidence()["mapReason"] == "accepted"


def test_phone_map_rejected_on_corrupt_geometry_without_losing_vector(phone):
  runtime, sm, _ = phone
  sm["navRoute"].coordinates64[1].longitude += 0.00001
  image, vector = runtime.update(sm, 0.0, MAP_SHAPE, VEC_SHAPE)
  assert image is None and vector is not None
  assert runtime._phone_route_context is None


def test_phone_map_crc_mismatch_does_not_clear_instruction(phone):
  runtime, sm, frame = phone
  runtime._phone_frames = {(frame.frame_id, frame.timestamp_eof_ns): replace(frame, pixels=bytes([7]) * 65536)}
  image, vector = runtime.update(sm, 0.0, MAP_SHAPE, VEC_SHAPE)
  assert image is None and vector is not None
  assert runtime.evidence()["mapReason"] == "pixel_crc_mismatch"


def test_phone_map_must_be_revalidated_on_frames_without_new_pixels(phone, monkeypatch):
  runtime, sm, _ = phone
  image, _ = runtime.update(sm, 0.0, MAP_SHAPE, VEC_SHAPE)
  assert image is not None
  sm.updated["navRoute"] = False
  monkeypatch.setattr(nav_inputs.time, "monotonic", lambda: 10.7)
  image, _ = runtime.update(sm, 0.0, MAP_SHAPE, VEC_SHAPE)
  assert image is None


def test_phone_instruction_without_binding_keeps_vector_and_drops_map(phone):
  runtime, sm, _ = phone
  sm["navInstruction"].routeBinding.version = 0
  image, vector = runtime.update(sm, 0.0, MAP_SHAPE, VEC_SHAPE)
  assert image is None and vector is not None


def test_phone_bad_new_route_revokes_previously_accepted_pixels(phone):
  runtime, sm, _ = phone
  assert runtime.update(sm, 0.0, MAP_SHAPE, VEC_SHAPE)[0] is not None
  sm.valid["navRoute"] = False
  image, vector = runtime.update(sm, 0.0, MAP_SHAPE, VEC_SHAPE)
  assert image is None and vector is not None
  assert runtime._phone_route_context is None


def test_phone_source_switch_clears_pending_pixels(phone, monkeypatch):
  runtime, sm, _ = phone
  assert runtime.update(sm, 0.0, MAP_SHAPE, VEC_SHAPE)[0] is not None
  monkeypatch.setattr(nav_inputs, "cplink_navi_active", lambda: False)
  image, _ = runtime.update(sm, 0.0, MAP_SHAPE, VEC_SHAPE)
  assert image is None
  assert not runtime._phone_frames
  assert runtime._phone_route_context is None


def test_black_phone_frame_immediately_revokes_cached_candidates(phone):
  runtime, _, _ = phone

  class Client:
    timestamp_eof = 10_190_000_000
    timestamp_sof = 10_150_000_000
    frame_id = 43

    def is_connected(self):
      return True

    def recv(self, _):
      class Buffer:
        width = height = 256
        data = bytes(65536)
      return Buffer()

  runtime.client = Client()
  runtime.map_image = np.ones(MAP_SHAPE, dtype=np.uint8)
  runtime._poll_map(MAP_SHAPE)
  assert not runtime._phone_frames
  assert runtime.map_image is None


def test_newer_invalid_sidecar_revokes_older_pixels_before_black_frame_arrives(phone):
  runtime, original, _ = phone
  _, _, _, metadata = frame_data()
  invalid = replace(metadata, frame_id=43, timestamp_sof_ns=10_150_000_000,
                    timestamp_eof_ns=10_190_000_000, valid=False, reason="off_route")
  sidecar = custom.NavMapFrameSP.new_message(**invalid.to_dict())

  class SM:
    updated = {**original.updated, "navMapFrameSP": True}
    valid = {**original.valid, "navMapFrameSP": True}
    recv_time = {**original.recv_time, "navMapFrameSP": 10.195}
    logMonoTime = {**original.logMonoTime, "navMapFrameSP": 10_190_000_000}

    def __getitem__(self, name):
      return sidecar if name == "navMapFrameSP" else original[name]

  image, vector = runtime.update(SM(), 0.0, MAP_SHAPE, VEC_SHAPE)
  assert image is None and vector is not None
  assert not runtime._phone_frames


def test_first_phone_poll_accepts_just_received_route_before_poll_timestamp(phone, monkeypatch):
  runtime, sm, _ = phone
  runtime._cplink_active = False
  runtime.update(sm, 0.0, MAP_SHAPE, VEC_SHAPE)
  assert runtime._phone_route_context is not None
  monkeypatch.setattr(nav_inputs.time, "monotonic", lambda: 10.8)
  sm.updated["navRoute"] = False
  sm.recv_time["navInstruction"] = 10.79
  sm.logMonoTime["navInstruction"] = 10_790_000_000
  runtime.update(sm, 0.0, MAP_SHAPE, VEC_SHAPE)
  assert runtime._phone_route_context is not None


def test_accepted_phone_frame_records_exact_sidecar_identity(phone):
  runtime, sm, frame = phone
  assert runtime.update(sm, 0.0, MAP_SHAPE, VEC_SHAPE)[0] is not None
  evidence = runtime.evidence()
  assert evidence["mapBinding"]["bindingId"] == sm["navRoute"].routeBinding.bindingId
  assert evidence["mapPixelCrc32"] == __import__("zlib").crc32(frame.pixels)
  assert evidence["mapRendererInstanceId"] == "mapsd-instance-1"


def test_invalid_sidecar_is_not_lost_when_instruction_is_invalid_on_same_frame(phone):
  runtime, original, _ = phone
  _, _, _, metadata = frame_data()
  invalid = replace(metadata, frame_id=43, timestamp_sof_ns=10_150_000_000,
                    timestamp_eof_ns=10_190_000_000, valid=False, reason="off_route")
  sidecar = custom.NavMapFrameSP.new_message(**invalid.to_dict())

  class SM:
    updated = {**original.updated, "navMapFrameSP": True}
    valid = {**original.valid, "navMapFrameSP": True, "navInstruction": False}
    recv_time = {**original.recv_time, "navMapFrameSP": 10.195}
    logMonoTime = {**original.logMonoTime, "navMapFrameSP": 10_190_000_000}

    def __getitem__(self, name):
      return sidecar if name == "navMapFrameSP" else original[name]

  sm = SM()
  assert runtime.update(sm, 0.0, MAP_SHAPE, VEC_SHAPE)[0] is None
  sm.updated["navMapFrameSP"] = False
  sm.valid["navInstruction"] = True
  assert runtime.update(sm, 0.0, MAP_SHAPE, VEC_SHAPE)[0] is None
  assert not runtime._phone_frames
