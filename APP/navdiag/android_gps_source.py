#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import select
import shlex
import signal
import subprocess
import time
import uuid

if __package__:
  from .device import Device, PACKAGE, ROOT, adb_client_argv, choose_adb
  from .root_probe import write_private
else:
  from device import Device, PACKAGE, ROOT, adb_client_argv, choose_adb
  from root_probe import write_private


PROBE_CLASS = PACKAGE + ".AndroidGpsProbe"
GPS_BRIDGE_PATH = "files/navdiag-gps/latest.json"
GPS_BRIDGE_START = PACKAGE + ".GPS_BRIDGE_START"
GPS_BRIDGE_STOP = PACKAGE + ".GPS_BRIDGE_STOP"
GPS_COMMAND_RECEIVER = PACKAGE + "/.GpsBridgeCommandReceiver"
GPS_COMMAND_START = PACKAGE + ".GPS_BRIDGE_COMMAND_START"
GPS_COMMAND_STOP = PACKAGE + ".GPS_BRIDGE_COMMAND_STOP"
GPS_BRIDGE_MAX_BYTES = 16 * 1024
MAX_RESULT_BYTES = 4096
MAX_WINDOW_NS = 1_000_000_000
MAX_AGE_NS = 2_000_000_000
_START_TIMEOUT_S = 10.0
_RESPONSE_TIMEOUT_S = 3.0
_BRIDGE_START_TIMEOUT_S = 8.0
_BRIDGE_READY_TIMEOUT_S = 20.0
_APK_PATH = re.compile(r"/data/app/(?:~~[A-Za-z0-9_+=-]+/)?" + re.escape(PACKAGE)
                       + r"-[A-Za-z0-9_+=-]+/base\.apk")
_BOOT_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
_REASON = re.compile(r"[a-z][a-z0-9_]{0,79}")
_FALSE_FLAGS = {"gnss_hardware_authenticity_verified", "amap_native_pose_freshness_verified",
                "coordinate_datum_globally_verified", "control_input_allowed"}
_BASE = {"schema_version", "source", "status"} | _FALSE_FLAGS
_OBSERVED = {"provider", "latitude", "longitude", "horizontal_accuracy_m", "elapsed_realtime_ns",
             "is_from_mock_provider", "phone_before_ns", "phone_after_ns", "phone_boot_id", "source_token",
             "age_lower_ns", "age_upper_ns", "source_elapsed_time_fresh"}
_LIVE_SOURCE = {"registration", "gps_registration", "passive_registration", "callback_count",
                "gps_callback_count", "passive_callback_count", "cache_read_count",
                "callbacks_during_observation", "observation_elapsed_ms", "callback_age_ms",
                "last_read_age_ms", "callback_thread_alive"}


class GpsProbeRejected(ValueError):

  def __init__(self, reason: str, live_source: dict | None = None):
    super().__init__(reason)
    self.live_source = _validate_live_source(live_source) if live_source is not None else None


class GpsBridgeRejected(GpsProbeRejected):

  def __init__(self, reason: str, live_source: dict | None = None):
    ValueError.__init__(self, reason)
    self.live_source = live_source


def _integer(value, minimum=0, maximum=(1 << 63) - 1):
  return type(value) is int and minimum <= value <= maximum


def _finite(value):
  try:
    return type(value) in (int, float) and math.isfinite(value)
  except OverflowError:
    return False


def _object(pairs):
  value = {}
  for key, item in pairs:
    if key in value:
      raise GpsProbeRejected("gps_probe_duplicate_key")
    value[key] = item
  return value


def _constant(_):
  raise GpsProbeRejected("gps_probe_nonfinite_json")


def _validate_live_source(value) -> dict:
  registrations = {"not_registered", "request_accepted"}
  if (not isinstance(value, dict) or value.keys() != _LIVE_SOURCE
      or value.get("registration") not in registrations
      or value.get("gps_registration") not in registrations
      or value.get("passive_registration") not in registrations
      or not _integer(value.get("callback_count"), 0, 144_000)
      or not _integer(value.get("gps_callback_count"), 0, 144_000)
      or not _integer(value.get("passive_callback_count"), 0, 144_000)
      or not _integer(value.get("cache_read_count"), 0, 144_000)
      or not _integer(value.get("callbacks_during_observation"), 0, 144_000)
      or not _integer(value.get("observation_elapsed_ms"), 0)
      or value.get("callbacks_during_observation") > value.get("callback_count")
      or value.get("gps_callback_count") + value.get("passive_callback_count") != value.get("callback_count")
      or (value.get("gps_registration") == "request_accepted"
          or value.get("passive_registration") == "request_accepted")
      != (value.get("registration") == "request_accepted")
      or value.get("callback_thread_alive") is not True
      or any(not _integer(value.get(key), -1) for key in
             ("callback_age_ms", "last_read_age_ms"))):
    raise ValueError("gps_probe_live_source_invalid")
  return value


