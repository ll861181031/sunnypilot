import hashlib
import json
import os
import shlex
import signal
import subprocess
import struct
from types import SimpleNamespace

import pytest

import native_route_source as native
from amap_memory_probe import ProbeError


EPOCH = "123:456789"
APK = "/data/app/com.jixiexiaoge.drivingassist.navdiag-test123==/base.apk"
PHONE = 9_000_000_000_000_000
RETRY_POSITION_FIELDS = (["tick_time_raw"], ["tick_time_raw", "input_tick_time_raw"],
                         ["native_st_pos_deg", "tick_time_raw"],
                         ["native_st_pos_deg", "tick_time_raw", "input_tick_time_raw"],
                         ["native_st_pos_deg", "matched_st_pos_deg", "tick_time_raw"],
                         ["native_st_pos_deg", "matched_st_pos_deg", "tick_time_raw", "input_tick_time_raw"])


def observation(**changes):
  result = {"schema_version": 1, "profile": native.SUPPORTED_PROFILE, "status": "observed",
            "reason": "consistent_read_only_observation", "path_identity_hash": "a" * 64,
            "navigation_state": "started", "navigation_running_observed": True,
            "sample_consistent": True, "gps_navigation_observed": True,
            "active_route_verified": False, "control_input_allowed": False,
            "source": "bounded_native_memory", "implementation": "java_read_only_no_injection",
            "atomic_snapshot": False, "observation_clock_domain": "phone_elapsed_realtime",
            "navi_type_raw": 0, "navi_state_raw": 1, "main_index": 0, "path_count": 3,
            "process_epoch_hash": hashlib.sha256(EPOCH.encode()).hexdigest(), "read_calls": 29,
            "read_bytes": 376, "observation_started_monotonic_ns": PHONE,
            "observation_finished_monotonic_ns": PHONE + 1_000_000, "observation_window_ms": 1.0}
  result.update(changes)
  return result


def unavailable(reason="manager_not_registered"):
  result = observation()
  result = {key: value for key, value in result.items() if key in native._BASE_FIELDS}
  result.update(status="unavailable", reason=reason, path_identity_hash=None, navigation_state="unknown",
                navigation_running_observed=False, sample_consistent=False, gps_navigation_observed=False)
  return result


def private_position(**changes):
  result = {"status": "observed", "reason": "unverified_position_semantics",
            **dict.fromkeys(native._POSITION_FLAGS, False), "source": "native_loc_info",
            "source_type_raw": 0, "coordinate_frame_candidate": "amap_native_map", "source_clock": "unknown",
            "native_st_pos_deg": [11.25, 22.5], "matched_st_pos_deg": [11.2502, 22.5001],
            "segment_index": 2, "link_index": 3, "point_index": 4, "point_index_source_field": "postCur",
            "match_index": 1, "match_count": 2, "is_on_guide_road_raw": 1,
            "tick_time_raw": 1001000, "input_tick_time_raw": 1000000, "path_identity_hash": "a" * 64}
  result.update(changes)
  return result


def private_navi_info(**changes):
  binding = {"status": "verified", "reason": "segment_object_verified",
             "segment_object_verified": True, "icon_id_getter_vtable_verified": True,
             "icon_getter_vtable_verified": True}
  branch = {"maneuver_id_raw": 14, "maneuver_segment_raw": 2, "maneuver_link_raw": 3, "maneuver_point_raw": 4,
            "exit_number_raw": 2, "exit_angle_raw": 90, "obtain_supported_raw": 1, "data_changed_raw": 0,
            "segment_remain_time_raw": 40, "segment_remain_distance_raw": 500,
            "next_road_name_segment_raw": 5, "next_road_name_link_raw": 6, "split_raw": 0,
            "segment_binding": binding}
  result = {"schema_version": 2, "status": "observed", "reason": "unverified_navi_info_semantics",
            "diagnostic_only": True, "control_input_allowed": False, "route_render_input_allowed": False,
            "source": "native_navi_info", "path_identity_hash": "a" * 64,
            "route_remain_time_raw": 100, "route_remain_distance_raw": 1000, "navigation_info_type_raw": 0,
            "item_count": 3, "item_index": 1, "current_segment": 2, "current_link": 3, "current_point": 4,
            "branch_flag_raw": 1, "branches": [branch, dict(branch, maneuver_id_raw=15)]}
  result.update(changes)
  return result


def encode(value):
  return json.dumps(value, ensure_ascii=False).encode() + b"\n"


def selection_unavailable(reason="selection_changed_during_read"):
  return {**unavailable(reason), "_position": {"status": "unavailable", "reason": "snapshot_unavailable",
                                              **dict.fromkeys(native._POSITION_FLAGS, False)}}


def selection_diagnosis(**changes):
  result = {"schema_version": 1, "source": "native_observation_pair", "changed_fields": ["position"],
            "position_changed_fields": ["tick_time_raw"], "position_serialization_changed": True}
  result.update(changes)
  return result


def snapshot_retry(**changes):
  result = {"schema_version": 1, "source": "native_observation_pair", "extra_observations": 1,
            "recovered": True, "initial_diagnosis": selection_diagnosis()}
  result.update(changes)
  return result


def retried_observation(**changes):
  return observation(_position=private_position(tick_time_raw=1002000), read_calls=66, read_bytes=975,
                     **{"_snapshot_retry": snapshot_retry(), **changes})


def navi_retry_diagnosis(**changes):
  return selection_diagnosis(**{"schema_version": 2, "navi_info_changed_fields": [],
                                "navi_info_serialization_changed": False, **changes})


@pytest.mark.parametrize("fields", RETRY_POSITION_FIELDS)
def test_navi_snapshot_position_retry_survives_source_protocol(setup, fields):
  initial = navi_retry_diagnosis(position_changed_fields=fields)
  retry = snapshot_retry(initial_diagnosis=initial)
  value = retried_observation(_snapshot_retry=retry, _navi_info=private_navi_info())
  setup.options["responses"] = [encode(value)]
  with native.NativeRouteProbeSource(setup.device) as source:
    result = source.observe_position_navi_info()
  assert result["_snapshot_retry"] == retry
  assert result["_position"]["tick_time_raw"] == 1002000
  assert result["_navi_info"] == private_navi_info()
  assert result["control_input_allowed"] is False


@pytest.mark.parametrize("reason", ["selection_changed_during_read", "read_failed", "process_epoch_changed",
                                   "snapshot_retry_deadline_exceeded"])
@pytest.mark.parametrize("fields", RETRY_POSITION_FIELDS)
def test_failed_navi_snapshot_position_retry_preserves_evidence(setup, reason, fields):
  value = selection_unavailable(reason)
  value["_navi_info"] = {key: item for key, item in private_navi_info().items() if key in native._NAVI_INFO_BASE}
  value["_navi_info"].update(status="unavailable", reason="snapshot_unavailable")
  value["_snapshot_retry"] = snapshot_retry(recovered=False, initial_diagnosis=navi_retry_diagnosis(position_changed_fields=fields))
  if reason == "selection_changed_during_read":
    value["_selection_diagnosis"] = navi_retry_diagnosis(
      changed_fields=["navi_info"], position_changed_fields=[], position_serialization_changed=False,
      navi_info_changed_fields=["branches"], navi_info_serialization_changed=True)
  setup.options["responses"] = [encode(value)]
  with native.NativeRouteProbeSource(setup.device) as source:
    result = source.observe_position_navi_info()
  assert result["reason"] == reason
  assert result["_snapshot_retry"] == value["_snapshot_retry"]


