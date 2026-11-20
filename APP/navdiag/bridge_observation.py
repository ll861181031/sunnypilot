from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
import math
import re
import shlex
import struct
import subprocess
import time

if __package__:
  from .device import adb_client_argv
else:
  from device import adb_client_argv


PACKAGE = "com.jixiexiaoge.drivingassist.navdiag"
LATEST_PATH = "files/navdiag-bridge/latest.json"
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_DURATION_MS = 7_200_000
MAX_SOURCE_AGE_S = 3.0
PREFIX = b"NAVDIAG_BRIDGE_V1\n"
TRAILER = re.compile(rb"\nNAVDIAG_BRIDGE_RC=([0-9]{3})\n\Z")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")


class BridgeError(ValueError):
  pass


@dataclass(frozen=True)
class PhoneClockSample:
  elapsed_ns: int
  boot_id: str
  resolution_ns: int = 10_000_000


@dataclass(frozen=True)
class PhoneReadWindow:
  phone_before_ns: int
  phone_after_ns: int
  host_started_ns: int
  host_finished_ns: int
  device_boot_id: str
  device_resolution_ns: int = 10_000_000

  def validate(self) -> None:
    numbers = (self.phone_before_ns, self.phone_after_ns, self.host_started_ns,
               self.host_finished_ns, self.device_resolution_ns)
    if any(type(value) is not int or value < 0 for value in numbers):
      raise BridgeError("invalid_clock_window")
    if (self.phone_after_ns < self.phone_before_ns or self.host_finished_ns < self.host_started_ns
        or not 1 <= self.device_resolution_ns <= 1_000_000_000
        or UUID.fullmatch(self.device_boot_id) is None):
      raise BridgeError("invalid_clock_window")
    host_span = self.host_finished_ns - self.host_started_ns
    phone_span = self.phone_after_ns - self.phone_before_ns
    if host_span > 10_000_000_000 or phone_span > host_span + 2 * self.device_resolution_ns:
      raise BridgeError("inconsistent_clock_window")


def _command(device, remote: str) -> list[str]:
  return adb_client_argv(device, "-s", device.serial, "shell", "-T", remote)


def read_latest(device, *, runner: Callable = subprocess.run) -> bytes:
  script = "".join(["printf 'NAVDIAG_BRIDGE_V1\\n'; ",
                    f"if [ -L files ] || [ -L files/navdiag-bridge ] || [ -L {LATEST_PATH} ] ",
                    f"|| [ ! -f {LATEST_PATH} ]; then navdiag_rc=44; ",
                    f"else head -c {MAX_FILE_BYTES + 1} {LATEST_PATH}; navdiag_rc=$?; fi; ",
                    "printf '\\nNAVDIAG_BRIDGE_RC=%03d\\n' \"$navdiag_rc\"; exit \"$navdiag_rc\""])
  try:
    result = runner(_command(device, f"run-as {PACKAGE} sh -c {shlex.quote(script)}"),
                    capture_output=True, timeout=5, check=False)
  except (OSError, subprocess.SubprocessError):
    raise BridgeError("bridge_read_failed") from None
  raw = result.stdout
  if not isinstance(raw, bytes) or len(raw) > MAX_FILE_BYTES + len(PREFIX) + 128:
    raise BridgeError("bridge_file_size_limit")
  end = TRAILER.search(raw)
  if result.returncode != 0 or not raw.startswith(PREFIX) or end is None or end.group(1) != b"000":
    raise BridgeError("bridge_read_failed")
  data = raw[len(PREFIX):end.start()]
  if not data or len(data) > MAX_FILE_BYTES:
    raise BridgeError("bridge_file_size_limit")
  return data


def read_phone_clock(device, *, runner: Callable = subprocess.run) -> PhoneClockSample:
  remote = "head -c 64 /proc/sys/kernel/random/boot_id; head -c 128 /proc/uptime"
  try:
    result = runner(_command(device, remote), capture_output=True, timeout=5, check=False)
    if result.returncode != 0 or not isinstance(result.stdout, bytes) or len(result.stdout) > 192:
      raise BridgeError("phone_clock_unavailable")
    lines = result.stdout.decode("ascii").splitlines()
    if len(lines) != 2 or UUID.fullmatch(lines[0]) is None:
      raise BridgeError("phone_clock_unavailable")
    values = lines[1].split()
    if len(values) != 2 or re.fullmatch(r"[0-9]+\.[0-9]{1,9}", values[0]) is None:
      raise BridgeError("phone_clock_unavailable")
    fraction = len(values[0].split(".")[1])
    return PhoneClockSample(int(Decimal(values[0]) * 1_000_000_000), lines[0], 10 ** (9 - fraction))
  except (OSError, subprocess.SubprocessError, UnicodeError, ArithmeticError):
    raise BridgeError("phone_clock_unavailable") from None


