"""Bounded independent source/consumer route observations; no phone or GPU."""
from copy import deepcopy
import hashlib
import json
from types import MappingProxyType, SimpleNamespace

import numpy as np
import pytest

from APP.navdiag import live_pipeline_probe as probe
from openpilot.selfdrive.modeld import nav_inputs
from openpilot.selfdrive.navigation import mapsd, navd
from openpilot.selfdrive.navigation.amap_phone_route import AmapPhoneRouteAdapter
from openpilot.selfdrive.navigation.nav_map_contract import binding_from_snapshot
from openpilot.selfdrive.navigation.tests.test_amap_phone_pipeline import (
  MAP_SHAPE, NOW_NS, VEC_SHAPE, PhoneSM, make_sample, model_runtime, queue_frame,
)


def fixture():
  sample = make_sample(sequence=7)
  sample.selected.cache_route_index = 0
  adapter = AmapPhoneRouteAdapter()
  publication = adapter.accept(sample, now_ns=NOW_NS)
  assert publication is not None, adapter.reason
  binding = binding_from_snapshot(publication.snapshot, publication.active, now_ns=NOW_NS).to_dict()
  runtime = {"source": "phone", "mapReason": "accepted", "mapCorrelationReason": "accepted", "mapBinding": binding}
  return sample, runtime


def test_independent_binding_matches_source_without_paths_or_coordinates():
  sample, runtime = fixture()
  original = deepcopy(runtime)
  result = probe.route_identity_diagnostics(sample, runtime, map_valid=True)
  assert result["selected"]["path_identity_hash"] == sample.selected.identity_hash
  assert result["selected"]["cache_route_index"] == 0
  assert result["selected"]["source_session_id"] == sample.selected.source_session_id
  assert result["consumer_binding"]["routeId"] == runtime["mapBinding"]["routeId"]
  assert result["consumer_binding"]["bindingId"] == runtime["mapBinding"]["bindingId"]
  assert result["consumer_binding"]["observedSequence"] == sample.sequence
  assert result["consumer_binding_status"] == "observed"
  assert result["source_consumer_binding_match"] is True
  assert all(value is True for value in result["matches"].values())
  encoded = json.dumps(result, allow_nan=False)
  assert sample.selected.source_path not in encoded
  assert sample.transport_session not in encoded
  assert "sourceObjectId" not in encoded
  assert all(word not in encoded for word in ("longitude", "latitude", "coordinates", "points_native"))
  assert runtime == original
  result["consumer_binding"]["routeId"] = "f" * 64
  assert runtime == original


@pytest.mark.parametrize(("key", "value", "comparison"), [
  ("routeId", "b" * 64, "route_identity"),
  ("cacheSha256", "b" * 64, "route_identity"),
  ("sourceSessionId", "b" * 64, "route_identity"),
  ("transportSessionId", "other-session", "transport_session"),
  ("sourceObjectId", "other-cache-object", "source_object"),
  ("observedSequence", 8, "observation_sequence"),
])
def test_consumer_mismatch_is_recorded_without_copying_source(key, value, comparison):
  sample, runtime = fixture()
  runtime["mapBinding"][key] = value
  result = probe.route_identity_diagnostics(sample, runtime, map_valid=True)
  assert result["matches"][comparison] is False
  assert result["source_consumer_binding_match"] is False
  if key in result["consumer_binding"]:
    assert result["consumer_binding"][key] == value


