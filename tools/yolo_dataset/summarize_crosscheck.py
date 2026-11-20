#!/usr/bin/env python3
"""Summarize crosscheck_records.jsonl into CSV + markdown review lists."""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path


def main(argv=None) -> int:
  ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
  ap.add_argument("records")
  ap.add_argument("--out-dir", required=True)
  ap.add_argument("--top", type=int, default=300)
  args = ap.parse_args(argv)
  records = [json.loads(line) for line in Path(args.records).read_text(encoding="utf-8").splitlines() if line.strip()]
  out = Path(args.out_dir)
  out.mkdir(parents=True, exist_ok=True)
  rows = []
  counts = Counter()
  by_image = []
  for rec in records:
    severity = 0.0
    for item in rec.get("missing", []):
      counts[f"missing:{item['cls']}"] += 1
      severity += item["conf"] * 2
      rows.append([rec["dataset"], rec["id"], "missing", "", item["cls"], f"{item['conf']:.3f}",
                   *[f"{v:.6f}" for v in item["box"]]])
    for item in rec.get("class_mismatch", []):
      counts[f"mismatch:{item['label_cls']}->{item['pred_cls']}"] += 1
      severity += item["conf"] * 3
      rows.append([rec["dataset"], rec["id"], "mismatch", item["label_cls"], item["pred_cls"],
                   f"{item['conf']:.3f}", *[f"{v:.6f}" for v in item["box"]]])
    for item in rec.get("model_missed", []):
      counts[f"model_missed:{item['cls']}"] += 1
      rows.append([rec["dataset"], rec["id"], "model_missed", item["cls"], "", "",
                   *[f"{v:.6f}" for v in item["box"]]])
    by_image.append((severity, rec["dataset"], rec["id"], len(rec.get("missing", [])),
                     len(rec.get("class_mismatch", [])), len(rec.get("model_missed", []))))
  with (out / "crosscheck_review.csv").open("w", newline="", encoding="utf-8-sig") as handle:
    writer = csv.writer(handle)
    writer.writerow(["dataset", "id", "type", "label_cls", "pred_cls", "conf", "x1", "y1", "x2", "y2"])
    writer.writerows(rows)
  by_image.sort(reverse=True)
  top = by_image[: args.top]
  lines = ["# YOLO11x 交叉核对摘要", "", "| 指标 | 数量 |", "|---|---|"]
  lines += [f"| {k} | {v} |" for k, v in counts.most_common()]
  lines += ["", f"## 最需要看的 {len(top)} 张图", "", "| 严重度 | 批次 | id | missing | mismatch | model_missed |", "|---|---|---|---|---|---|"]
  lines += [f"| {s:.1f} | {d} | {i} | {a} | {b} | {c} |" for s, d, i, a, b, c in top]
  (out / "crosscheck_summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
  print(json.dumps({"records": len(records), "images": len(by_image), "rows": len(rows)}, ensure_ascii=False))
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
