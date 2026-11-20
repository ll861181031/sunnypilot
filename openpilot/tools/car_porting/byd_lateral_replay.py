"""Read-only lateral experiments on local Song ALT logs.

Only files under the explicitly selected output directory are written. No live
process, hardware connection, CAN publisher, or production Params is created.
"""

import argparse
import csv
import hashlib
import json
import math
import subprocess
from collections import Counter
from pathlib import Path

import numpy as np

from openpilot.tools.car_porting.byd_lateral_candidates import HostReplay, PreviewReplay, curve_speed, result
from openpilot.tools.car_porting.byd_lateral_data import read_route
from openpilot.tools.car_porting.byd_lateral_fit import fit_responses, response_group

DEFAULT_MANIFEST = Path(__file__).with_name("byd_lateral_routes.json")


def summary(rows):
  errors, healthy_seconds, integral, large_seconds, prepare_seconds = [], 0., 0., 0., 0.
  prepare_windows = []
  window_start = None
  window_reason = None
  previous = None
  yaw_pairs = []
  curves = []
  curve = None

  def is_curve_sample(row):
    return bool(row.get("clean") and row.get("latActive") and row.get("requestedCurvature") is not None
                and abs(row["requestedCurvature"]) * row["speed"] ** 2 >= .5)

  for row in rows:
    dt = 0.
    if previous and row["segment"] == previous["segment"] and 0 < row["t"] - previous["t"] <= .06:
      dt = row["t"] - previous["t"]
      if previous.get("latActive") and previous["prepare"]:
        prepare_seconds += dt
      if is_curve_sample(previous):
        error = abs(previous["desired"] - previous["rawAngle"])
        healthy_seconds += dt
        integral += error * dt
        large_seconds += dt if error > 8 else 0
        if curve:
          curve["end"] = row["t"]
          curve["errorIntegralDegSeconds"] += error * dt
          curve["errorOver8Seconds"] += dt if error > 8 else 0
    if is_curve_sample(row):
      error = abs(row["desired"] - row["rawAngle"])
      errors.append(error)
      if curve is None or dt == 0 or curve["direction"] != (1 if row["requestedCurvature"] > 0 else -1):
        if curve:
          curves.append(curve)
        curve = {"segment": row["segment"], "start": row["t"], "end": row["t"],
                 "direction": 1 if row["requestedCurvature"] > 0 else -1, "errorIntegralDegSeconds": 0.,
                 "errorOver8Seconds": 0., "maxMeasuredAngleDeg": 0., "errors": []}
      curve["end"] = row["t"]
      curve["errors"].append(error)
      curve["maxMeasuredAngleDeg"] = max(curve["maxMeasuredAngleDeg"], abs(row["rawAngle"]))
    elif curve:
      curves.append(curve)
      curve = None
    preparing = row.get("latActive") and row["prepare"]
    if window_start is not None and (not preparing or dt == 0):
      first_active = row["t"] if dt and row.get("latActive") and row["active"] else None
      end = first_active if first_active is not None else previous["t"]
      prepare_windows.append({"start": window_start, "end": end, "seconds": end - window_start,
                              "firstActiveSubmissionTime": first_active, "reason": window_reason})
      window_start = None
    if preparing:
      if window_start is None:
        window_start = row["t"]
        diagnostic = row.get("diagnostic", {})
        window_reason = diagnostic.get("cycleReason") or diagnostic.get("entryReason") or "unknown_without_cycle_diagnostic"
    if all(row.get(k) is not None for k in ("imuYaw", "canYaw")):
      if 0 <= row["t"] - row.get("imuTime", -math.inf) <= .06 and 0 <= row["t"] - row.get("yawTime", -math.inf) <= .06:
        yaw_pairs.append((row["imuYaw"], row["canYaw"]))
    previous = row
  if window_start is not None:
    prepare_windows.append({"start": window_start, "end": previous["t"], "seconds": previous["t"] - window_start,
                            "firstActiveSubmissionTime": None, "reason": window_reason})
  if curve:
    curves.append(curve)
  for curve in curves:
    curve["errorP95Deg"] = float(np.percentile(curve.pop("errors"), 95))
    curve["successfulLargeAngleCoverage"] = curve["maxMeasuredAngleDeg"] >= 40 and curve["errorP95Deg"] <= 8
  yaw = {"samples": len(yaw_pairs), "correlation": None, "medianAbsDifferenceRadS": None}
  if yaw_pairs:
    a, b = np.asarray(yaw_pairs).T
    yaw["medianAbsDifferenceRadS"] = float(np.median(abs(a - b)))
    if np.std(a) > 1e-9 and np.std(b) > 1e-9:
      yaw["correlation"] = float(np.corrcoef(a, b)[0, 1])
  return {"requests": len(rows), "receipts": dict(Counter(r["receipt"] for r in rows)),
          "cleanCurveSamples": len(errors), "errorP95Deg": float(np.percentile(errors, 95)) if errors else None,
          "cleanCurveSeconds": healthy_seconds, "errorIntegralDegSeconds": integral, "errorOver8Seconds": large_seconds,
          "prepareSecondsWhileLatActive": prepare_seconds, "prepareWindows": prepare_windows,
          "curveWindows": curves, "successfulLargeAngleWindows": sum(c["successfulLargeAngleCoverage"] for c in curves),
          "curveWindowDefinition": "continuous healthy samples with requested lateral acceleration >= 0.5 m/s2, split at gaps or direction changes",
          "integrationMethod": "left_sample_hold_for_intervals_up_to_60ms; segment boundaries are not bridged",
          "yawCrossCheck": yaw, "inputIssues": dict(Counter(issue for r in rows for issue in r.get("issues", [])))}


