import importlib
import json
import signal
import time
import uuid

import pytest
import zstandard

from openpilot.cereal import log
from openpilot.tools.lib.logreader import LogReader


@pytest.fixture
def capture():
  name = 'openpilot.tools.car_porting.byd_capture'
  assert importlib.util.find_spec(name) is not None, 'The passive capture implementation is missing'
  return importlib.import_module(name)


def can_bytes(nanos, buses=(0, 2, 130), valid=True, kind='can'):
  event = log.Event.new_message(logMonoTime=nanos, valid=valid)
  frames = event.init(kind, len(buses))
  for frame, bus in zip(frames, buses, strict=True):
    frame.address = 0x32E
    frame.src = bus
    frame.dat = bytes.fromhex('64666699b7d8f8af')
  return event.to_bytes()


def read_segment(path):
  decoder = zstandard.ZstdDecompressor().decompressobj()
  raw = decoder.decompress(path.read_bytes())
  assert decoder.eof
  return raw, list(LogReader.from_bytes(raw))


def test_preserves_every_envelope_including_duplicates_invalid_and_echo(capture, tmp_path):
  session = capture.CaptureSession(tmp_path / 'capture', started_ns=1_000_000_000)
  raw = can_bytes(1_100_000_000)
  invalid = can_bytes(1_200_000_000, valid=False)
  session.record(raw, 1_150_000_000)
  session.record(raw, 1_160_000_000)
  session.record(invalid, 1_250_000_000)
  assert (session.path / '000000' / 'rlog.lock').exists()
  summary = session.finish('duration')
  stored, events = read_segment(session.path / '000000' / 'rlog.zst')
  assert stored == raw + raw + invalid
  assert [frame.src for frame in events[0].can] == [0, 2, 130]
  assert not events[-1].valid
  assert summary['messages']['can'] == 3
  assert summary['can_frames_by_src'] == {'0': 3, '2': 3, '130': 3}
  assert summary['invalid_messages']['can'] == 1
  assert not (session.path / '000000' / 'rlog.lock').exists()


def test_rotation_closes_independently_readable_segments(capture, tmp_path):
  session = capture.CaptureSession(tmp_path / 'capture', started_ns=1_000_000_000, segment_seconds=1)
  session.record(can_bytes(1_100_000_000), 1_100_000_000)
  session.record(can_bytes(2_100_000_000), 2_100_000_000)
  assert not (session.path / '000000' / 'rlog.lock').exists()
  assert (session.path / '000001' / 'rlog.lock').exists()
  session.finish('user_stop')
  assert len(list(session.path.glob('*/rlog.zst'))) == 2
  assert len(read_segment(session.path / '000000' / 'rlog.zst')[1]) == 1
  assert len(read_segment(session.path / '000001' / 'rlog.zst')[1]) == 1


def test_existing_session_is_never_overwritten(capture, tmp_path):
  path = tmp_path / 'capture'
  path.mkdir()
  sentinel = path / 'rlog.zst'
  sentinel.write_bytes(b'previous recording')
  with pytest.raises(FileExistsError):
    capture.CaptureSession(path)
  assert sentinel.read_bytes() == b'previous recording'


@pytest.mark.parametrize('raw', [can_bytes(900_000_000), can_bytes(1_100_000_000, buses=(130,)),
                                 can_bytes(1_100_000_000, valid=False), can_bytes(3_000_000_000),
                                 can_bytes(1_100_000_000, kind='sendcan')])
def test_stale_invalid_future_echo_and_sendcan_do_not_prove_live_rx(capture, tmp_path, raw):
  session = capture.CaptureSession(tmp_path / 'capture', started_ns=1_000_000_000)
  session.record(raw, 1_200_000_000)
  assert not session.can_ready(1_200_000_000, 5)
  assert session.stop_reason(7_000_000_000, 5, 5) == 'no_fresh_can'
  session.finish('no_fresh_can')


