"""A multiple-button row must fit the panel no matter how many options it has.

The row used to be button_count * button_width wide unconditionally. The chevron
metrics setting has five options and the row had room for four, so the fifth was
never drawn - the setting looked like it had four choices - and it still owned an
invisible click region. Both halves of that bug start with the width math.
"""
from openpilot.system.ui.sunnypilot.widgets.list_view import fitted_button_width

# Measured: the visual settings panel is 1320 px wide and the button row gets 1280.
AVAILABLE = 1280.0


def test_a_row_that_already_fits_keeps_its_configured_width():
  assert fitted_button_width(300, AVAILABLE, 3) == 300


def test_a_row_with_too_many_buttons_shares_the_available_width():
  fitted = fitted_button_width(300, AVAILABLE, 5)
  assert fitted < 300
  assert abs(fitted * 5 - AVAILABLE) < 1e-6


def test_the_five_option_row_that_used_to_overflow_now_fits():
  assert 5 * fitted_button_width(300, AVAILABLE, 5) <= AVAILABLE + 1e-6


def test_every_row_fits_regardless_of_the_option_count():
  for count in range(1, 9):
    assert fitted_button_width(300, AVAILABLE, count) * count <= AVAILABLE + 1e-6


def test_a_degenerate_row_does_not_divide_by_zero():
  assert fitted_button_width(300, AVAILABLE, 0) == 300


def test_the_width_is_never_negative():
  assert fitted_button_width(300, 0, 5) == 0
