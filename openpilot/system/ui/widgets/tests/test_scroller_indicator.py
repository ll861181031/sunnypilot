"""The scroll indicator's geometry, tested without a GL context.

The settings pages are plain scrollable lists and the scroll panel draws no
affordance of its own, so a page whose last row sat below the fold looked
identical to a page that simply ended. The bar is the only thing telling the
driver there is more to see, which makes its geometry worth a test: a thumb
parked off screen or a track one pixel wide would be silently useless.
"""
import pyray as rl

from openpilot.system.ui.widgets.scroller_tici import (
  SCROLLBAR_MIN_THUMB, SCROLLBAR_WIDTH, scroll_indicator_rects,
)

VIEWPORT = rl.Rectangle(100, 50, 900, 600)


def test_content_that_fits_draws_no_indicator():
  assert scroll_indicator_rects(VIEWPORT, content_height=400, scroll=0.0) is None


def test_content_exactly_filling_the_viewport_draws_no_indicator():
  assert scroll_indicator_rects(VIEWPORT, content_height=VIEWPORT.height, scroll=0.0) is None


def test_content_one_pixel_taller_draws_one():
  # The tolerance is one pixel; anything more would hide a real scrollable row.
  assert scroll_indicator_rects(VIEWPORT, content_height=VIEWPORT.height + 2, scroll=0.0) is not None


def test_the_track_spans_the_viewport_and_the_bar_sits_at_the_right_edge():
  track, thumb = scroll_indicator_rects(VIEWPORT, content_height=1200, scroll=0.0)
  assert track.x == thumb.x
  assert track.y == VIEWPORT.y
  assert track.height == VIEWPORT.height
  assert track.width == SCROLLBAR_WIDTH
  assert track.x + track.width <= VIEWPORT.x + VIEWPORT.width


def test_the_thumb_shrinks_as_the_content_grows():
  _, short = scroll_indicator_rects(VIEWPORT, content_height=1200, scroll=0.0)
  _, tall = scroll_indicator_rects(VIEWPORT, content_height=4800, scroll=0.0)
  assert tall.height < short.height


def test_the_thumb_never_disappears_into_nothing():
  _, thumb = scroll_indicator_rects(VIEWPORT, content_height=VIEWPORT.height * 1000, scroll=0.0)
  assert thumb.height >= SCROLLBAR_MIN_THUMB


def test_the_thumb_sits_at_the_top_before_any_scrolling():
  track, thumb = scroll_indicator_rects(VIEWPORT, content_height=1200, scroll=0.0)
  assert thumb.y == track.y


def test_the_thumb_reaches_the_bottom_at_the_end_of_the_content():
  content = 1200.0
  track, thumb = scroll_indicator_rects(VIEWPORT, content_height=content,
                                        scroll=-(content - VIEWPORT.height))
  assert abs((thumb.y + thumb.height) - (track.y + track.height)) < 1e-3


def test_the_thumb_stays_inside_the_track_when_over_scrolled():
  # Overscroll happens for a frame while a drag bounces back.
  content = 1200.0
  top_track, top_thumb = scroll_indicator_rects(VIEWPORT, content_height=content, scroll=200.0)
  assert top_thumb.y >= top_track.y
  end_track, end_thumb = scroll_indicator_rects(VIEWPORT, content_height=content, scroll=-9999.0)
  assert end_thumb.y + end_thumb.height <= end_track.y + end_track.height + 1e-3


def test_a_degenerate_viewport_is_ignored():
  assert scroll_indicator_rects(rl.Rectangle(0, 0, 900, 0), content_height=1200, scroll=0.0) is None