@pytest.mark.parametrize("changes", [
  {"position_changed_fields": fields} for fields in (
    ["native_st_pos_deg"], ["native_st_pos_deg", "input_tick_time_raw"],
    ["native_st_pos_deg", "tick_time_raw", "matched_st_pos_deg", "segment_index"],
    ["input_tick_time_raw"], ["tick_time_raw", "matched_st_pos_deg"], ["tick_time_raw", "segment_index"])
] + [
  {"changed_fields": [field, "position"]} for field in ("path_id", "nav_state", "main_index", "header", "chain")
] + [
  {"changed_fields": ["position", "navi_info"], "navi_info_changed_fields": ["branches"],
   "navi_info_serialization_changed": True},
  {"changed_fields": ["position", "navi_info"], "navi_info_serialization_changed": True},
  {"navi_info_changed_fields": ["branches"]}, {"schema_version": 1},
])
def test_navi_snapshot_retry_rejects_unqualified_position_identity_or_action_changes(setup, changes):
  retry = snapshot_retry(initial_diagnosis=navi_retry_diagnosis(**changes))
  setup.options["responses"] = [encode(retried_observation(_snapshot_retry=retry, _navi_info=private_navi_info()))]
  with native.NativeRouteProbeSource(setup.device) as source:
    with pytest.raises(ProbeError, match="native_probe_snapshot_retry_invalid"):
      source.observe_position_navi_info()


def geometry_frame(*, path_hash=None, points=None, spans=((0, 0, 2),), source_epoch=EPOCH):
  points = points or ((3_600_000, 7_200_000), (3_600_100, 7_200_000))
  payload = bytearray(112 + len(spans) * 12 + len(points) * 8)
  payload[:16] = b"AMAP_NAV_GEO_V2\x00"
  struct.pack_into("<8I", payload, 16, 1, 0, 1, 1, 0, 1, len(spans), len(points))
  payload[48:80] = bytes.fromhex(path_hash or "a" * 64)
  payload[80:112] = hashlib.sha256(source_epoch.encode()).digest()
  for index, (segment, start, count) in enumerate(spans):
    struct.pack_into("<3I", payload, 112 + index * 12, segment, start, count)
  offset = 112 + len(spans) * 12
  for index, point in enumerate(points):
    struct.pack_into("<2i", payload, offset + index * 8, *point)
  body = bytes(payload)
  return b"NAVDIAG_GEOMETRY_V2 " + str(len(body)).encode() + b" " + hashlib.sha256(body).hexdigest().encode() + b"\n" + body + b"\n"


class FakeInput:
  def __init__(self, process):
    self.process = process
    self.closed = False
    self.writes = []

  def write(self, value):
    if self.closed:
      raise BrokenPipeError
    self.writes.append(value)
    if value in (b"OBSERVE\n", b"OBSERVE_POSITION\n", b"OBSERVE_NAVI_INFO\n",
                 b"OBSERVE_POSITION_NAVI_INFO\n"):
      response = self.process.responses.pop(0)
      if response == "EOF":
        os.close(self.process.output_fd)
        self.process.output_fd = None
      elif response is not None:
        os.write(self.process.output_fd, response)
    elif value == b"OBSERVE_GEOMETRY\n":
      response = self.process.geometry_responses.pop(0)
      if response == "EOF":
        os.close(self.process.output_fd)
        self.process.output_fd = None
      elif response is not None:
        os.write(self.process.output_fd, response)
    elif value == b"QUIT\n" and not self.process.hang_quit:
      self.process.returncode = 0
    return len(value)

  def flush(self):
    if self.closed:
      raise ValueError("closed")

  def close(self):
    self.closed = True


class FakeProcess:
  def __init__(self, responses, header, hang_quit, geometry_responses=None):
    incoming, self.output_fd = os.pipe()
    self.stdout = os.fdopen(incoming, "rb", buffering=0)
    self.stdin = FakeInput(self)
    self.responses = list(responses)
    self.geometry_responses = list(geometry_responses or [])
    self.returncode = None
    self.pid = 22222
    self.hang_quit = hang_quit
    self.wait_calls = []
    if header is not None:
      os.write(self.output_fd, header)

  def poll(self):
    return self.returncode

  def wait(self, timeout):
    self.wait_calls.append(timeout)
    if self.returncode is None:
      raise subprocess.TimeoutExpired("synthetic-worker", timeout)
    return self.returncode

  def dispose(self):
    if self.output_fd is not None:
      os.close(self.output_fd)
      self.output_fd = None
    if not self.stdout.closed:
      self.stdout.close()


@pytest.fixture
def setup(monkeypatch):
  processes, launches, cleanup, killed = [], [], [], []
  options = {"responses": [encode(observation())], "header": b"NAVDIAG_ROUTE_V1 4444 5555\n",
             "geometry_responses": [], "hang_quit": False, "pm": "package:" + APK + "\n", "cleanup_rc": 0}

  def shell(*args, **kwargs):
    if args[1].startswith("pm path "):
      assert args == ("-T", "pm path " + native.PACKAGE + " | head -c 2049")
      assert kwargs == {"timeout": 5}
      return SimpleNamespace(stdout=options["pm"], returncode=0)
    cleanup.append((args, kwargs))
    return SimpleNamespace(stdout="", returncode=options["cleanup_rc"])

  def popen(args, **kwargs):
    launches.append((args, kwargs))
    process = FakeProcess(options["responses"], options["header"], options["hang_quit"],
                          options["geometry_responses"])
    processes.append(process)
    return process

  def killpg(pid, sig):
    killed.append((pid, sig))
    for process in processes:
      if process.pid == pid:
        process.returncode = -sig

  monkeypatch.setattr(native, "process_identity", lambda device: EPOCH)
  monkeypatch.setattr(native.subprocess, "Popen", popen)
  monkeypatch.setattr(native.os, "killpg", killpg)
  monkeypatch.setattr(native, "_RESPONSE_TIMEOUT_S", 0.01)
  monkeypatch.setattr(native, "_START_TIMEOUT_S", 0.01)
  ticks = iter(range(1_000_000_000, 2_000_000_000, 1_000_000))
  monkeypatch.setattr(native.time, "monotonic_ns", lambda: next(ticks))
  device = SimpleNamespace(adb="/fixed/adb", port=5039, serial="synthetic-device", shell=shell)
  state = SimpleNamespace(options=options, processes=processes, launches=launches, cleanup=cleanup,
                          killed=killed, device=device)
  yield state
  for process in processes:
    process.dispose()


def test_fixed_package_and_main_class_start_only_after_private_handshake(setup):
  with native.NativeRouteProbeSource(setup.device) as source:
    assert source.epoch == EPOCH
    args, kwargs = setup.launches[0]
    assert args[:9] == ["/fixed/adb", "-H", "127.0.0.1", "-P", "5039", "-s", "synthetic-device", "shell", "-T"]
    assert kwargs == {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE,
                      "stderr": subprocess.DEVNULL, "start_new_session": True}
    script = shlex.split(args[-1])
    assert script[:2] == ["su", "-c"] and len(script) == 3
    assert "CLASSPATH=" + APK in script[2]
    assert script[2].endswith("exec /system/bin/app_process /system/bin " + native.PROBE_CLASS)
    assert 'IFS= read -r navdiag_ack' in script[2]
    assert setup.processes[0].stdin.writes == [b"START\n"]
  assert setup.processes[0].stdin.writes == [b"START\n", b"QUIT\n"]
  assert source.cleanup_verified and not setup.cleanup and not setup.killed
  assert setup.processes[0].stdin.closed and setup.processes[0].stdout.closed


