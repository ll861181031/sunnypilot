#!/usr/bin/env python3
"""Stage 3: select frames and write the annotated-dataset skeleton for the labeler.

Selection
---------
Every extracted frame is scored from its auto detections::

    priority = 5 * two_wheelers + 2 * persons + 1 * vehicles

(the counts are the COCO-model detections after the rider-deduplication of
``autolabel.py``). Distinct *routes* are never split between train and val, so a
model cannot memorise a road from one split and be scored on the other.

The batch is filled in two buckets, day (08:00-17:59 from the segment mtime) and
night, ``--day-frac`` of the target each. Inside a bucket:

* up to ``--neg-frac`` of the slots go to randomly drawn negatives (frames without
  any person/vehicle detection - crowded scenes are already over-represented by
  the priority ordering, so a few empty frames keep the false-positive rate down);
* the rest is filled by priority, highest first;
* at most ``--per-route-cap`` frames per route, so one busy road cannot dominate.

When a bucket cannot supply its share (this corpus has far fewer night segments),
the shortfall is taken from the other bucket and reported in ``stats.json``.

Outputs (``--out/<batch>/``): images, labels (= a copy of auto_labels), the labeler
contract files (``auto_labels``, ``auto_meta.json``, ``dataset.json``,
``taxonomy.json``, ``review_state.json``, ``train.txt``, ``val.txt``, ``data.yaml``),
``stats.json`` and a Chinese ``README.md``; ``--package`` additionally writes a
``.tar.gz`` next to the batch directory for scp.
"""

from __future__ import annotations

import argparse
import os
import random
import shutil
import sys
import tarfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common

CONF_BUCKETS = [(0.18, 0.25), (0.25, 0.35), (0.35, 0.5), (0.5, 0.7), (0.7, 1.01)]
LETTERBOX_NOTE = "frames whose source aspect ratio differs are padded, not stretched; see source_width/source_height"


# ------------------------------------------------------------------ candidates

def load_candidates(work: str, taxonomy: common.Taxonomy) -> tuple[list[dict], dict]:
  """Join _meta and _auto into flat per-frame candidate records."""
  meta_root = os.path.join(work, "_meta")
  auto_root = os.path.join(work, "_auto")
  candidates: list[dict] = []
  skipped = {"routes_without_meta": 0, "routes_without_auto": 0,
             "frames_without_auto": 0, "frames_without_image": 0, "routes": 0}
  if not os.path.isdir(meta_root):
    raise FileNotFoundError(f"no _meta under {work}; run extract.py first")
  for name in sorted(os.listdir(meta_root)):
    if not name.endswith(".json"):
      continue
    meta = common.read_json(os.path.join(meta_root, name), {})
    frames = meta.get("frames") or []
    if not frames:
      skipped["routes_without_meta"] += 1
      continue
    route = meta.get("route") or os.path.splitext(name)[0]
    auto_file = os.path.join(auto_root, f"{route}.json")
    auto = common.read_json(auto_file, {})
    if not auto or not auto.get("frames"):
      skipped["routes_without_auto"] += 1
      continue
    skipped["routes"] += 1
    frame_dir = os.path.join(work, "_frames", route)
    for entry in frames:
      stem = os.path.splitext(entry["file"])[0]
      if stem not in auto["frames"]:
        skipped["frames_without_auto"] += 1
        continue
      if not os.path.isfile(os.path.join(frame_dir, entry["file"])):
        skipped["frames_without_image"] += 1
        continue
      detections = auto["frames"][stem]
      counts = common.count_by_group(detections)
      candidates.append({
        "route": route,
        "file": entry["file"],
        "source_width": meta.get("source_width"),
        "source_height": meta.get("source_height"),
        "t": float(entry.get("t", 0.0)),
        "hour": meta.get("hour"),
        "daylight": bool(meta.get("daylight", True)),
        "brightness": float(entry.get("brightness", 0.0)),
        "detections": detections,
        "counts": counts,
        "priority": common.priority_of(counts),
        "negative": len(detections) == 0,
      })
  return candidates, skipped


