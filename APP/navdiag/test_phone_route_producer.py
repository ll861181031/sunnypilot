from dataclasses import FrozenInstanceError, replace
import gc
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import weakref

import pytest

from APP.navdiag import phone_route_producer as producer
from APP.navdiag.amap_memory_probe import identity_hash
from APP.navdiag.android_gps_source import AndroidGpsFix
from APP.navdiag.live_route_snapshot import SelectedCacheRoute
from APP.navdiag.route_snapshot import CACHE_DIRECTORY, read_file
from tools.cplink_shim.amap_route_cache import decode_cache, SUPPORTED_PROFILE
from tools.cplink_shim.tests.test_amap_route_cache import cache, route


BOOT = "01234567-89ab-cdef-0123-456789abcdef"
EPOCH = "123:456"


class Clock:
  def __init__(self):
    self.now = 10_000_000_000
    self.lock = threading.Lock()
  def __call__(self):
    with self.lock:
      return self.now
  def advance(self, amount=1_000_000):
    with self.lock:
      self.now += amount
      return self.now


class Resource:
  def __init__(self):
    self.cleanup_verified = False
    self.process = object()
    self.closed = 0
  def __enter__(self):
    return self
  def __exit__(self, *_):
    self.closed += 1
    self.process = None
    self.cleanup_verified = True


class Native(Resource):
  def __init__(self, clock):
    super().__init__()
    self.clock = clock
    self.epoch = EPOCH
    self.path = 123
    self.active = True
    self.changes = {}
    self.calls = 0
    self.error = None
    self.hook = None
  def observe_position(self):
    if self.error is not None:
      raise self.error
    self.calls += 1
    if self.hook is not None:
      self.hook(self)
    start = self.clock.advance()
    finish = self.clock.advance()
    path_hash = identity_hash(self.path, self.epoch)
    position = {"status": "observed", "path_identity_hash": path_hash, "is_on_guide_road_raw": 1,
                "segment_index": 0, "link_index": 0, "point_index": 0,
                "native_st_pos_deg": [10.0, 20.0], "matched_st_pos_deg": [10.0, 20.0],
                "tick_time_raw": self.calls, "input_tick_time_raw": self.calls + 50}
    value = {"status": "observed", "profile": producer.SUPPORTED_PROFILE, "sample_consistent": True,
             "gps_navigation_observed": self.active, "navi_type_raw": 0, "navi_state_raw": 1,
             "process_epoch_hash": hashlib.sha256(self.epoch.encode()).hexdigest(),
             "path_identity_hash": path_hash, "_position": position,
             "host_observation_clock_domain": "host_monotonic",
             "host_observation_started_monotonic_ns": start,
             "host_observation_finished_monotonic_ns": finish}
    value.update(self.changes)
    return value

  def observe_position_navi_info(self):
    return self.observe_position()


class Gps(Resource):
  def __init__(self, clock):
    super().__init__()
    self.clock = clock
    self.stale = False
    self.error = None
    self.last = None
  def observe(self):
    if self.error is not None:
      raise self.error
    start, finish = self.clock.advance(), self.clock.advance()
    self.last = AndroidGpsFix("a" * 64, 1.0, 99_000_000, 2_000_000_000 if self.stale else 100_000_000,
                              start, finish, BOOT, 1_000_000_000, (10., 20.))
    return self.last, b"private not retained", {"host_started_ns": start, "host_finished_ns": finish}


class Files(dict):
  pass


class Collector:
  def __init__(self, clock):
    self.clock = clock
    self.calls = 0
    self.delay_ns = 0
    self.last_files = None
    self.bad_hash = False
    self.source_session = "b" * 64
  def __call__(self, device, native):
    self.calls += 1
    self.clock.advance(self.delay_ns)
    raw = cache(ids=[native.path], routes=[route(native.path)])
    decoded = decode_cache(raw, profile=SUPPORTED_PROFILE)
    path = CACHE_DIRECTORY + "/123.dat"
    selected = SelectedCacheRoute(decoded.routes[0], 0, path, "0" * 64 if self.bad_hash else decoded.sha256,
                                   self.source_session, identity_hash(native.path, native.epoch), 1, 2)
    files = Files({path: raw, CACHE_DIRECTORY + "/456.dat": b"unselected memory" * 100})
    self.last_files = weakref.ref(files)
    return selected, {"private_unused": b"not retained"}, files


def rig(**kwargs):
  clock = Clock()
  collector = Collector(clock)
  worker = producer.PhoneRouteProducer(collector=collector, clock_ns=clock, interval_s=0.01, **kwargs)
  worker._session = "test-session"
  return SimpleNamespace(clock=clock, collector=collector, worker=worker, native=Native(clock), gps=Gps(clock), device=Resource())


def accept(state):
  sample = state.worker._take_sample(state.device, state.native, state.gps)
  state.worker._sample = sample
  return sample


def wait_for(check, timeout=1.0):
  deadline = time.monotonic() + timeout
  while time.monotonic() < deadline:
    result = check()
    if result:
      return result
    time.sleep(0.001)
  raise AssertionError("合成后台线程未按期推进")


def test_selected_geometry_reused_but_each_sample_owns_new_real_observation_and_sequence():
  state = rig()
  first, second = accept(state), accept(state)
  assert state.collector.calls == 1
  assert first.selected is second.selected and first.cache_bytes is second.cache_bytes
  assert second.sequence == first.sequence + 1 and second.host_start_ns > first.host_start_ns
  assert second.native_before["_position"]["tick_time_raw"] != second.native_after["_position"]["tick_time_raw"]
  assert state.worker.latest() is second
  before = (second.sequence, second.host_start_ns, second.gps.source_token)
  for _ in range(5):
    assert state.worker.latest() is second
  assert (second.sequence, second.host_start_ns, second.gps.source_token) == before


def anchor_retry_rig(field="point_index", *, continuous=False, fault=None):
  state = rig()
  state.previous = accept(state)
  state.observations, state.fixes, state.broadcasts = [], [], []
  observe_native, observe_gps = state.native.observe_position_navi_info, state.gps.observe

  def native():
    value = observe_native()
    count = len(state.observations)
    value["_position"][field] = count if continuous else int(count > 0)
    state.observations.append(value)
    return value

  def gps():
    fix, raw, evidence = observe_gps()
    count = len(state.fixes)
    if fault == "cumulative_deadline":
      state.clock.advance(1_100_000_000)
    if fault == "gps_rollback":
      fix = replace(fix, elapsed_realtime_ns=5_000_000_000 - count * 1_000_000_000,
                    source_token=("b" if count else "a") * 64)
    elif fault == "gps_conflict":
      fix = replace(fix, source_token=("b" if count else "a") * 64)
    if count:
      if fault == "gps_boot":
        fix = replace(fix, phone_boot_id="11111111-1111-1111-1111-111111111111")
      elif fault == "stale":
        fix = replace(fix, age_upper_ns=2_000_000_000)
      elif fault == "gps_content":
        fix = replace(fix, coordinates=(10.1, 20.))
      elif fault == "window":
        fix = replace(fix, host_finished_ns=fix.host_finished_ns + 1_000_000_000)
      elif fault == "deadline":
        state.clock.advance(2_000_000_000)
      elif fault == "route":
        state.native.path = 456
      elif fault == "cancel":
        state.worker._withdraw("phone_navigation_not_active")
      elif fault == "stop":
        state.worker._stop.set()
      elif fault == "generation":
        state.worker._advance_generation()
      elif fault == "read":
        state.native.error = producer.native_module.ProbeError("read_failed")
    state.fixes.append(fix)
    return fix, raw, evidence

  def broadcast(*_):
    start, finish = state.clock.advance(), state.clock.advance()
    value = {"status": "unavailable", "reason": "no_broadcast", "read_number": len(state.broadcasts) + 1,
             "host_read_started_ns": start, "host_read_finished_ns": finish}
    state.broadcasts.append(value)
    return value

  state.native.observe_position_navi_info = native
  state.gps.observe = gps
  state.worker._bridge_observe = broadcast
  return state


@pytest.mark.parametrize("field", ["segment_index", "link_index", "point_index"])
def test_anchor_pair_retry_uses_new_bracket_without_publishing_mixed_indices(field):
  state = anchor_retry_rig(field)
  sample = state.worker._take_sample(state.device, state.native, state.gps)
  assert len(state.observations) == 3 and len(state.fixes) == len(state.broadcasts) == 2
  assert sample.native_before["_position"][field] == sample.native_after["_position"][field] == 1
  assert sample.host_start_ns == state.observations[1]["host_observation_started_monotonic_ns"]
  assert sample.native_before["host_observation_finished_monotonic_ns"] <= sample.gps.host_started_ns
  assert sample.gps is state.fixes[1]
  assert sample.gps.host_finished_ns <= sample.broadcast["host_read_started_ns"]
  assert sample.broadcast["host_read_finished_ns"] <= sample.native_after["host_observation_started_monotonic_ns"]
  assert sample.broadcast["read_number"] == 2
  assert sample.selected is state.previous.selected and state.collector.calls == 1
  assert sample.sequence == state.previous.sequence + 1
  assert state.worker._sample is state.previous
  assert sample.anchor_pair_retry["recovered"] is True
  assert sample.anchor_pair_retry["extra_pair_observations"] == 1
  assert sample.anchor_pair_retry["original_host_start_ns"] == state.observations[0]["host_observation_started_monotonic_ns"]
  assert sample.anchor_pair_retry["recovery_host_start_ns"] == sample.host_start_ns
  with pytest.raises(TypeError):
    sample.anchor_pair_retry["recovered"] = False


@pytest.mark.parametrize("field", ["segment_index", "link_index", "point_index"])
def test_anchor_pair_retry_never_accepts_a_third_anchor_or_loops(field):
  state = anchor_retry_rig(field, continuous=True)
  with pytest.raises(producer.PhoneRouteProducerRejected, match="phone_anchor_changed_during_read"):
    state.worker._take_sample(state.device, state.native, state.gps)
  assert len(state.observations) == 3 and len(state.fixes) == len(state.broadcasts) == 2
  assert state.worker.latest() is None and state.worker._inactive is None
  assert state.worker._last_host_start == state.previous.host_start_ns
  assert state.worker._sequence == state.previous.sequence


@pytest.mark.parametrize("fault", ["gps_rollback", "gps_conflict", "gps_boot", "gps_content", "window", "stale", "deadline",
                                    "cumulative_deadline", "route", "cancel", "stop", "generation", "read"])
def test_anchor_pair_retry_cannot_bypass_time_identity_generation_or_failure(fault):
  state = anchor_retry_rig(fault=fault)
  with pytest.raises((producer.PhoneRouteProducerRejected, producer.native_module.ProbeError, producer._Stopped)):
    state.worker._take_sample(state.device, state.native, state.gps)
  assert len(state.fixes) == 2 and len(state.observations) <= 3
  assert state.worker._last_host_start == state.previous.host_start_ns
  assert state.worker._sequence == state.previous.sequence
  assert state.worker._sample is None or state.worker._sample is state.previous


def test_anchor_pair_retry_original_deadline_is_not_restarted_from_second_bracket():
  state = anchor_retry_rig(fault="cumulative_deadline")
  with pytest.raises(producer.PhoneRouteProducerRejected, match="^phone_anchor_retry_deadline_exceeded$"):
    state.worker._take_sample(state.device, state.native, state.gps)
  assert len(state.observations) == 2 and len(state.fixes) == 2 and len(state.broadcasts) == 1


