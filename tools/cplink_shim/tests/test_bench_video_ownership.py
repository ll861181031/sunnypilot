import fcntl
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import bench_video


@pytest.fixture
def video(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    instance = bench_video.BenchVideo()
    instance._spawn = MagicMock(side_effect=lambda *args: MagicMock(pid=123, poll=lambda: None))
    monkeypatch.setattr(instance, "_wait_service", lambda *args: True)
    monkeypatch.setattr(instance, "_wait_port", lambda *args: True)
    monkeypatch.setattr(bench_video.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(bench_video.os, "killpg", lambda *args: None)
    yield instance
    instance.stop()


def lock_path():
    return Path(os.environ["XDG_RUNTIME_DIR"]) / f"sunnypilot-{os.getuid()}.lock"


def test_driving_lock_blocks_bench_before_any_child_is_spawned(video):
    with lock_path().open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert not video.start()
        video._spawn.assert_not_called()


def test_bench_holds_driving_lock_until_children_are_stopped(video):
    assert video.start()
    with lock_path().open("a") as other:
        with pytest.raises(BlockingIOError):
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        video.stop()
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_an_existing_listener_does_not_hide_dead_webrtcd(monkeypatch):
    video = bench_video.BenchVideo()
    video.webrtcd = MagicMock(poll=lambda: 1)
    connect = MagicMock(return_value=MagicMock())
    monkeypatch.setattr(bench_video.socket, "create_connection", connect)
    assert not video._wait_port(.1)
    connect.assert_not_called()


def test_killed_children_are_reaped(video, monkeypatch):
    child = MagicMock(pid=123, poll=lambda: None)
    child.wait.side_effect = [bench_video.subprocess.TimeoutExpired("test", 0), 0]
    video.webcam = child
    video.stop()
    assert child.wait.call_count == 2


@pytest.mark.parametrize("wait_for", ["service", "port"])
def test_startup_waits_honor_stop_request(monkeypatch, wait_for):
    from openpilot.cereal import messaging

    video = bench_video.BenchVideo()
    video.stop_requested = True
    video.webrtcd = MagicMock(poll=lambda: None)
    submaster = MagicMock()
    submaster.return_value.valid = {}
    connect = MagicMock(side_effect=OSError)
    monkeypatch.setattr(messaging, "SubMaster", submaster)
    monkeypatch.setattr(bench_video.socket, "create_connection", connect)
    if wait_for == "service":
        assert not video._wait_service("narrowRoadCameraState", .01)
        submaster.assert_not_called()
    else:
        assert not video._wait_port(.01)
        connect.assert_not_called()
