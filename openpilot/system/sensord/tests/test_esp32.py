import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path

from openpilot.system.sensord.esp32d import (
  Configuration, ConfigurationError, Sample, SampleHealth, messages, run_session,
)


class TestESP32Backend(unittest.TestCase):
  def _write(self, root: Path, *, bridge="ESP32-C3", identity="303a:1001//dev/ttyACM0/6a/69", confirmed=True,
             matrix=None):
    cal = {
      "schema_version": 1,
      "device": {"model": "LSM6DS3", "bridge": bridge, "who_am_i": "0x69",
                 "address": "0x6A", "identity": identity, "coordinate_frame": "accel_gyro_y_inverted",
                 "odr_hz": 104, "accel_range_g": 2, "gyro_range_dps": 250},
      "calibration": {
        "accel": {"bias_g": [0, 0, 0], "scale": [1, 1, 1],
                  "faces": dict.fromkeys(("+X", "-X", "+Y", "-Y", "+Z", "-Z"), 0.0)},
        "gyro": {"bias_dps": [0, 0, 0]},
      },
    }
    path = root / "calibration.json"
    path.write_text(json.dumps(cal))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    config = {
      "schema_version": 1, "serial_port": "/dev/ttyACM0",
      "calibration_path": str(path), "calibration_sha256": digest,
      "calibration_identity": identity if confirmed else "",
      "device_from_sensor": matrix or [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
      "mounting_confirmed": confirmed,
    }
    cfg_path = root / "esp32.json"
    cfg_path.write_text(json.dumps(config))
    return cfg_path

  def test_load_and_validity(self):
    with tempfile.TemporaryDirectory() as tmp:
      cfg = Configuration.load(self._write(Path(tmp)))
      self.assertTrue(cfg.mounting_confirmed)
      self.assertEqual(cfg.calibration_identity, "303a:1001//dev/ttyACM0/6a/69")
      sample = Sample(1_000_000_000, (0.0, 0.0, 1.0), (0.0, 0.0, 0.0), 30.0, 1)
      services = [name for name, _ in messages(cfg, sample, healthy=True)]
      self.assertEqual(services, ["accelerometer", "gyroscope"])

  def test_corrected_event_undoes_calibration_y_inversion(self):
    with tempfile.TemporaryDirectory() as tmp:
      cfg = Configuration.load(self._write(Path(tmp), matrix=[[1, 0, 0], [0, -1, 0], [0, 0, -1]]))
      # Calibration-frame +Y is physical -Y (chip Y points left, calibration
      # frame flips it).  With X-forward/Y-left/Z-up, the device event for +1 g
      # on calibration +Y is -1 g on event Y.
      sample = Sample(1_000_000_000, (0.0, 1.0, 0.0), (0.0, 2.0, 0.0), 30.0, 1)
      accel, gyro = cfg.corrected_event(sample)
      self.assertAlmostEqual(accel[0], 0.0)
      self.assertAlmostEqual(accel[1], -9.80665, places=4)
      self.assertAlmostEqual(accel[2], 0.0)
      self.assertAlmostEqual(gyro[1], -2.0 * 3.141592653589793 / 180.0, places=6)

  def test_unconfirmed_without_calibration_is_diagnostic_only(self):
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      path = root / "esp32.json"
      path.write_text(json.dumps({"schema_version": 1, "serial_port": "/dev/ttyACM0",
                                  "mounting_confirmed": False,
                                  "device_from_sensor": [[1, 0, 0], [0, 1, 0], [0, 0, 1]]}))
      cfg = Configuration.load(path)
      sample = Sample(1_000_000_000, (0.0, 0.0, 1.0), (0.0, 0.0, 0.0), 30.0, 1)
      accel_msg = next(msg for service, msg in messages(cfg, sample, healthy=True) if service == "accelerometer")
      self.assertFalse(accel_msg.valid)

  def test_wrong_bridge_rejected(self):
    with tempfile.TemporaryDirectory() as tmp:
      with self.assertRaises(ConfigurationError):
        Configuration.load(self._write(Path(tmp), bridge="CH347"))

  def test_health_recovery(self):
    health = SampleHealth()
    for i in range(52):
      self.assertFalse(health.check(Sample(i * 10_000_000, (0, 0, 1), (0, 0, 0), 30.0, i), i * 10_000_000))
    self.assertTrue(health.check(Sample(52 * 10_000_000, (0, 0, 1), (0, 0, 0), 30.0, 52), 52 * 10_000_000))

  def test_run_session_publishes_gps_location(self):
    sent = []
    class Publisher:
      def send(self, service, message):
        sent.append((service, message))
    stop = threading.Event()
    class FakeSensor:
      port = "/dev/null"
      gnss = {"t": 1, "fix": 2, "sats": 12, "lat": 31.0, "lon": 106.0, "alt": 300.0,
              "speed_mps": 1.0, "course": 90.0, "hdop": 0.55, "stale": False}
      def __init__(self):
        self.done = False
      def open(self): pass
      def close(self): pass
      def sample(self):
        if not self.done:
          self.done = True
          return Sample(1_000_000_000, (0, 0, 1), (0, 0, 0), 30.0, 1)
        stop.set()
        return None
    with tempfile.TemporaryDirectory() as tmp:
      cfg = Configuration.load(self._write(Path(tmp)))
      run_session(cfg, Publisher(), stop, sensor_factory=lambda port: FakeSensor())
    services = [service for service, _ in sent]
    self.assertIn("accelerometer", services)
    self.assertIn("gpsLocation", services)
    gps = next(message for service, message in sent if service == "gpsLocation")
    self.assertTrue(gps.valid)
    self.assertTrue(gps.gpsLocation.hasFix)
    self.assertAlmostEqual(gps.gpsLocation.latitude, 31.0)


if __name__ == "__main__":
  unittest.main()


class TestSerialNoiseIsNotAMessage(unittest.TestCase):
  """Regression: a bare number on the wire used to crash the daemon.

  json.loads returns int/float/list/str for a line like "1234", and every caller
  then does message.get("type"), which raises
  "AttributeError: 'int' object has no attribute 'get'".
  """

  def _driver(self, lines):
    from openpilot.system.sensord import esp32d

    class _Link:
      def __init__(self, payloads):
        self._lines = list(payloads)

      def readline(self):
        return self._lines.pop(0) if self._lines else b""

      def close(self):
        pass

    driver = esp32d.ESP32Serial.__new__(esp32d.ESP32Serial)
    driver.port = "/dev/null"
    driver.link = _Link(lines)
    driver.gnss = {}
    driver._buffer = ""
    return driver

  def test_non_object_json_is_dropped(self):
    driver = self._driver([b"1234\n", b"12.5\n", b"[1,2,3]\n", b'"hello"\n', b"null\n"])
    for _ in range(5):
      self.assertIsNone(driver._read_message())

  def test_a_real_object_still_passes(self):
    driver = self._driver([b'{"type":"imu","ax":1,"ay":2,"az":3,"gx":4,"gy":5,"gz":6,"temp":30,"seq":7}\n'])
    message = driver._read_message()
    self.assertEqual(message["type"], "imu")

  def test_sample_survives_a_number_on_the_wire(self):
    driver = self._driver([b"1234\n"] * 8)
    self.assertIsNone(driver.sample())

  def test_sample_still_returns_a_reading_after_noise(self):
    driver = self._driver([b"1234\n",
                           b'{"type":"imu","ax":1,"ay":2,"az":3,"gx":4,"gy":5,"gz":6,"temp":30,"seq":9}\n'])
    sample = driver.sample()
    self.assertIsNotNone(sample)
    self.assertEqual(sample.seq, 9)
