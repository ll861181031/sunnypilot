"""Independent USB receive in an already-silent Panda, without CAN or safety writes."""

import os
import time
from pathlib import Path
from types import SimpleNamespace

import usb1

from openpilot.cereal import log
from panda import Panda, unpack_can_buffer
from panda.python.usb import PandaUsbHandle


def conflicting_processes():
  names = {'boardd', 'pandad', 'pandad.py', 'card', 'card.py', 'controlsd', 'controlsd.py',
           'manager.py', 'run_23tang_dmi.sh', 'openpilot.system.manager.manager',
           'openpilot.selfdrive.pandad.pandad', 'openpilot.selfdrive.car.card'}
  found = []
  for proc in Path('/proc').iterdir():
    if not proc.name.isdigit() or int(proc.name) == os.getpid():
      continue
    try:
      argv = proc.joinpath('cmdline').read_bytes().split(b'\x00')
      matches = {Path(os.fsdecode(arg)).name for arg in argv} & names
      found.extend(f'{name} (pid {proc.name})' for name in sorted(matches))
    except (FileNotFoundError, ProcessLookupError, PermissionError):
      continue
  return found


class PandaSource:
  def __init__(self):
    conflicts = conflicting_processes()
    if conflicts:
      raise RuntimeError('Direct capture refused while vehicle processes are running: ' + ', '.join(conflicts))
    self.context = usb1.USBContext()
    self.handle = None
    self.maintained = False
    self.pending = b''
    self.info = {'source': 'panda_usb', 'can_transmission': False, 'safety_mode_writes': False,
                 'usb_maintenance_writes': ['inactive_heartbeat', 'power_save'],
                 'timestamp_basis': 'host USB receive batch, not per-frame wire timestamps',
                 'startup_frames_discarded': 0, 'power_save_restored': False}
    try:
      devices = [d for d in self.context.getDeviceList(skip_on_error=True)
                 if d.getVendorID() in Panda.USB_VIDS and d.getProductID() in Panda.USB_PIDS]
      if len(devices) != 1 or devices[0].getProductID() != 0xddcc:
        raise RuntimeError('Need exactly one running USB Panda; no automatic device selection, reset or flashing')
      self.handle = devices[0].open()
      self.handle.claimInterface(0)
      versions = bytes(self._read(0xdd, 3))
      if versions != bytes([Panda.HEALTH_PACKET_VERSION, Panda.CAN_PACKET_VERSION, Panda.CAN_HEALTH_PACKET_VERSION]):
        raise RuntimeError(f'Panda packet version mismatch: {list(versions)}; capture will not flash firmware')
      # Reuse the repository parsers, not Panda.__init__, which resets communications and CAN rates.
      self.reader = SimpleNamespace(_handle=PandaUsbHandle(self.handle), health_version=versions[0],
                                    can_health_version=versions[2], HEALTH_PACKET_VERSION=Panda.HEALTH_PACKET_VERSION,
                                    HEALTH_STRUCT=Panda.HEALTH_STRUCT, CAN_HEALTH_PACKET_VERSION=Panda.CAN_HEALTH_PACKET_VERSION,
                                    CAN_HEALTH_STRUCT=Panda.CAN_HEALTH_STRUCT)
      self.initial = self.snapshot()
      self._validate(self.initial)
      self.info.update(initial=self.initial, hardware_type=bytes(self._read(0xc1, 64)).hex(),
                       firmware_version=bytes(self._read(0xd6, 64)).decode('utf-8', errors='replace'))
      self.maintained = True
      self._write(0xf3, 0)
      self._write(0xe7, 0)
      self.last_maintenance = time.monotonic()
      # Old FIFO contents have no wire timestamps. Drain by reading before declaring live capture.
      deadline = time.monotonic() + 2
      while True:
        batch = self._read_can(100)
        frames, self.pending = unpack_can_buffer(self.pending + batch)
        self.info['startup_frames_discarded'] += len(frames)
        if not batch:
          if self.pending:
            raise RuntimeError('Incomplete USB CAN packet in startup buffer')
          break
        if time.monotonic() >= deadline:
          raise RuntimeError('Could not drain the pre-existing CAN queue within 2 seconds')
      self.baseline = self.snapshot()
      self.info['baseline'] = self.baseline
      self.check_health()
    except BaseException:
      self.close()
      raise

  def _read(self, request, length):
    return self.handle.controlRead(Panda.REQUEST_IN, request, 0, 0, length, timeout=1000)

  def _write(self, request, value):
    if not ((request == 0xf3 and value == 0) or (request == 0xe7 and value in (0, 1))):
      raise ValueError('Only inactive heartbeat and power-save maintenance are permitted')
    self.handle.controlWrite(Panda.REQUEST_OUT, request, value, 0, b'', timeout=1000)

  def snapshot(self):
    return {'health': Panda.health(self.reader), 'can_health': [Panda.can_health(self.reader, bus) for bus in range(3)]}

  @staticmethod
  def _validate(snapshot):
    health = snapshot['health']
    if health['safety_mode'] != 0 or health['controls_allowed']:
      raise RuntimeError('Panda must already be silent (safety mode 0), with controls disallowed; no automatic mode change')
    if health['faults'] or health['car_harness_status'] not in (1, 2):
      raise RuntimeError('Panda fault or unrecognized bypass harness; direct capture refused')

  def check_health(self):
    current = self.snapshot()
    self.info['latest'] = current
    self._validate(current)
    if current['health']['power_save_enabled']:
      raise RuntimeError('Panda entered power save during capture')
    for key in ('rx_buffer_overflow', 'tx_buffer_overflow', 'uptime'):
      before, now = self.baseline['health'][key], current['health'][key]
      if (key == 'uptime' and now < before) or (key != 'uptime' and now != before):
        raise RuntimeError(f'Panda reset/overflow detected: {key} changed from {before} to {now}')
    for bus in (0, 2):
      before, now = self.baseline['can_health'][bus], current['can_health'][bus]
      if now['bus_off']:
        raise RuntimeError(f'CAN bus {bus} is bus-off')
      for key in ('can_speed', 'can_data_speed', 'total_rx_lost_cnt', 'total_tx_cnt', 'total_fwd_cnt', 'can_core_reset_count'):
        if now[key] != before[key]:
          raise RuntimeError(f'CAN bus {bus} changed {key}: {before[key]} -> {now[key]}')
    return current

  def _read_can(self, timeout):
    try:
      return bytes(self.handle.bulkRead(1, 16384, timeout=max(1, timeout)))
    except usb1.USBErrorTimeout:
      return b''

  def poll(self, timeout=100):
    if time.monotonic() - self.last_maintenance >= 1:
      self.check_health()
      conflicts = conflicting_processes()
      if conflicts:
        raise RuntimeError('Vehicle process started during direct capture: ' + ', '.join(conflicts))
      self._write(0xf3, 0)
      self.last_maintenance = time.monotonic()
    batch = self._read_can(timeout)
    frames, self.pending = unpack_can_buffer(self.pending + batch)
    if not frames:
      if not batch:
        time.sleep(min(timeout / 1000, 0.01))
      return []
    event = log.Event.new_message(logMonoTime=time.monotonic_ns(), valid=True)
    for output, (address, payload, bus) in zip(event.init('can', len(frames)), frames, strict=True):
      output.address, output.dat, output.src = address, payload, bus
    return [event.to_bytes()]

  def close(self):
    if self.handle is not None:
      try:
        if self.maintained:
          current = self.snapshot()
          self.info['final_before_restore'] = current
          if current['health']['safety_mode'] == 0 and not current['health']['controls_allowed']:
            power_save = self.initial['health']['power_save_enabled']
            self._write(0xe7, power_save)
            self.info['power_save_restored'] = Panda.health(self.reader)['power_save_enabled'] == power_save
          else:
            self.info['restore_skipped'] = 'Safety configuration changed; leaving it untouched'
      except Exception as exc:
        self.info['close_error'] = f'{type(exc).__name__}: {exc}'
      finally:
        self.handle.close()
        self.handle = None
    if self.context is not None:
      self.context.close()
      self.context = None