# ------------------------------------------------------------------- selection

def select_frames(candidates: list[dict], target: int, day_frac: float, neg_frac: float,
                  per_route_cap: int, seed: int) -> tuple[list[dict], dict]:
  """Pick ``target`` candidates; returns (selected, plan report)."""
  rng = random.Random(seed)
  buckets: dict[bool, list[dict]] = {True: [], False: []}   # True = daylight
  for cand in candidates:
    buckets[cand["daylight"]].append(cand)

  available = {bucket: len(rows) for bucket, rows in buckets.items()}
  want_day = int(round(target * day_frac))
  quota = {True: min(want_day, available[True]),
           False: min(target - want_day, available[False])}
  # Whatever one bucket cannot supply is offered to the bucket that still has
  # spare frames, so ``target`` is met whenever the corpus allows it.
  shortfall = target - quota[True] - quota[False]
  for bucket in (True, False):
    if shortfall <= 0:
      break
    spare = available[bucket] - quota[bucket]
    add = min(max(0, spare), shortfall)
    quota[bucket] += add
    shortfall -= add

  selected: list[dict] = []
  per_route: dict[str, int] = {}
  filled = {True: 0, False: 0}
  for bucket in (True, False):
    need = quota[bucket]
    rows = buckets[bucket]
    if need <= 0:
      continue
    picked: list[dict] = []
    # 1) negatives: strict first (no detections at all), topped up with frames
    #    that only contain traffic lights/signs so no road user is present.
    strict = [c for c in rows if c["negative"]]
    background = [c for c in rows if not c["negative"] and c["priority"] == 0]
    rng.shuffle(strict)
    rng.shuffle(background)
    want_neg = min(int(round(need * neg_frac)), len(strict) + len(background))
    for cand in strict + background:
      if len(picked) >= want_neg:
        break
      if per_route.get(cand["route"], 0) >= per_route_cap:
        continue
      picked.append(cand)
      per_route[cand["route"]] = per_route.get(cand["route"], 0) + 1
    # 2) the rest by priority, deterministic tie-break on (route, t).
    positives = sorted((c for c in rows if not c["negative"] and c["priority"] > 0),
                       key=lambda c: (-c["priority"], c["route"], c["t"]))
    for cand in positives:
      if len(picked) >= need:
        break
      if per_route.get(cand["route"], 0) >= per_route_cap:
        continue
      picked.append(cand)
      per_route[cand["route"]] = per_route.get(cand["route"], 0) + 1
    # 3) still short (negatives beyond the quota, background-only frames): use them
    #    too rather than returning a smaller batch than the corpus allows.
    if len(picked) < need:
      chosen = {id(c) for c in picked}
      leftovers = sorted((c for c in rows if id(c) not in chosen),
                         key=lambda c: (-c["priority"], c["route"], c["t"]))
      for cand in leftovers:
        if len(picked) >= need:
          break
        if per_route.get(cand["route"], 0) >= per_route_cap:
          continue
        picked.append(cand)
        per_route[cand["route"]] = per_route.get(cand["route"], 0) + 1
    filled[bucket] = len(picked)
    selected.extend(picked)

  plan = {
    "requested": target,
    "quota": {"day": quota[True], "night": quota[False]},
    "available": {"day": available[True], "night": available[False]},
    "filled": {"day": filled[True], "night": filled[False]},
    "selected": len(selected),
    "negatives": {
      "requested": int(round(target * neg_frac)),
      "selected": sum(1 for c in selected if c["negative"]),
      "background_only_selected": sum(1 for c in selected
                                      if not c["negative"] and c["priority"] == 0),
      "strict_available": sum(1 for c in candidates if c["negative"]),
      "background_available": sum(1 for c in candidates
                                  if not c["negative"] and c["priority"] == 0),
    },
    "per_route_cap": per_route_cap,
    "routes_used": len(per_route),
  }
  return selected, plan


