"""Local log decoding for lateral experiments; no sockets, Params, or hardware."""

import hashlib
import math
import warnings
from bisect import bisect_left, bisect_right
from collections import defaultdict
from pathlib import Path

from openpilot.tools.lib.logreader import LogReader

FINGERPRINT = "BYD_SONG_PLUS_DMI_23_ALT"
PARAM_KEYS = {"LagdToggle", "LagdToggleDelay", "LagdValueCache", "SmartCruiseControlVision", "ModelRunnerTypeCache"}
VM_FIELDS = ("mass", "rotationalInertia", "wheelbase", "centerToFront", "steerRatioRear", "tireStiffnessFront",
             "tireStiffnessRear", "steerRatio")


def number(value):
  value = float(value)
  return value if math.isfinite(value) else None


def match_returns(rows, returns, window=0.1):
  """Require a unique, causal one-to-one match, including the physical bus."""
  by_key = defaultdict(list)
  for i, receipt in enumerate(returns):
    by_key[(receipt["bus"], receipt["hex"])].append(i)
  candidates = []
  owners = defaultdict(list)
  for row_index, row in enumerate(rows):
    found = [i for i in by_key[(row["bus"], row["hex"])] if 0 <= returns[i]["t"] - row["t"] <= window]
    candidates.append(found)
    for i in found:
      owners[i].append(row_index)
  for row, found in zip(rows, candidates, strict=True):
    row["receipt"] = "unknown"
    if len(found) == 1 and len(owners[found[0]]) == 1:
      receipt = returns[found[0]]
      row["receipt"] = receipt["kind"]
      row["receiptTime"] = receipt["t"]
    elif found:
      row["receipt"] = "ambiguous"


