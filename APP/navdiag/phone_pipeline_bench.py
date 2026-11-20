from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import time
from types import SimpleNamespace
import uuid

import numpy as np

from APP.navdiag.android_gps_source import parse_gps_probe
from APP.navdiag.live_route_snapshot import SelectedCacheRoute
from APP.navdiag.root_probe import write_private
from openpilot.cereal import messaging
from openpilot.cereal.visionipc import VisionStreamType
from openpilot.common.prefix import OpenpilotPrefix
from openpilot.selfdrive.modeld.nav_inputs import NavRuntime
from openpilot.selfdrive.modeld.nav_reset import NavigationResetMixin
from openpilot.selfdrive.navigation.amap_phone_route import AmapPhoneRouteAdapter
from openpilot.selfdrive.navigation.mapsd import MapsD
from openpilot.selfdrive.navigation.navd import Navd
from tools.cplink_shim.amap_route_cache import SUPPORTED_PROFILE, decode_cache


class _Model(NavigationResetMixin):
  input_shapes = {"nav_map": (1, 1, 256, 256), "navigation": (1, 47)}


class ObservedPhoneRouteAdapter(AmapPhoneRouteAdapter):

  def __init__(self):
    super().__init__()
    self._call_context = None

  def accept(self, sample, *, now_ns, source_status=None):
    self._call_context = {
      "schema_version": 1, "now_ns": now_ns, "sample_present": sample is not None,
      "source_status": ({key: getattr(source_status, key, None) for key in (
        "running", "connected", "reason", "transport_session", "sequence")}
        if source_status is not None else None),
    }
    return super().accept(sample, now_ns=now_ns, source_status=source_status)

  def call_context(self):
    if self._call_context is None:
      return None
    return {**self._call_context, "source_status": (dict(self._call_context["source_status"])
            if self._call_context["source_status"] is not None else None)}


class PhonePipelineBench:
  def __init__(self, model=None):
    self.model = model if model is not None else _Model()
    self.sample = None
    self.source_status = None
    self.prefix = OpenpilotPrefix("navcheck_" + uuid.uuid4().hex[:12])

  def bind_source_status(self, source_status):
    self.navd.phone_source.status = source_status
    return self

  def __enter__(self):
    import os
    from msgq.visionipc import VisionIpcServer
    self.old_mode = os.environ.get("SUNNYPILOT_AMAP_ROOT_NAV")
    os.environ["SUNNYPILOT_AMAP_ROOT_NAV"] = "1"
    self.prefix.__enter__()
    self.pm = messaging.PubMaster(["navRoute", "navInstruction", "navMapFrameSP"])
    self.map_sm = messaging.SubMaster(["navRoute", "navInstruction", "gpsLocation", "gpsLocationExternal"])
    self.model_sm = messaging.SubMaster(["navRoute", "navInstruction", "navMapFrameSP"])
    phone_source = SimpleNamespace(latest=lambda: self.sample)
    if self.source_status is not None:
      phone_source.status = self.source_status
    self.navd = Navd(self.map_sm, self.pm, phone_source=phone_source)
    self.navd.phone_adapter = ObservedPhoneRouteAdapter()
    self.mapsd = MapsD()
    self.runtime = NavRuntime()
    self.server = VisionIpcServer("mapsd")
    self.server.create_buffers_with_sizes(VisionStreamType.VISION_STREAM_MAP, 4, 256, 256, 65536, 256, 65536)
    self.server.start_listener()
    self.renderer_id = uuid.uuid4().hex
    self.frame = 0
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
      self.runtime.inputs_for_model(self.model_sm, 0.0, self.model)
      client = self.runtime.client
      if (time.monotonic() >= self.runtime._accept_after and client is not None and client.is_connected()
          and self.runtime._phone_matcher is not None):
        return self
      time.sleep(0.01)
    self.__exit__(None, None, None)
    raise RuntimeError("bench_visionipc_connect_timeout")

  def step(self, sample):
    self.sample = sample
    self.navd.publish_route(time.monotonic())
    self.navd.publish_instruction(time.monotonic())
    self.map_sm.update(20)
    image, stamp = self.mapsd.update(self.map_sm)
    self.frame += 1
    self.mapsd.publish_frame(self.server, self.pm, image, stamp, frame_id=self.frame, renderer_instance_id=self.renderer_id)
    self.model_sm.update(20)
    inputs = self.runtime.inputs_for_model(self.model_sm, 0.0, self.model)
    if stamp and not self.runtime.map_valid:
      for _ in range(3):
        time.sleep(0.005)
        inputs = self.runtime.inputs_for_model(self.model_sm, 0.0, self.model)
        if self.runtime.map_valid:
          break
    return inputs, {"source_reason": self.navd.phone_adapter.reason,
                    "adapter_call_context": self.navd.phone_adapter.call_context(),
                    "source_decision": self.navd.phone_adapter.decision_evidence(), "map_timestamp": stamp,
                    "map_valid": self.runtime.map_valid, "instruction_valid": self.runtime.instruction_valid,
                    "adapter_instruction_reason": self.navd.phone_adapter.instruction_reason,
                    "runtime": self.runtime.evidence(), "reset_requested": self.model.pending_nav_resets()}

  def __exit__(self, *_):
    import gc
    import os
    self.runtime.client = None
    self.server = None
    self.pm = self.map_sm = self.model_sm = None
    gc.collect()
    self.prefix.__exit__(None, None, None)
    if self.old_mode is None:
      os.environ.pop("SUNNYPILOT_AMAP_ROOT_NAV", None)
    else:
      os.environ["SUNNYPILOT_AMAP_ROOT_NAV"] = self.old_mode


