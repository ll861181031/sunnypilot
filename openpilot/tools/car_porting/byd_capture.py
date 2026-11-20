"""Automatically record IPC or an already-silent USB Panda, without starting vehicle-control processes."""

import argparse
import json
import math
import os
import shutil
import signal
import sys
import time
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import zstandard

from openpilot.cereal import log, messaging
from openpilot.cereal.services import SERVICE_LIST
from openpilot.tools.car_porting.byd_longitudinal_audit import DBC_NAME, FLAGS, decode_frame
from openpilot.tools.car_porting.byd_panda_capture import PandaSource


REQUIRED_BUSES = (0, 2)
MIN_FREE_BYTES = 512 * 1024 * 1024
MARKER_CAN_SOURCES = {(0, 0x342), (0, 0x1F0), (2, 0x32D), (2, 0x32E)}
MARKER_SOURCES = {f'can:{bus}:{address:#x}' for bus, address in MARKER_CAN_SOURCES} | {'carState', 'carControl'}


def default_services():
  return tuple(SERVICE_LIST)


def utc_now():
  return datetime.now(UTC).isoformat()


def write_json_new(path, value):
  with path.open('x') as output:
    json.dump(value, output, indent=2, allow_nan=False)
    output.write('\n')


class AutomaticMarkers:
  def __init__(self, emit):
    self.emit = emit
    self.previous = {}
    self.sources_observed = set()
    self.rejected_frames = Counter()

  def update(self, source, values, nanos, evidence, **details):
    previous_ns, previous = self.previous.get(source, (None, None))
    if previous_ns is not None and nanos <= previous_ns:
      return
    continuity = 'first_sample' if previous is None else 'continuous'
    if previous_ns is not None and nanos - previous_ns >= 1_000_000_000:
      previous, continuity = None, 'gap'
    for name, value in values.items():
      if previous is None or previous.get(name) != value:
        self.emit(f'{source}.{name}', source=source, signal=name, value=value,
                  previous=None if previous is None else previous.get(name), initial=previous is None,
                  continuity=continuity, logMonoTime=nanos, evidence=evidence, **details)
    self.previous[source] = (nanos, values)
    self.sources_observed.add(source)

  def observe(self, event, received_ns, started_ns):
    service, nanos = event.which(), event.logMonoTime
    if not event.valid or not started_ns <= nanos <= received_ns or received_ns - nanos >= 1_000_000_000:
      # Do not bridge known invalid data when inferring a state transition.
      for source in list(self.previous):
        if source == service or (service == 'can' and source.startswith('can:')):
          del self.previous[source]
      return
    if service == 'can':
      for frame in event.can:
        if (frame.src, frame.address) not in MARKER_CAN_SOURCES:
          continue
        source = f'can:{frame.src}:{frame.address:#x}'
        try:
          decoded = decode_frame(frame.address, frame.dat)
        except ValueError as exc:
          self.rejected_frames[str(exc)] += 1
          self.previous.pop(source, None)
          continue
        if frame.address == 0x342:
          values = {'gas_pedal_pressed': decoded['GAS_PEDAL'] > 0.01,
                    'brake_pedal_pressed': decoded['BRAKE_PEDAL'] > 0.01}
        elif frame.address == 0x1F0:
          speed = sum(decoded[f'WHEELSPEED_{wheel}'] for wheel in ('FL', 'FR', 'BL', 'BR')) / 4 / 3.6
          values = {'standstill': speed < 0.1}
        elif frame.address == 0x32D:
          values = {'cruise_state': int(decoded['CRUISE_STATE']), 'set_speed_kph': decoded['SET_SPEED']}
        else:
          values = {flag: int(decoded[flag]) for flag in FLAGS}
        self.update(source, values, nanos, 'dbc_candidate', payload=frame.dat.hex())
    elif service == 'carControl':
      self.update(service, {'latActive': event.carControl.latActive, 'longActive': event.carControl.longActive},
                  nanos, 'published_state')
    elif service == 'carState':
      state = event.carState
      if not state.canValid:
        self.previous.pop(service, None)
        return
      self.update(service, {
        'cruise_enabled': state.cruiseState.enabled, 'cruise_available': state.cruiseState.available,
        'cruise_standstill': state.cruiseState.standstill,
        'set_speed_mps': state.cruiseState.speed if math.isfinite(state.cruiseState.speed) else None,
        'gasPressed': state.gasPressed, 'brakePressed': state.brakePressed,
        'standstill': state.standstill, 'gearShifter': str(state.gearShifter),
        'steeringPressed': state.steeringPressed,
      }, nanos, 'published_state')


