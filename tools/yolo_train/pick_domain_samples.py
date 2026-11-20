#!/usr/bin/env python3
"""Pick a small, diverse set of the car's own traffic-light crops for labelling.

The S2TLD-trained classifier scores 99.3% on its own validation split but missed
two of six real crops pulled off this car. That gap is domain shift: S2TLD is
portrait phone photography, while the car sees ~39x59 px blobs from a 1344x760
dashcam with its own compression, exposure and lens. Closing it needs labels from
this camera, and labelling thousands of crops by hand is not worth anyone's time -
a few hundred well-chosen ones is.

The selection avoids the obvious trap of taking the first N boxes. Consecutive
frames are nearly identical and would waste most of the budget on one junction at
one moment, so crops are bucketed by what actually varies in appearance (size,
brightness, colour cast, horizontal position) and then picked round-robin with a
feature-distance guard that refuses near-duplicates.

Crops are written at native resolution so a human can actually see them; the
manifest records their source box so `apply_review.py` can rebuild them at the
classifier's 48x72 input without resampling twice.

Runs on the Linux box, where the frozen dataset and the current model live.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import sys
from pathlib import Path

import numpy as np

TRAFFIC_LIGHT_CLASS = 14      # road_users_detailed_v3
FEATURE_DECIMALS = 4
# A crop whose feature vector is closer than this (in normalised L2) to one
# already picked is treated as a duplicate.
DUPLICATE_DISTANCE = 0.045


def parse_args(argv=None):
  p = argparse.ArgumentParser(description="Pick diverse traffic-light crops for labelling")
  p.add_argument("--dataset", action="append", default=None,
                 help="dataset root with images/ labels/ (repeatable); "
                      "defaults to batch_001 and batch_002_night")
  p.add_argument("--model", default="openpilot/sunnypilot/yolod/models/traffic_light_state.onnx",
                 help="current classifier, used to pre-label so review is mostly confirming")
  p.add_argument("--out", required=True)
  p.add_argument("--count", type=int, default=600)
  p.add_argument("--min-conf", type=float, default=0.40,
                 help="drop low-confidence auto boxes. 61%% of the raw boxes sit below 0.30 and "
                      "are mostly signs, tail lights and reflections; 0.40 keeps ~1.2k candidates")
  p.add_argument("--min-side", type=int, default=10)
  p.add_argument("--max-aspect", type=float, default=3.0)
  p.add_argument("--seed", type=int, default=0)
  return p.parse_args(argv)


def parse_label_line(line: str):
  parts = line.split()
  if len(parts) < 5:
    return None
  try:
    cls = int(float(parts[0]))
    cx, cy, bw, bh = (float(v) for v in parts[1:5])
  except ValueError:
    return None
  return cls, cx, cy, bw, bh


def match_confidence(auto_entries, cx, cy, cls):
  """Confidence of the auto detection that best matches this label box.

  Matching by list position would be wrong: the label files have been hand-edited,
  so their order no longer has to follow the detector's output. Nearest centre
  among the same class is stable under reordering and insertion.
  """
  best, best_d = 1.0, None
  for entry in auto_entries:
    if entry.get("cls") != cls:
      continue
    xyxy = entry.get("xyxy") or []
    if len(xyxy) != 4:
      continue
    ex, ey = (xyxy[0] + xyxy[2]) / 2, (xyxy[1] + xyxy[3]) / 2
    d = (ex - cx) ** 2 + (ey - cy) ** 2
    if best_d is None or d < best_d:
      best, best_d = float(entry.get("conf", 1.0)), d
  return best


def collect_candidates(roots, min_conf):
  """Return every usable traffic-light box as a candidate dict."""
  root_conf = {}
  for root in roots:
    auto_meta = root / "auto_meta.json"
    if auto_meta.is_file():
      try:
        data = json.loads(auto_meta.read_text(encoding="utf-8"))
        root_conf[str(root)] = {k: v for k, v in data.items()}
      except (OSError, ValueError):
        root_conf[str(root)] = {}
    else:
      root_conf[str(root)] = {}

  candidates = []
  for root in roots:
    labels = sorted((root / "labels").glob("*.txt"))
    for label_path in labels:
      stem = label_path.stem
      image_path = None
      for ext in (".jpg", ".png", ".jpeg"):
        candidate = root / "images" / f"{stem}{ext}"
        if candidate.is_file():
          image_path = candidate
          break
      if image_path is None:
        continue

      # auto_meta keeps the detector confidence; the label file itself does not.
      auto_entries = root_conf[str(root)].get(stem, [])

      for line in label_path.read_text(encoding="utf-8").splitlines():
        parsed = parse_label_line(line)
        if parsed is None:
          continue
        cls, cx, cy, bw, bh = parsed
        if cls != TRAFFIC_LIGHT_CLASS:
          continue
        conf = match_confidence(auto_entries, cx, cy, cls)
        if conf < min_conf:
          continue
        candidates.append({"image": image_path, "cx": cx, "cy": cy, "bw": bw, "bh": bh,
                           "conf": conf, "root": root})
  return candidates


def crop_features(crop_bgr, w_px, h_px, cx, cy):
  """Compact description of how a lamp appears - the axes the domain shift lives on."""
  import cv2
  small = cv2.resize(crop_bgr, (32, 32), interpolation=cv2.INTER_AREA).astype(np.float32)
  hsv = cv2.cvtColor(small.astype(np.uint8), cv2.COLOR_BGR2HSV).astype(np.float32)
  b, g, r = small[:, :, 0].mean(), small[:, :, 1].mean(), small[:, :, 2].mean()
  v = hsv[:, :, 2]
  # The lamp itself is the brightest part of the crop; its hue is what the
  # classifier keys on, so it must be represented in the diversity features.
  thr = np.percentile(v, 90)
  hot = hsv[v >= thr]
  return {
    "log_h": round(float(np.log(max(h_px, 1.0))), FEATURE_DECIMALS),
    "aspect": round(float(w_px / max(h_px, 1.0)), FEATURE_DECIMALS),
    "brightness": round(float(v.mean()), 1),
    "sat": round(float(hsv[:, :, 1].mean()), 1),
    # Hue is circular; store its cosine and sine so distance stays meaningful.
    "hue_cos": round(float(np.cos(np.deg2rad(hot[:, 0].mean()))), FEATURE_DECIMALS),
    "hue_sin": round(float(np.sin(np.deg2rad(hot[:, 0].mean()))), FEATURE_DECIMALS),
    "rgb": [round(float(r), 1), round(float(g), 1), round(float(b), 1)],
    "x": round(float(cx), FEATURE_DECIMALS),
  }


def to_vector(feat):
  return np.array([
    feat["log_h"] / 4.0,
    min(feat["aspect"], 3.0) / 3.0,
    feat["brightness"] / 255.0,
    feat["sat"] / 255.0,
    feat["hue_cos"], feat["hue_sin"],
    feat["x"],
  ], dtype=np.float32)


def pick_diverse(items, count):
  """Round-robin over size/brightness/position buckets, rejecting near-duplicates."""
  if not items:
    return []
  for it in items:
    f = it["feat"]
    it["bucket"] = (
      "dark" if f["brightness"] < 70 else ("mid" if f["brightness"] < 150 else "bright"),
      min(3, int(np.searchsorted([22, 40, 70], np.exp(f["log_h"])))),
      min(2, int(f["x"] * 3)),
    )

  buckets = {}
  for it in items:
    buckets.setdefault(it["bucket"], []).append(it)
  for group in buckets.values():
    group.sort(key=lambda i: -i["conf"])

  chosen = []
  taken = []
  order = sorted(buckets, key=lambda k: -len(buckets[k]))
  while len(chosen) < count and any(buckets[k] for k in order):
    for key in order:
      group = buckets[key]
      while group:
        candidate = group.pop(0)
        vec = to_vector(candidate["feat"])
        if taken and min(float(np.linalg.norm(vec - t)) for t in taken) < DUPLICATE_DISTANCE:
          continue
        taken.append(vec)
        chosen.append(candidate)
        break
      if len(chosen) >= count:
        break
  return chosen


def build_review_html(samples, labels, path):
  """One self-contained page: no server, no dependencies, works offline.

  Three UI decisions come from how labelling 600 crops actually goes:

  * **Labelling never moves anything.** The first version re-appended every card
    on each keystroke and hid the card just labelled, so the grid reflowed and the
    viewport jumped - the person had to scroll back down after every single mark.
    A labelled card is now dimmed in place, and the DOM is reordered only when the
    filter itself changes.
  * **The hotkey targets the crop under the cursor**, not the first unlabelled
    card; keyboard marking used to be useless once your hand was on the mouse.
  * **The filter can select an assigned class or the model's guess.** Reviewing
    "everything the model called red" is how the red bias shows up, and reviewing
    "everything I called red" is how mistakes get caught. Per-class counts sit in
    the header so the balance is visible before exporting.
  """
  DISPLAY = {"red": "红", "yellow": "黄", "green": "绿", "off": "灭",
             "wait_on": "待", "reject": "不是灯", "skip": "看不清", "?": "?"}
  tiles = []
  for sample in samples:
    name = sample["name"]
    encoded = base64.b64encode(sample["crop_bytes"]).decode("ascii")
    guess = labels[name]["pred"]
    prob = labels[name]["prob"]
    buttons = "".join(
      f'<button data-v="{value}" title="{hint}">{DISPLAY[value]}</button>'
      for value, hint in (("red", "红灯 (r)"), ("yellow", "黄灯 (y)"), ("green", "绿灯 (g)"),
                          ("off", "没点亮 (o)"), ("wait_on", "等待 (w)"),
                          ("reject", "不是信号灯：路牌/路灯/尾灯/反光 (x)"),
                          ("skip", "是灯但看不清，直接丢弃 (s)")))
    buttons += '<button data-v="" title="清除标记，恢复成未标注 (Backspace)">清</button>'
    tiles.append(
      f'<div class="c" data-name="{name}" data-guess="{guess}">'
      f'<img src="data:image/jpeg;base64,{encoded}" loading="lazy">'
      f'<div class="g">猜 {DISPLAY.get(guess, guess)} {prob:.2f}</div>'
      f'<div class="b">{buttons}</div></div>')

  option_groups = [
    ("状态", [("todo", "未标注"), ("all", "全部"), ("low", "置信度低的优先")]),
    ("按已标注结果", [(f"label:{c}", f"已标 {DISPLAY[c]}") for c in
                 ("red", "yellow", "green", "off", "wait_on", "reject", "skip")]),
    ("按模型猜测", [(f"guess:{c}", f"模型猜 {DISPLAY[c]}") for c in
                ("red", "yellow", "green", "off", "wait_on", "?")]),
  ]
  options_html = ""
  for group_name, entries in option_groups:
    options_html += f'<optgroup label="{group_name}">'
    for value, text in entries:
      options_html += f'<option value="{value}">{text}</option>'
    options_html += "</optgroup>"

  html = r"""<!doctype html>
