#!/usr/bin/env python3
"""Normalized CPlink 7714 navigation state with per-stream freshness.

The app sends one ``item_update`` per JSON stream on its own WebSocket.  This module is
the single place that decides whether a stream is still fresh; the M2 publisher reads
only fresh values and never renews an old TBT/speed just because another stream arrived.
"""
from __future__ import annotations

import math
import threading
import time
from typing import Any

# Seconds.  The app's manifest advertises stale_timeout_ms=10000, but guidance/lane/speed
# are useless long before that; vehicle/route/status get a little more slack.
TTL = {
    "guidance_current": 3.0,
    "guidance_next": 3.0,
    "lane_current": 3.0,
    "lane_ahead": 3.0,
    "speed": 3.0,
    "traffic_signal": 3.0,
    "crossroad": 3.0,
    "vehicle": 5.0,
    "route": 5.0,
    "navigation_status": 10.0,
    "app_status": 10.0,
    "camera_state": 5.0,
    "composition_state": 5.0,
}

NAV_STREAMS = ("guidance_current", "guidance_next", "lane_current", "lane_ahead", "route", "crossroad")


def navigation_requested(status: Any) -> bool:
    return (isinstance(status, dict) and status.get("guidance_active") is True
            and status.get("route_present") is True)


def guidance_allowed(status: Any) -> bool:
    if not navigation_requested(status):
        return False
    if "off_route" in status:
        return status["off_route"] is False
    # The phone's guiding schema omits off_route. Accept only that explicit mode;
    # unknown modes and an explicitly unknown/true off_route remain blocked.
    return status.get("mode") == "guiding"


class NaviState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._streams: dict[str, dict[str, Any]] = {}
        self._session: str | None = None
        self._owners: dict[str, object] = {}
        self._sequences: dict[str, int] = {}
        self._rejections: dict[str, str] = {}
        self._generation = 0

    def start_session(self, session: str) -> None:
        with self._lock:
            self._session = session
            self._streams.clear()
            self._owners.clear()
            self._sequences.clear()
            self._rejections.clear()
            self._generation += 1

    def end_session(self, session: str) -> None:
        with self._lock:
            if session == self._session:
                self._session = None
                self._streams.clear()
                self._owners.clear()
                self._sequences.clear()
                self._rejections.clear()
                self._generation += 1

    def open_stream(self, name: str, session: str, owner: object) -> bool:
        with self._lock:
            if session != self._session:
                return False
            self._owners[name] = owner
            self._streams.pop(name, None)
            if name != "traffic_signal":
                self._sequences.pop(name, None)
            if name in ("navigation_status", "guidance_current"):
                self._clear_navigation()
            return True

    def close_stream(self, name: str, session: str, owner: object) -> None:
        with self._lock:
            if session == self._session and self._owners.get(name) is owner:
                self._owners.pop(name, None)
                self._streams.pop(name, None)
                if name in ("navigation_status", "guidance_current"):
                    self._clear_navigation()

    def _clear_navigation(self) -> None:
        # Caller holds the lock. A route/status transition cannot resurrect old TBT.
        for name in NAV_STREAMS:
            self._streams.pop(name, None)

    def _fresh(self, name: str, now: float) -> Any | None:
        item = self._streams.get(name)
        if item is not None and self._fresh_reason(name, item, now) == "ok":
            return item["value"]
        return None

    @staticmethod
    def _fresh_reason(name: str, item: dict, now: float) -> str:
        age = now - item["recv_mono"]
        if not math.isfinite(age) or age < 0.0:
            return "clock_abnormal"
        return "stale" if age > TTL.get(name, 3.0) else "ok"

    def update(self, name: str, present: bool, value: Any, source_ts: int | None,
               sequence: int | None = None, *, session: str | None = None,
               owner: object | None = None) -> bool:
        now = time.monotonic()
        with self._lock:
            if owner is not None and (session != self._session or self._owners.get(name) is not owner):
                return False
            if session is not None and session != self._session:
                return False
            if not math.isfinite(now):
                self._rejections[name] = "clock_abnormal"
                return False
            if sequence is not None:
                if type(sequence) is not int or sequence < 0:
                    self._rejections[name] = "invalid_sequence"
                    return False
                previous = self._sequences.get(name)
                if previous is not None and sequence <= previous:
                    self._rejections[name] = "duplicate" if sequence == previous else "out_of_order"
                    return False
                self._sequences[name] = sequence
            self._rejections.pop(name, None)
            if name == "navigation_status":
                old_allowed = guidance_allowed(self._fresh(name, now))
                if not old_allowed or not present or not guidance_allowed(value):
                    self._clear_navigation()
            if not present or value is None:
                self._streams.pop(name, None)
                if name == "guidance_current":
                    self._clear_navigation()
                return True
            self._streams[name] = {
                "value": value,
                "present": True,
                "source_ts": source_ts,
                "recv_mono": now,
                "sequence": sequence,
            }
            return True

    def navigation_snapshot(self) -> dict[str, Any]:
        """Read related streams atomically, with explicit status authorizing guidance."""
        now = time.monotonic()
        with self._lock:
            if not guidance_allowed(self._fresh("navigation_status", now)):
                self._clear_navigation()
            return {name: item["value"] for name, item in self._streams.items()
                    if self._fresh_reason(name, item, now) == "ok"}

    def get(self, name: str) -> Any | None:
        now = time.monotonic()
        with self._lock:
            item = self._streams.get(name)
            if item is None:
                return None
            if self._fresh_reason(name, item, now) != "ok":
                return None
            return item["value"]

    def age(self, name: str) -> float | None:
        now = time.monotonic()
        with self._lock:
            item = self._streams.get(name)
            return None if item is None else max(0.0, now - item["recv_mono"])

    def summary(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            return {
                name: {
                    "age_s": round(max(0.0, now - item["recv_mono"]), 2),
                    "fresh": self._fresh_reason(name, item, now) == "ok",
                    "seq": item.get("sequence"),
                    "source_ts": item.get("source_ts"),
                    "recv_mono": item["recv_mono"],
                    "session": self._session or "",
                    "generation": self._generation,
                    "reason": self._fresh_reason(name, item, now),
                    "last_rejection": self._rejections.get(name, ""),
                }
                for name, item in self._streams.items()
            }

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            return {
                name: item["value"]
                for name, item in self._streams.items()
                if self._fresh_reason(name, item, now) == "ok"
            }

    def record(self, name: str) -> dict[str, Any]:
        """Read one value and its evidence atomically across session changes."""
        now = time.monotonic()
        with self._lock:
            item = self._streams.get(name)
            reason = "no_input" if item is None else self._fresh_reason(name, item, now)
            return {
                "value": item["value"] if item is not None and reason == "ok" else None,
                "fresh": reason == "ok", "reason": reason,
                "session": self._session or "", "generation": self._generation,
                "seq": None if item is None else item.get("sequence"),
                "source_ts": None if item is None else item.get("source_ts"),
                "recv_mono": None if item is None else item["recv_mono"],
                "last_rejection": self._rejections.get(name, ""),
            }
