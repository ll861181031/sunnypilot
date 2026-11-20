"""GCJ-02 / BD-09 conversions, shared by the navigation stack and the console.

The car, `navd` and OSRM all speak WGS-84. AMap (tiles and web services) speaks GCJ-02,
Baidu links speak BD-09. This is the public transform; it is accurate to about a metre
inside China and a no-op outside it. Functions take/return ``(longitude, latitude)``.
"""
import math

_A = 6378245.0
_EE = 0.00669342162296594323
_X_PI = math.pi * 3000.0 / 180.0


def out_of_china(lon: float, lat: float) -> bool:
  return not (73.66 < lon < 135.05 and 3.86 < lat < 53.55)


def _transform_lat(lon: float, lat: float) -> float:
  ret = -100.0 + 2.0 * lon + 3.0 * lat + 0.2 * lat * lat + 0.1 * lon * lat + 0.2 * math.sqrt(abs(lon))
  ret += (20.0 * math.sin(6.0 * lon * math.pi) + 20.0 * math.sin(2.0 * lon * math.pi)) * 2.0 / 3.0
  ret += (20.0 * math.sin(lat * math.pi) + 40.0 * math.sin(lat / 3.0 * math.pi)) * 2.0 / 3.0
  ret += (160.0 * math.sin(lat / 12.0 * math.pi) + 320 * math.sin(lat * math.pi / 30.0)) * 2.0 / 3.0
  return ret


def _transform_lon(lon: float, lat: float) -> float:
  ret = 300.0 + lon + 2.0 * lat + 0.1 * lon * lon + 0.1 * lon * lat + 0.1 * math.sqrt(abs(lon))
  ret += (20.0 * math.sin(6.0 * lon * math.pi) + 20.0 * math.sin(2.0 * lon * math.pi)) * 2.0 / 3.0
  ret += (20.0 * math.sin(lon * math.pi) + 40.0 * math.sin(lon / 3.0 * math.pi)) * 2.0 / 3.0
  ret += (150.0 * math.sin(lon / 12.0 * math.pi) + 300.0 * math.sin(lon / 30.0 * math.pi)) * 2.0 / 3.0
  return ret


def wgs84_to_gcj02(lon: float, lat: float) -> tuple[float, float]:
  if out_of_china(lon, lat):
    return lon, lat
  dlat = _transform_lat(lon - 105.0, lat - 35.0)
  dlon = _transform_lon(lon - 105.0, lat - 35.0)
  radlat = lat / 180.0 * math.pi
  magic = 1 - _EE * math.sin(radlat) ** 2
  sqrtmagic = math.sqrt(magic)
  dlat = (dlat * 180.0) / ((_A * (1 - _EE)) / (magic * sqrtmagic) * math.pi)
  dlon = (dlon * 180.0) / (_A / sqrtmagic * math.cos(radlat) * math.pi)
  return lon + dlon, lat + dlat


def gcj02_to_wgs84(lon: float, lat: float) -> tuple[float, float]:
  """Inverse of the public GCJ algorithm: iterate the forward transform until it lands."""
  if out_of_china(lon, lat):
    return lon, lat
  wlon, wlat = lon, lat
  for _ in range(6):
    glon, glat = wgs84_to_gcj02(wlon, wlat)
    wlon += lon - glon
    wlat += lat - glat
  return wlon, wlat


def gcj02_to_bd09(lon: float, lat: float) -> tuple[float, float]:
  z = math.sqrt(lon * lon + lat * lat) + 0.00002 * math.sin(lat * _X_PI)
  theta = math.atan2(lat, lon) + 0.000003 * math.cos(lon * _X_PI)
  return z * math.cos(theta) + 0.0065, z * math.sin(theta) + 0.006


def bd09_to_gcj02(lon: float, lat: float) -> tuple[float, float]:
  x, y = lon - 0.0065, lat - 0.006
  z = math.sqrt(x * x + y * y) - 0.00002 * math.sin(y * _X_PI)
  theta = math.atan2(y, x) - 0.000003 * math.cos(x * _X_PI)
  return z * math.cos(theta), z * math.sin(theta)


def bd09_to_wgs84(lon: float, lat: float) -> tuple[float, float]:
  return gcj02_to_wgs84(*bd09_to_gcj02(lon, lat))


def to_wgs84(latitude: float, longitude: float, coordinate: str = "wgs84") -> tuple[float, float]:
  """(lat, lon) in WGS-84 for a point tagged with its own system. Unknown systems raise."""
  coordinate = (coordinate or "wgs84").lower()
  if coordinate == "wgs84":
    return float(latitude), float(longitude)
  if coordinate == "gcj02":
    lon, lat = gcj02_to_wgs84(float(longitude), float(latitude))
    return lat, lon
  if coordinate == "bd09":
    lon, lat = bd09_to_wgs84(float(longitude), float(latitude))
    return lat, lon
  raise ValueError(f"unsupported coordinate system: {coordinate}")


def to_gcj02(latitude: float, longitude: float) -> tuple[float, float]:
  """(lat, lon) in GCJ-02, ready for AMap tiles or web services."""
  lon, lat = wgs84_to_gcj02(float(longitude), float(latitude))
  return lat, lon