class CaptureSession:
  def __init__(self, path, *, label='', started_ns=None, segment_seconds=60, can_timeout=5, services=None, source_mode='ipc'):
    if not math.isfinite(segment_seconds) or segment_seconds <= 0:
      raise ValueError('segment_seconds must be finite and positive')
    if not math.isfinite(can_timeout) or can_timeout <= 0:
      raise ValueError('can_timeout must be finite and positive')
    self.path = Path(path)
    self.path.mkdir(mode=0o700, parents=True, exist_ok=False)
    self.started_ns = time.monotonic_ns() if started_ns is None else started_ns
    self.can_wait_started_ns = self.started_ns
    self.capture_source = 'ipc'
    self.panda_info = None
    self.error = None
    self.segment_ns = int(segment_seconds * 1e9)
    self.can_timeout = can_timeout
    self.services = default_services() if services is None else tuple(services)
    self.messages = Counter()
    self.invalid_messages = Counter()
    self.can_frames = Counter()
    self.stream_frames = Counter()
    self.last_can = {}
    self.first_mono = {}
    self.last_mono = {}
    self.op_actuation = False
    self.host_tx_frames = 0
    self.ever_ready = False
    self.write_failed = False
    self.stream = None
    self.output = None
    self.segment_path = None
    self.segment_started_ns = None
    self.segment_count = 0
    self.finished = False
    self.markers = (self.path / 'markers.jsonl').open('x', buffering=1)
    self.marker_counts = Counter()
    self.auto = AutomaticMarkers(self.auto_mark)
    write_json_new(self.path / 'session.json', {
      'label': label or self.path.name, 'started_utc': utc_now(), 'started_monotonic_ns': self.started_ns,
      'read_only_subscriber': source_mode == 'ipc', 'services_requested': self.services,
      'requested_source': source_mode, 'can_transmission': False,
      'automatic_markers': True, 'marker_dbc': DBC_NAME,
      'required_rx_buses': REQUIRED_BUSES, 'segment_seconds': segment_seconds, 'can_timeout': can_timeout,
      'intentional_decimation': False, 'transport_loss_checked': False,
      'scope': [
        'All available registered IPC services; all received CAN addresses and src values.',
        'Direct USB receive may be selected in auto/panda mode; only inactive heartbeat and power-save USB maintenance.',
        'Raw VisionIPC camera buffers are not recorded.',
        'Encoded media is present only if another process publishes it.',
        'Existing vehicle-control processes are not modified.',
      ],
    })
    self.auto_mark('session_start')

  def _close_segment(self):
    if self.stream is None:
      return
    try:
      self.stream.close()
      self.output.flush()
      os.fsync(self.output.fileno())
    except BaseException:
      self.write_failed = True
      raise
    finally:
      self.output.close()
      self.stream = None
    if not self.write_failed:
      # Remove only the marker created by this writer for the completed segment.
      (self.segment_path / 'rlog.lock').unlink()

  def _open_segment(self, received_ns):
    self._close_segment()
    self.segment_path = self.path / f'{self.segment_count:06d}'
    self.segment_path.mkdir(mode=0o700)
    with (self.segment_path / 'rlog.lock').open('x') as lock:
      lock.write(str(os.getpid()))
    self.output = (self.segment_path / 'rlog.zst').open('xb')
    self.stream = zstandard.ZstdCompressor(level=1).stream_writer(self.output, closefd=False)
    self.segment_started_ns = received_ns
    self.segment_count += 1

  def record(self, raw, received_ns=None):
    if self.finished:
      raise ValueError('Session already finished')
    received_ns = time.monotonic_ns() if received_ns is None else received_ns
    if self.stream is None or received_ns - self.segment_started_ns >= self.segment_ns:
      self._open_segment(received_ns)
    try:
      # Keep the original bytes, including invalid envelopes and repeated frames.
      self.stream.write(raw)
      with log.Event.from_bytes(raw) as event:
        service, nanos = event.which(), event.logMonoTime
        self.messages[service] += 1
        self.first_mono.setdefault(service, nanos)
        self.last_mono[service] = nanos
        self.invalid_messages[service] += int(not event.valid)
        if service == 'can':
          fresh = event.valid and self.started_ns <= nanos <= received_ns and received_ns - nanos < 1_000_000_000
          for frame in event.can:
            self.can_frames[str(frame.src)] += 1
            self.stream_frames[f'{frame.src}:{frame.address:#x}'] += 1
            if fresh and frame.src in REQUIRED_BUSES:
              self.last_can[frame.src] = received_ns
          if not self.ever_ready and self.can_ready(received_ns, self.can_timeout):
            self.ever_ready = True
            self.auto_mark('can_ready', logMonoTime=nanos)
        elif service == 'sendcan':
          self.host_tx_frames += len(event.sendcan)
        elif service == 'carControl':
          self.op_actuation |= bool(event.carControl.latActive or event.carControl.longActive)
        self.auto.observe(event, received_ns, self.started_ns)
    except BaseException:
      self.write_failed = True
      raise

  def can_ready(self, now_ns, timeout):
    return all(bus in self.last_can and 0 <= now_ns - self.last_can[bus] < timeout * 1e9 for bus in REQUIRED_BUSES)

  def stop_reason(self, now_ns, startup_timeout, can_timeout):
    if not self.ever_ready and now_ns - self.can_wait_started_ns >= startup_timeout * 1e9:
      return 'no_fresh_can'
    if self.ever_ready and not self.can_ready(now_ns, can_timeout):
      return 'can_stream_lost'
    return None

  def mark(self, label):
    label = label.strip()
    if label:
      self.markers.write(json.dumps({'label': label[:512], 'wall_time_utc': utc_now(), 'source': 'operator',
                                    'automatic': False, 'monotonic_ns': time.monotonic_ns()}) + '\n')

  def auto_mark(self, label, *, source='capture', **details):
    self.markers.write(json.dumps({'label': label, 'source': source, 'automatic': True,
                                  'wall_time_utc': utc_now(), 'monotonic_ns': time.monotonic_ns(), **details},
                                 allow_nan=False) + '\n')
    self.marker_counts[label] += 1

  def flush(self):
    if self.stream is not None:
      self.stream.flush(zstandard.FLUSH_BLOCK)

  def finish(self, reason):
    if self.finished:
      raise ValueError('Session already finished')
    if not self.ever_ready and reason in ('duration', 'user_stop'):
      reason = 'no_fresh_can'
    try:
      self._close_segment()
      self.auto_mark('session_end', result=reason, archive_complete=not self.write_failed)
      self.markers.flush()
      os.fsync(self.markers.fileno())
    finally:
      self.markers.close()
      self.finished = True
    summary = {
      'result': reason, 'successful': reason in ('duration', 'user_stop') and self.ever_ready and not self.write_failed,
      'archive_complete': not self.write_failed, 'finished_utc': utc_now(),
      'finished_monotonic_ns': time.monotonic_ns(), 'segments': self.segment_count,
      'messages': dict(self.messages), 'invalid_messages': dict(self.invalid_messages),
      'can_frames_by_src': dict(self.can_frames), 'can_frames_by_stream': dict(self.stream_frames),
      'first_logMonoTime': self.first_mono, 'last_logMonoTime': self.last_mono,
      'services_not_observed': sorted(set(self.services) - self.messages.keys()),
      'host_tx_frames': self.host_tx_frames, 'op_actuation_observed': self.op_actuation,
      'stock_only_proven': False, 'transport_loss_checked': False,
      'capture_source': self.capture_source, 'panda': self.panda_info, 'error': self.error,
      'auto_marking': {
        'event_counts': dict(self.marker_counts), 'sources_observed': sorted(self.auto.sources_observed),
        'sources_not_observed': sorted(MARKER_SOURCES - self.auto.sources_observed),
        'rejected_can_frames': dict(self.auto.rejected_frames),
        'meaning': 'Observed changes only; snapshots are not actions, and DBC candidates do not prove ECU acceptance or control ownership.',
      },
    }
    write_json_new(self.path / 'summary.json', summary)
    return summary


