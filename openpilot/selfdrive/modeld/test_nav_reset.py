"""Verify independent navigation history reset without GPU or driving processes."""
from types import SimpleNamespace
from array import array
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import socket
import struct
import tempfile

import numpy as np
import pytest

from openpilot.selfdrive.modeld import mgxd, modeld, nav_evidence_protocol as protocol
from openpilot.selfdrive.modeld.test_mgxd_engine import SPECS, construct
from openpilot.selfdrive.modeld.test_migraphx_recovery import ProtocolPeer, remote_state, run_frame


class Queue:
  def __init__(self):
    self.value = np.full((3, 1, 2), 7, np.float32)
    self.realized = 0

  def assign(self, value):
    self.value[:] = value
    return self

  def realize(self):
    self.realized += 1
    return self


@pytest.mark.parametrize('backend', [modeld.ModelState, modeld.MigraphxModelState])
@pytest.mark.parametrize('channel', ['map', 'navigation'])
def test_host_reset_is_independent_and_idempotent(backend, channel):
  state = object.__new__(backend)
  state.nav = True
  state.prev_desire = np.arange(8, dtype=np.float32)
  state.input_queues = {name: Queue() for name in ('img_q', 'big_img_q', 'feat_q', 'desire_q', 'state_map_q', 'state_nav_q')}
  state._state = {name: np.full((3, 1, 2), 7, np.float32) for name in
                  ('state_img_q', 'state_feat_q', 'state_desire_q', 'state_map_features_q', 'state_navigation_features_q')}
  request = {'map_epoch' if channel == 'map' else 'navigation_epoch': 4}
  state.request_nav_reset(**request)
  assert state.pending_nav_resets() == {channel: 4}
  assert state.nav_reset_applied == {}
  state._apply_pending_nav_reset()
  arrays = ({key: value.value for key, value in state.input_queues.items()}
            if backend is modeld.ModelState else state._state)
  selected = (('state_map_q' if channel == 'map' else 'state_nav_q') if backend is modeld.ModelState else
              ('state_map_features_q' if channel == 'map' else 'state_navigation_features_q'))
  for name, value in arrays.items():
    np.testing.assert_array_equal(value, 0 if name == selected else 7)
  np.testing.assert_array_equal(state.prev_desire, np.arange(8))
  assert state.nav_reset_applied == {channel: 4}
  arrays[selected].fill(11)
  state.request_nav_reset(**request)
  state._apply_pending_nav_reset()
  np.testing.assert_array_equal(arrays[selected], 11)
  state.request_nav_reset(map_epoch=5, navigation_epoch=5)
  state._apply_pending_nav_reset()
  assert state.nav_reset_applied == {'map': 5, 'navigation': 5}


@pytest.mark.parametrize('epoch', [-1, True, 1.5, 1 << 64])
def test_invalid_epoch_is_rejected_without_partial_request(epoch):
  state = object.__new__(modeld.ModelState)
  with pytest.raises(ValueError):
    state.request_nav_reset(map_epoch=3, navigation_epoch=epoch)
  assert state.pending_nav_resets() == {}


def test_epoch_regression_is_rejected_and_pending_latest_is_kept():
  state = object.__new__(modeld.ModelState)
  state.request_nav_reset(map_epoch=5)
  with pytest.raises(ValueError):
    state.request_nav_reset(map_epoch=4)
  assert state.pending_nav_resets() == {'map': 5}


