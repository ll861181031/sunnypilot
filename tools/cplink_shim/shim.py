#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
    TCP 7000   HTTP + WS      /api/heartbeat_status /api/params_bulk /api/param_set
                              /api/egpu/model /api/settings/snapshot /ws/compact_state

    /data/sunnypilot/.venv/bin/python /data/sunnypilot/tools/cplink_shim/shim.py

"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import secrets
import signal
import socket
import struct
import sys
import time
from collections import OrderedDict
from urllib.parse import parse_qs, urlsplit

from bench_page import INDEX_HTML as BENCH_HTML
from compact_codec import ALL_SERVICES as COMPACT_SERVICES, encode_batch, encode_frame
from compact_source import CompactSource
from lamp7711 import TLA_MAP_PATH, LampServer, LampSource
from nav_lamp import CerealLampPublisher, NavLampBridge, params_yolo_gate
try:
    from stopline_source import StopLineSource
except Exception:  # optional: cv2 / VisionIPC may be absent on a plain PC
    StopLineSource = None
from camera_hub import CAMERA_HTML, CameraHub
from navi_publisher import NaviPublisher
from navi_state import NaviState


PROTOCOL_VERSION = 2
CATALOG_REVISION = 1
SCHEMA_VERSION = 1

JSON_NAMES = [
    "vehicle", "guidance_current", "guidance_next", "lane_current",
    "lane_ahead", "speed", "traffic_signal", "crossroad", "route",
    "navigation_status", "app_status", "camera_state", "composition_state",
]
IMAGE_NAMES = [
    "tbt_current_compact", "tbt_current_full", "tbt_next",
    "traffic_signal", "lane_top", "lane_bottom",
    "safety_primary", "safety_secondary", "safety_section",
    "crossroad_minimized", "crossroad_expanded",
    "center_tbt_icon", "center_tbt_text", "center_tbt_fee",
]
RENDER_NAMES = ["map_main"]

CATALOG: list[tuple[str, str]] = (
    [("json", n) for n in JSON_NAMES]
    + [("image", n) for n in IMAGE_NAMES]
    + [("render", n) for n in RENDER_NAMES]
)
STREAM_HANDLE = {(kind, name): i + 1 for i, (kind, name) in enumerate(CATALOG)}
JSON_STREAM_HANDLE = {name: STREAM_HANDLE[("json", name)] for name in JSON_NAMES}

DELIVERY_MODE = "on_change_with_heartbeat"
STREAM_INTERVAL_MS = 1000
STREAM_STALE_TIMEOUT_MS = 10000

# Per-service send cadence for /ws/compact_state.  The app expires each service locally
# (500/750/1500/3000 ms), so these are deliberately faster than the TTLs: a stopped car
# still produces frames, and the UI does not flicker to "stale" while values are constant.
COMPACT_CADENCE = {
    "carState": 0.05,
    "controlsState": 0.05,
    "carControl": 0.05,
    "modelV2": 0.05,
    "longitudinalPlan": 0.10,
    "radarState": 0.10,
    "lateralPlan": 0.10,
    "selfdriveState": 0.50,
    "carrotMan": 0.50,
    "carrotNavi": 0.50,
    "deviceState": 1.00,
    "gpsLocationExternal": 1.00,
}
COMPACT_SOURCE_STALE_S = 0.5

WS_MAX_MESSAGE = 8 * 1024 * 1024
UTF8 = "utf-8"
CT_JSON = "application/json"
CT_TEXT = "text/plain; charset=utf-8"

USER_AGENT_TAG = "cplink_shim/1.0"

LOG = logging.getLogger("cplink_shim")



class Config:
    def __init__(self) -> None:
        self.http_port = int(os.environ.get("CPLINK_HTTP_PORT", "7000"))
        self.navi_port = int(os.environ.get("CPLINK_NAVI_PORT", "7714"))
        self.discovery_port = int(os.environ.get("CPLINK_DISCOVERY_PORT", "7705"))
        self.cmd_port = int(os.environ.get("CPLINK_CMD_PORT", "7706"))
        self.lamp_port = int(os.environ.get("CPLINK_LAMP_PORT", "7711"))
        self.navi_gate = os.environ.get("CPLINK_NAVI_GATE", "/tmp/cplink_navi_source")
        self.video_gate = os.environ.get("CPLINK_VIDEO_GATE", "/tmp/cplink_video")
        self.bench = os.environ.get("CPLINK_BENCH", "") not in ("", "0", "false")
        self.ip = os.environ.get("CPLINK_IP", "").strip()
        self.broadcast_addrs = os.environ.get("CPLINK_BROADCAST_ADDRS", "").strip()
        self.last_7706_file = os.environ.get(
            "CPLINK_LAST_7706_FILE", "/tmp/cplink_shim_7706_last.json")
        self.params_file = os.environ.get(
            "CPLINK_PARAMS_FILE", "/tmp/cplink_shim_params.json")
        self.log_level = os.environ.get("CPLINK_LOG_LEVEL", "INFO").upper()
        self.force_stdlib = os.environ.get("CPLINK_FORCE_STDLIB", "") not in ("", "0", "false")
        self.discovery_interval = float(os.environ.get("CPLINK_DISCOVERY_INTERVAL", "1.0"))


