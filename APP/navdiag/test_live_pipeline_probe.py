from __future__ import annotations

import json
from pathlib import Path
import sys
import types

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from APP.navdiag import live_pipeline_probe as probe


def test_anchor_diagnostics_reports_exact_bounded_indices_without_coordinates():
  before = {"segment_index": 3, "link_index": 4, "point_index": 8}
  after = {"segment_index": 3, "link_index": 4, "point_index": 9}
  sample = types.SimpleNamespace(native_before={"_position": {"status": "observed", **before}},
                                 native_after={"_position": {"status": "observed", **after}})
  result = probe.sample_position_diagnostics(sample)
  assert result["source_anchors"] == {"native_before": before, "native_after": after,
                                      "changed_fields": ["point_index"]}
  assert not any(key.endswith("_deg") for key in result["source_anchors"])


@pytest.mark.parametrize("bad", [True, False, -1, 2 ** 31, 0.5, "1", None, {}, []])
def test_anchor_diagnostics_never_coerces_invalid_indices(bad):
  sample = types.SimpleNamespace(native_before={"_position": {"status": "observed", "segment_index": bad,
    "link_index": 0, "point_index": 0}}, native_after=None)
  result = probe.sample_position_diagnostics(sample)["source_anchors"]
  assert result["native_before"] == {"segment_index": "unknown", "link_index": 0, "point_index": 0}
  assert result["native_after"] == dict.fromkeys(("segment_index", "link_index", "point_index"), "unknown")
  assert result["changed_fields"] == "unknown"


def anchor_retry_evidence():
  return {"schema_version": 1, "source": "phone_native_gps_native", "extra_pair_observations": 1,
          "recovered": True, "original_host_start_ns": 10, "original_host_finished_ns": 30,
          "recovery_host_start_ns": 20, "recovery_host_finished_ns": 40,
          "anchor_before": {"segment_index": 0, "link_index": 0, "point_index": 0},
          "anchor_after": {"segment_index": 0, "link_index": 0, "point_index": 1}}


def test_pair_retry_diagnostics_preserves_only_bounded_host_evidence():
  value = anchor_retry_evidence()
  sample = types.SimpleNamespace(anchor_pair_retry=value)
  assert probe.anchor_pair_retry_diagnostics(sample) == value
  assert probe.anchor_pair_retry_diagnostics(None) is None


@pytest.mark.parametrize("changes", [
  {"schema_version": True}, {"extra_pair_observations": 2}, {"extra_pair_observations": True},
  {"source": "native_observation_pair"}, {"recovered": False}, {"recovered": 1},
  {"original_host_start_ns": True}, {"original_host_start_ns": 21},
  {"recovery_host_start_ns": 31}, {"recovery_host_finished_ns": 29},
  {"recovery_host_finished_ns": 2_000_000_010}, {"private_coordinates": [10.0, 20.0]},
  {"anchor_before": {"segment_index": 0, "link_index": 0, "point_index": True}},
  {"anchor_after": {"segment_index": 0, "link_index": 0, "point_index": 0}},
])
def test_pair_retry_diagnostics_rejects_unbounded_or_malformed_evidence(changes):
  sample = types.SimpleNamespace(anchor_pair_retry={**anchor_retry_evidence(), **changes})
  assert probe.anchor_pair_retry_diagnostics(sample) is None


def test_duration_bounds_reject_out_of_range(tmp_path, capsys):
  for bad in (14, 181, 0, -5):
    with pytest.raises(SystemExit):
      probe.main(["--out", str(tmp_path / f"x{bad}"), "--duration", str(bad)])
  assert not list(tmp_path.iterdir())


def test_duration_bounds_accept_edges(tmp_path, monkeypatch):
  seen = {}

  class FakeBench:
    def __init__(self):
      self.prefix = "navcheck_fake"
    def __enter__(self): return self
    def __exit__(self, *_): seen["bench_closed"] = True
    def step(self, sample):
      import numpy as np
      return ({"nav_map": np.zeros((1, 1, 256, 256), np.uint8),
               "navigation": np.zeros((1, 47), np.float32)},
              {"source_reason": "phone_source_unavailable", "map_valid": False,
               "instruction_valid": False, "reset_requested": False,
               "runtime": {"mapReason": "absent"}})

  class FakeProducer:
    def __init__(self, *a, **k): self.observations = {}
    def start(self):
      return self
    def latest(self): return None
    def status(self):
      return types.SimpleNamespace(running=True, reason="phone_source_unavailable", sequence=0,
                                   transport_session=None, connected=False,
                                   cleanup_verified=True, cleanup_diagnostics=())
    def close(self):
      seen["producer_closed"] = True
      return True
    def summary(self): return {"collector_calls": 0, "recover_hits": 0}

  monkeypatch.setattr(probe, "ObservedProducer", FakeProducer)
  monkeypatch.setattr(probe, "PhonePipelineBench", FakeBench)
  monkeypatch.setattr(probe, "SAMPLE_INTERVAL_S", 0)
  fake_clock = iter([0.0, 0.0] + [float(i) for i in range(1, 40)])
  monkeypatch.setattr(probe.time, "monotonic", lambda: next(fake_clock, 99.0))
  try:
    probe.main(["--out", str(tmp_path / "edge"), "--duration", "15"])
  except StopIteration:
    pass
  assert seen.get("producer_closed") and seen.get("bench_closed")


