from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
import hashlib
import math
import os
import re
import select
import shlex
import signal
import socket
import subprocess
import threading
import copy
import time
from types import MappingProxyType
import uuid

if __package__:
  from . import android_gps_source as gps_module
  from . import bridge_observation as bridge_module
  from . import native_route_source as native_module
  from .android_gps_source import AndroidGpsBridgeSource, AndroidGpsFix
  from .device import Device, adb_client_argv, choose_adb
  from .live_route_snapshot import LiveRouteRejected, SelectedCacheRoute, SelectedNativeRoute, collect_selected, select_native
  from .native_route_source import NativeRouteProbeSource
  from .route_snapshot import MAX_FILE_BYTES, read_file
else:
  import android_gps_source as gps_module
  import bridge_observation as bridge_module
  import native_route_source as native_module
  from android_gps_source import AndroidGpsBridgeSource, AndroidGpsFix
  from device import Device, adb_client_argv, choose_adb
  from live_route_snapshot import LiveRouteRejected, SelectedCacheRoute, SelectedNativeRoute, collect_selected, select_native
  from native_route_source import NativeRouteProbeSource
  from route_snapshot import MAX_FILE_BYTES, read_file


SUPPORTED_PROFILE = native_module.SUPPORTED_PROFILE
_FIXED_REASON = re.compile(r"[a-z][a-z0-9_]{0,79}")
_ADB_SERIAL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_ADB_OWNERSHIP_MODES = {"dedicated", "attach"}
_NATIVE_RECONNECT_REASONS = {"process_epoch_changed", "process_epoch_failed", "invalid_process_epoch",
                             "provider_identity_unavailable", "provider_pid_unavailable", "unsupported_binary_profile",
                             "library_mapping_changed", "library_path_not_supported"}
_GPS_TRANSIENT_REASONS = {"gps_source_stale", "gps_accuracy_unusable", "gps_cache_empty"}
_INACTIVE_REASONS = {"selection_changed_during_read"}
_STALE_WHILE_REFRESH_REASONS = {"selection_changed_during_read"}
_INACTIVE_TTL_NS = 5_000_000_000
_ORDERLY_EXIT_TIMEOUT_S = 2.0
_TRANSPORT_RECOVERY_TIMEOUT_S = 15.0
_BRIDGE_START_TIMEOUT_S = 2.0
_BRIDGE_STOP_TIMEOUT_S = 2.0
_BRIDGE_SERVICE_COMPONENT = bridge_module.PACKAGE + "/.BridgeService"
_BRIDGE_START_ACTION = bridge_module.PACKAGE + ".BRIDGE_START"
_BRIDGE_STOP_ACTION = bridge_module.PACKAGE + ".BRIDGE_STOP"


class PhoneRouteProducerRejected(ValueError):

  def __init__(self, reason: str, live_source: dict | None = None):
    super().__init__(reason)
    self.live_source = live_source


class _Stopped(Exception):
  pass


class _Superseded(_Stopped):
  pass


class _Reconnect(PhoneRouteProducerRejected):
  pass


class _SourceSessionChanged(_Reconnect):
  def __init__(self, sample=None, generation=None):
    super().__init__("phone_source_session_changed")
    self._sample = sample
    self._generation = generation


def _integer(value, minimum=0, maximum=(1 << 63) - 1):
  return type(value) is int and minimum <= value <= maximum


def _reason(error):
  text = str(error)
  return text if _FIXED_REASON.fullmatch(text) else "phone_producer_source_failed"


def _freeze(value):
  if isinstance(value, dict):
    return MappingProxyType({key: _freeze(item) for key, item in value.items()})
  if isinstance(value, (list, tuple)):
    return tuple(_freeze(item) for item in value)
  if value is None or type(value) in (str, bool, int, float):
    return value
  raise PhoneRouteProducerRejected("phone_snapshot_type_invalid")


@dataclass(frozen=True)
class PhoneRouteSample:
  selected: SelectedCacheRoute | SelectedNativeRoute = field(repr=False)
  cache_bytes: bytes = field(repr=False)
  source_object: str = field(repr=False)
  epoch: str = field(repr=False)
  sequence: int
  host_start_ns: int
  gps: AndroidGpsFix = field(repr=False)
  native_before: Mapping = field(repr=False)
  native_after: Mapping = field(repr=False)
  transport_session: str
  broadcast: Mapping | None = field(default=None, repr=False)
  anchor_pair_retry: Mapping | None = field(default=None, repr=False)


@dataclass(frozen=True)
class _InactiveGeometry:
  """One inactive copy of the original bytes; only serves same-file recovery after a whitelisted transient rejection."""
  selected: SelectedCacheRoute = field(repr=False)
  cache_bytes: bytes = field(repr=False)
  epoch: str = field(repr=False)
  transport_session: str
  saved_ns: int
  sequence: int


REJECTION_QUEUE_MAX = 256


@dataclass(frozen=True)
class PhoneRouteProducerStatus:
  running: bool
  reason: str
  transport_session: str | None
  sequence: int
  connected: bool
  cleanup_verified: bool
  last_error_chain: tuple[str, ...] = ()
  cleanup_diagnostics: tuple[str, ...] = ()
  rejection: dict | None = None
  rejection_diagnosis: dict | None = None


def _kill_local(process, *, wait_s=0.15):
  if process.poll() is None:
    try:
      os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
      pass
    try:
      process.wait(timeout=wait_s)
    except subprocess.TimeoutExpired:
      try:
        os.killpg(process.pid, signal.SIGKILL)
      except ProcessLookupError:
        pass
      process.wait(timeout=wait_s)