@dataclass(frozen=True)
class AndroidGpsFix:
  source_token: str
  horizontal_accuracy_m: float
  age_lower_ns: int
  age_upper_ns: int
  host_started_ns: int
  host_finished_ns: int
  phone_boot_id: str = field(repr=False)
  elapsed_realtime_ns: int = field(repr=False)
  coordinates: tuple[float, float] = field(repr=False)
  live_source: dict | None = field(default=None, repr=False)

  def fresh_at(self, now_ns: int) -> bool:
    return (_integer(now_ns, self.host_finished_ns)
            and self.age_upper_ns + now_ns - self.host_finished_ns < MAX_AGE_NS)

  def summary(self) -> dict:
    return {"source": "android_fine_last_known_gps", "clock": "phone_elapsed_realtime_nanos",
            "source_token": self.source_token, "horizontal_accuracy_m": self.horizontal_accuracy_m,
            "age_lower_ns": self.age_lower_ns, "age_upper_ns": self.age_upper_ns,
            "host_started_ns": self.host_started_ns, "host_finished_ns": self.host_finished_ns,
            "coordinates_available_privately": True,
            "source_elapsed_time_fresh": self.fresh_at(self.host_finished_ns),
            **({"live_source": self.live_source} if self.live_source is not None else {}),
            **dict.fromkeys(sorted(_FALSE_FLAGS), False)}


@dataclass(frozen=True)
class AndroidGpsBridgeFix(AndroidGpsFix):
  def summary(self) -> dict:
    result = super().summary()
    result["source"] = "android_fine_live_gps_bridge"
    result["bridge_source_verified"] = True
    return result


def _bridge_live_source(value: dict) -> dict:
  keys = ("session_id", "sequence", "callback_count", "gps_callback_count",
          "passive_callback_count", "provider",
          "service_started_realtime_ns", "service_updated_realtime_ns",
          "source_elapsed_realtime_ns", "callback_age_ns", "service_thread_alive")
  if (not isinstance(value, dict) or any(key not in value for key in keys)
      or not isinstance(value.get("session_id"), str) or re.fullmatch(r"[0-9a-f]{32}", value["session_id"]) is None
      or not _integer(value.get("sequence")) or not _integer(value.get("callback_count"))
      or not _integer(value.get("gps_callback_count"))
      or not _integer(value.get("passive_callback_count"))
      or value.get("gps_callback_count") + value.get("passive_callback_count")
      != value.get("callback_count")
      or value.get("provider") != "gps"
      or not _integer(value.get("service_started_realtime_ns"), 1)
      or not _integer(value.get("service_updated_realtime_ns"), value["service_started_realtime_ns"])
      or not _integer(value.get("source_elapsed_realtime_ns"))
      or not _integer(value.get("callback_age_ns"), -1)
      or (value.get("status") == "starting"
          and (value["source_elapsed_realtime_ns"] != 0 or value["callback_age_ns"] != -1))
      or (value.get("status") != "starting"
          and (value["callback_age_ns"] < 0
               or value["service_updated_realtime_ns"] - value["source_elapsed_realtime_ns"]
               != value["callback_age_ns"]))
      or value.get("service_thread_alive") is not True):
    raise GpsBridgeRejected("gps_bridge_live_source_invalid")
  return {key: value[key] for key in keys}


