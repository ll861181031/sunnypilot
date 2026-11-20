import ast
import csv
import json
import math
from copy import deepcopy
from pathlib import Path

import numpy as np
import pytest

from opendbc.can import CANPacker
from opendbc.car.byd import carcontroller
from openpilot.cereal import messaging
from openpilot.tools.car_porting import byd_lateral_data as data
from openpilot.tools.car_porting import byd_lateral_replay as replay
from openpilot.tools.car_porting.byd_lateral_candidates import HostReplay, PreviewReplay, curve_speed, preview_target
from openpilot.tools.car_porting.byd_lateral_fit import fit_responses


def inputs(curvature=0., speed=10.):
  t = np.linspace(0., 6., 33)
  k = np.broadcast_to(curvature, t.shape).copy()
  row = {"route": "test", "segment": 0, "t": 10., "bus": 0, "hex": "f43128000000a00c", "command": 0.,
         "active": True, "prepare": False, "receipt": "sent", "desired": 0., "rawAngle": 0., "rawDriver": 0,
         "driverTorque": 0., "driverPressed": False, "speed": speed, "speedRaw": speed, "aEgo": 0., "issues": [],
         "canValid": True, "fault": False, "epsFault": 0, "pandaAllowed": True, "pandaFault": False,
         "pandaValid": True, "pandaTime": 10., "controlSPValid": True, "controlSPTime": 10.,
         "cruiseEnabled": True, "controlEnabled": True, "latActive": True, "longActive": False,
         "template": "f43118160000a00c", "templateTime": 10., "epsCounter": 0,
         "plan": {"t": t, "yaw": np.cumsum(k * speed) * (t[1] - t[0]), "yawRate": k * speed,
                  "speed": np.full(33, speed), "x": speed * t, "y": np.zeros(33)},
         "planTime": 10., "planFrameId": 42, "planValid": True,
         "vehicleParameters": {"valid": True, "time": 10., "stiffness": 1., "steerRatio": 15., "roll": 0., "offset": 0.},
         "delayDebugTime": 10., "delayDebug": {"valid": True, "modelOutputType": "plan", "actionExtractor": "planYawV1",
            "frameId": 42, "actionTime": .475, "effectiveDelay": .3, "modelSpeed": speed,
            "smoothSeconds": .1, "previousCurvature": .002, "curvatureSmoothing": True, "minControlSpeed": .3}}
  meta = {"route": "test", "source": "synthetic", "openpilotLongitudinalControl": True,
          "segments": [], "vm": {"mass": 2136., "rotationalInertia": 3830., "wheelbase": 2.765,
             "centerToFront": 1.3825, "steerRatioRear": 0., "tireStiffnessFront": 163761.,
             "tireStiffnessRear": 258873., "steerRatio": 15.}}
  return row, meta


def capabilities(**overrides):
  return {"lateral_accel_max": 1.5, "steer_rate_max_deg_s": 30., "decel_max": 3., "accel_max": 1.,
          "jerk_max": 100., "cruise_speed_mps": 10., **overrides}


@pytest.mark.parametrize("receipts,expected", [
  ([{"t": 1.02, "bus": 0, "hex": "abcd", "kind": "sent"}], "sent"),
  ([{"t": 1.02, "bus": 0, "hex": "abcd", "kind": "rejected"}], "rejected"),
  ([{"t": 1.02, "bus": 2, "hex": "abcd", "kind": "sent"}], "unknown"),
  ([{"t": 1.02, "bus": 0, "hex": "abce", "kind": "sent"}], "unknown"),
  ([{"t": .99, "bus": 0, "hex": "abcd", "kind": "sent"}], "unknown"),
  ([{"t": 1.11, "bus": 0, "hex": "abcd", "kind": "sent"}], "unknown"),
  ([{"t": 1.02, "bus": 0, "hex": "abcd", "kind": "sent"}] * 2, "ambiguous"),
  ([], "unknown"),
])
def test_exact_causal_receipts(receipts, expected):
  rows = [{"t": 1., "bus": 0, "hex": "abcd"}]
  data.match_returns(rows, receipts)
  assert rows[0]["receipt"] == expected