@pytest.mark.parametrize('backend', [modeld.ModelState, modeld.MigraphxModelState])
@pytest.mark.parametrize('channel', ['map', 'navigation'])
def test_backend_run_resets_before_model_evaluates(backend, channel, monkeypatch):
  monkeypatch.delenv('MODELD_PROFILE', raising=False)
  state = object.__new__(backend)
  state.nav = True
  state.chestnut = False
  state.prev_desire = np.zeros(8, np.float32)
  state.output_slices = {'hidden_state': slice(0, 0)}
  state.parser = SimpleNamespace(parse_outputs=lambda output: output)
  source = SimpleNamespace(map=np.ones((1, 1, 256, 256), np.float32), vec=np.ones(47, np.float32), refresh=lambda: None)
  state.nav_src = state._nav_src = source
  observed = []
  if backend is modeld.ModelState:
    state.input_queues = {name: Queue() for name in ('img_q', 'big_img_q', 'feat_q', 'desire_q', 'state_map_q', 'state_nav_q')}
    state.modeld_inputs = tuple(state.input_queues)
    state.frame_copy_size = 6
    state.frame_views = {name: np.zeros(6, np.uint8) for name in ('img', 'big_img')}
    state.npy = {'desire': np.zeros(8, np.float32), 'traffic_convention': np.zeros((1, 2), np.float32),
                 'action_t': np.zeros((1, 2), np.float32), 'tfm': np.zeros((3, 3), np.float32),
                 'big_tfm': np.zeros((3, 3), np.float32), 'nav_map': np.zeros((1, 1, 256, 256), np.uint8),
                 'nav': np.zeros(47, np.float32), 'prev_feat': np.zeros(0, np.float32)}
    def run_model(**queues):
      observed.append({name: value.value.copy() for name, value in queues.items()})
      return [SimpleNamespace(numpy=lambda: np.zeros((1, 18580), np.float32))]
    state.run_model = run_model
    selected = 'state_map_q' if channel == 'map' else 'state_nav_q'
  else:
    state._state = {name: np.full((3, 1, 2), 7, np.float32) for name in
                    ('state_img_q', 'state_feat_q', 'state_desire_q', 'state_map_features_q', 'state_navigation_features_q')}
    def run(feed):
      observed.append({name: value.copy() for name, value in feed.items() if name.startswith('state_')})
      return [np.zeros(18580, np.float32)]
    state._prog = SimpleNamespace(run=run)
    state._mgx = SimpleNamespace(argument=lambda value: value)
    state._output_names = ('outputs',)
    state._warp_camera = lambda *_: np.zeros((6, 128, 256), np.uint8)
    selected = 'state_map_features_q' if channel == 'map' else 'state_navigation_features_q'
  state.request_nav_reset(**{'map_epoch' if channel == 'map' else 'navigation_epoch': 1})
  run_frame(state)
  assert len(observed) == 1
  for name, value in observed[0].items():
    np.testing.assert_array_equal(value, 0 if name == selected else 7)
  assert state.nav_reset_applied == {channel: 1}


@pytest.mark.parametrize('capture', [False, True])
@pytest.mark.parametrize('map_reset,navigation_reset', [(True, False), (False, True), (True, True)])
def test_resident_reset_only_zeros_selected_history_on_same_stream(tmp_path, monkeypatch, capture, map_reset, navigation_reset):
  _, hip, _, _, create = construct(tmp_path, monkeypatch, capture)
  engine = create()
  try:
    feed = {name: np.zeros(shape, dtype) for name, (shape, dtype) in SPECS.items()}
    engine.step(feed)
    before = engine.snapshot_state_host()
    addresses = {name: value['address'] for name, value in engine.state_layout.items()}
    original_zero = hip.zero
    zeros = []
    def zero(address, size, stream):
      assert stream == engine.stream
      zeros.append(address)
      original_zero(address, size, stream)
    monkeypatch.setattr(hip, 'zero', zero)
    engine.reset_navigation(map_reset=map_reset, navigation_reset=navigation_reset)
    selected = set()
    if map_reset:
      selected.add('state_map_features_q')
    if navigation_reset:
      selected.add('state_navigation_features_q')
    assert set(zeros) == {addresses[name] for name in selected}
    after = engine.snapshot_state_host()
    for name in before:
      np.testing.assert_array_equal(after[name], 0 if name in selected else before[name])
    assert engine.frames == 1
    engine.step(feed)
    final = engine.snapshot_state_host()
    for name in final:
      np.testing.assert_array_equal(final[name], 1 if name in selected else before[name] + 1)
    assert addresses == {name: value['address'] for name, value in engine.state_layout.items()}
  finally:
    engine.close()


class ResetPeer(ProtocolPeer):
  def __init__(self, *, flags=24, magic=b'MGX5', sequence=1, bad_header=False, failure=None, reset_supported=True):
    super().__init__(failure)
    output = np.full(18580, 1.5, np.float32).tobytes()
    receipt = protocol.encode_receipt({'worker_session': 7, 'step_seq': sequence, 'navigation_reset_supported': reset_supported})
    ack = struct.pack('<HHIQQ', 99 if bad_header else 1, 24, flags, 7, sequence)
    self.reply.extend(magic + (ack + receipt if magic == b'MGX5' else receipt if magic == b'MGX4' else b'') + output)