def decode_events(events, route, segment, t0=0., metadata=None):
  from openpilot.selfdrive.locationd.helpers import Pose, PoseCalibrator

  metadata = {} if metadata is None else metadata
  metadata.setdefault("firmwareIdentity", "unknown")
  metadata.setdefault("modelIdentity", "unknown")
  rows, returns, diagnostics = [], [], []
  state = {}
  calibrator = PoseCalibrator()
  previous_times = {}
  for msg in events:
    t = msg.logMonoTime * 1e-9 - t0
    kind = msg.which()
    if t < previous_times.get(kind, -math.inf):
      metadata.setdefault("issues", []).append(f"nonmonotonic_{kind}")
      continue
    previous_times[kind] = t
    if kind == "initData":
      init = msg.initData
      metadata.update(version=str(init.version), commit=str(init.gitCommit), dirty=bool(init.dirty))
      params = {}
      model_parts = []
      for entry in init.params.entries:
        key = str(entry.key)
        if key in PARAM_KEYS:
          params[key] = bytes(entry.value).decode("utf-8", errors="replace")
        if "Bundle" in key and "Model" in key:
          model_parts.append((key, hashlib.sha256(bytes(entry.value)).hexdigest()))
      metadata["parameters"] = params
      if model_parts:
        metadata["modelIdentity"] = hashlib.sha256(repr(sorted(model_parts)).encode()).hexdigest()
    elif kind == "carParams":
      cp = msg.carParams
      metadata.update(fingerprint=str(cp.carFingerprint), openpilotLongitudinalControl=bool(cp.openpilotLongitudinalControl),
                      steerActuatorDelay=float(cp.steerActuatorDelay), vm={k: float(getattr(cp, k)) for k in VM_FIELDS})
    elif kind == "carState":
      cs = msg.carState
      state.update(stateTime=t, stateValid=bool(msg.valid), speed=number(cs.vEgo), speedRaw=number(cs.vEgoRaw), measured=number(cs.steeringAngleDeg),
                   aEgo=number(cs.aEgo),
                   driverPressed=bool(cs.steeringPressed), driverTorque=number(cs.steeringTorque), canValid=bool(cs.canValid),
                   fault=bool(cs.steerFaultTemporary or cs.steerFaultPermanent), brake=bool(cs.brakePressed),
                   cruiseEnabled=bool(cs.cruiseState.enabled))
    elif kind == "carControl":
      cc = msg.carControl
      state.update(controlTime=t, controlValid=bool(msg.valid), desired=number(cc.actuators.steeringAngleDeg), latActive=bool(cc.latActive),
                   longActive=bool(cc.longActive), controlEnabled=bool(cc.enabled), requestedCurvature=number(cc.actuators.curvature))
    elif kind == "carControlSP":
      mads = msg.carControlSP.mads
      state.update(controlSPTime=t, controlSPValid=bool(msg.valid), madsActive=bool(mads.active),
                   madsAvailable=bool(mads.available), madsEnabled=bool(mads.enabled))
    elif kind == "carStateSP" and msg.carStateSP.bydLateral.valid:
      diagnostic = msg.carStateSP.bydLateral.to_dict()
      diagnostics.append({**diagnostic, "route": route, "segment": segment,
                          "t": diagnostic["sampleMonoTime"] * 1e-9 - t0, "publishedTime": t})
    elif kind == "modelDataV2SP" and msg.modelDataV2SP.lateralDelayDebug.valid:
      state["delayDebug"] = msg.modelDataV2SP.lateralDelayDebug.to_dict()
      state["delayDebug"]["valid"] &= bool(msg.valid)
      state["delayDebugTime"] = t
    elif kind == "lateralDelay":
      state.update(publishedDelay=number(msg.lateralDelay.lateralDelay), delayTime=t, delayStatus=str(msg.lateralDelay.status))
    elif kind in ("modelV2", "drivingModelData"):
      model = getattr(msg, kind)
      state.update(actionTime=t, modelCurvature=number(model.action.desiredCurvature))
      if kind == "modelV2":
        state["plan"] = {"t": list(model.orientation.t), "yaw": list(model.orientation.z),
                         "yawRate": list(model.orientationRate.z), "speed": list(model.velocity.x),
                         "x": list(model.position.x), "y": list(model.position.y)}
        state.update(planTime=t, planFrameId=int(model.frameId), planValid=bool(msg.valid))
    elif kind == "vehicleParameters":
      v = msg.vehicleParameters
      state["vehicleParameters"] = {"stiffness": number(v.stiffnessFactor), "steerRatio": number(v.steerRatio),
                                    "roll": number(v.roll), "offset": number(v.angleOffsetDeg), "time": t, "valid": bool(msg.valid)}
    elif kind == "pandaStates" and len(msg.pandaStates) == 1:
      p = msg.pandaStates[0]
      state.update(pandaTime=t, pandaValid=bool(msg.valid), pandaAllowed=bool(p.controlsAllowed or p.controlsAllowedLateral),
                   txBlocked=int(p.safetyTxBlocked), pandaFault=bool(p.faults or p.safetyRxChecksInvalid))
    elif kind == "extrinsicsCalibration":
      calibrator.feed_extrinsics_calibration(msg.extrinsicsCalibration)
    elif kind == "deviceMotion":
      motion = msg.deviceMotion
      if motion.inputsOK and motion.posenetOK and motion.angularVelocityDevice.valid and calibrator.calib_valid:
        pose = calibrator.build_calibrated_pose(Pose.from_device_motion(motion))
        state.update(imuYaw=number(pose.angular_velocity.yaw), imuTime=t)
    elif kind == "can":
      for packet in msg.can:
        data = bytes(packet.dat)
        if packet.src == 0 and packet.address == 0x11F and len(data) == 5:
          state.update(rawAngle=int.from_bytes(data[:2], "little", signed=True) / 10, angleTime=t)
        elif packet.src == 0 and packet.address == 0x1FC and len(data) == 8:
          torque = (int.from_bytes(data, "little") >> 4) & 0xFFF
          state.update(rawDriver=torque - 4096 if torque >= 2048 else torque,
                       epsFault=(data[5] >> 6) & 3, epsPrepare=bool(data[0] & 1), epsAngleState=bool(data[0] & 2),
                       epsBit45=bool(data[5] & 0x20), epsCounter=data[6] >> 4, epsTime=t)
          if state["epsFault"]:
            metadata["rawEPSFaultFrames"] = metadata.get("rawEPSFaultFrames", 0) + 1
        elif packet.src == 0 and packet.address == 0x222 and len(data) == 8:
          state.update(canYaw=(int.from_bytes(data[:2], "little") & 0xFFF) * 0.002132603 - 2.094216146, yawTime=t)
        elif packet.address == 0x1E2 and len(data) == 8:
          if packet.src in (128, 130, 192, 194):
            returns.append({"t": t, "hex": data.hex(), "bus": packet.src & 0x3F,
                            "kind": "rejected" if packet.src & 0x40 else "sent"})
          elif packet.src == 2:
            state.update(template=data.hex(), templateTime=t)
    elif kind == "sendcan":
      for packet in msg.sendcan:
        data = bytes(packet.dat)
        if packet.address != 0x1E2 or len(data) != 8:
          continue
        row = {**state, "route": route, "segment": segment, "t": t, "bus": int(packet.src), "hex": data.hex(),
               "command": int.from_bytes(data[3:5], "little", signed=True) / 10,
               "active": bool(data[2] & 0x20), "prepare": bool(data[2] & 0x10)}
        row["issues"] = [f"missing_or_stale_{key}" for key in ("stateTime", "controlTime", "angleTime", "epsTime")
                         if not 0 <= t - state.get(key, -math.inf) <= 0.06]
        for key in ("speed", "speedRaw", "desired", "rawAngle", "rawDriver", "driverTorque", "aEgo"):
          if row.get(key) is None:
            row["issues"].append(f"missing_{key}")
        for key in ("stateValid", "controlValid"):
          if not row.get(key):
            row["issues"].append(f"invalid_{key}")
        row["clean"] = (not row["issues"] and row.get("canValid", False) and not row.get("fault", True)
                        and not row.get("driverPressed", True) and not row.get("epsFault", 1)
                        and abs(row.get("rawDriver", 9999)) < 80 and row.get("speed", 0) > 2)
        rows.append(row)
  match_returns(rows, returns)
  # Diagnostics are published on the next card cycle. Match their original
  # sample time, never the publication time or the next cycle's measurements.
  submissions = [d for d in diagnostics if d["submitted"]]
  times = [r["t"] for r in rows]
  for diagnostic in submissions:
    candidates = rows[bisect_left(times, diagnostic["t"]):bisect_right(times, diagnostic["t"] + .015)]
    if len(candidates) == 1 and "diagnostic" not in candidates[0]:
      candidates[0]["diagnostic"] = diagnostic
  metadata.setdefault("diagnostics", []).extend(diagnostics)
  for row in rows:
    row["fitEligible"] = bool(row["clean"] and row["active"] and row["receipt"] == "sent"
                             and row.get("latActive") and not row.get("pandaFault", True) and row.get("pandaValid")
                             and 0 <= row["t"] - row.get("pandaTime", -math.inf) <= 1.5)
  return rows, metadata


