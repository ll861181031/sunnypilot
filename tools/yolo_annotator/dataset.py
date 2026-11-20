"""Dataset access layer for the YOLO labeler.

The on-disk batch layout is the one produced by ``tools/yolo_dataset``::

    <batch>/
      images/<id>.jpg          # image, typically 1344x760
      labels/<id>.txt          # current YOLO label: "class cx cy w h" (normalised)
      auto_labels/<id>.txt     # automatic pre-labels (read-only reference)
      auto_meta.json           # {"<id>": [{"cls": 0, "conf": .83, "xyxy": [x1,y1,x2,y2]}]}
      dataset.json             # schema, batch, classes, images, counts, split
      taxonomy.json            # class table (22 classes)
      review_state.json        # maintained by this app

Everything here is plain Python (no Tk) so it can be unit tested headless.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from collections.abc import Sequence

import geometry

SCHEMA_VERSION = 1

FILTER_ALL = "all"
FILTER_UNREVIEWED = "unreviewed"
FILTER_REVIEWED = "reviewed"
FILTER_AUTO_TWOWHEEL = "auto_twowheel"
FILTER_HIGH_PRIORITY = "high_priority"
FILTER_HAS_AUTO = "has_auto"
FILTER_EDITED = "edited"

#: GUI order of the filter combobox.
FILTER_CHOICES: tuple[tuple[str, str], ...] = (
    ("全部", FILTER_ALL),
    ("未复核", FILTER_UNREVIEWED),
    ("已复核", FILTER_REVIEWED),
    ("有自动两轮", FILTER_AUTO_TWOWHEEL),
    ("高优先级", FILTER_HIGH_PRIORITY),
    ("有自动预标", FILTER_HAS_AUTO),
    ("已手工修改", FILTER_EDITED),
)

#: Classes considered "two wheelers" for the auto-prefiltered review queue.
TWO_WHEELER_NAMES = (
    "two_wheeler",
    "bicycle",
    "e_bike",
    "motorcycle",
    "tricycle",
    "tricycle_cargo",
    "tricycle_awning",
    "tricycle_passenger",
    "tricycle_other",
)

#: priority >= this value counts as "high" (see build.py: 5*two_wheeler + 2*person + 1*vehicle).
PRIORITY_HIGH = 3

IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".bmp", ".webp")


class DatasetError(RuntimeError):
    """Raised when a batch directory cannot be opened."""


# ---------------------------------------------------------------------------
# Small file helpers
# ---------------------------------------------------------------------------
def atomic_write_text(path: os.PathLike | str, text: str, encoding: str = "utf-8") -> None:
    """Write *text* to *path* atomically (tmp file + ``os.replace``)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".tmp_",
                                    suffix=".part")
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def atomic_write_json(path: os.PathLike | str, payload: Any, indent: int = 2) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=indent) + "\n")


def read_json(path: os.PathLike | str, default: Any = None) -> Any:
    """Read JSON, returning *default* when the file is missing or invalid."""
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Class table
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ClassDef:
    """One entry of the taxonomy."""

    id: int
    name: str
    zh: str = ""
    color: tuple[int, int, int] = (200, 200, 200)
    desc: str = ""

    @property
    def label(self) -> str:
        """Human readable label used in the palette and on the canvas."""
        return self.zh or self.name

    @property
    def color_hex(self) -> str:
        r, g, b = (int(c) for c in self.color[:3])
        return f"#{r:02x}{g:02x}{b:02x}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "zh": self.zh,
            "color": list(self.color),
            "desc": self.desc,
        }