def test_observation_preserves_phone_domain_and_adds_explicit_host_bracket(setup):
  with native.NativeRouteProbeSource(setup.device) as source:
    result = source.observe()
    assert result["observation_started_monotonic_ns"] == PHONE
    assert result["observation_finished_monotonic_ns"] == PHONE + 1_000_000
    assert result["observation_clock_domain"] == "phone_elapsed_realtime"
    assert result["host_observation_started_monotonic_ns"] == 1_000_000_000
    assert result["host_observation_finished_monotonic_ns"] == 1_001_000_000
    assert result["host_observation_clock_domain"] == "host_monotonic"
    assert not result["active_route_verified"] and not result["control_input_allowed"]
    assert setup.processes[0].stdin.writes == [b"START\n", b"OBSERVE\n"]


def test_native_selection_diagnosis_survives_actual_source_protocol(setup):
  value = selection_unavailable("selection_changed_during_read")
  value["_selection_diagnosis"] = selection_diagnosis()
  setup.options["responses"] = [encode(value)]
  with native.NativeRouteProbeSource(setup.device) as source:
    result = source.observe_position()
  assert result["_selection_diagnosis"] == selection_diagnosis()
  assert result["status"] == "unavailable"
  assert result["sample_consistent"] is False and result["gps_navigation_observed"] is False


def test_legacy_native_selection_rejection_without_diagnosis_is_compatible(setup):
  setup.options["responses"] = [encode(selection_unavailable("selection_changed_during_read"))]
  with native.NativeRouteProbeSource(setup.device) as source:
    result = source.observe_position()
  assert result["reason"] == "selection_changed_during_read" and "_selection_diagnosis" not in result


@pytest.mark.parametrize("changes", [
  {"schema_version": True}, {"schema_version": 2}, {"source": "metadata_file"},
  {"changed_fields": []}, {"changed_fields": ["position", "position"]}, {"changed_fields": "position"},
  {"changed_fields": ["position"] * 9}, {"changed_fields": ["arbitrary_field"]}, {"changed_fields": [{}]},
  {"position_changed_fields": ["latitude"]}, {"position_changed_fields": "tick_time_raw"},
  {"position_changed_fields": ["tick_time_raw", "tick_time_raw"]}, {"position_changed_fields": [{}]},
  {"position_changed_fields": ["tick_time_raw"] * 25}, {"position_serialization_changed": 1},
  {"position_serialization_changed": False}, {"changed_fields": ["path_id"], "position_serialization_changed": False},
  {"raw_address": 12345}, {"coordinates": [11.25, 22.5]},
])
def test_invalid_native_selection_diagnosis_is_rejected(setup, changes):
  value = selection_unavailable("selection_changed_during_read")
  value["_selection_diagnosis"] = selection_diagnosis(**changes)
  setup.options["responses"] = [encode(value)]
  with native.NativeRouteProbeSource(setup.device) as source:
    with pytest.raises(ProbeError, match="native_probe_selection_diagnosis_invalid"):
      source.observe_position()
    assert source.cleanup_verified


@pytest.mark.parametrize("value", [observation(), selection_unavailable("process_epoch_changed")])
def test_native_selection_diagnosis_cannot_attach_to_success_or_other_failure(setup, value):
  value = {**value, "_selection_diagnosis": selection_diagnosis()}
  setup.options["responses"] = [encode(value)]
  with native.NativeRouteProbeSource(setup.device) as source:
    with pytest.raises(ProbeError, match="native_probe_selection_diagnosis_invalid"):
      source.observe_position()


def test_native_selection_diagnosis_allows_exact_serialization_only_difference(setup):
  value = selection_unavailable("selection_changed_during_read")
  value["_selection_diagnosis"] = selection_diagnosis(position_changed_fields=[])
  setup.options["responses"] = [encode(value)]
  with native.NativeRouteProbeSource(setup.device) as source:
    result = source.observe_position()
  assert result["_selection_diagnosis"]["position_changed_fields"] == []


def test_navi_info_serialization_only_change_does_not_need_a_mismatched_field(setup):
  value = selection_unavailable("selection_changed_during_read")
  value["_navi_info"] = {
    "schema_version": 2, "status": "unavailable", "reason": "selection_changed_during_read",
    "diagnostic_only": True, "control_input_allowed": False, "route_render_input_allowed": False,
  }
  value.pop("_position")
  value["_selection_diagnosis"] = {
    "schema_version": 2, "source": "native_observation_pair", "changed_fields": ["navi_info"],
    "position_changed_fields": [], "position_serialization_changed": False,
    "navi_info_changed_fields": [], "navi_info_serialization_changed": True}
  setup.options["responses"] = [encode(value)]
  with native.NativeRouteProbeSource(setup.device) as source:
    result = source.observe_navi_info()
  assert result["_selection_diagnosis"]["navi_info_changed_fields"] == []


def test_native_non_position_selection_diagnosis_is_supported(setup):
  value = selection_unavailable("selection_changed_during_read")
  value["_selection_diagnosis"] = selection_diagnosis(changed_fields=["chain", "header", "nav_type", "nav_state",
    "path_count", "main_index", "path_id"], position_changed_fields=[], position_serialization_changed=False)
  setup.options["responses"] = [encode(value)]
  with native.NativeRouteProbeSource(setup.device) as source:
    result = source.observe_position()
  assert result["_selection_diagnosis"] == value["_selection_diagnosis"]


def test_position_selection_diagnosis_cannot_be_reported_by_identity_only_command(setup):
  value = unavailable("selection_changed_during_read")
  value["_selection_diagnosis"] = selection_diagnosis()
  setup.options["responses"] = [encode(value)]
  with native.NativeRouteProbeSource(setup.device) as source:
    with pytest.raises(ProbeError, match="native_probe_selection_diagnosis_invalid"):
      source.observe()


@pytest.mark.parametrize("diagnosis", [None, [], {}, {"schema_version": 1}])
def test_missing_or_malformed_selection_diagnosis_object_is_rejected(setup, diagnosis):
  value = selection_unavailable()
  value["_selection_diagnosis"] = diagnosis
  setup.options["responses"] = [encode(value)]
  with native.NativeRouteProbeSource(setup.device) as source:
    with pytest.raises(ProbeError, match="native_probe_selection_diagnosis_invalid"):
      source.observe_position()


def test_second_real_observation_can_advance_phone_time(setup):
  setup.options["responses"].append(encode(observation(observation_started_monotonic_ns=PHONE + 2_000_000,
                                                     observation_finished_monotonic_ns=PHONE + 3_000_000)))
  with native.NativeRouteProbeSource(setup.device) as source:
    first, second = source.observe(), source.observe()
    assert second["host_observation_started_monotonic_ns"] > first["host_observation_finished_monotonic_ns"]


