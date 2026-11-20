"""Production LongControl, offline historical open-loop replay only. No transport."""

import argparse
import hashlib
import json
import math
from collections import Counter
from dataclasses import replace
from itertools import groupby
from pathlib import Path
from types import MappingProxyType

from opendbc.car.structs import car
from openpilot.cereal import log
from openpilot.common.realtime import DT_CTRL
from openpilot.selfdrive.controls.lib.longcontrol import LongControl, LongCtrlState
from openpilot.tools.car_porting.byd_longcontrol_diagnostics import ReplayDiagnostics
from openpilot.tools.car_porting.byd_longitudinal_audit import local_log_paths, read_local_events
from openpilot.tools.car_porting.byd_longitudinal_replay import CAN_CONTEXT, FINGERPRINT, SERVICES, ReplayContext
from openpilot.tools.car_porting.byd_longitudinal_shadow import ShadowConfig, ShadowController, ShadowResult
from openpilot.tools.car_porting.runtime_support import require_plan_target_fields


DT_NS = round(DT_CTRL * 1e9)
MAX_SPAN_NS = 600_000_000_000
MODE = 'historical_open_loop_longcontrol'


def cp_snapshot(cp):
  """Only the recorded metadata needed by LongControl, not VIN/ECU identifiers."""
  out = {name: getattr(cp, name) for name in ('carFingerprint', 'openpilotLongitudinalControl', 'startingState',
                                            'vEgoStarting', 'stopAccel', 'startAccel', 'stoppingDecelRate')}
  out['longitudinalTuning'] = cp.longitudinalTuning.to_dict()
  return out


def validate_cp(cp):
  if cp.carFingerprint != FINGERPRINT:
    raise ValueError('Unconfirmed vehicle fingerprint')
  tune = cp.longitudinalTuning
  for bp, gains in ((list(tune.kpBP), list(tune.kpV)), (list(tune.kiBP), list(tune.kiV))):
    if (not bp or len(bp) != len(gains) or not all(math.isfinite(v) and v >= 0 for v in (*bp, *gains)) or
        any(a >= b for a, b in zip(bp, bp[1:], strict=False))):
      raise ValueError('Invalid recorded longitudinal tuning')
  if (not all(math.isfinite(v) for v in (tune.kf, cp.vEgoStarting, cp.stopAccel, cp.startAccel, cp.stoppingDecelRate)) or
      tune.kf < 0 or cp.vEgoStarting < 0 or cp.stopAccel >= 0 or cp.startAccel < 0 or cp.stoppingDecelRate <= 0):
    raise ValueError('Invalid recorded longitudinal parameters')


class FrozenReplayParams:
  """Read-only in-memory values; unknown future controller keys fail loudly.

  Logged CP gains are held constant. Runtime tuning/stop-smoothing Params are
  NOT captured in these logs: zero means CP fallback/no smoothing; gas ramp is
  the controller constructor's existing 5 s assumption, not a measured value.
  """

  def __init__(self, cp):
    tune = cp.longitudinalTuning
    self.values = MappingProxyType({
      'LongTuningKpV': float(tune.kpV[0]) * 100,
      'LongTuningKiV': float(tune.kiV[0]) * 1000,
      'LongTuningKf': float(tune.kf) * 100,
      'StoppingAccel': 0., 'StoppingDecelRate': 0., 'StartAccel': 0.,
      'DecelLimitVEgoMax': 0., 'DecelLimitVEgoMin': 0.,
      'DecelLimitAEgoMax': 0., 'DecelLimitAEgoMin': 0.,
      'GasSmoothTime': 50., 'SmoothStopMode': 0,
    })

  def get_float(self, key):
    return float(self.values[key])

  def get_int(self, key):
    return int(self.values[key])


