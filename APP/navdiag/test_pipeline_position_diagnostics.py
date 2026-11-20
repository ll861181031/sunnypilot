"""Bounded, coordinate-free sample observations; no phone, GPU, or control process."""
from copy import deepcopy
from dataclasses import replace
import json
from types import MappingProxyType, SimpleNamespace

import numpy as np
import pytest

from APP.navdiag import live_pipeline_probe as probe
from openpilot.common.gcj import wgs84_to_gcj02
from openpilot.selfdrive.navigation.amap_phone_route import _distance
from openpilot.selfdrive.navigation.tests.test_amap_phone_pipeline import make_sample


def _inputs_and_details():
  return ({"nav_map": np.zeros((1, 1, 256, 256), np.uint8), "navigation": np.zeros((1, 47), np.float32)},
          {"runtime": {}, "source_reason": "phone_pose_space_mismatch", "map_valid": False,
           "instruction_valid": False, "reset_requested": False})


def test_position_diagnostics_use_adapter_metric_and_preserve_input(monkeypatch):
  sample = make_sample()
  ego = wgs84_to_gcj02(*sample.gps.coordinates)
  before = sample.native_before["_position"]
  after = sample.native_after["_position"]
  before.update(native_st_pos_deg=[ego[0], ego[1] + .00001], tick_time_raw=10, input_tick_time_raw=20)
  after.update(native_st_pos_deg=[ego[0], ego[1] - .00002], tick_time_raw=11, input_tick_time_raw=20)
  original = deepcopy(sample)
  monkeypatch.setattr(probe.time, "monotonic_ns", lambda: pytest.fail("diagnostic must not invent source time"))

  result = probe.sample_position_diagnostics(sample)

  assert result["gps_to_native_before_m"] == pytest.approx(1.111950802335329)
  assert result["gps_to_native_after_m"] == pytest.approx(2.223901604670658)
  assert result["native_before_to_after_m"] == pytest.approx(3.335852407005987)
  assert result["matched_to_source_edge_before_m"] == pytest.approx(0.0, abs=0.02)
  assert result["matched_to_source_edge_after_m"] == pytest.approx(0.0, abs=0.02)
  native_after = _distance(ego, original.native_after["_position"]["matched_st_pos_deg"])
  assert result["gps_to_source_edge_m"] == pytest.approx(native_after, abs=0.02)
  assert result["gps_to_native_before_m"] == _distance(ego, before["native_st_pos_deg"])
  assert result["gps_to_native_after_m"] == _distance(ego, after["native_st_pos_deg"])
  assert result["native_before_to_after_m"] == _distance(before["native_st_pos_deg"], after["native_st_pos_deg"])
  assert result["native_tick_time_changed"] is True
  assert result["native_input_tick_time_changed"] is False
  assert result["clock"] == "host_monotonic_ns"
  assert result["sample_host_start_ns"] == sample.host_start_ns
  assert result["gps"] == {
    "source_clock": "phone_elapsed_realtime_ns",
    "source_elapsed_realtime_ns": sample.gps.elapsed_realtime_ns,
    "source_age_upper_at_host_finished_ns": sample.gps.age_upper_ns,
    "host_window_ns": {"started": sample.gps.host_started_ns, "finished": sample.gps.host_finished_ns},
  }
  for side in ("native_before", "native_after"):
    observed = getattr(sample, side)
    assert result[side + "_host_window_ns"] == {
      "started": observed["host_observation_started_monotonic_ns"],
      "finished": observed["host_observation_finished_monotonic_ns"],
    }
  assert sample == original
  text = json.dumps(result, allow_nan=False)
  for secret in ("native_st_pos_deg", "coordinates", "path_cache", "longitude", "latitude"):
    assert secret not in text


