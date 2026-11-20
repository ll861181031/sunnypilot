#!/usr/bin/env python3
"""Offline stop-line prototype (classical CV, no learned detector).

This is a research tool, not a car process.  It looks for the single thick white
transverse band a stop line paints across the ego lane and reports its image row,
pixel width and a rough range.  It deliberately refuses to guess: no band, no
candidate.

Run a single image:

    .venv/bin/python tools/stopline_proto.py --frame /tmp/frame.jpg --out /tmp/annot.jpg

Run every N-th frame of a segment:

    .venv/bin/python tools/stopline_proto.py --video ~/.comma/.../fcamera.hevc \
        --start 30 --end 45 --step 10 --out-dir /tmp/stopline_out
"""
from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass, asdict

import cv2
import numpy as np

# Narrow road camera: 1344x760, focal ~1861 px.  Lane width is an assumption used
# only for a rough range; the detector's primary output is the image row.
IMAGE_W, IMAGE_H = 1344, 760
FOCAL_PX = 1861.0
LANE_WIDTH_M = 3.5
# Camera height and principal row from modelV2 lane-line geometry (z ~ 1.32 m).
# Used only for a rough row -> metres mapping; calibrate per install.
CAMERA_HEIGHT_M = 1.32
PRINCIPAL_ROW = 380.0

# A broad ego-lane-shaped trapezoid.  It intentionally includes some neighbouring
# road: the detector then requires a band that spans most of the ROI, which rejects
# most lane markings while still finding a stop line on a curve or during a lane
# change.  The later production version should use modelV2 lane lines.
ROI_BOTTOM_Y = 735
ROI_TOP_Y = 380
ROI_BOTTOM_X = (300, 1050)
ROI_TOP_X = (540, 800)


@dataclass
class Candidate:
    row: int
    top: int
    bottom: int
    thickness: int
    coverage: float
    contrast: float
    distance_m: float
    score: float


def _roi_x(y: int) -> tuple[int, int]:
    t = (ROI_BOTTOM_Y - y) / max(1, ROI_BOTTOM_Y - ROI_TOP_Y)
    t = min(1.0, max(0.0, t))
    x1 = int(ROI_BOTTOM_X[0] + (ROI_TOP_X[0] - ROI_BOTTOM_X[0]) * t)
    x2 = int(ROI_BOTTOM_X[1] + (ROI_TOP_X[1] - ROI_BOTTOM_X[1]) * t)
    return x1, x2


def bright_mask(bgr: np.ndarray) -> np.ndarray:
    """White paint under day light, local bright bands under sodium street lights.

    At night the lane is almost black and white paint is only a few grey levels
    above the asphalt; a saturation test rejects exactly the paint we need.  A
    top-hat gives the local contrast instead.  The caller's white-coverage test
    then decides whether the band spans the lane.
    """
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    v = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)[:, :, 2]
    if float(v.mean()) < 60.0:
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (21, 21))
        tophat = cv2.morphologyEx(gray, cv2.MORPH_TOPHAT, kernel)
        mask = (tophat > 9).astype(np.uint8) * 255
    else:
        hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
        _, s, val = cv2.split(hsv)
        mask = ((s < 80) & (val > 150)).astype(np.uint8) * 255
    return cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))

def row_profile(mask: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    rows = np.arange(ROI_TOP_Y, ROI_BOTTOM_Y + 1)
    frac = np.zeros(len(rows), dtype=np.float32)
    for i, y in enumerate(rows):
        x1, x2 = _roi_x(int(y))
        seg = mask[y, x1:x2]
        frac[i] = float(seg.mean()) / 255.0 if seg.size else 0.0
    return rows, frac


def find_runs(rows: np.ndarray, frac: np.ndarray, threshold: float) -> list[tuple[int, int]]:
    runs = []
    start = None
    for i, value in enumerate(frac):
        if value >= threshold and start is None:
            start = i
        elif value < threshold and start is not None:
            runs.append((start, i - 1))
            start = None
    if start is not None:
        runs.append((start, len(frac) - 1))
    return runs


def _horizontal_segments(bgr: np.ndarray) -> list[tuple[int, int, int, int]]:
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    v = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)[:, :, 2]
    if float(v.mean()) < 60.0:
        edges = cv2.Canny(gray, 20, 60)
    else:
        edges = cv2.Canny(gray, 45, 140)
    roi_mask = np.zeros_like(edges)
    for y in range(ROI_TOP_Y, ROI_BOTTOM_Y + 1):
        x1, x2 = _roi_x(y)
        roi_mask[y, x1:x2] = 255
    edges = cv2.bitwise_and(edges, roi_mask)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=30,
                            minLineLength=60, maxLineGap=25)
    out = []
    if lines is None:
        return out
    for x1, y1, x2, y2 in lines[:, 0]:
        if x2 < x1:
            x1, x2 = x2, x1
            y1, y2 = y2, y1
        angle = abs(np.degrees(np.arctan2(y2 - y1, x2 - x1)))
        if angle <= 10.0 or angle >= 170.0:
            out.append((int(x1), int(y1), int(x2), int(y2)))
    return out


