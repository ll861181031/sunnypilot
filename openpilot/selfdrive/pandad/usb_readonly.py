"""Desktop USB Panda identification and health monitoring, without device writes.

The old health layout is from cpv9-shengda_0429/panda/board/health.h (version 16).
This mode does not receive/send CAN, set safety/heartbeat, reset or flash Panda.
It is not a control backend; manager/hardwared keep it offroad.
"""
import signal
import struct
import time

import usb1

from panda import Panda
from openpilot.cereal import messaging
from openpilot.common.swaglog import cloudlog

LEGACY_PROTOCOL = b"\x10\x04\x05"
LEGACY_HEALTH = struct.Struct("<IIIIIIIIBBBBBHBBBHfBBHBHHB")
HARDWARE_TYPES = {7: "redPanda", 8: "redPandaV2", 9: "tres", 10: "cuatro"}


class UsbPandaReader:
  def __init__(self, handle):
    self.handle = handle
    hardware = self._read(0xc1, 64)
    if len(hardware) != 1 or hardware[0] not in HARDWARE_TYPES:
      raise ValueError(f"Unsupported Panda hardware type: {hardware.hex()}")
    self.panda_type = HARDWARE_TYPES[hardware[0]]
    self.version = self._read(0xd6, 128).decode("utf-8", errors="replace")
    self.protocol = self._read(0xdd, 8)
    # Do not guess a packet layout or bypass a protocol mismatch.
    if self.protocol != LEGACY_PROTOCOL:
      raise ValueError(f"Unsupported USB read-only Panda protocol: {list(self.protocol)}")

  def _read(self, request: int, length: int) -> bytes:
    return bytes(self.handle.controlRead(0xc0, request, 0, 0, length, timeout=1000))

  def read_state(self) -> dict:
    data = self._read(0xd2, LEGACY_HEALTH.size)
    if len(data) != LEGACY_HEALTH.size:
      raise ValueError(f"Invalid Panda health length: {len(data)} != {LEGACY_HEALTH.size}")
    a = LEGACY_HEALTH.unpack(data)
    if a[11] > 2 or a[14] > 2:
      raise ValueError("Invalid Panda health enum value")
    return {
      "pandaType": self.panda_type, "uptime": a[0], "voltage": a[1], "current": a[2],
      "safetyTxBlocked": a[3], "safetyRxInvalid": a[4], "txBufferOverflow": a[5], "rxBufferOverflow": a[6],
      "faults": [bit for bit in range(27) if a[7] & (1 << bit)],
      # This backend never controls the car. Do not let a bench harness with the
      # ignition line pulled high put the whole stack onroad, and never claim that
      # controls are allowed.
      "ignitionLine": False, "ignitionCan": False, "controlsAllowed": False,
      "harnessStatus": a[11], "safetyModel": a[12], "safetyParam": a[13], "faultStatus": a[14],
      "powerSaveEnabled": bool(a[15]), "heartbeatLost": bool(a[16]), "alternativeExperience": a[17],
      "interruptLoad": a[18], "fanPower": a[19], "safetyRxChecksInvalid": bool(a[20]),
      "spiErrorCount": a[21], "sbu1Voltage": a[23] / 1000.0, "sbu2Voltage": a[24] / 1000.0,
    }


def make_messages(state: dict):
  panda = messaging.new_message("pandaStates", 1, valid=True)
  panda.pandaStates[0] = state
  peripheral = messaging.new_message("peripheralState", valid=True)
  peripheral.peripheralState = {key: state[key] for key in ("pandaType", "voltage", "current")}
  return panda, peripheral


def main():
  stopped = False

  def stop(signum, frame):
    nonlocal stopped
    stopped = True

  signal.signal(signal.SIGINT, stop)
  signal.signal(signal.SIGTERM, stop)
  pm = messaging.PubMaster(["pandaStates", "peripheralState"])
  last_error = None
  while not stopped:
    try:
      with usb1.USBContext() as context:
        devices = [d for d in context.getDeviceList(skip_on_error=True)
                   if d.getVendorID() in Panda.USB_VIDS and d.getProductID() == 0xddcc]
        if len(devices) != 1:
          raise ValueError(f"USB read-only detection needs exactly one running Panda, found {len(devices)}")
        handle = devices[0].open()
        try:
          reader = UsbPandaReader(handle)
          cloudlog.warning(f"Panda connected: {reader.panda_type}, {reader.version}; USB read-only monitoring, controls disabled")
          last_error = None
          while not stopped:
            state, peripheral = make_messages(reader.read_state())
            pm.send("pandaStates", state)
            pm.send("peripheralState", peripheral)
            time.sleep(0.1)
        finally:
          handle.close()
    except (usb1.USBError, ValueError) as exc:
      if str(exc) != last_error:
        cloudlog.warning(f"Panda USB 只读识别：{exc}")
        last_error = str(exc)
      # Do not publish an empty pandaStates: it refreshes recv_time and hides the
      # disconnect from hardwared's timeout check. Staying silent lets that timeout
      # clear ignition, which is the intended "panda gone" signal.
      pm.send("peripheralState", messaging.new_message("peripheralState", valid=False))
      time.sleep(1)


if __name__ == "__main__":
  main()