def test_same_positions_are_zero_with_frozen_observations():
  sample = make_sample()
  for side in ("native_before", "native_after"):
    observation = getattr(sample, side)
    position = {**observation["_position"], "native_st_pos_deg": tuple(observation["_position"]["native_st_pos_deg"])}
    setattr(sample, side, MappingProxyType({**observation, "_position": MappingProxyType(position)}))
  result = probe.sample_position_diagnostics(sample)
  assert [result[key] for key in ("gps_to_native_before_m", "gps_to_native_after_m", "native_before_to_after_m")] == [0., 0., 0.]
  assert result["native_tick_time_changed"] == "unknown"
  assert result["native_input_tick_time_changed"] == "unknown"


@pytest.mark.parametrize("sample", [None, SimpleNamespace(), SimpleNamespace(gps=None, native_before=None, native_after=17)])
def test_missing_position_evidence_is_explicit_unknown(sample):
  result = probe.sample_position_diagnostics(sample)
  assert result == {
    "schema_version": 1, "clock": "host_monotonic_ns", "sample_host_start_ns": "unknown",
    "gps_to_native_before_m": "unknown", "gps_to_native_after_m": "unknown", "native_before_to_after_m": "unknown",
    "matched_to_source_edge_before_m": "unknown", "matched_to_source_edge_after_m": "unknown",
    "gps_to_source_edge_m": "unknown",
    "native_tick_time_changed": "unknown", "native_input_tick_time_changed": "unknown",
    "source_anchors": {
      "native_before": dict.fromkeys(("segment_index", "link_index", "point_index"), "unknown"),
      "native_after": dict.fromkeys(("segment_index", "link_index", "point_index"), "unknown"),
      "changed_fields": "unknown",
    },
    "gps": {"source_clock": "phone_elapsed_realtime_ns", "source_elapsed_realtime_ns": "unknown",
            "source_age_upper_at_host_finished_ns": "unknown", "host_window_ns": "unknown"},
    "native_before_host_window_ns": "unknown", "native_after_host_window_ns": "unknown",
  }
  json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("bad", [None, [], [121.0], [True, 31.0], [float("nan"), 31.0], [float("inf"), 31.0],
                                  [181.0, 31.0], [121.0, 91.0], [10 ** 1000, 31.0], "121,31"])
def test_bad_native_position_is_unknown_without_hiding_good_side(bad):
  sample = make_sample()
  sample.native_before["_position"]["native_st_pos_deg"] = bad
  result = probe.sample_position_diagnostics(sample)
  assert result["gps_to_native_before_m"] == "unknown"
  assert result["gps_to_native_after_m"] == 0.
  assert result["native_before_to_after_m"] == "unknown"
  json.dumps(result, allow_nan=False)


def test_matched_source_edge_offset_is_reported_without_coordinates():
  sample = make_sample()
  index = __import__("openpilot.selfdrive.navigation.amap_route_anchor",
                     fromlist=["AmapRouteAnchorIndex"]).AmapRouteAnchorIndex(sample.selected.route)
  edge = index.edge(0, 0, 0)
  offset_lat = 8.0 / 111195.0802335329
  sample.native_before["_position"]["matched_st_pos_deg"] = [edge.start[0], edge.start[1] + offset_lat]
  sample.native_after["_position"]["matched_st_pos_deg"] = [edge.start[0], edge.start[1] + offset_lat]
  result = probe.sample_position_diagnostics(sample)
  assert result["matched_to_source_edge_before_m"] == pytest.approx(8.0)
  assert result["matched_to_source_edge_after_m"] == pytest.approx(8.0)
  text = json.dumps(result, allow_nan=False)
  for secret in ("matched_st_pos_deg", "longitude", "latitude"):
    assert secret not in text


@pytest.mark.parametrize("bad", [None, (True, 31.0), (float("nan"), 31.0), (181.0, 31.0), (10 ** 1000, 31.0)])
def test_bad_gps_position_does_not_hide_native_displacement(bad):
  sample = make_sample()
  sample.gps = replace(sample.gps, coordinates=bad)
  result = probe.sample_position_diagnostics(sample)
  assert result["gps_to_native_before_m"] == result["gps_to_native_after_m"] == "unknown"
  assert result["native_before_to_after_m"] == 0.


