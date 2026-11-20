"""A bounded no-acceleration candidate after loss of a trustworthy close lead."""
from dataclasses import dataclass
import math
from typing import Any


LEAD_GUARD_MAX_AGE = 1.25
LEAD_GUARD_ENTER_DISTANCE = 45.0
LEAD_GUARD_FRESH_AGE = 0.25
_RELEASE_REASONS = {'cutOut', 'away', 'far', 'invalidClock', 'invalidDistance'}


def _field(lead: Any, name: str, default: Any = None) -> Any:
  return lead.get(name, default) if isinstance(lead, dict) else getattr(lead, name, default)


@dataclass(frozen=True)
class LeadUncertaintyDecision:
  active: bool = False
  acceleration: float = 0.0
  reason: str = 'noHistory'
  age: float = 0.0
  track_id: int = 0


class LeadUncertaintyGuard:
  """Compute a candidate only; the caller owns recording and candidate merging.

  The deadline is derived from the trustworthy observation timestamp. Predictions,
  uncertain observations and duplicate messages never extend it. A reset requires
  a new measured lead before the guard can arm again.
  """

  def __init__(self):
    self._observed: dict[int, float] = {}
    self._last_model_time = -math.inf
    self._last_now = -math.inf
    self._decision = LeadUncertaintyDecision()

  def reset(self) -> None:
    self._observed.clear()
    self._decision = LeadUncertaintyDecision()

  def update(self, leads: list[Any], model_time: float, v_ego: float, enabled: bool,
             standstill: bool = False, driver_override: bool = False, now: float | None = None) -> LeadUncertaintyDecision:
    now = model_time if now is None else now
    if not enabled or driver_override or standstill or v_ego <= .3:
      self.reset()
      return LeadUncertaintyDecision(reason='driverOverride' if driver_override else 'inactive')
    if not all(math.isfinite(value) for value in (model_time, now, v_ego)) or model_time < 0.0 or now < model_time:
      self.reset()
      return LeadUncertaintyDecision(reason='invalidClock')
    if model_time < self._last_model_time or now < self._last_now:
      self.reset()
      return LeadUncertaintyDecision(reason='invalidClock')
    self._last_now = now
    if model_time == self._last_model_time:
      observed_at = self._observed.get(self._decision.track_id)
      if self._decision.active and observed_at is not None and now - observed_at <= LEAD_GUARD_MAX_AGE:
        self._decision = LeadUncertaintyDecision(True, 0.0, 'leadUncertain', now - observed_at, self._decision.track_id)
        return self._decision
      self._decision = LeadUncertaintyDecision(reason='staleModel')
      return self._decision
    self._last_model_time = model_time

    eligible = []
    visible_ids = set()
    for lead in leads:
      track_id = int(_field(lead, 'visionTrackId', 0))
      if track_id <= 0:
        continue
      visible_ids.add(track_id)
      state = str(_field(lead, 'observationState', 'unknown'))
      reason = str(_field(lead, 'releaseReason', ''))
      age = float(_field(lead, 'observationAge', math.inf))
      distance = float(_field(lead, 'observationDRel', -1.0) if _field(lead, 'observationShadow', False)
                       else _field(lead, 'dRel', math.inf))
      if reason in _RELEASE_REASONS or not math.isfinite(age) or age < 0.0:
        self._observed.pop(track_id, None)
        continue
      if state == 'measured' and bool(_field(lead, 'present', False)):
        if max(age, now - model_time) <= LEAD_GUARD_FRESH_AGE and 0.5 <= distance <= LEAD_GUARD_ENTER_DISTANCE:
          # Measured refers to this model message. The published observationAge
          # already includes radard processing delay; do not subtract it twice.
          self._observed[track_id] = model_time
        else:
          self._observed.pop(track_id, None)
      elif state in ('predicted', 'uncertain') and track_id in self._observed:
        # Use both independently advancing clocks; neither may renew the budget.
        elapsed = max(age, now - self._observed[track_id])
        if elapsed <= LEAD_GUARD_MAX_AGE:
          eligible.append(LeadUncertaintyDecision(True, 0.0, 'leadUncertain', elapsed, track_id))
        else:
          self._observed.pop(track_id, None)
    self._observed = {key: t for key, t in self._observed.items()
                      if key in visible_ids and now - t <= LEAD_GUARD_MAX_AGE}
    self._decision = max(eligible, key=lambda decision: decision.age) if eligible else LeadUncertaintyDecision()
    return self._decision