@pytest.mark.parametrize(("key", "value"), [
  ("routeId", "/private/path"), ("cacheSha256", "a" * 63), ("sourceSessionId", "A" * 64),
  ("bindingId", True), ("sourceGeometrySha256", None), ("geometrySha256", "g" * 64),
  ("provider", "/private/provider"), ("revision", True), ("revision", -1),
  ("observedSequence", True), ("observedSequence", 0), ("observedSequence", 1 << 64),
  ("observedMonoTime", float("nan")), ("observedMonoTime", -1),
  ("transportSessionId", "x" * 257), ("sourceObjectId", "x" * 4097),
  ("sourceObjectId", "\ud800"), ("transportSessionId", "unsafe\nidentity"),
])
def test_malformed_consumer_identity_is_unknown_and_cannot_forge_match(key, value):
  sample, runtime = fixture()
  runtime["mapBinding"][key] = value
  result = probe.route_identity_diagnostics(sample, runtime, map_valid=True)
  assert result["consumer_binding_status"] == "unknown"
  assert result["source_consumer_binding_match"] == "unknown"
  assert all(value == "unknown" for value in result["matches"].values())
  encoded = json.dumps(result, allow_nan=False)
  assert "/private/" not in encoded


@pytest.mark.parametrize("fault", ["map_false", "map_bool_alias", "source", "reason", "correlation", "version", "missing"])
def test_rejected_or_missing_consumer_binding_never_falls_back_to_source(fault):
  sample, runtime = fixture()
  map_valid = True
  if fault == "map_false":
    map_valid = False
  elif fault == "map_bool_alias":
    map_valid = 1
  elif fault == "source":
    runtime["source"] = "native"
  elif fault == "reason":
    runtime["mapReason"] = "phoneMapSuppressed"
  elif fault == "correlation":
    runtime["mapCorrelationReason"] = "mapSidecarRejected"
  elif fault == "version":
    runtime["mapBinding"]["version"] = True
  else:
    runtime.pop("mapBinding")
  result = probe.route_identity_diagnostics(sample, runtime, map_valid=map_valid)
  assert result["selected"]["path_identity_hash"] == sample.selected.identity_hash
  assert set(result["consumer_binding"].values()) == {"unknown"}
  assert result["source_consumer_binding_match"] == "unknown"


@pytest.mark.parametrize(("field", "value"), [
  ("identity_hash", "A" * 64), ("identity_hash", 123), ("source_session_id", "not-a-hash"),
  ("cache_sha256", None), ("cache_route_index", True), ("cache_route_index", -1), ("cache_route_index", 1 << 32),
])
def test_invalid_selected_fields_are_unknown(field, value):
  sample, runtime = fixture()
  setattr(sample.selected, field, value)
  result = probe.route_identity_diagnostics(sample, runtime, map_valid=True)
  key = "path_identity_hash" if field == "identity_hash" else field
  assert result["selected"][key] == "unknown"
  if field != "cache_route_index":
    assert result["source_consumer_binding_match"] == "unknown"


@pytest.mark.parametrize("runtime", [None, {}, [], True, "unknown"])
def test_missing_evidence_is_explicit_unknown(runtime):
  result = probe.route_identity_diagnostics(None, runtime, map_valid=True)
  assert set(result["selected"].values()) == {"unknown"}
  assert set(result["consumer_binding"].values()) == {"unknown"}
  assert result["source_consumer_binding_match"] == "unknown"


def test_extra_binding_fields_and_frozen_mappings_cannot_leak_private_values():
  sample, runtime = fixture()
  runtime["mapBinding"].update(latitude=31.5, longitude=121.5, secret_path="/private/raw")
  runtime["mapBinding"] = MappingProxyType(runtime["mapBinding"])
  result = probe.route_identity_diagnostics(sample, MappingProxyType(runtime), map_valid=True)
  assert result["source_consumer_binding_match"] is True
  encoded = json.dumps(result, allow_nan=False)
  assert all(secret not in encoded for secret in ("latitude", "longitude", "/private/raw"))


