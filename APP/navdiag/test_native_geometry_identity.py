from copy import deepcopy
import hashlib
import struct
from types import SimpleNamespace

import pytest

from APP.navdiag import native_route_source as native
from APP.navdiag import live_route_snapshot as live
from APP.navdiag import live_pipeline_probe as probe
from APP.navdiag import test_phone_route_producer as producer_tests
from test_native_route_source import EPOCH, geometry_frame, setup as setup


KIND = "amap_native_window_except_path_count_v1"


def payload(*, count=1, index=0, state=1, path_hash=None, source_epoch=EPOCH, **kwargs):
  data = bytearray(geometry_frame(path_hash=path_hash, source_epoch=source_epoch, **kwargs).split(b"\n", 1)[1][:-1])
  struct.pack_into("<3I", data, 24, state, count, index)
  return bytes(data)


def parsed(data=None, **kwargs):
  source = native.NativeRouteProbeSource.__new__(native.NativeRouteProbeSource)
  source.epoch = kwargs.get("source_epoch", EPOCH)
  result = source._parse_geometry(payload(**kwargs) if data is None else data)
  result.update(host_observation_clock_domain="host_monotonic",
                host_observation_started_monotonic_ns=1, host_observation_finished_monotonic_ns=2)
  return result


def expected_identity(data):
  return hashlib.sha256(b"AMAP_NATIVE_GEOMETRY_ID_V1\x00" + data[:28] + data[32:]).hexdigest()


@pytest.mark.parametrize("count", [2, 8, 32])
def test_alternative_count_updates_raw_payload_but_not_selected_geometry_identity(count):
  first, second = parsed(), parsed(count=count)
  left, right = (live.select_native(value, epoch=EPOCH) for value in (first, second))
  assert left.source_session_id == right.source_session_id
  assert left.cache_sha256 == right.cache_sha256 == expected_identity(payload())
  assert left.source_path == right.source_path
  assert left.source_payload_sha256 == first["source_geometry_sha256"] == hashlib.sha256(payload()).hexdigest()
  assert right.source_payload_sha256 == second["source_geometry_sha256"] == hashlib.sha256(payload(count=count)).hexdigest()
  assert left.source_payload_sha256 != right.source_payload_sha256
  assert left.geometry_identity_kind == right.geometry_identity_kind == KIND
  assert right.route.path_count == count
  assert right.summary()["source_payload_sha256"] == right.source_payload_sha256
  assert right.summary()["geometry_identity_kind"] == KIND


@pytest.mark.parametrize("change", ["point", "span", "path", "state", "index", "epoch"])
def test_all_fields_except_alternative_count_still_change_identity(change):
  original = bytearray(payload(count=2))
  changed = bytearray(original)
  if change == "point":
    struct.pack_into("<i", changed, 124, 3_600_001)
  elif change == "span":
    changed[112] = 1
  elif change == "path":
    changed[48] ^= 1
  elif change == "state":
    struct.pack_into("<I", changed, 24, 3)
  elif change == "index":
    struct.pack_into("<I", changed, 32, 1)
  else:
    changed[80] ^= 1
  assert native.geometry_identity_sha256(bytes(changed)) != native.geometry_identity_sha256(bytes(original))


@pytest.mark.parametrize("count,index", [(0, 0), (33, 0), (1, 1), (2, 2)])
def test_identity_change_never_bypasses_raw_header_validation(count, index):
  with pytest.raises(native.ProbeError, match="native_probe_geometry_header_invalid"):
    parsed(count=count, index=index)


def test_count_byte_tamper_still_fails_full_wire_payload_checksum(setup):
  import native_route_source as wire_native
  header, body = geometry_frame().split(b"\n", 1)
  changed = bytearray(body)
  struct.pack_into("<I", changed, 28, 2)
  setup.options["geometry_responses"] = [header + b"\n" + changed]
  with wire_native.NativeRouteProbeSource(setup.device) as source:
    with pytest.raises(wire_native.ProbeError, match="native_probe_geometry_hash_mismatch"):
      source.observe_geometry()
    assert source.cleanup_verified