@pytest.mark.parametrize("changes", [
  {"schema_version": True}, {"schema_version": 2}, {"profile": "other"}, {"source": "arbitrary_memory"},
  {"implementation": "injected"}, {"observation_clock_domain": "host_monotonic"},
  {"active_route_verified": True}, {"control_input_allowed": True}, {"atomic_snapshot": True},
  {"sample_consistent": 1}, {"gps_navigation_observed": False}, {"navigation_running_observed": False},
  {"reason": "unexpected reason containing details"}, {"path_identity_hash": "g" * 64},
  {"path_count": True}, {"path_count": 33}, {"path_count": 0}, {"main_index": 3},
  {"navi_type_raw": True}, {"navi_state_raw": -1}, {"navigation_state": "paused"},
  {"read_calls": 513}, {"read_calls": 28}, {"read_bytes": 16385}, {"read_bytes": 10},
  {"observation_started_monotonic_ns": True}, {"observation_finished_monotonic_ns": PHONE - 1},
  {"observation_finished_monotonic_ns": PHONE + 4_000_000_000}, {"observation_window_ms": 1.1},
  {"raw_address": 12345}, {"path_id": 123}, {"_position": {"latitude": 0}},
])
def test_malformed_or_unexpected_trust_claim_closes_channel(setup, changes):
  setup.options["responses"] = [encode(observation(**changes))]
  with native.NativeRouteProbeSource(setup.device) as source:
    with pytest.raises(ProbeError, match="native_probe_result_invalid"):
      source.observe()
    assert source.process is None and source.cleanup_verified
    assert setup.processes[0].stdin.closed and setup.processes[0].stdout.closed


def test_process_replacement_closes_source_and_cannot_be_retried(setup):
  setup.options["responses"] = [encode(observation(process_epoch_hash="f" * 64))]
  with native.NativeRouteProbeSource(setup.device) as source:
    with pytest.raises(ProbeError, match="process_epoch_changed"):
      source.observe()
    with pytest.raises(ProbeError, match="native_probe_source_not_started"):
      source.observe()
  assert len(setup.launches) == 1


@pytest.mark.parametrize("started,finished", [(PHONE, PHONE + 1_000_000),
                                               (PHONE - 2_000_000, PHONE - 1_000_000),
                                               (PHONE + 500_000, PHONE + 1_500_000)])
def test_replayed_or_overlapping_phone_interval_is_rejected(setup, started, finished):
  setup.options["responses"].append(encode(observation(observation_started_monotonic_ns=started,
                                                     observation_finished_monotonic_ns=finished)))
  with native.NativeRouteProbeSource(setup.device) as source:
    source.observe()
    with pytest.raises(ProbeError, match="native_probe_phone_time_regressed"):
      source.observe()
    assert source.cleanup_verified


@pytest.mark.parametrize("raw,reason", [
  (b"{broken-json}\n", "native_probe_json_invalid"),
  (b'{"schema_version":1,"schema_version":1}\n', "native_probe_json_invalid"),
  (b'{"number":NaN}\n', "native_probe_json_invalid"),
  (b'{"number":Infinity}\n', "native_probe_json_invalid"),
  (b"\xff\n", "native_probe_json_invalid"),
  (b"[]\n", "native_probe_result_invalid"),
  (b"\n", "native_probe_json_invalid"),
  (b"{}\n{}\n", "native_probe_unsolicited_output"),
  (b"x" * (native.MAX_RESULT_BYTES + 1), "native_probe_result_too_large"),
  (None, "native_probe_channel_timeout"),
  ("EOF", "native_probe_channel_closed"),
])
def test_protocol_errors_close_worker(setup, raw, reason):
  setup.options["responses"] = [raw]
  with native.NativeRouteProbeSource(setup.device) as source:
    with pytest.raises(ProbeError, match=reason):
      source.observe()
    assert source.cleanup_verified
  assert setup.processes[0].stdin.closed and setup.processes[0].stdout.closed


def test_unsolicited_delayed_line_cannot_become_next_request_response(setup):
  with native.NativeRouteProbeSource(setup.device) as source:
    source.observe()
    os.write(setup.processes[0].output_fd, encode(observation()))
    with pytest.raises(ProbeError, match="native_probe_unsolicited_output"):
      source.observe()
    assert setup.processes[0].stdin.writes.count(b"OBSERVE\n") == 1


@pytest.mark.parametrize("path", ["package:/data/app/other-test/base.apk\n", "package:" + APK,
                                  "package:" + APK + "\npackage:" + APK + "\n",
                                  "package:/data/app/" + native.PACKAGE + "-abc/../base.apk\n",
                                  "package:/data/app/" + native.PACKAGE + "-abc/split.apk\n",
                                  "package:" + "x" * 2050 + "\n"])
def test_pm_path_rejects_other_package_split_multiline_or_traversal(setup, path):
  setup.options["pm"] = path
  with pytest.raises(ProbeError, match="native_probe_apk_path_invalid"):
    with native.NativeRouteProbeSource(setup.device):
      pytest.fail("unexpected start")
  assert not setup.launches


@pytest.mark.parametrize("header", [b"unrecognized\n", b"NAVDIAG_ROUTE_V1 0 123\n",
                                     b"NAVDIAG_ROUTE_V1 123 456\nextra\n", None])
def test_unacknowledged_start_never_launches_java_and_closes_shell(setup, header):
  setup.options["header"] = header
  with pytest.raises(ProbeError):
    with native.NativeRouteProbeSource(setup.device):
      pytest.fail("unexpected start")
  assert b"START\n" not in setup.processes[0].stdin.writes
  assert setup.processes[0].stdin.closed and setup.processes[0].stdout.closed
  assert not setup.cleanup


def test_timeout_with_hung_quit_cleans_only_handshaken_remote_and_local_group(setup):
  setup.options.update(responses=[None], hang_quit=True)
  with native.NativeRouteProbeSource(setup.device) as source:
    with pytest.raises(ProbeError, match="native_probe_channel_timeout"):
      source.observe()
    assert source.cleanup_verified
  assert len(setup.cleanup) == 1
  args, kwargs = setup.cleanup[0]
  script = shlex.split(args[1])[-1]
  assert "navdiag_pid=4444" in script and '"$1" = 5555' in script
  assert "NAVDIAG_PROBE_SESSION=" + source._token in script
  assert native.PROBE_CLASS in script and "/proc/4444/environ" in script
  assert "pkill" not in script and "/proc/*" not in script
  assert kwargs == {"timeout": 5, "check": False}
  assert setup.killed == [(22222, signal.SIGTERM)]
  assert setup.processes[0].stdin.closed and setup.processes[0].stdout.closed


def test_remote_cleanup_cannot_be_claimed_if_ownership_or_device_check_fails(setup):
  setup.options.update(responses=[None], hang_quit=True, cleanup_rc=76)
  source = native.NativeRouteProbeSource(setup.device)
  source.__enter__()
  with pytest.raises(ProbeError, match="native_probe_cleanup_unverified"):
    source.observe()
  assert not source.cleanup_verified and source.process is None
  assert setup.killed == [(22222, signal.SIGTERM)]


def test_unavailable_is_never_promoted_and_source_is_closed(setup):
  setup.options["responses"] = [encode(unavailable())]
  with native.NativeRouteProbeSource(setup.device) as source:
    result = source.observe()
    assert result["status"] == "unavailable" and result["path_identity_hash"] is None
    assert not result["sample_consistent"] and not result["gps_navigation_observed"]
    assert source.process is None and source.cleanup_verified


def test_empty_route_and_paused_navigation_keep_original_meaning(setup):
  empty = observation(path_count=0, main_index=0, path_identity_hash=None,
                      navigation_running_observed=False, gps_navigation_observed=False, read_calls=17)
  paused = observation(navi_state_raw=2, navigation_state="paused", navigation_running_observed=False,
                       gps_navigation_observed=False, observation_started_monotonic_ns=PHONE + 2_000_000,
                       observation_finished_monotonic_ns=PHONE + 3_000_000)
  setup.options["responses"] = [encode(empty), encode(paused)]
  with native.NativeRouteProbeSource(setup.device) as source:
    assert source.observe()["path_identity_hash"] is None
    assert source.observe()["navigation_state"] == "paused"


