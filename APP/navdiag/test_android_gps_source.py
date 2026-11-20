import hashlib
import json
import shlex
import signal
import subprocess
from types import SimpleNamespace

import pytest

import android_gps_source as gps
from test_native_route_source import FakeProcess


BOOT = "01234567-89ab-cdef-0123-456789abcdef"
APK = "/data/app/com.jixiexiaoge.drivingassist.navdiag-test123==/base.apk"
BEFORE = 100_000_000_000
AFTER = 100_001_000_000
SOURCE = 99_800_000_000
HOST = 1_000_000_000


def observation(**changes):
  result = {"schema_version": 1, "source": "android_fine_last_known_gps", "status": "observed",
            **dict.fromkeys(gps._FALSE_FLAGS, False), "provider": "gps", "longitude": 11.25, "latitude": 22.5,
            "horizontal_accuracy_m": 1.0, "is_from_mock_provider": False, "phone_boot_id": BOOT,
            "phone_before_ns": BEFORE, "phone_after_ns": AFTER, "elapsed_realtime_ns": SOURCE,
            "source_token": hashlib.sha256(f"android-gps-location-v1\x00{BOOT}\x00{SOURCE}".encode()).hexdigest(),
            "age_lower_ns": BEFORE - SOURCE, "age_upper_ns": AFTER - SOURCE, "source_elapsed_time_fresh": True}
  result.update(changes)
  return result


def encoded(value):
  return json.dumps(value).encode()


def parse(value, *, finish=HOST + 10_000_000):
  return gps.parse_gps_probe(encoded(value), HOST, finish)


def test_private_fix_uses_source_time_and_conservative_complete_host_window():
  fix = parse(observation())
  assert fix.coordinates == (11.25, 22.5)
  assert fix.age_lower_ns == 200_000_000 and fix.age_upper_ns == 211_000_000
  assert fix.fresh_at(fix.host_finished_ns + 1_788_999_999)
  assert not fix.fresh_at(fix.host_finished_ns + 1_789_000_000)
  assert not fix.fresh_at(fix.host_started_ns)
  summary = fix.summary()
  assert summary["source_elapsed_time_fresh"] and summary["coordinates_available_privately"]
  assert all(summary[key] is False for key in gps._FALSE_FLAGS)
  assert BOOT not in repr(fix) and "11.25" not in repr(fix)
  assert "longitude" not in summary and "latitude" not in summary and "elapsed_realtime_ns" not in summary


def bridge_observation(**changes):
  source_ns = SOURCE
  result = {
    "schema_version": 1, "source": "android_fine_live_gps_bridge", "status": "observed",
    "reason": "fresh_gps", **dict.fromkeys(gps._FALSE_FLAGS, False),
    "session_id": "0" * 32, "sequence": 7, "callback_count": 7, "gps_callback_count": 7,
    "passive_callback_count": 0,
    "provider": "gps", "service_started_realtime_ns": HOST,
    "service_updated_realtime_ns": BEFORE, "source_elapsed_realtime_ns": source_ns,
    "callback_age_ns": BEFORE - source_ns, "service_thread_alive": True,
    "latitude": 22.5, "longitude": 11.25, "horizontal_accuracy_m": 1.0,
    "source_token": hashlib.sha256(f"android-gps-location-v1\x00{BOOT}\x00{source_ns}".encode()).hexdigest(),
  }
  result.update(changes)
  return result


def parse_bridge(value, *, ended=HOST + 10_000_000):
  return gps.parse_gps_bridge(encoded(value), BOOT, HOST, ended)


def test_app_uid_bridge_fix_is_bounded_and_never_publicly_exposes_coordinates():
  fix = parse_bridge(bridge_observation())
  assert fix.coordinates == (11.25, 22.5)
  assert fix.age_lower_ns == 200_000_000 and fix.age_upper_ns == 210_000_000
  assert fix.summary()["source"] == "android_fine_live_gps_bridge"
  assert fix.summary()["bridge_source_verified"] is True
  assert all(fix.summary()[key] is False for key in gps._FALSE_FLAGS)
  assert "11.25" not in str(fix.summary()) and "22.5" not in str(fix.summary())


@pytest.mark.parametrize("changes,reason", [
  ({"provider": "fused"}, "gps_bridge_live_source_invalid"),
  ({"passive_callback_count": 1}, "gps_bridge_live_source_invalid"),
  ({"source_token": "0" * 64}, "gps_bridge_source_identity_invalid"),
  ({"callback_age_ns": BEFORE - SOURCE + 1}, "gps_bridge_live_source_invalid"),
  ({"horizontal_accuracy_m": 26}, "gps_accuracy_unusable"),
  ({"latitude": 91}, "gps_coordinates_invalid"),
  ({"control_input_allowed": True}, "gps_bridge_schema_invalid"),
])
def test_app_uid_bridge_rejects_identity_gap_and_untrusted_claims(changes, reason):
  with pytest.raises(gps.GpsBridgeRejected, match="^" + reason + "$"):
    parse_bridge(bridge_observation(**changes))


