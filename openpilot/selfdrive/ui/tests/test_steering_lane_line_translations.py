"""The lane-line settings added to the Steering panel must be Chinese in both
Simplified and Traditional catalogs, not just present as English fallbacks."""
import pytest

from openpilot.system.ui.lib.multilang import Multilang

KEYS = (
  "Lane Line Type Detection",
  "Classify the ego-lane boundaries as solid or dashed with an on-device model.",
  "Lane Change Line Check",
  "0: off, 1: block solid/unknown, 2: solid/unknown need torque.",
  "Block solid",
  "Torque override",
)


@pytest.mark.parametrize("language", ["zh-CHS", "zh-CHT"])
def test_lane_line_settings_have_chinese_translations(language):
  multilang = Multilang()
  multilang._language = language
  multilang.setup()
  for key in KEYS:
    translated = multilang.tr(key)
    assert translated != key, f"{language} is missing a translation for {key!r}"
    assert any("\u4e00" <= char <= "\u9fff" for char in translated), f"{language} translation is not Chinese: {key!r}"