class OfflineLongControl:
  """Eligibility checks plus real PID; no command encoding or vehicle ownership.

  The old shadow controller supplies only conservative eligibility/latching.
  Its acceleration calculation is discarded. Reported values come directly
  from LongControl with experimental acceleration bounds and no added slew.
  """

  def __init__(self, cp, config=None):
    validate_cp(cp)
    self.recorded_cp = cp_snapshot(cp)
    self.cp = car.CarParams.new_message(**self.recorded_cp)
    self.cp.openpilotLongitudinalControl = True  # isolated object, never persisted
    self.config = config or ShadowConfig()
    if self.config.speed_kp != 0:
      raise ValueError('Use recorded LongControl gains, not shadow speed-P')
    self.params = FrozenReplayParams(self.cp)
    self.controller = LongControl(self.cp, params=self.params)
    self.gate = ShadowController(self.config)
    self.last_ns = None

  def _withdraw(self, gas_pressed=False):
    # Exercise production inactive/reset path, using only inert local objects.
    self.controller.update(False, car.CarState.new_message(gasPressed=gas_pressed), log.LongitudinalPlan.new_message(),
                           (self.config.accel_min, self.config.accel_max), 0., log.RadarState.new_message())

  def step(self, sample, cs, plan, radar):
    if self.last_ns is not None and sample.now_ns - self.last_ns != DT_NS:
      sample = replace(sample, input_fault='nonuniform_controller_timestep')
    self.last_ns = sample.now_ns
    if cs is not None and (not math.isfinite(cs.vEgo) or not math.isfinite(cs.aEgo) or cs.vEgo < 0):
      sample = replace(sample, input_fault='nonfinite_or_negative_vehicle_dynamics')
    if any(v is None for v in (cs, plan, radar)):
      sample = replace(sample, input_fault=sample.input_fault or 'missing_controller_input')
    else:
      # Never let an adapter's reduced sample hide actual controller inputs.
      sample = replace(sample, gas_pressed=sample.gas_pressed or cs.gasPressed,
                       brake_pressed=sample.brake_pressed or cs.brakePressed,
                       should_stop=sample.should_stop or plan.shouldStop or cs.cruiseState.standstill or cs.softHoldActive > 0 or cs.vEgo < .1,
                       aeb_or_fcw=sample.aeb_or_fcw or cs.stockAeb or cs.stockFcw or plan.fcw,
                       allow_throttle=sample.allow_throttle and plan.allowThrottle,
                       allow_brake=sample.allow_brake and plan.allowBrake,
                       accel_target=plan.aTarget, speed_target=plan.vTargetNow, jerk_target=plan.jTargetNow)
      if not cs.canValid or cs.canTimeout or str(cs.gearShifter) != 'drive' or any(radar.radarErrors.to_dict().values()):
        sample = replace(sample, input_fault=sample.input_fault or 'invalid_vehicle_or_radar')
    gate = self.gate.update(sample)
    if gate.accel_mps2 is None:
      self._withdraw(sample.gas_pressed)
      return gate
    output, _, _ = self.controller.update(True, cs, plan, (self.config.accel_min, self.config.accel_max),
                                           (sample.now_ns - sample.plan_ns) * 1e-9, radar)
    protocol_blocked = self.controller.long_control_state != LongCtrlState.pid
    if (protocol_blocked or not math.isfinite(output) or (output > 0 and not sample.allow_throttle) or
        (output < 0 and not sample.allow_brake)):
      self.gate.latched = True
      self.gate.armed = False
      # Already executed this fixed timestep. Reset without a second update,
      # which would advance the periodic Params clock twice at the same time.
      self.controller.reset()
      self.controller.long_control_state = LongCtrlState.off
      self.controller.last_output_accel = 0.
      if protocol_blocked:
        return ShadowResult('protocol_blocked', 'stop_resume_unverified')
      return ShadowResult('inhibited', 'nonfinite_or_inhibited_pid_output')
    return ShadowResult('tracking', 'production_longcontrol_offline', float(output))