@dataclass
class Taxonomy:
    """The class table, always loaded from the dataset (never hard coded)."""

    name: str = "taxonomy"
    version: int = 1
    classes: list[ClassDef] = field(default_factory=list)
    note: str = ""

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Taxonomy:
        classes: list[ClassDef] = []
        for index, raw in enumerate(payload.get("classes") or []):
            color = raw.get("color") or (200, 200, 200)
            if len(color) < 3:
                color = (200, 200, 200)
            classes.append(ClassDef(
                id=int(raw.get("id", index)),
                name=str(raw.get("name", f"class_{index}")),
                zh=str(raw.get("zh", "")),
                color=(int(color[0]), int(color[1]), int(color[2])),
                desc=str(raw.get("desc", "")),
            ))
        classes.sort(key=lambda c: c.id)
        return cls(name=str(payload.get("name", "taxonomy")),
                   version=int(payload.get("version", 1) or 1),
                   classes=classes,
                   note=str(payload.get("note", "")))

    @classmethod
    def from_file(cls, path: os.PathLike | str) -> Taxonomy:
        payload = read_json(path)
        if not isinstance(payload, dict):
            raise DatasetError(f"无法读取类别表: {path}")
        taxonomy = cls.from_dict(payload)
        if not taxonomy.classes:
            raise DatasetError(f"类别表为空: {path}")
        return taxonomy

    def __len__(self) -> int:
        return len(self.classes)

    def __iter__(self):
        return iter(self.classes)

    def get(self, class_id: int) -> ClassDef | None:
        for item in self.classes:
            if item.id == class_id:
                return item
        return None

    @property
    def ids(self) -> list[int]:
        return [c.id for c in self.classes]

    @property
    def max_id(self) -> int:
        return max(self.ids) if self.classes else -1

    def color(self, class_id: int) -> tuple[int, int, int]:
        item = self.get(class_id)
        return item.color if item else (200, 200, 200)

    def color_hex(self, class_id: int) -> str:
        item = self.get(class_id)
        return item.color_hex if item else "#c8c8c8"

    def display(self, class_id: int) -> str:
        item = self.get(class_id)
        if item is None:
            return f"未知({class_id})"
        return item.label

    def name_of(self, class_id: int) -> str:
        item = self.get(class_id)
        return item.name if item else f"class_{class_id}"

    @property
    def two_wheeler_ids(self) -> list[int]:
        return [c.id for c in self.classes if c.name in TWO_WHEELER_NAMES]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "note": self.note,
            "classes": [c.to_dict() for c in self.classes],
        }


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------
@dataclass
class Label:
    """One YOLO label: class id plus a normalised centre box."""

    cls: int
    cx: float
    cy: float
    w: float
    h: float
    #: True when the box still matches an automatic pre-label. Not persisted.
    auto: bool = field(default=False, compare=False)
    #: Detection confidence of the matching pre-label, when known.
    conf: float | None = field(default=None, compare=False)

    # -- conversions ------------------------------------------------------
    @property
    def cxcywh(self) -> tuple[float, float, float, float]:
        return (self.cx, self.cy, self.w, self.h)

    @property
    def xyxy(self) -> tuple[float, float, float, float]:
        half_w = self.w / 2.0
        half_h = self.h / 2.0
        return (self.cx - half_w, self.cy - half_h, self.cx + half_w, self.cy + half_h)

    def as_abs(self, width: float, height: float) -> geometry.Box:
        return geometry.norm_to_abs_box(self.xyxy, width, height)

    @classmethod
    def from_xyxy(cls, class_id: int, x1: float, y1: float, x2: float, y2: float,
                  auto: bool = False, conf: float | None = None) -> Label:
        x1, y1, x2, y2 = _clamp_norm((x1, y1, x2, y2))
        w = max(0.0, x2 - x1)
        h = max(0.0, y2 - y1)
        return cls(cls=int(class_id), cx=(x1 + x2) / 2.0, cy=(y1 + y2) / 2.0,
                   w=w, h=h, auto=auto, conf=conf)

    @classmethod
    def from_abs_xyxy(cls, class_id: int, x1: float, y1: float, x2: float, y2: float,
                      width: float, height: float) -> Label:
        box = geometry.abs_to_norm_box((x1, y1, x2, y2), width, height)
        return cls.from_xyxy(class_id, *box)

    def key(self) -> tuple[float, ...]:
        """Order-insensitive identity used to compare label sets."""
        return (float(self.cls), round(self.cx, 5), round(self.cy, 5),
                round(self.w, 5), round(self.h, 5))

    def copy(self) -> Label:
        return replace(self)

    def to_line(self) -> str:
        return f"{int(self.cls)} {self.cx:.6f} {self.cy:.6f} {self.w:.6f} {self.h:.6f}"

    def clamped(self) -> Label:
        box = _clamp_norm(self.xyxy)
        return Label.from_xyxy(self.cls, *box, auto=self.auto, conf=self.conf)