def detect_local_ip(hint: str = "") -> str:
    if hint:
        return hint
    for target in ("192.168.123.1", "10.0.0.1", "8.8.8.8"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.settimeout(0.3)
                s.connect((target, 9))
                ip = s.getsockname()[0]
                if ip and not ip.startswith("127."):
                    return ip
        except OSError:
            continue
    return "192.168.123.70"


def subnet_broadcast(ip: str) -> str:
    parts = ip.split(".")
    if len(parts) == 4:
        return ".".join(parts[:3] + ["255"])
    return "255.255.255.255"



PARAM_DEFAULTS = OrderedDict([
    ("AlwaysLateral", "0"),
    ("ExperimentalMode", "0"),
    ("ShareData", "1"),
    ("SpeedFromPCM", "0"),
    ("EnableSpeedLimit", "0"),
])


class ParamStore:

    def __init__(self, path: str) -> None:
        self.path = path
        self.values: OrderedDict[str, str] = OrderedDict(PARAM_DEFAULTS)
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding=UTF8) as f:
                data = json.load(f)
            if isinstance(data, dict):
                for k, v in data.items():
                    if isinstance(k, str) and isinstance(v, (str, int, float, bool)):
                        self.values[k] = _param_str(v)
                LOG.info("params loaded from %s (%d keys)", self.path, len(self.values))
        except FileNotFoundError:
            LOG.info("params file %s absent, using defaults", self.path)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("params load failed (%s): %s", self.path, exc)

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding=UTF8) as f:
                json.dump(self.values, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("params save failed (%s): %s", self.path, exc)

    def bulk(self, names: list[str]) -> dict:
        out = {}
        for name in names:
            out[name] = self.values.get(name, "0")
        return out

    def set(self, name: str, value) -> None:
        self.values[name] = _param_str(value)
        self._save()


def _param_str(value) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


class ShimState:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.started = time.time()
        self.ip = detect_local_ip(cfg.ip)
        self.transport_name = "?"

        self.params = ParamStore(cfg.params_file)

        self.sessions: dict[str, dict] = {}
        self.manifest_builds = 0
        self.protocol_errors = 0
        self.control_conns = 0
        self.stream_conns = 0
        self.item_updates = 0
        self.navi_state = NaviState()
        self.navi_publisher: NaviPublisher | None = None
        self.nav_lamp: NavLampBridge | None = None
        self.stopline: StopLineSource | None = None
        self.lamp_source: LampSource | None = None
        self.lamp_server: LampServer | None = None
        self.bench_state = None
        self.bench_car = None
        self.bench_app = None
        self.camera_hub: CameraHub | None = None

        self.stream_count = {name: 0 for name in JSON_NAMES}
        self.stream_last: dict[str, dict] = {}
        self.stream_conn_open = {name: 0 for name in JSON_NAMES}

        # 7000 compact_state
        self.compact_conns = 0
        self.compact_clients = 0
        self.compact_source: CompactSource | None = None
        self.compact_seq = {name: 0 for name in COMPACT_SERVICES}
        self.compact_shared: dict = {}
        self.compact_last_sent = {name: 0.0 for name in COMPACT_SERVICES}
        self.compact_frames = 0
        self.compact_batches = 0
        self.compact_last_batch_ms = 0

        # 7706
        self.udp_packets = 0
        self.udp_heartbeats = 0
        self.udp_commands = 0
        self.udp_bad_packets = 0
        self.udp_acks = 0
        self.last_7706: dict | None = None
        self.last_7706_from = ""
        self.last_7706_ms = 0

        # 7705
        self.broadcast_sent = 0
        self.broadcast_raw = ""

    def new_session(self, sid: str, app_version: str, query: dict) -> dict:
        now = time.time()
        for previous in self.sessions.values():
            previous["control_open"] = False
        self.navi_state.start_session(sid)
        session = {
            "session_id": sid,
            "app_version": app_version,
            "created": now,
            "revision": CATALOG_REVISION,
            "applied": False,
            "control_remote": "",
            "control_open": True,
            "query_limits": query.get("limits") if isinstance(query, dict) else None,
            "query_streams": len(query.get("streams") or []) if isinstance(query, dict) else 0,
            "streams_requested": [],
        }
        self.sessions[sid] = session
        if len(self.sessions) > 32:
            for old in sorted(self.sessions, key=lambda k: self.sessions[k]["created"])[:-32]:
                self.sessions.pop(old, None)
        return session

    def drop_session(self, sid: str) -> None:
        self.navi_state.end_session(sid)
        session = self.sessions.get(sid)
        if session is not None:
            session["control_open"] = False

    # ── 7706 ──
    def record_7706(self, data: bytes, addr, transport_peer: str) -> None:
        now_ms = int(time.time() * 1000)
        raw = data.decode(UTF8, "replace").strip()
        record = {
            "received_ms": now_ms,
            "received_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now_ms / 1000)),
            "from": addr,
            "bytes": len(data),
            "raw": raw[:4096],
        }
        try:
            packet = json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            self.udp_bad_packets += 1
            record["parse_error"] = str(exc)
            LOG.warning("7706 非 JSON 包 from=%s len=%d err=%s raw=%r",
                        addr, len(data), exc, raw[:200])
        else:
            self.udp_packets += 1
            if isinstance(packet, dict):
                record["packet"] = packet
                self._classify_7706(packet, addr)
            else:
                self.udp_bad_packets += 1
                LOG.warning("7706 顶层不是对象 from=%s raw=%r", addr, raw[:200])
                record["packet"] = packet

        self.last_7706 = record
        self.last_7706_from = addr
        self.last_7706_ms = now_ms
        self.touch_video_gate()
        self._write_last_7706(record)

    def touch_video_gate(self) -> None:
        """Tell the manager that a CPlink phone is present (starts webrtcd/encoderd)."""
        try:
            with open(self.cfg.video_gate, "w", encoding="utf-8") as gate:
                gate.write(str(int(time.time())))
        except OSError as exc:
            LOG.debug("video gate touch failed: %s", exc)

    def _classify_7706(self, packet: dict, addr: str) -> None:
        carrot_index = packet.get("carrotIndex")
        epoch = packet.get("epochTime")
        tz = packet.get("timezone")
        cmd = packet.get("carrotCmd")
        cmd_index = packet.get("carrotCmdIndex")
        cmd_arg = packet.get("carrotArg")
        goal_x = packet.get("goalPosX")
        goal_y = packet.get("goalPosY")
        goal_name = packet.get("szGoalName")

        has_cmd = isinstance(cmd, str) and cmd.strip() != ""
        has_goal = any(v is not None for v in (goal_x, goal_y, goal_name))

        if has_cmd:
            self.udp_commands += 1
            LOG.info("7706 COMMAND from=%s carrotIndex=%s carrotCmdIndex=%s carrotCmd=%r carrotArg=%r",
                     addr, carrot_index, cmd_index, cmd, cmd_arg)
        if has_goal:
            self.udp_commands += 1
            LOG.info("7706 DESTINATION from=%s goalPosX=%r goalPosY=%r szGoalName=%r",
                     addr, goal_x, goal_y, goal_name)
        if not has_cmd and not has_goal and "carrotCmd" not in packet:
            self.udp_heartbeats += 1
            LOG.debug("7706 heartbeat from=%s carrotIndex=%s epochTime=%s tz=%s",
                      addr, carrot_index, epoch, tz)
            return

        if not has_cmd:
            LOG.info("7706 空命令包 from=%s carrotIndex=%s carrotCmdIndex=%s carrotCmd=%r",
                     addr, carrot_index, cmd_index, cmd)

    def _write_last_7706(self, record: dict) -> None:
        path = self.cfg.last_7706_file
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding=UTF8) as f:
                json.dump(record, f, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("写 %s 失败: %s", path, exc)

    def compact_payload(self, per_conn: dict | None = None) -> list[bytes]:
        """One CVB1 batch of the services that are due this tick (empty if none).

        ``per_conn`` carries this connection's own cadence timers and sequence
        numbers.  Sharing them across clients makes the second client skip every
        batch (its "last sent" was just updated by the first one), which shows up
        in the app as truncated / out-of-order compact samples.
        """
        source = self.compact_source
        if source is None:
            return []
        snapshot, updated = source.snapshot()
        if not snapshot:
            return []
        now = time.monotonic()
        if now - updated > COMPACT_SOURCE_STALE_S:
            return []
        conn = per_conn if per_conn is not None else self.compact_shared
        last_sent = conn.setdefault("last_sent", {})
        seqs = conn.setdefault("seq", {})
        frames: list[bytes] = []
        for name in COMPACT_SERVICES:
            values = snapshot.get(name)
            period = COMPACT_CADENCE.get(name)
            if values is None or period is None:
                continue
            if now - last_sent.get(name, 0.0) < period:
                continue
            seq = (seqs.get(name, 0) + 1) & 0xFFFF
            seqs[name] = seq
            # global copy is only for /status display
            self.compact_seq[name] = seq
            frames.append(encode_frame(name, seq, values))
            last_sent[name] = now
        if not frames:
            return []
        self.compact_frames += len(frames)
        self.compact_batches += 1
        self.compact_last_batch_ms = int(now * 1000)
        return [encode_batch(frames)]

    def bench_status(self) -> dict:
        if self.bench_state is None:
            return {"ok": False, "error": "bench mode is not enabled"}
        color, stop_line = self.lamp_source.snapshot() if self.lamp_source else (0, None)
        return {
            "ok": True,
            "state": self.bench_state.snapshot(),
            "car": {
                "available": bool(self.bench_car and self.bench_car.available),
                "published": self.bench_car.published if self.bench_car else 0,
                "error": self.bench_car.last_error if self.bench_car else "",
            },
            "app": self.bench_app.status() if self.bench_app else {},
            "shim": {
                "compact": {
                    "clients": self.compact_clients,
                    "frames": self.compact_frames,
                    "batches": self.compact_batches,
                    "sequence": dict(self.compact_seq),
                },
                "navi": {
                    "publisher": bool(self.navi_publisher and self.navi_publisher.available),
                    "streams": self.navi_state.summary(),
                },
                "lamp": {"color": color, "stop_line_m": stop_line},
            },
        }

    def status(self) -> dict:
        now = time.time()
        return {
            "shim": True,
            "transport": self.transport_name,
            "pid": os.getpid(),
            "ip": self.ip,
            "uptime": round(now - self.started, 1),
            "ports": {
                "udp_discovery": self.cfg.discovery_port,
                "udp_command": self.cfg.cmd_port,
                "http_ws": self.cfg.http_port,
                "navi_ws": self.cfg.navi_port,
            },
            "broadcast": {
                "sent": self.broadcast_sent,
                "targets": broadcast_targets(self.cfg, self.ip),
                "raw": self.broadcast_raw,
            },
            "navi_v2": {
                "protocol_version": PROTOCOL_VERSION,
                "catalog_revision": CATALOG_REVISION,
                "catalog_size": len(CATALOG),
                "json_streams": len(JSON_NAMES),
                "manifest_builds": self.manifest_builds,
                "protocol_errors": self.protocol_errors,
                "control_conns": self.control_conns,
                "stream_conns": self.stream_conns,
                "item_updates": self.item_updates,
                "sessions": [
                    {
                        "session_id": s["session_id"],
                        "app_version": s["app_version"],
                        "applied": s["applied"],
                        "control_open": s["control_open"],
                        "age": round(now - s["created"], 1),
                        "open_streams": sorted(
                            n for n, c in self.stream_conn_open.items() if c > 0
                        ) if s["control_open"] else [],
                    }
                    for s in sorted(self.sessions.values(), key=lambda x: -x["created"])
                ],
            },
            "streams": {
                name: {
                    "count": self.stream_count[name],
                    "open_conns": self.stream_conn_open[name],
                    "last": self.stream_last.get(name),
                }
                for name in JSON_NAMES
            },
            "udp_7706": {
                "packets": self.udp_packets,
                "heartbeats": self.udp_heartbeats,
                "commands": self.udp_commands,
                "bad_packets": self.udp_bad_packets,
                "acks": self.udp_acks,
                "last_from": self.last_7706_from,
                "last_age_ms": (int(now * 1000) - self.last_7706_ms) if self.last_7706_ms else None,
                "last": self.last_7706,
                "last_file": self.cfg.last_7706_file,
            },
            "lamp": {
                "available": bool(self.lamp_source and self.lamp_source.available),
                "error": self.lamp_source.last_error if self.lamp_source else "",
                "color": self.lamp_source.snapshot()[0] if self.lamp_source else 0,
                "clients": self.lamp_server.clients if self.lamp_server else 0,
                "sent": self.lamp_server.sent if self.lamp_server else 0,
                "tla_confirmed": (os.getenv("CPLINK_TLA_CONFIRMED", "0") == "1"
                                  or TLA_MAP_PATH.exists()),
            },
            "navi_state": {
                "streams": self.navi_state.summary(),
                "publisher": {
                    "available": bool(self.navi_publisher and self.navi_publisher.available),
                    "error": self.navi_publisher.last_error if self.navi_publisher else "",
                },
            },
            "nav_lamp": self.nav_lamp.status() if self.nav_lamp else {"enabled": False},
            "compact": {
                "conns": self.compact_conns,
                "clients": self.compact_clients,
                "frames": self.compact_frames,
                "batches": self.compact_batches,
                "last_batch_ms": self.compact_last_batch_ms,
                "source_available": bool(self.compact_source and self.compact_source.available),
                "source_error": self.compact_source.last_error if self.compact_source else "",
                "sequence": dict(self.compact_seq),
            },
        }


def broadcast_targets(cfg: Config, ip: str) -> list[str]:
    if cfg.broadcast_addrs:
        return [a.strip() for a in cfg.broadcast_addrs.split(",") if a.strip()]
    return ["255.255.255.255", subnet_broadcast(ip)]



INDEX_TEXT = (
    "CPlink shim (CP搭子 / navipilot 车端兼容层)\n"
    "\n"
    "GET  /api/heartbeat_status\n"
    "GET  /api/params_bulk?names=A,B\n"
    "POST /api/param_set   {\"name\":\"AlwaysLateral\",\"value\":1}\n"
    "GET  /api/egpu/model\n"
    "GET  /api/settings/snapshot\n"
    "GET  /api/status        (shim 自身诊断)\n"
    "WS   /ws/compact_state\n"
    "WS   :7714 /api/navi/ws/v2/control/amap_auto_1.0\n"
    "WS   :7714 /api/navi/ws/v2/json/{session_id}/{stream_name}\n"
)


def _json_body(obj) -> tuple[int, str, bytes]:
    return 200, CT_JSON, json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode(UTF8)


def dispatch_http(state: ShimState, method: str, target: str, body: bytes):
    split = urlsplit(target)
    path = split.path
    query = parse_qs(split.query)

    if path in ("/camera", "/camera/"):
        return 200, "text/html; charset=utf-8", CAMERA_HTML.encode(UTF8)

    if path == "/frame":
        jpg = _camera_hub(state).jpeg()
        if jpg is None:
            return 503, "text/plain; charset=utf-8", "摄像头帧还没就绪（camerad 未启动？）".encode(UTF8)
        return 200, "image/jpeg", jpg

    if path == "/api/camera_status":
        return _json_body({"ok": True, "camera": _camera_hub(state).stats(), "shim": True})

    if path == "/api/heartbeat_status":
        now = time.time()
        return _json_body({
            "ok": True,
            "hb": round(now, 3),
            "uptime": int(now - state.started),
            "shim": True,
        })

    if path == "/api/params_bulk":
        raw = (query.get("names") or [""])[0]
        names = [n.strip() for n in raw.split(",") if n.strip()]
        return _json_body({"ok": True, "values": state.params.bulk(names), "shim": True})

    if path == "/api/param_set":
        if method.upper() != "POST":
            return 405, CT_JSON, json.dumps(
                {"ok": False, "error": "POST required", "shim": True}).encode(UTF8)
        try:
            payload = json.loads(body.decode(UTF8) or "{}")
            if not isinstance(payload, dict):
                raise ValueError("body must be an object")
        except Exception as exc:  # noqa: BLE001
            LOG.warning("param_set 报文非法: %s (%r)", exc, body[:200])
            return 400, CT_JSON, json.dumps(
                {"ok": False, "error": f"invalid json: {exc}", "shim": True}).encode(UTF8)
        name = payload.get("name")
        if not isinstance(name, str) or not name:
            return 400, CT_JSON, json.dumps(
                {"ok": False, "error": "name required", "shim": True}).encode(UTF8)
        value = payload.get("value")
        state.params.set(name, value)
        LOG.info("param_set name=%s value=%r (shim-local)", name, value)
        return _json_body({"ok": True, "name": name, "value": state.params.values[name],
                           "shim": True})

    if path == "/api/settings/snapshot":
        values = {k: str(v) for k, v in state.params.values.items()}
        return _json_body({"ok": True, "values": values, "shim": True})

    if path == "/api/egpu/model":
        return _json_body({
            "shim": True,
            "egpu": {"available": False, "active": False, "model": None},
            "jetlink": {
                "label": "Jetlink",
                "state": "waiting",
                "active": False,
                "fresh": False,
                "severity": "unknown",
                "reason": "cplink_shim: no jetlink backend on sunnypilot",
            },
        })

    if path in ("/bench", "/bench/", "/bench.html"):
        return 200, CT_TEXT, BENCH_HTML.encode(UTF8)

    if path == "/api/bench/state":
        return _json_body(state.bench_status())

    if path in ("/api/bench/set", "/api/bench/scenario"):
        if method.upper() != "POST":
            return 405, CT_JSON, json.dumps(
                {"ok": False, "error": "POST required"}).encode(UTF8)
        if state.bench_state is None:
            return 400, CT_JSON, json.dumps(
                {"ok": False, "error": "bench mode is not enabled"}).encode(UTF8)
        try:
            payload = json.loads(body.decode(UTF8) or "{}")
            if not isinstance(payload, dict):
                raise ValueError("body must be an object")
            if path == "/api/bench/scenario":
                name = payload.get("name")
                if not isinstance(name, str):
                    raise ValueError("name required")
                state.bench_state.apply_scenario(name)
            else:
                state.bench_state.update(payload)
        except ValueError as exc:
            return 400, CT_JSON, json.dumps(
                {"ok": False, "error": str(exc)}).encode(UTF8)
        return _json_body({"ok": True, "state": state.bench_state.snapshot()})

    if path == "/api/lamp.json":
        source = state.lamp_source
        color, stop_line = source.snapshot() if source is not None else (0, None)
        return _json_body({
            "ok": True,
            "color": color,
            "stop_line_m": stop_line,
            "available": bool(source and source.available),
            "error": source.last_error if source else "",
            "last": source.last if source else {},
            "tla_raw": source.tla_raw if source else {},
            "tla_confirmed": (os.getenv("CPLINK_TLA_CONFIRMED", "0") == "1"
                              or TLA_MAP_PATH.exists()),
            "tla_bus": os.getenv("CPLINK_TLA_BUS", "2"),
            "clients": state.lamp_server.clients if state.lamp_server else 0,
            "sent": state.lamp_server.sent if state.lamp_server else 0,
        })

    if path == "/api/navi_state.json":
        return _json_body({
            "ok": True,
            "summary": state.navi_state.summary(),
            "streams": state.navi_state.snapshot(),
        })

    if path == "/api/nav_lamp.json":
        return _json_body({
            "ok": True,
            "bridge": state.nav_lamp.status() if state.nav_lamp else None,
            "yolo_objects_fields": ("egoLightState", "egoLightFrames", "egoLightDistance"),
        })

    if path == "/api/compact_state.json":
        source = state.compact_source
        snapshot, updated = source.snapshot() if source is not None else ({}, 0.0)
        return _json_body({
            "ok": True,
            "source_available": bool(source and source.available),
            "source_error": source.last_error if source else "",
            "age_s": round(time.monotonic() - updated, 3) if updated else None,
            "sequence": dict(state.compact_seq),
            "services": snapshot,
        })

    if path == "/api/status":
        return _json_body(state.status())

    if path in ("/", "/index.html"):
        return 200, CT_TEXT, INDEX_TEXT.encode(UTF8)

    return 404, CT_JSON, json.dumps({"ok": False, "error": "not found", "path": path,
                                     "shim": True}).encode(UTF8)



CONTROL_RE = re.compile(r"^/api/navi/ws/v2/control/(?P<app>[A-Za-z0-9_.\-]+)$")
STREAM_RE = re.compile(r"^/api/navi/ws/v2/json/(?P<sid>[A-Za-z0-9_\-]+)/(?P<name>[A-Za-z0-9_]+)$")


def dispatch_ws(state: ShimState, path: str, remote: str):
    if path in ("/ws/camera/road", "/camera/road"):
        return CameraPeer(state, remote)
    if path == "/ws/compact_state":
        return CompactPeer(state, remote)
    match = CONTROL_RE.match(path)
    if match:
        return ControlPeer(state, match.group("app"), remote)
    match = STREAM_RE.match(path)
    if match:
        sid, name = match.group("sid"), match.group("name")
        if name not in JSON_NAMES:
            LOG.warning("7714 json 流拒绝: name=%s 不在 13 个 json 流内 (from=%s)", name, remote)
            return None
        if not state.sessions.get(sid, {}).get("control_open", False):
            LOG.warning("7714 json 流拒绝: 未知 session=%s name=%s (from=%s)", sid, name, remote)
            return None
        return StreamPeer(state, sid, name, remote)
    return None


def protocol_error(code: str, message: str, session_id: str | None = None,
                   recoverable: bool = True) -> str:
    payload = {
        "type": "protocol_error",
        "protocol_version": PROTOCOL_VERSION,
        "code": code,
        "message": message,
        "recoverable": recoverable,
        "sent_at_ms": int(time.time() * 1000),
    }
    if session_id:
        payload["session_id"] = session_id
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


class WsPeer:

    periodic: float | None = None

    def __init__(self, state: ShimState, remote: str) -> None:
        self.state = state
        self.remote = remote

    def on_open(self) -> list[str]:
        return []

    def handle_text(self, text: str) -> list[str]:
        return []

    def handle_binary(self, data: bytes) -> None:
        LOG.info("%s binary frame %d bytes (ignored)", self.tag, len(data))

    def binary_frames(self) -> list[bytes]:
        """Binary payloads to send on this tick; transports call this at 20 Hz."""
        return []

    def on_close(self) -> None:
        return

    def periodic_message(self) -> str | None:
        return None

    tag = "ws"


class CompactPeer(WsPeer):
    """7000 /ws/compact_state: text hello, then CVS1/CVB1 binary frames only."""

    tag = "7000 compact_state"

    def __init__(self, state: "ShimState", remote: str) -> None:
        super().__init__(state, remote)
        self.per_conn: dict = {}

    def on_open(self) -> list[str]:
        self.state.compact_conns += 1
        self.state.compact_clients += 1
        LOG.info("7000 compact_state 连接建立 from=%s (clients=%d)",
                 self.remote, self.state.compact_clients)
        # The app requires this exact mode string and all 12 services; a missing entry
        # makes CarrotWsClient cancel the socket and reconnect forever.
        return [json.dumps({"mode": "carrot-state-v1", "services": COMPACT_SERVICES},
                           separators=(",", ":"))]

    def handle_text(self, text: str) -> list[str]:
        # After the hello the app sends nothing; log unexpected text instead of
        # answering with another text frame (the app would parse it as a bad hello).
        LOG.info("7000 compact_state 收到文本 from=%s %r", self.remote, text[:200])
        return []

    def binary_frames(self) -> list[bytes]:
        return self.state.compact_payload(self.per_conn)

    def on_close(self) -> None:
        self.state.compact_clients = max(0, self.state.compact_clients - 1)
        LOG.info("7000 compact_state 断开 from=%s (clients=%d)",
                 self.remote, self.state.compact_clients)


def build_manifest(session_id: str) -> dict:
    streams = []
    for kind, name in CATALOG:
        enabled = kind == "json"
        params = {}
        if enabled:
            params = {
                "delivery_mode": DELIVERY_MODE,
                "interval_ms": STREAM_INTERVAL_MS,
                "stale_timeout_ms": STREAM_STALE_TIMEOUT_MS,
            }
        streams.append({
            "kind": kind,
            "name": name,
            "schema_version": SCHEMA_VERSION,
            "stream_handle": STREAM_HANDLE[(kind, name)],
            "enabled": enabled,
            "params": params,
        })
    return {
        "type": "subscription_manifest",
        "protocol_version": PROTOCOL_VERSION,
        "session_id": session_id,
        "revision": CATALOG_REVISION,
        "streams": streams,
    }


class ControlPeer(WsPeer):

    tag = "7714 control"

    def __init__(self, state: ShimState, app_version: str, remote: str) -> None:
        super().__init__(state, remote)
        self.app_version = app_version
        self.session_id: str | None = None

    def on_open(self) -> list[str]:
        self.state.control_conns += 1
        LOG.info("7714 control 连接建立 from=%s app_version=%s (等待 requirements_query)",
                 self.remote, self.app_version)
        return []

    def handle_text(self, text: str) -> list[str]:
        try:
            msg = json.loads(text)
        except Exception as exc:  # noqa: BLE001
            self.state.protocol_errors += 1
            LOG.warning("7714 control 非法 JSON from=%s: %s", self.remote, exc)
            return [protocol_error("bad_json", "control message must be JSON", self.session_id)]
        if not isinstance(msg, dict):
            self.state.protocol_errors += 1
            return [protocol_error("bad_message", "control message must be an object",
                                   self.session_id)]

        mtype = msg.get("type")
        if mtype == "requirements_query":
            return self._on_requirements(msg)
        if mtype == "manifest_applied":
            return self._on_manifest_applied(msg)

        LOG.info("7714 control 其它报文 type=%s (忽略) session=%s from=%s",
                 mtype, self.session_id, self.remote)
        return []

    def _on_requirements(self, msg: dict) -> list[str]:
        version = msg.get("protocol_version")
        if version != PROTOCOL_VERSION:
            self.state.protocol_errors += 1
            LOG.warning("7714 requirements_query protocol_version=%r 不支持 (仅 %d) from=%s",
                        version, PROTOCOL_VERSION, self.remote)
            return [protocol_error(
                "unsupported_protocol_version",
                f"only protocol_version {PROTOCOL_VERSION} is supported", self.session_id)]

        revision = msg.get("catalog_revision")
        if revision is not None and revision != CATALOG_REVISION:
            self.state.protocol_errors += 1
            LOG.warning("7714 requirements_query catalog_revision=%r 不支持 (仅 %d) from=%s",
                        revision, CATALOG_REVISION, self.remote)
            return [protocol_error(
                "unsupported_catalog_revision",
                f"only catalog_revision {CATALOG_REVISION} is supported", self.session_id)]

        requested = msg.get("streams")
        supported = 0
        if isinstance(requested, list):
            for entry in requested:
                if isinstance(entry, dict) and entry.get("supported"):
                    supported += 1

        session = self.state.new_session(secrets.token_hex(8), self.app_version, msg)
        session["control_remote"] = self.remote
        session["streams_requested"] = [
            f"{e.get('kind')}:{e.get('name')}" for e in requested or [] if isinstance(e, dict)
        ][:64]
        self.session_id = session["session_id"]

        manifest = build_manifest(self.session_id)
        self.state.manifest_builds += 1
        payload = json.dumps(manifest, ensure_ascii=False, separators=(",", ":"))
        LOG.info(
            "7714 manifest 下发 session=%s app_version=%s revision=%d 条目=%d json_enabled=%d "
            "query_streams=%d supported=%d limits=%s bytes=%d",
            self.session_id, self.app_version, CATALOG_REVISION, len(manifest["streams"]),
            sum(1 for s in manifest["streams"] if s["enabled"]),
            len(requested) if isinstance(requested, list) else 0, supported,
            json.dumps(msg.get("limits"), separators=(",", ":")), len(payload))
        return [payload]

    def _on_manifest_applied(self, msg: dict) -> list[str]:
        sid = msg.get("session_id")
        revision = msg.get("revision")
        effective = msg.get("effective_config")
        if not isinstance(sid, str) or sid not in self.state.sessions:
            LOG.warning("7714 manifest_applied 未知 session=%r from=%s", sid, self.remote)
            return [protocol_error("unknown_session", "manifest session is not active",
                                   self.session_id)]
        session = self.state.sessions[sid]
        session["applied"] = True
        enabled_names = []
        if isinstance(effective, list):
            for entry in effective:
                if isinstance(entry, dict) and entry.get("enabled"):
                    enabled_names.append(f"{entry.get('kind')}:{entry.get('name')}")
        LOG.info("7714 manifest_applied session=%s revision=%s enabled=%d %s from=%s",
                 sid, revision, len(enabled_names),
                 ",".join(enabled_names[:16]), self.remote)
        return []

    def on_close(self) -> None:
        if self.session_id:
            self.state.drop_session(self.session_id)
        LOG.info("7714 control 断开 session=%s from=%s", self.session_id, self.remote)


class StreamPeer(WsPeer):

    tag = "7714 json"

    def __init__(self, state: ShimState, session_id: str, name: str, remote: str) -> None:
        super().__init__(state, remote)
        self.session_id = session_id
        self.name = name
        self.handle = JSON_STREAM_HANDLE[name]
        self.last_sequence: int | None = None

    def on_open(self) -> list[str]:
        self.state.stream_conns += 1
        self.state.navi_state.open_stream(self.name, self.session_id, self)
        self.state.stream_conn_open[self.name] = self.state.stream_conn_open.get(self.name, 0) + 1
        LOG.info("7714 json 流打开 name=%s handle=%d session=%s from=%s",
                 self.name, self.handle, self.session_id, self.remote)
        return []

    def handle_text(self, text: str) -> list[str]:
        try:
            msg = json.loads(text)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("7714 json 非法 JSON name=%s: %s raw=%r", self.name, exc, text[:200])
            return []
        if not isinstance(msg, dict):
            LOG.warning("7714 json 顶层不是对象 name=%s", self.name)
            return []

        mtype = msg.get("type")
        if mtype == "protocol_error":
            LOG.warning("7714 json 收到 protocol_error name=%s code=%s message=%s",
                        self.name, msg.get("code"), msg.get("message"))
            return []
        if mtype != "item_update":
            LOG.info("7714 json 其它报文 type=%s name=%s (忽略)", mtype, self.name)
            return []

        sequence = msg.get("sequence")
        present = msg.get("present")
        reason = msg.get("reason")
        msg_name = msg.get("name")
        msg_sid = msg.get("session_id")
        revision = msg.get("manifest_revision")
        stream_handle = msg.get("stream_handle")
        kind = msg.get("kind")
        source_ts = msg.get("source_timestamp_ms")
        sent_ts = msg.get("sent_at_ms")
        value = msg.get("value")
        value_json = json.dumps(value, ensure_ascii=False, separators=(",", ":")) if value is not None else ""
        if len(value_json) > 400:
            value_json = value_json[:400] + "..."

        self.state.item_updates += 1
        self.state.stream_count[self.name] = self.state.stream_count.get(self.name, 0) + 1

        log_item = LOG.debug if self.state.cfg.bench else LOG.info
        log_item("7714 item_update name=%s seq=%s present=%s handle=%s rev=%s kind=%s sid=%s "
                 "src_ts=%s sent_ts=%s value=%s reason=%s from=%s",
                 self.name, sequence, present, stream_handle, revision, kind, msg_sid,
                 source_ts, sent_ts, value_json or "null", reason, self.remote)

        if msg_name is not None and msg_name != self.name:
            LOG.warning("7714 item_update name 不匹配: socket=%s 报文=%s", self.name, msg_name)
        if msg_sid is not None and msg_sid != self.session_id:
            LOG.warning("7714 item_update session_id 不匹配: socket=%s 报文=%s",
                        self.session_id, msg_sid)
        if stream_handle is not None and stream_handle != self.handle:
            LOG.warning("7714 item_update stream_handle 不匹配: name=%s 期望=%d 报文=%s",
                        self.name, self.handle, stream_handle)
        if revision is not None and revision != CATALOG_REVISION:
            LOG.warning("7714 item_update manifest_revision=%s 期望 %d", revision, CATALOG_REVISION)
        if present is False and not (isinstance(reason, str) and reason.strip()):
            LOG.warning("7714 item_update present=false 但 reason 为空 name=%s seq=%s",
                        self.name, sequence)
        identity_ok = (
            msg_name in (None, self.name)
            and msg_sid in (None, self.session_id)
            and stream_handle in (None, self.handle)
            and revision in (None, CATALOG_REVISION)
            and kind in (None, "json")
        )
        if not identity_ok or type(present) is not bool or (present and not isinstance(value, dict)):
            return []
        if type(sequence) is not int or sequence < 0:
            return []
        if self.last_sequence is not None and sequence <= self.last_sequence:
            LOG.warning("7714 item_update sequence 未递增 name=%s last=%d now=%d",
                        self.name, self.last_sequence, sequence)
            return []
        if not self.state.navi_state.update(
            self.name, present, value, source_ts if type(source_ts) is int else None, sequence,
            session=self.session_id, owner=self,
        ):
            return []
        self.last_sequence = sequence

        self.state.stream_last[self.name] = {
            "sequence": sequence,
            "present": present,
            "reason": reason,
            "session_id": msg_sid,
            "stream_handle": stream_handle,
            "manifest_revision": revision,
            "source_timestamp_ms": source_ts,
            "sent_at_ms": sent_ts,
            "received_ms": int(time.time() * 1000),
            "value_bytes": len(value_json),
            "value_preview": value_json[:200] if value_json else None,
            "count": self.state.stream_count[self.name],
        }

        return []

    def on_close(self) -> None:
        self.state.navi_state.close_stream(self.name, self.session_id, self)
        open_now = max(0, self.state.stream_conn_open.get(self.name, 1) - 1)
        self.state.stream_conn_open[self.name] = open_now
        LOG.info("7714 json 流关闭 name=%s session=%s from=%s", self.name, self.session_id,
                 self.remote)



WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class StdlibWebSocket:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self._frag = bytearray()
        self._frag_op = 0
        self.closed = False

    async def handshake(self, headers: dict[str, str]) -> None:
        key = headers.get("sec-websocket-key", "")
        digest = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        self.writer.write(
            ("HTTP/1.1 101 Switching Protocols\r\n"
             "Upgrade: websocket\r\n"
             "Connection: Upgrade\r\n"
             f"Sec-WebSocket-Accept: {digest}\r\n\r\n").encode()
        )
        await self.writer.drain()

    async def send_text(self, text: str) -> None:
        await self._send(0x1, text.encode(UTF8))

    async def send_bytes(self, payload: bytes) -> None:
        await self._send(0x2, payload)

    async def _send(self, opcode: int, payload: bytes) -> None:
        if self.closed:
            raise ConnectionError("websocket closed")
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(length)
        elif length < 65536:
            header.append(126)
            header += struct.pack("!H", length)
        else:
            header.append(127)
            header += struct.pack("!Q", length)
        self.writer.write(bytes(header) + payload)
        await self.writer.drain()

    async def _read_exact(self, n: int) -> bytes:
        return await self.reader.readexactly(n)

    async def recv(self):
        while True:
            try:
                head = await self._read_exact(2)
            except (asyncio.IncompleteReadError, ConnectionError):
                return ("close", None)
            fin = bool(head[0] & 0x80)
            opcode = head[0] & 0x0F
            masked = bool(head[1] & 0x80)
            length = head[1] & 0x7F
            if length == 126:
                length = struct.unpack("!H", await self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", await self._read_exact(8))[0]
            if length > WS_MAX_MESSAGE:
                raise ConnectionError(f"frame too large: {length}")
            mask = await self._read_exact(4) if masked else b""
            data = await self._read_exact(length) if length else b""
            if masked and data:
                data = bytes(byte ^ mask[i & 3] for i, byte in enumerate(data))

            if opcode == 0x8:
                try:
                    await self._send(0x8, data[:2])
                except ConnectionError:
                    pass
                return ("close", None)
            if opcode == 0x9:
                await self._send(0xA, data)
                continue
            if opcode == 0xA:
                continue
            if opcode in (0x1, 0x2):
                if fin:
                    return ("text" if opcode == 0x1 else "binary", data)
                self._frag = bytearray(data)
                self._frag_op = opcode
                continue
            if opcode == 0x0:
                self._frag += data
                if fin:
                    op = self._frag_op
                    payload = bytes(self._frag)
                    self._frag = bytearray()
                    return ("text" if op == 0x1 else "binary", payload)
                continue

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            await self._send(0x8, b"\x03\xe8")
        except Exception:  # noqa: BLE001
            pass


class StdlibServer:

    def __init__(self, state: ShimState, port: int) -> None:
        self.state = state
        self.port = port
        self.server: asyncio.AbstractServer | None = None
        self._tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "0.0.0.0", self.port,
                                                 limit=WS_MAX_MESSAGE + 65536)
        LOG.info("stdlib HTTP/WS 监听 0.0.0.0:%d", self.port)

    async def stop(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        for task in list(self._tasks):
            task.cancel()

    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        remote = _peer_name(writer)
        task = asyncio.current_task()
        if task:
            self._tasks.add(task)
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=15)
            lines = head.decode("latin-1").split("\r\n")
            if not lines or len(lines[0].split(" ")) < 2:
                await _write_http(writer, 400, CT_JSON,
                                  b'{"ok":false,"error":"bad request line"}')
                return
            method, target = lines[0].split(" ")[0], lines[0].split(" ")[1]
            headers: dict[str, str] = {}
            for line in lines[1:]:
                if not line.strip():
                    continue
                name, _, value = line.partition(":")
                headers[name.strip().lower()] = value.strip()

            body = b""
            length = int(headers.get("content-length", "0") or 0)
            if length:
                body = await asyncio.wait_for(reader.readexactly(length), timeout=15)

            is_ws = headers.get("upgrade", "").lower() == "websocket"
            path = urlsplit(target).path

            if not is_ws:
                status, ctype, payload = dispatch_http(self.state, method, target, body)
                await _write_http(writer, status, ctype, payload)
                return

            peer = dispatch_ws(self.state, path, remote)
            if peer is None:
                await _write_http(writer, 404, CT_JSON,
                                  b'{"ok":false,"error":"unknown websocket path"}')
                return

            ws = StdlibWebSocket(reader, writer)
            await ws.handshake(headers)
            await self._run_peer(ws, peer)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            LOG.debug("stdlib 连接异常 from=%s: %s", remote, exc)
        finally:
            if task:
                self._tasks.discard(task)
            _close_writer(writer)

    async def _run_peer(self, ws: StdlibWebSocket, peer: WsPeer) -> None:
        try:
            for message in peer.on_open():
                await ws.send_text(message)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("%s 初始消息发送失败: %s", peer.tag, exc)
            return

        ticker = None
        if peer.periodic:
            ticker = asyncio.create_task(self._tick(ws, peer))
        binary_ticker = asyncio.create_task(self._binary_tick(ws, peer))

        try:
            while True:
                kind, data = await ws.recv()
                if kind == "close":
                    return
                if kind == "text":
                    for message in peer.handle_text(data.decode(UTF8, "replace")):
                        await ws.send_text(message)
                elif kind == "binary":
                    peer.handle_binary(data)
        except (asyncio.IncompleteReadError, ConnectionError, ConnectionResetError):
            return
        finally:
            if ticker:
                ticker.cancel()
            binary_ticker.cancel()
            peer.on_close()

    async def _tick(self, ws: StdlibWebSocket, peer: WsPeer) -> None:
        try:
            while True:
                await asyncio.sleep(peer.periodic or 5.0)
                message = peer.periodic_message()
                if message is None:
                    return
                await ws.send_text(message)
        except (asyncio.CancelledError, ConnectionError, ConnectionResetError):
            return

    async def _binary_tick(self, ws: StdlibWebSocket, peer: WsPeer) -> None:
        try:
            while True:
                await asyncio.sleep(0.05)
                for payload in peer.binary_frames():
                    await ws.send_bytes(payload)
        except (asyncio.CancelledError, ConnectionError, ConnectionResetError):
            return


def _peer_name(writer: asyncio.StreamWriter) -> str:
    try:
        peer = writer.get_extra_info("peername")
        if peer:
            return f"{peer[0]}:{peer[1]}"
    except Exception:  # noqa: BLE001
        pass
    return "?"


async def _write_http(writer: asyncio.StreamWriter, status: int, ctype: str,
                      payload: bytes) -> None:
    reason = {200: "OK", 400: "Bad Request", 404: "Not Found", 405: "Method Not Allowed"}.get(
        status, "OK")
    head = (
        f"HTTP/1.1 {status} {reason}\r\n"
        f"Content-Type: {ctype}\r\n"
        f"Content-Length: {len(payload)}\r\n"
        "Connection: close\r\n"
        "Cache-Control: no-store\r\n"
        "\r\n"
    ).encode()
    try:
        writer.write(head + payload)
        await writer.drain()
    except (ConnectionError, ConnectionResetError):
        pass


def _close_writer(writer: asyncio.StreamWriter) -> None:
    try:
        writer.close()
    except Exception:  # noqa: BLE001
        pass



async def _aiohttp_view(state: ShimState, request):
    if (request.headers.get("Upgrade") or "").lower() == "websocket":
        return await _aiohttp_ws(state, request)
    body = await request.read()
    status, ctype, payload = dispatch_http(
        state, request.method, request.path_qs, body)
    return _aio_response(status, ctype, payload)


def _aio_response(status: int, ctype: str, payload: bytes):
    from aiohttp import web
    return web.Response(status=status, body=payload, headers={"Content-Type": ctype})


async def _aiohttp_ws(state: ShimState, request):
    from aiohttp import WSMsgType, web

    remote = f"{request.remote}:{request.transport.get_extra_info('peername', ('?', 0))[1]}" \
        if request.transport is not None else str(request.remote)
    peer = dispatch_ws(state, request.path, remote)
    if peer is None:
        return _aio_response(404, CT_JSON, b'{"ok":false,"error":"unknown websocket path"}')

    ws = web.WebSocketResponse(heartbeat=30.0, autoping=True, max_msg_size=WS_MAX_MESSAGE)
    await ws.prepare(request)

    for message in peer.on_open():
        await ws.send_str(message)

    ticker = None
    if peer.periodic:
        ticker = asyncio.create_task(_aio_tick(ws, peer))
    binary_ticker = asyncio.create_task(_aio_binary_tick(ws, peer))
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                for message in peer.handle_text(msg.data):
                    await ws.send_str(message)
            elif msg.type == WSMsgType.BINARY:
                peer.handle_binary(msg.data)
            elif msg.type == WSMsgType.ERROR:
                LOG.debug("%s 传输错误: %s", peer.tag, ws.exception())
                break
    finally:
        if ticker:
            ticker.cancel()
        binary_ticker.cancel()
        peer.on_close()
    return ws


async def _aio_tick(ws, peer: WsPeer) -> None:
    try:
        while True:
            await asyncio.sleep(peer.periodic or 5.0)
            message = peer.periodic_message()
            if message is None:
                return
            await ws.send_str(message)
    except (asyncio.CancelledError, ConnectionError, ConnectionResetError):
        return


async def _aio_binary_tick(ws, peer: WsPeer) -> None:
    try:
        while True:
            await asyncio.sleep(0.05)
            for payload in peer.binary_frames():
                await ws.send_bytes(payload)
    except (asyncio.CancelledError, ConnectionError, ConnectionResetError):
        return


async def start_aiohttp(state: ShimState, port: int):
    from aiohttp import web
    app = web.Application(client_max_size=256 * 1024, logger=LOG)
    app.router.add_route("*", "/{tail:.*}", lambda request: _aiohttp_view(state, request))
    runner = web.AppRunner(app, access_log=None, handle_signals=False)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    LOG.info("aiohttp HTTP/WS 监听 0.0.0.0:%d", port)
    return runner


# ─────────────────────────────── UDP 7705 / 7706 ───────────────────────────────

class UdpCommandProtocol(asyncio.DatagramProtocol):
    def __init__(self, state: ShimState) -> None:
        self.state = state
        self.transport = None

    def connection_made(self, transport) -> None:
        self.transport = transport
        sock = transport.get_extra_info("socket")
        if sock is not None:
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            except OSError:
                pass
        LOG.info("UDP 7706 监听 0.0.0.0:%d", self.state.cfg.cmd_port)

    def datagram_received(self, data: bytes, addr) -> None:
        peer = f"{addr[0]}:{addr[1]}" if isinstance(addr, tuple) else str(addr)
        self.state.record_7706(data, peer, peer)
        self._ack_heartbeat(data, addr)

    def _ack_heartbeat(self, data: bytes, addr) -> None:
        """Optional 7706 heartbeat receipt; the app does not require it."""
        if self.transport is None:
            return
        try:
            packet = json.loads(data.decode(UTF8, "replace"))
        except Exception:  # noqa: BLE001
            return
        if not isinstance(packet, dict) or "carrotCmd" in packet or "goalPosX" in packet:
            return
        ack = {
            "carrotIndex": packet.get("carrotIndex"),
            "epochTime": int(time.time()),
            "ack": True,
        }
        try:
            self.transport.sendto(
                json.dumps(ack, separators=(",", ":")).encode(UTF8), addr)
            self.state.udp_acks += 1
        except OSError as exc:
            LOG.debug("7706 ack 发送失败: %s", exc)

    def error_received(self, exc: Exception) -> None:
        LOG.warning("UDP 7706 错误: %s", exc)


async def broadcast_loop(state: ShimState, transport) -> None:
    cfg = state.cfg
    if cfg.bench:
        # A real phone must not discover the bench on 7705 and connect to its ports.
        LOG.info("bench mode: 7705 broadcast disabled")
        while True:
            await asyncio.sleep(3600)
    tick = 0
    while True:
        tick += 1
        if tick % 10 == 1:
            detected = detect_local_ip(cfg.ip)
            if detected != state.ip:
                LOG.warning("本机 IP 变化 %s -> %s", state.ip, detected)
                state.ip = detected
        payload = json.dumps({
            "ip": state.ip,
            "name": "openpilot",
            "port": cfg.http_port,
            "proto": "cplink",
        }, separators=(",", ":")).encode(UTF8)
        state.broadcast_raw = payload.decode(UTF8)
        for target in broadcast_targets(cfg, state.ip):
            try:
                transport.sendto(payload, (target, cfg.discovery_port))
            except Exception as exc:  # noqa: BLE001
                LOG.warning("7705 广播 %s:%d 失败: %s", target, cfg.discovery_port, exc)
        state.broadcast_sent += 1
        if tick % 30 == 1:
            LOG.debug("7705 广播 #%d %s -> %s", tick, state.broadcast_raw,
                      broadcast_targets(cfg, state.ip))
        await asyncio.sleep(cfg.discovery_interval)


async def video_gate_loop(state: ShimState) -> None:
    """Keep /tmp/cplink_video while the phone is heartbeating; remove it after 60 s."""
    while True:
        await asyncio.sleep(10)
        if state.last_7706_ms and time.time() * 1000 - state.last_7706_ms < 60_000:
            continue
        try:
            os.unlink(state.cfg.video_gate)
        except OSError:
            pass


async def stats_loop(state: ShimState) -> None:
    last_packets = 0
    last_updates = 0
    while True:
        await asyncio.sleep(60)
        packets = state.udp_packets
        updates = state.item_updates
        LOG.info(
            "统计 uptime=%ds 7705广播=%d 7706包=%d(+%d) 心跳=%d 命令=%d item_update=%d(+%d) "
            "sessions=%d compact_clients=%d 7706_last_from=%s",
            int(time.time() - state.started), state.broadcast_sent, packets, packets - last_packets,
            state.udp_heartbeats, state.udp_commands, updates, updates - last_updates,
            len(state.sessions), state.compact_clients, state.last_7706_from or "-")
        last_packets = packets
        last_updates = updates



async def amain(cfg: Config) -> int:
    state = ShimState(cfg)
    state.compact_source = CompactSource()
    state.compact_source.start()
    state.navi_publisher = NaviPublisher(state.navi_state, gate_path=cfg.navi_gate)
    state.navi_publisher.start()
    if cfg.bench:
        from bench_app import BenchApp
        from bench_car import BenchCar
        from bench_state import BenchState
        state.bench_state = BenchState()
        state.bench_state.apply_scenario("driving")
        state.bench_car = BenchCar(state.bench_state)
        state.bench_car.start()
        state.bench_app = BenchApp(state.bench_state, cfg.http_port, cfg.navi_port,
                                   cfg.cmd_port, cfg.lamp_port)
    state.transport_name = "stdlib" if cfg.force_stdlib else "aiohttp"
    if not cfg.force_stdlib:
        try:
            import aiohttp  # noqa: F401
        except ImportError:
            LOG.info("aiohttp 不可用，回落标准库实现")
            state.transport_name = "stdlib"

    LOG.info("=" * 78)
    LOG.info("%s 启动 ip=%s transport=%s", USER_AGENT_TAG, state.ip, state.transport_name)
    LOG.info("  7705 广播 -> %s:%d  每 %.1fs",
             broadcast_targets(cfg, state.ip), cfg.discovery_port, cfg.discovery_interval)
    LOG.info("  7706 接收 0.0.0.0:%d -> %s", cfg.cmd_port, cfg.last_7706_file)
    LOG.info("  7000 HTTP/WS 0.0.0.0:%d", cfg.http_port)
    LOG.info("  7714 navi v2 WS 0.0.0.0:%d (catalog=%d, json=%d)",
             cfg.navi_port, len(CATALOG), len(JSON_NAMES))
    LOG.info("=" * 78)

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    servers = []
    transports = []
    tasks = []

    state.lamp_source = LampSource()
    state.lamp_source.start()
    state.lamp_server = LampServer(state.lamp_source, state.ip, cfg.lamp_port)
    try:
        await state.lamp_server.start()
        servers.append(state.lamp_server)
    except OSError as exc:
        LOG.error("TCP %d 绑定失败（端口被占用？）: %s", state.lamp_server.port, exc)
        state.lamp_server = None

    # The phone navigation already carries the signal state, distance and countdown.
    # Publish it on the ego-light fields the planner consumes so YOLO can stay off.
    # CPLINK_NAV_LAMP=0 disables the bridge without touching code.
    if (StopLineSource is not None
            and os.getenv("CPLINK_STOPLINE_ASSIST", "0") not in ("0", "false", "False")):
        try:
            state.stopline = StopLineSource(state.navi_state)
            state.stopline.start()
            LOG.info("停止线距离源已启动（低速/无前车/红灯门控）")
        except Exception as exc:  # noqa: BLE001
            state.stopline = None
            LOG.warning("停止线距离源未启动: %s", exc)

    if os.getenv("CPLINK_NAV_LAMP", "1") not in ("0", "false", "False"):
        try:
            state.nav_lamp = NavLampBridge(
                state.navi_state,
                publish=CerealLampPublisher(),
                enabled=params_yolo_gate(),
                stopline=state.stopline,
            )
            state.nav_lamp.start()
            LOG.info("导航灯态桥已启动: %s", state.nav_lamp.status())
        except Exception as exc:  # noqa: BLE001 - the shim keeps running without it
            state.nav_lamp = None
            LOG.warning("导航灯态桥未启动: %s", exc)

    try:
        try:
            broadcast_transport, _ = await loop.create_datagram_endpoint(
                asyncio.DatagramProtocol, local_addr=("0.0.0.0", 0), allow_broadcast=True)
        except OSError as exc:
            LOG.error("创建 7705 广播 socket 失败: %s", exc)
            return 2
        transports.append(broadcast_transport)

        try:
            cmd_transport, _ = await loop.create_datagram_endpoint(
                lambda: UdpCommandProtocol(state), local_addr=("0.0.0.0", cfg.cmd_port))
        except OSError as exc:
            LOG.error("UDP %d 绑定失败（端口被占用？）: %s", cfg.cmd_port, exc)
            return 2
        transports.append(cmd_transport)

        tasks.append(asyncio.create_task(broadcast_loop(state, broadcast_transport)))
        tasks.append(asyncio.create_task(stats_loop(state)))
        tasks.append(asyncio.create_task(video_gate_loop(state)))

        if state.transport_name == "aiohttp":
            try:
                servers.append(await start_aiohttp(state, cfg.http_port))
                servers.append(await start_aiohttp(state, cfg.navi_port))
            except OSError as exc:
                LOG.error("TCP 端口绑定失败（%d/%d）: %s", cfg.http_port, cfg.navi_port, exc)
                return 2
        else:
            for port in (cfg.http_port, cfg.navi_port):
                server = StdlibServer(state, port)
                try:
                    await server.start()
                except OSError as exc:
                    LOG.error("TCP %d 绑定失败: %s", port, exc)
                    return 2
                servers.append(server)

        if state.bench_app is not None:
            state.bench_app.start()
        LOG.info("就绪：等待手机连接%s", "（车架模式）" if cfg.bench else "")
        await stop_event.wait()
        LOG.info("收到退出信号，开始收尾")
        return 0
    finally:
        for task in tasks:
            task.cancel()
        # Close the fake phone first: its WebSockets keep aiohttp's runner.cleanup()
        # waiting, which made systemd stop time out.
        if state.bench_app is not None:
            await state.bench_app.stop()
        if state.bench_car is not None:
            state.bench_car.stop()
        for server in servers:
            try:
                await server.cleanup()
            except AttributeError:
                await server.stop()
            except Exception as exc:  # noqa: BLE001
                LOG.debug("server 收尾异常: %s", exc)
        for transport in transports:
            try:
                transport.close()
            except Exception:  # noqa: BLE001
                pass
        if state.compact_source is not None:
            state.compact_source.stop()
        if state.navi_publisher is not None:
            state.navi_publisher.stop()
        if state.nav_lamp is not None:
            state.nav_lamp.stop()
        if state.stopline is not None:
            state.stopline.stop()
        if state.lamp_source is not None:
            state.lamp_source.stop()
        LOG.info("已退出")


def main() -> int:
    parser = argparse.ArgumentParser(description="CPlink (CP搭子/navipilot) 车端兼容层")
    parser.add_argument("--ip", default=None, help="广播里携带的本机 IP（默认自动探测）")
    parser.add_argument("--log-level", default=None, help="DEBUG/INFO/WARNING")
    parser.add_argument("--bench", action="store_true",
                        help="车架模式：假车+假手机，独立 msgq 前缀和端口")
    args = parser.parse_args()

    cfg = Config()
    if args.bench:
        cfg.bench = True
    if cfg.bench:
        prefix = "cplink-bench"
        os.environ["OPENPILOT_PREFIX"] = prefix
        try:
            os.makedirs(f"/dev/shm/msgq_{prefix}", exist_ok=True)
        except OSError:
            pass
        if not os.environ.get("CPLINK_HTTP_PORT"):
            cfg.http_port = 17000
        if not os.environ.get("CPLINK_NAVI_PORT"):
            cfg.navi_port = 17714
        if not os.environ.get("CPLINK_CMD_PORT"):
            cfg.cmd_port = 17706
        if not os.environ.get("CPLINK_DISCOVERY_PORT"):
            cfg.discovery_port = 17705
        if not os.environ.get("CPLINK_LAMP_PORT"):
            cfg.lamp_port = 17711
        cfg.navi_gate = os.environ.get("CPLINK_NAVI_GATE",
                                       "/tmp/cplink_navi_source_bench")
        cfg.video_gate = os.environ.get("CPLINK_VIDEO_GATE",
                                        "/tmp/cplink_video_bench")
        cfg.last_7706_file = os.environ.get("CPLINK_LAST_7706_FILE",
                                            "/tmp/cplink_shim_7706_bench.json")
        cfg.params_file = os.environ.get("CPLINK_PARAMS_FILE",
                                         "/tmp/cplink_shim_params_bench.json")
        os.environ.setdefault("CPLINK_TLA_CONFIRMED", "1")
        os.environ.setdefault("CPLINK_TLA_RED", "1")
        os.environ.setdefault("CPLINK_TLA_GREEN", "2")
        os.environ.setdefault("CPLINK_TLA_YELLOW", "3")
        cfg.ip = cfg.ip or "127.0.0.1"
    if args.ip:
        cfg.ip = args.ip
    if args.log_level:
        cfg.log_level = args.log_level.upper()

    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    try:
        return asyncio.run(amain(cfg))
    except KeyboardInterrupt:
        return 0


def _camera_hub(state: "ShimState") -> CameraHub:
    hub = state.camera_hub
    if hub is None:
        hub = CameraHub()
        state.camera_hub = hub
        hub.start()
    return hub


class CameraPeer(WsPeer):

    tag = "camera"

    def __init__(self, state: "ShimState", remote: str) -> None:
        super().__init__(state, remote)
        self._hub = _camera_hub(state)
        self._seq = 0
        self._hub.attach()
        LOG.info("摄像头 H.264 客户端接入 (from=%s)", remote)

    def binary_frames(self) -> list[bytes]:
        packets, seq = self._hub.packets_since(self._seq)
        self._seq = seq
        return packets

    def on_close(self) -> None:
        self._hub.detach()
        LOG.info("摄像头 H.264 客户端断开 (from=%s)", self.remote)


if __name__ == "__main__":
    sys.exit(main())
