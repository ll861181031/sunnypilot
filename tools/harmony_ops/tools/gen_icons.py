#!/usr/bin/env python3
"""Generate the launcher and start-window icons for the harmony_ops app.

Why a generator instead of committed binaries: the icons are the only binary
assets in the project, and a generator keeps them reproducible and reviewable in
a repo whose other tooling is text-only. Run it from anywhere:

    /data/sunnypilot/.venv/bin/python tools/harmony_ops/tools/gen_icons.py

with a filled accent bar and a status dot each, on a dark rounded square.

Sizing note: HarmonyOS launchers mask the icon themselves, and the platform
guidance for the app icon is a 1024x1024 PNG. Content is therefore kept inside
the central 62% so no launcher mask clips it.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw

# Supersample factor: geometry is drawn this many times oversampled and then
# downsampled, which is what gives the pills and dots clean edges without
# depending on a font or an SVG rasteriser.
SS = 4

# The three rows, in the app's tab order. Colours are picked to stay legible on
# the dark background and to differ enough to read at 48 px.
ROWS = (
    ("#2DD4BF", 0.62),
    ("#FBBF24", 0.45),
    ("#818CF8", 0.78),  # indigo - DSH
)

BG_TOP = (11, 15, 20)
BG_BOTTOM = (22, 28, 38)


def _vertical_gradient(size: int) -> Image.Image:
  img = Image.new("RGB", (1, size))
  px = img.load()
  for y in range(size):
    t = y / max(size - 1, 1)
    px[0, y] = tuple(round(a + (b - a) * t) for a, b in zip(BG_TOP, BG_BOTTOM))
  return img.resize((size, size), Image.NEAREST)


def render(size: int) -> Image.Image:
  """Render the icon at `size` px, returning an opaque RGB image."""
  w = size * SS
  img = _vertical_gradient(w).convert("RGBA")
  d = ImageDraw.Draw(img)

  # Content safe box: central 62%, so launcher masks (circle, squircle, rounded
  # square) never clip the rows.
  box = w * 0.62
  x0 = (w - box) / 2
  x1 = x0 + box

  gap = box * 0.095
  row_h = (box - 2 * gap) / 3
  radius = row_h / 2
  dot_r = row_h * 0.17
  dot_inset = row_h * 0.30

  for i, (colour, fill_ratio) in enumerate(ROWS):
    top = x0 + (0 if i == 0 else i * (row_h + gap))
    # The pill itself only hints at the row; the accent fill carries the meaning.
    d.rounded_rectangle((x0, top, x1, top + row_h),
                        radius=radius, fill=(255, 255, 255, 18))
    # Accent fill from the left edge, e.g. a utilisation bar.
    fill_w = box * fill_ratio
    if fill_w > radius * 2:
      d.rounded_rectangle((x0, top, x0 + fill_w, top + row_h), radius=radius, fill=colour)
    # Status dot pinned to the right end, inside the pill.
    cx = x1 - dot_inset - dot_r
    cy = top + row_h / 2
    d.ellipse((cx - dot_r, cy - dot_r, cx + dot_r, cy + dot_r), fill=colour)

  return img.convert("RGB").resize((size, size), Image.LANCZOS)


def main() -> None:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--project-root", type=Path,
                      default=Path(__file__).resolve().parents[1],
                      help="harmony_ops project root (default: the generator's parent dir)")
  parser.add_argument("--app-icon-size", type=int, default=1024,
                      help="launcher icon size in px (platform guidance: 1024)")
  parser.add_argument("--start-icon-size", type=int, default=512,
                      help="startWindowIcon size in px")
  args = parser.parse_args()

  root = args.project_root
  targets = (
    (root / "AppScope/resources/base/media/app_icon.png", args.app_icon_size),
    (root / "entry/src/main/resources/base/media/startIcon.png", args.start_icon_size),
    (root / "entry/src/main/resources/base/media/foreground.png", args.start_icon_size),
  )

  for path, size in targets:
    path.parent.mkdir(parents=True, exist_ok=True)
    render(size).save(path, format="PNG", optimize=True)
    print(f"wrote {path} ({size}x{size}, {path.stat().st_size} bytes)")


if __name__ == "__main__":
  main()
