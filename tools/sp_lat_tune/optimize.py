"""Choosing the next lateral parameter pair to drive.

The search space is two dimensional and small: the settings page exposes exactly
two knobs that change tracking quality.

    lead_tenths   0.5 .. 3.4 deg    high-speed half of the commanded-angle bound
    rate_tenths   0.5 .. 1.0        steering rate ceiling (the panda keeps 2x this)

The settings page may only TIGHTEN what the car ships. Values above the shipped
table are no-ops, which is why the search space stops at 3.4 deg instead of the
panda's 10 deg envelope.

Everything else the settings page offers is either safety behaviour (driver torque,
rearm) or a cap that never fires in practice (absolute angle).

Two rules this module exists to enforce.

1. **Feasibility is a filter, not a weight.** A trial that provoked a single TooFast
   frame is not "slightly worse", it is disqualified -- the fault escalates to
   steerTempUnavailable, which hands the wheel back. The 6.0 deg experiment was
   reverted for exactly this reason and a weighted objective would have kept it.

2. **No surrogate can predict the closed loop.** A Gaussian process here is only a
   way to choose the next drive; it is never evidence about how the car will behave.
   Every number it produces is a guess that the next drive either confirms or kills.
"""

from __future__ import annotations

import numpy as np

# Settings-page ranges, in tenths.
LEAD_RANGE = (5, 34)
RATE_RANGE = (5, 10)
LEAD_STEP = 5
RATE_STEP = 1

# Hard ceilings. Both are the top of the useful settings range: a larger lead is a
# no-op (the host takes min(shipped, setting)), and a larger rate clamps to the
# shipped table top (1.0 deg per 100 Hz cycle).
LEAD_HARD_MAX = 34
RATE_HARD_MAX = 10

# The settings page can only tighten the shipped table, so the sweep stays at or
# below the values already validated on the car. There is no measured TooFast
# knee inside this range; keep the prior flat rather than inventing one.
LEAD_RISK = ((5, 0.0), (34, 0.0))

JERK_LIMIT = 3.6

# Minimum separation between two suggestions, in normalised parameter space.
MIN_SEPARATION = 0.25


def lead_risk_prior(lead_tenths: int) -> float:
  xs, ys = zip(*LEAD_RISK, strict=True)
  return float(np.interp(lead_tenths, xs, ys))


def feasible(trial: dict, *, jerk_limit: float = JERK_LIMIT) -> tuple[bool, str]:
  if not trial.get("frames_active"):
    return False, "no active frames"
  # Attribute before disqualifying: steerFaultTemporary also latches on a panda
  # snapshot that is not fresh, and across the local corpus that artifact was 812
  # frames while 0x1FC carried no TORQUE_TEMP_FAILED at all. Only the raw EPS pulse
  # with a commanded lead large enough to explain it disqualifies a pair.
  attributed = trial.get("toofast_attributed", trial.get("toofast_frames", 0))
  if attributed > 0:
    return False, f"TooFast under load in {attributed} frames"
  if trial["steer_fault_permanent_frames"] > 0:
    return False, "EPS permanent fault"
  if trial["jerk_p99"] == trial["jerk_p99"] and trial["jerk_p99"] > jerk_limit:
    return False, f"jerk p99 {trial['jerk_p99']:.2f} > {jerk_limit}"
  return True, ""


def objective(trial: dict) -> float:
  """Lower is better. Only meaningful for feasible trials.

  ``undershoot_aggregate`` rather than the mean of per-frame ratios: the ratio is
  unbounded when the car is barely turning, so a handful of near-straight frames
  can dominate a mean. The aggregate is sum(desired)/sum(actual) and is not.
  """
  under = trial.get("undershoot_aggregate", float("nan"))
  if under != under:
    under = 1.0
  severe = trial.get("severe_ratio", 0.0)
  if severe != severe:
    severe = 0.0
  # Weighted this way because severe understeer is what the driver feels, while the
  # aggregate is what the data shows. Neither is allowed to dominate alone.
  return float(under + 0.5 * severe)


def _space() -> list[tuple[int, int]]:
  leads = list(range(LEAD_RANGE[0], LEAD_RANGE[1] + 1, LEAD_STEP))
  rates = list(range(RATE_RANGE[0], RATE_RANGE[1] + 1, RATE_STEP))
  return [(lead, rate) for lead in leads for rate in rates]


def _normalise(lead: int, rate: int) -> tuple[float, float]:
  return ((lead - LEAD_RANGE[0]) / (LEAD_RANGE[1] - LEAD_RANGE[0]),
          (rate - RATE_RANGE[0]) / (RATE_RANGE[1] - RATE_RANGE[0]))


