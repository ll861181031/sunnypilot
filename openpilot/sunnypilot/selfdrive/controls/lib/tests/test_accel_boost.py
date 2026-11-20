"""Predictive acceleration boost (ported from upstream sunnypilot PR #2089).

The point of the feature is the gate, not the arithmetic: it adds acceleration while
the model predicts no braking, and gets out of the way in curves, when the model wants
to stop, and whenever it is already asking to slow down. These tests pin the gate
edges, because a boost that leaks into a braking situation is the failure mode that
matters.

The swap between AccelBoostSP and the legacy gas-pedal AccelBoost is covered too: on a
car without openpilot longitudinal control the predictive version cannot do anything,
so silently keeping it would leave a switch that looks on and reads zero forever.
"""
import tempfile

import pytest

from openpilot.common.parameterized import parameterized
from openpilot.common.params import Params
from openpilot.common.realtime import DT_MDL
from openpilot.cereal import log
from openpilot.selfdrive.controls.lib.accel_boost import AccelBoost
from openpilot.sunnypilot import PARAMS_UPDATE_PERIOD
from openpilot.sunnypilot.selfdrive.controls.lib.accel_boost import ACCEL_BOOST_MAX, AccelBoostSP
from openpilot.sunnypilot.selfdrive.controls.lib.longitudinal_planner import LongitudinalPlannerSP


class _Ns:
  """Attribute bag that also behaves like a SubMaster for the keys we read."""

  def __init__(self, **kw):
    self.__dict__.update(kw)


def make_sm(brake_prob=0.05, gas_prob=0.3, lat_accel=0.0, v_ego=30.0, personality=0, enabled=True, should_stop=False):
  meta = _Ns(disengagePredictions=_Ns(brakePressProbs=[0.0, brake_prob, 0.0, 0.0, 0.0, 0.0],
                                      gasPressProbs=[0.0, gas_prob, 0.0, 0.0, 0.0, 0.0]))
  action = _Ns(shouldStop=should_stop, desiredCurvature=lat_accel / v_ego ** 2)
  return {'modelV2': _Ns(meta=meta, action=action), 'carState': _Ns(vEgo=v_ego),
          'selfdriveState': _Ns(enabled=enabled, personality=personality)}


def settle(sm, frames=100):
  boost = AccelBoostSP()
  for _ in range(frames):
    boost.update(sm, 0.0, 9.0, 9.0)
  return boost.total_boost


class TestTheGate:
  @parameterized.expand([(0,), (1,), (2,)])
  def test_personality_caps_the_boost(self, personality):
    assert abs(settle(make_sm(personality=personality)) - ACCEL_BOOST_MAX[personality]) < 1e-6

  def test_a_predicted_brake_press_closes_it(self):
    assert settle(make_sm(brake_prob=0.5)) == 0.0

  def test_a_predicted_gas_press_reopens_it(self):
    assert abs(settle(make_sm(brake_prob=0.5, gas_prob=0.9)) - ACCEL_BOOST_MAX[0]) < 1e-6

  def test_should_stop_closes_it(self):
    assert settle(make_sm(gas_prob=0.9, should_stop=True)) == 0.0

  @parameterized.expand([(1.5, 1.0), (2.0, 0.5), (2.5, 0.0), (-2.0, 0.5)])
  def test_curves_fade_it_out(self, lat_accel, scale):
    """Gated on |kappa| * v^2, so the same curvature fades faster at speed."""
    assert abs(settle(make_sm(lat_accel=lat_accel)) - scale * ACCEL_BOOST_MAX[0]) < 1e-6

  def test_below_min_speed_it_is_zero(self):
    assert settle(make_sm(v_ego=0.5)) == 0.0

  def test_disengaged_resets_it(self):
    boost = AccelBoostSP()
    for _ in range(100):
      boost.update(make_sm(), 0.0, 9.0, 9.0)
    assert boost.update(make_sm(enabled=False), 0.0, 9.0, 9.0) == 0.0
    assert boost.boost == 0.0 and boost.total_boost == 0.0

  def test_missing_predictions_are_a_no_op(self):
    """Old or partial model output must not be read as "no braking predicted"."""
    boost = AccelBoostSP()
    sm = make_sm()
    sm['modelV2'].meta.disengagePredictions.brakePressProbs = []
    assert boost.update(sm, 0.0, 9.0, 9.0) == 0.0
    assert boost.total_boost == 0.0

  def test_it_never_touches_the_return_value_when_gated_off(self):
    boost = AccelBoostSP()
    for _ in range(100):
      boost.update(make_sm(), 0.0, 9.0, 9.0)
    assert boost.update(make_sm(brake_prob=0.5), -1.75, 9.0, 9.0) == pytest.approx(-1.75)


