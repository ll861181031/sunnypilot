import ast
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from APP.navdiag.live_gpu_probe import require_valid_map, runtime_summary, source_status


def fixture():
  inputs = {"nav_map": np.ones((1, 1, 256, 256), np.uint8), "navigation": np.zeros((1, 47), np.float32)}
  details = {"map_valid": True, "instruction_valid": False, "source_reason": "accepted", "reset_requested": {"map": 1},
             "runtime": {"mapReason": "accepted", "instructionReason": "invalidMessage", "private_coordinates": [1, 2]}}
  return inputs, details


def test_real_map_can_be_valid_without_inventing_instruction():
  inputs, details = fixture()
  assert require_valid_map(inputs, details)
  assert not inputs["navigation"].any()


@pytest.mark.parametrize("failure", ["zero_map", "rejected", "wrong_dtype", "nonfinite_navigation"])
def test_invalid_input_never_counts_as_valid_gpu_sample(failure):
  inputs, details = fixture()
  if failure == "zero_map":
    inputs["nav_map"].fill(0)
  elif failure == "rejected":
    details["runtime"]["mapReason"] = "expired"
  elif failure == "wrong_dtype":
    inputs["nav_map"] = inputs["nav_map"].astype(np.float32)
  else:
    inputs["navigation"][0, 0] = np.nan
  assert not require_valid_map(inputs, details)


def test_summary_does_not_copy_private_fields_or_raw_session():
  _, details = fixture()
  assert "private_coordinates" not in runtime_summary(details)["runtime"]
  result = source_status(SimpleNamespace(running=True, reason="accepted", sequence=1,
    transport_session="private-session", connected=True, cleanup_verified=False))
  assert "transport_session" not in result
  assert len(result["session_hash"]) == 64 and result["session_hash"] != "private-session"


def test_gpu_summary_preserves_independent_route_binding_without_private_paths():
  import json
  from APP.navdiag.test_pipeline_route_identity_diagnostics import fixture as route_fixture
  sample, runtime = route_fixture()
  _, details = fixture()
  details["runtime"] = runtime
  summary = runtime_summary(details, sample)
  identity = summary["route_identity_diagnostics"]
  assert identity["source_consumer_binding_match"] is True
  assert identity["consumer_binding"]["routeId"] == runtime["mapBinding"]["routeId"]
  assert sample.selected.source_path not in json.dumps(summary)
  assert sample.transport_session not in json.dumps(summary)
  runtime["mapBinding"]["routeId"] = "f" * 64
  assert runtime_summary(details, sample)["route_identity_diagnostics"]["source_consumer_binding_match"] is False


def test_gpu_summary_never_fills_missing_consumer_binding_from_source():
  from APP.navdiag.test_pipeline_route_identity_diagnostics import fixture as route_fixture
  sample, _ = route_fixture()
  _, details = fixture()
  identity = runtime_summary(details, sample)["route_identity_diagnostics"]
  assert identity["selected"]["path_identity_hash"] == sample.selected.identity_hash
  assert identity["consumer_binding_status"] == "unknown"
  assert identity["source_consumer_binding_match"] == "unknown"


def test_live_gpu_probe_attaches_to_the_existing_shared_adb_server():
  source = Path(__import__("APP.navdiag.live_gpu_probe", fromlist=["__file__"]).__file__).read_text()
  tree = ast.parse(source)
  calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
           and isinstance(node.func, ast.Name) and node.func.id == "PhoneRouteProducer"]
  assert len(calls) == 1
  values = {keyword.arg: ast.literal_eval(keyword.value) for keyword in calls[0].keywords}
  assert values == {"port": 5039, "adb_ownership": "attach"}
