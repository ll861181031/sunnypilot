"""ESP32-C3 USB serial backend for PC sensord.

The ESP32-C3 firmware emits newline-delimited JSON at ~100 Hz.  This backend
keeps the same safety contract as the device backend: the calibration file and
mounting confirmation must match, and every accel/gyro message carries
valid=False until the mounting is confirmed.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import signal
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import serial

from openpilot.cereal import messaging
from openpilot.common.swaglog import cloudlog
from openpilot.system.sensord.esp32gps import build_gps_message

G = 9.80665
CALIBRATION_FRAME = "accel_gyro_y_inverted"
# IMU Studio's formulas are stored in a Y-inverted frame.  Undo that reflection
# before applying device_from_sensor, so the mounting matrix is expressed in the
# physical sensor/chip axes.
CALIBRATION_FROM_SENSOR = np.diag([1., -1., 1.])
# Both SP location estimators convert SensorEventData v to [-v[2], -v[1], -v[0]].
DEVICE_FROM_EVENT = np.array([[0., 0., -1.], [0., -1., 0.], [-1., 0., 0.]])
MAX_SAMPLE_GAP_NS = 100_000_000
MAX_AGE_NS = 50_000_000
NO_SAMPLE_TIMEOUT_NS = 250_000_000
RECOVERY_SAMPLES = 52


class SensorError(RuntimeError):
  pass


class ConfigurationError(ValueError):
  pass


@dataclass(frozen=True)
class Sample:
  timestamp_ns: int
  accel_g: tuple[float, float, float]
  gyro_dps: tuple[float, float, float]
  temperature_c: float
  seq: int


@dataclass(frozen=True)
class Configuration:
  serial_port: str
  accel_bias: np.ndarray
  accel_scale: np.ndarray
  gyro_bias: np.ndarray
  device_from_sensor: np.ndarray
  mounting_confirmed: bool
  calibration_sha256: str
  calibration_identity: str

  @classmethod
  def load(cls, path: Path):
    try:
      config = json.loads(path.read_text())
      if config.get("schema_version") != 1:
        raise ConfigurationError("Unsupported ESP32 configuration version")
      port = config.get("serial_port", "/dev/ttyACM0")
      if not isinstance(port, str) or not port:
        raise ConfigurationError("serial_port must be a non-empty string")
      confirmed = config.get("mounting_confirmed", False)
      if not isinstance(confirmed, bool):
        raise ConfigurationError("mounting_confirmed must be boolean")

      accel_bias = np.zeros(3)
      accel_scale = np.ones(3)
      gyro_bias = np.zeros(3)
      digest = ""
      identity = ""
      calibration_path = config.get("calibration_path")
      if calibration_path:
        calibration_path = Path(calibration_path).expanduser()
        if not calibration_path.is_absolute():
          raise ConfigurationError("calibration_path must be absolute")
        raw = calibration_path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        expected_digest = config.get("calibration_sha256", "")
        if expected_digest and digest != expected_digest:
          raise ConfigurationError("Calibration file changed: update calibration_sha256")
        saved = json.loads(raw)
        device = saved.get("device", {})
        expected = {"model": "LSM6DS3", "who_am_i": "0x69", "coordinate_frame": CALIBRATION_FRAME,
                    "odr_hz": 104, "accel_range_g": 2, "gyro_range_dps": 250}
        if saved.get("schema_version") != 1 or any(device.get(k) != v for k, v in expected.items()):
          raise ConfigurationError("Calibration device, axes or ranges do not match this backend")
        if not str(device.get("bridge", "")).startswith("ESP32-C3"):
          raise ConfigurationError("Calibration was not produced by the ESP32-C3 backend")
        identity = str(device.get("identity", ""))
        expected_identity = config.get("calibration_identity", "")
        if expected_identity and identity != expected_identity:
          raise ConfigurationError("Calibration identity mismatch")
        cal = saved["calibration"]
        accel = cal.get("accel")
        gyro = cal.get("gyro")
        if not accel or not gyro:
          raise ConfigurationError("Incomplete calibration (need six-face accel and gyro bias)")
        if set(accel.get("faces", {})) != {"+X", "-X", "+Y", "-Y", "+Z", "-Z"}:
          raise ConfigurationError("Incomplete six-face calibration")
        accel_bias = np.asarray(accel["bias_g"], dtype=float)
        accel_scale = np.asarray(accel["scale"], dtype=float)
        gyro_bias = np.asarray(gyro["bias_dps"], dtype=float)
        if accel_bias.shape != (3,) or accel_scale.shape != (3,) or gyro_bias.shape != (3,):
          raise ConfigurationError("Invalid calibration vector sizes")
        if not np.isfinite(accel_bias).all() or not np.isfinite(accel_scale).all() or not np.isfinite(gyro_bias).all():
          raise ConfigurationError("Calibration contains non-finite values")
        if np.any(np.abs(accel_bias) > 0.2) or np.any(accel_scale < 0.8) or np.any(accel_scale > 1.3):
          raise ConfigurationError("Accelerometer calibration out of range")
        if np.any(np.abs(gyro_bias) > 10.):
          raise ConfigurationError("Gyroscope bias out of range")
      elif confirmed:
        raise ConfigurationError("Cannot confirm mounting without a calibration file")

      rotation = config.get("device_from_sensor")
      if rotation is None:
        if confirmed:
          raise ConfigurationError("device_from_sensor is required when mounting is confirmed")
        rotation = np.eye(3)  # diagnostic frame only; messages remain invalid
      rotation = np.asarray(rotation, dtype=float)
      if (rotation.shape != (3, 3) or not np.isfinite(rotation).all() or
          not np.allclose(rotation @ rotation.T, np.eye(3), atol=1e-6, rtol=0) or
          not math.isclose(float(np.linalg.det(rotation)), 1., abs_tol=1e-6)):
        raise ConfigurationError("device_from_sensor must be a proper 3x3 rotation (determinant +1)")

      return cls(port, accel_bias, accel_scale, gyro_bias, rotation, confirmed, digest, identity)
    except (KeyError, TypeError, ValueError, OSError) as error:
      if isinstance(error, ConfigurationError):
        raise
      raise ConfigurationError(f"Cannot load ESP32 configuration: {error}") from error

  def corrected_event(self, sample: Sample) -> tuple[np.ndarray, np.ndarray]:
    # Calibration bias/scale live in the Y-inverted calibration frame.  Undo the
    # Y reflection first; device_from_sensor then maps physical chip axes to the
    # vehicle device frame.
    accel = CALIBRATION_FROM_SENSOR @ ((np.asarray(sample.accel_g) - self.accel_bias) * self.accel_scale)
    gyro = CALIBRATION_FROM_SENSOR @ (np.asarray(sample.gyro_dps) - self.gyro_bias)
    event_from_sensor = DEVICE_FROM_EVENT.T @ self.device_from_sensor
    return (event_from_sensor @ accel * G,
            event_from_sensor @ gyro * (math.pi / 180.))


class ESP32Serial:
  def __init__(self, port: str):
    self.port = port
    self.link = None
    self.gnss = {}
    self._buffer = ""

  def open(self):
    try:
      self.link = serial.Serial(self.port, 115200, timeout=0.05)
      self.link.dtr = False
      self.link.rts = False
    except Exception as error:
      raise SensorError(f"Cannot open ESP32 serial {self.port}: {error}") from error
    # Opening CDC can reset the board, and the firmware auto-scans the GNSS baud
    # before it emits hello. Wait for the boot banner instead of treating the
    # first 250 ms of silence as a sensor failure.
    deadline = time.monotonic() + 20.0
    hello = False
    while time.monotonic() < deadline:
      message = self._read_message()
      if message is None:
        continue
      if message.get("type") == "hello":
        hello = True
        break
      if message.get("type") == "gnss":
        self.gnss = message
    if not hello:
      self.close()
      raise SensorError("ESP32 firmware did not send hello after opening the serial port")
    return self

  def _read_message(self, max_lines: int = 8):
    """Next parsed JSON object, or None when the port has nothing to give.

    Noise is skipped rather than reported, because None means "no data" to every
    caller and returning it for a garbled line ends the whole sampling cycle.
    Anything that is not a JSON object counts as noise: json.loads returns
    int/float/list/str for a line like "1234", which is exactly what a partially
    flushed serial line produces, and every caller then does message.get("type") -
    crashing the daemon with "AttributeError: 'int' object has no attribute 'get'".

    The bound matters: a genuinely silent port returns b"" and exits on the first
    iteration, so this never spins.
    """
    if self.link is None:
      return None
    for _ in range(max_lines):
      raw = self.link.readline()
      if not raw:
        return None
      try:
        message = json.loads(raw.decode("utf-8", "replace").strip())
      except (ValueError, UnicodeError):
        continue
      if isinstance(message, dict):
        return message
    return None

  def sample(self):
    if self.link is None:
      return None
    for _ in range(8):
      message = self._read_message()
      if message is None:
        return None
      kind = message.get("type")
      if kind == "gnss":
        self.gnss = message
        continue
      if kind in ("hello", "error", "gnss_status", "gnss_config"):
        continue
      if kind != "imu":
        continue
      values = [message.get(k, 0.0) for k in ("ax", "ay", "az", "gx", "gy", "gz", "temp")]
      if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in values):
        continue
      return Sample(time.monotonic_ns(), tuple(float(v) for v in values[:3]),
                    tuple(float(v) for v in values[3:6]), float(values[6]), int(message.get("seq", 0)))
    return None

  def close(self):
    if self.link is not None:
      try:
        self.link.close()
      except Exception:
        pass
      self.link = None


class SampleHealth:
  def __init__(self):
    self.last_timestamp_ns = 0
    self.last_seq = -1
    self.healthy_count = 0

  def check(self, sample: Sample, now_ns: int) -> bool:
    gap = sample.timestamp_ns - self.last_timestamp_ns
    self.last_timestamp_ns = max(self.last_timestamp_ns, sample.timestamp_ns)
    finite = all(math.isfinite(v) for v in (*sample.accel_g, *sample.gyro_dps, sample.temperature_c))
    seq_ok = sample.seq != self.last_seq
    self.last_seq = sample.seq
    healthy = (finite and seq_ok and 0 < gap <= MAX_SAMPLE_GAP_NS and
               0 <= now_ns - sample.timestamp_ns <= MAX_AGE_NS and
               -40 <= sample.temperature_c <= 85)
    self.healthy_count = self.healthy_count + 1 if healthy else 0
    return self.healthy_count >= RECOVERY_SAMPLES


def messages(config: Configuration, sample: Sample, healthy: bool, include_temperature: bool = False):
  accel, gyro = config.corrected_event(sample)
  finite = bool(np.isfinite(accel).all() and np.isfinite(gyro).all() and math.isfinite(sample.temperature_c))
  valid = bool(config.mounting_confirmed and healthy and finite)
  for service, field, values in (("accelerometer", "acceleration", accel), ("gyroscope", "gyroUncalibrated", gyro)):
    message = messaging.new_message(service, valid=valid)
    event = getattr(message, service)
    event.timestamp = sample.timestamp_ns
    event.source = "lsm6ds3"
    event.init(field).v = values.tolist()
    yield service, message
  if include_temperature:
    message = messaging.new_message("temperatureSensor", valid=bool(healthy and finite))
    message.temperatureSensor.timestamp = sample.timestamp_ns
    message.temperatureSensor.source = "lsm6ds3"
    message.temperatureSensor.temperature = sample.temperature_c
    yield "temperatureSensor", message


def run_session(config: Configuration, publisher, stop: threading.Event, sensor_factory=ESP32Serial) -> None:
  sensor = sensor_factory(config.serial_port)
  try:
    sensor.open()
    cloudlog.info("ESP32-C3 LSM6DS3 connected: port=%s mounting_confirmed=%s calibration_sha256=%s",
                  config.serial_port, config.mounting_confirmed, config.calibration_sha256)
    health = SampleHealth()
    last_sample_ns = time.monotonic_ns()
    next_temperature_ns = 0
    last_gnss_t = None
    while not stop.is_set():
      sample = sensor.sample()
      now_ns = time.monotonic_ns()
      # GNSS arrives at 1 Hz alongside the 100 Hz IMU stream.  Publish each new
      # record once, independent of whether this iteration produced an IMU sample.
      gnss = sensor.gnss
      gnss_t = gnss.get("t") if isinstance(gnss, dict) else None
      if gnss_t is not None and gnss_t != last_gnss_t:
        gps_message = build_gps_message(gnss)
        if gps_message is not None:
          publisher.send("gpsLocation", gps_message)
        last_gnss_t = gnss_t
      if sample is None:
        if now_ns - last_sample_ns > NO_SAMPLE_TIMEOUT_NS:
          raise SensorError("No fresh ESP32 sample for 250 ms")
        stop.wait(0.002)
        continue
      last_sample_ns = now_ns
      healthy = health.check(sample, now_ns)
      include_temperature = now_ns >= next_temperature_ns
      for service, message in messages(config, sample, healthy, include_temperature):
        publisher.send(service, message)
      if include_temperature:
        next_temperature_ns = now_ns + 500_000_000
  finally:
    sensor.close()


def main():
  config_path = Path(os.getenv("SUNNYPILOT_ESP32_CONFIG", "~/.config/sunnypilot/esp32.json")).expanduser()
  stop = threading.Event()
  for sig in (signal.SIGINT, signal.SIGTERM):
    signal.signal(sig, lambda *_: stop.set())
  # The ESP32 publishes gpsLocation (1 Hz); make sure locationd_llk selects it
  # instead of the absent /dev/ttyHS0 u-blox source.
  if not os.path.exists("/dev/ttyHS0"):
    from openpilot.common.params import Params
    Params().put_bool("UbloxAvailable", False, block=True)
  publisher = messaging.PubMaster(["accelerometer", "gyroscope", "temperatureSensor", "gpsLocation"])
  while not stop.is_set():
    try:
      config = Configuration.load(config_path)
      if not config.mounting_confirmed:
        cloudlog.warning("ESP32 mounting is unconfirmed: diagnostic data only, accelerometer/gyroscope valid=False")
      run_session(config, publisher, stop)
    except (ConfigurationError, SensorError, serial.SerialException, OSError) as error:
      cloudlog.error("ESP32 unavailable; no valid IMU output: %s", error)
      stop.wait(5.0)


if __name__ == "__main__":
  main()