def test_report_written_on_exception_and_both_closed(tmp_path, monkeypatch):
  seen = {}

  class BoomBench:
    def __init__(self): self.prefix = "navcheck_boom"
    def __enter__(self): return self
    def __exit__(self, *_): seen["bench"] = True
    def step(self, sample): raise RuntimeError("boom_after_start")

  class FakeProducer:
    def __init__(self, *a, **k): pass
    def start(self):
      seen["started"] = True
      return self
    def latest(self): return None
    def status(self):
      return types.SimpleNamespace(running=False, reason="phone_producer_stopped", sequence=0,
                                   transport_session=None, connected=False,
                                   cleanup_verified=True, cleanup_diagnostics=())
    def close(self):
      seen["producer"] = True
      return True
    def summary(self): return {"collector_calls": 3, "recover_hits": 0}

  monkeypatch.setattr(probe, "ObservedProducer", FakeProducer)
  monkeypatch.setattr(probe, "PhonePipelineBench", BoomBench)
  out = tmp_path / "boom"
  code = probe.main(["--out", str(out), "--duration", "15"])
  assert code == 1
  assert seen == {"started": True, "bench": True, "producer": True}
  report = json.loads((out / "report.json").read_text())
  assert "boom_after_start" in report["error"]
  assert report["close"]["cleanup_verified"] is True
  assert (out / "frames.jsonl").exists()


def test_intervals_separate_startup_from_recovery():
  records = [
    {"map_valid": False, "since_start_ms": 0.0},
    {"map_valid": False, "since_start_ms": 500.0},
    {"map_valid": True, "since_start_ms": 2618.9},
    {"map_valid": True, "since_start_ms": 5000.0},
    {"map_valid": False, "since_start_ms": 20208.0},
    {"map_valid": False, "since_start_ms": 21000.0},
    {"map_valid": True, "since_start_ms": 22625.0},
  ]
  result = probe.intervals(records)
  assert result["first_available_ms"] == 2618.9
  assert result["startup_phase_ms"] == 2618.9
  assert len(result["recovery_gaps"]) == 1
  assert result["recovery_gaps"][0]["duration_ms"] == pytest.approx(2417.0)
  assert result["unrecovered_loss_ms"] is None


def test_intervals_marks_open_loss():
  result = probe.intervals([{"map_valid": True, "since_start_ms": 10.0},
                            {"map_valid": False, "since_start_ms": 20.0}])
  assert result["unrecovered_loss_ms"] == 20.0
  assert result["recovery_gaps"] == []


def test_transport_lifecycle_requires_loss_and_new_session_recovery():
  records = [
    {"map_valid": True, "since_start_ms": 10.0, "sequence": 1,
     "transport_session_hash": "a" * 64, "selected_command": 9},
    {"map_valid": False, "since_start_ms": 20.0, "sequence": None,
     "transport_session_hash": None, "selected_command": -1},
    {"map_valid": False, "since_start_ms": 30.0, "sequence": None,
     "transport_session_hash": None, "selected_command": -1},
    {"map_valid": True, "since_start_ms": 40.0, "sequence": 2,
     "transport_session_hash": "b" * 64, "selected_command": 9},
  ]
  assert probe.transport_lifecycle(records) == {
    "status": "recovered",
    "reason": "new_transport_session_with_valid_sequence",
    "initial_session_hash": "a" * 64,
    "recovered_session_hash": "b" * 64,
    "loss_started_ms": 20.0,
    "recovered_ms": 40.0,
    "loss_duration_ms": 20.0,
    "stale_command_after_loss": "absent",
  }


def test_transport_lifecycle_accepts_new_session_sequence_reset():
  records = [
    {"map_valid": True, "since_start_ms": 10.0, "sequence": 31,
     "transport_session_hash": "a" * 64, "selected_command": 9},
    {"map_valid": False, "since_start_ms": 20.0, "sequence": None,
     "transport_session_hash": None, "selected_command": -1},
    {"map_valid": True, "since_start_ms": 30.0, "sequence": 1,
     "transport_session_hash": "b" * 64, "selected_command": 9},
  ]
  result = probe.transport_lifecycle(records)
  assert result["status"] == "recovered"
  assert result["reason"] == "new_transport_session_with_valid_sequence"
  assert result["initial_session_hash"] == "a" * 64
  assert result["recovered_session_hash"] == "b" * 64