def test_missing_bus_and_later_disconnect_are_reported(capture, tmp_path):
  session = capture.CaptureSession(tmp_path / 'capture', started_ns=1_000_000_000)
  session.record(can_bytes(1_100_000_000, buses=(0,)), 1_200_000_000)
  assert not session.can_ready(1_200_000_000, 5)
  session.record(can_bytes(1_300_000_000, buses=(2,)), 1_400_000_000)
  assert session.can_ready(1_400_000_000, 5)
  assert session.stop_reason(1_400_000_000, 5, 5) is None
  assert session.stop_reason(7_000_000_000, 5, 5) == 'can_stream_lost'
  summary = session.finish('can_stream_lost')
  assert summary['result'] == 'can_stream_lost'
  assert not summary['successful']


def test_empty_capture_cannot_report_success(capture, tmp_path):
  session = capture.CaptureSession(tmp_path / 'capture')
  summary = session.finish('user_stop')
  assert not summary['successful']
  assert not list(session.path.glob('*/rlog.zst'))
  assert json.loads((session.path / 'summary.json').read_text())['result'] == 'no_fresh_can'


def test_operator_markers_have_wall_and_monotonic_time(capture, tmp_path):
  session = capture.CaptureSession(tmp_path / 'capture', label='stock ACC run 1')
  before = time.monotonic_ns()
  session.mark('cancel pressed')
  session.finish('user_stop')
  marker, = [row for row in read_markers(session) if row['label'] == 'cancel pressed']
  assert marker['label'] == 'cancel pressed'
  assert before <= marker['monotonic_ns'] <= time.monotonic_ns()
  assert marker['wall_time_utc'].endswith('+00:00')
  metadata = json.loads((session.path / 'session.json').read_text())
  assert metadata['label'] == 'stock ACC run 1'
  assert metadata['read_only_subscriber'] is True
  assert session.path.stat().st_mode & 0o077 == 0


def test_active_op_and_host_transmission_are_not_mislabeled_as_stock(capture, tmp_path):
  session = capture.CaptureSession(tmp_path / 'capture', started_ns=1_000_000_000)
  event = log.Event.new_message(logMonoTime=1_100_000_000, valid=True)
  event.init('carControl').longActive = True
  session.record(event.to_bytes(), 1_200_000_000)
  session.record(can_bytes(1_100_000_000, kind='sendcan', buses=(0,)), 1_200_000_000)
  result = session.finish('user_stop')
  assert result['op_actuation_observed']
  assert result['host_tx_frames'] == 1
  assert result['stock_only_proven'] is False


def test_all_registered_services_are_selected_by_default(capture):
  from openpilot.cereal.services import SERVICE_LIST
  assert set(capture.default_services()) == set(SERVICE_LIST)


def test_real_ipc_subscription_does_not_conflate(capture):
  import msgq
  endpoint = 'byd_capture_test_' + uuid.uuid4().hex
  publisher = msgq.pub_sock(endpoint)
  source = capture.MessageSource([endpoint])
  expected = [can_bytes(1_000_000_000 + i) for i in range(20)]
  for raw in expected:
    publisher.send(raw)
  actual = []
  deadline = time.monotonic() + 2
  while len(actual) < len(expected) and time.monotonic() < deadline:
    actual.extend(source.poll(10))
  assert actual == expected


def test_write_failure_keeps_recording_lock(capture, tmp_path, monkeypatch):
  session = capture.CaptureSession(tmp_path / 'capture', started_ns=1_000_000_000)
  session.record(can_bytes(1_100_000_000), 1_200_000_000)

  class FullDisk:
    def write(self, raw):
      raise OSError('disk full')

    def close(self):
      pass

  original = session.stream
  monkeypatch.setattr(session, 'stream', FullDisk())
  with pytest.raises(OSError, match='disk full'):
    session.record(can_bytes(1_300_000_000), 1_400_000_000)
  original.close()
  result = session.finish('write_error')
  assert (session.path / '000000' / 'rlog.lock').exists()
  assert not result['archive_complete']
  assert not result['successful']


