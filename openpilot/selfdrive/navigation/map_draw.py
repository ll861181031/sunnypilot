"""Geometry and rasterization for the model's 256x256 map input.

Ported from commaai/openpilot's `openpilot/selfdrive/navigation/renderer.py`
(nav-model-integration branch, MIT) and trimmed to what this build can supply: the
training layout (1 m per pixel, ego three quarters down the frame, white ego arrow,
grey route) without the Protomaps basemap, which needs comma's authenticated tile
archive. The route itself is real, so the model still gets route geometry and a
consistent ego frame; the surrounding road graph is the remaining gap.

Keeping the exact geometry constants matters more than it looks: the model was
trained on this framing, so a different ego offset or scale would feed it a
distribution it never saw.
"""
import math

import numpy as np

SIZE = 256
FORWARD_OFFSET = 64.0  # the ego sits three quarters down the frame
EGO = np.array([SIZE / 2, SIZE / 2 + FORWARD_OFFSET])  # pixel position of the ego
EARTH_CIRCUMFERENCE = 40075016.68557849  # equatorial, for Web Mercator
BACKGROUND = 0
ROUTE_INTENSITY = 128
ROUTE_WIDTH = 5.0  # pixels
MAX_ROAD_DISTANCE = 100.0  # meters from the ego to the nearest route point for a valid map
ARROW = np.array([(0, -9), (7, 7), (0, 3), (-7, 7)], dtype=np.float64)  # ego marker, pixels around the ego
ARROW_OUTLINE = 2.0  # pixels


def mercator(coordinates: np.ndarray) -> np.ndarray:
  """Longitude/latitude -> normalized Web Mercator, x east / y south."""
  coordinates = np.asarray(coordinates, dtype=np.float64).reshape((-1, 2))
  longitude, latitude = coordinates.T
  y = (1.0 - np.arcsinh(np.tan(np.deg2rad(latitude))) / np.pi) / 2.0
  return np.column_stack(((longitude + 180.0) / 360.0, y))


def project(points: np.ndarray, longitude: float, latitude: float, bearing: float) -> np.ndarray:
  """Mercator points to pixels of a frame at a position, headed along a bearing in degrees clockwise from north."""
  points = np.asarray(points, dtype=np.float64).reshape((-1, 2))
  origin = mercator([[longitude, latitude]])[0]
  delta = points - origin
  delta[:, 0] = (delta[:, 0] + 0.5) % 1.0 - 0.5  # wrap across the antimeridian
  delta *= EARTH_CIRCUMFERENCE * math.cos(math.radians(latitude))  # meters, which are pixels
  c, s = math.cos(math.radians(bearing)), math.sin(math.radians(bearing))
  return EGO + delta @ np.array([[c, -s], [s, c]])  # x right, y down


def draw_segments(image: np.ndarray, starts: np.ndarray, ends: np.ndarray, radii, intensity: int,
                  *, overlay: bool = False, caps: np.ndarray | None = None):
  """Strokes with round joins and one pixel of analytic edge antialiasing.

  Max blending (the default) makes draw order irrelevant; overlay replaces the pixels beneath.
  """
  starts = np.asarray(starts, dtype=np.float64).reshape((-1, 2))
  ends = np.asarray(ends, dtype=np.float64).reshape((-1, 2))
  if not len(starts):
    return
  radii = np.broadcast_to(np.asarray(radii, dtype=np.float64), (len(starts),))
  reach = radii[:, None] + 1
  low = np.maximum(np.floor(np.minimum(starts, ends) - reach), 0).astype(int)
  high = np.minimum(np.ceil(np.maximum(starts, ends) + reach), [image.shape[1] - 1, image.shape[0] - 1]).astype(int)
  visible = np.flatnonzero((high >= low).all(axis=1))
  round_caps = np.ones((len(starts), 2), dtype=bool) if caps is None else caps
  for i, (sx, sy), (ex, ey), (x0, y0), (x1, y1), radius in zip(
      visible.tolist(), starts[visible].tolist(), ends[visible].tolist(),
      low[visible].tolist(), high[visible].tolist(), radii[visible].tolist(), strict=True):
    dx, dy = ex - sx, ey - sy
    px = np.arange(x0, x1 + 1) + (0.5 - sx)
    py = (np.arange(y0, y1 + 1) + (0.5 - sy))[:, None]
    along = (px * dx + py * dy) / max(dx * dx + dy * dy, 1e-12)
    t = np.clip(along, 0, 1)
    alpha = np.clip(radius + 0.5 - np.hypot(px - t * dx, py - t * dy), 0, 1)
    if not round_caps[i, 0]:
      alpha = alpha * (along >= 0)
    if not round_caps[i, 1]:
      alpha = alpha * (along <= 1)
    view = image[y0:y1 + 1, x0:x1 + 1]
    if overlay:
      view[:] = np.rint(view * (1 - alpha) + intensity * alpha).astype(np.uint8)
    else:
      np.maximum(view, np.rint(alpha * intensity).astype(np.uint8), out=view)


