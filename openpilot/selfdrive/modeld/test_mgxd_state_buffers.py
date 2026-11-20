"""Check daemon recurrent-buffer reuse without loading any GPU runtime."""
import multiprocessing
import os
import socket
import struct
import sys
import time
from array import array
from types import SimpleNamespace

import numpy as np
import pytest

from openpilot.selfdrive.modeld import mgxd


def run_daemon(mode, fault, model_file, sock_path, sender):
  monkeypatch = pytest.MonkeyPatch()
  monkeypatch.setattr(mgxd, 'MODEL', str(model_file))
  monkeypatch.setattr(mgxd, 'SOCK', str(sock_path))
  monkeypatch.delenv('MODELD_MGXD_CPU', raising=False)
  monkeypatch.delenv('MGXD_PROFILE', raising=False)
  monkeypatch.setitem(sys.modules, 'cv2', SimpleNamespace())
  monkeypatch.setattr(mgxd, 'warp_camera', lambda *args: np.zeros((6, 128, 256), np.uint8))
  observations = []
  previous_outputs = {}

  def run(feed):
    if fault == 'feedback_assertion':
      raise AssertionError('Injected recurrent feedback assertion failure')
    frame = len(observations)
    states = {k: v for k, v in feed.items() if k.startswith('state_')}
    observed = {}
    for name, value in states.items():
      expected = 0 if frame == 0 or (mode == 'wrong_shape' and name == 'state_desire_q') else frame + 0.25
      if value.dtype == np.uint8:
        expected = int(expected)
      np.testing.assert_array_equal(value, np.full_like(value, expected))
      assert value.flags.owndata
      if name in previous_outputs:
        assert not np.shares_memory(value, previous_outputs[name])
      observed[name] = value.ctypes.data
    observations.append(observed)
    if frame == 2:
      raise RuntimeError('HIP test end: stop the bounded CPU daemon')
    outputs = [np.full(18580, frame + 1, np.float32)]
    for name, value in states.items():
      dtype = np.float64 if mode == 'cast_dtype' else value.dtype
      shape = (1,) if mode == 'wrong_shape' and name == 'state_desire_q' else value.shape
      result = np.full(shape, frame + 1.25, dtype=dtype)
      previous_outputs[name] = result
      outputs.append(result)
    return outputs

  monkeypatch.setitem(sys.modules, 'migraphx', SimpleNamespace(
    load=lambda _: SimpleNamespace(run=run), argument=lambda value: value))
  try:
    mgxd.serve()
  except BaseException as exc:
    sender.send((type(exc).__name__, str(exc), observations))
  else:
    sender.send((None, '', observations))
  finally:
    sender.close()
    monkeypatch.undo()


def exercise_daemon(tmp_path, mode, fault=None, cleanup=None):
  model_file = tmp_path / 'model.mxr'
  model_file.touch()
  sock_path = tmp_path / 'daemon.sock'
  # A separate process remains stoppable when serve catches a test assertion or
  # a client-side failure leaves it blocked in recvmsg/accept. Spawn avoids
  # inheriting native OpenCV worker locks from earlier tests.
  context = multiprocessing.get_context('spawn')
  receiver, sender = context.Pipe(duplex=False)
  process = context.Process(target=run_daemon, args=(mode, fault, model_file, sock_path, sender))
  fd = os.memfd_create('mgxd-cpu-state-test', os.MFD_CLOEXEC)
  os.write(fd, b'000000')
  try:
    process.start()
    sender.close()
    deadline = time.monotonic() + 3
    while not sock_path.exists():
      assert process.is_alive(), 'CPU daemon exited before opening its socket'
      assert time.monotonic() < deadline, 'CPU daemon socket startup timed out'
      time.sleep(0.01)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
      client.settimeout(0.5 if fault else 3)
      client.connect(str(sock_path.with_name('missing.sock') if fault == 'connection_error' else sock_path))
      client.sendall(b'MGX3' + struct.pack('<5i', 2, 2, 2, 4, 6))
      for frame in range(3):
        client.sendmsg([bytes([0]) + struct.pack('<30f', *([0.] * 30))],
                       [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array('i', [fd, fd]))])
        if frame < 2:
          reply = mgxd.recv_exact(client, 4 + 18580 * 4)
          assert reply[:4] == (b'BAD!' if fault == 'reply_assertion' else b'MGX3')
          np.testing.assert_array_equal(np.frombuffer(reply, np.float32, offset=4),
                                        np.full(18580, frame + 1, np.float32))
      assert receiver.poll(4), 'CPU daemon did not exit after its bounded run'
      exception_type, error, observations = receiver.recv()
      assert exception_type == 'RuntimeError' and 'GPU fault' in error, (exception_type, error)
  finally:
    if process.pid is not None:
      process.join(timeout=0.1)
      if process.is_alive():
        process.terminate()
        process.join(timeout=1)
      if process.is_alive():
        process.kill()
        process.join(timeout=1)
      if cleanup is not None:
        cleanup.update(pid=process.pid, reaped=not process.is_alive(), exitcode=process.exitcode)
      assert not process.is_alive(), 'CPU daemon survived bounded cleanup'
      process.close()
    receiver.close()
    sender.close()
    os.close(fd)
  return observations


@pytest.mark.parametrize('mode', ['same_dtype', 'cast_dtype', 'wrong_shape'])
def test_daemon_reuses_owned_state_buffers_and_preserves_feedback(tmp_path, mode):
  observations = exercise_daemon(tmp_path, mode)
  assert len(observations) == 3
  assert observations[1] == observations[0], 'Per-frame feedback must reuse the existing state allocations'
  assert observations[2] == observations[0], 'State pointers must remain stable across frames'


@pytest.mark.parametrize('fault,exception', [
  ('feedback_assertion', TimeoutError),
  ('reply_assertion', AssertionError),
  ('connection_error', FileNotFoundError),
])
def test_daemon_cleanup_is_bounded_on_failure(tmp_path, fault, exception):
  cleanup = {}
  started = time.monotonic()
  with pytest.raises(exception):
    exercise_daemon(tmp_path, 'same_dtype', fault=fault, cleanup=cleanup)
  assert time.monotonic() - started < 8, 'Failure cleanup exceeded its bounded timeout'
  assert cleanup['reaped'] and cleanup['exitcode'] is not None
