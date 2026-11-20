"""Observe sustained steering pressure before proposing a positive-acceleration cap."""

from dataclasses import dataclass
import math

from opendbc.car.byd.lateral_diagnostics import Limit


@dataclass(frozen=True)
class TurnSpeedGuardResult:
  valid: bool = False
  active: bool = False
  proposed_accel_max: float = math.inf
  accel_max: float = math.inf
  reason: str = 'inactive'
  error_deg: float = 0.0
  pressure_seconds: float = 0.0


class TurnSpeedGuard:
  """Shadow-only by default; no angle, rate, or braking authority is added.

  The error threshold comes from the existing per-frame lead bound. Confirmation
  and freshness are observation settings, not validated vehicle control limits.
  """

  def __init__(self, *, enforce: bool = False, confirmation_time: float = 0.3, max_age: float = 0.2):
    self.enforce = enforce
    self.confirmation_ns = int(confirmation_time * 1e9)
    self.max_age_ns = int(max_age * 1e9)
    self._error_since_ns = 0
    self._last_sample_ns = 0
    self._last_now_ns = 0
    self._last_evidence_ns = 0
    self._evidence_reason = ''
    self._turn_sign = 0

  def _reset(self, reason: str, *, valid: bool = False, error_deg: float = 0.0) -> TurnSpeedGuardResult:
    self._error_since_ns = 0
    self._last_sample_ns = 0
    self._last_evidence_ns = 0
    self._evidence_reason = ''
    self._turn_sign = 0
    return TurnSpeedGuardResult(valid=valid, reason=reason, error_deg=error_deg)

  def update(self, car_state_sp, *, now_ns: int, message_alive: bool, message_valid: bool,
             long_active: bool, lateral_active: bool, overriding: bool, v_ego: float) -> TurnSpeedGuardResult:
    backwards = now_ns < self._last_now_ns
    self._last_now_ns = now_ns
    if backwards:
      return self._reset('clock_reversal')
    if overriding:
      return self._reset('driver_override')
    if not long_active or not lateral_active:
      return self._reset('control_inactive')
    if not math.isfinite(v_ego) or v_ego <= 0.0:
      return self._reset('not_moving')
    if not message_alive or not message_valid:
      return self._reset('message_unavailable')

    diag = car_state_sp.bydLateral
    if not diag.valid or not diag.requestedActive or not diag.active:
      return self._reset('diagnostics_inactive')
    sample_ns, submitted_ns = int(diag.sampleMonoTime), int(diag.lastSubmittedMonoTime)
    # submitted is only true in the send cycle. The recent real submission is
    # what makes the intervening diagnostic samples usable by the 20 Hz planner.
    if (sample_ns <= 0 or submitted_ns <= 0 or submitted_ns > sample_ns or
        not 0 <= now_ns - sample_ns <= self.max_age_ns or
        not 0 <= now_ns - submitted_ns <= self.max_age_ns or sample_ns < self._last_sample_ns):
      return self._reset('stale_diagnostics')
    desired, measured, final, bound = (float(diag.desiredAngleDeg), float(diag.measuredAngleDeg),
                                        float(diag.finalAngleDeg), float(diag.leadBoundDeg))
    if not all(math.isfinite(value) for value in (desired, measured, final, bound)) or bound <= 0.0:
      return self._reset('invalid_angles')

    error = desired - measured
    same_direction = desired * final > 0.0 and desired * measured >= 0.0 and final * error > 0.0
    progressing = abs(final) >= abs(measured)
    if not same_direction or not progressing or abs(error) <= bound:
      return self._reset('tracking_clear', valid=True, error_deg=error)

    if self._last_sample_ns and sample_ns - self._last_sample_ns > self.max_age_ns:
      self._reset('sample_gap')
    turn_sign = 1 if desired > 0.0 else -1
    if self._turn_sign and self._turn_sign != turn_sign:
      self._reset('turn_direction_changed')
    self._turn_sign = turn_sign
    self._last_sample_ns = sample_ns
    if not self._error_since_ns:
      self._error_since_ns = sample_ns
    elapsed_ns = sample_ns - self._error_since_ns
    reasons = []
    if int(diag.limitMask) & int(Limit.LEAD):
      reasons.append('lead')
    if int(diag.limitMask) & int(Limit.RATE):
      reasons.append('rate')
    if car_state_sp.epsTooFast:
      reasons.append('eps_too_fast')
    if reasons:
      self._last_evidence_ns = sample_ns
      self._evidence_reason = '+'.join(reasons)
    recent_evidence = self._last_evidence_ns > 0 and sample_ns - self._last_evidence_ns <= self.confirmation_ns
    active = elapsed_ns >= self.confirmation_ns and recent_evidence
    reason = ('pressure:' if active else 'confirming:') + self._evidence_reason if recent_evidence else 'no_limiting_evidence'
    return TurnSpeedGuardResult(valid=True, active=active, proposed_accel_max=0.0 if active else math.inf,
                                accel_max=0.0 if active and self.enforce else math.inf, reason=reason,
                                error_deg=error, pressure_seconds=elapsed_ns / 1e9)
