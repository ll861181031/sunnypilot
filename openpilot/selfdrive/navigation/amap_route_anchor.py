from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
import math

from tools.cplink_shim.amap_route_cache import COORDINATE_SCALE, MAX_SEGMENTS, CachedRoute


EARTH_RADIUS_M = 6_371_008.8
METERS_PER_DEGREE = 2 * math.pi * EARTH_RADIUS_M / 360
MAX_ROUTE_POINTS = 100_000
MAX_EDGE_M = 2_000.0
LONG_EDGE_RELATIVE_TOLERANCE = 0.02
HEADING_HALF_WINDOW_M = 50.0


class AmapRouteAnchorRejected(ValueError):
  pass


def _index(value, name: str, length: int) -> int:
  if type(value) is not int or not 0 <= value < length:
    raise AmapRouteAnchorRejected("invalid_" + name)
  return value


def _native_point(point) -> tuple[int, int]:
  if (not isinstance(point, (tuple, list)) or len(point) != 2 or any(type(value) is not int for value in point)
      or not -180 * COORDINATE_SCALE <= point[0] <= 180 * COORDINATE_SCALE
      or not -90 * COORDINATE_SCALE <= point[1] <= 90 * COORDINATE_SCALE):
    raise AmapRouteAnchorRejected("invalid_native_coordinate")
  return tuple(point)


def _degree_point(point) -> tuple[float, float]:
  if not isinstance(point, (tuple, list)) or len(point) != 2:
    raise AmapRouteAnchorRejected("invalid_position")
  try:
    valid = all(type(value) in (int, float) and math.isfinite(value) for value in point)
  except OverflowError:
    valid = False
  if not valid or not -180 <= point[0] <= 180 or not -90 <= point[1] <= 90:
    raise AmapRouteAnchorRejected("invalid_position")
  return tuple(map(float, point))


def _within_tolerance(geometric: float, declared: float) -> bool:
  """Relative agreement between measured geometry and the cache's declared length."""
  return declared > 0 and abs(geometric - declared) / declared <= LONG_EDGE_RELATIVE_TOLERANCE


def _length(start: tuple[float, float], end: tuple[float, float]) -> float:
  return math.hypot((end[0] - start[0]) * math.cos(math.radians((start[1] + end[1]) / 2)),
                    end[1] - start[1]) * METERS_PER_DEGREE


@dataclass(frozen=True)
class SourceEdge:
  segment_index: int
  link_index: int
  point_index: int
  global_segment: int
  start: tuple[float, float] = field(repr=False)
  end: tuple[float, float] = field(repr=False)
  start_progress_m: float
  length_m: float


@dataclass(frozen=True)
class AnchoredProjection:
  edge: SourceEdge
  fraction: float
  lateral_m: float
  progress_m: float
  coordinate: tuple[float, float] = field(repr=False)
  pose: tuple[float, float, float] = field(repr=False)

  @property
  def global_segment(self) -> int:
    return self.edge.global_segment


