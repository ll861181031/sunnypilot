"""Offline BYD longitudinal experiments from completed local logs; no vehicle TX."""

import argparse
import json
import math
from collections import Counter
from dataclasses import asdict
from itertools import groupby
from pathlib import Path

from openpilot.tools.car_porting.byd_longitudinal_audit import decode_frame, local_log_paths, read_local_events
from openpilot.tools.car_porting.byd_longitudinal_candidate import encode_candidate, validate_template
from openpilot.tools.car_porting.byd_longitudinal_shadow import ShadowConfig, ShadowController, ShadowInputs
from openpilot.tools.car_porting.runtime_support import require_plan_target_fields


FINGERPRINT = 'BYD_SONG_PLUS_DMI_23_ALT'
SERVICES = ('modelV2', 'radarState', 'longitudinalPlan', 'carState', 'selfdriveState')
CAN_CONTEXT = {(2, 0x32D): 'hud', (0, 0x1F0): 'wheel', (0, 0x342): 'pedals', (2, 0x32E): 'template'}


class ReplayContext:
  """One-segment evidence cache; pending faults survive until the next tick."""

  def __init__(self):
    self.services = {}
    self.can = {}
    self.counters = {}
    self.pending_fault = ''
    self.pending_gas = False
    self.pending_stop = False
    self.pending_template_error = ''
    self.pending_disengagement = False
    self.command_frames = 0
    self.cruise_states = Counter()
    self.hud_error_values = Counter()
    self.can_errors = Counter()

  def ingest_can(self, events, now):
    frames = []
    invalid = any(not event.valid for event in events)
    if invalid:
      self.can.clear()
      self.counters.clear()
      self.pending_fault = 'invalid_can_event'
      self.can_errors[self.pending_fault] += 1
      return
    for event in events:
      frames.extend(f for f in event.can if (f.src, f.address) in CAN_CONTEXT)
    counts = Counter((f.src, f.address) for f in frames)
    for f in frames:
      key = (f.src, f.address)
      name = CAN_CONTEXT[key]
      self.can.pop(name, None)
      if name == 'template':
        self.command_frames += 1
      if counts[key] != 1:
        self.counters.pop(key, None)
        self.pending_fault = f'ambiguous_{name}_batch'
        self.can_errors[self.pending_fault] += 1
        continue
      try:
        values = decode_frame(f.address, f.dat)
      except ValueError as exc:
        self.counters.pop(key, None)
        self.pending_fault = f'{name}_{exc}'
        self.can_errors[self.pending_fault] += 1
        continue
      if 'COUNTER' in values:
        current = int(values['COUNTER'])
        previous = self.counters.get(key)
        self.counters[key] = current
        if previous is not None and current != (previous + 1) % 16:
          self.pending_fault = f'{name}_counter_discontinuity'
          self.can_errors[self.pending_fault] += 1
          continue
      if name == 'hud':
        self.cruise_states[str(int(values['CRUISE_STATE']))] += 1
        # ERROR=4 is present throughout ordinary stock driving and hold in
        # route 7b38267791/37 and capture 20260924T125238Z/000002. Its name is
        # not evidence that every nonzero value is an actual fault flag.
        self.hud_error_values[str(int(values['ERROR']))] += 1
        if values['AEB'] or values['FCW']:
          self.pending_fault = 'observed_aeb_or_fcw'
        self.pending_gas |= values['CRUISE_STATE'] == 5
        self.pending_stop |= values['CRUISE_STATE'] in (6, 7)
        self.pending_disengagement |= values['CRUISE_STATE'] in (0, 1, 2)
        if values['CRUISE_STATE'] not in range(8):
          self.pending_fault = 'observed_unsupported_cruise_state'
      if name == 'template':
        self.pending_stop |= bool(values['STANDSTILL_STATE'] or values['STANDSTILL_RESUME'] or f.dat[0] == 0)
        try:
          validate_template(bytes(f.dat))
        except ValueError as exc:
          self.pending_template_error = f'observed_template_{exc}'
      if name == 'pedals':
        self.pending_gas |= values['GAS_PEDAL'] > 0.01
      if name == 'pedals' and values['BRAKE_PEDAL'] > 0.01:
        self.pending_fault = 'observed_brake_pedal'
      if name == 'wheel' and any(values[f'FAULT_{w}'] for w in ('FL', 'FR', 'BL', 'BR')):
        self.pending_fault = 'observed_wheel_fault'
      if name == 'wheel':
        self.pending_stop |= sum(values[f'WHEELSPEED_{w}'] for w in ('FL', 'FR', 'BL', 'BR')) / 14.4 < 0.1
      self.can[name] = (now, values, bytes(f.dat))

  def ingest_service(self, event, ambiguous):
    name = event.which()
    self.services[name] = event
    if ambiguous or not event.valid:
      self.pending_fault = f'ambiguous_{name}_batch' if ambiguous else f'invalid_{name}'
      return
    if name == 'carState':
      state = event.carState
      self.pending_gas |= state.gasPressed
      self.pending_stop |= state.cruiseState.standstill
      if state.brakePressed or state.stockAeb or state.stockFcw or not state.canValid or state.canTimeout:
        self.pending_fault = 'observed_vehicle_brake_or_fault'
      elif str(state.gearShifter) != 'drive' or not math.isfinite(state.vEgo) or state.vEgo < 0:
        self.pending_fault = 'observed_vehicle_gear_or_speed_fault'
    elif name == 'selfdriveState':
      state = event.selfdriveState
      if state.state == 'softDisabling':
        self.pending_fault = 'observed_selfdrive_exit'
      elif state.state == 'disabled' and not state.enabled and not state.active:
        self.pending_disengagement = True
    elif name == 'radarState':
      radar = event.radarState
      if (any(radar.radarErrors.to_dict().values()) or radar.mdMonoTime <= 0 or
          not 0 <= event.logMonoTime - radar.mdMonoTime < 500_000_000):
        self.pending_fault = 'observed_radar_fault'
    elif name == 'longitudinalPlan':
      plan = event.longitudinalPlan
      self.pending_stop |= plan.shouldStop
      if (plan.fcw or not all(math.isfinite(v) for v in (plan.aTarget, plan.vTargetNow, plan.jTargetNow)) or
          plan.vTargetNow < 0 or plan.modelMonoTime <= 0 or not 0 <= event.logMonoTime - plan.modelMonoTime < 500_000_000 or
          (not plan.allowThrottle and plan.aTarget > 0) or (not plan.allowBrake and plan.aTarget < 0)):
        self.pending_fault = 'observed_plan_fault_or_inhibition'

  def make_inputs(self, now, tick, identity_valid):
    # Missing values use inert placeholders ONLY with a mandatory failure reason.
    data = {'now_ns': now, 'plan_ns': 0, 'model_ns': 0, 'radar_ns': 0, 'plan_model_ns': 0, 'radar_model_ns': 0, 'vehicle_ns': 0,
            'enabled': False, 'cruise_state': 0, 'speed_mps': 0.0, 'gear': 'unknown', 'gas_pressed': False, 'brake_pressed': False,
            'should_stop': False, 'aeb_or_fcw': False, 'accel_target': 0.0, 'speed_target': 0.0, 'jerk_target': 0.0}

    def failed(reason):
      return ShadowInputs(**data, input_fault=reason)

    if not identity_valid:
      return failed('unconfirmed_vehicle_identity')
    if not tick.valid:
      return failed('invalid_carControl')
    if self.pending_fault:
      return failed(self.pending_fault)
    for name in SERVICES:
      event = self.services.get(name)
      if event is None:
        return failed(f'missing_{name}')
      if not event.valid:
        return failed(f'invalid_{name}')
      limit = 100_000_000 if name in ('carState', 'selfdriveState') else 500_000_000
      if event.logMonoTime <= 0 or not 0 <= now - event.logMonoTime < limit:
        return failed(f'stale_or_future_{name}')
    for name in CAN_CONTEXT.values():
      sample = self.can.get(name)
      if sample is None:
        return failed(f'missing_{name}')
      if not 0 <= now - sample[0] < 100_000_000:
        return failed(f'stale_or_future_{name}')
    plan_event = self.services['longitudinalPlan']
    plan = plan_event.longitudinalPlan
    radar_event = self.services['radarState']
    radar = radar_event.radarState
    if plan.modelMonoTime > plan_event.logMonoTime or radar.mdMonoTime > radar_event.logMonoTime:
      return failed('future_source_timestamp')
    cs_event = self.services['carState']
    cs = cs_event.carState
    sd = self.services['selfdriveState'].selfdriveState
    hud, wheel, pedals = (self.can[name][1] for name in ('hud', 'wheel', 'pedals'))
    if not cs.canValid or cs.canTimeout:
      return failed('invalid_vehicle_can')
    if any(radar.radarErrors.to_dict().values()):
      return failed('radar_error')
    if sd.state not in ('disabled', 'preEnabled', 'enabled', 'overriding'):
      return failed('selfdrive_exit_state')
    if not sd.enabled and (sd.state != 'disabled' or sd.active):
      return failed('inconsistent_selfdrive_state')
    if tick.carControl.enabled != sd.enabled:
      return failed('enable_state_disagreement')
    if tick.carControl.enabled and (not sd.active or sd.state not in ('enabled', 'overriding')):
      return failed('selfdrive_not_active')
    if bool(cs.cruiseState.enabled) != (int(hud['CRUISE_STATE']) in (3, 4, 5, 6, 7)):
      return failed('cruise_state_disagreement')
    if any(wheel[f'FAULT_{w}'] for w in ('FL', 'FR', 'BL', 'BR')):
      return failed('wheel_sensor_fault')
    data.update(plan_ns=plan_event.logMonoTime, model_ns=self.services['modelV2'].logMonoTime,
                radar_ns=radar_event.logMonoTime, plan_model_ns=plan.modelMonoTime, radar_model_ns=radar.mdMonoTime,
                vehicle_ns=cs_event.logMonoTime, enabled=bool(tick.carControl.enabled and sd.enabled),
                cruise_state=int(hud['CRUISE_STATE']), speed_mps=sum(wheel[f'WHEELSPEED_{w}'] for w in ('FL', 'FR', 'BL', 'BR')) / 14.4,
                gear=str(cs.gearShifter), gas_pressed=bool(self.pending_gas or cs.gasPressed or pedals['GAS_PEDAL'] > 0.01),
                brake_pressed=bool(cs.brakePressed or pedals['BRAKE_PEDAL'] > 0.01),
                should_stop=bool(self.pending_stop or plan.shouldStop or cs.cruiseState.standstill),
                aeb_or_fcw=bool(hud['AEB'] or hud['FCW'] or cs.stockAeb or cs.stockFcw or plan.fcw),
                accel_target=plan.aTarget, speed_target=plan.vTargetNow, jerk_target=plan.jTargetNow,
                allow_throttle=bool(plan.allowThrottle), allow_brake=bool(plan.allowBrake), interrupted=self.pending_disengagement)
    # Inspect special templates even if HUD has not transitioned yet. Hold is not
    # the ordinary -5 m/s2 value suggested by the candidate DBC field name.
    if data['enabled'] and not (data['gas_pressed'] or data['brake_pressed'] or data['should_stop'] or
                                data['aeb_or_fcw'] or data['cruise_state'] in (5, 6, 7) or data['speed_mps'] < 0.1):
      if self.pending_template_error:
        return failed(self.pending_template_error)
      try:
        validate_template(self.can['template'][2])
      except ValueError as exc:
        return failed(f'template_{exc}')
    return ShadowInputs(**data)