def native_geometry(identity):
  return {
    "status": "observed", "geometry_kind": "current_native_path_window",
    "source_geometry_sha256": "f" * 64, "path_identity_hash": identity,
    "epoch_sha256": hashlib.sha256(EPOCH.encode()).hexdigest(),
    "point_count": 3, "segment_count": 1, "link_count": 1,
    "points_native": ((10_000_000, 20_000_000), (10_000_100, 20_000_000),
                      (10_000_100, 20_000_100)),
    "segments": (((0, 3),),), "main_index": 0, "path_count": 1,
    "navi_type_raw": 0, "navi_state_raw": 1,
    "host_observation_clock_domain": "host_monotonic",
    "host_observation_started_monotonic_ns": 1, "host_observation_finished_monotonic_ns": 2,
    "complete_current_path_window_verified": True, "active_route_verified": False,
    "coordinate_frame": "amap_native_map", "coordinate_datum_verified": False,
    "control_input_allowed": False,
  }


def test_native_geometry_success_never_scans_route_cache():
  state = rig()
  state.native.observe_geometry = lambda: native_geometry(identity_hash(state.native.path, state.native.epoch))
  sample = accept(state)
  second = accept(state)
  assert sample.selected.geometry_kind == "current_native_path_window"
  assert sample.selected.complete_route_verified is False
  assert sample.cache_bytes == b"" and state.collector.calls == 0
  assert second.selected is sample.selected and second.cache_bytes == b""


def test_native_geometry_path_must_match_route_observed_before_and_after():
  state = rig()
  state.native.observe_geometry = lambda: native_geometry(identity_hash(456, EPOCH))
  with pytest.raises(producer.PhoneRouteProducerRejected, match="phone_selected_identity_changed"):
    state.worker._take_sample(state.device, state.native, state.gps)
  assert state.collector.calls == 0 and state.worker._cache is None


def test_native_window_is_never_retained_for_file_cache_recovery():
  state = rig()
  state.native.observe_geometry = lambda: native_geometry(identity_hash(state.native.path, state.native.epoch))
  accept(state)
  state.worker._on_rejection(producer.PhoneRouteProducerRejected("selection_changed_during_read"),
                             state.worker._sequence)
  assert state.worker._inactive is None and state.worker._retained is None
  assert state.worker._selection is None and state.worker._cache is None


def test_old_probe_without_native_geometry_keeps_bounded_cache_fallback():
  state = rig()
  assert not hasattr(state.native, "observe_geometry")
  sample = accept(state)
  assert isinstance(sample.selected, SelectedCacheRoute)
  assert state.collector.calls == 1 and sample.cache_bytes


def test_rejection_events_preserve_gps_live_source_without_changing_reason():
  state = rig()
  live = {"registration": "request_accepted", "gps_registration": "request_accepted",
          "passive_registration": "request_accepted", "callback_count": 7,
          "gps_callback_count": 3, "passive_callback_count": 4, "cache_read_count": 9,
          "callbacks_during_observation": 7, "observation_elapsed_ms": 120,
          "callback_age_ms": 120, "last_read_age_ms": 130, "callback_thread_alive": True}
  error = producer.gps_module.GpsProbeRejected("gps_source_stale", live_source=live)
  state.worker._on_rejection(error, state.worker._sequence, state.worker._generation)
  record = state.worker.drain_rejection_events()["events"][0]
  assert record["reason"] == "gps_source_stale" and record["live_source"] == live
  assert record["live_source_present"] is True


def test_slow_cache_collection_is_followed_by_new_position_not_retimed_old_sample():
  state = rig()
  state.collector.delay_ns = 1_500_000_000
  before = state.clock()
  sample = accept(state)
  assert sample.host_start_ns > before + state.collector.delay_ns
  assert sample.host_start_ns == sample.native_before["host_observation_started_monotonic_ns"]
  assert state.clock() - sample.host_start_ns < 10_000_000


def test_route_change_decodes_again_and_keeps_only_selected_original_blob():
  state = rig()
  first = accept(state)
  state.native.path = 456
  second = accept(state)
  assert state.collector.calls == 2 and second.selected.identity_hash != first.selected.identity_hash
  assert hashlib.sha256(second.cache_bytes).hexdigest() == second.selected.cache_sha256
  assert second.source_object == second.selected.source_path
  gc.collect()
  assert state.collector.last_files() is None
  assert state.worker._cache is second.cache_bytes


def test_source_epoch_change_requires_reconnect_instead_of_reusing_identity_by_path_id():
  state = rig()
  accept(state)
  state.native.epoch = "123:999"
  with pytest.raises(producer._Reconnect, match="phone_provider_epoch_changed"):
    accept(state)
  assert state.collector.calls == 1 and state.worker._sample is None and state.worker._inactive is None


@pytest.mark.parametrize("geometry", ["cache", "native"])
@pytest.mark.parametrize("withdraw_first", [True, False])
def test_source_session_change_requires_new_transport_before_publish(geometry, withdraw_first):
  state = rig()
  if geometry == "native":
    def observe_geometry():
      value = native_geometry(identity_hash(state.native.path, state.native.epoch))
      value["source_geometry_sha256"] = ("f" if state.native.path == 123 else "e") * 64
      return value
    state.native.observe_geometry = observe_geometry
  first = accept(state)
  first_generation = state.worker._active()
  if withdraw_first:
    state.worker._withdraw("phone_navigation_not_active")
  state.native.path = 456
  state.collector.source_session = "c" * 64
  with pytest.raises(producer._Reconnect, match="^phone_source_session_changed$"):
    accept(state)
  assert state.worker._sample is None and state.worker._selection is None
  assert state.worker._cache is None and state.worker._inactive is None and state.worker._retained is None
  assert state.worker._sequence == first.sequence
  assert not state.worker._publish(first, first_generation)


def test_source_session_change_with_invalid_pair_does_not_replace_verified_session():
  state = rig()
  accept(state)
  state.worker._withdraw("phone_navigation_not_active")
  state.collector.source_session = "c" * 64
  state.gps.stale = True
  with pytest.raises(producer.PhoneRouteProducerRejected, match="phone_pair_inconsistent_or_stale"):
    accept(state)
  state.gps.stale = False
  with pytest.raises(producer._Reconnect, match="^phone_source_session_changed$"):
    accept(state)


def test_snapshot_and_nested_positions_are_immutable_and_repr_does_not_expose_coordinates():
  state = rig()
  sample = accept(state)
  with pytest.raises(FrozenInstanceError):
    sample.sequence = 99
  with pytest.raises(TypeError):
    sample.native_before["_position"]["native_st_pos_deg"][0] = 999
  with pytest.raises(TypeError):
    sample.native_after["_position"]["is_on_guide_road_raw"] = 0
  assert "10.0" not in repr(sample) and EPOCH not in repr(sample)


def test_old_sample_and_old_gps_cannot_be_renewed_by_latest_reads():
  state = rig()
  sample = accept(state)
  state.clock.advance(1_500_000_000)
  assert state.worker.latest() is sample
  state.clock.advance(500_000_000)
  assert state.worker.latest() is None and state.worker._selection is None and state.worker._cache is None
  assert state.worker.status().reason == "phone_sample_expired"
  assert sample.host_start_ns < state.clock()
  state = rig()
  sample = accept(state)
  state.worker._sample = replace(sample, gps=replace(sample.gps, age_upper_ns=1_999_999_999))
  assert state.worker.latest() is None


@pytest.mark.parametrize("change", [
  {"gps_navigation_observed": False}, {"navi_type_raw": 1}, {"navi_type_raw": False},
  {"navi_state_raw": 4}, {"profile": "unknown"}, {"sample_consistent": False},
  {"process_epoch_hash": "0" * 64}, {"host_observation_clock_domain": "phone_elapsed_realtime"},
])
def test_cancel_simulation_and_wrong_epoch_never_read_cache(change):
  state = rig()
  state.native.changes = change
  with pytest.raises(producer.PhoneRouteProducerRejected):
    state.worker._take_sample(state.device, state.native, state.gps)
  assert state.collector.calls == 0


@pytest.mark.parametrize("onroad", [0, 2, -1, True, None])
def test_native_onroad_must_be_exact_integer_one(onroad):
  state = rig()
  position = state.native.observe_position()["_position"]
  position["is_on_guide_road_raw"] = onroad
  state.native.changes = {"_position": position}
  with pytest.raises(producer.PhoneRouteProducerRejected, match="position_unavailable"):
    state.worker._take_sample(state.device, state.native, state.gps)
  assert state.collector.calls == 0


def test_gps_expiry_and_route_change_during_pair_never_publish():
  state = rig()
  state.gps.stale = True
  with pytest.raises(producer.PhoneRouteProducerRejected, match="inconsistent_or_stale"):
    state.worker._take_sample(state.device, state.native, state.gps)
  state = rig()
  state.native.hook = lambda item: setattr(item, "path", 456) if item.calls == 3 else None
  with pytest.raises(producer.PhoneRouteProducerRejected, match="inconsistent_or_stale"):
    state.worker._take_sample(state.device, state.native, state.gps)


def test_selected_cache_hash_mismatch_rejected_before_sampling_gps():
  state = rig()
  state.collector.bad_hash = True
  with pytest.raises(producer.PhoneRouteProducerRejected, match="cache_invalid"):
    state.worker._take_sample(state.device, state.native, state.gps)
  assert state.gps.last is None


def test_replayed_native_start_cannot_create_new_sequence():
  state = rig()
  sample = accept(state)
  state.worker._last_host_start = state.clock() + 100_000_000
  with pytest.raises(producer.PhoneRouteProducerRejected, match="observation_replayed"):
    state.worker._take_sample(state.device, state.native, state.gps)
  assert state.worker._sequence == sample.sequence


class ThreadRig:
  def __init__(self, *, blocking_collector=False):
    self.clock = Clock()
    self.collector = Collector(self.clock)
    self.devices, self.natives, self.gpses = [], [], []
    self.started = threading.Event()
    self.blocking_collector = blocking_collector
    self.worker = producer.PhoneRouteProducer(device_factory=self.device, native_factory=self.native,
      bridge_service_factory=self.bridge_service,
      gps_factory=self.gps, collector=self.collect, clock_ns=self.clock, interval_s=0.01)
  def device(self, stop):
    item = Resource()
    self.devices.append(item)
    return item
  def native(self, device, stop):
    item = Native(self.clock)
    self.natives.append(item)
    return item
  def bridge_service(self, device, stop):
    return Resource()
  def gps(self, device, stop):
    item = Gps(self.clock)
    self.gpses.append(item)
    return item
  def collect(self, device, native):
    self.started.set()
    if self.blocking_collector:
      self.worker._stop.wait()
      raise producer._Stopped()
    return self.collector(device, native)


class RecordingStopEvent:
  def __init__(self):
    self._event = threading.Event()
    self._lock = threading.Lock()
    self._waits = []
  def set(self):
    self._event.set()
  def is_set(self):
    return self._event.is_set()
  def wait(self, timeout=None):
    with self._lock:
      self._waits.append(timeout)
    return self._event.wait(timeout)
  def wait_count(self):
    with self._lock:
      return len(self._waits)


