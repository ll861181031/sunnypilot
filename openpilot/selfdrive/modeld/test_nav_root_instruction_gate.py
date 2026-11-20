"""Root mode accepts only actions authenticated against the current route."""
from types import SimpleNamespace

import numpy as np
import pytest

from openpilot.selfdrive.modeld import nav_inputs
from openpilot.selfdrive.modeld.nav_reset import NavigationResetMixin
from openpilot.selfdrive.navigation.helpers import navigation_input
from openpilot.selfdrive.navigation.tests.test_amap_phone_pipeline import (
  MAP_SHAPE, VEC_SHAPE, make_sample, model_runtime, pipeline as pipeline, queue_frame,
)


class Model(NavigationResetMixin):
  input_shapes = {"nav_map": MAP_SHAPE, "navigation": VEC_SHAPE}

  def __init__(self, *, supported=True):
    self.nav_reset_supported = supported
    self.reset_requests = []

  def request_nav_reset(self, **epochs):
    self.reset_requests.append(epochs)
    super().request_nav_reset(**epochs)


class SM:
  def __init__(self, *, now=100.0, valid=True):
    self.valid = {"navInstruction": valid}
    self.updated = {}
    self.recv_time = {"navInstruction": now - 0.01}
    self.logMonoTime = {"navInstruction": round((now - 0.02) * 1e9)}
    self.instruction = SimpleNamespace(allManeuvers=[{"type": "turn", "modifier": "right", "distance": 50.0}])

  def __getitem__(self, name):
    assert name == "navInstruction"
    return self.instruction


@pytest.fixture(autouse=True)
def root_mode(monkeypatch):
  monkeypatch.setenv("SUNNYPILOT_AMAP_ROOT_NAV", "1")


@pytest.fixture
def subject(monkeypatch):
  monkeypatch.setattr(nav_inputs.time, "monotonic", lambda: 100.0)
  monkeypatch.setattr(nav_inputs, "cplink_navi_active", lambda: True)
  runtime = nav_inputs.NavRuntime()
  runtime._cplink_active = True
  runtime._client_failed = True
  return runtime


@pytest.mark.parametrize("valid", [False, True])
def test_consumer_independently_rejects_root_legacy_instruction(subject, valid):
  inputs = subject.inputs_for_model(SM(valid=valid), 0.0, Model())
  assert not inputs["navigation"].any()
  assert not subject.instruction_valid
  assert subject.evidence()["instructionReason"] == ("invalidMessage" if not valid else "legacyBinding")
  assert subject.evidence()["selectedCommand"] == -1
  assert subject.evidence()["selectedManeuverIndex"] == -1


def test_root_gate_does_not_decode_untrusted_maneuver_fields(subject):
  class InvalidInstruction:
    @property
    def allManeuvers(self):
      pytest.fail("Root gate must reject before decoding legacy maneuvers")

  sm = SM()
  sm.instruction = InvalidInstruction()
  assert not subject.inputs_for_model(sm, 0.0, Model())["navigation"].any()
  assert subject.evidence()["instructionReason"] == "legacyBinding"


def test_root_mode_is_sampled_once_per_update(subject, monkeypatch):
  calls = []
  monkeypatch.setattr(nav_inputs, "amap_root_nav_enabled", lambda: calls.append(1) or len(calls) == 1, raising=False)
  assert not subject.inputs_for_model(SM(), 0.0, Model())["navigation"].any()
  assert calls == [1]
  assert subject.evidence()["instructionReason"] == "legacyBinding"


def test_first_root_model_requests_navigation_reset_once_even_before_ack(subject):
  model = Model()
  for _ in range(3):
    assert not subject.inputs_for_model(SM(), 0.0, model)["navigation"].any()
  assert model.pending_nav_resets() == {"navigation": 1}
  assert model.reset_requests == [{"navigation_epoch": 1}]
  model._commit_nav_reset(model.pending_nav_resets())
  subject.inputs_for_model(SM(), 0.0, model)
  assert model.pending_nav_resets() == {}
  assert len(model.reset_requests) == 1


def test_each_root_model_gets_one_initial_navigation_reset(subject):
  first, second = Model(), Model()
  subject.inputs_for_model(SM(), 0.0, first)
  first._commit_nav_reset(first.pending_nav_resets())
  subject.inputs_for_model(SM(), 0.0, second)
  assert first.reset_requests == [{"navigation_epoch": 1}]
  assert second.reset_requests == [{"navigation_epoch": 2}]
  subject.inputs_for_model(SM(), 0.0, second)
  assert len(second.reset_requests) == 1


