from dataclasses import replace
import json
import struct

from Crypto.Cipher import AES
import pytest

from tools.cplink_shim import amap_metadata_codec as codec


@pytest.fixture
def material():
  return codec.CryptoMaterial(key=bytes(range(16)), iv=bytes(range(16, 32)), marker=b"synthetic-marker", profile="synthetic")


def encrypt_raw(plain, material):
  return AES.new(material.key, AES.MODE_CBC, material.iv).encrypt(plain)


@pytest.mark.parametrize("payload_size", [0, 1, 11, 12, 13, 27, 28, 29, 414])
def test_envelope_exact_byte_roundtrip_and_padding_boundaries(material, payload_size):
  payload = bytes((i * 7) % 256 for i in range(payload_size))
  ciphertext = codec.encode_envelope(payload, material)
  assert len(ciphertext) == ((20 + payload_size + 15) // 16) * 16
  assert codec.decode_envelope(ciphertext, material) == payload
  assert codec.encode_envelope(codec.decode_envelope(ciphertext, material), material) == ciphertext


def test_realistic_metadata_schema_does_not_report_values_or_route_success(material):
  metadata = {"scene": 1, "type": 2, "mainIdx": 0, "savetime": 123,
              "endPoints": [{"name": "synthetic-destination", "lon": 123.4567, "lat": 45.6789}]}
  payload = json.dumps(metadata).encode()
  ciphertext = codec.encode_envelope(payload, material)
  assert codec.decode_metadata(ciphertext, material) == metadata
  report = codec.inspect_metadata(ciphertext, material)
  assert report["metadata_valid"] is True
  assert report["ciphertext_roundtrip"] is True
  assert report["payload_bytes"] == len(payload)
  assert report["endpoint_count"] == 1
  assert report["complete_route_verified"] is False
  assert report["route_identity_verified"] is False
  assert report["control_input_generated"] is False
  printed = json.dumps(report)
  for value in ("synthetic-destination", "123.4567", "45.6789"):
    assert value not in printed


@pytest.mark.parametrize("ciphertext", [b"", b"x", b"x" * 16, b"x" * 31, b"x" * 33])
def test_incomplete_or_unaligned_ciphertext_is_rejected(material, ciphertext):
  with pytest.raises(codec.MetadataDecodeError, match="ciphertext_length"):
    codec.decode_envelope(ciphertext, material)


@pytest.mark.parametrize("field", ["key", "iv", "marker"])
def test_wrong_material_is_rejected_without_disclosing_values(material, field):
  ciphertext = codec.encode_envelope(b"{}", material)
  wrong = replace(material, **{field: b"x" * 16})
  with pytest.raises(codec.MetadataDecodeError, match="marker_mismatch"):
    codec.decode_envelope(ciphertext, wrong)
  assert material.key.hex() not in repr(material)
  assert material.iv.hex() not in repr(material)


@pytest.mark.parametrize("declared_size", [0xFFFFFFFF, 13])
def test_declared_length_must_fit_available_bytes(material, declared_size):
  plain = material.marker + struct.pack("<I", declared_size) + b"{}" + b"\0" * 10
  with pytest.raises(codec.MetadataDecodeError, match="payload_length"):
    codec.decode_envelope(encrypt_raw(plain, material), material)


def test_nonzero_padding_is_rejected(material):
  plain = material.marker + struct.pack("<I", 2) + b"{}" + b"\0" * 9 + b"x"
  with pytest.raises(codec.MetadataDecodeError, match="padding"):
    codec.decode_envelope(encrypt_raw(plain, material), material)


def test_extra_full_zero_block_is_rejected(material):
  plain = material.marker + struct.pack("<I", 2) + b"{}" + b"\0" * 26
  with pytest.raises(codec.MetadataDecodeError, match="padding"):
    codec.decode_envelope(encrypt_raw(plain, material), material)


@pytest.mark.parametrize("payload", [b"not json", b"\xff", b"[]", b"null", b"true", b"1",
  b'{"x":NaN}', b'{"x":Infinity}', b'{"x":-Infinity}', b'{"x":1e1000}', b'{"x":1,"x":2}'])
def test_invalid_or_nonobject_json_is_rejected(material, payload):
  with pytest.raises(codec.MetadataDecodeError, match="json"):
    codec.decode_metadata(codec.encode_envelope(payload, material), material)


def test_size_limits_are_checked_before_decrypt(material, monkeypatch):
  monkeypatch.setattr(codec, "MAX_ENVELOPE_BYTES", 32)
  with pytest.raises(codec.MetadataDecodeError, match="size_limit"):
    codec.decode_envelope(b"x" * 48, material)
  with pytest.raises(codec.MetadataDecodeError, match="size_limit"):
    codec.encode_envelope(b"x" * 13, material)


def test_metadata_limit_is_separate_from_binary_envelope(material, monkeypatch):
  ciphertext = codec.encode_envelope(b'{"message":"synthetic"}', material)
  monkeypatch.setattr(codec, "MAX_METADATA_BYTES", 32)
  assert codec.decode_envelope(ciphertext, material)
  with pytest.raises(codec.MetadataDecodeError, match="size_limit"):
    codec.decode_metadata(ciphertext, material)


@pytest.mark.parametrize("field", ["key", "iv", "marker"])
def test_material_requires_exact_aes128_lengths(material, field):
  with pytest.raises(codec.MetadataDecodeError, match="material_length"):
    replace(material, **{field: b"short"})


def test_unknown_profile_is_rejected_before_file_read(tmp_path):
  with pytest.raises(codec.MetadataDecodeError, match="unsupported_profile"):
    codec.load_material(tmp_path / "missing-gbl", tmp_path / "missing-gcomm", profile="unknown")


def test_changed_library_is_rejected_before_offset_extraction(tmp_path):
  first, second = tmp_path / "gbl", tmp_path / "gcomm"
  first.write_bytes(b"not the validated ELF")
  second.write_bytes(b"also not the validated ELF")
  with pytest.raises(codec.MetadataDecodeError, match="library_hash_mismatch"):
    codec.load_material(first, second, profile=codec.SUPPORTED_PROFILE)


def test_elf_virtual_address_uses_load_mapping():
  data = bytearray(256)
  data[:6] = b"\x7fELF\x02\x01"
  struct.pack_into("<H", data, 18, 183)
  struct.pack_into("<Q", data, 32, 64)
  struct.pack_into("<HH", data, 54, 56, 1)
  struct.pack_into("<IIQQQQQQ", data, 64, 1, 4, 128, 0x2000, 0, 128, 128, 4096)
  data[144:148] = b"test"
  assert codec._elf_bytes_at(bytes(data), 0x2010, 4) == b"test"
  with pytest.raises(codec.MetadataDecodeError, match="elf_address"):
    codec._elf_bytes_at(bytes(data), 0x207F, 2)


@pytest.mark.parametrize("data", [b"", b"not ELF", b"\x7fELF" + b"\0" * 100])
def test_malformed_elf_is_rejected(data):
  with pytest.raises(codec.MetadataDecodeError, match="elf"):
    codec._elf_bytes_at(data, 0, 16)