def test_receipt_cannot_belong_to_two_requests():
  rows = [{"t": 1. + i * .02, "bus": 0, "hex": "same"} for i in range(2)]
  data.match_returns(rows, [{"t": 1.03, "bus": 0, "hex": "same", "kind": "sent"}])
  assert all(r["receipt"] == "ambiguous" for r in rows)


def event(kind, t, size=None):
  msg = messaging.new_message(kind, size=size)
  msg.valid = True
  msg.logMonoTime = int(t * 1e9)
  return msg


def packets(kind, t, frames):
  msg = event(kind, t, len(frames))
  for packet, (address, payload, bus) in zip(getattr(msg, kind), frames, strict=True):
    packet.address, packet.dat, packet.src = address, payload, bus
  return msg


def test_qlog_action_compatibility_missing_inputs_and_raw_can():
  packer = CANPacker("byd_song_plus_dmi_23_alt")
  raw = [packer.make_can_msg("STEER_MODULE", 0, {"STEER_ANGLE": -12.3}),
         packer.make_can_msg("STEERING_TORQUE_ANGLE", 0, {"Steer_Torque_Sensor": -6.5, "TORQUE_TEMP_FAILED": 2, "COUNTER": 3}),
         packer.make_can_msg("ACCELEROMETER", 0, {"YAW_RATE": .1})]
  model = event("drivingModelData", 1.)
  model.drivingModelData.action.desiredCurvature = .012
  request = (0x1E2, bytes.fromhex("f43128fcff00f0c7"), 0)
  rows, meta = data.decode_events([model, packets("can", 1.01, raw), packets("sendcan", 1.02, [request]),
                                  packets("can", 1.04, [(request[0], request[1], 128)])], "test", 0)
  assert len(rows) == 1 and rows[0]["modelCurvature"] == pytest.approx(.012)
  assert rows[0]["rawAngle"] == pytest.approx(-12.3)
  assert rows[0]["rawDriver"] == -65
  assert rows[0]["epsFault"] == 2 and meta["rawEPSFaultFrames"] == 1
  assert rows[0]["receipt"] == "sent" and not rows[0]["fitEligible"]
  assert "missing_or_stale_stateTime" in rows[0]["issues"]
  assert "plan" not in rows[0]


def test_diagnostic_uses_original_cycle_time():
  request = (0x1E2, bytes.fromhex("f43128fcff00f0c7"), 0)
  diagnostic = event("carStateSP", 1.03)
  diagnostic.carStateSP.bydLateral = {"valid": True, "sampleMonoTime": 1_015_000_000, "submitted": True, "frame": 7}
  rows, meta = data.decode_events([packets("sendcan", 1.02, [request]), diagnostic], "test", 0)
  assert rows[0]["diagnostic"]["frame"] == 7
  assert meta["diagnostics"][0]["t"] == pytest.approx(1.015)
  assert meta["diagnostics"][0]["publishedTime"] == pytest.approx(1.03)


def test_bad_time_and_nonfinite_fields_are_marked():
  later, earlier = event("carState", 2.), event("carState", 1.)
  later.carState.vEgo = math.nan
  rows, meta = data.decode_events([later, earlier, packets("sendcan", 2.02, [(0x1E2, bytes(8), 0)])], "test", 0)
  assert "nonmonotonic_carState" in meta["issues"]
  assert "missing_speed" in rows[0]["issues"]
  assert not rows[0]["clean"]


def test_truncated_and_missing_segments_are_not_fit_eligible(tmp_path, monkeypatch):
  path = tmp_path / "test--0"
  path.mkdir()
  (path / "rlog.zst").write_bytes(b"invalid")
  def truncated(*args, **kwargs):
    import warnings
    warnings.warn("truncated", stacklevel=2)
    return []
  monkeypatch.setattr(data, "LogReader", truncated)
  _, meta = data.read_route({"root": str(tmp_path), "route": "test", "segments": [0, 1]})
  assert [s["status"] for s in meta["segments"]] == ["incomplete", "missing"]


