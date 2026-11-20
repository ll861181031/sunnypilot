"""Offline A/B for the SCC-V curve controller and the radard lead hold.

Replays recorded rlogs through the real production code paths
(`SmartCruiseControlVision`, the radard probability filter and `LeadHold`) and prints
old / current / aggressive-preset numbers side by side. Nothing is started and no CAN
is touched.

Usage (from the repo root, with the project venv):

  PYTHONPATH=/data/sunnypilot .venv/bin/python -m openpilot.tools.car_porting.byd_scc_lead_ab \
      000001a1--9bd82bd837 000001a2--95e74180e0

`SCC-V demand` counts frames where the controller was active and asked for at least
0.3 m/s slower than the car was going - i.e. frames where the feature would actually
brake. `lead drops` counts present->absent transitions; the replay reproduces the
logged `radarState.leadOne.present` exactly (sanity line), so its before/after delta is
the hold's effect and nothing else.
"""

import contextlib
import glob
import os
import sys
from types import SimpleNamespace

import numpy as np

from openpilot.common.constants import CV
from openpilot.common.filter_simple import FirstOrderFilter
from openpilot.common.realtime import DT_MDL
from openpilot.cereal import custom
from openpilot.selfdrive.controls.radard import RADAR_TO_CAMERA, LeadHold
from openpilot.sunnypilot.selfdrive.controls.lib.smart_cruise_control import vision_controller as vc
from openpilot.sunnypilot.selfdrive.controls.lib.smart_cruise_control.vision_controller import SmartCruiseControlVision
from openpilot.tools.lib.logreader import LogReader

VisionState = custom.LongitudinalPlanSP.SmartCruiseControl.VisionState
REALDATA = os.environ.get("REALDATA", "/home/lsr/.comma/media/0/realdata")

OLD = {
  "_ENTERING_PRED_LAT_ACC_TH": 1.3,
  "_ABORT_ENTERING_PRED_LAT_ACC_TH": 1.1,
  "_TURNING_LAT_ACC_TH": 1.6,
  "_LEAVING_LAT_ACC_TH": 1.3,
  "_FINISH_LAT_ACC_TH": 1.1,
  "_A_LAT_REG_MAX": 2.0,
  "_ENTERING_SMOOTH_DECEL_V": [-0.2, -1.0],
  "_ENTERING_SMOOTH_DECEL_BP": [1.3, 3.0],
  "MIN_V": 20 * CV.KPH_TO_MS,
}

# The tuned values currently in the tree.
NEW = {
  "_ENTERING_PRED_LAT_ACC_TH": vc._ENTERING_PRED_LAT_ACC_TH,
  "_ABORT_ENTERING_PRED_LAT_ACC_TH": vc._ABORT_ENTERING_PRED_LAT_ACC_TH,
  "_TURNING_LAT_ACC_TH": vc._TURNING_LAT_ACC_TH,
  "_LEAVING_LAT_ACC_TH": vc._LEAVING_LAT_ACC_TH,
  "_FINISH_LAT_ACC_TH": vc._FINISH_LAT_ACC_TH,
  "_A_LAT_REG_MAX": vc._A_LAT_REG_MAX,
  "_ENTERING_SMOOTH_DECEL_V": list(vc._ENTERING_SMOOTH_DECEL_V),
  "_ENTERING_SMOOTH_DECEL_BP": list(vc._ENTERING_SMOOTH_DECEL_BP),
  "MIN_V": vc.MIN_V,
}

# One step further (0.12 g). Not shipped: no highway data to justify it yet.
ALT = {
  "_ENTERING_PRED_LAT_ACC_TH": 0.8,
  "_ABORT_ENTERING_PRED_LAT_ACC_TH": 0.6,
  "_TURNING_LAT_ACC_TH": 1.2,
  "_LEAVING_LAT_ACC_TH": 1.0,
  "_FINISH_LAT_ACC_TH": 0.8,
  "_A_LAT_REG_MAX": 1.2,
  "_ENTERING_SMOOTH_DECEL_V": [-0.6, -1.4],
  "_ENTERING_SMOOTH_DECEL_BP": [0.85, 2.2],
  "MIN_V": 12 * CV.KPH_TO_MS,
}

ACTIVE_STATES = (VisionState.entering, VisionState.turning, VisionState.leaving)


@contextlib.contextmanager
def constants(values: dict):
  saved = {k: getattr(vc, k) for k in values}
  for key, value in values.items():
    setattr(vc, key, value)
  try:
    yield
  finally:
    for key, value in saved.items():
      setattr(vc, key, value)


class FakeSM:
  """The two services `SmartCruiseControlVision.update` reads."""

  def __init__(self, model, controls):
    self._d = {"modelV2": model, "controlsState": controls}

  def __getitem__(self, key):
    return self._d[key]


def segments(route: str) -> list[str]:
  return sorted(glob.glob(f"{REALDATA}/{route}--*"), key=lambda p: int(p.rsplit("--", 1)[1]))


