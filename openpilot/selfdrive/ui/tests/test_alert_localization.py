"""Every user-visible alert string must have a Simplified Chinese translation.

The alert texts are produced server-side by `selfdrived` as English literals and
only translated on the rendering side, so a missing catalog entry silently
falls back to English. These tests pin the catalog down so a new alert cannot be
added without its translation, and so translations cannot drop the `%1`
placeholder or HTML tags used by the offroad alert renderer.
"""

import ast
import json
import re
from pathlib import Path

import pytest

from openpilot.common.basedir import BASEDIR
from openpilot.selfdrive.ui.translations.potools import parse_po
from openpilot.system.ui.lib.multilang import TRANSLATIONS_DIR

ZH_CHS_PO = TRANSLATIONS_DIR.joinpath('app_zh-CHS.po')
OFFROAD_ALERTS = Path(BASEDIR).joinpath('openpilot/selfdrive/selfdrived/alerts_offroad.json')

# Directories whose alert literals reach the user through tr().
ALERT_SOURCE_DIRS = (
  'openpilot/selfdrive/selfdrived',
  'openpilot/sunnypilot/selfdrive/selfdrived',
  'openpilot/selfdrive/ui',
  'openpilot/system/ui',
)
TAG_RE = re.compile(r'</?([a-zA-Z0-9]+)[^>]*>')


def _translations() -> dict[str, str]:
  _, entries = parse_po(ZH_CHS_PO)
  return {entry.msgid: entry.msgstr for entry in entries if not entry.is_plural and entry.msgid}


def _alert_literals() -> set[str]:
  """String literals passed to `Alert(...)` and its `*Alert` helpers."""
  literals: set[str] = set()
  for rel in ALERT_SOURCE_DIRS:
    for path in Path(BASEDIR).joinpath(rel).rglob('*.py'):
      if 'tests' in path.parts:
        # Test fixtures build placeholder alerts ("alert"), not user-facing text.
        continue
      tree = ast.parse(path.read_text(encoding='utf-8'))
      for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
          continue
        if node.func.id != 'Alert' and not node.func.id.endswith('Alert'):
          continue
        values = list(node.args) + [kw.value for kw in node.keywords]
        for value in values:
          if isinstance(value, ast.Constant) and isinstance(value.value, str) and value.value.strip():
            literals.add(value.value)
  return literals


def test_alert_literals_are_translated():
  translations = _translations()
  missing = sorted(text for text in _alert_literals() if not translations.get(text))
  assert not missing, f"alert strings without a zh-CHS translation: {missing}"


def test_offroad_alert_templates_keep_placeholder_and_tags():
  translations = _translations()
  alerts = json.loads(OFFROAD_ALERTS.read_text(encoding='utf-8'))

  for key, config in alerts.items():
    template = config.get('text', '')
    # A bare placeholder is filled at runtime by the caller.
    if not template or template == '%1':
      continue

    translated = translations.get(template)
    assert translated, f"{key}: no zh-CHS translation for {template!r}"
    assert translated.count('%1') == template.count('%1'), f"{key}: %1 placeholder lost in translation"
    assert sorted(TAG_RE.findall(translated)) == sorted(TAG_RE.findall(template)), (
      f"{key}: HTML tags changed in translation"
    )


@pytest.mark.parametrize('text1,text2,expected', [
  ('Take Control', 'Driver Distracted', ('请接管', '驾驶员分心')),
  ('Communication Issue Between Processes', '', ('进程间通信异常', '')),
  ('Press Resume to Exit Standstill', 'Slow down to engage', ('请按 Resume 退出起步驻车', '请减速后才能接管')),
  ('Stock ACC Cancellation Unconfirmed', 'Brake or Cancel Cruise Manually', ('原车 ACC 尚未确认退出', '请踩刹车或手动取消巡航')),
  ('Cancel Stock ACC to Re-engage', '', ('请先取消原车 ACC，再重新启用', '')),
])
def test_renderers_translate_alert_text(monkeypatch, text1, text2, expected):
  import importlib
  from types import SimpleNamespace

  from openpilot.cereal import messaging
  from openpilot.system.ui.lib.multilang import multilang

  monkeypatch.setattr(multilang, '_translations', _translations())

  for module_name in (
    'openpilot.selfdrive.ui.onroad.alert_renderer',
    'openpilot.selfdrive.ui.mici.onroad.alert_renderer',
  ):
    module = importlib.import_module(module_name)
    monkeypatch.setattr(module, 'ui_state', SimpleNamespace(started_frame=0))
    renderer = module.AlertRenderer.__new__(module.AlertRenderer)

    sm = messaging.SubMaster(['selfdriveState'])
    ss = messaging.new_message('selfdriveState').selfdriveState
    ss.alertText1, ss.alertText2 = text1, text2
    ss.alertSize, ss.alertStatus = 'mid', 'normal'
    ss.alertHudVisual = 'steerRequired'
    sm.data['selfdriveState'] = ss
    sm.updated['selfdriveState'] = True
    sm.recv_frame['selfdriveState'] = 1

    result = renderer.get_alert(sm)
    assert (result.text1, result.text2) == expected, module_name


@pytest.mark.parametrize('text,expected', [
  ('Drive above 40 km/h to engage', '行驶速度高于 40 km/h 才能接管'),
  ('Steer Assist Unavailable Below 35 km/h', '转向辅助在速度低于 35 km/h 时不可用'),
  ('Drive Above 40 km/h', '行驶速度需高于 40 km/h'),
  ('Speed Error: 1.3 m/s', '速度误差：1.3 米/秒'),
  ('Calibration Invalid: Turn camera left 4.2°', '标定无效：向左转动摄像头 4.2°'),
  ('Angle offset too high (Offset: 52.0°)', '角度偏差过大（偏差 52.0°）'),
  ('Check tires, pressure, or alignment (Factor: 1.8)', '请检查轮胎、胎压或四轮定位（系数 1.8）'),
])
def test_dynamic_alerts_are_localized(monkeypatch, text, expected):
  from openpilot.system.ui.lib.multilang import multilang
  from openpilot.selfdrive.ui.onroad.alert_text import localize_alert_text

  monkeypatch.setattr(multilang, '_translations', _translations())
  assert localize_alert_text(text) == expected


def test_dynamic_templates_come_from_the_catalog(monkeypatch):
  from openpilot.selfdrive.ui.onroad.alert_text import DYNAMIC_ALERT_PATTERNS
  from openpilot.system.ui.lib.multilang import load_translations, multilang, tr

  translations = _translations()
  monkeypatch.setattr(multilang, '_translations', load_translations(ZH_CHS_PO)[0])
  catalog_text = ZH_CHS_PO.read_text(encoding='utf-8')
  for _pattern, template in DYNAMIC_ALERT_PATTERNS:
    translated = translations.get(template)
    assert translated, template
    # Glyphs are only prepared for characters that appear inside the catalog.
    assert set(translated) <= set(catalog_text), template
    assert tr(template) == translated


def test_static_takeover_text_is_translated(monkeypatch):
  # Static alert texts are handled by tr() in the renderer, not by the
  # dynamic-pattern pass, so pin the catalog side here.
  translations = _translations()
  assert translations.get('TAKE CONTROL IMMEDIATELY') == '请立即接管控制'