def test_readiness_uses_configured_can_timeout(capture, tmp_path):
  session = capture.CaptureSession(tmp_path / 'capture', started_ns=1_000_000_000, can_timeout=10)
  session.record(can_bytes(2_000_000_000, buses=(0,)), 2_000_000_000)
  session.record(can_bytes(8_000_000_000, buses=(2,)), 8_000_000_000)
  assert session.ever_ready
  assert session.stop_reason(8_000_000_000, 10, 10) is None
  session.finish('user_stop')


@pytest.mark.parametrize(('mode', 'expected', 'exit_code'), [
  ('empty', 'no_fresh_can', 2),
  ('duration', 'duration', 0),
  ('interrupt', 'user_stop', 0),
  ('interrupt_after_loss', 'can_stream_lost', 2),
])
def test_cli_records_or_reports_missing_feed(capture, tmp_path, monkeypatch, capsys, mode, expected, exit_code):
  clock = [1_000_000_000]
  handlers = {}

  def register_signal(sig, handler):
    previous = handlers.get(sig, signal.SIG_DFL)
    handlers[sig] = handler
    return previous

  class Source:
    def __init__(self, services):
      assert 'can' in services
      self.polls = 0

    def poll(self, timeout):
      self.polls += 1
      clock[0] += 1_000_000_000
      if mode == 'empty':
        return []
      if self.polls == 2 and mode.startswith('interrupt'):
        if mode == 'interrupt_after_loss':
          clock[0] += 6_000_000_000
        handlers[signal.SIGINT](signal.SIGINT, None)
        return []
      return [can_bytes(clock[0])]

  monkeypatch.setattr(capture, 'MessageSource', Source)
  monkeypatch.setattr(capture.time, 'monotonic_ns', lambda: clock[0])
  monkeypatch.setattr(capture.signal, 'signal', register_signal)
  class NoInput:
    def isatty(self):
      raise AssertionError('Automatic recording must not inspect or read stdin')

  monkeypatch.setattr(capture.sys, 'stdin', NoInput())
  result = capture.main(['--output-root', str(tmp_path), '--duration', '2', '--startup-timeout', '2', '--source', 'ipc'])
  assert result == exit_code
  session_path, = tmp_path.iterdir()
  summary = json.loads((session_path / 'summary.json').read_text())
  assert summary['result'] == expected
  assert summary['successful'] is (exit_code == 0)
  assert summary['archive_complete']
  assert not list(session_path.glob('*/rlog.lock'))
  if mode == 'empty':
    assert not list(session_path.glob('*/rlog.zst'))
  else:
    assert read_segment(session_path / '000000' / 'rlog.zst')[1]
  assert f'RESULT={expected}' in capsys.readouterr().out
  assert all(handler == signal.SIG_DFL for handler in handlers.values())


@pytest.mark.parametrize('args', [
  ['--duration', '-1'], ['--duration', 'nan'], ['--duration', 'inf'], ['--duration', 'bad'],
  ['--startup-timeout', '0'], ['--can-timeout', '0'], ['--segment-seconds', '0'],
])
def test_cli_rejects_invalid_arguments_before_creating_session(capture, tmp_path, args):
  with pytest.raises(SystemExit) as result:
    capture.main(['--output-root', str(tmp_path), *args])
  assert result.value.code == 2
  assert not list(tmp_path.iterdir())


def read_markers(session):
  return [json.loads(line) for line in (session.path / 'markers.jsonl').read_text().splitlines()]


def raw_frame(nanos, address, payload, bus, valid=True, kind='can'):
  event = log.Event.new_message(logMonoTime=nanos, valid=valid)
  frame, = event.init(kind, 1)
  frame.address, frame.dat, frame.src = address, bytes.fromhex(payload), bus
  return event.to_bytes()