@pytest.mark.parametrize("change", [
  {"geometry_identity_sha256": "a" * 64}, {"geometry_identity_sha256": True},
  {"geometry_identity_kind": "unknown"}, {"source_geometry_sha256": "b" * 64},
  {"points_native": ((3_600_001, 7_200_000), (3_600_100, 7_200_000))},
  {"path_count": 2},
])
def test_select_native_recomputes_raw_and_geometry_identity_instead_of_trusting_claims(change):
  value = parsed()
  value.update(change)
  with pytest.raises(live.LiveRouteRejected, match="native_geometry_identity_invalid"):
    live.select_native(value, epoch=EPOCH)


@pytest.mark.parametrize("missing", ["geometry_identity_kind", "geometry_identity_sha256"])
def test_partial_new_identity_contract_is_rejected(missing):
  value = parsed()
  value.pop(missing)
  with pytest.raises(live.LiveRouteRejected, match="native_geometry_identity_invalid"):
    live.select_native(value, epoch=EPOCH)


def test_legacy_geometry_without_new_identity_fields_keeps_old_binding():
  value = parsed()
  value.pop("geometry_identity_kind")
  value.pop("geometry_identity_sha256")
  selected = live.select_native(value, epoch=EPOCH)
  assert selected.source_session_id == value["source_geometry_sha256"]
  assert selected.geometry_identity_kind == "native_payload_sha256_legacy"
  assert selected.source_payload_sha256 == value["source_geometry_sha256"]


def test_same_geometry_recollection_after_count_change_does_not_roll_transport():
  state = producer_tests.rig()
  counts = iter((1, 2))
  state.native.observe_geometry = lambda: parsed(count=next(counts), source_epoch=state.native.epoch,
    path_hash=producer_tests.identity_hash(state.native.path, state.native.epoch))
  first = producer_tests.accept(state)
  state.worker._on_rejection(producer_tests.producer.PhoneRouteProducerRejected("selection_changed_during_read"),
                             state.worker._sequence, state.worker._active())
  second = producer_tests.accept(state)
  assert first.selected.cache_sha256 == second.selected.cache_sha256
  assert first.selected.source_payload_sha256 != second.selected.source_payload_sha256
  assert second.transport_session == first.transport_session
  assert second.sequence > first.sequence and second.host_start_ns > first.host_start_ns


@pytest.mark.parametrize("changed", ["point", "path"])
def test_real_geometry_or_path_change_still_requires_new_transport(changed):
  state = producer_tests.rig()
  values = [parsed(source_epoch=state.native.epoch, path_hash=producer_tests.identity_hash(state.native.path, state.native.epoch))]
  state.native.observe_geometry = lambda: values[0]
  producer_tests.accept(state)
  state.worker._on_rejection(producer_tests.producer.PhoneRouteProducerRejected("selection_changed_during_read"),
                             state.worker._sequence, state.worker._active())
  if changed == "point":
    values[0] = parsed(source_epoch=state.native.epoch, path_hash=producer_tests.identity_hash(state.native.path, state.native.epoch),
                       points=((3_600_001, 7_200_000), (3_600_100, 7_200_000)))
  else:
    state.native.path = 456
    values[0] = parsed(source_epoch=state.native.epoch, path_hash=producer_tests.identity_hash(state.native.path, state.native.epoch))
  with pytest.raises(producer_tests.producer._SourceSessionChanged):
    producer_tests.accept(state)


@pytest.mark.parametrize("raw", [None, True, "private/path", "A" * 64, "a" * 64])
def test_raw_payload_hash_is_bounded_diagnostic_not_identity_match_authority(raw):
  sample = SimpleNamespace(selected=SimpleNamespace(source_payload_sha256=raw))
  original = deepcopy(sample)
  result = probe.route_identity_diagnostics(sample, {}, map_valid=False)
  assert result["selected"]["source_payload_sha256"] == (raw if raw == "a" * 64 else "unknown")
  assert result["source_consumer_binding_match"] == "unknown"
  assert sample == original