def load_split_reference(paths: list[str]) -> dict[str, str]:
  """route -> split, read from the dataset.json of batches that already exist.

  Two batches that share routes must agree on those routes' split, otherwise
  concatenating them leaks a road from train into val.
  """
  reference: dict[str, str] = {}
  for path in paths:
    dataset = common.read_json(path, {})
    for image in dataset.get("images") or []:
      split = image.get("split")
      if split in ("train", "val"):
        reference.setdefault(image["route"], split)
  return reference


def split_routes(routes: list[str], val_frac: float, seed: int,
                 reference: dict[str, str] | None = None) -> tuple[set[str], set[str]]:
  """Route-level train/val split: whole routes, never frames, so no leakage.

  With ``reference`` (route -> split from an earlier batch) the shared routes keep
  their original assignment and only the new routes are assigned here, so any set
  of batches built this way can be concatenated without cross-batch leakage.
  """
  unique = sorted(set(routes))
  if reference:
    known: dict[str, str] = {}
    for route in unique:
      if route in reference:
        known[route] = reference[route]
    fresh = [route for route in unique if route not in reference]
    if fresh:
      rng = random.Random(seed)
      rng.shuffle(fresh)
      n_val = max(1, int(round(len(fresh) * val_frac))) if len(fresh) > 1 else 0
      for position, route in enumerate(fresh):
        known[route] = "val" if position < n_val else "train"
    val = {route for route, split in known.items() if split == "val"}
    return set(unique) - val, val
  rng = random.Random(seed)
  rng.shuffle(unique)
  n_val = max(1, int(round(len(unique) * val_frac))) if len(unique) > 1 else 0
  val = set(unique[:n_val])
  return set(unique) - val, val


# -------------------------------------------------------------------- writing

def class_histogram(detections: list[dict], taxonomy: common.Taxonomy) -> dict[str, int]:
  hist: dict[str, int] = {}
  for det in detections:
    name = taxonomy.classes[int(det["cls"])]["name"]
    hist[name] = hist.get(name, 0) + 1
  return hist


def conf_histogram(values: list[float]) -> list[dict]:
  buckets = []
  for low, high in CONF_BUCKETS:
    buckets.append({"lo": low, "hi": high,
                    "boxes": sum(1 for v in values if low <= v < high)})
  return buckets


