"""Synthetic closed-loop LongControl experiments, NOT a BYD actuator model."""

import argparse
import json
import math
from collections import Counter, deque
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from opendbc.car.structs import car
from openpilot.cereal import log
from openpilot.common.realtime import DT_CTRL
from openpilot.tools.car_porting.byd_longcontrol_replay import DT_NS, FINGERPRINT, OfflineLongControl
from openpilot.tools.car_porting.byd_longitudinal_shadow import ShadowInputs
from openpilot.tools.car_porting.runtime_support import require_plan_target_fields


SCENARIOS = ('tracking', 'brake', 'gas', 'aeb', 'stale_plan', 'nan_plan', 'invalid_vehicle', 'stop', 'disabled', 'reject_commands')


@dataclass(frozen=True)
class PlantConfig:
  # Hypotheses for software experiments; none measured on this vehicle.
  delay_s: float = .15
  lag_s: float = .3
  watchdog_s: float = .1
  accel_min: float = -2.
  accel_max: float = 1.

  def __post_init__(self):
    if (not all(math.isfinite(v) for v in asdict(self).values()) or
        not 0 <= self.delay_s <= 5 or not DT_CTRL <= self.lag_s <= 10 or
        not DT_CTRL <= self.watchdog_s <= 5 or not -10 <= self.accel_min < 0 < self.accel_max <= 10 or
        any(not math.isclose(t / DT_CTRL, round(t / DT_CTRL), abs_tol=1e-9) for t in (self.delay_s, self.watchdog_s))):
      raise ValueError('Invalid hypothetical plant configuration')


@dataclass(frozen=True)
class PlantState:
  speed_mps: float
  accel_mps2: float
  watchdog_expired: bool


class HypotheticalActuator:
  """Delay queue, first-order lag, and hypothetical watchdog at fixed DT_CTRL.

  None is an absent command, not zero. On watchdog expiry queued commands are
  discarded and the desired acceleration becomes zero; actual synthetic
  acceleration decays with lag. This is NOT an ECU watchdog or stopping model.
  """

  def __init__(self, config=None, initial_speed=10.):
    self.config = config or PlantConfig()
    if not math.isfinite(initial_speed) or initial_speed < 0:
      raise ValueError('Invalid initial synthetic speed')
    self.speed = float(initial_speed)
    self.accel = self.target = 0.
    self.index = self.accepted_commands = 0
    self.last_command = None
    self.queue = deque()

  def step(self, command, accept=True):
    cfg = self.config
    if command is not None and (not math.isfinite(command) or not cfg.accel_min <= command <= cfg.accel_max):
      raise ValueError('Invalid synthetic actuator command')
    if command is not None and accept:
      self.queue.append((self.index + round(cfg.delay_s / DT_CTRL), command))
      self.last_command = self.index
      self.accepted_commands += 1
    expired = self.last_command is None or self.index - self.last_command >= round(cfg.watchdog_s / DT_CTRL)
    if expired:
      self.queue.clear()
      self.target = 0.
    else:
      while self.queue and self.queue[0][0] <= self.index:
        _, self.target = self.queue.popleft()
    self.accel += (self.target - self.accel) * DT_CTRL / cfg.lag_s
    self.accel = max(self.accel, -self.speed / DT_CTRL)
    self.speed = max(0., self.speed + self.accel * DT_CTRL)
    self.index += 1
    return PlantState(self.speed, self.accel, expired)


