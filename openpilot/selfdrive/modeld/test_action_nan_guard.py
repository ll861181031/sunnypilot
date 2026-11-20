
import numpy as np
import pytest

from openpilot.cereal import log
from openpilot.selfdrive.modeld.modeld import get_action_from_model

NAN = float("nan")


def action(curv: float, accel: float) -> log.ModelDataV2.Action:
  return log.ModelDataV2.Action(desiredCurvature=curv, desiredAcceleration=accel, shouldStop=False)


def run(action_out: tuple[float, float], prev: log.ModelDataV2.Action, v_ego: float = 10.0):
  """Feed a raw model 'action' pair through the same path modeld uses."""
  out = {"action": np.array([[action_out[0], action_out[1]]], dtype=np.float32)}
  return get_action_from_model(out, prev, 0.0, 0.0, v_ego)


def test_nan_curvature_reuses_previous_value():
  out = run((NAN, -0.5), action(0.02, 0.0))
  assert np.isfinite(out.desiredCurvature)
  assert out.desiredCurvature == pytest.approx(0.02)
  # A valid accel must survive (smoothing moves it toward -0.5, never back to NaN).
  assert np.isfinite(out.desiredAcceleration)
  assert -0.5 <= out.desiredAcceleration <= 0.0


def test_nan_accel_reuses_previous_value():
  out = run((0.05, NAN), action(0.0, -1.0))
  assert np.isfinite(out.desiredAcceleration)
  assert out.desiredAcceleration == pytest.approx(-1.0)


def test_nan_previous_action_does_not_stick():
  out = run((NAN, NAN), action(NAN, NAN))
  assert out.desiredCurvature == 0.0
  assert out.desiredAcceleration == 0.0


def test_repeated_nan_never_escapes_as_nan():
  prev = action(0.0, 0.0)
  for _ in range(5):
    prev = run((NAN, NAN), prev)
    assert np.isfinite(prev.desiredCurvature)
    assert np.isfinite(prev.desiredAcceleration)


def test_one_bad_frame_does_not_destroy_the_next_good_one():
  prev = action(0.02, 0.0)
  prev = run((NAN, NAN), prev)
  assert prev.desiredCurvature == pytest.approx(0.02)
  out = run((0.10, 0.0), prev)
  assert np.isfinite(out.desiredCurvature)
  assert out.desiredCurvature != pytest.approx(0.02)
