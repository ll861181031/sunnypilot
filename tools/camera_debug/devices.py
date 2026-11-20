"""Enumerate V4L2 video nodes and the modes each one advertises.

Read-only by design: nothing here opens a camera. The debug UI uses this to
offer only modes the hardware actually advertises, so a run cannot silently
fall back to a different size/rate and produce numbers that describe a mode the
user did not pick.

UVC exposes each camera as two nodes (streaming + metadata). Both are listed,
but only the node with the ``Video Capture`` capability can be opened as a
camera, which is why ``VideoNode.capture`` exists.
"""

import glob
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field

SYSFS_VIDEO = "/sys/class/video4linux"
BY_ID_DIR = "/dev/v4l/by-id"
V4L2_CTL = "v4l2-ctl"

# V4L2_CAP_VIDEO_CAPTURE / V4L2_CAP_VIDEO_CAPTURE_MPLANE, used only as a
# fallback when `v4l2-ctl --info` cannot be parsed for its capability list.
CAP_VIDEO_CAPTURE_BITS = 0x00000001 | 0x00001000

_FOURCC_RE = re.compile(r"^\s*\[(\d+)\]:\s*'([^']+)'")
_SIZE_RE = re.compile(r"^\s*Size:\s*(\w+)\s*(.*)$")
_WH_RE = re.compile(r"(\d+)x(\d+)")
_FPS_RE = re.compile(r"\(([\d.]+)\s*fps\)")
_CAPS_RE = re.compile(r"^\s*(Device Caps|Capabilities)\s*:\s*(0x[0-9a-fA-F]+)")
_VIDEO_CAPTURE_LINE_RE = re.compile(r"^\s+Video Capture\s*$")

# OpenCV reports the fourcc as a packed int; the UI works in four-character
# strings. Order matters: this is the preference order offered by the UI.
KNOWN_FOURCC = ("MJPG", "YUYV")


@dataclass
class Mode:
  """One advertised (fourcc, size) pair with the frame rates it supports."""
  width: int
  height: int
  fourcc: str
  fps: list = field(default_factory=list)
  stepwise: bool = False

  def as_dict(self):
    return {"width": self.width, "height": self.height, "fourcc": self.fourcc,
            "fps": self.fps, "stepwise": self.stepwise}


@dataclass
class VideoNode:
  node: str
  index: int
  card: str
  capture: bool
  modes: list = field(default_factory=list)
  by_id: str = ""
  holders: list = field(default_factory=list)
  usb: dict = field(default_factory=dict)

  def as_dict(self):
    return {"node": self.node, "index": self.index, "card": self.card,
            "capture": self.capture, "by_id": self.by_id,
            "modes": [m.as_dict() for m in self.modes], "holders": self.holders,
            "usb": self.usb}


def have_v4l2_ctl() -> bool:
  return shutil.which(V4L2_CTL) is not None


def _run(args: list) -> str:
  """Run a query command, returning '' instead of raising.

  Every caller degrades to a manual-entry path when the query fails, so a
  missing device or an unplugged camera can never take the tool down.
  """
  try:
    out = subprocess.run([V4L2_CTL, *args], capture_output=True, text=True, timeout=5)
  except (OSError, subprocess.SubprocessError):
    return ""
  return out.stdout or ""


def parse_list_formats_ext(text: str) -> list:
  """Parse `v4l2-ctl --list-formats-ext` output into Mode records.

  Only the human-readable format is parsed: it is stable across v4l2-ctl
  versions while the JSON mode has changed shape. Stepwise-only formats (some
  UVC drivers) are recorded at their maximum size and flagged, because the UI
  still needs to offer them.
  """
  modes = []
  fourcc = None
  current = None
  for line in text.splitlines():
    m = _FOURCC_RE.match(line)
    if m:
      fourcc = m.group(2).upper()
      current = None
      continue
    if fourcc is None:
      continue
    s = _SIZE_RE.match(line)
    if s:
      kind, rest = s.group(1).lower(), s.group(2)
      # Drop the trailing "(step 1x1)" of stepwise lines: it contains an "NxM"
      # that would otherwise be mistaken for the maximum size.
      wh = _WH_RE.findall(rest.split("(")[0])
      if not wh:
        continue
      # Stepwise lines read `Size: Stepwise 640x480 - 1920x1080 (step 1x1)`;
      # the last pair on the line is the maximum and is the only one worth offering.
      width, height = (int(v) for v in wh[-1])
      current = Mode(width=width, height=height, fourcc=fourcc, fps=[], stepwise=kind != "discrete")
      modes.append(current)
      continue
    f = _FPS_RE.search(line)
    if f is not None and current is not None:
      fps = float(f.group(1))
      if fps not in current.fps:
        current.fps.append(fps)

  merged = {}
  for mode in modes:
    key = (mode.fourcc, mode.width, mode.height)
    if key in merged:
      for fps in mode.fps:
        if fps not in merged[key].fps:
          merged[key].fps.append(fps)
    else:
      merged[key] = mode
  for mode in merged.values():
    mode.fps.sort(reverse=True)
  return list(merged.values())


