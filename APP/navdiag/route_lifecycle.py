#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import time

from device import Device, PACKAGE, ROOT, capture_service_running, choose_adb
from root_probe import validate_duration, write_private
from route_snapshot import MAX_TOTAL_BYTES, gather_window


def phone_elapsed_ns(device: Device) -> int:
  result = device.shell("cat", "/proc/uptime")
  text = result.stdout.split()
  if not text or re.fullmatch(r"[0-9]+\.[0-9]+", text[0]) is None:
    raise ValueError("phone_elapsed_unavailable")
  return int(float(text[0]) * 1e9)


def collect(device: Device, output: Path, duration: int, *, gather=gather_window,
            elapsed=phone_elapsed_ns, clock=time.monotonic, sleep=time.sleep) -> dict:
  validate_duration(duration)
  output.mkdir(mode=0o700, parents=True, exist_ok=False)
  output.chmod(0o700)
  started, total, samples = clock(), 0, 0
  seen: set[str] = set()
  reasons: dict[str, int] = {}
  journal = output / "observations.jsonl"
  descriptor = os.open(journal, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
  with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
    while clock() - started < duration:
      observation = {"sample": samples + 1, "host_started_mono_ns": int(clock() * 1e9)}
      try:
        observation["phone_elapsed_before_ns"] = elapsed(device)
        report, files = gather(device)
        for item in report["files"]:
          data = files[item["source"]]
          digest = hashlib.sha256(data).hexdigest()
          if digest != item["sha256"]:
            raise ValueError("snapshot_hash_mismatch")
          filename = "blob-" + digest + ".bin"
          if digest not in seen:
            if total + len(data) > MAX_TOTAL_BYTES:
              raise ValueError("lifecycle_total_limit")
            write_private(output / filename, data)
            seen.add(digest)
            total += len(data)
          item["file"] = filename
        observation.update(report)
        observation["phone_elapsed_after_ns"] = elapsed(device)
        observation["status"] = "stable"
      except Exception as error:
        reason = str(error) if isinstance(error, ValueError) else type(error).__name__
        if not re.fullmatch(r"[a-zA-Z0-9_]{1,80}", reason):
          reason = type(error).__name__
        observation.update(status="rejected", reason=reason)
        reasons[reason] = reasons.get(reason, 0) + 1
      observation["host_finished_mono_ns"] = int(clock() * 1e9)
      handle.write(json.dumps(observation, ensure_ascii=False) + "\n")
      handle.flush()
      os.fsync(handle.fileno())
      samples += 1
      if observation.get("reason") == "lifecycle_total_limit":
        break
      sleep(max(0.0, min(2.0, duration - (clock() - started))))
  report = {"schema_version": 1, "samples": samples, "unique_blobs": len(seen), "blob_bytes": total,
            "rejections": reasons, "duration_seconds": clock() - started,
            "active_guidance_verified": False, "route_identity_verified": False,
            "journal_sha256": hashlib.sha256(journal.read_bytes()).hexdigest()}
  write_private(output / "report.json", (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode())
  return report


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--adb")
  parser.add_argument("--serial")
  parser.add_argument("--port", type=int, default=5039)
  parser.add_argument("--duration", type=int, default=180)
  parser.add_argument("--out", type=Path)
  parser.add_argument("--start-capture", action="store_true")
  args = parser.parse_args()
  validate_duration(args.duration)
  output = args.out or ROOT / "captures" / ("route-lifecycle-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
  with Device(choose_adb(args.adb), args.port, args.serial) as device:
    if capture_service_running(device):
      raise RuntimeError("已有采集会话，请先结束，避免本次采集覆盖用户操作")
    try:
      if args.start_capture:
        result = device.shell("am", "start", "-W", "-n", f"{PACKAGE}/.MainActivity", "--es", "command", "start")
        if "Error:" in result.stdout or not capture_service_running(device):
          raise RuntimeError("broadcast_capture_not_started")
      report = collect(device, output, args.duration)
    finally:
      if args.start_capture:
        device.shell("am", "startservice", "-n", f"{PACKAGE}/.CaptureService",
                     "-a", f"{PACKAGE}.STOP", check=False)
        deadline = time.monotonic() + 20
        while capture_service_running(device) and time.monotonic() < deadline:
          time.sleep(0.25)
        if capture_service_running(device):
          raise RuntimeError("采集器尚在保存，不强停；请稍后执行 device.py close")
        device.shell("am", "force-stop", PACKAGE)
  print(json.dumps({**report, "output": str(output.resolve())}, ensure_ascii=False))


if __name__ == "__main__":
  main()
