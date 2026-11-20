"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import pyray as rl

from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.system.ui.lib.application import (FONT_DIR, FONT_SCALE, LOCAL_CJK_FONT, FontWeight, font_codepoints,
                                                  gui_app)
from openpilot.system.ui.widgets import Widget


class RoadNameRenderer(Widget):
  def __init__(self):
    super().__init__()
    self.road_name = ""
    self.is_metric = False
    self.font_demi = gui_app.font(FontWeight.SEMI_BOLD)
    self._road_font = None
    self._road_font_key = None

  def _font_for_text(self, text: str):
    """Load the glyphs actually present in this OSM road name.

    The bundled Inter fonts have no CJK glyphs and the translation fallback
    atlas only contains characters used by the UI strings.  Road names are
    arbitrary OSM text, so build a small font atlas from the current name.
    """
    codepoints = font_codepoints(set(text) | set(map(chr, range(32, 127))) | {"…"})
    if LOCAL_CJK_FONT.is_file():
      path = LOCAL_CJK_FONT
    else:
      path = FONT_DIR / "NotoSansCJKsc-Regular.otf"
      if not path.is_file():
        path = FONT_DIR / "unifont.otf"
    key = (path.as_posix(), tuple(codepoints))
    if key == self._road_font_key and self._road_font is not None:
      return self._road_font
    if self._road_font is not None:
      rl.unload_font(self._road_font)
    buffer = rl.ffi.new("int[]", codepoints)
    self._road_font = rl.load_font_ex(path.as_posix(), 48, rl.ffi.cast("int *", buffer), len(codepoints))
    rl.set_texture_filter(self._road_font.texture, rl.TextureFilter.TEXTURE_FILTER_BILINEAR)
    self._road_font_key = key
    return self._road_font

  @staticmethod
  def _measure(font, text: str, size: int):
    # Deliberate: _draw() below bypasses gui_app's patched draw_text_ex and uses the
    # font that actually carries every road-name glyph, so the measurement has to
    # come from the same unpatched call or the two disagree.
    return rl.measure_text_ex(font, text, size * FONT_SCALE, 0)  # noqa: TID251

  @staticmethod
  def _draw(font, text: str, position, size: int, color):
    # gui_app patches rl.draw_text_ex with a translation-only fallback atlas;
    # this renderer already selected a font containing every road-name glyph.
    draw = getattr(rl, "_orig_draw_text_ex", rl.draw_text_ex)
    draw(font, text, position, size * FONT_SCALE, 0, color)

  def update(self):
    sm = ui_state.sm
    if sm.recv_frame["carState"] < ui_state.started_frame:
      return

    self.is_metric = ui_state.is_metric

    if sm.updated["liveMapDataSP"]:
      lmd = sm["liveMapDataSP"]
      self.road_name = lmd.roadName

  def _render(self, rect: rl.Rectangle):
    if not self.road_name or not ui_state.road_name_toggle:
      return

    text = self.road_name
    font = self._font_for_text(text)
    text_size = self._measure(font, text, 46)

    padding = 40
    rect_width = max(200, min(text_size.x + padding, rect.width - 40))

    road_rect = rl.Rectangle(rect.x + rect.width / 2 - rect_width / 2, rect.y - 4, rect_width, 60)

    rl.draw_rectangle_rounded(road_rect, 0.2, 10, rl.Color(0, 0, 0, 120))

    max_text_width = road_rect.width - 20
    if text_size.x > max_text_width:
      while text_size.x > max_text_width and len(text) > 3:
        text = text[:-1]
        text_size = self._measure(font, text + "...", 46)
      text = text + "..."

    sz = self._measure(font, text, 46)
    origin = rl.Vector2(road_rect.x + road_rect.width / 2 - sz.x / 2, road_rect.y + road_rect.height / 2 - sz.y / 2)
    self._draw(font, text, origin, 46, rl.Color(255, 255, 255, 200))
