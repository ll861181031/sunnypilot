"""Exercise the real legacy turn-desire branch without starting a model loop."""
import ast
import inspect
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from openpilot.cereal import custom
from openpilot.selfdrive.modeld import modeld


class SM:
  def __init__(self):
    self.valid = {"navInstruction": True}
    self.logMonoTime = {"navInstruction": 99_900_000_000}
    self.recv_time = {"navInstruction": 99.99}
    self.nav_reads = 0
    self.data = {
      "navInstruction": SimpleNamespace(maneuverType="turn", maneuverDistance=10.0, maneuverModifier="left"),
      "carState": SimpleNamespace(), "carControl": SimpleNamespace(latActive=True),
      "carStateSP": SimpleNamespace(blinkerLatched=False, leftLaneLine=-1, rightLaneLine=-1,
                                     leftBlindspotUnavailable=False, rightBlindspotUnavailable=False),
    }

  def __getitem__(self, name):
    if name == "navInstruction":
      self.nav_reads += 1
    return self.data[name]


class Runtime:
  def __init__(self, *, valid=True, reason="accepted", command=15, distance=10.0):
    self.instruction_valid = valid
    self._evidence = {
      "instructionReason": reason, "selectedCommand": command,
      "adjustedDistance": distance,
    }

  def evidence(self):
    return self._evidence.copy()


def actual_desire_input(sm, runtime):
  tree = ast.parse(inspect.getsource(modeld.main))
  initial = next(node for node in ast.walk(tree) if isinstance(node, ast.Assign)
                 and any(isinstance(target, ast.Name) and target.id == "navi_turn" for target in node.targets)
                 and isinstance(node.value, ast.Constant) and node.value.value is None)
  branch = next(node for node in ast.walk(tree) if isinstance(node, ast.If)
                and any(isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
                        and child.func.id == "cplink_navi_desire_enabled" for child in ast.walk(node.test)))
  update = next(node for node in ast.walk(tree) if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Attribute) and node.value.func.attr == "update"
                and isinstance(node.value.func.value, ast.Name) and node.value.func.value.id == "DH")
  captured = []
  namespace = {"sm": sm, "nav_runtime": runtime, "v_ego": 5.0, "custom": custom, "lane_change_prob": 0.0,
               "left_edge": False, "right_edge": False, "lane_line_check": 0,
               "cplink_navi_desire_enabled": modeld.cplink_navi_desire_enabled,
               "trusted_navi_turn": modeld.trusted_navi_turn,
               "DH": SimpleNamespace(update=lambda *args, **kwargs: captured.append(kwargs))}
  exec(compile(ast.Module(body=[initial, branch, update], type_ignores=[]), "<actual navigation desire>", "exec"), namespace)
  return captured[0]["navi_turn_direction"]


@pytest.fixture
def fresh_gate(monkeypatch, tmp_path):
  path = tmp_path / "cplink_navi_desire"
  path.write_text("1")
  monkeypatch.setattr(modeld, "CPLINK_DESIRE_GATE", str(path))
  monkeypatch.setattr(modeld.time, "time", lambda: path.stat().st_mtime + 0.1)
  return path


@pytest.mark.parametrize("modifier", ["left", "right"])
@pytest.mark.parametrize("produced", [10.0, 99.9])
def test_root_blocks_real_turn_desire_branch_with_valid_queued_or_bypass_instruction(monkeypatch, fresh_gate, modifier, produced):
  monkeypatch.setenv("SUNNYPILOT_AMAP_ROOT_NAV", "1")
  sm = SM()
  sm.logMonoTime["navInstruction"] = round(produced * 1e9)
  assert actual_desire_input(sm, Runtime(command=15 if modifier == "left" else 10)) is None
  assert sm.nav_reads == 0


def test_root_gate_does_not_read_legacy_opt_in_file(monkeypatch):
  monkeypatch.setenv("SUNNYPILOT_AMAP_ROOT_NAV", "1")
  monkeypatch.setattr(modeld.os.path, "getmtime", lambda *_: pytest.fail("Root mode must reject before reading legacy opt-in"))
  assert not modeld.cplink_navi_desire_enabled()


@pytest.mark.parametrize("modifier,expected", [("left", "turnLeft"), ("right", "turnRight")])
def test_nonroot_turn_desire_branch_keeps_original_behavior(monkeypatch, fresh_gate, modifier, expected):
  monkeypatch.setenv("SUNNYPILOT_AMAP_ROOT_NAV", "0")
  sm = SM()
  runtime = Runtime(command=15 if modifier == "left" else 10)
  assert actual_desire_input(sm, runtime) == getattr(custom.ModelDataV2SP.TurnDirection, expected)
  assert sm.nav_reads == 0


@pytest.mark.parametrize(("reason", "valid", "distance", "command"), [
  ("staleProduced", True, 10.0, 10),
  ("legacyBinding", False, 10.0, 10),
  ("instructionRouteMismatch", False, 10.0, 15),
  ("accepted", False, 10.0, 15),
  ("accepted", True, 30.0, 15),
  ("accepted", True, -0.1, 15),
  ("accepted", True, math.nan, 15),
  ("accepted", True, 10.0, 8),
])
def test_turn_desire_rejects_untrusted_runtime_evidence(reason, valid, distance, command):
  assert modeld.trusted_navi_turn(Runtime(valid=valid, reason=reason, distance=distance, command=command), 5.0) is None


@pytest.mark.parametrize(("command", "expected"), [
  (15, "turnLeft"), (10, "turnRight"), (9, "turnRight"),
])
def test_turn_desire_uses_the_same_accepted_navigation_action(command, expected):
  assert modeld.trusted_navi_turn(Runtime(command=command), 5.0) == \
    getattr(custom.ModelDataV2SP.TurnDirection, expected)


@pytest.mark.parametrize("age,enabled", [(0.0, True), (4.999, True), (5.0, False), (6.0, False)])
def test_nonroot_file_lease_boundaries_are_unchanged(monkeypatch, fresh_gate, age, enabled):
  monkeypatch.setenv("SUNNYPILOT_AMAP_ROOT_NAV", "0")
  monkeypatch.setattr(modeld.time, "time", lambda: fresh_gate.stat().st_mtime + age)
  assert modeld.cplink_navi_desire_enabled() is enabled


def test_nonroot_missing_opt_in_stays_disabled(monkeypatch, fresh_gate):
  monkeypatch.setenv("SUNNYPILOT_AMAP_ROOT_NAV", "0")
  fresh_gate.unlink()
  assert not modeld.cplink_navi_desire_enabled()


def test_root_switch_blocks_and_release_restores_same_legacy_gate(monkeypatch, fresh_gate):
  for root, enabled in [("0", True), ("1", False), ("1", False), ("0", True)]:
    monkeypatch.setenv("SUNNYPILOT_AMAP_ROOT_NAV", root)
    assert modeld.cplink_navi_desire_enabled() is enabled


def test_tinygrad_loop_has_no_separate_navigation_turn_entry():
  path = Path(modeld.__file__).resolve().parents[2] / "sunnypilot/modeld_v2/modeld.py"
  tree = ast.parse(path.read_text())
  calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
           and node.func.attr == "update" and isinstance(node.func.value, ast.Name) and node.func.value.id == "DH"]
  assert len(calls) == 1
  assert all(keyword.arg != "navi_turn_direction" for keyword in calls[0].keywords)
  assert not any(isinstance(node, ast.Constant) and node.value == "navInstruction" for node in ast.walk(tree))