def test_latest_is_nonblocking_while_background_cache_read_is_waiting_and_close_joins():
  state = ThreadRig(blocking_collector=True)
  state.worker.start()
  try:
    assert state.started.wait(1.0)
    started = time.monotonic()
    assert state.worker.latest() is None
    assert time.monotonic() - started < 0.05
  finally:
    started = time.monotonic()
    assert state.worker.close()
    assert time.monotonic() - started < 0.5
  assert all(item.closed == 1 and item.cleanup_verified for item in state.devices + state.natives + state.gpses)
  assert not state.worker.status().running and state.worker.status().cleanup_verified


def test_background_cancel_withdraws_and_clears_geometry_without_retaining_old_sample():
  state = ThreadRig()
  state.worker.start()
  try:
    wait_for(state.worker.latest)
    state.natives[0].active = False
    wait_for(lambda: state.worker.status().reason == "phone_navigation_not_active")
    assert state.worker.latest() is None and state.worker._selection is None and state.worker._cache is None
  finally:
    assert state.worker.close()


def test_background_cancel_replan_recovers_adapter_with_new_source_and_transport():
  from openpilot.selfdrive.navigation.amap_phone_route import AmapPhoneRouteAdapter
  from openpilot.selfdrive.navigation.tests.test_amap_phone_pipeline import (
    NATIVE_POSITION, WGS_POSITION, make_sample,
  )
  state = ThreadRig()
  provider = {"path": 123, "active": True}

  class PositionedNative(Native):
    def observe_position(self):
      self.path, self.active = provider["path"], provider["active"]
      value = super().observe_position()
      value["_position"].update(native_st_pos_deg=NATIVE_POSITION, matched_st_pos_deg=NATIVE_POSITION)
      return value

  class PositionedGps(Gps):
    def observe(self):
      fix, raw, window = super().observe()
      return replace(fix, coordinates=WGS_POSITION), raw, window

  def native(device, stop):
    item = PositionedNative(state.clock)
    state.natives.append(item)
    return item

  def gps(device, stop):
    item = PositionedGps(state.clock)
    state.gpses.append(item)
    return item

  def collect(device, source):
    template = make_sample(path_id=source.path)
    decoded = decode_cache(template.cache_bytes, profile=SUPPORTED_PROFILE)
    selected = SelectedCacheRoute(
      decoded.routes[0], 0, template.selected.source_path, decoded.sha256, decoded.sha256,
      template.selected.identity_hash, 1, 2)
    return selected, {}, {selected.source_path: template.cache_bytes}

  state.worker._native_factory = native
  state.worker._gps_factory = gps
  state.worker._collector = collect
  adapter = AmapPhoneRouteAdapter()
  state.worker.start()
  try:
    first = wait_for(state.worker.latest)
    publication = adapter.accept(first, now_ns=state.clock())
    assert publication is not None, adapter.reason
    provider["active"] = False
    wait_for(lambda: state.worker.status().reason == "phone_navigation_not_active")
    assert state.worker.latest() is None
    assert adapter.accept(None, now_ns=state.clock()) is None
    provider.update(path=456, active=True)
    recovered = wait_for(lambda: sample if (sample := state.worker.latest()) is not None
                        and sample.transport_session != first.transport_session else None)
    assert recovered.selected.source_session_id != first.selected.source_session_id
    publication = adapter.accept(recovered, now_ns=state.clock())
    assert publication is not None, adapter.reason
    assert publication.active.transport_session_id == recovered.transport_session
    assert publication.snapshot.identity.route_id == recovered.selected.identity_hash
    assert len(state.devices) == len(state.natives) == len(state.gpses) == 1
    assert state.devices[0].closed == state.natives[0].closed == state.gpses[0].closed == 0
    assert adapter.accept(first, now_ns=state.clock()) is None
    assert adapter.reason == "phone_retired_transport_observation"
  finally:
    assert state.worker.close()
  assert all(item.cleanup_verified for item in state.devices + state.natives + state.gpses)


@pytest.mark.parametrize("geometry", ["cache", "native"])
@pytest.mark.parametrize("cancel_first", [False, True])
def test_background_route_rollover_reuses_live_resources_with_fresh_sample(geometry, cancel_first):
  state = ThreadRig()
  provider = {"path": 123, "active": True}
  original_factory = state.worker._native_factory
  geometry_paths = []

  def native(device, stop):
    item = original_factory(device, stop)
    original_observe = item.observe_position

    def observe_position():
      item.path, item.active = provider["path"], provider["active"]
      return original_observe()

    item.observe_position = observe_position
    if geometry == "native":
      def observe_geometry():
        geometry_paths.append(item.path)
        value = native_geometry(identity_hash(item.path, item.epoch))
        value["source_geometry_sha256"] = ("f" if item.path == 123 else "e") * 64
        return value

      item.observe_geometry = observe_geometry
    return item

  state.worker._native_factory = native
  state.worker.start()
  try:
    first = wait_for(state.worker.latest)
    original_resources = state.devices + state.natives + state.gpses
    if cancel_first:
      provider["active"] = False
      wait_for(lambda: state.worker.status().reason == "phone_navigation_not_active")
      assert state.worker.latest() is None
    state.collector.source_session = "c" * 64
    provider.update(path=456, active=True)
    recovered = wait_for(lambda: sample if (sample := state.worker.latest()) is not None
                        and sample.transport_session != first.transport_session else None)
    assert recovered.selected.source_session_id != first.selected.source_session_id
    assert recovered.selected.identity_hash == identity_hash(456, EPOCH)
    assert recovered.host_start_ns > first.host_start_ns
    assert recovered.native_before["host_observation_started_monotonic_ns"] > first.host_start_ns
    if geometry == "native":
      assert geometry_paths == [123, 456]
    else:
      assert state.collector.calls == 2
    assert len(state.devices) == len(state.natives) == len(state.gpses) == 1
    assert all(item.closed == 0 and not item.cleanup_verified for item in original_resources)
    assert not state.worker._publish(first, 0)
    assert state.worker.status().connected
  finally:
    assert state.worker.close()
  assert all(item.closed == 1 and item.cleanup_verified for item in state.devices + state.natives + state.gpses)


def source_change_pending(state):
  first = accept(state)
  generation = state.worker._active()
  state.collector.source_session = "c" * 64
  state.native.path = 456
  with pytest.raises(producer._Reconnect, match="^phone_source_session_changed$"):
    accept(state)
  return first, generation + 1


def test_source_rollover_preserves_physical_clock_barriers_and_recollects_geometry():
  state = rig()
  first, generation = source_change_pending(state)
  old_clock = (state.worker._last_host_start, state.worker._gps_identity, state.worker._provider_epoch)
  assert state.worker._rollover_source_transport(expected_generation=generation)
  assert state.worker._session != first.transport_session
  assert state.worker._sequence == state.worker._published_sequence == 0
  assert state.worker._published_session is None
  assert state.worker._route_source_session is None
  assert state.worker._sample is state.worker._selection is state.worker._retained is state.worker._inactive is None
  assert (state.worker._last_host_start, state.worker._gps_identity, state.worker._provider_epoch) == old_clock
  recovered = accept(state)
  assert recovered.sequence == 1 and recovered.host_start_ns > first.host_start_ns
  assert recovered.transport_session != first.transport_session
  assert state.collector.calls == 3
  assert not state.worker._publish(first, generation - 1)


@pytest.mark.parametrize("boundary", ["generation", "reason", "stopped", "closed"])
def test_source_rollover_cannot_overwrite_a_newer_or_closed_state(boundary):
  state = rig()
  _, generation = source_change_pending(state)
  if boundary == "generation":
    state.worker._advance_generation()
  elif boundary == "reason":
    state.worker._withdraw("phone_navigation_not_active", advance=False)
  elif boundary == "stopped":
    state.worker._stop.set()
  else:
    state.worker._closed = True
  before = (state.worker._session, state.worker._generation, state.worker._sequence,
            state.worker._route_source_session, state.worker._reason)
  assert not state.worker._rollover_source_transport(expected_generation=generation)
  assert (state.worker._session, state.worker._generation, state.worker._sequence,
          state.worker._route_source_session, state.worker._reason) == before


def test_source_rollover_does_not_erase_gps_epoch_and_time_rollback_protection():
  state = rig()
  _, generation = source_change_pending(state)
  assert state.worker._rollover_source_transport(expected_generation=generation)
  original = state.gps.observe

  def rollback():
    fix, raw, window = original()
    return replace(fix, elapsed_realtime_ns=fix.elapsed_realtime_ns - 1), raw, window

  state.gps.observe = rollback
  with pytest.raises(producer._Reconnect, match="^phone_gps_epoch_or_time_changed$"):
    accept(state)
  assert state.worker.latest() is None


def test_source_session_reason_from_other_reconnect_still_rebuilds_resources():
  state = ThreadRig()
  state.worker.start()
  try:
    first = wait_for(state.worker.latest)
    state.gpses[0].error = producer._Reconnect("phone_source_session_changed")
    wait_for(lambda: sample if (sample := state.worker.latest()) is not None
             and sample.transport_session != first.transport_session else None)
    assert len(state.devices) == len(state.natives) == len(state.gpses) == 2
    assert state.devices[0].closed == state.natives[0].closed == state.gpses[0].closed == 1
  finally:
    assert state.worker.close()
  assert all(item.closed == 1 and item.cleanup_verified for item in state.devices + state.natives + state.gpses)


def pending_source_handoff():
  state = rig()
  first = accept(state)
  original_generation = state.worker._active()
  state.native.path = 456
  state.collector.source_session = "c" * 64
  with pytest.raises(producer._SourceSessionChanged) as captured:
    state.worker._take_sample(state.device, state.native, state.gps, original_generation)
  return state, first, captured.value, original_generation + 1


def test_source_handoff_adopts_only_the_unpublished_new_pair_with_original_clocks():
  state, first, error, generation = pending_source_handoff()
  pending = error._sample
  assert state.worker.latest() is None and pending.selected is not first.selected
  calls = state.collector.calls, state.native.calls
  sample, new_generation = state.worker._adopt_source_sample(error, expected_generation=generation)
  assert error._sample is None
  assert (state.collector.calls, state.native.calls) == calls == (2, 6)
  assert sample.transport_session not in (first.transport_session, pending.transport_session)
  assert new_generation == generation + 1 and sample.sequence == 1
  assert sample.selected is pending.selected and sample.cache_bytes is pending.cache_bytes
  assert sample.gps is pending.gps and sample.host_start_ns == pending.host_start_ns
  assert sample.native_before is pending.native_before and sample.native_after is pending.native_after
  assert sample.broadcast is pending.broadcast
  assert state.worker._last_host_start == sample.host_start_ns
  assert state.worker._gps_identity == (sample.gps.phone_boot_id, sample.gps.elapsed_realtime_ns, sample.gps.source_token)
  assert state.worker._provider_epoch == sample.epoch
  assert state.worker._selection is sample.selected and state.worker._cache is sample.cache_bytes
  assert state.worker._inactive is None and state.worker.latest() is None
  assert not state.worker._publish(first, new_generation) and not state.worker._publish(pending, generation)
  assert state.worker._publish(sample, new_generation) and state.worker.latest() is sample
  assert state.worker._adopt_source_sample(error, expected_generation=new_generation) is None
  assert "10.0" not in repr(error) and EPOCH not in repr(error)


@pytest.mark.parametrize("boundary", ["generation", "reason", "stopped", "closed", "provider", "transport",
                                     "sequence", "host_replay", "host_expired", "host_future", "gps_expired",
                                     "gps_boot", "gps_rollback", "gps_token", "same_source", "error_generation"])
