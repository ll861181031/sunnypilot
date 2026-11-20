import copy
from dataclasses import replace
import json
import struct
from types import SimpleNamespace

import pytest

import bridge_observation as bridge


BOOT = "11111111-1111-4111-8111-111111111111"
SESSION = "22222222-2222-4222-8222-222222222222"
SOURCE = 1_000_000_000_000


def typed(kind, value):
  result = {"type": kind, "value": value}
  if kind in ("double", "float"):
    fmt = "d" if kind == "double" else "f"
    result["bits"] = format(int.from_bytes(struct.pack("<" + fmt, value), "little"), "x")
  return result


def fixture(*, source=SOURCE, published=None, sequence=1, session=SESSION, key="10001"):
  if published is None:
    published = source + 10_000_000
  fields = {"KEY_TYPE": typed("int", int(key)), "TYPE": typed("int", 0),
            "CUR_SEG_NUM": typed("int", 3), "ROUTE_REMAIN_DIS": typed("int", 1234),
            "CAR_LATITUDE": typed("double", 0.0), "CAR_LONGITUDE": typed("double", 0.0),
            "CUR_ROAD_NAME": typed("string", "合成测试道路"), "nullable": typed("null", None)}
  raw = {"action": "AUTONAVI_STANDARD_BROADCAST_SEND", "targetPackage": None, "component": None,
         "complete": True, "truncated": False, "issues": [],
         "extras": {"type": "bundle", "value": fields, "length": len(fields), "capturedLength": len(fields)}}
  record = {"keyType": int(key), "sequence": sequence, "receivedElapsedNs": source,
            "receivedWallMs": 10000, "complete": True, "decodeErrors": 0, "senderVerified": False,
            "sourceAgeMs": (published - source) // 1_000_000, "rawIntent": raw}
  return {"schemaVersion": 1, "sessionId": session, "status": "receiving", "reason": "receiver_registered",
          "active": True, "valid": True, "activeRouteVerified": False, "controlInputAllowed": False,
          "senderVerified": False, "freshnessChecked": False, "sequence": sequence, "written": sequence,
          "ignored": 0, "startedElapsedNs": source - 1_000_000_000, "publishedElapsedNs": published,
          "startedWallMs": 9000, "publishedWallMs": 10010, "maxDurationMs": bridge.MAX_DURATION_MS,
          "maxFileBytes": bridge.MAX_FILE_BYTES, "lastError": "", "records": {key: record}}


def window(*, source=SOURCE, age_ms=20, host_start=5_000_000_000_000):
  return bridge.PhoneReadWindow(source + (age_ms - 5) * 1_000_000, source + age_ms * 1_000_000,
                                host_start, host_start + 25_000_000, BOOT)


def encode(value):
  return json.dumps(value, ensure_ascii=False).encode()


def observe(value=None, clock=None, observer=None):
  return (observer or bridge.BridgeObserver()).observe(encode(value or fixture()), clock or window())


def test_fresh_typed_broadcast_is_observation_only_and_zero_position_is_not_repaired():
  value = observe()
  assert value["status"] == "observed"
  record = value["records"]["10001"]
  assert record["fields"]["CAR_LATITUDE"] == 0.0
  assert record["fields"]["CUR_SEG_NUM"] == 3
  assert record["typed_fields"]["CAR_LATITUDE"]["type"] == "double"
  assert record["source_age_upper_ns"] == 30_000_000
  assert record["conservative_host_received_ns"] == window().host_started_ns - 30_000_000
  assert not value["active_route_verified"] and not value["control_input_allowed"]
  assert "active_path_id" not in value


@pytest.mark.parametrize("age_ms,status", [(489, "observed"), (490, "observed"), (491, "unavailable"), (3000, "unavailable")])
def test_source_age_uses_phone_upper_bound_with_resolution(age_ms, status):
  value = observe(clock=window(age_ms=age_ms))
  assert value["status"] == status
  assert value["records"]["10001"]["fresh"] == (status == "observed")


@pytest.mark.parametrize("value", [0, -1, 3.1, True, float("nan"), float("inf"), "0.5"])
def test_freshness_limit_cannot_exceed_three_seconds_or_use_invalid_values(value):
  with pytest.raises(ValueError):
    bridge.BridgeObserver(value)


def test_phone_and_host_absolute_monotonic_values_are_never_compared():
  source = 5_000_000_000_000_000_000
  value = observe(fixture(source=source), window(source=source, host_start=1_000_000_000))
  assert value["status"] == "observed"
  assert value["records"]["10001"]["source_age_upper_ns"] == 30_000_000


def test_repeated_session_sequence_never_renews_host_source_stamp():
  observer = bridge.BridgeObserver()
  first = observe(observer=observer)
  value = fixture(published=SOURCE + 30_000_000)
  second = observe(value, window(age_ms=40, host_start=window().host_started_ns + 2_000_000_000), observer)
  assert second["status"] == "unavailable"
  assert second["records"]["10001"]["replayed"]
  assert (first["records"]["10001"]["conservative_host_received_ns"]
          == second["records"]["10001"]["conservative_host_received_ns"])


def test_unchanged_fresh_replay_ages_from_the_original_host_anchor():
  observer = bridge.BridgeObserver()
  first = observe(observer=observer)
  second_window = window(age_ms=40, host_start=window().host_started_ns + 250_000_000)
  second = observe(fixture(published=SOURCE + 30_000_000), second_window, observer)

  assert second["status"] == "observed"
  record = second["records"]["10001"]
  assert record["replayed"] is True
  assert record["source_age_upper_ns"] == 305_000_000
  assert (record["conservative_host_received_ns"]
          == first["records"]["10001"]["conservative_host_received_ns"])


def test_same_sequence_cannot_change_typed_payload():
  observer = bridge.BridgeObserver()
  observe(observer=observer)
  value = fixture()
  value["records"]["10001"]["rawIntent"]["extras"]["value"]["CUR_SEG_NUM"]["value"] = 4
  assert observe(value, observer=observer)["reason"] == "same_sequence_content_changed"


def test_new_sequence_with_new_source_time_can_advance_observation():
  observer = bridge.BridgeObserver()
  first = observe(observer=observer)
  advanced = SOURCE + 1_000_000_000
  second = observe(fixture(source=advanced, sequence=2), window(source=advanced, host_start=window().host_started_ns + 1_000_000_000), observer)
  assert second["status"] == "observed" and not second["records"]["10001"]["replayed"]
  assert second["records"]["10001"]["conservative_host_received_ns"] > first["records"]["10001"]["conservative_host_received_ns"]


def test_root_sequence_regression_is_rejected():
  observer = bridge.BridgeObserver()
  observe(fixture(sequence=2), observer=observer)
  assert observe(observer=observer)["reason"] == "receiver_sequence_regressed"


def test_record_source_time_cannot_regress_when_sequence_advances():
  observer = bridge.BridgeObserver()
  observe(observer=observer)
  assert observe(fixture(source=SOURCE - 1, sequence=2), observer=observer)["reason"] == "record_sequence_or_time_regressed"


def test_new_session_can_reset_sequence_but_retired_session_cannot_reappear():
  observer = bridge.BridgeObserver()
  observe(observer=observer)
  new = "33333333-3333-4333-8333-333333333333"
  assert observe(fixture(session=new), observer=observer)["status"] == "observed"
  assert observe(observer=observer)["reason"] == "retired_receiver_session_replayed"


def test_missing_10001_cannot_be_substituted_by_10019_state():
  value = observe(fixture(key="10019"))
  assert value["status"] == "unavailable" and value["reason"] == "10001_missing"
  assert value["records"]["10019"]["fresh"]


@pytest.mark.parametrize("field,value,reason", [
  ("schemaVersion", 2, "unsupported_bridge_schema"),
  ("schemaVersion", True, "unsupported_bridge_schema"),
  ("sessionId", "arbitrary-session", "invalid_receiver_session"),
  ("active", False, "receiver_inactive_or_invalid"),
  ("valid", False, "receiver_inactive_or_invalid"),
  ("reason", "queue_overflow", "receiver_failed"),
  ("lastError", "write_failed", "receiver_failed"),
  ("activeRouteVerified", True, "unexpected_bridge_trust_claim"),
  ("senderVerified", True, "unexpected_bridge_trust_claim"),
  ("freshnessChecked", True, "unexpected_bridge_trust_claim"),
  ("queueOverflow", 1, "bridge_loss_reported"),
  ("dropped", 1, "bridge_loss_reported"),
  ("maxDurationMs", bridge.MAX_DURATION_MS + 1, "invalid_session_duration"),
  ("maxFileBytes", bridge.MAX_FILE_BYTES + 1, "invalid_bridge_limit"),
])
def test_malformed_or_invalid_receiver_is_rejected(field, value, reason):
  root = fixture()
  root[field] = value
  assert observe(root)["reason"] == reason


@pytest.mark.parametrize("field,value,reason", [
  ("keyType", 10019, "record_type_mismatch"),
  ("keyType", 10001.0, "record_type_mismatch"),
  ("sequence", 2, "invalid_record_sequence"),
  ("sequence", True, "invalid_record_sequence"),
  ("receivedElapsedNs", SOURCE + 20_000_000, "invalid_source_time"),
  ("sourceAgeMs", 0, "invalid_source_age"),
  ("complete", False, "record_incomplete"),
  ("decodeErrors", 1, "record_incomplete"),
])
def test_record_integrity_boundaries(field, value, reason):
  root = fixture()
  root["records"]["10001"][field] = value
  assert observe(root)["reason"] == reason


@pytest.mark.parametrize("field,value", [("complete", False), ("truncated", True),
                                        ("issues", [{"reason": "depth_limit"}]), ("action", "OTHER_ACTION")])
def test_raw_intent_decode_issues_are_never_promoted(field, value):
  root = fixture()
  root["records"]["10001"]["rawIntent"][field] = value
  assert observe(root)["reason"] == "intent_incomplete"


@pytest.mark.parametrize("node,reason", [
  ({"type": "double", "value": 1.0, "bits": "0"}, "typed_float_bits_invalid"),
  ({"type": "double", "value": "NaN", "bits": "7ff8000000000000"}, "unsupported_typed_value"),
  ({"type": "int", "value": True}, "typed_integer_invalid"),
  ({"type": "array", "value": [], "length": 1, "capturedLength": 0}, "typed_collection_incomplete"),
  ({"type": "unsupported", "reason": "unsupported_parcelable"}, "unsupported_typed_value"),
])
def test_typed_values_remain_exact_and_complete(node, reason):
  root = fixture()
  root["records"]["10001"]["rawIntent"]["extras"]["value"]["CAR_LATITUDE"] = node
  assert observe(root)["reason"] == reason


def test_json_duplicate_keys_nonfinite_and_size_are_rejected():
  observer = bridge.BridgeObserver()
  assert observer.observe(b'{"schemaVersion":1,"schemaVersion":1}', window())["reason"] == "duplicate_json_key"
  assert observer.observe(b'{"x":NaN}', window())["reason"] == "nonfinite_json_number"
  assert observer.observe(b" " * (bridge.MAX_FILE_BYTES + 1), window())["reason"] == "bridge_file_size_limit"


@pytest.mark.parametrize("clock,reason", [
  (replace(window(), phone_after_ns=SOURCE), "invalid_clock_window"),
  (replace(window(), host_finished_ns=0), "invalid_clock_window"),
  (replace(window(), phone_after_ns=SOURCE + 2_000_000_000), "inconsistent_clock_window"),
  (replace(window(), device_boot_id="bad"), "invalid_clock_window"),
])
def test_read_clock_bracket_is_checked(clock, reason):
  assert observe(clock=clock)["reason"] == reason


def device():
  return SimpleNamespace(adb="/fixed/adb", port=5039, serial="synthetic-device")


def test_read_latest_uses_fixed_run_as_path_bounded_head_and_raw_no_root_channel():
  calls = []
  data = encode(fixture())

  def runner(args, **kwargs):
    calls.append((args, kwargs))
    return SimpleNamespace(stdout=bridge.PREFIX + data + b"\nNAVDIAG_BRIDGE_RC=000\n", returncode=0)

  assert bridge.read_latest(device(), runner=runner) == data
  args, kwargs = calls[0]
  assert args[:9] == ["/fixed/adb", "-H", "127.0.0.1", "-P", "5039", "-s", "synthetic-device", "shell", "-T"]
  assert "run-as " + bridge.PACKAGE in args[-1]
  assert "head -c 2097153 " + bridge.LATEST_PATH in args[-1]
  assert "su " not in args[-1] and "start" not in args[-1]
  assert kwargs == {"capture_output": True, "timeout": 5, "check": False}


@pytest.mark.parametrize("raw,returncode,reason", [
  (bridge.PREFIX + b"x" + b"\nNAVDIAG_BRIDGE_RC=044\n", 44, "bridge_read_failed"),
  (b"wrong", 0, "bridge_read_failed"),
  (bridge.PREFIX + b"x" * (bridge.MAX_FILE_BYTES + 1) + b"\nNAVDIAG_BRIDGE_RC=000\n", 0, "bridge_file_size_limit"),
])
def test_read_latest_rejects_missing_changed_framing_and_oversize(raw, returncode, reason):
  with pytest.raises(bridge.BridgeError, match=reason):
    bridge.read_latest(device(), runner=lambda *_, **__: SimpleNamespace(stdout=raw, returncode=returncode))


def test_phone_uptime_is_decimal_and_has_explicit_resolution():
  sample = bridge.read_phone_clock(device(), runner=lambda *_, **__: SimpleNamespace(
    returncode=0, stdout=(BOOT + "\n123.45 678.90\n").encode()))
  assert sample == bridge.PhoneClockSample(123_450_000_000, BOOT, 10_000_000)


def test_injected_device_observation_uses_own_two_clock_domains_and_does_not_start_services():
  phone = iter([bridge.PhoneClockSample(window().phone_before_ns, BOOT), bridge.PhoneClockSample(window().phone_after_ns, BOOT)])
  host = iter([window().host_started_ns, window().host_finished_ns])
  value = bridge.observe_device(None, bridge.BridgeObserver(), read=lambda _: encode(fixture()),
                                phone_clock=lambda _: next(phone), host_clock=lambda: next(host))
  assert value["status"] == "observed"


def test_phone_reboot_inside_read_invalidates_entire_observation():
  phone = iter([bridge.PhoneClockSample(SOURCE, BOOT), bridge.PhoneClockSample(1, SESSION)])
  value = bridge.observe_device(None, bridge.BridgeObserver(), read=lambda _: encode(fixture()),
                                phone_clock=lambda _: next(phone))
  assert value["reason"] == "phone_reboot_during_read"


def test_mutating_returned_typed_fields_does_not_change_replay_fingerprint():
  observer = bridge.BridgeObserver()
  data = fixture()
  first = observe(data, observer=observer)
  first["records"]["10001"]["typed_fields"]["CUR_SEG_NUM"]["value"] = 999
  assert observe(copy.deepcopy(data), observer=observer)["status"] == "observed"
