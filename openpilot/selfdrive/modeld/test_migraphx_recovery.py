"""Exercise MIGraphX recovery without loading a GPU or starting modeld."""
import os
import socket
import struct
import sys
import tempfile
from array import array
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from openpilot.selfdrive.modeld import mgxd, modeld


class ProtocolPeer:
  def __init__(self, failure=None):
    self.failure = failure
    self.sent = []
    self.reply = bytearray()
    self.closed = False
    self.header_read = False

  def sendall(self, data):
    self.sent.append(bytes(data))

  def sendmsg(self, buffers, ancillary):
    if self.failure == "send":
      raise BrokenPipeError("peer disconnected while sending the frame")
    assert ancillary[0][:2] == (socket.SOL_SOCKET, socket.SCM_RIGHTS)
    assert len(array("i", ancillary[0][2])) == 2
    data = b"".join(buffers)
    self.sent.append(data)
    return len(data)

  def recv_into(self, target, length):
    if self.failure == "receive" or (self.failure == "payload" and self.header_read):
      raise TimeoutError("inference response unavailable")
    if not self.reply:
      if not self.sent:
        raise TimeoutError("server is still waiting for the new connection's handshake")
      assert self.sent[0] == b"MGX3" + struct.pack("<5i", 2, 2, 2, 4, 6)
      assert len(self.sent[1]) == 121
      assert self.sent[1][0] & 3 == 3
      assert len(self.sent[2]) == 256 * 256 * 4
      assert len(self.sent[3]) == 47 * 4
      magic = b"BAD!" if self.failure == "magic" else b"MGX3"
      self.reply.extend(magic + np.full(18580, 1.5, np.float32).tobytes())
    count = min(length, len(self.reply))
    target[:count] = self.reply[:count]
    del self.reply[:count]
    self.header_read = True
    return count

  def close(self):
    self.closed = True


def remote_state(first, replacements):
  state = object.__new__(modeld.MigraphxRemoteState)
  state._sock = first
  state._hello_sent = False
  state._last_map = state._last_nav = None
  state.prev_desire = np.zeros(8, np.float32)
  state._nav_src = SimpleNamespace(map=np.ones((1, 1, 256, 256), np.float32), vec=np.ones(47, np.float32), refresh=lambda: None)
  state._try_connect = lambda: replacements.pop(0)
  state.output_slices = {}
  state.slice_outputs = lambda output, slices: output
  state.parser = SimpleNamespace(parse_outputs=lambda output: output)
  return state


def run_frame(state, fds=(101, 102), after_enqueue=None):
  bufs = {name: SimpleNamespace(fd=fd, width=2, height=2, stride=2, uv_offset=4, data=b"000000")
          for name, fd in zip(("img", "big_img"), fds, strict=True)}
  desire = np.zeros(8, np.float32)
  desire[2] = 1
  inputs = {"desire_pulse": desire, "traffic_convention": np.zeros((1, 2), np.float32), "action_t": np.zeros((1, 2), np.float32)}
  return state.run(bufs, {name: np.eye(3, dtype=np.float32) for name in bufs}, inputs, after_enqueue=after_enqueue)


@pytest.mark.parametrize("failure", ["receive", "payload", "send", "magic"])
def test_reconnect_sends_handshake_and_complete_current_frame(failure):
  first, second = ProtocolPeer(failure), ProtocolPeer()
  state = remote_state(first, [second])

  output = run_frame(state)

  np.testing.assert_array_equal(output, np.full(18580, 1.5, np.float32))
  assert first.closed
  # A retry must preserve the rising-edge desire rather than consume it twice.
  assert struct.unpack_from("<30f", second.sent[1], 1)[20] == 1.0


def test_failed_retry_closes_connection_and_does_not_retry_forever():
  first, second = ProtocolPeer("receive"), ProtocolPeer("receive")
  remaining = [second]
  state = remote_state(first, remaining)

  with pytest.raises(TimeoutError):
    run_frame(state)

  assert remaining == []
  assert first.closed and second.closed