def read_route(task):
  root, route = Path(task["root"]), task["route"]
  log_type = task.get("logType", "rlog")
  if log_type not in ("rlog", "qlog"):
    raise ValueError("logType must be rlog or qlog")
  rows, metadata = [], {"route": route, "source": task.get("source", "unknown"), "logType": log_type, "segments": [], "issues": []}
  for segment in task["segments"]:
    path = root / f"{route}--{segment}" / f"{log_type}.zst"
    if not path.is_file():
      metadata["segments"].append({"segment": segment, "status": "missing", "path": str(path)})
      continue
    decoded = []
    failure = None
    with warnings.catch_warnings(record=True) as caught:
      warnings.simplefilter("always")
      try:
        events = LogReader(str(path), sort_by_time=False, only_union_types=True)
        decoded, metadata = decode_events(events, route, segment, task.get("t0", 0), metadata)
      except Exception as exc:
        failure = f"{type(exc).__name__}: {exc}"
    partial = bool(caught or failure)
    if metadata.get("fingerprint") != FINGERPRINT:
      failure = "missing_or_wrong_fingerprint"
      partial = True
    for row in decoded:
      row["segmentComplete"] = not partial
      row["fitEligible"] &= not partial and log_type == "rlog"
      if log_type == "qlog":
        row["issues"].append("qlog_has_no_full_rate_guarantee")
    rows.extend(decoded)
    metadata["segments"].append({"segment": segment, "status": "incomplete" if partial else "complete", "path": str(path),
                                 "warnings": [str(w.message) for w in caught], "error": failure, "requests": len(decoded),
                                 "identity": {k: metadata.get(k, "unknown") for k in ("commit", "dirty", "modelIdentity", "parameters")}})
  # Dirty commits cannot establish equality of the executed code. Cohorts must
  # be explicitly supplied with supporting provenance, or remain route-local.
  cohort = task.get("cohort") if task.get("cohortEvidence") else None
  metadata["cohort"] = cohort or f"unverified:{route}"
  metadata["cohortEvidence"] = task.get("cohortEvidence", "unknown")
  for row in rows:
    row["cohort"] = metadata["cohort"]
  return rows, metadata
