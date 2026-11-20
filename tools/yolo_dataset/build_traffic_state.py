#!/usr/bin/env python3
"""Build a 21-class dataset with red/yellow/green traffic-light states.

- images are hardlinked from the unique 17-class dataset;
- non-reviewed traffic_light boxes are classified as flag / red / yellow / green
  (CLIP for flag-vs-light, HSV for the lamp colour);
- reviewed/edited images keep their traffic_light boxes as the generic class so
  human boxes are never silently rewritten.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
from collections import Counter
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from openpilot.sunnypilot.yolod.traffic_light_state import classify_traffic_light

OLD = ['person', 'two_wheeler', 'tricycle', 'car', 'van', 'truck', 'bus', 'trailer',
       'special_vehicle', 'stroller', 'wheelchair', 'animal', 'cone', 'barrier',
       'traffic_light', 'traffic_sign', 'other_obstacle']
NEW = OLD[:14] + ['traffic_light', 'traffic_light_red', 'traffic_light_yellow',
                  'traffic_light_green', 'traffic_sign', 'other_obstacle', 'flag']
REMAP = {i: i for i in range(14)}
REMAP[14] = 14
REMAP[15] = 18
REMAP[16] = 19
TL = 14
TL_RED, TL_YELLOW, TL_GREEN, FLAG = 15, 16, 17, 20
ZH = ['行人', '两轮车', '三轮车', '小汽车', '面包车', '货车', '客车', '挂车/房车',
      '特种车辆', '婴儿车', '轮椅', '动物', '锥桶', '护栏/水马', '交通信号灯(未定色)',
      '交通信号灯-红', '交通信号灯-黄', '交通信号灯-绿', '交通标志', '其他障碍物', '旗帜/非信号灯']


def link_or_copy(src: Path, dst: Path) -> None:
  if dst.exists():
    return
  try:
    os.link(src, dst)
  except OSError:
    shutil.copy2(src, dst)


def main(argv=None) -> int:
  ap = argparse.ArgumentParser()
  ap.add_argument('--src', required=True, help='all17_unique dataset')
  ap.add_argument('--reviews', required=True, help='batch dir containing review_state.json')
  ap.add_argument('--out', required=True)
  ap.add_argument('--limit', type=int, default=0)
  ap.add_argument('--clip', action='store_true', default=True)
  args = ap.parse_args(argv)
  src = Path(args.src); out = Path(args.out)
  (out / 'images').mkdir(parents=True, exist_ok=True)
  (out / 'labels').mkdir(parents=True, exist_ok=True)
  manifest = json.loads((src / 'dataset.json').read_text(encoding='utf-8'))
  images = manifest['images'][:args.limit] if args.limit else manifest['images']
  # review_state is per source batch; the unique IDs are b1_ / n_.
  reviews = {}
  for bdir in Path(args.reviews).glob('batch_*'):
    p = bdir / 'review_state.json'
    if p.exists():
      reviews.update(json.loads(p.read_text(encoding='utf-8')))

  import clip
  import torch
  device = 'cuda' if torch.cuda.is_available() else 'cpu'
  model, prep = clip.load('ViT-B/32', device=device, jit=False)
  prompts = ['a traffic light', 'a Chinese flag', 'a street sign', 'a car tail light', 'a building']
  with torch.no_grad():
    tf = model.encode_text(clip.tokenize(prompts).to(device)); tf /= tf.norm(dim=-1, keepdim=True)

  pending = []
  label_cache = {}
  for im in images:
    iid = im['id']; prefix, old = iid.split('_', 1)
    src_lab = src / 'labels' / f'{iid}.txt'
    lines = [l.split() for l in src_lab.read_text(encoding='utf-8').splitlines() if l.strip()]
    edited = bool(reviews.get(old, {}).get('edited') or reviews.get(old, {}).get('reviewed'))
    new_lines = []
    for parts in lines:
      cid = int(parts[0]); vals = parts[1:5]
      if cid == TL and not edited:
        pending.append((iid, old, im['file'], vals, parts[5:], cid))
        continue
      new_cid = TL if cid == TL else REMAP.get(cid, cid)
      new_lines.append(' '.join([str(new_cid), *vals, *parts[5:]]))
    label_cache[iid] = new_lines
  print('pending un-reviewed traffic lights', len(pending), 'reviewed images keep generic', flush=True)

  # Batch CLIP for flag-vs-light decisions.
  flags = []
  for start in range(0, len(pending), 64):
    batch = pending[start:start+64]; crops = []
    for iid, old, file, vals, rest, cid in batch:
      img = cv2.imread(str(src / file)); h, w = img.shape[:2]
      cx, cy, bw, bh = map(float, vals)
      x1, y1 = max(0, int((cx-bw/2)*w)), max(0, int((cy-bh/2)*h))
      x2, y2 = min(w, int((cx+bw/2)*w)), min(h, int((cy+bh/2)*h))
      crop = img[y1:y2, x1:x2]
      crops.append(crop)
    valid = [c for c in crops if c.size and c.shape[0] >= 8 and c.shape[1] >= 6]
    if valid:
      tensors = torch.stack([prep(Image.fromarray(cv2.cvtColor(c, cv2.COLOR_BGR2RGB))) for c in valid]).to(device)
      with torch.no_grad():
        f = model.encode_image(tensors); f /= f.norm(dim=-1, keepdim=True)
        probs = (f @ tf.T).softmax(-1).detach().cpu().numpy()
    else:
      probs = np.zeros((0, len(prompts)))
    pi = 0
    for crop in crops:
      if not crop.size or crop.shape[0] < 8 or crop.shape[1] < 6:
        flags.append(False); continue
      p = probs[pi]; pi += 1
      flags.append(bool(p[1] > p[0] or p[2] > p[0] or p[3] > p[0] or p[4] > p[0]))
    if start % 512 == 0: print('clip', start, '/', len(pending), flush=True)

  counts = Counter()
  for (iid, old, file, vals, rest, oldcid), is_flag in zip(pending, flags, strict=True):
    img = cv2.imread(str(src / file)); h, w = img.shape[:2]
    cx, cy, bw, bh = map(float, vals)
    x1, y1 = max(0, int((cx-bw/2)*w)), max(0, int((cy-bh/2)*h))
    x2, y2 = min(w, int((cx+bw/2)*w)), min(h, int((cy+bh/2)*h))
    crop = img[y1:y2, x1:x2]
    if is_flag:
      new_cid = FLAG
    else:
      state = classify_traffic_light(crop)
      new_cid = {'红': TL_RED, '黄': TL_YELLOW, '绿': TL_GREEN}.get(state, TL)
    counts[new_cid] += 1
    label_cache[iid].append(' '.join([str(new_cid), *vals, *rest]))

  # Copy image links + write labels, manifest, splits.
  class_defs = [{'id': i, 'name': n, 'zh': ZH[i]} for i, n in enumerate(NEW)]
  out_images = []
  for im in images:
    iid = im['id']; link_or_copy(src / im['file'], out / 'images' / f'{iid}.jpg')
    (out / 'labels' / f'{iid}.txt').write_text('\n'.join(label_cache[iid]) + '\n', encoding='utf-8')
    item = dict(im); item['file'] = f'images/{iid}.jpg'; out_images.append(item)
  (out / 'train.txt').write_text((src / 'train.txt').read_text(encoding='utf-8'), encoding='utf-8')
  (out / 'val.txt').write_text((src / 'val.txt').read_text(encoding='utf-8'), encoding='utf-8')
  payload = {'schema': 1, 'batch': out.name, 'taxonomy': 'traffic_state_v4',
             'taxonomy_version': 4, 'classes': class_defs, 'images': out_images,
             'counts': {}, 'split': manifest.get('split', {})}
  (out / 'dataset.json').write_text(json.dumps(payload, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
  (out / 'taxonomy.json').write_text(json.dumps({'version': 4, 'name': 'traffic_state_v4',
                                                 'classes': class_defs}, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
  (out / 'data.yaml').write_text('path: ' + out.as_posix() + '\ntrain: train.txt\nval: val.txt\nnc: ' + str(len(NEW)) +
                                 '\nnames:\n' + ''.join(f'  - {n}\n' for n in NEW), encoding='utf-8')
  print(json.dumps({'out': str(out), 'images': len(images), 'classes': len(NEW),
                    'state_counts': {NEW[k]: v for k, v in counts.items()},
                    'flag': counts[FLAG], 'generic': counts[TL]}, ensure_ascii=False))
  return 0


if __name__ == '__main__':
  raise SystemExit(main())
