import struct
import unittest
from unittest.mock import Mock

from openpilot.selfdrive.pandad.usb_readonly import UsbPandaReader, make_messages


class ReadOnlyHandle:
  """No USB write, claim, reset or bulk API exists on this test handle."""
  def __init__(self, health=None, versions=b"\x10\x04\x05"):
    self.health = health or struct.pack(
      "<IIIIIIIIBBBBBHBBBHfBBHBHHB",
      123, 13500, 200, 0, 0, 0, 0, 0, 1, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0.25, 0, 0, 0, 0, 1000, 2000, 0,
    )
    self.versions = versions

  def controlRead(self, request_type, request, value, index, length, timeout):
    assert request_type == 0xc0 and value == index == 0 and timeout > 0
    return {0xc1: b"\x07", 0xd6: b"test-firmware", 0xdd: self.versions, 0xd2: self.health}[request][:length]


class TestUsbReadOnly(unittest.TestCase):
  def test_old_health_is_translated_to_real_sp_messages_without_writes(self):
    reader = UsbPandaReader(ReadOnlyHandle())
    state, peripheral = make_messages(reader.read_state())
    self.assertTrue(state.valid)
    panda = state.pandaStates[0]
    self.assertEqual(panda.pandaType, "redPanda")
    self.assertEqual(panda.voltage, 13500)
    self.assertEqual(panda.uptime, 123)
    # Read-only mode must never claim the car is on or that controls are allowed.
    self.assertFalse(panda.ignitionLine)
    self.assertFalse(panda.controlsAllowed)
    self.assertEqual(panda.safetyModel, "silent")
    self.assertAlmostEqual(panda.interruptLoad, 0.25)
    self.assertEqual(peripheral.peripheralState.voltage, 13500)

  def test_unknown_protocol_is_rejected(self):
    with self.assertRaisesRegex(ValueError, "protocol"):
      UsbPandaReader(ReadOnlyHandle(versions=b"\x11\x04\x05"))

  def test_truncated_health_is_rejected(self):
    reader = UsbPandaReader(ReadOnlyHandle(health=b"\x00" * 12))
    with self.assertRaisesRegex(ValueError, "health"):
      reader.read_state()

  def test_bad_hardware_type_is_rejected(self):
    handle = Mock()
    handle.controlRead.return_value = b"\xff"
    with self.assertRaisesRegex(ValueError, "hardware"):
      UsbPandaReader(handle)