def test_new_client_explicitly_requests_session_reset_without_epoch_side_effect():
  peer = ResetPeer(flags=24, reset_supported=True)
  state = remote_state(peer, [])
  run_frame(state)
  assert peer.sent[1][0] & 32
  assert state.nav_session_reset_confirmed
  assert state.nav_reset_applied == {} and state.pending_nav_resets() == {}


def test_old_worker_ignores_session_request_and_remains_explicitly_unconfirmed():
  peer = ProtocolPeer()
  state = remote_state(peer, [])
  run_frame(state)
  assert peer.sent[1][0] & 32
  assert not state.nav_session_reset_confirmed
  assert state.nav_reset_applied == {}


def test_capable_worker_without_session_ack_does_not_retry_or_publish_result():
  peer = ResetPeer(magic=b'MGX4', reset_supported=True)
  replacements = [ProtocolPeer()]
  state = remote_state(peer, replacements)
  with pytest.raises(RuntimeError, match='navigation reset'):
    run_frame(state)
  assert not state.nav_session_reset_confirmed and peer.closed
  assert len(replacements) == 1
  assert sum(len(item) == 121 for item in peer.sent) == 1


def test_truncated_session_ack_does_not_retry_frame_after_mgx5_prefix():
  peer = ResetPeer(failure='payload')
  replacements = [ProtocolPeer()]
  state = remote_state(peer, replacements)
  with pytest.raises(TimeoutError):
    run_frame(state)
  assert not state.nav_session_reset_confirmed and peer.closed
  assert len(replacements) == 1
  assert sum(len(item) == 121 for item in peer.sent) == 1


@pytest.mark.parametrize('bad_receipt', ['invalid', 'missing_capability', 'worker_mismatch'])
def test_session_ack_needs_valid_same_frame_capable_receipt(bad_receipt):
  peer = ResetPeer(flags=24, reset_supported=True)
  receipt_start = 4 + protocol.RESET_ACK_SIZE
  if bad_receipt == 'invalid':
    peer.reply[receipt_start:receipt_start + 2] = b'\xff\xff'
  elif bad_receipt == 'missing_capability':
    struct.pack_into('<I', peer.reply, receipt_start + 4, 0)
  else:
    struct.pack_into('<Q', peer.reply, receipt_start + 8, 8)
  state = remote_state(peer, [ProtocolPeer()])
  with pytest.raises(RuntimeError, match='navigation reset'):
    run_frame(state)
  assert not state.nav_session_reset_confirmed and state.nav_reset_applied == {}
  assert peer.closed and sum(len(item) == 121 for item in peer.sent) == 1


def test_remote_reset_requires_same_frame_ack_and_does_not_repeat_epoch():
  peer = ResetPeer(flags=24)
  state = remote_state(peer, [])
  state.request_nav_reset(map_epoch=3, navigation_epoch=9)
  run_frame(state)
  assert peer.sent[1][0] & 24 == 24
  assert state.nav_reset_applied == {'map': 3, 'navigation': 9}
  assert state.pending_nav_resets() == {}
  state.request_nav_reset(map_epoch=3, navigation_epoch=9)
  run_frame(state)
  assert peer.sent[-1][0] & 24 == 0


@pytest.mark.parametrize('kwargs', [{'magic': b'MGX3'}, {'magic': b'MGX4'}, {'flags': 16},
                                   {'bad_header': True}, {'sequence': 0}])
def test_remote_reset_bad_ack_is_not_success_and_never_repeats_frame(kwargs):
  peer = ResetPeer(**kwargs)
  remaining = [ProtocolPeer()]
  state = remote_state(peer, remaining)
  state.request_nav_reset(map_epoch=5)
  with pytest.raises(RuntimeError, match='navigation reset'):
    run_frame(state)
  assert state.nav_reset_applied == {}
  assert state.pending_nav_resets() == {'map': 5}
  assert len(remaining) == 1
  assert sum(len(item) == 121 for item in peer.sent) == 1


