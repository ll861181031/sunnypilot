#!/usr/bin/env python3
"""Camera debug web tool.

A standalone, read-only-with-respect-to-the-vehicle helper for this desktop
rig: pick one or two V4L2 cameras, set the mode, and watch the *measured*
read rate, per-camera and combined. It exists because the two cameras on this
machine share one USB 2.0 bus, so each one's rate depends on whether the other
is streaming - a number the driving stack never reports.

It never starts, stops or signals any other process. If a camera is already
held (camerad during a drive) the open fails and the holder PID is shown.

Run:
  .venv/bin/python tools/camera_debug/camera_debug.py --host 0.0.0.0 --port 8099
  .venv/bin/python tools/camera_debug/camera_debug.py --fake 2   # no hardware
"""

import argparse
import atexit
import json
import mimetypes
import os
import signal
import socket
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tools.camera_debug import devices
from tools.camera_debug.capture import (
  CameraSpec, CaptureSession, SessionLog, FOURCC_CHOICES,
)

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}
PREVIEW_DEFAULT_FPS = 10.0


def fake_devices(count: int) -> list:
  """Synthetic cameras, so the whole tool can be verified without hardware."""
  out = []
  for i in range(count):
    out.append({
      "node": f"fake:{i}", "index": 100 + i, "card": f"虚拟相机 {i + 1}（--fake）",
      "capture": True, "by_id": "", "holders": [], "usb": {},
      "modes": [
        {"width": 1920, "height": 1080, "fourcc": "MJPG", "fps": [30.0, 25.0, 20.0, 15.0, 10.0], "stepwise": False},
        {"width": 1344, "height": 760, "fourcc": "MJPG", "fps": [30.0, 20.0, 10.0], "stepwise": False},
        {"width": 1280, "height": 720, "fourcc": "MJPG", "fps": [30.0, 20.0, 10.0], "stepwise": False},
      ],
      "fake": True,
    })
  return out


class App:
  """Shared state: the current session, the archived runs, one lock."""

  def __init__(self, fake_count=0):
    self.lock = threading.RLock()
    self.session = None
    self.log = SessionLog()
    self.fake_count = fake_count

  def device_list(self):
    nodes = []
    if self.fake_count:
      nodes.extend(fake_devices(self.fake_count))
    nodes.extend(n.as_dict() for n in devices.enumerate_nodes())
    return {
      "v4l2_ctl": devices.have_v4l2_ctl(),
      "fake_count": self.fake_count,
      "cameras": nodes,
    }

  def start(self, payload):
    raw = payload.get("cameras") or []
    if not 1 <= len(raw) <= 2:
      raise ValueError("只支持同时采集 1 或 2 个摄像头")
    specs = []
    warnings = []
    nodes = {n.node: n for n in devices.enumerate_nodes()}
    for i, entry in enumerate(raw):
      spec = CameraSpec.from_dict(entry, i)
      if not spec.device:
        raise ValueError(f"相机{i + 1} 未选择设备")
      spec.fake = spec.device.startswith("fake:")
      if spec.fourcc not in FOURCC_CHOICES:
        raise ValueError(f"不支持的 fourcc：{spec.fourcc}")
      if not 1 <= spec.buffersize <= 8:
        raise ValueError("buffersize 必须在 1..8 之间")
      if not 1 <= spec.fps <= 120:
        raise ValueError("帧率必须在 1..120 之间")
      if spec.width < 16 or spec.height < 16 or spec.width % 2 or spec.height % 2:
        raise ValueError("分辨率必须是 >=16 的偶数（NV12 要求）")
      # A requested mode the hardware does not advertise is reported, not
      # rejected: the driver picks the closest one and the UI shows both the
      # requested and the negotiated values side by side.
      node = nodes.get(spec.device)
      if node is not None:
        ok, note = devices.mode_supported(node, spec.fourcc, spec.width, spec.height, spec.fps)
        if not ok:
          warnings.append(note)
      specs.append(spec)

    warnings.extend(self._shared_bus_warnings(specs, nodes))

    with self.lock:
      self.stop_locked()
      session = CaptureSession(specs, self.log)
      session.start()
      self.session = session
      # Give the threads a moment so the caller learns immediately whether the
      # device actually opened (a camera held by camerad fails here).
      deadline = time.monotonic() + 1.5
      while time.monotonic() < deadline and any(s.state == "starting" for s in session.streams):
        time.sleep(0.05)
      opened = [{"slot": s.slot, "device": s.spec.device, "state": s.state, "error": s.error}
                for s in session.streams]
    return {"ok": all(o["state"] != "failed" for o in opened), "warnings": warnings, "cameras": opened}

  @staticmethod
  def _shared_bus_warnings(specs, nodes) -> list:
    """Warn when both cameras will compete for one USB 2.0 bus.

    UVC reserves bus time per *altsetting*, so a camera that advertises 4K can
    demand a ~196 Mbit/s isochronous reservation even for a 1280x720 request.
    Two such cameras on one bus make the kernel refuse the second one with
    "Not enough bandwidth for altsetting N" - which the user sees as "only one
    video shows". Telling them up front beats letting them guess.
    """
    if len(specs) != 2:
      return []
    usbs = [(nodes.get(s.device).usb if nodes.get(s.device) else None) or {} for s in specs]
    if not all(u.get("bus") for u in usbs):
      return []
    if usbs[0]["bus"] != usbs[1]["bus"]:
      return []
    scope = "同一个上游端口之下" if usbs[0]["root_port"] == usbs[1]["root_port"] else "同一条 USB 总线"
    head = f"两台相机在{scope}（usb{usbs[0]['bus']}）：共用一条总线的等时带宽；本机实测 1080p+1080p 可以同时跑"
    tail = "。若哪一路起不来（内核报 Not enough bandwidth），再降分辨率/帧率或把一台相机换到另一条 USB 总线"
    return [head + tail]

  def stop_locked(self):
    if self.session is not None:
      self.session.stop()
      self.session = None

  def stop(self):
    with self.lock:
      self.stop_locked()

  def stats(self):
    with self.lock:
      if self.session is None:
        return {"running": False, "cameras": [], "combined": {"n_cameras": 0}}
      return self.session.snapshot()


