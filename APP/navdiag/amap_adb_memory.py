#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import select
import shlex
import subprocess
import time

if __package__:
  from .amap_memory_probe import EXPECTED_HASHES, ProbeError, parse_maps, probe_snapshot
  from .device import Device, ROOT, adb_client_argv, choose_adb
  from .root_probe import write_private
else:
  from amap_memory_probe import EXPECTED_HASHES, ProbeError, parse_maps, probe_snapshot
  from device import Device, ROOT, adb_client_argv, choose_adb
  from root_probe import write_private


PREFIX = b"NAVDIAG_MEMORY_V1\n"
TRAILER = b"\nNAVDIAG_MEMORY_RC=000\n"


def process_identity(device: Device) -> str:
  script = "".join(["navdiag_pid=$(pidof com.autonavi.amapauto); ",
                   "case \"$navdiag_pid\" in ''|*[!0-9]*) exit 44;; esac; ",
                   "awk '{print $1 \":\" $22}' /proc/\"$navdiag_pid\"/stat"])
  result = device.shell("-T", "su -c " + shlex.quote(script))
  value = result.stdout.strip()
  if re.fullmatch(r"[1-9][0-9]*:[0-9]+", value) is None:
    raise ProbeError("provider_identity_unavailable")
  return value


class NativeMemorySource:
  def __init__(self, device: Device):
    self.device = device
    self.process = None
    self.epoch = process_identity(device)
    self.pid = self.epoch.split(":")[0]
    result = device.shell("-T", "su -c " + shlex.quote("cat /proc/" + self.pid + "/maps"))
    self.maps_text = result.stdout
    maps = parse_maps(self.maps_text)
    self.hashes = {}
    for name, expected in EXPECTED_HASHES.items():
      paths = {row.path for row in maps if row.path.endswith("/" + name)}
      if len(paths) != 1:
        raise ProbeError("library_mapping_ambiguous")
      path = paths.pop()
      if re.fullmatch(r"/data/app/com\.autonavi\.amapauto-[A-Za-z0-9_+=-]+/lib/arm64/" + re.escape(name), path) is None:
        raise ProbeError("library_path_not_supported")
      result = device.shell("-T", "su -c " + shlex.quote("sha256sum " + shlex.quote(path)))
      digest = result.stdout.split()[0] if result.stdout.split() else ""
      if digest != expected:
        raise ProbeError("unsupported_binary_profile")
      self.hashes[name] = digest
    if process_identity(device) != self.epoch:
      raise ProbeError("process_epoch_changed")

  def __enter__(self):
    script = "".join(["while read -r navdiag_address navdiag_size; do ",
      "case \"$navdiag_address:$navdiag_size\" in *[!0-9:]*|:*|*:) exit 45;; esac; ",
      "[ \"$navdiag_size\" -ge 1 ] && [ \"$navdiag_size\" -le 64 ] || exit 46; ",
      "printf 'NAVDIAG_MEMORY_V1\\n'; ",
      "dd if=/proc/" + self.pid + "/mem bs=1 skip=\"$navdiag_address\" count=\"$navdiag_size\" 2>/dev/null; ",
      "navdiag_result=$?; printf '\\nNAVDIAG_MEMORY_RC=%03d\\n' \"$navdiag_result\"; done"])
    self.process = subprocess.Popen(adb_client_argv(self.device, "-s", self.device.serial,
      "shell", "-T", "su -c " + shlex.quote(script)), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
      stderr=subprocess.DEVNULL, start_new_session=True)
    return self

  def __exit__(self, *_):
    if self.process is not None:
      self.process.stdin.close()
      try:
        self.process.wait(timeout=2)
      except subprocess.TimeoutExpired:
        self.process.terminate()
        try:
          self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
          self.process.kill()
          self.process.wait(timeout=2)
      self.process.stdout.close()
      self.process = None

  def _exact(self, count: int) -> bytes:
    result = bytearray()
    deadline = time.monotonic() + 3
    while len(result) < count:
      remaining = deadline - time.monotonic()
      if remaining <= 0 or not select.select([self.process.stdout], [], [], remaining)[0]:
        raise ProbeError("memory_channel_timeout")
      part = os.read(self.process.stdout.fileno(), count - len(result))
      if not part:
        raise ProbeError("memory_channel_closed")
      result.extend(part)
    return bytes(result)

  def _read(self, address: int, size: int) -> bytes:
    if (type(address) is not int or type(size) is not int or not 0 < address < 2 ** 48
        or not 1 <= size <= 64 or address + size > 2 ** 48):
      raise ProbeError("memory_request_invalid")
    self.process.stdin.write(f"{address} {size}\n".encode("ascii"))
    self.process.stdin.flush()
    if self._exact(len(PREFIX)) != PREFIX:
      raise ProbeError("memory_channel_header")
    data = self._exact(size)
    if self._exact(len(TRAILER)) != TRAILER:
      raise ProbeError("memory_channel_read_failed")
    return data

  def observe(self) -> dict:
    if self.process is None:
      raise ProbeError("memory_source_not_started")
    return probe_snapshot(self.maps_text, self._read, library_sha256=self.hashes,
                          process_epoch=lambda: process_identity(self.device))


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--adb")
  parser.add_argument("--serial")
  parser.add_argument("--port", type=int, default=5039)
  parser.add_argument("--out", type=Path)
  args = parser.parse_args()
  with Device(choose_adb(args.adb), args.port, args.serial) as device:
    with NativeMemorySource(device) as source:
      report = source.observe()
      report["mapped_library_sha256"] = source.hashes
      report["maps_sha256"] = hashlib.sha256(source.maps_text.encode()).hexdigest()
  output = args.out or ROOT / "captures" / ("memory-probe-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
  output.mkdir(mode=0o700, parents=True, exist_ok=False)
  write_private(output / "report.json", (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode())
  print(json.dumps({**report, "report": str((output / "report.json").resolve())}, ensure_ascii=False))


if __name__ == "__main__":
  try:
    main()
  except Exception as error:
    reason = str(error) if isinstance(error, ProbeError) else type(error).__name__
    raise SystemExit("Root 导航只读探针失败：" + reason) from None