def parse_gps_bridge(raw: bytes, boot_id: str, host_started_ns: int, host_finished_ns: int) -> AndroidGpsBridgeFix:
  if (not isinstance(raw, bytes) or not 1 <= len(raw) <= GPS_BRIDGE_MAX_BYTES
      or not isinstance(boot_id, str) or _BOOT_ID.fullmatch(boot_id) is None
      or not _integer(host_started_ns, 1) or not _integer(host_finished_ns, host_started_ns)
      or host_finished_ns - host_started_ns > MAX_WINDOW_NS):
    raise GpsBridgeRejected("gps_bridge_host_window_or_size_invalid")
  try:
    value = json.loads(raw, object_pairs_hook=_object, parse_constant=_constant)
  except (ValueError, UnicodeError, RecursionError):
    raise GpsBridgeRejected("gps_bridge_json_invalid") from None
  base = {"schema_version", "source", "status", "reason"} | _FALSE_FLAGS | {
    "session_id", "sequence", "callback_count", "gps_callback_count",
    "passive_callback_count", "provider",
    "service_started_realtime_ns", "service_updated_realtime_ns",
    "source_elapsed_realtime_ns", "callback_age_ns", "service_thread_alive"}
  observed = base | {"latitude", "longitude", "horizontal_accuracy_m", "source_token"}
  if (not isinstance(value, dict) or type(value.get("schema_version")) is not int
      or value.get("schema_version") != 1 or value.get("source") != "android_fine_live_gps_bridge"
      or any(value.get(key) is not False for key in _FALSE_FLAGS)
      or not isinstance(value.get("reason"), str) or _REASON.fullmatch(value["reason"]) is None):
    raise GpsBridgeRejected("gps_bridge_schema_invalid")
  live = _bridge_live_source(value)
  if value.get("status") != "observed":
    if value["status"] == "starting":
      mapped = "gps_cache_empty"
    else:
      mapped = {"gps_bridge_source_stale": "gps_source_stale",
                "gps_bridge_accuracy_unusable": "gps_accuracy_unusable",
                "gps_bridge_fix_missing": "gps_cache_empty",
                "waiting_for_gps": "gps_cache_empty",
                "fresh_gps": "gps_bridge_status_invalid"}.get(value["reason"], value["reason"])
    if value.keys() != base or mapped == "gps_bridge_status_invalid":
      raise GpsBridgeRejected("gps_bridge_failure_invalid")
    raise GpsBridgeRejected(mapped, live)
  if value.keys() != observed:
    raise GpsBridgeRejected("gps_bridge_fix_invalid")
  source_ns = value["source_elapsed_realtime_ns"]
  expected = hashlib.sha256(f"android-gps-location-v1\x00{boot_id}\x00{source_ns}".encode("ascii")).hexdigest()
  if value.get("source_token") != expected:
    raise GpsBridgeRejected("gps_bridge_source_identity_invalid", live)
  accuracy, lon, lat = (value[key] for key in ("horizontal_accuracy_m", "longitude", "latitude"))
  if not _finite(accuracy) or not 0 < accuracy <= 25:
    raise GpsBridgeRejected("gps_accuracy_unusable", live)
  if not _finite(lon) or not _finite(lat) or not -180 <= lon <= 180 or not -90 <= lat <= 90:
    raise GpsBridgeRejected("gps_coordinates_invalid", live)
  lower = live["callback_age_ns"]
  upper = lower + host_finished_ns - host_started_ns
  if upper >= MAX_AGE_NS:
    raise GpsBridgeRejected("gps_source_stale", live)
  return AndroidGpsBridgeFix(expected, float(accuracy), lower, upper, host_started_ns,
                             host_finished_ns, boot_id, source_ns, (float(lon), float(lat)), live)


def parse_gps_probe(raw: bytes, host_started_ns: int, host_finished_ns: int) -> AndroidGpsFix:
  if (not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_RESULT_BYTES
      or not _integer(host_started_ns, 1) or not _integer(host_finished_ns, host_started_ns)
      or host_finished_ns - host_started_ns > MAX_WINDOW_NS):
    raise GpsProbeRejected("gps_probe_host_window_or_size_invalid")
  try:
    value = json.loads(raw, object_pairs_hook=_object, parse_constant=_constant)
  except (ValueError, UnicodeError, RecursionError):
    raise GpsProbeRejected("gps_probe_json_invalid") from None
  if (not isinstance(value, dict) or not _BASE <= value.keys()
      or type(value.get("schema_version")) is not int or value["schema_version"] != 1
      or value.get("source") != "android_fine_last_known_gps"
      or any(value.get(key) is not False for key in _FALSE_FLAGS)):
    raise GpsProbeRejected("gps_probe_schema_invalid")
  if value.get("status") == "unavailable":
    private = {"_private_security_exception"} if "_private_security_exception" in value else set()
    live = {"_live_source"} if "_live_source" in value else set()
    try:
      live_source = _validate_live_source(value["_live_source"]) if live else None
    except ValueError:
      raise GpsProbeRejected("gps_probe_live_source_invalid") from None
    if (value.keys() != _BASE | {"reason"} | private | live or not isinstance(value.get("reason"), str)
        or _REASON.fullmatch(value["reason"]) is None):
      raise GpsProbeRejected("gps_probe_failure_invalid")
    if private and (value["reason"] != "gps_cache_security_rejected"
                    or not isinstance(value["_private_security_exception"], str)
                    or len(value["_private_security_exception"]) > 1024):
      raise GpsProbeRejected("gps_probe_failure_invalid")
    raise GpsProbeRejected(value["reason"], live_source)
  live = {"_live_source"} if "_live_source" in value else set()
  live_source = None
  if live:
    try:
      live_source = _validate_live_source(value["_live_source"])
    except ValueError:
      raise GpsProbeRejected("gps_probe_live_source_invalid") from None
  if (value.get("status") != "observed" or value.keys() != _BASE | _OBSERVED | live
      or value.get("provider") != "gps" or value.get("is_from_mock_provider") is not False
      or value.get("source_elapsed_time_fresh") is not True):
    raise GpsProbeRejected("gps_probe_fix_invalid")
  boot = value["phone_boot_id"]
  before, after, elapsed = (value[key] for key in ("phone_before_ns", "phone_after_ns", "elapsed_realtime_ns"))
  if (not isinstance(boot, str) or _BOOT_ID.fullmatch(boot) is None
      or not _integer(before, 1) or not _integer(after, before) or not _integer(elapsed, 1, after)
      or after - before > host_finished_ns - host_started_ns):
    raise GpsProbeRejected("gps_probe_phone_clock_invalid")
  expected_token = hashlib.sha256(f"android-gps-location-v1\x00{boot}\x00{elapsed}".encode("ascii")).hexdigest()
  lower, upper = max(0, before - elapsed), after - elapsed
  if (value["source_token"] != expected_token
      or not _integer(value["age_lower_ns"]) or value["age_lower_ns"] != lower
      or not _integer(value["age_upper_ns"]) or value["age_upper_ns"] != upper):
    raise GpsProbeRejected("gps_probe_source_identity_or_age_invalid")
  upper += host_finished_ns - host_started_ns
  if upper >= MAX_AGE_NS:
    raise GpsProbeRejected("gps_source_stale")
  accuracy, lon, lat = (value[key] for key in ("horizontal_accuracy_m", "longitude", "latitude"))
  if not _finite(accuracy) or not 0 < accuracy <= 25:
    raise GpsProbeRejected("gps_accuracy_unusable")
  if not _finite(lon) or not _finite(lat) or not -180 <= lon <= 180 or not -90 <= lat <= 90:
    raise GpsProbeRejected("gps_coordinates_invalid")
  return AndroidGpsFix(expected_token, float(accuracy), lower, upper, host_started_ns, host_finished_ns,
                       boot, elapsed, (float(lon), float(lat)), live_source)


