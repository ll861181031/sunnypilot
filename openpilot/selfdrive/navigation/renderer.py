"""Protomaps basemap rasterization for the model's map input.

Adapted from commaai/openpilot's `openpilot/selfdrive/navigation/renderer.py`
(nav-model-integration branch, MIT). The road styling, draw order and the training
framing are kept; the pixel geometry comes from `map_draw` so there is exactly one copy
of the 1 m/px projection in this build.
"""
import math
from functools import lru_cache

import numpy as np

from openpilot.selfdrive.navigation.map_draw import (
  BACKGROUND,
  EGO,
  EARTH_CIRCUMFERENCE,
  MAX_ROAD_DISTANCE,
  ROUTE_INTENSITY,
  ROUTE_WIDTH,
  SIZE,
  draw_segments,
  draw_vehicle,
  mercator,
  project,
)
from openpilot.selfdrive.navigation.pmtiles import PMTiles
from openpilot.selfdrive.navigation.route import nearest_segment
from openpilot.selfdrive.navigation.vector_tile import Road, decode_roads

ZOOM = 15  # tile zoom; the style zoom is derived from the 1 m per pixel scale instead
ROAD_INTENSITY = 255
CUT_WIDTH = 3.0  # black separator under a bridge, added to the road width

# The training style's road classes as (zoom, width, zoom, width) stops for the interior and
# for one side of the casing, in pixels. Casings are white there too, so they only widen the
# stroke. Only each class's top interval is kept: the style zoom at 1 m per pixel stays above
# 14 until 78 degrees of latitude, where the lower stops begin.
MAJOR, MEDIUM, STREET, MINOR, TRACK = range(5)
STOPS = np.array([[[5, 0.75, 18, 32], [10, 1, 18, 2]],  # MAJOR: motorway, trunk, primary
                  [[5, 0.1, 18, 26], [10, 0.75, 18, 2]],  # MEDIUM: secondary, tertiary
                  [[14, 2, 18, 18], [12, 0.75, 20, 2]],  # STREET: residential, unclassified, living streets
                  [[14, 0.5, 18, 12], [12, 0.75, 20, 2]],  # MINOR: service roads and minor links
                  [[14, 1, 18, 12], [12, 0.75, 20, 2]]], dtype=np.float64)  # TRACK


def stroke_widths(zoom: float, styles: np.ndarray, levels: np.ndarray) -> np.ndarray:
  """Full stroke width of each segment: the style's exponential (base 1.5) interpolation
  between its stops, flat beyond them, with the casing on both sides except for tunnels,
  which are drawn at their interior width."""
  z0, w0, z1, w1 = np.moveaxis(STOPS, -1, 0)
  z = np.clip(zoom, z0, z1)
  table = w0 + (w1 - w0) * (1.5 ** (z - z0) - 1) / (1.5 ** (z1 - z0) - 1)  # (style, interior / casing)
  return table[styles, 0] + np.where(levels < 0, 0, 2 * table[styles, 1])


def road_style(road: Road) -> int | None:
  """The style for a road, or None when hidden. Protomaps kind_detail is the underlying OSM highway type."""
  detail = road.detail
  if detail in ('platform', 'pier', 'raceway'):
    return None
  if detail in ('motorway', 'trunk', 'primary') or (not detail and road.kind == 'highway'):
    return MAJOR
  if detail in ('secondary', 'tertiary') or (not detail and road.kind == 'major_road'):
    return MEDIUM
  if detail == 'track':
    return TRACK
  if detail in ('secondary_link', 'tertiary_link', 'service', 'driveway', 'parking_aisle', 'alley') or (
    road.kind == 'other' and detail != 'living_street'):
    return MINOR
  return STREET


def road_level(road: Road) -> int:
  """Draw order: tunnels, then ground roads, then bridges, each cutting over the levels beneath."""
  return 1 if road.bridge else -1 if road.tunnel else 0


