"""Legacy acceleration boost: the driver's own gas pedal.

This is the fallback the longitudinal planner uses when either the predictive
`AccelBoostSP` (openpilot/sunnypilot/selfdrive/controls/lib/accel_boost.py) is
switched off or the car is not running openpilot longitudinal control. It predates
the predictive version and does something much narrower: while the driver holds the
gas and the model's own plan is what is limiting the car, it lets the car ask for a
little more than the model would.

The predictive version asks nothing of the driver -- it boosts when the model does
not predict braking. Keeping both means switching `AccelBoostSP` off returns the car
to the old gas-pedal behaviour instead of turning boost off entirely, which is what
upstream sunnypilot does.
"""
import numpy as np
from openpilot.common.constants import CV
from openpilot.common.realtime import DT_MDL

ACCEL_BOOST_MAX = 0.2
ACCEL_BOOST_RATE = 0.025
ACCEL_BOOST_PER_OVERRIDE = 0.05
ACCEL_BOOST_MIN_SPEED = 10 * CV.MPH_TO_MS


class AccelBoost:
  def __init__(self):
    self.total_boost = 0.0
    self.boost_this_override = 0.0
    self.boost_eligible = False

  def update(self, sm, output_a_target_e2e, output_a_target_mpc, a_cruise):
    enabled = sm['selfdriveState'].enabled
    gas_pressed = sm['carState'].gasPressed
    v_ego = sm['carState'].vEgo
    speed_scale = np.interp(v_ego, [ACCEL_BOOST_MIN_SPEED, 2 * ACCEL_BOOST_MIN_SPEED], [0.0, 1.0])
    boost_scale = speed_scale * np.interp(output_a_target_e2e, [-1.0, -0.5, 5.0], [0.0, 1.0, 1.0], right=0.0)
    model_limited = output_a_target_e2e + self.total_boost * boost_scale < min(output_a_target_mpc, a_cruise) - 0.1

    if not enabled or not gas_pressed:
      self.boost_this_override = 0.0
      # Latch eligibility before the override changes the lead plan.
      self.boost_eligible = enabled and model_limited

    if not enabled:
      self.total_boost = 0.0
    elif gas_pressed and self.boost_eligible:
      increase = min(ACCEL_BOOST_RATE * DT_MDL * speed_scale,
                     ACCEL_BOOST_PER_OVERRIDE - self.boost_this_override, ACCEL_BOOST_MAX - self.total_boost)
      self.total_boost += increase
      self.boost_this_override += increase

    return output_a_target_e2e + self.total_boost * boost_scale