def _start_script(apk_path: str, token: str) -> str:
  return "".join([
    "navdiag_stat=$(cat /proc/$$/stat) || exit 71; ",
    "navdiag_fields=${navdiag_stat##*) }; set -- $navdiag_fields; ",
    '[ "$#" -ge 20 ] || exit 71; shift 19; ',
    'case "$1" in \'\'|*[!0-9]*) exit 71;; esac; ',
    'printf \'NAVDIAG_GPS_V1 %s %s\\n\' "$$" "$1"; ',
    'IFS= read -r navdiag_ack || exit 72; [ "$navdiag_ack" = START ] || exit 72; ',
    "export NAVDIAG_GPS_SESSION=" + shlex.quote(token) + "; ",
    "export CLASSPATH=" + shlex.quote(apk_path) + "; ",
    "exec /system/bin/app_process /system/bin " + PROBE_CLASS,
  ])


def _cleanup_script(pid: str, start_time: str, token: str) -> str:
  command = "/system/bin/app_process\n/system/bin\n" + PROBE_CLASS
  return "".join([
    "navdiag_pid=" + pid + "; navdiag_owned() { ",
    '[ -d "/proc/$navdiag_pid" ] || return 3; ',
    'navdiag_stat=$(cat "/proc/$navdiag_pid/stat") || { [ ! -d "/proc/$navdiag_pid" ] && return 3; return 76; }; ',
    "navdiag_fields=${navdiag_stat##*) }; set -- $navdiag_fields; ",
    '[ "$#" -ge 20 ] || return 76; [ "$1" != Z ] || return 3; shift 19; ',
    '[ "$1" = ' + shlex.quote(start_time) + " ] || return 3; ",
    "tr '\\000' '\\n' < /proc/" + pid + "/environ | grep -F -x -q "
    + shlex.quote("NAVDIAG_GPS_SESSION=" + token) + " || return 76; ",
    "navdiag_cmdline=$(tr '\\000' '\\n' < /proc/" + pid + "/cmdline) || return 76; ",
    'case "$navdiag_cmdline" in ' + shlex.quote(command) + "|" + shlex.quote(PROBE_CLASS)
    + ") return 0;; *) return 76;; esac; }; ",
    "navdiag_check() { navdiag_owned; navdiag_rc=$?; "
    + '[ "$navdiag_rc" = 3 ] && exit 0; [ "$navdiag_rc" = 0 ] || exit 76; }; ',
    'navdiag_check; kill -TERM "$navdiag_pid" 2>/dev/null || { navdiag_check; exit 76; }; ',
    "for navdiag_attempt in 1 2 3 4 5 6 7 8 9 10; do navdiag_check; sleep 0.1; done; ",
    'navdiag_check; kill -KILL "$navdiag_pid" 2>/dev/null || { navdiag_check; exit 76; }; ',
    "for navdiag_attempt in 1 2 3 4 5 6 7 8 9 10; do navdiag_check; sleep 0.1; done; navdiag_check; exit 76",
  ])


