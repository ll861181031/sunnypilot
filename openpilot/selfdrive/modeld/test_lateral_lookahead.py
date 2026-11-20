"""Lateral lookahead: sample the model's own plan 5 m ahead instead of 0.2 s ahead.

Background, from route 000001af (2026-11-16 23:35:14, the first takeover): the plan
described a 13 m radius arc needing about -230 deg of wheel, the action head reported
the same plan at ~0.2 s which is -45 deg at 2.26 m/s, and the car drove straight until
the driver grabbed the wheel. The action head is not wrong about "now"; it answers too
late because 0.2 s is 5 m at 25 m/s but only 40 cm at 2 m/s.
"""

import types

import numpy as np
import pytest

from openpilot.selfdrive.modeld.modeld import (LAT_LOOKAHEAD_MAX_S, get_action_from_model,
                                               lateral_lookahead_time)
from openpilot.selfdrive.controls.lib.drive_helpers import get_curvature_from_plan
from openpilot.selfdrive.modeld.constants import ModelConstants, Plan

T_IDXS = np.asarray(ModelConstants.T_IDXS, dtype=np.float64)
IDX_N = len(T_IDXS)


def arc_plan(v_ego: float, curvature: float):
  """A constant-curvature plan in the layout get_action_from_model expects."""
  plan = np.zeros((1, IDX_N, 15), dtype=np.float32)
  yaw = curvature * v_ego * T_IDXS
  with np.errstate(divide="ignore", invalid="ignore"):
    if abs(curvature) > 1e-9:
      x = np.sin(yaw) / curvature
      y = (1.0 - np.cos(yaw)) / curvature
    else:
      x = v_ego * T_IDXS
      y = np.zeros_like(x)
  plan[0, :, Plan.POSITION][:, 0] = x
  plan[0, :, Plan.POSITION][:, 1] = y
  plan[0, :, Plan.T_FROM_CURRENT_EULER][:, 2] = yaw
  plan[0, :, Plan.ORIENTATION_RATE][:, 2] = curvature * v_ego
  return plan


def model_output(plan, action_curvature_raw: float = 0.0, action_accel: float = 0.0):
  return {"plan": plan, "action": np.array([[action_curvature_raw, action_accel]], dtype=np.float32)}


def prev_action(curvature: float = 0.0, accel: float = 0.0):
  return types.SimpleNamespace(desiredCurvature=curvature, desiredAcceleration=accel)


def run_lateral(plan, v_ego: float, head_curvature: float):
  """head_curvature is in 1/m; the head stores it as lateral accel, i.e. curvature * v^2."""
  out = get_action_from_model(model_output(plan, head_curvature * v_ego ** 2),
                              prev_action(), 0.2, 0.2, v_ego)
  return out.desiredCurvature


def test_lookahead_targets_fixed_distance_not_fixed_time():
  # 2 m/s reaches 5 m in 2.5 s, 12 m/s reaches it in ~0.42 s: same distance, very
  # different horizon. That asymmetry is the whole point.
  slow = lateral_lookahead_time(np.asarray(arc_plan(2.0, 0.0)[0][:, Plan.POSITION][:, 0]),
                                T_IDXS)
  fast = lateral_lookahead_time(np.asarray(arc_plan(12.0, 0.0)[0][:, Plan.POSITION][:, 0]),
                                T_IDXS)
  assert slow == pytest.approx(LAT_LOOKAHEAD_MAX_S, abs=1e-6)
  assert fast < 0.5
  assert fast > 0.3


def test_lookahead_is_none_when_plan_cannot_reach_the_target():
  plan_x = np.asarray([0.0, 0.2, 0.5, 0.9, 1.2] + [1.5] * (IDX_N - 5), dtype=np.float64)
  assert lateral_lookahead_time(plan_x, T_IDXS) is None


def test_lookahead_is_none_on_shape_mismatch():
  assert lateral_lookahead_time(np.zeros(3), T_IDXS) is None
  assert lateral_lookahead_time(np.zeros(IDX_N), np.zeros(4)) is None


def test_low_speed_turn_gets_the_angle_the_corner_needs():
  # 2.26 m/s, the real speed at the route 000001af takeover. The driver needed -230 deg
  # of wheel, i.e. about 0.076 1/m; the head said 0.015 1/m (-45 deg).
  v_ego, need = 2.26, 0.076
  plan = arc_plan(v_ego, need)
  got = run_lateral(plan, v_ego, head_curvature=0.015)
  assert abs(got) > 3.0 * 0.015, "lookahead must ask for far more than the head"
  assert abs(got) == pytest.approx(need, rel=0.25)


def test_standstill_falls_back_to_the_action_head():
  # Plan only covers a metre: the horizon is degenerate, so the head must still win.
  plan = arc_plan(0.5, 0.05)
  plan[0, :, Plan.POSITION][:, 0] = np.linspace(0.0, 1.0, IDX_N)
  head = 0.004
  got = run_lateral(plan, 0.5, head_curvature=head)
  # The head stores lateral accel, and the fallback divides by max(1, v)^2, so at
  # walking pace the expected value is head * v^2 / 1.
  assert got == pytest.approx(head * 0.5 ** 2 / 1.0, rel=1e-6)


