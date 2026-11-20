
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from openpilot.cereal import custom
from openpilot.selfdrive.ui.sunnypilot import model_info
from openpilot.selfdrive.ui.sunnypilot.layouts.settings import models as layout_mod


def _big_bundle():
  b = custom.ModelManagerSP.ModelBundle.new_message()
  b.index = 0
  b.internalName = "amd_driving_big_tinygrad.pkl"
  b.displayName = "big_driving_supercombo @f22b8b34 h16384 (779 MB)"
  b.ref = "local:igpu-big"
  b.environment = "local"
  b.runner = "tinygrad"
  b.is20hz = True
  b.generation = 0
  b.minimumSelectorVersion = 19
  b.status = "downloaded"
  return b


def _small_bundle():
  b = custom.ModelManagerSP.ModelBundle.new_message()
  b.index = 0
  b.internalName = "amd_tsfdom.pkl"
  b.displayName = "TSFDOM (113 MB, local)"
  b.ref = "local:igpu-small:amd_tsfdom.pkl"
  b.environment = "local"
  b.runner = "stock"
  b.is20hz = True
  b.generation = 0
  b.minimumSelectorVersion = 19
  b.status = "downloaded"
  return b


def test_big_entry_is_offered_without_the_env_switch(tmp_path):
  big = tmp_path / "amd_driving_big_tinygrad.pkl"
  big.write_bytes(b"big")
  with patch("openpilot.selfdrive.modeld.helpers.big_model_artifact", return_value=big):
    bundle = model_info.local_bundle()
  assert bundle is not None
  assert bundle.ref == "local:igpu-big"
  assert bundle.internalName == "amd_driving_big_tinygrad.pkl"


def test_selecting_the_big_entry_writes_the_big_slot():
  layout = layout_mod.ModelsLayout.__new__(layout_mod.ModelsLayout)
  layout._selection_source = "chestnut"
  bundle = _big_bundle()
  layout.model_dialog = SimpleNamespace(selection_ref="local:igpu-big")
  params = MagicMock()
  with patch.object(layout_mod, "bundles_for_source", side_effect=lambda s: [bundle] if s == "chestnut" else []), \
       patch.object(layout_mod.ui_state, "params", params):
    layout._on_model_selected(layout_mod.DialogResult.CONFIRM)
  params.put.assert_called_once_with("ModelManager_ActiveBundleChestnut", bundle.to_dict(), block=True)
  params.remove.assert_not_called()


def test_selecting_a_small_model_turns_the_big_model_off():
  layout = layout_mod.ModelsLayout.__new__(layout_mod.ModelsLayout)
  layout._selection_source = "qcom"
  small = _small_bundle()
  layout.model_dialog = SimpleNamespace(selection_ref=small.ref)
  params = MagicMock()
  with patch.object(layout_mod, "bundles_for_source", side_effect=lambda s: [small] if s == "qcom" else []), \
       patch.object(layout_mod, "get_selected_bundle", return_value=_big_bundle()), \
       patch.object(layout_mod.ui_state, "params", params):
    layout._on_model_selected(layout_mod.DialogResult.CONFIRM)
  params.put.assert_called_once_with("ModelManager_ActiveBundle", small.to_dict(), block=True)
  params.remove.assert_called_once_with("ModelManager_ActiveBundleChestnut")


def test_selecting_default_on_the_big_row_clears_it():
  layout = layout_mod.ModelsLayout.__new__(layout_mod.ModelsLayout)
  layout._selection_source = "chestnut"
  layout.model_dialog = SimpleNamespace(selection_ref="Default")
  params = MagicMock()
  with patch.object(layout_mod.ui_state, "params", params):
    layout._on_model_selected(layout_mod.DialogResult.CONFIRM)
  params.remove.assert_called_once_with("ModelManager_ActiveBundleChestnut")


def test_carrying_model_reports_the_big_pick():
  big = SimpleNamespace(name="amd_driving_big_tinygrad.pkl")
  with patch("openpilot.selfdrive.modeld.helpers.selected_big_pkl", return_value=big), \
       patch.object(model_info, "get_selected_bundle", return_value=_big_bundle()), \
       patch.object(model_info, "igpu_display_name", return_value="big_driving_supercombo"):
    source, name, display = model_info.carrying_model()
  assert source == "igpu"
  assert name == "amd_driving_big_tinygrad.pkl"
  assert display.startswith("big_driving_supercombo")


def test_big_row_default_means_small_model():
  with patch.object(model_info.ui_state, "chestnut_present", False):
    assert model_info.default_choice_label("chestnut") == "Small model (Default)"