def write_dataset(args, taxonomy: common.Taxonomy, selected: list[dict], plan: dict,
                  skipped: dict, work: str) -> dict:
  out_dir = os.path.join(args.out, args.batch)
  if os.path.isdir(out_dir):
    if not args.overwrite:
      raise SystemExit(f"{out_dir} already exists; pass --overwrite to rebuild it from scratch")
    shutil.rmtree(out_dir)
  for sub in ("images", "labels", "auto_labels"):
    os.makedirs(os.path.join(out_dir, sub), exist_ok=True)

  selected.sort(key=lambda c: (c["route"], c["t"]))
  routes = [c["route"] for c in selected]
  reference = load_split_reference(args.split_from) if args.split_from else {}
  train_routes, val_routes = split_routes(routes, args.val_frac, args.seed, reference)

  images_meta: list[dict] = []
  auto_meta: dict[str, list[dict]] = {}
  review_state: dict[str, dict] = {}
  counts: dict[str, int] = {}
  images_by_class: dict[str, int] = {}
  day_night = {"day": {"images": 0, "boxes": {}}, "night": {"images": 0, "boxes": {}}}
  conf_values: list[float] = []
  route_stats: dict[str, dict] = {}
  train_lines: list[str] = []
  val_lines: list[str] = []

  for index, cand in enumerate(selected, start=1):
    sample_id = common.id_of(index)
    source = os.path.join(work, "_frames", cand["route"], cand["file"])
    target = os.path.join(out_dir, "images", f"{sample_id}.jpg")
    if args.keep_frames:
      shutil.copy2(source, target)
    else:
      os.replace(source, target)
    lines = [common.format_yolo_line(int(d["cls"]), d["xyxy"]) for d in cand["detections"]]
    common.write_labels(os.path.join(out_dir, "auto_labels", f"{sample_id}.txt"), lines)
    common.write_labels(os.path.join(out_dir, "labels", f"{sample_id}.txt"), lines)
    auto_meta[sample_id] = [{"cls": int(d["cls"]), "conf": float(d["conf"]),
                             "xyxy": [float(v) for v in d["xyxy"]]} for d in cand["detections"]]
    review_state[sample_id] = {"reviewed": False, "edited": False, "updated": 0}

    hist = class_histogram(cand["detections"], taxonomy)
    for name, n in hist.items():
      counts[name] = counts.get(name, 0) + n
      images_by_class[name] = images_by_class.get(name, 0) + 1
    bucket = "day" if cand["daylight"] else "night"
    day_night[bucket]["images"] += 1
    for name, n in hist.items():
      day_night[bucket]["boxes"][name] = day_night[bucket]["boxes"].get(name, 0) + n
    conf_values.extend(float(d["conf"]) for d in cand["detections"])
    split = "val" if cand["route"] in val_routes else "train"
    (val_lines if split == "val" else train_lines).append(f"images/{sample_id}.jpg")
    images_meta.append({"id": sample_id, "file": f"images/{sample_id}.jpg",
                        "width": args.width, "height": args.height,
                        "source_width": cand.get("source_width"),
                        "source_height": cand.get("source_height"),
                        "route": cand["route"], "t": round(cand["t"], 3),
                        "hour": cand["hour"], "daylight": bool(cand["daylight"]),
                        "brightness": round(cand["brightness"], 1),
                        "split": split, "priority": int(cand["priority"]),
                        "negative": bool(cand["negative"]),
                        "auto_counts": hist})
    entry = route_stats.setdefault(cand["route"], {"images": 0, "priority_sum": 0,
                                                   "daylight": bool(cand["daylight"]),
                                                   "hour": cand["hour"], "two_wheeler": 0})
    entry["images"] += 1
    entry["priority_sum"] += cand["priority"]
    entry["two_wheeler"] += cand["counts"]["two_wheeler"]

  created = common.utc_now()
  dataset = {
    "schema": 1,
    "batch": args.batch,
    "created_utc": created,
    "taxonomy": taxonomy.name,
    "taxonomy_version": taxonomy.version,
    "classes": taxonomy.classes,
    "geometry": {"width": args.width, "height": args.height,
                 "resize": "letterbox (aspect preserved, pad 114)",
                 "note": LETTERBOX_NOTE,
                 "sampled_fps": args.sample_fps},
    "images": images_meta,
    "counts": {c["name"]: counts.get(c["name"], 0) for c in taxonomy.classes},
    "split": {"train": len(train_lines), "val": len(val_lines),
              "train_routes": len(train_routes), "val_routes": len(val_routes),
              "val_frac": args.val_frac},
  }
  common.write_json(os.path.join(out_dir, "dataset.json"), dataset)
  common.write_json(os.path.join(out_dir, "auto_meta.json"), auto_meta)
  common.write_json(os.path.join(out_dir, "review_state.json"), review_state)
  shutil.copy2(args.taxonomy, os.path.join(out_dir, "taxonomy.json"))
  with open(os.path.join(out_dir, "train.txt"), "w", encoding="utf-8") as f:
    f.write("\n".join(train_lines) + "\n")
  with open(os.path.join(out_dir, "val.txt"), "w", encoding="utf-8") as f:
    f.write("\n".join(val_lines) + "\n")

  names = taxonomy.names
  yaml_lines = [f"path: {os.path.abspath(out_dir)}", "train: train.txt", "val: val.txt",
                f"nc: {len(names)}", "names:", *[f"  - {n}" for n in names]]
  with open(os.path.join(out_dir, "data.yaml"), "w", encoding="utf-8") as f:
    f.write("\n".join(yaml_lines) + "\n")

  stats = {
    "batch": args.batch,
    "created_utc": created,
    "total_images": len(images_meta),
    "split": dataset["split"],
    "by_class": {c["name"]: {"boxes": counts.get(c["name"], 0),
                             "images": images_by_class.get(c["name"], 0)}
                 for c in taxonomy.classes},
    "two_wheeler_total": sum(counts.get(taxonomy.classes[i]["name"], 0)
                             for i in common.TWO_WHEELER_IDS),
    "person_total": counts.get("person", 0),
    "day_night": day_night,
    "negatives": {"images": sum(1 for i in images_meta if i["negative"]),
                  "priority_zero": sum(1 for i in images_meta
                                       if not i["negative"] and i["priority"] == 0),
                  "requested_frac": args.neg_frac,
                  "requested_count": plan["negatives"]["requested"],
                  "strict_available": plan["negatives"]["strict_available"],
                  "background_available": plan["negatives"]["background_available"]},
    "priority": {
      "max": max((i["priority"] for i in images_meta), default=0),
      "mean": round(sum(i["priority"] for i in images_meta) / len(images_meta), 3)
      if images_meta else 0.0,
      "with_two_wheeler": sum(1 for i in images_meta if i["auto_counts"].get("two_wheeler", 0) > 0),
      "with_person": sum(1 for i in images_meta if i["auto_counts"].get("person", 0) > 0),
      "histogram": {str(bound): sum(1 for i in images_meta if i["priority"] >= bound)
                    for bound in (0, 1, 2, 5, 10, 20)},
    },
    "auto_conf": {
      "boxes": len(conf_values),
      "mean": round(sum(conf_values) / len(conf_values), 4) if conf_values else 0.0,
      "buckets": conf_histogram(conf_values),
      "below_0_25": sum(1 for v in conf_values if v < 0.25),
    },
    "source_geometry": {},
    "by_hour": {str(hour): sum(1 for i in images_meta if i["hour"] == hour)
                for hour in sorted({i["hour"] for i in images_meta if i["hour"] is not None})},
    "brightness": {
      "day_mean": round(sum(i["brightness"] for i in images_meta if i["daylight"]) /
                        max(1, sum(1 for i in images_meta if i["daylight"])), 1),
      "night_mean": round(sum(i["brightness"] for i in images_meta if not i["daylight"]) /
                          max(1, sum(1 for i in images_meta if not i["daylight"])), 1),
      "night_above_100": sum(1 for i in images_meta
                             if not i["daylight"] and i["brightness"] > 100),
    },
    "by_route": {
      "routes": len(route_stats),
      "per_route_min": min((v["images"] for v in route_stats.values()), default=0),
      "per_route_max": max((v["images"] for v in route_stats.values()), default=0),
      "per_route_mean": round(sum(v["images"] for v in route_stats.values()) /
                              max(1, len(route_stats)), 2),
      "with_two_wheeler": sum(1 for v in route_stats.values() if v["two_wheeler"] > 0),
    },
    "selection": plan,
    "skipped": skipped,
    "source": {"work": os.path.abspath(work), "data_root": args.data_root,
               "model": args.model, "conf": args.conf, "iou": args.iou},
    "elapsed_seconds": None,
  }
  return {"out_dir": out_dir, "stats": stats, "dataset": dataset}