def test_root_mode_transitions_clear_only_navigation_once_and_restore_legacy(subject, monkeypatch):
  model, sm = Model(), SM()
  monkeypatch.setenv("SUNNYPILOT_AMAP_ROOT_NAV", "0")
  first = subject.inputs_for_model(sm, 5.0, model)
  expected = navigation_input(sm.instruction.allManeuvers, 0.1).reshape(VEC_SHAPE)
  np.testing.assert_allclose(first["navigation"], expected)
  assert model.pending_nav_resets() == {"navigation": 1}
  model._commit_nav_reset(model.pending_nav_resets())
  monkeypatch.setenv("SUNNYPILOT_AMAP_ROOT_NAV", "1")
  assert not subject.inputs_for_model(sm, 5.0, model)["navigation"].any()
  assert model.pending_nav_resets() == {"navigation": 2}
  model._commit_nav_reset(model.pending_nav_resets())
  subject.inputs_for_model(sm, 5.0, model)
  assert model.pending_nav_resets() == {}
  monkeypatch.setenv("SUNNYPILOT_AMAP_ROOT_NAV", "0")
  np.testing.assert_allclose(subject.inputs_for_model(sm, 5.0, model)["navigation"], expected)
  assert subject.evidence()["instructionReason"] == "accepted"
  assert model.pending_nav_resets() == {"navigation": 3}
  model._commit_nav_reset(model.pending_nav_resets())
  subject.inputs_for_model(sm, 5.0, model)
  assert model.pending_nav_resets() == {}
  assert model.reset_requests == [
    {"navigation_epoch": 1}, {"navigation_epoch": 2}, {"navigation_epoch": 3},
  ]


def test_unsupported_worker_gets_zero_input_and_reset_waits_for_capability(subject):
  model = Model(supported=False)
  assert not subject.inputs_for_model(SM(), 0.0, model)["navigation"].any()
  assert model.pending_nav_resets() == model.nav_reset_applied == {}
  assert not model.reset_requests
  model.nav_reset_supported = True
  subject.inputs_for_model(SM(), 0.0, model)
  assert model.pending_nav_resets() == {"navigation": 1}
  assert model.nav_reset_applied == {}
  model._commit_nav_reset(model.pending_nav_resets())
  subject.inputs_for_model(SM(), 0.0, model)
  assert model.pending_nav_resets() == {}
  assert len(model.reset_requests) == 1


def test_verified_root_right_turn_reaches_both_model_inputs(pipeline, monkeypatch):
  subject = model_runtime(pipeline, monkeypatch)
  model = Model()
  inputs = subject.inputs_for_model(pipeline.sm, 0.0, model)
  assert inputs["nav_map"].any() and subject.map_valid
  assert subject.instruction_valid
  assert inputs["navigation"][0, 10] == pytest.approx(1.0)
  assert inputs["navigation"][0, -1] == pytest.approx(0.79)
  assert subject.evidence()["mapReason"] == "accepted"
  assert subject.evidence()["instructionReason"] == "accepted"
  assert subject.evidence()["selectedCommand"] == 10
  assert model.pending_nav_resets() == {"map": 1, "navigation": 1}


def test_verified_root_left_turn_reaches_both_model_inputs(pipeline, monkeypatch):
  sample = make_sample()
  for branch in sample.native_after["_navi_info"]["branches"]:
    branch["maneuver_id_raw"] = 2
  fields = sample.broadcast["records"]["10001"]["fields"]
  fields["ICON"] = 2
  fields["NEW_ICON"] = 2
  publication = pipeline.adapter.accept(sample, now_ns=pipeline.clock[0])
  assert publication is not None, pipeline.adapter.reason
  pipeline.sm.publish(publication, pipeline.clock[0])

  subject = model_runtime(pipeline, monkeypatch)
  model = Model()
  inputs = subject.inputs_for_model(pipeline.sm, 0.0, model)

  assert inputs["nav_map"].any() and subject.map_valid
  assert subject.instruction_valid
  assert inputs["navigation"][0, 15] == pytest.approx(1.0)
  assert inputs["navigation"][0, -1] == pytest.approx(0.79)
  assert subject.evidence()["instructionReason"] == "accepted"
  assert subject.evidence()["selectedCommand"] == 15
  assert model.pending_nav_resets() == {"map": 1, "navigation": 1}


