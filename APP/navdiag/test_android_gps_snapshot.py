from copy import deepcopy

import pytest

from android_gps_snapshot import GpsSnapshotRejected, parse_elapsed_time, parse_gps_snapshot


def window():
  return {"host_started_ns": 10_000_000_000, "host_finished_ns": 10_050_000_000,
          "phone_before": {"boot_id": "01234567-89ab-cdef-0123-456789abcdef", "elapsed_ns": 100_800_000_000, "resolution_ns": 10_000_000},
          "phone_after": {"boot_id": "01234567-89ab-cdef-0123-456789abcdef", "elapsed_ns": 100_840_000_000, "resolution_ns": 10_000_000}}


def dump(*, provider="gps", et="+1m40s", accuracy="1", coordinates="31******,106******", extra=""):
  return "".join(["  Last Known Locations:\n",
                 "    fused: Location[fused 31******,106****** hAcc=1 et=+1s]\n",
                 f"    gps: Location[{provider} {coordinates} hAcc={accuracy} et={et} {extra}]\n",
                 "  Last Known Locations Coarse Intervals:\n",
                 "    gps: Location[gps 31******,106****** hAcc=1 et=+1m40s]\n",
                 "  gps provider:\n    useable=true\n  mStarted=true\n",
                 "  network provider:\n    useable=false\n  mStarted=false\n"])


def test_redacted_fine_gps_yields_conservative_age_but_never_coordinates_or_control():
  fix = parse_gps_snapshot(dump(), window())
  assert fix.age_lower_ns == 799_000_000 and fix.age_upper_ns == 900_000_000
  assert fix.horizontal_accuracy_m == 1
  assert fix.coordinates is None and not fix.coordinates_in_dump
  summary = fix.summary()
  assert summary["source_elapsed_time_fresh"]
  assert not summary["control_input_allowed"] and not summary["amap_native_pose_freshness_verified"]
  assert "01234567" not in repr(fix) and "+1m40s" not in repr(fix)


def test_freshness_expires_from_original_fix_not_from_new_parser_read():
  first = parse_gps_snapshot(dump(), window())
  assert first.fresh_at(11_149_999_999)
  assert not first.fresh_at(11_150_000_000)
  assert not first.fresh_at(9_000_000_000)
  later = window()
  for name in ("host_started_ns", "host_finished_ns"):
    later[name] += 2_000_000_000
  for name in ("phone_before", "phone_after"):
    later[name]["elapsed_ns"] += 2_000_000_000
  with pytest.raises(GpsSnapshotRejected, match="gps_source_stale"):
    parse_gps_snapshot(dump(), later)


def test_repeated_gps_token_is_stable_and_advancing_fix_changes_it():
  first = parse_gps_snapshot(dump(), window())
  second = parse_gps_snapshot(dump(et="+1m40s100ms"), window())
  assert first.source_token == parse_gps_snapshot(dump(), window()).source_token
  assert first.source_token != second.source_token


@pytest.mark.parametrize("text", ["1700000000", "2026-11-15", "-1s", "+1.5s", "+1s1s", "+1ms1s", "+", "+0ms", "+1hfoo"])
def test_unknown_wall_or_elapsed_time_never_passes(text):
  with pytest.raises(GpsSnapshotRejected):
    parse_elapsed_time(text)


def test_elapsed_time_includes_days_and_millisecond_resolution():
  assert parse_elapsed_time("+1d10h44m6s903ms") == 125_046_903_000_000


@pytest.mark.parametrize("change,reason", [
  ({"provider": "fused"}, "provider_or_format"), ({"et": "+1s"}, "source_stale"),
  ({"et": "+1m41s"}, "time_in_future"), ({"accuracy": "NaN"}, "accuracy_unusable"),
  ({"accuracy": "Inf"}, "accuracy_unusable"), ({"accuracy": "0"}, "accuracy_unusable"),
  ({"accuracy": "21"}, "accuracy_unusable"), ({"extra": "mock"}, "mock_gps"),
  ({"extra": "isMock=true"}, "mock_gps"), ({"extra": "isFromMockProvider=true"}, "mock_gps"),
])
def test_unusable_sources_are_rejected(change, reason):
  with pytest.raises(GpsSnapshotRejected, match=reason):
    parse_gps_snapshot(dump(**change), window())


def test_coarse_gps_and_fresh_fused_cannot_substitute_for_fine_gps():
  text = dump().replace("    gps: Location[gps 31******,106****** hAcc=1 et=+1m40s ]\n", "", 1)
  text = text.replace("fused 31******,106****** hAcc=1 et=+1s", "fused 31******,106****** hAcc=1 et=+1m40s")
  with pytest.raises(GpsSnapshotRejected, match="fine_gps_missing"):
    parse_gps_snapshot(text, window())


@pytest.mark.parametrize("replacement,reason", [("useable=false", "provider_unusable"), ("mStarted=false", "provider_not_started")])
def test_provider_must_be_actively_running(replacement, reason):
  before = "useable=true" if replacement.startswith("useable") else "mStarted=true"
  with pytest.raises(GpsSnapshotRejected, match=reason):
    parse_gps_snapshot(dump().replace(before, replacement), window())


def test_boot_change_and_impossible_read_window_are_rejected():
  for change in ("boot", "reverse", "too_long", "phone_ahead"):
    data = deepcopy(window())
    if change == "boot":
      data["phone_after"]["boot_id"] = "01234567-89ab-cdef-0123-456789abcdee"
    elif change == "reverse":
      data["host_started_ns"], data["host_finished_ns"] = data["host_finished_ns"], data["host_started_ns"]
    elif change == "too_long":
      data["host_finished_ns"] += 2_000_000_000
    else:
      data["phone_after"]["elapsed_ns"] += 200_000_000
    with pytest.raises(GpsSnapshotRejected):
      parse_gps_snapshot(dump(), data)


def test_clear_coordinates_require_explicit_request_and_are_absent_from_summary():
  text = dump(coordinates="31.123456,106.123456")
  assert parse_gps_snapshot(text, window()).coordinates is None
  fix = parse_gps_snapshot(text, window(), include_coordinates=True)
  assert fix.coordinates == (106.123456, 31.123456)
  assert "106.123456" not in repr(fix) and "31.123456" not in str(fix.summary())


def test_nested_bundle_location_cannot_override_gps_source_age():
  nested = "extras=Bundle[{nested=Location[fused 31******,106****** hAcc=1 et=+1m40s]}]"
  with pytest.raises(GpsSnapshotRejected, match="gps_source_stale"):
    parse_gps_snapshot(dump(et="+1s", extra=nested), window())


@pytest.mark.parametrize("text", [b"\xff", "\x00", "X" * (2 * 1024 * 1024 + 1)])
def test_malformed_or_unbounded_input_is_rejected(text):
  with pytest.raises(GpsSnapshotRejected):
    parse_gps_snapshot(text, window())