def replay_events(events, config: ShadowConfig | None = None) -> dict:
  """Replay monotonic envelopes; no context is ever shared across invocations.

  CarParams is segment metadata (may be recorded after the first data tick).
  All dynamic inputs use only samples at or before the tick. Services with the
  same timestamp are grouped and duplicates rejected, independent of file order.
  This is NOT full process replay, nor a closed-loop plant simulation.
  """
  events = list(events)
  cfg = config or ShadowConfig()
  identities = [e for e in events if e.which() == 'carParams']
  identity_valid = bool(identities) and all(e.valid and e.carParams.carFingerprint == FINGERPRINT for e in identities)
  ctx, controller = ReplayContext(), ShadowController(cfg)
  states, reasons, messages = Counter(), Counter(), Counter()
  examples, transitions = [], []
  candidate_count = calculation_ticks = recorded_active = clipped = 0
  next_counter = last_encoded_ns = last_group_ns = None
  last_state = None
  accel_range = None
  for now, group_iter in groupby(events, key=lambda e: e.logMonoTime):
    group = list(group_iter)
    if now <= 0 or (last_group_ns is not None and now < last_group_ns):
      ctx.services.clear()
      ctx.can.clear()
      ctx.counters.clear()
      ctx.pending_fault = 'nonmonotonic_event_time'
    last_group_ns = now
    kinds = Counter(e.which() for e in group)
    messages.update(kinds)
    for e in group:
      name = e.which()
      if name in SERVICES:
        ctx.ingest_service(e, kinds[name] > 1)
    if kinds['can']:
      ctx.ingest_can([e for e in group if e.which() == 'can'], now)
    for tick in (e for e in group if e.which() == 'carControl'):
      if kinds['carControl'] > 1:
        ctx.pending_fault = 'ambiguous_carControl_batch'
      calculation_ticks += 1
      recorded_active += int(tick.carControl.longActive)
      sample = ctx.make_inputs(now, tick, identity_valid)
      result = controller.update(sample)
      ctx.pending_fault = ''
      ctx.pending_gas = ctx.pending_stop = False
      ctx.pending_template_error = ''
      ctx.pending_disengagement = False
      states[result.state] += 1
      reasons[result.reason] += 1
      clipped += int(result.target_clipped)
      key = (result.state, result.reason)
      if key != last_state and len(transitions) < 100:
        transitions.append({'logMonoTime': now, 'state': result.state, 'reason': result.reason})
      last_state = key
      if result.accel_mps2 is None:
        next_counter = last_encoded_ns = None
        continue
      if last_encoded_ns is not None and now - last_encoded_ns < 20_000_000:
        continue
      template = ctx.can['template']
      if next_counter is None:
        next_counter = (int(template[1]['COUNTER']) + 1) % 16
      encoded = encode_candidate(template[2], result.accel_mps2, next_counter)
      candidate_count += 1
      low, high = accel_range or (result.accel_mps2, result.accel_mps2)
      accel_range = [min(low, result.accel_mps2), max(high, result.accel_mps2)]
      if len(examples) < 20:
        examples.append({'logMonoTime': now, 'template_logMonoTime': template[0], 'template_hex': template[2].hex(),
                         'counter': next_counter, 'calculated_accel_mps2': result.accel_mps2, **asdict(encoded)})
      last_encoded_ns = now
      next_counter = (next_counter + 1) % 16
  return {'control_ready': False, 'mode': 'offline_hypothesis_only', 'config': asdict(cfg),
          'config_meaning': 'Unvalidated simulation parameters, not BYD actuator safety limits',
          'identity_valid': identity_valid, 'fingerprints': sorted({e.carParams.carFingerprint for e in identities}),
          'can_only': bool(messages['can']) and not any(messages[s] for s in (*SERVICES, 'carControl')),
          'message_counts': dict(messages), 'calculation_ticks': calculation_ticks,
          'recorded_long_active_ticks': recorded_active, 'candidate_count': candidate_count,
          'calculated_accel_range_mps2': accel_range, 'target_clipped_ticks': clipped,
          'states': dict(states), 'reasons': dict(reasons), 'stock_command_frames': ctx.command_frames,
          'cruise_state_frames': dict(ctx.cruise_states), 'can_errors': dict(ctx.can_errors),
          'opaque_hud_error_counts': dict(ctx.hud_error_values),
          'candidate_examples': examples, 'state_transition_examples': transitions,
          'limitations': ['No ECU acceptance, arbitration, dynamics or AEB validation',
                         'Not production LongControl/process replay; no real-time frequency health proof',
                         'Candidate non-acceleration bits are preserved hypotheses, not validated semantics',
                         'No hold/resume synthesis; unchecksummed pedal evidence remains provisional']}