@pytest.mark.parametrize("log_type", ["rlog", "qlog"])
def test_qlog_can_screen_but_cannot_identify_or_replay_control(tmp_path, monkeypatch, log_type):
  path = tmp_path / "test--0"
  path.mkdir()
  (path / f"{log_type}.zst").write_bytes(b"synthetic")
  row, meta = inputs()
  row.update(fitEligible=True, clean=True)
  monkeypatch.setattr(data, "LogReader", lambda *a, **kw: [])
  monkeypatch.setattr(data, "decode_events", lambda *a: ([deepcopy(row)], {**a[-1], "fingerprint": data.FINGERPRINT}))
  rows, _ = data.read_route({"root": str(tmp_path), "route": "test", "segments": [0], "logType": log_type})
  assert rows[0]["fitEligible"] == (log_type == "rlog")
  assert HostReplay().update(rows[0], 10.)["status"] == ("candidate" if log_type == "rlog" else "invalid")


def synthetic_response(routes=3, cohort="verified", taus=None):
  rows = []
  dt, lag, alpha = .02, 5, math.exp(-.02 / .2)
  for route in range(routes):
    if taus is not None:
      alpha = math.exp(-dt / taus[route])
    t = np.arange(0, 20, dt)
    u = 5 + 2 * np.sin(.8 * t + route * .2)
    y = np.full(len(t), u[0])
    for i in range(len(t) - 1):
      y[i + 1] = alpha * y[i] + (1 - alpha) * u[max(0, i - lag)]
    rows.extend({"route": str(route), "cohort": cohort, "segment": 0, "t": time, "speed": 10.,
                 "command": command, "desired": command, "rawAngle": angle, "fitEligible": True}
                for time, command, angle in zip(t, u, y, strict=True))
  return rows


def test_response_fit_uses_whole_route_holdouts():
  fitted = fit_responses(synthetic_response())
  assert fitted["status"] == "hypotheses"
  model = next(g for g in fitted["groups"] if g["status"] == "hypothesis")
  assert model["model"]["delaySeconds"] == pytest.approx(.1, abs=.021)
  assert model["model"]["timeConstantSeconds"] == pytest.approx(.2, abs=.03)
  assert {f["heldOutRoute"] for f in model["holdouts"]} == {"0", "1", "2"}
  assert not fitted["controlReady"]


def test_fit_accepts_valid_data_after_a_missing_measurement():
  rows = synthetic_response()
  missing = {**rows[0], "fitEligible": False}
  del missing["rawAngle"]
  assert fit_responses([missing, *rows])["status"] == "hypotheses"


def test_response_fit_rejects_inconsistent_route_responses():
  outcome = fit_responses(synthetic_response(taus=(.04, .2, .6)))
  assert outcome["status"] == "unidentifiable"
  assert any(g.get("stability", {}).get("timeConstantRatio", 0) > 2. for g in outcome["groups"])


@pytest.mark.parametrize("kind", ["single_route", "unknown_version", "no_excitation", "bad_response"])
def test_response_fit_rejects_insufficient_or_unhelpful_data(kind):
  rows = synthetic_response(1 if kind == "single_route" else 3, "unverified:test" if kind == "unknown_version" else "verified")
  if kind == "no_excitation":
    for r in rows:
      r["command"] = r["desired"] = r["rawAngle"] = 5.
  if kind == "bad_response":
    for r in rows:
      r["rawAngle"] = 0.
  assert fit_responses(rows)["status"] == "unidentifiable"


def test_preview_preserves_recorded_time_and_smoothing():
  row, meta = inputs(.01)
  baseline = preview_target(row, meta)
  assert baseline["status"] == "candidate"
  assert baseline["actionTime"] == pytest.approx(.475)
  assert baseline["curvatureSmoothingReplayed"]
  candidate = preview_target(row, meta, .5)
  assert candidate["actionTime"] == pytest.approx(.675)
  assert not candidate["controlReady"]


