"""One place that turns origin/destination/waypoints into a `Route`.

Order: AMap Driving (best Chinese road data, includes traffic, needs the web key) and then
OSRM (no key, global demo server). Both produce the same `Route` contract navd already
uses: geometry in WGS-84 and maneuvers whose `distance` is measured from the route start.
AMap answers in GCJ-02, so geometry is converted here.
"""
import os

import numpy as np
import requests

from openpilot.common.gcj import gcj02_to_wgs84, wgs84_to_gcj02
from openpilot.common.swaglog import cloudlog

AMAP_URL = "https://restapi.amap.com/v3/direction/driving"
AMAP_KEY_PARAM = "NavAmapKey"
AMAP_KEY_ENV = "SUNNYPILOT_NAV_AMAP_KEY"
OSRM_URL = os.getenv("SUNNYPILOT_NAV_OSRM", "https://router.project-osrm.org")
MAPBOX_URL = "https://api.mapbox.com/directions/v5/mapbox/driving-traffic"
AMAP_WAYPOINTS_MAX = 16


class Route:
  """Geometry plus maneuvers, both in the units the rest of the pipeline expects."""

  def __init__(self, coordinates, maneuvers: list[dict], distance_m: float | None = None,
               duration_s: float | None = None, source: str = ""):
    self.coordinates = np.asarray(coordinates, dtype=np.float64).reshape((-1, 2))
    self.maneuvers = maneuvers
    self.distance_m = distance_m
    self.duration_s = duration_s
    self.source = source

  @classmethod
  def from_file(cls, path) -> "Route | None":
    import json
    try:
      data = json.loads(path.read_text())
      coordinates = data["coordinates"]
      maneuvers = data.get("maneuvers", [])
    except (OSError, KeyError, ValueError, TypeError):
      return None
    if len(coordinates) < 2:
      return None
    return cls(coordinates, maneuvers, source="file")

  @classmethod
  def from_osrm(cls, payload: dict) -> "Route | None":
    """OSRM route -> geometry plus maneuvers with distance from the route start."""
    try:
      route = payload["routes"][0]
      coordinates = route["geometry"]["coordinates"]
      legs = route.get("legs") or []
    except (KeyError, IndexError, TypeError):
      return None
    maneuvers, along = [], 0.0
    for leg in legs:
      for step in leg.get("steps", []):
        maneuver = step.get("maneuver", {})
        name = step.get("name") or ""
        kind = maneuver.get("type", "")
        modifier = maneuver.get("modifier", "")
        text = " ".join(part for part in (kind.replace("_", " "), modifier, f"onto {name}" if name else "") if part)
        maneuvers.append({"distance": along, "type": kind, "modifier": modifier, "text": text})
        along += float(step.get("distance", 0.0))
    return cls(coordinates, maneuvers, round(float(route.get("distance", 0.0)), 1),
               round(float(route.get("duration", 0.0)), 1), "osrm")

  @classmethod
  def from_mapbox(cls, payload: dict) -> "Route | None":
    try:
      route = payload["routes"][0]
      coordinates = route["geometry"]["coordinates"]
      legs = route["legs"]
    except (KeyError, IndexError, TypeError):
      return None
    maneuvers, along = [], 0.0
    for leg in legs:
      for step in leg.get("steps", []):
        maneuver = step.get("maneuver", {})
        maneuvers.append({
          "distance": along,
          "type": maneuver.get("type", ""),
          "modifier": maneuver.get("modifier", ""),
          "text": maneuver.get("instruction", ""),
        })
        along += float(step.get("distance", 0.0))
    return cls(coordinates, maneuvers, round(float(route.get("distance", 0.0)), 1),
               round(float(route.get("duration", 0.0)), 1), "mapbox")

  @classmethod
  def from_amap(cls, payload: dict) -> "Route | None":
    """AMap driving path -> geometry in WGS-84 with Valhalla-ish maneuver names."""
    try:
      path = payload["route"]["paths"][0]
      steps = path["steps"]
    except (KeyError, IndexError, TypeError):
      return None
    coordinates: list[tuple[float, float]] = []
    maneuvers, along = [], 0.0
    for step in steps:
      along += float(step.get("distance", 0.0))
      for pair in (step.get("polyline") or "").split(";"):
        try:
          lon, lat = (float(v) for v in pair.split(",")[:2])
        except (TypeError, ValueError):
          continue
        wlon, wlat = gcj02_to_wgs84(lon, lat)
        if not coordinates or abs(coordinates[-1][0] - wlon) > 1e-9 or abs(coordinates[-1][1] - wlat) > 1e-9:
          coordinates.append((wlon, wlat))
      maneuver_type, modifier = amap_maneuver(step.get("action") or "", step.get("instruction") or "")
      maneuvers.append({"distance": along, "type": maneuver_type, "modifier": modifier,
                        "text": step.get("instruction") or step.get("action") or ""})
    if len(coordinates) < 2:
      return None
    return cls(coordinates, maneuvers, round(float(path.get("distance", 0.0)), 1),
               round(float(path.get("duration", 0.0)), 1), "amap")