<meta charset="utf-8">
<title>信号灯裁剪标注</title>
<style>
 body{background:#141414;color:#ddd;font:13px system-ui,sans-serif;margin:0;padding:12px}
 #bar{position:sticky;top:0;background:#1e1e1e;padding:8px 10px;border-bottom:1px solid #333;
      z-index:5}
 #row1{display:flex;gap:12px;align-items:center;flex-wrap:wrap}
 #tally{margin-top:5px;font-size:12px;color:#9a9a9a}
 #tally span{margin-right:10px;white-space:nowrap}
 #legend{margin-top:5px;font-size:12px;color:#8a8a8a;line-height:1.6}
 #legend b{color:#ccc}
 #grid{display:flex;flex-wrap:wrap;gap:10px;margin-top:12px}
 .c{background:#1b1b1b;border:2px solid #333;border-radius:6px;padding:5px;width:146px}
 .c.done{opacity:.42}
 .c.hov{border-color:#4a9eff}
 .c.flash{border-color:#3f3}
 .c.clr{border-color:#f80}
 .b button[data-v=""]{background:#2d2d2d;color:#999}
 .c img{width:134px;height:134px;object-fit:contain;background:#000;display:block;margin:0 auto}
 .g{text-align:center;color:#888;font-size:11px;margin:3px 0}
 .b{display:flex;flex-wrap:wrap;gap:3px;justify-content:center}
 .b button{font-size:11px;padding:2px 5px;border:0;border-radius:3px;background:#333;color:#ccc;cursor:pointer}
 .b button:hover{background:#666}
 .b button[data-v=red]{background:#5a1d1d}
 .b button[data-v=yellow]{background:#5c5116}
 .b button[data-v=green]{background:#1a4a26}
 .b button[data-v=reject]{background:#4a1a3a}
 .b button[data-v=skip]{background:#242424;color:#777}
 select{background:#262626;color:#ddd;border:1px solid #444;padding:4px;border-radius:4px;max-width:230px}
 #exp{background:#265a2c;color:#e8e8e8;border:0;padding:6px 12px;border-radius:4px;cursor:pointer}
 #imp,#rst,#ref{background:#333;color:#ddd;border:1px solid #4a4a4a;padding:5px 10px;
                border-radius:4px;cursor:pointer}
 #rst{background:#3a2020;border-color:#5a2a2a}
 #imp:hover,#rst:hover,#ref:hover{background:#4a4a4a}
</style>
<div id="bar">
 <div id="row1">
  <b>信号灯裁剪标注</b>
  <span id="stat"></span>
  <label>筛选 <select id="f">__OPTIONS__</select></label>
  <button id="ref">重新筛选</button>
  <button id="exp">导出 labels.json</button>
  <button id="imp">导入 labels.json</button>
  <button id="rst">清空重来</button>
  <input type="file" id="file" accept=".json,application/json" style="display:none">
 </div>
 <div id="tally"></div>
 <div id="legend">
  <b>标注方法</b>：鼠标悬停在图上（蓝框高亮），按 <b>r红 y黄 g绿 o灭 w待</b> 直接标记，
  标完鼠标移到下一张继续。&nbsp;<b>x</b>=不是信号灯，<b>s</b>=看不清。
  &nbsp;<b>Backspace</b>=清除这张、恢复未标注，<b>Ctrl+Z</b>=撤销上一步。
  &nbsp;空格/PageDn 翻页。&nbsp;<b>标完的卡片留在原位只变暗，页面不会跳</b>
  （想清掉已标的点「重新筛选」）。
  <br><b>都不是</b>时选哪个：<b style="color:#d98cd9">不是灯</b> = 确定它不是信号灯
  （路牌、路灯、尾灯、反光、纯色块）→ 会写成第 6 类 <code>not_light</code> 进训练集；
  <b style="color:#888">看不清</b> = 是灯但判断不了状态 → 直接丢弃，不进训练集。
 </div>
</div>
<div id="grid">__TILES__</div>
<script>
 const KEY="tl_review_v4";
 const MAP={r:"red",y:"yellow",g:"green",o:"off",w:"wait_on",x:"reject",s:"skip"};
 const SHOW={red:"红",yellow:"黄",green:"绿",off:"灭",wait_on:"待",reject:"不是灯",skip:"看不清","?":"?"};
 const ORDER=["red","yellow","green","off","wait_on","reject","skip"];
 let state=JSON.parse(localStorage.getItem(KEY)||"{}");
 const cards=[...document.querySelectorAll(".c")];
 const grid=document.getElementById("grid");
 const stat=document.getElementById("stat");
 const tally=document.getElementById("tally");
 const sel=document.getElementById("f");
 let hovered=null;

 function matches(card, f){
   const parts=f.split(":");
   const mode=parts[0], arg=parts[1];
   const v=state[card.dataset.name];
   if(mode==="todo") return !v;
   if(mode==="label") return v===arg;
   if(mode==="guess") return card.dataset.guess===arg;
   return true;
 }

 // 只更新样式与计数：不碰 DOM 顺序，所以标注时页面不跳。
 function refresh(){
   const byLabel={}, byGuess={};
   let done=0;
   for(const c of cards){
     const v=state[c.dataset.name];
     c.classList.toggle("done", !!v);
     if(v){ done++; byLabel[v]=(byLabel[v]||0)+1; }
     const g=c.dataset.guess; byGuess[g]=(byGuess[g]||0)+1;
   }
   stat.textContent=done+" / "+cards.length+" 已标注";
   let pieces=[];
   for(const k of ORDER) pieces.push('<span>'+SHOW[k]+' '+(byLabel[k]||0)+'</span>');
   tally.innerHTML="<b style='color:#bbb'>已标</b> "+pieces.join("")
     +"<span style='color:#666'>|</span>"
     +"<b style='color:#bbb'>未标</b> <span>"+(cards.length-done)+"</span>"
     +"<span style='color:#666'>|</span><b style='color:#bbb'>模型猜</b> "
     +ORDER.filter(k=>byGuess[k]).map(k=>'<span>'+SHOW[k]+' '+(byGuess[k]||0)+'</span>').join("");
   for(const opt of sel.options){
     const mode=opt.value.split(":")[0];
     if(mode!=="label" && mode!=="guess") continue;
     const base=opt.dataset.base||opt.textContent.replace(/ \(\d+\)$/,"");
     opt.dataset.base=base;
     const n=(mode==="label"?byLabel:byGuess)[opt.value.split(":")[1]]||0;
     opt.textContent=base+" ("+n+")";
   }
 }

 // 换筛选/重新筛选时才重排 DOM。
 function applyFilter(){
   const f=sel.value;
   const order=cards.map((c,i)=>[c,i]);
   if(f==="low"){
     order.sort((a,b)=>parseFloat(a[0].dataset.guess.split(" ").pop())
                       -parseFloat(b[0].dataset.guess.split(" ").pop()));
   }else{
     order.sort((a,b)=>a[1]-b[1]);
   }
   for(const pair of order){
     const c=pair[0];
     grid.appendChild(c);
     c.style.display=matches(c,f)?"":"none";
   }
   refresh();
 }

 // 单步历史：手滑打错时 Ctrl+Z 能退回去，不用再翻回去找那张图。
 const history=[];
 function remember(name){ history.push([name, state[name]]); if(history.length>300) history.shift(); }

 function onTarget(){
   if(hovered && hovered.offsetParent) return hovered;
   return cards.find(c=>c.offsetParent) || null;
 }

 function bump(card, cls){
   card.classList.add(cls);
   setTimeout(()=>card.classList.remove(cls), 200);
 }

 // 恢复成「未标注」：删掉键而不是写空串，导出时该条目才会是 null。
 function clearLabel(card){
   if(!(card.dataset.name in state)){ bump(card,"clr"); return; }
   remember(card.dataset.name);
   delete state[card.dataset.name];
   localStorage.setItem(KEY, JSON.stringify(state));
   card.classList.remove("done");
   card.style.display=matches(card, sel.value)?"":"none";
   bump(card,"clr");
   refresh();
 }

 function undo(){
   const last=history.pop(); if(!last) return;
   const name=last[0], prev=last[1];
   if(prev===undefined) delete state[name]; else state[name]=prev;
   localStorage.setItem(KEY, JSON.stringify(state));
   const card=cards.find(c=>c.dataset.name===name);
   if(card){
     card.classList.toggle("done", !!state[name]);
     card.style.display=matches(card, sel.value)?"":"none";
     bump(card,"clr");
   }
   refresh();
 }

 function setLabel(card, value){
   remember(card.dataset.name);
   state[card.dataset.name]=value;
   localStorage.setItem(KEY, JSON.stringify(state));
   bump(card,"flash");
   refresh();
   // 只有「已标 X」视图会移走改判后的卡片 - 那里它确实不再属于当前视图。
   // 「未标注」「全部」「模型猜」视图一律不动：卡片只变暗，布局纹丝不动，
   // 这样鼠标一路往下按就行，不用回头找位置。想清掉已标的点「重新筛选」。
   if(sel.value.indexOf("label:")===0 && !matches(card, sel.value))
     setTimeout(()=>{ if(!matches(card, sel.value)) card.style.display="none"; }, 200);
 }

 grid.addEventListener("mouseover", e=>{
   const c=e.target.closest(".c");
   if(!c || c===hovered) return;
   if(hovered) hovered.classList.remove("hov");
   hovered=c; c.classList.add("hov");
 });

 grid.addEventListener("click", e=>{
   const b=e.target.closest("button"); if(!b) return;
   const v=b.dataset.v;
   if(v) setLabel(b.closest(".c"), v);
   else clearLabel(b.closest(".c"));
 });

 addEventListener("keydown", e=>{
   if(e.target.tagName==="SELECT" || e.target.tagName==="INPUT") return;
   const k=e.key.toLowerCase();
   if(k===" " || k==="pagedown"){ e.preventDefault(); scrollBy(0, innerHeight*0.9); return; }
   if(k==="pageup"){ e.preventDefault(); scrollBy(0, -innerHeight*0.9); return; }
   if(k==="home"){ e.preventDefault(); scrollTo(0,0); return; }
   if(k==="end"){ e.preventDefault(); scrollTo(0, document.body.scrollHeight); return; }
   if((e.ctrlKey||e.metaKey) && k==="z"){ e.preventDefault(); undo(); return; }
   // 清除只作用于鼠标底下那张：退回「第一张」会清掉你根本没看的东西。
   if(k==="backspace" || k==="delete" || k==="0"){
     e.preventDefault();
     if(hovered && hovered.offsetParent) clearLabel(hovered);
     return;
   }
   const m=MAP[k]; if(!m) return;
   e.preventDefault();
   let target=(hovered && hovered.offsetParent) ? hovered : null;
   if(!target) target=cards.find(c=>!state[c.dataset.name] && c.offsetParent);
   if(target) setLabel(target, m);
 });

 sel.addEventListener("change", applyFilter);
 document.getElementById("ref").addEventListener("click", applyFilter);

 document.getElementById("rst").addEventListener("click",()=>{
   const n=Object.keys(state).length;
   if(!confirm("清空本地标注进度？当前已标注 "+n+" / "+cards.length+" 个。「已导出」的 labels.json 不受影响，之后可以导入恢复。")) return;
   state={}; localStorage.removeItem(KEY); refresh(); applyFilter();
 });

 document.getElementById("imp").addEventListener("click",()=>document.getElementById("file").click());
 document.getElementById("file").addEventListener("change",ev=>{
   const f=ev.target.files[0]; if(!f) return;
   const reader=new FileReader();
   reader.onload=()=>{
     let data; try{ data=JSON.parse(reader.result); }
     catch(err){ alert("不是合法的 JSON："+err.message); return; }
     if(!data || typeof data!=="object"){ alert("格式不对，应该是 {文件名: 类别}"); return; }
     let n=0;
     for(const c of cards){
       const v=data[c.dataset.name];
       if(v){ state[c.dataset.name]=String(v).toLowerCase(); n++; }
     }
     const extra=Object.keys(data).filter(k=>!cards.some(c=>c.dataset.name===k)).length;
     localStorage.setItem(KEY,JSON.stringify(state)); refresh(); applyFilter();
     alert("已导入 "+n+" 条标注"+(extra?"；"+extra+" 条文件名不匹配，已忽略":""));
   };
   reader.readAsText(f);
   ev.target.value="";
 });

 document.getElementById("exp").addEventListener("click",()=>{
   const out={};
   for(const c of cards){ out[c.dataset.name]=state[c.dataset.name]||null; }
   const blob=new Blob([JSON.stringify(out,null,1)],{type:"application/json"});
   const a=document.createElement("a");
   a.href=URL.createObjectURL(blob); a.download="labels.json"; a.click();
 });

 applyFilter();
</script>
""".replace("__TILES__", "".join(tiles)).replace("__OPTIONS__", options_html)
  path.write_text(html, encoding="utf-8")


def main(argv=None) -> int:
  args = parse_args(argv)
  import cv2

  roots = [Path(p) for p in (args.dataset or
                             ["/data/yolo_dataset/batch_001", "/data/yolo_dataset/batch_002_night"])]
  roots = [r for r in roots if r.is_dir()]
  if not roots:
    raise SystemExit("no dataset roots found")

  candidates = collect_candidates(roots, args.min_conf)
  print(f"  candidate boxes (conf>={args.min_conf}): {len(candidates)}")

  # One crop per source image keeps the selection from spending its budget on
  # several lamps of the same junction in the same frame.
  by_image = {}
  for cand in candidates:
    by_image.setdefault(str(cand["image"]), []).append(cand)
  print(f"  distinct images: {len(by_image)}")

  from openpilot.sunnypilot.yolod.traffic_light_classifier import TrafficLightStateClassifier
  classifier = TrafficLightStateClassifier(args.model)
  if not classifier.available:
    print(f"  classifier unavailable ({classifier.load_error}); pre-labels will be empty")
  else:
    print(f"  pre-labelling with {Path(args.model).name}")

  items = []
  for image_key, group in by_image.items():
    best = max(group, key=lambda c: c["conf"])
    image = cv2.imread(str(best["image"]))
    if image is None:
      continue
    h, w = image.shape[:2]
    x1 = int(round((best["cx"] - best["bw"] / 2) * w))
    x2 = int(round((best["cx"] + best["bw"] / 2) * w))
    y1 = int(round((best["cy"] - best["bh"] / 2) * h))
    y2 = int(round((best["cy"] + best["bh"] / 2) * h))
    x1, x2 = max(0, x1), min(w, x2)
    y1, y2 = max(0, y1), min(h, y2)
    w_px, h_px = x2 - x1, y2 - y1
    if w_px < args.min_side or h_px < args.min_side:
      continue
    if w_px > args.max_aspect * h_px or h_px > args.max_aspect * w_px:
      continue
    crop = image[y1:y2, x1:x2]
    if crop.size == 0:
      continue
    items.append({
      "image": best["image"], "box": [x1, y1, x2, y2], "conf": best["conf"],
      "w_px": w_px, "h_px": h_px,
      "feat": crop_features(crop, w_px, h_px, best["cx"], best["cy"]),
      "crop": crop,
    })
  print(f"  usable crops: {len(items)}")

  chosen = pick_diverse(items, args.count)
  print(f"  selected: {len(chosen)}")

  out = Path(args.out)
  (out / "samples").mkdir(parents=True, exist_ok=True)
  samples, labels, manifest = [], {}, {}
  for index, item in enumerate(chosen):
    name = f"{index:05d}.jpg"
    ok, encoded = cv2.imencode(".jpg", item["crop"], [cv2.IMWRITE_JPEG_QUALITY, 92])
    if not ok:
      continue
    (out / "samples" / name).write_bytes(encoded.tobytes())
    prediction = classifier.classify(item["crop"]) if classifier.available else None
    labels[name] = {"pred": prediction.label if prediction else "?",
                    "prob": round(prediction.prob, 4) if prediction else 0.0}
    manifest[name] = {
      "source": str(item["image"]),
      "box": item["box"],
      "native": [item["w_px"], item["h_px"]],
      "detector_conf": round(item["conf"], 4),
      "features": item["feat"],
    }
    samples.append({"name": name, "crop_bytes": encoded.tobytes()})

  (out / "seed_labels.json").write_text(json.dumps(labels, indent=1) + "\n", encoding="utf-8")
  (out / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
  build_review_html(samples, labels, out / "review.html")

  from collections import Counter
  tally = Counter(v["pred"] for v in labels.values())
  low = sum(1 for v in labels.values() if v["prob"] < 0.7)
  print("\n  pre-label distribution:", dict(tally))
  print(f"  low-confidence (<0.70), worth looking at first: {low}")
  sizes = np.array([m["native"] for m in manifest.values()])
  print(f"  crop size: W median {np.median(sizes[:,0]):.1f}  H median {np.median(sizes[:,1]):.1f}")
  print(f"\n  open: {out/'review.html'}")
  print(f"  then: python tools/yolo_train/apply_review.py {out} --labels <exported labels.json>")
  return 0


if __name__ == "__main__":
  sys.exit(main())
