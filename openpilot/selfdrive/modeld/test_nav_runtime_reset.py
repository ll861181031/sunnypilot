from types import SimpleNamespace

import numpy as np
import pytest

from openpilot.selfdrive.modeld.nav_inputs import NavRuntime
from openpilot.selfdrive.modeld.nav_reset import NavigationResetMixin


MAP_SHAPE = (1, 1, 256, 256)
VEC_SHAPE = (1, 47)


class Model(NavigationResetMixin):
  input_shapes = {"nav_map": MAP_SHAPE, "navigation": VEC_SHAPE}

  def __init__(self, supported=True):
    self.nav_reset_supported = supported


@pytest.fixture(autouse=True)
def isolated_navigation_environment(monkeypatch):
  monkeypatch.delenv("SUNNYPILOT_AMAP_ROOT_NAV", raising=False)
  monkeypatch.delenv("SUNNYPILOT_NAV_SOURCE", raising=False)
  yield


def runtime(monkeypatch, *, phone=True):
  subject = NavRuntime()
  subject._cplink_active = phone
  subject._phone_route_context = SimpleNamespace(binding=SimpleNamespace(route_key=lambda: ("route-1",)))
  subject._evidence = {"mapReason": "accepted", "instructionReason": "accepted"}
  values = [np.ones(MAP_SHAPE, np.uint8), np.ones(VEC_SHAPE, np.float32)]

  def update(*args):
    subject.map_valid, subject.instruction_valid = (value is not None for value in values)
    return tuple(values)

  monkeypatch.setattr(subject, "update", update)
  return subject, values


def test_rejected_runtime_channels_are_explicit_zeros_even_with_file_source(monkeypatch):
  monkeypatch.setenv("SUNNYPILOT_NAV_SOURCE", "files:/deliberately-not-a-live-source")
  subject, values = runtime(monkeypatch)
  values[:] = [None, None]
  inputs = subject.inputs_for_model({}, 0.0, Model())
  assert set(inputs) == {"nav_map", "navigation"}
  assert inputs["nav_map"].shape == MAP_SHAPE and inputs["nav_map"].dtype == np.uint8
  assert inputs["navigation"].shape == VEC_SHAPE and inputs["navigation"].dtype == np.float32
  assert not inputs["nav_map"].any() and not inputs["navigation"].any()


def test_phone_map_requires_history_reset_capability_without_losing_vector(monkeypatch):
  subject, values = runtime(monkeypatch)
  model = Model(supported=False)
  inputs = subject.inputs_for_model({}, 0.0, model)
  assert not inputs["nav_map"].any()
  assert inputs["navigation"] is values[1]
  assert not subject.map_valid and subject.instruction_valid
  assert subject.evidence()["mapReason"] == "navigationResetUnavailable"
  assert model.pending_nav_resets() == {}


def test_first_verified_phone_map_resets_only_map_and_repeat_is_idempotent(monkeypatch):
  subject, values = runtime(monkeypatch)
  values[1] = None
  model = Model()
  assert subject.inputs_for_model({}, 0.0, model)["nav_map"] is values[0]
  first = model.pending_nav_resets()
  assert first == {"map": 1}
  model._commit_nav_reset(first)
  subject.inputs_for_model({}, 0.0, model)
  assert model.pending_nav_resets() == {}


def test_map_loss_clears_map_history_and_preserves_instruction_history(monkeypatch):
  subject, values = runtime(monkeypatch)
  model = Model()
  subject.inputs_for_model({}, 0.0, model)
  model._commit_nav_reset(model.pending_nav_resets())
  values[0] = None
  inputs = subject.inputs_for_model({}, 0.0, model)
  assert not inputs["nav_map"].any() and inputs["navigation"] is values[1]
  assert model.pending_nav_resets() == {"map": 2}
  model._commit_nav_reset(model.pending_nav_resets())
  subject.inputs_for_model({}, 0.0, model)
  assert model.pending_nav_resets() == {}


def test_instruction_loss_resets_only_navigation_history(monkeypatch):
  subject, values = runtime(monkeypatch)
  model = Model()
  subject.inputs_for_model({}, 0.0, model)
  model._commit_nav_reset(model.pending_nav_resets())
  values[1] = None
  inputs = subject.inputs_for_model({}, 0.0, model)
  assert inputs["nav_map"] is values[0] and not inputs["navigation"].any()
  assert model.pending_nav_resets() == {"navigation": 2}


