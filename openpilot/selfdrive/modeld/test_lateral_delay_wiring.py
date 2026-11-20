import ast
import inspect
from types import SimpleNamespace

import numpy as np
import pytest

from openpilot.cereal import custom, log
from openpilot.selfdrive.modeld import modeld as stock
from openpilot.sunnypilot.modeld_v2 import modeld as tiny
from openpilot.sunnypilot.livedelay.helpers import fill_lateral_delay_debug, lateral_action_time, select_lat_delay


class Parameters:
  def __init__(self, learned, fixed):
    self.learned, self.fixed = learned, fixed

  def get_bool(self, key):
    assert key == "LagdToggle"
    return self.learned

  def get(self, key, return_default=False):
    assert key == "LagdValueCache" and return_default
    return self.fixed


@pytest.mark.parametrize("module", [stock, tiny])
@pytest.mark.parametrize("learned,fixed", [(True, 0.45), (False, 0.45), (False, 0.30)])
def test_actual_model_loop_uses_selected_delay_once(module, learned, fixed):
  model = SimpleNamespace(LAT_SMOOTH_SECONDS=0.1)
  selected, source = select_lat_delay(Parameters(learned, fixed), 0.3)
  model.lat_delay = selected
  # Evaluate the real main-loop assignment without starting cameras or models.
  tree = ast.parse(inspect.getsource(module.main))
  assignment = next(n for n in ast.walk(tree) if isinstance(n, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "lat_action_t" for t in n.targets))
  env = {"model": model, "LAT_SMOOTH_SECONDS": 0.1, "frame_delay": 0.05, "action_delay": 0.025,
         "lateral_action_time": lateral_action_time, "lat_delay": 999}
  exec(compile(ast.Module(body=[assignment], type_ignores=[]), "<actual timing>", "exec"), env)
  assert env["lat_action_t"] == pytest.approx(selected + 0.175)
  assert source == ("published" if learned else "fixedCache")
  # Both the input tensor and action extraction consume that same local variable.
  calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
  action = next(n for n in calls if getattr(n.func, "id", getattr(n.func, "attr", "")) == "get_action_from_model")
  assert any(isinstance(a, ast.Name) and a.id == "lat_action_t" for a in action.args)
  assert any(isinstance(n, ast.List) and n.elts and isinstance(n.elts[0], ast.Name)
             and n.elts[0].id == "lat_action_t" for n in ast.walk(tree))


def test_mode_switch_and_delay_diagnostic():
  params = Parameters(False, 0.45)
  assert select_lat_delay(params, 0.3) == (0.45, "fixedCache")
  params.learned = True
  delay, source = select_lat_delay(params, 0.3)
  model = SimpleNamespace(lat_delay=delay, lat_delay_source=source)
  msg = custom.ModelDataV2SP.new_message()
  assert not msg.lateralDelayDebug.valid
  fill_lateral_delay_debug(msg, model, 0.1, lateral_action_time(delay, 0.1, 0.05, 0.025), 42, {"action": []})
  with custom.ModelDataV2SP.from_bytes(msg.to_bytes()) as restored:
    assert restored.lateralDelayDebug.frameId == 42
    assert restored.lateralDelayDebug.actionTime == pytest.approx(0.475)
    assert restored.lateralDelayDebug.modelOutputType == "action"


@pytest.mark.parametrize("stock_path,output,extractor,smoothing", [
  (True, {"plan": []}, "planYawV1", True),
  (False, {"plan": []}, "planYawV1", True),
  (False, {"plan": [], "desired_curvature": []}, "unsupported", True),
  (False, {"plan": [], "planplus": []}, "unsupported", True),
])
def test_diagnostics_identify_replayable_extractor(stock_path, output, extractor, smoothing):
  model = SimpleNamespace(lat_delay=.3, lat_delay_source="fixedCache", generation=10, PLANPLUS_CONTROL=1.5,
                          MIN_LAT_CONTROL_SPEED=.3)
  msg = custom.ModelDataV2SP.new_message()
  fill_lateral_delay_debug(msg, model, .1, .475, 42, output, .002, 10., stock=stock_path)
  assert msg.lateralDelayDebug.actionExtractor == extractor
  assert msg.lateralDelayDebug.curvatureSmoothing == smoothing
  assert msg.lateralDelayDebug.previousCurvature == pytest.approx(.002)
  assert msg.lateralDelayDebug.modelSpeed == 10.


@pytest.mark.parametrize("module", [stock, tiny])
@pytest.mark.parametrize("action_head", [False, True])
def test_plan_and_action_heads_keep_their_semantics(module, action_head, monkeypatch):
  captured = []
  state = SimpleNamespace(PLANPLUS_CONTROL=1.0, mlsim=False, generation=9, LONG_SMOOTH_SECONDS=0.,
                          LAT_SMOOTH_SECONDS=0., MIN_LAT_CONTROL_SPEED=0.3,
                          constants=SimpleNamespace(T_IDXS=np.linspace(0, 10, 33)))
  output = {"plan": np.zeros((1, 33, 15))}
  if action_head:
    output["action"] = np.array([[1.0, 0.0]])
  if module is stock:
    def curvature(*args):
      captured.append(args[-1])
      return 0.012
    monkeypatch.setattr(stock, "get_curvature_from_plan", curvature)
    result = stock.get_action_from_model(output, log.ModelDataV2.Action(), 0.475, 0.3, 10.)
  else:
    def curvature(output, plan, speed, action_time, mlsim):
      captured.append(action_time)
      return 0.012
    monkeypatch.setattr(tiny, "get_curvature_from_output", curvature)
    result = tiny.ModelState.get_action_from_model(state, output, log.ModelDataV2.Action(), 0.475, 0.3, 10.)
  assert captured == ([] if action_head else [0.475])
  assert result.desiredCurvature == pytest.approx(0.01 if action_head else 0.012)
