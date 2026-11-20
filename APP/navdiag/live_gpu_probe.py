#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from APP.navdiag.isolated_gpu_probe import run
from APP.navdiag.live_pipeline_probe import route_identity_diagnostics
from APP.navdiag.phone_pipeline_bench import PhonePipelineBench
from APP.navdiag.phone_route_producer import PhoneRouteProducer


def source_status(status):
  return {"running": status.running, "reason": status.reason, "sequence": status.sequence,
          "session_hash": hashlib.sha256(status.transport_session.encode()).hexdigest() if status.transport_session else None,
          "connected": status.connected, "cleanup_verified": status.cleanup_verified}


def runtime_summary(details, sample=None):
  runtime = details["runtime"]
  keys = ("mapReason", "mapPixelCrc32", "mapFrameId", "mapRendererInstanceId", "mapCorrelationReason", "instructionReason")
  return {"source_reason": details["source_reason"], "map_valid": details["map_valid"],
          "instruction_valid": details["instruction_valid"], "reset_requested": details["reset_requested"],
          "runtime": {key: runtime.get(key) for key in keys},
          "route_identity_diagnostics": route_identity_diagnostics(sample, runtime, map_valid=details["map_valid"])}


def require_valid_map(inputs, details):
  image, vector = inputs["nav_map"], inputs["navigation"]
  return (details["map_valid"] is True and details["runtime"].get("mapReason") == "accepted"
          and image.shape == (1, 1, 256, 256) and image.dtype == np.uint8 and bool(image.any())
          and vector.shape == (1, 47) and vector.dtype == np.float32 and np.isfinite(vector).all())


def live_callback(model, infer, report):
  report.update(mode="live_phone_source_actual_msgq_visionipc_gpu", source_clock_translated=False,
                actual_msgq=True, actual_visionipc=True, source_events=[], artificial_withdrawal_only=True,
                user_navigation_cancelled_by_probe=False)
  report["source_sha256"].update({path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in (
    "APP/navdiag/live_gpu_probe.py", "APP/navdiag/live_pipeline_probe.py",
    "APP/navdiag/phone_pipeline_bench.py", "APP/navdiag/phone_route_producer.py",
    "APP/navdiag/android_gps_source.py", "APP/navdiag/native_route_source.py",
    "openpilot/selfdrive/navigation/amap_phone_route.py", "openpilot/selfdrive/navigation/navd.py",
    "openpilot/selfdrive/navigation/mapsd.py", "openpilot/selfdrive/modeld/nav_inputs.py")})
  producer = PhoneRouteProducer(port=5039, adb_ownership="attach")
  observed_sequences, gps_tokens, sessions = set(), set(), set()
  started = time.monotonic()
  deadline = started + 45
  prior_status = None
  producer.start()
  try:
    with PhonePipelineBench(model=model) as bench:
      for index in range(5):
        selected = {}

        def next_valid(selected=selected):
          nonlocal prior_status
          while time.monotonic() < deadline:
            status = source_status(producer.status())
            if status != prior_status:
              report["source_events"].append({"since_start_ms": round((time.monotonic() - started) * 1000, 3), **status})
              prior_status = status
            sample = producer.latest()
            if sample is None or sample.sequence in observed_sequences:
              time.sleep(0.02)
              continue
            inputs, details = bench.step(sample)
            if not require_valid_map(inputs, details):
              report["last_rejection"] = runtime_summary(details)
              time.sleep(0.02)
              continue
            now_ns = time.monotonic_ns()
            selected.update(sequence=sample.sequence,
              transport_session_hash=hashlib.sha256(sample.transport_session.encode()).hexdigest(),
              gps_source_token=sample.gps.source_token,
              gps_age_upper_ms=(sample.gps.age_upper_ns + now_ns - sample.gps.host_finished_ns) / 1e6,
              source_observation_age_ms=(now_ns - sample.host_start_ns) / 1e6,
              gps_accuracy_m=sample.gps.horizontal_accuracy_m,
              cache_sha256=sample.selected.cache_sha256, route_points=len(sample.selected.route.points_native),
              pipeline=runtime_summary(details, sample))
            observed_sequences.add(sample.sequence)
            gps_tokens.add(sample.gps.source_token)
            sessions.add(sample.transport_session)
            return inputs
          raise RuntimeError("live_phone_five_valid_sequences_timeout")

        frame = infer("live_route_" + str(index + 1), next_valid, reset_flags=None)
        frame["live_source"] = selected
        if frame["map_features_crc32"] == report["frames"][0]["map_features_crc32"]:
          raise RuntimeError("live_map_features_equal_zero_baseline")

      withdrawal = {}

      def withdraw():
        inputs, details = bench.step(None)
        if details["map_valid"] or inputs["nav_map"].any() or "map" not in model.pending_nav_resets():
          raise RuntimeError("live_artificial_withdrawal_not_applied")
        withdrawal.update(runtime_summary(details))
        return inputs

      frame = infer("live_artificial_withdrawal", withdraw, reset_flags=None)
      frame["pipeline"] = withdrawal
      report.update(valid_source_sequence_count=len(observed_sequences), unique_gps_source_tokens=len(gps_tokens),
                    source_session_count=len(sessions), live_chain_frames_complete=True,
                    producer_status_before_close=source_status(producer.status()))
  finally:
    close_started = time.monotonic()
    report["producer_thread_stopped"] = producer.close()
    report["producer_close_ms"] = (time.monotonic() - close_started) * 1000
    report["producer_status_after_close"] = source_status(producer.status())
  if not report["producer_thread_stopped"] or not report["producer_status_after_close"]["cleanup_verified"]:
    raise RuntimeError("live_producer_cleanup_unverified")


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--config", type=Path, default=Path.home() / ".config/sunnypilot/mgxd-runtime.conf")
  parser.add_argument("--out", type=Path, required=True)
  args = parser.parse_args()
  args.artifact = args.metadata = None
  args.live_callback = live_callback
  result = run(args)
  print(json.dumps({"success": result["success"], "valid_sequences": result.get("valid_source_sequence_count", 0),
                    "gpu_frames": len(result["frames"]), "producer_thread_stopped": result.get("producer_thread_stopped"),
                    "worker_process_cleaned": result["worker_process_cleaned"],
                    "report": str((args.out / "report.json").resolve())}, ensure_ascii=False))


if __name__ == "__main__":
  main()
