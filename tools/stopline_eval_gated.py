#!/usr/bin/env python3
"""Offline evaluation of the classical stop-line prototype behind a low-speed / no-lead gate.

This is a research tool, not a car process.  It replays a recorded ``fcamera.hevc``
next to the matching ``rlog.zst``, runs ``tools/stopline_proto.py`` at 10 Hz, and
reports which confirmed stop-line candidates would be allowed to matter for a
stop decision:

  * ``vEgo < 8.9 m/s`` (32 km/h), and
  * no lead vehicle: ``leadOne.present == False`` or ``leadOne.dRel >= 30 m``.

Confirmed candidates outside the gate are counted and kept as evidence, never as
triggers.  Nothing here touches the car: no control, no stack, no hardware.

Usage:

    .venv/bin/python tools/stopline_eval_gated.py \
        --out-dir artifacts/stopline_eval \
        000000f7--60502000ce--0:30:45 \
        00000106--78faf979c5--0:0:30

With no positional cases the DEFAULT_CASES list below is evaluated.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path

import cv2
import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def load_impl(name: str):
    """Return (detector module, human path). 'prod' is the shipped detector."""
    if name == "prod":
        from openpilot.sunnypilot.stopline import detector as mod
        return mod, "openpilot/sunnypilot/stopline/detector.py"
    from tools import stopline_proto as mod
    return mod, "tools/stopline_proto.py"


def annotate(img: np.ndarray, candidates: list, impl) -> np.ndarray:
    """Draw candidates without depending on a specific detector module."""
    out = img.copy()
    roi_x = getattr(impl, "_roi_x", None)
    for i, cand in enumerate(candidates):
        if roi_x is not None:
            x1, x2 = roi_x(cand.row)
        else:
            x1, x2 = 0, out.shape[1] - 1
        color = (0, 255, 0) if i == 0 else (0, 200, 255)
        cv2.rectangle(out, (x1, cand.top), (x2, cand.bottom), color, 2)
        cv2.putText(out, f"row={cand.row} cov={cand.coverage:.2f} d={cand.distance_m:.1f}m",
                    (max(0, x1), max(20, cand.top - 8)), cv2.FONT_HERSHEY_SIMPLEX,
                    0.55, color, 2, cv2.LINE_AA)
        bottom_y = getattr(impl, "ROI_BOTTOM_Y", None)
        bottom_x = getattr(impl, "ROI_BOTTOM_X", None)
        if bottom_y is not None and bottom_x is not None:
            cv2.rectangle(out, (bottom_x[0], bottom_y), (bottom_x[1], bottom_y), (255, 0, 255), 1)
    return out

MEDIA_ROOT = Path(os.path.expanduser("~/.comma/media/0/realdata"))
DEFAULT_OUT = REPO_ROOT / "artifacts" / "stopline_eval"

# (route, start_s, end_s, human note)
DEFAULT_CASES: list[tuple[str, float, float, str]] = [
    ("000000f7--60502000ce--0", 30.0, 45.0,
     "night city, low speed, no lead — expected real stop line"),
    ("00000106--78faf979c5--0", 0.0, 30.0,
     "day wet bridge, ~11–12 m/s — should be speed-gated"),
    ("00000112--89786bd74c--0", 32.0, 40.0,
     "high-speed approach, 12–22 m/s — should be speed-gated"),
    ("00000097--4f3f72a3b0--10", 30.0, 45.0,
     "night behind a bus, ~99% lead present — should be lead-gated"),
    ("000000e9--4979179d0e--1", 0.0, 30.0,
     "extra: parked / very low speed city"),
    ("000000e9--4979179d0e--2", 0.0, 30.0,
     "extra: low speed city, intermittent lead"),
    ("000000e9--4979179d0e--9", 45.0, 60.0,
     "extra: night city, ~4.3 m/s, no lead"),
    ("000000e9--4979179d0e--10", 0.0, 30.0,
     "extra: night city, ~5.2 m/s, no lead"),
    ("00000107--989dcb26d3--1", 0.0, 30.0,
     "extra: very slow city creep, ~2.7 m/s, no lead"),
]

# Manual review, done by looking at the contact sheets and full-resolution frames
# of each case (2026-11-14), keyed by detector implementation.  The script never
# infers this; it is an operator note stored next to the numbers so JSON and
# Markdown agree.  Details avoid frame counts where both implementations agree.
def _review(verdict: str, detail: str) -> dict[str, str]:
    return {"verdict": verdict, "detail": detail}


REVIEW: dict[tuple[str, str, float, float], dict[str, str]] = {}


def _add_review(impl: str, route: str, start: float, end: float,
                verdict: str, detail: str) -> None:
    REVIEW[(impl, route, start, end)] = _review(verdict, detail)


for _impl in ("prod", "proto"):
    _add_review(_impl, "000000f7--60502000ce--0", 30.0, 45.0, "true_positive",
                "箱体始终锁在斑马线前那根横贯本车道的粗白带上，距离随接近连续收敛到 10.6–10.9 m，"
                "vEgo 7.9–8.2 m/s，8 m/s 下 10 m 可停。真实停止线；箱体比白带略高约 10–20 px"
                "（行→米约差 0.5–1 m），距离偏大约 1 m。")
    _add_review(_impl, "00000106--78faf979c5--0", 0.0, 30.0, "gated_false_positive",
                "箱体落在桥洞出口湿滑路面的水光/路缘亮带上（部分帧还在桥洞内湿地面），"
                "不是停止线；速度门控按设计拦下。")
    _add_review(_impl, "00000112--89786bd74c--0", 32.0, 40.0, "gated_true_positive",
                "箱体锁在本车道停止线上（信号路口前的横贯白带），是真停止线；"
                "检测距离只有约 11–17 m，在 12–22 m/s 下物理上停不住，速度门控按设计拦下——"
                "也说明原型不能承担高速主制动。")
    _add_review(_impl, "00000097--4f3f72a3b0--10", 30.0, 45.0, "gated_false_positive",
                "箱体落在公交车左侧绿色隔离柱灯带（及其白边）上，不是停止线；"
                "前车 present、dRel 中位约 9 m，前车门控按设计拦下。")
    _add_review(_impl, "000000e9--4979179d0e--1", 0.0, 30.0, "no_detection",
                "停驶段（vEgo 恒 0），无原始候选，负样本对照。")
    _add_review(_impl, "00000107--989dcb26d3--1", 0.0, 30.0, "false_positive",
                "箱体落在白色车列/店招亮带（约 21–23 m，proto 另有 17–20 m 的白色护栏），"
                "不是停止线；低速蠕行门控拦不住这类静态亮带。")
    _add_review(_impl, "000000e9--4979179d0e--10", 0.0, 30.0, "false_positive",
                "箱体落在白色车列/路缘（约 12.3–12.6 m），不是停止线。")

_add_review("prod", "000000e9--4979179d0e--2", 0.0, 30.0, "no_detection",
            "生产模块本窗口无跟踪确认；proto 会在右侧店面灯带的横向亮带上误触发。")
_add_review("proto", "000000e9--4979179d0e--2", 0.0, 30.0, "false_positive",
            "箱体落在右侧店面灯带形成的横向亮带上（row≈490、约 22 m 几乎不动），不是本车道停止线。")
_add_review("prod", "000000e9--4979179d0e--9", 45.0, 60.0, "no_detection",
            "生产模块本窗口无跟踪确认；proto 会在斜交路口对侧停车带/路缘白线上误触发。")
_add_review("proto", "000000e9--4979179d0e--9", 45.0, 60.0, "false_positive",
            "6 帧触发、5 段（每段仅 1–2 帧）；箱体落在斜交路口对侧停车带/路缘的白色边线"
            "（与店招灯带混在一起），不是本车道停止线。")

IMPL_LABELS = {"prod": "生产模块（prod）", "proto": "原型（proto）"}

VERDICT_LABELS = {
    "true_positive": "真停止线（触发）",
    "gated_true_positive": "真停止线（被门控）",
    "false_positive": "误检（触发）",
    "gated_false_positive": "误检（被门控）",
    "no_detection": "无检测",
}

# Gate thresholds (must match the task description).
V_EGO_MAX = 8.9          # m/s
LEAD_MIN_DREL = 30.0     # m
DETECT_HZ = 10.0         # every 2nd frame of a 20 fps stream
FRAME_STEP = 2
MAX_STALENESS_S = 0.75   # rlog sample considered current for a video frame


def _r(value, ndigits: int = 3) -> float:
    return round(float(value), ndigits)


def _fmt(value, ndigits: int = 1, unit: str = "") -> str:
    if value is None:
        return "n/a"
    return f"{value:.{ndigits}f}{unit}"


class Signals:
    """vEgo and leadOne time series, relative to the first rlog event.

    The task aligns the video on its first frame and the log on its first
    ``logMonoTime``.  Both zeroes are close but not identical in general, so the
    caller is told the remaining alignment uncertainty (0.5–1 s) explicitly and
    may shift the log lookup with ``--time-offset``.
    """

    def __init__(self, rlog_path: Path) -> None:
        # Imported lazily so `--help` works without the cereal build being ready.
        from openpilot.tools.lib.logreader import LogReader

        cs_ns: list[int] = []
        cs_v: list[float] = []
        rad_ns: list[int] = []
        rad_present: list[bool] = []
        rad_drel: list[float] = []
        first_ns: int | None = None
        prev_ns: int | None = None
        cluster_ns: int | None = None

        for evt in LogReader(str(rlog_path)):
            mt = int(evt.logMonoTime)
            if first_ns is None:
                first_ns = mt
            # A segment rlog can start with a lone initData written seconds or
            # minutes before the live log (seen here: a 601 s metadata prefix).
            # Splitting on gaps > 2 s and using the last cluster keeps the gate
            # aligned with the driving part instead of the metadata prefix.
            if prev_ns is not None and mt - prev_ns > 2_000_000_000:
                cluster_ns = mt
            prev_ns = mt
            which = evt.which()
            if which == "carState":
                cs_ns.append(mt)
                cs_v.append(float(evt.carState.vEgo))
            elif which == "radarState":
                lead = evt.radarState.leadOne
                rad_ns.append(mt)
                rad_present.append(bool(lead.present))
                rad_drel.append(float(lead.dRel))

        if not cs_ns:
            raise RuntimeError(f"no carState in {rlog_path}")
        zero_ns = cluster_ns if cluster_ns is not None else first_ns
        assert zero_ns is not None
        self.zero_ns = zero_ns
        self.leading_gap_s = _r((zero_ns - first_ns) / 1e9, 2)

        cs = [(mt - zero_ns) / 1e9 for mt in cs_ns if mt >= zero_ns]
        cs_v_kept = [v for mt, v in zip(cs_ns, cs_v) if mt >= zero_ns]
        rad = [(mt - zero_ns) / 1e9 for mt in rad_ns if mt >= zero_ns]
        rad_kept = [(p, d) for mt, p, d in zip(rad_ns, rad_present, rad_drel) if mt >= zero_ns]
        if not cs:
            # Degenerate file: fall back to every sample.
            cs = [(mt - first_ns) / 1e9 for mt in cs_ns]
            cs_v_kept = list(cs_v)
            rad = [(mt - first_ns) / 1e9 for mt in rad_ns]
            rad_kept = list(zip(rad_present, rad_drel))
            self.zero_ns = first_ns
            self.leading_gap_s = 0.0

        self.cs_t = np.asarray(cs, dtype=np.float64)
        self.cs_v = np.asarray(cs_v_kept, dtype=np.float64)
        self.rad_t = np.asarray(rad, dtype=np.float64)
        self.rad_present = np.asarray([p for p, _ in rad_kept], dtype=bool)
        self.rad_drel = np.asarray([d for _, d in rad_kept], dtype=np.float64)
        self.span_s = _r((max(cs[-1], rad[-1] if rad else cs[-1]) - min(cs[0], rad[0] if rad else cs[0])), 2)

    def at(self, t: float) -> tuple[float | None, bool | None, float | None, float]:
        """Latest sample at or before t; returns (vEgo, lead_present, dRel, staleness)."""
        i = int(np.searchsorted(self.cs_t, t, side="right")) - 1
        v_ego = None
        v_age = float("inf")
        if i >= 0:
            v_ego = float(self.cs_v[i])
            v_age = float(t - self.cs_t[i])

        present: bool | None = None
        drel: float | None = None
        r_age = float("inf")
        if self.rad_t.size:
            j = int(np.searchsorted(self.rad_t, t, side="right")) - 1
            if j >= 0:
                r_age = float(t - self.rad_t[j])
                if r_age <= MAX_STALENESS_S:
                    present = bool(self.rad_present[j])
                    drel = float(self.rad_drel[j]) if present else None
        if v_age > MAX_STALENESS_S:
            v_ego = None
        return v_ego, present, drel, max(v_age, r_age)


def gate_reasons(v_ego: float | None, present: bool | None, drel: float | None,
                 v_max: float = V_EGO_MAX, lead_min: float = LEAD_MIN_DREL) -> list[str]:
    """Empty list means 'would trigger'; otherwise why it must not."""
    reasons: list[str] = []
    if v_ego is None or v_ego >= v_max:
        reasons.append("speed")
    if present is None:
        reasons.append("no_radar")
    elif present and drel is not None and drel < lead_min:
        reasons.append("lead")
    return reasons


def load_video_size(video: Path) -> tuple[int, int]:
    import av
    with av.open(str(video)) as cap:
        stream = cap.streams.video[0]
        return stream.codec_context.width, stream.codec_context.height


def _thumb(img: np.ndarray, width: int = 640) -> bytes:
    h, w = img.shape[:2]
    out = cv2.resize(img, (width, max(1, int(round(h * width / w)))), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 84])
    if not ok:
        raise RuntimeError("jpeg encode failed")
    return buf.tobytes()


def _tile(jpeg: bytes, label: str, width: int = 640, bar: int = 26) -> np.ndarray:
    img = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
    h, w = img.shape[:2]
    if w != width:
        img = cv2.resize(img, (width, int(round(h * width / w))), interpolation=cv2.INTER_AREA)
        h = img.shape[0]
    tile = np.full((h + bar, width, 3), 16, dtype=np.uint8)
    tile[bar:] = img
    cv2.putText(tile, label, (6, bar - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                (0, 255, 255) if "TRIGGER" in label else (0, 180, 255), 1, cv2.LINE_AA)
    return tile


def build_sheet(items: list[dict], title: str, out_path: Path, cols: int = 3,
                width: int = 640, max_items: int = 24) -> Path | None:
    """One row per `cols` images, each labelled with time / distance / coverage."""
    if not items:
        return None
    stride = max(1, math.ceil(len(items) / max_items))
    chosen = items[::stride]
    tiles = []
    for item in chosen:
        h = item["hit"]
        v_txt = f"{h['vEgo']:.1f}" if h.get("vEgo") is not None else "n/a"
        label = (f"{'TRIGGER' if item.get('trigger') else 'GATED'} t={h['t']:.1f}s "
                 f"d={h['distance_m']:.1f}m cov={h['coverage']:.2f} v={v_txt}m/s")
        if item.get("reason"):
            label += f" [{item['reason']}]"
        tiles.append(_tile(h["jpeg"], label, width=width))
    th = max(t.shape[0] for t in tiles)
    rows = math.ceil(len(tiles) / cols)
    header = 40
    sheet_w = cols * width + (cols - 1) * 4
    sheet = np.full((header + rows * (th + 4), sheet_w, 3), 24, dtype=np.uint8)
    cv2.putText(sheet, title, (8, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.62, (255, 255, 255), 1, cv2.LINE_AA)
    for idx, tile in enumerate(tiles):
        r, c = divmod(idx, cols)
        y = header + r * (th + 4)
        x = c * (width + 4)
        tile = cv2.copyMakeBorder(tile, 0, th - tile.shape[0], 0, 0, cv2.BORDER_CONSTANT, value=(24, 24, 24))
        sheet[y:y + th, x:x + width] = tile
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out_path), sheet, [cv2.IMWRITE_JPEG_QUALITY, 88])
    return out_path


def build_intervals(hits: list[dict], gap_s: float = 0.35) -> list[dict]:
    """Group consecutive trigger frames into continuous intervals."""
    intervals: list[dict] = []
    current: dict | None = None
    for hit in hits:
        if current is None or hit["t"] - current["t_end"] > gap_s:
            current = {"t_start": hit["t"], "t_end": hit["t"], "frames": [hit]}
            intervals.append(current)
        else:
            current["t_end"] = hit["t"]
            current["frames"].append(hit)

    out = []
    for iv in intervals:
        frames = iv["frames"]
        dist = [f["distance_m"] for f in frames]
        cov = [f["coverage"] for f in frames]
        vs = [f["vEgo"] for f in frames if f["vEgo"] is not None]
        out.append({
            "t_start": _r(iv["t_start"], 2),
            "t_end": _r(iv["t_end"], 2),
            "n_frames": len(frames),
            "duration_s": _r(iv["t_end"] - iv["t_start"] + 1.0 / DETECT_HZ, 2),
            "distance_m": {"min": _r(min(dist), 2), "max": _r(max(dist), 2),
                           "median": _r(float(np.median(dist)), 2)},
            "coverage": {"min": _r(min(cov)), "max": _r(max(cov)),
                         "median": _r(float(np.median(cov)))},
            "vEgo_mps": {"min": _r(min(vs), 2), "max": _r(max(vs), 2),
                         "median": _r(float(np.median(vs)), 2)} if vs else None,
        })
    return out


def process_case(route: str, start: float, end: float, note: str, out_dir: Path,
                 time_offset: float, max_sheet_items: int, impl, impl_name: str,
                 impl_path: str) -> dict:
    import av

    video = MEDIA_ROOT / route / "fcamera.hevc"
    rlog = MEDIA_ROOT / route / "rlog.zst"
    if not video.exists():
        raise FileNotFoundError(f"missing video: {video}")
    if not rlog.exists():
        raise FileNotFoundError(f"missing rlog: {rlog}")

    tag = f"{route}_{start:g}-{end:g}"
    # A rerun with a different implementation may legitimately produce no sheet
    # where a previous run did; drop stale sheets so the directory stays truthful.
    out_dir.mkdir(parents=True, exist_ok=True)
    for suffix in ("_hits.jpg", "_gated.jpg"):
        stale = out_dir / f"{tag}{suffix}"
        if stale.exists():
            stale.unlink()

    t_wall = time.time()
    sig = Signals(rlog)
    cap = av.open(str(video))
    stream = cap.streams.video[0]

    tracker = impl.StopLineTracker()
    zero: float | None = None
    decoded = 0
    processed = 0
    timeline: list[dict] = []
    hits: list[dict] = []
    gated: list[dict] = []
    raw_frames = 0
    raw_dets = 0
    speed_pass = 0
    lead_pass = 0
    both_pass = 0

    try:
        for frame in cap.decode(stream):
            if zero is None:
                zero = frame.time or 0.0
            t = (frame.time or 0.0) - zero
            if t > end + 1e-6:
                break
            if t < start - 1e-6:
                decoded += 1
                continue
            if decoded % FRAME_STEP != 0:
                decoded += 1
                continue

            img = frame.to_ndarray(format="bgr24")
            cands = impl.detect(img)
            confirmed = tracker.update(cands, t)
            v_ego, present, drel, staleness = sig.at(t + time_offset)
            reasons = gate_reasons(v_ego, present, drel)

            if v_ego is not None and v_ego < V_EGO_MAX:
                speed_pass += 1
            if present is False or (present is True and drel is not None and drel >= LEAD_MIN_DREL):
                lead_pass += 1
            if not reasons:
                both_pass += 1

            processed += 1
            if cands:
                raw_frames += 1
                raw_dets += len(cands)

            row = {
                "t": _r(t, 2),
                "n_raw": len(cands),
                "confirmed": confirmed is not None,
                "vEgo": _r(v_ego, 2) if v_ego is not None else None,
                "lead_present": present,
                "dRel": _r(drel, 1) if drel is not None else None,
                "gate": reasons,
            }
            timeline.append(row)

            if confirmed is not None:
                hit = {
                    "t": _r(t, 2),
                    "row": int(confirmed.row),
                    "distance_m": _r(confirmed.distance_m, 2),
                    "coverage": _r(confirmed.coverage),
                    "contrast": _r(confirmed.contrast, 1),
                    "score": _r(confirmed.score, 3),
                    "vEgo": _r(v_ego, 2) if v_ego is not None else None,
                    "lead_present": present,
                    "dRel": _r(drel, 1) if drel is not None else None,
                    "jpeg": _thumb(annotate(img, [confirmed], impl)),
                }
                if reasons:
                    hit["reason"] = "+".join(reasons)
                    gated.append({"hit": hit, "trigger": False, "reason": hit["reason"]})
                else:
                    hits.append(hit)
            decoded += 1
    finally:
        cap.close()

    # Drop raw bytes from JSON-visible dicts.
    hits_json = [{k: v for k, v in h.items() if k != "jpeg"} for h in hits]
    gated_json = [{"reason": g["reason"], **{k: v for k, v in g["hit"].items() if k != "jpeg"}}
                  for g in gated]

    intervals = build_intervals(hits_json)
    v_processed = [r["vEgo"] for r in timeline if r["vEgo"] is not None]
    lead_samples = [r for r in timeline if r["lead_present"] is not None]
    lead_present_frac = (sum(1 for r in lead_samples if r["lead_present"]) / len(lead_samples)
                         if lead_samples else None)
    drels = [r["dRel"] for r in lead_samples if r["lead_present"] and r["dRel"] is not None]

    hits_sheet = build_sheet(
        [{"hit": h, "trigger": True} for h in hits],
        f"{route}  {start:g}-{end:g}s  triggers={len(hits)}  gate: vEgo<{V_EGO_MAX} m/s & no lead<{LEAD_MIN_DREL:g} m",
        out_dir / f"{tag}_hits.jpg", max_items=max_sheet_items)
    gated_sheet = build_sheet(
        gated,
        f"{route}  {start:g}-{end:g}s  gated-out confirmed={len(gated)}  (not triggers)",
        out_dir / f"{tag}_gated.jpg", max_items=max(1, max_sheet_items // 2))

    reasons_count: dict[str, int] = {}
    for g in gated_json:
        reasons_count[g["reason"]] = reasons_count.get(g["reason"], 0) + 1

    result = {
        "route": route,
        "note": note,
        "window_s": [start, end],
        "video": str(video),
        "rlog": str(rlog),
        "detector": {"impl": impl_name, "module": impl_path},
        "params": {
            "detect_hz": DETECT_HZ,
            "frame_step": FRAME_STEP,
            "v_ego_max_mps": V_EGO_MAX,
            "lead_min_drel_m": LEAD_MIN_DREL,
            "time_offset_s": time_offset,
            "tracker_min_frames": tracker.min_frames,
            "max_staleness_s": MAX_STALENESS_S,
        },
        "alignment": {
            "video_zero": "first decoded frame.time",
            "log_zero": "first event of the last contiguous rlog cluster "
                        "(leading metadata/initData prefix dropped)",
            "log_leading_prefix_s": sig.leading_gap_s,
            "estimated_uncertainty_s": "0.5-1.0",
            "time_offset_applied_s": time_offset,
        },
        "log_span_s": sig.span_s,
        "counts": {
            "frames_processed": processed,
            "frames_with_raw_candidate": raw_frames,
            "raw_candidates_total": raw_dets,
            "frames_tracker_confirmed": len(hits) + len(gated),
            "frames_gated_out": len(gated),
            "frames_triggered": len(hits),
        },
        "gates": {
            "speed_pass_frames": speed_pass,
            "lead_pass_frames": lead_pass,
            "both_pass_frames": both_pass,
            "speed_pass_pct": _r(100.0 * speed_pass / processed, 1) if processed else 0.0,
            "lead_pass_pct": _r(100.0 * lead_pass / processed, 1) if processed else 0.0,
        },
        "signals": {
            "vEgo_mps": {
                "min": _r(min(v_processed), 2), "median": _r(float(np.median(v_processed)), 2),
                "max": _r(max(v_processed), 2),
            } if v_processed else None,
            "lead_present_frac": _r(lead_present_frac) if lead_present_frac is not None else None,
            "dRel_present_median_m": _r(float(np.median(drels)), 1) if drels else None,
        },
        "gated_reasons": reasons_count,
        "trigger_intervals": intervals,
        "hits": hits_json,
        "gated_out": gated_json,
        "timeline": timeline,
        "artifacts": {
            "hits_sheet": str(hits_sheet) if hits_sheet else None,
            "gated_sheet": str(gated_sheet) if gated_sheet else None,
        },
        "runtime_s": _r(time.time() - t_wall, 2),
    }

    review = REVIEW.get((impl_name, route, start, end))
    result["review"] = ({"verdict": review["verdict"],
                         "label": VERDICT_LABELS[review["verdict"]],
                         "detail": review["detail"]} if review else None)

    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"{tag}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    result["_json_path"] = str(json_path)
    result["_hits_sheet"] = str(hits_sheet) if hits_sheet else None
    result["_gated_sheet"] = str(gated_sheet) if gated_sheet else None
    return result


def _cmp_cells(r: dict) -> list[str]:
    c = r["counts"]
    dist = cov = v = "—"
    if r["trigger_intervals"]:
        d0 = min(iv["distance_m"]["min"] for iv in r["trigger_intervals"])
        d1 = max(iv["distance_m"]["max"] for iv in r["trigger_intervals"])
        c0 = min(iv["coverage"]["min"] for iv in r["trigger_intervals"])
        c1 = max(iv["coverage"]["max"] for iv in r["trigger_intervals"])
        vs = [iv["vEgo_mps"] for iv in r["trigger_intervals"] if iv["vEgo_mps"]]
        dist = f"{d0:.1f}–{d1:.1f}"
        cov = f"{c0:.2f}–{c1:.2f}"
        v = f"{min(x['min'] for x in vs):.1f}–{max(x['max'] for x in vs):.1f}" if vs else "—"
    return [str(c["frames_processed"]), str(c["frames_with_raw_candidate"]),
            str(c["frames_tracker_confirmed"]), str(c["frames_gated_out"]),
            str(c["frames_triggered"]), str(len(r["trigger_intervals"])),
            dist, cov, v]


def render_comparison(results: list[dict], compare_dir: Path, out_dir: Path) -> Path:
    """Side-by-side table of this run and another implementation's JSONs."""
    rows: list[tuple[str, str, dict | None]] = []
    other_label = "other"
    other_module = "?"
    for r in results:
        tag = f"{r['route']}_{r['window_s'][0]:g}-{r['window_s'][1]:g}.json"
        other_path = compare_dir / tag
        other = None
        if other_path.exists():
            other = json.loads(other_path.read_text(encoding="utf-8"))
            other_label = (other.get("detector") or {}).get("impl", other_label)
            other_module = (other.get("detector") or {}).get("module", other_module)
        rows.append((r["route"], r["detector"]["impl"], r))
        rows.append((r["route"], other_label, other))

    this_label = results[0]["detector"]["impl"]
    lines = ["# 检测实现对比（同一门控、同一窗口）", "",
             f"本表对比 **{IMPL_LABELS.get(this_label, this_label)}**（`{results[0]['detector']['module']}`）"
             f" 与 **{IMPL_LABELS.get(other_label, other_label)}**（`{other_module}`，"
             f"目录 `{compare_dir}`）。", "",
             "| 案例 | 实现 | 处理帧 | 原始候选帧 | 确认帧 | 门控掉 | 触发帧 | 区间 | 触发距离(m) | 触发 coverage | 触发 vEgo |",
             "|---|---|---:|---:|---:|---:|---:|---:|---|---|---|"]
    for route, label, r in rows:
        if r is None:
            lines.append(f"| `{route}` | {label} | — | — | — | — | — | — | — | — | — |")
            continue
        cells = _cmp_cells(r)
        win = f"{r['window_s'][0]:g}–{r['window_s'][1]:g}"
        lines.append(f"| `{route}` {win}s | {label} | " + " | ".join(cells) + " |")
    lines.append("")
    lines.append("说明：两列使用同一份视频、同一 rlog 时间轴与同一门控；差异只来自检测/聚类参数。")
    lines.append("")
    path = out_dir / "comparison.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def render_summary(results: list[dict], out_dir: Path) -> Path:
    lines: list[str] = []
    lines.append("# 停止线检测：低速 + 无前车 门控离线评估")
    lines.append("")
    lines.append(f"生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}　仅离线研究，未接入车端控制。")
    lines.append("")
    if results:
        det = results[0].get("detector") or {}
        label = IMPL_LABELS.get(det.get("impl", ""), det.get("impl", "?"))
        lines.append(f"检测模块：**{label}**（`{det.get('module', '?')}`）。")
        lines.append("")
    lines.append("门控条件：`vEgo < 8.9 m/s`（32 km/h）且无前车（`leadOne.present == False` "
                 "或 `leadOne.dRel >= 30 m`）。检测以 10 Hz（每 2 帧）运行，跟踪器要求连续 3 帧确认。")
    lines.append("")
    lines.append("**时间对齐**：视频以首帧 `frame.time` 为零点；rlog 取**最后一个连续事件簇**的首个 "
                 "`logMonoTime` 为零点（部分 segment 的 rlog 开头只有一条 `initData`，与真实行车日志相隔数十秒，"
                 "逐条 JSON 的 `alignment.log_leading_prefix_s` 给出被丢弃的前缀长度）。"
                 "两个零点仍不完全重合，估计残差 **0.5–1 s**，秒级时间戳只用于定位，不作跨源严格因果结论。")
    lines.append("")
    lines.append("## 总表")
    lines.append("")
    lines.append("| 路线 | 窗口(s) | 处理帧 | 原始候选帧 | 跟踪确认帧 | 门控掉帧 | **触发帧** | 触发区间 | 距离(m) | coverage | vEgo(m/s) | 复核 | 接触表 |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|---|---|---|---|---|")
    for r in results:
        c = r["counts"]
        s = r["signals"]
        v = s["vEgo_mps"] if s else None
        dist_txt = cov_txt = v_txt = "—"
        if r["trigger_intervals"]:
            d0 = min(iv["distance_m"]["min"] for iv in r["trigger_intervals"])
            d1 = max(iv["distance_m"]["max"] for iv in r["trigger_intervals"])
            c0 = min(iv["coverage"]["min"] for iv in r["trigger_intervals"])
            c1 = max(iv["coverage"]["max"] for iv in r["trigger_intervals"])
            v0 = min(iv["vEgo_mps"]["min"] for iv in r["trigger_intervals"] if iv["vEgo_mps"])
            v1 = max(iv["vEgo_mps"]["max"] for iv in r["trigger_intervals"] if iv["vEgo_mps"])
            dist_txt = f"{d0:.1f}–{d1:.1f}"
            cov_txt = f"{c0:.2f}–{c1:.2f}"
            v_txt = f"{v0:.1f}–{v1:.1f}"
        elif v:
            v_txt = f"{v['min']:.1f}–{v['max']:.1f}"
        sheet = Path(r["_hits_sheet"]).name if r["_hits_sheet"] else None
        sheet_cell = f"[{sheet}]({sheet})" if sheet else "—"
        verdict = r["review"]["label"] if r.get("review") else "—"
        lines.append(
            f"| `{r['route']}` | {r['window_s'][0]:g}–{r['window_s'][1]:g} | {c['frames_processed']} | "
            f"{c['frames_with_raw_candidate']} | {c['frames_tracker_confirmed']} | {c['frames_gated_out']} | "
            f"**{c['frames_triggered']}** | {len(r['trigger_intervals'])} | {dist_txt} | {cov_txt} | "
            f"{v_txt} | {verdict} | {sheet_cell} |")
    lines.append("")

    confirmed = sum(r["counts"]["frames_tracker_confirmed"] for r in results)
    true_frames = sum(r["counts"]["frames_triggered"] for r in results
                      if (r.get("review") or {}).get("verdict") == "true_positive")
    true_frames += sum(r["counts"]["frames_gated_out"] for r in results
                       if (r.get("review") or {}).get("verdict") == "gated_true_positive")
    false_frames = confirmed - true_frames
    triggered = sum(r["counts"]["frames_triggered"] for r in results)
    true_triggered = sum(r["counts"]["frames_triggered"] for r in results
                         if (r.get("review") or {}).get("verdict") == "true_positive")
    slow_free = [r for r in results if (r.get("review") or {}).get("verdict") in
                 ("true_positive", "false_positive")]
    slow_true = [r for r in slow_free if r["review"]["verdict"] == "true_positive"]

    lines.append("## 结论")
    lines.append("")
    lines.append(f"- 全部 {confirmed} 个“跟踪确认帧”里，人工复核判为真停止线 {true_frames} 帧、"
                 f"误检 {false_frames} 帧。")
    lines.append(f"- 通过门控的 {triggered} 个触发帧中，真停止线 {true_triggered} 帧；"
                 f"其余 {triggered - true_triggered} 帧是低速无前车条件下的误检。")
    lines.append(f"- 门控按设计拦下的真停止线："
                 f"{sum(r['counts']['frames_gated_out'] for r in results if (r.get('review') or {}).get('verdict') == 'gated_true_positive')} 帧"
                 f"（`00000112--89786bd74c--0` 高速接近路口，速度门控）；"
                 f"被拦下的误检："
                 f"{sum(r['counts']['frames_gated_out'] for r in results if (r.get('review') or {}).get('verdict') == 'gated_false_positive')} 帧。")
    if slow_free:
        lines.append(f"- 低速+无前车样本 {len(slow_free)} 条（{', '.join('`' + r['route'] + '`' for r in slow_free)}），"
                     f"其中 {len(slow_true)} 条为真停止线、{len(slow_free) - len(slow_true)} 条为误检："
                     "低速+无前车是**必要但不充分**条件，门控解决“该不该看”，不解决“看得对不对”。")
    lines.append("")
    lines.append("## 逐条")
    lines.append("")
    for r in results:
        c, g, s = r["counts"], r["gates"], r["signals"]
        lines.append(f"### `{r['route']}`　{r['window_s'][0]:g}–{r['window_s'][1]:g} s")
        lines.append("")
        lines.append(f"场景：{r['note']}")
        lines.append("")
        lines.append(f"- 处理帧 {c['frames_processed']}，原始候选帧 {c['frames_with_raw_candidate']}"
                     f"（候选总数 {c['raw_candidates_total']}），跟踪确认帧 {c['frames_tracker_confirmed']}，"
                     f"门控掉 {c['frames_gated_out']}，**触发 {c['frames_triggered']}**")
        lines.append(f"- 门控通过率：速度 {g['speed_pass_frames']}/{c['frames_processed']}"
                     f"（{g['speed_pass_pct']:.1f}%），无前车 {g['lead_pass_frames']}/{c['frames_processed']}"
                     f"（{g['lead_pass_pct']:.1f}%），两者同时 {g['both_pass_frames']}")
        if s:
            v = s["vEgo_mps"]
            lead_txt = (f"{100 * s['lead_present_frac']:.1f}%"
                        if s["lead_present_frac"] is not None else "n/a")
            lines.append(f"- 信号：vEgo {v['min']:.2f}–{v['max']:.2f} m/s（中位 {v['median']:.2f}），"
                         f"前车出现比例 {lead_txt}，出现时 dRel 中位 "
                         f"{('%.1f m' % s['dRel_present_median_m']) if s['dRel_present_median_m'] is not None else 'n/a'}")
        if r.get("review"):
            lines.append(f"- 人工复核：**{r['review']['label']}** — {r['review']['detail']}")
        if r["gated_reasons"]:
            why = "，".join(f"{k} {v}" for k, v in sorted(r["gated_reasons"].items()))
            lines.append(f"- 门控原因（确认后被丢弃）：{why}")
        if r["trigger_intervals"]:
            lines.append("")
            lines.append("| 触发区间(s) | 帧数 | 距离(m) | coverage | vEgo(m/s) |")
            lines.append("|---|---:|---|---|---|")
            for iv in r["trigger_intervals"]:
                vv = iv["vEgo_mps"]
                lines.append(f"| {iv['t_start']:.1f}–{iv['t_end']:.1f} | {iv['n_frames']} | "
                             f"{iv['distance_m']['min']:.2f}–{iv['distance_m']['max']:.2f}"
                             f"（中位 {iv['distance_m']['median']:.2f}） | "
                             f"{iv['coverage']['min']:.2f}–{iv['coverage']['max']:.2f} | "
                             f"{vv['min']:.2f}–{vv['max']:.2f} |" if vv else "—")
            lines.append("")
        else:
            lines.append("- 触发区间：无")
        if r["_hits_sheet"]:
            lines.append(f"- 触发接触表：[{Path(r['_hits_sheet']).name}]({Path(r['_hits_sheet']).name})")
        if r["_gated_sheet"]:
            lines.append(f"- 门控掉候选接触表：[{Path(r['_gated_sheet']).name}]({Path(r['_gated_sheet']).name})")
        lines.append(f"- 明细 JSON：[{Path(r['_json_path']).name}]({Path(r['_json_path']).name})")
        lines.append("")
    lines.append("## 备注")
    lines.append("")
    lines.append("- 触发只表示“在门控下会触发”，是否为真实停止线仍需按接触表人工判读；"
                 "分类结论见本轮汇报。")
    lines.append("- 距离来自检测模块的单目行→米近似（`f=1861 px`、`H=1.32 m`、`cy=380`），"
                 "未按本车安装重新标定，只作相对比较；箱体比真实白带略高 10–20 px（约 0.5–1 m）。")
    lines.append("- 生产模块与原型在同一批数据上的对比见 `comparison.md`；原型的完整结果在 "
                 "`proto_baseline/`（含它自己的 `summary.md`）。")
    lines.append("- 全程离线：未启动车端栈、未改 `tools/stopline_proto.py`、未写硬件。")
    lines.append("")

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "summary.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def parse_case(text: str) -> tuple[str, float, float, str]:
    parts = text.split(":")
    if len(parts) != 3:
        raise argparse.ArgumentTypeError(f"expected ROUTE:START:END, got {text!r}")
    route, start, end = parts
    return route, float(start), float(end), "user-specified"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cases", nargs="*", type=parse_case, help="ROUTE:START:END (repeatable)")
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT))
    ap.add_argument("--time-offset", type=float, default=0.0,
                    help="seconds added to video time before looking up the log")
    ap.add_argument("--sheet-max", type=int, default=24,
                    help="max images per contact sheet")
    ap.add_argument("--impl", choices=["prod", "proto"], default="prod",
                    help="detector module: prod=openpilot/sunnypilot/stopline/detector.py, "
                         "proto=tools/stopline_proto.py")
    ap.add_argument("--compare-dir", default=None,
                    help="directory with the other implementation's JSONs; writes comparison.md")
    args = ap.parse_args()

    cases = args.cases or DEFAULT_CASES
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    impl, impl_path = load_impl(args.impl)
    print(f"detector: {args.impl} ({impl_path})", flush=True)

    results = []
    for route, start, end, note in cases:
        print(f"[case] {route} {start:g}-{end:g}s", flush=True)
        try:
            res = process_case(route, start, end, note, out_dir,
                               args.time_offset, args.sheet_max,
                               impl, args.impl, impl_path)
        except FileNotFoundError as exc:
            print(f"  skip: {exc}", flush=True)
            continue
        c = res["counts"]
        print(f"  processed={c['frames_processed']} raw={c['frames_with_raw_candidate']} "
              f"confirmed={c['frames_tracker_confirmed']} gated={c['frames_gated_out']} "
              f"triggered={c['frames_triggered']} intervals={len(res['trigger_intervals'])} "
              f"({res['runtime_s']}s)", flush=True)
        results.append(res)

    summary = render_summary(results, out_dir)
    print(f"summary: {summary}")
    if args.compare_dir:
        comp = render_comparison(results, Path(args.compare_dir), out_dir)
        print(f"comparison: {comp}")
    totals = {
        "cases": len(results),
        "processed": sum(r["counts"]["frames_processed"] for r in results),
        "triggered": sum(r["counts"]["frames_triggered"] for r in results),
    }
    print(json.dumps(totals))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