class HypotheticalResponse:
  def __init__(self):
    self.reset()

  def reset(self):
    self.history = []
    self.value = None
    self.last_time = None
    self.key = None

  def update(self, row, angle, model, key):
    delay, tau = model["delaySeconds"], model["timeConstantSeconds"]
    if not all(math.isfinite(x) and x >= 0 for x in (delay, tau)):
      self.reset()
      return result("invalid", "invalid_response_hypothesis")
    t = row["t"]
    if self.last_time is None or key != self.key or not 0 < t - self.last_time <= .06:
      self.history = []
      self.value = row["rawAngle"]
      self.last_time = t
    dt = t - self.last_time
    self.history.append((t, angle))
    old = [u for stamp, u in self.history if stamp <= t - delay]
    if old and dt > 0:
      alpha = math.exp(-dt / tau) if tau > 0 else 0.
      self.value = alpha * self.value + (1 - alpha) * old[-1]
    self.history = [(stamp, u) for stamp, u in self.history if stamp >= t - delay - .1]
    self.last_time, self.key = t, key
    return result("hypothesis", "open_loop_actuator_prediction", predictedAngleDeg=float(self.value),
                  recordedTrajectoryReusedForInputs=True, closedLoopValidated=False)


def validate_config(config):
  if not isinstance(config, dict):
    raise ValueError("experiment config must be an object")
  allowed = {"preview_delays_s", "curve_speed", "recovery_hold_seconds", "response_model"}
  if set(config) - allowed:
    raise ValueError(f"Unknown experiment keys: {sorted(set(config) - allowed)}")
  delays = config.get("preview_delays_s", [None])
  if not isinstance(delays, list) or not delays or any(d is not None and (not isinstance(d, (int, float)) or
                                                                       not math.isfinite(d) or d <= 0) for d in delays):
    raise ValueError("preview_delays_s must contain positive finite values or null (recorded baseline)")
  hold = config.get("recovery_hold_seconds")
  if hold is not None and (not isinstance(hold, (int, float)) or not math.isfinite(hold) or hold < 0):
    raise ValueError("invalid recovery_hold_seconds")
  if "response_model" in config:
    model = config["response_model"]
    if not isinstance(model, dict) or any(k not in model or not isinstance(model[k], (int, float)) or not math.isfinite(model[k]) or model[k] < 0
                                         for k in ("delaySeconds", "timeConstantSeconds")):
      raise ValueError("invalid response_model")
  if "curve_speed" in config and not isinstance(config["curve_speed"], dict):
    raise ValueError("curve_speed must be an object with explicit capabilities")
  return config


