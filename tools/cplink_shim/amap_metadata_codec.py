#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import math
from pathlib import Path
import struct

from Crypto.Cipher import AES


SUPPORTED_PROFILE = "amapauto-arm64-934396f73adf"
GBL_SHA256 = "934396f73adf760af02e77ae8bd650c4e659522acd28db35f501d7633675a419"
GCOMM_SHA256 = "5dc6970a99a7bfd60276e0094602376a77057c2dbfa4a0ced59941df31bd5f4b"
MAX_LIBRARY_BYTES = 128 * 1024 * 1024
MAX_ENVELOPE_BYTES = 64 * 1024 * 1024
MAX_METADATA_BYTES = 1024 * 1024


class MetadataDecodeError(ValueError):
  pass


@dataclass(frozen=True)
class CryptoMaterial:
  key: bytes = field(repr=False)
  iv: bytes = field(repr=False)
  marker: bytes = field(repr=False)
  profile: str

  def __post_init__(self):
    if any(len(value) != 16 for value in (self.key, self.iv, self.marker)):
      raise MetadataDecodeError("material_length")


def _read_bounded(path: Path, limit: int) -> bytes:
  with path.open("rb") as handle:
    data = handle.read(limit + 1)
  if len(data) > limit:
    raise MetadataDecodeError("size_limit")
  return data


def _elf_bytes_at(data: bytes, address: int, size: int) -> bytes:
  if len(data) < 64 or data[:6] != b"\x7fELF\x02\x01" or struct.unpack_from("<H", data, 18)[0] != 183:
    raise MetadataDecodeError("unsupported_elf")
  phoff = struct.unpack_from("<Q", data, 32)[0]
  phentsize, phnum = struct.unpack_from("<HH", data, 54)
  if phentsize < 56 or phnum > 1024 or phoff + phentsize * phnum > len(data):
    raise MetadataDecodeError("invalid_elf_headers")
  for index in range(phnum):
    p_type, _, offset, virtual, _, file_size, _, _ = struct.unpack_from("<IIQQQQQQ", data, phoff + index * phentsize)
    if p_type == 1 and virtual <= address and size >= 0 and address + size <= virtual + file_size:
      file_offset = offset + address - virtual
      if file_offset + size > len(data):
        raise MetadataDecodeError("invalid_elf_segment")
      return data[file_offset:file_offset + size]
  raise MetadataDecodeError("elf_address_not_backed_by_file")


def load_material(gbl_path: Path, gcomm_path: Path, *, profile: str) -> CryptoMaterial:
  if profile != SUPPORTED_PROFILE:
    raise MetadataDecodeError("unsupported_profile")
  gbl = _read_bounded(Path(gbl_path), MAX_LIBRARY_BYTES)
  gcomm = _read_bounded(Path(gcomm_path), MAX_LIBRARY_BYTES)
  if hashlib.sha256(gbl).hexdigest() != GBL_SHA256 or hashlib.sha256(gcomm).hexdigest() != GCOMM_SHA256:
    raise MetadataDecodeError("library_hash_mismatch")
  key = bytes(value ^ 0x58 for value in _elf_bytes_at(gcomm, 0x66F7C, 16))
  iv = bytes(value ^ 0x58 for value in _elf_bytes_at(gcomm, 0x66F8C, 16))
  return CryptoMaterial(key=key, iv=iv, marker=_elf_bytes_at(gbl, 0x2A5E208, 16), profile=profile)


