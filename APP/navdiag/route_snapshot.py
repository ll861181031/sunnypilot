#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess
import time

if __package__:
  from .device import Device, ROOT, adb_client_argv, choose_adb
  from .root_probe import ALLOWED_PATH, PREFIX, TRAILER, write_private
else:
  from device import Device, ROOT, adb_client_argv, choose_adb
  from root_probe import ALLOWED_PATH, PREFIX, TRAILER, write_private


CACHE_DIRECTORY = "/sdcard/Android/data/com.autonavi.amapauto/files/amapauto9/path_cache"
PERSIST_DIRECTORY = "/data/user/0/com.autonavi.amapauto/files/route_data"
SELECTION_PATH = PERSIST_DIRECTORY + "/route_result.json"
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_CACHES = 8


def allowed_path(path: str) -> bool:
  return (path in (ALLOWED_PATH, SELECTION_PATH)
          or any(re.fullmatch(re.escape(directory) + r"/[0-9]{1,20}\.dat", path) is not None
                 for directory in (CACHE_DIRECTORY, PERSIST_DIRECTORY)))


def read_file(device: Device, path: str) -> bytes:
  if not allowed_path(path):
    raise ValueError("navigation_path_not_allowed")
  quoted = shlex.quote(path)
  script = "".join(["printf 'NAVDIAG_PROBE_V1\\n'; ",
                    f"if [ -L {quoted} ] || [ ! -f {quoted} ]; then navdiag_status=44; ",
                    f"else head -c {MAX_FILE_BYTES + 1} {quoted}; navdiag_status=$?; fi; ",
                    "printf '\\nNAVDIAG_PROBE_RC=%03d\\n' \"$navdiag_status\"; exit \"$navdiag_status\""])
  binary_call = getattr(device, "call_binary", None)
  if binary_call is None:
    result = subprocess.run(adb_client_argv(device, "-s", device.serial,
                             "shell", "-T", "su -c " + shlex.quote(script)), capture_output=True, timeout=10, check=False)
  else:
    result = binary_call("shell", "-T", "su -c " + shlex.quote(script), timeout=10, check=False)
  match = TRAILER.search(result.stdout)
  if result.returncode or not result.stdout.startswith(PREFIX) or match is None or int(match.group(1)):
    raise ValueError("navigation_file_read_failed")
  data = result.stdout[len(PREFIX):match.start()]
  if not data or len(data) > MAX_FILE_BYTES:
    raise ValueError("navigation_file_size_limit")
  return data


def cache_names(device: Device) -> tuple[str, ...]:
  script = f"find {shlex.quote(CACHE_DIRECTORY)} -maxdepth 1 -type f -name '*.dat'"
  result = device.shell("-T", "su -c " + shlex.quote(script))
  names = tuple(sorted(result.stdout.splitlines()))
  if len(names) > MAX_CACHES or any(not allowed_path(name) or name == ALLOWED_PATH for name in names):
    raise ValueError("navigation_cache_listing_limit")
  return names


def provider_epoch(device: Device) -> str:
  script = "".join(["navdiag_pid=$(pidof com.autonavi.amapauto); ",
                    "case \"$navdiag_pid\" in ''|*[!0-9]*) exit 44;; esac; ",
                    "cat /proc/sys/kernel/random/boot_id; ",
                    "awk '{print $1, $22}' /proc/\"$navdiag_pid\"/stat"])
  result = device.shell("-T", "su -c " + shlex.quote(script))
  lines = result.stdout.splitlines()
  if len(lines) != 2 or re.fullmatch(r"[0-9a-fA-F-]{36}", lines[0]) is None or re.fullmatch(r"[0-9]+ [0-9]+", lines[1]) is None:
    raise ValueError("provider_process_not_verified")
  return hashlib.sha256(result.stdout.encode()).hexdigest()


def navigation_names(device: Device) -> tuple[str, ...]:
  commands = [f"if [ -f {shlex.quote(ALLOWED_PATH)} ]; then printf '%s\\n' {shlex.quote(ALLOWED_PATH)}; fi"]
  for directory in (CACHE_DIRECTORY, PERSIST_DIRECTORY):
    quoted = shlex.quote(directory)
    commands.append(f"if [ -d {quoted} ]; then find {quoted} -maxdepth 1 -type f; fi")
  result = device.shell("-T", "su -c " + shlex.quote("; ".join(commands)))
  names = tuple(sorted(name for name in result.stdout.splitlines() if allowed_path(name)))
  if len(names) > 2 * MAX_CACHES + 2 or len(names) != len(set(names)):
    raise ValueError("navigation_cache_listing_limit")
  return names