def test_app_uid_bridge_maps_only_whitelisted_transient_reasons():
  value = bridge_observation(status="unavailable", reason="gps_bridge_source_stale")
  for key in ("latitude", "longitude", "horizontal_accuracy_m", "source_token"):
    value.pop(key)
  with pytest.raises(gps.GpsBridgeRejected, match="^gps_source_stale$") as error:
    parse_bridge(value)
  assert error.value.live_source["callback_count"] == 7
  assert error.value.live_source["gps_callback_count"] == 7
  assert error.value.live_source["passive_callback_count"] == 0


def test_app_uid_bridge_reports_gps_and_passive_callbacks_separately():
  fix = parse_bridge(bridge_observation(gps_callback_count=4, passive_callback_count=3))
  assert fix.live_source["callback_count"] == 7
  assert fix.live_source["gps_callback_count"] == 4
  assert fix.live_source["passive_callback_count"] == 3


def test_starting_bridge_is_whitelisted_as_empty_not_protocol_corruption():
  value = bridge_observation(status="starting", reason="waiting_for_gps", sequence=0, callback_count=0,
                             gps_callback_count=0, service_updated_realtime_ns=HOST,
                             source_elapsed_realtime_ns=0, callback_age_ns=-1)
  for key in ("latitude", "longitude", "horizontal_accuracy_m", "source_token"):
    value.pop(key)
  with pytest.raises(gps.GpsBridgeRejected, match="^gps_cache_empty$") as error:
    parse_bridge(value)
  assert error.value.live_source["callback_count"] == 0


def test_starting_status_requires_exact_minus_one_age():
  value = bridge_observation(status="starting", reason="waiting_for_gps", callback_age_ns=0)
  for key in ("latitude", "longitude", "horizontal_accuracy_m", "source_token"):
    value.pop(key)
  with pytest.raises(gps.GpsBridgeRejected, match="^gps_bridge_live_source_invalid$"):
    parse_bridge(value)


def test_app_uid_bridge_failure_must_not_retain_coordinates():
  value = bridge_observation(status="unavailable", reason="gps_bridge_provider_invalid")
  with pytest.raises(gps.GpsBridgeRejected, match="gps_bridge_failure_invalid"):
    parse_bridge(value)


def test_app_uid_bridge_rejects_arbitrary_failure_text_and_coordinates():
  value = bridge_observation(status="unavailable", reason="gps_bridge_source_stale", latitude=22.5)
  with pytest.raises(gps.GpsBridgeRejected, match="gps_bridge_failure_invalid"):
    parse_bridge(value)
  value = bridge_observation(status="unavailable", reason="denied for location 22.5")
  for key in ("latitude", "longitude", "horizontal_accuracy_m", "source_token"):
    value.pop(key)
  with pytest.raises(gps.GpsBridgeRejected, match="gps_bridge_schema_invalid"):
    parse_bridge(value)


def test_reread_same_fix_does_not_refresh_token_or_source_age():
  first = parse(observation())
  later = observation(phone_before_ns=BEFORE + 1_900_000_000, phone_after_ns=AFTER + 1_900_000_000,
                      age_lower_ns=BEFORE - SOURCE + 1_900_000_000, age_upper_ns=AFTER - SOURCE + 1_900_000_000)
  with pytest.raises(gps.GpsProbeRejected, match="gps_source_stale"):
    parse(later)
  later = observation(phone_before_ns=BEFORE + 100_000_000, phone_after_ns=AFTER + 100_000_000,
                      age_lower_ns=BEFORE - SOURCE + 100_000_000, age_upper_ns=AFTER - SOURCE + 100_000_000)
  second = parse(later)
  assert first.source_token == second.source_token
  assert second.age_upper_ns == first.age_upper_ns + 100_000_000


@pytest.mark.parametrize("changes", [
  {"schema_version": True}, {"schema_version": 2}, {"source": "fused"}, {"provider": "fused"},
  {"status": "trusted"}, {"is_from_mock_provider": True}, {"is_from_mock_provider": 0},
  {"phone_boot_id": "invalid"}, {"phone_before_ns": True}, {"phone_after_ns": BEFORE - 1},
  {"elapsed_realtime_ns": AFTER + 1}, {"elapsed_realtime_ns": 0}, {"source_token": "0" * 64},
  {"age_lower_ns": True}, {"age_lower_ns": 0}, {"age_upper_ns": 0}, {"source_elapsed_time_fresh": 1},
  {"longitude": float("nan")}, {"latitude": float("inf")}, {"longitude": True}, {"latitude": 91},
  {"longitude": 181}, {"horizontal_accuracy_m": 0}, {"horizontal_accuracy_m": 26},
  {"horizontal_accuracy_m": True}, {"horizontal_accuracy_m": float("inf")},
  {"raw_path": "/unapproved"}, {"control_input_allowed": True},
  {"gnss_hardware_authenticity_verified": True}, {"amap_native_pose_freshness_verified": True},
  {"coordinate_datum_globally_verified": True},
])
def test_malformed_sources_and_unproven_claims_fail_closed(changes):
  with pytest.raises(gps.GpsProbeRejected):
    parse(observation(**changes))


