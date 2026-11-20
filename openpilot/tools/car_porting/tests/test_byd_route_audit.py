"""Unit tests for the offline BYD route audit tool.

Synthetic frames only: no rlog, no hardware, no CAN.
"""

import pytest

from openpilot.tools.car_porting.byd_route_audit import (
  MPC_BUS, PLATFORM, REQUIRED, Frame, analyse, forwarding_scan, is_real_rx,
  safety_scan, template_builder_scan,
)


def template_bytes(upper: int, lower: int, counter: int = 0) -> bytes:
  """A stock 0x1E2 payload with a valid trailer checksum and counter."""
  dat = bytearray(8)
  u, l = upper & 0x3FF, lower & 0x3FF
  dat[0] = u & 0xFF
  dat[1] = ((u >> 8) & 0x03) | ((l & 0x3F) << 2)
  dat[2] = (l >> 6) & 0x0F
  dat[6] = (counter & 0xF) << 4
  dat[7] = (0xFF - sum(dat[:7])) & 0xFF
  return bytes(dat)


# well-formed stock templates: 500/-500 (the dominant value) and 300/-300 (the low
# end of the sweep seen in real captures)
GOOD_TEMPLATE = template_bytes(500, -500)
LOW_TEMPLATE = template_bytes(300, -300)


def _payload(addr: int) -> bytes:
  if addr == 0x1E2:
    return GOOD_TEMPLATE
  return bytes(5 if addr == 0x11F else 8)


def req_frames(duration_s: float = 3.0, omit=None, bus_override=None, dlc_override=None,
               rate_scale: float = 1.0, template: bytes | None = None):
  frames = []
  for addr, (bus, rate, _name) in REQUIRED.items():
    if addr == omit:
      continue
    effective = bus_override.get(addr, bus) if bus_override else bus
    payload = dlc_override.get(addr, _payload(addr)) if dlc_override else _payload(addr)
    if addr == 0x1E2 and template is not None:
      payload = template
    step_ns = int(1e9 / (rate * rate_scale))
    for t in range(0, int(duration_s * 1e9) + 1, step_ns):
      frames.append(Frame(addr, effective, payload, t))
  return frames


def test_clean_route_passes():
  report = analyse(req_frames(), PLATFORM)
  assert report["ok"], report["problems"]
  assert report["streams"]["0x11F"]["dlc"] == [5]
  assert report["streams"]["0x11F"]["rate_hz"] == pytest.approx(100, abs=1)
  assert sum(report["template_pairs"].values()) > 0


def test_missing_required_message_is_reported():
  report = analyse(req_frames(omit=0x1F0), PLATFORM)
  assert not report["ok"]
  assert any("0x1F0" in p and "absent" in p for p in report["problems"])


def test_message_on_wrong_bus_is_reported():
  report = analyse(req_frames(bus_override={0x1F0: MPC_BUS}), PLATFORM)
  assert not report["ok"]
  assert any("0x1F0" in p and "absent on bus 0" in p for p in report["problems"])


def test_wrong_dlc_is_reported():
  report = analyse(req_frames(dlc_override={0x1F0: bytes(5)}), PLATFORM)
  assert not report["ok"]
  assert any("0x1F0" in p and "DLC" in p for p in report["problems"])


def test_correct_sixteen_byte_message_does_not_trip_dlc_check():
  # 0x11F really is 5 bytes; 8 would be wrong.
  report = analyse(req_frames(dlc_override={0x11F: bytes(8)}), PLATFORM)
  assert any("0x11F" in p and "DLC" in p for p in report["problems"])


def test_low_rate_is_reported():
  report = analyse(req_frames(rate_scale=0.5), PLATFORM)
  assert not report["ok"]
  assert any("rate" in p for p in report["problems"])


def test_wrong_platform_is_reported():
  report = analyse(req_frames(), "TOYOTA_COROLLA_TSS2")
  assert not report["ok"]
  assert any("carFingerprint" in p for p in report["problems"])


@pytest.mark.parametrize("template", [GOOD_TEMPLATE, LOW_TEMPLATE])
def test_varying_but_valid_templates_are_accepted(template):
  report = analyse(req_frames(template=template), PLATFORM)
  assert report["ok"], report["problems"]


