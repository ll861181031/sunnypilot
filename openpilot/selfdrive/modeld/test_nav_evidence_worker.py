"""Verify navigation receipts with CPU fakes and bounded Unix socket pairs."""
import gc
import importlib.util
import os
from pathlib import Path
import socket
import struct
import tempfile
import weakref
from array import array
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import numpy as np
import pytest

from openpilot.selfdrive.modeld import mgxd, nav_evidence_protocol as protocol
from openpilot.selfdrive.modeld.test_mgxd_engine import SPECS, construct


def worker_module():
  alternate = os.environ.get('TEST_NAV_EVIDENCE_MGXD')
  if not alternate:
    return mgxd
  spec = importlib.util.spec_from_file_location('evidence_worker_under_test', alternate)
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


def feeds():
  result = {name: np.zeros(shape, dtype) for name, (shape, dtype) in SPECS.items()}
  result['navigation'].fill(3)
  result['nav_map'][:] = np.arange(65536, dtype=np.float32).reshape(1, 1, 256, 256)
  return result


def test_receipt_is_fixed_size_and_round_trips_all_fields():
  evidence = {name: index + 11 for index, name in enumerate(protocol.FIELDS)}
  evidence.update(dict.fromkeys(protocol.FLAGS, True))
  encoded = protocol.encode_receipt(evidence)
  assert protocol.RECEIPT_SIZE == len(encoded) == 96
  decoded = protocol.decode_receipt(encoded)
  assert decoded == {'protocol_version': 1, **evidence}
  assert protocol.crc32_buffer(np.arange(47, dtype=np.float32)) == protocol.crc32_buffer(np.arange(47, dtype=np.float32).tobytes())


@pytest.mark.parametrize('kind', ['short', 'version', 'size', 'flags'])
def test_bad_receipt_is_rejected(kind):
  data = bytearray(protocol.encode_receipt({}))
  if kind == 'short':
    data.pop()
  elif kind == 'version':
    struct.pack_into('<H', data, 0, 99)
  elif kind == 'size':
    struct.pack_into('<H', data, 2, 4)
  else:
    struct.pack_into('<I', data, 4, 1 << 31)
  with pytest.raises(ValueError):
    protocol.decode_receipt(data)


def test_completed_engine_staging_and_gpu_reads_preserve_outputs(tmp_path, monkeypatch):
  _, hip, _, _, create = construct(tmp_path, monkeypatch, 1)
  engine = create()
  try:
    feed = feeds()
    before = mgxd.prepare_navigation_evidence(feed)
    output = engine.step(feed)
    output_bytes = output.tobytes()
    state = {name: argument.array.copy() for name, argument in engine.arguments.items() if name.startswith('state_')}
    start = len(hip.events)
    receipt = mgxd.finish_navigation_evidence(before, engine, output, worker_session=5, step_seq=1,
                                              started_ns=10, completed_ns=20, sample_gpu=True)
    assert hip.events[start:] == ['copy_2', 'copy_2', 'sync']
    assert receipt['feed_valid'] and receipt['staging_valid'] and receipt['gpu_sampled'] and receipt['gpu_match']
    assert receipt['capture'] and receipt['synchronized'] and receipt['output_valid']
    assert receipt['gpu_sample_mono_time'] > 0 and receipt['gpu_sample_duration_ns'] > 0
    assert receipt['feed_nav_crc32'] == receipt['staging_nav_crc32'] == receipt['gpu_nav_crc32']
    assert receipt['feed_map_crc32'] == receipt['staging_map_crc32'] == receipt['gpu_map_crc32']
    assert receipt['output_crc32'] == protocol.crc32_buffer(output_bytes)
    assert output.tobytes() == output_bytes
    for name, expected in state.items():
      np.testing.assert_array_equal(engine.arguments[name].array, expected)
    start = len(hip.events)
    nonsample = engine.navigation_evidence(sample_gpu=False)
    assert hip.events[start:] == []
    assert nonsample['staging_valid'] and not nonsample['gpu_sampled'] and not nonsample['gpu_match']
    for name in ('gpu_nav_crc32', 'gpu_map_crc32', 'gpu_sample_mono_time', 'gpu_sample_duration_ns'):
      assert nonsample[name] == 0
  finally:
    engine.close()
  assert not engine.nav_readback_arguments and not engine.nav_readback_views