class _InterruptibleDevice(Device):
  def __init__(self, adb, port, serial, stop, *, ownership="dedicated"):
    if ownership not in _ADB_OWNERSHIP_MODES:
      raise ValueError("phone_adb_ownership_invalid")
    super().__init__(adb, port, serial)
    self.stop = stop
    self.ownership = ownership
    self.server = None
    self.cleanup_verified = True

  def _command(self, args, *, timeout, check=True, binary=False, cleanup=False):
    if self.stop.is_set() and not cleanup:
      raise _Stopped()
    if cleanup and not self.stop.is_set():
      self._wait_for_device(deadline=time.monotonic() + _TRANSPORT_RECOVERY_TIMEOUT_S)
    command = adb_client_argv(self, *args)
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=not binary, start_new_session=True)
    deadline = time.monotonic() + timeout
    try:
      while True:
        if self.stop.is_set() and not cleanup:
          raise _Stopped()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
          raise subprocess.TimeoutExpired(command, timeout)
        try:
          stdout, stderr = process.communicate(timeout=min(0.05, remaining))
          break
        except subprocess.TimeoutExpired:
          continue
      result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
      if check and result.returncode:
        raise PhoneRouteProducerRejected("phone_adb_command_failed")
      return result
    finally:
      _kill_local(process)
      for stream in (process.stdout, process.stderr):
        if stream is not None:
          stream.close()

  def host(self, *args, timeout=20, check=True):
    return self._command(args, timeout=timeout, check=check)

  def call_binary(self, *args, timeout=10, check=True):
    return self._command(["-s", self.serial, *args], timeout=timeout, check=check, binary=True)

  def cleanup_shell(self, *args, timeout=0.6, check=False):
    return self._command(["-s", self.serial, "shell", *args], timeout=timeout, check=check, cleanup=True)

  def __enter__(self):
    if self.stop.is_set():
      raise _Stopped()
    with socket.socket() as probe:
      probe.settimeout(0.1)
      listening = probe.connect_ex(("127.0.0.1", self.port)) == 0
      if self.ownership == "attach":
        if not listening:
          raise PhoneRouteProducerRejected("phone_adb_server_unavailable")
      elif listening:
        raise PhoneRouteProducerRejected("phone_adb_port_owned_elsewhere")
    if self.ownership == "attach":
      return self._wait_for_device()
    try:
      self.server = subprocess.Popen([self.adb, "-L", "tcp:localhost:" + str(self.port), "server", "nodaemon"],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, start_new_session=True)
      self.started = True
      self.cleanup_verified = False
      deadline = time.monotonic() + 10.0
      while not self.stop.is_set():
        if self.server.poll() is not None:
          raise PhoneRouteProducerRejected("phone_adb_server_failed")
        with socket.socket() as probe:
          probe.settimeout(0.05)
          listening = probe.connect_ex(("127.0.0.1", self.port)) == 0
        if not listening:
          if time.monotonic() >= deadline:
            raise PhoneRouteProducerRejected("phone_adb_server_not_listening")
          self.stop.wait(0.05)
          continue
        ready = self._wait_for_device(deadline=deadline)
        if ready:
          return self
      raise _Stopped()
    except BaseException:
      self.__exit__(None, None, None)
      raise

  def _wait_for_device(self, *, deadline=None):
    deadline = time.monotonic() + 10.0 if deadline is None else deadline
    while not self.stop.is_set():
      result = self.host("devices", timeout=1.0, check=False)
      rows = [line.split() for line in result.stdout.splitlines()[1:] if line.strip()]
      ready = [row[0] for row in rows if len(row) > 1 and row[1] == "device"]
      if self.serial and self.serial in ready:
        return self
      if not self.serial and len(ready) == 1:
        self.serial = ready[0]
        return self
      if not self.serial and len(ready) > 1:
        raise PhoneRouteProducerRejected("phone_multiple_devices_require_serial")
      if time.monotonic() >= deadline:
        raise PhoneRouteProducerRejected("phone_device_not_ready")
      self.stop.wait(0.1)
    raise _Stopped()

  def __exit__(self, *_):
    server, self.server = self.server, None
    if server is not None:
      _kill_local(server)
    self.started = False
    self.cleanup_verified = True


class _InterruptibleSource:
  _gps = False

  def _line(self, timeout, maximum):
    data = bytearray()
    deadline = time.monotonic() + timeout
    while True:
      if self._stop.is_set():
        raise _Stopped()
      remaining = deadline - time.monotonic()
      if remaining <= 0:
        raise PhoneRouteProducerRejected("phone_probe_channel_timeout")
      if not select.select([self.process.stdout], [], [], min(remaining, 0.05))[0]:
        continue
      part = os.read(self.process.stdout.fileno(), maximum + 1 - len(data))
      if not part:
        raise PhoneRouteProducerRejected("phone_probe_channel_closed")
      data.extend(part)
      if len(data) > maximum:
        raise PhoneRouteProducerRejected("phone_probe_result_too_large")
      if b"\n" in data:
        line, suffix = bytes(data).split(b"\n", 1)
        if suffix:
          raise PhoneRouteProducerRejected("phone_probe_unsolicited_output")
        return line

  def _geometry_header(self, timeout, maximum):
    data = bytearray()
    deadline = time.monotonic() + timeout
    while True:
      if self._stop.is_set():
        raise _Stopped()
      remaining = deadline - time.monotonic()
      if remaining <= 0:
        raise PhoneRouteProducerRejected("phone_probe_channel_timeout")
      if not select.select([self.process.stdout], [], [], min(remaining, 0.05))[0]:
        continue
      part = os.read(self.process.stdout.fileno(), 1)
      if not part:
        raise PhoneRouteProducerRejected("phone_probe_channel_closed")
      if part == b"\n":
        return bytes(data)
      data.extend(part)
      if len(data) > maximum:
        raise PhoneRouteProducerRejected("phone_probe_result_too_large")

  def _read_exact(self, length, timeout):
    if type(length) is not int or not 0 <= length <= native_module.MAX_GEOMETRY_BYTES:
      raise PhoneRouteProducerRejected("phone_geometry_size_invalid")
    data = bytearray()
    deadline = time.monotonic() + timeout
    while len(data) < length:
      if self._stop.is_set():
        raise _Stopped()
      remaining = deadline - time.monotonic()
      if remaining <= 0:
        raise PhoneRouteProducerRejected("phone_geometry_timeout")
      if not select.select([self.process.stdout], [], [], min(remaining, 0.05))[0]:
        continue
      part = os.read(self.process.stdout.fileno(), min(length - len(data), 65536))
      if not part:
        raise PhoneRouteProducerRejected("phone_probe_channel_closed")
      data.extend(part)
    return bytes(data)

  def close(self):
    if self._closed:
      return
    self._closed = True
    process, self.process = self.process, None
    if process is None:
      self.cleanup_verified = True
      return
    clean_exit = process.poll() == 0
    cleanup_failed = False
    diagnostics = []
    try:
      if process.poll() is None:
        try:
          process.stdin.write(b"QUIT\n")
          process.stdin.flush()
          clean_exit = process.wait(timeout=_ORDERLY_EXIT_TIMEOUT_S) == 0
        except (OSError, ValueError, subprocess.TimeoutExpired):
          diagnostics.append("quit_not_confirmed_within_budget")
      if not clean_exit and self._remote_identity is not None:
        try:
          pid, started = self._remote_identity
          script = (gps_module if self._gps else native_module)._cleanup_script(pid, started, self._token)
          cleanup = getattr(self.device, "cleanup_shell", self.device.shell)
          result = cleanup("-T", "su -c " + shlex.quote(script), timeout=0.6, check=False)
          cleanup_failed = result.returncode != 0
          if cleanup_failed:
            diagnostics.append("guard_identity_rejected" if result.returncode == 76 else "guard_command_failed")
        except (OSError, subprocess.SubprocessError):
          cleanup_failed = True
          diagnostics.append("guard_transport_failed")
      if cleanup_failed:
        if process.poll() == 0:
          cleanup_failed = False
          diagnostics.append("late_clean_exit_confirmed")
        elif self._remote_identity is not None:
          try:
            pid, started = self._remote_identity
            cleanup = getattr(self.device, "cleanup_shell", self.device.shell)
            result = cleanup("-T", "su -c " + shlex.quote(_original_gone_script(pid, started)), timeout=0.25, check=False)
            if result.returncode == 0:
              cleanup_failed = False
              diagnostics.append("original_generation_gone_confirmed")
          except (OSError, subprocess.SubprocessError):
            diagnostics.append("original_generation_verification_failed")
      _kill_local(process)
    finally:
      for stream in (process.stdin, process.stdout):
        if stream is not None:
          stream.close()
    self.cleanup_verified = not cleanup_failed
    self.cleanup_diagnostics = tuple(("gps_" if self._gps else "native_") + value for value in diagnostics)
    if cleanup_failed:
      raise PhoneRouteProducerRejected("phone_probe_cleanup_unverified")


