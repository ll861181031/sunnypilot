"""Failed retry evidence across producer and actual logging, without phone access."""
from copy import deepcopy
import json

import pytest

from APP.navdiag import native_route_source as native
from APP.navdiag import phone_route_producer as producer
from APP.navdiag.test_native_route_source import EPOCH, navi_retry_diagnosis, selection_diagnosis, selection_unavailable, snapshot_retry
from APP.navdiag.test_phone_rejection_events import run_script
from openpilot.cereal import log
from openpilot.selfdrive.navigation import navd
from openpilot.selfdrive.navigation.tests import test_phone_rejection_logging as logging_test


def _failed_observation(reason):
  value = selection_unavailable(reason)
  value["_snapshot_retry"] = snapshot_retry(recovered=False)
  if reason == "selection_changed_during_read":
    value["_selection_diagnosis"] = selection_diagnosis(position_changed_fields=["input_tick_time_raw"])
  source = object.__new__(native.NativeRouteProbeSource)
  source.epoch, source._last_phone_finished = EPOCH, None
  source._validate(value, include_position=True)
  value.update(host_observation_clock_domain="host_monotonic", host_observation_started_monotonic_ns=10_001_000_000,
               host_observation_finished_monotonic_ns=10_002_000_000)
  return value


@pytest.mark.parametrize("reason", ["selection_changed_during_read", "read_failed", "short_read", "process_epoch_changed",
                                   "process_epoch_failed", "snapshot_retry_deadline_exceeded"])
def test_failed_retry_keeps_initial_evidence_separate_from_final_diagnosis(reason):
  value = _failed_observation(reason)
  state = run_script([value])
  event, = state.worker.drain_rejection_events()["events"]
  assert event["reason"] == reason and event["snapshot_retry_present"] is True
  assert event["snapshot_retry"] == value["_snapshot_retry"]
  assert event["snapshot_retry"]["initial_diagnosis"]["position_changed_fields"] == ["tick_time_raw"]
  if reason == "selection_changed_during_read":
    assert event["diagnosis"]["position_changed_fields"] == ["input_tick_time_raw"]
    assert event["diagnosis"]["observation_window"]["host_started_ns"] == 10_001_000_000
  else:
    assert event["diagnosis"] is None and event["diagnosis_present"] is False
  value["_snapshot_retry"]["initial_diagnosis"]["position_changed_fields"].clear()
  assert event["snapshot_retry"]["initial_diagnosis"]["position_changed_fields"] == ["tick_time_raw"]
  assert len(json.dumps(event)) < 2000


def test_missing_retry_clears_prior_evidence_and_status_cannot_mutate_queue():
  state = run_script([_failed_observation("read_failed"), {"status": "unavailable", "reason": "position_missing"},
                      _failed_observation("snapshot_retry_deadline_exceeded")])
  status = state.worker.status()
  status.rejection["snapshot_retry"]["initial_diagnosis"]["position_changed_fields"].clear()
  events = state.worker.drain_rejection_events()["events"]
  assert [item["snapshot_retry_present"] for item in events] == [True, False, True]
  assert events[1]["snapshot_retry"] is None
  assert all(item["snapshot_retry"]["initial_diagnosis"]["position_changed_fields"] == ["tick_time_raw"]
             for item in (events[0], events[2]))