def _clamp_norm(box: Sequence[float]) -> geometry.Box:
    x1, y1, x2, y2 = geometry.normalize_box(box)
    return (geometry.clamp(x1, 0.0, 1.0), geometry.clamp(y1, 0.0, 1.0),
            geometry.clamp(x2, 0.0, 1.0), geometry.clamp(y2, 0.0, 1.0))


def parse_label_lines(text: str, taxonomy: Taxonomy | None = None
                      ) -> tuple[list[Label], list[str]]:
    """Parse YOLO label text.

    Returns ``(labels, warnings)``.  Blank lines are ignored, a line with
    more than 5 columns keeps the first 5, malformed lines are skipped with a
    warning (never raised: a single bad line must not make an image
    unopenable).
    """
    labels: list[Label] = []
    warnings: list[str] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.replace(",", " ").split()
        if len(parts) < 5:
            warnings.append(f"第 {lineno} 行不足 5 列，已跳过")
            continue
        try:
            class_id = int(float(parts[0]))
            cx, cy, w, h = (float(parts[i]) for i in range(1, 5))
        except ValueError:
            warnings.append(f"第 {lineno} 行无法解析，已跳过")
            continue
        if taxonomy is not None and taxonomy.get(class_id) is None:
            warnings.append(f"第 {lineno} 行类别 {class_id} 不在类别表中，已跳过")
            continue
        if w <= 0.0 or h <= 0.0:
            warnings.append(f"第 {lineno} 行宽/高为 0，已跳过")
            continue
        labels.append(Label.from_xyxy(class_id, cx - w / 2, cy - h / 2,
                                     cx + w / 2, cy + h / 2))
    return labels, warnings


def format_labels(labels: Sequence[Label]) -> str:
    """Serialise labels to YOLO text (empty string for a negative sample)."""
    lines = [label.to_line() for label in labels]
    return "\n".join(lines) + ("\n" if lines else "")


def read_labels(path: os.PathLike | str, taxonomy: Taxonomy | None = None
                ) -> tuple[list[Label], list[str]]:
    """Read a label file; a missing file means "no boxes yet"."""
    try:
        with open(path, encoding="utf-8") as handle:
            return parse_label_lines(handle.read(), taxonomy)
    except FileNotFoundError:
        return [], []
    except OSError as exc:
        return [], [f"读取失败: {exc}"]


def write_labels(path: os.PathLike | str, labels: Sequence[Label]) -> None:
    """Atomically write a label file (empty file == negative sample)."""
    atomic_write_text(path, format_labels(labels))


def labels_key(labels: Sequence[Label]) -> list[tuple[float, ...]]:
    return sorted(label.key() for label in labels)


def labels_equal(a: Sequence[Label], b: Sequence[Label], tol: float = 1e-4) -> bool:
    """Order-insensitive comparison of two label lists."""
    if len(a) != len(b):
        return False
    ka = labels_key(a)
    kb = labels_key(b)
    for left, right in zip(ka, kb, strict=True):
        if left[0] != right[0]:
            return False
        if any(abs(x - y) > tol for x, y in zip(left[1:], right[1:], strict=True)):
            return False
    return True


def match_auto(labels: Sequence[Label], auto_labels: Sequence[Label],
               iou_threshold: float = 0.85) -> list[bool]:
    """Flag which *labels* still coincide with an automatic pre-label.

    A label counts as automatic when a pre-label of the same class overlaps
    it with IoU >= *iou_threshold*.
    """
    flags: list[bool] = []
    for label in labels:
        flag = False
        box = label.xyxy
        for auto in auto_labels:
            if auto.cls != label.cls:
                continue
            if geometry.box_iou(box, auto.xyxy) >= iou_threshold:
                flag = True
                break
        flags.append(flag)
    return flags


