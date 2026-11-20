
import ast
from pathlib import Path
from unittest.mock import patch

from openpilot.selfdrive.ui.layouts.settings import developer as base_dev
from openpilot.selfdrive.ui.sunnypilot.layouts.settings import developer as sp_dev
from openpilot.selfdrive.ui.mici.layouts.settings import developer as mici_dev


def test_adb_ssh_controls_are_disabled_on_the_desktop():
  with patch.object(base_dev, "PC", True):
    assert base_dev.adb_ssh_controls_enabled() is False
  with patch.object(base_dev, "PC", False):
    assert base_dev.adb_ssh_controls_enabled() is True


def test_the_page_explains_why_adb_ssh_do_nothing_here():
  src = Path(base_dev.__file__).read_text()
  assert "adb_ssh_pc" in src, "桌面上的 ADB/SSH 必须有一段说明，而不是静默无效"
  assert "enabled=adb_ssh_controls_enabled" in src
  assert "enabled=lambda: adb_ssh_controls_enabled() and ui_state.is_offroad()" in src


def test_quickboot_is_not_offered_on_the_desktop():
  with patch.object(sp_dev, "PC", True):
    assert sp_dev.quickboot_available() is False
  with patch.object(sp_dev, "PC", False):
    assert sp_dev.quickboot_available() is True


def test_the_dead_github_runner_toggle_is_gone():
  src = Path(sp_dev.__file__).read_text()
  assert "EnableGithubRunner" not in src
  ui_root = Path(sp_dev.__file__).parents[3]
  for path in ui_root.rglob("*.py"):
    if "__pycache__" in str(path) or "settings" not in path.parts:
      continue
    assert "EnableGithubRunner" not in path.read_text(), f"{path} 还在提供这个无效开关"


def test_the_quickboot_row_is_optional():
  src = Path(sp_dev.__file__).read_text()
  assert "if self.prebuilt_toggle is not None:" in src
  assert "self.prebuilt_toggle = None" in src or "else None" in src


def test_mici_leaves_adb_ssh_out_on_the_desktop():
  src = Path(mici_dev.__file__).read_text()
  assert "from openpilot.selfdrive.ui.layouts.settings.developer import adb_ssh_controls_enabled" in src, \
    "小屏必须复用大屏那个判定，不能自己再写一份 PC 判断"

  tree = ast.parse(src)
  cls = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "DeveloperLayoutMici")
  init = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "__init__")

  assigned = [n for n in ast.walk(init) if isinstance(n, ast.Assign)
              and any(getattr(target, "id", None) == "connection_toggles" for target in n.targets)]
  assert len(assigned) == 1, "ADB/SSH 两行必须由同一个变量决定，否则容易只改一半"
  value = assigned[0].value
  assert isinstance(value, ast.IfExp), "两行必须是条件加入，而不是无条件构造后置灰"
  assert "adb_ssh_controls_enabled" in ast.unparse(value.test)
  assert {"_adb_toggle", "_ssh_toggle"} <= {n.attr for n in ast.walk(value) if isinstance(n, ast.Attribute)}

  added = [n for n in ast.walk(init) if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "add_widgets"]
  assert added, "scroller.add_widgets 没找到"
  assert any(isinstance(element, ast.Starred) and getattr(element.value, "id", None) == "connection_toggles"
             for element in ast.walk(added[0].args[0])), \
    "条件化的两行没有进到 scroller 里"
