"""Validate explicit resident-engine selection without loading HIP."""
from types import SimpleNamespace

import pytest

from openpilot.selfdrive.modeld import mgxd


def test_legacy_mode_keeps_existing_engine(monkeypatch):
  monkeypatch.delenv('MODELD_MGXD_ENGINE', raising=False)
  assert mgxd.create_engine(object()) is None


def test_capture_mode_loads_only_the_validated_engine(monkeypatch):
  import sys
  calls = []
  sentinel = object()

  def build(runtime, path):
    calls.append((runtime, path))
    return sentinel

  monkeypatch.setenv('MODELD_MGXD_ENGINE', 'capture')
  monkeypatch.setitem(sys.modules, 'openpilot.selfdrive.modeld.mgxd_engine', SimpleNamespace(ResidentEngine=build))
  runtime = object()
  assert mgxd.create_engine(runtime) is sentinel
  assert calls == [(runtime, mgxd.MODEL)]


def test_capture_initialization_failure_never_falls_back(monkeypatch):
  import sys

  def fail(*_):
    raise RuntimeError('HIP capture failed')

  monkeypatch.setenv('MODELD_MGXD_ENGINE', 'capture')
  monkeypatch.setitem(sys.modules, 'openpilot.selfdrive.modeld.mgxd_engine', SimpleNamespace(ResidentEngine=fail))
  with pytest.raises(RuntimeError, match='HIP capture failed'):
    mgxd.create_engine(object())


def test_unknown_mode_is_rejected(monkeypatch):
  monkeypatch.setenv('MODELD_MGXD_ENGINE', 'typo')
  with pytest.raises(ValueError, match='MODELD_MGXD_ENGINE'):
    mgxd.create_engine(object())


@pytest.mark.parametrize('failure', [False, True])
def test_engine_is_closed_when_serving_ends(monkeypatch, failure):
  import sys
  closed = []
  engine = SimpleNamespace(close=lambda: closed.append(True))
  monkeypatch.delenv('MODELD_MGXD_CPU', raising=False)
  monkeypatch.delenv('MODELD_MGXD_ROCM_ROOT', raising=False)
  monkeypatch.setenv('MODELD_MGXD_ENGINE', 'legacy')
  monkeypatch.setitem(sys.modules, 'cv2', SimpleNamespace())
  monkeypatch.setitem(sys.modules, 'migraphx', SimpleNamespace())
  monkeypatch.setattr(mgxd, 'create_engine', lambda _: engine)

  def run(*args):
    assert args[-1] is engine
    if failure:
      raise RuntimeError('injected failure')

  monkeypatch.setattr(mgxd, '_serve', run)
  if failure:
    with pytest.raises(RuntimeError, match='injected'):
      mgxd.serve()
  else:
    mgxd.serve()
  assert closed == [True]


def test_fragmented_request_keeps_fds_and_closes_incomplete_request():
  import os
  import socket
  from array import array
  from concurrent.futures import ThreadPoolExecutor

  data = bytes([0]) + bytes(120)
  fd = os.memfd_create('capture-protocol-test')
  os.write(fd, b'camera')
  try:
    for complete in (True, False):
      before = len(os.listdir('/proc/self/fd'))
      client, server = socket.socketpair()
      with client, server, ThreadPoolExecutor(max_workers=1) as pool:
        server.settimeout(2)
        future = pool.submit(mgxd.recv_frame, server)
        client.sendmsg([data[:7]], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array('i', [fd, fd]))])
        if complete:
          client.sendall(data[7:])
        client.shutdown(socket.SHUT_WR)
        message, descriptors = future.result(timeout=3)
        try:
          if complete:
            assert message == data and len(descriptors) == 2
            assert all(os.pread(value, 6, 0) == b'camera' for value in descriptors)
          else:
            assert message is None and not descriptors
        finally:
          for value in descriptors:
            os.close(value)
      assert len(os.listdir('/proc/self/fd')) == before
  finally:
    os.close(fd)
