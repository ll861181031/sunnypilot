"""Select a host-compatible mapd and pass the active openpilot paths to it."""
import hashlib
import json
import os
import platform
from pathlib import Path

from openpilot.sunnypilot.mapd import MAPD_BIN_DIR, MAPD_PATH, VERSION

NATIVE_MAPD_PATH = Path(MAPD_BIN_DIR) / "mapd-linux-amd64"


def mapd_path() -> Path:
  return NATIVE_MAPD_PATH if platform.machine() == "x86_64" else Path(MAPD_PATH)


def mapd_unavailable_reason() -> str | None:
  """None when a usable mapd exists, otherwise a human-readable reason.

  Kept separate from mapd_available() so callers can surface *why* mapd is off
  instead of silently dropping the mapd and OSM manager processes.
  """
  machine = platform.machine()
  if platform.system() != "Linux":
    return f"unsupported OS {platform.system()}"
  if machine not in ("x86_64", "aarch64"):
    return f"unsupported architecture {machine}"

  binary = mapd_path()
  try:
    if not binary.exists():
      return f"{binary} is missing"
    if not os.access(binary, os.X_OK):
      return f"{binary} is not executable"
    with binary.open("rb") as f:
      header = f.read(20)
      if header[:6] != b"\x7fELF\x02\x01":
        return f"{binary} is not a 64-bit little-endian ELF"
      if int.from_bytes(header[18:20], "little") != {"x86_64": 62, "aarch64": 183}[machine]:
        return f"{binary} is built for a different architecture"
      if machine == "x86_64":
        metadata = json.loads(binary.with_suffix(".json").read_text())
        f.seek(0)
        if metadata.get("version") != VERSION:
          return f"{binary} version {metadata.get('version')!r} does not match {VERSION!r}"
        if metadata.get("sha256") != hashlib.file_digest(f, "sha256").hexdigest():
          return f"{binary} checksum does not match its manifest"
    return None
  except (OSError, ValueError, AttributeError) as e:
    return f"{binary} could not be validated: {e}"


def mapd_available() -> bool:
  """Reject missing, wrong-architecture, or outdated local executables."""
  return mapd_unavailable_reason() is None


def main() -> None:
  from openpilot.common.hardware.hw import Paths
  from openpilot.common.params import Params

  if not mapd_available():
    raise RuntimeError("No compatible mapd. On Linux x86_64, run python -m openpilot.sunnypilot.mapd.build_native --go /path/to/go")
  root = Paths.mapd_root()
  os.makedirs(root, exist_ok=True)
  env = dict(os.environ,
             MAPD_ROOT=root,
             MAPD_PARAMS_PATH=Params().get_param_path(),
             MAPD_MEM_PARAMS_PATH=Params("/dev/shm/params").get_param_path())
  binary = str(mapd_path())
  os.chdir(root)
  os.execve(binary, [binary], env)


if __name__ == "__main__":
  main()