def _clusters(segments: list[tuple[int, int, int, int]]) -> list[dict]:
    """Group near-horizontal Hough segments into one band per stop line."""
    clusters: list[dict] = []
    for x1, y1, x2, y2 in sorted(segments, key=lambda s: (s[1] + s[3]) / 2):
        row = (y1 + y2) / 2
        target = None
        for c in clusters:
            if abs(c["row"] - row) <= 6:
                target = c
                break
        if target is None:
            clusters.append({"row": row, "ys": [y1, y2], "spans": [(x1, x2)]})
        else:
            target["ys"].extend([y1, y2])
            target["spans"].append((x1, x2))
            target["row"] = float(np.mean(target["ys"]))
    out = []
    for c in clusters:
        spans = sorted(c["spans"])
        merged = []
        for a, b in spans:
            if not merged or a > merged[-1][1] + 30:
                merged.append([a, b])
            else:
                merged[-1][1] = max(merged[-1][1], b)
        union = sum(b - a for a, b in merged)
        out.append({"row": int(round(c["row"])), "union": union,
                    "top": int(min(c["ys"])), "bottom": int(max(c["ys"])),
                    "segments": len(c["spans"]), "merged": merged})
    return out


def detect(bgr: np.ndarray, max_candidates: int = 3) -> list[Candidate]:
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    night = float(cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)[:, :, 2].mean()) < 60.0
    clusters = _clusters(_horizontal_segments(bgr))
    raw = []
    for c in clusters:
        row = c["row"]
        # 10-40 m ahead is where a stop decision is useful; rows above this are
        # too far to range reliably and are dominated by bridges/guardrails.
        if not (480 <= row <= 620):
            continue
        thickness = c["bottom"] - c["top"] + 1
        if not (2 <= thickness <= 30):
            continue
        x1, x2 = _roi_x(row)
        roi_w = max(1, x2 - x1)
        if c["union"] < 160 or c["union"] / roi_w < 0.28:
            continue
        # The band must lie over ego's lane, not only on the shoulder/guardrail.
        # A stop line crosses the centre; a white road edge does not.
        centre_lo, centre_hi = 400, 950
        overlap = sum(max(0, min(b, centre_hi) - max(a, centre_lo))
                      for a, b in c["merged"])
        if overlap / (centre_hi - centre_lo) < 0.35:
            continue
        # Contrast: the band against the road just above/below it.
        top, bottom = c["top"], c["bottom"]
        pad = max(3, thickness)
        above = gray[max(ROI_TOP_Y, top - pad - 12):max(ROI_TOP_Y + 1, top - pad), x1:x2]
        below = gray[min(IMAGE_H - 1, bottom + pad):min(IMAGE_H, bottom + pad + 12), x1:x2]
        ref = np.concatenate([above.ravel(), below.ravel()]) if above.size or below.size else np.array([])
        contrast = float(gray[top:bottom + 1, x1:x2].mean() - (ref.mean() if ref.size else 0.0))
        if contrast < (6.0 if night else 14.0):
            continue
        distance = FOCAL_PX * CAMERA_HEIGHT_M / max(1.0, row - PRINCIPAL_ROW)
        coverage = c["union"] / roi_w
        # A stop line is white paint, not just an edge: bridge beams, guardrails and
        # lane arrows can produce a horizontal Hough line but not a white band.
        white = bright_mask(bgr)[top:bottom + 1, x1:x2]
        white_cov = float(white.mean()) / 255.0 if white.size else 0.0
        if white_cov < (0.30 if night else 0.18):
            continue
        score = coverage * min(1.0, contrast / 60.0) * min(1.0, c["union"] / 300.0) * min(1.0, white_cov / 0.35)
        raw.append(Candidate(row=row, top=top, bottom=bottom, thickness=thickness,
                             coverage=coverage, contrast=contrast,
                             distance_m=distance, score=score))

    # A zebra crossing is several similar bands stacked above the nearest one.
    # Keep the nearest sustained band (the stop line sits before the zebra), and
    # drop the farther ones so one physical stop line yields one candidate.
    raw.sort(key=lambda c: c.row, reverse=True)
    kept: list[Candidate] = []
    for cand in raw:
        if kept and abs(cand.row - kept[-1].row) < 25:
            continue
        kept.append(cand)
    # The stop line is the nearest sustained transverse band; keep the nearest
    # first so the tracker never jumps to a farther lane marking when both exist.
    kept.sort(key=lambda c: c.row, reverse=True)
    return kept[:max_candidates]

def annotate(bgr: np.ndarray, candidates: list[Candidate]) -> np.ndarray:
    out = bgr.copy()
    for i, c in enumerate(candidates):
        x1, x2 = _roi_x(c.row)
        color = (0, 255, 0) if i == 0 else (0, 200, 255)
        cv2.rectangle(out, (x1, c.top), (x2, c.bottom), color, 2)
        cv2.putText(out, f"row={c.row} cov={c.coverage:.2f} d={c.distance_m:.1f}m",
                    (max(0, x1), max(20, c.top - 8)), cv2.FONT_HERSHEY_SIMPLEX,
                    0.55, color, 2, cv2.LINE_AA)
    cv2.rectangle(out, (ROI_BOTTOM_X[0], ROI_BOTTOM_Y), (ROI_BOTTOM_X[1], ROI_BOTTOM_Y),
                  (255, 0, 255), 1)
    return out