@pytest.mark.parametrize('failure', ['receive', 'send', 'payload'])
def test_remote_reset_transport_failure_does_not_retry_frame(failure):
  peer = ProtocolPeer(failure)
  remaining = [ProtocolPeer()]
  state = remote_state(peer, remaining)
  state.request_nav_reset(navigation_epoch=2)
  with pytest.raises((OSError, RuntimeError)):
    run_frame(state)
  assert state.pending_nav_resets() == {'navigation': 2}
  assert state.nav_reset_applied == {}
  assert len(remaining) == 1 and peer.closed


def test_remote_reset_rejects_stale_worker_sequence():
  peer = ResetPeer(sequence=2)
  state = remote_state(peer, [])
  state._nav_previous_worker = (7, 2)
  state.request_nav_reset(map_epoch=3)
  with pytest.raises(RuntimeError, match='navigation reset'):
    run_frame(state)
  assert state.nav_reset_applied == {}


def test_remote_reset_ack_must_match_the_same_response_receipt():
  peer = ResetPeer(sequence=2)
  struct.pack_into('<Q', peer.reply, 4 + 16, 3)
  state = remote_state(peer, [])
  state.request_nav_reset(map_epoch=3)
  with pytest.raises(RuntimeError, match='navigation reset'):
    run_frame(state)
  assert state.nav_reset_applied == {}


def test_remote_session_request_does_not_create_epochs_and_capability_expires_on_legacy_reply():
  peer = ResetPeer()
  state = remote_state(peer, [])
  assert not getattr(state, 'nav_reset_supported', False)
  run_frame(state)
  assert state.nav_reset_supported
  assert state.nav_session_reset_confirmed
  assert peer.sent[1][0] & 32
  assert peer.sent[1][0] & 24 == 0
  assert state.nav_reset_applied == {}
  run_frame(state)
  assert not state.nav_reset_supported
  assert not state.nav_session_reset_confirmed
  assert peer.sent[-1][0] & 24 == 0
  assert peer.sent[-1][0] & 32 == 0


def test_remote_reset_capability_does_not_survive_reconnection():
  state = remote_state(ResetPeer(), [ProtocolPeer()])
  run_frame(state)
  assert state.nav_reset_supported
  state._sock.close()
  state._ensure_server()
  assert not state.nav_reset_supported
  assert not state.nav_session_reset_confirmed


def test_reconnected_client_requests_new_session_and_combines_explicit_reset():
  first, second = ResetPeer(sequence=1), ResetPeer(sequence=2)
  state = remote_state(first, [second])
  run_frame(state)
  state._sock.close()
  state._ensure_server()
  state.request_nav_reset(map_epoch=5)
  run_frame(state)
  assert second.sent[1][0] & 32 and second.sent[1][0] & 8
  assert state.nav_session_reset_confirmed
  assert state.nav_reset_applied == {'map': 5}


def test_capability_flag_is_diagnostic_only_for_an_older_client(monkeypatch):
  def old_decode(payload):
    if protocol.RECEIPT_STRUCT.unpack(payload)[2] & (1 << 9):
      raise ValueError('Unsupported navigation receipt header')
    return protocol.decode_receipt(payload)
  monkeypatch.setattr(modeld, 'decode_receipt', old_decode)
  peer = ResetPeer(magic=b'MGX4', reset_supported=True)
  state = remote_state(peer, [])
  np.testing.assert_array_equal(run_frame(state), np.full(18580, 1.5, np.float32))
  assert sum(len(item) == 121 for item in peer.sent) == 1
  assert state.last_nav_evidence['receiptReason'] == 'invalidReceipt'
  assert not state.nav_reset_supported


@pytest.mark.parametrize('backend', [modeld.ModelState, modeld.MigraphxModelState])
def test_local_reset_capability_is_available_without_inference(backend):
  state = object.__new__(backend)
  assert state.nav_reset_supported