def parse_capture_capability(text: str) -> bool:
  """True when `v4l2-ctl --info` says this node itself can stream frames.

  Only the `Device Caps` block describes the node. The top-level `Capabilities`
  block is device-wide and lists `Video Capture` for *both* nodes of a UVC
  camera - measured on this rig, /dev/video1 (the metadata node) reports
  0x84a00001 there - so reading it would offer the metadata node as a camera.
  """
  section = None
  device_caps = None
  device_has_flag = False
  top_caps = None
  for line in text.splitlines():
    m = _CAPS_RE.match(line)
    if m:
      section = m.group(1)
      if section == "Device Caps":
        device_caps = int(m.group(2), 16)
      else:
        top_caps = int(m.group(2), 16)
      continue
    if _VIDEO_CAPTURE_LINE_RE.match(line) and section == "Device Caps":
      device_has_flag = True
  if device_has_flag:
    return True
  if device_caps is not None:
    return bool(device_caps & CAP_VIDEO_CAPTURE_BITS)
  # Older v4l2-ctl only prints the device-wide capabilities.
  if top_caps is not None:
    return bool(top_caps & CAP_VIDEO_CAPTURE_BITS)
  return False


def list_by_id() -> dict:
  """Map /dev/videoN -> the stable /dev/v4l/by-id name, when one exists."""
  out = {}
  for path in sorted(glob.glob(os.path.join(BY_ID_DIR, "*"))):
    try:
      node = os.path.realpath(path)
    except OSError:
      continue
    out.setdefault(node, os.path.basename(path))
  return out


def _node_index(name: str) -> int:
  try:
    return int(name.replace("video", ""))
  except ValueError:
    return -1


_USB_PORT_RE = re.compile(r"^\d+-[\d.]+$")


def parse_usb_topology(device_dir: str, port: str) -> dict:
  """Read one USB device's sysfs attributes.

  Split out from `usb_topology` so the parsing can be tested against a
  directory tree instead of the machine's real USB bus.
  """
  def read(name, default=""):
    try:
      with open(os.path.join(device_dir, name)) as f:
        return f.read().strip()
    except OSError:
      return default

  def num(name):
    try:
      return int(read(name))
    except ValueError:
      return None

  # "1-1.2" is port 2 of the hub on root port 1: `behind_hub` matters because a
  # hub adds another device that can share the same isochronous budget.
  return {"port": port, "root_port": port.partition(".")[0], "behind_hub": "." in port,
          "bus": num("busnum"), "dev": num("devnum"),
          "speed_mbps": num("speed"), "product": read("product")}


def usb_topology(node: str, sysfs: str = SYSFS_VIDEO) -> dict:
  """Where a camera sits on the USB tree, for the shared-bus warning.

  Two UVC cameras on one USB 2.0 bus cannot both stream reliably: the driver
  reserves bus time by the *altsetting* it must switch to, not by the average
  bitrate, and a modest request from a 4K-capable camera can still need a
  196 Mbit/s isochronous reservation. Measured here: with the other camera
  already streaming the kernel refused the DECXIN's altsetting 11 with
  "Not enough bandwidth". Knowing the bus lets the UI warn *before* a run
  instead of after a silent failure.
  """
  name = os.path.basename(node)
  if not name.startswith("video"):
    return {}
  device_dir = os.path.dirname(os.path.realpath(os.path.join(sysfs, name, "device")))
  port = os.path.basename(device_dir)
  if not _USB_PORT_RE.match(port) or not os.path.isdir(device_dir):
    return {}
  return parse_usb_topology(device_dir, port)