def test_transport_lifecycle_rejects_startup_or_incomplete_session_transition():
  records = [
    {"map_valid": True, "since_start_ms": 1.0, "sequence": None,
     "transport_session_hash": None, "selected_command": 9},
    {"map_valid": False, "since_start_ms": 2.0, "sequence": None,
     "transport_session_hash": None, "selected_command": -1},
    {"map_valid": True, "since_start_ms": 3.0, "sequence": 1,
     "transport_session_hash": "b" * 64, "selected_command": 9},
  ]
  result = probe.transport_lifecycle(records)
  assert result["status"] == "not_observed"
  assert result["reason"] == "initial_observation_incomplete"


def test_transport_lifecycle_rejects_same_session_or_invalid_sequence_recovery():
  base = {"map_valid": True, "since_start_ms": 10.0, "sequence": 7,
          "transport_session_hash": "a" * 64, "selected_command": 9}
  loss = {"map_valid": False, "since_start_ms": 20.0, "sequence": None,
          "transport_session_hash": None, "selected_command": -1}
  same = base | {"since_start_ms": 30.0, "sequence": 8}
  invalid_new = base | {"since_start_ms": 40.0, "sequence": -1,
                        "transport_session_hash": "b" * 64}
  for recovered in (same, invalid_new):
    result = probe.transport_lifecycle([base, loss, recovered])
    assert result["status"] == "unrecovered"
    assert result["reason"] == "new_session_not_recovered"


def test_transport_lifecycle_records_stale_command_during_loss():
  records = [
    {"map_valid": True, "since_start_ms": 10.0, "sequence": 1,
     "transport_session_hash": "a" * 64, "selected_command": 9},
    {"map_valid": False, "since_start_ms": 20.0, "sequence": None,
     "transport_session_hash": None, "selected_command": 9},
  ]
  result = probe.transport_lifecycle(records)
  assert result["status"] == "unrecovered"
  assert result["stale_command_after_loss"] == "present"


def test_frame_record_has_no_coordinates():
  import numpy as np
  sample = types.SimpleNamespace(sequence=7, transport_session="sess-a",
                                 gps=types.SimpleNamespace(source_token="tok-1", age_upper_ns=1000,
                                                           host_finished_ns=0, horizontal_accuracy_m=1.2),
                                 selected=types.SimpleNamespace(cache_sha256="a" * 64, route=None),
                                 broadcast={
                                   "status": "observed", "reason": "fresh_10001_observation",
                                   "bridge_transport_valid": True, "receiver_active": True,
                                   "host_read_started_ns": 10, "host_read_finished_ns": 20,
                                   "records": {"10001": {
                                     "sequence": 119, "fresh": True, "replayed": True,
                                     "source_age_upper_ns": 750_000_000,
                                   }},
                                 })
  navigation = np.zeros((1, 47), np.float32)
  navigation[0, 15] = 1.0
  navigation[0, -1] = 0.42
  inputs = {"nav_map": np.ones((1, 1, 256, 256), np.uint8),
            "navigation": navigation}
  details = {"source_reason": "accepted", "map_valid": True, "instruction_valid": True,
             "reset_requested": False, "adapter_instruction_reason": "accepted", "runtime": {
               "mapReason": "accepted", "instructionReason": "accepted", "selectedCommand": 15,
               "selectedManeuverIndex": 0, "rawDistance": 50.0, "adjustedDistance": 42.0,
               "traveledDistance": 8.0}}
  record = probe.frame_record(0, 12.5, sample, details, inputs)
  text = json.dumps(record)
  assert "latitude" not in text and "longitude" not in text
  assert record["map_nonzero"] is True and record["map_pixels_nonzero"] == 256 * 256
  assert record["gps_source_token"] == "tok-1"
  assert record["transport_session_hash"] and len(record["transport_session_hash"]) == 64
  assert record["instruction_reason"] == "accepted"
  assert record["adapter_instruction_reason"] == "accepted"
  assert record["selected_command"] == 15
  assert record["navigation_nonzero_commands"] == [15]
  assert record["navigation_distance_fraction"] == pytest.approx(0.42)
  assert record["broadcast_diagnostics"] == {
    "status": "observed", "reason": "fresh_10001_observation",
    "bridge_transport_valid": True, "receiver_active": True,
    "record_fresh": True, "record_replayed": True, "record_sequence": 119,
    "source_age_upper_ns": 750_000_000,
    "host_read_window": {"started": 10, "finished": 20},
  }


def _navi_info_observation():
  binding = {"status": "verified", "reason": "segment_object_verified",
             "segment_object_verified": True, "icon_id_getter_vtable_verified": True,
             "icon_getter_vtable_verified": True}
  branch = {"maneuver_id_raw": 14, "maneuver_segment_raw": 2, "maneuver_link_raw": 3, "maneuver_point_raw": 4,
            "exit_number_raw": 2, "exit_angle_raw": 90, "obtain_supported_raw": 1, "data_changed_raw": 0,
            "segment_remain_time_raw": 40, "segment_remain_distance_raw": 500,
            "next_road_name_segment_raw": 5, "next_road_name_link_raw": 6, "split_raw": 0,
            "segment_binding": binding}
  return {
    "path_identity_hash": "a" * 64,
    "_navi_info": {
      "schema_version": 2, "status": "observed", "reason": "unverified_navi_info_semantics",
      "diagnostic_only": True, "control_input_allowed": False, "route_render_input_allowed": False,
      "source": "native_navi_info", "path_identity_hash": "a" * 64,
      "route_remain_time_raw": 100, "route_remain_distance_raw": 1000, "navigation_info_type_raw": 0,
      "item_count": 3, "item_index": 1, "current_segment": 2, "current_link": 3, "current_point": 4,
      "branch_flag_raw": 1, "branches": [branch, dict(branch, maneuver_id_raw=15)],
    },
  }