def test_verified_root_sharp_right_turn_reaches_both_model_inputs(pipeline, monkeypatch):
  sample = make_sample()
  for branch in sample.native_after["_navi_info"]["branches"]:
    branch["maneuver_id_raw"] = 7
  fields = sample.broadcast["records"]["10001"]["fields"]
  fields["ICON"] = 7
  fields["NEW_ICON"] = 7
  publication = pipeline.adapter.accept(sample, now_ns=pipeline.clock[0])
  assert publication is not None, pipeline.adapter.reason
  pipeline.sm.publish(publication, pipeline.clock[0])

  subject = model_runtime(pipeline, monkeypatch)
  model = Model()
  inputs = subject.inputs_for_model(pipeline.sm, 0.0, model)

  assert inputs["nav_map"].any() and subject.map_valid
  assert subject.instruction_valid
  assert inputs["navigation"][0, 11] == pytest.approx(1.0)
  assert inputs["navigation"][0, -1] == pytest.approx(0.79)
  assert subject.evidence()["instructionReason"] == "accepted"
  assert subject.evidence()["selectedCommand"] == 11
  assert model.pending_nav_resets() == {"map": 1, "navigation": 1}


@pytest.mark.parametrize("binding", ["legacy", "wrong_route"])
def test_untrusted_instruction_binding_cannot_unlock_root_action(pipeline, monkeypatch, binding):
  subject = model_runtime(pipeline, monkeypatch)
  if binding == "legacy":
    pipeline.sm["navInstruction"].routeBinding.version = 0
  else:
    pipeline.sm["navInstruction"].routeBinding.routeId = "not-the-current-route"
  model = Model()
  inputs = subject.inputs_for_model(pipeline.sm, 0.0, model)
  assert not inputs["navigation"].any() and not subject.instruction_valid
  assert subject.evidence()["instructionReason"] == (
    "legacyBinding" if binding == "legacy" else "instructionRouteMismatch")


@pytest.mark.parametrize("transition", ["cancel", "change_route", "expire"])
def test_root_revoke_and_recovery_reset_both_histories_once(pipeline, monkeypatch, transition):
  subject = model_runtime(pipeline, monkeypatch)
  model = Model()
  first = subject.inputs_for_model(pipeline.sm, 0.0, model)
  assert first["nav_map"].any() and first["navigation"].any()
  model._commit_nav_reset(model.pending_nav_resets())
  pipeline.clock[0] += 2_100_000_000 if transition == "expire" else 100_000_000
  if transition == "cancel":
    pipeline.adapter.withdraw(pipeline.clock[0], "phone_navigation_stopped")
    pipeline.sm.publish(None, pipeline.clock[0])
  elif transition == "change_route":
    sample = make_sample(now_ns=pipeline.clock[0], sequence=2, path_id=456, elapsed_ns=10_100_000_000)
    publication = pipeline.adapter.accept(sample, now_ns=pipeline.clock[0])
    assert publication is not None, pipeline.adapter.reason
    pipeline.sm.publish(publication, pipeline.clock[0])
    subject._update_phone_route(pipeline.sm, pipeline.clock[0] / 1e9)
    queue_frame(subject, pipeline, frame_id=43)
  second = subject.inputs_for_model(pipeline.sm, 0.0, model)
  assert bool(second["nav_map"].any()) is (transition == "change_route")
  if transition == "cancel":
    # navd publishes an invalid instruction with the withdrawal, so only the map
    # history changes; stale navigation still expires independently on modeld.
    assert not second["navigation"].any()
    assert model.pending_nav_resets() == {"map": 2, "navigation": 2}
  elif transition == "change_route":
    # A new route is a new navigation history even when it has the same action.
    assert second["navigation"].any()
    assert model.pending_nav_resets() == {"map": 2, "navigation": 2}
  else:
    # No new route message arrived; the old instruction expires below.
    assert not second["navigation"].any()
    assert model.pending_nav_resets() == {"map": 2, "navigation": 2}
  model._commit_nav_reset(model.pending_nav_resets())
  pipeline.clock[0] += 200_000_000
  sample = make_sample(now_ns=pipeline.clock[0], sequence=3, path_id=456 if transition == "change_route" else 123,
                       elapsed_ns=12_300_000_000)
  publication = pipeline.adapter.accept(sample, now_ns=pipeline.clock[0])
  assert publication is not None, pipeline.adapter.reason
  pipeline.sm.publish(publication, pipeline.clock[0])
  subject._update_phone_route(pipeline.sm, pipeline.clock[0] / 1e9)
  queue_frame(subject, pipeline, frame_id=44)
  recovered = subject.inputs_for_model(pipeline.sm, 0.0, model)
  assert recovered["nav_map"].any()
  assert recovered["navigation"].any()
  if transition in ("cancel", "expire"):
    assert model.pending_nav_resets() == {"map": 3, "navigation": 3}
  else:
    assert recovered["navigation"].any()
    assert model.pending_nav_resets() == {}
  expected_requests = [{"navigation_epoch": 1}, {"navigation_epoch": 2}]
  if transition in ("cancel", "expire"):
    expected_requests.append({"navigation_epoch": 3})
  assert [request for request in model.reset_requests
          if "navigation_epoch" in request] == expected_requests