def draw_vehicle(image: np.ndarray):
  """The training ego marker: a white arrow with a black outline, drawn over everything else."""
  arrow = ARROW + EGO
  (x0, y0), (x1, y1) = (np.floor(arrow.min(axis=0)) - 2).astype(int), (np.ceil(arrow.max(axis=0)) + 2).astype(int)
  px, py = np.meshgrid(np.arange(x0, x1) + 0.5, np.arange(y0, y1) + 0.5)
  inside = np.zeros(px.shape, dtype=bool)
  for a, b, c in ((arrow[0], arrow[1], arrow[2]), (arrow[0], arrow[2], arrow[3])):
    sides = [(q[0] - p[0]) * (py - p[1]) - (q[1] - p[1]) * (px - p[0]) for p, q in ((a, b), (b, c), (c, a))]
    inside |= ((sides[0] >= 0) & (sides[1] >= 0) & (sides[2] >= 0)) | ((sides[0] <= 0) & (sides[1] <= 0) & (sides[2] <= 0))
  image[y0:y1, x0:x1][inside] = 255
  draw_segments(image, arrow, np.roll(arrow, -1, axis=0), ARROW_OUTLINE / 2, 0, overlay=True)


def nearest_route_distance(pixels: np.ndarray) -> float:
  if not len(pixels):
    return math.inf
  if len(pixels) == 1:
    return float(np.linalg.norm(pixels[0] - EGO))
  starts = pixels[:-1]
  segment = pixels[1:] - starts
  fraction = np.clip(((EGO - starts) * segment).sum(axis=1)
                     / np.maximum((segment * segment).sum(axis=1), 1e-12), 0.0, 1.0)
  return float(np.linalg.norm(starts + fraction[:, None] * segment - EGO, axis=1).min())


def render_route(route: np.ndarray, longitude: float, latitude: float, bearing: float) -> tuple[np.ndarray, bool]:
  """The map image at a pose: route grey, ego white, everything else background.

  Returns the image and whether the route actually comes near the ego, mirroring the upstream
  map-validity gate (the model must not be told a map exists when the route is somewhere else)."""
  image = np.full((SIZE, SIZE), BACKGROUND, dtype=np.uint8)
  route = np.asarray(route, dtype=np.float64).reshape((-1, 2))
  pixels = project(mercator(route), longitude, latitude, bearing) if len(route) else np.empty((0, 2))
  if len(pixels) >= 2:
    draw_segments(image, pixels[:-1], pixels[1:], ROUTE_WIDTH / 2, ROUTE_INTENSITY, overlay=True)
  draw_vehicle(image)
  # A single point is not guidance: validity needs a real segment near the ego.
  valid = len(pixels) >= 2 and nearest_route_distance(pixels) <= MAX_ROAD_DISTANCE
  return image, valid