def test_source_handoff_cannot_adopt_stale_or_superseded_pair(boundary):
  state, _, error, generation = pending_source_handoff()
  sample = error._sample
  if boundary == "generation":
    state.worker._advance_generation()
  elif boundary == "reason":
    state.worker._withdraw("phone_navigation_not_active", advance=False)
  elif boundary == "stopped":
    state.worker._stop.set()
  elif boundary == "closed":
    state.worker._closed = True
  elif boundary == "provider":
    state.worker._provider_epoch = "123:999"
  elif boundary == "transport":
    error._sample = replace(sample, transport_session="retired")
  elif boundary == "sequence":
    error._sample = replace(sample, sequence=sample.sequence + 1)
  elif boundary == "host_replay":
    error._sample = replace(sample, host_start_ns=state.worker._last_host_start)
  elif boundary == "host_expired":
    state.clock.advance(state.worker._max_sample_age_ns)
  elif boundary == "host_future":
    state.clock.now = sample.native_after["host_observation_finished_monotonic_ns"] - 1
  elif boundary == "gps_expired":
    error._sample = replace(sample, gps=replace(sample.gps, age_upper_ns=2_000_000_000))
  elif boundary == "gps_boot":
    error._sample = replace(sample, gps=replace(sample.gps, phone_boot_id="11234567-89ab-cdef-0123-456789abcdef"))
  elif boundary == "gps_rollback":
    error._sample = replace(sample, gps=replace(sample.gps, elapsed_realtime_ns=sample.gps.elapsed_realtime_ns - 1))
  elif boundary == "gps_token":
    error._sample = replace(sample, gps=replace(sample.gps, source_token="d" * 64))
  elif boundary == "same_source":
    error._sample = replace(sample, selected=replace(sample.selected, source_session_id=state.worker._route_source_session))
  else:
    error._generation -= 1

  def state_values():
    return (state.worker._generation, state.worker._session, state.worker._sequence, state.worker._published_sequence,
            state.worker._published_session, state.worker._selection, state.worker._cache, state.worker._retained,
            state.worker._inactive, state.worker._route_source_session, state.worker._last_host_start,
            state.worker._gps_identity, state.worker._provider_epoch, state.worker._reason)

  before = state_values()
  assert state.worker._adopt_source_sample(error, expected_generation=generation) is None
  assert state_values() == before and error._sample is None
  assert state.worker.latest() is None


def test_background_expired_handoff_discards_pair_and_recollects_on_same_resources():
  state = ThreadRig()
  original_adopt = state.worker._adopt_source_sample
  attempts = []

  def expire_once(error, **kwargs):
    attempts.append(error._sample)
    state.clock.advance(2_000_000_000)
    result = original_adopt(error, **kwargs)
    assert result is None
    return result

  state.worker._adopt_source_sample = expire_once
  state.worker.start()
  try:
    first = wait_for(state.worker.latest)
    state.collector.source_session = "c" * 64
    state.natives[0].path = 456
    recovered = wait_for(lambda: sample if (sample := state.worker.latest()) is not None
                        and sample.transport_session != first.transport_session else None)
    assert len(attempts) == 1 and state.collector.calls == 3
    assert recovered.host_start_ns > attempts[0].host_start_ns + 2_000_000_000
    assert recovered.selected is not attempts[0].selected
    assert len(state.devices) == len(state.natives) == len(state.gpses) == 1
  finally:
    assert state.worker.close()


def test_disconnect_withdraws_then_reconnects_using_new_transport_and_new_selection():
  state = ThreadRig()
  state.worker.start()
  try:
    first = wait_for(state.worker.latest)
    state.natives[0].error = OSError("synthetic private disconnect details")
    second = wait_for(lambda: sample if (sample := state.worker.latest()) is not None
                      and sample.transport_session != first.transport_session else None)
    assert state.collector.calls >= 2 and second.transport_session != first.transport_session
    assert state.devices[0].closed == 1 and state.natives[0].closed == 1 and state.gpses[0].closed == 1
  finally:
    assert state.worker.close()
  assert all(item.cleanup_verified for item in state.devices + state.natives + state.gpses)


def test_sample_expiry_supersedes_only_inflight_round_and_reuses_phone_transport():
  state = ThreadRig()
  state.worker.start()
  try:
    first = wait_for(state.worker.latest)
    state.clock.advance(2_100_000_000)
    assert state.worker.latest() is None
    second = wait_for(lambda: sample if (sample := state.worker.latest()) is not None
                      and sample.sequence > 1 else None)
    assert second.transport_session == first.transport_session
    assert state.worker.status().connected is True
    assert len(state.devices) == len(state.natives) == len(state.gpses) == 1
    assert all(item.closed == 0 for item in state.devices + state.natives + state.gpses)
    assert state.collector.calls >= 2
  finally:
    assert state.worker.close()
  assert all(item.closed == 1 and item.cleanup_verified
             for item in state.devices + state.natives + state.gpses)


def test_close_can_interrupt_device_wait_instead_of_leaving_daemon_thread():
  ready = threading.Event()
  class WaitingDevice(Resource):
    def __init__(self, stop):
      super().__init__()
      self.stop = stop
    def __enter__(self):
      ready.set()
      self.stop.wait()
      self.__exit__()
      raise producer._Stopped()
  worker = producer.PhoneRouteProducer(device_factory=WaitingDevice, interval_s=0.01)
  worker.start()
  assert ready.wait(1.0)
  started = time.monotonic()
  assert worker.close() and time.monotonic() - started < 0.5
  assert not worker._thread.daemon and not worker._thread.is_alive()


def test_background_read_line_checks_stop_without_waiting_full_protocol_timeout():
  incoming, outgoing = os.pipe()
  source = producer._NativeSource.__new__(producer._NativeSource)
  source._stop = threading.Event()
  stream = os.fdopen(incoming, "rb", buffering=0)
  source.process = SimpleNamespace(stdout=stream)
  timer = threading.Timer(0.01, source._stop.set)
  timer.start()
  try:
    started = time.monotonic()
    with pytest.raises(producer._Stopped):
      source._line(10.0, 100)
    assert time.monotonic() - started < 0.2
  finally:
    timer.join()
    stream.close()
    os.close(outgoing)


def test_interruptible_binary_cache_call_does_not_use_global_subprocess_run():
  path = CACHE_DIRECTORY + "/123.dat"
  calls = []
  class Device:
    def call_binary(self, *args, **kwargs):
      calls.append((args, kwargs))
      return SimpleNamespace(returncode=0, stdout=b"NAVDIAG_PROBE_V1\nprivate-cache\nNAVDIAG_PROBE_RC=000\n")
  assert read_file(Device(), path) == b"private-cache"
  assert calls[0][0][:2] == ("shell", "-T")
  assert calls[0][1] == {"timeout": 10, "check": False}


def test_package_import_does_not_need_sys_path_injection():
  root = str(Path(__file__).resolve().parents[2])
  result = subprocess.run([sys.executable, "-c", "from APP.navdiag.phone_route_producer import PhoneRouteProducer; "
                           + "p=PhoneRouteProducer(); assert p.latest() is None; assert p.close()"],
                          cwd=root, capture_output=True, text=True, check=False)
  assert result.returncode == 0, result.stderr


def test_owned_foreground_adb_refuses_existing_port_without_launch_or_kill(monkeypatch):
  class Socket:
    def __enter__(self):
      return self
    def __exit__(self, *_):
      pass
    def settimeout(self, _):
      pass
    def connect_ex(self, _):
      return 0
  monkeypatch.setattr(producer.socket, "socket", Socket)
  monkeypatch.setattr(producer.subprocess, "Popen", lambda *a, **k: pytest.fail("不能启动或接管其他服务"))
  device = producer._InterruptibleDevice("/fixed/adb", 5039, None, threading.Event())
  with pytest.raises(producer.PhoneRouteProducerRejected, match="port_owned_elsewhere"):
    device.__enter__()
  assert device.cleanup_verified and device.server is None


def test_attach_foreground_adb_never_starts_or_kills_existing_server(monkeypatch):
  stop = threading.Event()
  connections = []

  class Socket:
    def __enter__(self):
      return self
    def __exit__(self, *_):
      pass
    def settimeout(self, _):
      pass
    def connect_ex(self, address):
      connections.append(address)
      return 0

  def host(_self, *args, **kwargs):
    assert args == ("devices",)
    return SimpleNamespace(stdout="List of devices attached\nsynthetic-phone\tdevice\n", returncode=0)

  monkeypatch.setattr(producer.socket, "socket", Socket)
  monkeypatch.setattr(producer.subprocess, "Popen", lambda *a, **k: pytest.fail("附加模式不能启动 ADB"))
  monkeypatch.setattr(producer, "_kill_local", lambda *a: pytest.fail("附加模式不能关闭 ADB"))
  monkeypatch.setattr(producer._InterruptibleDevice, "host", host)
  device = producer._InterruptibleDevice(
    "/fixed/adb", 5037, "synthetic-phone", stop, ownership="attach")
  with device:
    assert device.started is False and device.server is None
    assert device.serial == "synthetic-phone"
  assert connections == [("127.0.0.1", 5037)]
  assert device.cleanup_verified and device.started is False and device.server is None


def test_attach_foreground_adb_fails_closed_when_server_is_missing(monkeypatch):
  class Socket:
    def __enter__(self):
      return self
    def __exit__(self, *_):
      pass
    def settimeout(self, _):
      pass
    def connect_ex(self, _):
      return 111

  monkeypatch.setattr(producer.socket, "socket", Socket)
  monkeypatch.setattr(producer.subprocess, "Popen", lambda *a, **k: pytest.fail("附加模式不能启动 ADB"))
  device = producer._InterruptibleDevice(
    "/fixed/adb", 5037, "synthetic-phone", threading.Event(), ownership="attach")
  with pytest.raises(producer.PhoneRouteProducerRejected, match="phone_adb_server_unavailable"):
    device.__enter__()
  assert device.started is False and device.server is None and device.cleanup_verified


def test_host_command_stop_terminates_only_own_command_process_group(monkeypatch):
  stop = threading.Event()
  class Process:
    pid = 123456
    returncode = None
    stdout = io.StringIO()
    stderr = io.StringIO()
    def poll(self):
      return self.returncode
    def communicate(self, timeout):
      stop.set()
      raise subprocess.TimeoutExpired("synthetic", timeout)
    def wait(self, timeout):
      return self.returncode
  process = Process()
  killed = []
  monkeypatch.setattr(producer.subprocess, "Popen", lambda *a, **k: process)
  def kill(pid, sig):
    killed.append((pid, sig))
    process.returncode = -sig
  monkeypatch.setattr(producer.os, "killpg", kill)
  device = producer._InterruptibleDevice("/fixed/adb", 5039, None, stop)
  with pytest.raises(producer._Stopped):
    device.host("devices", timeout=10)
  assert killed == [(process.pid, signal.SIGTERM)] and process.stdout.closed and process.stderr.closed


def test_cleanup_waits_for_transport_recovery_before_touching_remote_process(monkeypatch):
  events = []
  process = SimpleNamespace(pid=123456, returncode=0, stdout=io.StringIO(), stderr=io.StringIO(),
                            communicate=lambda timeout: (None, None), poll=lambda: 0)
  monkeypatch.setattr(producer.subprocess, "Popen",
                      lambda *args, **kwargs: events.append("cleanup") or process)
  monkeypatch.setattr(producer.os, "killpg", lambda *_args: None)
  device = producer._InterruptibleDevice(
    "/fixed/adb", 5039, "synthetic-phone", threading.Event())

  def wait_for_device(*, deadline):
    assert deadline > time.monotonic()
    events.append("reconnected")
    return device

  monkeypatch.setattr(device, "_wait_for_device", wait_for_device)
  result = device.cleanup_shell("-T", "true")
  assert result.returncode == 0
  assert events == ["reconnected", "cleanup"]