@pytest.mark.parametrize("missing", ["is_from_mock_provider", "elapsed_realtime_ns", "phone_boot_id", "latitude",
                                      "longitude", "horizontal_accuracy_m"])
def test_missing_mock_or_source_fields_are_never_defaulted(missing):
  value = observation()
  value.pop(missing)
  with pytest.raises(gps.GpsProbeRejected, match="gps_probe_fix_invalid"):
    parse(value)


@pytest.mark.parametrize("raw", [b"\xff", b"[]", b"{}", b"{broken}", b'{"x":NaN}',
                                b'{"schema_version":1,"schema_version":1}', b"x" * 4097])
def test_non_json_duplicate_nonfinite_and_oversize_fail_without_raw_message(raw):
  with pytest.raises(gps.GpsProbeRejected) as error:
    gps.parse_gps_probe(raw, HOST, HOST + 10_000_000)
  assert raw[:8].decode("ascii", "ignore") not in str(error.value) or len(raw) < 8


def test_host_window_phone_duration_and_source_age_are_independently_checked():
  for started, finished in ((HOST + 1, HOST), (0, HOST), (HOST, HOST + gps.MAX_WINDOW_NS + 1)):
    with pytest.raises(gps.GpsProbeRejected):
      gps.parse_gps_probe(encoded(observation()), started, finished)
  with pytest.raises(gps.GpsProbeRejected, match="phone_clock"):
    parse(observation(phone_after_ns=BEFORE + 20_000_000))
  value = observation(elapsed_realtime_ns=BEFORE - 1_990_000_000,
                      age_lower_ns=1_990_000_000, age_upper_ns=1_991_000_000)
  value["source_token"] = hashlib.sha256(f"android-gps-location-v1\x00{BOOT}\x00{value['elapsed_realtime_ns']}".encode()).hexdigest()
  with pytest.raises(gps.GpsProbeRejected, match="gps_source_stale"):
    parse(value)


def unavailable(reason="gps_cache_security_rejected", **changes):
  result = {"schema_version": 1, "source": "android_fine_last_known_gps", "status": "unavailable",
            "reason": reason, **dict.fromkeys(gps._FALSE_FLAGS, False)}
  result.update(changes)
  return result


def live_source(**changes):
  result = {"registration": "request_accepted", "gps_registration": "request_accepted",
            "passive_registration": "request_accepted", "callback_count": 7,
            "gps_callback_count": 3, "passive_callback_count": 4, "cache_read_count": 9,
            "callbacks_during_observation": 7, "observation_elapsed_ms": 120,
            "callback_age_ms": 120, "last_read_age_ms": 130, "callback_thread_alive": True}
  result.update(changes)
  return result


def test_root_security_rejection_preserves_fixed_reason_but_never_accepts_partial_coordinates():
  with pytest.raises(gps.GpsProbeRejected, match="^gps_cache_security_rejected$"):
    parse(unavailable())
  with pytest.raises(gps.GpsProbeRejected, match="failure_invalid"):
    parse(unavailable(longitude=11.25))
  with pytest.raises(gps.GpsProbeRejected, match="failure_invalid"):
    parse(unavailable(reason="denied for location 11.25"))
  with pytest.raises(gps.GpsProbeRejected, match="^gps_cache_security_rejected$") as error:
    parse(unavailable(_private_security_exception="private uid/package reason 11.25"))
  assert "11.25" not in str(error.value)
  with pytest.raises(gps.GpsProbeRejected, match="failure_invalid"):
    parse(unavailable("gps_source_stale", _private_security_exception="not a security error"))


def test_live_source_diagnostics_are_validated_and_attached_to_rejection():
  with pytest.raises(gps.GpsProbeRejected, match="^gps_source_stale$") as error:
    parse(unavailable("gps_source_stale", _live_source=live_source()))
  assert error.value.live_source == live_source()
  assert "latitude" not in repr(error.value.live_source)
  assert "source_token" not in repr(error.value.live_source)
  with pytest.raises(ValueError, match="gps_probe_live_source_invalid"):
    gps.GpsProbeRejected("gps_source_stale", live_source(latitude=22.5))


def test_live_source_diagnostics_are_attached_to_successful_fix():
  fix = parse(observation(_live_source=live_source()))
  assert fix.live_source == live_source()
  assert fix.summary()["live_source"] == live_source()


