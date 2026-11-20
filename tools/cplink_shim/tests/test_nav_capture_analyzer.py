import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

import nav_capture_analyzer as analyzer


def typed(value):
    if isinstance(value, dict):
        return {"type": "bundle", "value": {key: typed(item) for key, item in value.items()}}
    if isinstance(value, list):
        return {"type": "array", "length": len(value), "value": [typed(item) for item in value]}
    if value is None:
        return {"type": "null", "value": None}
    name = "boolean" if isinstance(value, bool) else "int" if isinstance(value, int) else "double" if isinstance(value, float) else "string"
    return {"type": name, "value": value}


def record(extras, sequence=1, **kwargs):
    return {
        "schemaVersion": 1, "sessionId": "fixture-session", "sequence": sequence,
        "receivedWallMs": 1000 + sequence, "receivedElapsedNs": sequence * 1000000,
        "action": "AUTONAVI_STANDARD_BROADCAST_SEND", "targetPackage": None,
        "sender": {"verified": False, "package": None, "uid": None},
        "extras": typed(extras), "complete": True, "truncated": False, "issues": [], **kwargs,
    }


def capture(tmp_path, records, *, complete=True, counts=None, events=None):
    directory = tmp_path / "capture"
    directory.mkdir()
    stats = {
        "received": len(records), "written": len(records), "incomplete": 0,
        "truncated": 0, "dropped": 0, "decodeErrors": 0, "eventDropped": 0,
        **(counts or {}),
    }
    manifest = {"schemaVersion": 1, "sessionId": "fixture-session", "status": "completed" if complete else "running",
                "receiverScope": "registered_actions", "targetedBroadcastCoverage": "unknown", "counts": stats}
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (directory / "broadcasts.jsonl").write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")
    (directory / "events.jsonl").write_text("".join(json.dumps(item) + "\n" for item in (events or [])), encoding="utf-8")
    if complete:
        hashes = {name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
                  for name in ("manifest.json", "broadcasts.jsonl", "events.jsonl")}
        (directory / "completed.json").write_text(json.dumps({"schemaVersion": 1, "sessionId": "fixture-session",
                                                             "status": "completed", "counts": stats, "fileHashes": hashes}),
                                                 encoding="utf-8")
    return directory


def route_payload():
    return {"KEY_TYPE": 10003, "route": {"polyline": [
        {"lat": 39.123456, "lon": 116.654321},
        {"lat": 39.123556, "lon": 116.654421},
        {"lat": 39.123656, "lon": 116.654521},
    ]}}


def test_route_polyline_is_available_as_shape_evidence_without_coordinates(tmp_path):
    payload = route_payload()
    payload["destinationName"] = "fixture-private-destination"
    result = analyzer.analyze_capture(capture(tmp_path, [record(payload)]))
    assert result["status"] == "available"
    assert result["integrity"]["status"] == "complete"
    candidate = result["route_candidates"][0]
    assert candidate["path"] == "$.extras.route.polyline"
    assert candidate["sequence"] == 1
    assert candidate["point_count"] == 3
    assert candidate["coordinate_ranges_valid"] is True
    assert candidate["consecutive_gap_check"] == "passed"
    assert candidate["complete_route_verified"] is False
    assert result["source_attribution"] == "unverified"
    assert result["counts"]["actions"] == {"AUTONAVI_STANDARD_BROADCAST_SEND": 1}
    assert result["counts"]["key_types"] == {"10003": 1}
    output = json.dumps(result)
    for secret in ("39.123456", "116.654321", "fixture-private-destination"):
        assert secret not in output
    assert result["control_input_generated"] is False


@pytest.mark.parametrize("payload", [
    {"KEY_TYPE": 10003, "route": {"polyline": []}},
    {"KEY_TYPE": 10065, "latitude": 39.1, "longitude": 116.2},
    {"KEY_TYPE": 10003, "destination": {"lat": 39.1, "lon": 116.2}},
    {"KEY_TYPE": 10003, "waypoints": [{"lat": 39.1, "lon": 116.2}, {"lat": 39.2, "lon": 116.3}, {"lat": 39.3, "lon": 116.4}]},
    {"KEY_TYPE": 10065, "location_history": [{"lat": 39.1, "lon": 116.2}, {"lat": 39.2, "lon": 116.3}, {"lat": 39.3, "lon": 116.4}]},
])
def test_empty_or_position_only_is_not_a_complete_route(tmp_path, payload):
    result = analyzer.analyze_capture(capture(tmp_path, [record(payload)]))
    assert result["status"] == "insufficient_evidence"
    assert not any(item["status"] == "available" for item in result["route_candidates"])
    assert result["coverage"]["negative_geometry_claim_allowed"] is False
    assert result["coverage"]["targeted_broadcast_coverage"] == "unknown"


