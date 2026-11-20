#!/usr/bin/env python3
"""
------------


--------------

-----------------------------------


----
    python h264_ws.py --port 7708
    GET /health -> {"clients":0,"frames":...,"kbytes":...,"fps":...}
    WS  /camera/road
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import struct
import time

from aiohttp import web, WSMsgType

from openpilot.cereal import messaging

LOG = logging.getLogger("h264ws")

V4L2_BUF_FLAG_KEYFRAME = 0x8

TOPIC = "livestreamNarrowRoadEncodeData"

HEADER = struct.Struct(">IIB")  # len, pts_ms, flags


class Hub:

    def __init__(self) -> None:
        self.clients: set[web.WebSocketResponse] = set()
        self.frames = 0
        self.bytes = 0
        self.keyframes = 0
        self.dropped = 0
        self.fps = 0.0
        self._window_frames = 0
        self._window_at = time.monotonic()
        self.last_frame_at = 0.0

    def add(self, ws: web.WebSocketResponse) -> None:
        self.clients.add(ws)
        LOG.info("client connected, total=%d", len(self.clients))

    def remove(self, ws: web.WebSocketResponse) -> None:
        self.clients.discard(ws)
        LOG.info("client gone, total=%d", len(self.clients))

    def stats(self) -> dict[str, float | int]:
        return {
            "clients": len(self.clients),
            "frames": self.frames,
            "keyframes": self.keyframes,
            "kbytes": self.bytes // 1024,
            "fps": round(self.fps, 1),
            "dropped": self.dropped,
        }

    async def broadcast(self, payload: bytes, pts_ms: int, flags: int) -> None:
        if not self.clients:
            return
        packet = HEADER.pack(len(payload) + HEADER.size, pts_ms, flags) + payload
        self.frames += 1
        self.bytes += len(packet)
        self._window_frames += 1
        now = time.monotonic()
        self.last_frame_at = now
        if now - self._window_at >= 1.0:
            self.fps = self._window_frames / (now - self._window_at)
            self._window_frames = 0
            self._window_at = now
        for ws in list(self.clients):
            if ws.closed:
                self.clients.discard(ws)
                continue
            try:
                await ws.send_bytes(packet)
            except Exception:
                self.dropped += 1
                self.clients.discard(ws)
                LOG.warning("client dropped (send failed)")


async def publish_loop(hub: Hub) -> None:
    sock = messaging.sub_sock(TOPIC, conflate=True)
    loop = asyncio.get_running_loop()
    log_at = time.monotonic()
    try:
        while True:
            msg = messaging.recv_one_or_none(sock)
            if msg is None:
                await asyncio.sleep(0.002)
            else:
                encode_data = getattr(msg, msg.which())
                idx = encode_data.idx
                flags = 1 if (idx.flags & V4L2_BUF_FLAG_KEYFRAME) else 0
                if flags:
                    hub.keyframes += 1
                payload = encode_data.header + encode_data.data
                pts_ms = int(idx.timestampEof / 1e6) if idx.timestampEof else 0
                await hub.broadcast(payload, pts_ms, flags)
            now = time.monotonic()
            if loop and now - log_at >= 30.0:
                log_at = now
                LOG.info(
                    "统计 clients=%d 帧=%d 关键帧=%d %.0f fps %d KB 丢弃=%d",
                    len(hub.clients), hub.frames, hub.keyframes, hub.fps, hub.bytes // 1024, hub.dropped,
                )
    finally:
        del sock


async def camera_ws(request: web.Request) -> web.WebSocketResponse:
    hub: Hub = request.app["hub"]
    params = request.app["params"]
    ws = web.WebSocketResponse(heartbeat=30.0, autoping=True, max_msg_size=1 << 20)
    await ws.prepare(request)
    hub.add(ws)
    try:
        params.put("LivestreamRequestKeyframe", True, block=False)
    except Exception:
        LOG.debug("request keyframe failed", exc_info=True)
    try:
        async for message in ws:
            if message.type == WSMsgType.ERROR:
                break
    finally:
        hub.remove(ws)
    return ws


async def health(request: web.Request) -> web.Response:
    hub: Hub = request.app["hub"]
    body = json.dumps({**hub.stats(), "topic": TOPIC, "port": request.app["port"]})
    return web.Response(text=body, content_type="application/json")


async def index_page(request: web.Request) -> web.Response:
    hub: Hub = request.app["hub"]
    return web.Response(
        text=(
            "CPlink H.264 WS (CP搭子 备用视频通道)\n"
            f"WS  /camera/road\nGET /health\n\n{json.dumps(hub.stats(), ensure_ascii=False)}\n"
        ),
        content_type="text/plain; charset=utf-8",
    )


async def main_async(port: int) -> None:
    from openpilot.common.params import Params

    hub = Hub()
    app = web.Application()
    app["hub"] = hub
    app["params"] = Params()
    app["port"] = port
    app.router.add_get("/health", health)
    app.router.add_get("/camera/road", camera_ws)
    app.router.add_get("/", index_page)

    publisher = asyncio.create_task(publish_loop(hub))
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    LOG.info("H.264 WS 已启动: ws://0.0.0.0:%d/camera/road", port)
    try:
        await asyncio.Event().wait()
    finally:
        publisher.cancel()
        await runner.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser(description="CP搭子 H.264 over WS 备用视频通道")
    parser.add_argument("--port", type=int, default=7708)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(main_async(args.port))


if __name__ == "__main__":
    main()