def test_navi_info_shadow_is_bounded_and_never_copies_private_fields():
  sample = types.SimpleNamespace(native_before=_navi_info_observation(),
                                 native_after=_navi_info_observation())
  result = probe.native_navi_info_shadow(sample, "native_after")
  assert result["status"] == "observed" and result["path_identity_hash"] == "a" * 64
  assert result["control_input_allowed"] is False and result["route_render_input_allowed"] is False
  assert result["branches"][1]["maneuver_id_raw"] == 15
  text = json.dumps(result)
  assert "latitude" not in text and "raw_address" not in text


def test_navi_info_shadow_retains_quarantined_raw_fields_without_certifying_actions():
  observation = _navi_info_observation()
  navi = observation["_navi_info"]
  navi["reason"] = "navi_info_next_segment_invalid"
  navi["branches"][1]["next_road_name_segment_raw"] = 0xfffffffe
  result = probe.native_navi_info_shadow(types.SimpleNamespace(native_after=observation), "native_after")
  assert result["status"] == "observed"
  assert result["action_semantics_reason"] == "navi_info_next_segment_invalid"
  assert result["field_rejections"] == [{"branch_index": 1, "field": "next_road_name_segment_raw",
                                        "reason": "navi_info_next_segment_invalid",
                                        "raw_u32": 4294967294, "raw_i32": -2}]
  assert result["branches"][0]["segment_binding"]["status"] == "verified"
  assert result["control_input_allowed"] is False


def test_navi_info_shadow_rejects_false_quarantine_without_invalid_raw():
  observation = _navi_info_observation()
  observation["_navi_info"]["reason"] = "navi_info_next_segment_invalid"
  result = probe.native_navi_info_shadow(types.SimpleNamespace(native_after=observation), "native_after")
  assert result["status"] == "unavailable"


def test_navi_info_shadow_accepts_frozen_branch_tuples():
  observation = _navi_info_observation()
  observation["_navi_info"]["branches"] = tuple(observation["_navi_info"]["branches"])
  result = probe.native_navi_info_shadow(
    types.SimpleNamespace(native_after=observation), "native_after")
  assert result["status"] == "observed"
  assert isinstance(result["branches"], list)
  assert result["control_input_allowed"] is False


def _unavailable_navi_info(reason):
  return {"schema_version": 2, "status": "unavailable", "reason": reason,
          "diagnostic_only": True, "control_input_allowed": False, "route_render_input_allowed": False}


@pytest.mark.parametrize("reason", ["navi_info_path_match_missing", "navi_info_count_zero"])
def test_navi_info_shadow_preserves_valid_unavailable_reason(reason):
  sample = types.SimpleNamespace(native_after={"_navi_info": _unavailable_navi_info(reason)})
  assert probe.native_navi_info_shadow(sample, "native_after") == {"status": "unavailable", "reason": reason}


@pytest.mark.parametrize("extra", [{"branches": []}, {"path_identity_hash": "a" * 64}, {"source": "native_navi_info"}])
def test_unavailable_navi_info_shadow_never_exposes_partial_payload(extra):
  sample = types.SimpleNamespace(native_after={"_navi_info": {**_unavailable_navi_info("navi_info_count_zero"), **extra}})
  assert probe.native_navi_info_shadow(sample, "native_after") == {
    "status": "unavailable", "reason": "missing_or_malformed_observation"}


@pytest.mark.parametrize("reason", ["private=121.1", None, "x" * 81])
def test_unavailable_navi_info_shadow_sanitizes_non_reason_text(reason):
  sample = types.SimpleNamespace(native_after={"_navi_info": _unavailable_navi_info(reason)})
  assert probe.native_navi_info_shadow(sample, "native_after") == {"status": "unavailable", "reason": "unknown_reason"}


@pytest.mark.parametrize("change", [{"diagnostic_only": False}, {"control_input_allowed": True},
                                  {"route_render_input_allowed": True}])
def test_unavailable_navi_info_shadow_keeps_permission_guards(change):
  sample = types.SimpleNamespace(native_after={"_navi_info": {**_unavailable_navi_info("navi_info_count_zero"), **change}})
  assert probe.native_navi_info_shadow(sample, "native_after") == {
    "status": "unavailable", "reason": "unsafe_observation_contract"}


def test_navi_info_shadow_rejects_unreviewed_private_fields():
  observation = _navi_info_observation()
  observation["_navi_info"].update(latitude=22.5, raw_address=140000000000)
  result = probe.native_navi_info_shadow(
    types.SimpleNamespace(native_after=observation), "native_after")
  assert result == {"status": "unavailable", "reason": "missing_or_malformed_observation"}