class StopLineTracker:
    """Require a band to persist over consecutive frames before reporting it.

    A single Hough frame can fire on a wet edge, an arrow or a shadow.  A real
    stop line moves smoothly towards the car (row grows, distance shrinks) and
    persists.  Three consecutive 10 Hz frames are enough for the low-speed gate
    while rejecting isolated false positives.
    """

    def __init__(self, min_frames: int = 3, max_gap_s: float = 0.5) -> None:
        self.min_frames = max(1, int(min_frames))
        self.max_gap_s = max_gap_s
        self.count = 0
        self.prev: Candidate | None = None
        self.prev_t = 0.0
        self.history: list[Candidate] = []
        self.last_good: Candidate | None = None

    def reset(self) -> None:
        self.count = 0
        self.prev = None
        self.history = []
        self.last_good = None

    def update(self, candidates: list[Candidate], t: float) -> Candidate | None:
        if not candidates:
            if self.prev is not None and t - self.prev_t > self.max_gap_s:
                self.reset()
            return None
        cand = max(candidates, key=lambda c: c.row)  # nearest band
        # Once a stop line has been confirmed, never jump to a farther band: it is
        # a different marking (lane edge, second line), and adopting it would move
        # the stop point backwards.  A nearer band is still allowed (the real line
        # becoming visible after a false first lock).
        if self.last_good is not None and cand.distance_m > self.last_good.distance_m + 2.5:
            return None
        if self.prev is None or t - self.prev_t > self.max_gap_s:
            self.count = 1
            self.history = [cand]
        else:
            dz = self.prev.distance_m - cand.distance_m
            drow = cand.row - self.prev.row
            if (-1.5 <= dz <= 4.0 and -3 <= drow <= 40
                    and cand.coverage >= 0.5):
                self.count += 1
                self.history.append(cand)
                self.history = self.history[-self.min_frames:]
            else:
                self.count = 1
                self.history = [cand]
        self.prev = cand
        self.prev_t = t
        if self.count < self.min_frames or not self.history:
            return None
        recent = self.history[-self.min_frames:]
        out = Candidate(
            row=int(np.median([c.row for c in recent])),
            top=min(c.top for c in recent), bottom=max(c.bottom for c in recent),
            thickness=int(np.median([c.thickness for c in recent])),
            coverage=float(np.median([c.coverage for c in recent])),
            contrast=float(np.median([c.contrast for c in recent])),
            distance_m=float(np.median([c.distance_m for c in recent])),
            score=float(np.median([c.score for c in recent])),
        )
        self.last_good = out
        return out

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--frame")
    ap.add_argument("--video")
    ap.add_argument("--start", type=float, default=0.0)
    ap.add_argument("--end", type=float, default=60.0)
    ap.add_argument("--step", type=int, default=10, help="decode every N-th frame")
    ap.add_argument("--out")
    ap.add_argument("--out-dir")
    ap.add_argument("--track", action="store_true", help="require 3 consecutive frames")
    args = ap.parse_args()

    if args.frame:
        img = cv2.imread(args.frame)
        if img is None:
            raise SystemExit(f"cannot read {args.frame}")
        cands = detect(img)
        out = annotate(img, cands)
        path = args.out or "stopline_annotated.jpg"
        cv2.imwrite(path, out)
        print(json.dumps([asdict(c) for c in cands], indent=2))
        print("saved", path)
        return 0

    if not args.video:
        raise SystemExit("need --frame or --video")
    import av

    out_dir = args.out_dir or "stopline_frames"
    os.makedirs(out_dir, exist_ok=True)
    cap = av.open(args.video)
    stream = cap.streams.video[0]
    t0 = None
    index = 0
    report = []
    tracker = StopLineTracker()
    decoded = 0
    for frame in cap.decode(stream):
        if t0 is None:
            t0 = frame.time or 0.0
        t = (frame.time or 0.0) - t0
        if t < args.start:
            continue
        if t > args.end:
            break
        if decoded % max(1, args.step) == 0:
            img = frame.to_ndarray(format="bgr24")
            cands = detect(img)
            if args.track:
                kept = tracker.update(cands, t)
                shown = [kept] if kept is not None else []
            else:
                shown = cands
            if shown:
                annotated = annotate(img, shown)
                name = f"{index:06d}_{t:05.1f}s.jpg"
                cv2.imwrite(os.path.join(out_dir, name), annotated)
            report.append({"t": round(t, 2), "candidates": [asdict(c) for c in shown]})
        decoded += 1
        index += 1
    cap.close()
    with open(os.path.join(out_dir, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    hits = sum(1 for r in report if r["candidates"])
    print(f"frames={len(report)} hits={hits} out={out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
