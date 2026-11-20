"""Shared helpers for the YOLO dataset pipeline.

Everything here is pure Python (plus numpy/cv2) so the individual stages stay
testable without ffmpeg, a model, or the real camera data.

Layout conventions used by every stage (see README.md):

    /data/yolo_dataset/                 working root (never inside the repo)
      _frames/<route>/<nnnn>.jpg         raw extracted frames
      _meta/<route>.json                 per-frame t / hour / brightness
      _auto/<route>.json                 per-frame auto detections
      <batch>/                           the delivered dataset
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, UTC

# Canonical image geometry of a delivered dataset: the published road-camera
# resolution of sunnypilot. Sources with a different aspect ratio are
# letterboxed (padding, no stretching) so every frame is geometrically
# consistent while keeping the full field of view.
TARGET_SIZE = (1344, 760)

# Taxonomy ids that are "two wheelers" for prioritisation: bicycle, e_bike,
# motorcycle and the four tricycle buckets. Riders of these carry the active
# learner rather than the passive pedestrian class, hence the high weight.
TWO_WHEELER_IDS = frozenset({1, 2})
PERSON_ID = 0
# Everything that drives on four wheels.
VEHICLE_IDS = frozenset({3, 4, 5, 6, 7, 8})

# Priority weights, exactly as specified in the task: 5 per two-wheeler
# detection, 2 per person, 1 per vehicle.
PRIORITY_WEIGHTS = ((TWO_WHEELER_IDS, 5), ((PERSON_ID,), 2), (VEHICLE_IDS, 1))

DAY_START_HOUR = 8
DAY_END_HOUR = 18  # exclusive

DEFAULT_TAXONOMY = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "yolo_annotator", "taxonomy.json")
DEFAULT_DATA_ROOT = os.path.expanduser("~/.comma/media/0/realdata")
DEFAULT_WORK_ROOT = "/data/yolo_dataset"
DEFAULT_MODEL = os.path.join("/data/sunnypilot", "openpilot", "sunnypilot", "yolod",
                             "models", "yolo11n.onnx")


# --------------------------------------------------------------------------- io

def utc_now() -> str:
  return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_json(path: str, default=None):
  if not os.path.exists(path):
    return default
  with open(path, encoding="utf-8") as f:
    return json.load(f)


def write_json(path: str, payload, indent: int = 2) -> None:
  """Write JSON atomically so a killed run cannot leave a half-written file."""
  directory = os.path.dirname(os.path.abspath(path))
  os.makedirs(directory, exist_ok=True)
  fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
  try:
    with os.fdopen(fd, "w", encoding="utf-8") as f:
      json.dump(payload, f, ensure_ascii=False, indent=indent)
      f.write("\n")
    os.replace(tmp, path)
  except BaseException:
    if os.path.exists(tmp):
      os.unlink(tmp)
    raise


def read_jsonl(path: str) -> list[dict]:
  rows: list[dict] = []
  if not os.path.exists(path):
    return rows
  with open(path, encoding="utf-8") as f:
    for line in f:
      line = line.strip()
      if line:
        rows.append(json.loads(line))
  return rows


# ---------------------------------------------------------------------- taxonomy

@dataclass(frozen=True)
class Taxonomy:
  name: str
  version: int
  classes: list[dict]          # [{"id", "name", "zh", "desc", "color"}]
  coco_to_taxonomy: dict       # COCO label -> taxonomy name

  @property
  def num_classes(self) -> int:
    return len(self.classes)

  @property
  def names(self) -> list[str]:
    return [c["name"] for c in self.classes]

  @property
  def id_by_name(self) -> dict[str, int]:
    return {c["name"]: int(c["id"]) for c in self.classes}

  def class_of(self, name: str) -> int | None:
    return self.id_by_name.get(name)

  def map_coco(self, coco_label: str) -> int | None:
    """COCO class name -> taxonomy id, or None when the class is not modelled."""
    taxonomy_name = self.coco_to_taxonomy.get(coco_label)
    if taxonomy_name is None:
      return None
    return self.id_by_name.get(taxonomy_name)


def load_taxonomy(path: str = DEFAULT_TAXONOMY) -> Taxonomy:
  raw = read_json(path)
  if not isinstance(raw, dict) or "classes" not in raw:
    raise ValueError(f"not a taxonomy file: {path}")
  classes = sorted(raw["classes"], key=lambda c: int(c["id"]))
  ids = [int(c["id"]) for c in classes]
  if ids != list(range(len(classes))):
    raise ValueError(f"taxonomy ids must be 0..{len(classes) - 1}, got {ids}")
  names = [c["name"] for c in classes]
  if len(set(names)) != len(names):
    raise ValueError("taxonomy class names must be unique")
  mapping = raw.get("coco_to_taxonomy") or {}
  name_set = set(names)
  unknown = sorted({v for v in mapping.values() if v not in name_set})
  if unknown:
    raise ValueError(f"coco_to_taxonomy points at unknown classes: {unknown}")
  return Taxonomy(name=raw.get("name", "unknown"),
                  version=int(raw.get("version", 1)),
                  classes=[{"id": int(c["id"]), "name": c["name"], "zh": c.get("zh", c["name"]),
                            "desc": c.get("desc", ""), "color": list(c.get("color", [128, 128, 128]))}
                           for c in classes],
                  coco_to_taxonomy={str(k): str(v) for k, v in mapping.items()})


# ----------------------------------------------------------------- source frames

def route_id(route_dir: str) -> str:
  return os.path.basename(route_dir.rstrip("/"))


def camera_path(route_dir: str, camera: str = "fcamera") -> str:
  for ext in (".hevc", ".mp4"):
    candidate = os.path.join(route_dir, f"{camera}{ext}")
    if os.path.isfile(candidate) and os.path.getsize(candidate) > 0:
      return candidate
  return ""


def list_routes(data_root: str = DEFAULT_DATA_ROOT) -> list[str]:
  """Route directories that have a usable road camera, boot excluded."""
  if not os.path.isdir(data_root):
    return []
  routes = []
  with os.scandir(data_root) as entries:
    for entry in entries:
      if not entry.is_dir() or entry.name == "boot":
        continue
      if camera_path(entry.path):
        routes.append(entry.path)
  return sorted(routes)


def recorded_hour(route_dir: str) -> int | None:
  """Local hour of the segment.

  Loggerd writes the segment while it records, so the camera file mtime is the
  end of the segment; within a 60 s segment that is accurate to a minute. There
  is no timezone-free timestamp inside the file names, so this is the cheapest
  signal that does not require decoding rlog.
  """
  path = camera_path(route_dir)
  if not path:
    return None
  try:
    return datetime.fromtimestamp(os.path.getmtime(path)).hour
  except (OSError, OverflowError, ValueError):
    return None


def is_daylight(hour: int | None) -> bool:
  """Day bucket definition fixed by the task: 08:00-17:59 is day, the rest night."""
  if hour is None:
    return True
  return DAY_START_HOUR <= hour < DAY_END_HOUR


def new_route_meta(route_dir: str, **extra) -> dict:
  hour = recorded_hour(route_dir)
  meta = {"route": route_id(route_dir), "dir": os.path.abspath(route_dir),
          "hour": hour, "daylight": is_daylight(hour)}
  meta.update(extra)
  return meta


# ------------------------------------------------------------------- image utils

def mean_brightness(bgr) -> float:
  """Mean luma (0-255) of a BGR image; used to sanity check the day/night split."""
  import cv2
  import numpy as np
  if bgr is None:
    return 0.0
  if bgr.ndim == 3:
    luma = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
  else:
    luma = bgr
  return float(np.asarray(luma, dtype=np.float32).mean())


LETTERBOX_PAD = 114  # the Ultralytics / detector.py padding value


def letterbox_size(src_w: int, src_h: int, size: tuple[int, int] = TARGET_SIZE) -> tuple[int, int, int, int]:
  """Geometry of an aspect-preserving fit: (new_w, new_h, pad_x, pad_y)."""
  dst_w, dst_h = int(size[0]), int(size[1])
  if src_w <= 0 or src_h <= 0:
    raise ValueError(f"invalid source size {src_w}x{src_h}")
  scale = min(dst_w / src_w, dst_h / src_h)
  new_w = min(dst_w, max(1, int(round(src_w * scale))))
  new_h = min(dst_h, max(1, int(round(src_h * scale))))
  return new_w, new_h, (dst_w - new_w) // 2, (dst_h - new_h) // 2


def letterbox(bgr, size: tuple[int, int] = TARGET_SIZE, pad_value: int = LETTERBOX_PAD):
  """Resize keeping the aspect ratio and pad to ``size``; returns (image, geometry)."""
  import cv2
  import numpy as np
  src_h, src_w = bgr.shape[:2]
  new_w, new_h, pad_x, pad_y = letterbox_size(src_w, src_h, size)
  resized = cv2.resize(bgr, (new_w, new_h), interpolation=cv2.INTER_LINEAR) \
      if (new_w, new_h) != (src_w, src_h) else bgr
  dst_w, dst_h = int(size[0]), int(size[1])
  if (new_w, new_h) == (dst_w, dst_h):
    return resized, (new_w, new_h, pad_x, pad_y)
  canvas = np.full((dst_h, dst_w, 3), pad_value, dtype=np.uint8)
  canvas[pad_y:pad_y + new_h, pad_x:pad_x + new_w] = resized
  return canvas, (new_w, new_h, pad_x, pad_y)


def ffmpeg_filter(fps: float, size: tuple[int, int] = TARGET_SIZE) -> str:
  """ffmpeg filter that samples ``fps`` frames per second into a letterboxed image.

  Frames whose aspect ratio already matches the target are pure-scaled; the
  others are padded instead of stretched (``pad`` keeps them centred and the
  offsets even so JPEG chroma subsampling stays clean).
  """
  dst_w, dst_h = int(size[0]), int(size[1])
  pad_color = f"0x{LETTERBOX_PAD:02x}{LETTERBOX_PAD:02x}{LETTERBOX_PAD:02x}"
  scale = f"scale={dst_w}:{dst_h}:force_original_aspect_ratio=decrease:force_divisible_by=2"
  pad = f"pad={dst_w}:{dst_h}:(ow-iw)/2:(oh-ih)/2:color={pad_color}"
  return f"fps={fps:g},{scale},{pad},setsar=1"


# ------------------------------------------------------------------------ yolo io

def format_yolo_line(cls: int, box_normalized) -> str:
  """``cls cx cy w h`` with six decimals from a normalized (x1, y1, x2, y2) box."""
  x1, y1, x2, y2 = (clamp01(v) for v in box_normalized)
  cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
  w, h = x2 - x1, y2 - y1
  return f"{int(cls)} {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}"


def clamp01(value: float) -> float:
  return min(1.0, max(0.0, float(value)))


def write_labels(path: str, lines: list[str]) -> None:
  os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
  with open(path, "w", encoding="utf-8") as f:
    for line in lines:
      f.write(line.rstrip("\n") + "\n")


def parse_label_line(line: str, num_classes: int) -> tuple[int, tuple[float, float, float, float]]:
  """Parse one YOLO label line, raising ValueError with a human-readable reason."""
  parts = line.split()
  if len(parts) != 5:
    raise ValueError(f"expected 5 columns, got {len(parts)}")
  try:
    values = [float(p) for p in parts]
  except ValueError as error:
    raise ValueError(f"non-numeric column ({error})") from error
  cls = values[0]
  if cls != int(cls):
    raise ValueError(f"class id must be an integer, got {parts[0]}")
  cls = int(cls)
  if not 0 <= cls < num_classes:
    raise ValueError(f"class id {cls} outside 0..{num_classes - 1}")
  cx, cy, w, h = values[1:]
  if not all(0.0 <= v <= 1.0 for v in (cx, cy, w, h)):
    raise ValueError("coordinates outside [0, 1]")
  if w <= 0 or h <= 0:
    raise ValueError("box width/height must be positive")
  return cls, (cx, cy, w, h)


# -------------------------------------------------------------------- prioritising

def count_by_group(detections) -> dict[str, int]:
  """Count auto detections per priority group.

  ``detections`` is an iterable of dicts with a taxonomy class in ``cls``.
  """
  counts = {"two_wheeler": 0, "person": 0, "vehicle": 0, "other": 0}
  for det in detections:
    cls = int(det["cls"])
    if cls in TWO_WHEELER_IDS:
      counts["two_wheeler"] += 1
    elif cls == PERSON_ID:
      counts["person"] += 1
    elif cls in VEHICLE_IDS:
      counts["vehicle"] += 1
    else:
      counts["other"] += 1
  return counts


def priority_of(counts: dict[str, int]) -> int:
  """5 per two-wheeler, 2 per person, 1 per vehicle (other classes weigh 0)."""
  return 5 * counts["two_wheeler"] + 2 * counts["person"] + counts["vehicle"]


# ------------------------------------------------------------------------- naming

def id_of(index: int) -> str:
  """Six-digit zero-padded sample id: 1 -> '000001'."""
  if index < 1:
    raise ValueError("sample ids start at 1")
  return f"{index:06d}"