@pytest.mark.parametrize("change", [
  {"control_input_allowed": True}, {"route_render_input_allowed": True},
  {"diagnostic_only": False}, {"path_identity_hash": "b" * 64},
  {"item_count": -1}, {"branch_flag_raw": 2}, {"branches": []},
])
def test_navi_info_shadow_fails_closed_on_unsafe_or_malformed_data(change):
  observation = _navi_info_observation()
  observation["_navi_info"].update(change)
  result = probe.native_navi_info_shadow(types.SimpleNamespace(native_after=observation), "native_after")
  assert result["status"] == "unavailable"
  assert "branches" not in result


def test_frame_record_logs_action_shadow_without_coordinates():
  import numpy as np
  sample = types.SimpleNamespace(sequence=8, transport_session="sess-b",
                                 gps=types.SimpleNamespace(source_token="tok-2", age_upper_ns=1000,
                                                           host_finished_ns=0, horizontal_accuracy_m=1.2),
                                 selected=types.SimpleNamespace(cache_sha256="a" * 64, route=None),
                                 **_navi_info_observation(), native_after=_navi_info_observation())
  sample.native_before = _navi_info_observation()
  inputs = {"nav_map": np.zeros((1, 1, 256, 256), np.uint8),
            "navigation": np.zeros((1, 47), np.float32)}
  details = {"source_reason": "accepted", "map_valid": True, "instruction_valid": False,
             "reset_requested": False, "runtime": {"mapReason": "accepted"}}
  record = probe.frame_record(8, 100.0, sample, details, inputs)
  shadow = record["navi_info_shadow"]
  assert shadow["native_before"]["status"] == shadow["native_after"]["status"] == "observed"
  assert shadow["native_after"]["branches"][0]["exit_angle_raw"] == 90
  assert "latitude" not in json.dumps(record) and "raw_address" not in json.dumps(record)


def test_frame_record_logs_bounded_gps_callback_diagnostics_without_coordinates():
  import time
  import numpy as np
  live = {"registration": "request_accepted", "gps_registration": "request_accepted",
          "passive_registration": "request_accepted", "callback_count": 7,
          "gps_callback_count": 3, "passive_callback_count": 4, "cache_read_count": 9,
          "callbacks_during_observation": 7, "observation_elapsed_ms": 120,
          "callback_age_ms": 120, "last_read_age_ms": 130, "callback_thread_alive": True}
  sample = types.SimpleNamespace(sequence=8, transport_session="sess-live",
                                 gps=types.SimpleNamespace(
                                   source_token="tok-live", age_upper_ns=1000,
                                   host_finished_ns=time.monotonic_ns(),
                                   horizontal_accuracy_m=1.2, live_source=live),
                                 selected=None, **_navi_info_observation(),
                                 native_after=_navi_info_observation())
  sample.native_before = _navi_info_observation()
  inputs = {"nav_map": np.zeros((1, 1, 256, 256), np.uint8),
            "navigation": np.zeros((1, 47), np.float32)}
  details = {"source_reason": "accepted", "map_valid": True, "instruction_valid": False,
             "reset_requested": False, "runtime": {"mapReason": "accepted"}}
  record = probe.frame_record(8, 100.0, sample, details, inputs)
  assert record["gps_live_source"] == live
  assert "latitude" not in json.dumps(record) and "longitude" not in json.dumps(record)


def test_observed_producer_wraps_collector_without_changing_result():
  sentinel = object()
  calls = []

  def base(device, native):
    calls.append(1)
    return sentinel

  class Probe(probe.ObservedProducer):
    def __init__(self):
      super().__init__(collector=base, port=5039)

  instance = Probe()
  assert instance._collector(None, None) is sentinel
  assert calls == [1]
  assert instance.summary()["collector_calls"] == 1
  assert instance.summary()["recover_calls"] == 0


def test_actions_alignment_requires_monotonic_s(tmp_path):
  actions = tmp_path / "actions.jsonl"
  actions.write_text(
    json.dumps({"action": "navigate_start", "monotonic_s": 102.5}) + "\n"
    + json.dumps({"action": "navigate_cancel", "timestamp": 1700000000.0}) + "\n",
    encoding="utf-8")
  result = probe.align_actions(actions, 100.0)
  assert result["present"] is True
  assert result["aligned"] is False and result["unaligned_entries"] == 1
  assert result["entries"][0]["since_start_ms"] == 2500.0
  assert result["entries"][1]["since_start_ms"] is None
  assert result["entries"][1]["aligned"] is False


def test_actions_absent_is_not_fabricated(tmp_path):
  result = probe.align_actions(tmp_path / "missing.jsonl", 100.0)
  assert result == {"present": False, "aligned": None, "entries": None}


