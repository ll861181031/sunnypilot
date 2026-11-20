#!/usr/bin/env python3
"""Fake CPlink phone for the bench: 7706 heartbeat, 7714 navi, 7000 compact, 7711 lamps.

It talks to the shim on the bench ports (127.0.0.1).  Every frame it decodes is
recorded, so the control page can show whether the whole chain is actually alive.
"""
from __future__ import annotations

import asyncio
import json
import logging
import socket
import struct
import time

from bench_state import BenchState
from compact_codec import decode_frames

LOG = logging.getLogger("cplink_shim.bench.app")

JSON_STREAMS = [
    "guidance_current", "guidance_next", "lane_current", "road_status",
    "speed", "vehicle", "route", "navigation_status", "traffic_signal",
]
# The shim's catalog uses navigation_status/app_status/camera_state/composition_state;
# only open the ones the fake publisher actually fills.
ACTIVE_STREAMS = [
    "guidance_current", "guidance_next", "lane_current", "speed",
    "vehicle", "route", "navigation_status", "traffic_signal",
]


class BenchApp:
    def __init__(self, state: BenchState, http_port: int, navi_port: int,
                 cmd_port: int, lamp_port: int) -> None:
        self.state = state
        self.http_port = http_port
        self.navi_port = navi_port
        self.cmd_port = cmd_port
        self.lamp_port = lamp_port
        self._tasks: list[asyncio.Task] = []
        self._status = {
            "heartbeat": {"sent": 0, "error": ""},
            "navi": {"connected": False, "session": "", "sent": 0, "error": "",
                     "streams": []},
            "compact": {"connected": False, "frames": 0, "error": "",
                        "services": [], "seen": [], "last": {}},
            "lamp": {"connected": False, "frames": 0, "error": "", "last": {}},
            "video": "skipped (no stack in bench)",
        }
        self._carrot_index = 0

    def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._tasks = [
            loop.create_task(self._heartbeat_loop(), name="bench-app-udp"),
            loop.create_task(self._navi_loop(), name="bench-app-navi"),
            loop.create_task(self._compact_loop(), name="bench-app-compact"),
            loop.create_task(self._lamp_loop(), name="bench-app-lamp"),
        ]
        LOG.info("bench app started (http=%d navi=%d cmd=%d lamp=%d)",
                 self.http_port, self.navi_port, self.cmd_port, self.lamp_port)

    async def stop(self) -> None:
        tasks = list(self._tasks)
        self._tasks = []
        for task in tasks:
            task.cancel()
        if tasks:
            # aiohttp teardown can ignore cancellation while closing a socket; do not
            # let that hold the whole shim open on SIGTERM.
            await asyncio.wait(tasks, timeout=2.0)

    def status(self) -> dict:
        return json.loads(json.dumps(self._status))

    # ── 7706 ──
    async def _heartbeat_loop(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setblocking(False)
        try:
            while True:
                self._carrot_index = (self._carrot_index + 1) & 0x7FFFFFFF
                packet = {
                    "carrotIndex": self._carrot_index,
                    "epochTime": int(time.time()),
                    "timezone": "Asia/Shanghai",
                }
                try:
                    sock.sendto(json.dumps(packet).encode(), ("127.0.0.1", self.cmd_port))
                    self._status["heartbeat"]["sent"] += 1
                    self._status["heartbeat"]["error"] = ""
                except OSError as exc:
                    self._status["heartbeat"]["error"] = str(exc)
                await asyncio.sleep(1.0)
        finally:
            sock.close()

    # ── 7714 ──
    def _guidance(self, current: bool) -> dict | None:
        s = self.state.snapshot()
        if not s["nav_active"]:
            return None
        if current:
            return {
                "distance_m": int(s["nav_distance_m"]),
                "turn_type": int(s["nav_turn_type"]),
                "main_text": s["nav_text"],
                "near_direction": "",
                "far_direction": "",
                "road_name": s["nav_road"],
                "time_sec": 30,
            }
        if int(s["next_turn_type"]) < 0:
            return None
        return {
            "distance_m": int(s["next_distance_m"]),
            "turn_type": int(s["next_turn_type"]),
            "main_text": "Next",
            "near_direction": "",
            "far_direction": "",
            "road_name": s["nav_road"],
            "time_sec": 120,
        }

    def _value(self, name: str):
        s = self.state.snapshot()
        if name in ("guidance_current", "guidance_next"):
            return self._guidance(name == "guidance_current")
        if name == "lane_current":
            if not s["nav_active"] or int(s["lane_count"]) <= 0:
                return None
            count = max(1, int(s["lane_count"]))
            turn_info = list(s["lane_turn_info"])[:count]
            if len(turn_info) < count:
                turn_info += [51] * (count - len(turn_info))
            return {
                "count": count,
                "distance_m": int(s["nav_distance_m"]),
                "visible": True,
                "lane_play": False,
                "current_lane": int(s["current_lane"]),
                "turn_code": int(s["nav_turn_type"]),
                "turn_info": turn_info,
                "road_category": int(s["road_category"]),
                "voice_code": 0,
                "lane_source": "bench",
            }
        if name == "speed":
            if not s["nav_active"]:
                return None
            return {
                "current_kph": float(s["speed_kph"]),
                "road_limit_kph": int(s["road_limit_kph"]),
                "section": {"active": False},
            }
        if name == "vehicle":
            return {
                "lat": float(s["gps_lat"]),
                "lon": float(s["gps_lon"]),
                "heading_deg": float(s["gps_bearing"]),
                "speed_kph": float(s["speed_kph"]),
                "road_name": s["nav_road"],
                "virtual_gps": True,
            }
        if name == "route":
            if not s["nav_active"]:
                return None
            return {
                "remaining_distance_m": 1000,
                "remaining_time_sec": 90,
            }
        if name == "navigation_status":
            return {
                "mode": "guide" if s["nav_active"] else "idle",
                "guidance_active": bool(s["nav_active"]),
                "off_route": False,
                "route_present": bool(s["nav_active"]),
            }
        if name == "traffic_signal":
            color = int(s["traffic_color"])
            if color not in (1, 2, 3):
                return None
            lamp = "red" if color == 1 else "green" if color == 2 else "yellow"
            return {
                "visible": True,
                "distance_m": int(s["signal_distance_m"]),
                "source": "navigation",
                "lights": {
                    lamp: {"valid": True, "on": True, "remain_sec": 12},
                },
            }
        return None

    async def _navi_loop(self) -> None:
        import aiohttp

        control = (f"ws://127.0.0.1:{self.navi_port}"
                   "/api/navi/ws/v2/control/amap_auto_1.0")
        while True:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(control, heartbeat=15) as ctrl:
                        await ctrl.send_str(json.dumps({
                            "type": "requirements_query", "protocol_version": 2,
                            "timestamp_ms": int(time.time() * 1000),
                            "app_version": "amap_auto_1.0", "catalog_revision": 1,
                            "limits": {"max_binary_frame_bytes": 0,
                                       "max_total_bitrate_kbps": 12000},
                            "streams": [],
                        }))
                        manifest = json.loads((await ctrl.receive()).data)
                        if manifest.get("type") != "subscription_manifest":
                            raise RuntimeError(f"bad manifest: {manifest.get('type')}")
                        sid = manifest["session_id"]
                        revision = manifest["revision"]
                        handles = {
                            item["name"]: item["stream_handle"]
                            for item in manifest["streams"] if item["kind"] == "json"
                        }
                        await ctrl.send_str(json.dumps({
                            "type": "manifest_applied", "protocol_version": 2,
                            "timestamp_ms": int(time.time() * 1000),
                            "session_id": sid, "revision": revision,
                            "effective_config": [],
                        }))
                        opened = []
                        for name in ACTIVE_STREAMS:
                            if name not in handles:
                                continue
                            ws = await session.ws_connect(
                                f"ws://127.0.0.1:{self.navi_port}"
                                f"/api/navi/ws/v2/json/{sid}/{name}")
                            opened.append((name, ws, handles[name]))
                        self._status["navi"].update({
                            "connected": True, "session": sid, "error": "",
                            "streams": [name for name, _, _ in opened],
                        })
                        sequences = {name: 0 for name, _, _ in opened}
                        try:
                            while True:
                                for name, ws, handle in opened:
                                    value = self._value(name)
                                    sequences[name] += 1
                                    now = int(time.time() * 1000)
                                    envelope = {
                                        "type": "item_update", "protocol_version": 2,
                                        "session_id": sid, "kind": "json", "name": name,
                                        "manifest_revision": revision,
                                        "stream_handle": handle, "schema_version": 1,
                                        "sequence": sequences[name],
                                        "source_timestamp_ms": now, "sent_at_ms": now,
                                        "present": value is not None,
                                        "value": value if value is not None else None,
                                    }
                                    if value is None:
                                        envelope["reason"] = "bench_idle"
                                    await ws.send_str(json.dumps(envelope))
                                    self._status["navi"]["sent"] += 1
                                await asyncio.sleep(0.5)
                        finally:
                            for _, ws, _ in opened:
                                await ws.close()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._status["navi"].update({
                    "connected": False, "error": f"{type(exc).__name__}: {exc}"})
                LOG.debug("bench navi reconnect: %s", exc)
                await asyncio.sleep(2.0)

    # ── 7000 compact ──
    async def _compact_loop(self) -> None:
        import aiohttp

        url = (f"ws://127.0.0.1:{self.http_port}/ws/compact_state"
               "?services=carState")
        while True:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(url) as ws:
                        hello = await asyncio.wait_for(ws.receive(), 5)
                        if hello.type != aiohttp.WSMsgType.TEXT:
                            raise RuntimeError("compact hello is not text")
                        payload = json.loads(hello.data)
                        if payload.get("mode") != "carrot-state-v1":
                            raise RuntimeError("bad compact hello mode")
                        self._status["compact"].update({
                            "connected": True, "error": "",
                            "hello_services": len(payload.get("services", [])),
                        })
                        async for msg in ws:
                            if msg.type != aiohttp.WSMsgType.BINARY:
                                continue
                            frames = decode_frames(msg.data)
                            self._status["compact"]["frames"] += len(frames)
                            self._status["compact"]["services"] = [
                                frame["name"] for frame in frames]
                            seen = set(self._status["compact"].get("seen", []))
                            seen.update(frame["name"] for frame in frames)
                            self._status["compact"]["seen"] = sorted(seen)
                            last = self._status["compact"]["last"]
                            for frame in frames:
                                last[frame["name"]] = {
                                    "sequence": frame["sequence"],
                                    "values": {
                                        key: value for key, value in frame["values"].items()
                                        if key in ("vEgo", "vCruiseCluster", "frameId",
                                                   "enabled", "speedLimit", "roadLimitKph",
                                                   "count", "present", "mainText")
                                    },
                                }
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._status["compact"].update({
                    "connected": False, "error": f"{type(exc).__name__}: {exc}"})
                await asyncio.sleep(2.0)

    # ── 7711 ──
    async def _lamp_loop(self) -> None:
        while True:
            try:
                reader, writer = await asyncio.open_connection(
                    "127.0.0.1", self.lamp_port)
                self._status["lamp"].update({"connected": True, "error": ""})
                try:
                    while True:
                        head = await reader.readexactly(4)
                        length = struct.unpack(">I", head)[0]
                        if length <= 0 or length > 65536:
                            raise RuntimeError(f"bad 7711 length {length}")
                        payload = await reader.readexactly(length)
                        data = json.loads(payload)
                        self._status["lamp"]["frames"] += 1
                        self._status["lamp"]["last"] = {
                            "sequence": data.get("sequence"),
                            "color": data.get("data", {}).get("carState", {}).get(
                                "trafficLightColor"),
                            "stopLineDist": data.get("data", {}).get("carState", {}).get(
                                "stopLineDist"),
                        }
                finally:
                    writer.close()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._status["lamp"].update({
                    "connected": False, "error": f"{type(exc).__name__}: {exc}"})
                await asyncio.sleep(2.0)
