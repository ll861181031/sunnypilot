"""Unit tests for the pure geometry helpers."""

from __future__ import annotations


import pytest

import geometry as g


# ---------------------------------------------------------------------------
# Basic box helpers
# ---------------------------------------------------------------------------
def test_normalize_box_orders_coordinates():
    assert g.normalize_box((10, 20, 5, 8)) == (5, 8, 10, 20)


def test_box_iou_known_values():
    assert g.box_iou((0, 0, 10, 10), (0, 0, 10, 10)) == pytest.approx(1.0)
    assert g.box_iou((0, 0, 10, 10), (20, 20, 30, 30)) == 0.0
    # Half overlapping: inter 50, union 150.
    assert g.box_iou((0, 0, 10, 10), (5, 0, 15, 10)) == pytest.approx(50 / 150)


def test_box_iou_degenerate_box_is_zero():
    assert g.box_iou((5, 5, 5, 5), (5, 5, 5, 5)) == 0.0


def test_boxes_close():
    assert g.boxes_close((0, 1, 2, 3), (0.00001, 1, 2, 3))
    assert not g.boxes_close((0, 1, 2, 3), (0.1, 1, 2, 3))


def test_norm_abs_roundtrip():
    box = (0.1, 0.2, 0.6, 0.8)
    absolute = g.norm_to_abs_box(box, 1000, 500)
    assert absolute == pytest.approx((100, 100, 600, 400))
    assert g.abs_to_norm_box(absolute, 1000, 500) == pytest.approx(box)


def test_abs_to_norm_clamps_to_image():
    assert g.abs_to_norm_box((-50, -50, 2000, 1000), 1000, 500) == (0.0, 0.0, 1.0, 1.0)


# ---------------------------------------------------------------------------
# View transform
# ---------------------------------------------------------------------------
def test_view_fit_centres_image():
    view = g.View.fit(1344, 760, 1000, 760, pad=1.0)
    assert view.zoom == pytest.approx(min(1000 / 1344, 760 / 760))
    # Image centre maps to canvas centre.
    cx, cy = view.to_canvas(1344 / 2, 760 / 2)
    assert cx == pytest.approx(500)
    assert cy == pytest.approx(380)


def test_view_roundtrip_image_canvas():
    view = g.View(zoom=2.5, ox=-120, oy=40)
    for point in [(0, 0), (100, 200), (1344, 760)]:
        canvas = view.to_canvas(*point)
        back = view.to_image(*canvas)
        assert back == pytest.approx(point)


def test_view_box_conversions_agree_with_point_conversion():
    view = g.View(zoom=0.75, ox=33, oy=-12)
    box = (10, 20, 300, 400)
    converted = view.box_to_canvas(box)
    assert converted[0] == pytest.approx(view.to_canvas(10, 20)[0])
    assert converted[3] == pytest.approx(view.to_canvas(300, 400)[1])
    assert view.box_to_image(converted) == pytest.approx(box)


def test_zoomed_at_keeps_anchor_pixel_fixed():
    view = g.View(zoom=1.0, ox=10, oy=20)
    anchor = (400.0, 300.0)
    image_point = view.to_image(*anchor)
    zoomed = view.zoomed_at(*anchor, 3.0)
    assert zoomed.zoom == pytest.approx(3.0)
    assert zoomed.to_canvas(*image_point) == pytest.approx(anchor)


def test_zoomed_at_respects_limits():
    view = g.View(zoom=1.0)
    assert view.zoomed_at(0, 0, 1000).zoom == pytest.approx(g.View.MAX_ZOOM)
    assert view.zoomed_at(0, 0, 0.000001).zoom == pytest.approx(g.View.MIN_ZOOM)


def test_panned_keeps_zoom():
    view = g.View(zoom=2.0, ox=0, oy=0)
    moved = view.panned(10, -5)
    assert (moved.zoom, moved.ox, moved.oy) == (2.0, 10.0, -5.0)


def test_visible_source_rect_clips_to_image():
    view = g.View(zoom=1.0, ox=0, oy=0)
    assert view.visible_source_rect(1000, 600, 1344, 760) == (0, 0, 1000, 600)
    view = g.View(zoom=1.0, ox=-500, oy=-200)
    assert view.visible_source_rect(1000, 600, 1344, 760) == (500, 200, 1344, 760)
    # Entirely to the left of the image: empty rect.
    view = g.View(zoom=1.0, ox=-2000, oy=0)
    left, top, right, bottom = view.visible_source_rect(1000, 600, 1344, 760)
    assert right <= left


