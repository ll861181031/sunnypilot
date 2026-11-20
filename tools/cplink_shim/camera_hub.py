
from __future__ import annotations

import logging
import struct
import threading
import time
from collections import deque

LOG = logging.getLogger("camera_hub")

V4L2_BUF_FLAG_KEYFRAME = 0x8
TOPIC = "livestreamNarrowRoadEncodeData"
HEADER = struct.Struct(">IIB")
JPEG_QUALITY = 70
JPEG_FPS = 12
JPEG_IDLE_SECONDS = 30.0
RESUBSCRIBE_SECONDS = 5.0

CAMERA_HTML = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CP搭子 · 前视摄像头</title>
<style>
  html,body{margin:0;height:100%;background:#05070d;color:#9fb3cc;
    font:13px/1.5 -apple-system,system-ui,"PingFang SC","Microsoft YaHei",sans-serif}
  #wrap{display:flex;flex-direction:column;height:100%}
  #bar{display:flex;gap:12px;align-items:center;padding:6px 10px;background:#0b1524;
    border-bottom:1px solid #16243a}
  #bar b{color:#e6eefc;font-weight:600}
  #dot{width:8px;height:8px;border-radius:50%;background:#22c55e}
  #dot.bad{background:#ef4444}
  #stage{flex:1;display:flex;align-items:center;justify-content:center;overflow:hidden}
  img{max-width:100%;max-height:100%;object-fit:contain;image-rendering:auto}
</style></head>
<body><div id="wrap">
  <div id="bar"><span id="dot"></span><b>前视 · JPEG 备用通道</b>
    <span id="stat">连接中…</span>
    <span style="margin-left:auto">稳一点的客户端请用 H.264：ws://车机IP:7000/ws/camera/road</span>
  </div>
  <div id="stage"><img id="v" alt="camera"></div>
