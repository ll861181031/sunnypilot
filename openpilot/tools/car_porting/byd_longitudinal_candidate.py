"""Diagnostic-only 0x32E hypotheses. No bus, sendcan tuple, or device transport."""

import math
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP

from openpilot.tools.car_porting.byd_longitudinal_audit import decode_frame


@dataclass(frozen=True)
class CandidateEncoding:
  payload_hex: str
  quantized_accel_mps2: float
  control_ready: bool = field(default=False, init=False)


def validate_template(template: bytes) -> dict[str, float]:
  """Accept a narrow observed moving template, not an ECU command contract.

  Layout/scaling follow byd_song_plus_dmi_23_alt.dbc; literal capture fixtures
  are in test_byd_longitudinal_shadow.py. Preserving the other bytes is NOT
  proof they remain semantically compatible with a different acceleration.
  """
  values = decode_frame(0x32E, template)
  if (template[0] == 0 or template[1:3] != b'\x66\x66' or not template[3] & 0x80 or
      template[6] & 0xF0 != 0xF0 or values['ESP_BEHAVIOR'] != 3 or values['BRAKE_BEHAVIOR'] not in (0, 2)):
    raise ValueError('unsupported_template')
  if (values['STANDSTILL_STATE'] or values['STANDSTILL_RESUME'] or values['ACC_OVERRIDE_OR_STANDSTILL'] or
      not values['ACC_CONTROLLABLE_AND_ON'] or not values['ACC_REQ_NOT_STANDSTILL']):
    raise ValueError('stop_resume_override_unverified')
  return values


def encode_candidate(template: bytes, accel_mps2: float, counter: int) -> CandidateEncoding:
  """Change only acceleration, counter and checksum; return text, never TX data.

  Bounds here express representation only, not safe acceleration limits. Raw
  zero is ambiguous hold/invalid in captures and is never a normal candidate.
  """
  validate_template(template)
  if type(counter) is not int or not 0 <= counter <= 15:
    raise ValueError('counter must be a four-bit integer')
  if not math.isfinite(accel_mps2) or not -4.95 <= accel_mps2 <= 7.75:
    raise ValueError('acceleration is nonfinite, unrepresentable or reserved')
  raw = int(((Decimal(str(accel_mps2)) + 5) / Decimal('0.05')).to_integral_value(rounding=ROUND_HALF_UP))
  data = bytearray(template)
  data[0] = raw
  data[6] = (data[6] & 0xF0) | counter
  data[7] = (0xFF - sum(data[:7])) & 0xFF
  return CandidateEncoding(data.hex(), raw * 0.05 - 5)