@pytest.mark.parametrize("reason", ["selection_changed_during_read", "read_failed", "snapshot_retry_deadline_exceeded"])
def test_failed_v2_retry_reaches_rejection_queue_with_original_reason(reason):
  value = selection_unavailable(reason)
  value["_navi_info"] = {"schema_version": 2, "status": "unavailable", "reason": "snapshot_unavailable",
                         "diagnostic_only": True, "control_input_allowed": False, "route_render_input_allowed": False}
  value["_snapshot_retry"] = snapshot_retry(recovered=False, initial_diagnosis=navi_retry_diagnosis())
  if reason == "selection_changed_during_read":
    value["_selection_diagnosis"] = navi_retry_diagnosis(
      changed_fields=["navi_info"], position_changed_fields=[], position_serialization_changed=False,
      navi_info_changed_fields=["branches"], navi_info_serialization_changed=True)
  source = object.__new__(native.NativeRouteProbeSource)
  source.epoch, source._last_phone_finished = EPOCH, None
  source._validate(value, include_position=True, include_navi_info=True)
  state = run_script([value])
  event, = state.worker.drain_rejection_events()["events"]
  assert event["reason"] == reason and event["snapshot_retry_present"] is True
  assert event["snapshot_retry"] == value["_snapshot_retry"]
  if reason == "selection_changed_during_read":
    assert event["diagnosis"]["navi_info_changed_fields"] == ["branches"]
  assert "native_st_pos_deg" not in json.dumps(event)


@pytest.mark.parametrize("reason", ["selection_changed_during_read", "process_epoch_changed"])
@pytest.mark.parametrize("bad_retry", [None, {}, {"coordinates": [121.0, 31.0]},
                                       snapshot_retry(recovered=True),
                                       snapshot_retry(recovered=False, initial_diagnosis=selection_diagnosis(
                                         position_changed_fields=["input_tick_time_raw"]))])
def test_invalid_optional_retry_does_not_change_original_rejection_or_leak(reason, bad_retry):
  value = _failed_observation(reason)
  value["_snapshot_retry"] = deepcopy(bad_retry)
  with pytest.raises(producer.PhoneRouteProducerRejected, match="^" + reason + "$") as caught:
    producer._native_identity(value, EPOCH)
  assert isinstance(caught.value, producer._Reconnect) == (reason == "process_epoch_changed")
  state = run_script([value])
  event, = state.worker.drain_rejection_events()["events"]
  assert event["snapshot_retry"] is None and event["snapshot_retry_present"] is False
  assert event["reason"] == reason and "coordinates" not in json.dumps(event)


def test_new_fields_reach_navd_and_older_events_stay_compatible(monkeypatch):
  state = run_script([_failed_observation("snapshot_retry_deadline_exceeded")])
  event, = state.worker.drain_rejection_events()["events"]
  old_event = logging_test.rejection(2)
  recorded = []
  monkeypatch.setattr(navd.cloudlog, "event", lambda name, **fields: recorded.append({"event": name, **fields}))
  navd.drain_phone_rejections(logging_test.QueueSource([event, old_event]))
  assert recorded[0]["snapshot_retry"] == event["snapshot_retry"]
  assert recorded[0]["snapshot_retry_present"] is True
  assert "snapshot_retry" not in recorded[1] and "snapshot_retry_present" not in recorded[1]


def test_production_log_preserves_failed_retry_evidence_in_real_rlog(tmp_path, monkeypatch):
  state = run_script([_failed_observation("selection_changed_during_read"), _failed_observation("process_epoch_changed")])
  events = state.worker.drain_rejection_events()["events"]
  assert len(events) == 2
  monkeypatch.setattr(logging_test, "rejection", lambda event_id: deepcopy(events[event_id - 1]))
  original_read = logging_test.read_log
  captured = []

  def read_and_capture(*args, **kwargs):
    result = original_read(*args, **kwargs)
    for item in log.Event.read_multiple_bytes(result[0]):
      if item.which() == "logMessage":
        body = json.loads(item.logMessage).get("msg", {})
        if isinstance(body, dict) and body.get("event") == "navd.phone_source_rejection":
          captured.append(body)
    return result

  monkeypatch.setattr(logging_test, "read_log", read_and_capture)
  logging_test.test_production_rejection_crosses_swaglog_logmessage_and_native_logger(tmp_path, monkeypatch)
  assert len(captured) == 2
  assert [item["snapshot_retry"] for item in captured] == [item["snapshot_retry"] for item in events]
  assert all(item["snapshot_retry_present"] is True for item in captured)
  assert captured[0]["diagnosis"]["position_changed_fields"] == ["input_tick_time_raw"]
  assert captured[1]["diagnosis"] is None and captured[1]["reason"] == "process_epoch_changed"
