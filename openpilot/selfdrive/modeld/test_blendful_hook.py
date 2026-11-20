
import numpy as np
import pytest

from openpilot.selfdrive.modeld.modeld import get_action_from_model
from openpilot.selfdrive.modeld.constants import ModelConstants, Plan
from openpilot.cereal import log
from openpilot.sunnypilot.selfdrive.controls.lib.blendful import Blendful


def make_model_output(curvature: float = 0.0, n: int = 33) -> dict:
  """A model_output shaped like the parser's, with a plan of constant curvature."""
  x = np.array(ModelConstants.X_IDXS, dtype=np.float32)
  plan = np.zeros((1, n, 15), dtype=np.float32)
  plan[0, :, Plan.POSITION.start + 0] = x
  plan[0, :, Plan.POSITION.start + 1] = curvature * x ** 2 / 2
  plan[0, :, Plan.POSITION.start + 2] = 1.22
  plan[0, :, Plan.VELOCITY.start + 0] = 15.0
  plan[0, :, Plan.ORIENTATION_RATE.start + 2] = curvature * 15.0
  return {
    'plan': plan,
    'lane_lines': np.zeros((1, 4, n, 2), dtype=np.float32),
    'lane_lines_prob': np.zeros((1, 8), dtype=np.float32),
    'lane_lines_stds': np.full((1, 4, n, 2), 0.5, dtype=np.float32),
  }


PREV = log.ModelDataV2.Action(desiredCurvature=0.0, desiredAcceleration=0.0, shouldStop=False)


class TestHookIsInertByDefault:
  def test_no_callable_leaves_curvature_untouched(self):
    out = make_model_output(0.01)
    a = get_action_from_model(out, PREV, 0.2, 0.5, 15.0)
    b = get_action_from_model(out, PREV, 0.2, 0.5, 15.0, None)
    assert a.desiredCurvature == b.desiredCurvature

  def test_zero_returning_callable_leaves_curvature_untouched(self):
    out = make_model_output(0.01)
    base = get_action_from_model(out, PREV, 0.2, 0.5, 15.0)
    hooked = get_action_from_model(out, PREV, 0.2, 0.5, 15.0, lambda k: 0.0)
    assert base.desiredCurvature == hooked.desiredCurvature


class TestCorrectionIsApplied:
  def test_positive_correction_raises_curvature(self):
    out = make_model_output(0.01)
    base = get_action_from_model(out, PREV, 0.2, 0.5, 15.0)
    hooked = get_action_from_model(out, PREV, 0.2, 0.5, 15.0, lambda k: 0.002)
    assert hooked.desiredCurvature > base.desiredCurvature

  def test_negative_correction_lowers_curvature(self):
    out = make_model_output(0.01)
    base = get_action_from_model(out, PREV, 0.2, 0.5, 15.0)
    hooked = get_action_from_model(out, PREV, 0.2, 0.5, 15.0, lambda k: -0.002)
    assert hooked.desiredCurvature < base.desiredCurvature

  def test_correction_receives_the_unsmoothed_curvature(self):
    """Blendful must see the model's own curvature, not the previous smoothed value."""
    out = make_model_output(0.01)
    seen = []
    get_action_from_model(out, PREV, 0.2, 0.5, 15.0, lambda k: seen.append(k) or 0.0)
    assert seen, "blendful was never called"
    # get_curvature_from_plan decides the sign from the plan's own orientation rate,
    # so assert on magnitude: the point is that it sees the model's curvature, not
    # the previous frame's smoothed value.
    assert abs(seen[0]) == pytest.approx(0.01, abs=2e-3)

  def test_acceleration_is_untouched(self):
    """Blendful is lateral only. Longitudinal must not move."""
    out = make_model_output(0.01)
    base = get_action_from_model(out, PREV, 0.2, 0.5, 15.0)
    hooked = get_action_from_model(out, PREV, 0.2, 0.5, 15.0, lambda k: 0.002)
    assert hooked.desiredAcceleration == base.desiredAcceleration


class TestBlendfulItself:
  def test_disabled_returns_zero(self):
    bf = Blendful()
    out = make_model_output(0.01)
    out['lane_lines_prob'][0, 1::2] = [0.9, 0.9, 0.9, 0.9]
    assert bf.update(out, 15.0, True, 0.01, enabled=False) == 0.0

  def test_below_min_speed_returns_zero(self):
    """MIN_SPEED is 5 m/s; below it the car is manoeuvring, not lane keeping."""
    bf = Blendful()
    out = make_model_output(0.01)
    out['lane_lines_prob'][0, 1::2] = [0.9, 0.9, 0.9, 0.9]
    for _ in range(20):
      v = bf.update(out, 2.0, True, 0.01, enabled=True)
    assert v == 0.0

  def test_no_lane_lines_returns_zero(self):
    """Both probabilities at zero: nothing to centre on, so stay out of the way."""
    bf = Blendful()
    out = make_model_output(0.01)
    out['lane_lines_prob'][0, 1::2] = [0.0, 0.0, 0.0, 0.0]
    for _ in range(20):
      v = bf.update(out, 15.0, True, 0.01, enabled=True)
    assert v == 0.0

  def test_inactive_returns_zero(self):
    bf = Blendful()
    out = make_model_output(0.01)
    out['lane_lines_prob'][0, 1::2] = [0.9, 0.9, 0.9, 0.9]
    for _ in range(20):
      v = bf.update(out, 15.0, False, 0.01, enabled=True)
    assert v == 0.0