def test_preview_smooths_once_per_model_frame_and_recurs_on_candidate_state():
  row, meta = inputs(.01)
  runner = PreviewReplay()
  first = runner.update(row, meta, .5)
  same = deepcopy(row)
  same["t"] += .02
  repeated = runner.update(same, meta, .5)
  assert repeated["curvature"] == first["curvature"]
  next_frame = deepcopy(same)
  next_frame["t"] += .04
  next_frame["delayDebugTime"] = next_frame["planTime"] = next_frame["t"]
  next_frame["planFrameId"] += 1
  next_frame["delayDebug"]["frameId"] += 1
  next_frame["plan"]["yaw"] *= 2
  next_frame["plan"]["yawRate"] *= 2
  expected_input = deepcopy(next_frame)
  expected_input["delayDebug"]["previousCurvature"] = first["curvature"]
  expected = preview_target(expected_input, meta, .5)
  outcome = runner.update(next_frame, meta, .5)
  assert outcome["curvature"] == expected["curvature"]
  assert outcome["curvature"] != preview_target(next_frame, meta, .5)["curvature"]


@pytest.mark.parametrize("issue,reason", [
  ("action", "action_head_requires_model_input_replay"), ("unknown", "unknown_model_output_type"),
  ("extractor", "unreplayable_action_extractor"), ("frame", "plan_and_action_frame_mismatch"),
  ("stale", "unknown_model_output_type"), ("plan_stale", "missing_or_stale_full_plan"),
])
def test_preview_rejects_nonreplayable_data(issue, reason):
  row, meta = inputs()
  if issue in ("action", "unknown"):
    row["delayDebug"]["modelOutputType"] = issue
  elif issue == "extractor":
    row["delayDebug"]["actionExtractor"] = "unsupported"
  elif issue == "frame":
    row["planFrameId"] = 41
  elif issue == "stale":
    row["delayDebugTime"] = 9.
  else:
    row["planTime"] = 9.
  assert preview_target(row, meta)["reason"] == reason


def test_recovery_experiment_does_not_modify_online_constant():
  original = carcontroller.EPS_REARM_HOLD_NS
  experiment = HostReplay(.25)
  assert experiment.controller_type.update.__globals__["EPS_REARM_HOLD_NS"] == 250_000_000
  assert carcontroller.EPS_REARM_HOLD_NS == original == 2_000_000_000
  row, _ = inputs()
  actual = HostReplay()
  for i in range(10):
    row.update(t=10. + .02 * i, controlSPTime=10. + .02 * i, pandaTime=10. + .02 * i, templateTime=10. + .02 * i)
    outcome = actual.update(row, 100.)
    assert outcome["status"] == "candidate"
    assert abs(outcome["angleDeg"] - row["rawAngle"]) <= 5.5


def test_controller_replay_fails_closed_for_missing_inputs():
  row, _ = inputs()
  row["speedRaw"] = None
  assert HostReplay().update(row, 100.)["status"] == "invalid"


def test_straight_has_no_curve_speed_limit():
  row, meta = inputs()
  outcome = curve_speed(row, meta, capabilities())
  assert outcome["status"] == "candidate"
  assert outcome["speedProfileMps"] == [10.] * 33
  assert set(outcome["constraint"]) == {"cruise"}
  assert not outcome["recordedLongitudinalAuthority"]
  row["longActive"] = True
  assert curve_speed(row, meta, capabilities())["recordedLongitudinalAuthority"]


@pytest.mark.parametrize("reverse", [False, True])
def test_curve_and_reverse_curve_enforce_angle_establishment(reverse):
  curvature = np.linspace(0., .04, 33)
  if reverse:
    curvature = .04 * np.sin(np.linspace(0., 2 * math.pi, 33))
  row, meta = inputs(curvature, speed=3.)
  outcome = curve_speed(row, meta, capabilities(steer_rate_max_deg_s=5., jerk_max=1000.))
  assert outcome["status"] in ("candidate", "infeasible")
  assert min(outcome["speedCapsMps"]) < 10.
  assert "steerRate" in outcome["constraint"]
  assert not outcome["controlReady"]


@pytest.mark.parametrize("direction", [-1, 1])
def test_future_curve_has_a_reachable_profile_with_a_steer_rate_constraint(direction):
  row, meta = inputs(direction * np.r_[np.zeros(10), np.linspace(0., .02, 23)], speed=3.)
  params = capabilities(steer_rate_max_deg_s=30., jerk_max=1000.)
  outcome = curve_speed(row, meta, params)
  assert outcome["status"] == "candidate"
  assert "steerRate" in outcome["constraint"]
  assert min(outcome["speedCapsMps"]) < params["cruise_speed_mps"]
  assert max(abs(x) for x in outcome["steeringRateDegS"]) <= params["steer_rate_max_deg_s"]
  assert min(outcome["accelerationMps2"]) >= -params["decel_max"] - 1e-6
  assert max(outcome["accelerationMps2"]) <= params["accel_max"] + 1e-6