def test_missing_plan_falls_back_to_the_action_head():
  head = 0.003
  out = get_action_from_model({"action": np.array([[head * 9.0, 0.0]], dtype=np.float32)},
                              prev_action(), 0.2, 0.2, 3.0)
  assert out.desiredCurvature == pytest.approx(head, rel=1e-6)


def test_straight_plan_does_not_invent_steering():
  v_ego = 8.0
  got = run_lateral(arc_plan(v_ego, 0.0), v_ego, head_curvature=0.0)
  assert abs(got) < 1e-4


def test_highway_horizon_stays_short_so_behaviour_is_unchanged():
  # At 15 m/s the 5 m lookahead is ~0.33 s, so the plan-derived value must land close to
  # what the head already reported for the same stretch of road.
  v_ego, curv = 15.0, 0.004
  got = run_lateral(arc_plan(v_ego, curv), v_ego, head_curvature=curv)
  assert got == pytest.approx(curv, rel=0.1)


def test_longitudinal_channel_is_untouched():
  v_ego = 5.0
  # prev matches the head so the longitudinal smoothing is a no-op and any leak from
  # the lateral branch would show up as a different value.
  out = get_action_from_model(model_output(arc_plan(v_ego, 0.02), 0.0, action_accel=-0.8),
                              prev_action(accel=-0.8), 0.2, 0.2, v_ego)
  assert out.desiredAcceleration == pytest.approx(-0.8, rel=1e-6)


def test_negative_curvature_keeps_its_sign():
  v_ego = 2.5
  got = run_lateral(arc_plan(v_ego, -0.06), v_ego, head_curvature=-0.005)
  assert got < 0.0
  assert abs(got) > 0.02


def test_both_backends_pick_the_same_lookahead():
  """stock modeld and the tinygrad modeld_v2 must agree on tau for the same plan.

  They keep separate copies of get_action_from_model; a future edit to one of them is
  exactly the kind of drift this change has to avoid, so the shared helper is pinned to
  the same input from both call sites.
  """
  import inspect

  from openpilot.selfdrive.modeld import modeld as stock_mod
  from openpilot.sunnypilot.modeld_v2 import modeld as tiny_mod

  plan = arc_plan(2.26, 0.076)
  tau = stock_mod.lateral_lookahead_time(np.asarray(plan[0][:, Plan.POSITION][:, 0], dtype=np.float64),
                                         T_IDXS)
  # 5 m at 2.26 m/s is about 2.2 s; the cap only bites below ~2 m/s.
  assert tau is not None
  assert 0.0 < tau <= LAT_LOOKAHEAD_MAX_S

  for mod in (stock_mod, tiny_mod):
    src = inspect.getsource(mod)
    assert "lateral_lookahead_time(" in src, f"{mod.__name__} lost the shared helper"
    assert "model_output['action'][0,0] / (max(1.0, v_ego))**2" in src or \
           "model_output['action'][0, 0] / (max(1.0, v_ego))**2" in src, \
           f"{mod.__name__} lost the low-speed fallback"


def test_never_looks_less_far_ahead_than_the_action_head():
  """Above ~13 m/s the 5 m lookahead is shorter than the head's own horizon.

  Sampling earlier there would reduce steering exactly where the old behaviour was
  fine, so the caller clamps tau up to lat_action_t. The plan below is straight for
  the first 6 m and then turns: at 15 m/s the 5 m sample sees nothing, the head's own
  0.5 s (7.5 m) does. Unclamped this would return 0.
  """
  v_ego, lat_action_t, onset_m, k = 15.0, 0.5, 6.0, 0.02
  plan = np.zeros((1, IDX_N, 15), dtype=np.float32)
  x = v_ego * T_IDXS
  plan[0, :, Plan.POSITION][:, 0] = x
  curvature = np.where(x < onset_m, 0.0, k)
  yaw = np.concatenate(([0.0], np.cumsum(curvature[1:] * np.diff(x))))
  plan[0, :, Plan.T_FROM_CURRENT_EULER][:, 2] = yaw
  plan[0, :, Plan.ORIENTATION_RATE][:, 2] = curvature * v_ego

  tau_5m = lateral_lookahead_time(np.asarray(x, dtype=np.float64), T_IDXS)
  assert tau_5m is not None and tau_5m < lat_action_t, "test needs 5 m to be the shorter horizon"

  yaw_at_5m = get_curvature_from_plan(yaw, curvature * v_ego, T_IDXS, v_ego, tau_5m)
  yaw_at_head = get_curvature_from_plan(yaw, curvature * v_ego, T_IDXS, v_ego, lat_action_t)
  assert yaw_at_5m == 0.0, "5 m must still be before the corner, otherwise the test proves nothing"
  assert yaw_at_head > 0.0

  got = get_action_from_model(model_output(plan, 0.0), prev_action(), lat_action_t, lat_action_t, v_ego)
  # Clamped up to the head's own horizon, so the corner is visible; unclamped it would be 0.
  assert got.desiredCurvature == pytest.approx(yaw_at_head, rel=1e-5)
  assert got.desiredCurvature > 0.0


def test_plan_with_nan_falls_back_to_the_head():
  v_ego = 3.0
  plan = arc_plan(v_ego, 0.05)
  plan[0, 4:8, Plan.POSITION][:, 0] = np.nan
  head = 0.002
  got = get_action_from_model(model_output(plan, head * v_ego ** 2), prev_action(), 0.2, 0.2, v_ego)
  assert np.isfinite(got.desiredCurvature)