def annotate_auto(labels: list[Label], auto_labels: Sequence[Label],
                  iou_threshold: float = 0.85) -> list[Label]:
    """Return copies of *labels* with the ``auto``/``conf`` fields filled in."""
    result: list[Label] = []
    for label in labels:
        matched: Label | None = None
        for auto in auto_labels:
            if auto.cls == label.cls and \
                    geometry.box_iou(label.xyxy, auto.xyxy) >= iou_threshold:
                matched = auto
                break
        copy = label.copy()
        copy.auto = matched is not None
        copy.conf = matched.conf if matched is not None else None
        result.append(copy)
    return result


# ---------------------------------------------------------------------------
# Automatic pre-labels metadata
# ---------------------------------------------------------------------------
@dataclass
class AutoBox:
    """One automatic pre-label from ``auto_meta.json`` (normalised xyxy)."""

    cls: int
    conf: float
    xyxy: tuple[float, float, float, float]

    def to_label(self) -> Label:
        return Label.from_xyxy(self.cls, *self.xyxy, auto=True, conf=self.conf)


def load_auto_meta(path: os.PathLike | str) -> dict[str, list[AutoBox]]:
    """Load ``auto_meta.json``; tolerant of missing/invalid data."""
    payload = read_json(path, default={}) or {}
    result: dict[str, list[AutoBox]] = {}
    if not isinstance(payload, dict):
        return result
    for image_id, entries in payload.items():
        boxes: list[AutoBox] = []
        for entry in entries or []:
            try:
                cls = int(entry["cls"])
                conf = float(entry.get("conf", 0.0))
                xyxy = tuple(float(v) for v in entry["xyxy"])
                if len(xyxy) != 4:
                    continue
                boxes.append(AutoBox(cls=cls, conf=conf,
                                     xyxy=_clamp_norm(xyxy)))  # type: ignore[arg-type]
            except (KeyError, TypeError, ValueError):
                continue
        result[str(image_id)] = boxes
    return result


# ---------------------------------------------------------------------------
# Review state
# ---------------------------------------------------------------------------
@dataclass
class ReviewEntry:
    reviewed: bool = False
    edited: bool = False
    updated: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"reviewed": bool(self.reviewed), "edited": bool(self.edited),
                "updated": float(self.updated)}


