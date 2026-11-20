"""Geometry helpers for the labeler.

Everything in this module is pure (no Tk, no I/O) so it can be unit tested
headless.  Two coordinate spaces are used:

* **image space**: pixels of the source JPEG, origin at the top-left corner,
  ``(0, 0) - (width, height)``.
* **canvas space**: pixels of the Tk canvas widget, same orientation.

A box is always a ``(x1, y1, x2, y2)`` tuple with ``x1 <= x2`` and
``y1 <= y2``.  Normalised (YOLO) boxes are the same tuple divided by the
image size; helper functions convert between the two.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Sequence

Box = tuple[float, float, float, float]

# Minimum box side, in image pixels, when creating or resizing a box.
MIN_BOX_PX = 4.0
# Handle hit radius, in canvas pixels (constant on screen regardless of zoom).
HANDLE_RADIUS_PX = 7.0
# Handle draw size, in canvas pixels.
HANDLE_SIZE_PX = 4

HANDLES = ("nw", "n", "ne", "e", "se", "s", "sw", "w")

# Tk cursor names for each resize handle.
HANDLE_CURSORS: dict[str, str] = {
    "nw": "top_left_corner",
    "n": "top_side",
    "ne": "top_right_corner",
    "e": "right_side",
    "se": "bottom_right_corner",
    "s": "bottom_side",
    "sw": "bottom_left_corner",
    "w": "left_side",
}


def clamp(value: float, low: float, high: float) -> float:
    """Clamp *value* into ``[low, high]`` (tolerates ``low > high``)."""
    if low > high:
        low, high = high, low
    return low if value < low else high if value > high else value


def normalize_box(box: Sequence[float]) -> Box:
    """Return *box* with ``x1 <= x2`` and ``y1 <= y2``."""
    x1, y1, x2, y2 = (float(v) for v in box)
    return (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))


def box_width(box: Sequence[float]) -> float:
    return float(box[2]) - float(box[0])


def box_height(box: Sequence[float]) -> float:
    return float(box[3]) - float(box[1])


def box_area(box: Sequence[float]) -> float:
    return max(0.0, box_width(box)) * max(0.0, box_height(box))


def box_iou(a: Sequence[float], b: Sequence[float]) -> float:
    """Intersection over union of two boxes in the same space."""
    ax1, ay1, ax2, ay2 = normalize_box(a)
    bx1, by1, bx2, by2 = normalize_box(b)
    iw = max(0.0, min(ax2, bx2) - max(ax1, bx1))
    ih = max(0.0, min(ay2, by2) - max(ay1, by1))
    inter = iw * ih
    if inter <= 0.0:
        return 0.0
    union = box_area((ax1, ay1, ax2, ay2)) + box_area((bx1, by1, bx2, by2)) - inter
    return inter / union if union > 0.0 else 0.0


def boxes_close(a: Sequence[float], b: Sequence[float], tol: float = 1e-4) -> bool:
    """True when every coordinate differs by at most *tol*."""
    return all(abs(float(x) - float(y)) <= tol
               for x, y in zip(a, b, strict=False))


def norm_to_abs_box(box: Sequence[float], width: float, height: float) -> Box:
    """Convert a normalised box to image pixels."""
    x1, y1, x2, y2 = normalize_box(box)
    return (x1 * width, y1 * height, x2 * width, y2 * height)


def abs_to_norm_box(box: Sequence[float], width: float, height: float) -> Box:
    """Convert an image-pixel box to normalised coordinates (clamped to 0..1)."""
    if width <= 0 or height <= 0:
        return (0.0, 0.0, 0.0, 0.0)
    x1, y1, x2, y2 = normalize_box(box)
    return (
        clamp(x1 / width, 0.0, 1.0),
        clamp(y1 / height, 0.0, 1.0),
        clamp(x2 / width, 0.0, 1.0),
        clamp(y2 / height, 0.0, 1.0),
    )


def clamp_box(box: Sequence[float], width: float, height: float, min_size: float = MIN_BOX_PX) -> Box:
    """Clip *box* to the image and enforce a minimum size.

    The box is first clipped to ``[0, width] x [0, height]``; if that leaves it
    smaller than *min_size* it is grown around its centre (and shifted back
    inside the image when needed).
    """
    x1, y1, x2, y2 = normalize_box(box)
    x1 = clamp(x1, 0.0, width)
    x2 = clamp(x2, 0.0, width)
    y1 = clamp(y1, 0.0, height)
    y2 = clamp(y2, 0.0, height)

    if box_width((x1, y1, x2, y2)) < min_size:
        cx = (x1 + x2) / 2.0
        half = min(min_size, width) / 2.0
        x1, x2 = cx - half, cx + half
    if box_height((x1, y1, x2, y2)) < min_size:
        cy = (y1 + y2) / 2.0
        half = min(min_size, height) / 2.0
        y1, y2 = cy - half, cy + half

    # Shift back inside the image after growing.
    dx = 0.0
    if x1 < 0.0:
        dx = -x1
    elif x2 > width:
        dx = width - x2
    dy = 0.0
    if y1 < 0.0:
        dy = -y1
    elif y2 > height:
        dy = height - y2
    return (x1 + dx, y1 + dy, x2 + dx, y2 + dy)


def move_box(box: Sequence[float], dx: float, dy: float,
             bounds: tuple[float, float] | None = None) -> Box:
    """Translate *box* by ``(dx, dy)``; keep it inside ``bounds`` if given."""
    x1, y1, x2, y2 = normalize_box(box)
    x1 += dx
    x2 += dx
    y1 += dy
    y2 += dy
    if bounds is not None:
        width, height = bounds
        dx2 = 0.0
        if x1 < 0.0:
            dx2 = -x1
        elif x2 > width:
            dx2 = width - x2
        dy2 = 0.0
        if y1 < 0.0:
            dy2 = -y1
        elif y2 > height:
            dy2 = height - y2
        x1 += dx2
        x2 += dx2
        y1 += dy2
        y2 += dy2
    return (x1, y1, x2, y2)


def resize_box(box: Sequence[float], handle: str, dx: float, dy: float,
               min_size: float = MIN_BOX_PX) -> Box:
    """Move one edge/corner of *box* by ``(dx, dy)``.

    The opposite edge stays fixed; the moving edge is stopped at
    ``min_size`` so the box can never become inverted.
    """
    if handle not in HANDLES:
        raise ValueError(f"unknown handle: {handle!r}")
    x1, y1, x2, y2 = normalize_box(box)
    if "w" in handle:
        x1 = min(x1 + dx, x2 - min_size)
    if "e" in handle:
        x2 = max(x2 + dx, x1 + min_size)
    if "n" in handle:
        y1 = min(y1 + dy, y2 - min_size)
    if "s" in handle:
        y2 = max(y2 + dy, y1 + min_size)
    return (x1, y1, x2, y2)


def box_from_drag(x0: float, y0: float, x1: float, y1: float,
                  min_size: float = MIN_BOX_PX) -> Box | None:
    """Build a normalised box from a drag; ``None`` when too small."""
    box = normalize_box((x0, y0, x1, y1))
    if box_width(box) < min_size or box_height(box) < min_size:
        return None
    return box


def handle_positions(box: Sequence[float]) -> dict[str, tuple[float, float]]:
    """Return the 8 handle centres of *box* (same space as the input box)."""
    x1, y1, x2, y2 = normalize_box(box)
    cx = (x1 + x2) / 2.0
    cy = (y1 + y2) / 2.0
    return {
        "nw": (x1, y1),
        "n": (cx, y1),
        "ne": (x2, y1),
        "e": (x2, cy),
        "se": (x2, y2),
        "s": (cx, y2),
        "sw": (x1, y2),
        "w": (x1, cy),
    }


def hit_handle(box: Sequence[float], x: float, y: float,
               radius: float = HANDLE_RADIUS_PX) -> str | None:
    """Return the handle of *box* under canvas point ``(x, y)``, if any."""
    for name, (hx, hy) in handle_positions(box).items():
        if abs(x - hx) <= radius and abs(y - hy) <= radius:
            return name
    return None


def point_in_box(box: Sequence[float], x: float, y: float) -> bool:
    x1, y1, x2, y2 = normalize_box(box)
    return x1 <= x <= x2 and y1 <= y <= y2


def hit_test(boxes: Sequence[Sequence[float]], x: float, y: float,
             selected: int | None = None,
             handle_radius: float = HANDLE_RADIUS_PX) -> tuple[int | None, str | None]:
    """Hit test canvas-space *boxes* at ``(x, y)``.

    Returns ``(index, handle)``:

    * ``(i, "nw")`` / ... -- a resize handle of the selected box was hit
    * ``(i, None)``      -- the inside of box *i* was hit
    * ``(None, None)``   -- empty space

    Handles of the selected box win over everything else; otherwise the last
    (top-most) box wins so newly drawn boxes stay grabbable.
    """
    if selected is not None and 0 <= selected < len(boxes):
        handle = hit_handle(boxes[selected], x, y, handle_radius)
        if handle is not None:
            return selected, handle
    for index in range(len(boxes) - 1, -1, -1):
        if point_in_box(boxes[index], x, y):
            return index, None
    return None, None


@dataclass
class View:
    """Affine mapping between image pixels and canvas pixels.

    ``canvas = image * zoom + (ox, oy)``.
    """

    zoom: float = 1.0
    ox: float = 0.0
    oy: float = 0.0

    MIN_ZOOM = 0.05
    MAX_ZOOM = 16.0

    # -- conversions ------------------------------------------------------
    def to_canvas(self, ix: float, iy: float) -> tuple[float, float]:
        return ix * self.zoom + self.ox, iy * self.zoom + self.oy

    def to_image(self, cx: float, cy: float) -> tuple[float, float]:
        return (cx - self.ox) / self.zoom, (cy - self.oy) / self.zoom

    def box_to_canvas(self, box: Sequence[float]) -> Box:
        x1, y1, x2, y2 = normalize_box(box)
        cx1, cy1 = self.to_canvas(x1, y1)
        cx2, cy2 = self.to_canvas(x2, y2)
        return (cx1, cy1, cx2, cy2)

    def box_to_image(self, box: Sequence[float]) -> Box:
        x1, y1, x2, y2 = normalize_box(box)
        ix1, iy1 = self.to_image(x1, y1)
        ix2, iy2 = self.to_image(x2, y2)
        return (ix1, iy1, ix2, iy2)

    # -- construction -----------------------------------------------------
    @classmethod
    def fit(cls, img_w: float, img_h: float, canvas_w: float, canvas_h: float,
            pad: float = 0.98) -> View:
        """View that fits the whole image into the canvas, centred."""
        if img_w <= 0 or img_h <= 0 or canvas_w <= 0 or canvas_h <= 0:
            return cls()
        zoom = min(canvas_w / img_w, canvas_h / img_h) * pad
        zoom = clamp(zoom, cls.MIN_ZOOM, cls.MAX_ZOOM)
        return cls(zoom=zoom,
                   ox=(canvas_w - img_w * zoom) / 2.0,
                   oy=(canvas_h - img_h * zoom) / 2.0)

    # -- mutations (return new View instances) ----------------------------
    def zoomed_at(self, cx: float, cy: float, factor: float,
                  min_zoom: float = MIN_ZOOM, max_zoom: float = MAX_ZOOM) -> View:
        """Zoom by *factor* keeping canvas point ``(cx, cy)`` fixed."""
        new_zoom = clamp(self.zoom * factor, min_zoom, max_zoom)
        if new_zoom == self.zoom:
            return View(self.zoom, self.ox, self.oy)
        ix, iy = self.to_image(cx, cy)
        return View(zoom=new_zoom, ox=cx - ix * new_zoom, oy=cy - iy * new_zoom)

    def panned(self, dx: float, dy: float) -> View:
        return View(zoom=self.zoom, ox=self.ox + dx, oy=self.oy + dy)

    def with_zoom(self, zoom: float, min_zoom: float = MIN_ZOOM,
                  max_zoom: float = MAX_ZOOM) -> View:
        return View(zoom=clamp(zoom, min_zoom, max_zoom), ox=self.ox, oy=self.oy)

    # -- visibility -------------------------------------------------------
    def visible_source_rect(self, canvas_w: float, canvas_h: float,
                            img_w: float, img_h: float) -> tuple[int, int, int, int]:
        """Clipped integer source rect ``(left, top, right, bottom)``.

        May be empty (``right <= left``) when the image is entirely off-screen.
        """
        ix1, iy1 = self.to_image(0.0, 0.0)
        ix2, iy2 = self.to_image(canvas_w, canvas_h)
        left = max(0, min(int(ix1), int(img_w)))
        top = max(0, min(int(iy1), int(img_h)))
        right = max(0, min(int(ix2 + 0.999), int(img_w)))
        bottom = max(0, min(int(iy2 + 0.999), int(img_h)))
        return left, top, right, bottom


def clamp_view(view: View, img_w: float, img_h: float,
               canvas_w: float, canvas_h: float, keep: float = 0.35) -> View:
    """Keep at least a *keep* fraction of the image reachable on screen."""
    margin_x = max(40.0, canvas_w * keep)
    margin_y = max(40.0, canvas_h * keep)

    # Right edge of the image must stay right of -margin, left edge left of canvas+margin.
    min_ox = -img_w * view.zoom + margin_x
    max_ox = canvas_w - margin_x
    min_oy = -img_h * view.zoom + margin_y
    max_oy = canvas_h - margin_y
    ox = clamp(view.ox, min_ox, max_ox)
    oy = clamp(view.oy, min_oy, max_oy)
    return View(zoom=view.zoom, ox=ox, oy=oy)


def cursor_for_handle(handle: str | None) -> str:
    """Tk cursor name for a handle (or the crosshair for empty space)."""
    if handle is None:
        return "crosshair"
    return HANDLE_CURSORS.get(handle, "crosshair")
