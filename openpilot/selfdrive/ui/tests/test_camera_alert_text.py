import importlib
from types import SimpleNamespace

import pytest

from openpilot.cereal import log, messaging
from openpilot.common.prefix import OpenpilotPrefix
from openpilot.selfdrive.ui.onroad.alert_text import (MOUNT_PART_TEMPLATES, camera_alert_text,
                                                     localize_alert_text, mount_alert_text)
from openpilot.system.ui.lib.multilang import multilang, load_translations, TRANSLATIONS_DIR


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
  with OpenpilotPrefix():
    monkeypatch.setenv('SUNNYPILOT_DISABLE_DRIVER_CAMERA', '1')
    monkeypatch.setattr(multilang, '_translations', {})
    yield


def test_intentionally_disabled_cabin_notice():
  assert camera_alert_text('cameraMalfunction/permanent', 'Camera Malfunction', 'cabinCamera') == (
    'Driver Camera Disabled', 'Driver monitoring unavailable; assistance disabled',
  )


def test_chinese_notice(monkeypatch):
  translations, _ = load_translations(TRANSLATIONS_DIR.joinpath('app_zh-CHS.po'))
  monkeypatch.setattr(multilang, '_translations', translations)
  assert camera_alert_text('cameraMalfunction/permanent', 'Camera Malfunction', 'cabinCamera') == (
    '车内摄像头已禁用', '驾驶员监测不可用，无法启用辅助驾驶',
  )


@pytest.mark.parametrize('disabled', [None, '0'])
def test_enabled_or_default_cabin_fault_is_unchanged(monkeypatch, disabled):
  if disabled is None:
    monkeypatch.delenv('SUNNYPILOT_DISABLE_DRIVER_CAMERA')
  else:
    monkeypatch.setenv('SUNNYPILOT_DISABLE_DRIVER_CAMERA', disabled)
  assert camera_alert_text('cameraMalfunction/permanent', 'Camera Malfunction', 'cabinCamera') == (
    'Camera Malfunction', 'cabinCamera',
  )


@pytest.mark.parametrize('text2', [
  'narrowRoadCamera', 'wideRoadCamera', 'narrowRoadCamera, cabinCamera',
  'cabinCamera, wideRoadCamera', 'narrowRoadCamera, cabinCamera, wideRoadCamera', '',
])
def test_road_mixed_and_unspecified_faults_are_unchanged(text2):
  assert camera_alert_text('cameraMalfunction/permanent', 'Camera Malfunction', text2) == ('Camera Malfunction', text2)


@pytest.mark.parametrize('alert_type,text1,text2', [
  ('cameraMalfunction/noEntry', 'sunnypilot Unavailable', 'Camera Malfunction: Reboot Your Device'),
  ('cameraMalfunction/softDisable', 'TAKE CONTROL IMMEDIATELY', 'Camera Malfunction'),
  ('cameraFrameRate/permanent', 'Camera Frame Rate Low', 'Reboot your Device'),
  ('other/permanent', 'Camera Malfunction', 'cabinCamera'),
  ('cameraMalfunction/permanent', 'Different fault', 'cabinCamera'),
])
def test_other_alerts_and_takeover_instructions_are_unchanged(alert_type, text1, text2):
  assert camera_alert_text(alert_type, text1, text2) == (text1, text2)


@pytest.mark.parametrize('module_name', [
  'openpilot.selfdrive.ui.onroad.alert_renderer',
  'openpilot.selfdrive.ui.mici.onroad.alert_renderer',
])
@pytest.mark.parametrize('text2', ['cabinCamera', 'narrowRoadCamera, cabinCamera'])
def test_renderers_preserve_alert_properties_and_source(monkeypatch, module_name, text2):
  module = importlib.import_module(module_name)
  monkeypatch.setattr(module, 'ui_state', SimpleNamespace(started_frame=0))
  renderer = module.AlertRenderer.__new__(module.AlertRenderer)
  sm = messaging.SubMaster(['selfdriveState'])
  ss = messaging.new_message('selfdriveState').selfdriveState
  ss.alertType = 'cameraMalfunction/permanent'
  ss.alertText1, ss.alertText2 = 'Camera Malfunction', text2
  ss.alertSize, ss.alertStatus = 'mid', 'normal'
  ss.alertHudVisual = 'steerRequired'
  sm.data['selfdriveState'] = ss
  sm.updated['selfdriveState'] = True
  sm.recv_frame['selfdriveState'] = 1
  before = ss.to_dict()

  result = renderer.get_alert(sm)
  assert (result.text1, result.text2) == camera_alert_text(ss.alertType, ss.alertText1, ss.alertText2)
  assert result.size == ss.alertSize.raw
  assert result.status == ss.alertStatus.raw
  if hasattr(result, 'alert_type'):
    assert result.alert_type == ss.alertType
    assert result.visual_alert == ss.alertHudVisual
  assert ss.to_dict() == before