def test_cleanup_does_not_wait_for_transport_during_intentional_close(monkeypatch):
  stop = threading.Event()
  stop.set()
  process = SimpleNamespace(pid=123456, returncode=0, stdout=io.StringIO(), stderr=io.StringIO(),
                            communicate=lambda timeout: (None, None), poll=lambda: 0)
  monkeypatch.setattr(producer.subprocess, "Popen", lambda *_args, **_kwargs: process)
  monkeypatch.setattr(producer.os, "killpg", lambda *_args: None)
  device = producer._InterruptibleDevice("/fixed/adb", 5039, "synthetic-phone", stop)
  monkeypatch.setattr(device, "_wait_for_device",
                      lambda **_kwargs: pytest.fail("主动关闭不能等待 transport"))

  assert device.cleanup_shell("-T", "true").returncode == 0


def test_navigation_bridge_starts_non_exported_service_then_confirms_cleanup(monkeypatch):
  stop = threading.Event()
  clock = Clock()
  start = SimpleNamespace(returncode=0,
    stdout="Starting service: Intent { act=com.jixiexiaoge.drivingassist.navdiag.BRIDGE_START "
           + "cmp=com.jixiexiaoge.drivingassist.navdiag/.BridgeService }\n")
  stop_result = SimpleNamespace(returncode=0,
    stdout="Starting service: Intent { act=com.jixiexiaoge.drivingassist.navdiag.BRIDGE_STOP "
           + "cmp=com.jixiexiaoge.drivingassist.navdiag/.BridgeService }\n")
  command_calls = []
  shell_calls, cleanup_calls = [], []

  def command(args, **kwargs):
    command_calls.append((args, kwargs))
    return start

  def shell(*args, **kwargs):
    shell_calls.append((args, kwargs))
    return SimpleNamespace(returncode=0,
      stdout=("* ServiceRecord{bridge u0 com.jixiexiaoge.drivingassist.navdiag/.BridgeService}\n"
              + "    isForeground=true foregroundId=2\n") if command_calls else "")

  def cleanup(*args, **kwargs):
    cleanup_calls.append((args, kwargs))
    return stop_result if "start-service" in args else SimpleNamespace(returncode=0, stdout="")

  device = SimpleNamespace(adb="/fixed/adb", port=5039, serial="synthetic-phone",
                           _command=command, shell=shell, cleanup_shell=cleanup)
  with producer._NavigationBroadcastBridge(device, stop, clock=clock) as bridge:
    assert bridge.started and not bridge.cleanup_verified
  assert bridge.cleanup_verified and bridge.cleanup_diagnostics == ()
  assert "start-foreground-service" in command_calls[0][0]
  assert "run-as" in command_calls[0][0]
  assert "com.jixiexiaoge.drivingassist.navdiag.BRIDGE_START" in command_calls[0][0]
  assert any("dumpsys" in args for args, _ in shell_calls)
  assert any("start-service" in args and
             any("BRIDGE_STOP" in value for value in args)
             for args, _ in cleanup_calls)


@pytest.mark.parametrize("failure", ["start_error", "never_live", "stop_error", "stop_still_registered"])
def test_navigation_bridge_fails_closed_with_fixed_diagnostics(failure):
  stop = threading.Event()
  clock = Clock()
  stop_returncode = 1 if failure == "stop_error" else 0
  stop_stdout = "Error: synthetic\n" if failure == "stop_error" else "Starting service:\n"
  live_lines = ["* ServiceRecord{bridge u0 com.jixiexiaoge.drivingassist.navdiag/.BridgeService}",
                "    isForeground=true"]
  stopped_lines = ["* ServiceRecord{bridge u0 com.jixiexiaoge.drivingassist.navdiag/.BridgeService}"]
  registered_lines = live_lines[:1]
  status_calls = []

  def status(*_args, **_kwargs):
    status_calls.append(1)
    if len(status_calls) == 1:
      lines = []
    elif failure == "never_live":
      lines = registered_lines
    elif failure == "stop_still_registered" and len(status_calls) >= 4:
      lines = stopped_lines
    elif len(status_calls) == 2:
      lines = live_lines
    else:
      lines = []
    clock.advance(100_000_000)
    return SimpleNamespace(returncode=0, stdout="\n".join(lines) + "\n")

  start_stdout = ("Error: synthetic\n" if failure == "start_error"
                  else "Starting service:\n")
  device = SimpleNamespace(
    adb="/fixed/adb", port=5039, serial="synthetic-phone",
    _command=lambda *_a, **_k: SimpleNamespace(returncode=0, stdout=start_stdout),
    shell=status,
    cleanup_shell=lambda *_a, **_k: SimpleNamespace(returncode=stop_returncode, stdout=stop_stdout),
  )
  bridge = producer._NavigationBroadcastBridge(device, stop, clock=clock)
  if failure in ("start_error", "never_live"):
    with pytest.raises(producer.PhoneRouteProducerRejected,
                       match="start_failed|start_not_registered"):
      with bridge:
        pass
  else:
    with bridge:
      pass
    if failure == "stop_error":
      assert not bridge.cleanup_verified
      assert bridge.cleanup_diagnostics == ("phone_bridge_stop_command_failed",)
    else:
      assert bridge.cleanup_verified


class NavigationBridgeRig:
  def __init__(self, monkeypatch):
    self.stop = threading.Event()
    self.clock = Clock()
    self.registered = self.live = False
    self.start_calls, self.cleanup_calls, self.resolve_calls = [], [], []
    self.start_results = []
    self.start_hook = None
    self.resolve_hook = None
    self.confirm_error = None
    self.stop_error = False
    self.resolved = producer._BRIDGE_SERVICE_COMPONENT + "\n"
    monkeypatch.setattr(producer.time, "sleep", lambda *_args: None)

  def _status(self):
    self.clock.advance(100_000_000)
    lines = (["* ServiceRecord{bridge u0 " + producer._BRIDGE_SERVICE_COMPONENT + "}"]
             if self.registered else [])
    if self.live:
      lines.append("    isForeground=true foregroundId=2")
    return SimpleNamespace(returncode=0, stdout="\n".join(lines))

  def _command(self, args, **_kwargs):
    self.start_calls.append(args)
    result = self.start_results.pop(0)
    if result is None:
      self.registered = True
      self.live = True
      result = SimpleNamespace(returncode=0, stdout="Starting service:\n", stderr="")
    if self.start_hook is not None:
      self.start_hook(self)
    if isinstance(result, BaseException):
      raise result
    return result

  def shell(self, *args, **_kwargs):
    if "query-services" in args:
      self.resolve_calls.append(args)
      if self.resolve_hook is not None:
        self.resolve_hook(self)
      return SimpleNamespace(returncode=0, stdout=self.resolved, stderr="")
    if self.start_calls and self.confirm_error is not None:
      raise self.confirm_error
    return self._status()

  def cleanup_shell(self, *args, **_kwargs):
    self.cleanup_calls.append(args)
    if "start-service" in args:
      if self.stop_error:
        return SimpleNamespace(returncode=1, stdout="", stderr="Error: synthetic")
      self.registered = self.live = False
      return SimpleNamespace(returncode=0, stdout="Starting service:\n", stderr="")
    return self._status()

  def bridge(self):
    device = SimpleNamespace(serial="synthetic-phone", _command=self._command,
                             shell=self.shell, cleanup_shell=self.cleanup_shell)
    return producer._NavigationBroadcastBridge(device, self.stop, clock=self.clock)


def cold_bridge_not_found(**changes):
  result = {"returncode": 255, "stdout": "Starting service:\n",
            "stderr": "Error: Not found; no service started.\n"}
  result.update(changes)
  return SimpleNamespace(**result)


def test_navigation_bridge_recovers_exact_cold_start_once_with_fixed_root_command(monkeypatch):
  import shlex
  rig = NavigationBridgeRig(monkeypatch)
  rig.start_results = [cold_bridge_not_found(), None]
  with rig.bridge() as bridge:
    assert bridge.started and not bridge.cleanup_verified
    assert len(rig.start_calls) == 2 and len(rig.resolve_calls) == 1
    first, recovered = rig.start_calls
    assert first[:5] == ["-s", "synthetic-phone", "shell", "run-as", producer.bridge_module.PACKAGE]
    assert recovered[:5] == ["-s", "synthetic-phone", "shell", "su", "-c"]
    root_command = shlex.split(recovered[5])
    assert len(root_command) == 1
    assert shlex.split(root_command[0]) == [
      "am", "start-foreground-service", "--user", "0", "-W",
      "-n", producer._BRIDGE_SERVICE_COMPONENT, "-a", producer._BRIDGE_START_ACTION]
  assert bridge.cleanup_verified and not rig.registered


@pytest.mark.parametrize("failure", [
  {"returncode": 1}, {"stderr": "Error: Permission Denial\n"},
  {"stderr": "Error: Not found; no service started.\nError: unrelated\n"},
  {"stderr": "Error: Not found; no service started.\n", "stdout": "Error: synthetic\n"},
])
def test_navigation_bridge_never_escalates_other_start_errors(monkeypatch, failure):
  rig = NavigationBridgeRig(monkeypatch)
  rig.start_results = [cold_bridge_not_found(**failure)]
  bridge = rig.bridge()
  with pytest.raises(producer.PhoneRouteProducerRejected, match="^phone_bridge_start_failed$"):
    bridge.__enter__()
  assert len(rig.start_calls) == 1 and not rig.resolve_calls
  assert bridge.cleanup_verified and not rig.registered


@pytest.mark.parametrize("resolved", ["", "unrelated/.BridgeService\n",
                                     producer._BRIDGE_SERVICE_COMPONENT + "\nunrelated/.Service\n"])
def test_navigation_bridge_requires_exact_resolution_before_root_recovery(monkeypatch, resolved):
  rig = NavigationBridgeRig(monkeypatch)
  rig.start_results = [cold_bridge_not_found()]
  rig.resolved = resolved
  bridge = rig.bridge()
  with pytest.raises(producer.PhoneRouteProducerRejected, match="^phone_bridge_start_failed$"):
    bridge.__enter__()
  assert len(rig.start_calls) == 1 and len(rig.resolve_calls) == 1
  assert bridge.cleanup_verified


def test_navigation_bridge_failed_root_recovery_is_not_repeated(monkeypatch):
  rig = NavigationBridgeRig(monkeypatch)
  rig.start_results = [cold_bridge_not_found(), cold_bridge_not_found()]
  bridge = rig.bridge()
  with pytest.raises(producer.PhoneRouteProducerRejected, match="^phone_bridge_start_failed$"):
    bridge.__enter__()
  assert len(rig.start_calls) == 2 and len(rig.resolve_calls) == 1
  assert bridge.cleanup_verified


def test_navigation_bridge_root_timeout_cleans_possible_remote_start(monkeypatch):
  rig = NavigationBridgeRig(monkeypatch)
  rig.start_results = [cold_bridge_not_found(), subprocess.TimeoutExpired("synthetic", 2)]

  def possible_start(state):
    if len(state.start_calls) == 2:
      state.registered = state.live = True

  rig.start_hook = possible_start
  bridge = rig.bridge()
  with pytest.raises(subprocess.TimeoutExpired):
    bridge.__enter__()
  assert len(rig.start_calls) == 2 and bridge.cleanup_verified and not rig.registered


