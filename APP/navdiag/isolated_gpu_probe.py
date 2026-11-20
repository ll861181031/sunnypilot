#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
import mmap
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

import numpy as np


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from openpilot.selfdrive.modeld.nav_evidence_protocol import RESET_FLAGS, REQUEST_RESET_MAP, REQUEST_RESET_NAVIGATION, crc32_buffer


def private_json(path: Path, value: dict) -> None:
  fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
  with os.fdopen(fd, "w") as stream:
    json.dump(value, stream, ensure_ascii=False, indent=2)
    stream.write("\n")
  os.chmod(path, 0o600)


def production_processes(allowed_pid: int | None = None) -> list[dict]:
  result = []
  for entry in Path("/proc").iterdir():
    if not entry.name.isdigit() or int(entry.name) in (os.getpid(), allowed_pid):
      continue
    try:
      argv = (entry / "cmdline").read_bytes().decode(errors="replace").strip("\0").split("\0")
      matched = any(arg in ("openpilot.selfdrive.modeld.mgxd", "openpilot.selfdrive.modeld.modeld",
                            "openpilot.system.manager.manager") or Path(arg).name in ("modeld", "modeld.py", "mgxd.py", "manager.py")
                    for arg in argv)
      if matched:
        result.append({"pid": int(entry.name), "argv": argv})
    except (FileNotFoundError, PermissionError, ProcessLookupError):
      continue
  return result


def runtime_config(path: Path) -> dict[str, str]:
  allowed = {"MODELD_BACKEND", "MODELD_MGXD_ROCM_ROOT", "MODELD_MIGRAPHX_ONNX", "HSA_OVERRIDE_GFX_VERSION",
             "MODELD_MGXD_ENGINE", "MODELD_MIGRAPHX_MXR"}
  result = {}
  for raw in path.read_text().splitlines():
    line = raw.strip()
    if not line or line.startswith("#"):
      continue
    tokens = shlex.split(line, comments=True)
    if len(tokens) != 2 or tokens[0] != "export" or "=" not in tokens[1]:
      raise ValueError("runtime_config_syntax_rejected")
    key, value = tokens[1].split("=", 1)
    if key not in allowed or key in result or not value or "$" in value or "`" in value:
      raise ValueError("runtime_config_value_rejected")
    result[key] = value
  if result.get("MODELD_MGXD_ENGINE") != "capture" or result.get("MODELD_BACKEND") != "migraphx-remote":
    raise ValueError("capture_runtime_required")
  return result


def artifact_inputs(path: Path, metadata_path: Path) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray], dict]:
  metadata = json.loads(metadata_path.read_text())
  if metadata.get("artifact_sha256") != hashlib.sha256(path.read_bytes()).hexdigest():
    raise ValueError("artifact_hash_mismatch")
  evidence = metadata.get("runtime_evidence", {})
  if evidence.get("mapReason") != "accepted":
    raise ValueError("runtime_map_not_accepted")
  with np.load(path, allow_pickle=False) as arrays:
    expected = {"nav_map": ((1, 1, 256, 256), np.dtype("uint8")),
                "navigation": ((1, 47), np.dtype("float32")),
                "revoked_nav_map": ((1, 1, 256, 256), np.dtype("uint8")),
                "revoked_navigation": ((1, 47), np.dtype("float32"))}
    values = {}
    for name, (shape, dtype) in expected.items():
      if name not in arrays or arrays[name].shape != shape or arrays[name].dtype != dtype or not np.isfinite(arrays[name]).all():
        raise ValueError("artifact_array_invalid")
      values[name] = np.ascontiguousarray(arrays[name]).copy()
  if not values["nav_map"].any() or values["revoked_nav_map"].any():
    raise ValueError("artifact_map_transition_invalid")
  if evidence.get("mapPixelCrc32") != crc32_buffer(values["nav_map"]):
    raise ValueError("runtime_pixel_crc_mismatch")
  valid = {name: values[name] for name in ("nav_map", "navigation")}
  revoked = {name: values["revoked_" + name] for name in ("nav_map", "navigation")}
  return valid, revoked, metadata