def _integer(value, reason: str, *, minimum: int = 0, maximum: int = 2 ** 63 - 1) -> int:
  if type(value) is not int or not minimum <= value <= maximum:
    raise BridgeError(reason)
  return value


def _pairs(pairs: list[tuple[str, object]]) -> dict:
  result = {}
  for key, value in pairs:
    if key in result:
      raise BridgeError("duplicate_json_key")
    result[key] = value
  return result


def _reject_constant(_):
  raise BridgeError("nonfinite_json_number")


class _TypedDecoder:
  def __init__(self):
    self.nodes = 0

  def value(self, node, depth: int = 0):
    self.nodes += 1
    if self.nodes > 20000 or depth > 16 or not isinstance(node, dict):
      raise BridgeError("typed_value_limit")
    kind, value = node.get("type"), node.get("value")
    if kind == "null" and value is None:
      return None
    if kind == "string" and isinstance(value, str) and len(value) <= 768 * 1024:
      return value
    if kind == "char" and isinstance(value, str) and len(value) == 1:
      return value
    if kind == "boolean" and type(value) is bool:
      return value
    bounds = {"byte": (-128, 127), "short": (-32768, 32767), "int": (-2 ** 31, 2 ** 31 - 1)}
    if kind in bounds:
      low, high = bounds[kind]
      return _integer(value, "typed_integer_invalid", minimum=low, maximum=high)
    if kind == "long" and isinstance(value, str) and re.fullmatch(r"-?(?:0|[1-9][0-9]{0,18})", value):
      return _integer(int(value), "typed_integer_invalid", minimum=-2 ** 63)
    if kind in ("double", "float") and type(value) in (int, float) and math.isfinite(value):
      bits = node.get("bits")
      maximum, fmt = (16, "d") if kind == "double" else (8, "f")
      if not isinstance(bits, str) or re.fullmatch(r"[0-9a-f]{1," + str(maximum) + r"}", bits) is None:
        raise BridgeError("typed_float_bits_invalid")
      try:
        actual = int.from_bytes(struct.pack("<" + fmt, value), "little")
      except (OverflowError, struct.error):
        raise BridgeError("typed_float_bits_invalid") from None
      if actual != int(bits, 16):
        raise BridgeError("typed_float_bits_invalid")
      return value
    if kind == "bundle" and isinstance(value, dict) and len(value) <= 512:
      if (type(node.get("length")) is not int or type(node.get("capturedLength")) is not int
          or node["length"] != len(value) or node["capturedLength"] != len(value)
          or any(not isinstance(key, str) or len(key) > 256 for key in value)):
        raise BridgeError("typed_collection_incomplete")
      return {key: self.value(child, depth + 1) for key, child in value.items()}
    if kind in ("array", "list") and isinstance(value, list) and len(value) <= 16384:
      if (type(node.get("length")) is not int or type(node.get("capturedLength")) is not int
          or node["length"] != len(value) or node["capturedLength"] != len(value)):
        raise BridgeError("typed_collection_incomplete")
      return [self.value(child, depth + 1) for child in value]
    raise BridgeError("unsupported_typed_value")