def test_gpu_readback_mismatch_is_evidence_not_inference_failure(tmp_path, monkeypatch):
  _, _, _, _, create = construct(tmp_path, monkeypatch, 1)
  engine = create()
  try:
    feed = feeds()
    output = engine.step(feed)
    engine.arguments['navigation'].array.fill(99)
    evidence = engine.navigation_evidence(sample_gpu=True)
    assert evidence['gpu_sampled'] and not evidence['gpu_match']
    assert not evidence['gpu_error'] and not evidence['diagnostic_error']
    assert np.isfinite(output).all()
    assert np.isfinite(engine.step(feed)).all()
  finally:
    engine.close()


def test_gpu_readback_failure_is_bounded_and_next_step_survives(tmp_path, monkeypatch):
  _, hip, _, _, create = construct(tmp_path, monkeypatch, 1)
  engine = create()
  original = hip.copy
  try:
    feed = feeds()
    first = engine.step(feed)
    def fail_readback(destination, source, size, kind, stream=None):
      if kind == 2:
        raise RuntimeError('diagnostic D2H failed')
      return original(destination, source, size, kind, stream)
    monkeypatch.setattr(hip, 'copy', fail_readback)
    evidence = engine.navigation_evidence(sample_gpu=True)
    assert evidence['staging_valid'] and evidence['gpu_error'] and evidence['diagnostic_error']
    assert evidence['error_code'] & protocol.ERROR_GPU
    assert not evidence['gpu_sampled'] and not evidence['gpu_match']
    assert np.isfinite(first).all() and np.isfinite(engine.step(feed)).all()
  finally:
    engine.close()


