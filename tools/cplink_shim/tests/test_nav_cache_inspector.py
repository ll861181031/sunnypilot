import struct
import zlib

import pytest

from tools.cplink_shim.nav_cache_inspector import MAGIC, inspect_cache


def container(payload=b"\xff\x00\n\rsynthetic-payload"):
  return MAGIC + struct.pack("<II", len(payload), zlib.crc32(payload)) + payload


def test_valid_container_never_claims_route_identity_or_geometry():
  report = inspect_cache(container())
  assert report["container_valid"] is True
  assert report["complete_route_verified"] is False
  assert report["route_identity_verified"] is False
  assert report["control_input_generated"] is False
  assert report["declared_payload_bytes"] == report["actual_payload_bytes"]


@pytest.mark.parametrize("data,reason", [(b"", "incomplete_header"), (MAGIC, "incomplete_header"),
  (b"unknown!" + b"\0" * 16, "unknown_container_magic"), (container(b""), "empty_payload"),
  (container()[:-1], "payload_length_mismatch"), (container() + b"x", "payload_length_mismatch")])
def test_invalid_containers_are_not_accepted(data, reason):
  report = inspect_cache(data)
  assert report["container_valid"] is False
  assert reason in report["reasons"]


def test_same_length_corruption_is_detected_by_crc():
  data = bytearray(container())
  data[-1] ^= 1
  report = inspect_cache(bytes(data))
  assert report["container_valid"] is False
  assert report["reasons"] == ["payload_crc_mismatch"]


def test_declared_length_is_not_used_to_allocate_memory():
  data = MAGIC + struct.pack("<II", 0xFFFFFFFF, 0) + b"x"
  report = inspect_cache(data)
  assert report["container_valid"] is False
  assert report["actual_payload_bytes"] == 1