def test_source_cannot_restart_after_close_or_enter_twice(setup):
  source = native.NativeRouteProbeSource(setup.device)
  source.__enter__()
  with pytest.raises(ProbeError, match="native_probe_source_already_used"):
    source.__enter__()
  source.close()
  source.close()
  with pytest.raises(ProbeError, match="native_probe_source_already_used"):
    source.__enter__()


def test_invalid_host_window_does_not_relabel_phone_clock(setup, monkeypatch):
  ticks = iter((1_000_000_000, 999_999_999))
  monkeypatch.setattr(native.time, "monotonic_ns", lambda: next(ticks))
  with native.NativeRouteProbeSource(setup.device) as source:
    with pytest.raises(ProbeError, match="native_probe_host_window_invalid"):
      source.observe()


def test_error_does_not_include_raw_worker_output(setup):
  setup.options["responses"] = [b"private-payload-not-for-report\n"]
  with native.NativeRouteProbeSource(setup.device) as source:
    with pytest.raises(ProbeError) as error:
      source.observe()
    assert "private-payload" not in str(error.value)


def test_cleanup_command_uses_last_stat_parenthesis_and_no_general_process_scan():
  script = native._cleanup_script("4444", "5555", "a" * 32)
  assert "${navdiag_stat##*) }" in script
  assert "/proc/4444/cmdline" in script and "/proc/4444/environ" in script
  assert 'kill -TERM "$navdiag_pid"' in script
  assert "kill -9" not in script and "killall" not in script
  assert "pidof" not in script and "ps " not in script


@pytest.mark.parametrize("case", ["owned", "old_epoch", "wrong_token", "wrong_cmdline", "ignores_term"])
def test_cleanup_shell_checks_ownership_and_observes_actual_local_exit(case):
  token = "b" * 32
  command = "/bin/cat" if case == "wrong_cmdline" else native.PROBE_CLASS
  options = {}
  if case == "ignores_term":
    options["preexec_fn"] = lambda: signal.signal(signal.SIGTERM, signal.SIG_IGN)
  process = subprocess.Popen([command], executable="/bin/cat", stdin=subprocess.PIPE,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                             env={**os.environ, "NAVDIAG_PROBE_SESSION": token}, **options)
  try:
    with open("/proc/" + str(process.pid) + "/stat", encoding="ascii") as stream:
      start_time = stream.read().rsplit(") ", 1)[1].split()[19]
    if case == "old_epoch":
      start_time = str(int(start_time) + 1)
    script = native._cleanup_script(str(process.pid), start_time, "c" * 32 if case == "wrong_token" else token)
    result = subprocess.run(["/bin/sh"], input=script, capture_output=True, text=True, timeout=5)
    if case in ("owned", "ignores_term"):
      assert result.returncode == 0
      assert process.wait(timeout=1) == (-signal.SIGTERM if case == "owned" else -signal.SIGKILL)
    else:
      assert result.returncode == (0 if case == "old_epoch" else 76)
      assert process.poll() is None
  finally:
    if process.poll() is None:
      process.kill()
    process.wait(timeout=1)
    process.stdin.close()


def test_position_is_only_available_through_explicit_fixed_command(setup):
  setup.options["responses"] = [encode(observation(_position=private_position(), read_calls=45, read_bytes=658))]
  with native.NativeRouteProbeSource(setup.device) as source:
    result = source.observe_position()
    assert result["_position"]["native_st_pos_deg"] == [11.25, 22.5]
    assert result["_position"]["tick_time_raw"] == 1001000
    assert result["_position"]["source_clock"] == "unknown"
    assert not any(result["_position"][key] for key in native._POSITION_FLAGS)
    assert setup.processes[0].stdin.writes == [b"START\n", b"OBSERVE_POSITION\n"]


def test_navi_info_is_only_available_through_explicit_fixed_command(setup):
  setup.options["responses"] = [encode(observation(_navi_info=private_navi_info(), read_calls=38, read_bytes=800))]
  with native.NativeRouteProbeSource(setup.device) as source:
    result = source.observe_navi_info()
    assert result["_navi_info"]["reason"] == "unverified_navi_info_semantics"
    assert result["_navi_info"]["diagnostic_only"] is True
    assert result["_navi_info"]["control_input_allowed"] is False
    assert result["_navi_info"]["route_render_input_allowed"] is False
    assert setup.processes[0].stdin.writes == [b"START\n", b"OBSERVE_NAVI_INFO\n"]


def test_position_and_navi_info_share_one_explicit_fixed_command(setup):
  setup.options["responses"] = [encode(observation(
    _position=private_position(), _navi_info=private_navi_info(path_identity_hash="a" * 64),
    read_calls=66, read_bytes=975))]
  with native.NativeRouteProbeSource(setup.device) as source:
    result = source.observe_position_navi_info()
  assert result["_position"]["segment_index"] == 2
  assert result["_navi_info"]["branches"][1]["maneuver_id_raw"] == 15
  assert setup.processes[0].stdin.writes == [
    b"START\n", b"OBSERVE_POSITION_NAVI_INFO\n", b"QUIT\n",
  ]


@pytest.mark.parametrize("partial", [None, {"path_identity_hash": "a" * 64}, {"branches": []},
                                   {"item_index": 0}])
@pytest.mark.parametrize("reason", ["navi_info_path_match_missing", "navi_info_count_zero"])
def test_missing_navi_info_match_keeps_only_independently_verified_parent(setup, partial, reason):
  action = {key: item for key, item in private_navi_info().items() if key in native._NAVI_INFO_BASE}
  action.update(status="unavailable", reason=reason)
  action.update(partial or {})
  setup.options["responses"] = [encode(observation(_position=private_position(), _navi_info=action))]
  with native.NativeRouteProbeSource(setup.device) as source:
    if partial is not None:
      with pytest.raises(ProbeError, match="native_probe_navi_info_invalid"):
        source.observe_position_navi_info()
    else:
      result = source.observe_position_navi_info()
      assert result["status"] == "observed" and result["sample_consistent"]
      assert result["_position"]["status"] == "observed"
      assert result["path_identity_hash"] == result["_position"]["path_identity_hash"]
      assert result["_navi_info"] == action


def test_geometry_v2_binary_frame_is_strictly_parsed_without_exposing_points(setup):
  frame = geometry_frame()
  setup.options["geometry_responses"] = [frame]
  with native.NativeRouteProbeSource(setup.device) as source:
    value = source.observe_geometry()
  assert value["status"] == "observed"
  assert value["geometry_kind"] == "current_native_path_window"
  assert value["path_identity_hash"] == "a" * 64
  assert value["segments"] == (((0, 2),),)
  assert value["points_native"] == ((3_600_000, 7_200_000), (3_600_100, 7_200_000))
  body = frame.split(b"\n", 1)[1][:-1]
  assert value["source_geometry_sha256"] == hashlib.sha256(body).hexdigest()
  assert "geo:" + ("a" * 64) not in str(value)
  assert setup.processes[0].stdin.writes == [b"START\n", b"OBSERVE_GEOMETRY\n", b"QUIT\n"]


@pytest.mark.parametrize("change", [
  lambda frame: frame.replace(b"AMAP_NAV_GEO_V2\x00", b"AMAP_NAV_GEO_V1\x00", 1),
  lambda frame: frame.replace(b" ", b"  ", 1),
  lambda frame: frame[:-1] + b"x",
])
def test_geometry_v2_malformed_header_or_binary_terminator_closes_source(setup, change):
  setup.options["geometry_responses"] = [change(geometry_frame())]
  with native.NativeRouteProbeSource(setup.device) as source:
    with pytest.raises(ProbeError):
      source.observe_geometry()
    assert source.cleanup_verified


