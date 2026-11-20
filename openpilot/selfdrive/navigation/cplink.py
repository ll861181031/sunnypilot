"""Shared CPlink ownership lease; navigation and speed are independent services."""
import os
import tempfile
import time
from pathlib import Path

CPLINK_GATE = Path("/tmp/cplink_navi_source")
GATE_MAX_AGE_S = 5.0
MESSAGE_TIMEOUT = {"navInstruction": 0.5, "liveMapDataSP": 1.5}


class CplinkMailbox:
  """Atomic local handoff; only navd/mapd own the public msgq sockets."""
  def __init__(self, gate_path: str | Path):
    self.gate_path = Path(gate_path)

  def send(self, service: str, msg) -> None:
    if service not in MESSAGE_TIMEOUT or msg.which() != service:
      raise ValueError(f"Unsupported CPlink message: {service}")
    path = Path(f"{self.gate_path}.{service}")
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temporary = Path(name)
    try:
      with os.fdopen(fd, "wb") as handle:
        handle.write(msg.to_bytes())
      os.replace(temporary, path)
    finally:
      temporary.unlink(missing_ok=True)


def read_cplink_message(service: str, gate_path: str | Path | None = None):
  """Preserve the original monotonic stamp; polling cannot make old data fresh."""
  from openpilot.cereal import log
  path = Path(f"{gate_path if gate_path is not None else CPLINK_GATE}.{service}")
  try:
    with path.open("rb") as handle:
      raw = handle.read(65537)
    if len(raw) > 65536:
      return None
    with log.Event.from_bytes(raw) as msg:
      age = time.monotonic() - msg.logMonoTime / 1e9
      if msg.which() == service and 0.0 <= age < MESSAGE_TIMEOUT[service]:
        return msg.as_builder()
  except Exception:
    # Missing, malformed, or unknown messages are unavailable.
    pass
  return None


def source_active(service: str, path: str | Path | None = None) -> bool:
  try:
    with open(path if path is not None else CPLINK_GATE, encoding="utf-8") as gate:
      # mtime is a wall-clock timestamp, so monotonic cannot be compared with it.
      age = time.time() - os.fstat(gate.fileno()).st_mtime  # noqa: TID251
      content = gate.read(32).strip()
    return 0.0 <= age < GATE_MAX_AGE_S and content in (service, "nav+speed")
  except (OSError, UnicodeError):
    return False


def cplink_navi_active() -> bool:
  return amap_root_nav_enabled() or source_active("nav")


def amap_root_nav_enabled() -> bool:
  return os.getenv("SUNNYPILOT_AMAP_ROOT_NAV", "0") == "1"
