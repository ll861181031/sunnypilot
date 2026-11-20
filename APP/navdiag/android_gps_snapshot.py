from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import math
import re


MAX_DUMP_BYTES = 2 * 1024 * 1024
MAX_WINDOW_NS = 1_000_000_000
DEFAULT_MAX_AGE_NS = 2_000_000_000
PRINTED_TIME_RESOLUTION_NS = 1_000_000
_BOOT_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_TIME_UNITS_NS = {"d": 86_400_000_000_000, "h": 3_600_000_000_000, "m": 60_000_000_000,
                  "s": 1_000_000_000, "ms": PRINTED_TIME_RESOLUTION_NS}


class GpsSnapshotRejected(ValueError):
  pass


def _integer(value, name: str, *, minimum: int = 0) -> int:
  if type(value) is not int or not minimum <= value < 1 << 63:
    raise GpsSnapshotRejected("invalid_" + name)
  return value


def parse_elapsed_time(text: str) -> int:
  if not isinstance(text, str) or not text.startswith("+") or len(text) > 64:
    raise GpsSnapshotRejected("invalid_elapsed_time")
  parts = list(re.finditer(r"(\d+)(ms|d|h|m|s)", text[1:]))
  if not parts or "".join(part.group(0) for part in parts) != text[1:]:
    raise GpsSnapshotRejected("invalid_elapsed_time")
  units = [part.group(2) for part in parts]
  if units != sorted(set(units), key=lambda name: -_TIME_UNITS_NS[name]):
    raise GpsSnapshotRejected("invalid_elapsed_time")
  result = sum(int(part.group(1)) * _TIME_UNITS_NS[part.group(2)] for part in parts)
  return _integer(result, "elapsed_time", minimum=1)


def _clock_window(window: dict) -> tuple[str, int, int, int, int, int, int]:
  if not isinstance(window, dict):
    raise GpsSnapshotRejected("invalid_clock_window")
  try:
    host_start = _integer(window["host_started_ns"], "host_start", minimum=1)
    host_finish = _integer(window["host_finished_ns"], "host_finish", minimum=1)
    before, after = window["phone_before"], window["phone_after"]
    if not isinstance(before, dict) or not isinstance(after, dict):
      raise GpsSnapshotRejected("invalid_phone_clock")
    boot = before["boot_id"]
    if not isinstance(boot, str) or _BOOT_ID.fullmatch(boot) is None or after["boot_id"] != boot:
      raise GpsSnapshotRejected("phone_boot_changed_or_invalid")
    phone_before = _integer(before["elapsed_ns"], "phone_before", minimum=1)
    phone_after = _integer(after["elapsed_ns"], "phone_after", minimum=1)
    resolution_before = _integer(before["resolution_ns"], "clock_resolution", minimum=1)
    resolution_after = _integer(after["resolution_ns"], "clock_resolution", minimum=1)
  except KeyError:
    raise GpsSnapshotRejected("incomplete_clock_window") from None
  if not 0 <= host_finish - host_start <= MAX_WINDOW_NS:
    raise GpsSnapshotRejected("host_read_window_too_long_or_reversed")
  if (resolution_before > 100_000_000 or resolution_after > 100_000_000 or phone_after < phone_before
      or phone_after - phone_before > host_finish - host_start + resolution_before + resolution_after):
    raise GpsSnapshotRejected("phone_clock_window_inconsistent")
  return boot, phone_before, phone_after, resolution_before, resolution_after, host_start, host_finish


@dataclass(frozen=True)
class GpsSnapshot:
  boot_id: str = field(repr=False)
  elapsed_realtime_ns: int = field(repr=False)
  horizontal_accuracy_m: float
  age_lower_ns: int
  age_upper_ns: int
  host_started_ns: int
  host_finished_ns: int
  max_age_ns: int
  coordinates_in_dump: bool
  coordinates: tuple[float, float] | None = field(default=None, repr=False)

  @property
  def source_token(self) -> str:
    data = f"android-gps-location-v1\x00{self.boot_id}\x00{self.elapsed_realtime_ns}".encode("ascii")
    return hashlib.sha256(data).hexdigest()

  def fresh_at(self, now_ns: int) -> bool:
    return (type(now_ns) is int and now_ns >= self.host_finished_ns
            and self.age_upper_ns + now_ns - self.host_finished_ns < self.max_age_ns)

  def summary(self) -> dict:
    return {"source": "android_fine_last_known_gps", "clock": "phone_elapsed_realtime_printed_ms",
            "source_token": self.source_token, "horizontal_accuracy_m": self.horizontal_accuracy_m,
            "age_lower_ns": self.age_lower_ns, "age_upper_ns": self.age_upper_ns,
            "host_started_ns": self.host_started_ns, "host_finished_ns": self.host_finished_ns,
            "coordinates_in_dump": self.coordinates_in_dump, "coordinates_included": self.coordinates is not None,
            "source_elapsed_time_fresh": self.fresh_at(self.host_finished_ns),
            "gnss_hardware_authenticity_verified": False, "amap_native_pose_freshness_verified": False,
            "coordinate_datum_globally_verified": False, "control_input_allowed": False}