def replay_scc(route: str) -> dict:
  controllers = {name: SmartCruiseControlVision() for name in ("old", "new", "alt")}
  for controller in controllers.values():
    controller.enabled = True
  configs = {"old": OLD, "new": NEW, "alt": ALT}
  res = {name: {"states": [], "at": [], "vt": [], "v": [], "long": []} for name in controllers}

  for segment in segments(route):
    v_ego = a_ego = 0.0
    controls = SimpleNamespace(curvature=0.0)
    long_active = False
    for msg in LogReader(f"{segment}/rlog.zst"):
      which = msg.which()
      if which == "carControl":
        long_active = bool(msg.carControl.longActive and msg.carControl.enabled)
      elif which == "carState":
        v_ego = msg.carState.vEgo
        a_ego = msg.carState.aEgo
      elif which == "controlsState":
        controls = msg.controlsState
      elif which == "modelV2":
        model = msg.modelV2
        if not len(model.velocity.x) or not len(model.orientationRate.z):
          continue
        sm = FakeSM(model, controls)
        for name, controller in controllers.items():
          with constants(configs[name]):
            controller.update(sm, long_active, False, v_ego, a_ego, 0.0)
          row = res[name]
          row["states"].append(int(controller.state))
          row["at"].append(float(controller.a_target))
          row["vt"].append(float(controller.output_v_target))
          row["v"].append(v_ego)
          row["long"].append(long_active)

  out = {}
  for name, row in res.items():
    states = np.array(row["states"], dtype=int)
    at = np.array(row["at"])
    vt = np.array(row["vt"])
    v = np.array(row["v"])
    long_active = np.array(row["long"], dtype=bool)
    active = np.isin(states, [int(s) for s in ACTIVE_STATES]) & long_active
    entering = (states == int(VisionState.entering)) & long_active
    turning = (states == int(VisionState.turning)) & long_active
    demand = active & np.isfinite(vt) & (vt < v - 0.3)
    bands = {}
    for label, low, high in (("5-8", 5.0, 8.0), ("8-11", 8.0, 11.0), ("11+", 11.0, 99.0)):
      band = active & (v >= low) & (v < high)
      bands[label] = (int(band.sum()), int((band & (vt < v - 0.3)).sum()))
    out[name] = {
      "frames": int(states.size),
      "active": int(active.sum()),
      "entering": int(entering.sum()),
      "turning": int(turning.sum()),
      "ent_at": float(np.mean(at[entering])) if entering.any() else float("nan"),
      "tur_at": float(np.mean(at[turning])) if turning.any() else float("nan"),
      "demand_frames": int(demand.sum()),
      "demand_mean": float(np.mean(v[demand & np.isfinite(vt)] - vt[demand & np.isfinite(vt)])) if demand.any() else float("nan"),
      "bands": bands,
    }
  return out


def replay_leads(route: str) -> dict:
  """Reproduce radarState.leadOne presence, then add the hold on top."""
  filt = FirstOrderFilter(0.0, 0.2, DT_MDL)
  hold = LeadHold()
  before, after, logged = [], [], []

  for segment in segments(route):
    v_ego = model_v_ego = 0.0
    for msg in LogReader(f"{segment}/rlog.zst"):
      which = msg.which()
      if which == "carState":
        v_ego = msg.carState.vEgo
      elif which == "modelV2":
        leads = msg.modelV2.leadsV3
        model_v_ego = msg.modelV2.velocity.x[0] if len(msg.modelV2.velocity.x) else v_ego
        if not len(leads):
          continue
        prob = float(leads[0].prob)
        if prob > filt.x:
          filt.x = prob
        else:
          filt.update(prob)
        present = filt.x > 0.5 and len(leads[0].x) > 0
        lead = {"present": False}
        if present:
          lead = {
            "present": True,
            "dRel": float(leads[0].x[0]) - RADAR_TO_CAMERA,
            "vRel": float(leads[0].v[0]) - model_v_ego,
            "vLead": v_ego + float(leads[0].v[0]) - model_v_ego,
            "modelProb": filt.x,
          }
        before.append(present)
        after.append(hold.update(lead, model_time=msg.logMonoTime / 1e9, v_ego=v_ego)["present"])
      elif which == "radarState":
        logged.append(bool(msg.radarState.leadOne.present))

  def stats(values):
    array = np.array(values, dtype=bool)
    drops = np.where(array[:-1] & ~array[1:])[0]
    return {"present": int(array.sum()), "pct": float(array.mean() * 100), "drops": int(drops.size)}

  return {
    "before": stats(before),
    "after": stats(after),
    "sanity": {"logged_pct": float(np.mean(logged) * 100), "replay_pct": float(np.mean(before) * 100)},
  }


def main() -> None:
  routes = sys.argv[1:] or ["000001a1--9bd82bd837", "000001a2--95e74180e0"]
  for route in routes:
    print(f"===== {route}")
    scc = replay_scc(route)
    print(" SCC-V          frames  active  entering  turning  ent_aT  tur_aT  demand  mean(v-vT)")
    for name in ("old", "new", "alt"):
      row = scc[name]
      head = f"  {name:4s} {row['frames']:>10} {row['active']:>7} {row['entering']:>9} {row['turning']:>8}"
      body = f" {row['ent_at']:>7.2f} {row['tur_at']:>7.2f} {row['demand_frames']:>7} {row['demand_mean']:>11.2f}"
      print(head + body)
      bands = row["bands"]
      band_txt = f"  v5-8: {bands['5-8'][0]:>5}/{bands['5-8'][1]:<5}"
      band_txt += f" v8-11: {bands['8-11'][0]:>5}/{bands['8-11'][1]:<5}"
      band_txt += f" v11+: {bands['11+'][0]:>5}/{bands['11+'][1]:<5}  (active/demand)"
      print(band_txt)
    leads = replay_leads(route)
    print(f" lead before: {leads['before']['present']} frames ({leads['before']['pct']:.1f}%)"
          + f", drops {leads['before']['drops']}")
    print(f" lead after : {leads['after']['present']} frames ({leads['after']['pct']:.1f}%)"
          + f", drops {leads['after']['drops']}")
    print(f" sanity: logged {leads['sanity']['logged_pct']:.1f}%"
          + f" vs replayed {leads['sanity']['replay_pct']:.1f}%")


if __name__ == "__main__":
  main()