@pytest.mark.parametrize("bad", [None, True, 0, -1, 1.5, "7", 1 << 64])
def test_bad_native_tick_is_unknown(bad):
  sample = make_sample()
  sample.native_before["_position"].update(tick_time_raw=bad, input_tick_time_raw=bad)
  sample.native_after["_position"].update(tick_time_raw=7, input_tick_time_raw=7)
  result = probe.sample_position_diagnostics(sample)
  assert result["native_tick_time_changed"] == result["native_input_tick_time_changed"] == "unknown"


@pytest.mark.parametrize("bad_window", [(None, 20), (True, 20), (0, 20), (21, 20), (10, float("inf")), (10, 1 << 63)])
def test_invalid_windows_remain_unknown(bad_window):
  sample = make_sample()
  sample.gps = replace(sample.gps, host_started_ns=bad_window[0], host_finished_ns=bad_window[1])
  sample.native_before["host_observation_started_monotonic_ns"] = bad_window[0]
  sample.native_before["host_observation_finished_monotonic_ns"] = bad_window[1]
  result = probe.sample_position_diagnostics(sample)
  assert result["gps"]["host_window_ns"] == result["native_before_host_window_ns"] == "unknown"
  assert isinstance(result["native_after_host_window_ns"], dict)


def test_wrong_clock_and_invalid_source_times_are_unknown():
  sample = make_sample()
  sample.host_start_ns = True
  sample.gps = replace(sample.gps, elapsed_realtime_ns=-1, age_upper_ns=True)
  sample.native_before["host_observation_clock_domain"] = "phone_elapsed_realtime"
  result = probe.sample_position_diagnostics(sample)
  assert result["sample_host_start_ns"] == result["native_before_host_window_ns"] == "unknown"
  assert result["gps"]["source_elapsed_realtime_ns"] == result["gps"]["source_age_upper_at_host_finished_ns"] == "unknown"


def test_frame_record_keeps_diagnostics_separate_from_adapter_reason():
  sample = make_sample()
  inputs, details = _inputs_and_details()
  result = probe.frame_record(4, 1200., sample, details, inputs)
  assert result["sample_position_diagnostics"] == probe.sample_position_diagnostics(sample)
  assert result["source_reason"] == details["source_reason"]
  assert result["map_valid"] is False
  assert result["native_snapshot_retries"] == []


def _retry_record(*, recovered=True):
  return {"schema_version": 1, "source": "native_observation_pair", "extra_observations": 1, "recovered": recovered,
          "initial_diagnosis": {"schema_version": 1, "source": "native_observation_pair", "changed_fields": ["position"],
                                "position_changed_fields": ["native_st_pos_deg", "tick_time_raw"],
                                "position_serialization_changed": True}}


def _freeze(value):
  if isinstance(value, dict):
    return MappingProxyType({key: _freeze(item) for key, item in value.items()})
  if isinstance(value, list):
    return tuple(_freeze(item) for item in value)
  return value


@pytest.mark.parametrize("fields", [["native_st_pos_deg", "tick_time_raw"],
                                    ["native_st_pos_deg", "tick_time_raw", "input_tick_time_raw"]])
