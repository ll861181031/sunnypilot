#!/usr/bin/env python3
"""Low-speed stop-line distance source for the CPlink shim.

The OpenCV/VisionIPC work runs in ``stopline_worker.py``, a separate process.  This
parent only:

* evaluates the nav/speed/lead gate,
* writes ``/dev/shm/cplink_stopline_gate.json`` for the worker,
* reads ``/dev/shm/cplink_stopline_fix.json`` for the nav lamp bridge.

Keeping camera work out of the shim's GIL is deliberate: the first on-road test
froze the shim's HTTP/WS event loop when the source connected VisionIPC in a
thread.  The feature must never be able to block the navigation bridge.
"""
from __future__ import annotations

import json
import logging
import math
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LOG = logging.getLogger("cplink_shim.stopline")

ROOT = Path(__file__).resolve().parents[2]
WORKER = Path(__file__).with_name("stopline_worker.py")
GATE_PATH = Path("/dev/shm/cplink_stopline_gate.json")
FIX_PATH = Path("/dev/shm/cplink_stopline_fix.json")
INPUT_MAX_AGE_S = 0.3
GATE_MAX_AGE_S = 0.4


def _input_reason(sm, name: str, now: float, max_age_s: float) -> str:
    if sm is None or not sm.seen.get(name):
        return "no_input"
    if not sm.valid.get(name):
        return "invalid_input"
    try:
        at = float(sm.recv_time[name])
        age = now - at
    except (AttributeError, KeyError, TypeError, ValueError):
        return "no_input"
    if not all(math.isfinite(v) for v in (now, at, max_age_s, age)) or age < 0.0:
        return "clock_abnormal"
    return "stale" if age > max_age_s else "ok"


def _fresh_input(sm, name: str, now: float, max_age_s: float) -> bool:
    return _input_reason(sm, name, now, max_age_s) == "ok"


@dataclass(frozen=True)
class StopLineFix:
    distance_m: float
    row: int
    coverage: float
    confidence: float
    at: float
    confirmations: int = 0
    target_id: str = ""
    frame_id: int = 0
    capture_mono: float = 0.0
    detected_mono: float = 0.0
    context: str = ""
    reason: str = ""


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