def replay_sample(paired_path: Path, cache_directory: Path, *, now_ns: int):
  original = json.loads(paired_path.read_text())
  epoch = original["native_process_epoch"]
  before, after = original["native_before"], original["native_after"]
  offset = now_ns - after["host_observation_finished_monotonic_ns"] - 1_000_000
  for observation in (before, after):
    for key in ("host_observation_started_monotonic_ns", "host_observation_finished_monotonic_ns"):
      observation[key] += offset
  window = original["gps_host_window"]
  gps = parse_gps_probe(json.dumps(original["gps"]).encode(), window["host_started_ns"], window["host_finished_ns"])
  gps = replace(gps, host_started_ns=gps.host_started_ns + offset, host_finished_ns=gps.host_finished_ns + offset)
  evidence = json.loads((cache_directory / "evidence.json").read_text())
  matches = []
  for item in evidence["snapshot"]["files"]:
    if "/path_cache/" not in item["source"]:
      continue
    raw = (cache_directory / item["file"]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != item["sha256"]:
      raise ValueError("replay_source_hash_mismatch")
    for index, route in enumerate(decode_cache(raw, profile=SUPPORTED_PROFILE).routes):
      from openpilot.selfdrive.navigation.amap_phone_route import _path_hash
      if _path_hash(route.path_id, epoch) == before["path_identity_hash"]:
        matches.append((item["source"], raw, index, route))
  if len(matches) != 1:
    raise ValueError("replay_selection_not_unique")
  source, raw, index, route = matches[0]
  selected = SelectedCacheRoute(route, index, source, hashlib.sha256(raw).hexdigest(),
    evidence["snapshot"]["source_app_session_id"], before["path_identity_hash"],
    before["host_observation_started_monotonic_ns"], after["host_observation_finished_monotonic_ns"])
  return SimpleNamespace(selected=selected, cache_bytes=raw, epoch=epoch, sequence=1,
    host_start_ns=before["host_observation_started_monotonic_ns"], gps=gps, native_before=before, native_after=after,
    transport_session="offline-replay-" + uuid.uuid4().hex)


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--paired", type=Path, required=True)
  parser.add_argument("--cache-directory", type=Path, required=True)
  parser.add_argument("--out", type=Path, required=True)
  args = parser.parse_args()
  args.out.mkdir(mode=0o700, parents=True, exist_ok=False)
  with PhonePipelineBench() as bench:
    sample = replay_sample(args.paired, args.cache_directory, now_ns=time.monotonic_ns())
    accepted, accepted_report = bench.step(sample)
    if not accepted_report["map_valid"] or not accepted["nav_map"].any():
      raise RuntimeError("replayed_map_not_accepted:" + accepted_report["source_reason"] + ":" + accepted_report["runtime"]["mapReason"])
    bench.model._commit_nav_reset(bench.model.pending_nav_resets())
    revoked, revoked_report = bench.step(None)
    if revoked_report["map_valid"] or revoked["nav_map"].any():
      raise RuntimeError("replayed_map_not_revoked")
    target = args.out / "model-inputs.npz"
    with target.open("wb") as handle:
      np.savez(handle, nav_map=accepted["nav_map"], navigation=accepted["navigation"],
               revoked_nav_map=revoked["nav_map"], revoked_navigation=revoked["navigation"])
    target.chmod(0o600)
    report = {"schema_version": 1, "mode": "offline_raw_phone_replay_host_clock_translated_once",
              "actual_msgq": True, "actual_visionipc": True, "control_stack_started": False,
              "source_paired_sha256": hashlib.sha256(args.paired.read_bytes()).hexdigest(),
              "cache_sha256": sample.selected.cache_sha256, "route_points": len(sample.selected.route.points_native),
              "accepted": accepted_report, "revoked": revoked_report,
              "artifact_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
              "runtime_evidence": accepted_report["runtime"],
              "provenance": {"mode": "offline_raw_phone_replay_host_clock_translated_once",
                             "chain": "cache + native/GPS pair -> adapter -> navd -> msgq -> mapsd -> VisionIPC -> NavRuntime"}}
  write_private(args.out / "report.json", (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode())
  print(json.dumps({"map_accepted": report["accepted"]["map_valid"], "map_revoked": not report["revoked"]["map_valid"],
                    "actual_visionipc": True, "report": str((args.out / "report.json").resolve())}, ensure_ascii=False))


if __name__ == "__main__":
  main()
