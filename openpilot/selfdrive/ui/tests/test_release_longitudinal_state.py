from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from opendbc.car.structs import car
from openpilot.common.params import Params
from openpilot.selfdrive.ui import ui_state as ui_module


@pytest.mark.parametrize("release,sp_release", [(False, False), (True, False), (False, True), (True, True)])
@pytest.mark.parametrize("alpha_enabled", [False, True])
def test_release_state_blocks_cached_alpha_longitudinal(monkeypatch, release, sp_release, alpha_enabled):
  cp = car.CarParams.new_message(brand="byd", carFingerprint="BYD_SONG_PLUS_DMI_23_ALT",
                                 alphaLongitudinalAvailable=True, openpilotLongitudinalControl=True)
  flags = {"IsReleaseBranch": release, "IsReleaseSpBranch": sp_release, "AlphaLongitudinalEnabled": alpha_enabled}
  params = Mock()
  params.get_bool.side_effect = lambda key: flags.get(key, False)
  params.get.side_effect = lambda key: cp.to_bytes() if key == "CarParamsPersistent" else None
  state = SimpleNamespace(params=params, started=False, chestnut_compiled=True, usb_connected=False, is_release=False)
  monkeypatch.setattr(ui_module, "read_int", lambda path: 0)
  monkeypatch.setattr(ui_module.UIStateSP, "update_params", lambda self: None)

  ui_module.UIState.update_params(state)

  assert state.is_release is (release or sp_release)
  assert state.has_longitudinal_control is (alpha_enabled and not (release or sp_release))


@pytest.mark.parametrize("available,release,saved,expected", [
  (None, False, True, True),
  (None, False, False, False),
  (None, False, None, None),
  (True, False, True, True),
  (False, False, True, None),
  (None, True, True, None),
  (True, True, True, None),
])
def test_startup_preserves_longitudinal_choice_until_capabilities_known(tmp_path, available, release, saved, expected):
  params = Params(str(tmp_path))
  if saved is not None:
    params.put_bool("AlphaLongitudinalEnabled", saved, block=True)
  cp = None if available is None else car.CarParams.new_message(alphaLongitudinalAvailable=available)
  state = SimpleNamespace(params=params, CP=cp, CP_SP=None, has_longitudinal_control=False, is_release=release)

  ui_module.UIStateSP._enforce_constraints(state)

  assert params.get("AlphaLongitudinalEnabled") is expected


@pytest.mark.parametrize("cp_known,sp_known", [(False, False), (True, False), (False, True), (True, True)])
def test_wait_for_car_capabilities_before_clearing_settings(tmp_path, cp_known, sp_known):
  p = Params(str(tmp_path))
  cp = car.CarParams.new_message(steerControlType="angle") if cp_known else None
  sp = SimpleNamespace(intelligentCruiseButtonManagementAvailable=False) if sp_known else None
  state = SimpleNamespace(params=p, CP=cp, CP_SP=sp, is_release=False, has_longitudinal_control=False, has_icbm=False)
  groups = {
    "EnforceTorqueControl": cp_known, "LateralJerkTorqueController": cp_known,
    "ExperimentalMode": cp_known, "DynamicExperimentalControl": cp_known,
    "IntelligentCruiseButtonManagement": sp_known,
    "CustomAccIncrementsEnabled": cp_known and sp_known, "SmartCruiseControlVision": cp_known and sp_known,
    "SmartCruiseControlMap": cp_known and sp_known,
  }
  for key in groups:
    p.put_bool(key, True, block=True)
  ui_module.UIStateSP._enforce_constraints(state)
  assert {key: p.get(key) for key in groups} == {key: None if clear else True for key, clear in groups.items()}


def test_bsm_choice_survives_opening_lane_change_page_before_identification(tmp_path, monkeypatch):
  from openpilot.selfdrive.ui.sunnypilot.layouts.settings.steering_sub_layouts import lane_change_settings as page
  p = Params(str(tmp_path))
  p.put_bool("AutoLaneChangeBsmDelay", True, block=True)
  monkeypatch.setattr(page, "ui_state", SimpleNamespace(CP=None, params=p))
  control = Mock()
  page.LaneChangeSettingsLayout._update_toggles(SimpleNamespace(_bsm_delay=control))
  control.action_item.set_enabled.assert_called_once_with(False)
  assert p.get_bool("AutoLaneChangeBsmDelay")


@pytest.mark.parametrize("stored,supported", [(None, True), (0, False), (1, True), (2, True)])
def test_real_runner_setting_controls_blendful_availability(tmp_path, stored, supported):
  state = object.__new__(ui_module.UIStateSP)
  state.params = Params(str(tmp_path))
  state.CP = state.CP_SP = None
  state.is_release = state.has_longitudinal_control = state.has_icbm = False
  state.chestnut_present = state.chestnut_active = state.chestnut_loading = state.chestnut_compiled = False
  state.is_offroad = lambda: True
  state._sp_initialized = True
  if stored is not None:
    state.params.put("ModelRunnerTypeCache", stored, block=True)
  state.update_params()
  assert state.blendful_supported() is supported


@pytest.mark.parametrize("release", [False, True])
def test_no_output_fallback_is_not_evidence_against_saved_preferences(tmp_path, release):
  p = Params(str(tmp_path))
  for key in ("AlphaLongitudinalEnabled", "AutoLaneChangeBsmDelay", "ExperimentalMode", "SmartCruiseControlMap"):
    p.put_bool(key, True, block=True)
  state = SimpleNamespace(params=p, CP=car.CarParams.new_message(brand="mock", dashcamOnly=True),
                          CP_SP=SimpleNamespace(intelligentCruiseButtonManagementAvailable=False),
                          is_release=release, has_icbm=False, has_longitudinal_control=False)
  ui_module.UIStateSP._enforce_constraints(state)
  assert p.get_bool("AlphaLongitudinalEnabled") is not release
  assert all(p.get_bool(key) for key in ("AutoLaneChangeBsmDelay", "ExperimentalMode", "SmartCruiseControlMap"))
