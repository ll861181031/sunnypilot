"""Fixed-size MGX3 navigation receipts and optional history-reset acknowledgment."""
import struct
import zlib


LEGACY_MAGIC = b'MGX3'
RECEIPT_MAGIC = b'MGX4'
REQUEST_RECEIPT = 4
REQUEST_RESET_MAP = 8
REQUEST_RESET_NAVIGATION = 16
REQUEST_NAV_SESSION = 32
RESET_FLAGS = REQUEST_RESET_MAP | REQUEST_RESET_NAVIGATION
RESET_RECEIPT_MAGIC = b'MGX5'
RESET_ACK_STRUCT = struct.Struct('<HHIQQ')
RESET_ACK_SIZE = RESET_ACK_STRUCT.size
RECEIPT_VERSION = 1
RECEIPT_STRUCT = struct.Struct('<HHIQQQQIIIIIIQQQII')
RECEIPT_SIZE = RECEIPT_STRUCT.size

FLAGS = {
  'capture': 1 << 0,
  'synchronized': 1 << 1,
  'feed_valid': 1 << 2,
  'staging_valid': 1 << 3,
  'output_valid': 1 << 4,
  'gpu_sampled': 1 << 5,
  'gpu_match': 1 << 6,
  'gpu_error': 1 << 7,
  'diagnostic_error': 1 << 8,
  'navigation_reset_supported': 1 << 9,
}
FIELDS = (
  'worker_session', 'step_seq', 'completed_mono_time', 'worker_step_ns',
  'feed_nav_crc32', 'feed_map_crc32', 'staging_nav_crc32', 'staging_map_crc32',
  'output_crc32', 'error_code', 'diagnostic_duration_ns', 'gpu_sample_mono_time',
  'gpu_sample_duration_ns', 'gpu_nav_crc32', 'gpu_map_crc32',
)
ERROR_FEED = 1
ERROR_STAGING = 2
ERROR_GPU = 4
ERROR_OUTPUT = 8
ERROR_ENCODING = 16


def crc32_buffer(value) -> int:
  """Hash the exact contiguous wire/staging bytes without allocating a copy."""
  return zlib.crc32(memoryview(value).cast('B')) & 0xffffffff


def encode_receipt(receipt: dict) -> bytes:
  flags = sum(mask for name, mask in FLAGS.items() if receipt.get(name, False))
  return RECEIPT_STRUCT.pack(RECEIPT_VERSION, RECEIPT_SIZE, flags,
                             *(int(receipt.get(name, 0)) for name in FIELDS))


def decode_receipt(payload: bytes) -> dict:
  if len(payload) != RECEIPT_SIZE:
    raise ValueError(f'Navigation receipt size {len(payload)} != {RECEIPT_SIZE}')
  version, size, flags, *values = RECEIPT_STRUCT.unpack(payload)
  if version != RECEIPT_VERSION or size != RECEIPT_SIZE or flags & ~sum(FLAGS.values()):
    raise ValueError('Unsupported navigation receipt header')
  return {'protocol_version': version, **dict(zip(FIELDS, values, strict=True)),
          **{name: bool(flags & mask) for name, mask in FLAGS.items()}}


def encode_reset_ack(flags: int, worker_session: int, step_seq: int) -> bytes:
  return RESET_ACK_STRUCT.pack(1, RESET_ACK_SIZE, flags, worker_session, step_seq)


def decode_reset_ack(payload: bytes) -> dict:
  if len(payload) != RESET_ACK_SIZE:
    raise ValueError('Invalid navigation reset acknowledgment size')
  version, size, flags, worker_session, step_seq = RESET_ACK_STRUCT.unpack(payload)
  if version != 1 or size != RESET_ACK_SIZE or not flags or flags & ~RESET_FLAGS or not worker_session or not step_seq:
    raise ValueError('Invalid navigation reset acknowledgment')
  return {'flags': flags, 'worker_session': worker_session, 'step_seq': step_seq}


ERROR_RECEIPT = encode_receipt({'diagnostic_error': True, 'error_code': ERROR_ENCODING})
