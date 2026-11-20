import hashlib
import json
from types import SimpleNamespace

import numpy as np
import pytest

import isolated_gpu_probe as probe


def frame():
  inputs = {"nav_map": np.ones((1, 1, 256, 256), np.uint8), "navigation": np.ones((1, 47), np.float32)}
  evidence = {"receiptVerified": True, "receiptReason": "matched", "gpuSampled": True, "gpuMatch": True,
              "capture": True, "synchronized": True, "diagnosticError": False, "workerSession": 99,
              "workerSequence": 7, "rpcNs": 100, "workerStepNs": 50}
  for name, key in (("nav_map", "Map"), ("navigation", "Nav")):
    crc = probe.crc32_buffer(inputs[name].astype(np.float32))
    evidence.update({prefix + key + "Crc32": crc for prefix in ("input", "feed", "staging", "gpu")})
  model = SimpleNamespace(last_nav_evidence=evidence, nav_session_reset_confirmed=True,
                          _nav_reset_ack={"flags": 24, "worker_session": 99, "step_seq": 7})
  return model, inputs, {"map_features": np.ones((1, 64), np.float32)}


def test_matching_worker_receipt_and_reset_are_accepted():
  model, inputs, output = frame()
  result = probe.validate_frame(model, inputs, output, expected_reset_flags=24)
  assert result["gpu_match"] and result["reset_ack_verified"]
  assert result["map_features_nonzero"] == 64
  assert result["input_map_crc32_float32"] == result["feed_map_crc32"] == result["staging_map_crc32"] == result["gpu_map_crc32"]
  assert result["input_nav_crc32_float32"] == result["feed_nav_crc32"] == result["staging_nav_crc32"] == result["gpu_nav_crc32"]


@pytest.mark.parametrize("key", ["inputMapCrc32", "feedMapCrc32", "stagingNavCrc32", "gpuMapCrc32"])
def test_crc_mismatch_cannot_be_certified(key):
  model, inputs, output = frame()
  model.last_nav_evidence[key] ^= 1
  with pytest.raises(RuntimeError, match="crc_mismatch"):
    probe.validate_frame(model, inputs, output)


def test_previous_frame_ack_is_rejected():
  model, inputs, output = frame()
  model._nav_reset_ack["step_seq"] = 6
  with pytest.raises(RuntimeError, match="reset_ack_mismatch"):
    probe.validate_frame(model, inputs, output, expected_reset_flags=24)


def test_unsampled_gpu_does_not_count_as_readback():
  model, inputs, output = frame()
  model.last_nav_evidence["gpuSampled"] = False
  with pytest.raises(RuntimeError, match="readback_unverified"):
    probe.validate_frame(model, inputs, output)


def test_nonfinite_features_are_rejected():
  model, inputs, output = frame()
  output["map_features"][0, 0] = np.nan
  with pytest.raises(RuntimeError, match="map_features_invalid"):
    probe.validate_frame(model, inputs, output)


def make_artifact(tmp_path):
  path, metadata_path = tmp_path / "input.npz", tmp_path / "metadata.json"
  image, navigation = np.ones((1, 1, 256, 256), np.uint8), np.zeros((1, 47), np.float32)
  np.savez(path, nav_map=image, navigation=navigation, revoked_nav_map=np.zeros_like(image), revoked_navigation=navigation)
  metadata = {"artifact_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
              "runtime_evidence": {"mapReason": "accepted", "mapPixelCrc32": probe.crc32_buffer(image)}}
  metadata_path.write_text(json.dumps(metadata))
  return path, metadata_path, metadata


def test_runtime_artifact_preserves_map_invalidation(tmp_path):
  path, metadata_path, _ = make_artifact(tmp_path)
  valid, revoked, _ = probe.artifact_inputs(path, metadata_path)
  assert valid["nav_map"].any() and not revoked["nav_map"].any()


@pytest.mark.parametrize("change", ["hash", "crc", "reason"])
def test_unverified_runtime_artifact_is_rejected(tmp_path, change):
  path, metadata_path, metadata = make_artifact(tmp_path)
  if change == "hash":
    metadata["artifact_sha256"] = "0" * 64
  elif change == "crc":
    metadata["runtime_evidence"]["mapPixelCrc32"] ^= 1
  else:
    metadata["runtime_evidence"]["mapReason"] = "stale"
  metadata_path.write_text(json.dumps(metadata))
  with pytest.raises(ValueError):
    probe.artifact_inputs(path, metadata_path)


def test_runtime_config_is_parsed_without_executing_shell(tmp_path):
  path = tmp_path / "runtime.conf"
  path.write_text("export MODELD_BACKEND=migraphx-remote\nexport MODELD_MGXD_ENGINE=capture\n")
  assert probe.runtime_config(path)["MODELD_MGXD_ENGINE"] == "capture"
  path.write_text("export MODELD_MGXD_ENGINE=$(touch unexpected)\n")
  with pytest.raises(ValueError):
    probe.runtime_config(path)
