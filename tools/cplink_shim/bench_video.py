#!/usr/bin/env python3
"""Bench video: current USB camera -> encoderd --stream -> webrtcd on 5001.

This is the real openpilot pipeline, just started without the full stack:

    webcamerad (V4L2 /dev/videoN) -> VisionIPC "camerad"
        -> encoderd --stream -> livestreamNarrowRoadEncodeData
        -> webrtcd (CPlink /stream endpoint)

The CPlink phone asks for cameras=["road"] and gets the narrow-road track.
"""
from __future__ import annotations

import argparse
import fcntl
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = "/data/sunnypilot"
OPENPILOT = f"{ROOT}/openpilot"


class BenchVideo:
    def __init__(self, camera: str = "0", port: int = 5001) -> None:
        self.camera = camera
        self.port = port
        self.webcam: subprocess.Popen | None = None
        self.encoderd: subprocess.Popen | None = None
        self.webrtcd: subprocess.Popen | None = None
        self.error = ""
        self._lock = None
        self.stop_requested = False

    @staticmethod
    def _env() -> dict:
        env = os.environ.copy()
        env.setdefault("NARROW_ROAD_CAM", "0")
        env.setdefault("PYTHONUNBUFFERED", "1")
        return env

    def _spawn(self, args: list[str], env: dict | None = None) -> subprocess.Popen:
        return subprocess.Popen(args, cwd=ROOT, env=env or self._env(),
                                start_new_session=True,
                                pass_fds=(self._lock.fileno(),) if self._lock is not None else ())

    def _wait_service(self, name: str, timeout: float = 10.0) -> bool:
        if self.stop_requested:
            return False
        try:
            from openpilot.cereal import messaging
            sm = messaging.SubMaster([name], ignore_alive=[name], ignore_avg_freq=[name])
        except Exception:
            return False
        deadline = time.monotonic() + timeout
        while not self.stop_requested and time.monotonic() < deadline:
            sm.update(200)
            if sm.valid.get(name):
                return True
        return False

    def _wait_port(self, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        while not self.stop_requested and time.monotonic() < deadline:
            if self.webrtcd is None or self.webrtcd.poll() is not None:
                return False
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
                    return True
            except OSError:
                time.sleep(0.2)
        return False

    def start(self) -> bool:
        lock_path = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / f"sunnypilot-{os.getuid()}.lock"
        try:
            self._lock = lock_path.open("a")
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.error = f"driving stack or another video bench owns {lock_path}: {exc}"
            if self._lock is not None:
                self._lock.close()
                self._lock = None
            return False
        env = self._env()
        env["NARROW_ROAD_CAM"] = self.camera
        self.webcam = self._spawn(
            [sys.executable, "-m", "openpilot.system.camerad.webcam.camerad"], env)
        if not self._wait_service("narrowRoadCameraState", 12.0):
            self.error = f"webcamerad did not publish narrowRoadCameraState (camera {self.camera})"
            self.stop()
            return False

        self.encoderd = self._spawn([f"{OPENPILOT}/system/loggerd/encoderd", "--stream"], env)
        if not self._wait_service("livestreamNarrowRoadEncodeData", 12.0):
            self.error = "encoderd --stream did not publish livestreamNarrowRoadEncodeData"
            self.stop()
            return False

        self.webrtcd = self._spawn(
            [sys.executable, "-m", "openpilot.system.webrtc.webrtcd",
             "--host", "0.0.0.0", "--port", str(self.port)], env)
        if not self._wait_port(12.0):
            self.error = f"webrtcd did not listen on {self.port}"
            self.stop()
            return False
        print(f"bench video ready: /dev/video{self.camera} -> 5001 (/stream, road track)", flush=True)
        return True

    def stop(self) -> None:
        for proc in (self.webrtcd, self.encoderd, self.webcam):
            if proc is None or proc.poll() is not None:
                continue
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (OSError, ProcessLookupError):
                try:
                    proc.terminate()
                except OSError:
                    pass
        deadline = time.monotonic() + 3.0
        for proc in (self.webrtcd, self.encoderd, self.webcam):
            if proc is None:
                continue
            remaining = max(0.1, deadline - time.monotonic())
            try:
                proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    pass
                proc.wait(timeout=3.0)
        self.webrtcd = self.encoderd = self.webcam = None
        if self._lock is not None:
            self._lock.close()
            self._lock = None


def main() -> int:
    parser = argparse.ArgumentParser(description="CPlink bench camera -> webrtcd 5001")
    parser.add_argument("--camera", default=os.getenv("CPLINK_BENCH_CAM", "0"),
                        help="V4L2 index (default 0)")
    parser.add_argument("--port", type=int, default=5001)
    args = parser.parse_args()

    video = BenchVideo(args.camera, args.port)

    def _stop(*_):
        video.stop_requested = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _stop)
    try:
        if not video.start():
            print(f"bench video failed: {video.error}", file=sys.stderr)
            return 2
        while not video.stop_requested:
            if any(proc is None or proc.poll() is not None for proc in (video.webcam, video.encoderd, video.webrtcd)):
                print("bench video child exited; stopping the remaining pipeline", file=sys.stderr)
                return 2
            time.sleep(1)
    finally:
        video.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