def test_automatic_lifecycle_and_label_need_no_operator(capture, tmp_path):
  session = capture.CaptureSession(tmp_path / 'automatic-run', started_ns=1_000_000_000)
  session.record(can_bytes(1_100_000_000), 1_100_000_000)
  summary = session.finish('duration')
  marks = read_markers(session)
  assert [m['label'] for m in marks if m['source'] == 'capture'] == ['session_start', 'can_ready', 'session_end']
  assert marks[-1]['result'] == 'duration'
  assert all(m['automatic'] for m in marks)
  assert summary['auto_marking']['event_counts']['session_end'] == 1
  metadata = json.loads((session.path / 'session.json').read_text())
  assert metadata['label'] == 'automatic-run'
  assert metadata['automatic_markers']


def test_raw_pedals_automatically_mark_edges_not_repeated_samples(capture, tmp_path):
  session = capture.CaptureSession(tmp_path / 'capture', started_ns=1_000_000_000)
  payloads = ['0000000000000000', '0010000000000000', '0010000000000000', '0000000000000000']
  for i, payload in enumerate(payloads):
    nanos = 1_100_000_000 + i * 20_000_000
    session.record(raw_frame(nanos, 0x342, payload, 0), nanos)
  summary = session.finish('user_stop')
  brake = [m for m in read_markers(session) if m['label'] == 'can:0:0x342.brake_pedal_pressed']
  assert [m['value'] for m in brake] == [False, True, False]
  assert [m['initial'] for m in brake] == [True, False, False]
  assert brake[1]['logMonoTime'] == 1_120_000_000
  assert brake[1]['previous'] is False
  assert brake[1]['evidence'] == 'dbc_candidate'
  assert summary['auto_marking']['sources_observed'] == ['can:0:0x342']


def test_cruise_changes_are_candidates_not_proof_of_stock_control(capture, tmp_path):
  session = capture.CaptureSession(tmp_path / 'capture', started_ns=1_000_000_000)
  # Hand-checked HUD states 6 then 3; byte 7 is the additive checksum.
  for nanos, payload in [(1_100_000_000, '000000000060009f'), (1_120_000_000, '00000000003000cf')]:
    session.record(raw_frame(nanos, 0x32D, payload, 2), nanos)
  summary = session.finish('user_stop')
  cruise = [m for m in read_markers(session) if m['label'] == 'can:2:0x32d.cruise_state']
  assert [m['value'] for m in cruise] == [6, 3]
  assert all(m['evidence'] == 'dbc_candidate' for m in cruise)
  assert summary['stock_only_proven'] is False


@pytest.mark.parametrize(('nanos', 'received', 'bus', 'valid', 'kind', 'payload'), [
  (900_000_000, 1_100_000_000, 2, True, 'can', '000000000060009f'),
  (1_100_000_000, 2_200_000_000, 2, True, 'can', '000000000060009f'),
  (1_300_000_000, 1_200_000_000, 2, True, 'can', '000000000060009f'),
  (1_100_000_000, 1_100_000_000, 130, True, 'can', '000000000060009f'),
  (1_100_000_000, 1_100_000_000, 0, True, 'can', '000000000060009f'),
  (1_100_000_000, 1_100_000_000, 2, False, 'can', '000000000060009f'),
  (1_100_000_000, 1_100_000_000, 2, True, 'sendcan', '000000000060009f'),
  (1_100_000_000, 1_100_000_000, 2, True, 'can', '0000000000600000'),
  (1_100_000_000, 1_100_000_000, 2, True, 'can', '00000000006000'),
])
def test_unreliable_frames_do_not_generate_vehicle_markers(capture, tmp_path, nanos, received, bus, valid, kind, payload):
  session = capture.CaptureSession(tmp_path / 'capture', started_ns=1_000_000_000)
  raw = raw_frame(nanos, 0x32D, payload, bus, valid, kind)
  session.record(raw, received)
  summary = session.finish('user_stop')
  assert not [m for m in read_markers(session) if m['source'].startswith('can:')]
  assert summary['auto_marking']['sources_observed'] == []
  assert read_segment(session.path / '000000' / 'rlog.zst')[0] == raw


