"""First-order, delayed response hypotheses with whole-route holdouts."""

from collections import defaultdict

import numpy as np

DT = 0.02
HORIZONS = (5, 15, 25)


def response_group(row, previous=None):
  speed_bin = int(row["speed"] * 3.6 // 10) * 10
  direction = "positiveAngle" if row["command"] >= 0 else "negativeAngle"
  phase = "hold"
  if (previous is not None and previous.get("rawAngle") is not None and previous.get("command") is not None and
      row["route"] == previous["route"] and row["segment"] == previous["segment"]):
    dt = row["t"] - previous["t"]
    if 0 < dt <= 0.06:
      rate = (row["rawAngle"] - previous["rawAngle"]) / dt
      if row["command"] * previous["command"] < 0 or row["command"] * row["rawAngle"] < 0:
        phase = "reverse"
      elif abs(rate) > 3:
        phase = "build" if row["rawAngle"] * rate >= 0 else "return"
  return f"{row.get('cohort', 'unknown')}|{speed_bin}-{speed_bin + 10}kph|{direction}|{phase}"


def collect_blocks(rows):
  grouped = defaultdict(list)
  current, current_key, previous = [], None, None

  def finish():
    if len(current) < 2 or current[-1]["t"] - current[0]["t"] < 1.5:
      return
    times = np.array([r["t"] for r in current])
    grid = np.arange(times[0], times[-1], DT)
    idx = np.searchsorted(times, grid, side="right") - 1
    grouped[current_key].append({"route": current[0]["route"], "t": grid,
                                 "u": np.array([r["command"] for r in current])[idx],
                                 "y": np.array([r["rawAngle"] for r in current])[idx]})

  for row in rows:
    eligible = row.get("fitEligible", False) and abs(row["desired"] - row["command"]) <= 0.15
    key = response_group(row, previous) if eligible else None
    continuous = previous is not None and row["route"] == previous["route"] and row["segment"] == previous["segment"]
    continuous = continuous and 0 < row["t"] - previous["t"] <= 0.06
    if not eligible or key != current_key or not continuous:
      finish()
      current = []
    if eligible:
      current.append(row)
    current_key, previous = key, row
  finish()
  return grouped


def fit_first_order(blocks):
  best = None
  for lag in range(33):
    xs, ys = [], []
    for block in blocks:
      y, u = block["y"], block["u"]
      if len(y) <= lag + 2 or np.ptp(u) < 1:
        continue
      delayed = u[:len(u) - lag - 1]
      xs.append(y[lag:-1] - delayed)
      ys.append(y[lag + 1:] - delayed)
    if not xs:
      continue
    x, target = np.concatenate(xs), np.concatenate(ys)
    denominator = float(x @ x)
    if denominator < 1e-8:
      continue
    alpha = float(x @ target / denominator)
    if not 0 <= alpha < 1:
      continue
    residual = target - alpha * x
    loss = float(np.mean(residual ** 2))
    if best is None or loss < best["loss"]:
      best = {"alpha": alpha, "delaySteps": lag, "delaySeconds": lag * DT,
              "timeConstantSeconds": float(-DT / np.log(alpha)) if alpha > 0 else 0., "loss": loss,
              "residualMeanDeg": float(np.mean(residual)), "residualP95Deg": float(np.percentile(abs(residual), 95))}
  return best


def validate_model(blocks, model):
  alpha, lag = model["alpha"], model["delaySteps"]
  result = {}
  for horizon in HORIZONS:
    errors, holds = [], []
    for block in blocks:
      y, u = block["y"], block["u"]
      for start in range(lag, len(y) - horizon, 5):
        predicted = y[start]
        for step in range(horizon):
          predicted = alpha * predicted + (1 - alpha) * u[start + step - lag]
        errors.append(abs(predicted - y[start + horizon]))
        holds.append(abs(y[start] - y[start + horizon]))
    if errors:
      result[f"{horizon * DT:.2f}s"] = {"maeDeg": float(np.mean(errors)), "holdMaeDeg": float(np.mean(holds)), "windows": len(errors)}
  return result


def fit_responses(rows):
  groups = collect_blocks(rows)
  result = []
  for group, blocks in sorted(groups.items()):
    routes = sorted({b["route"] for b in blocks})
    record = {"group": group, "routes": routes, "blocks": len(blocks), "controlReady": False,
              "status": "unidentifiable", "reason": "insufficient_independent_routes"}
    if len(routes) >= 2 and not group.startswith(("unverified:", "unknown")):
      folds = []
      for route in routes:
        model = fit_first_order([b for b in blocks if b["route"] != route])
        score = validate_model([b for b in blocks if b["route"] == route], model) if model else {}
        folds.append({"heldOutRoute": route, "model": model, "scores": score})
      record["holdouts"] = folds
      useful = all(f["scores"] and all(s["maeDeg"] < s["holdMaeDeg"] for s in f["scores"].values()) for f in folds)
      fold_models = [f["model"] for f in folds if f["model"]]
      delay_spread = max((m["delaySeconds"] for m in fold_models), default=0.) - min((m["delaySeconds"] for m in fold_models), default=0.)
      taus = [m["timeConstantSeconds"] for m in fold_models]
      tau_ratio = max(taus, default=0.) / max(min(taus, default=0.), 1e-6)
      record["stability"] = {"delaySpreadSeconds": delay_spread, "timeConstantRatio": tau_ratio,
                             "maxDelaySpreadSeconds": .1, "maxTimeConstantRatio": 2.}
      useful &= len(fold_models) == len(folds) and delay_spread <= .1 and tau_ratio <= 2.
      model = fit_first_order(blocks)
      if useful and model:
        record.update(status="hypothesis", reason="outperforms_hold_on_route_holdouts", model=model)
      else:
        record["reason"] = "unstable_or_not_better_than_hold"
    result.append(record)
  return {"controlReady": False, "modelFamily": "unit_gain_first_order_with_pure_delay", "samplePeriod": DT,
          "delaySearchSeconds": [0, 0.64], "groups": result,
          "coverage": {"requests": len(rows), "healthyAcceptedSamples": sum(bool(r.get("fitEligible")) for r in rows),
                       "unclippedHealthySamples": sum(bool(r.get("fitEligible")) and abs(r["desired"] - r["command"]) <= .15 for r in rows),
                       "continuousBlocks": sum(len(b) for b in groups.values()), "minimumBlockSeconds": 1.5},
          "reason": "evaluated_groups" if groups else "no_continuous_unclipped_identification_windows",
          "status": "hypotheses" if any(r["status"] == "hypothesis" for r in result) else "unidentifiable",
          "note": "闭环日志拟合仅是假设；未知版本不跨路线混合，结果不会写入车辆参数。"}
