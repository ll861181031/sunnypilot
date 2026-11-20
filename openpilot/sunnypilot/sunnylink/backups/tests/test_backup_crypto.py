import base64
import os

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from openpilot.sunnypilot.sunnylink.backups import utils
from openpilot.sunnypilot.sunnylink.backups.AESCipher import AESCipher


def _write_keypair(tmp_path):
  key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
  private_path = tmp_path / "device"
  private_path.write_bytes(key.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
  ))
  public_path = tmp_path / "device.pub"
  public_path.write_bytes(key.public_key().public_bytes(
    serialization.Encoding.PEM,
    serialization.PublicFormat.SubjectPublicKeyInfo,
  ))
  return private_path, public_path


def test_v2_uses_private_key_and_a_fresh_iv(monkeypatch, tmp_path):
  private_path, _ = _write_keypair(tmp_path)
  monkeypatch.setattr(utils, "get_key_path", lambda use_aes_256=False: str(private_path))

  first = utils.encrypt_compressed_data('{"answer": 42}')
  second = utils.encrypt_compressed_data('{"answer": 42}')

  raw_first = base64.b64decode(first)
  raw_second = base64.b64decode(second)
  assert raw_first.startswith(utils.BACKUP_MAGIC)
  assert raw_first[4:20] != raw_second[4:20], "each backup needs a fresh IV"
  assert utils.decrypt_compressed_data(first) == '{"answer": 42}'
  assert utils.decrypt_compressed_data(second) == '{"answer": 42}'


def test_legacy_v1_payload_still_restores(monkeypatch, tmp_path):
  _, public_path = _write_keypair(tmp_path)
  monkeypatch.setattr(utils, "get_key_path", lambda use_aes_256=False: str(public_path))

  key, iv = utils.KeyDerivation.derive_aes_key_iv(str(public_path), True)
  ciphertext = AESCipher(key, iv).encrypt(utils.compress_dat(b'{"legacy": true}'))
  encoded = base64.b64encode(ciphertext).decode()
  assert utils.decrypt_compressed_data(encoded, use_aes_256=True) == '{"legacy": true}'


def test_aes_rejects_a_corrupt_padding_block():
  key, iv = os.urandom(32), os.urandom(16)
  cipher = AESCipher(key, iv)
  blob = bytearray(cipher.encrypt(b"hello"))
  blob[-1] ^= 0xFF
  with pytest.raises(ValueError, match="padding"):
    cipher.decrypt(bytes(blob))
