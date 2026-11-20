import pyray as rl
import pytest

from openpilot.common.params import Params
from openpilot.system.ui.lib.application import gui_app
from openpilot.system.ui.sunnypilot.widgets.list_view import multiple_button_item_sp, option_item_sp, toggle_item_sp
from openpilot.system.ui.widgets.scroller_tici import Scroller


@pytest.mark.parametrize(("key", "make_row", "read", "stored", "displayed", "default"), [
  ("DisableEncoderd", lambda cb: toggle_item_sp("", param="DisableEncoderd", callback=cb),
   lambda action: action.get_state(), True, True, False),
  ("DeviceBootMode", lambda cb: multiple_button_item_sp("", "", ["0", "1"], param="DeviceBootMode", callback=cb),
   lambda action: action.get_selected_button(), 1, 1, 0),
  ("CameraOffset", lambda cb: option_item_sp("", "CameraOffset", -50, 50, use_float_scaling=True, on_value_changed=cb),
   lambda action: action.get_value(), 0.29, 29, 0),
  ("MaxTimeOffroad", lambda cb: option_item_sp("", "MaxTimeOffroad", 0, 1, value_map={0: 1800, 1: 60}, on_value_changed=cb),
   lambda action: action.get_value(), 60, 1, 0),
])
def test_reopen_reads_external_change_without_writing(tmp_path, monkeypatch, key, make_row, read, stored, displayed, default):
  monkeypatch.setenv("PARAMS_ROOT", str(tmp_path))
  monkeypatch.setattr(gui_app, "font", lambda _: rl.Font())
  remote = Params()
  row = make_row(lambda *_: pytest.fail("刷新不应触发用户回调"))
  page = Scroller([row])
  assert read(row.action_item) == default

  def reopen():
    with monkeypatch.context() as patch:
      def no_write(*args, **kwargs):
        pytest.fail("刷新不应写回参数")
      patch.setattr(Params, "put", no_write)
      patch.setattr(Params, "put_bool", no_write)
      page.show_event()

  page.hide_event()
  remote.put(key, stored, block=True)
  reopen()
  assert read(row.action_item) == displayed

  page.hide_event()
  remote.remove(key)
  reopen()
  assert read(row.action_item) == default


def test_click_outside_options_does_not_overwrite_external_selection(tmp_path, monkeypatch):
  monkeypatch.setenv("PARAMS_ROOT", str(tmp_path))
  monkeypatch.setattr(gui_app, "font", lambda _: rl.Font())
  row = multiple_button_item_sp("", "", ["0", "1"], param="DeviceBootMode")
  action = row.action_item
  action.set_rect(rl.Rectangle(0, 0, 300, 150))
  remote = Params()
  remote.put("DeviceBootMode", 1, block=True)
  monkeypatch.setattr(action.params, "put", lambda *args, **kwargs: pytest.fail("空白处不应写回选项"))
  action._handle_mouse_release(rl.Vector2(450, 75))
  assert remote.get("DeviceBootMode") == 1
