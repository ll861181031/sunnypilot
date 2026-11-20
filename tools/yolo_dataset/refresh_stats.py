#!/usr/bin/env python3
"""Recompute dataset counts / stats after manual or propagated label edits."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main(argv=None) -> int:
  ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
  ap.add_argument("dataset")
  args = ap.parse_args(argv)
  root = Path(args.dataset)
  payload = json.loads((root / "dataset.json").read_text(encoding="utf-8"))
  names = [c["name"] for c in payload["classes"]]
  daylight = {im["id"]: bool(im.get("daylight")) for im in payload["images"]}
  by_class = {name: {"boxes": 0, "images": 0} for name in names}
  day_boxes = {k: dict.fromkeys(names, 0) for k in ("day", "night")}
  for path in sorted((root / "labels").glob("*.txt")):
    seen = set()
    is_day = daylight.get(path.stem, True)
    for line in path.read_text(encoding="utf-8").splitlines():
      parts = line.split()
      if len(parts) < 5:
        continue
      name = names[int(parts[0])]
      by_class[name]["boxes"] += 1
      day_boxes["day" if is_day else "night"][name] += 1
      seen.add(name)
    for name in seen:
      by_class[name]["images"] += 1
  payload["counts"] = {name: by_class[name]["boxes"] for name in names}
  stats_path = root / "stats.json"
  stats = json.loads(stats_path.read_text(encoding="utf-8")) if stats_path.is_file() else {}
  stats["by_class"] = by_class
  stats["two_wheeler_total"] = sum(by_class[n]["boxes"] for n in ("two_wheeler", "tricycle") if n in by_class)
  stats["person_total"] = by_class.get("person", {}).get("boxes", 0)
  if "day_night" in stats:
    stats["day_night"]["day"]["boxes"] = day_boxes["day"]
    stats["day_night"]["night"]["boxes"] = day_boxes["night"]
  if "priority" in stats:
    stats["priority"]["with_two_wheeler"] = sum(
        1 for im in payload["images"]
        if any(im.get("auto_counts", {}).get(n, 0) for n in ("two_wheeler", "tricycle")))
  (root / "dataset.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
  (root / "stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
  print(json.dumps({"dataset": str(root), "two_wheeler": by_class["two_wheeler"]["boxes"],
                    "tricycle": by_class["tricycle"]["boxes"], "person": by_class["person"]["boxes"]},
                   ensure_ascii=False))
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