class TestRates:
  def test_builds_at_the_documented_rate(self):
    boost = AccelBoostSP()
    boost.update(make_sm(), 0.0, 9.0, 9.0)
    assert abs(boost.total_boost - 0.01) < 1e-6

  def test_releases_faster_than_it_builds(self):
    boost = AccelBoostSP()
    for _ in range(100):
      boost.update(make_sm(), 0.0, 9.0, 9.0)
    boost.update(make_sm(brake_prob=0.5), 0.0, 9.0, 9.0)
    assert abs(boost.total_boost - (ACCEL_BOOST_MAX[0] - 0.05)) < 1e-6

  def test_braking_aim_masks_the_boost_but_keeps_it_accumulated(self):
    """The e2e accel gate multiplies the output, not the internal ramp."""
    boost = AccelBoostSP()
    for _ in range(100):
      boost.update(make_sm(), 0.0, 9.0, 9.0)
    boost.update(make_sm(), 0.0, 9.0, 9.0)
    assert boost.total_boost > 0.2
    # model now wants -1.0 m/s^2: nothing is added on top
    braking = boost.update(make_sm(), -1.0, 9.0, 9.0)
    assert braking == pytest.approx(-1.0)
    assert boost.total_boost == 0.0
    assert boost.boost > 0.2, "the internal ramp must survive a braking phase"


class TestTheParameter:
  def test_declared_default_and_unset_read(self):
    """The key is declared default-on, but an unset param reads False.

    That is this store's normal behaviour (defaults only show through
    get(return_default=True)), and it matches upstream: the switch starts off and the
    driver opts in. Pinned so a future "make it default on" change is deliberate.
    """
    with tempfile.TemporaryDirectory() as d:
      p = Params(d)
      assert "AccelBoostSP" in [k.decode() if isinstance(k, bytes) else k for k in p.all_keys()]
      assert p.get_default_value("AccelBoostSP") is True
      assert p.get_bool("AccelBoostSP") is False

  def test_the_switch_is_read_on_a_timer_not_every_frame(self):
    with tempfile.TemporaryDirectory() as d:
      params = Params(d)
      params.put_bool("AccelBoostSP", True, block=True)
      boost = AccelBoostSP()
      boost.params = params
      assert boost.update_params() and boost.enabled
      for _ in range(int(PARAMS_UPDATE_PERIOD / DT_MDL) - 1):
        assert not boost.update_params()
      params.put_bool("AccelBoostSP", False, block=True)
      assert boost.update_params() and not boost.enabled
      assert boost.boost == 0.0 and boost.total_boost == 0.0


class TestThePlannerSwapsImplementation:
  @staticmethod
  def _planner(enabled: bool, openpilot_longitudinal: bool):
    planner = object.__new__(LongitudinalPlannerSP)
    planner.accel_boost_sp = AccelBoostSP()
    planner.accel_boost_sp.enabled = enabled
    planner.openpilot_longitudinal = openpilot_longitudinal
    planner.accel_boost = AccelBoost()
    planner._swap_accel_boost()
    return planner

  def test_enabled_with_openpilot_long_uses_the_predictive_one(self):
    planner = self._planner(enabled=True, openpilot_longitudinal=True)
    assert isinstance(planner.accel_boost, AccelBoostSP)

  def test_disabled_falls_back_to_the_gas_pedal_one(self):
    planner = self._planner(enabled=False, openpilot_longitudinal=True)
    assert type(planner.accel_boost) is AccelBoost

  def test_stock_longitudinal_falls_back_too(self):
    """Otherwise the switch would look on and read zero forever on a stock car."""
    planner = self._planner(enabled=True, openpilot_longitudinal=False)
    assert type(planner.accel_boost) is AccelBoost

  def test_the_swap_resets_fallback_state_but_not_the_predictive_one(self):
    planner = self._planner(enabled=True, openpilot_longitudinal=True)
    kept = planner.accel_boost
    planner._swap_accel_boost()
    assert planner.accel_boost is kept, "re-deciding must not throw away accumulated boost"


class TestThePlanPublishesIt:
  def test_the_field_exists_and_round_trips(self):
    from openpilot.cereal import messaging
    plan = messaging.new_message('longitudinalPlan')
    assert plan.longitudinalPlan.accelBoost == 0.0
    plan.longitudinalPlan.accelBoost = 0.25
    with log.Event.from_bytes(plan.to_bytes()) as back:
      assert back.longitudinalPlan.accelBoost == pytest.approx(0.25)

  def test_a_message_without_the_field_reads_zero(self):
    """Messages recorded before this change must not decode as a phantom boost."""
    from openpilot.cereal import messaging
    plan = messaging.new_message('longitudinalPlan')
    plan.longitudinalPlan.aTarget = -1.0
    with log.Event.from_bytes(plan.to_bytes()) as back:
      assert back.longitudinalPlan.accelBoost == 0.0
      assert back.longitudinalPlan.aTarget == pytest.approx(-1.0)

  def test_publish_writes_it(self):
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[6] / "openpilot/selfdrive/controls/lib/longitudinal_planner.py"
    assert "longitudinalPlan.accelBoost = float(self.accel_boost.total_boost)" in src.read_text()


class TestTheSettingsPageOffersIt:
  @staticmethod
  def _src():
    import pathlib
    return (pathlib.Path(__file__).resolve().parents[6] / "openpilot/selfdrive/ui/sunnypilot/layouts/settings/cruise.py").read_text()

  def test_the_toggle_is_bound_to_the_key(self):
    assert 'param="AccelBoostSP"' in self._src()

  def test_it_is_disabled_without_openpilot_longitudinal(self):
    src = self._src()
    assert "self.accel_boost_toggle.action_item.set_enabled(has_long)" in src

  def test_the_toggle_is_reachable(self):
    src = self._src()
    items = src.split("items = [", 1)[1].split("]", 1)[0]
    assert "self.accel_boost_toggle" in items