def test_rate_must_be_rechecked_at_the_candidate_speed():
  row, meta = inputs(np.linspace(0., .04, 33), speed=3.)
  outcome = curve_speed(row, meta, capabilities(steer_rate_max_deg_s=30., jerk_max=1000.))
  assert outcome["reason"] == "steer_rate_at_candidate_speed"
  assert max(abs(x) for x in outcome["steeringRateDegS"]) > 30.


def test_speed_without_capability_or_with_bad_plan_has_no_result():
  row, meta = inputs()
  assert curve_speed(row, meta, None)["status"] == "unavailable"
  row["plan"]["t"][5] = row["plan"]["t"][4]
  assert curve_speed(row, meta, capabilities())["reason"] == "nonmonotonic_plan_time"


def test_unreachable_speed_and_jerk_are_rejected():
  row, meta = inputs(.05)
  assert curve_speed(row, meta, capabilities())["reason"] == "insufficient_braking_distance"
  row, meta = inputs()
  row["aEgo"] = 1.
  assert curve_speed(row, meta, capabilities(jerk_max=.1))["reason"] == "jerk_limit"


def test_csv_missing_first_frame_does_not_change_columns(tmp_path):
  row, meta = inputs()
  missing = {**row, "desired": None}
  replay.write_csv(tmp_path / "rows.csv", replay.evaluate([missing, row], meta, "combined", {}))
  with (tmp_path / "rows.csv").open() as f:
    records = list(csv.DictReader(f))
  assert len(records) == 2
  assert json.loads(records[0]["sampledHostBaseline"])["status"] == "invalid"


@pytest.mark.parametrize("missing", ["desired", "rawAngle", "planValid"])
def test_replay_does_not_bridge_short_missing_input_gaps(missing):
  row, meta = inputs(.01)
  rows = [deepcopy(row) for _ in range(5)]
  for i, sample in enumerate(rows):
    sample["t"] += .02 * i
    sample["controlSPTime"] = sample["t"]
  rows[3][missing] = False if missing == "planValid" else None
  records = list(replay.evaluate(rows, meta, "preview", {"recovery_hold_seconds": .5}))
  if missing == "planValid":
    assert records[-1]["preview"]["None"]["hostReplay"]["diagnostic"]["frame"] == 0
  else:
    assert records[-1]["sampledHostBaseline"]["diagnostic"]["frame"] == 0
    assert records[-1]["recoveryExperiment"]["diagnostic"]["frame"] == 0


@pytest.mark.parametrize("gap", ["inactive", "unsupported"])
def test_response_hypothesis_restarts_after_unmodelled_gap(gap):
  row, meta = inputs(.01)
  rows = [deepcopy(row) for _ in range(6)]
  for i, sample in enumerate(rows):
    sample["t"] += .02 * i
    sample["controlSPTime"] = sample["t"]
  if gap == "inactive":
    rows[3]["latActive"] = False
  else:
    rows[3]["planValid"] = False
  records = list(replay.evaluate(rows, meta, "preview", {"response_model": {"delaySeconds": 0., "timeConstantSeconds": .1}}))
  assert records[2]["preview"]["None"]["hypotheticalResponse"]["predictedAngleDeg"] != row["rawAngle"]
  # The restart lands on the first row AFTER the gap, for both kinds of gap.
  # "unsupported" used to need index 5 because the response gate read
  # `diagnostic.active` - the state of the last frame that actually went out. After
  # an invalid row the controller resets and sends nothing, so `active` stayed
  # False for one extra cycle and looked like a second gap. The gate now reads
  # `requestedActive` (what this cycle was asked to do), so the phantom row is gone
  # and both parametrisations restart at the same offset.
  resumed = records[4]["preview"]["None"]["hypotheticalResponse"]
  assert resumed["predictedAngleDeg"] == row["rawAngle"]