class AndroidGpsProbeSource:
  def __init__(self, device: Device):
    self.device = device
    self.process = None
    self._remote_identity = None
    self._token = uuid.uuid4().hex
    self._closed = False
    self._last_phone_after = None
    self._boot = None
    self._started = False
    self.cleanup_verified = False
    self.last_private_response = None
    self.last_private_window = None

  def __enter__(self):
    if self._closed or self.process is not None:
      raise GpsProbeRejected("gps_probe_source_already_used")
    try:
      result = self.device.shell("-T", "pm path " + PACKAGE + " | head -c 2049", timeout=5)
      raw = result.stdout
      if (result.returncode != 0 or not isinstance(raw, str) or len(raw.encode("utf-8")) > 2048
          or not raw.endswith("\n") or raw.count("\n") != 1 or not raw.startswith("package:")):
        raise GpsProbeRejected("gps_probe_apk_path_invalid")
      apk_path = raw[len("package:"):-1]
      if _APK_PATH.fullmatch(apk_path) is None:
        raise GpsProbeRejected("gps_probe_apk_path_invalid")
      self.process = subprocess.Popen(
        adb_client_argv(self.device, "-s", self.device.serial, "shell", "-T",
                        "su -c " + shlex.quote(_start_script(apk_path, self._token))), stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
      header = self._line(_START_TIMEOUT_S, 128)
      match = re.fullmatch(rb"NAVDIAG_GPS_V1 ([1-9][0-9]{0,9}) ([0-9]{1,24})", header)
      if match is None:
        raise GpsProbeRejected("gps_probe_handshake_invalid")
      self._remote_identity = tuple(value.decode("ascii") for value in match.groups())
      self._write(b"START\n")
      return self
    except BaseException:
      self.close()
      raise

  def __exit__(self, *_):
    self.close()

  def _write(self, value: bytes):
    try:
      self.process.stdin.write(value)
      self.process.stdin.flush()
    except (OSError, ValueError, AttributeError):
      raise GpsProbeRejected("gps_probe_channel_closed") from None

  def _line(self, timeout: float, maximum: int) -> bytes:
    data = bytearray()
    deadline = time.monotonic() + timeout
    while True:
      remaining = deadline - time.monotonic()
      if remaining <= 0 or not select.select([self.process.stdout], [], [], remaining)[0]:
        raise GpsProbeRejected("gps_probe_channel_timeout")
      part = os.read(self.process.stdout.fileno(), maximum + 1 - len(data))
      if not part:
        raise GpsProbeRejected("gps_probe_channel_closed")
      data.extend(part)
      if len(data) > maximum:
        raise GpsProbeRejected("gps_probe_result_too_large")
      if b"\n" in data:
        line, suffix = bytes(data).split(b"\n", 1)
        if suffix:
          raise GpsProbeRejected("gps_probe_unsolicited_output")
        return line

  def _no_pending(self):
    if select.select([self.process.stdout], [], [], 0)[0]:
      part = os.read(self.process.stdout.fileno(), 1)
      raise GpsProbeRejected("gps_probe_unsolicited_output" if part else "gps_probe_channel_closed")

  def observe(self) -> tuple[AndroidGpsFix, bytes, dict]:
    if self.process is None or self._closed:
      raise GpsProbeRejected("gps_probe_source_not_started")
    try:
      started = time.monotonic_ns()
      self._no_pending()
      self._write(b"OBSERVE\n")
      raw = self._line(_RESPONSE_TIMEOUT_S, MAX_RESULT_BYTES)
      self._no_pending()
      finished = time.monotonic_ns()
      self.last_private_response = raw
      self.last_private_window = {"host_started_ns": started, "host_finished_ns": finished}
      fix = parse_gps_probe(raw, started, finished)
      value = json.loads(raw)
      if self._boot is not None and (fix.phone_boot_id != self._boot or value["phone_before_ns"] < self._last_phone_after):
        raise GpsProbeRejected("gps_probe_phone_epoch_regressed")
      self._boot, self._last_phone_after = fix.phone_boot_id, value["phone_after_ns"]
      return fix, raw, dict(self.last_private_window)
    except BaseException as error:
      if self._close_after_error(error):
        self.close()
      raise

  def _close_after_error(self, error):
    return True

  def close(self):
    if self._closed:
      return
    self._closed = True
    process, self.process = self.process, None
    if process is None:
      self.cleanup_verified = True
      return
    clean_exit = cleanup_failed = False
    try:
      if process.poll() is None:
        try:
          process.stdin.write(b"QUIT\n")
          process.stdin.flush()
          clean_exit = process.wait(timeout=2.0) == 0
        except (OSError, ValueError, subprocess.TimeoutExpired):
          pass
      if not clean_exit and self._remote_identity is not None:
        try:
          pid, start_time = self._remote_identity
          runner = getattr(self.device, "cleanup_shell", self.device.shell)
          result = runner("-T", "su -c " + shlex.quote(_cleanup_script(pid, start_time, self._token)),
                          timeout=0.6 if hasattr(self.device, "cleanup_shell") else 5, check=False)
          cleanup_failed = result.returncode != 0
        except (OSError, subprocess.SubprocessError):
          cleanup_failed = True
      if process.poll() is None:
        try:
          os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
          pass
        try:
          process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
          try:
            os.killpg(process.pid, signal.SIGKILL)
          except ProcessLookupError:
            pass
          process.wait(timeout=2.0)
    finally:
      for stream in (process.stdin, process.stdout):
        if stream is not None:
          stream.close()
    if cleanup_failed and process.poll() == 0:
      cleanup_failed = False
    self.cleanup_verified = not cleanup_failed
    if cleanup_failed:
      raise GpsProbeRejected("gps_probe_cleanup_unverified")


def _bridge_read_script() -> str:
  return "".join([
    "printf 'NAVDIAG_GPS_BRIDGE_V1\\n'; ",
    f"if [ -L files ] || [ -L files/navdiag-gps ] || [ -L {GPS_BRIDGE_PATH} ]; then navdiag_rc=46; ",
    f"elif [ ! -f {GPS_BRIDGE_PATH} ]; then navdiag_rc=44; ",
    f"else head -c {GPS_BRIDGE_MAX_BYTES + 1} {GPS_BRIDGE_PATH}; navdiag_rc=$?; fi; ",
    "printf '\\nNAVDIAG_GPS_BRIDGE_RC=%03d\\n' \"$navdiag_rc\"; exit \"$navdiag_rc\"",
  ])


_BRIDGE_PREFIX = b"NAVDIAG_GPS_BRIDGE_V1\n"
_BRIDGE_TRAILER = re.compile(rb"\nNAVDIAG_GPS_BRIDGE_RC=([0-9]{3})\n\Z")


def _gps_bridge_service_receipt_ok(stdout, action: str) -> bool:
  if not isinstance(stdout, str) or len(stdout.encode()) > 4096:
    return False
  component = "cmp=" + PACKAGE + "/.GpsBridgeService"
  return (not any(marker in stdout for marker in ("Error:", "Exception"))
          and component in stdout
          and "act=" + action in stdout
          and any(marker in stdout for marker in ("Starting service:", "Service started:")))


def _start_gps_bridge(device: Device, stop):
  command_args = ["-s", device.serial, "shell", "am", "start-foreground-service", "--user", "0",
                  "-W", "-n", PACKAGE + "/.GpsBridgeService", "-a", GPS_BRIDGE_START]
  command = adb_client_argv(device, *command_args)
  direct = getattr(device, "_command", None)
  if direct is not None:
    try:
      result = direct(command_args, timeout=_BRIDGE_START_TIMEOUT_S, check=False)
    except Exception:
      raise GpsBridgeRejected("gps_bridge_start_failed") from None
    if (result.returncode != 0
        or not _gps_bridge_service_receipt_ok(result.stdout, GPS_BRIDGE_START)):
      raise GpsBridgeRejected("gps_bridge_start_failed")
    return
  process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True, start_new_session=True)
  deadline = time.monotonic() + _BRIDGE_START_TIMEOUT_S
  try:
    while True:
      if stop is not None and stop.is_set():
        raise GpsBridgeRejected("gps_bridge_start_cancelled")
      remaining = deadline - time.monotonic()
      if remaining <= 0:
        raise GpsBridgeRejected("gps_bridge_start_timeout")
      if process.poll() is None:
        time.sleep(min(0.05, remaining))
        continue
      stdout = process.stdout.read() if process.stdout is not None else ""
      if (process.returncode != 0
          or not _gps_bridge_service_receipt_ok(stdout, GPS_BRIDGE_START)):
        raise GpsBridgeRejected("gps_bridge_start_failed")
      return
  finally:
    if process.poll() is None:
      try:
        os.killpg(process.pid, signal.SIGTERM)
      except ProcessLookupError:
        pass
      try:
        process.wait(timeout=0.2)
      except subprocess.TimeoutExpired:
        try:
          os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
          pass
    if process.stdout is not None:
      process.stdout.close()