def test_source_gap_starts_new_snapshot_without_inventing_transition(capture, tmp_path):
  session = capture.CaptureSession(tmp_path / 'capture', started_ns=1_000_000_000)
  for nanos, payload in [(1_100_000_000, '0000000000000000'), (3_100_000_000, '0010000000000000')]:
    session.record(raw_frame(nanos, 0x342, payload, 0), nanos)
  session.finish('user_stop')
  brake = [m for m in read_markers(session) if m['label'] == 'can:0:0x342.brake_pedal_pressed']
  assert len(brake) == 2
  assert all(m['initial'] and m['previous'] is None for m in brake)
  assert brake[1]['continuity'] == 'gap'


def test_published_op_state_changes_are_automatically_attributed(capture, tmp_path):
  session = capture.CaptureSession(tmp_path / 'capture', started_ns=1_000_000_000)
  for i, active in enumerate([False, True, True, False]):
    nanos = 1_100_000_000 + i * 20_000_000
    event = log.Event.new_message(logMonoTime=nanos, valid=True)
    event.init('carControl').latActive = active
    session.record(event.to_bytes(), nanos)
  session.finish('user_stop')
  lateral = [m for m in read_markers(session) if m['label'] == 'carControl.latActive']
  assert [m['value'] for m in lateral] == [False, True, False]
  assert all(m['evidence'] == 'published_state' for m in lateral)


def test_standstill_can_be_marked_without_carstate_publisher(capture, tmp_path):
  session = capture.CaptureSession(tmp_path / 'capture', started_ns=1_000_000_000)
  session.record(raw_frame(1_100_000_000, 0x1F0, '00000000000000ff', 0), 1_100_000_000)
  session.finish('user_stop')
  stopped, = [m for m in read_markers(session) if m['label'] == 'can:0:0x1f0.standstill']
  assert stopped['value'] is True
  assert stopped['initial']


@pytest.mark.parametrize('can_valid', [True, False])
def test_carstate_markers_require_valid_vehicle_state(capture, tmp_path, can_valid):
  session = capture.CaptureSession(tmp_path / 'capture', started_ns=1_000_000_000)
  for i, enabled in enumerate([False, True, True, False]):
    nanos = 1_100_000_000 + i * 20_000_000
    event = log.Event.new_message(logMonoTime=nanos, valid=True)
    state = event.init('carState')
    state.canValid = can_valid
    state.cruiseState.enabled = enabled
    state.gearShifter = 'drive'
    state.cruiseState.speed = float('nan')
    session.record(event.to_bytes(), nanos)
  summary = session.finish('user_stop')
  cruise = [m for m in read_markers(session) if m['label'] == 'carState.cruise_enabled']
  assert [m['value'] for m in cruise] == ([False, True, False] if can_valid else [])
  assert ('carState' in summary['auto_marking']['sources_observed']) == can_valid


def test_bad_checksum_breaks_marker_continuity_but_not_raw_recording(capture, tmp_path):
  session = capture.CaptureSession(tmp_path / 'capture', started_ns=1_000_000_000)
  for i, payload in enumerate(['000000000060009f', '0000000000600000', '00000000003000cf']):
    nanos = 1_100_000_000 + i * 20_000_000
    session.record(raw_frame(nanos, 0x32D, payload, 2), nanos)
  summary = session.finish('user_stop')
  cruise = [m for m in read_markers(session) if m['label'] == 'can:2:0x32d.cruise_state']
  assert [m['value'] for m in cruise] == [6, 3]
  assert all(m['initial'] for m in cruise)
  assert summary['auto_marking']['rejected_can_frames'] == {'checksum': 1}
  assert len(read_segment(session.path / '000000' / 'rlog.zst')[1]) == 3