# ones the model understands; anything else degrades to "continue straight", which is what a
# missing maneuver looks like to navigation_input() anyway.
_AMAP_RULES = (
  ("到达", ("arrive", "")),
  ("驶出环岛", ("exit roundabout", "")), ("离开环岛", ("exit roundabout", "")),
  ("环岛", ("roundabout", "")),
  ("掉头", ("turn", "uturn")),
  ("上高速", ("on ramp", "")), ("驶入高速", ("on ramp", "")), ("进入高速", ("on ramp", "")),
  ("下高速", ("off ramp", "")), ("驶离高速", ("off ramp", "")),
  ("向左前方", ("fork", "slight left")), ("向右前方", ("fork", "slight right")),
  ("靠左", ("fork", "slight left")), ("靠右", ("fork", "slight right")),
  ("左转", ("turn", "left")), ("右转", ("turn", "right")),
  ("直行", ("turn", "straight")), ("沿", ("turn", "straight")),
)


def amap_maneuver(action: str, instruction: str) -> tuple[str, str]:
  text = f"{action} {instruction}"
  for needle, result in _AMAP_RULES:
    if needle in text:
      return result
  return "turn", "straight"


def amap_key(handle=None) -> str | None:
  key = os.environ.get(AMAP_KEY_ENV, "").strip()
  if key:
    return key
  try:
    from openpilot.common.params import Params
    params = handle if handle is not None else Params()
    value = params.get(AMAP_KEY_PARAM)
    if isinstance(value, bytes):
      value = value.decode()
    return (value or "").strip() or None
  except Exception:
    return None


def fetch_mapbox_route(token: str, position: tuple[float, float], destination: tuple[float, float],
                       language: str | None = None) -> Route | None:
  """Mapbox Directions, kept for accounts that still have a token."""
  params = {"access_token": token, "geometries": "geojson", "overview": "full",
            "steps": "true", "banner_instructions": "true", "alternatives": "false"}
  if language:
    params["language"] = language
  url = f"{MAPBOX_URL}/{position[0]:.6f},{position[1]:.6f};{destination[0]:.6f},{destination[1]:.6f}"
  try:
    response = requests.get(url, params=params, timeout=10)
    response.raise_for_status()
    return Route.from_mapbox(response.json())
  except (requests.RequestException, ValueError) as e:
    cloudlog.warning(f"routing.mapbox_failed: {type(e).__name__}")
    return None


def fetch_osrm_route(position: tuple[float, float], destination: tuple[float, float],
                     base_url: str | None = None, waypoints=()) -> Route | None:
  """Route from the public OSRM server (no token). `waypoints` are (lon, lat) pairs."""
  points = [position, *waypoints, destination]
  coords = ";".join(f"{lon:.6f},{lat:.6f}" for lon, lat in points)
  url = f"{(base_url or OSRM_URL).rstrip('/')}/route/v1/driving/{coords}"
  try:
    response = requests.get(url, params={"overview": "full", "steps": "true", "geometries": "geojson"}, timeout=10)
    response.raise_for_status()
    return Route.from_osrm(response.json())
  except (requests.RequestException, ValueError) as e:
    cloudlog.warning(f"routing.osrm_failed: {type(e).__name__}")
    return None


def fetch_amap_route(position: tuple[float, float], destination: tuple[float, float],
                     waypoints=(), key: str | None = None) -> Route | None:
  """Route from AMap Driving. Positions are (lon, lat) in WGS-84; AMap wants GCJ-02."""
  key = key or amap_key()
  if not key:
    return None
  origin_lon, origin_lat = wgs84_to_gcj02(*position)
  dest_lon, dest_lat = wgs84_to_gcj02(*destination)
  params = {"key": key, "origin": f"{origin_lon:.6f},{origin_lat:.6f}",
            "destination": f"{dest_lon:.6f},{dest_lat:.6f}",
            "extensions": "base", "strategy": 0}
  if waypoints:
    converted = [wgs84_to_gcj02(lon, lat) for lon, lat in waypoints[:AMAP_WAYPOINTS_MAX]]
    params["waypoints"] = "|".join(f"{lon:.6f},{lat:.6f}" for lon, lat in converted)
  try:
    response = requests.get(AMAP_URL, params=params, timeout=10)
    response.raise_for_status()
    payload = response.json()
    if str(payload.get("status")) != "1":
      cloudlog.warning(f"routing.amap_refused: {payload.get('info')} {payload.get('infocode')}")
      return None
    return Route.from_amap(payload)
  except (requests.RequestException, ValueError) as e:
    cloudlog.warning(f"routing.amap_failed: {type(e).__name__}")
    return None


def plan_route(position: tuple[float, float], destination: tuple[float, float], waypoints=(),
               handle=None) -> tuple[Route | None, str]:
  """AMap first (better Chinese data), OSRM as the always-available fallback."""
  key = amap_key(handle)
  if key:
    route = fetch_amap_route(position, destination, waypoints, key=key)
    if route is not None:
      return route, "amap"
  route = fetch_osrm_route(position, destination, waypoints=waypoints)
  return route, "osrm" if route is not None else "none"