def evaluate(rows, metadata, mode, config, fitted=None):
  baseline = HostReplay()
  recovery = HostReplay(config["recovery_hold_seconds"]) if "recovery_hold_seconds" in config else None
  preview = mode in ("preview", "combined")
  speed = mode in ("curve-speed", "combined")
  delays = config.get("preview_delays_s", [None])
  controllers = {str(d): HostReplay() for d in delays}
  extractors = {str(d): PreviewReplay() for d in delays}
  responses = {str(d): HypotheticalResponse() for d in delays}
  models = {g["group"]: g["model"] for g in (fitted or {}).get("groups", []) if g["status"] == "hypothesis"}
  previous = None
  for row in rows:
    record = {"route": row["route"], "segment": row["segment"], "t": row["t"], "controlReady": False,
              "recordedDesiredDeg": row.get("desired"), "recordedAngleDeg": row.get("rawAngle"),
              "recordedCommandDeg": row["command"], "recordedReceipt": row["receipt"], "recordedActive": row["active"],
              "recordedPrepare": row["prepare"], "recordedEpsFault": row.get("epsFault"), "recordedDiagnostic": row.get("diagnostic"),
              "inputIssues": row.get("issues", []), "recordedErrorDeg": None,
              "sampledHostBaseline": result("invalid", "missing_recorded_angles")}
    if recovery:
      record["recoveryExperiment"] = result("invalid", "missing_recorded_angles")
    if row.get("desired") is not None and row.get("rawAngle") is not None:
      record["recordedErrorDeg"] = row["desired"] - row["rawAngle"]
      record["sampledHostBaseline"] = baseline.update(row, row["desired"])
      if recovery:
        record["recoveryExperiment"] = recovery.update(row, row["desired"])
    else:
      baseline.previous = None
      if recovery:
        recovery.previous = None
    if preview:
      variants = {}
      for delay in delays:
        candidate = extractors[str(delay)].update(row, metadata, delay)
        if candidate["status"] == "candidate":
          wire = controllers[str(delay)].update(row, candidate["desiredAngleDeg"])
          candidate["hostReplay"] = wire
          group = response_group(row, previous) if row.get("speed") is not None and row.get("rawAngle") is not None else "unknown"
          model = config.get("response_model", models.get(group))
          # Gate on what the controller was ASKED to do this cycle, not on
          # `active`, which reports whether the last frame actually transmitted was
          # active. With latActive False the controller stops sending, so `active`
          # freezes at its previous True and a real inactive gap never looks like
          # one - the response hypothesis kept blending across the gap.
          if model and wire["status"] == "candidate" and wire["diagnostic"]["requestedActive"]:
            key = (row["route"], row["segment"], group)
            candidate["hypotheticalResponse"] = responses[str(delay)].update(row, wire["angleDeg"], model, key)
          else:
            responses[str(delay)].reset()
            candidate["hypotheticalResponse"] = result("unavailable", "missing_response_model_or_inactive_command")
        else:
          controllers[str(delay)].previous = None
          responses[str(delay)].reset()
        variants[str(delay)] = candidate
      record["preview"] = variants
    if speed:
      record["curveSpeed"] = curve_speed(row, metadata, config.get("curve_speed"))
    previous = row
    yield record


def provenance():
  root = Path(__file__).resolve().parents[3]
  repositories = {}
  for name, path in (("host", root), ("opendbc", root / "opendbc_repo")):
    # The openpilot package may live one directory below the actual git root.
    try:
      top = subprocess.check_output(["git", "rev-parse", "--show-toplevel"], cwd=path, text=True).strip()
      commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=top, text=True).strip()
      diff = subprocess.check_output(["git", "diff", "--binary"], cwd=top)
      stat = subprocess.check_output(["git", "diff", "--stat"], cwd=top, text=True)
      repositories[name] = {"commit": commit, "diffSha256": hashlib.sha256(diff).hexdigest(), "diffStat": stat}
    except (OSError, subprocess.CalledProcessError):
      repositories[name] = {"status": "unknown"}
  files = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
           for p in Path(__file__).parent.glob("byd_lateral_*.py")}
  dependencies = [root / p for p in ("opendbc_repo/opendbc/car/byd/carcontroller.py", "opendbc_repo/opendbc/car/byd/carstate.py",
    "opendbc_repo/opendbc/car/byd/values.py", "opendbc_repo/opendbc/safety/modes/byd.h", "opendbc_repo/opendbc/car/vehicle_model.py",
    "openpilot/selfdrive/controls/lib/drive_helpers.py", "openpilot/selfdrive/modeld/modeld.py", "openpilot/sunnypilot/modeld_v2/modeld.py",
    "openpilot/sunnypilot/livedelay/helpers.py", "openpilot/cereal/custom.capnp")]
  dependency_hashes = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in dependencies if p.is_file()}
  return {"repositories": repositories, "toolFiles": files, "dependencyFiles": dependency_hashes,
          "manifestSha256": hashlib.sha256(DEFAULT_MANIFEST.read_bytes()).hexdigest(), "deviceFirmwareIdentity": "unknown"}