def test_default_cli_finishes_automatically_without_stdin_or_label(capture, tmp_path, monkeypatch):
  clock = [1_000_000_000]

  class Source:
    def __init__(self, services):
      pass

    def poll(self, timeout):
      clock[0] += 1_000_000_000
      return [can_bytes(clock[0])]

  monkeypatch.setattr(capture, 'MessageSource', Source)
  monkeypatch.setattr(capture.time, 'monotonic_ns', lambda: clock[0])
  monkeypatch.setattr(capture.sys, 'stdin', None)
  assert capture.main(['--output-root', str(tmp_path)]) == 0
  session_path, = tmp_path.iterdir()
  summary = json.loads((session_path / 'summary.json').read_text())
  metadata = json.loads((session_path / 'session.json').read_text())
  assert summary['result'] == 'duration'
  assert summary['successful']
  assert summary['messages']['can'] == 600
  assert summary['finished_monotonic_ns'] - metadata['started_monotonic_ns'] == 600_000_000_000
  assert metadata['label'] == session_path.name
  assert summary['auto_marking']['event_counts']['session_start'] == 1
  assert summary['auto_marking']['event_counts']['session_end'] == 1
  assert not list(session_path.glob('*/rlog.lock'))


@pytest.mark.parametrize('panda_failure', [False, True])
def test_automatic_fallback_captures_without_running_op(capture, tmp_path, monkeypatch, panda_failure):
  clock = [1_000_000_000]
  opened = []

  class EmptyIPC:
    def __init__(self, services):
      pass

    def poll(self, timeout):
      clock[0] += 1_000_000_000
      return []

  class Receiver:
    def __init__(self):
      self.info = {'power_save_restored': False}
      self.closed = False
      opened.append(self)

    def poll(self, timeout):
      if panda_failure:
        raise OSError('USB disconnected')
      return [can_bytes(clock[0])]

    def check_health(self):
      pass

    def close(self):
      self.closed = True
      self.info['power_save_restored'] = True

  monkeypatch.setattr(capture, 'MessageSource', EmptyIPC)
  monkeypatch.setattr(capture, 'PandaSource', Receiver, raising=False)
  monkeypatch.setattr(capture.time, 'monotonic_ns', lambda: clock[0])
  assert capture.main(['--output-root', str(tmp_path), '--duration', '8']) == (2 if panda_failure else 0)
  session_path, = tmp_path.iterdir()
  summary = json.loads((session_path / 'summary.json').read_text())
  assert summary['capture_source'] == 'panda_usb'
  assert summary['panda']['power_save_restored']
  assert opened[0].closed
  assert summary['successful'] is not panda_failure
  if panda_failure:
    assert 'USB disconnected' in summary['error']
  else:
    assert summary['can_frames_by_src']['0'] > 0


def test_partial_ipc_can_feed_never_triggers_usb_takeover(capture, tmp_path, monkeypatch):
  clock = [1_000_000_000]

  class PartialIPC:
    def __init__(self, services):
      pass

    def poll(self, timeout):
      clock[0] += 1_000_000_000
      return [can_bytes(clock[0], buses=(0,))]

  def forbidden_receiver():
    raise AssertionError('An existing CAN publisher must not be taken over')

  monkeypatch.setattr(capture, 'MessageSource', PartialIPC)
  monkeypatch.setattr(capture, 'PandaSource', forbidden_receiver, raising=False)
  monkeypatch.setattr(capture.time, 'monotonic_ns', lambda: clock[0])
  assert capture.main(['--output-root', str(tmp_path), '--duration', '8']) == 2
  session_path, = tmp_path.iterdir()
  summary = json.loads((session_path / 'summary.json').read_text())
  assert summary['result'] == 'no_fresh_can'
  assert summary['capture_source'] == 'ipc'