class ReviewState:
    """``review_state.json``: per image review flags maintained by this app."""

    def __init__(self, path: os.PathLike | str | None = None,
                 entries: dict[str, ReviewEntry] | None = None,
                 dirty: bool = False) -> None:
        self.path = Path(path) if path is not None else None
        self.entries: dict[str, ReviewEntry] = entries or {}
        self.dirty = dirty

    @classmethod
    def load(cls, path: os.PathLike | str) -> ReviewState:
        payload = read_json(path, default={}) or {}
        entries: dict[str, ReviewEntry] = {}
        if isinstance(payload, dict):
            for image_id, raw in payload.items():
                if not isinstance(raw, dict):
                    continue
                entries[str(image_id)] = ReviewEntry(
                    reviewed=bool(raw.get("reviewed", False)),
                    edited=bool(raw.get("edited", False)),
                    updated=float(raw.get("updated", 0.0) or 0.0),
                )
        return cls(path=Path(path), entries=entries)

    def get(self, image_id: str) -> ReviewEntry:
        return self.entries.get(image_id, ReviewEntry())

    def is_reviewed(self, image_id: str) -> bool:
        return self.get(image_id).reviewed

    def is_edited(self, image_id: str) -> bool:
        return self.get(image_id).edited

    def set(self, image_id: str, reviewed: bool | None = None,
            edited: bool | None = None, now: float | None = None) -> ReviewEntry:
        entry = self.entries.get(image_id) or ReviewEntry()
        if reviewed is not None:
            entry.reviewed = bool(reviewed)
        if edited is not None:
            entry.edited = bool(edited)
        # Wall clock on purpose: this is a persisted "last touched" timestamp.
        entry.updated = time.time() if now is None else float(now)  # noqa: TID251
        self.entries[image_id] = entry
        self.dirty = True
        return entry

    def save(self, path: os.PathLike | str | None = None) -> Path:
        target = Path(path) if path is not None else self.path
        if target is None:
            raise DatasetError("review_state.json 路径未知")
        payload = {image_id: entry.to_dict() for image_id, entry in
                   sorted(self.entries.items())}
        atomic_write_json(target, payload)
        self.path = target
        self.dirty = False
        return target

    def to_dict(self) -> dict[str, Any]:
        return {image_id: entry.to_dict() for image_id, entry in
                sorted(self.entries.items())}


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------
@dataclass
class ImageEntry:
    """One image of the batch, as described by ``dataset.json``."""

    id: str
    file: str
    width: int = 0
    height: int = 0
    route: str = ""
    t: float = 0.0
    hour: int = -1
    split: str = ""
    priority: int = 0
    auto_counts: dict[str, int] = field(default_factory=dict)
    index: int = 0

    def path(self, root: Path) -> Path:
        candidate = Path(self.file)
        if candidate.is_absolute():
            return candidate
        return root / candidate

    @property
    def priority_stars(self) -> str:
        return "P" + str(int(self.priority))

    def summary(self, reviewed: bool, edited: bool) -> str:
        """One line of the image listbox: state, id, priority, route tag."""
        mark = "✓" if reviewed else "·"
        edit = "✎" if edited else " "
        parts = self.route.split("--")
        if len(parts) >= 3:
            tag = f"{parts[1][-4:]}-{parts[2]}"
        elif parts and parts[0]:
            tag = parts[0][-6:]
        else:
            tag = "-"
        if self.hour >= 0:
            tag = f"{tag} {self.hour:02d}h"
        return f"{mark}{edit} {self.id} P{self.priority} {tag}"

    @classmethod
    def from_dict(cls, raw: dict[str, Any], index: int = 0,
                  root: Path | None = None) -> ImageEntry:
        image_id = str(raw.get("id", ""))
        file = raw.get("file") or ""
        if not file:
            file = f"images/{image_id}.jpg"
        return cls(
            id=image_id,
            file=str(file),
            width=int(raw.get("width") or 0),
            height=int(raw.get("height") or 0),
            route=str(raw.get("route") or ""),
            t=float(raw.get("t") or 0.0),
            hour=int(raw.get("hour") if raw.get("hour") is not None else -1),
            split=str(raw.get("split") or ""),
            priority=int(raw.get("priority") or 0),
            auto_counts=dict(raw.get("auto_counts") or {}),
            index=index,
        )


