#!/usr/bin/env python3
"""Stop-line worker: camera and OpenCV live in this process, never in the shim.

The shim's HTTP/WS event loop and the stop-line detector used to share one Python
process.  A VisionIPC connect or a long OpenCV call in that thread could starve
the event loop, which is exactly what happened on the first on-road test.  This
worker is launched by ``stopline_source.py`` as a child process and communicates
through two tiny JSON files in /dev/shm:

* gate file (parent -> worker): ``{"enabled": bool, "speed": ..., "lead": ...}``
* fix file (worker -> parent):  ``{"valid": bool, "distance_m": ..., "at": ...}``

It never publishes msgq, writes CAN, or touches the planner.
"""
from __future__ import annotations

import json
import logging
import math
import os
import signal
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from openpilot.sunnypilot.stopline.detector import StopLineTracker, detect

LOG = logging.getLogger("cplink_stopline_worker")
_running = True
GATE_MAX_AGE_S = 0.4
FRAME_MAX_AGE_S = 0.4


def _gate_reason(gate: dict, now: float, max_age_s: float = GATE_MAX_AGE_S) -> str:
    try:
        at = float(gate["at"])
    except (KeyError, TypeError, ValueError):
        return "no_input"
    if not all(math.isfinite(v) for v in (at, now, max_age_s)) or now < at:
        return "clock_abnormal"
    if now - at > max_age_s:
        return "stale"
    return "ok" if gate.get("enabled") is True else "condition_not_met"


def _fresh_gate(gate: dict, now: float, max_age_s: float) -> bool:
    return _gate_reason(gate, now, max_age_s) == "ok"


class StopLineProcessor:
    """Validate parent and camera evidence before advancing the track."""

    def __init__(self) -> None:
        self.tracker = StopLineTracker()

    def reject(self, reason: str, now: float) -> dict:
        self.tracker.reset()
        return {"valid": False, "at": now, "confirmations": 0, "reason": reason}

    def process(self, lines, gate: dict, capture_mono: float, detected_mono: float,
                frame_id: int) -> dict:
        reason = _gate_reason(gate, detected_mono)
        if reason != "ok":
            return self.reject(f"gate_{reason}", detected_mono)
        if (not all(math.isfinite(v) for v in (capture_mono, detected_mono)) or
                capture_mono <= 0.0 or detected_mono < capture_mono):
            return self.reject("frame_clock_abnormal", detected_mono)
        if detected_mono - capture_mono > FRAME_MAX_AGE_S:
            return self.reject("frame_stale", detected_mono)
        try:
            speed = float(gate["speed"])
            context_at = float(gate.get("context_at", 0.0))
            frame_id = int(frame_id)
            if not math.isfinite(speed) or not math.isfinite(context_at) or speed < 0.0 or frame_id < 0:
                raise ValueError("invalid frame or speed")
        except (KeyError, ValueError, TypeError, OverflowError):
            return self.reject("invalid_input", detected_mono)
        if capture_mono < context_at:
            return self.reject("frame_before_context", detected_mono)
        context = str(gate.get("context", ""))
        fix = self.tracker.update(lines, capture_mono, speed_mps=speed,
                                  context=context, frame_id=frame_id)
        result = {"valid": fix is not None, "at": detected_mono,
                  "capture_mono": capture_mono, "detected_mono": detected_mono,
                  "frame_id": frame_id, "context": context,
                  "target_id": self.tracker.target_id, "confirmations": self.tracker.count,
                  "reason": self.tracker.reason}
        if fix is not None:
            result.update(distance_m=float(fix.distance_m), row=int(fix.row),
                          coverage=float(fix.coverage),
                          confidence=float(min(1.0, fix.coverage) * min(1.0, fix.contrast / 60.0)))
        return result


def _stop(_signum, _frame):
    global _running
    _running = False


