import unittest

from openpilot.cereal import log
from openpilot.system.sensord.esp32gps import build_gps_message


class TestESP32Gps(unittest.TestCase):
  def _gnss(self, **overrides):
    data = {
      "t": 12345, "fix": 2, "sats": 12, "lat": 31.0303388, "lon": 106.4157203,
      "alt": 304.8, "speed_mps": 1.5, "course": 90.0, "hdop": 0.55,
      "stale": False,
    }
    data.update(overrides)
    return data

  def test_valid_fix_is_converted(self):
    message = build_gps_message(self._gnss())
    self.assertIsNotNone(message)
    self.assertTrue(message.valid)
    gps = message.gpsLocation
    self.assertTrue(gps.hasFix)
    self.assertEqual(gps.source, log.GpsLocationData.SensorSource.external)
    self.assertAlmostEqual(gps.latitude, 31.0303388)
    self.assertAlmostEqual(gps.longitude, 106.4157203)
    self.assertAlmostEqual(gps.altitude, 304.8)
    self.assertAlmostEqual(gps.speed, 1.5)
    self.assertAlmostEqual(gps.bearingDeg, 90.0)
    self.assertAlmostEqual(gps.vNED[0], 0.0, places=5)
    self.assertAlmostEqual(gps.vNED[1], 1.5, places=5)
    self.assertEqual(gps.satelliteCount, 12)
    self.assertGreater(gps.horizontalAccuracy, 0)
    self.assertGreater(gps.verticalAccuracy, 0)
    self.assertGreater(gps.speedAccuracy, 0)
    self.assertGreater(gps.bearingAccuracyDeg, 0)

  def test_no_fix_is_still_alive_with_has_fix_false(self):
    message = build_gps_message(self._gnss(fix=0))
    self.assertIsNotNone(message)
    self.assertTrue(message.valid)
    self.assertFalse(message.gpsLocation.hasFix)

  def test_stale_fix_is_not_used(self):
    message = build_gps_message(self._gnss(stale=True))
    self.assertIsNotNone(message)
    self.assertFalse(message.gpsLocation.hasFix)

  def test_invalid_coordinates_are_rejected(self):
    self.assertIsNone(build_gps_message(self._gnss(lat=91)))
    self.assertIsNone(build_gps_message(self._gnss(lon=181)))
    self.assertIsNone(build_gps_message(self._gnss(lat=float("nan"))))
    self.assertIsNone(build_gps_message(None))

  def test_bearing_and_speed_are_ned(self):
    message = build_gps_message(self._gnss(speed_mps=2.0, course=180.0))
    v = message.gpsLocation.vNED
    self.assertAlmostEqual(v[0], -2.0, places=5)
    self.assertAlmostEqual(v[1], 0.0, places=5)
    self.assertEqual(v[2], 0.0)


if __name__ == "__main__":
  unittest.main()
