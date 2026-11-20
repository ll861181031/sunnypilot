"""Glue between the lead-bound estimator and the car's 100 Hz loop.

`opendbc/car/byd/lead_learn.py` is a pure function of samples. This module owns the
three things it deliberately does not:

  * the 60 ms lookahead that attributes an EPS refusal to the frames that preceded
    it. The estimator sees one sample at a time and cannot look forward, so a small
    buffer here replays the window the corpus buckets use.
  * Params persistence: the curve under `BydLeadBoundTable` (apply mode) and the
    proposal under `BydLeadBoundShadow` (shadow mode), so a restart resumes from
    what was learnt instead of from scratch.
  * the mode switch.

Shadow mode is the default and it is not caution for its own sake. A one-pass
replay is off-policy - the recording carries whatever lead the car actually sent -
so a raise cannot be validated offline at all. Shadow mode collects the proposals
on real drives at zero risk; the proposals are then the thing to A/B against on
the next drive, which is the cheap half of the loop.
"""

from collections import deque

from opendbc.car.byd.lead_learn import LeadBoundLearner

MODE_OFF = 0
MODE_SHADOW = 1
MODE_APPLY = 2

# The settings page (and therefore the ops console, which renders the same table)
# reserves 0 for "unset" on every knob, so the stored value is 1-based. Keeping the
# two domains separate here is cheaper than making the page special-case one row.
#   0 unset -> shadow (the shipped behaviour)   1 -> off
#   2 shadow                                    3 -> apply
PARAM_MODES = {0: MODE_SHADOW, 1: MODE_OFF, 2: MODE_SHADOW, 3: MODE_APPLY}

# Each mode persists under its own key. Reading the wrong one is not a crash - the
# parser accepts either - it is worse: the learner silently restarts from whatever
# the other mode last wrote. In shadow mode that meant every restart began from the
# shipped curve and threw away the drive's proposal.
LEARN_TABLE_KEY = "BydLeadBoundTable"
LEARN_SHADOW_KEY = "BydLeadBoundShadow"


def table_key(mode: int) -> str:
  """The Params key the given mode owns."""
  return LEARN_TABLE_KEY if mode == MODE_APPLY else LEARN_SHADOW_KEY


def resume_keys(mode: int) -> tuple[str, str]:
  """Own key first, then the other one as a fallback for a mode switch."""
  own = table_key(mode)
  return own, LEARN_TABLE_KEY if own == LEARN_SHADOW_KEY else LEARN_SHADOW_KEY


def mode_from_param(raw) -> int:
  """Map the stored Param onto a mode. Unknown values fall back to shadow."""
  try:
    value = int(raw)
  except (TypeError, ValueError):
    return MODE_SHADOW
  return PARAM_MODES.get(value, MODE_SHADOW)


class LeadBoundLearning:
  """Drives the estimator from the control loop. Pure state, no Params access."""

  LOOKAHEAD_FRAMES = 6   # 60 ms at 100 Hz, the window the measured buckets use

  def __init__(self, bp, values, mode: int = MODE_SHADOW):
    self.mode = int(mode)
    self.learner = LeadBoundLearner(bp=bp, values=values, enabled=self.enabled)
    self.changes = 0
    self._frames: deque = deque()

  @property
  def enabled(self) -> bool:
    return self.mode != MODE_OFF

  def wants_apply(self) -> bool:
    return self.mode == MODE_APPLY

  def step(self, v_ego, at_bound: bool, starved: bool, too_fast: bool) -> bool:
    """One 100 Hz cycle. Returns True when the emitted curve moved."""
    if not self.enabled:
      return False

    self._frames.append((float(v_ego), bool(at_bound), bool(starved), bool(too_fast)))
    if len(self._frames) <= self.LOOKAHEAD_FRAMES:
      return False

    # The frame leaving the buffer is now 60 ms in the past; any refusal still in
    # the buffer happened inside its attribution window.
    v, at, st, _ = self._frames.popleft()
    fault = any(frame[3] for frame in self._frames)
    if self.learner.observe(v, at, st, fault):
      self.changes += 1
      return True
    return False

  def table_text(self) -> str:
    return self.learner.to_param()

  def table(self):
    return self.learner.table()

  def summary(self) -> dict:
    return {
      "mode": self.mode,
      "frames": self.learner.frames,
      "changes": self.changes,
      "table": self.table_text(),
    }