def _stop_gps_bridge(device: Device):
  runner = getattr(device, "cleanup_shell", None) or device.shell
  result = runner("am", "start-service", "--user", "0", "-W", "-n",
                  PACKAGE + "/.GpsBridgeService", "-a", GPS_BRIDGE_STOP,
                  timeout=5, check=False)
  return (result.returncode == 0
          and _gps_bridge_service_receipt_ok(result.stdout, GPS_BRIDGE_STOP))


def _gps_bridge_running(device: Device, *, cleanup=False):
  runner = getattr(device, "cleanup_shell", None) if cleanup else None
  runner = runner or device.shell
  result = runner("dumpsys", "activity", "services", PACKAGE, timeout=5, check=False)
  return any("ServiceRecord{" in line and PACKAGE + "/" in line and "GpsBridgeService" in line
             for line in result.stdout.splitlines())


def _gps_bridge_service_live(device: Device):
  result = device.shell("dumpsys", "activity", "services", PACKAGE, timeout=5, check=False)
  lines = result.stdout.splitlines()
  if not any("ServiceRecord{" in line and PACKAGE + "/" in line and "GpsBridgeService" in line
             for line in lines):
    return False
  return any("isForeground=true" in line for line in lines)


def _gps_bridge_snapshot_status(device: Device):
  raw = _read_gps_bridge(device, None)
  try:
    value = json.loads(raw, object_pairs_hook=_object, parse_constant=_constant)
  except (ValueError, UnicodeError, RecursionError):
    raise GpsBridgeRejected("gps_bridge_json_invalid") from None
  if not isinstance(value, dict) or not isinstance(value.get("status"), str):
    raise GpsBridgeRejected("gps_bridge_schema_invalid")
  live_source = _bridge_live_source(value)
  return value["status"], live_source