class AmapRouteAnchorIndex:
  coordinate_frame = "amap_native_map"
  coordinate_datum_verified = False

  def __init__(self, route: CachedRoute):
    if not isinstance(route, CachedRoute) or not 1 <= len(route.segments) <= MAX_SEGMENTS:
      raise AmapRouteAnchorRejected("invalid_cached_route")
    points: list[tuple[int, int]] = []
    spans = []
    for segment in route.segments:
      if not 2 <= len(segment.points_native) <= MAX_ROUTE_POINTS or not segment.links:
        raise AmapRouteAnchorRejected("invalid_segment_shape")
      native = tuple(_native_point(point) for point in segment.points_native)
      if points and points[-1] != native[0]:
        raise AmapRouteAnchorRejected("discontinuous_segment_join")
      offset = len(points) - 1 if points else 0
      if offset + len(native) > MAX_ROUTE_POINTS:
        raise AmapRouteAnchorRejected("route_point_limit")
      expected_start = 0
      links = []
      for link in segment.links:
        start, count = link.point_start, link.point_count
        if (type(start) is not int or type(count) is not int or count < 2 or start != expected_start
            or start + count > len(native)):
          raise AmapRouteAnchorRejected("invalid_link_coverage")
        links.append((offset + start, count))
        expected_start = start + count - 1
      if expected_start != len(native) - 1:
        raise AmapRouteAnchorRejected("incomplete_link_coverage")
      spans.append(tuple(links))
      points.extend(native[1:] if points else native)
    self.points_native = tuple(points)
    self.coordinates = tuple((x / COORDINATE_SCALE, y / COORDINATE_SCALE) for x, y in points)
    self._spans = tuple(spans)
    lengths = [_length(start, end)
               for start, end in zip(self.coordinates[:-1], self.coordinates[1:], strict=True)]
    # A native edge beyond the conservative bound needs the cache's own declared
    # lengths to corroborate it. This only admits geometry already in the encoding;
    # point order, coordinates and per-edge lengths are never modified or bridged.
    if any(length > MAX_EDGE_M for length in lengths) and not self._long_edge_corroborated(route, lengths):
      raise AmapRouteAnchorRejected("discontinuous_source_edge")
    distance = [0.0]
    for length in lengths:
      distance.append(distance[-1] + length)
    if distance[-1] <= 0:
      raise AmapRouteAnchorRejected("degenerate_route")
    self.distance_m = tuple(distance)

  @classmethod
  def from_native(cls, points_native, spans):
    """Build directly from a validated V2 compact point array and global link spans.

    Adjacent native links can share multiple exact points in the same compact
    array. Both starts and ends must advance, with at least one shared point
    and at least one new point; no coordinates are dropped or reconstructed.
    Native geometry has no declared per-link lengths, so any edge above the
    conservative 2 km gate remains an explicit rejection.
    """
    if not isinstance(points_native, (tuple, list)) or not isinstance(spans, (tuple, list)):
      raise AmapRouteAnchorRejected("invalid_native_route")
    if not 1 <= len(spans) <= MAX_SEGMENTS or not 2 <= len(points_native) <= MAX_ROUTE_POINTS:
      raise AmapRouteAnchorRejected("invalid_native_route")
    points = tuple(_native_point(point) for point in points_native)
    checked_spans = []
    previous_segment = -1
    previous_end = 0
    for segment_index, segment in enumerate(spans):
      if not isinstance(segment, (tuple, list)) or not segment:
        raise AmapRouteAnchorRejected("invalid_segment_shape")
      row = []
      first_in_segment = True
      previous_start = -1
      segment_end = -1
      for entry in segment:
        if not isinstance(entry, (tuple, list)) or len(entry) != 2:
          raise AmapRouteAnchorRejected("invalid_link_coverage")
        start, count = entry
        if (type(start) is not int or type(count) is not int or count < 2
            or start < 0 or start + count > len(points)):
          raise AmapRouteAnchorRejected("invalid_link_coverage")
        if first_in_segment:
          if (segment_index == 0 and start != 0) or (
              segment_index > 0 and start != previous_end - 1):
            raise AmapRouteAnchorRejected("discontinuous_segment_join")
          first_in_segment = False
        elif not previous_start < start < segment_end < start + count:
          raise AmapRouteAnchorRejected("invalid_link_coverage")
        row.append((start, count))
        previous_start = start
        segment_end = start + count
      if segment_end != len(points) if segment_index == len(spans) - 1 else False:
        raise AmapRouteAnchorRejected("incomplete_link_coverage")
      checked_spans.append(tuple(row))
      previous_segment, previous_end = segment_index, segment_end
    if previous_segment != len(spans) - 1 or previous_end != len(points):
      raise AmapRouteAnchorRejected("incomplete_link_coverage")

    instance = cls.__new__(cls)
    instance.points_native = points
    instance.coordinates = tuple((x / COORDINATE_SCALE, y / COORDINATE_SCALE) for x, y in points)
    instance._spans = tuple(checked_spans)
    lengths = [_length(start, end)
               for start, end in zip(instance.coordinates[:-1], instance.coordinates[1:], strict=True)]
    if any(length > MAX_EDGE_M for length in lengths):
      raise AmapRouteAnchorRejected("discontinuous_source_edge")
    distance = [0.0]
    for length in lengths:
      distance.append(distance[-1] + length)
    if distance[-1] <= 0:
      raise AmapRouteAnchorRejected("degenerate_route")
    instance.distance_m = tuple(distance)
    return instance

  @staticmethod
  def _declared_positive(value) -> bool:
    """Strict positive int inside the decoder's uint32 range; bool and floats rejected."""
    return type(value) is int and 0 < value <= 0xFFFFFFFF

  def _long_edge_corroborated(self, route: CachedRoute, lengths: list[float]) -> bool:
    """Every link carrying a >MAX_EDGE_M edge must match its declared length within tolerance.

    Lengths are recomputed here with the same great-circle rule the downstream
    verifier uses, so the two 2% gates can never disagree; distance_m is untouched.
    """
    from openpilot.selfdrive.navigation.phone_route import _edge_length
    lengths = [_edge_length(a, b) for a, b in zip(self.coordinates[:-1], self.coordinates[1:], strict=True)]
    route_declared = route.distance_centi_m
    if not self._declared_positive(route_declared):
      return False
    route_m = route_declared / 100
    total_geometric = sum(lengths)
    for segment, spans in zip(route.segments, self._spans, strict=True):
      for link, (offset, count) in zip(segment.links, spans, strict=True):
        edge_lengths = lengths[offset:offset + count - 1]
        if not any(length > MAX_EDGE_M for length in edge_lengths):
          continue
        link_declared = link.distance_centi_m
        if not self._declared_positive(link_declared):
          return False
        link_m = link_declared / 100
        if link_m > route_m:
          return False
        geometric = sum(edge_lengths)
        if not _within_tolerance(geometric, link_m):
          return False
        if not _within_tolerance(total_geometric, route_m):
          return False
    return True

  def __repr__(self):
    return f"AmapRouteAnchorIndex(segments={len(self._spans)}, points={len(self.coordinates)}, coordinate_frame={self.coordinate_frame!r})"

  def edge(self, segment_index: int, link_index: int, point_index: int) -> SourceEdge:
    segment = self._spans[_index(segment_index, "segment_index", len(self._spans))]
    offset, count = segment[_index(link_index, "link_index", len(segment))]
    _index(point_index, "point_index", count)
    if point_index == count - 1:
      raise AmapRouteAnchorRejected("terminal_local_point_unverified")
    index = offset + point_index
    length = self.distance_m[index + 1] - self.distance_m[index]
    if length <= 0:
      raise AmapRouteAnchorRejected("degenerate_source_edge")
    return SourceEdge(segment_index, link_index, point_index, index,
                      self.coordinates[index], self.coordinates[index + 1], self.distance_m[index], length)

  def pose(self, progress_m: float) -> tuple[float, float, float]:
    try:
      valid = type(progress_m) in (int, float) and math.isfinite(progress_m)
    except OverflowError:
      valid = False
    if not valid:
      raise AmapRouteAnchorRejected("invalid_progress")

    def interpolate(progress):
      progress = min(self.distance_m[-1], max(0.0, progress))
      index = min(len(self.coordinates) - 2, bisect_right(self.distance_m, progress) - 1)
      length = self.distance_m[index + 1] - self.distance_m[index]
      fraction = (progress - self.distance_m[index]) / length if length > 0 else 0.0
      return tuple(a + fraction * (b - a) for a, b in zip(self.coordinates[index], self.coordinates[index + 1], strict=True))

    before = interpolate(progress_m - HEADING_HALF_WINDOW_M)
    center = interpolate(progress_m)
    after = interpolate(progress_m + HEADING_HALF_WINDOW_M)
    bearing = math.degrees(math.atan2((after[0] - before[0]) * math.cos(math.radians(before[1])), after[1] - before[1])) % 360
    return (*center, bearing)

  def project(self, segment_index: int, link_index: int, point_index: int, position,
              *, max_lateral_m: float = 2.0) -> AnchoredProjection:
    edge = self.edge(segment_index, link_index, point_index)
    lon, lat = _degree_point(position)
    try:
      valid_limit = type(max_lateral_m) in (int, float) and math.isfinite(max_lateral_m) and 0 < max_lateral_m <= 1000
    except OverflowError:
      valid_limit = False
    if not valid_limit:
      raise AmapRouteAnchorRejected("invalid_lateral_limit")
    x_scale = METERS_PER_DEGREE * math.cos(math.radians((edge.start[1] + edge.end[1]) / 2))
    dx, dy = (edge.end[0] - edge.start[0]) * x_scale, (edge.end[1] - edge.start[1]) * METERS_PER_DEGREE
    px, py = (lon - edge.start[0]) * x_scale, (lat - edge.start[1]) * METERS_PER_DEGREE
    squared_length = dx * dx + dy * dy
    if squared_length <= 0:
      raise AmapRouteAnchorRejected("degenerate_source_edge")
    fraction = min(1.0, max(0.0, (px * dx + py * dy) / squared_length))
    lateral = math.hypot(px - fraction * dx, py - fraction * dy)
    if lateral > max_lateral_m:
      raise AmapRouteAnchorRejected("position_outside_source_edge")
    coordinate = tuple(a + fraction * (b - a) for a, b in zip(edge.start, edge.end, strict=True))
    progress = edge.start_progress_m + fraction * edge.length_m
    return AnchoredProjection(edge, fraction, lateral, progress, coordinate, self.pose(progress))
