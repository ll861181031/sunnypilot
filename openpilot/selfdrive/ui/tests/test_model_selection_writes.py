
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from openpilot.cereal import custom
from openpilot.selfdrive.ui.sunnypilot.layouts.settings import models as layout_mod


def _bundle(ref, name="amd_tsfdom.pkl"):
  b = custom.ModelManagerSP.ModelBundle.new_message()
  b.index = 0
  b.internalName = name
  b.displayName = "TSFDOM (100 MB, local)"
  b.ref = ref
  b.environment = "local"
  b.runner = "stock"
  b.is20hz = True
  b.generation = 0
  b.minimumSelectorVersion = 19
  b.status = "downloaded"
  return b


def _layout(source="qcom"):
  layout = layout_mod.ModelsLayout.__new__(layout_mod.ModelsLayout)
  layout._selection_source = source
  return layout


def test_selecting_a_local_model_writes_the_slot():
  ref = "local:igpu-small:amd_tsfdom.pkl"
  bundle = _bundle(ref)
  layout = _layout()
  layout.model_dialog = SimpleNamespace(selection_ref=ref)
  params = MagicMock()
  with patch.object(layout_mod, "bundles_for_source", side_effect=lambda s: [bundle] if s == "qcom" else []), \
       patch.object(layout_mod.ui_state, "params", params):
    layout._on_model_selected(layout_mod.DialogResult.CONFIRM)
  params.put.assert_called_once_with("ModelManager_ActiveBundle", bundle.to_dict(), block=True)
  assert not any(call.args and call.args[0] == "OnroadCycleRequested" for call in params.put_bool.call_args_list)


def test_default_removes_the_pick():
  layout = _layout()
  layout.model_dialog = SimpleNamespace(selection_ref="Default")
  params = MagicMock()
  with patch.object(layout_mod.ui_state, "params", params):
    layout._on_model_selected(layout_mod.DialogResult.CONFIRM)
  params.remove.assert_called_once_with("ModelManager_ActiveBundle")


def test_cancelling_writes_nothing():
  layout = _layout()
  layout.model_dialog = SimpleNamespace(selection_ref="local:igpu-small:amd_tsfdom.pkl")
  params = MagicMock()
  with patch.object(layout_mod.ui_state, "params", params):
    layout._on_model_selected(layout_mod.DialogResult.CANCEL)
  params.put.assert_not_called()
  params.remove.assert_not_called()