def test_initial_connection_delivers_model_output_without_reconnecting():
  peer = ProtocolPeer()
  state = remote_state(peer, [])
  np.testing.assert_array_equal(run_frame(state), np.full(18580, 1.5, np.float32))
  assert not peer.closed


def test_unchanged_second_frame_keeps_connection_and_navigation_cache():
  peer = ProtocolPeer()
  state = remote_state(peer, [])
  run_frame(state)
  run_frame(state)
  assert len(peer.sent) == 5
  assert len(peer.sent[-1]) == 121
  assert peer.sent[-1][0] & 3 == 0
  assert struct.unpack_from("<30f", peer.sent[-1], 1)[20] == 0.0


def test_successful_retry_calls_after_enqueue_once():
  state = remote_state(ProtocolPeer("receive"), [ProtocolPeer()])
  callbacks = []
  run_frame(state, after_enqueue=lambda: callbacks.append(True))
  assert callbacks == [True]


def test_parser_error_does_not_retry_inference():
  peer = ProtocolPeer()
  state = remote_state(peer, [])
  def fail_parse(output):
    raise RuntimeError("invalid model output")
  state.parser.parse_outputs = fail_parse
  with pytest.raises(RuntimeError, match="invalid model output"):
    run_frame(state)
  assert not peer.closed


def test_retry_over_real_unix_socket_transfers_camera_fds():
  def respond(conn):
    with conn:
      conn.settimeout(2.0)
      assert mgxd.recv_exact(conn, 24) == b"MGX3" + struct.pack("<5i", 2, 2, 2, 4, 6)
      frame, ancillary, _, _ = conn.recvmsg(121, socket.CMSG_SPACE(8))
      descriptors = array("i")
      for level, kind, data in ancillary:
        assert (level, kind) == (socket.SOL_SOCKET, socket.SCM_RIGHTS)
        descriptors.frombytes(data)
      try:
        assert len(descriptors) == 2
        frame += mgxd.recv_exact(conn, 121 - len(frame))
        assert frame[0] & 3 == 3
        assert struct.unpack_from("<30f", frame, 1)[20] == 1.0
        for fd in descriptors:
          assert os.pread(fd, 6, 0) == b"000000"
        extra = mgxd.recv_exact(conn, (65536 + 47) * 4)
        np.testing.assert_array_equal(np.frombuffer(extra, np.float32), np.ones(65536 + 47, np.float32))
        conn.sendall(b"MGX3")
        conn.sendall(np.full(18580, 1.5, np.float32).tobytes())
      finally:
        for fd in descriptors:
          os.close(fd)

  with tempfile.TemporaryFile() as camera, ThreadPoolExecutor(max_workers=1) as pool:
    camera.write(b"000000")
    camera.flush()
    client, server = socket.socketpair()
    client.settimeout(2.0)
    future = pool.submit(respond, server)
    with client:
      state = remote_state(ProtocolPeer("receive"), [client])
      result = run_frame(state, (camera.fileno(), camera.fileno()))
      np.testing.assert_array_equal(result, np.full(18580, 1.5, np.float32))
    future.result(timeout=3.0)


def test_remote_backend_failure_does_not_silently_start_another_gpu_backend(monkeypatch):
  monkeypatch.setenv("MODELD_BACKEND", "migraphx-remote")
  def unavailable(*args):
    raise RuntimeError("remote GPU backend unavailable")
  def unexpected_fallback(*args):
    pytest.fail("a failed GPU backend must not initialize tinygrad on the same GPU")
  monkeypatch.setattr(modeld, "MigraphxRemoteState", unavailable)
  monkeypatch.setattr(modeld, "ModelState", unexpected_fallback)
  with pytest.raises(RuntimeError, match="remote GPU backend unavailable"):
    modeld.create_model_state(2, 2, False)


