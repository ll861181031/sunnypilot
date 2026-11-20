#!/usr/bin/env python3
"""Bridge the phone navigation traffic-light stream to the planner.

The CPlink app publishes a ``traffic_signal`` JSON stream over the navi v2
WebSocket (``distance_m`` plus ``lights.{red,yellow,green}.{valid,on,remain_sec}``).
``NaviState`` already stores it with a 3 s TTL.  The planner does not know about
that stream, but it already consumes the ego-light summary fields that ``yolod``
publishes on ``yoloObjects``:

    egoLightState / egoLightFrames / egoLightDistance

This adapter publishes lamp evidence and independent stop-line evidence through
``yoloObjects`` while ``YoloEnabled`` is off.  Missing distance remains explicit;
stale input stops publication.  Intersection/direction relevance is never inferred
from a countdown or a missing signal.
"""
from __future__ import annotations

import json
import logging
import math
import os
import sys
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from collections.abc import Callable

LOG = logging.getLogger("cplink_shim.nav_lamp")

# NaviState expires traffic_signal after 3 s; the planner expires yoloObjects after
# 0.3 s.  The bridge therefore republishes at 10 Hz while the navigation value is
# fresh, and stops as soon as NaviState says it is stale.
PUBLISH_HZ = 10.0
MIN_CONFIRMATIONS = 2
NEAR_DISTANCE_M = 40.0
MAX_DISTANCE_M = 150.0
LIGHT_PRIORITY = ("red", "yellow", "green")
YOLO_GATE_PERIOD_S = 10.0

# The planner subtracts TrafficLightStopOffset (5 m by default) from this value.
# Keep this at 0 unless a drive proves the navigation distance is measured to the
# stop line rather than to the lamp; then this can convert it to lamp distance.
def _distance_bias() -> float:
    try:
        return float(os.getenv("CPLINK_NAV_LAMP_DISTANCE_BIAS_M", "0") or 0.0)
    except ValueError:
        LOG.warning("CPLINK_NAV_LAMP_DISTANCE_BIAS_M is not a number; using 0")
        return 0.0


DISTANCE_BIAS_M = _distance_bias()


@dataclass(frozen=True)
class LampReading:
    state: str
    distance_m: float
    remain_s: float | None
    source: str
    is_stopline: bool = False
    source_session: str = ""
    source_sequence: int = 0
    source_recv_mono: float = 0.0
    source_timestamp_ms: int = 0
    intersection_id: str = ""
    direction: str = ""
    relevant: bool = False
    lamp_confirmations: int = 0
    stopline_confirmations: int = 0
    target_id: str = ""
    camera_frame_id: int = 0
    capture_mono: float = 0.0
    detected_mono: float = 0.0
    gate_reason: str = ""


def _light_on(entry: Any) -> bool:
    """Whether one ``lights.<colour>`` entry is an active, usable lamp."""
    if isinstance(entry, bool):
        return entry
    if not isinstance(entry, dict):
        return False
    if entry.get("valid") is False:
        return False
    return entry.get("on") is True


def map_traffic_signal(value: Any) -> LampReading | None:
    """Map one ``traffic_signal`` value to a planner reading, or None.

    Only an explicitly visible signal with at least one active lamp is usable.
    A missing ``distance_m`` is reported as -1 (state-only); a fabricated range
    would be the one error that can brake the car.  Multiple active lamps are
    resolved in the safe order red > yellow > green: a contradictory payload
    must not release a red stop.
    """
    if not isinstance(value, dict):
        return None
    if value.get("visible") is False:
        return None
    lights = value.get("lights")
    if not isinstance(lights, dict):
        return None

    state = next((name for name in LIGHT_PRIORITY if _light_on(lights.get(name))), None)
    if state is None:
        # "visible but no lamp on" is not a usable state for control: publishing
        # "off" would release a committed stop on a partial payload.
        return None

    raw_distance = value.get("distance_m")
    if raw_distance is None:
        # The shipping CPlink app sends colour + countdown but no distance.  Keep
        # the state (the planner can use it as a stop *reason*) while making the
        # missing range explicit; a fabricated distance must never brake the car.
        distance = -1.0
    else:
        try:
            distance = float(raw_distance) + DISTANCE_BIAS_M
        except (TypeError, ValueError):
            return None
        if not (0.0 < distance <= MAX_DISTANCE_M):
            return None

    entry = lights.get(state)
    remain: float | None = None
    if isinstance(entry, dict) and entry.get("remain_sec") is not None:
        try:
            remain = float(entry["remain_sec"])
            if not math.isfinite(remain) or remain < 0.0:
                remain = None
        except (TypeError, ValueError):
            remain = None

    source = value.get("source")
    intersection_id = value.get("intersection_id")
    direction = value.get("direction")
    intersection_id = intersection_id if isinstance(intersection_id, str) else ""
    direction = direction if direction in ("straight", "left", "right", "uturn") else ""
    return LampReading(state=state, distance_m=distance, remain_s=remain,
                       source=str(source) if source else "navigation",
                       intersection_id=intersection_id, direction=direction,
                       relevant=bool(intersection_id and direction and value.get("relevant") is True))


