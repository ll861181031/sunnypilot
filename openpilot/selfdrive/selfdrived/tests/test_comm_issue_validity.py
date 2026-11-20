"""commIssue must not fire twice for one hiccup.

calibrationd, paramsd and plannerd publish their message-level valid flag from their own
inputs' health, not from the quality of what they produce:

  calibrationd: msg.valid = sm.all_checks()              # cameraOdometry, carState
  paramsd:      learner.get_msg(sm.all_checks())         # deviceMotion, extrinsicsCalibration, carState
  plannerd:     plan_send.valid = sm.all_checks()        # longitudinal_planner.py:162

selfdrived runs all_checks() over its SubMaster, and any failure there becomes
commIssue, which is SOFT_DISABLE + NO_ENTRY. Without an exclusion the same hiccup is
counted twice - once on the message that had it, and again on everything downstream -
and costs a disengagement that no dedicated alert asked for.

Measured on route 00000046: 36 commIssue events, and 36/36 of them fall within 300 ms
of an invalid vehicleParameters, extrinsicsCalibration or cameraOdometry frame (199
such frames). The condition behind them is already reported by locationdTemporaryError,
which carries the same NO_ENTRY + SOFT_DISABLE flags.

longitudinalPlan is the same cascade one level down, measured on 2026-10-17: an invalid
liveLocationKalman made vehicleParameters invalid, paramsd published that, plannerd
republished it as an invalid longitudinalPlan, and selfdrived then counted it a third
time as commIssue and refused to engage at all.

The exclusion is for validity only. Liveness and frequency must still be enforced, or
a publisher that dies or stalls would go unnoticed.
"""

import unittest

from openpilot.selfdrive.selfdrived.events import EVENTS, ET, EventName
from openpilot.selfdrive.selfdrived.selfdrived import VALIDITY_MIRROR_MESSAGES


class TestValidityMirrorMessages(unittest.TestCase):

  def test_only_the_mirrored_messages_are_excluded(self):
    self.assertEqual(set(VALIDITY_MIRROR_MESSAGES),
                     {'extrinsicsCalibration', 'vehicleParameters', 'longitudinalPlan'})

  def test_the_plan_flag_is_excluded_as_a_mirror_not_as_a_metric(self):
    # plannerd sets plan_send.valid = sm.all_checks() (longitudinal_planner.py:162), so an
    # invalid longitudinalPlan says nothing about the plan itself. Measured 2026-10-17:
    # an invalid liveLocationKalman made vehicleParameters invalid, which plannerd
    # republished as an invalid longitudinalPlan, which selfdrived counted as commIssue
    # and refused to engage on.
    self.assertIn('longitudinalPlan', VALIDITY_MIRROR_MESSAGES)

  def test_messages_that_report_their_own_quality_are_not_excluded(self):
    """carState and deviceMotion publish real state; a bad reading there matters."""
    for name in ('carState', 'deviceMotion', 'modelV2', 'controllerState', 'pandaStates'):
      with self.subTest(name=name):
        self.assertNotIn(name, VALIDITY_MIRROR_MESSAGES)

  def test_calibration_invalid_still_has_its_own_alert(self):
    """calStatus is the real calibration state and is read separately."""
    self.assertIn(EventName.calibrationInvalid, EVENTS)

  def test_the_cascade_condition_is_still_reported_elsewhere(self):
    """The underlying locationd problem keeps a dedicated NO_ENTRY + SOFT_DISABLE alert."""
    e = EVENTS[EventName.locationdTemporaryError]
    self.assertIn(ET.NO_ENTRY, e)
    self.assertIn(ET.SOFT_DISABLE, e)

  def test_comm_issue_is_still_defined(self):
    """The exclusion narrows what raises commIssue; it does not remove the alert."""
    self.assertIn(EventName.commIssue, EVENTS)


if __name__ == "__main__":
  unittest.main()