# ---------------------------------------------------------------------------
# Dataset
# ---------------------------------------------------------------------------
class Dataset:
    """A single batch directory, opened for annotation."""

    def __init__(self, root: Path, payload: dict[str, Any], taxonomy: Taxonomy,
                 images: list[ImageEntry], auto_meta: dict[str, list[AutoBox]],
                 review_state: ReviewState, warnings: list[str] | None = None) -> None:
        self.root = root
        self.payload = payload
        self.taxonomy = taxonomy
        self.images = images
        self.auto_meta = auto_meta
        self.review_state = review_state
        self.warnings: list[str] = list(warnings or [])
        self._by_id: dict[str, ImageEntry] = {img.id: img for img in images}
        self._class_counts_cache: dict[str, int] | None = None
        self._auto_cache: dict[str, list[Label]] = {}
        self._two_wheeler_ids = set(taxonomy.two_wheeler_ids)

    # -- construction -----------------------------------------------------
    @classmethod
    def open(cls, root: os.PathLike | str) -> Dataset:
        root_path = Path(root).expanduser().resolve()
        if not root_path.is_dir():
            raise DatasetError(f"数据集目录不存在: {root_path}")
        payload = read_json(root_path / "dataset.json")
        if not isinstance(payload, dict):
            raise DatasetError(
                f"缺少 dataset.json（或无法解析）: {root_path}"
            )
        warnings: list[str] = []

        taxonomy_path = root_path / "taxonomy.json"
        if taxonomy_path.is_file():
            taxonomy = Taxonomy.from_file(taxonomy_path)
        elif payload.get("classes"):
            taxonomy = Taxonomy.from_dict({"classes": payload["classes"]})
            warnings.append("数据集缺少 taxonomy.json，已使用 dataset.json 的 classes")
        else:
            raise DatasetError(f"数据集缺少类别表（taxonomy.json / classes）: {root_path}")

        raw_images = payload.get("images")
        images: list[ImageEntry] = []
        if isinstance(raw_images, list) and raw_images:
            for index, raw in enumerate(raw_images):
                if isinstance(raw, dict):
                    entry = ImageEntry.from_dict(raw, index=index, root=root_path)
                    if entry.id:
                        images.append(entry)
        else:
            images = _scan_images(root_path)
            warnings.append("dataset.json 未列出图片，已扫描 images/ 目录")

        if not images:
            raise DatasetError(f"数据集中没有任何图片: {root_path}")

        auto_meta = load_auto_meta(root_path / "auto_meta.json")
        review_path = root_path / "review_state.json"
        review_state = ReviewState.load(review_path)
        dataset = cls(root_path, payload, taxonomy, images, auto_meta,
                      review_state, warnings)
        if not review_path.exists():
            # Create the file up front so background sync always has it.
            review_state.save(review_path)
        return dataset

    # -- basic properties -------------------------------------------------
    @property
    def batch(self) -> str:
        return str(self.payload.get("batch") or self.root.name)

    @property
    def split_counts(self) -> dict[str, int]:
        raw = self.payload.get("split")
        if isinstance(raw, dict):
            return {str(k): int(v) for k, v in raw.items()}
        counts: dict[str, int] = {}
        for image in self.images:
            counts[image.split or "unsplit"] = counts.get(image.split or "unsplit", 0) + 1
        return counts

    def image(self, image_id: str) -> ImageEntry:
        entry = self._by_id.get(image_id)
        if entry is None:
            raise DatasetError(f"数据集中没有图片 id: {image_id}")
        return entry

    def image_path(self, entry: ImageEntry) -> Path:
        path = entry.path(self.root)
        if path.is_file():
            return path
        # Fall back to images/<id>.<ext> when dataset.json lists an odd path.
        for suffix in IMAGE_SUFFIXES:
            candidate = self.root / "images" / f"{entry.id}{suffix}"
            if candidate.is_file():
                return candidate
        return path

    def label_path(self, image_id: str) -> Path:
        return self.root / "labels" / f"{image_id}.txt"

    def auto_label_path(self, image_id: str) -> Path:
        return self.root / "auto_labels" / f"{image_id}.txt"

    def review_state_path(self) -> Path:
        return self.root / "review_state.json"

    # -- labels -----------------------------------------------------------
    def load_labels(self, image_id: str) -> list[Label]:
        labels, warnings = read_labels(self.label_path(image_id), self.taxonomy)
        for warning in warnings:
            self.warnings.append(f"{image_id}: {warning}")
        return labels

    def load_auto_labels(self, image_id: str) -> list[Label]:
        """Pre-labels for *image_id*.

        ``auto_meta.json`` is preferred because it also carries the detection
        confidence used by the canvas captions; the ``auto_labels/`` file is
        the fallback (same boxes, no confidence).
        """
        cached = self._auto_cache.get(image_id)
        if cached is not None:
            return [label.copy() for label in cached]
        labels: list[Label] = []
        if self.auto_meta.get(image_id):
            labels = [box.to_label() for box in self.auto_meta[image_id]]
        if not labels:
            path = self.auto_label_path(image_id)
            if path.is_file():
                labels, _ = read_labels(path, self.taxonomy)
        self._auto_cache[image_id] = [label.copy() for label in labels]
        return labels

    def has_auto(self, image_id: str) -> bool:
        if self.auto_meta.get(image_id):
            return True
        path = self.auto_label_path(image_id)
        try:
            return path.is_file() and path.stat().st_size > 0
        except OSError:
            return False

    def edited_vs_auto(self, image_id: str, labels: Sequence[Label]) -> bool:
        auto = self.load_auto_labels(image_id)
        if not auto and not labels:
            return False
        return not labels_equal(labels, auto, tol=1e-4)

    def save_labels(self, image_id: str, labels: Sequence[Label],
                    reviewed: bool | None = None,
                    edited: bool | None = None,
                    now: float | None = None) -> dict[str, Any]:
        """Write one label file and update ``review_state.json`` atomically."""
        entry = self.image(image_id)
        clamped = [label.clamped() for label in labels]
        write_labels(self.label_path(image_id), clamped)

        if edited is None:
            edited = self.edited_vs_auto(image_id, clamped)
        elif edited:
            edited = True  # never downgrade an explicit edit flag
        state = self.review_state.set(image_id, reviewed=reviewed, edited=edited,
                                      now=now)
        self.review_state.save(self.review_state_path())
        self._class_counts_cache = None
        return {"id": image_id, "count": len(clamped),
                "reviewed": state.reviewed, "edited": state.edited,
                "width": entry.width, "height": entry.height}

    def mark_reviewed(self, image_id: str, reviewed: bool = True,
                      now: float | None = None) -> dict[str, Any]:
        state = self.review_state.set(image_id, reviewed=reviewed, now=now)
        self.review_state.save(self.review_state_path())
        return {"id": image_id, "reviewed": state.reviewed, "edited": state.edited}

    def annotate_auto_flags(self, image_id: str, labels: list[Label]) -> list[Label]:
        auto = self.load_auto_labels(image_id)
        return annotate_auto(labels, auto)

    # -- statistics -------------------------------------------------------
    def progress(self) -> dict[str, Any]:
        total = len(self.images)
        reviewed = sum(1 for img in self.images
                       if self.review_state.is_reviewed(img.id))
        edited = sum(1 for img in self.images if self.review_state.is_edited(img.id))
        return {
            "total": total,
            "reviewed": reviewed,
            "unreviewed": total - reviewed,
            "edited": edited,
            "reviewed_pct": (reviewed / total * 100.0) if total else 0.0,
        }

    def class_counts(self, use_cache: bool = True) -> dict[str, int]:
        """Number of boxes per class id, from the current label files."""
        if use_cache and self._class_counts_cache is not None:
            return dict(self._class_counts_cache)
        counts: dict[str, int] = {str(cid): 0 for cid in self.taxonomy.ids}
        for entry in self.images:
            labels, _ = read_labels(self.label_path(entry.id), self.taxonomy)
            for label in labels:
                key = str(label.cls)
                counts[key] = counts.get(key, 0) + 1
        self._class_counts_cache = counts
        return dict(counts)

    def priority_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entry in self.images:
            key = str(entry.priority)
            counts[key] = counts.get(key, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: int(kv[0])))

    def auto_class_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {str(cid): 0 for cid in self.taxonomy.ids}
        for entry in self.images:
            for label in self.load_auto_labels(entry.id):
                key = str(label.cls)
                counts[key] = counts.get(key, 0) + 1
        return counts

    def stats(self) -> dict[str, Any]:
        progress = self.progress()
        return {
            "batch": self.batch,
            "root": str(self.root),
            "total": progress["total"],
            "reviewed": progress["reviewed"],
            "unreviewed": progress["unreviewed"],
            "edited": progress["edited"],
            "reviewed_pct": round(progress["reviewed_pct"], 2),
            "split": self.split_counts,
            "priority": self.priority_counts(),
            "class_counts": self.class_counts(),
            "auto_class_counts": self.auto_class_counts(),
            "taxonomy_size": len(self.taxonomy),
        }

    def manifest_counts(self) -> dict[str, Any]:
        """Compact counts dict embedded in ``sync_manifest.json``."""
        progress = self.progress()
        return {
            "total": progress["total"],
            "reviewed": progress["reviewed"],
            "unreviewed": progress["unreviewed"],
            "edited": progress["edited"],
            "reviewed_pct": round(progress["reviewed_pct"], 2),
            "split": self.split_counts,
            "class_counts": self.class_counts(),
        }

    # -- filtering --------------------------------------------------------
    def has_auto_two_wheeler(self, image_id: str) -> bool:
        if not self._two_wheeler_ids:
            return False
        boxes = self.auto_meta.get(image_id)
        if boxes is not None:
            return any(box.cls in self._two_wheeler_ids for box in boxes)
        # No metadata: fall back to the pre-label file.
        return any(label.cls in self._two_wheeler_ids
                   for label in self.load_auto_labels(image_id))

    def matches(self, entry: ImageEntry, kind: str = FILTER_ALL,
                query: str = "") -> bool:
        if kind == FILTER_UNREVIEWED and self.review_state.is_reviewed(entry.id):
            return False
        if kind == FILTER_REVIEWED and not self.review_state.is_reviewed(entry.id):
            return False
        if kind == FILTER_EDITED and not self.review_state.is_edited(entry.id):
            return False
        if kind == FILTER_HIGH_PRIORITY and entry.priority < PRIORITY_HIGH:
            return False
        if kind == FILTER_HAS_AUTO and not self.has_auto(entry.id):
            return False
        if kind == FILTER_AUTO_TWOWHEEL and not self.has_auto_two_wheeler(entry.id):
            return False
        if query:
            needle = query.strip().lower()
            if needle:
                haystack = " ".join((entry.id, entry.route, str(entry.priority),
                                     str(entry.hour), entry.split)).lower()
                if needle not in haystack:
                    return False
        return True

    def filter_images(self, kind: str = FILTER_ALL, query: str = "") -> list[ImageEntry]:
        return [entry for entry in self.images if self.matches(entry, kind, query)]