def test_actual_navruntime_evidence_is_read_without_reconstructing_consumer(monkeypatch):
  monkeypatch.setattr(mapsd.time, "monotonic", lambda: NOW_NS / 1e9)
  monkeypatch.setattr(mapsd.time, "monotonic_ns", lambda: NOW_NS)
  for module in (mapsd, nav_inputs, navd):
    monkeypatch.setattr(module, "cplink_navi_active", lambda: True)
  sample, _ = fixture()
  adapter = AmapPhoneRouteAdapter()
  publication = adapter.accept(sample, now_ns=NOW_NS)
  case = SimpleNamespace(clock=[NOW_NS], sample=sample, adapter=adapter, publication=publication,
                         sm=PhoneSM(publication), renderer=mapsd.MapsD())
  runtime = model_runtime(case, monkeypatch)
  image, _ = runtime.update(case.sm, 0.0, MAP_SHAPE, VEC_SHAPE)
  assert image is not None and runtime.map_valid
  details = {"runtime": runtime.evidence(), "source_reason": adapter.reason, "map_valid": runtime.map_valid,
             "instruction_valid": runtime.instruction_valid, "reset_requested": False}
  inputs = {"nav_map": image, "navigation": np.zeros(VEC_SHAPE, np.float32)}
  result = probe.frame_record(0, 0, sample, details, inputs)["route_identity_diagnostics"]
  assert result["source_consumer_binding_match"] is True
  assert result["consumer_binding"]["bindingId"] == runtime.evidence()["mapBinding"]["bindingId"]
  assert result["consumer_binding"]["sourceObjectHash"] == hashlib.sha256(sample.selected.source_path.encode()).hexdigest()


def test_consumer_does_not_reuse_old_session_after_transport_reconnect(monkeypatch):
  clock = [NOW_NS]
  monkeypatch.setattr(mapsd.time, "monotonic", lambda: clock[0] / 1e9)
  monkeypatch.setattr(mapsd.time, "monotonic_ns", lambda: clock[0])
  for module in (mapsd, nav_inputs, navd):
    monkeypatch.setattr(module, "cplink_navi_active", lambda: True)
  old_sample, _ = fixture()
  old_adapter = AmapPhoneRouteAdapter()
  old_publication = old_adapter.accept(old_sample, now_ns=NOW_NS)
  pipeline = SimpleNamespace(
    clock=[NOW_NS], sample=old_sample, adapter=old_adapter, publication=old_publication,
    sm=PhoneSM(old_publication), renderer=mapsd.MapsD())
  runtime = model_runtime(pipeline, monkeypatch)
  image, _ = runtime.update(pipeline.sm, 0.0, MAP_SHAPE, VEC_SHAPE)
  assert image is not None and image.any()
  assert runtime.evidence()["mapBinding"]["transportSessionId"] == "transport-1"

  reconnect_ns = NOW_NS + 500_000_000
  clock[0] = reconnect_ns
  pipeline.clock[0] = reconnect_ns
  old_adapter.withdraw(reconnect_ns, "phone_source_unavailable")
  new_sample = make_sample(now_ns=reconnect_ns, sequence=2,
                           elapsed_ns=old_sample.gps.elapsed_realtime_ns + 500_000_000)
  new_sample.transport_session = "transport-2"
  new_adapter = AmapPhoneRouteAdapter()
  new_publication = new_adapter.accept(new_sample, now_ns=reconnect_ns + 100_000_000)
  assert new_publication is not None, new_adapter.reason
  new_sm = PhoneSM(new_publication, now_ns=reconnect_ns + 100_000_000)
  pipeline.sample, pipeline.adapter, pipeline.publication = new_sample, new_adapter, new_publication
  pipeline.sm, pipeline.renderer = new_sm, mapsd.MapsD()
  pipeline.renderer._update_phone_route(new_sm, (reconnect_ns + 100_000_000) / 1e9)
  clock[0] = reconnect_ns + 100_000_000
  pipeline.clock[0] = clock[0]
  image, _ = runtime.update(new_sm, 0.0, MAP_SHAPE, VEC_SHAPE)
  assert image is None
  queue_frame(runtime, pipeline, frame_id=43)
  image, _ = runtime.update(new_sm, 0.0, MAP_SHAPE, VEC_SHAPE)
  assert image is not None and image.any()
  binding = runtime.evidence()["mapBinding"]
  assert binding["transportSessionId"] == "transport-2"
  assert binding["observedSequence"] == new_publication.active.observed_sequence