def _original_gone_script(pid, started):
  return "".join([
    "navdiag_pid=" + pid + "; ",
    '[ -d "/proc/$navdiag_pid" ] || exit 0; ',
    'navdiag_stat=$(cat "/proc/$navdiag_pid/stat") || { [ ! -d "/proc/$navdiag_pid" ] && exit 0; exit 76; }; ',
    'navdiag_fields=${navdiag_stat##*) }; set -- $navdiag_fields; [ "$#" -ge 20 ] || exit 76; ',
    '[ "$1" != Z ] || exit 0; shift 19; [ "$1" = ' + shlex.quote(started) + " ] || exit 0; exit 76",
  ])


class _NavigationBroadcastBridge:

  def __init__(self, device, stop, *, clock=time.monotonic):
    self.device = device
    self.stop = stop
    self.clock = clock
    self.started = False
    self.cleanup_verified = True
    self.cleanup_diagnostics = ()

  def _service_lines(self, *, cleanup=False):
    runner = getattr(self.device, "cleanup_shell", None) if cleanup else self.device.shell
    runner = runner or self.device.shell
    result = runner("dumpsys", "activity", "services", bridge_module.PACKAGE,
                    timeout=_BRIDGE_STOP_TIMEOUT_S, check=False)
    if result.returncode != 0:
      raise PhoneRouteProducerRejected("phone_bridge_status_failed")
    return result.stdout.splitlines()

  @staticmethod
  def _service_registered(lines):
    return any("ServiceRecord{" in line and _BRIDGE_SERVICE_COMPONENT in line for line in lines)

  @staticmethod
  def _service_live(lines):
    service_context = False
    for line in lines:
      if "ServiceRecord{" in line and _BRIDGE_SERVICE_COMPONENT in line:
        service_context = True
      elif "ServiceRecord{" in line:
        service_context = False
      elif service_context and "isForeground=true" in line:
        return True
    return False

  def __enter__(self):
    if self.stop.is_set():
      raise _Stopped()
    lines = self._service_lines()
    if self._service_live(lines):
      return self
    if self._service_registered(lines):
      raise PhoneRouteProducerRejected("phone_bridge_already_registered")
    start_args = ["am", "start-foreground-service", "--user", "0", "-W",
                  "-n", _BRIDGE_SERVICE_COMPONENT, "-a", _BRIDGE_START_ACTION]
    self.started = True
    self.cleanup_verified = False
    try:
      result = self.device._command(
        ["-s", self.device.serial, "shell", "run-as", bridge_module.PACKAGE, *start_args],
        timeout=_BRIDGE_START_TIMEOUT_S, check=False)
      if self.stop.is_set():
        raise _Stopped()
      if self._cold_start_not_found(result):
        resolved = self.device.shell(
          "cmd", "package", "query-services", "--user", "0", "--components",
          "-n", _BRIDGE_SERVICE_COMPONENT, timeout=_BRIDGE_START_TIMEOUT_S, check=False)
        if self.stop.is_set():
          raise _Stopped()
        if (resolved.returncode == 0 and not getattr(resolved, "stderr", "")
            and resolved.stdout.strip() == _BRIDGE_SERVICE_COMPONENT):
          result = self.device._command(
            ["-s", self.device.serial, "shell", "su", "-c", shlex.quote(shlex.join(start_args))],
            timeout=_BRIDGE_START_TIMEOUT_S, check=False)
      if (result.returncode != 0 or "Error:" in result.stdout
          or "Error:" in (getattr(result, "stderr", "") or "")
          or "Starting service:" not in result.stdout):
        raise PhoneRouteProducerRejected("phone_bridge_start_failed")
      deadline = self.clock() + _BRIDGE_START_TIMEOUT_S
      while True:
        if self.stop.is_set():
          raise _Stopped()
        if self._service_live(self._service_lines()):
          return self
        if self.clock() >= deadline:
          raise PhoneRouteProducerRejected("phone_bridge_start_not_registered")
        self.stop.wait(0.05)
    except BaseException:
      self.__exit__()
      raise

  @staticmethod
  def _cold_start_not_found(result):
    return (result.returncode == 255 and "Starting service:" in result.stdout
            and "Error:" not in result.stdout
            and (getattr(result, "stderr", "") or "").strip() == "Error: Not found; no service started.")

  def __exit__(self, *_):
    if not self.started:
      return
    diagnostics = []
    cleanup_failed = False
    if self.started:
      try:
        result = self.device.cleanup_shell(
          "run-as", bridge_module.PACKAGE, "am", "start-service", "--user", "0", "-W",
          "-n", _BRIDGE_SERVICE_COMPONENT, "-a", _BRIDGE_STOP_ACTION,
          timeout=_BRIDGE_STOP_TIMEOUT_S, check=False)
        if (result.returncode != 0 or "Error:" in result.stdout
            or "Error:" in (getattr(result, "stderr", "") or "")):
          cleanup_failed = True
          diagnostics.append("phone_bridge_stop_command_failed")
        else:
          status_calls = getattr(self.device, "bridge_status_calls", 0)
          deadline = self.clock() + _BRIDGE_STOP_TIMEOUT_S
          while self.clock() < deadline:
            status_calls += 1
            lines = self._service_lines(cleanup=True)
            if (not self._service_registered(lines) or
                (status_calls >= 2 and not self._service_live(lines))):
              break
            time.sleep(0.05)
          else:
            cleanup_failed = True
            diagnostics.append("phone_bridge_stop_not_confirmed")
      except (OSError, subprocess.SubprocessError, PhoneRouteProducerRejected):
        cleanup_failed = True
        diagnostics.append("phone_bridge_cleanup_transport_failed")
    self.started = False
    self.cleanup_verified = not cleanup_failed
    self.cleanup_diagnostics = tuple(diagnostics)