@pytest.mark.parametrize("template,reason", [
  (bytes([0x2C, 0x09, 0x04, 0x00, 0x00, 0x00, 0x00, 0x00]), "asymmetric"),
  (bytes([0x00, 0x20, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]), "jerk upper"),
  (bytes([0xF4, 0x31, 0x58, 0x00, 0x00, 0x00, 0xA0, 0x0C]), "reserved bits"),
  (bytes([0xF4, 0x31, 0x18, 0x00, 0x00, 0x01, 0xA0, 0x0C]), "reserved bits"),
  (bytes([0xF4, 0x31, 0x18, 0x00, 0x00, 0x00, 0xA1, 0x0C]), "reserved bits"),
  (bytes([0xF4, 0x31, 0x18, 0x00, 0x00, 0x00, 0xA0]), "length"),
])
def test_malformed_templates_are_reported(template, reason):
  report = analyse(req_frames(template=template), PLATFORM)
  assert not report["ok"]
  assert any("0x1E2" in p and reason in p for p in report["problems"]), report["problems"]


def test_template_sweep_across_time_is_accepted():
  frames = list(req_frames(omit=0x1E2))
  step = int(1e9 / 50)
  for i, template in enumerate([GOOD_TEMPLATE, LOW_TEMPLATE] * 20):
    frames.append(Frame(0x1E2, MPC_BUS, template, i * step))
  report = analyse(frames, PLATFORM)
  assert report["ok"], report["problems"]
  assert report["template_pairs"] == {"500/-500": 20, "300/-300": 20}


@pytest.mark.parametrize("src,expected", [(0, True), (2, True), (0x82, False), (0x80, False), (0x02, True)])
def test_panda_flagged_frames_are_not_real_rx(src, expected):
  assert is_real_rx(src) is expected


def test_safety_scan_runs_the_c_layer_and_flags_bad_frames():
  # Synthetic frames carry no valid counters/checksums, so the real C safety must
  # reject them. This proves the scan exercises SAFETY_BYD rather than rubber-stamping.
  scan = safety_scan(req_frames(duration_s=1.0))
  assert scan["available"] is True
  assert scan["fed"], scan
  assert sum(scan["rejected"].values()) > 0, scan


def test_safety_scan_is_empty_of_rejections_for_an_unrelated_address_set():
  # No required address at all: nothing is fed to the safety hook.
  scan = safety_scan([Frame(0x700, 0, bytes(8), 0)])
  assert scan["available"] is True
  assert scan["fed"] == {}
  assert scan["rejected"] == {}


def test_template_builder_accepts_real_stock_templates():
  step = int(1e9 / 50)
  frames = [Frame(0x1E2, MPC_BUS, template_bytes(500, -500, i), i * step) for i in range(5)]
  frames += [Frame(0x1E2, MPC_BUS, template_bytes(300, -300, 5 + i), (5 + i) * step) for i in range(5)]
  scan = template_builder_scan(frames)
  # each frame is built twice (inactive + active)
  assert scan["accepted"] == 20
  assert scan["rejected"] == 0


def test_template_builder_rejects_a_malformed_stock_template():
  bad = bytes([0x2C, 0x09, 0x04, 0x00, 0x00, 0x00, 0x00, 0x00])  # asymmetric pair
  scan = template_builder_scan([Frame(0x1E2, MPC_BUS, bad, 0)])
  assert scan["accepted"] == 0
  assert scan["rejected"] == 2
  assert scan["rejected_examples"] == [bad.hex()]


def test_forwarding_scan_blocks_only_while_engaged():
  stock = GOOD_TEMPLATE
  disengaged = [Frame(0x1E2, MPC_BUS, stock, i * int(1e9 / 50), False, False) for i in range(4)]
  scan = forwarding_scan(disengaged)
  assert scan["available"] is True
  assert scan["stock_lkas_frames"] == 4
  assert scan["forward_blocked"] == 0

  engaged_not_steering = [Frame(0x1E2, MPC_BUS, stock, i * int(1e9 / 50), True, False) for i in range(4)]
  scan = forwarding_scan(engaged_not_steering)
  assert scan["forward_blocked"] == 4
  # openpilot was engaged but not steering: the stock command was withheld.
  assert scan["blocked_with_lateral_inactive"] == 4