def test_navigation_bridge_stop_during_resolution_does_not_start_root(monkeypatch):
  rig = NavigationBridgeRig(monkeypatch)
  rig.start_results = [cold_bridge_not_found()]
  rig.resolve_hook = lambda state: state.stop.set()
  bridge = rig.bridge()
  with pytest.raises(producer._Stopped):
    bridge.__enter__()
  assert len(rig.start_calls) == 1 and len(rig.resolve_calls) == 1
  assert bridge.cleanup_verified


@pytest.mark.parametrize("failure", ["stopped", "timeout", "status_error", "never_foreground"])
def test_navigation_bridge_partial_start_is_cleaned_when_enter_fails(monkeypatch, failure):
  rig = NavigationBridgeRig(monkeypatch)
  rig.start_results = [None]

  def interrupt(state):
    if failure == "stopped":
      state.stop.set()
    elif failure == "timeout":
      raise subprocess.TimeoutExpired("synthetic", 2)
    elif failure == "status_error":
      state.confirm_error = producer.PhoneRouteProducerRejected("phone_bridge_status_failed")
    else:
      state.live = False

  rig.start_hook = interrupt
  bridge = rig.bridge()
  with pytest.raises((producer._Stopped, subprocess.TimeoutExpired, producer.PhoneRouteProducerRejected)):
    bridge.__enter__()
  assert bridge.cleanup_verified and not bridge.started and not rig.registered
  assert any("start-service" in args for args in rig.cleanup_calls)


def test_navigation_bridge_stop_before_root_retry_does_not_escalate(monkeypatch):
  rig = NavigationBridgeRig(monkeypatch)
  rig.start_results = [cold_bridge_not_found()]
  rig.start_hook = lambda state: state.stop.set()
  bridge = rig.bridge()
  with pytest.raises(producer._Stopped):
    bridge.__enter__()
  assert len(rig.start_calls) == 1 and not rig.resolve_calls
  assert bridge.cleanup_verified


@pytest.mark.parametrize("live", [True, False])
def test_navigation_bridge_preserves_preexisting_service(monkeypatch, live):
  rig = NavigationBridgeRig(monkeypatch)
  rig.start_results = [None]
  rig.registered, rig.live = True, live
  bridge = rig.bridge()
  if live:
    with bridge:
      assert not bridge.started and bridge.cleanup_verified
  else:
    with pytest.raises(producer.PhoneRouteProducerRejected, match="^phone_bridge_already_registered$"):
      bridge.__enter__()
  assert not rig.start_calls and not rig.cleanup_calls and rig.registered


def test_navigation_bridge_failed_enter_preserves_unverified_cleanup(monkeypatch):
  rig = NavigationBridgeRig(monkeypatch)
  rig.start_results = [None]
  rig.start_hook = lambda state: state.stop.set()
  rig.stop_error = True
  bridge = rig.bridge()
  with pytest.raises(producer._Stopped):
    bridge.__enter__()
  assert not bridge.cleanup_verified
  assert bridge.cleanup_diagnostics == ("phone_bridge_stop_command_failed",)
  bridge.__exit__()
  assert not bridge.cleanup_verified


@pytest.mark.parametrize("change", [
  {"status": "unavailable", "reason": "process_epoch_changed"},
  {"process_epoch_hash": "0" * 64}, {"profile": "unsupported-new-profile"},
])
def test_alive_java_probe_with_epoch_or_profile_change_is_recreated_with_new_transport(change):
  state = ThreadRig()
  state.worker.start()
  try:
    first = wait_for(state.worker.latest)
    original = state.natives[0]
    original.changes = change
    assert original.process is not None
    second = wait_for(lambda: sample if (sample := state.worker.latest()) is not None
                      and sample.transport_session != first.transport_session else None)
    assert second.transport_session != first.transport_session and original.closed == 1
    assert state.collector.calls >= 2 and state.gpses[0].closed == 1
  finally:
    assert state.worker.close()


def test_boot_change_from_still_alive_gps_probe_forces_new_transport():
  state = ThreadRig()
  state.worker.start()
  try:
    first = wait_for(state.worker.latest)
    original = state.gpses[0]
    observe = original.observe
    def changed_boot():
      fix, raw, window = observe()
      return replace(fix, phone_boot_id="01234567-89ab-cdef-0123-456789abcdee"), raw, window
    original.observe = changed_boot
    second = wait_for(lambda: sample if (sample := state.worker.latest()) is not None
                      and sample.transport_session != first.transport_session else None)
    assert second.transport_session != first.transport_session and original.closed == 1
  finally:
    assert state.worker.close()


def test_source_failure_reason_is_preserved_when_cleanup_error_would_mask_it():
  state = ThreadRig()
  state.worker.start()
  try:
    wait_for(state.worker.latest)
    error = producer._Reconnect("phone_probe_cleanup_unverified")
    error.__context__ = producer.gps_module.GpsProbeRejected("gps_source_stale")
    state.natives[0].error = error
    wait_for(lambda: state.worker.status().last_error_chain)
    assert state.worker.status().last_error_chain == ("phone_probe_cleanup_unverified", "gps_source_stale")
  finally:
    assert state.worker.close()


@pytest.mark.parametrize("resolution", ["late_exit", "original_gone", "still_alive", "already_exited"])
def test_cleanup_exit_race_only_succeeds_with_confirmed_exit_or_gone_original_generation(monkeypatch, resolution):
  class Process:
    pid = 987654
    returncode = 0 if resolution == "already_exited" else None
    def __init__(self):
      self.stdin, self.stdout = io.BytesIO(), io.BytesIO()
      self.waits = []
    def poll(self):
      return self.returncode
    def wait(self, timeout):
      self.waits.append(timeout)
      if self.returncode is None:
        raise subprocess.TimeoutExpired("synthetic-root-channel", timeout)
      return self.returncode
  process = Process()
  cleanup_calls, killed = [], []
  def cleanup(*args, **kwargs):
    cleanup_calls.append((args, kwargs))
    if resolution == "late_exit":
      process.returncode = 0
    return SimpleNamespace(returncode=0 if len(cleanup_calls) == 2 and resolution == "original_gone" else 76)
  def kill(pid, sig):
    killed.append((pid, sig))
    process.returncode = -sig
  monkeypatch.setattr(producer.os, "killpg", kill)
  source = producer._InterruptibleSource.__new__(producer._InterruptibleSource)
  source._gps = True
  source.device = SimpleNamespace(cleanup_shell=cleanup, shell=lambda *a, **k: pytest.fail("不能绕过有界清理"))
  source.process, source._closed = process, False
  source._remote_identity = ("4444", "5555")
  source._token = "abc123"
  source.cleanup_verified = False
  if resolution == "still_alive":
    with pytest.raises(producer.PhoneRouteProducerRejected, match="cleanup_unverified"):
      source.close()
    assert not source.cleanup_verified
  else:
    source.close()
    assert source.cleanup_verified
  assert process.stdin.closed and process.stdout.closed
  assert all(timeout <= producer._ORDERLY_EXIT_TIMEOUT_S for timeout in process.waits)
  if resolution == "already_exited":
    assert not cleanup_calls and not killed
  else:
    first = cleanup_calls[0]
    assert first[1] == {"timeout": 0.6, "check": False}
    assert "NAVDIAG_GPS_SESSION=abc123" in first[0][1] and "4444" in first[0][1] and "5555" in first[0][1]
    if len(cleanup_calls) == 2:
      second = cleanup_calls[1]
      assert second[1] == {"timeout": 0.25, "check": False}
      assert "kill" not in second[0][1] and "5555" in second[0][1]
  source.close()


def test_cleanup_accepts_orderly_exit_inside_budget_without_diagnostics(monkeypatch):
  class Process:
    pid = 987654
    returncode = None
    def __init__(self):
      self.stdin, self.stdout = io.BytesIO(), io.BytesIO()
      self.waits = []
    def poll(self):
      return self.returncode
    def wait(self, timeout):
      self.waits.append(timeout)
      self.returncode = 0
      return self.returncode
  process = Process()
  cleanup_calls, killed = [], []
  monkeypatch.setattr(producer.os, "killpg", lambda pid, sig: killed.append((pid, sig)))
  source = producer._InterruptibleSource.__new__(producer._InterruptibleSource)
  source._gps = False
  source.device = SimpleNamespace(
    cleanup_shell=lambda *a, **k: cleanup_calls.append((a, k)),
    shell=lambda *a, **k: pytest.fail("有序正常退出不能调用清理"),
  )
  source.process, source._closed = process, False
  source._remote_identity = ("4444", "5555")
  source._token = "abc123"
  source.cleanup_verified = False
  source.cleanup_diagnostics = ()

  source.close()

  assert source.cleanup_verified
  assert source.cleanup_diagnostics == ()
  assert process.waits == [producer._ORDERLY_EXIT_TIMEOUT_S]
  assert not cleanup_calls and not killed
  assert process.stdin.closed and process.stdout.closed


def test_foreground_adb_waits_for_own_listener_then_device_wait_is_interruptible(monkeypatch):
  stop, launches, killed = threading.Event(), [], []
  connections = []
  class Socket:
    def __enter__(self):
      return self
    def __exit__(self, *_):
      pass
    def settimeout(self, _):
      pass
    def connect_ex(self, address):
      connections.append(address)
      return 111 if len(connections) <= 2 else 0
  class Process:
    pid = 765432
    returncode = None
    def poll(self):
      return self.returncode
    def wait(self, timeout):
      return self.returncode
  process = Process()
  def popen(*args, **kwargs):
    launches.append((args, kwargs))
    return process
  def kill(pid, sig):
    killed.append((pid, sig))
    process.returncode = -sig
  monkeypatch.setattr(producer.socket, "socket", Socket)
  monkeypatch.setattr(producer.subprocess, "Popen", popen)
  monkeypatch.setattr(producer.os, "killpg", kill)
  device = producer._InterruptibleDevice("/fixed/adb", 5039, None, stop)
  def host(*args, **kwargs):
    assert len(connections) >= 3
    stop.set()
    return SimpleNamespace(stdout="List of devices attached\nphone unauthorized\n", returncode=0)
  device.host = host
  with pytest.raises(producer._Stopped):
    device.__enter__()
  assert launches[0][0][0] == ["/fixed/adb", "-L", "tcp:localhost:5039", "server", "nodaemon"]
  assert killed == [(process.pid, signal.SIGTERM)] and device.cleanup_verified and device.server is None


def test_transient_native_snapshot_race_withdraws_but_keeps_same_connection_and_original_reason():
  state = ThreadRig()
  state.worker.start()
  try:
    first = wait_for(state.worker.latest)
    native = state.natives[0]
    native.changes = {"status": "unavailable", "reason": "selection_changed_during_read"}
    wait_for(lambda: state.worker.status().reason == "selection_changed_during_read")
    assert state.worker.latest() is first and state.worker._selection is None
    assert native.process is not None and len(state.devices) == 1
    native.changes = {}
    second = wait_for(lambda: sample if (sample := state.worker.latest()) is not None
                      and sample.sequence > first.sequence else None)
    assert second.transport_session == first.transport_session and second.sequence > first.sequence
  finally:
    assert state.worker.close()


