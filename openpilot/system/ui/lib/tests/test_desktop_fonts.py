from pathlib import Path

import pytest

pytest.importorskip("fontTools")
from fontTools.ttLib import TTFont
from fontTools.pens.recordingPen import RecordingPen

from openpilot.system.ui.lib.application import FONT_DIR, font_codepoints
from openpilot.system.ui.prepare_desktop_fonts import build_font


def test_controls_are_not_glyphs_but_variation_selectors_are():
  assert font_codepoints("A\n\r\t\u00a0\ufe0f") == [65, 160, 65039]


def test_missing_symbols_get_real_glyphs_and_keep_primary_metrics(tmp_path):
  primary = Path(str(FONT_DIR)) / "Inter-Regular.ttf"
  donor = Path(str(FONT_DIR)) / "unifont.otf"
  target = tmp_path / "complete.otf"
  chars = font_codepoints("A⚙✔中文")
  result = build_font(primary, [donor], chars, target)
  assert result['fallback_codepoints'] == [ord(c) for c in sorted("⚙✔中文")]
  with TTFont(primary) as before, TTFont(target) as after:
    assert set(after.getBestCmap()) == set(chars)
    assert after['hmtx'][after.getBestCmap()[65]] == before['hmtx'][before.getBestCmap()[65]]
    glyphs = after.getGlyphSet()
    for codepoint in chars:
      pen = RecordingPen()
      glyphs[after.getBestCmap()[codepoint]].draw(pen)
      assert pen.value, f"U+{codepoint:04X} has no outline"


def test_unavailable_glyph_fails_without_writing_a_replacement(tmp_path):
  primary = Path(str(FONT_DIR)) / "Inter-Regular.ttf"
  target = tmp_path / "incomplete.otf"
  with pytest.raises(ValueError, match="U\\+4E2D"):
    build_font(primary, [], [ord('中')], target)
  assert not target.exists()


def test_cff_primary_can_be_completed_from_a_truetype_donor(tmp_path):
  primary = Path(str(FONT_DIR)) / "unifont.otf"
  donor = Path('/usr/share/fonts/truetype/freefont/FreeSerif.ttf')
  if not donor.is_file():
    pytest.skip('Desktop FreeFont is not installed')
  target = tmp_path / "symbols.otf"
  build_font(primary, [donor], font_codepoints("中🔥\ufe0f"), target)
  with TTFont(target) as after:
    cmap = after.getBestCmap()
    assert set(cmap) == {ord('中'), ord('🔥'), 0xFE0F}
    assert after['hmtx'][cmap[0xFE0F]][0] == 0