def _scan_images(root: Path) -> list[ImageEntry]:
    """Fallback: derive the image list from the ``images/`` directory."""
    image_dir = root / "images"
    if not image_dir.is_dir():
        return []
    entries: list[ImageEntry] = []
    for index, path in enumerate(sorted(image_dir.iterdir())):
        if path.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        entries.append(ImageEntry(id=path.stem, file=f"images/{path.name}",
                                  index=index))
    return entries


# ---------------------------------------------------------------------------
# Dataset directory resolution
# ---------------------------------------------------------------------------
def resolve_dataset_dir(explicit: str | None = None,
                        app_dir: os.PathLike | str | None = None,
                        default: str | None = None) -> Path | None:
    """Figure out which batch directory to open.

    Order: ``--dataset`` -> ``$YOLO_DATASET`` -> ``dataset_dir.txt`` next to
    the app -> *default* -> ``None``.
    """
    if explicit:
        # An explicit path wins even when it does not exist: the caller turns
        # it into a precise error instead of silently opening another batch.
        return Path(explicit).expanduser()
    candidates: list[Path] = []
    env_value = os.environ.get("YOLO_DATASET")
    if env_value:
        candidates.append(Path(env_value).expanduser())
    base = Path(app_dir) if app_dir is not None else Path(__file__).resolve().parent
    pointer = base / "dataset_dir.txt"
    if pointer.is_file():
        try:
            text = pointer.read_text(encoding="utf-8").strip()
        except OSError:
            text = ""
        if text and not text.startswith("#"):
            candidates.append(Path(text).expanduser())
    if default:
        candidates.append(Path(default).expanduser())
    for candidate in candidates:
        if candidate.is_dir() and (candidate / "dataset.json").is_file():
            return candidate.resolve()
    return None