def _gps_bridge_snapshot_ready(device: Device):
  status, _ = _gps_bridge_snapshot_status(device)
  return status in {"observed", "unavailable"}


def _clear_gps_bridge_snapshot(device: Device):
  result = device.shell(
      "run-as", PACKAGE, "sh", "-c",
      shlex.quote("rm -f files/navdiag-gps/latest.json files/navdiag-gps/latest.json.tmp"),
      timeout=2, check=False)
  if result.returncode != 0:
    raise GpsBridgeRejected("gps_bridge_stale_snapshot_cleanup_failed")


def _wait_for_gps_bridge_ready(device: Device, stop):
  deadline = time.monotonic() + _BRIDGE_READY_TIMEOUT_S
  while True:
    if stop is not None and stop.is_set():
      raise GpsBridgeRejected("gps_bridge_start_cancelled")
    try:
      if _gps_bridge_snapshot_ready(device):
        return
    except (OSError, subprocess.SubprocessError):
      pass
    remaining = deadline - time.monotonic()
    if remaining <= 0:
      last_live_source = None
      try:
        _, last_live_source = _gps_bridge_snapshot_status(device)
      except (OSError, subprocess.SubprocessError, GpsBridgeRejected):
        pass
      raise GpsBridgeRejected("gps_bridge_start_not_registered", last_live_source)
    if stop is not None:
      stop.wait(min(0.05, remaining))
    else:
      time.sleep(min(0.05, remaining))


def _read_phone_boot(device: Device):
  runner = getattr(device, "call_binary", None)
  if runner is not None:
    result = runner("shell", "head", "-c", "64", "/proc/sys/kernel/random/boot_id",
                    timeout=5, check=False)
  else:
    result = subprocess.run(
        adb_client_argv(device, "-s", device.serial, "shell", "head", "-c", "64",
                        "/proc/sys/kernel/random/boot_id"),
        capture_output=True, timeout=5, check=False)
  if result.returncode != 0 or not isinstance(result.stdout, bytes) or len(result.stdout) > 65:
    raise GpsBridgeRejected("gps_bridge_boot_id_unavailable")
  try:
    boot = result.stdout.decode("ascii").strip()
  except UnicodeError:
    raise GpsBridgeRejected("gps_bridge_boot_id_unavailable") from None
  if _BOOT_ID.fullmatch(boot) is None:
    raise GpsBridgeRejected("gps_bridge_boot_id_unavailable")
  return boot


def _read_gps_bridge(device: Device, stop):
  process = subprocess.Popen(
      adb_client_argv(device, "-s", device.serial, "shell", "-T",
                      f"run-as {PACKAGE} sh -c " + shlex.quote(_bridge_read_script())),
      stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
      start_new_session=True)
  deadline = time.monotonic() + 1.0
  data = bytearray()
  try:
    while True:
      if stop is not None and stop.is_set():
        raise GpsBridgeRejected("gps_bridge_read_cancelled")
      remaining = deadline - time.monotonic()
      if remaining <= 0:
        raise GpsBridgeRejected("gps_bridge_read_timeout")
      if not select.select([process.stdout], [], [], min(remaining, 0.05))[0]:
        continue
      part = os.read(process.stdout.fileno(), GPS_BRIDGE_MAX_BYTES + 256 - len(data))
      if not part:
        break
      data.extend(part)
      if len(data) > GPS_BRIDGE_MAX_BYTES + 256:
        raise GpsBridgeRejected("gps_bridge_file_size_limit")
    remaining = max(0.01, deadline - time.monotonic())
    if process.poll() is None:
      try:
        process.wait(timeout=remaining)
      except subprocess.TimeoutExpired:
        raise GpsBridgeRejected("gps_bridge_read_timeout") from None
    end = _BRIDGE_TRAILER.search(data)
    if (process.returncode != 0 or not data.startswith(_BRIDGE_PREFIX) or end is None
        or end.group(1) != b"000"):
      raise GpsBridgeRejected("gps_bridge_read_failed")
    payload = bytes(data[len(_BRIDGE_PREFIX):end.start()])
    if not payload or len(payload) > GPS_BRIDGE_MAX_BYTES:
      raise GpsBridgeRejected("gps_bridge_file_size_limit")
    return payload
  except subprocess.TimeoutExpired:
    raise GpsBridgeRejected("gps_bridge_read_timeout") from None
  finally:
    if process.poll() is None:
      try:
        os.killpg(process.pid, signal.SIGTERM)
      except ProcessLookupError:
        pass
      try:
        process.wait(timeout=0.2)
      except subprocess.TimeoutExpired:
        try:
          os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
          pass
    if process.stdout is not None:
      process.stdout.close()