class StopLineSource:
    def __init__(self, navi_state: Any, *,
                 clock=time.monotonic,
                 rate_hz: float = 10.0,
                 max_speed_ms: float | None = None,
                 max_lead_m: float = 30.0,
                 max_age_s: float = 0.4,
                 gate_path: Path | None = None,
                 fix_path: Path | None = None) -> None:
        self.navi_state = navi_state
        self._clock = clock
        self.period_s = 1.0 / max(1.0, float(rate_hz))
        self.max_speed_ms = (float(os.getenv("CPLINK_STOPLINE_MAX_SPEED_KMH", "32")) / 3.6
                             if max_speed_ms is None else float(max_speed_ms))
        self.max_lead_m = float(os.getenv("CPLINK_STOPLINE_MAX_LEAD_M", str(max_lead_m)))
        self.max_age_s = max(0.1, float(max_age_s))
        self.gate_path = Path(gate_path or os.getenv("CPLINK_STOPLINE_GATE", GATE_PATH))
        self.fix_path = Path(fix_path or os.getenv("CPLINK_STOPLINE_FIX", FIX_PATH))

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None
        self._restart_at = 0.0

        self._state = "idle"
        self._error = ""
        self._enabled = False
        self._speed_mps: float | None = None
        self._lead_drel: float | None = None
        self._last_state_log = ""
        self._context = ""
        self._context_at = 0.0
        self._gate_at: float | None = None
        self._lead_seen_at: float | None = None
        self._lead_seen_distance: float | None = None

    # ── public API ──
    def snapshot(self) -> StopLineFix | None:
        with self._lock:
            if not self._enabled:
                return None
            gate_at = self._gate_at
            context = self._context
        now = self._clock()
        if gate_at is None or not math.isfinite(now - gate_at) or not 0.0 <= now - gate_at <= GATE_MAX_AGE_S:
            return None
        data = _read_json(self.fix_path)
        if not data.get("valid"):
            return None
        try:
            at = float(data["at"])
            distance = float(data["distance_m"])
            row = int(data["row"])
            coverage = float(data.get("coverage", 0.0))
            confidence = float(data.get("confidence", 0.0))
            capture_mono = float(data["capture_mono"])
            detected_mono = float(data["detected_mono"])
            confirmations = int(data["confirmations"])
            frame_id = int(data["frame_id"])
        except (KeyError, TypeError, ValueError):
            return None
        if not all(math.isfinite(v) for v in (at, distance, coverage, confidence, capture_mono, detected_mono)):
            return None
        if not 0.0 <= now - capture_mono <= self.max_age_s or not capture_mono <= detected_mono <= now or at > now:
            return None
        if not (0.0 < distance <= 60.0):
            return None
        if confirmations < 3 or not data.get("target_id") or data.get("context") != context:
            return None
        return StopLineFix(distance_m=distance, row=row, coverage=coverage,
                           confidence=confidence, at=at, confirmations=confirmations,
                           target_id=str(data["target_id"]), frame_id=frame_id,
                           capture_mono=capture_mono, detected_mono=detected_mono,
                           context=context, reason=str(data.get("reason", "")))

    def status(self) -> dict[str, Any]:
        data = _read_json(self.fix_path)
        fix = self.snapshot()
        with self._lock:
            proc = self._proc
            return {
                "enabled": self._enabled,
                "state": self._state,
                "speed_mps": None if self._speed_mps is None else round(self._speed_mps, 2),
                "lead_drel_m": None if self._lead_drel is None else round(self._lead_drel, 1),
                "worker_pid": None if proc is None else proc.pid,
                "worker_alive": bool(proc is not None and proc.poll() is None),
                "fix": None if fix is None else {
                    "distance_m": fix.distance_m, "row": fix.row,
                    "coverage": fix.coverage, "confidence": fix.confidence,
                    "age_s": round(self._clock() - fix.capture_mono, 3),
                    "confirmations": fix.confirmations, "target_id": fix.target_id,
                    "frame_id": fix.frame_id, "capture_mono": fix.capture_mono,
                    "detected_mono": fix.detected_mono,
                },
                "worker_reason": str(data.get("reason", "no_input")),
                "error": self._error or str(data.get("error", "")),
            }

    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return True
        self.gate_path.parent.mkdir(parents=True, exist_ok=True)
        self.fix_path.parent.mkdir(parents=True, exist_ok=True)
        _write_json(self.gate_path, {"enabled": False, "at": self._clock()})
        self._ensure_worker()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="stopline-gate", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            self._enabled = False
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        _write_json(self.gate_path, {"enabled": False, "at": self._clock()})
        with self._lock:
            proc = self._proc
            self._proc = None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                proc.kill()

    # ── worker supervision ──
    def _ensure_worker(self) -> None:
        now = self._clock()
        with self._lock:
            proc = self._proc
            if proc is not None and proc.poll() is None:
                return
            if now < self._restart_at:
                return
            self._restart_at = now + 2.0
            self._proc = subprocess.Popen(
                [sys.executable, str(WORKER), "--gate", str(self.gate_path),
                 "--fix", str(self.fix_path), "--rate", str(1.0 / self.period_s)],
                cwd=str(ROOT), env=os.environ.copy(),
            )
            LOG.info("stopline worker started pid=%s", self._proc.pid)

    # ── gating ──
    def _nav_red_or_yellow(self, value) -> bool:
        if not isinstance(value, dict) or value.get("visible") is False:
            return False
        lights = value.get("lights")
        if not isinstance(lights, dict):
            return False
        for name in ("red", "yellow"):
            entry = lights.get(name)
            if isinstance(entry, dict) and entry.get("valid") is not False and entry.get("on") is True:
                return True
        return False

    def _speed_mps_now(self, sm) -> float | None:
        try:
            if _fresh_input(sm, "carState", self._clock(), INPUT_MAX_AGE_S):
                speed = float(sm["carState"].vEgo)
                return speed if math.isfinite(speed) and speed >= 0.0 else None
        except Exception:
            pass
        # Phone speed has a different latency budget and cannot replace a stale
        # control input to authorize a final-stop target.
        return None

    def _lead_drel_now(self, sm) -> float | None:
        now = self._clock()
        distances = []
        observed = False
        malformed = False
        # radarState includes the short model-lead hold.  Consult both sources:
        # an isolated model probability dip must not open the stop-line gate.
        try:
            if _fresh_input(sm, "radarState", now, INPUT_MAX_AGE_S):
                radar = sm["radarState"]
                for name in ("leadOne", "leadTwo"):
                    lead = getattr(radar, name)
                    distance = float(lead.dRel)
                    if type(lead.present) is not bool or not math.isfinite(distance):
                        raise ValueError("invalid radar lead")
                    if lead.present:
                        if distance <= 0.0:
                            raise ValueError("invalid radar lead distance")
                        distances.append(distance)
                observed = True
        except (AttributeError, KeyError, TypeError, ValueError):
            malformed = True
        try:
            if _fresh_input(sm, "modelV2", now, INPUT_MAX_AGE_S):
                leads = sm["modelV2"].leadsV3
                if len(leads) < 2:
                    raise ValueError("missing model lead slot")
                for index in range(2):
                    lead = leads[index]
                    prob = float(lead.prob)
                    x = list(lead.x)
                    drel = float(x[0]) if x else math.nan
                    if not math.isfinite(prob) or not 0.0 <= prob <= 1.0 or not math.isfinite(drel):
                        raise ValueError("invalid model lead")
                    if prob >= 0.5:
                        if drel <= 0.0:
                            raise ValueError("invalid model lead distance")
                        distances.append(drel)
                observed = True
        except (AttributeError, KeyError, TypeError, ValueError):
            malformed = True
        # A valid close observation always blocks the gate. Missing or malformed
        # slots cannot be refuted by another slot saying it sees nothing.
        if malformed and (not distances or min(distances) >= self.max_lead_m):
            return None
        if distances:
            distance = min(distances)
            self._lead_seen_at, self._lead_seen_distance = now, distance
            return distance
        if observed:
            if self._lead_seen_at is not None and 0.0 <= now - self._lead_seen_at <= 0.5:
                return max(0.0, self._lead_seen_distance - (self._speed_mps or 0.0) * (now - self._lead_seen_at))
            return 1e9
        return None

    def _gates(self, sm) -> tuple[bool, str]:
        self._speed_mps = self._lead_drel = None
        if not all(math.isfinite(v) and v > 0.0 for v in (self.max_speed_ms, self.max_lead_m, self.max_age_s)):
            return False, "invalid_configuration"
        try:
            record = (self.navi_state.record("traffic_signal") if hasattr(self.navi_state, "record")
                      else {"value": self.navi_state.get("traffic_signal"), "reason": "no_input"})
        except Exception:
            record = {"value": None, "reason": "no_input"}
        value = record.get("value") or {}
        if not isinstance(value, dict):
            return False, "navigation invalid_input"
        context = json.dumps([record.get("session", ""), record.get("generation", 0),
                              value.get("intersection_id", ""), value.get("direction", "")],
                             separators=(",", ":"))
        if context != self._context:
            self._context = context
            self._context_at = self._clock()
            self._lead_seen_at = self._lead_seen_distance = None
        if not value:
            return False, f"navigation {record.get('reason', 'no_input')}"
        if not self._nav_red_or_yellow(value):
            return False, "no red/yellow"
        speed = self._speed_mps_now(sm)
        self._speed_mps = speed
        if speed is None:
            reason = _input_reason(sm, "carState", self._clock(), INPUT_MAX_AGE_S)
            return False, "no speed" if reason == "no_input" else f"carState {'invalid_input' if reason == 'ok' else reason}"
        if speed >= self.max_speed_ms:
            return False, "fast"
        lead = self._lead_drel_now(sm)
        self._lead_drel = lead
        if lead is None:
            reasons = [_input_reason(sm, name, self._clock(), INPUT_MAX_AGE_S) for name in ("modelV2", "radarState")]
            return False, "lead clock_abnormal" if "clock_abnormal" in reasons else "lead stale" if "stale" in reasons else "lead unknown"
        if lead < self.max_lead_m:
            return False, "lead"
        return True, "ok"

    def _run(self) -> None:
        from openpilot.cereal import messaging

        try:
            sm = messaging.SubMaster(["carState", "radarState", "modelV2"],
                                     ignore_alive=["carState", "radarState", "modelV2"],
                                     ignore_avg_freq=["carState", "radarState", "modelV2"])
        except Exception as exc:
            with self._lock:
                self._error = f"cereal: {type(exc).__name__}: {exc}"
            LOG.warning("停止线门控不可用（cereal）: %s", self._error)
            return

        while not self._stop.is_set():
            try:
                sm.update(0)
            except Exception:
                pass
            enabled, reason = self._gates(sm)
            with self._lock:
                self._enabled = enabled
                self._state = reason
                self._gate_at = self._clock()
            if reason != self._last_state_log:
                self._last_state_log = reason
                LOG.info("停止线门控 state=%s speed=%s lead=%s", reason,
                         None if self._speed_mps is None else round(self._speed_mps, 2),
                         None if self._lead_drel is None else round(self._lead_drel, 1))
            _write_json(self.gate_path, {
                "enabled": enabled,
                "speed": self._speed_mps,
                "lead": self._lead_drel,
                "at": self._gate_at, "context": self._context,
                "context_at": self._context_at, "reason": reason,
            })
            self._ensure_worker()
            self._stop.wait(self.period_s)
