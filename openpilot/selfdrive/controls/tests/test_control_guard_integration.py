import time

import numpy as np
import pytest

from opendbc.car import structs
from opendbc.car.byd.lateral_diagnostics import Limit
from openpilot.cereal import messaging
from openpilot.selfdrive.controls.lib.longitudinal_planner import LongitudinalPlanner
from openpilot.selfdrive.controls.tests.test_light_stop_evidence import nav
from openpilot.sunnypilot.selfdrive.controls.lib.longitudinal_planner import LongitudinalPlannerSP


class Samples(dict):
  def __init__(self, now):
    names = ['carState', 'carStateSP', 'carControl', 'controlsState', 'selfdriveState',
             'vehicleParameters', 'modelV2', 'radarState', 'yoloObjects']
    super().__init__({name: getattr(messaging.new_message(name), name) for name in names})
    self.seen = dict.fromkeys(names, True)
    self.valid = dict.fromkeys(names, True)
    self.recv_time = dict.fromkeys(names, now)
    self.logMonoTime = dict.fromkeys(names, int(now * 1e9))
    self['carState'].vEgo = 5.0
    self['carState'].vCruise = 40.0
    self['carControl'].enabled = True
    self['carControl'].longActive = True
    self['carControl'].latActive = True
    self['controlsState'].longControlState = 'pid'
    self['selfdriveState'].enabled = True
    diag = self['carStateSP'].bydLateral
    diag.valid = diag.requestedActive = diag.active = True
    diag.desiredAngleDeg = 60.0
    diag.measuredAngleDeg = 20.0
    diag.finalAngleDeg = 25.0
    diag.leadBoundDeg = 5.0
    diag.limitMask = int(Limit.LEAD)

  def advance(self, now):
    self.recv_time = dict.fromkeys(self.recv_time, now)
    self.logMonoTime = dict.fromkeys(self.logMonoTime, int(now * 1e9))
    diag = self['carStateSP'].bydLateral
    diag.sampleMonoTime = int(now * 1e9)
    diag.lastSubmittedMonoTime = int((now - .01) * 1e9)
    self['radarState'].mdMonoTime = int(now * 1e9)


@pytest.fixture
def rig(monkeypatch):
  # Isolate settings/network consumers while retaining the real planner update,
  # actual MPC, guard state machines and candidate merge.
  monkeypatch.setattr(LongitudinalPlannerSP, '__init__', lambda *a, **kw: None)
  monkeypatch.setattr(LongitudinalPlannerSP, 'update', lambda *a, **kw: None)
  monkeypatch.setattr(LongitudinalPlannerSP, 'update_targets', lambda self, sm, v, a, cruise: (cruise, a))
  monkeypatch.setattr(LongitudinalPlanner, '_refresh_switches', lambda self: None)
  monkeypatch.setattr(LongitudinalPlanner, 'is_e2e', lambda self, sm: True)
  cp = structs.CarParams(openpilotLongitudinalControl=True, longitudinalActuatorDelay=.3,
                         steerRatio=15.5, wheelbase=2.7)
  planner = LongitudinalPlanner(cp, structs.CarParamsSP(), init_v=5.0)
  clock = [time.monotonic()]
  monkeypatch.setattr('openpilot.selfdrive.controls.lib.longitudinal_planner.time.monotonic', lambda: clock[0])
  samples = Samples(clock[0])
  samples['modelV2'].action.desiredAcceleration = 1.0
  return planner, samples, clock


def advance(rig, frames=10):
  planner, samples, clock = rig
  for _ in range(frames):
    clock[0] += .05
    samples.advance(clock[0])
    planner.update(samples)


def test_turn_shadow_records_pressure_without_changing_actual_acceleration(rig):
  planner, _, _ = rig
  advance(rig)
  assert planner.turn_guard_result.active
  assert np.isinf(planner.turn_guard_result.accel_max)
  assert planner.output_a_target > 0


def test_enforced_turn_guard_caps_acceleration_but_preserves_stronger_braking(rig):
  planner, samples, _ = rig
  planner.turn_guard.enforce = True
  advance(rig)
  assert planner.output_a_target == 0.0
  samples['modelV2'].action.desiredAcceleration = -2.0
  samples['modelV2'].action.shouldStop = True
  advance(rig, 1)
  assert planner.output_a_target == pytest.approx(-2.0)
  assert planner.output_should_stop