def test_gps_counts_separates_time_accuracy_and_usability():
  base = {"map_valid": True, "gps_age_upper_ms": 100.0, "gps_accuracy_m": 1.0}
  records = [
    dict(base),
    dict(base, gps_age_upper_ms=2500.0),
    dict(base, gps_accuracy_m=9.0),
    dict(base, gps_age_upper_ms=None, gps_accuracy_m=None),
    dict(base, map_valid=False),
  ]
  counts = probe.gps_counts(records)
  assert counts["fresh"] == 3 and counts["stale"] == 1 and counts["unknown"] == 1
  assert counts["invalid"] == 0
  assert counts["schema_version"] == 3
  assert counts["accuracy_strict"] == 3
  assert counts["accuracy_degraded_map"] == 1
  assert counts["accuracy_rejected"] == 0
  assert counts["accuracy_qualified"] == 4
  assert counts["accuracy_unqualified"] == 0
  assert counts["accuracy_unknown"] == 1
  assert counts["accuracy_invalid"] == 0
  assert counts["usable"] == 3 and counts["unusable"] == 1
  assert counts["usability_unknown"] == 1 and counts["usability_invalid"] == 0
  assert counts["fresh_but_map_rejected"] == 1
  assert counts["stale_with_valid_map"] == 1
  assert counts["usable_but_map_rejected"] == 1
  assert counts["unusable_with_valid_map"] == 1


@pytest.mark.parametrize(("age", "state"), [
  (0, "fresh"), (1999.999, "fresh"), (2000, "stale"), (2000.001, "stale"),
  (10 ** 400, "stale"), (None, "unknown"),
  (-0.001, "invalid"), (float("nan"), "invalid"), (float("inf"), "invalid"),
  (float("-inf"), "invalid"), (True, "invalid"), (False, "invalid"),
  ("100", "invalid"), ([], "invalid"), ({}, "invalid"),
])
def test_gps_counts_age_boundaries_and_invalid_values(age, state):
  counts = probe.gps_counts([{"gps_age_upper_ms": age, "gps_accuracy_m": 1.0, "map_valid": False}])
  assert counts["fresh"] == (state == "fresh")
  assert counts["stale"] == (state == "stale")
  assert counts["unknown"] == (state == "unknown")
  assert counts["invalid"] == (state == "invalid")
  assert counts["accuracy_qualified"] == 1
  expected = {"fresh": "usable", "stale": "unusable", "unknown": "usability_unknown", "invalid": "usability_invalid"}[state]
  assert counts[expected] == 1
  assert counts["fresh_but_map_rejected"] == (state == "fresh")
  assert counts["usable_but_map_rejected"] == (state == "fresh")


@pytest.mark.parametrize(("accuracy", "state"), [
  (0.00001, "strict"), (1, "strict"), (5.0, "strict"),
  (5.00001, "degraded_map"), (15, "degraded_map"), (25, "degraded_map"),
  (25.001, "rejected"), (10 ** 400, "rejected"), (None, "unknown"),
  (0, "invalid"), (-1, "invalid"), (float("nan"), "invalid"),
  (float("inf"), "invalid"), (float("-inf"), "invalid"),
  (True, "invalid"), (False, "invalid"), ("1.0", "invalid"), ([], "invalid"), ({}, "invalid"),
])
def test_gps_counts_quality_does_not_change_time_status(accuracy, state):
  counts = probe.gps_counts([{"gps_age_upper_ms": 100.0, "gps_accuracy_m": accuracy, "map_valid": False}])
  assert counts["fresh"] == 1 and counts["stale"] == 0
  assert counts["unknown"] == 0 and counts["invalid"] == 0
  assert counts["accuracy_" + state] == 1
  qualified = state in ("strict", "degraded_map")
  assert counts["accuracy_qualified" if qualified else "accuracy_unqualified"] == (0 if state in ("unknown", "invalid") else 1)
  expected = ("usable" if qualified else "usability_unknown" if state == "unknown"
              else "usability_invalid" if state == "invalid" else "unusable")
  assert counts[expected] == 1
  assert counts["fresh_but_map_rejected"] == 1
  assert counts["usable_but_map_rejected"] == qualified


@pytest.mark.parametrize("fields", [["tick_time_raw"], ["tick_time_raw", "input_tick_time_raw"],
                                    ["native_st_pos_deg", "tick_time_raw"],
                                    ["native_st_pos_deg", "tick_time_raw", "input_tick_time_raw"],
                                    ["native_st_pos_deg", "matched_st_pos_deg", "tick_time_raw"],
                                    ["native_st_pos_deg", "matched_st_pos_deg", "tick_time_raw", "input_tick_time_raw"]])
def test_native_retry_preserves_v2_unchanged_action_evidence(fields):
  initial = {"schema_version": 2, "source": "native_observation_pair",
             "changed_fields": ["position"], "position_changed_fields": fields,
             "position_serialization_changed": True, "navi_info_changed_fields": [],
             "navi_info_serialization_changed": False}
  retry = {"schema_version": 1, "source": "native_observation_pair", "extra_observations": 1,
           "recovered": True, "initial_diagnosis": initial}
  sample = types.SimpleNamespace(native_before={"_snapshot_retry": retry}, native_after=None)
  assert probe.native_snapshot_retries(sample) == [{"side": "native_before", **retry}]
  initial["position_changed_fields"] = ["native_st_pos_deg"]
  assert probe.native_snapshot_retries(sample) == []
  initial["position_changed_fields"] = ["tick_time_raw"]
  initial["navi_info_serialization_changed"] = True
  assert probe.native_snapshot_retries(sample) == []