def test_server_start_logs_outside_tmp_and_closes_parent_handle(tmp_path, monkeypatch):
  expected = tmp_path / "state" / "sunnypilot" / "mgxd.log"
  monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
  monkeypatch.delenv("MODELD_MGXD_LOG", raising=False)
  opened = []
  def checked_open(path, *args, **kwargs):
    assert Path(path) == expected
    handle = open(path, *args, **kwargs)
    opened.append(handle)
    return handle
  monkeypatch.setattr(modeld, "open", checked_open, raising=False)
  calls = []
  monkeypatch.setattr(modeld.subprocess, "Popen", lambda *args, **kwargs: calls.append(kwargs))
  state = remote_state(None, [None, ProtocolPeer()])

  state._ensure_server()

  assert expected.is_file()
  assert opened[0].closed
  assert calls[0]["stdout"] is calls[0]["stderr"]


def test_log_override_and_spawn_failure_close_parent_handle(tmp_path, monkeypatch):
  expected = tmp_path / "custom" / "mgxd.log"
  monkeypatch.setenv("MODELD_MGXD_LOG", str(expected))
  def checked_open(path, *args, **kwargs):
    assert Path(path) == expected
    return open(path, *args, **kwargs)
  monkeypatch.setattr(modeld, "open", checked_open, raising=False)
  handles = []
  def fail_spawn(*args, **kwargs):
    handles.append(kwargs["stdout"])
    raise OSError("injected spawn failure")
  monkeypatch.setattr(modeld.subprocess, "Popen", fail_spawn)
  with pytest.raises(OSError, match="injected spawn failure"):
    remote_state(None, [None])._ensure_server()
  assert expected.is_file()
  assert handles[0].closed


def test_gpu_fault_exits_daemon_without_reloading_damaged_context(tmp_path, monkeypatch):
  model_file = tmp_path / "model.mxr"
  model_file.touch()
  monkeypatch.setattr(mgxd, "MODEL", str(model_file))
  monkeypatch.setattr(mgxd, "SOCK", str(tmp_path / "mgxd.sock"))
  monkeypatch.delenv("MODELD_MGXD_CPU", raising=False)
  monkeypatch.setitem(sys.modules, "cv2", SimpleNamespace())
  load_calls = []
  def fail_inference(feed):
    raise RuntimeError("HIP error: unspecified launch failure")
  def load_program(path):
    load_calls.append(path)
    return SimpleNamespace(run=fail_inference)
  monkeypatch.setitem(sys.modules, "migraphx", SimpleNamespace(load=load_program, argument=lambda value: value))
  monkeypatch.setattr(mgxd, "warp_camera", lambda *args: np.zeros((6, 128, 256), np.uint8))

  with tempfile.TemporaryFile() as camera:
    camera.write(b"000000")
    camera.flush()
    fds = [os.dup(camera.fileno()), os.dup(camera.fileno())]
    hello = bytearray(b"MGX3" + struct.pack("<5i", 2, 2, 2, 4, 6))
    frames = [(bytes([0]) + struct.pack("<30f", *([0.0] * 30)),
               [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array("i", fds).tobytes())], 0, None), (b"", [], 0, None)]
    def read_hello(target, n):
      count = min(n, len(hello))
      target[:count] = hello[:count]
      del hello[:count]
      return count
    conn = SimpleNamespace(recv_into=read_hello, recvmsg=lambda *args: frames.pop(0), close=lambda: None)
    accepted = [conn]
    def accept():
      if not accepted:
        pytest.fail("GPU fault must stop this daemon before accepting another client")
      return accepted.pop(0), None
    server = SimpleNamespace(bind=lambda path: Path(path).touch(), listen=lambda count: None, accept=accept, close=lambda: None)
    fake_socket = SimpleNamespace(AF_UNIX=socket.AF_UNIX, SOCK_STREAM=socket.SOCK_STREAM, SOL_SOCKET=socket.SOL_SOCKET,
                                  SCM_RIGHTS=socket.SCM_RIGHTS, CMSG_SPACE=socket.CMSG_SPACE, socket=lambda *args: server)
    monkeypatch.setattr(mgxd, "socket", fake_socket)
    try:
      with pytest.raises(RuntimeError, match="GPU fault"):
        mgxd.serve()
    finally:
      for fd in fds:
        try:
          os.close(fd)
        except OSError:
          pass
  assert load_calls == [str(model_file)]
