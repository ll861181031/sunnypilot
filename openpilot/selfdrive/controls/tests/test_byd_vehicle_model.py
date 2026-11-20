import math

import pytest

from opendbc.car import structs
from opendbc.car.byd.interface import CarInterface
from opendbc.car.byd.values import CAR
from opendbc.car.vehicle_model import VehicleModel
from openpilot.cereal import messaging
from openpilot.common.realtime import DT_CTRL
from openpilot.selfdrive.controls.controlsd import Controls
from openpilot.selfdrive.controls.lib.latcontrol_angle import LatControlAngle
from openpilot.selfdrive.controls.lib.longcontrol import LongControl
from openpilot.sunnypilot.selfdrive.controls.lib.blinker_pause_lateral import BlinkerPauseLateral


class OfflineMessages(dict):
  def __init__(self):
    services = ('carState', 'vehicleParameters', 'selfdriveState', 'selfdriveStateSP', 'modelV2',
                'longitudinalPlan', 'lateralDelay', 'lateralManeuverPlan', 'carOutput', 'driverMonitoringState')
    super().__init__((s, getattr(messaging.new_message(s), s)) for s in services)
    self['onroadEvents'] = []
    self.valid = dict.fromkeys(services, True)
    self.valid.update(lateralManeuverPlan=False, driverAssistance=False)
    self.logMonoTime = dict.fromkeys(services, 0)


class OfflinePublisher:
  def __init__(self):
    self.messages = {}

  def send(self, service, message):
    self.messages[service] = message.to_dict()[service]


def make_controls(brand='byd', controls_class=Controls):
  # Bypass only process startup: no Params, messaging sockets, or live controls.
  controls = controls_class.__new__(controls_class)
  controls.CP = CarInterface.get_non_essential_params(CAR.BYD_SONG_PLUS_DMI_23_ALT)
  controls.CP.brand = brand
  controls.CP.openpilotLongitudinalControl = False
  controls.CP_SP = structs.CarParamsSP(pcmCruiseSpeed=True)
  controls.CI = CarInterface
  controls.sm = OfflineMessages()
  controls.pm = OfflinePublisher()
  controls.blinker_pause_lateral = BlinkerPauseLateral.__new__(BlinkerPauseLateral)
  controls.blinker_pause_lateral.enabled = False
  controls.LoC = LongControl(controls.CP, controls.CP_SP)
  controls.LaC = LatControlAngle(controls.CP, controls.CP_SP, controls.CI, DT_CTRL)
  controls.VM = VehicleModel(controls.CP)
  controls.steer_limited_by_safety = False
  controls.curvature = controls.desired_curvature = 0.0
  controls.calibrated_pose = None
  controls.sm['selfdriveState'].enabled = controls.sm['selfdriveState'].active = True
  controls.sm['carState'].canValid = True
  controls.sm['carState'].vEgo = 5.0
  controls.sm['vehicleParameters'].steerRatio = 15.5
  controls.sm['vehicleParameters'].stiffnessFactor = 1.0
  return controls


@pytest.mark.parametrize('brand', ['byd', 'nissan'])
@pytest.mark.parametrize('speed,curvature,measured,offset', [
  (3.0, -0.04, 100.0, 1.2),
  (10.0, 0.01, -25.0, -0.6),
  (25.0, 0.002, -4.0, 0.0),
  (5.0, 0.0, 1.2, 1.2),
])
def test_control_and_published_curvature_use_physical_ratio(brand, speed, curvature, measured, offset):
  controls = make_controls(brand)
  # Symmetric axle loads and stiffness give zero slip factor: k = angle / (ratio * wheelbase).
  controls.CP.wheelbase = 2.8
  controls.CP.centerToFront = 1.4
  controls.CP.tireStiffnessFront = controls.CP.tireStiffnessRear = 80000.0
  controls.VM = VehicleModel(controls.CP)
  controls.sm['carState'].vEgo = speed
  controls.sm['carState'].steeringAngleDeg = measured
  controls.sm['vehicleParameters'].angleOffsetDeg = offset
  controls.sm['modelV2'].action.desiredCurvature = curvature
  controls.desired_curvature = curvature

  command, angle_log = controls.state_control()
  expected_angle = math.degrees(-curvature * 2.8 * 15.5) + offset
  expected_curvature = -math.radians(measured - offset) / (2.8 * 15.5)
  assert command.latActive
  assert command.actuators.steeringAngleDeg == pytest.approx(expected_angle, abs=1e-5)
  assert angle_log.steeringAngleDesiredDeg == pytest.approx(expected_angle, abs=1e-5)
  assert controls.curvature == pytest.approx(expected_curvature, abs=1e-8)

  controls.publish(command, angle_log)
  assert controls.pm.messages['carControl']['currentCurvature'] == pytest.approx(expected_curvature, abs=1e-8)
  assert controls.pm.messages['controlsState']['curvature'] == pytest.approx(expected_curvature, abs=1e-8)


