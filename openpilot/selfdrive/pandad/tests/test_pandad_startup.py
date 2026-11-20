import unittest
import os
from unittest.mock import Mock, patch

from openpilot.selfdrive.pandad import pandad


class StopLoop(BaseException):
  pass


def check_unsupported_panda(testcase):
  # Run the real wrapper loop with USB, hardware recovery and flashing isolated.
  # An external Panda is detected, but none passes the existing support filter.
  with patch.object(pandad, "Panda") as panda, \
       patch.object(pandad, "PandaDFU") as dfu, \
       patch.object(pandad, "HARDWARE"), \
       patch.object(pandad.signal, "signal"), \
       patch.object(pandad, "check_panda_support", return_value=[]), \
       patch.object(pandad, "flash_panda") as flash, \
       patch.object(pandad.subprocess, "Popen") as process, \
       patch.object(pandad.time, "sleep") as sleep, \
       patch.object(pandad, "cloudlog") as log:
    panda.list.side_effect = [[], ["external-test-panda"], StopLoop]
    dfu.list.return_value = []
    with testcase.assertRaises(StopLoop):
      pandad.main()

  log.exception.assert_not_called()
  sleep.assert_called_once_with(1)
  flash.assert_not_called()
  process.assert_not_called()


def check_usb_support(internal):
  context = Mock()
  handle = Mock()
  handle.controlRead.return_value = pandad.Panda.HW_TYPE_TRES if internal else pandad.Panda.HW_TYPE_RED_PANDA
  with patch.object(pandad, "PC", internal, create=True), \
       patch.object(pandad.Panda, "spi_list", return_value=[]), \
       patch.object(pandad.Panda, "usb_connect", return_value=(context, handle, "test-panda", False)):
    assert pandad.check_panda_support(["test-panda"]) == (["test-panda"] if internal else [])
  handle.close.assert_called_once_with()
  context.close.assert_called_once_with()


class TestPandadStartup(unittest.TestCase):
  def test_embedded_device_ignores_desktop_readonly_mode(self):
    with patch.dict(os.environ, {"PANDA_USB_READONLY": "1"}), patch("openpilot.selfdrive.pandad.PC", False):
      self.assertFalse(pandad.usb_readonly_enabled())

  def test_readonly_dispatch_never_enters_constructor_or_flasher(self):
    with patch.dict(os.environ, {"PANDA_USB_READONLY": "1"}), \
         patch("openpilot.selfdrive.pandad.PC", True), \
         patch("openpilot.selfdrive.pandad.usb_readonly.main") as monitor, \
         patch.object(pandad, "Panda") as panda, patch.object(pandad, "PandaDFU") as dfu, \
         patch.object(pandad, "flash_panda") as flasher:
      pandad.main()
    monitor.assert_called_once_with()
    panda.assert_not_called()
    panda.list.assert_not_called()
    dfu.list.assert_not_called()
    flasher.assert_not_called()

  def test_unsupported_panda_waits_without_assertion_or_flashing(self):
    check_unsupported_panda(self)

  def test_usb_support_filter_is_preserved(self):
    for internal in (True, False):
      with self.subTest(internal=internal):
        check_usb_support(internal)

  def test_spi_panda_takes_precedence_without_opening_usb(self):
    with patch.object(pandad, "Panda") as panda:
      panda.spi_list.return_value = ["spi-panda"]
      self.assertEqual(pandad.check_panda_support(["external", "spi-panda"]), ["spi-panda"])
      panda.assert_not_called()