class MessageSource:
  def __init__(self, services):
    self.poller = messaging.Poller()
    self.sockets = [messaging.sub_sock(service, poller=self.poller, conflate=False) for service in services]

  def poll(self, timeout=100):
    for sock in self.poller.poll(timeout):
      # Bound each queue drain so a busy stream cannot starve stop/health checks.
      for _ in range(256):
        raw = sock.receive(non_blocking=True)
        if raw is None:
          break
        yield raw


def finite_nonnegative(text):
  value = float(text)
  if not math.isfinite(value) or value < 0:
    raise argparse.ArgumentTypeError('Expected a finite nonnegative number')
  return value


def connect_panda_source(session):
  source = PandaSource()
  try:
    session.capture_source = 'panda_usb'
    session.panda_info = source.info
    session.can_wait_started_ns = time.monotonic_ns()
    session.auto_mark('panda_connected', source='capture', device=source.info)
    print('PANDA_RX: silent mode preserved; CAN receive only, inactive USB heartbeat and power-save maintenance.', flush=True)
    return source
  except BaseException:
    source.close()
    raise


def main(argv=None):
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--duration', type=finite_nonnegative, default=600, help='Seconds; 0 records until Ctrl+C (default: 600)')
  parser.add_argument('--label', default='', help='Optional description; defaults to the unique session name')
  parser.add_argument('--output-root', type=Path, default=Path(__file__).resolve().parents[2] / 'data' / 'byd-captures')
  parser.add_argument('--startup-timeout', type=finite_nonnegative, default=5, help='Wait for fresh RX on buses 0 and 2')
  parser.add_argument('--can-timeout', type=finite_nonnegative, default=5, help='Stop if either required RX bus disappears')
  parser.add_argument('--segment-seconds', type=finite_nonnegative, default=60)
  parser.add_argument('--source', choices=('auto', 'ipc', 'panda'), default='auto',
                      help='auto: use IPC, or guarded silent-Panda receive when no CAN publisher is present (default)')
  args = parser.parse_args(argv)
  if min(args.startup_timeout, args.can_timeout, args.segment_seconds) <= 0:
    parser.error('Timeouts and segment duration must be positive')

  session = None
  panda_source = None
  previous_handlers = {}
  stop = []
  reason = 'duration'
  try:
    args.output_root.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(args.output_root).free < MIN_FREE_BYTES:
      raise OSError('Less than 512 MiB free; recording not started')
    name = datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
    session = CaptureSession(args.output_root / name, label=args.label,
                             segment_seconds=args.segment_seconds, can_timeout=args.can_timeout, source_mode=args.source)
    for sig in (signal.SIGINT, signal.SIGTERM):
      previous_handlers[sig] = signal.signal(sig, lambda signum, frame: stop.append(signum))
    print(f'CAPTURE_DIR={session.path}', flush=True)
    print('No CAN transmission, Params writes, process startup, safety-mode change, or flashing.', flush=True)
    print('Existing OP processes are NOT disabled by this script. Video requires an existing encoder publisher.', flush=True)
    print('Waiting for fresh CAN RX on buses 0 and 2. Events are marked automatically; no keyboard input needed.', flush=True)
    print(f'Auto-save after {args.duration:g} seconds; Ctrl+C can stop early.' if args.duration else
          'Recording until Ctrl+C; automatic segment saves remain enabled.', flush=True)
    source = MessageSource(session.services)
    if args.source == 'panda':
      panda_source = connect_panda_source(session)
    announced = False
    warned = False
    next_status = session.started_ns
    while not stop:
      for raw in source.poll(0 if panda_source is not None else 100):
        session.record(raw)
        if stop:
          break
      if panda_source is not None and not stop:
        for raw in panda_source.poll(100):
          session.record(raw)
      now = time.monotonic_ns()
      reason = session.stop_reason(now, args.startup_timeout, args.can_timeout)
      if (reason == 'no_fresh_can' and args.source == 'auto' and panda_source is None and not stop
          and not any(session.messages[name] for name in ('can', 'carControl', 'sendcan'))
          and (not args.duration or now - session.started_ns < args.duration * 1e9)):
        print('No IPC CAN publisher detected. Checking independent silent-Panda receive; no OP processes will be started.', flush=True)
        panda_source = connect_panda_source(session)
        continue
      if reason is not None:
        break
      if args.duration and now - session.started_ns >= args.duration * 1e9:
        reason = 'duration'
        break
      if session.ever_ready and not announced:
        print('RECORDING: fresh RX confirmed on buses 0 and 2. This is not proof of stock-only control.', flush=True)
        announced = True
      if (session.op_actuation or session.host_tx_frames) and not warned:
        print('WARNING: existing host control activity/TX observed; captured for attribution, not blocked.', flush=True)
        warned = True
      if now >= next_status:
        if shutil.disk_usage(session.path).free < MIN_FREE_BYTES:
          reason = 'low_disk'
          break
        session.flush()
        print(f'MESSAGES={sum(session.messages.values())} CAN_FRAMES={sum(session.can_frames.values())}',
              f'AUTO_MARKERS={sum(session.marker_counts.values())} RX_SOURCES={dict(session.can_frames)}', flush=True)
        next_status = now + 2_000_000_000
    if stop:
      reason = session.stop_reason(time.monotonic_ns(), args.startup_timeout, args.can_timeout) or 'user_stop'
  except Exception as exc:
    reason = 'capture_error'
    if session is not None:
      session.write_failed = True
      session.error = f'{type(exc).__name__}: {exc}'
    print(f'ERROR: {type(exc).__name__}: {exc}', file=sys.stderr, flush=True)
  finally:
    if panda_source is not None:
      try:
        if reason in ('duration', 'user_stop'):
          panda_source.check_health()
      except Exception as exc:
        reason = 'capture_error'
        session.error = f'{type(exc).__name__}: {exc}'
        print(f'ERROR final Panda health: {session.error}', file=sys.stderr, flush=True)
      finally:
        panda_source.close()
      if not panda_source.info.get('power_save_restored'):
        reason = 'capture_error'
        session.error = session.error or str(panda_source.info.get('close_error', 'Panda power state was not restored'))
        print(f'ERROR: {session.error}', file=sys.stderr, flush=True)
    if session is not None:
      try:
        summary = session.finish(reason)
        reason = summary['result']
        print(f'RESULT={reason} ARCHIVE_COMPLETE={summary["archive_complete"]} PATH={session.path}', flush=True)
        if not summary['successful']:
          reason = reason if reason not in ('duration', 'user_stop') else 'capture_error'
      except Exception as exc:
        reason = 'capture_error'
        print(f'ERROR finalizing capture: {exc}; retain any rlog.lock files as incomplete.', file=sys.stderr, flush=True)
    for sig, previous in previous_handlers.items():
      signal.signal(sig, previous)
  if reason in ('no_fresh_can', 'can_stream_lost'):
    print('No healthy live CAN feed. This script did not start or restart the vehicle project.',
          'Do not start a control stack while driving to fix a missing feed.', file=sys.stderr, flush=True)
  return 0 if reason in ('duration', 'user_stop') else 2


if __name__ == '__main__':
  raise SystemExit(main())
