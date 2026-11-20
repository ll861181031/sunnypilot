import unittest
from unittest.mock import Mock, patch

from panda import Panda
from openpilot.selfdrive.pandad import pandad


class TestUsbSelection(unittest.TestCase):
  def select(self, hardware, pc=True):
    handle = Mock()
    handle.controlRead.return_value = hardware
    context = Mock()
    with patch.object(pandad, 'PC', pc, create=True), \
         patch.object(Panda, 'spi_list', return_value=[]), \
         patch.object(Panda, 'usb_connect', return_value=(context, handle, 'red', False)), \
         patch.object(Panda, '__init__', side_effect=AssertionError('probe must not reset CAN')):
      result = pandad.check_panda_support(['red'])
    return result, handle, context

  def test_pc_accepts_external_red_without_resetting_can(self):
    result, handle, context = self.select(Panda.HW_TYPE_RED_PANDA)
    self.assertEqual(result, ['red'])
    handle.controlRead.assert_called_once_with(Panda.REQUEST_IN, 0xc1, 0, 0, 1, timeout=1000)
    handle.controlWrite.assert_not_called()
    handle.close.assert_called_once_with()
    context.close.assert_called_once_with()

  def test_comma_device_does_not_select_external_red(self):
    self.assertEqual(self.select(Panda.HW_TYPE_RED_PANDA, pc=False)[0], [])

  def test_pc_rejects_unsupported_or_malformed_hardware(self):
    for hardware in (b'\x03', b'', b'\x07\x00', b'\xff'):
      with self.subTest(hardware=hardware):
        self.assertEqual(self.select(hardware)[0], [])

  def test_multiple_external_pandas_are_not_selected_arbitrarily(self):
    with patch.object(pandad, 'PC', True, create=True), patch.object(Panda, 'spi_list', return_value=[]), \
         patch.object(Panda, '__init__', side_effect=AssertionError('probe must not reset CAN')), \
         patch.object(Panda, 'usb_connect') as connect:
      self.assertEqual(pandad.check_panda_support(['red1', 'red2']), [])
      connect.assert_not_called()


if __name__ == '__main__':
  unittest.main()