@pytest.mark.parametrize("changes", [
  {"registration": "unknown"}, {"gps_registration": "unknown"}, {"passive_registration": "unknown"},
  {"callback_count": True}, {"callback_count": -1}, {"gps_callback_count": -1},
  {"passive_callback_count": 8}, {"passive_registration": "not_registered"},
  {"cache_read_count": 144_001}, {"callback_age_ms": -2}, {"last_read_age_ms": -2},
  {"callbacks_during_observation": -1}, {"callbacks_during_observation": 8},
  {"observation_elapsed_ms": -1},
  {"callback_thread_alive": False}, {"latitude": 22.5},
])
def test_invalid_live_source_diagnostics_fail_closed(changes):
  with pytest.raises(gps.GpsProbeRejected):
    parse(unavailable("gps_source_stale", _live_source=live_source(**changes)))


def test_observed_stale_age_is_diagnostics_not_a_source_freshness_claim():
  value = unavailable("gps_source_stale", _live_source=live_source(last_read_age_ms=9_800))
  with pytest.raises(gps.GpsProbeRejected, match="^gps_source_stale$") as error:
    parse(value)
  assert error.value.live_source["last_read_age_ms"] == 9_800


@pytest.fixture
def setup(monkeypatch):
  processes, launches, cleanup, killed = [], [], [], []
  options = {"responses": [encoded(observation()) + b"\n"], "header": b"NAVDIAG_GPS_V1 4444 5555\n",
             "hang_quit": False, "pm": "package:" + APK + "\n", "cleanup_rc": 0}

  def shell(*args, **kwargs):
    if args[1].startswith("pm path "):
      return SimpleNamespace(stdout=options["pm"], returncode=0)
    cleanup.append((args, kwargs))
    return SimpleNamespace(stdout="", returncode=options["cleanup_rc"])

  def popen(args, **kwargs):
    launches.append((args, kwargs))
    process = FakeProcess(options["responses"], options["header"], options["hang_quit"])
    processes.append(process)
    return process

  def killpg(pid, sig):
    killed.append((pid, sig))
    for process in processes:
      if process.pid == pid:
        process.returncode = -sig

  monkeypatch.setattr(gps.subprocess, "Popen", popen)
  monkeypatch.setattr(gps.os, "killpg", killpg)
  monkeypatch.setattr(gps, "_RESPONSE_TIMEOUT_S", 0.01)
  monkeypatch.setattr(gps, "_START_TIMEOUT_S", 0.01)
  ticks = iter(range(HOST, HOST + 1_000_000_000, 10_000_000))
  monkeypatch.setattr(gps.time, "monotonic_ns", lambda: next(ticks))
  state = SimpleNamespace(options=options, processes=processes, launches=launches, cleanup=cleanup, killed=killed,
                          device=SimpleNamespace(adb="/fixed/adb", port=5039, serial="synthetic-device", shell=shell))
  yield state
  for process in processes:
    process.dispose()


def test_fixed_root_command_handshake_observe_and_clean_quit(setup):
  with gps.AndroidGpsProbeSource(setup.device) as source:
    args, kwargs = setup.launches[0]
    assert args[:9] == ["/fixed/adb", "-H", "127.0.0.1", "-P", "5039", "-s", "synthetic-device", "shell", "-T"]
    script = shlex.split(args[-1])
    assert script[:2] == ["su", "-c"] and len(script) == 3
    assert script[2].endswith("exec /system/bin/app_process /system/bin " + gps.PROBE_CLASS)
    assert "NAVDIAG_GPS_SESSION=" in script[2] and 'IFS= read -r navdiag_ack' in script[2]
    assert kwargs == {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE,
                      "stderr": subprocess.DEVNULL, "start_new_session": True}
    fix, raw, window = source.observe()
    assert fix.coordinates == (11.25, 22.5) and json.loads(raw)["longitude"] == 11.25
    assert window == {"host_started_ns": HOST, "host_finished_ns": HOST + 10_000_000}
  assert setup.processes[0].stdin.writes == [b"START\n", b"OBSERVE\n", b"QUIT\n"]
  assert source.cleanup_verified and not setup.cleanup and not setup.killed
  assert setup.processes[0].stdin.closed and setup.processes[0].stdout.closed


@pytest.mark.parametrize("raw,reason", [
  (b"{broken}\n", "json_invalid"), (b"{}\n{}\n", "unsolicited_output"),
  (b"x" * 4097, "result_too_large"), (None, "channel_timeout"), ("EOF", "channel_closed"),
])
def test_bad_protocol_closes_owned_worker_without_retry(setup, raw, reason):
  setup.options["responses"] = [raw]
  with gps.AndroidGpsProbeSource(setup.device) as source:
    with pytest.raises(gps.GpsProbeRejected, match=reason):
      source.observe()
    with pytest.raises(gps.GpsProbeRejected, match="source_not_started"):
      source.observe()
  assert source.cleanup_verified and len(setup.launches) == 1