def exercise_pending_readback_failure(tmp_path, monkeypatch, failure, recovery):
  _, hip, allocations, program, create = construct(tmp_path, monkeypatch, 1)
  engine = create()
  baseline_path = tmp_path / 'baseline'
  baseline_path.mkdir()
  _, _, baseline_allocations, _, create_baseline = construct(baseline_path, monkeypatch, 1)
  baseline = create_baseline()
  original_copy, original_sync = hip.copy, hip.synchronize
  copies = []
  sync_failed = False
  owners = {}
  try:
    feed = feeds()
    first = engine.step(feed)
    expected_first = baseline.step(feed)
    np.testing.assert_array_equal(first, expected_first)
    first_bytes = first.tobytes()
    calls = program.run_calls
    launches = hip.events.count('launch')

    def failing_copy(destination, source, size, kind, stream=None):
      if kind == 2:
        copies.append(destination)
        if failure == 'second_copy' and len(copies) == 2:
          raise RuntimeError('second diagnostic D2H failed after the first was queued')
      return original_copy(destination, source, size, kind, stream)

    def failing_sync(stream=None):
      nonlocal sync_failed
      if failure == 'sync' and not sync_failed:
        sync_failed = True
        raise RuntimeError('diagnostic sync failed with both D2H copies still queued')
      return original_sync(stream)

    monkeypatch.setattr(hip, 'copy', failing_copy)
    monkeypatch.setattr(hip, 'synchronize', failing_sync)
    evidence = engine.navigation_evidence(sample_gpu=True)
    assert evidence['diagnostic_error'] and evidence['gpu_error'] and evidence['error_code'] & protocol.ERROR_GPU
    assert not evidence['gpu_sampled'] and not evidence['gpu_match']
    assert len(hip.pending) == (1 if failure == 'second_copy' else 2)
    assert engine.frames == 1 and program.run_calls == calls and hip.events.count('launch') == launches
    owners = {name: weakref.ref(argument) for name, argument in engine.nav_readback_arguments.items()}
    addresses = {name: argument.data_ptr() for name, argument in engine.nav_readback_arguments.items()}
    # Remove the fake allocator's extra ownership; only the engine may keep these alive.
    allocations.clear()
    baseline_allocations.clear()
    gc.collect()
    assert set(owners) == {'navigation', 'nav_map'}
    assert all(reference() is engine.nav_readback_arguments[name] for name, reference in owners.items())
    assert copies[0] == addresses['navigation'] and copies[1] == addresses['nav_map']
    assert first.tobytes() == first_bytes
    for name in engine.state_layout:
      np.testing.assert_array_equal(engine.arguments[name].array, baseline.arguments[name].array)

    def checked_sync(stream=None):
      assert all(reference() is engine.nav_readback_arguments[name] for name, reference in owners.items())
      original_sync(stream)
      # The old queued copies execute before a subsequent inference's new H2D uploads.
      np.testing.assert_array_equal(engine.nav_readback_views['navigation'], feed['navigation'])
      if failure == 'sync':
        np.testing.assert_array_equal(engine.nav_readback_views['nav_map'], feed['nav_map'])

    monkeypatch.setattr(hip, 'synchronize', checked_sync)
    if recovery == 'step':
      next_feed = feeds()
      next_feed['navigation'].fill(8)
      next_feed['nav_map'].fill(0)
      second = engine.step(next_feed)
      expected_second = baseline.step(next_feed)
      np.testing.assert_array_equal(second, expected_second)
      for name in engine.state_layout:
        np.testing.assert_array_equal(engine.arguments[name].array, baseline.arguments[name].array)
      assert engine.frames == 2 and program.run_calls == calls and hip.events.count('launch') == launches + 1
      assert first.tobytes() == first_bytes
    else:
      engine.close()
      assert engine.frames == 1 and program.run_calls == calls and hip.events.count('launch') == launches
    assert not hip.pending
  finally:
    engine.close()
    baseline.close()
  gc.collect()
  assert owners and all(reference() is None for reference in owners.values())
  assert not engine.nav_readback_arguments and not engine.nav_readback_views and not hip.pending
  assert hip.events[-1] == 'destroy_stream'


@pytest.mark.parametrize('recovery', ['step', 'close'])
def test_second_readback_copy_failure_retains_queued_destination(tmp_path, monkeypatch, recovery):
  exercise_pending_readback_failure(tmp_path, monkeypatch, 'second_copy', recovery)


@pytest.mark.parametrize('recovery', ['step', 'close'])
def test_readback_sync_failure_retains_both_queued_destinations(tmp_path, monkeypatch, recovery):
  exercise_pending_readback_failure(tmp_path, monkeypatch, 'sync', recovery)


def test_feed_and_staging_mismatch_cannot_claim_full_gpu_match():
  feed = feeds()
  receipt = mgxd.prepare_navigation_evidence(feed)
  evidence = {'staging_valid': True, 'staging_nav_crc32': receipt['feed_nav_crc32'] ^ 1,
              'staging_map_crc32': receipt['feed_map_crc32'], 'gpu_sampled': True,
              'gpu_nav_crc32': receipt['feed_nav_crc32'] ^ 1, 'gpu_map_crc32': receipt['feed_map_crc32'], 'gpu_match': True}
  engine = SimpleNamespace(capture=True, navigation_evidence=lambda **_: evidence)
  result = mgxd.finish_navigation_evidence(receipt, engine, b'output', worker_session=1, step_seq=2,
                                            started_ns=10, completed_ns=20, sample_gpu=True)
  assert not result['gpu_match']