@pytest.mark.parametrize('reason', ['stale', 'override', 'disengaged'])
def test_turn_guard_is_removed_for_invalid_control_context(rig, reason):
  planner, samples, clock = rig
  planner.turn_guard.enforce = True
  advance(rig)
  samples.advance(clock[0] + .05)
  clock[0] += .05
  if reason == 'stale':
    samples.recv_time['carStateSP'] = clock[0] - 1
  elif reason == 'override':
    samples['carState'].gasPressed = True
  else:
    samples['carControl'].longActive = False
  planner.update(samples)
  assert not planner.turn_guard_result.active


def test_disengaged_planner_cannot_commit_a_red_light(rig):
  planner, samples, _ = rig
  planner.light_stop = True
  samples['yoloObjects'].egoLightState = 'red'
  samples['yoloObjects'].egoLightFrames = 6
  samples['yoloObjects'].egoLightDistance = 15
  advance(rig, 1)
  assert planner.light_stop_committed
  samples['carControl'].longActive = False
  advance(rig, 1)
  assert not planner.light_stop_committed
  assert planner.light_stop_reason == 'control_inactive_or_override'


def test_guard_diagnostics_distinguish_shadow_from_enforcement(rig):
  planner, _, _ = rig
  advance(rig)
  plan = messaging.new_message('longitudinalPlanSP').longitudinalPlanSP
  planner.populate_control_evidence(plan)
  assert plan.turnGuardActive
  assert not plan.turnGuardEnforced
  assert plan.turnGuardAccelMax == 0
  assert plan.turnGuardReason.startswith('pressure:')
  planner.turn_guard.enforce = True
  advance(rig, 1)
  planner.populate_control_evidence(plan)
  assert plan.turnGuardEnforced


def test_lead_shadow_and_enforced_merge_use_real_planner(rig):
  planner, samples, _ = rig
  lead = samples['radarState'].leadOne
  lead.present = True
  lead.dRel = 30
  lead.vLead = lead.vLeadK = 15
  lead.aLeadTau = 1.5
  lead.visionTrackId = 1
  lead.observationState = 'measured'
  advance(rig, 1)
  lead.present = False
  lead.observationState = 'uncertain'
  lead.observationAge = .6
  advance(rig, 1)
  assert planner.lead_guard_result.active
  assert planner.output_a_target > 0
  planner.lead_guard_enforce = True
  advance(rig, 1)
  assert planner.output_a_target == 0
  samples['modelV2'].action.desiredAcceleration = -2
  advance(rig, 1)
  assert planner.output_a_target == pytest.approx(-2)


def test_expired_radar_does_not_rearm_guard_on_duplicate_frame(rig):
  planner, samples, clock = rig
  lead = samples['radarState'].leadOne
  lead.present = True
  lead.dRel = 30
  lead.vLead = lead.vLeadK = 15
  lead.aLeadTau = 1.5
  lead.visionTrackId = 1
  lead.observationState = 'measured'
  advance(rig, 1)
  clock[0] += 1
  planner.update(samples)
  assert not planner.lead_guard_result.active
  lead.present = False
  lead.observationState = 'uncertain'
  lead.observationAge = .1
  advance(rig, 1)
  assert not planner.lead_guard_result.active


def test_unassociated_navigation_never_removes_existing_model_braking(rig):
  planner, samples, _ = rig
  planner.light_stop = planner.e2e_stop_guard = True
  samples['modelV2'].velocity.x = [5.0, 0.0]
  samples['modelV2'].orientation.z = [0.0, 0.0]
  samples['modelV2'].action.desiredAcceleration = -2.0
  samples['modelV2'].action.shouldStop = False
  samples['yoloObjects'] = nav(relevant=False)
  advance(rig, 1)
  assert not planner.light_stop_committed
  assert planner.light_stop_state == 'slow_only'
  assert planner.output_a_target == pytest.approx(-2.0)


def test_plain_mapping_simulation_keeps_existing_planner_contract(rig):
  planner, samples, _ = rig
  planner.update(dict(samples))
  assert np.isfinite(planner.output_a_target)
  assert not planner.turn_guard_result.active
  assert not planner.lead_guard_result.active