def gather_window(device, *, read=read_file, listing=navigation_names, epoch=provider_epoch) -> tuple[dict, dict[str, bytes]]:
  started = time.monotonic_ns()
  before_epoch, names = epoch(device), listing(device)
  files: dict[str, bytes] = {}
  total = 0
  for name in names:
    if not allowed_path(name):
      raise ValueError("navigation_path_not_allowed")
    data = read(device, name)
    total += len(data)
    if total > MAX_TOTAL_BYTES:
      raise ValueError("snapshot_size_limit")
    files[name] = data
  if listing(device) != names or epoch(device) != before_epoch:
    raise ValueError("provider_or_cache_set_changed")
  if any(read(device, name) != data for name, data in files.items()):
    raise ValueError("navigation_file_changed_during_read")
  if listing(device) != names or epoch(device) != before_epoch:
    raise ValueError("provider_or_cache_set_changed")
  return {"schema_version": 1, "source_app_session_id": before_epoch,
          "read_started_mono_ns": started, "read_finished_mono_ns": time.monotonic_ns(),
          "read_window_stable": True, "active_guidance_verified": False,
          "route_identity_verified": False, "complete_route_verified": False,
          "files": [{"source": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                    for name, data in files.items()]}, files


SELECTION_DIAG_MAX_KEYS = 12
SELECTION_DIAG_MAX_BYTES = 512
SELECTION_DIAG_MAX_DEPTH = 3
SELECTION_DIAG_MAX_LEN = 4096


def _json_shape(value, depth=0):
  """Bounded type/shape summary; never returns raw strings or coordinates."""
  if depth >= SELECTION_DIAG_MAX_DEPTH:
    return "truncated"
  if isinstance(value, dict):
    return {str(key)[:64]: _json_shape(item, depth + 1) for key, item in list(value.items())[:SELECTION_DIAG_MAX_KEYS]}
  if isinstance(value, list):
    return [_json_shape(item, depth + 1) for item in value[:SELECTION_DIAG_MAX_KEYS]]
  if isinstance(value, bool):
    return "bool"
  if isinstance(value, (int, float)):
    return "number"
  if isinstance(value, str):
    return "string"
  if value is None:
    return "null"
  return "other"


def _json_paths(a, b, prefix="", depth=0, out=None):
  """Changed paths with type-only values; bounded and explicit about truncation."""
  if out is None:
    out = []
  if len(out) >= SELECTION_DIAG_MAX_KEYS:
    if not any(entry.get("truncated") for entry in out):
      out.append({"path": prefix[:SELECTION_DIAG_MAX_LEN], "truncated": "keys"})
    return out
  if depth >= SELECTION_DIAG_MAX_DEPTH:
    if a != b:
      out.append({"path": prefix[:SELECTION_DIAG_MAX_LEN], "change": "depth_truncated",
                  "type": _json_shape(a, depth)})
    return out
  if isinstance(a, dict) and isinstance(b, dict):
    for key in sorted(set(a) | set(b)):
      if len(out) >= SELECTION_DIAG_MAX_KEYS:
        break
      path = f"{prefix}/{key}"[:SELECTION_DIAG_MAX_LEN]
      if key not in a:
        out.append({"path": path, "change": "added", "type": _json_shape(b[key], depth)})
      elif key not in b:
        out.append({"path": path, "change": "removed", "type": _json_shape(a[key], depth)})
      elif a[key] != b[key]:
        _json_paths(a[key], b[key], path, depth + 1, out)
    return out
  if isinstance(a, list) and isinstance(b, list):
    if len(a) != len(b):
      out.append({"path": prefix[:SELECTION_DIAG_MAX_LEN], "change": "length",
                  "type": f"{len(a)}->{len(b)}"})
      return out
    for index, (left, right) in enumerate(zip(a, b, strict=True)):
      if len(out) >= SELECTION_DIAG_MAX_KEYS:
        break
      if left != right:
        _json_paths(left, right, f"{prefix}/{index}"[:SELECTION_DIAG_MAX_LEN], depth + 1, out)
    return out
  if a != b:
    out.append({"path": prefix[:SELECTION_DIAG_MAX_LEN], "change": "value",
                "type": _json_shape(a, depth)})
  return out


def selection_diagnosis(stage, *, epoch_before, epoch_after, epoch_known, metadata_before, metadata_after,
                        read_started_ns, read_finished_ns):
  """Bounded, coordinate-free evidence for one selection_changed_during_read.

  metadata_after is None when the epoch check short-circuited before re-reading it.
  """
  def digest(raw):
    return None if raw is None else hashlib.sha256(raw).hexdigest()

  diag = {"schema_version": 1, "stage": stage, "clock": "host_monotonic_ns",
          "read_started_mono_ns": read_started_ns, "read_finished_mono_ns": read_finished_ns,
          "epoch_changed": (epoch_before != epoch_after) if epoch_known else "unknown",
          "metadata_read": metadata_after is not None,
          "metadata_changed": None if metadata_after is None else metadata_after != metadata_before,
          "metadata_before_bytes": None if metadata_before is None else len(metadata_before),
          "metadata_after_bytes": None if metadata_after is None else len(metadata_after),
          "metadata_before_sha256": digest(metadata_before),
          "metadata_after_sha256": digest(metadata_after)}
  if metadata_after is None or metadata_before is None:
    diag["json"] = "unknown"
    return diag
  try:
    before = json.loads(metadata_before[:SELECTION_DIAG_MAX_BYTES * 1024])
    after = json.loads(metadata_after[:SELECTION_DIAG_MAX_BYTES * 1024])
  except (ValueError, UnicodeDecodeError, TypeError) as error:
    diag["json"] = {"status": "unparsed", "error": type(error).__name__}
    return diag
  changed = _json_paths(before, after)
  diag["json"] = {"status": "parsed", "bytes_changed": metadata_after != metadata_before,
                  "semantic_changed": bool(changed), "changed": changed,
                  "keys_before": len(before) if isinstance(before, dict) else None,
                  "keys_after": len(after) if isinstance(after, dict) else None}
  return diag


def _selection_changed(stage, epoch_before, metadata_before, metadata_after, started, *, epoch_after=None):
  """Attach bounded evidence to the unchanged exception, then raise the original reason."""
  error = ValueError("selection_changed_during_read")
  try:
    error.diagnosis = selection_diagnosis(
      stage, epoch_before=epoch_before,
      epoch_after=epoch_after if epoch_after is not None else epoch_before,
      epoch_known=epoch_after is not None, metadata_before=metadata_before,
      metadata_after=metadata_after, read_started_ns=started, read_finished_ns=time.monotonic_ns())
  except Exception:
    # Diagnosis must never replace or mask the original rejection.
    error.diagnosis = {"schema_version": 1, "stage": stage, "status": "diagnosis_failed"}
  return error


def gather(device, *, read=read_file, listing=cache_names, epoch=provider_epoch) -> tuple[dict, dict[str, bytes]]:
  started = time.monotonic_ns()
  before_epoch = epoch(device)
  before_names = listing(device)
  if not before_names:
    raise ValueError("no_navigation_cache")
  before_metadata = read(device, ALLOWED_PATH)
  files = {}
  for name in before_names:
    data = read(device, name)
    if sum(map(len, files.values())) + len(data) + len(before_metadata) > MAX_TOTAL_BYTES:
      raise ValueError("snapshot_size_limit")
    files[name] = data
  if listing(device) != before_names or epoch(device) != before_epoch:
    raise ValueError("provider_or_cache_set_changed")
  after_metadata = read(device, ALLOWED_PATH)
  if after_metadata != before_metadata:
    raise _selection_changed("A", before_epoch, before_metadata, after_metadata, started)
  if any(read(device, name) != data for name, data in files.items()):
    raise ValueError("cache_changed_during_read")
  after_epoch = epoch(device)
  if after_epoch != before_epoch:
    raise _selection_changed("B", before_epoch, before_metadata, None, started, epoch_after=after_epoch)
  final_metadata = read(device, ALLOWED_PATH)
  if final_metadata != before_metadata:
    raise _selection_changed("B", before_epoch, before_metadata, final_metadata, started,
                             epoch_after=after_epoch)
  files[ALLOWED_PATH] = before_metadata
  report = {"schema_version": 1, "source_app_session_id": before_epoch,
            "read_started_mono_ns": started, "read_finished_mono_ns": time.monotonic_ns(),
            "read_window_stable": True, "active_guidance_verified": False,
            "route_identity_verified": False, "complete_route_verified": False,
            "files": [{"source": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                      for name, data in files.items()]}
  return report, files


def main() -> None:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--adb")
  parser.add_argument("--port", type=int, default=5039)
  parser.add_argument("--serial")
  parser.add_argument("--out", type=Path)
  args = parser.parse_args()
  out = args.out or ROOT / "captures" / ("route-snapshot-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
  with Device(choose_adb(args.adb), args.port, args.serial) as device:
    report, files = gather(device)
  out.mkdir(mode=0o700, parents=True, exist_ok=False)
  for source, data in files.items():
    local = "metadata.bin" if source == ALLOWED_PATH else Path(source).name
    write_private(out / local, data)
    next(item for item in report["files"] if item["source"] == source)["file"] = local
  write_private(out / "report.json", (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode())
  print(json.dumps({"files": len(files), "read_window_stable": True, "report": str((out / "report.json").resolve()),
                    "route_identity_verified": False, "complete_route_verified": False}, ensure_ascii=False))


if __name__ == "__main__":
  try:
    main()
  except Exception as error:
    raise SystemExit(f"导航快照失败：{type(error).__name__}") from None