def validate_frame(model, inputs, outputs, *, expected_reset_flags: int = 0, require_gpu: bool = True) -> dict:
  evidence = dict(model.last_nav_evidence)
  if evidence.get("receiptVerified") is not True or evidence.get("receiptReason") != "matched":
    raise RuntimeError("worker_receipt_unverified")
  for name, key in (("nav_map", "Map"), ("navigation", "Nav")):
    expected = crc32_buffer(np.ascontiguousarray(inputs[name], np.float32))
    for prefix in ("input", "feed", "staging"):
      if evidence.get(prefix + key + "Crc32") != expected:
        raise RuntimeError("worker_input_crc_mismatch")
    if require_gpu and evidence.get("gpu" + key + "Crc32") != expected:
      raise RuntimeError("worker_gpu_crc_mismatch")
  if require_gpu and (evidence.get("gpuSampled") is not True or evidence.get("gpuMatch") is not True):
    raise RuntimeError("worker_gpu_readback_unverified")
  if not evidence.get("capture") or not evidence.get("synchronized") or evidence.get("diagnosticError"):
    raise RuntimeError("worker_capture_unverified")
  ack = getattr(model, "_nav_reset_ack", None)
  if expected_reset_flags and (ack is None or ack.get("flags") != expected_reset_flags
                             or ack.get("worker_session") != evidence.get("workerSession")
                             or ack.get("step_seq") != evidence.get("workerSequence")):
    raise RuntimeError("worker_reset_ack_mismatch")
  features = np.asarray(outputs["map_features"])
  if features.shape != (1, 64) or not np.isfinite(features).all():
    raise RuntimeError("map_features_invalid")
  return {"worker_sequence": evidence["workerSequence"], "receipt_verified": True,
          "input_map_nonzero": int(np.count_nonzero(inputs["nav_map"])),
          "input_navigation_nonzero": int(np.count_nonzero(inputs["navigation"])),
          "input_map_crc32_float32": evidence["inputMapCrc32"], "input_nav_crc32_float32": evidence["inputNavCrc32"],
          "feed_map_crc32": evidence["feedMapCrc32"], "staging_map_crc32": evidence["stagingMapCrc32"],
          "feed_nav_crc32": evidence["feedNavCrc32"], "staging_nav_crc32": evidence["stagingNavCrc32"],
          "gpu_map_crc32": evidence.get("gpuMapCrc32", 0),
          "gpu_nav_crc32": evidence.get("gpuNavCrc32", 0), "gpu_sampled": evidence.get("gpuSampled", False),
          "gpu_match": evidence.get("gpuMatch", False), "capture": evidence["capture"],
          "reset_flags": ack.get("flags", 0) if ack else 0, "reset_ack_verified": bool(expected_reset_flags),
          "session_reset_confirmed": model.nav_session_reset_confirmed,
          "map_features_nonzero": int(np.count_nonzero(features)), "map_features_l2": float(np.linalg.norm(features)),
          "map_features_crc32": crc32_buffer(np.ascontiguousarray(features)),
          "rpc_ms": evidence["rpcNs"] / 1e6, "worker_step_ms": evidence["workerStepNs"] / 1e6}


