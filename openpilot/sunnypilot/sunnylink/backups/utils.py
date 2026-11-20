"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import base64
import hashlib
import os
import zlib
import re
import json
from pathlib import Path

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa, ec

from openpilot.common.api.base import KEYS
from openpilot.sunnypilot.sunnylink.backups.AESCipher import AESCipher
from openpilot.common.hardware.hw import Paths


BACKUP_MAGIC = b"SPB2"


class KeyDerivation:
  @staticmethod
  def _load_key(file_path: str) -> bytes:
    with open(file_path, 'rb') as f:
      return f.read()

  @staticmethod
  def derive_aes_key_iv(key_path: str, use_aes_256: bool) -> tuple[bytes, bytes]:
    key_pem: bytes = KeyDerivation._load_key(key_path)
    key_plain = key_pem.decode(errors="ignore")

    if "private" in key_plain.lower():
      private_key = serialization.load_pem_private_key(key_pem, password=None, backend=default_backend())
      if isinstance(private_key, (rsa.RSAPrivateKey, ec.EllipticCurvePrivateKey)):
        public_key = private_key.public_key()
      else:
        raise ValueError("Invalid key format: Unable to determine if key is public or private.")
    elif "public" in key_plain.lower():
      public_key = serialization.load_pem_public_key(key_pem, backend=default_backend())
      if not isinstance(public_key, (rsa.RSAPublicKey, ec.EllipticCurvePublicKey)):
        raise ValueError("Invalid key format: Unable to determine if key is public or private.")
    else:
      raise ValueError("Invalid key format: Unable to determine if key is public or private.")

    if isinstance(public_key, rsa.RSAPublicKey):
      der_data = public_key.public_bytes(encoding=serialization.Encoding.DER, format=serialization.PublicFormat.PKCS1)
    elif isinstance(public_key, ec.EllipticCurvePublicKey):
      der_data = public_key.public_bytes(encoding=serialization.Encoding.DER, format=serialization.PublicFormat.SubjectPublicKeyInfo)
    else:
      raise ValueError("Unsupported key type.")

    if use_aes_256:
      # AES-256-CBC
      key = hashlib.sha256(der_data).digest()
      iv = hashlib.md5(der_data).digest()
    else:
      # AES-128-CBC
      key = hashlib.md5(der_data).digest()
      iv = hashlib.md5(der_data).digest()  # Insecure IV reuse, kept for compatibility

    return key, iv

  @staticmethod
  def derive_backup_key() -> bytes:
    """Key for the v2 envelope, derived from the device private key.

    The old path derived key+IV from the PUBLIC key, which every API client can
    read; that made the backup encryption nominal. The private key never leaves
    the device, and the IV is random per backup (stored in the envelope).
    """
    key_pem = KeyDerivation._load_key(get_key_path(use_aes_256=True))
    private_key = serialization.load_pem_private_key(key_pem, password=None, backend=default_backend())
    der = private_key.private_bytes(
      encoding=serialization.Encoding.DER,
      format=serialization.PrivateFormat.PKCS8,
      encryption_algorithm=serialization.NoEncryption(),
    )
    return hashlib.sha256(der).digest()


def uncompress_dat(data):
  """
  Decompress data using zlib.

  Args:
      data (bytes): Compressed data

  Returns:
      bytes: Decompressed data
  """
  if not data.startswith(b"ZLIB"):
    raise ValueError("compressed payload has no ZLIB magic")
  return zlib.decompress(data[4:])


def compress_dat(data):
  """
  Compress data using zlib.

  Args:
      data (bytes): Data to compress

  Returns:
      bytes: Compressed data
  """
  compressed_data = zlib.compress(data, level=9)
  return b"ZLIB" + compressed_data


def get_key_path(use_aes_256=False) -> str:
  key_path = ""
  for key in KEYS:
    if os.path.isfile(Paths.persist_root() + f'/comma/{key}') and os.path.isfile(Paths.persist_root() + f'/comma/{key}.pub'):
      key_path = str(Path(Paths.persist_root() + f'/comma/{key}') if use_aes_256 else Path(Paths.persist_root() + f'/comma/{key}.pub'))
      break

  if not key_path:
    raise FileNotFoundError("No valid key pair found in persist storage.")

  return key_path


def decrypt_compressed_data(encrypted_base64, use_aes_256=False):
  """Decrypt and decompress a backup payload.

  Supports the v2 envelope (private-key derivation + random IV) and the legacy
  v1 format (public-key derivation + fixed IV) so existing backups can still be
  restored. Failures raise; the manager reports them instead of treating an empty
  string as a successful restore.
  """
  encrypted_data = base64.b64decode(encrypted_base64)

  if encrypted_data.startswith(BACKUP_MAGIC):
    if len(encrypted_data) < len(BACKUP_MAGIC) + 16:
      raise ValueError("backup v2 payload is too short")
    iv = encrypted_data[len(BACKUP_MAGIC):len(BACKUP_MAGIC) + 16]
    ciphertext = encrypted_data[len(BACKUP_MAGIC) + 16:]
    key = KeyDerivation.derive_backup_key()
  else:
    # Legacy v1: fixed IV and public-key derivation.
    key, iv = KeyDerivation.derive_aes_key_iv(get_key_path(use_aes_256), use_aes_256)
    ciphertext = encrypted_data

  decrypted_data = AESCipher(key, iv).decrypt(ciphertext)
  return uncompress_dat(decrypted_data).decode("utf-8")


def encrypt_compressed_data(text, use_aes_256=True):
  """Compress and encrypt a backup payload into the v2 envelope.

  v2 = b"SPB2" + 16-byte random IV + AES-256-CBC(PKCS#7). The ``use_aes_256``
  argument is kept for call-site compatibility; v2 always uses AES-256 because the
  key is now a SHA-256 of private-key material.
  """
  compressed_data = compress_dat(text.encode("utf-8"))
  iv = os.urandom(16)
  ciphertext = AESCipher(KeyDerivation.derive_backup_key(), iv).encrypt(compressed_data)
  return base64.b64encode(BACKUP_MAGIC + iv + ciphertext).decode("utf-8")


def camel_to_snake(name):
  """Convert camelCase to snake_case."""
  name = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', name)
  return re.sub('([a-z0-9])([A-Z])', r'\1_\2', name).lower()


def transform_dict(obj):
  """Recursively transform dictionary keys from camelCase to snake_case."""
  if isinstance(obj, dict):
    return {camel_to_snake(k): transform_dict(v) for k, v in obj.items()}
  elif isinstance(obj, list):
    return [transform_dict(item) for item in obj]
  return obj


class SnakeCaseEncoder(json.JSONEncoder):
  def encode(self, o):
    transformed_obj = transform_dict(o)
    return super().encode(transformed_obj)