def test_nested_bundle_and_json_string_route_are_supported(tmp_path):
    geometry = {"type": "LineString", "coordinates": [[116.2, 39.1], [116.2001, 39.1001], [116.2002, 39.1002]]}
    payload = {"KEY_TYPE": 10003, "EXTRA_ROUTE": {"detail": {"geometry": geometry}},
               "route_json": json.dumps(route_payload()["route"])}
    result = analyzer.analyze_capture(capture(tmp_path, [record(payload)]))
    assert result["status"] == "available"
    assert {item["path"] for item in result["route_candidates"]} == {
        "$.extras.EXTRA_ROUTE.detail.geometry.coordinates", "$.extras.route_json.polyline",
    }


def test_unknown_parcelable_is_unknown_not_empty_route(tmp_path):
    item = record({"KEY_TYPE": 10003})
    item["extras"]["value"]["route"] = {"type": "unsupported", "className": "vendor.RouteParcelable"}
    item["complete"] = False
    item["issues"] = [{"path": "$.extras.route", "reason": "unsupported_type", "className": "vendor.RouteParcelable"}]
    result = analyzer.analyze_capture(capture(tmp_path, [item], counts={"incomplete": 1}))
    assert result["status"] == "insufficient_evidence"
    assert result["counts"]["unknown_value_nodes"] == 1
    assert result["integrity"]["status"] == "incomplete"
    assert not result["route_candidates"]


def test_truncation_drop_and_interrupted_capture_remain_insufficient(tmp_path):
    item = record(route_payload(), sequence=2, complete=False, truncated=True,
                  issues=[{"path": "$.extras.route", "reason": "byte_limit"}])
    result = analyzer.analyze_capture(capture(tmp_path, [item], complete=False,
                                            counts={"received": 2, "dropped": 1, "incomplete": 1, "truncated": 1},
                                            events=[{"kind": "drop", "count": 1}]))
    assert result["status"] == "insufficient_evidence"
    assert result["integrity"]["status"] == "incomplete"
    assert "missing_completion_marker" in result["integrity"]["reasons"]
    assert result["counts"]["declared"]["dropped"] == 1
    assert result["counts"]["events"]["drop"] == 1
    assert result["route_candidates"][0]["status"] == "insufficient_evidence"


@pytest.mark.parametrize("points,reason", [
    ([{"lat": 95.0, "lon": 116.0}, {"lat": 39.1, "lon": 116.1}, {"lat": 39.2, "lon": 116.2}], "invalid_coordinate_range"),
    ([{"lat": 39.1, "lon": 116.1}, {"lat": 20.1, "lon": 116.1}, {"lat": 39.2, "lon": 116.2}], "large_consecutive_gap"),
    ([{"lat": 39.1, "lon": 116.1}] * 3, "degenerate_points"),
    ([[30.0, 40.0], [30.0001, 40.0001], [30.0002, 40.0002]], "ambiguous_coordinate_order"),
    ([{"lat": 39.1, "lon": 116.1}, {"lat": 39.1001, "lon": 116.1001}], "too_few_points"),
])
def test_unsafe_geometry_shapes_are_not_available(tmp_path, points, reason):
    result = analyzer.analyze_capture(capture(tmp_path, [record({"route": {"polyline": points}})]))
    assert result["status"] == "insufficient_evidence"
    assert reason in result["route_candidates"][0]["reasons"]


def test_sequence_gap_duplicate_and_cross_session_are_reported(tmp_path):
    items = [record(route_payload(), 1), record(route_payload(), 3), record(route_payload(), 3, sessionId="other-session")]
    result = analyzer.analyze_capture(capture(tmp_path, items))
    assert result["status"] == "insufficient_evidence"
    assert result["counts"]["sequences"]["missing"] == 1
    assert result["counts"]["sequences"]["duplicates"] == 1
    assert "session_mismatch" in result["integrity"]["reasons"]


def test_counter_mismatch_and_malformed_last_line_are_detected(tmp_path):
    directory = capture(tmp_path, [record(route_payload())], counts={"written": 3})
    with (directory / "broadcasts.jsonl").open("a", encoding="utf-8") as handle:
        handle.write('{"sequence":2')
    result = analyzer.analyze_capture(directory)
    assert result["status"] == "insufficient_evidence"
    assert result["counts"]["malformed_records"] == 1
    assert "written_count_mismatch" in result["integrity"]["reasons"]
    assert "unterminated_jsonl" in result["integrity"]["reasons"]


def test_root_missing_is_machine_readable(tmp_path):
    result = analyzer.analyze_capture(tmp_path / "absent")
    assert result["status"] == "insufficient_evidence"
    assert result["integrity"]["status"] == "incomplete"
    assert "missing_manifest" in result["integrity"]["reasons"]


def test_cli_outputs_only_json_without_raw_values(tmp_path):
    directory = capture(tmp_path, [record(route_payload())])
    completed = subprocess.run([sys.executable, str(Path(analyzer.__file__)), str(directory)], capture_output=True, text=True, check=False)
    assert completed.returncode == 0
    result = json.loads(completed.stdout)
    assert result["status"] == "available"
    assert completed.stderr == ""
    assert "39.123456" not in completed.stdout


