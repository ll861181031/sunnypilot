"""Read-only explanations of replay coverage, never controller decisions."""

import math
from collections import Counter, defaultdict

from openpilot.tools.car_porting.byd_longitudinal_audit import decode_frame


def finite_or_none(value):
  return float(value) if math.isfinite(value) else None


class ReplayDiagnostics:
  """Observer of an existing replay; cannot alter samples or clear a latch.

  A request means recorded carControl/selfdriveState agree about enable, NOT
  longitudinal ownership. Frame counts describe logging/transport only.
  All summaries are segment-local and examples are bounded.
  """

  def __init__(self, dt_ns, can_context):
    self.dt_ns = dt_ns
    self.can_context = can_context
    self.partitions = {key: {'ticks': 0, 'output_ticks': 0, 'states': Counter(), 'reasons': Counter()}
                       for key in ('requested', 'not_requested', 'unknown')}
    self.vehicle = {'valid_samples': 0, 'invalid_samples': 0, 'flags': Counter(),
                    'negative_speed_range_mps': None, 'negative_speed_examples': []}
    self.batches = defaultdict(Counter)
    self.batch_examples = defaultdict(list)
    self.transport = Counter()
    self.invalid_can = 0
    self.triggers = Counter()
    self.inherited = Counter()
    self.episode = None
    self.episodes = []
    self.pending_examples = []
    self.output_run = 0
    self.run_lengths = Counter()
    self.last_ns = None
    self.ambiguous_request_stamps = {}
    self.pending_request_ambiguity = False

  def _note(self, now, category, **fields):
    # Only first few observations since previous grid tick. Counts stay exact.
    if len(self.pending_examples) < 12:
      self.pending_examples.append({'logMonoTime': now, 'category': category, **fields})

  def observe_group(self, now, group):
    kinds = Counter(e.which() for e in group)
    for kind in ('carControl', 'selfdriveState'):
      if kinds[kind] > 1:
        self.ambiguous_request_stamps[kind] = now
        self.pending_request_ambiguity = True
    can_events = []
    for event in group:
      kind = event.which()
      if kind == 'carState':
        if not event.valid:
          self.vehicle['invalid_samples'] += 1
          continue
        cs = event.carState
        self.vehicle['valid_samples'] += 1
        flags = {
          'brake_pressed': cs.brakePressed, 'gas_pressed': cs.gasPressed,
          'can_invalid': not cs.canValid, 'can_timeout': cs.canTimeout,
          'stock_aeb': cs.stockAeb, 'stock_fcw': cs.stockFcw, 'not_drive': str(cs.gearShifter) != 'drive',
          'nonfinite_speed': not math.isfinite(cs.vEgo), 'nonfinite_acceleration': not math.isfinite(cs.aEgo),
          'negative_filtered_speed': math.isfinite(cs.vEgo) and cs.vEgo < 0,
          'negative_filtered_with_zero_raw': math.isfinite(cs.vEgo) and cs.vEgo < 0 and cs.vEgoRaw == 0,
          'soft_hold': cs.softHoldActive > 0,
        }
        self.vehicle['flags'].update(k for k, value in flags.items() if value)
        if flags['negative_filtered_speed']:
          old = self.vehicle['negative_speed_range_mps'] or [cs.vEgo, cs.vEgo]
          self.vehicle['negative_speed_range_mps'] = [min(old[0], cs.vEgo), max(old[1], cs.vEgo)]
          if len(self.vehicle['negative_speed_examples']) < 5:
            self.vehicle['negative_speed_examples'].append({'logMonoTime': now, 'vEgo': cs.vEgo,
                                                           'vEgoRaw': finite_or_none(cs.vEgoRaw),
                                                           'aEgo': finite_or_none(cs.aEgo), 'gear': str(cs.gearShifter)})
        if any(flags.values()):
          self._note(now, 'vehicle_flags', flags=[k for k, value in flags.items() if value])
      if kind in ('can', 'sendcan'):
        if not event.valid:
          self.invalid_can += int(kind == 'can')
          continue
        for frame in getattr(event, kind):
          if frame.address in (0x32D, 0x32E, 0x32F):
            self.transport[f'{kind}:src{frame.src}:{frame.address:#x}'] += 1
        if kind == 'can':
          can_events.append(event)
    # Same-timestamp invalid CAN prevents valid-context attribution, just as
    # ReplayContext does. Transport counts above remain envelope observations.
    if any(e.which() == 'can' and not e.valid for e in group):
      return
    frames = defaultdict(list)
    for event in can_events:
      for frame in event.can:
        key = (frame.src, frame.address)
        if key in self.can_context:
          frames[key].append(frame)
    for key, items in frames.items():
      name = self.can_context[key]
      stats = self.batches[name]
      stats['frames'] += len(items)
      stats['batches'] += 1
      if len(items) <= 1:
        continue
      payloads = {bytes(f.dat) for f in items}
      stats['ambiguous_batches'] += 1
      stats['ambiguous_frames'] += len(items)
      stats['identical_payload_batches' if len(payloads) == 1 else 'distinct_payload_batches'] += 1
      try:
        decoded = [decode_frame(key[1], raw) for raw in payloads]
        stats['valid_payload_ambiguous_batches'] += 1
        counters = sorted({int(v['COUNTER']) for v in decoded if 'COUNTER' in v})
        fields = {tuple(sorted((k, value) for k, value in values.items() if k not in ('COUNTER', 'CHECKSUM')))
                  for values in decoded}
        stats['same_declared_fields_ambiguous_batches' if len(fields) == 1 else 'different_declared_fields_ambiguous_batches'] += 1
      except ValueError:
        counters = []
        stats['invalid_payload_ambiguous_batches'] += 1
      self._note(now, 'ambiguous_batch', stream=name, frames=len(items))
      if len(self.batch_examples[name]) < 3:
        self.batch_examples[name].append({'logMonoTime': now, 'frames': len(items),
                                          'distinct_payloads': len(payloads), 'counter_value_set': counters,
                                          'payload_examples': [raw.hex() for raw in sorted(payloads)[:4]]})

  def observe_tick(self, now, tick, selfdrive, result, was_latched, is_latched, ctx):
    self.last_ns = now
    request = 'unknown'
    if (tick is not None and selfdrive is not None and tick.valid and selfdrive.valid and not self.pending_request_ambiguity and
        tick.logMonoTime != self.ambiguous_request_stamps.get('carControl') and
        selfdrive.logMonoTime != self.ambiguous_request_stamps.get('selfdriveState') and
        0 <= now - tick.logMonoTime < 100_000_000 and 0 <= now - selfdrive.logMonoTime < 100_000_000):
      sd = selfdrive.selfdriveState
      if tick.carControl.enabled and sd.enabled and sd.active and sd.state in ('enabled', 'overriding'):
        request = 'requested'
      elif not tick.carControl.enabled and not sd.enabled and not sd.active and sd.state == 'disabled':
        request = 'not_requested'
    p = self.partitions[request]
    p['ticks'] += 1
    p['output_ticks'] += int(result.accel_mps2 is not None)
    p['states'][result.state] += 1
    p['reasons'][result.reason] += 1
    if result.accel_mps2 is not None:
      self.output_run += 1
    elif self.output_run:
      self.run_lengths[self.output_run] += 1
      self.output_run = 0
    if is_latched and not was_latched:
      self.triggers[result.reason] += 1
      cs_event = ctx.services.get('carState')
      cs = cs_event.carState if cs_event is not None else None
      self.episode = {
        'start_ns': now, 'trigger': result.reason, 'request_partition_at_trigger': request,
        'latched_grid_ticks': 0, 'inherited_block_ticks': 0,
        'input_fault_at_trigger': ctx.pending_fault,
        'observations_since_previous_tick': list(self.pending_examples),
        'service_ages_ms': {name: (now - e.logMonoTime) / 1e6 for name, e in ctx.services.items()},
        'can_context': {name: {'age_ms': (now - s[0]) / 1e6, 'raw': s[2].hex()} for name, s in ctx.can.items()},
        'vehicle_at_trigger': None if cs is None else {'vEgo': finite_or_none(cs.vEgo),
                               'vEgoRaw': finite_or_none(cs.vEgoRaw), 'aEgo': finite_or_none(cs.aEgo),
                               'gear': str(cs.gearShifter), 'brake': cs.brakePressed, 'gas': cs.gasPressed},
      }
    if is_latched and self.episode is not None:
      self.episode['latched_grid_ticks'] += 1
      if result.state == 'fault_latched':
        self.episode['inherited_block_ticks'] += 1
        self.inherited[self.episode['trigger']] += 1
    if was_latched and not is_latched and self.episode is not None:
      self._close_episode(now, 'healthy_disengagement' if result.state == 'off' else 'other_clear')
    self.pending_examples.clear()
    self.pending_request_ambiguity = False

  def _close_episode(self, end_ns, reason):
    self.episode.update(end_ns_exclusive=end_ns, end_reason=reason)
    if len(self.episodes) < 100:
      self.episodes.append(self.episode)
    self.episode = None

  def finish(self):
    if self.episode is not None:
      self._close_episode(self.last_ns + self.dt_ns, 'segment_end_not_recovered')
    if self.output_run:
      self.run_lengths[self.output_run] += 1
      self.output_run = 0
    return {
      'request_partitions': self.partitions, 'vehicle_observations': self.vehicle,
      'can_batch_observations': dict(self.batches), 'can_batch_examples': dict(self.batch_examples),
      'longitudinal_transport_frames': dict(self.transport), 'invalid_can_envelopes': self.invalid_can,
      'ecu_acceptance_established': False,
      'latch_trigger_counts': dict(self.triggers), 'inherited_latch_ticks_by_trigger': dict(self.inherited),
      'latch_episode_examples': self.episodes, 'latch_examples_truncated': sum(self.triggers.values()) > len(self.episodes),
      'output_run_tick_histogram': {str(n): count for n, count in sorted(self.run_lengths.items())},
      'max_contiguous_output_ticks': max(self.run_lengths, default=0),
      'meaning': [
        'Read-only accounting; no relaxation, deduplication, relatching or extra controller evaluations',
        'Request partitions use recorded enable agreement only, not a claim of ECU ownership or full eligibility',
        'Vehicle flags overlap; valid envelopes do not validate decoded physical semantics',
        'Batch validity means payload length/declared DBC checksum only; no chronology inferred from frame order',
        'Same declared fields excludes COUNTER/CHECKSUM, not unknown bits; does not authorize deduplication',
        'Latch origin is the first replay rejection that set the latch, not a diagnosed vehicle fault',
        'Grid counts and synthetic endpoint are analysis bookkeeping, not physical execution duration',
        'sendcan/TX echo/Panda rejection are not ECU acceptance/rejection/timeout confirmation',
      ],
    }