def test_stale_replayed_source_does_not_keep_channel_alive(setup):
  setup.options["responses"] = [encoded(unavailable("gps_source_stale")) + b"\n"]
  with gps.AndroidGpsProbeSource(setup.device) as source:
    with pytest.raises(gps.GpsProbeRejected, match="^gps_source_stale$"):
      source.observe()
  assert source.cleanup_verified


@pytest.mark.parametrize("epoch_change", ["boot", "clock"])
def test_phone_epoch_or_clock_regression_is_rejected_across_observations(setup, epoch_change):
  second = observation()
  if epoch_change == "boot":
    second.update(phone_before_ns=BEFORE + 20_000_000, phone_after_ns=AFTER + 20_000_000,
                  age_lower_ns=BEFORE - SOURCE + 20_000_000, age_upper_ns=AFTER - SOURCE + 20_000_000)
    second["phone_boot_id"] = "01234567-89ab-cdef-0123-456789abcdee"
    second["source_token"] = hashlib.sha256(
      f"android-gps-location-v1\x00{second['phone_boot_id']}\x00{SOURCE}".encode()).hexdigest()
  setup.options["responses"].append(encoded(second) + b"\n")
  with gps.AndroidGpsProbeSource(setup.device) as source:
    source.observe()
    with pytest.raises(gps.GpsProbeRejected, match="epoch_regressed"):
      source.observe()
  assert source.cleanup_verified


def test_hung_quit_cleans_only_handshake_pid_generation_and_session(setup):
  setup.options["hang_quit"] = True
  with gps.AndroidGpsProbeSource(setup.device) as source:
    source.observe()
  script = shlex.split(setup.cleanup[0][0][1])[2]
  assert "navdiag_pid=4444" in script and "5555" in script
  assert "NAVDIAG_GPS_SESSION=" in script and source._token in script
  assert gps.PROBE_CLASS in script and 'kill -TERM "$navdiag_pid"' in script
  assert setup.killed == [(22222, signal.SIGTERM)] and source.cleanup_verified


def test_remote_cleanup_failure_is_never_reported_as_verified(setup):
  setup.options.update(hang_quit=True, cleanup_rc=76)
  source = gps.AndroidGpsProbeSource(setup.device)
  with pytest.raises(gps.GpsProbeRejected, match="cleanup_unverified"):
    with source:
      source.observe()
  assert not source.cleanup_verified


@pytest.mark.parametrize("header", [b"NAVDIAG_GPS_V1 0 5555\n", b"wrong\n", b"NAVDIAG_GPS_V1 4444 5555\nextra\n", None])
def test_invalid_handshake_never_sends_start_or_arbitrary_observe(setup, header):
  setup.options["header"] = header
  with pytest.raises(gps.GpsProbeRejected):
    with gps.AndroidGpsProbeSource(setup.device):
      pytest.fail("unexpected startup")
  assert b"START\n" not in setup.processes[0].stdin.writes and not setup.cleanup


def test_private_file_writer_creates_mode600_and_refuses_existing_file(tmp_path):
  path = tmp_path / "gps-private.json"
  gps.write_private(path, encoded(observation()))
  assert path.stat().st_mode & 0o777 == 0o600
  with pytest.raises(FileExistsError):
    gps.write_private(path, b"replacement")


def test_public_cli_only_prints_summary_and_writes_raw_privately(tmp_path, monkeypatch, capsys):
  output = tmp_path / "private"
  raw = encoded(observation())
  fix = parse(observation())

  class FakeContext:
    cleanup_verified = False
    def __init__(self, *_):
      pass
    def __enter__(self):
      return self
    def __exit__(self, *_):
      self.cleanup_verified = True
    def observe(self):
      return fix, raw, {"host_started_ns": HOST, "host_finished_ns": HOST + 10_000_000}

  monkeypatch.setattr(gps, "Device", FakeContext)
  monkeypatch.setattr(gps, "AndroidGpsProbeSource", FakeContext)
  monkeypatch.setattr(gps, "choose_adb", lambda _: "/fixed/adb")
  monkeypatch.setattr("sys.argv", ["android_gps_source.py", "--out", str(output)])
  gps.main()
  public = capsys.readouterr().out
  assert BOOT not in public and "11.25" not in public and "22.5" not in public and "latitude" not in public
  assert json.loads(public)["cleanup_verified"]
  assert (output / "gps-private.json").read_bytes() == raw + b"\n"
  assert output.stat().st_mode & 0o777 == 0o700
  assert all(path.stat().st_mode & 0o777 == 0o600 for path in output.iterdir())