def _gp_predict(x_train: np.ndarray, y_train: np.ndarray, x_test: np.ndarray,
                *, length: float = 0.45, noise: float = 0.05) -> tuple[np.ndarray, np.ndarray]:
  """Plain RBF Gaussian process. Small, dependency-free, and enough for 2-D.

  Returns (mean, std). With fewer than two observations the prior is returned, which
  is honest: there is nothing to interpolate yet.
  """
  if len(x_train) == 0:
    return np.full(len(x_test), float(np.mean(y_train) if len(y_train) else 1.0)), np.ones(len(x_test))
  if len(x_train) == 1:
    return np.full(len(x_test), float(y_train[0])), np.ones(len(x_test))

  def kernel(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    d2 = ((a[:, None, :] - b[None, :, :]) ** 2).sum(-1)
    return np.exp(-0.5 * d2 / length ** 2)

  k = kernel(x_train, x_train) + noise * np.eye(len(x_train))
  k_s = kernel(x_train, x_test)
  k_ss = np.diag(kernel(x_test, x_test))
  try:
    chol = np.linalg.cholesky(k)
    alpha = np.linalg.solve(chol.T, np.linalg.solve(chol, y_train))
    v = np.linalg.solve(chol, k_s)
    mean = k_s.T @ alpha
    var = np.maximum(k_ss - (v ** 2).sum(0), 1e-9)
  except np.linalg.LinAlgError:
    return np.full(len(x_test), float(np.mean(y_train))), np.ones(len(x_test))
  return mean, np.sqrt(var)


def suggest(store, *, n: int = 1, explore: float = 1.0) -> list[dict]:
  """Next parameter pairs to drive, best first.

  Before any data exists this is a space-filling design (the four corners plus the
  middle of the range) rather than a random guess: the corners are what tells the
  surrogate which direction the cost actually moves in.
  """
  trials = store.trials()
  tried = set(trials)
  space = [p for p in _space() if p not in tried]
  if not space:
    return []

  # With fewer than three observations the surrogate has no shape: every candidate
  # gets the same mean and the same uncertainty, so "the best" is decided by list
  # order. Drive a designed set instead of dressing that up as an optimum.
  if len(trials) < 3:
    order = [(5, 5), (5, 10), (30, 7), (30, 5), (30, 10), (15, 7), (34, 10)]
    picks = [p for p in order if p in space][:n]
    return [{"lead_tenths": l, "rate_tenths": r, "reason": "space-filling start"}
            for l, r in picks]

  xs = np.asarray([_normalise(*k) for k in trials], dtype=float)
  ys = np.asarray([objective(t) for t in trials.values()], dtype=float)
  # An infeasible trial is not a slightly worse point, it is a wall. Pushing it far
  # up the cost surface keeps the surrogate away without pretending to know why.
  penalty = float(np.nanmax(ys)) + 2.0
  for i, trial in enumerate(trials.values()):
    ok, _ = feasible(trial)
    if not ok:
      ys[i] = penalty

  test = np.asarray([_normalise(*p) for p in space], dtype=float)
  mean, std = _gp_predict(xs, ys, test)

  # Lower confidence bound: exploit where the surrogate is good, explore where it
  # is not. The TooFast prior enters here as a floor on the predicted cost.
  prior = np.asarray([lead_risk_prior(l) for l, _ in space], dtype=float)
  score = mean - explore * std + 2.0 * prior
  order = np.argsort(score)

  # With one or two observations every candidate has almost the same prediction, so
  # taking the top n by score returns n neighbours of the same point. That wastes a
  # drive per neighbour; enforce a minimum separation instead.
  out: list[dict] = []
  chosen: list[tuple[float, float]] = []
  for idx in order:
    lead, rate = space[int(idx)]
    point = _normalise(lead, rate)
    if any(max(abs(point[0] - c[0]), abs(point[1] - c[1])) < MIN_SEPARATION for c in chosen):
      continue
    chosen.append(point)
    out.append({"lead_tenths": lead, "rate_tenths": rate,
                "predicted": float(mean[int(idx)]),
                "uncertainty": float(std[int(idx)]),
                "too_fast_prior": float(prior[int(idx)])})
    if len(out) == n:
      break
  return out


def best(trials: dict) -> dict | None:
  """Best feasible trial, or None when nothing has qualified yet."""
  qualified = [t for t in trials.values() if feasible(t)[0]]
  if not qualified:
    return None
  return min(qualified, key=objective)