def write_csv(path, records):
  counts = Counter()
  limits = Counter()
  predicted_errors = {}
  transitions = []
  last_states = {}
  active_submissions = []
  last_active = {}
  count = 0
  with path.open("w") as f:
    writer = None
    for record in records:
      location = (record["route"], record["segment"])
      if writer is None:
        writer = csv.DictWriter(f, fieldnames=list(record))
        writer.writeheader()
      writer.writerow({k: json.dumps(v, ensure_ascii=False, allow_nan=False) if isinstance(v, (dict, list)) else v for k, v in record.items()})
      for key in ("curveSpeed", "recoveryExperiment", "sampledHostBaseline"):
        if key in record:
          counts[f"{key}:{record[key]['status']}:{record[key]['reason']}"] += 1
          diagnostic = record[key].get("diagnostic")
          if diagnostic and diagnostic["submitted"]:
            if diagnostic["active"] and not last_active.get((location, key), False):
              active_submissions.append({"t": record["t"], "segment": record["segment"], "variant": key,
                                         "reason": diagnostic["entryReason"], "recoveryTimingVerified": False})
            last_active[(location, key)] = diagnostic["active"]
          if diagnostic and diagnostic["state"] != last_states.get((location, key)):
            transitions.append({"t": record["t"], "segment": record["segment"], "variant": key,
                                "state": diagnostic["state"], "reason": diagnostic["entryReason"],
                                "active": diagnostic["active"], "submitted": diagnostic["submitted"]})
            last_states[(location, key)] = diagnostic["state"]
      for delay, candidate in record.get("preview", {}).items():
        counts[f"preview[{delay}]:{candidate['status']}:{candidate['reason']}"] += 1
        wire = candidate.get("hostReplay", {})
        if wire.get("diagnostic"):
          limits[f"preview[{delay}]:mask:{wire['diagnostic']['limitMask']}"] += 1
        predicted = candidate.get("hypotheticalResponse", {}).get("predictedAngleDeg")
        if predicted is not None and record["recordedDesiredDeg"] is not None:
          predicted_errors.setdefault(delay, []).append(abs(predicted - record["recordedDesiredDeg"]))
      count += 1
  return {"rows": count, "outcomes": dict(counts), "previewLimitMasks": dict(limits),
          "replayedTransitions": transitions, "replayedFirstActiveSubmissions": active_submissions,
          "hypotheticalErrorsByDelay": {delay: {"samples": len(errors), "errorP95Deg": float(np.percentile(errors, 95))}
                                        for delay, errors in predicted_errors.items()},
          "closedLoopValidated": False}