def params_yolo_gate(period_s: float = YOLO_GATE_PERIOD_S) -> Callable[[], bool]:
    """Return a cached callable telling whether the bridge may publish.

    The bridge must not fight ``yolod`` for the ``yoloObjects`` topic.  While
    ``YoloEnabled`` is true, the YOLO daemon owns the message and this bridge is
    disabled.  Params reads hit disk, so they are rate limited; a failed read is
    treated as "YOLO off" (the bridge stays available) because disabling the only
    light source on a Params error would be the worse failure mode.
    """
    cached = {"value": False, "at": -period_s}

    def enabled() -> bool:
        now = time.monotonic()
        if now - cached["at"] < period_s:
            return not cached["value"]
        cached["at"] = now
        try:
            from openpilot.common.params import Params
            cached["value"] = bool(Params().get_bool("YoloEnabled"))
        except Exception:
            cached["value"] = False
        return not cached["value"]

    return enabled


class CerealLampPublisher:
    """Publish the adapter's reading as a minimal ``yoloObjects`` message."""

    def __init__(self) -> None:
        # shim.py runs with tools/cplink_shim as sys.path[0]; the repo root is
        # needed for openpilot.cereal unless the launcher already exported it.
        root = Path(__file__).resolve().parents[2]
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from openpilot.cereal import messaging

        self._messaging = messaging
        self._pm = None
        self.sent = 0
        self.last_error = ""
        self._last_connect_error_log = 0.0

    def _ensure_publisher(self) -> bool:
        """Create the publisher lazily: the shim may start before the car stack.

        A failed connect is retried on the next publication instead of disabling
        the bridge for the lifetime of the process.  Errors are rate limited so a
        parked car with no msgq does not fill the journal.
        """
        if self._pm is not None:
            return True
        try:
            self._pm = self._messaging.PubMaster(["yoloObjects"])
            return True
        except Exception as exc:
            self.last_error = f"publisher: {type(exc).__name__}: {exc}"
            now = time.monotonic()
            if now - self._last_connect_error_log >= 10.0:
                self._last_connect_error_log = now
                LOG.warning("导航灯态发布器不可用（等待 msgq）: %s", self.last_error)
            return False

    def __call__(self, reading: LampReading, frames: int, frame_id: int) -> bool:
        if not self._ensure_publisher():
            return False
        try:
            message = self._messaging.new_message("yoloObjects")
            message.valid = True
            payload = message.yoloObjects
            payload.frameId = int(frame_id) & 0xFFFFFFFF
            payload.frameWidth = 0
            payload.frameHeight = 0
            payload.inferenceMs = 0.0
            payload.model = "cplink-navi"
            payload.init("classNames", 0)
            payload.init("objects", 0)
            payload.egoLightState = reading.state
            payload.egoLightFrames = int(max(0, min(65535, frames)))
            payload.egoLightDistance = float(reading.distance_m)
            payload.egoLightIsStopLine = bool(reading.is_stopline)
            payload.egoLightSource = "navigation"
            payload.egoLightSession = reading.source_session
            payload.egoLightSequence = int(reading.source_sequence)
            payload.egoLightReceivedMonoTime = int(reading.source_recv_mono * 1e9)
            payload.egoLightIntersectionId = reading.intersection_id
            payload.egoLightDirection = reading.direction
            payload.egoLightRelevant = reading.relevant
            payload.egoLightConfirmations = min(65535, reading.lamp_confirmations)
            payload.stopLineConfirmations = min(65535, reading.stopline_confirmations)
            payload.stopLineTargetId = reading.target_id
            payload.stopLineFrameId = reading.camera_frame_id & 0xFFFFFFFF
            payload.stopLineCaptureMonoTime = int(reading.capture_mono * 1e9)
            payload.stopLineDetectedMonoTime = int(reading.detected_mono * 1e9)
            payload.stopLineReason = reading.gate_reason
            self._pm.send("yoloObjects", message)
            self.sent += 1
            self.last_error = ""
            return True
        except Exception as exc:
            # msgq can disappear when the stack restarts; retry from scratch next time.
            self._pm = None
            self.last_error = f"publish: {type(exc).__name__}: {exc}"
            return False


