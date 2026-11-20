"""Calibration alerts must say which way to aim the camera and how far.

The alerts previously only showed the current angle ('Remount Device (Pitch:
6.7°, Yaw: -4.2°)') or 'Drive Above 40 km/h', which leaves the driver to guess
the direction. They now state the correction to apply.
"""

import math

import pytest

from openpilot.common.test import OpenpilotTestCase
from openpilot.cereal import log, messaging
from openpilot.common.camera_mount import MOUNT_ADJUST_MIN_DEG, aim_offset_px, camera_mount_adjustment
from openpilot.selfdrive.locationd.calibrationd import mount_adjustment
from openpilot.selfdrive.selfdrived.events import (
  calibration_incomplete_alert,
  calibration_invalid_alert,
  calibration_invalid_no_entry,
  calibration_invalid_soft_disable,
  mount_needs_adjustment,
)

Status = log.ExtrinsicsCalibration.Status


class FakeSM:
  """Just enough of a SubMaster for the calibration alerts."""

  def __init__(self, rpy, cal_status=Status.invalid, cal_perc=100.0):
    msg = messaging.new_message('extrinsicsCalibration')
    msg.extrinsicsCalibration.rpyCalib = list(rpy)
    msg.extrinsicsCalibration.calStatus = cal_status
    msg.extrinsicsCalibration.calPerc = cal_perc
    self.ec = msg.extrinsicsCalibration

  def __getitem__(self, key):
    assert key == 'extrinsicsCalibration'
    return self.ec


def deg(value: float) -> float:
  return math.radians(value)


class TestCameraMountAdjustment(OpenpilotTestCase):
  def test_direction_is_opposite_to_where_the_camera_points(self):
    # Positive yaw means the camera points left, so the fix is to turn it right.
    assert camera_mount_adjustment(deg(4.2), 0.0) == "Turn camera right 4.2°"
    assert camera_mount_adjustment(deg(-4.2), 0.0) == "Turn camera left 4.2°"
    # Positive pitch correction means the camera has to be tilted up.
    assert camera_mount_adjustment(0.0, deg(6.7)) == "tilt up 6.7°"
    assert camera_mount_adjustment(0.0, deg(-3.0)) == "tilt down 3.0°"

  def test_both_axes_are_reported_together(self):
    assert camera_mount_adjustment(deg(-4.16), deg(6.72)) == "Turn camera left 4.2°, tilt up 6.7°"

  def test_straight_ahead_needs_no_adjustment(self):
    assert camera_mount_adjustment(0.0, 0.0) == ""
    assert camera_mount_adjustment(deg(MOUNT_ADJUST_MIN_DEG / 2), deg(-MOUNT_ADJUST_MIN_DEG / 2)) == ""

  def test_mount_inside_the_window_needs_nothing(self):
    # The point of the guide: a mount calibrationd already accepts must not be
    # reported as an error, it only needs driving time.
    inside = [0.0, deg(6.9), deg(-3.2)]
    assert mount_adjustment(inside) == (0.0, 0.0)
    assert not mount_needs_adjustment(inside)

  def test_mount_outside_the_window_reports_the_remaining_correction(self):
    yaw, pitch = mount_adjustment([0.0, deg(11.0), deg(-4.5)])
    assert yaw == pytest.approx(deg(-4.5 + 3.96 - 0.3), abs=deg(0.02))
    assert pitch == pytest.approx(deg(11.0 - 9.74 + 0.3), abs=deg(0.02))
    assert mount_needs_adjustment([0.0, deg(11.0), deg(-4.5)])


