#!/usr/bin/env python3
"""Zero-shot CLIP scan of traffic-light crops (flags / signs / real lights)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np
import torch
from PIL import Image

PROMPTS = [
  "a photo of a traffic light",
  "a close-up photo of a traffic light with red green and yellow lamps",
  "a photo of a Chinese national flag",
  "a red flag with yellow stars",
  "a photo of a glowing neon shop sign",
  "a photo of a billboard or advertising sign",
  "a photo of a road sign or warning sign",
  "a photo of a street light",
  "a photo of a car tail light",
  "a photo of a red lantern",
]


def load_rows(path):
  rows = json.loads(Path(path).read_text(encoding="utf-8"))
  return rows


def main(argv=None):
  ap = argparse.ArgumentParser()
  ap.add_argument("rows", help="rows.json from probe_traffic_lights.py")
  ap.add_argument("--images", required=True)
  ap.add_argument("--out", default="clip_scores.json")
  ap.add_argument("--limit", type=int, default=200)
  ap.add_argument("--batch", type=int, default=32)
  args = ap.parse_args(argv)
  rows = load_rows(args.rows)[: args.limit]
  from transformers import CLIPModel, CLIPProcessor
  model = CLIPModel.from_pretrained("openai/clip-vit-base-patch32")
  proc = CLIPProcessor.from_pretrained("openai/clip-vit-base-patch32")
  model.eval()
  device = "cuda" if torch.cuda.is_available() else "cpu"
  model.to(device)
  text = proc(text=PROMPTS, return_tensors="pt", padding=True)
  with torch.no_grad():
    text_feat = model.get_text_features(**{k: v.to(device) for k, v in text.items()})
    text_feat = text_feat / text_feat.norm(dim=-1, keepdim=True)
  out = []
  for start in range(0, len(rows), args.batch):
    batch = rows[start:start + args.batch]
    crops = []
    for r in batch:
      img = cv2.imread(str(Path(args.images) / f"{r['id']}.jpg"))
      x1, y1, x2, y2 = r["box"]
      crop = img[y1:y2, x1:x2]
      crops.append(Image.fromarray(cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)).resize((224, 224)))
    inputs = proc(images=crops, return_tensors="pt")
    with torch.no_grad():
      img_feat = model.get_image_features(**{k: v.to(device) for k, v in inputs.items()})
      img_feat = img_feat / img_feat.norm(dim=-1, keepdim=True)
      logits = (100.0 * img_feat @ text_feat.T).softmax(dim=-1).cpu().numpy()
    for r, probs in zip(batch, logits, strict=True):
      idx = int(np.argmax(probs))
      out.append({"id": r["id"], "box": r["box"], "top": PROMPTS[idx],
                  "prob": float(probs[idx]), "probs": {p: float(v) for p, v in zip(PROMPTS, probs, strict=True)}})
    print(f"{min(start + args.batch, len(rows))}/{len(rows)}", flush=True)
  Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
  print("saved", args.out)


if __name__ == "__main__":
  raise SystemExit(main())