def _decode_record(record, key: str, root: dict) -> tuple[dict, dict]:
  if not isinstance(record, dict) or type(record.get("keyType")) is not int or record["keyType"] != int(key):
    raise BridgeError("record_type_mismatch")
  sequence = _integer(record.get("sequence"), "invalid_record_sequence", minimum=1)
  if sequence > root["sequence"]:
    raise BridgeError("invalid_record_sequence")
  received = _integer(record.get("receivedElapsedNs"), "invalid_source_time", minimum=1)
  _integer(record.get("receivedWallMs"), "invalid_source_wall", minimum=1)
  if not root["startedElapsedNs"] <= received <= root["publishedElapsedNs"]:
    raise BridgeError("invalid_source_time")
  expected_age_ms = (root["publishedElapsedNs"] - received) // 1_000_000
  if _integer(record.get("sourceAgeMs"), "invalid_source_age") != expected_age_ms:
    raise BridgeError("invalid_source_age")
  if (record.get("complete") is not True or type(record.get("decodeErrors")) is not int
      or record["decodeErrors"] != 0 or record.get("senderVerified") is not False):
    raise BridgeError("record_incomplete")
  raw = record.get("rawIntent")
  if (not isinstance(raw, dict) or raw.get("action") != "AUTONAVI_STANDARD_BROADCAST_SEND"
      or raw.get("complete") is not True or raw.get("truncated") is not False or raw.get("issues") != []):
    raise BridgeError("intent_incomplete")
  extras = raw.get("extras")
  if not isinstance(extras, dict) or extras.get("type") != "bundle":
    raise BridgeError("intent_extras_missing")
  fields = _TypedDecoder().value(extras)
  key_type = extras["value"].get("KEY_TYPE")
  if (not isinstance(key_type, dict) or key_type.get("type") != "int"
      or type(fields.get("KEY_TYPE")) is not int or fields["KEY_TYPE"] != int(key)):
    raise BridgeError("record_type_mismatch")
  return fields, extras["value"]


def _decode(data: bytes, window: PhoneReadWindow) -> dict:
  if not isinstance(data, bytes) or not 1 <= len(data) <= MAX_FILE_BYTES:
    raise BridgeError("bridge_file_size_limit")
  try:
    root = json.loads(data.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_reject_constant)
  except (ValueError, UnicodeError, RecursionError) as error:
    if isinstance(error, BridgeError):
      raise
    raise BridgeError("invalid_bridge_json") from None
  if not isinstance(root, dict) or type(root.get("schemaVersion")) is not int or root["schemaVersion"] != 1:
    raise BridgeError("unsupported_bridge_schema")
  if not isinstance(root.get("sessionId"), str) or UUID.fullmatch(root["sessionId"]) is None:
    raise BridgeError("invalid_receiver_session")
  if root.get("active") is not True or root.get("valid") is not True:
    raise BridgeError("receiver_inactive_or_invalid")
  if root.get("status") != "receiving" or root.get("reason") != "receiver_registered" or root.get("lastError") != "":
    raise BridgeError("receiver_failed")
  if any(root.get(key) is not False for key in ("activeRouteVerified", "controlInputAllowed", "senderVerified", "freshnessChecked")):
    raise BridgeError("unexpected_bridge_trust_claim")
  for key in ("dropped", "overflow", "queueOverflow", "decodeErrors"):
    if key in root and root[key] not in (0, False):
      raise BridgeError("bridge_loss_reported")
  for key in ("sequence", "written", "startedElapsedNs", "publishedElapsedNs"):
    _integer(root.get(key), "invalid_bridge_counter", minimum=1)
  _integer(root.get("ignored"), "invalid_bridge_counter")
  duration = _integer(root.get("maxDurationMs"), "invalid_session_duration", minimum=1, maximum=MAX_DURATION_MS)
  if root.get("maxFileBytes") != MAX_FILE_BYTES:
    raise BridgeError("invalid_bridge_limit")
  if not root["startedElapsedNs"] <= root["publishedElapsedNs"] <= window.phone_after_ns + window.device_resolution_ns:
    raise BridgeError("future_phone_timestamp")
  if window.phone_after_ns + window.device_resolution_ns - root["startedElapsedNs"] > duration * 1_000_000:
    raise BridgeError("receiver_session_expired")
  records = root.get("records")
  if (not isinstance(records, dict) or not records or set(records) - {"10001", "10019"}
      or not len(records) <= root["written"] <= root["sequence"]
      or root["ignored"] > root["sequence"]):
    raise BridgeError("invalid_bridge_records")
  for key, record in records.items():
    _decode_record(record, key, root)
  return root