def test_security_failure_cli_saves_private_message_without_public_echo(tmp_path, monkeypatch, capsys):
  output = tmp_path / "private-failure"
  raw = encoded(unavailable(_private_security_exception="synthetic private 11.25,22.5"))

  class FakeContext:
    cleanup_verified = False
    last_private_response = raw
    last_private_window = {"host_started_ns": HOST, "host_finished_ns": HOST + 10_000_000}
    def __init__(self, *_):
      pass
    def __enter__(self):
      return self
    def __exit__(self, *_):
      self.cleanup_verified = True
    def observe(self):
      raise gps.GpsProbeRejected("gps_cache_security_rejected")

  monkeypatch.setattr(gps, "Device", FakeContext)
  monkeypatch.setattr(gps, "AndroidGpsProbeSource", FakeContext)
  monkeypatch.setattr(gps, "choose_adb", lambda _: "/fixed/adb")
  monkeypatch.setattr("sys.argv", ["android_gps_source.py", "--out", str(output)])
  with pytest.raises(gps.GpsProbeRejected, match="^gps_cache_security_rejected$"):
    gps.main()
  public = capsys.readouterr().out
  assert "11.25" not in public and "synthetic private" not in public and "_private_security_exception" not in public
  report = json.loads(public)
  assert report["status"] == "unavailable" and report["reason"] == "gps_cache_security_rejected"
  assert report["cleanup_verified"] and (output / "gps-private.json").read_bytes() == raw + b"\n"
  assert all(path.stat().st_mode & 0o777 == 0o600 for path in output.iterdir())


@pytest.mark.parametrize("name", [".AndroidGpsProbe", gps.PACKAGE + ".AndroidGpsProbe"])
def test_read_only_gps_probe_can_never_be_manifest_application_component(tmp_path, name):
  from build import check_manifest
  path = tmp_path / "manifest.xml"
  path.write_text('<manifest xmlns:android="http://schemas.android.com/apk/res/android" package="' + gps.PACKAGE
                  + '"><application><activity android:name="' + name + '"/></application></manifest>')
  with pytest.raises(ValueError, match="app_process"):
    check_manifest(path)


def test_production_gps_source_is_app_uid_bridge_not_deprecated_root_probe():
  from APP.navdiag import phone_route_producer as producer
  assert issubclass(producer._GpsSource, producer.AndroidGpsBridgeSource)
  assert not hasattr(producer, "AndroidGpsProbeSource")


def test_gps_bridge_waits_for_service_and_initial_snapshot(monkeypatch):
  snapshots = [False, False, True]
  started, stopped, boots = [], [], []
  device = SimpleNamespace(serial="synthetic-phone")

  monkeypatch.setattr(gps, "_gps_bridge_running",
                      lambda _device, cleanup=False: False)
  monkeypatch.setattr(gps, "_gps_bridge_service_live", lambda _device: False)
  monkeypatch.setattr(gps, "_gps_bridge_snapshot_ready", lambda _device: snapshots.pop(0))
  monkeypatch.setattr(gps, "_clear_gps_bridge_snapshot", lambda _device: None)
  monkeypatch.setattr(gps, "_start_gps_bridge", lambda _device, _stop: started.append(True))
  monkeypatch.setattr(gps, "_stop_gps_bridge", lambda _device: stopped.append(True) or True)
  monkeypatch.setattr(gps, "_read_phone_boot", lambda _device: boots.append(True) or BOOT)
  monkeypatch.setattr(gps.time, "sleep", lambda _seconds: None)
  source = gps.AndroidGpsBridgeSource(device)
  with source:
    assert started == [True] and boots == [True]
  assert stopped == [True] and source.cleanup_verified


def test_gps_bridge_business_snapshot_is_sufficient_when_huawei_dumpsys_lags(monkeypatch):
  device = SimpleNamespace(serial="synthetic-phone")
  monkeypatch.setattr(gps, "_gps_bridge_service_live", lambda _device: False)
  monkeypatch.setattr(gps, "_gps_bridge_running", lambda _device, cleanup=False: False)
  monkeypatch.setattr(gps, "_gps_bridge_snapshot_ready", lambda _device: True)
  monkeypatch.setattr(gps, "_clear_gps_bridge_snapshot", lambda _device: None)
  monkeypatch.setattr(gps, "_start_gps_bridge", lambda _device, _stop: None)
  monkeypatch.setattr(gps, "_read_phone_boot", lambda _device: BOOT)
  monkeypatch.setattr(gps, "_stop_gps_bridge", lambda _device: True)
  source = gps.AndroidGpsBridgeSource(device)
  with source:
    pass
  assert source.cleanup_verified


