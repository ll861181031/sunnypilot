#!/usr/bin/env python3
"""Hand fresh CPlink navigation to sunnypilot's native publishers.

Two source mailboxes are refreshed:

* ``navInstruction`` at 5 Hz (modeld's ``INSTRUCTION_TIMEOUT`` is 0.5 s, so 2 Hz would
  be dropped every other frame).  It also feeds the optional turn-desire path.
* ``liveMapDataSP`` at 2 Hz for the existing SpeedLimitResolver/speed-limit assist.

A ``/tmp/cplink_navi_source`` lease and atomic Cap'n Proto mailboxes carry the phone
inputs to navd/mapd. Those processes remain the sole msgq publishers throughout source
changes; creating another PubMaster would invalidate their existing sockets.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

from navi_state import NaviState, navigation_requested

LOG = logging.getLogger("cplink_shim.navi")

GATE_PATH = Path("/tmp/cplink_navi_source")
RELEASE_DELAY_S = 0.5

# CP turn code -> (Valhalla maneuver type, modifier).  Unknown/fork codes are not mapped:
# sending a fork code as a turn is worse than sending nothing.
CP_TURNS = {
    0: ("continue", "straight"),
    9: ("continue", "straight"),
    12: ("turn", "left"),
    13: ("turn", "right"),
    14: ("turn", "uturn"),
    16: ("turn", "sharp left"),
    19: ("turn", "sharp right"),
    51: ("continue", "straight"),
    101: ("off ramp", "right"),
    102: ("off ramp", "left"),
    131: ("roundabout", "straight"),
    132: ("exit roundabout", "straight"),
    133: ("roundabout", "right"),
    139: ("roundabout", "left"),
    142: ("roundabout", "straight"),
    201: ("arrive", "straight"),
}

DIRECTION_BY_CP = {
    12: "left", 16: "left", 139: "left",
    13: "right", 19: "right", 133: "right",
    0: "straight", 9: "straight", 51: "straight", 142: "straight",
}


def _num(value, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if number == number and abs(number) != float("inf") else default


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def turn_for_code(code) -> tuple[str, str] | None:
    try:
        return CP_TURNS.get(int(code))
    except (TypeError, ValueError, OverflowError):
        return None


def direction_for_code(code) -> str:
    try:
        return DIRECTION_BY_CP.get(int(code), "none")
    except (TypeError, ValueError, OverflowError):
        return "none"


class NaviPublisher:
    def __init__(self, state: NaviState, gate_path: str | Path | None = None) -> None:
        self.state = state
        self.gate_path = Path(gate_path) if gate_path else GATE_PATH
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pm = None
        self._last_empty_instruction = 0.0
        self._last_gate = 0.0
        self._gate_content = ""
        self._owns_nav = False
        self._owns_speed = False
        self._nav_release_at: float | None = None
        self._speed_release_at: float | None = None
        self._last_instruction = 0.0
        self._last_map = 0.0
        self._instruction_valid = False
        self.available = False
        self.last_error = ""

    def start(self) -> bool:
        try:
            from openpilot.cereal import messaging
            from openpilot.selfdrive.navigation.cplink import CplinkMailbox
            self._pm = CplinkMailbox(self.gate_path)
            self._messaging = messaging
        except Exception as exc:
            self.last_error = f"navigation mailbox unavailable: {exc}"
            LOG.warning("navi publisher unavailable: %s", self.last_error)
            return False
        self.available = True
        self._thread = threading.Thread(target=self._run, name="navi-publisher", daemon=True)
        self._thread.start()
        LOG.info("navi mailbox started (5 Hz navInstruction, 2 Hz liveMapDataSP)")
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            if self._thread.is_alive():
                return  # A still-running publisher must retain its ownership lease.
            self._thread = None
        try:
            if self._pm is not None and self._gate_content:
                if self._owns_nav:
                    self._last_empty_instruction = 0.0
                    self._publish_empty_instruction(time.monotonic())
                if self._owns_speed:
                    self._pm.send("liveMapDataSP", self._map_data(None, None, None))
        finally:
            self._release_gate()

    # ── gate file ──
    def _touch_gate(self) -> bool:
        content = "nav+speed" if self._owns_nav and self._owns_speed else "nav" if self._owns_nav else "speed"
        now = time.monotonic()
        if content == self._gate_content and now - self._last_gate < 1.0 and self.gate_path.exists():
            return True
        try:
            # Readers must never observe an empty/truncated ownership marker.
            temporary = self.gate_path.with_name(f".{self.gate_path.name}.{os.getpid()}.tmp")
            temporary.write_text(content, encoding="utf-8")
            os.replace(temporary, self.gate_path)
            self._last_gate = now
            self._gate_content = content
            return True
        except OSError as exc:
            LOG.debug("gate touch failed: %s", exc)
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            return False

    def _release_gate(self) -> None:
        try:
            self.gate_path.unlink()
        except OSError:
            pass
        self._gate_content = ""
        self._last_gate = 0.0

    # ── building ──
    def _instruction(self, guidance: dict, next_guidance: dict | None,
                     lane: dict | None, speed: dict | None, route: dict | None):
        messaging = self._messaging
        msg = messaging.new_message("navInstruction", valid=True)
        instruction = msg.navInstruction
        code = guidance.get("turn_type", -1)
        turn = turn_for_code(code)
        instruction.maneuverPrimaryText = str(guidance.get("main_text", ""))
        instruction.maneuverSecondaryText = ""
        instruction.maneuverType = turn[0] if turn else ""
        instruction.maneuverModifier = turn[1] if turn else ""
        instruction.maneuverDistance = _num(guidance.get("distance_m", 0.0))
        remaining = _num(route.get("remain_distance_m", route.get("remaining_distance_m", 0.0))) if route else 0.0
        instruction.distanceRemaining = remaining
        maneuvers = []
        if turn:
            maneuvers.append({
                "distance": _num(guidance.get("distance_m", 0.0)),
                "type": turn[0],
                "modifier": turn[1],
            })
        if next_guidance:
            next_turn = turn_for_code(next_guidance.get("turn_type", -1))
            if next_turn:
                maneuvers.append({
                    "distance": _num(next_guidance.get("distance_m", 0.0)),
                    "type": next_turn[0],
                    "modifier": next_turn[1],
                })
        instruction.allManeuvers = maneuvers
        limit_kph = _num((speed or {}).get("road_limit_kph", 0.0))
        instruction.speedLimit = limit_kph / 3.6 if limit_kph > 0 else 0.0
        lanes = self._lanes(messaging, instruction, lane, code)
        if lanes:
            instruction.lanes = lanes
        return msg

    @staticmethod
    def _lanes(messaging, instruction, lane: dict | None, current_code) -> list:
        if not lane or lane.get("visible") is False:
            return []
        codes = lane.get("turn_info")
        if not isinstance(codes, list) or not codes:
            return []
        count = lane.get("count", len(codes))
        if type(count) is not int or count != len(codes):
            return []
        directions = [direction_for_code(code) for code in codes]
        current_lane = lane.get("current_lane")
        active = current_lane - 1 if type(current_lane) is int else -1
        if not 0 <= active < len(directions):
            active = -1
        return [{
            "directions": [direction],
            "active": index == active and direction != "none",
            "activeDirection": direction if index == active else "none",
        } for index, direction in enumerate(directions)]

    def _map_data(self, speed: dict | None, vehicle: dict | None,
                  guidance: dict | None):
        messaging = self._messaging
        msg = messaging.new_message("liveMapDataSP", valid=speed is not None)
        data = msg.liveMapDataSP
        road = _num((speed or {}).get("road_limit_kph", 0.0))
        data.speedLimitValid = road > 0
        data.speedLimit = road / 3.6 if road > 0 else 0.0
        section = (speed or {}).get("section") or {}
        ahead = 0.0
        ahead_distance = 0.0
        if isinstance(section, dict) and section.get("active"):
            ahead = _num(section.get("speed_limit_kph", 0.0))
            ahead_distance = _num(section.get("remaining_distance_m", 0.0))
        data.speedLimitAheadValid = ahead > 0
        data.speedLimitAhead = ahead / 3.6 if ahead > 0 else 0.0
        data.speedLimitAheadDistance = ahead_distance
        data.roadName = str((vehicle or {}).get("road_name")
                            or (guidance or {}).get("road_name") or "")
        return msg

    def _publish_empty_instruction(self, now: float) -> None:
        if not self._instruction_valid and now - self._last_empty_instruction < 0.2:
            return
        msg = self._messaging.new_message("navInstruction", valid=False)
        msg.navInstruction.allManeuvers = []
        msg.navInstruction.maneuverDistance = 0.0
        msg.navInstruction.distanceRemaining = 0.0
        self._pm.send("navInstruction", msg)
        self._last_empty_instruction = now
        self._instruction_valid = False

    def step(self, now: float) -> None:
        snapshot = self.state.navigation_snapshot()
        guidance = snapshot.get("guidance_current")
        speed = snapshot.get("speed")
        wants_nav = navigation_requested(snapshot.get("navigation_status"))
        wants_speed = speed is not None

        # Keep the phone source selected briefly while withdrawing an instruction/limit.
        # Once released, stop writing empties and let the native source resume.
        for service, wanted in (("nav", wants_nav), ("speed", wants_speed)):
            owns = getattr(self, f"_owns_{service}")
            deadline = getattr(self, f"_{service}_release_at")
            if wanted:
                setattr(self, f"_owns_{service}", True)
                setattr(self, f"_{service}_release_at", None)
            elif owns and deadline is None:
                setattr(self, f"_{service}_release_at", now + RELEASE_DELAY_S)
            elif owns and now >= deadline:
                setattr(self, f"_owns_{service}", False)
                setattr(self, f"_{service}_release_at", None)

        if self._owns_nav or self._owns_speed:
            if not self._touch_gate():
                return  # Cannot select the phone source: do not refresh its mailbox.
        elif self._gate_content:
            self._release_gate()

        if self._owns_nav:
            if guidance is not None and now - self._last_instruction >= 0.2:
                self._pm.send("navInstruction", self._instruction(
                    guidance, snapshot.get("guidance_next"), snapshot.get("lane_current"),
                    speed, snapshot.get("route")))
                self._last_instruction = now
                self._instruction_valid = True
            elif guidance is None:
                self._publish_empty_instruction(now)
        if self._owns_speed and (speed is None or now - self._last_map >= 0.5):
            self._pm.send("liveMapDataSP", self._map_data(speed, snapshot.get("vehicle"), guidance))
            self._last_map = now

    # ── loop ──
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.step(time.monotonic())
            except Exception as exc:
                self.last_error = str(exc)
                LOG.warning("navi publish failed: %s", exc)
            time.sleep(0.1)