def test_native_retry_accepts_only_bounded_double_tick_recovery():
  initial = {"schema_version": 1, "source": "native_observation_pair",
             "changed_fields": ["position"], "position_changed_fields": ["tick_time_raw", "input_tick_time_raw"],
             "position_serialization_changed": True}
  retry = {"schema_version": 1, "source": "native_observation_pair", "extra_observations": 1,
           "recovered": True, "initial_diagnosis": initial}
  sample = types.SimpleNamespace(native_before=None, native_after={"_snapshot_retry": retry})
  assert probe.native_snapshot_retries(sample) == [{
    "side": "native_after", "schema_version": 1, "source": "native_observation_pair",
    "extra_observations": 1, "recovered": True, "initial_diagnosis": dict(initial)}]
  retry["initial_diagnosis"]["position_changed_fields"].append("native_st_pos_deg")
  assert probe.native_snapshot_retries(sample) == []


@pytest.mark.parametrize(("record", "usability"), [
  ({}, "usability_unknown"),
  ({"gps_accuracy_m": 1.0}, "usability_unknown"),
  ({"gps_age_upper_ms": 100.0}, "usability_unknown"),
  ({"gps_age_upper_ms": 2000.0}, "unusable"),
  ({"gps_accuracy_m": 6.0}, "usability_unknown"),
  ({"gps_age_upper_ms": -1, "gps_accuracy_m": 6.0}, "usability_invalid"),
  ({"gps_age_upper_ms": 2000, "gps_accuracy_m": True}, "usability_invalid"),
  ({"gps_age_upper_ms": None, "gps_accuracy_m": float("inf")}, "usability_invalid"),
])
def test_gps_counts_missing_and_invalid_dimensions(record, usability):
  counts = probe.gps_counts([dict(record, map_valid=True)])
  assert counts[usability] == 1
  assert sum(counts[key] for key in ("fresh", "stale", "unknown", "invalid")) == 1
  assert sum(counts["accuracy_" + key] for key in ("qualified", "unqualified", "unknown", "invalid")) == 1
  assert sum(counts[key] for key in ("usable", "unusable", "usability_unknown", "usability_invalid")) == 1
  assert counts["unusable_with_valid_map"] == (usability == "unusable")


def test_gps_counts_keeps_empty_partitions_and_does_not_mutate_records():
  empty = probe.gps_counts([])
  assert empty["schema_version"] == 3
  assert all(value == 0 for key, value in empty.items() if key != "schema_version")
  record = {"gps_age_upper_ms": 100, "gps_accuracy_m": 9, "map_valid": False}
  original = dict(record)
  counts = probe.gps_counts([record])
  assert record == original
  assert counts["fresh_but_map_rejected"] == 1
  assert counts["usable_but_map_rejected"] == 1


def test_bench_enter_failure_exits_only_once(tmp_path, monkeypatch):
  exits = []

  class FailingBench:
    def __init__(self): self.prefix = "navcheck_fail"
    def __enter__(self):
      self.__exit__(None, None, None)
      raise RuntimeError("bench_enter_failed")
    def __exit__(self, *_):
      exits.append(1)
      return False
    def step(self, sample):
      raise AssertionError("step must not run")

  class FakeProducer:
    def __init__(self, *a, **k): pass
    def start(self): return self
    def latest(self): return None
    def status(self):
      return types.SimpleNamespace(running=False, reason="phone_producer_stopped", sequence=0,
                                   transport_session=None, connected=False,
                                   cleanup_verified=True, cleanup_diagnostics=())
    def close(self): return True
    def summary(self): return {"collector_calls": 0, "recover_hits": 0}

  monkeypatch.setattr(probe, "ObservedProducer", FakeProducer)
  monkeypatch.setattr(probe, "PhonePipelineBench", FailingBench)
  out = tmp_path / "enterfail"
  code = probe.main(["--out", str(out), "--duration", "15"])
  assert code != 0
  assert len(exits) == 1, exits
  report = json.loads((out / "report.json").read_text())
  assert report["bench_entered"] is False
  assert "bench_enter_failed" in report["error"]