def test_gps_bridge_commands_start_services_directly_without_opening_activity(monkeypatch):
  commands = []

  class Device:
    serial = "synthetic-phone"
    adb = "/fixed/adb"
    port = 5039
    def _command(self, args, *, timeout, check):
      commands.append(("start", args, timeout, check))
      return SimpleNamespace(returncode=0, stdout=(
        'Starting service: Intent { act=' + gps.GPS_BRIDGE_START + ' cmp='
        + gps.PACKAGE + '/.GpsBridgeService }\n'
        'Service started: Intent { act=' + gps.GPS_BRIDGE_START + ' cmp='
        + gps.PACKAGE + '/.GpsBridgeService }\n'))
    def shell(self, *args, **kwargs):
      commands.append(("stop", args, kwargs.get("timeout"), kwargs.get("check")))
      return SimpleNamespace(returncode=0, stdout=(
        'Starting service: Intent { act=' + gps.GPS_BRIDGE_STOP + ' cmp='
        + gps.PACKAGE + '/.GpsBridgeService }\n'
        'Service started: Intent { act=' + gps.GPS_BRIDGE_STOP + ' cmp='
        + gps.PACKAGE + '/.GpsBridgeService }\n'))

  device = Device()
  gps._start_gps_bridge(device, None)
  assert gps._stop_gps_bridge(device)
  expected_start = ["-s", device.serial, "shell", "am", "start-foreground-service",
                    "--user", "0", "-W", "-n", gps.PACKAGE + "/.GpsBridgeService",
                    "-a", gps.GPS_BRIDGE_START]
  assert commands[0][1] == expected_start
  assert commands[1][1] == ("am", "start-service", "--user", "0", "-W", "-n",
                            gps.PACKAGE + "/.GpsBridgeService", "-a", gps.GPS_BRIDGE_STOP)
  assert all("MainActivity" not in str(item) for item in commands)


@pytest.mark.parametrize("stdout", [
  "Service started: Intent { act=" + gps.GPS_BRIDGE_START + " cmp=other/.GpsBridgeService }\n",
  "Error: Requires permission not exported from uid 10144\n",
])
def test_gps_bridge_start_rejects_ambiguous_or_wrong_business_result(stdout):
  class Device:
    serial = "synthetic-phone"
    adb = "/fixed/adb"
    port = 5039
    def _command(self, *_args, **_kwargs):
      return SimpleNamespace(returncode=0, stdout=stdout)

  with pytest.raises(gps.GpsBridgeRejected, match="^gps_bridge_start_failed$"):
    gps._start_gps_bridge(Device(), None)


@pytest.mark.parametrize("stdout", [
  "Starting service: Intent { act=" + gps.GPS_BRIDGE_START + " cmp="
  + gps.PACKAGE + "/.GpsBridgeService }\n",
  "Starting service: Intent { act=" + gps.GPS_BRIDGE_START + " cmp="
  + gps.PACKAGE + "/.GpsBridgeService }\n"
  "Service started: Intent { act=" + gps.GPS_BRIDGE_START + " cmp="
  + gps.PACKAGE + "/.GpsBridgeService }\n",
])
def test_gps_bridge_accepts_huawei_starting_or_started_service_receipt(stdout):
  class Device:
    serial = "synthetic-phone"
    adb = "/fixed/adb"
    port = 5039
    def _command(self, *_args, **_kwargs):
      return SimpleNamespace(returncode=0, stdout=stdout)

  gps._start_gps_bridge(Device(), None)


def test_gps_bridge_readiness_requires_first_callback_not_starting_file(monkeypatch):
  values = [
    bridge_observation(status="starting", reason="waiting_for_gps", sequence=0, callback_count=0,
                       gps_callback_count=0, service_updated_realtime_ns=HOST,
                       source_elapsed_realtime_ns=0, callback_age_ns=-1),
    bridge_observation(),
  ]
  for key in ("latitude", "longitude", "horizontal_accuracy_m", "source_token"):
    values[0].pop(key)
  reads = []

  def read(_device, _stop):
    reads.append(True)
    return encoded(values.pop(0))

  monkeypatch.setattr(gps, "_read_gps_bridge", read)
  assert gps._gps_bridge_snapshot_ready(SimpleNamespace()) is False
  assert gps._gps_bridge_snapshot_ready(SimpleNamespace()) is True
  assert len(reads) == 2


def test_gps_bridge_startup_timeout_reports_bounded_starting_diagnostics(monkeypatch):
  starting = bridge_observation(status="starting", reason="waiting_for_gps", sequence=0,
                                callback_count=0, gps_callback_count=0,
                                service_updated_realtime_ns=HOST,
                                source_elapsed_realtime_ns=0, callback_age_ns=-1)
  for key in ("latitude", "longitude", "horizontal_accuracy_m", "source_token"):
    starting.pop(key)
  monkeypatch.setattr(gps, "_read_gps_bridge", lambda _device, _stop: encoded(starting))
  ticks = iter([0.0, 19.0, 20.0])
  monkeypatch.setattr(gps.time, "monotonic", lambda: next(ticks, 20.0))
  monkeypatch.setattr(gps.time, "sleep", lambda _seconds: None)
  with pytest.raises(gps.GpsBridgeRejected,
                     match="^gps_bridge_start_not_registered$") as error:
    gps._wait_for_gps_bridge_ready(SimpleNamespace(), None)
  assert error.value.live_source["callback_count"] == 0
  assert error.value.live_source["service_thread_alive"] is True
  assert gps._BRIDGE_READY_TIMEOUT_S == 20.0