def test_legacy_engine_receipt_does_not_claim_staging_or_gpu_confirmation():
  receipt = mgxd.finish_navigation_evidence(mgxd.prepare_navigation_evidence(feeds()), None, b'output',
                                             worker_session=1, step_seq=2, started_ns=10,
                                             completed_ns=20, sample_gpu=False)
  result = protocol.decode_receipt(protocol.encode_receipt(receipt))
  assert result['feed_valid'] and result['output_valid'] and not result['diagnostic_error']
  assert not any(result[name] for name in ('capture', 'synchronized', 'staging_valid', 'gpu_sampled', 'gpu_match'))


class EndServer(BaseException):
  pass


def socket_run(tmp_path, monkeypatch, requests, failure=None, alternate=False):
  worker = worker_module() if alternate else mgxd
  sock_path = tmp_path / 'worker.sock'
  client, accepted = socket.socketpair()
  client.settimeout(2)
  accepted.settimeout(2)
  sessions = [accepted]
  observed, samples = [], []
  outputs = []
  real_time = worker.time
  clock_ns = [10_000_000_000]

  def monotonic_ns():
    clock_ns[0] += 1000
    return clock_ns[0]

  monkeypatch.setattr(worker, 'time', SimpleNamespace(monotonic_ns=monotonic_ns, perf_counter=real_time.perf_counter))

  def accept():
    if not sessions:
      raise EndServer()
    return sessions.pop(), None

  fake_server = SimpleNamespace(bind=lambda path: Path(path).touch(), listen=lambda _: None,
                                accept=accept, close=lambda: None)
  socket_api = SimpleNamespace(AF_UNIX=socket.AF_UNIX, SOCK_STREAM=socket.SOCK_STREAM,
                               SOL_SOCKET=socket.SOL_SOCKET, SCM_RIGHTS=socket.SCM_RIGHTS,
                               CMSG_SPACE=socket.CMSG_SPACE, MSG_CTRUNC=socket.MSG_CTRUNC,
                               socket=lambda *_: fake_server)
  monkeypatch.setattr(worker, 'socket', socket_api)
  monkeypatch.setattr(worker, 'SOCK', str(sock_path))
  monkeypatch.setattr(worker, 'warp_camera', lambda *_: np.zeros((6, 128, 256), np.uint8))
  monkeypatch.delenv('MGXD_PROFILE', raising=False)
  from openpilot.selfdrive.modeld import mgxd_runtime
  monkeypatch.setattr(mgxd_runtime, 'audit_runtime', lambda _: None)

  def step(feed, audit=False):
    observed.append({name: feed[name].copy() for name in ('navigation', 'nav_map')})
    if len(observed) == 4:
      clock_ns[0] += 1_000_000_000
    output = np.full(18580, len(observed) + .25, np.float32)
    outputs.append(output.tobytes())
    return output

  def evidence(sample_gpu=False):
    samples.append(sample_gpu)
    if failure == 'evidence':
      raise RuntimeError('diagnostic implementation failed')
    latest = observed[-1]
    nav, image = protocol.crc32_buffer(latest['navigation']), protocol.crc32_buffer(latest['nav_map'])
    return {'staging_valid': True, 'staging_nav_crc32': nav, 'staging_map_crc32': image,
            'gpu_sampled': sample_gpu, 'gpu_match': sample_gpu,
            'gpu_sample_mono_time': 123 if sample_gpu else 0, 'gpu_sample_duration_ns': 7 if sample_gpu else 0,
            'gpu_nav_crc32': nav if sample_gpu else 0, 'gpu_map_crc32': image if sample_gpu else 0}

  engine = SimpleNamespace(capture=True, metadata=lambda: {'graph_audit': {}}, step=step,
                           navigation_evidence=evidence)
  if failure == 'encode':
    monkeypatch.setattr(worker, 'encode_receipt', lambda _: (_ for _ in ()).throw(ValueError('bad receipt')))
  if failure == 'prepare':
    monkeypatch.setattr(worker, 'prepare_navigation_evidence', lambda _: (_ for _ in ()).throw(ValueError('bad feed crc')))

  def run():
    try:
      worker._serve(SimpleNamespace(), SimpleNamespace(), engine)
    except EndServer:
      return
    finally:
      accepted.close()

  replies = []
  with tempfile.TemporaryFile() as camera, ThreadPoolExecutor(max_workers=1) as pool:
    camera.write(b'000000')
    camera.flush()
    future = pool.submit(run)
    try:
      client.sendall(b'MGX3' + struct.pack('<5i', 2, 2, 2, 4, 6))
      for flags, nav, image in requests:
        fixed = struct.pack('<30f', *([0.] * 30))
        client.sendmsg([bytes([flags]) + fixed], [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                                                  array('i', [camera.fileno(), camera.fileno()]))])
        if flags & 1:
          client.sendall(image.tobytes())
        if flags & 2:
          client.sendall(nav.tobytes())
        magic = worker.recv_exact(client, 4)
        receipt = protocol.decode_receipt(worker.recv_exact(client, protocol.RECEIPT_SIZE)) if magic == b'MGX4' else None
        output = bytes(worker.recv_exact(client, 18580 * 4))
        replies.append((magic, receipt, output))
    finally:
      client.close()
    future.result(timeout=3)
  return replies, observed, samples, outputs


