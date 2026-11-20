"""When the WIDE CAM note belongs on the onroad screen, and which camera it draws."""

import pytest

from openpilot.selfdrive.ui.sunnypilot.onroad.wide_camera_indicator import (
  NARROW_ROAD_CAM, WIDE_CAM, indicator_visible, onroad_camera_stream)


class TestIndicatorVisibility:
  def test_hidden_while_the_screen_draws_the_main_camera(self):
    assert not indicator_visible(False, True)

  def test_shown_while_the_screen_draws_the_wide_camera(self):
    assert indicator_visible(True, True)

  @pytest.mark.parametrize("on_screen", [True, False])
  def test_never_shown_before_the_onroad_processes_start(self, on_screen):
    # Before startup there is no camera state to report, and the same widget is
    # reachable from the layouts that draw before going onroad.
    assert not indicator_visible(on_screen, False)

  def test_a_stale_state_cannot_survive_a_restart_by_itself(self):
    assert not indicator_visible(True, False)


class TestOnroadCameraStream:
  """Which camera the onroad screen draws.

  The picture is the main (narrow) camera. It used to follow modeld so the driver
  saw the camera the model was reading, but this build's wide camera is a fisheye
  that DEVICE_CAMERAS describes as a pinhole, so the model overlay was projected
  into visibly the wrong place. A wrong picture is worse than a picture of the
  other camera. It also must not show the wide camera just because the wide device
  happens to be the one behind the narrow index after a lens swap.
  """

  @pytest.mark.parametrize("wide_available", [True, False])
  def test_the_picture_is_the_main_camera_whenever_it_exists(self, wide_available):
    assert onroad_camera_stream(True, wide_available) == NARROW_ROAD_CAM

  def test_the_wide_camera_is_only_the_fallback_for_a_missing_main_stream(self):
    # No main camera: drawing nothing would be worse than drawing the wide one.
    assert onroad_camera_stream(False, True) == WIDE_CAM

  def test_no_stream_at_all_stays_on_the_main_id(self):
    # Nothing to show; do not claim a wide view that cannot exist.
    assert onroad_camera_stream(False, False) == NARROW_ROAD_CAM

  def test_a_running_wide_input_does_not_move_the_picture(self):
    # The regression this pins: the model reading the wide camera used to switch
    # the screen onto the fisheye. It must not any more, or the overlay goes wrong
    # again and the wide picture becomes the permanent view.
    assert onroad_camera_stream(True, True) == NARROW_ROAD_CAM

  def test_it_never_prefers_the_wide_stream_over_an_available_main(self):
    for narrow_available in (True, False):
      for wide_available in (True, False):
        stream = onroad_camera_stream(narrow_available, wide_available)
        if narrow_available:
          assert stream == NARROW_ROAD_CAM
        elif wide_available:
          assert stream == WIDE_CAM
        else:
          assert stream == NARROW_ROAD_CAM


class TestTheNoteFollowsTheDrawnPicture:
  """The note is a view label, not a model-input badge.

  The model reads the wide camera continuously in dual-camera mode; if the note
  followed that state it would be a permanent overlay on a screen that is showing
  the main camera. It follows the stream that is actually drawn instead.
  """

  def test_no_note_while_the_main_camera_is_the_picture(self):
    # Both streams exist and modeld feeds the wide one, but the screen shows main.
    assert onroad_camera_stream(True, True) == NARROW_ROAD_CAM
    assert not indicator_visible(onroad_camera_stream(True, True) == WIDE_CAM, True)

  def test_the_note_appears_only_with_the_wide_fallback_picture(self):
    stream = onroad_camera_stream(False, True)
    assert stream == WIDE_CAM
    assert indicator_visible(stream == WIDE_CAM, True)