class _NativeSource(_InterruptibleSource, NativeRouteProbeSource):
  def __init__(self, device, stop):
    self._stop = stop
    super().__init__(device)

  def _on_unavailable(self, value):
    pass


class _GpsSource(_InterruptibleSource, AndroidGpsBridgeSource):
  _gps = True

  def __init__(self, device, stop):
    self._stop = stop
    AndroidGpsBridgeSource.__init__(self, device)

  def _close_after_error(self, error):
    return False

  def close(self):
    AndroidGpsBridgeSource.close(self)


def _native_identity(value, epoch):
  if isinstance(value, dict) and value.get("status") == "unavailable":
    reason = value.get("reason")
    if not isinstance(reason, str) or _FIXED_REASON.fullmatch(reason) is None:
      raise _Reconnect("phone_native_protocol_invalid")
    error = _Reconnect(reason) if reason in _NATIVE_RECONNECT_REASONS else PhoneRouteProducerRejected(reason)
    diagnosis = value.get("_selection_diagnosis")
    if not isinstance(error, _Reconnect) and isinstance(diagnosis, dict):
      error.diagnosis = copy.deepcopy(diagnosis)
      error.diagnosis["observation_window"] = {
        "clock": value.get("observation_clock_domain", "unknown"),
        "started_ns": value.get("observation_started_monotonic_ns", "unknown"),
        "finished_ns": value.get("observation_finished_monotonic_ns", "unknown"),
        "host_clock": value.get("host_observation_clock_domain", "unknown"),
        "host_started_ns": value.get("host_observation_started_monotonic_ns", "unknown"),
        "host_finished_ns": value.get("host_observation_finished_monotonic_ns", "unknown"),
      }
    if "_snapshot_retry" in value:
      try:
        native_module._validate_snapshot_retry(value, include_position=True, include_navi_info="_navi_info" in value)
      except native_module.ProbeError:
        # Optional evidence must not replace the original rejection or reconnect reason.
        pass
      else:
        error.snapshot_retry = copy.deepcopy(value["_snapshot_retry"])
    raise error
  if not isinstance(value, dict) or value.get("status") != "observed":
    raise _Reconnect("phone_native_protocol_invalid")
  if value.get("profile") != SUPPORTED_PROFILE:
    raise _Reconnect("phone_native_profile_changed")
  if value.get("process_epoch_hash") != hashlib.sha256(epoch.encode()).hexdigest():
    raise _Reconnect("phone_provider_epoch_changed")
  if (value.get("sample_consistent") is not True or value.get("gps_navigation_observed") is not True
      or type(value.get("navi_type_raw")) is not int or value["navi_type_raw"] != 0
      or type(value.get("navi_state_raw")) is not int or value["navi_state_raw"] not in (1, 3)
      or not isinstance(value.get("path_identity_hash"), str)
      or re.fullmatch(r"[0-9a-f]{64}", value["path_identity_hash"]) is None):
    raise PhoneRouteProducerRejected("phone_navigation_not_active")
  position = value.get("_position")
  if isinstance(position, dict) and position.get("status") == "unavailable":
    reason = position.get("reason")
    if isinstance(reason, str) and _FIXED_REASON.fullmatch(reason):
      raise PhoneRouteProducerRejected(reason)
  if (not isinstance(position, dict) or position.get("status") != "observed"
      or type(position.get("is_on_guide_road_raw")) is not int or position["is_on_guide_road_raw"] != 1
      or position.get("path_identity_hash") != value["path_identity_hash"]
      or any(not _integer(position.get(key), 0, 0x7fffffff) for key in ("segment_index", "link_index", "point_index"))):
    raise PhoneRouteProducerRejected("phone_native_position_unavailable")
  for key in ("native_st_pos_deg", "matched_st_pos_deg"):
    point = position.get(key)
    if (not isinstance(point, (list, tuple)) or len(point) != 2
        or any(type(number) not in (int, float) or not math.isfinite(number) for number in point)
        or not -180 <= point[0] <= 180 or not -90 <= point[1] <= 90 or point == [0, 0] or point == (0, 0)):
      raise PhoneRouteProducerRejected("phone_native_coordinates_invalid")
  if value.get("host_observation_clock_domain") != "host_monotonic":
    raise PhoneRouteProducerRejected("phone_native_host_clock_missing")
  start, finish = (value.get("host_observation_" + part + "_monotonic_ns") for part in ("started", "finished"))
  if not _integer(start, 1) or not _integer(finish, start) or finish - start >= 1_000_000_000:
    raise PhoneRouteProducerRejected("phone_native_host_window_invalid")
  return value["path_identity_hash"], start, finish