def _read_json(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _connect():
    from msgq.visionipc import VisionIpcClient
    from openpilot.cereal.visionipc import VisionStreamType

    # Do not call available_streams(): on this webcam camerad it can block while
    # another client owns the listener.  A direct non-blocking connect retries.
    client = VisionIpcClient("camerad", VisionStreamType.VISION_STREAM_NARROW_ROAD, True)
    return client if client.connect(False) else None


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--gate", required=True)
    ap.add_argument("--fix", required=True)
    ap.add_argument("--rate", type=float, default=10.0)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s")
    gate_path = Path(args.gate)
    fix_path = Path(args.fix)
    period = 1.0 / max(1.0, args.rate)
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    from openpilot.sunnypilot.yolod.nv12 import nv12_to_bgr

    processor = StopLineProcessor()
    client = None
    last_frame_at = 0.0
    next_at = 0.0
    last_state = ""
    frames = 0
    detections = 0
    last_stats_log = 0.0

    while _running:
        now = time.monotonic()
        gate = _read_json(gate_path)
        enabled = _fresh_gate(gate, now, GATE_MAX_AGE_S)
        if not enabled:
            if last_state != "disabled":
                last_state = "disabled"
                LOG.info("worker disabled")
            _write_json(fix_path, processor.reject(f"gate_{_gate_reason(gate, now)}", now))
            time.sleep(0.05)
            continue

        if last_state != "enabled":
            last_state = "enabled"
            LOG.info("worker enabled speed=%s lead=%s", gate.get("speed"), gate.get("lead"))

        if client is None:
            try:
                client = _connect()
            except Exception as exc:
                client = None
                _write_json(fix_path, {"valid": False, "at": now,
                                       "error": f"camera: {type(exc).__name__}: {exc}"})
            if client is None:
                _write_json(fix_path, {"valid": False, "at": now, "error": "camera unavailable"})
                time.sleep(2.0)
                continue
            LOG.info("worker connected to camerad narrow")
            processor.tracker.reset()
            last_frame_at = time.monotonic()

        try:
            buffer = client.recv(0)
        except Exception as exc:
            client = None
            result = processor.reject("camera_error", now)
            result["error"] = f"recv: {type(exc).__name__}: {exc}"
            _write_json(fix_path, result)
            time.sleep(0.5)
            continue
        if buffer is None:
            if now - last_frame_at > FRAME_MAX_AGE_S:
                _write_json(fix_path, processor.reject("frame_stale", now))
            if now - last_frame_at > 5.0:
                client = None
            time.sleep(0.02)
            continue
        last_frame_at = now
        # SOF belongs to the captured frame.  Detection completion must never
        # extend the apparent age of a queued or delayed camera observation.
        capture_mono = float(client.timestamp_sof) * 1e-9
        frame_id = int(client.frame_id)

        if (buffer.width, buffer.height) != (1344, 760):
            result = processor.reject("frame_shape", now)
            result["error"] = f"frame {buffer.width}x{buffer.height}"
            _write_json(fix_path, result)
            time.sleep(0.2)
            continue

        if now < next_at:
            time.sleep(min(0.01, max(0.001, next_at - now)))
            continue
        next_at = now + period

        frames += 1
        try:
            bgr = nv12_to_bgr(buffer.data, buffer.width, buffer.height,
                              buffer.stride, buffer.uv_offset)
            lines = detect(bgr)
            if lines:
                detections += 1
            detected_mono = time.monotonic()
            current_gate = _read_json(gate_path)
            if current_gate.get("context") != gate.get("context"):
                result = processor.reject("gate_context_changed", detected_mono)
            else:
                result = processor.process(lines, current_gate, capture_mono, detected_mono, frame_id)
        except Exception as exc:
            result = processor.reject("detection_error", time.monotonic())
            result["error"] = f"detect: {type(exc).__name__}: {exc}"
            _write_json(fix_path, result)
            continue

        if now - last_stats_log > 5.0:
            last_stats_log = now
            LOG.info("worker frames=%d detections=%d", frames, detections)
        result.update(frames=frames, detections=detections)
        _write_json(fix_path, result)

    _write_json(fix_path, {"valid": False, "at": time.monotonic()})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