README_TEMPLATE = """# {batch} — 道路目标检测数据集（自动预标 + 人工复核）

由 `tools/yolo_dataset/` 流水线生成：`extract.py` 抽帧 → `autolabel.py` 预标 →
`build.py` 选帧与组装。**自动预标不是最终标签**，`labels/` 初始等于 `auto_labels/`，
需要用标注器人工复核/改类后才算真值。

## 规模

| 项目 | 数值 |
|---|---|
| 图像总数 | {total_images} |
| train / val | {train} / {val}（按 **route** 划分，val 占 {val_frac:.0%} routes，无泄漏） |
| 图像尺寸 | {width}x{height}（letterbox，无拉伸；见下） |
| 类别数 | {num_classes}（见 `taxonomy.json`） |
| 两轮/三轮框总数 | {two_wheeler} |
| 行人框总数 | {person} |
| 负样本（无任何检出） | {negative} 张 |
| 白天 / 夜间 | {day} / {night} |
| 标注格式 | YOLO `cls cx cy w h`（归一化到本图 {width}x{height}） |

## 文件

```
images/<id>.jpg         图像，{width}x{height}
labels/<id>.txt         当前标签（初始 = 自动预标）；空文件 = 负样本（无目标）
auto_labels/<id>.txt    自动预标原样（只读参考）
auto_meta.json          {{"<id>": [{{"cls":int,"conf":float,"xyxy":[x1,y1,x2,y2]}}]}}  归一化
dataset.json            元数据：schema/batch/classes/images[]/counts/split
taxonomy.json           22 类定义（含中文名/颜色/coco 映射）
review_state.json       {{"<id>": {{"reviewed":bool,"edited":bool,"updated":unix}}}}  初始全 false
train.txt / val.txt     `images/<id>.jpg` 列表（相对本目录）
data.yaml               Ultralytics 配置
stats.json              逐类/昼夜/route/priority/置信度分布
```

## 选取规则（为什么是这些帧）

- 优先级 `priority = 5×两轮车 + 2×行人 + 1×机动车`（COCO 预标计数），越高越优先；
- 白天/夜间各占 {day_frac:.0%}（夜间段不足时由白天补，实际比例见 `stats.json`）；
- 负样本约 {neg_frac:.0%}（随机抽取的"画面无目标"帧，用于压低误检）；
- 每条 route 最多 {per_route_cap} 帧，避免单一路段刷屏；
- 抽帧频率 {sample_fps} Hz（每 {sample_interval:.0f} 秒 1 帧）。

## 几何说明（重要）

源数据有两种分辨率：{source_geometry}。为保持**几何一致且不丢视野**，
非 16:9 的源用 **letterbox**（等比缩放到高 760，左右补 114 灰边），
**不做横向拉伸**——拉伸会让所有物体横向变胖约 10%，与另一批帧产生域偏移。
训练时请直接使用原图，Ultralytics 会再做一次 letterbox，两边一致的灰边不会引入偏差。
每张图的原始分辨率记在 `dataset.json` 的 `source_width/source_height`。

## 标注约定（与 `taxonomy.json` 一致）

- **两轮/三轮的框必须包含骑行者**（不是分开标 person）。
- `person` 只标**不在车上**的行人。
- 自动预标用 COCO 模型，只能区分 `bicycle(1)` / `motorcycle(3)`；
  **`e_bike(2)` 与三轮车 4-7 类必须人工改类**——COCO 没有这些类别。
- 预标阈值 conf={conf}，**刻意偏低**以保召回：低分框大多是误检，请删除而不是保留。

## 复现

```bash
cd /data/sunnypilot
.venv/bin/python tools/yolo_dataset/extract.py --work <work> [--routes N]
.venv/bin/python tools/yolo_dataset/autolabel.py --work <work>
.venv/bin/python tools/yolo_dataset/build.py --work <work> --out /data/yolo_dataset --batch {batch} --target {total_images}
.venv/bin/python tools/yolo_dataset/validate.py --dataset /data/yolo_dataset/{batch}
```

标注完成后用 `merge_inbox.py` 从 `/home/lsr/yolo_label_inbox/{batch}/` 合并回传结果。
"""


