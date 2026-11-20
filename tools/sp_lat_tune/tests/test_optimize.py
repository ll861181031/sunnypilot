"""Tests for the sweep optimiser.

The two properties worth pinning are that feasibility is a filter (never a weight)
and that a suggestion is always a pair that has not been driven yet.
"""

from __future__ import annotations

from tools.sp_lat_tune import optimize


class FakeStore:
  def __init__(self, trials: dict | None = None):
    self._trials = trials or {}

  def trials(self) -> dict:
    return self._trials


def _trial(lead=55, rate=24, *, under=1.2, severe=0.1, toofast=0, jerk=1.0, frames=1000):
  return {"lead_tenths": lead, "rate_tenths": rate, "frames_active": frames,
          "undershoot_aggregate": under, "severe_ratio": severe,
          "toofast_frames": toofast, "steer_fault_permanent_frames": 0, "jerk_p99": jerk}


class TestFeasibility:
  def test_clean_trial_passes(self):
    assert optimize.feasible(_trial())[0]

  def test_toofast_is_disqualifying_not_merely_worse(self):
    """The 6.0 deg experiment traded TooFast for tracking and was reverted."""
    ok, why = optimize.feasible(_trial(toofast=1))
    assert not ok and "TooFast" in why

  def test_jerk_above_the_limit_is_disqualifying(self):
    ok, why = optimize.feasible(_trial(jerk=optimize.JERK_LIMIT + 0.01))
    assert not ok and "jerk" in why

  def test_no_active_frames_is_not_a_result(self):
    ok, why = optimize.feasible(_trial(frames=0))
    assert not ok and "active" in why


class TestObjective:
  def test_lower_undershoot_wins(self):
    assert optimize.objective(_trial(under=1.05)) < optimize.objective(_trial(under=1.40))

  def test_severe_understeer_is_penalised_beyond_the_aggregate(self):
    mild = optimize.objective(_trial(under=1.2, severe=0.0))
    harsh = optimize.objective(_trial(under=1.2, severe=0.5))
    assert harsh > mild

  def test_missing_undershoot_falls_back_instead_of_producing_nan(self):
    value = optimize.objective(_trial(under=float("nan"), severe=float("nan")))
    assert value == value


class TestSuggest:
  def test_first_suggestions_are_space_filling_not_random(self):
    """The corners are what tells the surrogate which way the cost moves."""
    picks = optimize.suggest(FakeStore(), n=4)
    assert len(picks) == 4
    pairs = {(p["lead_tenths"], p["rate_tenths"]) for p in picks}
    assert (5, 5) in pairs and (5, 10) in pairs

  def test_one_observation_still_uses_the_design_not_a_fake_optimum(self):
    """A GP fitted to one point has no shape; picking its argmin is list order.

    This is the regression that would otherwise send every drive to the low corner
    and never touch the region the bottleneck analysis flagged.
    """
    picks = optimize.suggest(FakeStore({(15, 7): _trial(15, 7)}), n=3)
    leads = {p["lead_tenths"] for p in picks}
    assert len(leads) > 1, picks

  def test_suggestions_are_always_untried(self):
    trials = {(5, 5): _trial(5, 5), (15, 7): _trial(15, 7)}
    picks = optimize.suggest(FakeStore(trials), n=3)
    for p in picks:
      assert (p["lead_tenths"], p["rate_tenths"]) not in trials

  def test_suggestions_stay_inside_the_settings_page_ranges(self):
    picks = optimize.suggest(FakeStore(), n=5)
    for p in picks:
      assert optimize.LEAD_RANGE[0] <= p["lead_tenths"] <= optimize.LEAD_RANGE[1]
      assert optimize.RATE_RANGE[0] <= p["rate_tenths"] <= optimize.RATE_RANGE[1]

  def test_an_exhausted_space_returns_nothing(self):
    everything = {(l, r): _trial(l, r) for l in range(5, 35, 5) for r in range(5, 11)}
    assert optimize.suggest(FakeStore(everything), n=1) == []

  def test_the_opening_design_stays_out_of_the_steep_fault_bucket(self):
    """7 deg measured 16.33% of frames faulting; 4-6 deg measured 0.38-3.61%.

    The first drives are the ones with no data behind them, so they are the ones that
    must not start inside the steep part of the curve.
    """
    picks = optimize.suggest(FakeStore(), n=5)
    assert all(p["lead_tenths"] <= 34 for p in picks), picks


class TestBest:
  def test_no_feasible_trial_yields_none(self):
    assert optimize.best({(30, 10): _trial(30, 10, toofast=3)}) is None

  def test_the_lowest_objective_wins(self):
    trials = {(5, 10): _trial(5, 10, under=1.4),
              (15, 10): _trial(15, 10, under=1.1)}
    assert optimize.best(trials)["lead_tenths"] == 15


class TestRiskPrior:
  def test_prior_is_monotonic_in_lead(self):
    values = [optimize.lead_risk_prior(l) for l in (5, 10, 20, 30, 34)]
    assert values == sorted(values)

  def test_prior_is_flat_across_the_useful_range(self):
    # The page can only tighten the shipped table; there is no measured TooFast
    # knee inside the search range, so the prior stays flat.
    for lead in (5, 15, 25, 34):
      assert optimize.lead_risk_prior(lead) == 0.0
