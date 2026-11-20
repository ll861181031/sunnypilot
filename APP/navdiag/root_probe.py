#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import time

if __package__:
  from .device import Device, ROOT, adb_client_argv, choose_adb
else:
  from device import Device, ROOT, adb_client_argv, choose_adb


ALLOWED_PATH = "/data/user/0/com.autonavi.amapauto/files/naviPathInfo.json"
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
REPORT_RESERVE = 128 * 1024
PREFIX = b"NAVDIAG_PROBE_V1\n"
TRAILER = re.compile(rb"\nNAVDIAG_PROBE_RC=([0-9]{3})\n\Z")


def validate_duration(value: int) -> int:
  if type(value) is not int or not 1 <= value <= 180:
    raise ValueError("采样时长必须是 1–180 秒的整数")
  return value


def remote_command(path: str = ALLOWED_PATH) -> str:
  if path != ALLOWED_PATH:
    raise ValueError("仅允许读取固定的高德导航临时文件")
  quoted = shlex.quote(path)
  script = "".join([
    "printf 'NAVDIAG_PROBE_V1\\n'; ",
    f"if [ -L {quoted} ]; then navdiag_status=46; ",
    f"elif [ ! -e {quoted} ]; then navdiag_status=44; ",
    f"elif [ ! -f {quoted} ]; then navdiag_status=45; ",
    f"elif [ ! -r {quoted} ]; then navdiag_status=47; ",
    f"else head -c {MAX_FILE_BYTES + 1} {quoted}; navdiag_status=$?; fi; ",
    "printf '\\nNAVDIAG_PROBE_RC=%03d\\n' \"$navdiag_status\"; exit \"$navdiag_status\"",
  ])
  return "su -c " + shlex.quote(script)


def parse_sample(returncode: int, raw: bytes) -> tuple[dict, bytes | None]:
  if not isinstance(raw, bytes):
    raise TypeError("采样输出必须是原始 bytes")
  end = TRAILER.search(raw)
  if not raw.startswith(PREFIX) or end is None:
    return {"status": "command_error" if returncode else "protocol_error", "returncode": returncode}, None
  remote_code = int(end.group(1))
  metadata = {"returncode": returncode, "remote_returncode": remote_code}
  if returncode not in (0, remote_code):
    return {**metadata, "status": "transport_error"}, None
  if remote_code != 0:
    status = {44: "missing", 45: "not_regular_file", 46: "symlink_rejected", 47: "unreadable"}.get(remote_code, "read_error")
    return {**metadata, "status": status}, None
  payload = raw[len(PREFIX):end.start()]
  if len(payload) > MAX_FILE_BYTES:
    return {**metadata, "status": "oversize", "observed_bytes": len(payload)}, None
  if not payload:
    return {**metadata, "status": "empty", "observed_bytes": 0}, None
  return {**metadata, "status": "read", "observed_bytes": len(payload)}, payload


def read_once(device: Device, timeout: float) -> tuple[dict, bytes | None]:
  command = adb_client_argv(device, "-s", device.serial, "shell", "-T", remote_command())
  try:
    result = subprocess.run(command, capture_output=True, text=False, timeout=timeout, check=False)
  except subprocess.TimeoutExpired:
    return {"status": "timeout"}, None
  return parse_sample(result.returncode, result.stdout)


def write_private(path: Path, payload: bytes) -> None:
  descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
  with os.fdopen(descriptor, "wb") as stream:
    os.fchmod(stream.fileno(), 0o600)
    stream.write(payload)
    stream.flush()
    os.fsync(stream.fileno())


class SnapshotStore:
  def __init__(self, output: Path, max_total_bytes: int = MAX_TOTAL_BYTES):
    if not REPORT_RESERVE < max_total_bytes <= MAX_TOTAL_BYTES:
      raise ValueError("总量限额无效")
    self.output = output
    self.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    self.output.chmod(0o700)
    self.data_limit = max_total_bytes - REPORT_RESERVE
    self.total_bytes = 0
    self.samples: dict[str, dict] = {}

  def save(self, payload: bytes) -> dict:
    if not isinstance(payload, bytes) or len(payload) > MAX_FILE_BYTES:
      raise ValueError("样本必须是未超限的原始字节")
    digest = hashlib.sha256(payload).hexdigest()
    if digest in self.samples:
      return {"status": "duplicate", "sha256": digest}
    if self.total_bytes + len(payload) > self.data_limit:
      return {"status": "total_limit", "observed_bytes": len(payload)}
    name = f"sample-{len(self.samples) + 1:03d}-{digest}.bin"
    write_private(self.output / name, payload)
    item = {"file": name, "sha256": digest, "bytes": len(payload)}
    self.samples[digest] = item
    self.total_bytes += len(payload)
    return {"status": "saved", **item}

  def report(self, report: dict) -> Path:
    payload = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if len(payload) > REPORT_RESERVE:
      raise ValueError("报告超过预留限额")
    path = self.output / "report.json"
    write_private(path, payload)
    return path


def collect(device: Device, output: Path, duration_seconds: int = 60, *, reader=read_once,
            clock=time.monotonic, sleep=time.sleep) -> dict:
  duration = validate_duration(duration_seconds)
  store = SnapshotStore(output)
  started = clock()
  deadline = started + duration
  next_sample = started
  report = {
    "schema_version": 1, "source_path": ALLOWED_PATH,
    "started_utc": datetime.now(UTC).isoformat(), "duration_limit_seconds": duration,
    "interval_seconds": 1, "max_file_bytes": MAX_FILE_BYTES, "max_total_bytes": MAX_TOTAL_BYTES,
    "report_reserve_bytes": REPORT_RESERVE, "source_complete_verified": False,
    "complete_route_verified": False, "format": "uninterpreted_bytes", "events": [],
  }
  stop_reason = "duration_limit"
  try:
    while clock() < deadline:
      remaining = next_sample - clock()
      if remaining > 0:
        sleep(min(remaining, max(0, deadline - clock())))
      if clock() >= deadline:
        break
      sample_started = clock()
      event, payload = reader(device, min(5.0, deadline - sample_started))
      event["elapsed_ms"] = round((sample_started - started) * 1000)
      if payload is not None:
        event.update(store.save(payload))
      report["events"].append(event)
      if event["status"] == "total_limit":
        stop_reason = "total_limit"
        break
      next_sample = sample_started + 1.0
  except KeyboardInterrupt:
    stop_reason = "interrupted"
  except Exception:
    stop_reason = "probe_error"
    raise
  finally:
    report.update({"stop_reason": stop_reason, "elapsed_ms": round((clock() - started) * 1000),
                   "counts": dict(Counter(item["status"] for item in report["events"])),
                   "samples": list(store.samples.values()), "distinct_samples": len(store.samples),
                   "saved_bytes": store.total_bytes})
    store.report(report)
  return {"counts": report["counts"], "distinct_samples": len(store.samples), "saved_bytes": store.total_bytes,
          "stop_reason": stop_reason, "report": str((output / "report.json").resolve())}


def main() -> None:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--adb")
  parser.add_argument("--port", type=int, default=5039)
  parser.add_argument("--serial")
  parser.add_argument("--duration", type=int, default=60)
  parser.add_argument("--out", type=Path)
  args = parser.parse_args()
  validate_duration(args.duration)
  output = args.out or ROOT / "captures" / ("root-probe-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
  with Device(choose_adb(args.adb), args.port, args.serial) as device:
    summary = collect(device, output, args.duration)
  print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
  try:
    main()
  except Exception as error:
    raise SystemExit(f"导航只读采样失败：{type(error).__name__}，请检查本地 report.json（若已创建）") from None