</div>
<script>
let n = 0, t0 = performance.now(), err = 0;
const img = document.getElementById('v'), dot = document.getElementById('dot'), st = document.getElementById('stat');
function tick() {
  const url = '/frame?t=' + Date.now();
  const probe = new Image();
  probe.onload = () => {
    img.src = url; n++; err = 0;
    dot.className = '';
    const fps = (n / ((performance.now() - t0) / 1000)).toFixed(1);
    st.textContent = fps + ' fps · ' + n + ' 帧';
  };
  probe.onerror = () => { err++; dot.className = 'bad'; st.textContent = '取帧失败 ×' + err; };
  probe.src = url;
}
setInterval(tick, 1000 / 12);
tick();
</script></body></html>
"""


class Packet:
    __slots__ = ("seq", "data", "pts_ms", "flags")

    def __init__(self, seq: int, data: bytes, pts_ms: int, flags: int) -> None:
        self.seq = seq
        self.data = data
        self.pts_ms = pts_ms
        self.flags = flags


class CameraHub:

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._packets: deque[Packet] = deque(maxlen=16)
        self._seq = 0
        self._clients = 0
        self.frames = 0
        self.bytes = 0
        self.started = False
        self.fps = 0.0
        self._window_frames = 0
        self._window_at = time.monotonic()
        self._jpeg = JpegSource()
        self.error = ""

    def start(self) -> None:
        with self._lock:
            if self.started:
                return
            self.started = True
        thread = threading.Thread(target=self._encoded_loop, name="camera-h264", daemon=True)
        thread.start()
        LOG.info("摄像头 H.264 通道已启动（topic=%s）", TOPIC)

    def _encoded_loop(self) -> None:
        try:
            from openpilot.cereal import messaging
        except Exception as error:  # pragma: no cover
            self.error = f"cereal 不可用: {error}"
            LOG.warning("摄像头通道停用：%s", self.error)
            return
        sock = None
        idle = 0
        last_msg_at = 0.0
        while True:
            if sock is None:
                try:
                    sock = messaging.sub_sock(TOPIC, conflate=True, timeout=1000)
                except Exception as error:
                    self.error = f"订阅失败: {error}"
                    sock = None
                if sock is None:
                    self.error = self.error or "等 encoderd 发布编码流"
                    time.sleep(1.0)
                    continue
                self.error = ""
                last_msg_at = time.monotonic()
                LOG.info("摄像头 H.264 已订阅 %s", TOPIC)
            msg = messaging.recv_one_or_none(sock)
            if msg is None:
                if time.monotonic() - last_msg_at > RESUBSCRIBE_SECONDS:
                    LOG.info("摄像头 H.264 %.0f 秒没有消息，重新订阅", RESUBSCRIBE_SECONDS)
                    try:
                        sock.close()
                    except Exception:
                        pass
                    sock = None
                    continue
                idle += 1
                time.sleep(0.002 if idle < 50 else 0.02)
                continue
            idle = 0
            last_msg_at = time.monotonic()
            try:
                encode_data = getattr(msg, msg.which())
                idx = encode_data.idx
                flags = 1 if (idx.flags & V4L2_BUF_FLAG_KEYFRAME) else 0
                payload = encode_data.header + encode_data.data
                pts_ms = int(idx.timestampEof / 1e6) if idx.timestampEof else 0
            except Exception as error:
                LOG.debug("编码帧解析失败: %s", error)
                continue
            with self._lock:
                self._seq += 1
                packet = Packet(self._seq, HEADER.pack(len(payload) + HEADER.size, pts_ms, flags) + payload,
                                pts_ms, flags)
                if self._clients > 0:
                    self._packets.append(packet)
                    self.frames += 1
                    self.bytes += len(packet.data)
                    self._window_frames += 1
                    now = time.monotonic()
                    if now - self._window_at >= 1.0:
                        self.fps = self._window_frames / (now - self._window_at)
                        self._window_frames = 0
                        self._window_at = now

    def packets_since(self, seq: int, limit: int = 6) -> tuple[list[bytes], int]:
        with self._lock:
            if self._clients == 0:
                return [], self._seq
            out = [p.data for p in self._packets if p.seq > seq][-limit:]
            return out, self._seq

    def attach(self) -> None:
        with self._lock:
            self._clients += 1
        self.start()

    def detach(self) -> None:
        with self._lock:
            self._clients = max(0, self._clients - 1)
            if self._clients == 0:
                self._packets.clear()

    def stats(self) -> dict:
        with self._lock:
            return {
                "clients": self._clients,
                "frames": self.frames,
                "kbytes": self.bytes // 1024,
                "fps": round(self.fps, 1),
                "topic": TOPIC,
                "jpeg": self._jpeg.stats(),
                "error": self.error,
            }

    def jpeg(self) -> bytes | None:
        return self._jpeg.get()


class JpegSource:

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._last_request = 0.0
        self._latest: bytes | None = None
        self._at = 0.0
        self.error = ""
        self.encodes = 0

    def ensure(self) -> None:
        with self._lock:
            self._last_request = time.monotonic()
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._loop, name="camera-jpeg", daemon=True)
            self._thread.start()

    def get(self) -> bytes | None:
        self.ensure()
        with self._lock:
            return self._latest

    def _loop(self) -> None:
        import numpy as np
        try:
            import cv2
            from openpilot.cereal.visionipc import VisionStreamType
            from msgq.visionipc.visionipc_pyx import VisionIpcClient
        except Exception as error:
            self.error = f"JPEG 依赖缺失: {error}"
            LOG.warning("JPEG 快照停用：%s", self.error)
            return

        client = None
        last_buf_at = 0.0
        while True:
            if time.monotonic() - self._last_request > JPEG_IDLE_SECONDS:
                LOG.info("JPEG 快照空闲，线程退出")
                return
            if client is None:
                fresh = VisionIpcClient("camerad", VisionStreamType.VISION_STREAM_NARROW_ROAD, True)
                if not fresh.connect(True):
                    self.error = "VisionIPC 连接失败（camerad 没在跑？）"
                    time.sleep(0.5)
                    continue
                client = fresh
                self.error = ""
                last_buf_at = time.monotonic()
            buf = client.recv()
            if buf is None:
                if time.monotonic() - last_buf_at > RESUBSCRIBE_SECONDS:
                    LOG.info("JPEG 源 %.0f 秒没有新帧，重连 camerad", RESUBSCRIBE_SECONDS)
                    try:
                        client.close()
                    except Exception:
                        pass
                    client = None
                    continue
                time.sleep(0.01)
                continue
            last_buf_at = time.monotonic()
            try:
                height, width, stride = buf.height, buf.width, buf.stride
                raw = np.frombuffer(buf.data, dtype=np.uint8)
                uv_offset = getattr(buf, "uv_offset", 0) or (stride * height)
                y_plane = raw[:stride * height].reshape(height, stride)[:, :width]
                uv_plane = raw[uv_offset: uv_offset + stride * (height // 2)]
                uv_plane = uv_plane.reshape(height // 2, stride)[:, :width]
                nv12 = np.ascontiguousarray(np.vstack([y_plane, uv_plane]))
                bgr = cv2.cvtColor(nv12, cv2.COLOR_YUV2BGR_NV12)
                ok, encoded = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
                if not ok:
                    continue
                with self._lock:
                    self._latest = encoded.tobytes()
                    self._at = time.monotonic()
                    self.encodes += 1
            except Exception as error:
                self.error = f"编码失败: {error}"
                LOG.debug("JPEG 编码失败: %s", error)
            time.sleep(1.0 / JPEG_FPS)

    def stats(self) -> dict:
        with self._lock:
            return {
                "ready": self._latest is not None,
                "encodes": self.encodes,
                "age_ms": int((time.monotonic() - self._at) * 1000) if self._at else -1,
                "error": self.error,
            }
