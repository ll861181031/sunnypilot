"""GPS-only ESP32-C3/M9N bridge for PC sunnypilot.

Use this when the vehicle IMU is disabled but the ESP32/M9N USB GNSS should
still feed locationd_llk. It opens the same USB CDC port as esp32d, so the two
processes are mutually exclusive; process_config starts this one only while the
ESP32 IMU process is disabled.
"""
from __future__ import annotations

import json
import os
import signal
import threading
import time
from pathlib import Path

import serial

from openpilot.cereal import messaging
from openpilot.common.swaglog import cloudlog
from openpilot.system.sensord.esp32d import ESP32Serial, SensorError
from openpilot.system.sensord.esp32gps import build_gps_message

NO_DATA_TIMEOUT_NS = 1_000_000_000


def serial_port() -> str:
  port = os.getenv("SUNNYPILOT_ESP32_GPS_PORT") or os.getenv("SUNNYPILOT_ESP32_PORT") or "/dev/ttyACM0"
  config_path = Path(os.getenv("SUNNYPILOT_ESP32_CONFIG", "~/.config/sunnypilot/esp32.json")).expanduser()
  if config_path.exists():
    try:
      configured = json.loads(config_path.read_text()).get("serial_port")
      if isinstance(configured, str) and configured:
        port = configured
    except (OSError, ValueError):
      pass
  return port


def run_session(publisher, stop: threading.Event, port: str | None = None,
                sensor_factory=ESP32Serial) -> None:
  sensor = sensor_factory(port or serial_port())
  last_gnss_t = None
  last_rx_ns = time.monotonic_ns()
  try:
    sensor.open()
    cloudlog.info("ESP32-C3 GNSS bridge connected: port=%s", sensor.port)
    while not stop.is_set():
      sample = sensor.sample()
      now_ns = time.monotonic_ns()
      gnss = sensor.gnss
      gnss_t = gnss.get("t") if isinstance(gnss, dict) else None
      if gnss_t is not None and gnss_t != last_gnss_t:
        message = build_gps_message(gnss)
        if message is not None:
          publisher.send("gpsLocation", message)
        last_gnss_t = gnss_t
        last_rx_ns = now_ns
      elif sample is not None:
        last_rx_ns = now_ns

      if now_ns - last_rx_ns > NO_DATA_TIMEOUT_NS:
        raise SensorError("No fresh ESP32 GPS data for 1 s")
      stop.wait(0.002)
  finally:
    sensor.close()


def main():
  stop = threading.Event()
  for sig in (signal.SIGINT, signal.SIGTERM):
    signal.signal(sig, lambda *_: stop.set())
  # The ESP32 publishes gpsLocation (1 Hz); make sure locationd_llk selects it
  # instead of the absent /dev/ttyHS0 u-blox source.
  if not os.path.exists("/dev/ttyHS0"):
    from openpilot.common.params import Params
    Params().put_bool("UbloxAvailable", False, block=True)
  publisher = messaging.PubMaster(["gpsLocation"])
  while not stop.is_set():
    try:
      run_session(publisher, stop)
    except (SensorError, serial.SerialException, OSError) as error:
      cloudlog.error("ESP32 GNSS unavailable: %s", error)
      stop.wait(2.0)


if __name__ == "__main__":
  main()