def test_native_retry_records_preserve_actual_two_sides_without_rejection(fields):
  sample = make_sample()
  before_retry, after_retry = _retry_record(), _retry_record(recovered=False)
  before_retry["initial_diagnosis"]["position_changed_fields"] = list(fields)
  after_retry["initial_diagnosis"]["position_changed_fields"] = ["tick_time_raw"]
  sample.native_before["_snapshot_retry"] = deepcopy(before_retry)
  sample.native_after["_snapshot_retry"] = deepcopy(after_retry)
  original = deepcopy(sample)
  sample.native_before = _freeze(sample.native_before)
  sample.native_after = _freeze(sample.native_after)
  inputs, details = _inputs_and_details()
  first = probe.frame_record(1, 250., sample, details, inputs)
  second = probe.frame_record(2, 500., sample, details, inputs)
  assert first["native_snapshot_retries"] == [{"side": "native_before", **before_retry}, {"side": "native_after", **after_retry}]
  assert first["native_snapshot_retries"] == second["native_snapshot_retries"]
  assert first["sequence"] == second["sequence"] == sample.sequence
  assert first["source_reason"] == details["source_reason"]
  assert first["rejection"] is None and first["rejection_diagnosis"] is None
  assert sample.native_before["_snapshot_retry"] == _freeze(original.native_before["_snapshot_retry"])
  json.dumps(first, allow_nan=False)
  first["native_snapshot_retries"][0]["initial_diagnosis"]["position_changed_fields"].clear()
  assert tuple(sample.native_before["_snapshot_retry"]["initial_diagnosis"]["position_changed_fields"]) == tuple(fields)


@pytest.mark.parametrize("side", ["native_before", "native_after"])
def test_failed_triple_retry_diagnostic_is_preserved(side):
  sample = make_sample()
  retry = _retry_record(recovered=False)
  retry["initial_diagnosis"]["position_changed_fields"] = ["native_st_pos_deg", "tick_time_raw", "input_tick_time_raw"]
  getattr(sample, side)["_snapshot_retry"] = deepcopy(retry)
  result = probe.native_snapshot_retries(sample)
  assert result == [{"side": side, **retry}]
  assert result[0]["recovered"] is False
  json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("fields", [["input_tick_time_raw"], ["native_st_pos_deg", "input_tick_time_raw"],
                                    ["native_st_pos_deg", "tick_time_raw", "matched_st_pos_deg"],
                                    ["native_st_pos_deg", "tick_time_raw", "input_tick_time_raw", "segment_index"],
                                    ["native_st_pos_deg", "tick_time_raw", "input_tick_time_raw", "input_tick_time_raw"]])
def test_retry_diagnostic_keeps_unobserved_field_combinations_rejected(fields):
  sample = make_sample()
  retry = _retry_record()
  retry["initial_diagnosis"]["position_changed_fields"] = fields
  sample.native_before["_snapshot_retry"] = retry
  assert probe.native_snapshot_retries(sample) == []


@pytest.mark.parametrize("mutation", ["extra_key", "coordinates", "oversize", "bad_recovered", "bad_extra_observations", "bad_source"])
def test_invalid_retry_record_cannot_add_sensitive_or_unbounded_data(mutation):
  sample = make_sample()
  retry = _retry_record()
  if mutation == "extra_key":
    retry["untrusted"] = "sensitive value"
  elif mutation == "coordinates":
    retry["initial_diagnosis"]["position_changed_fields"] = ["121.1,31.1"]
  elif mutation == "oversize":
    retry["initial_diagnosis"]["changed_fields"] *= 1000
  elif mutation == "bad_recovered":
    retry["recovered"] = 1
  elif mutation == "bad_extra_observations":
    retry["extra_observations"] = 2
  else:
    retry["source"] = "121.1,31.1"
  sample.native_before["_snapshot_retry"] = retry
  sample.native_after["_snapshot_retry"] = _retry_record()
  inputs, details = _inputs_and_details()
  record = probe.frame_record(0, 0., sample, details, inputs)
  assert record["native_snapshot_retries"] == [{"side": "native_after", **_retry_record()}]
  assert "121.1,31.1" not in json.dumps(record)


def test_legacy_and_missing_samples_have_no_invented_retry():
  inputs, details = _inputs_and_details()
  for sample in (None, make_sample()):
    record = probe.frame_record(0, 0., sample, details, inputs)
    assert record["native_snapshot_retries"] == []