def test_gps_bridge_clears_previous_snapshot_before_start(monkeypatch):
  calls = []

  class Device:
    def shell(self, *args, **kwargs):
      calls.append((args, kwargs))
      return SimpleNamespace(returncode=0)

  gps._clear_gps_bridge_snapshot(Device())
  assert calls == [(('run-as', gps.PACKAGE, 'sh', '-c',
                      "'rm -f files/navdiag-gps/latest.json files/navdiag-gps/latest.json.tmp'"),
                    {'timeout': 2, 'check': False})]


def test_gps_bridge_distinguishes_stale_record_and_live_service(monkeypatch):
  outputs = iter([
    "  * ServiceRecord{fixture u0 com.jixiexiaoge.drivingassist.navdiag/.GpsBridgeService}\n",
    "  * ServiceRecord{fixture u0 com.jixiexiaoge.drivingassist.navdiag/.GpsBridgeService}\n"
    "    isForeground=true\n",
  ])
  device = SimpleNamespace(shell=lambda *_args, **_kwargs: SimpleNamespace(
    returncode=0, stdout=next(outputs)))
  assert not gps._gps_bridge_service_live(device)
  assert gps._gps_bridge_service_live(device)


def test_gps_bridge_stop_accepts_huawei_starting_receipt_and_checks_action(monkeypatch):
  outputs = iter([
    "Starting service: Intent { act=" + gps.GPS_BRIDGE_STOP + " cmp="
    + gps.PACKAGE + "/.GpsBridgeService }\n",
    "Starting service: Intent { act=" + gps.GPS_BRIDGE_START + " cmp="
    + gps.PACKAGE + "/.GpsBridgeService }\n",
  ])
  device = SimpleNamespace(shell=lambda *_args, **_kwargs: SimpleNamespace(
    returncode=0, stdout=next(outputs)))
  assert gps._stop_gps_bridge(device)
  assert not gps._stop_gps_bridge(device)


def test_gps_bridge_clears_stale_shell_record_before_restart(monkeypatch):
  states = iter([True, False])
  stopped, starts = [], []
  device = SimpleNamespace(serial="synthetic-phone")
  monkeypatch.setattr(gps, "_gps_bridge_service_live", lambda _device: False)
  monkeypatch.setattr(gps, "_clear_gps_bridge_snapshot", lambda _device: None)
  monkeypatch.setattr(gps, "_gps_bridge_running",
                      lambda _device, cleanup=False: next(states) and not cleanup)
  monkeypatch.setattr(gps, "_stop_gps_bridge", lambda _device: stopped.append(True) or True)
  monkeypatch.setattr(gps, "_start_gps_bridge", lambda _device, _stop: starts.append(True))
  monkeypatch.setattr(gps, "_wait_for_gps_bridge_ready", lambda *_args: True)
  monkeypatch.setattr(gps, "_read_phone_boot", lambda _device: BOOT)
  monkeypatch.setattr(gps, "_stop_gps_bridge", lambda _device: stopped.append(True) or True)
  source = gps.AndroidGpsBridgeSource(device)
  with source:
    assert stopped == [True] and starts == [True]
  assert stopped == [True, True] and source.cleanup_verified


def test_gps_bridge_startup_failure_keeps_original_reason_after_cleanup_failure(monkeypatch):
  device = SimpleNamespace(serial="synthetic-phone")
  running = [False]
  monkeypatch.setattr(gps, "_gps_bridge_running",
                      lambda _device, cleanup=False: True if cleanup else running.pop(0))
  monkeypatch.setattr(gps, "_gps_bridge_service_live", lambda _device: False)
  monkeypatch.setattr(gps, "_gps_bridge_snapshot_ready", lambda _device: False)
  monkeypatch.setattr(gps, "_clear_gps_bridge_snapshot", lambda _device: None)
  monkeypatch.setattr(gps, "_start_gps_bridge", lambda _device, _stop: None)
  monkeypatch.setattr(gps, "_wait_for_gps_bridge_ready",
                      lambda *_args: (_ for _ in ()).throw(
                        gps.GpsBridgeRejected("gps_bridge_start_not_registered")))
  monkeypatch.setattr(gps, "_stop_gps_bridge", lambda _device: False)
  ticks = iter([0.0, 0.0, 0.0, 10.0])
  monkeypatch.setattr(gps.time, "monotonic", lambda: next(ticks, 5.0))
  monkeypatch.setattr(gps.time, "sleep", lambda _seconds: None)
  source = gps.AndroidGpsBridgeSource(device)
  with pytest.raises(gps.GpsBridgeRejected, match="^gps_bridge_start_not_registered$"):
    source.__enter__()
  assert not source.cleanup_verified