def test_close_false_returns_nonzero_and_marks_unverified(tmp_path, monkeypatch):
  class OkBench:
    def __init__(self): self.prefix = "navcheck_ok"
    def __enter__(self): return self
    def __exit__(self, *_): return False
    def step(self, sample):
      import numpy as np
      return ({"nav_map": np.zeros((1, 1, 256, 256), np.uint8),
               "navigation": np.zeros((1, 47), np.float32)},
              {"source_reason": "phone_source_unavailable", "map_valid": False,
               "instruction_valid": False, "reset_requested": False,
               "runtime": {"mapReason": "absent"}})

  class BadProducer:
    def __init__(self, *a, **k): pass
    def start(self): return self
    def latest(self): return None
    def status(self):
      return types.SimpleNamespace(running=True, reason="phone_producer_stopping", sequence=0,
                                   transport_session=None, connected=False,
                                   cleanup_verified=False, cleanup_diagnostics=("x",))
    def close(self): return False
    def summary(self): return {}

  monkeypatch.setattr(probe, "ObservedProducer", BadProducer)
  monkeypatch.setattr(probe, "PhonePipelineBench", OkBench)
  monkeypatch.setattr(probe, "SAMPLE_INTERVAL_S", 0)
  out = tmp_path / "badclose"
  code = probe.main(["--out", str(out), "--duration", "15"])
  assert code == 1
  report = json.loads((out / "report.json").read_text())
  assert report["cleanup_verified"] is False
  assert report["close"]["producer_thread_stopped"] is False


def test_timing_fields_and_duration_covers_init(tmp_path, monkeypatch):
  class OkBench:
    def __init__(self): self.prefix = "navcheck_time"
    def __enter__(self): return self
    def __exit__(self, *_): return False
    def step(self, sample):
      import numpy as np
      return ({"nav_map": np.zeros((1, 1, 256, 256), np.uint8),
               "navigation": np.zeros((1, 47), np.float32)},
              {"source_reason": "phone_source_unavailable", "map_valid": False,
               "instruction_valid": False, "reset_requested": False,
               "runtime": {"mapReason": "absent"}})

  class FakeProducer:
    def __init__(self, *a, **k): pass
    def start(self): return self
    def latest(self): return None
    def status(self):
      return types.SimpleNamespace(running=False, reason="phone_producer_stopped", sequence=0,
                                   transport_session=None, connected=False,
                                   cleanup_verified=True, cleanup_diagnostics=())
    def close(self): return True
    def summary(self): return {"collector_calls": 0, "recover_hits": 0}

  monkeypatch.setattr(probe, "ObservedProducer", FakeProducer)
  monkeypatch.setattr(probe, "PhonePipelineBench", OkBench)
  monkeypatch.setattr(probe, "SAMPLE_INTERVAL_S", 0)
  ticks = iter([100.0 + i * 0.1 for i in range(400)])
  monkeypatch.setattr(probe.time, "monotonic", lambda: next(ticks, 999.0))
  out = tmp_path / "timing"
  probe.main(["--out", str(out), "--duration", "15"])
  report = json.loads((out / "report.json").read_text())
  for key in ("started_monotonic_s", "wall_clock_anchor_s", "init_ms", "sampling_window_s",
              "cleanup_ms", "total_wall_s"):
    assert key in report, key
  assert report["sampling_window_s"] <= 15.0
  assert report["sampling_started_since_start_ms"] >= report["init_ms"]


def test_attach_transport_is_recorded_as_not_owned(tmp_path, monkeypatch):
  class OkBench:
    def __init__(self): self.prefix = "navcheck_attach"
    def __enter__(self): return self
    def __exit__(self, *_): pass
    def step(self, _sample):
      import numpy as np
      return ({"nav_map": np.zeros((1, 1, 256, 256), np.uint8),
               "navigation": np.zeros((1, 47), np.float32)},
              {"source_reason": "phone_source_unavailable", "map_valid": False,
               "instruction_valid": False, "reset_requested": False,
               "runtime": {"mapReason": "absent"}})

  seen = {}
  class AttachProducer:
    connected_adb_ownership = "attach"
    connected_adb_port = 5037
    def __init__(self, **kwargs):
      seen.update(kwargs)
      self.observations = {}
    def start(self): return self
    def latest(self): return None
    def status(self):
      return types.SimpleNamespace(running=False, reason="phone_source_unavailable", sequence=0,
                                   transport_session=None, connected=False,
                                   cleanup_verified=True, cleanup_diagnostics=())
    def close(self): return True
    def summary(self): return {"collector_calls": 0, "recover_hits": 0}

  monkeypatch.setattr(probe, "ObservedProducer", AttachProducer)
  monkeypatch.setattr(probe, "PhonePipelineBench", OkBench)
  monkeypatch.setattr(probe, "SAMPLE_INTERVAL_S", 0)
  ticks = iter([100.0 + i * 0.1 for i in range(400)])
  monkeypatch.setattr(probe.time, "monotonic", lambda: next(ticks, 999.0))
  out = tmp_path / "attach"
  probe.main(["--out", str(out), "--duration", "15", "--port", "5037",
              "--adb-ownership", "attach", "--serial", "phone", "--adb", "/fixed/adb"])
  report = json.loads((out / "report.json").read_text())
  assert seen == {"port": 5037, "serial": "phone", "adb": "/fixed/adb", "adb_ownership": "attach"}
  assert report["adb_transport"] == {"ownership": "attach", "adb_port": 5037, "owned": False}
  assert report["host_resources"][-1]["owned"] is False