def write_readme(out_dir: str, args, taxonomy: common.Taxonomy, stats: dict) -> None:
  dataset_split = stats["split"]
  text = README_TEMPLATE.format(
      batch=args.batch,
      total_images=stats["total_images"],
      train=dataset_split["train"], val=dataset_split["val"],
      val_frac=dataset_split["val_frac"],
      width=args.width, height=args.height, num_classes=taxonomy.num_classes,
      two_wheeler=stats["two_wheeler_total"], person=stats["person_total"],
      negative=stats["negatives"]["images"],
      day=stats["day_night"]["day"]["images"], night=stats["day_night"]["night"]["images"],
      day_frac=args.day_frac, neg_frac=args.neg_frac, per_route_cap=args.per_route_cap,
      sample_fps=args.sample_fps,
      sample_interval=(1.0 / args.sample_fps if args.sample_fps > 0 else 0),
      source_geometry="1344x760（早期）与 1928x1208（2026-10 起）",
      conf=args.conf)
  with open(os.path.join(out_dir, "README.md"), "w", encoding="utf-8") as f:
    f.write(text)


TOP_LEVEL_FILES = ("dataset.json", "auto_meta.json", "review_state.json", "taxonomy.json",
                    "train.txt", "val.txt", "data.yaml", "stats.json", "README.md")


