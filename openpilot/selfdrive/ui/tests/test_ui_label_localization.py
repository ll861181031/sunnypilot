"""Widget labels must be translated at render time and exist in the zh-CHS catalog.

The raylib widgets draw their text verbatim, so an English literal passed
straight to `Label`/`UnifiedLabel`/`Button` shows up untranslated no matter how
complete the catalog is. These tests keep the wrapped literals honest: any new
label has to go through `tr()` and have a Simplified Chinese entry.
"""

import ast
from pathlib import Path

from openpilot.common.basedir import BASEDIR
from openpilot.selfdrive.ui.translations.potools import parse_po
from openpilot.system.ui.lib.multilang import TRANSLATIONS_DIR

ZH_CHS_PO = TRANSLATIONS_DIR.joinpath('app_zh-CHS.po')
UI_ROOTS = ('openpilot/selfdrive/ui', 'openpilot/system/ui')
LABEL_WIDGETS = {'Label', 'UnifiedLabel', 'Button', 'PillButton', 'GreyBigButton',
                 'BigPillButton', 'NavButton', 'HtmlRenderer', 'TextInput'}
# Product and format names stay as-is on purpose.
KEEP_AS_IS = {'openpilot', 'sunnypilot', 'comma', 'comma.ai'}


def _translations() -> dict[str, str]:
  _, entries = parse_po(ZH_CHS_PO)
  return {entry.msgid: entry.msgstr for entry in entries if not entry.is_plural and entry.msgid}


def _label_literals() -> dict[str, list[str]]:
  literals: dict[str, list[str]] = {}
  for rel in UI_ROOTS:
    for path in Path(BASEDIR).joinpath(rel).rglob('*.py'):
      if 'tests' in path.parts:
        continue
      tree = ast.parse(path.read_text(encoding='utf-8'))
      for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
          continue
        if node.func.id not in LABEL_WIDGETS or not node.args:
          continue
        first = node.args[0]
        # tr("...") wrapping is already handled; only flag raw constants.
        if isinstance(first, ast.Call) and isinstance(first.func, ast.Name) and first.func.id == 'tr':
          continue
        if isinstance(first, ast.Constant) and isinstance(first.value, str) and first.value.strip():
          literals.setdefault(first.value, []).append(path.name)
  return literals


def test_widget_labels_have_chinese_translations():
  translations = _translations()
  missing = {
    text: files for text, files in _label_literals().items()
    if text not in KEEP_AS_IS and not translations.get(text)
  }
  assert not missing, f"widget labels without a zh-CHS translation: {missing}"


def test_tr_wrapped_labels_keep_their_source_key():
  """A wrapped literal must still exist verbatim as a catalog key."""
  translations = _translations()
  bad: list[str] = []
  for rel in UI_ROOTS:
    for path in Path(BASEDIR).joinpath(rel).rglob('*.py'):
      if 'tests' in path.parts:
        continue
      tree = ast.parse(path.read_text(encoding='utf-8'))
      for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or node.func.id != 'tr':
          continue
        if not node.args:
          continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str) and first.value.strip():
          if not translations.get(first.value):
            bad.append(f"{path.name}: {first.value!r}")
  assert not bad, f"tr() literals missing from the catalog: {sorted(set(bad))}"