def test_transient_native_snapshot_race_retries_immediately_without_interval_gap_or_resource_rebuild():
  state = ThreadRig()
  state.worker._stop = RecordingStopEvent()
  state.worker.start()
  try:
    first = wait_for(state.worker.latest)
    native = state.natives[0]

    def fail_once_then_recover(item):
      if not hasattr(item, "rejection_wait_count"):
        item.changes = {"status": "unavailable", "reason": "selection_changed_during_read"}
        item.rejection_wait_count = state.worker._stop.wait_count()
      else:
        item.recovery_wait_count = state.worker._stop.wait_count()
        item.changes = {}

    native.hook = fail_once_then_recover
    second = wait_for(lambda: sample if (sample := state.worker.latest()) is not None
                      and sample.sequence > first.sequence else None)

    assert native.recovery_wait_count == native.rejection_wait_count
    assert second.transport_session == first.transport_session
    assert second.sequence > first.sequence
    assert len(state.devices) == len(state.natives) == len(state.gpses) == 1
    assert all(item.closed == 0 for item in state.devices + state.natives + state.gpses)
    events = state.worker.drain_rejection_events()["events"]
    assert [event["reason"] for event in events] == ["selection_changed_during_read"]
  finally:
    assert state.worker.close()
  assert all(item.closed == 1 and item.cleanup_verified
             for item in state.devices + state.natives + state.gpses)


def test_transient_gps_cache_rejection_does_not_restart_transport_or_renew_old_sample():
  state = ThreadRig()
  state.worker.start()
  try:
    first = wait_for(state.worker.latest)
    gps = state.gpses[0]
    gps.error = producer.gps_module.GpsProbeRejected("gps_source_stale")
    wait_for(lambda: state.worker.status().reason == "gps_source_stale")
    assert state.worker.latest() is None and state.worker._selection is None and len(state.devices) == 1
    gps.error = None
    second = wait_for(state.worker.latest)
    assert second.transport_session == first.transport_session and second.sequence > first.sequence
  finally:
    assert state.worker.close()


def test_offroute_position_preserves_fixed_rejection_reason_without_forcing_reconnect():
  state = rig()
  state.native.changes = {"_position": {"status": "unavailable", "reason": "position_not_on_route"}}
  with pytest.raises(producer.PhoneRouteProducerRejected, match="^position_not_on_route$") as error:
    state.worker._take_sample(state.device, state.native, state.gps)
  assert not isinstance(error.value, producer._Reconnect) and state.collector.calls == 0


def test_production_native_source_retains_complete_transient_unavailable_protocol():
  from test_native_route_source import FakeProcess, unavailable
  value = unavailable("selection_changed_during_read")
  process = FakeProcess([json.dumps(value).encode() + b"\n"] * 2, None, False)
  source = producer._NativeSource.__new__(producer._NativeSource)
  source._stop, source._closed = threading.Event(), False
  source.process, source._remote_identity, source._token = process, None, "test-session"
  source.epoch = EPOCH
  source._last_phone_finished = None
  source.cleanup_verified = False
  try:
    for _ in range(2):
      assert source.observe()["reason"] == "selection_changed_during_read"
      assert source.process is process and not source._closed
  finally:
    source.close()
    process.dispose()
  assert source.cleanup_verified


@pytest.mark.parametrize("reason,cls", [
  ("gps_source_stale", producer.gps_module.GpsProbeRejected),
  ("gps_accuracy_unusable", producer.gps_module.GpsProbeRejected),
  ("gps_cache_empty", producer.gps_module.GpsProbeRejected),
])
def test_legacy_diagnostic_gps_probe_retains_complete_transient_cache_rejection_protocol(reason, cls):
  from test_native_route_source import FakeProcess
  value = {"schema_version": 1, "source": "android_fine_last_known_gps", "status": "unavailable", "reason": reason,
           **dict.fromkeys(producer.gps_module._FALSE_FLAGS, False)}
  process = FakeProcess([json.dumps(value).encode() + b"\n"] * 2, None, False)
  source = producer.gps_module.AndroidGpsProbeSource.__new__(producer.gps_module.AndroidGpsProbeSource)
  source._close_after_error = lambda _error: False
  source.device = SimpleNamespace(shell=lambda *args, **kwargs: SimpleNamespace(returncode=0))
  source._stop, source._closed = threading.Event(), False
  source.process, source._remote_identity, source._token = process, None, "test-session"
  source.cleanup_verified = False
  source._boot = source._last_phone_after = None
  try:
    for _ in range(2):
      with pytest.raises(cls, match="^" + reason + "$"):
        source.observe()
      assert source.process is process and not source._closed
  finally:
    source.close()
    process.dispose()
  assert source.cleanup_verified


class InactiveFileDevice(Resource):
  """Open a bounded read_file channel for exactly the whitelisted cache and record every read."""
  def __init__(self, path, data):
    super().__init__()
    self.path, self.data = path, data
    self.reads, self.fail_at = [], None

  def call_binary(self, *args, **kwargs):
    if self.fail_at is not None and len(self.reads) == self.fail_at:
      return SimpleNamespace(returncode=44, stdout=b"")
    assert self.path in args[-1] and "head -c" in args[-1]
    self.reads.append((args, kwargs))
    return SimpleNamespace(returncode=0, stdout=b"NAVDIAG_PROBE_V1\n" + self.data + b"\nNAVDIAG_PROBE_RC=000\n")


def reject(state, expected="selection_changed_during_read", changes=None):
  state.native.changes = changes or {"status": "unavailable", "reason": expected}
  try:
    with pytest.raises(producer.PhoneRouteProducerRejected, match="^" + expected + "$"):
      state.worker._take_sample(state.device, state.native, state.gps)
  finally:
    state.native.changes = {}


def test_whitelisted_rejection_moves_one_geometry_to_inactive_and_recovery_double_reads_same_file():
  state = rig()
  first = accept(state)
  reject(state)
  slot = state.worker._inactive
  # The last verified sample remains readable only for its original lease; the geometry
  # sources themselves are retired and only one inactive geometry stays whitelisted.
  assert state.worker._sample is first and state.worker.latest() is first
  assert state.worker._selection is None and state.worker._cache is None
  assert slot is not None and slot.cache_bytes is first.cache_bytes and slot.selected is first.selected
  assert (slot.epoch, slot.transport_session) == (state.native.epoch, "test-session")
  device = InactiveFileDevice(first.selected.source_path, first.cache_bytes)
  recovered = state.worker._take_sample(device, state.native, state.gps)
  assert recovered.cache_bytes == first.cache_bytes and recovered.selected is first.selected
  assert recovered.native_before and recovered.native_after and state.gps.last is not None
  assert state.worker._inactive is None and state.worker._cache is recovered.cache_bytes


def test_same_size_changed_bytes_reject_reuse_and_fall_back_to_full_collector():
  state = rig()
  first = accept(state)
  reject(state)
  changed = first.cache_bytes[:-1] + bytes([first.cache_bytes[-1] ^ 1])
  assert len(changed) == len(first.cache_bytes) and changed != first.cache_bytes
  device = InactiveFileDevice(first.selected.source_path, changed)
  second = state.worker._take_sample(device, state.native, state.gps)
  assert state.collector.calls == 2 and second.cache_bytes == first.cache_bytes
  assert state.worker._inactive is None and len(device.reads) == 2


def test_single_read_disagreement_also_falls_back_to_full_collector():
  state = rig()
  first = accept(state)
  reject(state)
  device = InactiveFileDevice(first.selected.source_path, first.cache_bytes)
  device.fail_at = 1
  assert state.worker._take_sample(device, state.native, state.gps).cache_bytes == first.cache_bytes
  assert state.collector.calls == 2
  assert len(device.reads) == 1 and state.worker._inactive is None


def test_inactive_ttl_is_five_seconds_and_repeated_rejection_does_not_renew_it():
  state = rig()
  first = accept(state)
  reject(state)
  saved = state.worker._inactive.saved_ns
  state.clock.advance(2_000_000_000)
  reject(state)
  assert state.worker._inactive.saved_ns == saved
  assert state.worker._inactive.cache_bytes == first.cache_bytes
  device = InactiveFileDevice(first.selected.source_path, first.cache_bytes)
  assert state.worker._take_sample(device, state.native, state.gps).cache_bytes == first.cache_bytes
  assert state.collector.calls == 1 and len(device.reads) == 2
  # Reject once more after a successful recovery: the TTL restarts at the new save
  # time and still cannot be extended by later rejections.
  reject(state)
  state.clock.advance(4_000_000_000)
  reject(state)
  assert state.worker._inactive is not None
  state.clock.advance(1_000_000_000)
  reject(state)
  assert state.worker._inactive is not None and state.collector.calls == 1
  device = InactiveFileDevice(first.selected.source_path, first.cache_bytes)
  assert state.worker._take_sample(device, state.native, state.gps).cache_bytes == first.cache_bytes
  assert state.collector.calls == 2 and not device.reads


def test_cancel_session_change_and_close_destroy_inactive_geometry():
  state = rig()
  accept(state)
  reject(state)
  assert state.worker._inactive is not None
  state.worker._withdraw("phone_producer_stopping")
  assert state.worker._inactive is None

  state = rig()
  accept(state)
  state.native.active = False
  state.native.changes = {}
  reject(state, "phone_navigation_not_active", {"gps_navigation_observed": False})
  assert state.worker._inactive is None

  state = rig()
  accept(state)
  reject(state)
  slot = state.worker._inactive
  state.worker._session = "other-session"
  device = InactiveFileDevice(slot.selected.source_path, slot.cache_bytes)
  state.worker._take_sample(device, state.native, state.gps)
  assert state.collector.calls == 2 and not device.reads and state.worker._inactive is None

  state = rig()
  accept(state)
  reject(state)
  slot = state.worker._inactive
  state.native.epoch = "999:1"
  device = InactiveFileDevice(slot.selected.source_path, slot.cache_bytes)
  with pytest.raises(producer.PhoneRouteProducerRejected, match="phone_selected_identity_changed|phone_provider_epoch_changed"):
    state.worker._take_sample(device, state.native, state.gps)
  assert not device.reads and state.worker._inactive is None


def test_collector_finishing_after_close_never_publishes_old_geometry():
  clock = Clock()
  release, entered, produced = threading.Event(), threading.Event(), []
  gate = {"open": False}

  def collect(device, native):
    raw = cache(ids=[native.path], routes=[route(native.path)])
    decoded = decode_cache(raw, profile=SUPPORTED_PROFILE)
    path = CACHE_DIRECTORY + "/123.dat"
    selected = SelectedCacheRoute(decoded.routes[0], 0, path, decoded.sha256, "b" * 64,
                                  identity_hash(native.path, native.epoch), 1, 2)
    produced.append(raw)
    if gate["open"]:
      entered.set()
      release.wait(1.0)
    return selected, {}, Files({path: raw})

  worker = producer.PhoneRouteProducer(collector=collect, clock_ns=clock, interval_s=0.01)
  native, gps, device = Native(clock), Gps(clock), Resource()
  worker._session = "closing-session"
  gate["open"] = True
  outcome = {}

  def call():
    try:
      outcome["sample"] = worker._take_sample(device, native, gps)
    except BaseException as error:
      outcome["error"] = error

  thread = threading.Thread(target=call, daemon=True)
  thread.start()
  assert entered.wait(1.0)
  worker._stop.set()
  worker._withdraw("phone_producer_stopping")
  with worker._lock:
    worker._closed = True
  release.set()
  thread.join(1.0)
  assert not thread.is_alive() and produced
  # The in-flight collector already produced old geometry, but after withdraw/close it
  # may neither be written back nor published.
  assert isinstance(outcome.get("error"), producer._Stopped) and "sample" not in outcome
  assert worker._sample is None and worker._inactive is None and worker._selection is None
  assert worker._cache is None and worker.status().sequence == 0