def audit_output(out_dir: str, expected_images: int) -> list[str]:
  """Check the batch holds exactly the files this run wrote; returns problems.

  A rebuild that leaves files from the previous selection behind is the failure this
  guards against: the ids are reused, so leftovers silently attach an old label to a
  new frame.
  """
  problems: list[str] = []
  for sub in ("images", "labels", "auto_labels"):
    directory = os.path.join(out_dir, sub)
    names = os.listdir(directory) if os.path.isdir(directory) else []
    wrong = [n for n in names if not n.endswith((".jpg", ".txt"))]
    if wrong:
      problems.append(f"{sub}/ contains unexpected files: {wrong[:5]}")
    if len(names) != expected_images:
      problems.append(f"{sub}/ has {len(names)} files, expected {expected_images}")
  top = set(os.listdir(out_dir))
  # labels_backup/ is created by merge_inbox.py once the batch is being annotated.
  allowed_dirs = {"images", "labels", "auto_labels", "labels_backup"}
  extra = sorted(top - set(TOP_LEVEL_FILES) - allowed_dirs)
  if extra:
    problems.append(f"unexpected top-level entries: {extra[:5]}")
  missing = sorted(set(TOP_LEVEL_FILES) - top)
  if missing:
    problems.append(f"missing top-level files: {missing}")
  return problems


def package(out_dir: str, archive: str) -> dict:
  """tar.gz the batch directory (JPEGs barely compress, so level 1 keeps it quick)."""
  started = time.monotonic()
  batch_name = os.path.basename(out_dir.rstrip("/"))
  os.path.dirname(out_dir.rstrip("/")) or "."
  with tarfile.open(archive, "w:gz", compresslevel=1) as tar:
    tar.add(out_dir, arcname=batch_name)
  return {"archive": archive, "bytes": os.path.getsize(archive),
          "seconds": round(time.monotonic() - started, 1),
          "ratio": round(os.path.getsize(archive) / max(1, dir_size(out_dir)), 3)}