def encode_envelope(payload: bytes, material: CryptoMaterial) -> bytes:
  padded_size = ((20 + len(payload) + 15) // 16) * 16
  if padded_size > MAX_ENVELOPE_BYTES:
    raise MetadataDecodeError("size_limit")
  plain = material.marker + struct.pack("<I", len(payload)) + payload
  plain += b"\0" * (padded_size - len(plain))
  return AES.new(material.key, AES.MODE_CBC, material.iv).encrypt(plain)


def decode_envelope(ciphertext: bytes, material: CryptoMaterial) -> bytes:
  if len(ciphertext) > MAX_ENVELOPE_BYTES:
    raise MetadataDecodeError("size_limit")
  if len(ciphertext) < 32 or len(ciphertext) % 16:
    raise MetadataDecodeError("ciphertext_length")
  plain = AES.new(material.key, AES.MODE_CBC, material.iv).decrypt(ciphertext)
  if not hmac.compare_digest(plain[:16], material.marker):
    raise MetadataDecodeError("marker_mismatch")
  payload_size = struct.unpack_from("<I", plain, 16)[0]
  end = 20 + payload_size
  if end > len(plain):
    raise MetadataDecodeError("payload_length")
  padding = plain[end:]
  if len(padding) > 15 or any(padding):
    raise MetadataDecodeError("invalid_zero_padding")
  return plain[20:end]


def _json_object(payload: bytes) -> dict:
  def reject_constant(_):
    raise MetadataDecodeError("invalid_json_constant")

  def finite_float(text):
    value = float(text)
    if not math.isfinite(value):
      raise MetadataDecodeError("invalid_json_number")
    return value

  def unique_keys(pairs):
    result = {}
    for key, value in pairs:
      if key in result:
        raise MetadataDecodeError("duplicate_json_key")
      result[key] = value
    return result

  try:
    obj = json.loads(payload.decode("utf-8"), parse_constant=reject_constant,
                     parse_float=finite_float, object_pairs_hook=unique_keys)
  except (ValueError, UnicodeError, RecursionError):
    raise MetadataDecodeError("invalid_json") from None
  if not isinstance(obj, dict):
    raise MetadataDecodeError("json_object_required")
  return obj


def decode_metadata(ciphertext: bytes, material: CryptoMaterial) -> dict:
  if len(ciphertext) > MAX_METADATA_BYTES:
    raise MetadataDecodeError("size_limit")
  return _json_object(decode_envelope(ciphertext, material))


def _schema(value, depth=0):
  if depth >= 8:
    return {"type": type(value).__name__, "truncated": True}
  if isinstance(value, dict):
    return {"type": "object", "fields": {key: _schema(item, depth + 1) for key, item in value.items()}}
  if isinstance(value, list):
    return {"type": "array", "count": len(value), "sample_types": [_schema(item, depth + 1) for item in value[:2]]}
  if isinstance(value, str):
    return {"type": "string", "length": len(value)}
  return {"type": "null" if value is None else type(value).__name__}


def inspect_metadata(ciphertext: bytes, material: CryptoMaterial) -> dict:
  if len(ciphertext) > MAX_METADATA_BYTES:
    raise MetadataDecodeError("size_limit")
  payload = decode_envelope(ciphertext, material)
  metadata = _json_object(payload)
  endpoints = metadata.get("endPoints")
  return {"schema_version": 1, "profile": material.profile, "metadata_valid": True,
          "ciphertext_bytes": len(ciphertext), "ciphertext_sha256": hashlib.sha256(ciphertext).hexdigest(),
          "payload_bytes": len(payload), "zero_padding_bytes": len(ciphertext) - 20 - len(payload),
          "ciphertext_roundtrip": encode_envelope(payload, material) == ciphertext,
          "endpoint_count": len(endpoints) if isinstance(endpoints, list) else None,
          "schema": _schema(metadata), "complete_route_verified": False,
          "route_identity_verified": False, "control_input_generated": False}


def main() -> None:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("metadata_file", type=Path)
  parser.add_argument("--gbl-library", type=Path, required=True)
  parser.add_argument("--gcomm-library", type=Path, required=True)
  parser.add_argument("--profile", choices=[SUPPORTED_PROFILE], required=True)
  args = parser.parse_args()
  try:
    material = load_material(args.gbl_library, args.gcomm_library, profile=args.profile)
    report = inspect_metadata(_read_bounded(args.metadata_file, MAX_METADATA_BYTES), material)
  except MetadataDecodeError as error:
    print(json.dumps({"metadata_valid": False, "reason": str(error), "control_input_generated": False}))
    raise SystemExit(1) from None
  except OSError:
    print(json.dumps({"metadata_valid": False, "reason": "file_read_error", "control_input_generated": False}))
    raise SystemExit(1) from None
  print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
  main()
