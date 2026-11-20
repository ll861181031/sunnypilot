import importlib
from collections import deque

import pytest
import usb1

from openpilot.cereal import log
from panda import Panda, pack_can_buffer

from openpilot.tools.car_porting.runtime_support import (
  CAN_HEALTH_HELP, can_health_packet_version_present,
)

pytestmark = pytest.mark.skipif(
  not can_health_packet_version_present(), reason=CAN_HEALTH_HELP)



class USBHandle:
  def __init__(self):
    self.health = [0] * 26
    self.health[11], self.health[15], self.health[16] = 2, 1, 1
    self.health[6] = 9999
    self.can_health = [0] * 26
    self.can_health[17] = 500
    self.versions = bytes([16, 4, 5])
    self.writes = []
    self.batches = deque([b''])
    self.closed = False
    self.busy = False

  def claimInterface(self, number):
    assert number == 0
    if self.busy:
      raise usb1.USBErrorBusy()

  def close(self):
    self.closed = True

  def controlRead(self, request_type, request, value, index, length, timeout=1000):
    assert request_type == Panda.REQUEST_IN
    if request == 0xdd:
      return self.versions
    if request == 0xd2:
      return Panda.HEALTH_STRUCT.pack(*self.health)
    if request == 0xc2:
      return Panda.CAN_HEALTH_STRUCT.pack(*self.can_health)
    if request == 0xc1:
      return b'\x07'
    if request == 0xd6:
      return b'test-firmware'
    raise AssertionError(f'Unexpected USB read: {request:#x}')

  def controlWrite(self, request_type, request, value, index, data, timeout=1000):
    assert request_type == Panda.REQUEST_OUT and index == 0 and data == b''
    assert (request == 0xe7 and value in (0, 1)) or (request == 0xf3 and value == 0), 'Only wake/sleep and inactive heartbeat allowed'
    self.writes.append((request, value))
    if request == 0xe7:
      self.health[15] = value

  def bulkRead(self, endpoint, length, timeout):
    assert endpoint == 1 and timeout > 0
    return self.batches.popleft() if self.batches else b''

  def bulkWrite(self, *args, **kwargs):
    raise AssertionError('CAN transmission is forbidden')


class USBDevice:
  def __init__(self, handle):
    self.handle = handle

  def getVendorID(self):
    return 0x3801

  def getProductID(self):
    return 0xddcc

  def open(self):
    return self.handle


class USBContext:
  def __init__(self, handle):
    self.devices = [USBDevice(handle)]
    self.closed = False

  def getDeviceList(self, skip_on_error):
    return self.devices

  def close(self):
    self.closed = True


def setup_source(monkeypatch):
  name = 'openpilot.tools.car_porting.byd_panda_capture'
  assert importlib.util.find_spec(name) is not None, 'Standalone Panda receiver is missing'
  module = importlib.import_module(name)
  handle = USBHandle()
  context = USBContext(handle)
  monkeypatch.setattr(module.usb1, 'USBContext', lambda: context)
  monkeypatch.setattr(module, 'conflicting_processes', list)

  def forbidden_constructor(*args, **kwargs):
    raise AssertionError('Panda constructor reconfigures hardware')

  monkeypatch.setattr(Panda, '__init__', forbidden_constructor)
  return module, handle, context


def test_standalone_receive_keeps_all_frames_and_restores_power(monkeypatch):
  module, handle, context = setup_source(monkeypatch)
  source = module.PandaSource()
  frames = [(0x342, b'\x00' * 8, 0), (0x32E, bytes.fromhex('64666699b7d8f8af'), 2), (0x32E, b'\x11' * 8, 130)]
  # Echo flags occupy bits in the address header, not the outgoing bus field.
  batch = bytearray(pack_can_buffer(frames[:2])[0])
  echo = bytearray(pack_can_buffer([(frames[2][0], frames[2][1], 2)])[0])
  echo[1] |= 2
  echo[5] ^= 2
  handle.batches.extend([bytes(batch + echo)])
  raw, = source.poll(10)
  with log.Event.from_bytes(raw) as event:
    assert event.which() == 'can' and event.valid
    assert [(f.address, f.dat, f.src) for f in event.can] == frames
  source.close()
  assert handle.health[12] == 0 and handle.health[10] == 0
  assert handle.health[15] == 1
  assert handle.closed and context.closed
  assert (0xe7, 0) in handle.writes and handle.writes[-1] == (0xe7, 1)
  assert source.info['power_save_restored']


