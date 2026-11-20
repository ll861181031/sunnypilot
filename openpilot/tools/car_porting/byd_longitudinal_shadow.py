"""Pure offline hypothesis controller. Never import into the live vehicle path."""

import math
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ShadowConfig:
  # Experimental replay settings, NOT measured BYD actuator limits/tuning.
  accel_min: float = -2.0
  accel_max: float = 1.0
  jerk_up: float = 1.0
  jerk_down: float = 1.0
  speed_kp: float = 0.0

  def __post_init__(self):
    values = (self.accel_min, self.accel_max, self.jerk_up, self.jerk_down, self.speed_kp)
    if (not all(math.isfinite(v) for v in values) or not -4.95 <= self.accel_min <= 0 <= self.accel_max <= 7.75 or
        self.accel_min == self.accel_max or self.jerk_up <= 0 or self.jerk_down <= 0 or self.speed_kp < 0):
      raise ValueError('Invalid offline experiment configuration')


@dataclass(frozen=True)
class ShadowInputs:
  now_ns: int
  plan_ns: int
  model_ns: int
  radar_ns: int
  plan_model_ns: int
  radar_model_ns: int
  vehicle_ns: int
  enabled: bool
  cruise_state: int
  speed_mps: float
  gear: str
  gas_pressed: bool
  brake_pressed: bool
  should_stop: bool
  aeb_or_fcw: bool
  accel_target: float
  speed_target: float
  jerk_target: float
  input_fault: str = ''
  allow_throttle: bool = True
  allow_brake: bool = True
  interrupted: bool = False


@dataclass(frozen=True)
class ShadowResult:
  state: str
  reason: str
  accel_mps2: float | None = None
  target_clipped: bool = False
  control_ready: bool = field(default=False, init=False)


class ShadowController:
  """Feedforward + optional speed-P experiment; not the production OP PID.

  Timestamps describe recorded host messages, not real ECU deadlines. There is
  deliberately no hold/resume synthesis or output transport. ``None`` means no
  diagnostic candidate; it does NOT mean send zero acceleration to the vehicle.
  """

  def __init__(self, config: ShadowConfig | None = None):
    self.config = config or ShadowConfig()
    self.last_ns = None
    self.last_accel = 0.0
    self.armed = False
    self.latched = False

  def _suppress(self, state: str, reason: str, latch: bool = False) -> ShadowResult:
    self.latched |= latch and self.armed
    self.armed = False
    self.last_accel = 0.0
    return ShadowResult(state, reason)

  def update(self, sample: ShadowInputs) -> ShadowResult:
    previous_ns = self.last_ns
    self.last_ns = sample.now_ns
    if sample.input_fault:
      return self._suppress('invalid', sample.input_fault, latch=True)
    for name in ('plan_ns', 'model_ns', 'radar_ns', 'plan_model_ns', 'radar_model_ns', 'vehicle_ns'):
      stamp = getattr(sample, name)
      age_limit = 100_000_000 if name == 'vehicle_ns' else 500_000_000
      if stamp <= 0 or not 0 <= sample.now_ns - stamp < age_limit:
        return self._suppress('invalid', f'stale_or_future_{name}', latch=True)
    if previous_ns is not None and not 0 < sample.now_ns - previous_ns <= 100_000_000:
      return self._suppress('invalid', 'tick_discontinuity', latch=True)
    if not all(math.isfinite(v) for v in (sample.speed_mps, sample.accel_target, sample.speed_target, sample.jerk_target)):
      return self._suppress('invalid', 'nonfinite_input', latch=True)
    if sample.speed_mps < 0 or sample.speed_target < 0:
      return self._suppress('invalid', 'negative_speed', latch=True)
    if sample.cruise_state not in range(8):
      return self._suppress('invalid', 'unsupported_cruise_state', latch=True)
    if sample.brake_pressed:
      return self._suppress('brake', 'brake_pressed', latch=True)
    if sample.aeb_or_fcw:
      return self._suppress('invalid', 'aeb_or_fcw', latch=True)
    if sample.gear != 'drive':
      return self._suppress('invalid', 'not_in_drive', latch=True)
    # Only a healthy disengagement clears the latch, never a data dropout.
    if not sample.enabled or sample.cruise_state in (0, 1, 2):
      self.latched = False
      return self._suppress('off', 'disengaged')
    if self.latched:
      return self._suppress('fault_latched', 'healthy_disengagement_required')
    if sample.should_stop or sample.speed_mps < 0.1 or sample.cruise_state in (6, 7):
      return self._suppress('protocol_blocked', 'stop_resume_unverified', latch=True)
    if sample.gas_pressed or sample.cruise_state == 5:
      return self._suppress('override', 'driver_gas_override')
    if sample.interrupted:
      return self._suppress('interrupted', 'observed_disengagement')
    cfg = self.config
    target = sample.accel_target + cfg.speed_kp * (sample.speed_target - sample.speed_mps)
    if not math.isfinite(target):
      return self._suppress('invalid', 'nonfinite_computed_target', latch=True)
    if not sample.allow_throttle and (target > 0 or self.last_accel > 0):
      return self._suppress('inhibited', 'plan_throttle_inhibited', latch=True)
    if not sample.allow_brake and (target < 0 or self.last_accel < 0):
      return self._suppress('inhibited', 'plan_brake_inhibited', latch=True)
    if not self.armed:
      self.armed = True
      return ShadowResult('arming', 'zero_seed')
    bounded = max(cfg.accel_min, min(cfg.accel_max, target))
    dt = (sample.now_ns - previous_ns) * 1e-9
    self.last_accel = max(self.last_accel - cfg.jerk_down * dt, min(self.last_accel + cfg.jerk_up * dt, bounded))
    return ShadowResult('tracking', 'offline_hypothesis', self.last_accel, bounded != target)