@pytest.mark.parametrize('capture', [False, True])
def test_worker_socket_resets_selected_history_before_one_inference(tmp_path, monkeypatch, capture):
  client, accepted = socket.socketpair()
  client.settimeout(2)
  accepted.settimeout(2)
  sessions = [accepted]
  observed = []
  class EndServer(BaseException):
    pass

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
  monkeypatch.setattr(mgxd, 'socket', socket_api)
  monkeypatch.setattr(mgxd, 'SOCK', str(tmp_path / 'worker.sock'))
  model_file = tmp_path / 'model.mxr'
  model_file.touch()
  monkeypatch.setattr(mgxd, 'MODEL', str(model_file))
  monkeypatch.setattr(mgxd, 'warp_camera', lambda *_: np.zeros((6, 128, 256), np.uint8))
  monkeypatch.delenv('MGXD_PROFILE', raising=False)
  from openpilot.selfdrive.modeld import mgxd_runtime
  monkeypatch.setattr(mgxd_runtime, 'audit_runtime', lambda _: None)
  state_names = ('state_img_q', 'state_desire_q', 'state_feat_q', 'state_map_features_q', 'state_navigation_features_q')
  device_states = {name: np.zeros(2, np.float32) for name in state_names}

  def step(feed, audit=False):
    states = device_states if capture else {name: feed[name] for name in state_names}
    observed.append({name: float(value.flat[0]) for name, value in states.items()})
    output = np.full(18580, len(observed), np.float32)
    next_state = [states[name] + 1 for name in state_names]
    if capture:
      for name, value in zip(state_names, next_state, strict=True):
        device_states[name][:] = value
      return output
    return [output, *next_state]

  def reset(*, map_reset=False, navigation_reset=False):
    if map_reset:
      device_states['state_map_features_q'].fill(0)
    if navigation_reset:
      device_states['state_navigation_features_q'].fill(0)

  engine = SimpleNamespace(capture=True, metadata=lambda: {'graph_audit': {}}, step=step,
                           reset_navigation=reset, navigation_evidence=lambda **_: {}) if capture else None
  runtime = SimpleNamespace(load=lambda _: SimpleNamespace(run=step), argument=lambda value: value)
  def run():
    try:
      mgxd._serve(SimpleNamespace(), runtime, engine)
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
      for flags in (4, 12, 4, 20, 28):
        client.sendmsg([bytes([flags]) + struct.pack('<30f', *([0.] * 30))],
                       [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array('i', [camera.fileno(), camera.fileno()]))])
        magic = bytes(mgxd.recv_exact(client, 4))
        ack = protocol.decode_reset_ack(mgxd.recv_exact(client, protocol.RESET_ACK_SIZE)) if magic == b'MGX5' else None
        receipt = protocol.decode_receipt(mgxd.recv_exact(client, protocol.RECEIPT_SIZE))
        output = np.frombuffer(mgxd.recv_exact(client, 18580 * 4), np.float32)
        replies.append((magic, ack, receipt))
        np.testing.assert_array_equal(output, len(replies))
    finally:
      client.close()
    future.result(timeout=3)
  assert len(observed) == 5
  expected_map = [0, 0, 1, 2, 0]
  expected_nav = [0, 1, 2, 0, 0]
  for index, values in enumerate(observed):
    assert values['state_map_features_q'] == expected_map[index]
    assert values['state_navigation_features_q'] == expected_nav[index]
    assert all(values[name] == index for name in state_names[:3])
    magic, ack, receipt = replies[index]
    assert receipt['step_seq'] == index + 1
    assert receipt['navigation_reset_supported']
    if index in (1, 3, 4):
      assert magic == b'MGX5'
      assert ack['flags'] == {1: 8, 3: 16, 4: 24}[index]
      assert ack['worker_session'] == receipt['worker_session']
      assert ack['step_seq'] == receipt['step_seq']
    else:
      assert magic == b'MGX4' and ack is None