def test_clamp_view_keeps_image_reachable():
    view = g.View(zoom=1.0, ox=-100000, oy=100000)
    clamped = g.clamp_view(view, 1344, 760, 1000, 760)
    assert clamped.ox > -100000
    assert clamped.oy < 100000


# ---------------------------------------------------------------------------
# Hit testing / handles
# ---------------------------------------------------------------------------
def test_handle_positions_are_the_eight_expected_points():
    positions = g.handle_positions((10, 20, 110, 220))
    assert set(positions) == set(g.HANDLES)
    assert positions["nw"] == (10, 20)
    assert positions["se"] == (110, 220)
    assert positions["n"] == (60, 20)
    assert positions["w"] == (10, 120)


def test_hit_test_handle_of_selected_box_wins():
    boxes = [(0, 0, 100, 100), (50, 50, 150, 150)]
    # Near the "se" handle of box 0, which is also inside box 1.
    index, handle = g.hit_test(boxes, 100, 100, selected=0, handle_radius=6)
    assert (index, handle) == (0, "se")


def test_hit_test_topmost_box_wins():
    boxes = [(0, 0, 100, 100), (10, 10, 60, 60)]
    index, handle = g.hit_test(boxes, 30, 30, selected=None)
    assert (index, handle) == (1, None)


def test_hit_test_empty_space():
    assert g.hit_test([(0, 0, 10, 10)], 500, 500) == (None, None)


def test_hit_test_invalid_selected_index_is_ignored():
    assert g.hit_test([(0, 0, 10, 10)], 5, 5, selected=7) == (0, None)


def test_cursor_for_handle():
    assert g.cursor_for_handle(None) == "crosshair"
    assert g.cursor_for_handle("nw") == "top_left_corner"
    assert g.cursor_for_handle("e") == "right_side"


# ---------------------------------------------------------------------------
# Resize / move / clamp
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("handle,dx,dy,expected", [
    ("nw", 10, 10, (10, 20, 100, 200)),
    ("ne", -10, 10, (0, 20, 90, 200)),
    ("se", -10, -10, (0, 10, 90, 190)),
    ("sw", 10, -10, (10, 10, 100, 190)),
    ("n", 999, 10, (0, 20, 100, 200)),   # x unaffected by a horizontal-only handle
    ("s", 999, -10, (0, 10, 100, 190)),
    ("w", 10, 999, (10, 10, 100, 200)),  # y unaffected by a vertical-only handle
    ("e", -10, 999, (0, 10, 90, 200)),
])
def test_resize_box_moves_only_the_expected_edges(handle, dx, dy, expected):
    assert g.resize_box((0, 10, 100, 200), handle, dx, dy) == expected


def test_resize_box_never_inverts_and_respects_min_size():
    box = (0, 0, 20, 20)
    # Dragging the east edge far to the left stops at min_size.
    resized = g.resize_box(box, "e", -500, 0, min_size=4)
    assert resized[2] - resized[0] == pytest.approx(4)
    resized = g.resize_box(box, "n", 0, 500, min_size=4)
    assert resized[3] - resized[1] == pytest.approx(4)


def test_resize_box_unknown_handle_raises():
    with pytest.raises(ValueError):
        g.resize_box((0, 0, 10, 10), "middle", 1, 1)


def test_move_box_translates():
    assert g.move_box((10, 10, 50, 50), 5, -3) == (15, 7, 55, 47)


def test_move_box_is_clamped_into_bounds():
    width, height = 100, 100
    assert g.move_box((10, 10, 50, 50), -500, 0, bounds=(width, height)) == (0, 10, 40, 50)
    assert g.move_box((10, 10, 50, 50), 0, 500, bounds=(width, height)) == (10, 60, 50, 100)


def test_clamp_box_clips_to_image():
    assert g.clamp_box((-10, -10, 50, 50), 100, 100) == (0, 0, 50, 50)
    assert g.clamp_box((80, 80, 140, 140), 100, 100) == (80, 80, 100, 100)


def test_clamp_box_enforces_min_size_and_stays_inside():
    clamped = g.clamp_box((99, 99, 99, 99), 100, 100, min_size=6)
    assert clamped[2] - clamped[0] >= 6 - 1e-9
    assert clamped[3] - clamped[1] >= 6 - 1e-9
    assert 0 <= clamped[0] and clamped[2] <= 100
    assert 0 <= clamped[1] and clamped[3] <= 100


def test_box_from_drag_normalises_and_rejects_tiny_boxes():
    assert g.box_from_drag(100, 100, 20, 30) == (20, 30, 100, 100)
    assert g.box_from_drag(10, 10, 12, 12, min_size=4) is None
    assert g.box_from_drag(10, 10, 20, 11, min_size=4) is None  # too flat