def run(args) -> dict:
  existing = production_processes()
  if existing:
    raise RuntimeError("existing_model_or_manager_process")
  config = runtime_config(args.config)
  if args.artifact:
    valid, revoked, metadata = artifact_inputs(args.artifact, args.metadata)
  else:
    valid = revoked = metadata = None
  args.out.mkdir(parents=True, mode=0o700, exist_ok=False)
  report = {"schema_version": 1, "camera_source": "synthetic_zero_nv12_memfd", "road_inference_validated": False,
            "gpu_inference_in_this_run": True, "control_stack_started": False,
            "vehicle_control_started": False, "production_model_replaced": False, "frames": [],
            "runtime_config_sha256": hashlib.sha256(args.config.read_bytes()).hexdigest(),
            "artifact_sha256": metadata.get("artifact_sha256") if metadata else None,
            "artifact_provenance": metadata.get("provenance") if metadata else None,
            "success": False, "worker_process_cleaned": False}
  report["started_at_utc"] = datetime.now(UTC).isoformat()
  report["runtime_configuration"] = config
  report["source_sha256"] = {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in (
    "APP/navdiag/isolated_gpu_probe.py", "openpilot/selfdrive/modeld/modeld.py",
    "openpilot/selfdrive/modeld/mgxd.py", "openpilot/selfdrive/modeld/mgxd_engine.py",
    "openpilot/selfdrive/modeld/nav_evidence.py", "openpilot/selfdrive/modeld/nav_evidence_protocol.py")}
  with Path(config["MODELD_MIGRAPHX_MXR"]).open("rb") as model_file:
    report["model_sha256"] = hashlib.file_digest(model_file, "sha256").hexdigest()
  model = worker = None
  memory = []
  descriptors = []
  before_keys = {key: os.environ.get(key) for key in (*config, "MODELD_MGXD_SOCK", "MODELD_MGXD_LOG", "SUNNYPILOT_NAV_SOURCE")}
  from openpilot.selfdrive.modeld.mgxd_runtime import daemon_environment
  with tempfile.TemporaryDirectory(prefix="sp-nav-gpu-") as temporary:
    socket_path = Path(temporary) / "worker.sock"
    log_path = args.out / "worker.log"
    env = dict(os.environ)
    env.update(config, MODELD_MGXD_SOCK=str(socket_path), MODELD_MGXD_LOG=str(log_path), PYTHONDONTWRITEBYTECODE="1")
    env.pop("MGXD_PROFILE", None)
    env.pop("SUNNYPILOT_NAV_SOURCE", None)
    env = daemon_environment(env)
    try:
      for key, value in config.items():
        os.environ[key] = value
      os.environ.update(MODELD_MGXD_SOCK=str(socket_path), MODELD_MGXD_LOG=str(log_path))
      os.environ.pop("SUNNYPILOT_NAV_SOURCE", None)
      from openpilot.selfdrive.modeld.modeld import MigraphxRemoteState

      class IsolatedRemoteState(MigraphxRemoteState):
        def _ensure_server(self):
          self._hello_sent = False
          self.nav_reset_supported = False
          self.nav_session_reset_confirmed = False
          self._last_map = self._last_nav = None
          self._sock = self._try_connect()
          if self._sock is None:
            raise RuntimeError("isolated_worker_unavailable_no_autospawn")

      log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
      with os.fdopen(log_fd, "wb", buffering=0) as log:
        worker = subprocess.Popen([sys.executable, "-u", "-m", "openpilot.selfdrive.modeld.mgxd"],
                                  cwd=ROOT, env=env, stdout=log, stderr=log, start_new_session=True)
      report["worker_pid"] = worker.pid
      report["worker_pgid"] = os.getpgid(worker.pid)
      deadline = time.monotonic() + 45
      while not socket_path.exists():
        if worker.poll() is not None:
          raise RuntimeError("isolated_worker_start_failed")
        if time.monotonic() > deadline:
          raise RuntimeError("isolated_worker_start_timeout")
        time.sleep(0.1)
      model = IsolatedRemoteState(512, 256, False)
      for index in range(2):
        fd = os.memfd_create("sp-nav-gpu-synthetic-" + str(index), os.MFD_CLOEXEC)
        descriptors.append(fd)
        os.ftruncate(fd, 512 * 256 * 3 // 2)
        mapped = mmap.mmap(fd, 512 * 256 * 3 // 2)
        memory.append(mapped)
      bufs = {name: SimpleNamespace(width=512, height=256, stride=512, uv_offset=512 * 256, data=memory[index], fd=descriptors[index])
              for index, name in enumerate(("img", "big_img"))}
      transforms = {name: np.eye(3, dtype=np.float32) for name in bufs}
      base_inputs = {"desire_pulse": np.zeros(8, np.float32), "traffic_convention": np.array([1, 0], np.float32),
                     "action_t": np.array([0.2, 0.2], np.float32)}
      zeros = {"nav_map": np.zeros((1, 1, 256, 256), np.uint8), "navigation": np.zeros((1, 47), np.float32)}
      last_gpu_sample = 0.0
      feature_values = {}

      def infer(label, values, *, reset_flags=0, gpu=True):
        nonlocal last_gpu_sample
        if production_processes(worker.pid):
          raise RuntimeError("concurrent_model_or_manager_process")
        if gpu:
          remaining = 1.1 - (time.monotonic() - last_gpu_sample)
          if remaining > 0:
            time.sleep(remaining)
        if callable(values):
          values = values()
        if reset_flags is None:
          pending = model.pending_nav_resets()
          reset_flags = (REQUEST_RESET_MAP if "map" in pending else 0) | (REQUEST_RESET_NAVIGATION if "navigation" in pending else 0)
        inputs = {**base_inputs, **values}
        outputs = model.run(bufs, transforms, inputs)
        item = {"label": label, **validate_frame(model, inputs, outputs, expected_reset_flags=reset_flags, require_gpu=gpu)}
        report["frames"].append(item)
        feature_values[label] = outputs["map_features"].copy()
        if item["gpu_sampled"]:
          last_gpu_sample = time.monotonic()
        print(json.dumps({"phase": label, "receipt_verified": item["receipt_verified"],
                          "gpu_match": item["gpu_match"], "reset_flags": item["reset_flags"]}), flush=True)
        return item

      infer("zero_session_bootstrap", zeros, reset_flags=RESET_FLAGS)
      live_callback = getattr(args, "live_callback", None)
      if live_callback is not None:
        live_callback(model, infer, report)
      elif valid is None:
        model.request_nav_reset(map_epoch=1, navigation_epoch=1)
        infer("zero_explicit_reset", zeros, reset_flags=RESET_FLAGS)
      else:
        model.request_nav_reset(map_epoch=1, navigation_epoch=1)
        infer("runtime_map_accepted", valid, reset_flags=RESET_FLAGS)
        infer("runtime_map_cached", valid, gpu=False)
        model.request_nav_reset(map_epoch=2)
        flags = REQUEST_RESET_MAP
        navigation_changed = not np.array_equal(valid["navigation"], revoked["navigation"])
        if navigation_changed:
          model.request_nav_reset(navigation_epoch=2)
          flags |= REQUEST_RESET_NAVIGATION
        infer("runtime_map_revoked", revoked, reset_flags=flags)
        model.request_nav_reset(map_epoch=3)
        if navigation_changed:
          model.request_nav_reset(navigation_epoch=3)
        infer("runtime_map_restored", valid, reset_flags=flags)
        report["map_features_valid_vs_zero_l2"] = float(np.linalg.norm(feature_values["runtime_map_accepted"] - feature_values["zero_session_bootstrap"]))
        report["map_features_restore_equal"] = bool(np.array_equal(feature_values["runtime_map_accepted"], feature_values["runtime_map_restored"]))
        if report["map_features_valid_vs_zero_l2"] <= 1e-6:
          raise RuntimeError("map_features_not_distinguishable_from_zero")
        if not report["frames"][1]["map_features_nonzero"]:
          raise RuntimeError("accepted_map_features_zero")
      report["success"] = True
    except BaseException as error:
      report["error_type"] = type(error).__name__
      report["error_reason"] = str(error) if len(str(error)) < 160 else "long_error_see_private_worker_log"
      raise
    finally:
      if model is not None and model._sock is not None:
        model._sock.close()
      for mapped in memory:
        mapped.close()
      for fd in descriptors:
        os.close(fd)
      if worker is not None:
        if worker.poll() is None:
          if os.getpgid(worker.pid) != worker.pid:
            raise RuntimeError("owned_worker_process_group_changed")
          os.killpg(worker.pid, signal.SIGTERM)
          try:
            worker.wait(timeout=8)
          except subprocess.TimeoutExpired:
            os.killpg(worker.pid, signal.SIGKILL)
            worker.wait(timeout=5)
        report["worker_returncode"] = worker.returncode
        report["worker_process_cleaned"] = not Path(f"/proc/{worker.pid}").exists()
      for key, value in before_keys.items():
        if value is None:
          os.environ.pop(key, None)
        else:
          os.environ[key] = value
      report["other_model_process_count_after"] = len(production_processes())
      report["finished_at_utc"] = datetime.now(UTC).isoformat()
      private_json(args.out / "report.json", report)
  return report


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--config", type=Path, default=Path.home() / ".config/sunnypilot/mgxd-runtime.conf")
  parser.add_argument("--artifact", type=Path)
  parser.add_argument("--metadata", type=Path)
  parser.add_argument("--out", required=True, type=Path)
  args = parser.parse_args()
  if bool(args.artifact) != bool(args.metadata):
    parser.error("artifact 与 metadata 必须一起提供")
  result = run(args)
  print(json.dumps({"success": result["success"], "frames": len(result["frames"]),
                    "worker_process_cleaned": result["worker_process_cleaned"],
                    "report": str((args.out / "report.json").resolve())}, ensure_ascii=False))


if __name__ == "__main__":
  main()