@pytest.mark.parametrize('capture', [False, True])
def test_new_clients_clear_old_worker_navigation_while_legacy_client_keeps_its_behavior(tmp_path, monkeypatch, capture):
  pairs = [socket.socketpair() for _ in range(3)]
  for pair in pairs:
    for connection in pair:
      connection.settimeout(2)
  sessions = [pair[1] for pair in pairs]
  observed = []

  class EndServer(BaseException):
    pass

  def accept():
    if not sessions:
      raise EndServer()
    return sessions.pop(0), None

  server = SimpleNamespace(bind=lambda path: Path(path).touch(), listen=lambda _: None, accept=accept, close=lambda: None)
  socket_api = SimpleNamespace(AF_UNIX=socket.AF_UNIX, SOCK_STREAM=socket.SOCK_STREAM,
    SOL_SOCKET=socket.SOL_SOCKET, SCM_RIGHTS=socket.SCM_RIGHTS, CMSG_SPACE=socket.CMSG_SPACE,
    MSG_CTRUNC=socket.MSG_CTRUNC, socket=lambda *_: server)
  monkeypatch.setattr(mgxd, 'socket', socket_api)
  monkeypatch.setattr(mgxd, 'SOCK', str(tmp_path / 'worker.sock'))
  model_file = tmp_path / 'model.mxr'
  model_file.touch()
  monkeypatch.setattr(mgxd, 'MODEL', str(model_file))
  monkeypatch.setattr(mgxd, 'warp_camera', lambda *_: np.zeros((6, 128, 256), np.uint8))
  monkeypatch.delenv('MGXD_PROFILE', raising=False)
  from openpilot.selfdrive.modeld import mgxd_runtime
  monkeypatch.setattr(mgxd_runtime, 'audit_runtime', lambda _: None)
  names = ('state_img_q', 'state_desire_q', 'state_feat_q', 'state_map_features_q', 'state_navigation_features_q')
  device_states = {name: np.zeros(2, np.float32) for name in names}

  def step(feed, audit=False):
    states = device_states if capture else {name: feed[name] for name in names}
    observed.append({name: float(value.flat[0]) for name, value in states.items()})
    next_state = [states[name] + 1 for name in names]
    output = np.full(18580, len(observed), np.float32)
    if capture:
      for name, value in zip(names, next_state, strict=True):
        device_states[name][:] = value
      return output
    return [output, *next_state]

  def reset(*, map_reset=False, navigation_reset=False):
    for selected, name in ((map_reset, names[3]), (navigation_reset, names[4])):
      if selected:
        device_states[name].fill(0)

  engine = SimpleNamespace(capture=True, metadata=lambda: {'graph_audit': {}}, step=step,
    reset_navigation=reset, navigation_evidence=lambda **_: {}) if capture else None
  runtime = SimpleNamespace(load=lambda _: SimpleNamespace(run=step), argument=lambda value: value)

  def serve():
    try:
      mgxd._serve(SimpleNamespace(), runtime, engine)
    except EndServer:
      return
    finally:
      for _, connection in pairs:
        connection.close()

  with tempfile.TemporaryFile() as camera, ThreadPoolExecutor(max_workers=1) as pool:
    camera.write(b'000000')
    camera.flush()
    future = pool.submit(serve)
    try:
      for index in range(2):
        client = pairs[index][0]
        state = remote_state(client, [])
        if index:
          state.request_nav_reset(map_epoch=5)
        run_frame(state, (camera.fileno(), camera.fileno()))
        assert state.nav_session_reset_confirmed
        assert state.nav_reset_applied == ({'map': 5} if index else {})
        client.close()
      client = pairs[2][0]
      client.sendall(b'MGX3' + struct.pack('<5i', 2, 2, 2, 4, 6))
      client.sendmsg([bytes([4]) + struct.pack('<30f', *([0.] * 30))],
        [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array('i', [camera.fileno(), camera.fileno()]))])
      assert bytes(mgxd.recv_exact(client, 4)) == b'MGX4'
      mgxd.recv_exact(client, protocol.RECEIPT_SIZE + 18580 * 4)
    finally:
      for connection, _ in pairs:
        connection.close()
    future.result(timeout=3)
  assert len(observed) == 3
  assert [value[names[3]] for value in observed] == [0, 0, 1]
  assert [value[names[4]] for value in observed] == [0, 0, 1]
  assert all([value[name] for value in observed] == [0, 1, 2] for name in names[:3])


@pytest.mark.parametrize('kind', ['short', 'version', 'size', 'flags', 'session', 'sequence'])
def test_invalid_reset_ack_is_rejected(kind):
  data = bytearray(protocol.encode_reset_ack(8, 3, 1))
  if kind == 'short':
    data.pop()
  elif kind in ('version', 'size'):
    struct.pack_into('<H', data, 0 if kind == 'version' else 2, 99)
  elif kind == 'flags':
    struct.pack_into('<I', data, 4, 1 << 30)
  else:
    struct.pack_into('<Q', data, 8 if kind == 'session' else 16, 0)
  with pytest.raises(ValueError):
    protocol.decode_reset_ack(data)