def test_geometry_v2_point_outside_coordinate_range_is_rejected(setup):
  setup.options["geometry_responses"] = [geometry_frame(
    points=((180 * native.COORDINATE_SCALE + 1, 7_200_000), (3_600_100, 7_200_000)))]
  with native.NativeRouteProbeSource(setup.device) as source:
    with pytest.raises(ProbeError, match="native_probe_geometry_point_invalid"):
      source.observe_geometry()
    assert source.cleanup_verified


def test_legacy_unsupported_geometry_command_does_not_close_source(setup):
  setup.options["geometry_responses"] = [b"NAVDIAG_GEOMETRY_V2 ERROR unsupported_command\n"]
  with native.NativeRouteProbeSource(setup.device) as source:
    with pytest.raises(ProbeError, match="native_probe_geometry_unsupported"):
      source.observe_geometry()
    assert source.process is not None
    result = source.observe()
  assert result["status"] == "observed"


@pytest.mark.parametrize("changes", [
  {"diagnostic_only": False}, {"control_input_allowed": True}, {"route_render_input_allowed": True},
  {"schema_version": 1}, {"status": "verified"}, {"reason": "verified"}, {"source": "other"},
  {"path_identity_hash": "b" * 64}, {"item_count": 0}, {"item_count": 33}, {"item_index": 3},
  {"current_segment": -1}, {"current_link": -1}, {"current_point": -1}, {"branch_flag_raw": 2},
  {"branches": []}, {"branches": [private_navi_info()["branches"][0]]}, {"raw_address": 1},
  {"latitude": 1},
])
def test_navi_info_malformed_or_release_claim_is_rejected(setup, changes):
  setup.options["responses"] = [encode(observation(_navi_info=private_navi_info(**changes)))]
  with native.NativeRouteProbeSource(setup.device) as source:
    with pytest.raises(ProbeError, match="native_probe_navi_info_invalid"):
      source.observe_navi_info()
    assert source.cleanup_verified


@pytest.mark.parametrize("changes", [
  {"maneuver_id_raw": -1}, {"obtain_supported_raw": 2}, {"data_changed_raw": -1},
  {"extra": 1}, {"raw_address": 1},
])
def test_navi_info_branch_fields_are_exact_and_bounded(setup, changes):
  value = private_navi_info()
  value["branches"][0].update(changes)
  setup.options["responses"] = [encode(observation(_navi_info=value))]
  with native.NativeRouteProbeSource(setup.device) as source:
    with pytest.raises(ProbeError, match="native_probe_navi_info_invalid"):
      source.observe_navi_info()


@pytest.mark.parametrize("changes", [
  {"status": "unavailable", "reason": "segment_index_unavailable",
   "segment_object_verified": True},
  {"status": "verified", "reason": "segment_object_verified",
   "segment_object_verified": False, "icon_id_getter_vtable_verified": True,
   "icon_getter_vtable_verified": True},
  {"status": "verified", "reason": "segment_object_verified",
   "segment_object_verified": True, "icon_id_getter_vtable_verified": False,
   "icon_getter_vtable_verified": True},
  {"status": "verified", "reason": "segment_object_verified",
   "segment_object_verified": True, "icon_id_getter_vtable_verified": True,
   "icon_getter_vtable_verified": False},
  {"status": "verified", "reason": "segment_getter_mapping_invalid",
   "segment_object_verified": True, "icon_id_getter_vtable_verified": True,
   "icon_getter_vtable_verified": True},
  {"status": "verified", "reason": "segment_object_verified",
   "segment_object_verified": True, "icon_id_getter_vtable_verified": True,
   "icon_getter_vtable_verified": True, "raw_address": 1},
])
def test_navi_info_segment_binding_is_exact_and_fails_closed(setup, changes):
  value = private_navi_info()
  value["branches"][0]["segment_binding"] = changes
  setup.options["responses"] = [encode(observation(_navi_info=value))]
  with native.NativeRouteProbeSource(setup.device) as source:
    with pytest.raises(ProbeError, match="native_probe_navi_info_invalid"):
      source.observe_navi_info()
    assert source.cleanup_verified


def test_navi_info_unavailable_segment_binding_is_preserved_without_addresses(setup):
  value = private_navi_info()
  value["branches"][0]["segment_binding"] = {
    "status": "unavailable", "reason": "segment_index_unavailable"}
  setup.options["responses"] = [encode(observation(_navi_info=value))]
  with native.NativeRouteProbeSource(setup.device) as source:
    result = source.observe_navi_info()
  assert result["_navi_info"]["branches"][0]["segment_binding"] == {
    "status": "unavailable", "reason": "segment_index_unavailable"}
  assert "address" not in json.dumps(result)


@pytest.mark.parametrize("index", [0, 1])
@pytest.mark.parametrize("field,reason", [
  ("next_road_name_segment_raw", "navi_info_next_segment_invalid"),
  ("next_road_name_link_raw", "navi_info_next_link_invalid"),
])
@pytest.mark.parametrize("raw", [0x80000000, 0xfffffffe])
def test_next_road_quarantine_retains_raw_and_route(setup, index, field, reason, raw):
  value = private_navi_info()
  value["branches"][index][field] = raw
  value["reason"] = reason
  setup.options["responses"] = [encode(observation(_navi_info=value))]
  with native.NativeRouteProbeSource(setup.device) as source:
    result = source.observe_navi_info()
  assert result["status"] == "observed"
  assert result["_navi_info"] == value
  assert result["control_input_allowed"] is False


@pytest.mark.parametrize("index", [0, 1])
@pytest.mark.parametrize("field", ["next_road_name_segment_raw", "next_road_name_link_raw"])
@pytest.mark.parametrize("quarantine", ["none", "one_branch", "wrong_reason"])
def test_next_road_bad_raw_cannot_claim_verified_action(setup, index, field, quarantine):
  value = private_navi_info()
  value["branches"][index][field] = 0xfffffffe
  reason = "navi_info_next_segment_invalid" if field.endswith("segment_raw") else "navi_info_next_link_invalid"
  if quarantine == "one_branch":
    value["branches"][index]["segment_binding"] = {"status": "unavailable", "reason": reason}
  elif quarantine == "wrong_reason":
    value["reason"] = "navi_info_next_link_invalid" if field.endswith("segment_raw") else "navi_info_next_segment_invalid"
  setup.options["responses"] = [encode(observation(_navi_info=value))]
  with native.NativeRouteProbeSource(setup.device) as source:
    with pytest.raises(ProbeError, match="native_probe_navi_info_invalid"):
      source.observe_navi_info()


def test_old_probe_without_navi_info_schema_is_not_silently_accepted(setup):
  with native.NativeRouteProbeSource(setup.device) as source:
    with pytest.raises(ProbeError, match="native_probe_navi_info_missing"):
      source.observe_navi_info()


def test_navi_info_cannot_appear_on_normal_observe(setup):
  setup.options["responses"] = [encode(observation(_navi_info=private_navi_info()))]
  with native.NativeRouteProbeSource(setup.device) as source:
    with pytest.raises(ProbeError, match="native_probe_navi_info_unexpected"):
      source.observe()