def replay_logs(paths, config: ShadowConfig | None = None) -> dict:
  segments = []
  for path in local_log_paths(paths):
    # Independent processes may arrive in file order different from monotonic
    # timestamps. Merge recorded timestamps; never use future dynamic samples.
    result = replay_events(sorted(read_local_events(path), key=lambda e: e.logMonoTime), config)
    result['source'] = str(path)
    segments.append(result)
  return {'control_ready': False, 'mode': 'offline_hypothesis_only', 'segments': segments,
          'candidate_count': sum(s['candidate_count'] for s in segments)}


def main(argv=None):
  require_plan_target_fields()
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('logs', nargs='+', help='Completed local rlogs; no live or network sources')
  parser.add_argument('--output', type=Path, help='New JSON result; existing files are never overwritten')
  parser.add_argument('--accel-min', type=float, default=-2.0, help='Offline experiment only, not validated BYD limit')
  parser.add_argument('--accel-max', type=float, default=1.0, help='Offline experiment only, not validated BYD limit')
  parser.add_argument('--jerk-up', type=float, default=1.0)
  parser.add_argument('--jerk-down', type=float, default=1.0)
  parser.add_argument('--speed-kp', type=float, default=0.0, help='Optional experimental speed-P correction; no integral')
  args = parser.parse_args(argv)
  try:
    config = ShadowConfig(args.accel_min, args.accel_max, args.jerk_up, args.jerk_down, args.speed_kp)
    result = replay_logs(args.logs, config)
    serialized = json.dumps(result, indent=2, allow_nan=False) + '\n'
    if args.output:
      with args.output.open('x') as output:
        output.write(serialized)
    else:
      print(serialized, end='')
  except (ValueError, OSError, RuntimeWarning) as exc:
    parser.error(str(exc))
  return 0


if __name__ == '__main__':
  raise SystemExit(main())