def dir_size(path: str) -> int:
  total = 0
  for root, _dirs, files in os.walk(path):
    for name in files:
      try:
        total += os.path.getsize(os.path.join(root, name))
      except OSError:
        pass
  return total


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
  parser.add_argument("--work", default=common.DEFAULT_WORK_ROOT)
  parser.add_argument("--out", default=common.DEFAULT_WORK_ROOT)
  parser.add_argument("--batch", default="batch_001")
  parser.add_argument("--target", type=int, default=12000)
  parser.add_argument("--pilot", type=int, default=0, help="shorthand for a small validation batch")
  parser.add_argument("--taxonomy", default=common.DEFAULT_TAXONOMY)
  parser.add_argument("--model", default=common.DEFAULT_MODEL)
  parser.add_argument("--data-root", default=common.DEFAULT_DATA_ROOT)
  parser.add_argument("--day-frac", type=float, default=0.5)
  parser.add_argument("--neg-frac", type=float, default=0.1)
  parser.add_argument("--per-route-cap", type=int, default=40)
  parser.add_argument("--val-frac", type=float, default=0.15)
  parser.add_argument("--conf", type=float, default=0.18)
  parser.add_argument("--iou", type=float, default=0.45)
  parser.add_argument("--sample-fps", type=float, default=0.5)
  parser.add_argument("--seed", type=int, default=1234)
  parser.add_argument("--split-from", action="append", default=[],
                      help="dataset.json to reuse the route->split assignment from")
  parser.add_argument("--keep-frames", action="store_true", help="copy instead of move")
  parser.add_argument("--overwrite", action="store_true")
  parser.add_argument("--plan-only", action="store_true", help="report the selection plan only")
  parser.add_argument("--package", action="store_true", help="also write <out>/<batch>.tar.gz")
  parser.add_argument("--workers", type=int, default=8, help="accepted for CLI symmetry")
  args = parser.parse_args(argv)
  if args.pilot > 0:
    args.target = args.pilot
    if args.batch == "batch_001":
      args.batch = f"pilot_{args.pilot}"
  args.width, args.height = common.TARGET_SIZE

  started = time.monotonic()
  taxonomy = common.load_taxonomy(args.taxonomy)
  candidates, skipped = load_candidates(args.work, taxonomy)
  if not candidates:
    print(f"no usable candidates under {args.work}", file=sys.stderr)
    return 1
  selected, plan = select_frames(candidates, args.target, args.day_frac, args.neg_frac,
                                args.per_route_cap, args.seed)
  day_count = sum(1 for c in selected if c["daylight"])
  night_count = sum(1 for c in selected if not c["daylight"])
  neg_count = sum(1 for c in selected if c["negative"])
  span = f"day={day_count}, night={night_count}, negatives={neg_count}"
  print(f"candidates={len(candidates)} selected={len(selected)} ({span})")
  print(f"plan={plan}")
  if args.plan_only:
    return 0

  written = write_dataset(args, taxonomy, selected, plan, skipped, args.work)
  stats = written["stats"]
  out_dir = written["out_dir"]
  for image in written["dataset"]["images"]:
    size = f"{image.get('source_width')}x{image.get('source_height')}"
    stats["source_geometry"][size] = stats["source_geometry"].get(size, 0) + 1
  common.write_json(os.path.join(out_dir, "stats.json"), stats)
  write_readme(out_dir, args, taxonomy, stats)
  split = f"train={stats['split']['train']} val={stats['split']['val']}"
  boxes = f"tw={stats['two_wheeler_total']} person={stats['person_total']}"
  megabytes = f"{dir_size(out_dir) / 1e6:.1f} MB"
  print(f"wrote {out_dir}: {stats['total_images']} images, {split}, {boxes}, {megabytes}")
  stats["elapsed_seconds"] = round(time.monotonic() - started, 1)
  common.write_json(os.path.join(out_dir, "stats.json"), stats)
  problems = audit_output(out_dir, len(selected))
  if problems:
    print("output audit FAILED: " + "; ".join(problems), file=sys.stderr)
    return 1
  if args.package:
    info = package(out_dir, os.path.join(args.out, f"{args.batch}.tar.gz"))
    stats["package"] = info
    stats["elapsed_seconds"] = round(time.monotonic() - started, 1)
    common.write_json(os.path.join(out_dir, "stats.json"), stats)
    size = f"{info['bytes'] / 1e6:.1f} MB, ratio {info['ratio']}"
    print(f"packaged {info['archive']} ({size}) in {info['seconds']}s")
  print(f"build done in {time.monotonic() - started:.1f}s")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