@pytest.mark.parametrize(('field', 'value'), [(12, 8), (10, 1), (7, 1), (11, 0)])
def test_unsafe_initial_state_refuses_without_any_usb_write(monkeypatch, field, value):
  module, handle, context = setup_source(monkeypatch)
  handle.health[field] = value
  with pytest.raises(RuntimeError):
    module.PandaSource()
  assert handle.writes == []
  assert handle.closed and context.closed


def test_packet_mismatch_refuses_without_flashing_or_writing(monkeypatch):
  module, handle, context = setup_source(monkeypatch)
  handle.versions = b'\x01\x02\x03'
  with pytest.raises(RuntimeError, match='version'):
    module.PandaSource()
  assert not handle.writes
  assert handle.closed and context.closed


def test_busy_device_is_not_taken_over(monkeypatch):
  module, handle, context = setup_source(monkeypatch)
  handle.busy = True
  with pytest.raises(usb1.USBErrorBusy):
    module.PandaSource()
  assert not handle.writes
  assert handle.closed and context.closed


def test_existing_vehicle_process_blocks_direct_usb_access(monkeypatch):
  module, handle, context = setup_source(monkeypatch)
  monkeypatch.setattr(module, 'conflicting_processes', lambda: ['boardd'])
  with pytest.raises(RuntimeError, match='boardd'):
    module.PandaSource()
  assert not handle.writes


def test_usb_packet_fragments_are_reassembled(monkeypatch):
  module, handle, _ = setup_source(monkeypatch)
  source = module.PandaSource()
  payload = pack_can_buffer([(0x342, b'\x00' * 8, 0)])[0]
  handle.batches.extend([payload[:7], payload[7:]])
  assert source.poll(10) == []
  raw, = source.poll(10)
  with log.Event.from_bytes(raw) as event:
    assert event.can[0].address == 0x342 and event.can[0].dat == b'\x00' * 8
  source.close()


def test_stale_startup_buffer_is_not_treated_as_live_data(monkeypatch):
  module, handle, _ = setup_source(monkeypatch)
  handle.batches = deque([pack_can_buffer([(0x123, b'\x00' * 8, 0)])[0], b''])
  source = module.PandaSource()
  assert source.poll(10) == []
  assert source.info['startup_frames_discarded'] == 1
  source.close()


def test_new_overflow_is_reported_but_historical_overflow_is_baselined(monkeypatch):
  module, handle, _ = setup_source(monkeypatch)
  source = module.PandaSource()
  handle.health[6] += 1
  with pytest.raises(RuntimeError, match='overflow'):
    source.check_health()
  source.close()


def test_safety_change_stops_and_cleanup_does_not_overwrite_new_configuration(monkeypatch):
  module, handle, _ = setup_source(monkeypatch)
  source = module.PandaSource()
  handle.health[12] = 8
  with pytest.raises(RuntimeError, match='silent'):
    source.check_health()
  previous_writes = list(handle.writes)
  source.close()
  assert handle.writes == previous_writes
  assert source.info['power_save_restored'] is False
  assert handle.closed


@pytest.mark.parametrize('device_count', [0, 2])
def test_ambiguous_or_missing_device_never_changes_hardware(monkeypatch, device_count):
  module, handle, context = setup_source(monkeypatch)
  context.devices = [USBDevice(handle) for _ in range(device_count)]
  with pytest.raises(RuntimeError, match='exactly one'):
    module.PandaSource()
  assert not handle.writes
  assert context.closed


@pytest.mark.parametrize('field', [12, 13, 15])
def test_new_hardware_frame_loss_or_transmission_stops_capture(monkeypatch, field):
  module, handle, _ = setup_source(monkeypatch)
  source = module.PandaSource()
  handle.can_health[field] += 1
  with pytest.raises(RuntimeError, match='CAN bus'):
    source.check_health()
  source.close()
  assert source.info['power_save_restored']


def test_vehicle_process_started_mid_capture_stops_before_heartbeat(monkeypatch):
  module, handle, _ = setup_source(monkeypatch)
  source = module.PandaSource()
  source.last_maintenance -= 2
  monkeypatch.setattr(module, 'conflicting_processes', lambda: ['boardd'])
  previous_writes = list(handle.writes)
  with pytest.raises(RuntimeError, match='boardd'):
    source.poll(10)
  assert handle.writes == previous_writes
  source.close()
