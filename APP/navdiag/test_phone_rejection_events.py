from collections import deque

import pytest

from APP.navdiag import phone_route_producer as producer
from APP.navdiag.test_phone_route_producer import Native, ThreadRig, accept, rig


class ScriptedRig(ThreadRig):
  def __init__(self, script):
    self.script = deque(script)
    super().__init__()

  def native(self, device, stop):
    script = self.script

    class ScriptedNative(Native):
      def observe_position(self):
        if not script:
          stop.set()
          raise producer._Stopped()
        value = script.popleft()
        if callable(value):
          value = value()
        if isinstance(value, BaseException):
          raise value
        return value

    item = ScriptedNative(self.clock)
    self.natives.append(item)
    return item


def unavailable(reason):
  return {"status": "unavailable", "reason": reason}


def run_script(script):
  state = ScriptedRig(script)
  state.worker.start()
  try:
    state.worker._thread.join(2.0)
    assert not state.worker._thread.is_alive()
  finally:
    assert state.worker.close()
  assert all(item.cleanup_verified for item in state.devices + state.natives + state.gpses)
  return state


def test_inner_source_rejections_are_all_queued_without_any_status_poll():
  state = run_script([unavailable("selection_changed_during_read"),
                      unavailable("position_not_on_route"), unavailable("selection_changed_during_read")])
  batch = state.worker.drain_rejection_events()
  assert [event["event_id"] for event in batch["events"]] == [1, 2, 3]
  assert [event["reason"] for event in batch["events"]] == [
    "selection_changed_during_read", "position_not_on_route", "selection_changed_during_read"]
  assert batch["dropped_count"] == 0 and len(state.devices) == 1
  assert not any(event["stale_generation"] for event in batch["events"])
  assert state.worker.drain_rejection_events() == {"events": [], "dropped_count": 0}


def test_reconnect_is_recorded_once_across_inner_and_outer_handlers():
  state = run_script([unavailable("process_epoch_changed"), unavailable("selection_changed_during_read")])
  events = state.worker.drain_rejection_events()["events"]
  assert [event["reason"] for event in events] == ["process_epoch_changed", "selection_changed_during_read"]
  assert [event["event_id"] for event in events] == [1, 2]
  assert len(state.devices) == 2


def test_same_exception_instance_in_separate_attempts_is_not_deduplicated():
  error = ValueError("selection_changed_during_read")
  error.diagnosis = {"origin": "synthetic_native_compare", "changed": ["position.tick_time_raw"]}
  state = run_script([error, error, error])
  events = state.worker.drain_rejection_events()["events"]
  assert len(events) == 3
  assert [event["event_id"] for event in events] == [1, 2, 3]
  error.diagnosis["changed"].append("path_id")
  assert all(event["diagnosis"]["changed"] == ["position.tick_time_raw"] for event in events)


def test_plain_error_clears_diagnosis_from_previous_attempt():
  first, last = ValueError("selection_changed_during_read"), ValueError("selection_changed_during_read")
  first.diagnosis, last.diagnosis = {"stage": "first"}, {"stage": "last"}
  state = run_script([first, ValueError("gps_source_stale"), last])
  events = state.worker.drain_rejection_events()["events"]
  assert [event["diagnosis"] for event in events] == [{"stage": "first"}, None, {"stage": "last"}]
  assert [event["diagnosis_present"] for event in events] == [True, False, True]


def test_non_value_error_from_native_is_recorded_by_outer_handler():
  state = run_script([OSError("private device error"), unavailable("selection_changed_during_read")])
  events = state.worker.drain_rejection_events()["events"]
  assert [event["reason"] for event in events] == ["phone_producer_source_failed", "selection_changed_during_read"]
  assert len(state.devices) == 2
  assert "private device error" not in repr(events)


def test_close_racing_with_rejection_records_original_generation_without_reviving_data():
  state = rig()
  accept(state)
  generation = state.worker._active()
  state.native.changes = unavailable("selection_changed_during_read")
  state.native.hook = lambda _native: state.worker.close()
  with pytest.raises(producer.PhoneRouteProducerRejected, match="selection_changed_during_read"):
    state.worker._take_sample(state.device, state.native, state.gps, generation)
  events = state.worker.drain_rejection_events()["events"]
  assert len(events) == 1 and events[0]["generation"] == generation
  assert events[0]["state_generation"] > generation and events[0]["stale_generation"] is True
  assert state.worker.latest() is None and state.worker._inactive is None and state.worker._retained is None
  assert state.worker.status().reason == "phone_producer_stopped"


def test_stop_control_flow_is_not_recorded_as_a_source_rejection():
  state = run_script([])
  assert state.worker.drain_rejection_events() == {"events": [], "dropped_count": 0}


def test_real_rejection_path_queue_is_bounded_and_reports_overflow():
  state = rig()
  state.native.changes = unavailable("position_not_on_route")
  for _ in range(producer.REJECTION_QUEUE_MAX + 5):
    with pytest.raises(producer.PhoneRouteProducerRejected, match="position_not_on_route"):
      state.worker._take_sample(state.device, state.native, state.gps)
  batch = state.worker.drain_rejection_events()
  assert len(batch["events"]) == producer.REJECTION_QUEUE_MAX
  assert batch["dropped_count"] == 5 and batch["events"][0]["event_id"] == 6


def test_status_diagnosis_cannot_mutate_queued_event():
  state = rig()
  error = ValueError("selection_changed_during_read")
  error.diagnosis = {"changed": ["position.tick_time_raw"]}
  state.native.error = error
  with pytest.raises(ValueError, match="selection_changed_during_read"):
    state.worker._take_sample(state.device, state.native, state.gps)
  status = state.worker.status()
  status.rejection_diagnosis["changed"].append("path_id")
  status.rejection["diagnosis"]["changed"].append("path_count")
  event, = state.worker.drain_rejection_events()["events"]
  assert event["diagnosis"]["changed"] == ["position.tick_time_raw"]


def test_native_pair_diagnosis_and_its_real_time_windows_reach_queue():
  state = rig()
  diagnosis = {"schema_version": 1, "source": "native_observation_pair",
               "changed_fields": ["position"], "position_changed_fields": ["tick_time_raw"],
               "position_serialization_changed": True}
  state.native.changes = {**unavailable("selection_changed_during_read"), "_selection_diagnosis": diagnosis,
                          "observation_clock_domain": "phone_elapsed_realtime",
                          "observation_started_monotonic_ns": 10,
                          "observation_finished_monotonic_ns": 20}
  with pytest.raises(producer.PhoneRouteProducerRejected, match="selection_changed_during_read"):
    state.worker._take_sample(state.device, state.native, state.gps)
  event, = state.worker.drain_rejection_events()["events"]
  captured = event["diagnosis"]
  assert captured["source"] == "native_observation_pair"
  assert captured["changed_fields"] == ["position"]
  assert captured["position_changed_fields"] == ["tick_time_raw"]
  assert captured["observation_window"] == {
    "clock": "phone_elapsed_realtime", "started_ns": 10, "finished_ns": 20,
    "host_clock": "host_monotonic", "host_started_ns": 10_001_000_000, "host_finished_ns": 10_002_000_000}
  diagnosis["position_changed_fields"].append("point_index")
  assert captured["position_changed_fields"] == ["tick_time_raw"]