def test_window_integrals_use_the_same_samples_as_global_metrics():
  rows = []
  for i, (error, curvature, prepare) in enumerate([(2., .01, True), (10., .01, True), (5., -.01, False), (0., 0., False)]):
    rows.append({"route": "test", "segment": 0, "t": 1. + .02 * i, "clean": True, "latActive": True,
                 "requestedCurvature": curvature, "speed": 10., "desired": error, "rawAngle": 0.,
                 "prepare": prepare, "active": not prepare, "receipt": "sent"})
  outcome = replay.summary(rows)
  assert outcome["errorIntegralDegSeconds"] == pytest.approx(.34)
  assert sum(w["errorIntegralDegSeconds"] for w in outcome["curveWindows"]) == pytest.approx(.34)
  assert outcome["errorOver8Seconds"] == pytest.approx(.02)
  assert sum(w["errorOver8Seconds"] for w in outcome["curveWindows"]) == pytest.approx(.02)
  assert outcome["prepareSecondsWhileLatActive"] == pytest.approx(.04)
  assert outcome["prepareWindows"][0]["firstActiveSubmissionTime"] == pytest.approx(1.04)


def test_hypothetical_errors_are_not_mixed_between_delays(tmp_path):
  variants = {"0.3": {"status": "candidate", "reason": "test", "hypotheticalResponse": {"predictedAngleDeg": 1.}},
              "0.5": {"status": "candidate", "reason": "test", "hypotheticalResponse": {"predictedAngleDeg": 10.}}}
  record = {"route": "test", "segment": 0, "t": 1., "recordedDesiredDeg": 0., "preview": variants}
  outcome = replay.write_csv(tmp_path / "rows.csv", [record])
  assert outcome["hypotheticalErrorsByDelay"]["0.3"]["errorP95Deg"] == 1.
  assert outcome["hypotheticalErrorsByDelay"]["0.5"]["errorP95Deg"] == 10.


@pytest.mark.parametrize("mode", ["baseline", "fit", "preview", "curve-speed", "combined"])
def test_all_cli_modes_are_isolated(tmp_path, monkeypatch, mode):
  from openpilot.common.params import Params
  def forbidden(*args, **kwargs):
    raise AssertionError("live I/O used by offline experiment")
  monkeypatch.setattr(messaging, "PubMaster", forbidden)
  monkeypatch.setattr(Params, "put", forbidden)
  monkeypatch.setattr(Params, "put_bool", forbidden)
  row, meta = inputs()
  monkeypatch.setattr(replay, "read_route", lambda task: ([deepcopy(row)], deepcopy(meta)))
  monkeypatch.setattr(replay, "provenance", lambda: {"deviceFirmwareIdentity": "unknown"})
  manifest = tmp_path / "manifest.json"
  manifest.write_text('[{"route":"test"}]')
  output = tmp_path / "output"
  replay.main(["--manifest", str(manifest), "--output", str(output), "--mode", mode])
  report = json.loads((output / "summary.json").read_text())
  assert not report["controlReady"] and report["mode"] == mode
  assert (output / "report.md").is_file()
  assert len(list(output.glob("*.csv"))) == 1


@pytest.mark.parametrize("config", [{"unknown": True}, {"preview_delays_s": [float("nan")]},
                                   {"response_model": None}, {"curve_speed": []}, {"recovery_hold_seconds": -.1}])
def test_experiment_config_errors_are_explicit(config):
  with pytest.raises(ValueError):
    replay.validate_config(config)


def test_no_online_import_or_hardware_transport():
  paths = [Path(__file__).parents[1] / f"byd_lateral_{name}.py" for name in ("data", "fit", "candidates", "replay")]
  for path in paths:
    calls = [node for node in ast.walk(ast.parse(path.read_text())) if isinstance(node, ast.Call)]
    assert not any(getattr(node.func, "id", getattr(node.func, "attr", "")) in ("PubMaster", "Panda", "Params") for node in calls)
  root = Path(__file__).resolve().parents[3]
  for path in (root / "system/manager/process_config.py", root / "selfdrive/car/card.py"):
    assert "byd_lateral_replay" not in path.read_text()