class TestCalibrationAlertText(OpenpilotTestCase):
  def alert_args(self, sm):
    return (None, None, sm, False, 0, None)

  def test_invalid_alert_states_the_correction(self):
    alert = calibration_invalid_alert(*self.alert_args(FakeSM([0.0, deg(6.72), deg(-4.16)])))
    assert alert.alert_text_1 == "Calibration Invalid"
    # Only the yaw is outside the window; pitch is accepted as mounted.
    assert alert.alert_text_2 == "Turn camera left 0.5°"

  def test_invalid_alert_falls_back_to_raw_angles(self):
    # Inside the window but reported invalid: no correction to give, so keep the angles.
    alert = calibration_invalid_alert(*self.alert_args(FakeSM([0.0, 0.0, 0.0])))
    assert alert.alert_text_1 == "Calibration Invalid"
    assert alert.alert_text_2 == "Remount Device (Pitch: 0.0°, Yaw: 0.0°)"

  def test_incomplete_alert_keeps_the_drive_hint_when_the_mount_is_usable(self):
    alert = calibration_incomplete_alert(*self.alert_args(FakeSM([0.0, 0.0, 0.0], Status.uncalibrated, 68.0)))
    assert alert.alert_text_1 == "Calibrating: 68%"
    assert alert.alert_text_2.startswith("Drive Above")

  def test_incomplete_alert_keeps_driving_when_mount_is_acceptable(self):
    alert = calibration_incomplete_alert(*self.alert_args(FakeSM([0.0, deg(6.9), deg(-3.2)], Status.uncalibrated, 60.0)))
    assert alert.alert_text_1 == "Calibrating: 60%"
    assert alert.alert_text_2.startswith("Drive Above")

  def test_incomplete_alert_points_out_an_out_of_range_mount(self):
    # Out-of-range yaw: the alert reports the correction for every axis that is
    # off-centre, so one adjustment session can fix the whole mount.
    alert = calibration_incomplete_alert(*self.alert_args(FakeSM([0.0, deg(1.0), deg(-4.16)], Status.uncalibrated, 68.0)))
    assert alert.alert_text_1 == "Calibrating: 68%"
    assert alert.alert_text_2 == "Turn camera left 0.5°"

  def test_recalibrating_alert_uses_the_same_wording(self):
    alert = calibration_incomplete_alert(*self.alert_args(FakeSM([0.0, deg(1.0), deg(-5.0)], Status.recalibrating, 20.0)))
    assert alert.alert_text_1 == "Recalibrating: 20%"
    assert alert.alert_text_2 == "Turn camera left 1.3°"

  def test_blocking_alerts_also_carry_the_correction(self):
    # These are what the driver sees when openpilot refuses to engage.
    sm = FakeSM([0.0, deg(6.72), deg(-4.16)])
    for callback in (calibration_invalid_soft_disable, calibration_invalid_no_entry):
      alert = callback(*self.alert_args(sm))
      shown = f"{alert.alert_text_1} {alert.alert_text_2}"
      assert "Turn camera left 0.5°" in shown, shown

  def test_blocking_alerts_fall_back_when_angles_are_usable(self):
    sm = FakeSM([0.0, 0.0, 0.0])
    alert = calibration_invalid_no_entry(*self.alert_args(sm))
    assert "Remount Device & Recalibrate" in f"{alert.alert_text_1} {alert.alert_text_2}"

  def test_aim_offset_moves_the_way_the_camera_must_be_turned(self):
    focal = 1000.0
    # Pointing left (positive yaw) puts the road ahead right of centre, so the
    # marker sits to the right and the driver turns right to centre it.
    dx, dy = aim_offset_px(deg(4.0), 0.0, focal)
    assert dx > 0 and dy == 0
    # Pointing right puts it left of centre.
    dx, _ = aim_offset_px(deg(-4.0), 0.0, focal)
    assert dx < 0
    # Pointing down (positive pitch) puts it above centre.
    dx, dy = aim_offset_px(0.0, deg(6.0), focal)
    assert dx == 0 and dy < 0
    _, dy = aim_offset_px(0.0, deg(-6.0), focal)
    assert dy > 0

  def test_aim_offset_is_zero_when_pointing_straight_ahead(self):
    assert aim_offset_px(0.0, 0.0, 1000.0) == (0.0, 0.0)

  def test_aim_offset_scales_with_focal_length(self):
    small = aim_offset_px(deg(4.0), 0.0, 500.0)[0]
    large = aim_offset_px(deg(4.0), 0.0, 1000.0)[0]
    assert large == pytest.approx(2 * small)