def test_selection_v2_navi_info_change_is_strictly_rejected(setup):
  value = selection_unavailable("selection_changed_during_read")
  value["_navi_info"] = {
    "schema_version": 2, "status": "unavailable", "reason": "selection_changed_during_read",
    "diagnostic_only": True, "control_input_allowed": False, "route_render_input_allowed": False,
  }
  value.pop("_position")
  value["_selection_diagnosis"] = {
    "schema_version": 2, "source": "native_observation_pair", "changed_fields": ["navi_info"],
    "position_changed_fields": [], "position_serialization_changed": False,
    "navi_info_changed_fields": ["branch_flag_raw"], "navi_info_serialization_changed": True}
  setup.options["responses"] = [encode(value)]
  with native.NativeRouteProbeSource(setup.device) as source:
    result = source.observe_navi_info()
  assert result["_selection_diagnosis"]["navi_info_changed_fields"] == ["branch_flag_raw"]


@pytest.mark.parametrize("changes", [
  {"schema_version": 1}, {"navi_info_serialization_changed": False},
  {"navi_info_changed_fields": ["latitude"]},
  {"changed_fields": ["navi_info", "navi_info"]}, {"raw_address": 123},
  {"changed_fields": ["position"], "position_serialization_changed": True},
])
def test_invalid_navi_info_selection_diagnosis_is_rejected(setup, changes):
  value = selection_unavailable("selection_changed_during_read")
  value["_navi_info"] = private_navi_info()
  diagnosis = {"schema_version": 2, "source": "native_observation_pair", "changed_fields": ["navi_info"],
               "position_changed_fields": [], "position_serialization_changed": False,
               "navi_info_changed_fields": ["branch_flag_raw"], "navi_info_serialization_changed": True}
  diagnosis.update(changes)
  value["_selection_diagnosis"] = diagnosis
  setup.options["responses"] = [encode(value)]
  with native.NativeRouteProbeSource(setup.device) as source:
    with pytest.raises(ProbeError, match="native_probe_selection_diagnosis_invalid"):
      source.observe_navi_info()


def test_unchanged_native_ticks_do_not_gain_freshness_on_new_reads(setup):
  first = observation(_position=private_position(), read_calls=45, read_bytes=658)
  second = observation(_position=private_position(), read_calls=45, read_bytes=658,
                       observation_started_monotonic_ns=PHONE + 2_000_000,
                       observation_finished_monotonic_ns=PHONE + 3_000_000)
  setup.options["responses"] = [encode(first), encode(second)]
  with native.NativeRouteProbeSource(setup.device) as source:
    assert source.observe_position()["_position"] == source.observe_position()["_position"]


@pytest.mark.parametrize("changes", [
  {key: True} for key in native._POSITION_FLAGS
] + [
  {"status": "verified"}, {"reason": "valid_position"}, {"source_clock": "phone_elapsed_realtime"},
  {"coordinate_frame_candidate": "WGS84"}, {"source_type_raw": -1}, {"source_type_raw": True},
  {"path_identity_hash": "b" * 64}, {"match_count": 0}, {"match_count": 4}, {"match_index": 2},
  {"is_on_guide_road_raw": 0x81}, {"is_on_guide_road_raw": True}, {"segment_index": -1},
  {"link_index": 0x80000000}, {"point_index": -1}, {"point_index_source_field": "guessed_point"},
  {"tick_time_raw": 0}, {"input_tick_time_raw": 0x10000000000000000},
  {"native_st_pos_deg": [181, 22]}, {"native_st_pos_deg": [0, 0]},
  {"matched_st_pos_deg": [11, -91]}, {"native_st_pos_deg": [True, 22]},
  {"native_st_pos_deg": [10 ** 500, 22]}, {"matched_st_pos_deg": [11, 22, 3]},
  {"raw_address": 123}, {"latitude": 22},
])
def test_position_malformed_evidence_or_trust_claims_are_rejected(setup, changes):
  setup.options["responses"] = [encode(observation(_position=private_position(**changes), read_calls=45, read_bytes=658))]
  with native.NativeRouteProbeSource(setup.device) as source:
    with pytest.raises(ProbeError, match="native_probe_position_invalid"):
      source.observe_position()
    assert source.cleanup_verified


def test_old_probe_without_position_schema_is_not_silently_accepted(setup):
  with native.NativeRouteProbeSource(setup.device) as source:
    with pytest.raises(ProbeError, match="native_probe_position_missing"):
      source.observe_position()


def test_unavailable_position_has_no_partial_coordinate_payload(setup):
  position = {"status": "unavailable", "reason": "position_simulated", **dict.fromkeys(native._POSITION_FLAGS, False)}
  setup.options["responses"] = [encode(observation(_position=position))]
  with native.NativeRouteProbeSource(setup.device) as source:
    result = source.observe_position()
    assert result["status"] == "observed" and result["_position"] == position
    assert source.process is not None


def test_unavailable_top_level_cannot_claim_observed_position(setup):
  value = unavailable()
  value["_position"] = private_position()
  setup.options["responses"] = [encode(value)]
  with native.NativeRouteProbeSource(setup.device) as source:
    with pytest.raises(ProbeError, match="native_probe_position_invalid"):
      source.observe_position()


def test_unsigned_native_ticks_remain_exact_in_host_json(setup):
  setup.options["responses"] = [encode(observation(_position=private_position(tick_time_raw=0xffffffffffffffff),
                                                 read_calls=45, read_bytes=658))]
  with native.NativeRouteProbeSource(setup.device) as source:
    assert source.observe_position()["_position"]["tick_time_raw"] == 0xffffffffffffffff


def performance():
  return {"clock_domain": "phone_elapsed_realtime", "identity_read_calls": 3, "memory_read_calls": 29,
          **dict.fromkeys(native._PERFORMANCE_NS, 1000)}


def test_optional_private_performance_retains_phone_duration_domain(setup):
  setup.options["responses"] = [encode(observation(_performance=performance()))]
  with native.NativeRouteProbeSource(setup.device) as source:
    value = source.observe()
    assert value["_performance"] == performance()
    assert value["host_observation_clock_domain"] == "host_monotonic"


@pytest.mark.parametrize("changes", [{"clock_domain": "host_monotonic"}, {"memory_read_calls": 28},
                                        {"identity_read_calls": 4}, {"maps_read_ns": True},
                                        {"maps_parse_ns": -1}, {"raw_address": 12345}])
def test_invalid_performance_cannot_smuggle_other_data_or_clock_claims(setup, changes):
  setup.options["responses"] = [encode(observation(_performance={**performance(), **changes}))]
  with native.NativeRouteProbeSource(setup.device) as source:
    with pytest.raises(ProbeError, match="native_probe_performance_invalid"):
      source.observe()


@pytest.mark.parametrize("fields", RETRY_POSITION_FIELDS)
def test_snapshot_retry_recovery_survives_actual_source_protocol(setup, fields):
  retry = snapshot_retry(initial_diagnosis=selection_diagnosis(position_changed_fields=fields))
  value = retried_observation(_snapshot_retry=retry, _performance={**performance(), "identity_read_calls": 4,
                                                                 "memory_read_calls": 66})
  setup.options["responses"] = [encode(value)]
  with native.NativeRouteProbeSource(setup.device) as source:
    result = source.observe_position()
  assert result["_snapshot_retry"] == retry and result["status"] == "observed"
  assert result["_position"]["tick_time_raw"] == 1002000
  assert result["read_calls"] == 66 and result["read_bytes"] == 975
  assert result["observation_started_monotonic_ns"] == PHONE
  assert "_selection_diagnosis" not in result


