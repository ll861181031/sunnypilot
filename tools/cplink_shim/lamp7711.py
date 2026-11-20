#!/usr/bin/env python3
"""TCP 7711 lamp feed, decoded read-only from cereal ``can``.

Protocol: 4-byte big-endian length + UTF-8 JSON at 20 Hz, one frame per client:
``{version, sequence, timestamp, ip, data:{carState:{stopLineDist?, trafficLightColor}}}``

Sources on this car (see opendbc/car/byd/signal_catalog.csv and LIGHT_CONTROL_RESEARCH.md):

* 0x2B4: ADAS_TLA_LEFT/STRAIGHT/RIGHT_TRAFFIC_LIGHT_STATE at bits 34/37/40, width 3
  (bus 2 is the stock ADAS broadcast; bus 0 carries openpilot's own HMA frame).
* 0x2D5: TLA status fields exist only as feature ids; positions are not in the signal
  catalog, so this module only logs them in survey mode.
* 0x133: stalk/light state, **not** a traffic light; used for diagnostics only.

The 3-bit TLA values are not documented in this repo.  They are only mapped to
1/2/3 (red/green/yellow) when ``CPLINK_TLA_CONFIRMED=1`` and the three env values are
set from a parked survey; otherwise the feed sends color 0.  No fabricated lamps.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import struct
import threading
import time
from pathlib import Path

LOG = logging.getLogger("cplink_shim.lamp")

ADDR_STALKS = 0x133
ADDR_TLA_2B4 = 0x2B4
ADDR_TLA_2D5 = 0x2D5
TTL_S = 1.5
PORT = 7711


def _bits(data: bytes, start: int, width: int) -> int:
    """Little-endian (Intel) bit extraction; start is the LSB bit index."""
    value = 0
    for index in range(width):
        bit = start + index
        byte = bit // 8
        if byte >= len(data):
            return 0
        value |= ((data[byte] >> (bit % 8)) & 1) << index
    return value


def decode_stalks(data: bytes) -> dict:
    if len(data) < 8:
        return {}
    return {
        "light_mode": (data[3] >> 6) & 0x3,
        "turn_switch": (data[4] >> 4) & 0x7,
        "left_blinker": bool((data[0] >> 4) & 1),
        "right_blinker": bool((data[0] >> 5) & 1),
        "checksum_ok": (sum(data[:7]) + data[7]) & 0xFF == 0xFF,
    }


def decode_tla_2b4(data: bytes) -> dict:
    return {
        "left": _bits(data, 34, 3),
        "straight": _bits(data, 37, 3),
        "right": _bits(data, 40, 3),
    }


TLA_MAP_PATH = Path("/tmp/cplink_tla_map.json")


def _tla_mapping() -> dict[int, int]:
    """Confirmed survey values. Env wins; the file lets a test attach override a
    running shim without restarting it (removed when the attach mode stops)."""
    mapping: dict[int, int] = {}
    confirmed = os.getenv("CPLINK_TLA_CONFIRMED") == "1"
    if confirmed:
        for name, color in (("CPLINK_TLA_RED", 1), ("CPLINK_TLA_GREEN", 2),
                            ("CPLINK_TLA_YELLOW", 3)):
            try:
                mapping[int(os.getenv(name, "-1"))] = color
            except ValueError:
                pass
        return mapping
    try:
        data = json.loads(TLA_MAP_PATH.read_text(encoding="utf-8"))
        if data.get("confirmed"):
            for name, color in (("red", 1), ("green", 2), ("yellow", 3)):
                mapping[int(data[name])] = color
    except (OSError, ValueError, KeyError, TypeError):
        return {}
    return mapping


def tla_color(raw: int) -> int:
    """Map a confirmed survey value to 1 red / 2 green / 3 yellow; 0 = unknown."""
    return _tla_mapping().get(raw, 0)


class LampSource:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._color = 0
        self._stop_line: float | None = None
        self._updated = 0.0
        self._bus = int(os.getenv("CPLINK_TLA_BUS", "2"))
        self.last: dict = {}
        self.available = False
        self.last_error = ""
        self.frames = 0
        self.tla_raw = {"left": 0, "straight": 0, "right": 0}

    def start(self) -> bool:
        try:
            from openpilot.cereal import messaging
            self._sm = messaging.SubMaster(["can"], ignore_alive=["can"],
                                           ignore_avg_freq=["can"])
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"msgq unavailable: {exc}"
            LOG.warning("lamp source unavailable: %s", self.last_error)
            return False
        self.available = True
        self._thread = threading.Thread(target=self._run, name="lamp-source", daemon=True)
        self._thread.start()
        LOG.info("lamp source started (TLA bus=%s, confirmed=%s)",
                 self._bus, os.getenv("CPLINK_TLA_CONFIRMED", "0"))
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def snapshot(self) -> tuple[int, float | None]:
        now = time.monotonic()
        with self._lock:
            color = self._color if now - self._updated <= TTL_S else 0
            stop_line = self._stop_line if now - self._updated <= TTL_S else None
            return color, stop_line

    def _run(self) -> None:
        assert self._sm is not None
        while not self._stop.is_set():
            try:
                self._sm.update(0)
                if self._sm.valid.get("can"):
                    for frame in self._sm["can"]:
                        self._consume(int(frame.address), bytes(frame.dat), int(frame.src))
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
                LOG.debug("lamp source update failed: %s", exc)
                time.sleep(0.2)
            time.sleep(0.01)

    def _consume(self, address: int, data: bytes, src: int) -> None:
        self.frames += 1
        now = time.monotonic()
        if address == ADDR_STALKS:
            decoded = decode_stalks(data)
            if decoded:
                self.last = {"addr": "0x133", "src": src, **decoded}
            return
        if address == ADDR_TLA_2D5:
            self.last = {"addr": "0x2d5", "src": src, "raw": data.hex()}
            return
        if address != ADDR_TLA_2B4:
            return
        if self._bus >= 0 and src != self._bus:
            # openpilot also transmits 0x2B4 on the powertrain bus; do not trust its
            # TLA bits.  Survey with CPLINK_TLA_BUS=-1 if the stock ADAS is elsewhere.
            return
        decoded = decode_tla_2b4(data)
        self.tla_raw = decoded
        self.last = {"addr": "0x2b4", "src": src, **decoded}
        # Prefer the ego-lane straight lamp; fall back to left/right only if straight is 0.
        raw = decoded["straight"] or decoded["left"] or decoded["right"]
        color = tla_color(raw)
        with self._lock:
            self._color = color
            self._updated = now


class LampServer:
    def __init__(self, source: LampSource, ip: str, port: int = PORT) -> None:
        self.source = source
        self.ip = ip
        self.port = port
        self.server: asyncio.AbstractServer | None = None
        self.clients = 0
        self.sent = 0

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "0.0.0.0", self.port)
        LOG.info("TCP 7711 灯态监听 0.0.0.0:%d", self.port)

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None

    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        self.clients += 1
        LOG.info("7711 客户端连接 %s (clients=%d)", peer, self.clients)
        sequence = 0
        try:
            while True:
                color, stop_line = self.source.snapshot()
                sequence += 1
                car_state = {"trafficLightColor": color}
                if stop_line is not None:
                    car_state["stopLineDist"] = float(stop_line)
                frame = {
                    "version": 1,
                    "sequence": sequence,
                    "timestamp": int(time.time() * 1000),
                    "ip": self.ip,
                    "data": {"carState": car_state},
                }
                raw = json.dumps(frame, ensure_ascii=False, separators=(",", ":")).encode()
                writer.write(struct.pack(">I", len(raw)) + raw)
                await writer.drain()
                self.sent += 1
                await asyncio.sleep(0.05)
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            pass
        finally:
            self.clients = max(0, self.clients - 1)
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass
            LOG.info("7711 客户端断开 %s (clients=%d)", peer, self.clients)


def survey() -> int:
    """Park at a red and a green light; print raw TLA/stalk values, send nothing."""
    source = LampSource()
    if not source.start():
        print(source.last_error)
        return 2
    try:
        last = None
        while True:
            time.sleep(0.5)
            state = dict(source.last)
            if state != last:
                print(json.dumps(state, ensure_ascii=False), flush=True)
                last = state
    except KeyboardInterrupt:
        return 0
    finally:
        source.stop()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
    parser = argparse.ArgumentParser(description="CPlink 7711 lamp feed / CAN survey")
    parser.add_argument("--survey", action="store_true", help="print raw CAN values only")
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()
    if args.survey:
        raise SystemExit(survey())
    # Standalone server mode (the shim normally hosts this itself).
    src = LampSource()
    src.start()
    loop = asyncio.new_event_loop()
    srv = LampServer(src, os.getenv("CPLINK_IP", "192.168.123.70"), args.port)
    loop.run_until_complete(srv.start())
    try:
        loop.run_forever()
    except KeyboardInterrupt:
        pass