def replay_events(events, config=None):
  """Causal zero-order hold on a 100 Hz grid, anchored to first carControl.

  All envelopes at a grid time are ingested first; same-service duplicates are
  ambiguous. Short faults persist until a grid tick. CarParams is segment
  metadata and must be consistent. No state is shared across calls.
  """
  events = list(events)
  if any(e.logMonoTime <= 0 for e in events) or any(a.logMonoTime > b.logMonoTime for a, b in zip(events, events[1:], strict=False)):
    raise ValueError('Expected positive monotonic recorded timestamps')
  messages = Counter(e.which() for e in events)
  ticks = [e for e in events if e.which() == 'carControl']
  identities = [e for e in events if e.which() == 'carParams']
  identity_error = ''
  controller = None
  try:
    if not identities or any(not e.valid for e in identities):
      raise ValueError('Missing or invalid recorded CarParams')
    snapshots = [cp_snapshot(e.carParams) for e in identities]
    if any(s != snapshots[0] for s in snapshots[1:]):
      raise ValueError('Conflicting recorded CarParams')
    controller = OfflineLongControl(identities[0].carParams, config)
  except ValueError as exc:
    identity_error = str(exc)
  if ticks and ticks[-1].logMonoTime - ticks[0].logMonoTime > MAX_SPAN_NS:
    raise ValueError('Segment exceeds 600 s offline expansion limit')

  ctx = ReplayContext()
  diagnostics = ReplayDiagnostics(DT_NS, CAN_CONTEXT)
  states, reasons, control_states = Counter(), Counter(), Counter()
  output_examples, transitions = [], []
  outputs = []
  recorded_periods = [(b.logMonoTime - a.logMonoTime) / 1e6 for a, b in zip(ticks, ticks[1:], strict=False)]
  last_tick = None
  last_key = None
  grid_ticks = 0
  next_ns = ticks[0].logMonoTime if ticks else None
  final_ns = ticks[-1].logMonoTime if ticks else None

  def step(now):
    nonlocal grid_ticks, last_key
    grid_ticks += 1
    if last_tick is None or not 0 <= now - last_tick.logMonoTime < 100_000_000:
      ctx.pending_fault = 'stale_carControl'
    sample = ctx.make_inputs(now, last_tick, controller is not None)
    def get(name):
      return getattr(ctx.services[name], name) if name in ctx.services else None
    was_latched = controller.gate.latched if controller else False
    result = controller.step(sample, get('carState'), get('longitudinalPlan'), get('radarState')) if controller else (
      ShadowResult('invalid', 'unconfirmed_vehicle_identity'))
    diagnostics.observe_tick(now, last_tick, ctx.services.get('selfdriveState'), result, was_latched,
                             controller.gate.latched if controller else False, ctx)
    states[result.state] += 1
    reasons[result.reason] += 1
    if controller:
      control_states[str(controller.controller.long_control_state)] += 1
    key = result.state, result.reason
    if key != last_key and len(transitions) < 100:
      transitions.append({'logMonoTime': now, 'state': result.state, 'reason': result.reason})
    last_key = key
    if result.accel_mps2 is not None:
      outputs.append(result.accel_mps2)
      if len(output_examples) < 30:
        output_examples.append({'logMonoTime': now, 'plan_logMonoTime': sample.plan_ns,
                                'carState_logMonoTime': sample.vehicle_ns, 'carControl_logMonoTime': last_tick.logMonoTime,
                                'measured_vEgo_mps': float(get('carState').vEgo), 'accel_mps2': result.accel_mps2,
                                'pid_p': float(controller.controller.pid.p), 'pid_i': float(controller.controller.pid.i),
                                'pid_f': float(controller.controller.pid.f), 'hypothetical_activation': True})
    ctx.pending_fault = ctx.pending_template_error = ''
    ctx.pending_gas = ctx.pending_stop = ctx.pending_disengagement = False

  for now, group_iter in groupby(events, key=lambda e: e.logMonoTime):
    while next_ns is not None and next_ns < now and next_ns <= final_ns:
      step(next_ns)
      next_ns += DT_NS
    group = list(group_iter)
    diagnostics.observe_group(now, group)
    kinds = Counter(e.which() for e in group)
    for e in group:
      name = e.which()
      if name in SERVICES:
        ctx.ingest_service(e, kinds[name] > 1)
        if name == 'carState':
          ctx.pending_stop |= e.carState.softHoldActive > 0
          if not math.isfinite(e.carState.aEgo):
            ctx.pending_fault = 'observed_nonfinite_acceleration'
      elif name == 'carControl':
        last_tick = e
        if kinds[name] > 1 or not e.valid:
          ctx.pending_fault = 'invalid_or_ambiguous_carControl'
        if not e.carControl.enabled:
          ctx.pending_disengagement = True
    if kinds['can']:
      ctx.ingest_can([e for e in group if e.which() == 'can'], now)
    if next_ns == now and next_ns <= final_ns:
      step(next_ns)
      next_ns += DT_NS
  cfg = controller.config if controller else config or ShadowConfig()
  return {
    'control_ready': False, 'mode': MODE, 'identity_valid': controller is not None, 'identity_error': identity_error,
    'can_only': bool(messages['can']) and not any(messages[s] for s in (*SERVICES, 'carControl')),
    'message_counts': dict(messages), 'recorded_carcontrol_ticks': len(ticks),
    'recorded_long_active_ticks': sum(bool(e.carControl.longActive) for e in ticks),
    'grid_ticks': grid_ticks, 'pid_output_ticks': len(outputs), 'states': dict(states), 'reasons': dict(reasons),
    'longcontrol_states': dict(control_states), 'accel_range_mps2': [min(outputs), max(outputs)] if outputs else None,
    'timing': {'dt_ns': DT_NS, 'resampling': 'causal_zero_order_hold', 'carcontrol_max_age_ms': 100,
               'recorded_period_ms_min': min(recorded_periods) if recorded_periods else None,
               'recorded_period_ms_max': max(recorded_periods) if recorded_periods else None},
    'recorded_cp': controller.recorded_cp if controller else None,
    'isolated_parameter_snapshot': dict(controller.params.values) if controller else None,
    'experimental_accel_limits_mps2': [cfg.accel_min, cfg.accel_max],
    'config_provenance': 'CP gains recorded; runtime Params unavailable: CP fallback, smooth-stop off, 5 s gas ramp assumption',
    'output_examples': output_examples, 'state_transition_examples': transitions,
    'diagnostics': diagnostics.finish(),
    'can_errors': dict(ctx.can_errors), 'stock_command_frames': ctx.command_frames,
    'limitations': [
      'Hypothetical activation from recorded enable, NOT recorded longActive or ECU ownership',
      'Historical measured speed is unchanged by replay output; NOT a closed-loop vehicle response',
      'Production LongControl only, NOT full controls/planner/perception process replay',
      'Acceleration limits and non-recorded Params are offline assumptions, NOT BYD calibration',
      'No CAN candidate or transport; stop/hold/resume remain protocol-blocked',
      'No ECU acceptance/timeout/arbitration/AEB validation or real-time frequency proof',
    ],
  }


def replay_logs(paths, config=None):
  segments = []
  for path in local_log_paths(paths):
    result = replay_events(sorted(read_local_events(path), key=lambda e: e.logMonoTime), config)
    result['source'] = str(path)
    result['source_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    segments.append(result)
  return {'control_ready': False, 'mode': MODE, 'segments': segments,
          'grid_ticks': sum(s['grid_ticks'] for s in segments),
          'pid_output_ticks': sum(s['pid_output_ticks'] for s in segments)}


def main(argv=None):
  require_plan_target_fields()
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('logs', nargs='+', help='Completed local recordings; no device or network sources')
  parser.add_argument('--output', type=Path, help='New report file, never overwrite')
  parser.add_argument('--accel-min', type=float, default=-2., help='Uncalibrated offline bound')
  parser.add_argument('--accel-max', type=float, default=1., help='Uncalibrated offline bound')
  args = parser.parse_args(argv)
  try:
    result = replay_logs(args.logs, ShadowConfig(accel_min=args.accel_min, accel_max=args.accel_max))
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