def test_route_change_resets_map_even_without_a_black_frame(monkeypatch):
  subject, _ = runtime(monkeypatch)
  model = Model()
  subject.inputs_for_model({}, 0.0, model)
  model._commit_nav_reset(model.pending_nav_resets())
  subject._phone_route_context.binding.route_key = lambda: ("route-2",)
  subject.inputs_for_model({}, 0.0, model)
  assert model.pending_nav_resets() == {"map": 2}


def test_source_switch_resets_both_histories_when_both_were_used(monkeypatch):
  subject, _ = runtime(monkeypatch)
  model = Model()
  subject.inputs_for_model({}, 0.0, model)
  model._commit_nav_reset(model.pending_nav_resets())
  subject._cplink_active = False
  subject._source_changed_at = 10.0
  subject.inputs_for_model({}, 0.0, model)
  assert model.pending_nav_resets() == {"map": 2, "navigation": 2}


def test_old_worker_still_accepts_independent_instruction_and_native_map(monkeypatch):
  subject, values = runtime(monkeypatch, phone=False)
  model = Model(supported=False)
  inputs = subject.inputs_for_model({}, 0.0, model)
  assert inputs["nav_map"] is values[0] and inputs["navigation"] is values[1]
  assert model.pending_nav_resets() == {}


def test_actual_remote_payload_is_zero_despite_nonzero_backend_fallback(monkeypatch):
  from openpilot.selfdrive.modeld.test_migraphx_recovery import ProtocolPeer, remote_state
  subject, values = runtime(monkeypatch)
  values[:] = [None, None]
  peer = ProtocolPeer()
  state = remote_state(peer, [])
  state.input_shapes = Model.input_shapes
  inputs = subject.inputs_for_model({}, 0.0, state)
  inputs.update(desire_pulse=np.zeros(8, np.float32), traffic_convention=np.zeros((1, 2), np.float32),
                action_t=np.zeros((1, 2), np.float32))
  bufs = {name: SimpleNamespace(fd=fd, width=2, height=2, stride=2, uv_offset=4, data=b"000000")
          for name, fd in (("img", 101), ("big_img", 102))}
  state.run(bufs, {name: np.eye(3, dtype=np.float32) for name in bufs}, inputs)
  np.testing.assert_array_equal(np.frombuffer(peer.sent[2], np.float32), 0)
  np.testing.assert_array_equal(np.frombuffer(peer.sent[3], np.float32), 0)
  assert state.last_nav_evidence["navigation"] == [0.0] * 47


def test_history_reset_evidence_records_requested_and_confirmed_separately():
  from openpilot.cereal import log, messaging
  from openpilot.selfdrive.modeld.modeld import fill_nav_evidence
  model = Model()
  model.nav_session_reset_confirmed = True
  model.request_nav_reset(map_epoch=3, navigation_epoch=2)
  model._commit_nav_reset({"map": 3})
  msg = messaging.new_message("modelDataV2SP")
  fill_nav_evidence(msg.modelDataV2SP, None, model, 10, 11, 1_000)
  with log.Event.from_bytes(msg.to_bytes()) as decoded:
    evidence = decoded.modelDataV2SP.navEvidence
    assert evidence.navigationResetSupported
    assert evidence.navigationSessionResetConfirmed
    assert (evidence.mapResetRequestedEpoch, evidence.mapResetAppliedEpoch) == (3, 3)
    assert (evidence.instructionResetRequestedEpoch, evidence.instructionResetAppliedEpoch) == (2, 0)


def test_direct_migraphx_warmup_does_not_keep_file_navigation_history():
  from openpilot.selfdrive.modeld.modeld import MigraphxModelState
  from openpilot.selfdrive.modeld.nav_reset import NAV_STATE_KEYS
  state = object.__new__(MigraphxModelState)
  state.vision_input_names = ("img", "big_img")
  state._state = {name: np.full(3, 7, np.float32) for name in
                  ("state_img_q", "state_feat_q", "state_desire_q", *NAV_STATE_KEYS.values())}

  def warmup_run(*args):
    for value in state._state.values():
      value[:] = 11

  state.run = warmup_run
  state.warmup()
  for name, value in state._state.items():
    np.testing.assert_array_equal(value, 0 if name in NAV_STATE_KEYS.values() else 11)
