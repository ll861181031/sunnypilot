#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import struct
import zlib


MAGIC = b"BL-CRRD\0"
MAX_CACHE_BYTES = 64 * 1024 * 1024


def inspect_cache(data: bytes) -> dict:
  report = {"schema_version": 1, "bytes": len(data), "container_valid": False,
            "complete_route_verified": False, "route_identity_verified": False,
            "control_input_generated": False, "reasons": []}
  if len(data) > MAX_CACHE_BYTES:
    report["reasons"].append("cache_size_limit")
    return report
  report["sha256"] = hashlib.sha256(data).hexdigest()
  if len(data) < 16:
    report["reasons"].append("incomplete_header")
    return report
  if data[:8] != MAGIC:
    report["reasons"].append("unknown_container_magic")
    return report
  declared_length, declared_crc = struct.unpack_from("<II", data, 8)
  payload = memoryview(data)[16:]
  actual_crc = zlib.crc32(payload) & 0xFFFFFFFF
  report.update({"format": "BL-CRRD", "declared_payload_bytes": declared_length,
                 "actual_payload_bytes": len(payload), "declared_crc32": f"{declared_crc:08x}",
                 "actual_crc32": f"{actual_crc:08x}"})
  if declared_length != len(payload):
    report["reasons"].append("payload_length_mismatch")
  if declared_crc != actual_crc:
    report["reasons"].append("payload_crc_mismatch")
  if not payload:
    report["reasons"].append("empty_payload")
  report["container_valid"] = not report["reasons"]
  return report


def main() -> None:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("cache_file", type=Path)
  args = parser.parse_args()
  with args.cache_file.open("rb") as handle:
    data = handle.read(MAX_CACHE_BYTES + 1)
  print(json.dumps(inspect_cache(data), ensure_ascii=False, indent=2))


if __name__ == "__main__":
  main()
