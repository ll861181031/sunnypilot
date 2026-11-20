"""Convert ESP32-C3/M9N JSON GNSS messages into cereal gpsLocation.

The M9N is a meter-level receiver and emits NMEA GGA/RMC at 1 Hz.  locationd_llk
selects gpsLocation (the QCOM path) when UbloxAvailable is false, so publishing
gpsLocation works without the 10 Hz cadence required by gpsLocationExternal.
"""
from __future__ import annotations

import math
import time

from openpilot.cereal import log
from openpilot.cereal import messaging


def _finite(value, default=0.0):
  try:
    result = float(value)
  except (TypeError, ValueError):
    return default
  return result if math.isfinite(result) else default


def build_gps_message(gnss: dict | None):
  """Return a gpsLocation message, or None if the GNSS record is unusable.

  The event itself stays valid even without a fix: locationd uses allAliveAndValid()
  before it initializes its filter, and hasFix=False is the correct signal for
  "receiver is alive but there is no position yet".
  """
  if not isinstance(gnss, dict):
    return None

  lat = _finite(gnss.get("lat"), float("nan"))
  lon = _finite(gnss.get("lon"), float("nan"))
  alt = _finite(gnss.get("alt"), 0.0)
  speed = max(0.0, _finite(gnss.get("speed_mps"), 0.0))
  course = _finite(gnss.get("course"), 0.0) % 360.0
  hdop = _finite(gnss.get("hdop"), 99.0)
  fix = int(_finite(gnss.get("fix"), 0))
  sats = int(_finite(gnss.get("sats"), 0))
  stale = bool(gnss.get("stale", True))

  if not (math.isfinite(lat) and math.isfinite(lon) and -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
    return None

  has_fix = bool(fix > 0 and not stale)
  # 1-sigma-ish values.  QCOM locationd ignores horizontalAccuracy but its
  # accuracy positivity/sanity checks still apply to all three fields.
  hdop = max(0.5, min(hdop, 50.0))
  horizontal_accuracy = max(1.5, hdop * 2.5)
  vertical_accuracy = max(1.5, hdop * 2.0)
  speed_accuracy = max(0.3, speed * 0.05)
  bearing_accuracy = 15.0

  msg = messaging.new_message("gpsLocation", valid=True)
  gps = msg.gpsLocation
  gps.flags = 0
  gps.latitude = float(lat)
  gps.longitude = float(lon)
  gps.altitude = float(alt)
  gps.speed = float(speed)
  gps.bearingDeg = float(course)
  gps.horizontalAccuracy = float(horizontal_accuracy)
  # unixTimestampMillis is defined as the fix time in UTC, so this field needs the
  # wall clock - time.monotonic would break the contract the consumers rely on.
  gps.unixTimestampMillis = int(time.time() * 1000)  # noqa: TID251
  gps.source = log.GpsLocationData.SensorSource.external
  course_rad = math.radians(course)
  gps.vNED = [float(speed * math.cos(course_rad)), float(speed * math.sin(course_rad)), 0.0]
  gps.verticalAccuracy = float(vertical_accuracy)
  gps.bearingAccuracyDeg = float(bearing_accuracy)
  gps.speedAccuracy = float(speed_accuracy)
  gps.hasFix = has_fix
  gps.satelliteCount = max(-128, min(127, sats))
  return msg
