"""Map rasterization geometry: the training layout must survive the port.

The model was trained on 1 m per pixel with the ego three quarters down the frame, so a
silent change of scale or ego offset would feed it a distribution it never saw. These
tests pin the projection and the visible route/ego drawing.
"""
import numpy as np
import pytest

from openpilot.selfdrive.navigation.map_draw import (
  BACKGROUND,
  EGO,
  ROUTE_INTENSITY,
  SIZE,
  mercator,
  project,
  render_route,
)

LON, LAT = -122.4000, 37.8000
METERS_PER_DEGREE = 2 * np.pi * 6371008.8 / 360
LAT_DEG_PER_METER = 1 / METERS_PER_DEGREE
LON_DEG_PER_METER = 1 / (METERS_PER_DEGREE * np.cos(np.radians(LAT)))


def route_north(length_m: float, step_m: float = 10.0) -> np.ndarray:
  steps = int(length_m // step_m) + 1
  return np.array([[LON, LAT + i * step_m * LAT_DEG_PER_METER] for i in range(steps)])


class TestProjection:
  def test_a_point_ahead_lands_above_the_ego(self):
    point = mercator([[LON, LAT + 50 * LAT_DEG_PER_METER]])
    (x, y), = project(point, LON, LAT, bearing=0.0)
    assert x == pytest.approx(EGO[0], abs=0.2)
    assert y == pytest.approx(EGO[1] - 50, abs=0.2)

  def test_a_point_right_of_the_ego_lands_right(self):
    point = mercator([[LON + 30 * LON_DEG_PER_METER, LAT]])
    (x, y), = project(point, LON, LAT, bearing=0.0)
    assert x == pytest.approx(EGO[0] + 30, abs=0.2)
    assert y == pytest.approx(EGO[1], abs=0.2)

  def test_bearing_rotates_the_frame(self):
    """Heading east, a point one metre ahead is to the image's right, not above the ego."""
    point = mercator([[LON + 20 * LON_DEG_PER_METER, LAT]])
    (x, y), = project(point, LON, LAT, bearing=90.0)
    assert x == pytest.approx(EGO[0], abs=0.2)
    assert y == pytest.approx(EGO[1] - 20, abs=0.2)


class TestRenderRoute:
  def test_route_ahead_is_drawn_above_the_ego(self):
    route = route_north(120)
    image, valid = render_route(route, LON, LAT, 0.0)
    assert image.shape == (SIZE, SIZE) and image.dtype == np.uint8
    assert valid is True
    above = image[: int(EGO[1]) - 10, :]
    assert (above == ROUTE_INTENSITY).any(), "the route ahead must be visible"
    assert not (image[int(EGO[1]) + 10:, :] == ROUTE_INTENSITY).any()

  def test_ego_arrow_is_white(self):
    image, _ = render_route(route_north(60), LON, LAT, 0.0)
    y, x = int(EGO[1]) - 4, int(EGO[0])
    assert image[y, x] == 255

  def test_background_stays_empty_off_route(self):
    image, _ = render_route(route_north(60), LON, LAT, 0.0)
    assert image[0, 0] == BACKGROUND

  def test_a_route_far_away_is_not_valid(self):
    """The map-validity gate the model relies on: a route 2 km away must not look like guidance."""
    far = route_north(60) + np.array([0.02, 0.0])  # ~1.8 km east
    image, valid = render_route(far, LON, LAT, 0.0)
    assert valid is False
    assert image.shape == (SIZE, SIZE)

  def test_sparse_segment_near_ego_is_valid_even_when_both_vertices_are_far(self):
    route = np.array([[LON, LAT - 150 * LAT_DEG_PER_METER],
                      [LON, LAT + 150 * LAT_DEG_PER_METER]])
    image, valid = render_route(route, LON, LAT, 0.0)
    assert valid
    assert (image[:100] == ROUTE_INTENSITY).any()

  def test_no_route_is_not_valid(self):
    image, valid = render_route(np.empty((0, 2)), LON, LAT, 0.0)
    assert valid is False
    assert not (image[:100, :] == ROUTE_INTENSITY).any(), "no route means no guidance pixels"
    assert (image == 255).any(), "the ego arrow is still drawn"

  def test_single_point_route_is_not_valid(self):
    _, valid = render_route(np.array([[LON, LAT]]), LON, LAT, 0.0)
    assert valid is False