class Handler(BaseHTTPRequestHandler):
  server_version = "camera-debug/1.0"
  # HTTP/1.0 keeps the MJPEG preview honest: the stream has no length, so the
  # only correct framing is "until the connection closes".
  protocol_version = "HTTP/1.0"
  app = None

  # -- helpers ------------------------------------------------------------
  def json(self, obj, code=200, headers=None):
    body = json.dumps(obj, ensure_ascii=False).encode()
    self.send_response(code)
    self.send_header("Content-Type", "application/json; charset=utf-8")
    self.send_header("Content-Length", str(len(body)))
    self.send_header("Cache-Control", "no-store")
    for k, v in (headers or {}).items():
      self.send_header(k, v)
    self.end_headers()
    self.wfile.write(body)

  def error_json(self, message, code=400):
    self.json({"error": message}, code=code)

  def read_body(self):
    length = int(self.headers.get("Content-Length") or 0)
    if length <= 0:
      return {}
    raw = self.rfile.read(length)
    try:
      return json.loads(raw.decode() or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
      raise ValueError(f"请求体不是合法 JSON：{exc}") from exc

  def log_message(self, fmt, *args):
    # A browser polls this API several times a second; keep the terminal for
    # events that matter.
    pass

  # -- routing ------------------------------------------------------------
  def do_GET(self):
    url = urlparse(self.path)
    try:
      if url.path == "/":
        return self.serve_static("index.html")
      if url.path == "/api/devices":
        return self.json(self.app.device_list())
      if url.path == "/api/stats":
        return self.json(self.app.stats())
      if url.path == "/api/sessions":
        return self.json({"sessions": self.app.log.all(), "comparisons": self.app.log.comparisons()})
      if url.path == "/api/export":
        now = datetime.now()
        payload = {"exported_at": now.strftime("%Y-%m-%d %H:%M:%S"),
                   "sessions": self.app.log.all(),
                   "comparisons": self.app.log.comparisons()}
        return self.json(payload, headers={
          "Content-Disposition": f"attachment; filename=camera_debug_{int(now.timestamp())}.json"})
      if url.path.startswith("/preview/"):
        return self.stream_preview(url)
      if url.path.startswith("/web/"):
        return self.serve_static(url.path[len("/web/"):])
      return self.error_json("未知路径", 404)
    except (BrokenPipeError, ConnectionResetError):
      return
    except ValueError as exc:
      return self.error_json(str(exc))
    except Exception as exc:
      return self.error_json(f"{type(exc).__name__}: {exc}", 500)

  def do_POST(self):
    url = urlparse(self.path)
    try:
      if url.path == "/api/start":
        return self.json(self.app.start(self.read_body()))
      if url.path == "/api/stop":
        self.app.stop()
        return self.json({"ok": True})
      if url.path == "/api/sessions/clear":
        self.app.log.clear()
        return self.json({"ok": True})
      return self.error_json("未知路径", 404)
    except ValueError as exc:
      return self.error_json(str(exc))
    except Exception as exc:
      return self.error_json(f"{type(exc).__name__}: {exc}", 500)

  # -- static / preview ---------------------------------------------------
  def serve_static(self, rel):
    rel = rel.split("?")[0].lstrip("/")
    path = os.path.normpath(os.path.join(WEB_DIR, rel))
    if not path.startswith(WEB_DIR) or not os.path.isfile(path):
      return self.error_json("文件不存在", 404)
    ext = os.path.splitext(path)[1]
    ctype = STATIC_TYPES.get(ext) or mimetypes.guess_type(path)[0] or "application/octet-stream"
    with open(path, "rb") as f:
      body = f.read()
    self.send_response(200)
    self.send_header("Content-Type", ctype)
    self.send_header("Content-Length", str(len(body)))
    self.send_header("Cache-Control", "no-store")
    self.end_headers()
    self.wfile.write(body)

  def stream_preview(self, url):
    try:
      slot = int(url.path.rsplit("/", 1)[1])
    except ValueError:
      return self.error_json("预览路径必须是 /preview/<slot>", 404)
    fps = float((parse_qs(url.query).get("fps") or [PREVIEW_DEFAULT_FPS])[0])
    min_interval = 1.0 / max(0.5, min(fps, 30.0))

    self.send_response(200)
    self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
    self.send_header("Cache-Control", "no-store")
    self.end_headers()

    last_id = -1
    while True:
      session = self.app.session
      if session is None or slot >= len(session.streams):
        return
      stream = session.streams[slot]
      if not stream.spec.preview:
        return
      stream.wait_preview(last_id, timeout=1.0)
      # A stop() while a browser tab is still streaming must end this loop too.
      if self.app.session is not session:
        return
      frame_id, jpeg = stream.encode_preview()
      if jpeg is None or frame_id == last_id:
        continue
      last_id = frame_id
      try:
        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                         + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n")
      except (BrokenPipeError, ConnectionResetError):
        return
      time.sleep(min_interval)


def parse_args(argv=None):
  p = argparse.ArgumentParser(description="摄像头调试 web 工具（设置分辨率/帧率，测读取速率）")
  p.add_argument("--host", default="0.0.0.0", help="监听地址（默认 0.0.0.0，局域网可访问；无鉴权）")
  p.add_argument("--port", type=int, default=8099, help="监听端口（默认 8099）")
  p.add_argument("--fake", type=int, default=0, metavar="N",
                 help="额外提供 N 个虚拟相机，用于无硬件自测（不占用真实设备）")
  return p.parse_args(argv)


def build_server(host, port, fake_count=0):
  app = App(fake_count=fake_count)
  handler = type("BoundHandler", (Handler,), {"app": app})
  httpd = ThreadingHTTPServer((host, port), handler)
  httpd.daemon_threads = True
  return httpd, app


def main(argv=None):
  args = parse_args(argv)
  try:
    httpd, app = build_server(args.host, args.port, args.fake)
  except OSError as exc:
    print(f"无法监听 {args.host}:{args.port}：{exc}", file=sys.stderr)
    return 1

  def cleanup(*_):
    app.stop()

  atexit.register(cleanup)

  def _raise_interrupt(signum, frame):
    # SIGTERM must release the cameras the same way Ctrl-C does, or the next
    # stack start finds them held.
    raise KeyboardInterrupt

  for sig in (signal.SIGINT, signal.SIGTERM):
    signal.signal(sig, _raise_interrupt)

  shown = "127.0.0.1" if args.host in ("0.0.0.0", "") else args.host
  print(f"摄像头调试： http://{shown}:{args.port}/   （监听 {args.host}:{args.port}）")
  print("提示：该工具会独占所选相机；排查完请先停采集再启动行车栈。")
  if args.fake:
    print(f"已启用 {args.fake} 个虚拟相机（设备名 fake:0 …），不会占用真实相机。")
  if args.host in ("0.0.0.0", "::"):
    hostname = socket.gethostname()
    try:
      lan = socket.gethostbyname(hostname)
      print(f"局域网地址： http://{lan}:{args.port}/  （无鉴权，同网段均可访问）")
    except OSError:
      pass
  try:
    httpd.serve_forever()
  except KeyboardInterrupt:
    print("\n正在停止…")
  finally:
    app.stop()
    httpd.server_close()
  print("已停止，相机已释放。")
  return 0


if __name__ == "__main__":
  sys.exit(main())