class MapRenderer:
  def __init__(self, archive: PMTiles):
    self.archive = archive
    self.zoom = min(max(ZOOM, archive.min_zoom), archive.max_zoom)
    self.roads = lru_cache(maxsize=128)(self._load_roads)

  def _load_roads(self, x: int, y: int):
    """A tile's visible road segments as flat arrays: mercator starts/ends, style index,
    level and polyline end flags."""
    tile = self.archive.get(self.zoom, x, y)
    if tile is None:
      return None
    starts, ends = [np.empty((0, 2))], [np.empty((0, 2))]
    style_parts = [np.empty(0, int)]
    level_parts = [np.empty(0, int)]
    cap_parts = [np.empty((0, 2), bool)]
    for road in decode_roads(tile):
      style = road_style(road)
      if style is None:
        continue
      points = (road.points + np.array([x, y])) / (1 << self.zoom)
      n = len(points) - 1
      starts.append(points[:-1])
      ends.append(points[1:])
      style_parts.append(np.full(n, style))
      level_parts.append(np.full(n, road_level(road)))
      ends_of_line = np.zeros((n, 2), dtype=bool)
      ends_of_line[0, 0] = ends_of_line[-1, 1] = True
      cap_parts.append(ends_of_line)
    return (np.concatenate(starts), np.concatenate(ends), np.concatenate(style_parts),
            np.concatenate(level_parts), np.concatenate(cap_parts))

  def view_tiles(self, longitude: float, latitude: float) -> list[tuple[int, int]]:
    """Tiles covering the view at a point for every heading."""
    origin = mercator(np.array([[longitude, latitude]]))[0]
    # A bounding circle includes all headings, forward offset and road stroke widths.
    radius = (SIZE / math.sqrt(2) + 64.0 + 16) / (EARTH_CIRCUMFERENCE * math.cos(math.radians(latitude)))
    count = 1 << self.zoom
    low = np.floor((origin - radius) * count).astype(int)
    high = np.floor((origin + radius) * count).astype(int)
    return [(x % count, y) for y in range(low[1], high[1] + 1) for x in range(low[0], high[0] + 1) if 0 <= y < count]

  def route_tiles(self, route: np.ndarray) -> set[tuple[int, int]]:
    """Every tile the view can show while the pose is snapped onto the route, sampled every 100 m."""
    from openpilot.selfdrive.navigation.route import cumulative_distance
    distance = cumulative_distance(route)
    samples = np.column_stack([np.interp(np.arange(0, distance[-1], 100), distance, route[:, axis]) for axis in (0, 1)])
    return {tile for lon, lat in np.vstack((samples, route)).tolist() for tile in self.view_tiles(lon, lat)}

  def prefetch(self, route: np.ndarray):
    self.archive.prefetch((self.zoom, x, y) for x, y in self.route_tiles(route))

  def render(self, longitude: float, latitude: float, bearing: float, route: np.ndarray) -> tuple[np.ndarray, bool]:
    """The map image at a position and bearing, and whether every tile was present with a
    road near the ego."""
    image = np.full((SIZE, SIZE), BACKGROUND, dtype=np.uint8)
    loaded = [self.roads(x, y) for x, y in self.view_tiles(longitude, latitude)]
    tiles = [tile for tile in loaded if tile is not None]
    # The training renderer's latitude-dependent style zoom: the web mercator zoom with one
    # meter per pixel at this latitude, under the 512-pixel tile convention.
    style_zoom = math.log2(math.cos(math.radians(latitude)) * EARTH_CIRCUMFERENCE / 256) - 1
    road_distance = math.inf
    if tiles:
      starts, ends, styles, levels, caps = (np.concatenate(column) for column in zip(*tiles, strict=True))
      starts, ends = project(starts, longitude, latitude, bearing), project(ends, longitude, latitude, bearing)
      widths = stroke_widths(style_zoom, styles, levels)
      if len(starts):
        road_distance = nearest_segment(starts, ends, EGO)[2]
      for level in np.unique(levels):
        chosen = levels == level
        if level > levels.min():
          # Cut the roads beneath, open at way endpoints so a bridge does not end in a black oval.
          draw_segments(image, starts[chosen], ends[chosen], (widths[chosen] + CUT_WIDTH) / 2, BACKGROUND,
                        overlay=True, caps=~caps[chosen])
        draw_segments(image, starts[chosen], ends[chosen], widths[chosen] / 2, ROAD_INTENSITY)
    pixels = project(mercator(route), longitude, latitude, bearing)
    if len(pixels) >= 2:
      draw_segments(image, pixels[:-1], pixels[1:], ROUTE_WIDTH / 2, ROUTE_INTENSITY, overlay=True)
    draw_vehicle(image)
    return image, len(tiles) == len(loaded) and road_distance <= MAX_ROAD_DISTANCE

  def close(self):
    self.roads.cache_clear()
    self.archive.close()