class BridgeObserver:
  def __init__(self, max_source_age_s: float = 0.5):
    if type(max_source_age_s) not in (int, float) or not math.isfinite(max_source_age_s) or not 0 < max_source_age_s <= MAX_SOURCE_AGE_S:
      raise ValueError("广播源时效必须大于0且不超过3秒")
    self.max_source_age_ns = int(max_source_age_s * 1_000_000_000)
    self._sessions: OrderedDict[tuple[str, str], dict] = OrderedDict()
    self._current: tuple[str, str] | None = None

  @staticmethod
  def _unavailable(reason: str) -> dict:
    return {"schema_version": 1, "source": "adb_broadcast_observation", "status": "unavailable", "reason": reason,
            "sender_verified": False, "active_route_verified": False, "route_identity_verified": False,
            "control_input_allowed": False, "records": {}}

  def observe(self, data: bytes, window: PhoneReadWindow) -> dict:
    try:
      window.validate()
      root = _decode(data, window)
      identity = (window.device_boot_id, root["sessionId"])
      if identity in self._sessions and self._current != identity:
        raise BridgeError("retired_receiver_session_replayed")
      previous = self._sessions.get(identity, {"sequence": 0, "records": {}})
      if root["sequence"] < previous["sequence"]:
        raise BridgeError("receiver_sequence_regressed")
      observed, next_records = {}, {}
      for key, record in root["records"].items():
        fields, typed = _decode_record(record, key, root)
        fingerprint = hashlib.sha256(json.dumps({k: v for k, v in record.items() if k != "sourceAgeMs"},
                                               sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        prior = previous["records"].get(key)
        replayed = prior is not None and record["sequence"] == prior["sequence"]
        if prior and (record["sequence"] < prior["sequence"] or record["receivedElapsedNs"] < prior["source_ns"]):
          raise BridgeError("record_sequence_or_time_regressed")
        if replayed and fingerprint != prior["fingerprint"]:
          raise BridgeError("same_sequence_content_changed")
        age_upper_ns = window.phone_after_ns + window.device_resolution_ns - record["receivedElapsedNs"]
        # A receiver may legitimately publish less often than the producer samples it.
        # The first read establishes a conservative host receipt anchor; later reads of
        # the exact same sequence must not move that anchor forward or backward.
        host_received_ns = window.host_started_ns - age_upper_ns
        if replayed:
          host_received_ns = prior["host_received_ns"]
          age_upper_ns = window.host_finished_ns - host_received_ns
        fresh = age_upper_ns <= self.max_source_age_ns
        observed[key] = {"key_type": int(key), "sequence": record["sequence"], "fresh": fresh,
                         "replayed": replayed, "phone_received_elapsed_ns": record["receivedElapsedNs"],
                         "phone_received_wall_ms": record["receivedWallMs"], "source_age_upper_ns": age_upper_ns,
                         "conservative_host_received_ns": host_received_ns, "fields": fields, "typed_fields": typed}
        next_records[key] = {"sequence": record["sequence"], "source_ns": record["receivedElapsedNs"],
                             "host_received_ns": host_received_ns, "fingerprint": fingerprint}
      next_records = {**previous["records"], **next_records}
      self._sessions[identity] = {"sequence": root["sequence"], "records": next_records}
      self._sessions.move_to_end(identity)
      if len(self._sessions) > 64:
        self._sessions.pop(identity)
        raise BridgeError("receiver_session_history_limit")
      self._current = identity
      navigation = observed.get("10001")
      result = self._unavailable("10001_missing" if navigation is None else "10001_stale")
      result.update(receiver_session_id=root["sessionId"], receiver_sequence=root["sequence"],
                    receiver_active=True, bridge_transport_valid=True, records=observed,
                    host_read_started_ns=window.host_started_ns, host_read_finished_ns=window.host_finished_ns,
                    max_source_age_ns=self.max_source_age_ns, phone_clock_domain="elapsed_realtime")
      if navigation is not None and navigation["fresh"]:
        result.update(status="observed", reason="fresh_10001_observation")
      return result
    except (BridgeError, TypeError, OverflowError, KeyError) as error:
      return self._unavailable(str(error) if isinstance(error, BridgeError) else "invalid_bridge_value")


def observe_device(device, observer: BridgeObserver, *, read: Callable = read_latest,
                   phone_clock: Callable = read_phone_clock, host_clock: Callable = time.monotonic_ns) -> dict:
  try:
    started = host_clock()
    before = phone_clock(device)
    data = read(device)
    after = phone_clock(device)
    finished = host_clock()
    if before.boot_id != after.boot_id:
      raise BridgeError("phone_reboot_during_read")
    window = PhoneReadWindow(before.elapsed_ns, after.elapsed_ns, started, finished, before.boot_id,
                             max(before.resolution_ns, after.resolution_ns))
    return observer.observe(data, window)
  except (BridgeError, OSError, subprocess.SubprocessError) as error:
    return observer._unavailable(str(error) if isinstance(error, BridgeError) else "bridge_read_failed")