def test_camera_failure_still_blocks_engagement_and_disables_controls():
  from openpilot.common.realtime import DT_CTRL
  from openpilot.selfdrive.selfdrived.events import Events, EVENTS, ET, ImmediateDisableAlert
  from openpilot.selfdrive.selfdrived.state import StateMachine, SOFT_DISABLE_TIME

  event = log.OnroadEvent.EventName.cameraMalfunction
  assert set(EVENTS[event]) == {ET.PERMANENT, ET.NO_ENTRY, ET.SOFT_DISABLE}
  events = Events()
  events.add(event)
  events.add(log.OnroadEvent.EventName.buttonEnable)
  state = StateMachine()
  assert state.update(events) == (False, False)
  assert ET.NO_ENTRY in state.current_alert_types

  events.clear()
  events.add(event)
  state.state = log.SelfdriveState.OpenpilotState.enabled
  state.update(events)
  assert state.state == log.SelfdriveState.OpenpilotState.softDisabling
  for _ in range(int(SOFT_DISABLE_TIME / DT_CTRL)):
    state.update(events)
  assert state.state == log.SelfdriveState.OpenpilotState.disabled
  immediate_alert = EVENTS[event][ET.SOFT_DISABLE](None, None, None, False, 0, None)
  assert isinstance(immediate_alert, ImmediateDisableAlert)


def test_mount_guidance_is_rebuilt_from_translated_templates(monkeypatch):
  translations, _ = load_translations(TRANSLATIONS_DIR.joinpath('app_zh-CHS.po'))
  monkeypatch.setattr(multilang, '_translations', translations)

  assert mount_alert_text("Turn camera left 4.2°, tilt up 6.7°") == "向左转动摄像头 4.2°，向上抬 6.7°"
  assert mount_alert_text("Turn camera right 0.5°") == "向右转动摄像头 0.5°"
  assert mount_alert_text("tilt down 3.0°") == "向下压 3.0°"


@pytest.mark.parametrize('text', [
  "Remount Device (Pitch: 0.0°, Yaw: 0.0°)",
  "Calibration Invalid",
  "",
  "向左转动摄像头 4.2°",
])
def test_mount_guidance_leaves_other_text_alone(monkeypatch, text):
  translations, _ = load_translations(TRANSLATIONS_DIR.joinpath('app_zh-CHS.po'))
  monkeypatch.setattr(multilang, '_translations', translations)
  assert mount_alert_text(text) == text


def test_mount_templates_come_from_the_catalog(monkeypatch):
  # The aiming guide and the alert use the catalog for the wording: the font
  # preparation step only prepares glyphs found inside the .po files, so Chinese
  # hardcoded in the source would render as '?' on screen.
  translations, _ = load_translations(TRANSLATIONS_DIR.joinpath('app_zh-CHS.po'))
  monkeypatch.setattr(multilang, '_translations', translations)
  from openpilot.system.ui.lib.multilang import tr

  catalog_text = TRANSLATIONS_DIR.joinpath('app_zh-CHS.po').read_text(encoding='utf-8')
  for template in MOUNT_PART_TEMPLATES.values():
    translated = tr(template)
    assert translated != template, template
    assert set(translated) <= set(catalog_text), template


def test_mount_guidance_is_untouched_without_translations():
  # The autouse fixture clears the catalog, i.e. the English device language:
  # the sentence has to come back exactly as selfdrived sent it.
  assert mount_alert_text("Turn camera left 4.2°, tilt up 6.7°") == "Turn camera left 4.2°, tilt up 6.7°"
  assert localize_alert_text("Calibration Invalid: Turn camera left 4.2°") == "Calibration Invalid: Turn camera left 4.2°"
  assert localize_alert_text("Drive above 40 km/h to engage") == "Drive above 40 km/h to engage"