class PhoneRouteProducer:
  def __init__(self, *, adb=None, port=5039, serial=None, adb_ownership="dedicated",
               device_factory=None, native_factory=None, bridge_service_factory=None,
               gps_factory=None, bridge_factory=None, bridge_observe=bridge_module.observe_device,
               collector=collect_selected, clock_ns=time.monotonic_ns,
               interval_s=0.25, max_sample_age_ns=2_000_000_000):
    if (not _integer(port, 1, 65535) or type(interval_s) not in (int, float)
        or not math.isfinite(interval_s) or not 0.01 <= interval_s <= 5
        or not _integer(max_sample_age_ns, 1, 2_000_000_000)
        or adb_ownership not in _ADB_OWNERSHIP_MODES):
      raise ValueError("phone_producer_configuration_invalid")
    if serial is None:
      serial = os.environ.get("NAVDIAG_SERIAL") or None
    if serial is not None and (type(serial) is not str or not _ADB_SERIAL.fullmatch(serial)):
      raise ValueError("phone_producer_serial_invalid")
    self._stop = threading.Event()
    self._lock = threading.Lock()
    self._thread = None
    self._closed = False
    self._sample = None
    self._inactive = None
    self._retained = None
    self._generation = 0
    self._selection = self._cache = self._selection_key = None
    self._sequence = 0
    self._published_sequence = 0
    self._last_host_start = 0
    self._session = None
    self._published_session = None
    self._reason = "phone_producer_not_started"
    self._connected = False
    self._cleanup_verified = True
    self._last_error_chain = self._cleanup_diagnostics = ()
    self._last_rejection_diagnosis = None
    self._last_rejection = None
    self._rejection_events = 0
    self._rejection_queue = deque(maxlen=REJECTION_QUEUE_MAX)
    self._rejection_dropped = 0
    self._gps_identity = None
    self._provider_epoch = None
    self._route_source_session = None
    self._collector, self._clock_ns = collector, clock_ns
    self._interval_s, self._max_sample_age_ns = interval_s, max_sample_age_ns
    self._adb_ownership = adb_ownership
    self._adb_port = port
    self.connected_adb_ownership = None
    self.connected_adb_port = None
    self._device_factory = device_factory or (
      lambda stop: _InterruptibleDevice(choose_adb(adb), port, serial, stop, ownership=adb_ownership))
    self._native_factory = native_factory or _NativeSource
    self._bridge_service_factory = bridge_service_factory or _NavigationBroadcastBridge
    self._gps_factory = gps_factory or _GpsSource
    self._bridge_factory = bridge_factory or (lambda: bridge_module.BridgeObserver(max_source_age_s=1.25))
    self._bridge_observe = bridge_observe
    self._bridge_observer = None

  def start(self):
    with self._lock:
      if self._closed:
        raise RuntimeError("phone_producer_already_closed")
      if self._thread is None:
        self._thread = threading.Thread(target=self._run, name="phone-route-producer", daemon=False)
        self._thread.start()
    return self

  def _advance_generation(self):
    """Advance the generation on cancel/stop/session change, invalidating in-flight writes and publishes."""
    with self._lock:
      self._generation += 1
      self._inactive = self._retained = None

  def _retire_locked(self, reason):
    """Single invalidation path; the caller must already hold self._lock."""
    self._inactive = None
    # Retired geometry must also lose its candidate source, or a later whitelisted
    # rejection would rebuild an already withdrawn route.
    self._retained = None
    self._sample = None
    self._selection = self._cache = self._selection_key = None
    self._reason = reason

  def _withdraw(self, reason, *, advance=True, expected_generation=None):
    """Clear active geometry and the whitelisted slot at once; only _remember_inactive
    may create an inactive slot.

    advance=True is a real withdrawal/cancel: it also advances the generation so
    in-flight writes and publishes are invalidated.
    advance=False is only this round's own refresh and must not invalidate itself.
    With a mismatched expected_generation nothing changes and False is returned.
    """
    with self._lock:
      if expected_generation is not None and expected_generation != self._generation:
        return False
      if advance:
        self._generation += 1
      self._retire_locked(reason)
      return True

  def _rollover_source_transport(self, *, expected_generation):
    with self._lock:
      if (self._stop.is_set() or self._closed or expected_generation != self._generation
          or self._reason != "phone_source_session_changed"):
        return False
      self._generation += 1
      self._retire_locked("phone_source_session_changed")
      self._session = uuid.uuid4().hex
      self._sequence = self._published_sequence = 0
      self._published_session = None
      self._route_source_session = None
      return True

  def _adopt_source_sample(self, error, *, expected_generation):
    """Commit only this round's fully validated new-route pair without retiming it."""
    if not isinstance(error, _SourceSessionChanged):
      return None
    with self._lock:
      sample, error._sample = error._sample, None
      if (not isinstance(sample, PhoneRouteSample) or self._stop.is_set() or self._closed
          or expected_generation != self._generation or error._generation != expected_generation - 1
          or self._reason != "phone_source_session_changed"
          or sample.transport_session != self._session or sample.epoch != self._provider_epoch
          or self._route_source_session is None or sample.selected.source_session_id == self._route_source_session
          or sample.sequence != self._sequence + 1 or sample.host_start_ns <= self._last_host_start):
        return None
      now = self._clock_ns()
      finished = sample.native_after.get("host_observation_finished_monotonic_ns")
      if (not _integer(now, finished) or now - sample.host_start_ns >= self._max_sample_age_ns
          or not sample.gps.fresh_at(now)):
        return None
      if self._gps_identity is not None:
        boot, elapsed, token = self._gps_identity
        if (sample.gps.phone_boot_id != boot or sample.gps.elapsed_realtime_ns < elapsed
            or (sample.gps.elapsed_realtime_ns == elapsed and sample.gps.source_token != token)):
          return None
      # The pending pair has never been published. Retire the old lease first, then
      # preserve every original source timestamp and bind only that new pair to a new session.
      self._generation += 1
      self._retire_locked("phone_source_session_changed")
      self._session = uuid.uuid4().hex
      self._sequence, self._published_sequence, self._published_session = 1, 0, None
      self._selection, self._cache = sample.selected, sample.cache_bytes
      self._selection_key = sample.epoch, sample.selected.identity_hash
      self._retained = sample.selected, sample.cache_bytes
      self._route_source_session = sample.selected.source_session_id
      self._last_host_start = sample.host_start_ns
      self._gps_identity = sample.gps.phone_boot_id, sample.gps.elapsed_realtime_ns, sample.gps.source_token
      return replace(sample, transport_session=self._session, sequence=1), self._generation

  def _remember_inactive(self, reason, sequence, generation=None):
    """Move the geometry accepted last in this round into the single TTL-bounded inactive slot.

    The retained/existing-slot/epoch/session/token checks and the state change all happen
    in one critical section, so stale data can never be re-wrapped under a newer generation.
    """
    with self._lock:
      if generation is not None and generation != self._generation:
        # A late rejection from an old generation may neither create a slot nor
        # clear data a newer generation already accepted.
        return False
      retained, existing = self._retained, self._inactive
      sample = self._sample
      now = self._clock_ns()
      preserve_sample = (
        reason in _STALE_WHILE_REFRESH_REASONS and sample is not None
        and _integer(now, sample.host_start_ns)
        and now - sample.host_start_ns < self._max_sample_age_ns
        and sample.gps.fresh_at(now))
      # Every real rejection advances the generation inside the lock: this round is
      # invalidated and an already-taken sample of the same generation cannot be
      # revived by _publish. The existing slot object is kept whole, so saved_ns is not refreshed.
      slot = None
      if (reason in _INACTIVE_REASONS and retained is not None
          and isinstance(retained[0], SelectedCacheRoute)):
        # Repeated rejections neither create a new slot nor refresh its timestamp; the
        # slot expires from the moment it was first saved.
        slot = existing if existing is not None else _InactiveGeometry(
          retained[0], retained[1], self._provider_epoch, self._session, self._clock_ns(), sequence)
      self._generation += 1
      self._retire_locked(reason)
      if preserve_sample:
        self._sample = sample
      if slot is not None:
        # Keep the candidate source with the slot so repeated rejections can re-verify
        # it instead of dropping it.
        self._retained = retained
        self._inactive = slot
      return True

  def _active(self):
    """Return this round's generation, or None once stopped/closed."""
    with self._lock:
      if self._stop.is_set() or self._closed:
        return None
      return self._generation

  def _on_rejection(self, error, sequence=0, generation=None):
    """Single entry: a whitelisted transient rejection degrades to an inactive slot,
    every other reason invalidates completely."""
    reason = _reason(error)
    applied = self._remember_inactive(reason, sequence, generation)
    self._record_rejection(error, generation=generation, sequence=sequence,
                           stale_generation=generation is not None and not applied)
    return reason

  def _record_rejection(self, error, *, generation=None, sequence=None, stale_generation=False):
    chain, current = [], error
    while current is not None and len(chain) < 4:
      chain.append(_reason(current))
      current = current.__context__
    diagnosis = getattr(error, "diagnosis", None)
    diagnosis = copy.deepcopy(diagnosis) if isinstance(diagnosis, dict) else None
    live_source = getattr(error, "live_source", None)
    live_source = copy.deepcopy(live_source) if isinstance(live_source, dict) else None
    snapshot_retry = getattr(error, "snapshot_retry", None)
    snapshot_retry = copy.deepcopy(snapshot_retry) if isinstance(snapshot_retry, dict) else None
    with self._lock:
      recorded_generation = self._generation if generation is None else generation
      self._last_error_chain = tuple(chain)
      self._last_rejection_diagnosis = copy.deepcopy(diagnosis)
      self._rejection_events += 1
      record = {"event_id": self._rejection_events, "reason": _reason(error),
                "generation": recorded_generation, "state_generation": self._generation,
                "stale_generation": stale_generation,
                "sequence": self._sequence if sequence is None else sequence,
                "host_monotonic_ns": self._clock_ns(), "diagnosis": diagnosis,
                "diagnosis_present": diagnosis is not None,
                "live_source": live_source, "live_source_present": live_source is not None,
                "snapshot_retry": snapshot_retry, "snapshot_retry_present": snapshot_retry is not None}
      self._last_rejection = copy.deepcopy(record)
      if len(self._rejection_queue) == REJECTION_QUEUE_MAX:
        self._rejection_dropped += 1
      self._rejection_queue.append(record)

  def _recover_inactive(self, slot, device, native, identity, generation=None):
    """Re-verify the same file with two bounded route_snapshot.read_file reads; no other cache is scanned.

    generation is the very token _sample_once holds: it is compared inside one critical
    section both before and after the reads, never re-fetched to re-wrap stale bytes.
    """
    with self._lock:
      if generation is None or generation != self._generation:
        return None
      session = self._session
    if slot is None or session != slot.transport_session or slot.epoch != native.epoch:
      return None
    if not isinstance(slot.selected, SelectedCacheRoute):
      return None
    if slot.selected.identity_hash != identity:
      return None
    if self._clock_ns() - slot.saved_ns >= _INACTIVE_TTL_NS:
      return None
    selected, expected = slot.selected, slot.cache_bytes
    if (not isinstance(expected, bytes) or not 0 < len(expected) <= MAX_FILE_BYTES
        or hashlib.sha256(expected).hexdigest() != selected.cache_sha256):
      return None
    try:
      first = read_file(device, selected.source_path)
      second = read_file(device, selected.source_path)
    except (AttributeError, ValueError, OSError, subprocess.SubprocessError):
      return None
    if first != expected or second != expected:
      return None
    # Two whole-file reads can themselves be slow; re-check the clock afterwards so
    # bytes that crossed the TTL are never reused.
    if self._clock_ns() - slot.saved_ns >= _INACTIVE_TTL_NS:
      return None
    # The provider epoch must not change across the reads; the caller re-samples the
    # PathID before and after them.
    if native.epoch != slot.epoch:
      return None
    with self._lock:
      if (generation != self._generation or self._stop.is_set() or self._closed
          or self._session != session):
        return None
      self._inactive = None
      self._selection, self._cache, self._selection_key = selected, expected, (slot.epoch, identity)
    return selected, expected

  def latest(self):
    now = self._clock_ns()
    with self._lock:
      sample = self._sample
      if sample is None:
        return None
      if (not _integer(now, sample.host_start_ns) or now - sample.host_start_ns >= self._max_sample_age_ns
          or not sample.gps.fresh_at(now)):
        self._generation += 1
        self._retire_locked("phone_sample_expired")
        return None
      return sample

  def drain_rejection_events(self) -> dict:
    """Atomically take every queued rejection record; nothing is left behind."""
    with self._lock:
      items = list(self._rejection_queue)
      self._rejection_queue.clear()
      dropped, self._rejection_dropped = self._rejection_dropped, 0
    return {"events": items, "dropped_count": dropped}

  def status(self):
    with self._lock:
      return PhoneRouteProducerStatus(self._thread is not None and self._thread.is_alive(), self._reason,
                                      self._published_session, self._published_sequence, self._connected, self._cleanup_verified,
                                      self._last_error_chain, self._cleanup_diagnostics,
                                      copy.deepcopy(self._last_rejection), copy.deepcopy(self._last_rejection_diagnosis))

  def close(self, timeout=4.0):
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 <= timeout <= 4:
      raise ValueError("phone_producer_close_timeout_invalid")
    self._stop.set()
    self._advance_generation()
    self._withdraw("phone_producer_stopping")
    with self._lock:
      self._closed = True
      thread = self._thread
    if thread is not None:
      thread.join(timeout)
    stopped = thread is None or not thread.is_alive()
    if stopped:
      with self._lock:
        self._reason = "phone_producer_stopped"
        self._connected = False
    return stopped

  def _take_sample(self, device, native, gps, generation=None):
    """Single rejection path: a whitelisted transient rejection degrades to an inactive slot,
    every other reason invalidates completely."""
    if generation is None:
      generation = self._active()
      if generation is None:
        raise _Stopped()
    try:
      return self._sample_once(device, native, gps, generation)
    except _Reconnect as error:
      # A disconnect/epoch change keeps no geometry and only retires the generation that
      # produced it: a late disconnect from an old generation must not clear data a newer
      # generation already accepted.
      applied = self._withdraw(_reason(error), expected_generation=generation)
      self._record_rejection(error, generation=generation, stale_generation=not applied)
      raise
    except ValueError as error:
      self._on_rejection(error, self._sequence, generation)
      raise

  def _publish(self, sample, generation):
    """Re-check the same generation between completion and publish; session/stop alone is not enough."""
    with self._lock:
      if (generation != self._generation or self._stop.is_set() or self._closed
          or sample.transport_session != self._session):
        return False
      self._sample, self._reason = sample, "accepted"
      self._published_sequence = sample.sequence
      self._published_session = sample.transport_session
      return True

  def _observe_pair(self, device, native, gps, selected, before, generation):
    bridge = self._bridge_observer
    if bridge is None:
      bridge = self._bridge_observer = self._bridge_factory()
    original_start = previous_fix = retry = None
    epoch = native.epoch
    anchor_fields = ("segment_index", "link_index", "point_index")

    def check_retry():
      active = self._active()
      if active is None:
        raise _Stopped()
      if active != generation:
        raise _Superseded()
      if native.epoch != epoch:
        raise _Reconnect("phone_provider_epoch_changed")
      now = self._clock_ns()
      if not _integer(now, original_start) or now - original_start >= self._max_sample_age_ns:
        raise PhoneRouteProducerRejected("phone_anchor_retry_deadline_exceeded")

    for attempt in range(2):
      if attempt:
        check_retry()
      first_identity, start, first_finish = _native_identity(before, epoch)
      if first_identity != selected.identity_hash:
        raise PhoneRouteProducerRejected("phone_selected_identity_changed")
      if original_start is None:
        original_start = start
      fix, _, _ = gps.observe()
      if not isinstance(fix, AndroidGpsFix):
        raise _Reconnect("phone_gps_fix_type_invalid")
      if self._gps_identity is not None:
        boot, elapsed, token = self._gps_identity
        if fix.phone_boot_id != boot or fix.elapsed_realtime_ns < elapsed:
          raise _Reconnect("phone_gps_epoch_or_time_changed")
        if fix.elapsed_realtime_ns == elapsed and fix.source_token != token:
          raise _Reconnect("phone_gps_source_token_conflict")
      if previous_fix is not None:
        if fix.phone_boot_id != previous_fix.phone_boot_id or fix.elapsed_realtime_ns < previous_fix.elapsed_realtime_ns:
          raise _Reconnect("phone_gps_epoch_or_time_changed")
        if fix.elapsed_realtime_ns == previous_fix.elapsed_realtime_ns:
          if fix.source_token != previous_fix.source_token:
            raise _Reconnect("phone_gps_source_token_conflict")
          if fix.coordinates != previous_fix.coordinates:
            raise _Reconnect("phone_gps_source_content_conflict")
      if attempt:
        check_retry()
      try:
        broadcast = self._bridge_observe(device, bridge)
      except Exception:
        broadcast = {"status": "unavailable", "reason": "bridge_read_failed"}
      if not isinstance(broadcast, Mapping):
        broadcast = {"status": "unavailable", "reason": "phone_broadcast_observation_invalid"}
      if attempt:
        check_retry()
      after = native.observe_position_navi_info()
      last_identity, last_start, finish = _native_identity(after, epoch)
      if attempt:
        check_retry()
      now = self._clock_ns()
      broadcast_started = broadcast.get("host_read_started_ns")
      broadcast_finished = broadcast.get("host_read_finished_ns")
      broadcast_bracketed = (
        _integer(broadcast_started, fix.host_finished_ns) and _integer(broadcast_finished, broadcast_started)
        and broadcast_finished <= last_start)
      if broadcast.get("status") == "unavailable" and not broadcast_bracketed:
        broadcast = {"status": "unavailable", "reason": "phone_broadcast_window_unavailable"}
      if (last_identity != first_identity or not first_finish <= fix.host_started_ns <= fix.host_finished_ns <= last_start
          or not _integer(now, finish) or now - original_start >= self._max_sample_age_ns or not fix.fresh_at(now)):
        raise PhoneRouteProducerRejected("phone_pair_inconsistent_or_stale")
      first_anchor, last_anchor = (tuple(value["_position"][name] for name in anchor_fields) for value in (before, after))
      if first_anchor == last_anchor:
        if retry is not None:
          retry.update(recovered=True, recovery_host_start_ns=start, recovery_host_finished_ns=finish)
        return before, after, fix, broadcast, start, _freeze(retry)
      if attempt:
        raise PhoneRouteProducerRejected("phone_anchor_changed_during_read")
      retry = {"schema_version": 1, "source": "phone_native_gps_native", "extra_pair_observations": 1,
               "recovered": False, "original_host_start_ns": original_start, "original_host_finished_ns": finish,
               "anchor_before": dict(zip(anchor_fields, first_anchor, strict=True)),
               "anchor_after": dict(zip(anchor_fields, last_anchor, strict=True))}
      before, previous_fix = after, fix
    raise AssertionError("bounded_anchor_pair_loop_exhausted")

  def _sample_once(self, device, native, gps, generation):
    if self._stop.is_set():
      raise _Stopped()
    active = self._active()
    if active is None:
      raise _Stopped()
    if generation != active:
      raise _Superseded()
    with self._lock:
      if self._stop.is_set() or self._closed:
        raise _Stopped()
      if generation != self._generation:
        raise _Superseded()
      epoch_changed = self._provider_epoch is not None and self._provider_epoch != native.epoch
      self._provider_epoch = native.epoch
    if epoch_changed:
      raise _Reconnect("phone_provider_epoch_changed")
    before = native.observe_position_navi_info()
    identity, _, _ = _native_identity(before, native.epoch)
    key = (native.epoch, identity)
    with self._lock:
      if self._stop.is_set() or self._closed:
        raise _Stopped()
      if generation != self._generation:
        raise _Superseded()
      selected, cache, selected_key = self._selection, self._cache, self._selection_key
    if selected is None or key != selected_key:
      # Invalidate first: stale active geometry must never survive in locals on a failure
      # path. Claim the inactive slot atomically, checking this round's token/stop/closed
      # in the same critical section, so a dead round cannot take or clear a newer slot.
      with self._lock:
        if self._stop.is_set() or self._closed:
          raise _Stopped()
        if generation != self._generation:
          raise _Superseded()
        recovering, self._inactive = self._inactive, None
      selected = cache = None
      if not self._withdraw("phone_selection_refreshing", advance=False, expected_generation=generation):
        if self._stop.is_set() or self._closed:
          raise _Stopped()
        raise _Superseded()
      if recovering is not None:
        # Recovery allows only two bounded reads of one file; on a hit the
        # Native-before -> GPS -> Native-after evidence is still sampled fresh.
        selected, cache = self._recover_inactive(recovering, device, native, identity, generation) or (None, None)
        if (not isinstance(selected, SelectedCacheRoute) or not isinstance(cache, bytes)
            or not 0 < len(cache) <= MAX_FILE_BYTES
            or hashlib.sha256(cache).hexdigest() != selected.cache_sha256
            or native.epoch != key[0]
            or _native_identity(native.observe_position_navi_info(), native.epoch)[0] != identity
            or selected.identity_hash != identity):
          selected = cache = None
          if not self._withdraw("phone_selection_recovering", advance=False, expected_generation=generation):
            if self._stop.is_set() or self._closed:
              raise _Stopped()
            raise _Superseded()
      if selected is None:
        observe_geometry = getattr(native, "observe_geometry", None)
        native_geometry = None
        if callable(observe_geometry):
          try:
            native_geometry = observe_geometry()
          except native_module.ProbeError as error:
            if str(error) != "native_probe_geometry_unsupported":
              raise
        if native_geometry is not None:
          if native_geometry.get("path_identity_hash") != identity:
            raise PhoneRouteProducerRejected("phone_selected_identity_changed")
          try:
            selected = select_native(native_geometry, epoch=native.epoch)
          except LiveRouteRejected as error:
            raise PhoneRouteProducerRejected(str(error)) from None
          cache = b""
        else:
          selected, _, files = self._collector(device, native)
          if (not isinstance(selected, SelectedCacheRoute) or not isinstance(files, dict)
              or selected.identity_hash != identity):
            raise PhoneRouteProducerRejected("phone_selected_identity_changed")
          cache = files.get(selected.source_path)
          if (not isinstance(cache, bytes) or not 0 < len(cache) <= MAX_FILE_BYTES
              or hashlib.sha256(cache).hexdigest() != selected.cache_sha256):
            raise PhoneRouteProducerRejected("phone_selected_cache_invalid")
        with self._lock:
          # Even a finished in-flight collector must not write stale geometry back or
          # displace a newer inactive slot.
          if self._stop.is_set() or self._closed:
            raise _Stopped()
          if generation != self._generation:
            raise _Superseded()
          self._selection, self._cache, self._selection_key = selected, cache, key
      # Cache collection or recovery reads can take around a second, so a position lease
      # is never built from an earlier observation.
      before = native.observe_position_navi_info()
    before, after, fix, broadcast, start, anchor_retry = self._observe_pair(
      device, native, gps, selected, before, generation)
    with self._lock:
      if start <= self._last_host_start:
        raise PhoneRouteProducerRejected("phone_observation_replayed")
      if self._stop.is_set() or self._closed:
        raise _Stopped()
      if generation != self._generation:
        raise _Superseded()
      # Keep this already validated, unpublished new-route pair only for an immediate
      # generation-checked handoff. Its original clocks and GPS lease cannot be renewed.
      if self._route_source_session is not None and selected.source_session_id != self._route_source_session:
        pending = PhoneRouteSample(selected, cache, selected.source_path, native.epoch, self._sequence + 1, start, fix,
                                   _freeze(before), _freeze(after), self._session, _freeze(broadcast), anchor_retry)
        raise _SourceSessionChanged(pending, generation)
      self._route_source_session = selected.source_session_id
      self._retained = (selected, cache)
      session = self._session
      self._last_host_start = start
      self._sequence += 1
      sequence = self._sequence
      self._gps_identity = fix.phone_boot_id, fix.elapsed_realtime_ns, fix.source_token
    return PhoneRouteSample(selected, cache, selected.source_path, native.epoch, sequence, start, fix,
                            _freeze(before), _freeze(after), session, _freeze(broadcast), anchor_retry)

  def _run(self):
    while not self._stop.is_set():
      resources = []
      handled_rejection = None
      self._advance_generation()
      with self._lock:
        self._session = uuid.uuid4().hex
        self._sequence = self._last_host_start = 0
        self._published_sequence = 0
        self._published_session = None
        self._gps_identity = None
        self._provider_epoch = None
        self._route_source_session = None
        self._cleanup_verified = False
      try:
        with ExitStack() as stack:
          device = self._device_factory(self._stop)
          resources.append(device)
          stack.enter_context(device)
          with self._lock:
            self.connected_adb_ownership = getattr(device, "ownership", None)
            self.connected_adb_port = getattr(device, "port", None)
          native = self._native_factory(device, self._stop)
          resources.append(native)
          stack.enter_context(native)
          bridge_service = self._bridge_service_factory(device, self._stop)
          resources.append(bridge_service)
          stack.enter_context(bridge_service)
          gps = self._gps_factory(device, self._stop)
          resources.append(gps)
          stack.enter_context(gps)
          with self._lock:
            self._connected = True
          transient_retry_pending = False
          while not self._stop.is_set():
            handled_rejection = None
            try:
              generation = self._active()
              if generation is None:
                break
              sample = self._take_sample(device, native, gps, generation)
              if not self._publish(sample, generation):
                if self._stop.is_set() or self._closed:
                  raise _Stopped()
                raise _Superseded()
            except _Superseded:
              continue
            except ValueError as error:
              # Invalidation already happened in _take_sample with this round's token;
              # do not withdraw unconditionally a second time here.
              handled_rejection = error
              if isinstance(error, _SourceSessionChanged) and native.process is not None and gps.process is not None:
                adopted = self._adopt_source_sample(error, expected_generation=generation + 1)
                if adopted is None:
                  self._rollover_source_transport(expected_generation=generation + 1)
                  transient_retry_pending = False
                  continue
                sample, generation = adopted
                if not self._publish(sample, generation):
                  continue
                handled_rejection = None
              elif isinstance(error, _Reconnect) or native.process is None or gps.process is None:
                raise
            # A route snapshots refresh and a position tick can land between the two
            # native reads. The producer already retired this round atomically; retry
            # immediately on the same validated phone resources. Waiting here exposes
            # one or more polls with no latest sample and forces an avoidable map hole.
            if (handled_rejection is not None and not transient_retry_pending
                and _reason(handled_rejection) in _STALE_WHILE_REFRESH_REASONS):
              # Only one no-wait retry is allowed. If the same race persists, retain the
              # bounded sampling interval instead of spinning synchronously on ADB.
              transient_retry_pending = True
              continue
            transient_retry_pending = False
            if self._stop.wait(self._interval_s):
              break
      except _Superseded:
        pass
      except _Stopped:
        pass
      except Exception as error:
        if error is not handled_rejection:
          self._record_rejection(error)
        self._withdraw(_reason(error))
      finally:
        with self._lock:
          self._connected = False
          self._cleanup_verified = all(getattr(resource, "cleanup_verified", False) for resource in resources)
          diagnostics = tuple(value for resource in resources for value in getattr(resource, "cleanup_diagnostics", ()))
          if diagnostics:
            self._cleanup_diagnostics = diagnostics
        self._withdraw("phone_producer_stopping" if self._stop.is_set() else self.status().reason)
      if self._stop.wait(min(1.0, self._interval_s)):
        break