def parse_gps_snapshot(raw: bytes | str, window: dict, *, max_age_ns: int = DEFAULT_MAX_AGE_NS,
                       max_accuracy_m: float = 20.0, include_coordinates: bool = False) -> GpsSnapshot:
  _integer(max_age_ns, "max_age", minimum=1)
  if (type(max_accuracy_m) not in (float, int) or not math.isfinite(max_accuracy_m)
      or not 0 < max_accuracy_m <= 1000 or type(include_coordinates) is not bool):
    raise GpsSnapshotRejected("invalid_parser_limits")
  if isinstance(raw, bytes):
    if len(raw) > MAX_DUMP_BYTES:
      raise GpsSnapshotRejected("dump_size_limit")
    try:
      raw = raw.decode("utf-8")
    except UnicodeDecodeError:
      raise GpsSnapshotRejected("dump_not_utf8") from None
  if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_DUMP_BYTES or "\x00" in raw:
    raise GpsSnapshotRejected("invalid_dump")
  boot, phone_before, phone_after, _, resolution_after, host_start, host_finish = _clock_window(window)
  lines = raw.splitlines()
  sections = [index for index, line in enumerate(lines) if line.strip() == "Last Known Locations:"]
  if len(sections) != 1:
    raise GpsSnapshotRejected("fine_location_section_missing_or_ambiguous")
  start = sections[0] + 1
  end = next((index for index in range(start, len(lines))
              if lines[index].strip() == "Last Known Locations Coarse Intervals:"), None)
  if end is None:
    raise GpsSnapshotRejected("fine_location_section_not_closed")
  gps_lines = [line.strip() for line in lines[start:end] if re.match(r"\s*gps:", line)]
  if len(gps_lines) != 1:
    raise GpsSnapshotRejected("fine_gps_missing_or_ambiguous")
  match = re.fullmatch(r"gps:\s+Location\[gps\s+([^\s]+)\s+(.*)", gps_lines[0])
  if match is None:
    raise GpsSnapshotRejected("fine_gps_provider_or_format_invalid")
  coordinate_text, fields = match.groups()
  fields = fields.split("extras=", 1)[0]
  times = re.findall(r"\bet=([^\s\]]+)", fields)
  accuracies = re.findall(r"\bhAcc=([^\s\]]+)", fields)
  if len(times) != 1 or len(accuracies) != 1:
    raise GpsSnapshotRejected("gps_time_or_accuracy_missing_or_ambiguous")
  timestamp = parse_elapsed_time(times[0])
  try:
    accuracy = float(accuracies[0])
  except ValueError:
    raise GpsSnapshotRejected("gps_accuracy_invalid") from None
  if not math.isfinite(accuracy) or not 0 < accuracy <= max_accuracy_m:
    raise GpsSnapshotRejected("gps_accuracy_unusable")
  if re.search(r"\bmock\b|\bisMock\s*[=:]\s*true|\bisFromMockProvider\s*[=:]\s*true", fields, re.IGNORECASE):
    raise GpsSnapshotRejected("mock_gps_rejected")
  providers = [index for index, line in enumerate(lines) if line.strip() == "gps provider:"]
  if len(providers) != 1:
    raise GpsSnapshotRejected("gps_provider_state_missing_or_ambiguous")
  provider_start = providers[0] + 1
  provider_end = next((index for index in range(provider_start, len(lines))
                       if re.fullmatch(r"\s*\w+ provider:", lines[index])), len(lines))
  provider_text = "\n".join(lines[provider_start:provider_end])
  if re.findall(r"\buseable=(true|false)\b", provider_text) != ["true"]:
    raise GpsSnapshotRejected("gps_provider_unusable")
  if re.findall(r"\bmStarted=(true|false)\b", provider_text) != ["true"]:
    raise GpsSnapshotRejected("gps_provider_not_started")
  if timestamp > phone_after + resolution_after:
    raise GpsSnapshotRejected("gps_source_time_in_future")
  age_lower = max(0, phone_before - timestamp - PRINTED_TIME_RESOLUTION_NS)
  age_upper = phone_after + resolution_after - timestamp + host_finish - host_start
  if age_upper >= max_age_ns:
    raise GpsSnapshotRejected("gps_source_stale")
  coordinates = None
  clear_coordinates = re.fullmatch(r"([+-]?\d+(?:\.\d+)?),([+-]?\d+(?:\.\d+)?)", coordinate_text)
  if clear_coordinates:
    latitude, longitude = map(float, clear_coordinates.groups())
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
      raise GpsSnapshotRejected("gps_coordinates_invalid")
    if include_coordinates:
      coordinates = (longitude, latitude)
  elif re.fullmatch(r"-?\d{1,3}\*{6},-?\d{1,3}\*{6}", coordinate_text) is None:
    raise GpsSnapshotRejected("gps_coordinate_format_unknown")
  return GpsSnapshot(boot, timestamp, accuracy, age_lower, age_upper, host_start, host_finish,
                     max_age_ns, clear_coordinates is not None, coordinates)