def test_legacy_client_keeps_unchanged_reply_and_no_diagnostics(tmp_path, monkeypatch):
  feed = feeds()
  replies, observed, samples, outputs = socket_run(tmp_path, monkeypatch, [(3, feed['navigation'], feed['nav_map'])])
  assert replies == [(b'MGX3', None, outputs[0])]
  assert samples == []
  np.testing.assert_array_equal(observed[0]['navigation'], feed['navigation'])


def test_receipt_protocol_updates_cached_inputs_and_never_reuses_gpu_sample(tmp_path, monkeypatch):
  feed = feeds()
  zeros = np.zeros_like(feed['navigation'])
  requests = [(7, feed['navigation'], feed['nav_map']), (4, None, None), (6, zeros, None), (4, None, None)]
  replies, observed, samples, outputs = socket_run(tmp_path, monkeypatch, requests, alternate=True)
  assert samples == [True, False, False, True]
  session = None
  for index, (magic, receipt, output) in enumerate(replies):
    assert magic == b'MGX4'
    assert receipt['feed_valid'] and receipt['staging_valid'] and receipt['output_valid']
    assert receipt['synchronized'] and receipt['capture'] and not receipt['diagnostic_error']
    assert receipt['step_seq'] == index + 1 and receipt['completed_mono_time'] > 0
    session = receipt['worker_session'] if session is None else session
    assert receipt['worker_session'] == session and session != 0
    assert receipt['feed_nav_crc32'] == protocol.crc32_buffer(observed[index]['navigation'])
    assert receipt['feed_map_crc32'] == protocol.crc32_buffer(feed['nav_map'])
    assert receipt['output_crc32'] == protocol.crc32_buffer(output)
    assert output == outputs[index]
    assert receipt['gpu_sampled'] == (index in (0, 3)) and receipt['gpu_match'] == (index in (0, 3))
    if index in (1, 2):
      assert receipt['gpu_nav_crc32'] == receipt['gpu_map_crc32'] == receipt['gpu_sample_mono_time'] == 0
  assert replies[0][1]['feed_nav_crc32'] == replies[1][1]['feed_nav_crc32'] != replies[2][1]['feed_nav_crc32']


@pytest.mark.parametrize('failure', ['prepare', 'evidence', 'encode'])
def test_diagnostic_failure_still_returns_each_successful_inference(tmp_path, monkeypatch, failure):
  feed = feeds()
  replies, observed, _, outputs = socket_run(tmp_path, monkeypatch,
                                            [(7, feed['navigation'], feed['nav_map']), (4, None, None)], failure)
  assert len(observed) == len(replies) == 2
  for index, (magic, receipt, output) in enumerate(replies):
    assert magic == b'MGX4' and receipt['diagnostic_error']
    assert output == outputs[index]
