"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import pyray as rl

from openpilot.cereal.visionipc import VisionStreamType
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.system.ui.lib.application import gui_app, FontWeight
from openpilot.system.ui.lib.multilang import tr
from openpilot.system.ui.lib.text_measure import measure_text_cached
from openpilot.system.ui.widgets import Widget

TEXT = "WIDE CAM"
FONT_SIZE = 34
MARGIN_X = 30
MARGIN_Y = 24
LETTER_SPACING = 2.0
# Plain text on live road footage needs some separation or it disappears against a
# bright road. A shadow does that without a box behind it, which is what was asked
# for; a filled rectangle would also cover part of the model's own view.
SHADOW_OFFSET = 2.0
SHADOW_COLOR = rl.Color(0, 0, 0, 190)
TEXT_COLOR = rl.Color(0xff, 0xff, 0xff, 0xE0)

NARROW_ROAD_CAM = VisionStreamType.VISION_STREAM_NARROW_ROAD
WIDE_CAM = VisionStreamType.VISION_STREAM_WIDE_ROAD


def onroad_camera_stream(narrow_available: bool, wide_available: bool) -> VisionStreamType:
  """Which camera the onroad screen draws.

  The screen is the driver's picture of what the model is doing, so it stays on
  the narrow (main) camera: that is the lens the pinhole intrinsics, the
  calibration and the model overlay were built for. The wide camera is only a
  fallback for the one case where the narrow stream does not exist - a missing
  main camera must not leave the driver with a dead rectangle.

  This is also what keeps a swapped lens from turning into a permanently wide
  picture: after the lenses are exchanged the camera behind the narrow stream is
  still the one the UI draws, so the wide view cannot leak onto the road screen
  merely because its device happens to be enumerated first.
  """
  if narrow_available or not wide_available:
    return NARROW_ROAD_CAM
  return WIDE_CAM


def indicator_visible(wide_on_screen: bool, started: bool) -> bool:
  """Whether the WIDE CAM note belongs on screen.

  Strictly tied to the picture being drawn: the note tells the driver "you are
  looking through the wide camera", so it must not be a permanent badge while the
  screen shows the main camera. The model reads the wide camera continuously in
  dual-camera mode, but that is not a reason to label the main view - a constant
  note stops carrying information.

  `started` keeps it off before the onroad processes have started, where there is
  no camera state to report.
  """
  return bool(wide_on_screen and started)


class WideCameraIndicator(Widget):
  """Plain-text note that the onroad picture is the wide camera right now.

  The state is published by the road view itself (`ui_state.onroad_wide_camera`),
  which is the only place that knows which VIPC stream is being drawn. This widget
  never decides on its own; it mirrors the drawn stream.
  """

  def __init__(self):
    super().__init__()
    self._font = gui_app.font(FontWeight.SEMI_BOLD)

  def _render(self, rect: rl.Rectangle):
    if not indicator_visible(ui_state.onroad_wide_camera, ui_state.started):
      return

    pos = rl.Vector2(rect.x + MARGIN_X, rect.y + MARGIN_Y)

    # Shadow first, then the text on top, so the text stays legible over both dark
    # and light footage without a background panel.
    label = tr(TEXT)
    rl.draw_text_ex(self._font, label, rl.Vector2(pos.x + SHADOW_OFFSET, pos.y + SHADOW_OFFSET),
                    FONT_SIZE, LETTER_SPACING, SHADOW_COLOR)
    rl.draw_text_ex(self._font, label, pos, FONT_SIZE, LETTER_SPACING, TEXT_COLOR)

  def width(self, text: str = TEXT) -> float:
    """Text width at the rendered size, exposed so a caller can place this beside
    something else instead of guessing."""
    return measure_text_cached(self._font, text, FONT_SIZE).x