def simulate(scenario, config=None):
  if scenario not in SCENARIOS:
    raise ValueError('Unknown synthetic scenario')
  cp = car.CarParams.new_message(carFingerprint=FINGERPRINT, openpilotLongitudinalControl=False,
                                 vEgoStarting=.5, stopAccel=-.5, stoppingDecelRate=.8)
  cp.longitudinalTuning.kpBP = [0.]
  cp.longitudinalTuning.kpV = [.6]
  cp.longitudinalTuning.kiBP = [0.]
  cp.longitudinalTuning.kiV = [.1]
  cp.longitudinalTuning.kf = 1.
  controller = OfflineLongControl(cp)
  plant = HypotheticalActuator(config)
  states, reasons = Counter(), Counter()
  outputs, examples = [], []
  fault_outputs = resumed = saturated = 0
  for i in range(1500):
    now = 1_000_000_000 + i * DT_NS
    cs = car.CarState.new_message(vEgo=plant.speed, aEgo=plant.accel, canValid=True, gearShifter='drive')
    cs.cruiseState.enabled = True
    plan = log.LongitudinalPlan.new_message(aTarget=0., vTargetNow=11., jTargetNow=0., allowThrottle=True, allowBrake=True)
    radar = log.RadarState.new_message()
    sample = ShadowInputs(now, now, now, now, now, now, now, True, 3, plant.speed, 'drive',
                          False, False, False, False, 0., 11., 0.)
    fault_window = 500 <= i < 600 and scenario not in ('tracking', 'reject_commands')
    if fault_window:
      if scenario == 'brake':
        cs.brakePressed = True
      elif scenario == 'gas':
        cs.gasPressed = True
      elif scenario == 'aeb':
        cs.stockAeb = True
      elif scenario == 'stale_plan':
        sample = replace(sample, plan_ns=now - 500_000_000)
      elif scenario == 'nan_plan':
        plan.aTarget = float('nan')
      elif scenario == 'invalid_vehicle':
        cs.canValid = False
      elif scenario == 'stop':
        plan.shouldStop = True
      elif scenario == 'disabled':
        sample = replace(sample, enabled=False, cruise_state=2)
    # Deliberate healthy off interval to demonstrate recovery after latched faults.
    if 800 <= i < 900 and scenario not in ('tracking', 'reject_commands'):
      sample = replace(sample, enabled=False, cruise_state=2)
    result = controller.step(sample, cs, plan, radar)
    state = plant.step(result.accel_mps2, accept=scenario != 'reject_commands')
    states[result.state] += 1
    reasons[result.reason] += 1
    if result.accel_mps2 is not None:
      outputs.append(result.accel_mps2)
      fault_outputs += int(fault_window)
      resumed += int(i >= 900)
      saturated += int(abs(result.accel_mps2 - 1.) < 1e-6 or abs(result.accel_mps2 + 2.) < 1e-6)
    if i % 100 == 0 or i in (499, 500, 599, 600, 799, 800, 899, 900, 1499):
      examples.append({'time_s': i * DT_CTRL, 'synthetic_speed_mps': state.speed_mps,
                       'synthetic_accel_mps2': state.accel_mps2, 'controller_accel_mps2': result.accel_mps2,
                       'state': result.state, 'watchdog_expired': state.watchdog_expired})
  return {
    'control_ready': False, 'mode': 'synthetic_closed_loop_not_byd_validation', 'scenario': scenario,
    'duration_s': 15., 'dt_s': DT_CTRL, 'plant_assumptions': asdict(plant.config),
    'controller_gains': cp.longitudinalTuning.to_dict(),
    'provenance': 'Synthetic speed target 10 to 11 m/s; experimental PID gains and unmeasured plant parameters',
    'initial_speed_mps': 10., 'target_speed_mps': 11., 'final_speed_mps': plant.speed,
    'output_ticks': len(outputs), 'fault_window_output_ticks': fault_outputs,
    'resumed_after_disengagement_ticks': resumed, 'saturation_ticks': saturated,
    'plant_accepted_commands': plant.accepted_commands,
    'accel_range_mps2': [min(outputs), max(outputs)] if outputs else None,
    'states': dict(states), 'reasons': dict(reasons), 'trace_examples': examples,
    'limitations': ['No CAN encoding, ECU acceptance or AEB arbitration validation',
                    'Pedal/AEB scenarios test command suppression, not physical driver braking or collision avoidance',
                    'Stopping/holding/resuming commands remain blocked; not a full-speed vehicle model',
                    'Rejected actuator is a negative case; offline gate does not implement ECU feedback handling'],
  }


def main(argv=None):
  require_plan_target_fields()
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--output', type=Path, help='New JSON file; never overwrite')
  args = parser.parse_args(argv)
  try:
    result = {'control_ready': False, 'mode': 'synthetic_closed_loop_not_byd_validation',
              'scenarios': [simulate(scenario) for scenario in SCENARIOS]}
    serialized = json.dumps(result, indent=2, allow_nan=False) + '\n'
    if args.output:
      with args.output.open('x') as report:
        report.write(serialized)
    else:
      print(serialized, end='')
  except (ValueError, OSError) as exc:
    parser.error(str(exc))
  return 0


if __name__ == '__main__':
  raise SystemExit(main())