def report_text(report):
  lines = ["# 宋 ALT 横向离线工程验证", "", "本报告来自本地历史日志；候选不接入车辆控制，controlReady 始终为 false。", "",
           "## 实测记录", "", "| 路线 | 请求数 | 发送回执 | 拒发回执 | 弯道误差 P95° | >8°持续秒 | 准备空档秒 |", "|---|---:|---:|---:|---:|---:|---:|"]
  for route in report["routes"]:
    s = route["recorded"]
    p95 = f"{s['errorP95Deg']:.2f}" if s["errorP95Deg"] is not None else "无样本"
    lines.append(f"| {route['source']} / {route['route']} | {s['requests']} | {s['receipts'].get('sent', 0)} | " +
                 f"{s['receipts'].get('rejected', 0)} | {p95} | {s['errorOver8Seconds']:.2f} | {s['prepareSecondsWhileLatActive']:.2f} |")
  lines += ["", "## 候选与辨识", "", f"运行模式：`{report['mode']}`。辨识状态：`{report['fit']['status']}`。", "",
            "| 离线结果 | 帧数 |", "|---|---:|"]
  counts = Counter()
  for route in report["routes"]:
    counts.update(route.get("candidates", {}).get("outcomes", {}))
  lines.extend(f"| {key} | {count} |" for key, count in sorted(counts.items()))
  lines += ["", "## 解释与限制", "",
            "- 实测指标针对记录中的原始目标和实测角；不使用裁剪后的候选目标降低误差。",
            "- curveWindows 是连续有效弯道采样窗口，按数据空档、失效或转向方向改变拆分；不等同于物理弯道数量。积分按前一采样值保持计算。",
            "- 缺失或不明确的回执保持未知；发送回执不证明 EPS 接受执行目标。",
            "- sampledHostBaseline 是当前控制器在 50 Hz 样本上执行两个逻辑周期的重放，不能视为历史二进制的精确复现。",
            "- 拟合仅使用未明显受主机裁剪的健康片段；未证明版本一致的路线不混合拟合。",
            "- 预瞄不支持未知/action 模型时会明确报告；没有能力参数时不计算弯道速度。",
            "- 假设响应使用记录轨迹提供输入，并非新算法的真实闭环结果；轨迹改善和 EPS 故障变化未实车验证。",
            "- 各段完整性、原始 EPS 故障帧、横摆交叉检查和参数身份见 summary.json；设备固件身份未读取。", ""]
  return "\n".join(lines)


def run(manifest, output, mode="baseline", config=None):
  if mode not in ("baseline", "fit", "preview", "curve-speed", "combined"):
    raise ValueError("invalid mode")
  config = validate_config(config or {})
  tasks = json.loads(Path(manifest).read_text())
  if not isinstance(tasks, list) or not tasks:
    raise ValueError("manifest must be a non-empty list")
  output = Path(output)
  output.mkdir(parents=True, exist_ok=True)
  if any(output.iterdir()):
    raise ValueError("output directory must be empty")
  decoded = []
  for task in tasks:
    rows, metadata = read_route(task)
    decoded.append((rows, metadata))
    print(f"Decoded {metadata['route']}: {len(rows)} requests", flush=True)
  all_rows = [row for rows, _ in decoded for row in rows]
  fitted = fit_responses(all_rows) if mode in ("fit", "combined") else {"status": "not_requested", "groups": [], "controlReady": False}
  report = {"controlReady": False, "mode": mode, "configuration": config, "provenance": provenance(), "fit": fitted, "routes": []}
  report["provenance"]["manifestSha256"] = hashlib.sha256(Path(manifest).read_bytes()).hexdigest()
  for rows, metadata in decoded:
    diagnostics = metadata.pop("diagnostics", [])
    entry = {**metadata, "recorded": summary(rows)}
    filename = f"{metadata['source']}-{metadata['route']}.csv"
    if Path(filename).name != filename:
      raise ValueError("invalid route or source name")
    entry["candidates"] = write_csv(output / filename, evaluate(rows, metadata, mode, config, fitted))
    if diagnostics:
      with (output / filename.replace(".csv", "-cycles.csv")).open("w") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(diagnostics[0]))
        writer.writeheader()
        writer.writerows(diagnostics)
    entry["diagnosticCycles"] = len(diagnostics)
    report["routes"].append(entry)
  (output / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
  (output / "fit.json").write_text(json.dumps(fitted, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
  (output / "report.md").write_text(report_text(report))
  return report


def main(argv=None):
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
  parser.add_argument("--output", type=Path, required=True)
  parser.add_argument("--mode", choices=("baseline", "fit", "preview", "curve-speed", "combined"), default="baseline")
  parser.add_argument("--config", type=Path)
  args = parser.parse_args(argv)
  config = json.loads(args.config.read_text()) if args.config else {}
  run(args.manifest, args.output, args.mode, config)


if __name__ == "__main__":
  main()
