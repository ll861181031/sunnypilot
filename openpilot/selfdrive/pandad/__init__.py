import os

from openpilot.common.hardware import PC
from openpilot.selfdrive.pandad.pandad_api_impl import can_list_to_can_capnp, can_capnp_to_list

assert can_list_to_can_capnp
assert can_capnp_to_list


def usb_readonly_enabled() -> bool:
  return PC and os.getenv("PANDA_USB_READONLY") == "1"