# Original rlog inputs; yaw is decoded from bus 0, 0x222, with the CAN sign convention.
# 2026-11-10: 0000019d--a55a5e8b17--7, carState logMonoTime 9636.990252715 s.
# 2026-11-07: 000000f2--efb07443c5--14, carState logMonoTime 2054.999241867 s.
RECORDED_TURNS = [
  (3.2579212188720703, 184.39999389648438, 18.75368309020996, 0.9992758631706238,
   -0.4501098096370697, -0.005036214366555214, -0.0468953400850296, 0.2047298879999997),
  (3.1045339107513428, -236.6999969482422, 18.797183990478516, 1.0318502187728882,
   -0.7417422533035278, 0.020542966201901436, 0.07417463511228561, -0.25164715400000004),
]


@pytest.mark.parametrize('speed,measured,ratio,stiffness,offset,roll,curvature,yaw', RECORDED_TURNS,
                         ids=['continuous_turn', 'right_angle_turn'])
def test_recorded_oversteer_requests_unwind_and_matches_raw_yaw(speed, measured, ratio, stiffness, offset, roll, curvature, yaw):
  controls = make_controls()
  # Use the recorded CarParams, not today's vehicle defaults.
  for key, value in {'steerRatio': 19.0, 'wheelbase': 2.765000104904175, 'mass': 2136.0,
                     'rotationalInertia': 3830.510498046875, 'centerToFront': 1.3825000524520874,
                     'steerRatioRear': 0.0, 'tireStiffnessFront': 163761.21875, 'tireStiffnessRear': 258873.109375}.items():
    setattr(controls.CP, key, value)
  controls.VM = VehicleModel(controls.CP)
  controls.sm['carState'].vEgo = speed
  controls.sm['carState'].steeringAngleDeg = measured
  params = controls.sm['vehicleParameters']
  params.steerRatio, params.stiffnessFactor = ratio, stiffness
  params.angleOffsetDeg, params.roll = offset, roll
  controls.sm['modelV2'].action.desiredCurvature = curvature
  controls.desired_curvature = curvature

  command, _ = controls.state_control()
  # Both raw yaw observations exceed the requested curvature: adding lock is wrong.
  assert abs(-yaw / speed) > abs(curvature)
  assert abs(command.actuators.steeringAngleDeg - offset) < abs(measured - offset)
  assert controls.curvature == pytest.approx(-yaw / speed, abs=0.01)


@pytest.mark.parametrize('guard', ['inactive', 'steerFaultTemporary', 'steerFaultPermanent', 'standstill'])
def test_lateral_disable_keeps_measured_angle(guard):
  controls = make_controls()
  state = controls.sm['carState']
  state.steeringAngleDeg = -166.8
  controls.sm['modelV2'].action.desiredCurvature = -0.03
  if guard == 'inactive':
    controls.sm['selfdriveState'].active = False
  else:
    setattr(state, guard, True)

  command, angle_log = controls.state_control()
  assert not command.latActive
  assert not angle_log.active
  assert command.actuators.steeringAngleDeg == pytest.approx(state.steeringAngleDeg)
