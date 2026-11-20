#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any

MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_LINE_BYTES = 16 * 1024 * 1024
MAX_NODES = 200_000
MAX_DEPTH = 48
MAX_CANDIDATES = 1000
MAX_CONSECUTIVE_GAP_M = 2000.0
COUNT_FIELDS = ("received", "written", "incomplete", "truncated", "dropped", "decodeErrors", "eventDropped")
MISSING = object()
SAFE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,159}$")


def _safe_name(value: Any) -> str:
    if isinstance(value, str) and SAFE_NAME.fullmatch(value):
        return value
    if value is None:
        return "unknown"
    return "redacted_" + hashlib.sha256(str(value).encode("utf-8", errors="replace")).hexdigest()[:12]


def _integer(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _reject_constant(_value: str):
    raise ValueError("nonfinite_json_number")


def _json(text: str | bytes):
    return json.loads(text, parse_constant=_reject_constant)


def _load_object(path: Path, reasons: set[str], label: str) -> dict:
    if not path.is_file():
        reasons.add("missing_" + label)
        return {}
    if path.is_symlink():
        reasons.add("symlink_" + label)
        return {}
    try:
        if path.stat().st_size > MAX_LINE_BYTES:
            reasons.add("oversized_" + label)
            return {}
        data = _json(path.read_bytes())
        if not isinstance(data, dict):
            raise ValueError("not_an_object")
        return data
    except (OSError, UnicodeError, ValueError, RecursionError):
        reasons.add("unreadable_" + label)
        return {}


def _records(path: Path, reasons: set[str], counters: Counter, label: str):
    if not path.is_file() or path.is_symlink():
        reasons.add("missing_" + label)
        return
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            reasons.add("oversized_" + label)
            return
        with path.open("rb") as handle:
            while raw := handle.readline(MAX_LINE_BYTES + 1):
                if len(raw) > MAX_LINE_BYTES:
                    reasons.add("oversized_jsonl_record")
                    counters["oversized_records"] += 1
                    while raw and not raw.endswith(b"\n"):
                        raw = handle.readline(MAX_LINE_BYTES + 1)
                    continue
                if not raw.endswith(b"\n"):
                    reasons.add("unterminated_jsonl")
                try:
                    data = _json(raw)
                    if not isinstance(data, dict):
                        raise ValueError("not_an_object")
                except (UnicodeError, ValueError, RecursionError):
                    counters["malformed_records"] += 1
                    reasons.add("malformed_jsonl")
                    continue
                yield data
    except OSError:
        reasons.add("unreadable_" + label)


class TypedDecoder:
    def __init__(self):
        self.counts = Counter()
        self.reasons: set[str] = set()
        self.nodes = 0

    def decode(self, node: Any, depth: int = 0):
        self.nodes += 1
        if depth > MAX_DEPTH or self.nodes > MAX_NODES:
            self.reasons.add("analysis_structure_limit")
            return MISSING
        if not isinstance(node, dict) or not isinstance(node.get("type"), str):
            self.reasons.add("invalid_typed_value")
            return MISSING
        kind, value = node["type"].lower(), node.get("value")
        if node.get("truncated") is True or node.get("complete") is False:
            self.reasons.add("incomplete_typed_value")
        if kind in ("unsupported", "unknown", "parcelable", "unreadable"):
            self.counts["unreadable_value_nodes" if kind == "unreadable" else "unknown_value_nodes"] += 1
            self.reasons.add("unknown_value" if kind != "unreadable" else "unreadable_value")
            return MISSING
        if kind in ("bundle", "map", "object"):
            if not isinstance(value, dict):
                self.reasons.add("invalid_typed_bundle")
                return MISSING
            return {key: self.decode(item, depth + 1) for key, item in value.items()}
        if kind in ("array", "list"):
            if not isinstance(value, list):
                self.reasons.add("invalid_typed_array")
                return MISSING
            if "length" in node and node["length"] != len(value):
                self.reasons.add("typed_array_length_mismatch")
            return [self.decode(item, depth + 1) for item in value]
        if kind == "null":
            return None
        if kind == "char":
            if isinstance(value, str) and len(value) == 1:
                return value
            self.reasons.add("invalid_typed_char")
            return MISSING
        if kind == "string":
            if not isinstance(value, str):
                self.reasons.add("invalid_typed_string")
                return MISSING
            if value.lstrip().startswith(("{", "[")):
                try:
                    return _json(value)
                except (ValueError, RecursionError):
                    self.counts["unparsed_json_strings"] += 1
            return value
        if kind in ("long", "int", "short", "byte"):
            try:
                if isinstance(value, bool) or isinstance(value, float):
                    raise ValueError("invalid_integer")
                return int(value)
            except (ValueError, TypeError, OverflowError):
                self.reasons.add("invalid_typed_number")
                return MISSING
        if kind in ("double", "float"):
            if isinstance(value, str) and value in ("NaN", "Infinity", "-Infinity"):
                return float(value)
            if _number(value) is None:
                self.reasons.add("invalid_typed_number")
                return MISSING
            return value
        if kind == "boolean" and type(value) is bool:
            return value
        self.counts["unknown_value_nodes"] += 1
        self.reasons.add("unknown_value")
        return MISSING


def _number(value: Any) -> float | None:
    try:
        number = float(value) if type(value) in (int, float) else None
        return number if number is not None and math.isfinite(number) else None
    except (ValueError, OverflowError):
        return None


def _point(value: Any, order: str | None) -> tuple[float, float] | None:
    if isinstance(value, dict):
        lat = _number(value.get("lat", value.get("latitude")))
        lon = _number(value.get("lon", value.get("lng", value.get("longitude"))))
    elif isinstance(value, list) and len(value) == 2 and order:
        first, second = map(_number, value)
        lat, lon = (first, second) if order == "lat_lon" else (second, first)
    else:
        return None
    if lat is None or lon is None or not -90 <= lat <= 90 or not -180 <= lon <= 180:
        return None
    return lat, lon


def _gap(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    haversine = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 12_742_000 * math.asin(math.sqrt(min(1.0, max(0.0, haversine))))


def _candidate(points: list, path: str, order: str | None, sequence: int | None, record_complete: bool) -> dict:
    reasons = []
    if len(points) < 3:
        reasons.append("too_few_points")
    pair_array = bool(points) and all(isinstance(item, list) and len(item) == 2 for item in points)
    if pair_array and order is None:
        possible = [name for name in ("lat_lon", "lon_lat") if all(_point(item, name) is not None for item in points)]
        order = possible[0] if len(possible) == 1 else None
        if len(possible) > 1:
            reasons.append("ambiguous_coordinate_order")
    decoded = [_point(item, order) for item in points]
    valid = bool(points) and all(item is not None for item in decoded)
    if not valid:
        reasons.append("invalid_coordinate_range")
    unique = len({point for point in decoded if point is not None})
    if valid and unique < 3:
        reasons.append("degenerate_points")
    gaps = sum(_gap(first, second) > MAX_CONSECUTIVE_GAP_M for first, second in zip(decoded, decoded[1:], strict=False)
               if first is not None and second is not None)
    if gaps:
        reasons.append("large_consecutive_gap")
    if not record_complete:
        reasons.append("incomplete_record")
    return {
        "path": path, "sequence": sequence, "point_count": len(points), "valid_point_count": sum(point is not None for point in decoded),
        "coordinate_order": order if pair_array else "named_lat_lon", "coordinate_ranges_valid": valid,
        "consecutive_gap_check": "failed" if gaps else "passed" if valid and len(points) >= 3 else "unknown",
        "large_gap_count": gaps, "unique_point_count": unique,
        "status": "insufficient_evidence" if reasons else "available", "reasons": reasons,
        "complete_route_verified": False, "coordinate_system": "unverified",
    }


def find_route_candidates(value: Any, sequence: int | None, record_complete: bool) -> list[dict]:
    candidates = []
    visited = 0

    def walk(node, path, route_context=False, excluded=False, order=None, depth=0):
        nonlocal visited
        visited += 1
        if depth > MAX_DEPTH or visited > MAX_NODES or len(candidates) >= MAX_CANDIDATES:
            return
        if not isinstance(node, dict):
            return
        geojson = node.get("type") == "LineString"
        explicit_order = node.get("coordinate_order", node.get("coordinateOrder"))
        if explicit_order in ("lat_lon", "lon_lat"):
            order = explicit_order
        for key, child in node.items():
            name = str(key).lower()
            child_path = path + "." + _safe_name(key)
            excluded_names = ("destination", "endpoi", "startpoi", "waypoint", "location", "position", "vehicle", "history", "gps")
            child_excluded = excluded or any(word in name for word in excluded_names)
            context = route_context or any(word in name for word in ("route", "path", "polyline", "geometry"))
            eligible = "polyline" in name or (context and name in ("points", "coordinates", "coords", "path", "routepoints", "route_points", "shape"))
            if geojson and name == "coordinates":
                eligible, context = True, True
            if isinstance(child, list) and eligible and not child_excluded:
                candidates.append(_candidate(child, child_path, "lon_lat" if geojson else order, sequence, record_complete))
            elif isinstance(child, dict):
                walk(child, child_path, context, child_excluded, order, depth + 1)
            elif isinstance(child, list) and not child_excluded:
                for index, item in enumerate(child):
                    if isinstance(item, dict):
                        walk(item, f"{child_path}[{index}]", context, child_excluded, order, depth + 1)

    walk(value, "$.extras")
    return candidates


def _verify_hashes(root: Path, completed: dict, reasons: set[str]) -> str:
    expected = completed.get("fileHashes")
    if not isinstance(expected, dict):
        reasons.add("missing_file_hashes")
        return "missing"
    matched = True
    for name in ("manifest.json", "broadcasts.jsonl", "events.jsonl"):
        digest = expected.get(name)
        path = root / name
        if not isinstance(digest, str) or re.fullmatch(r"[a-fA-F0-9]{64}", digest) is None:
            reasons.add("missing_file_hash")
            matched = False
            continue
        try:
            if path.is_symlink() or path.stat().st_size > MAX_FILE_BYTES:
                raise OSError("unavailable_file")
            with path.open("rb") as handle:
                actual = hashlib.file_digest(handle, "sha256").hexdigest()
            if actual != digest.lower():
                reasons.add("file_hash_mismatch")
                matched = False
        except OSError:
            reasons.add("hash_file_unavailable")
            matched = False
    return "matched" if matched else "mismatch"


def analyze_capture(directory: str | Path) -> dict:
    root = Path(directory)
    reasons: set[str] = set()
    manifest = _load_object(root / "manifest.json", reasons, "manifest")
    completed = _load_object(root / "completed.json", reasons, "completion_marker")
    if manifest.get("status") != "completed" or completed.get("status") != "completed":
        reasons.add("capture_not_completed")
    if manifest.get("captureComplete") is False or completed.get("captureComplete") is False:
        reasons.add("declared_incomplete")
    if manifest.get("schemaVersion") != 1:
        reasons.add("unsupported_manifest_schema")
    session = manifest.get("sessionId")
    if not isinstance(session, str) or not session:
        reasons.add("missing_session_identity")
    if completed.get("sessionId") not in (None, session):
        reasons.add("session_mismatch")
    declared = manifest.get("counts", manifest)
    declared = declared if isinstance(declared, dict) else {}
    stats = {name: _integer(declared.get(name)) for name in COUNT_FIELDS}
    if any(value is None for value in stats.values()):
        reasons.add("missing_capture_counters")
    final_counts = completed.get("counts")
    if isinstance(final_counts, dict) and any(final_counts.get(name) != stats[name] for name in COUNT_FIELDS):
        reasons.add("completion_counter_mismatch")
    hash_status = _verify_hashes(root, completed, reasons)
    actions, key_types, event_kinds, issue_reasons = Counter(), Counter(), Counter(), Counter()
    counts = Counter()
    sequences = []
    candidates = []
    for entry in _records(root / "broadcasts.jsonl", reasons, counts, "broadcasts"):
        counts["broadcast_records"] += 1
        if entry.get("schemaVersion") != 1:
            reasons.add("unsupported_record_schema")
        if entry.get("sessionId") != session:
            reasons.add("session_mismatch")
        sequence = _integer(entry.get("sequence"))
        if sequence is None or sequence == 0:
            reasons.add("invalid_record_sequence")
        else:
            sequences.append(sequence)
        actions[_safe_name(entry.get("action"))] += 1
        record_complete = entry.get("complete") is True and entry.get("truncated") is False
        if not record_complete:
            counts["incomplete_records"] += 1
            reasons.add("incomplete_record")
        if entry.get("truncated") is True:
            counts["truncated_records"] += 1
        decoder = TypedDecoder()
        values = decoder.decode(entry.get("extras"))
        reasons.update(decoder.reasons)
        counts.update(decoder.counts)
        key_type = values.get("KEY_TYPE") if isinstance(values, dict) else None
        key_types[str(key_type) if type(key_type) is int else "unknown"] += 1
        issues = entry.get("issues")
        if not isinstance(issues, list):
            reasons.add("missing_issue_metadata")
        elif issues:
            counts["record_issues"] += len(issues)
            reasons.add("record_issues")
            for issue in issues:
                issue_reasons[_safe_name(issue.get("reason") if isinstance(issue, dict) else None)] += 1
        found = find_route_candidates(values, sequence, record_complete and not decoder.reasons and not issues)
        if len(candidates) + len(found) > MAX_CANDIDATES:
            reasons.add("candidate_summary_limit")
        candidates.extend(found[:max(0, MAX_CANDIDATES - len(candidates))])
    for event in _records(root / "events.jsonl", reasons, counts, "events"):
        event_kinds[_safe_name(event.get("kind"))] += 1
        if event.get("sessionId") not in (None, session):
            reasons.add("session_mismatch")
        if event.get("kind") in ("drop", "truncation", "decode_error", "error"):
            reasons.add("capture_loss_event")
    unique = set(sequences)
    sequence_stats = {
        "first": min(unique) if unique else None, "last": max(unique) if unique else None,
        "missing": max(unique) - len(unique) if unique else 0, "duplicates": len(sequences) - len(unique),
        "out_of_order": sum(right < left for left, right in zip(sequences, sequences[1:], strict=False)),
    }
    if any(sequence_stats[key] for key in ("missing", "duplicates", "out_of_order")):
        reasons.add("record_sequence_discontinuity")
    if stats["written"] != counts["broadcast_records"]:
        reasons.add("written_count_mismatch")
    if all(stats[name] is not None for name in ("received", "written", "dropped")) and stats["received"] != stats["written"] + stats["dropped"]:
        reasons.add("received_count_mismatch")
    if any(stats[name] for name in ("incomplete", "truncated", "dropped", "decodeErrors", "eventDropped")):
        reasons.add("capture_reports_loss")
    complete = not reasons
    available = complete and any(item["status"] == "available" for item in candidates)
    scope = manifest.get("scope", manifest)
    scope = scope if isinstance(scope, dict) else {}
    return {
        "schema_version": 1, "status": "available" if available else "insufficient_evidence",
        "status_meaning": "geometry_shape_evidence_only", "control_input_generated": False,
        "source_attribution": "unverified",
        "integrity": {"status": "complete" if complete else "incomplete", "hash_status": hash_status, "reasons": sorted(reasons)},
        "coverage": {
            "receiver_scope": _safe_name(scope.get("receiverScope")),
            "targeted_broadcast_coverage": "confirmed" if scope.get("targetedBroadcastCoverage") == "confirmed" else "unknown",
            "negative_geometry_claim_allowed": False,
        },
        "counts": {
            **{name: counts[name] for name in ("broadcast_records", "incomplete_records", "truncated_records", "malformed_records", "oversized_records",
                                             "unknown_value_nodes", "unreadable_value_nodes", "unparsed_json_strings", "record_issues")},
            "declared": stats, "actions": dict(actions), "key_types": dict(key_types), "events": dict(event_kinds), "sequences": sequence_stats,
            "issue_reasons": dict(issue_reasons),
        },
        "route_candidates": candidates,
        "conclusion": "发现坐标数组候选，尚未确认其是当前完整导航路线。" if available else "证据不足；未发现可用候选不等于来源不发送路线。",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_directory", type=Path)
    args = parser.parse_args()
    print(json.dumps(analyze_capture(args.capture_directory), ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
