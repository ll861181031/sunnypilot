#!/usr/bin/env python3
"""Migrate a dataset from taxonomy v1 (bicycle/e_bike/motorcycle) to v2.

Merges old classes 1/2/3 into new class 1 ``two_wheeler`` and shifts old
classes 4..21 down by two. Only the class id (first token) changes: every box
geometry and every manual edit is preserved. Rewrites labels, auto_labels,
auto_meta, dataset.json, stats.json, data.yaml and taxonomy.json, with a full
backup first.
"""
from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

OLD_TO_NEW = {0: 0, 1: 1, 2: 1, 3: 1, 4: 2, 5: 3, 6: 4, 7: 5, 8: 6, 9: 7,
              10: 8, 11: 9, 12: 10, 13: 11, 14: 12, 15: 13, 16: 14, 17: 15,
              18: 16, 19: 17, 20: 18, 21: 19}
NEW_THREE = {"tricycle_cargo", "tricycle_awning", "tricycle_passenger", "tricycle_other"}
TWO_NAMES = {"two_wheeler"}


def parse_labels(text: str) -> list[list[str]]:
  return [line.split() for line in text.splitlines() if line.strip()]


def remap_line(parts: list[str]) -> list[str]:
  old = int(parts[0])
  if old not in OLD_TO_NEW:
    raise SystemExit(f"unknown old class id {old}")
  return [str(OLD_TO_NEW[old]), *parts[1:]]


def write_labels(path: Path, rows: list[list[str]]) -> None:
  path.write_text("\n".join(" ".join(row) for row in rows) + ("\n" if rows else ""), encoding="utf-8")


def main(argv=None) -> int:
  ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
  ap.add_argument("dataset")
  ap.add_argument("--taxonomy", required=True, help="v2 taxonomy.json from tools/yolo_annotator")
  ap.add_argument("--dry-run", action="store_true")
  args = ap.parse_args(argv)
  root = Path(args.dataset)
  payload = json.loads((root / "dataset.json").read_text(encoding="utf-8"))
  if int(payload.get("taxonomy_version", 1)) >= 2:
    print(f"{root} already v2, skip")
    return 0
  taxonomy = json.loads(Path(args.taxonomy).read_text(encoding="utf-8"))
  names = [c["name"] for c in taxonomy["classes"]]
  stamp = time.strftime("%Y%m%d-%H%M%S")
  backup = root / f"backup_taxonomy_v1_{stamp}"
  if not args.dry_run:
    backup.mkdir(parents=True, exist_ok=True)
    for name in ("labels", "auto_labels"):
      shutil.copytree(root / name, backup / name)
    for name in ("auto_meta.json", "dataset.json", "stats.json", "data.yaml", "taxonomy.json"):
      shutil.copy2(root / name, backup / name)

  # labels / auto_labels: only the first token (class id) changes.
  by_class = {name: {"boxes": 0, "images": 0} for name in names}
  day_boxes = {k: dict.fromkeys(names, 0) for k in ("day", "night")}
  daylight = {image["id"]: bool(image.get("daylight")) for image in payload["images"]}
  for sub in ("labels", "auto_labels"):
    for path in sorted((root / sub).glob("*.txt")):
      rows = [remap_line(row) for row in parse_labels(path.read_text(encoding="utf-8"))]
      if not args.dry_run:
        write_labels(path, rows)
      if sub == "labels":
        seen = set()
        is_day = daylight.get(path.stem, True)
        for row in rows:
          name = names[int(row[0])]
          by_class[name]["boxes"] += 1
          seen.add(name)
          day_boxes["day" if is_day else "night"][name] += 1
        for name in seen:
          by_class[name]["images"] += 1

  # auto_meta: class ids only.
  auto_meta_path = root / "auto_meta.json"
  auto_meta = json.loads(auto_meta_path.read_text(encoding="utf-8"))
  for boxes in auto_meta.values():
    for item in boxes:
      item["cls"] = OLD_TO_NEW[int(item["cls"])]
  # dataset.json
  payload["taxonomy"] = taxonomy["name"]
  payload["taxonomy_version"] = 2
  payload["classes"] = [{"id": c["id"], "name": c["name"], "zh": c["zh"],
                         "desc": c["desc"], "color": c["color"]} for c in taxonomy["classes"]]
  payload["counts"] = {name: by_class[name]["boxes"] for name in names}
  for image in payload["images"]:
    old_counts = image.get("auto_counts") or {}
    merged: dict[str, int] = {}
    for old_name, value in old_counts.items():
      new_name = "two_wheeler" if old_name in ("bicycle", "e_bike", "motorcycle") else old_name
      # names of classes 4..21 shifted only in id, not name.
      merged[new_name] = merged.get(new_name, 0) + int(value)
    image["auto_counts"] = merged

  # stats.json
  stats_path = root / "stats.json"
  stats = json.loads(stats_path.read_text(encoding="utf-8")) if stats_path.is_file() else {}
  stats["by_class"] = by_class
  stats["two_wheeler_total"] = sum(by_class[n]["boxes"] for n in names
                                   if n == "two_wheeler" or n in NEW_THREE)
  stats["person_total"] = by_class["person"]["boxes"]
  if "day_night" in stats:
    stats["day_night"]["day"]["boxes"] = day_boxes["day"]
    stats["day_night"]["night"]["boxes"] = day_boxes["night"]
  if "priority" in stats:
    stats["priority"]["with_two_wheeler"] = sum(
        1 for image in payload["images"]
        if any(image.get("auto_counts", {}).get(n, 0) for n in (TWO_NAMES | NEW_THREE)))

  if not args.dry_run:
    (root / "taxonomy.json").write_text(json.dumps(taxonomy, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (root / "auto_meta.json").write_text(json.dumps(auto_meta, ensure_ascii=False), encoding="utf-8")
    (root / "dataset.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    (root / "stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    yaml_path = root / "data.yaml"
    if yaml_path.is_file():
      old_yaml = yaml_path.read_text(encoding="utf-8")
      prefix = old_yaml.split("nc:", 1)[0].rstrip() + "\n"
      lines = [f"nc: {len(names)}", "names:"]
      lines += [f"  - {name}" for name in names]
      yaml_path.write_text(prefix + "\n".join(lines) + "\n", encoding="utf-8")
    readme = root / "README.md"
    if readme.is_file():
      readme.write_text(readme.read_text(encoding="utf-8").replace("22 类", "20 类").replace("22 classes", "20 classes"),
                        encoding="utf-8")
  print(json.dumps({"dataset": str(root), "dry_run": args.dry_run,
                    "two_wheeler_boxes": by_class["two_wheeler"]["boxes"],
                    "two_wheeler_images": by_class["two_wheeler"]["images"],
                    "backup": str(backup) if not args.dry_run else None}, ensure_ascii=False))
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