@pytest.mark.parametrize("reason", ["selection_changed_during_read", "read_failed", "short_read",
                                     "process_epoch_changed", "process_epoch_failed", "snapshot_retry_deadline_exceeded"])
@pytest.mark.parametrize("fields", RETRY_POSITION_FIELDS)
def test_failed_snapshot_retry_retains_actual_failure_and_initial_evidence(setup, reason, fields):
  value = selection_unavailable(reason)
  value["_snapshot_retry"] = snapshot_retry(recovered=False, initial_diagnosis=selection_diagnosis(position_changed_fields=fields))
  value["_performance"] = {**performance(), "identity_read_calls": 4, "memory_read_calls": 46}
  if reason == "selection_changed_during_read":
    value["_selection_diagnosis"] = selection_diagnosis(position_changed_fields=["input_tick_time_raw"])
  setup.options["responses"] = [encode(value)]
  with native.NativeRouteProbeSource(setup.device) as source:
    result = source.observe_position()
  assert result["reason"] == reason and result["_snapshot_retry"]["recovered"] is False
  assert result["_snapshot_retry"]["initial_diagnosis"]["position_changed_fields"] == fields
  assert ("_selection_diagnosis" in result) == (reason == "selection_changed_during_read")


@pytest.mark.parametrize("changes", [
  {"schema_version": True}, {"schema_version": 2}, {"source": "other"}, {"extra_observations": True},
  {"extra_observations": 0}, {"extra_observations": 2}, {"recovered": 1}, {"recovered": False},
  {"unknown": "value"}, {"initial_diagnosis": None}, {"initial_diagnosis": []}, {"initial_diagnosis": {}},
  {"initial_diagnosis": selection_diagnosis(position_changed_fields=[])},
  {"initial_diagnosis": selection_diagnosis(position_changed_fields=["native_st_pos_deg"])},
  {"initial_diagnosis": selection_diagnosis(position_changed_fields=["input_tick_time_raw"])},
  {"initial_diagnosis": selection_diagnosis(position_changed_fields=["native_st_pos_deg", "input_tick_time_raw"])},
  {"initial_diagnosis": selection_diagnosis(position_changed_fields=["tick_time_raw", "matched_st_pos_deg"])},
  {"initial_diagnosis": selection_diagnosis(position_changed_fields=["tick_time_raw", "segment_index"])},
  {"initial_diagnosis": selection_diagnosis(position_changed_fields=["tick_time_raw", "source_type_raw"])},
  {"initial_diagnosis": selection_diagnosis(position_changed_fields=["native_st_pos_deg", "matched_st_pos_deg"])},
  {"initial_diagnosis": selection_diagnosis(position_changed_fields=["native_st_pos_deg", "tick_time_raw", "input_tick_time_raw",
                                                                    "segment_index"])},
  {"initial_diagnosis": selection_diagnosis(position_changed_fields=["native_st_pos_deg", "tick_time_raw", "input_tick_time_raw",
                                                                    "input_tick_time_raw"])},
  {"initial_diagnosis": selection_diagnosis(changed_fields=["path_id", "position"])},
])
def test_invalid_snapshot_retry_schema_is_rejected(setup, changes):
  setup.options["responses"] = [encode(retried_observation(_snapshot_retry=snapshot_retry(**changes)))]
  with native.NativeRouteProbeSource(setup.device) as source:
    with pytest.raises(ProbeError, match="native_probe_snapshot_retry_invalid"):
      source.observe_position()


@pytest.mark.parametrize("retry", [None, [], {}, {"schema_version": 1}])
def test_malformed_snapshot_retry_object_is_rejected(setup, retry):
  setup.options["responses"] = [encode(retried_observation(_snapshot_retry=retry))]
  with native.NativeRouteProbeSource(setup.device) as source:
    with pytest.raises(ProbeError, match="native_probe_snapshot_retry_invalid"):
      source.observe_position()


@pytest.mark.parametrize("position", [None, [], {"status": "unavailable"}])
def test_successful_retry_requires_observed_position(setup, position):
  value = retried_observation()
  value["_position"] = position
  setup.options["responses"] = [encode(value)]
  with native.NativeRouteProbeSource(setup.device) as source:
    with pytest.raises(ProbeError, match="native_probe_snapshot_retry_invalid"):
      source.observe_position()


@pytest.mark.parametrize("fields", RETRY_POSITION_FIELDS)
def test_snapshot_retry_is_forbidden_on_identity_only_command(setup, fields):
  value = retried_observation(_snapshot_retry=snapshot_retry(initial_diagnosis=selection_diagnosis(position_changed_fields=fields)))
  value.pop("_position")
  setup.options["responses"] = [encode(value)]
  with native.NativeRouteProbeSource(setup.device) as source:
    with pytest.raises(ProbeError, match="native_probe_snapshot_retry_invalid"):
      source.observe()


def test_retry_selection_failure_requires_final_pair_diagnosis(setup):
  value = {**selection_unavailable(), "_snapshot_retry": snapshot_retry(recovered=False)}
  setup.options["responses"] = [encode(value)]
  with native.NativeRouteProbeSource(setup.device) as source:
    with pytest.raises(ProbeError, match="native_probe_snapshot_retry_invalid"):
      source.observe_position()


@pytest.mark.parametrize("window_ns", [50_000_000, 50_000_001, 1_000_000_000])
@pytest.mark.parametrize("fields", RETRY_POSITION_FIELDS)
def test_late_successful_snapshot_retry_is_rejected(setup, window_ns, fields):
  setup.options["responses"] = [encode(retried_observation(observation_finished_monotonic_ns=PHONE + window_ns,
                                                          observation_window_ms=window_ns / 1_000_000,
                                                          _snapshot_retry=snapshot_retry(initial_diagnosis=selection_diagnosis(
                                                            position_changed_fields=fields))))]
  with native.NativeRouteProbeSource(setup.device) as source:
    with pytest.raises(ProbeError, match="native_probe_snapshot_retry_invalid"):
      source.observe_position()


@pytest.mark.parametrize("fields", RETRY_POSITION_FIELDS)
def test_retry_does_not_allow_fifth_epoch_read(setup, fields):
  setup.options["responses"] = [encode(retried_observation(_performance={**performance(), "identity_read_calls": 5,
                                                                        "memory_read_calls": 66},
                                                          _snapshot_retry=snapshot_retry(initial_diagnosis=selection_diagnosis(
                                                            position_changed_fields=fields))))]
  with native.NativeRouteProbeSource(setup.device) as source:
    with pytest.raises(ProbeError, match="native_probe_performance_invalid"):
      source.observe_position()


@pytest.mark.parametrize("fields", RETRY_POSITION_FIELDS)
@pytest.mark.parametrize(("changes", "reason"), [
  ({"read_calls": native.MAX_READ_CALLS + 1}, "native_probe_result_invalid"),
  ({"read_bytes": native.MAX_READ_BYTES + 1}, "native_probe_result_invalid"),
  ({"process_epoch_hash": "b" * 64}, "process_epoch_changed"),
  ({"_position": private_position(path_identity_hash="b" * 64)}, "native_probe_position_invalid"),
])
def test_snapshot_retry_preserves_read_budgets_and_identity_checks(setup, fields, changes, reason):
  retry = snapshot_retry(initial_diagnosis=selection_diagnosis(position_changed_fields=fields))
  value = retried_observation(_snapshot_retry=retry)
  value.update(changes)
  setup.options["responses"] = [encode(value)]
  with native.NativeRouteProbeSource(setup.device) as source:
    with pytest.raises(ProbeError, match=reason):
      source.observe_position()