def test_declared_array_length_mismatch_is_incomplete(tmp_path):
    item = record(route_payload())
    item["extras"]["value"]["route"]["value"]["polyline"]["length"] = 20
    result = analyzer.analyze_capture(capture(tmp_path, [item]))
    assert result["status"] == "insufficient_evidence"
    assert "typed_array_length_mismatch" in result["integrity"]["reasons"]


def test_nonfinite_and_boolean_coordinates_are_rejected(tmp_path):
    points = [{"lat": True, "lon": 116.2}, {"lat": "NaN", "lon": 116.2001}, {"lat": 39.1002, "lon": 116.2002}]
    result = analyzer.analyze_capture(capture(tmp_path, [record({"route": {"polyline": points}})]))
    assert result["status"] == "insufficient_evidence"
    assert result["route_candidates"][0]["coordinate_ranges_valid"] is False


def test_actual_top_level_manifest_and_scoped_coverage(tmp_path):
    directory = capture(tmp_path, [record(route_payload())])
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest.update(manifest.pop("counts"))
    manifest["scope"] = {"receiverScope": manifest.pop("receiverScope"),
                         "targetedBroadcastCoverage": manifest.pop("targetedBroadcastCoverage"), "senderIdentity": "unverified"}
    manifest["captureComplete"] = True
    manifest_path.write_text(json.dumps(manifest))
    completed_path = directory / "completed.json"
    completed = json.loads(completed_path.read_text())
    completed["fileHashes"]["manifest.json"] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    completed_path.write_text(json.dumps(completed))
    result = analyzer.analyze_capture(directory)
    assert result["status"] == "available"
    assert result["coverage"]["receiver_scope"] == "registered_actions"
    assert result["coverage"]["targeted_broadcast_coverage"] == "unknown"


def test_modified_export_file_fails_hash_verification(tmp_path):
    directory = capture(tmp_path, [record(route_payload())])
    events = directory / "events.jsonl"
    events.write_text('{"kind":"mark"}\n')
    result = analyzer.analyze_capture(directory)
    assert result["status"] == "insufficient_evidence"
    assert "file_hash_mismatch" in result["integrity"]["reasons"]


def test_integer_overflow_coordinate_does_not_crash_analyzer(tmp_path):
    payload = route_payload()
    payload["route"]["polyline"][0]["lat"] = 10 ** 400
    result = analyzer.analyze_capture(capture(tmp_path, [record(payload)]))
    assert result["status"] == "insufficient_evidence"
    assert result["route_candidates"][0]["coordinate_ranges_valid"] is False


def test_unreadable_and_drop_events_are_summarized_without_raw_values(tmp_path):
    item = record({"KEY_TYPE": 13012})
    item["extras"]["value"]["EXTRA_DRIVE_WAY"] = {"type": "unreadable", "className": "vendor.UnsupportedValue"}
    item["complete"] = False
    item["issues"] = [{"path": "$.extras.EXTRA_DRIVE_WAY", "reason": "decode_error"}]
    result = analyzer.analyze_capture(capture(tmp_path, [item], counts={"incomplete": 1, "decodeErrors": 1},
                                            events=[{"kind": "decode_error"}]))
    assert result["counts"]["unreadable_value_nodes"] == 1
    assert result["counts"]["issue_reasons"] == {"decode_error": 1}
    assert result["counts"]["events"] == {"decode_error": 1}
    assert result["status"] == "insufficient_evidence"


@pytest.mark.parametrize("node", [
    {"type": "char", "value": "A"},
    {"type": "double", "value": "NaN", "bits": "7ff8000000000000"},
    {"type": "double", "value": "Infinity", "bits": "7ff0000000000000"},
    {"type": "float", "value": "-Infinity", "bits": "ff800000"},
])
def test_codec_supported_scalar_does_not_invalidate_unrelated_geometry(tmp_path, node):
    item = record(route_payload())
    item["extras"]["value"]["unrelatedScalar"] = node
    result = analyzer.analyze_capture(capture(tmp_path, [item]))
    assert result["integrity"]["status"] == "complete"
    assert result["status"] == "available"
    assert result["route_candidates"][0]["complete_route_verified"] is False
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_codec_nonfinite_coordinate_is_readable_but_not_valid_geometry(tmp_path, value):
    item = record(route_payload())
    point = item["extras"]["value"]["route"]["value"]["polyline"]["value"][0]["value"]
    point["lat"] = {"type": "double", "value": value}
    result = analyzer.analyze_capture(capture(tmp_path, [item]))
    assert result["integrity"]["status"] == "complete"
    assert result["status"] == "insufficient_evidence"
    assert "invalid_coordinate_range" in result["route_candidates"][0]["reasons"]
    json.dumps(result, allow_nan=False)