class NavLampBridge:
    """Turn the navigation stream into planner readings at a fixed cadence.

    Confirmation is counted in *navigation updates*, not in bridge publications:
    the app sends at ~1 Hz and repeating the same value at 10 Hz must not turn two
    seconds of evidence into 0.2 s.  Lamp and camera confirmation counts remain
    independent; the planner applies the threshold for the identified source.
    """

    def __init__(self, navi_state: Any, *,
                 publish: Callable[[LampReading, int, int], bool] | None = None,
                 enabled: Callable[[], bool] | None = None,
                 stopline: Any | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 min_confirmations: int = MIN_CONFIRMATIONS,
                 rate_hz: float = PUBLISH_HZ) -> None:
        self.navi_state = navi_state
        self._publish = publish
        self._stopline = stopline
        self._enabled_fn = enabled or (lambda: True)
        self._enabled = True
        self._clock = clock
        self.min_confirmations = max(1, int(min_confirmations))
        self.period_s = 1.0 / max(0.5, float(rate_hz))

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self._seq: Any = None
        self._context: tuple | None = None
        self._reason = "no_input"
        self._last_state: str | None = None
        self._confirmations = 0
        self._reading: LampReading | None = None
        self._source_reading: LampReading | None = None
        self._reading_at = 0.0
        self._frame_id = 0
        self._published = 0
        self._last_error = ""
        self._last_log: tuple[str, float] | None = None

    # ── readable state for /api/nav_lamp.json and /api/status ──
    def status(self) -> dict[str, Any]:
        with self._lock:
            reading = self._reading
            age = None if reading is None else round(max(0.0, self._clock() - self._reading_at), 2)
            return {
                "enabled": self._enabled,
                "state": reading.state if reading else "",
                "distance_m": (round(reading.distance_m, 2)
                       if reading and reading.distance_m > 0 else None),
                "distance_known": bool(reading and reading.distance_m > 0),
                "remain_s": reading.remain_s if reading else None,
                "source": reading.source if reading else "",
                "is_stopline": bool(reading and reading.is_stopline),
                "stopline": (self._stopline.status() if self._stopline is not None else None),
                "confirmations": self._confirmations,
                "needed": self.min_confirmations,
                "published": self._published,
                "age_s": age,
                "last_error": self._last_error,
                "reason": self._reason,
                "source_session": reading.source_session if reading else "",
                "source_sequence": reading.source_sequence if reading else 0,
                "source_recv_mono": reading.source_recv_mono if reading else 0.0,
                "source_timestamp_ms": reading.source_timestamp_ms if reading else 0,
                "intersection_id": reading.intersection_id if reading else "",
                "direction": reading.direction if reading else "",
                "relevant": bool(reading and reading.relevant),
                "stopline_confirmations": reading.stopline_confirmations if reading else 0,
                "target_id": reading.target_id if reading else "",
            }

    def _reset(self) -> None:
        with self._lock:
            self._seq = None
            self._context = None
            self._last_state = None
            self._confirmations = 0
            self._reading = None
            self._source_reading = None
            self._reading_at = 0.0
            self._last_log = None

    def _summary(self) -> dict[str, Any] | None:
        try:
            summary = self.navi_state.summary()
        except Exception as exc:
            self._last_error = f"summary: {type(exc).__name__}: {exc}"
            return None
        item = summary.get("traffic_signal") if isinstance(summary, dict) else None
        return item if isinstance(item, dict) else None

    def _value(self) -> Any:
        try:
            return self.navi_state.get("traffic_signal")
        except Exception as exc:
            self._last_error = f"get: {type(exc).__name__}: {exc}"
            return None

    def poll_once(self, now: float | None = None) -> LampReading | None:
        """One cycle.  Returns the reading that was published, else None."""
        now = self._clock() if now is None else now
        enabled = False
        try:
            enabled = bool(self._enabled_fn())
        except Exception as exc:
            self._last_error = f"enabled: {type(exc).__name__}: {exc}"
        with self._lock:
            self._enabled = enabled
        if not enabled:
            self._reset()
            return None

        if hasattr(self.navi_state, "record"):
            summary = self.navi_state.record("traffic_signal")
            value = summary.get("value")
        else:
            summary = self._summary()
            value = self._value()
        if summary is None or summary.get("fresh") is not True:
            self._reset()
            self._reason = "no_input" if summary is None else summary.get("reason", "stale")
            return None
        if value is None:
            self._reset()
            self._reason = "no_input"
            return None

        reading = map_traffic_signal(value)
        if reading is None:
            self._reset()
            self._reason = "condition_not_met"
            return None

        recv_mono = summary.get("recv_mono", now)
        if not isinstance(recv_mono, (int, float)) or not math.isfinite(recv_mono) or not 0.0 <= now - recv_mono <= 3.0:
            self._reset()
            self._reason = "clock_abnormal" if not isinstance(recv_mono, (int, float)) or not math.isfinite(recv_mono) or recv_mono > now else "stale"
            return None
        session = str(summary.get("session", ""))
        context = (session, summary.get("generation", 0), reading.intersection_id, reading.direction)
        seq = summary.get("seq")
        if type(seq) is not int or seq < 0:
            self._reset()
            self._reason = "invalid_sequence"
            return None
        if self._context != context:
            self._reset()
            self._context = context
        if self._seq is not None and seq < self._seq:
            self._reason = "out_of_order"
            return None
        if self._seq == seq and self._source_reading is not None:
            # A repeated publication may carry a newer independent camera fix,
            # but it must never replace the lamp evidence or its receive time.
            reading = self._source_reading
        else:
            reading = replace(reading, source_session=session, source_sequence=seq,
                              source_recv_mono=float(recv_mono),
                              source_timestamp_ms=int(summary.get("source_ts") or 0))
            self._source_reading = reading

        # The navigation stream has no distance in the shipping app.  A confirmed
        # low-speed stop line can supply one; it is only used while the nav state
        # itself is red/yellow (the source gates speed, lead and camera too).
        if reading.state in ("red", "yellow") and reading.distance_m <= 0.0 and self._stopline is not None:
            fix = self._stopline.snapshot()
            if fix is not None and fix.context == json.dumps(list(context), separators=(",", ":")):
                reading = replace(reading, distance_m=float(fix.distance_m),
                                  is_stopline=True, stopline_confirmations=fix.confirmations,
                                  target_id=fix.target_id, camera_frame_id=fix.frame_id,
                                  capture_mono=fix.capture_mono, detected_mono=fix.detected_mono,
                                  gate_reason=fix.reason)
            else:
                status = self._stopline.status()
                reason = status.get("worker_reason", "no_stopline") if status.get("enabled", True) else status.get("state", "condition_not_met")
                reading = replace(reading, gate_reason=str(reason))

        with self._lock:
            if self._seq == seq:
                self._reason = "duplicate"
            elif reading.state != self._last_state:
                self._last_state = reading.state
                self._confirmations = 1
                self._reason = "confirming"
            else:
                self._confirmations += 1
                self._reason = "ok"
            self._seq = seq
            reading = replace(reading, lamp_confirmations=self._confirmations)
            self._reading = reading
            self._reading_at = reading.source_recv_mono
            confirmed = self._confirmations >= self.min_confirmations

        if not confirmed:
            return None

        frames = self._confirmations
        with self._lock:
            previous = self._last_log
            log_now = (previous is None or previous[0] != reading.state
                       or abs(previous[1] - reading.distance_m) >= 5.0)
            if log_now:
                self._last_log = (reading.state, reading.distance_m)
        if log_now:
            distance_text = (f"{reading.distance_m:.1f} m" if reading.distance_m > 0
                             else "distance unknown")
            LOG.info("nav lamp %s %s remain=%s source=%s", reading.state,
                     distance_text, reading.remain_s, reading.source)
        with self._lock:
            self._frame_id = (self._frame_id + 1) & 0xFFFFFFFF
            frame_id = self._frame_id
        if self._publish is not None:
            try:
                ok = bool(self._publish(reading, frames, frame_id))
            except Exception as exc:
                ok = False
                self._last_error = f"publish: {type(exc).__name__}: {exc}"
            if ok:
                with self._lock:
                    self._published += 1
                    self._last_error = ""
            else:
                with self._lock:
                    self._last_error = self._last_error or "publish returned false"
        with self._lock:
            self._reading = reading
            self._reading_at = reading.source_recv_mono
        return reading

    def _run(self) -> None:
        next_at = self._clock()
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as exc:
                with self._lock:
                    self._last_error = f"poll: {type(exc).__name__}: {exc}"
            next_at += self.period_s
            delay = next_at - self._clock()
            if delay > 0:
                self._stop.wait(delay)
            else:
                next_at = self._clock()

    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return True
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="nav-lamp", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._reset()