def holders(node: str) -> list:
  """PIDs holding `node` open, best effort.

  camerad keeps the road camera open for the whole drive, and a camera can only
  be debugged while nobody else holds it. Reporting the holder is how the UI
  explains a failed open without this tool ever touching another process.
  """
  target = os.path.realpath(node)
  pids = []
  for entry in glob.glob("/proc/[0-9]*/fd/*"):
    try:
      if os.readlink(entry) != node and os.path.realpath(entry) != target:
        continue
    except OSError:
      continue  # gone, or owned by another user
    pid = int(entry.split("/")[2])
    if pid != os.getpid() and pid not in pids:
      pids.append(pid)
  return sorted(pids)


# Enumerating shells out to v4l2-ctl twice per node, and the UI polls the device
# list. A few seconds of staleness is harmless: opening the device is the real
# test, and a hot-plug is picked up on the next refresh.
CACHE_TTL_S = 3.0
_cache = {"at": 0.0, "nodes": None}


def clear_cache():
  _cache["nodes"] = None


def enumerate_nodes(with_modes: bool = True, use_cache: bool = True) -> list:
  """All V4L2 nodes on the machine, streaming nodes first."""
  now = time.monotonic()
  if use_cache and _cache["nodes"] is not None and now - _cache["at"] < CACHE_TTL_S:
    return _cache["nodes"]
  by_id = list_by_id()
  has_ctl = have_v4l2_ctl()
  nodes = []
  for path in sorted(glob.glob(os.path.join(SYSFS_VIDEO, "video*")), key=_node_index_of_path):
    name = os.path.basename(path)
    node = f"/dev/{name}"
    try:
      with open(os.path.join(path, "name")) as f:
        card = f.read().strip()
    except OSError:
      card = name
    info = _run(["-d", node, "--info"]) if has_ctl else ""
    modes = parse_list_formats_ext(_run(["-d", node, "--list-formats-ext"])) if (with_modes and has_ctl) else []
    nodes.append(VideoNode(
      node=node,
      index=_node_index(name),
      card=card,
      # Without v4l2-ctl the node is assumed usable; opening it is the real test.
      capture=parse_capture_capability(info) if info else True,
      modes=modes,
      by_id=by_id.get(os.path.realpath(node), ""),
      holders=holders(node),
      usb=usb_topology(node),
    ))
  nodes.sort(key=lambda n: (not n.capture, n.index))
  _cache["nodes"] = nodes
  _cache["at"] = now
  return nodes


def _node_index_of_path(path: str) -> int:
  return _node_index(os.path.basename(path))


def camera_nodes(with_modes: bool = True) -> list:
  return [n for n in enumerate_nodes(with_modes=with_modes) if n.capture]


def find_node(node_or_by_id: str) -> VideoNode | None:
  wanted = os.path.realpath(node_or_by_id) if os.path.exists(node_or_by_id) else node_or_by_id
  for n in enumerate_nodes(with_modes=False):
    if n.node == wanted or n.by_id == node_or_by_id:
      return n
  return None


def mode_supported(node: VideoNode, fourcc: str, width: int, height: int, fps: float) -> tuple:
  """Check a requested mode against the advertised list.

  Returns (ok, note). A mismatch is reported, not enforced: the request still
  goes to the driver, and the negotiated values are displayed next to the
  requested ones precisely so the difference is visible.
  """
  if not node.modes:
    return True, "设备未上报模式列表（缺少 v4l2-ctl），按请求直接尝试"
  fourcc = fourcc.upper()
  same_fmt = [m for m in node.modes if m.fourcc == fourcc]
  if not same_fmt:
    return False, f"{node.node} 不支持 {fourcc}，可用：{', '.join(sorted({m.fourcc for m in node.modes}))}"
  exact = [m for m in same_fmt if m.width == width and m.height == height]
  if not exact:
    sizes = ", ".join(f"{m.width}x{m.height}" for m in same_fmt[:6])
    return False, f"{node.node} 的 {fourcc} 不支持 {width}x{height}，可用：{sizes} …"
  rates = exact[0].fps
  if rates and fps and not any(abs(fps - r) < 0.51 for r in rates):
    return False, f"{node.node} 在 {width}x{height} {fourcc} 下只支持 {rates} fps，驱动会自行取最接近值"
  return True, ""