class SlowInactiveFileDevice(InactiveFileDevice):
  """Advance the fake clock on every bounded read so recovery reads drag past the inactive TTL."""
  def __init__(self, path, data, clock, step_ns):
    super().__init__(path, data)
    self.clock, self.step_ns = clock, step_ns

  def call_binary(self, *args, **kwargs):
    result = super().call_binary(*args, **kwargs)
    self.clock.advance(self.step_ns)
    return result


def test_cancel_then_whitelisted_rejection_cannot_rebuild_stale_inactive_slot():
  state = rig()
  accept(state)
  assert state.worker._retained is not None
  # A plain withdraw/cancel must also retire the candidate source of old geometry and
  # leave no slot that could be rebuilt.
  state.worker._withdraw("phone_producer_stopping")
  assert state.worker._retained is None and state.worker._inactive is None
  reject(state)
  assert state.worker._inactive is None and state.worker._retained is None


def test_latest_expiry_clears_every_stale_reference_and_blocks_inactive_revive():
  state = rig()
  accept(state)
  state.clock.advance(2_000_000_000)
  assert state.worker.latest() is None
  assert (state.worker._sample is None and state.worker._retained is None and state.worker._inactive is None)
  assert state.worker._selection is None and state.worker._cache is None and state.worker._selection_key is None
  reject(state)
  assert state.worker._inactive is None and state.worker._retained is None


def test_recovery_reads_crossing_ttl_fall_back_to_full_collector():
  state = rig()
  first = accept(state)
  reject(state)
  slot = state.worker._inactive
  assert slot is not None
  # Still inside the TTL when recovery starts, but the two reads drag it past 5 s: the
  # expired bytes must not be reused afterwards.
  device = SlowInactiveFileDevice(first.selected.source_path, first.cache_bytes, state.clock, 3_000_000_000)
  recovered = state.worker._take_sample(device, state.native, state.gps)
  assert state.collector.calls == 2 and len(device.reads) == 2
  assert recovered.cache_bytes == first.cache_bytes and state.worker._inactive is None


def _blocked_collector(clock, entered, release):
  def collect(device, native):
    raw = cache(ids=[native.path], routes=[route(native.path)])
    decoded = decode_cache(raw, profile=SUPPORTED_PROFILE)
    path = CACHE_DIRECTORY + "/123.dat"
    entered.set()
    release.wait(5.0)
    selected = SelectedCacheRoute(decoded.routes[0], 0, path, decoded.sha256, "b" * 64,
                                  identity_hash(native.path, native.epoch), 1, 2)
    return selected, {}, Files({path: raw})
  return collect


def test_withdraw_during_blocked_collector_never_writes_back_old_geometry():
  clock, entered, release = Clock(), threading.Event(), threading.Event()
  worker = producer.PhoneRouteProducer(collector=_blocked_collector(clock, entered, release),
                                       clock_ns=clock, interval_s=0.01)
  worker._session = "withdraw-session"
  native, gps, device = Native(clock), Gps(clock), Resource()
  outcome = {}

  def call():
    try:
      outcome["sample"] = worker._take_sample(device, native, gps)
    except BaseException as error:
      outcome["error"] = error

  thread = threading.Thread(target=call, daemon=True)
  thread.start()
  assert entered.wait(1.0)
  # A real withdraw (not close) must also invalidate the in-flight round's generation.
  worker._withdraw("phone_producer_stopping")
  release.set()
  thread.join(1.0)
  assert not thread.is_alive()
  assert isinstance(outcome.get("error"), producer._Stopped) and "sample" not in outcome
  assert (worker._selection is None and worker._cache is None and worker._selection_key is None
          and worker._retained is None and worker._inactive is None)


def test_publish_rejects_sample_when_withdraw_happens_between_sample_and_publish():
  state = rig()
  generation = state.worker._active()
  sample = state.worker._take_sample(state.device, state.native, state.gps)
  # Same session and same stop flag, but a real withdraw advanced the generation: no publish.
  state.worker._withdraw("phone_producer_stopping")
  assert state.worker._publish(sample, generation) is False
  assert state.worker._sample is None and state.worker._retained is None
  # The explicit token is the only channel: the pre-withdraw token is rejected, and only
  # a fresh sample of the newer generation may publish.
  state.worker._generation += 1
  fresh = state.worker._take_sample(state.device, state.native, state.gps)
  assert state.worker._publish(sample, generation) is False
  assert state.worker._publish(fresh, state.worker._active()) is True


def test_status_reports_last_published_sequence_during_transient_refresh():
  state = rig()
  first = state.worker._take_sample(state.device, state.native, state.gps)
  assert state.worker._publish(first, state.worker._active())
  assert state.worker.status().sequence == first.sequence
  assert state.worker.status().transport_session == first.transport_session

  generation = state.worker._active()
  pending = state.worker._take_sample(state.device, state.native, state.gps)
  assert pending.sequence > first.sequence
  reject(state)
  status = state.worker.status()
  assert status.reason == "selection_changed_during_read"
  assert status.sequence == first.sequence
  assert status.transport_session == first.transport_session
  assert state.worker.latest() is first
  assert state.worker._publish(pending, generation) is False


def test_late_rejection_from_old_generation_cannot_rebuild_or_clear_new_data():
  state = rig()
  accept(state)
  stale_generation = state.worker._active()
  state.worker._withdraw("phone_producer_stopping")
  fresh = accept(state)
  assert state.worker._retained is not None
  # A late whitelisted rejection from an old generation neither clears new data nor
  # creates a new inactive slot.
  state.worker._on_rejection(producer.PhoneRouteProducerRejected("selection_changed_during_read"),
                             state.worker._sequence, stale_generation)
  assert state.worker._inactive is None
  assert state.worker._retained is not None and state.worker._cache == fresh.cache_bytes
  assert state.worker._generation == state.worker._generation


class HookLock:
  """One-shot hook lock: run an action after a given release count instead of racing with sleep."""
  def __init__(self, lock, after_release=None, fire_at=1):
    self._lock, self._after, self._fire_at = lock, after_release, fire_at
    self.releases = 0

  def __enter__(self):
    self._lock.acquire()
    return self

  def __exit__(self, *args):
    self._lock.release()
    self.releases += 1
    if self._after is not None and self.releases == self._fire_at:
      action, self._after = self._after, None
      action()


def test_remember_inactive_cannot_rebuild_after_withdraw_between_its_two_locks():
  state = rig()
  first = accept(state)
  generation = state.worker._active()
  # The old code read current under lock A, compared outside it, then took a new
  # generation under lock B; here a real withdraw is injected right after lock A releases.
  state.worker._lock = HookLock(state.worker._lock,
                                lambda: state.worker._withdraw("phone_producer_stopping"))
  state.worker._on_rejection(producer.PhoneRouteProducerRejected("selection_changed_during_read"), 1, generation)
  assert state.worker._inactive is None and state.worker._retained is None and state.worker._cache is None
  assert first.cache_bytes is not None


def test_recover_inactive_with_stale_token_never_performs_or_writes_back_reads():
  state = rig()
  first = accept(state)
  reject(state)
  slot = state.worker._inactive
  assert slot is not None
  device = InactiveFileDevice(first.selected.source_path, first.cache_bytes)
  stale = state.worker._active() - 1
  assert state.worker._recover_inactive(slot, device, state.native, slot.selected.identity_hash,
                                        stale) is None
  assert not device.reads and state.worker._cache is None


def test_reconnect_from_old_generation_cannot_clear_newer_accepted_data():
  state = rig()
  accept(state)
  stale = state.worker._active() - 1
  fresh_cache, fresh_retained = state.worker._cache, state.worker._retained
  assert fresh_cache is not None and fresh_retained is not None
  # A disconnect retire carrying an old generation must be refused and leave newer data untouched.
  assert state.worker._withdraw("phone_provider_epoch_changed", expected_generation=stale) is False
  assert state.worker._cache is fresh_cache and state.worker._retained is fresh_retained
  # The same failure through _take_sample must not clear new data either; drive the
  # _Reconnect branch with the old token.
  def boom(device, native, gps, generation):
    raise producer._Reconnect("phone_provider_epoch_changed")
  original, state.worker._sample_once = state.worker._sample_once, boom
  try:
    with pytest.raises(producer._Reconnect):
      state.worker._take_sample(state.device, state.native, state.gps, stale)
  finally:
    state.worker._sample_once = original
  assert state.worker._cache is fresh_cache and state.worker._retained is fresh_retained


@pytest.mark.parametrize("reason", ["selection_changed_during_read", "phone_navigation_not_active"])
def test_rejection_advances_generation_so_original_sample_cannot_be_published(reason):
  state = rig()
  generation = state.worker._active()
  sample = state.worker._take_sample(state.device, state.native, state.gps)
  assert state.worker._publish(sample, generation)
  generation = state.worker._active()
  pending = state.worker._take_sample(state.device, state.native, state.gps)
  changes = ({"gps_navigation_observed": False} if reason == "phone_navigation_not_active"
             else {"status": "unavailable", "reason": reason})
  # reject() already asserts the rejection with pytest.raises; only the resulting
  # generation and publish behaviour matter here.
  reject(state, reason, changes)
  # Every real rejection must advance the generation under the lock, or an already
  # taken sample of the same generation could still be published.
  assert state.worker._active() != generation
  assert state.worker._publish(pending, generation) is False
  if reason == "selection_changed_during_read":
    assert state.worker._sample is not None and state.worker._sample.sequence == sample.sequence
  else:
    assert state.worker._sample is None


def test_old_round_cannot_take_or_clear_newer_inactive_slot_before_native_returns():
  state = rig()
  first = accept(state)
  stale = state.worker._active()
  slot = producer._InactiveGeometry(first.selected, first.cache_bytes, state.native.epoch,
                                    state.worker._session, state.clock(), 1)

  def install_new_slot(native):
    native.hook = None
    state.worker._withdraw("phone_producer_stopping")
    with state.worker._lock:
      state.worker._inactive = slot
      state.worker._retained = (first.selected, first.cache_bytes)

  state.native.hook = install_new_slot
  # The old round is really withdrawn before native returns, and the newer generation
  # has already installed its own inactive slot.
  with pytest.raises(producer._Stopped):
    state.worker._take_sample(state.device, state.native, state.gps, stale)
  assert state.worker._inactive is slot and state.worker._retained is not None


def test_withdraw_during_recovery_double_read_never_writes_back_or_collects():
  state = rig()
  first = accept(state)
  reject(state)
  slot = state.worker._inactive
  assert slot is not None

  class WithdrawingDevice(InactiveFileDevice):
    """Really withdraw right after the first bounded read, returning to the post-read check in _recover_inactive."""
    def call_binary(self, *args, **kwargs):
      result = super().call_binary(*args, **kwargs)
      if len(self.reads) == 1:
        state.worker._withdraw("phone_producer_stopping")
      return result

  device = WithdrawingDevice(first.selected.source_path, first.cache_bytes)
  with pytest.raises(producer._Stopped):
    state.worker._take_sample(device, state.native, state.gps)
  # Withdrawn mid-read: nothing may be written back and it must not degrade into a full
  # collector scan.
  assert state.collector.calls == 1
  assert state.worker._cache is None and state.worker._selection is None