class AndroidGpsBridgeSource:

  def __init__(self, device: Device):
    self.device = device
    self.process = None
    self._closed = False
    self._stop = None
    self._started = False
    self._boot = None
    self._session = None
    self._last_sequence = None
    self._last_source_ns = None
    self.cleanup_verified = False
    self.last_private_response = None
    self.last_private_window = None

  def __enter__(self):
    if self._closed or self.process is not None:
      raise GpsBridgeRejected("gps_bridge_source_already_used")
    try:
      if _gps_bridge_service_live(self.device):
        raise GpsBridgeRejected("gps_bridge_already_running")
      if _gps_bridge_running(self.device):
        _stop_gps_bridge(self.device)
      _clear_gps_bridge_snapshot(self.device)
      _start_gps_bridge(self.device, self._stop)
      self.process = SimpleProcessMarker()
      self._started = True
      _wait_for_gps_bridge_ready(self.device, self._stop)
      self._boot = _read_phone_boot(self.device)
      return self
    except BaseException:
      try:
        self.close()
      except GpsBridgeRejected:
        pass
      raise

  def __exit__(self, *_):
    self.close()

  def observe(self):
    if self._closed or self.process is None:
      raise GpsBridgeRejected("gps_bridge_source_not_started")
    started = time.monotonic_ns()
    boot_before = _read_phone_boot(self.device)
    raw = _read_gps_bridge(self.device, self._stop)
    boot_after = _read_phone_boot(self.device)
    finished = time.monotonic_ns()
    self.last_private_response = raw
    self.last_private_window = {"host_started_ns": started, "host_finished_ns": finished}
    if boot_before != boot_after or boot_before != self._boot:
      raise GpsBridgeRejected("gps_bridge_boot_changed")
    fix = parse_gps_bridge(raw, boot_before, started, finished)
    value = json.loads(raw)
    sequence = value["sequence"]
    if self._session is not None:
      if value["session_id"] != self._session or sequence < self._last_sequence:
        raise GpsBridgeRejected("gps_bridge_session_regressed")
      if sequence == self._last_sequence and fix.source_token != self._last_source_ns:
        raise GpsBridgeRejected("gps_bridge_source_token_conflict")
    self._session, self._last_sequence, self._last_source_ns = (
      value["session_id"], sequence, fix.source_token)
    return fix, raw, dict(self.last_private_window)

  def close(self):
    if self._closed:
      return
    self._closed = True
    self.process = None
    if not self._started:
      self.cleanup_verified = True
      return
    command_ok = _stop_gps_bridge(self.device)
    deadline = time.monotonic() + 10.0
    running = True
    while True:
      running = _gps_bridge_running(self.device, cleanup=True)
      if not running or time.monotonic() >= deadline:
        break
      time.sleep(0.1)
    self.cleanup_verified = command_ok and not running
    if not self.cleanup_verified:
      raise GpsBridgeRejected("gps_bridge_cleanup_unverified")


class SimpleProcessMarker:
  pass


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--adb")
  parser.add_argument("--port", type=int, default=5039)
  parser.add_argument("--serial")
  parser.add_argument("--out", type=Path)
  args = parser.parse_args()
  output = args.out or ROOT / "captures" / ("android-gps-api-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
  output.mkdir(mode=0o700, parents=True, exist_ok=False)
  output.chmod(0o700)
  source = None
  failure = None
  raw = window = None
  try:
    with Device(choose_adb(args.adb), args.port, args.serial) as device:
      source = AndroidGpsProbeSource(device)
      with source:
        fix, raw, window = source.observe()
        summary = fix.summary()
  except GpsProbeRejected as error:
    failure = error
    summary = {"status": "unavailable", "reason": str(error), **dict.fromkeys(sorted(_FALSE_FLAGS), False)}
    if source is not None:
      raw, window = source.last_private_response, source.last_private_window
  if raw is not None:
    write_private(output / "gps-private.json", raw + b"\n")
  if window is not None:
    write_private(output / "clock-window.json", (json.dumps(window) + "\n").encode("ascii"))
  summary.update(private_output=str(output.resolve()), cleanup_verified=source is not None and source.cleanup_verified)
  write_private(output / "report.json", (json.dumps(summary, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
  print(json.dumps(summary, ensure_ascii=False))
  if failure is not None:
    raise failure


if __name__ == "__main__":
  try:
    main()
  except GpsProbeRejected as error:
    raise SystemExit("GPS 只读采样拒绝：" + str(error)) from None
  except Exception as error:
    raise SystemExit("GPS 只读采样失败：" + type(error).__name__) from None
