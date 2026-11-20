import importlib
import json
from importlib.util import find_spec

import pytest

from openpilot.selfdrive.controls.lib import longcontrol

from openpilot.tools.car_porting.runtime_support import (
  PLAN_TARGET_HELP, plan_target_fields_present,
)

pytestmark = pytest.mark.skipif(
  not plan_target_fields_present(), reason=PLAN_TARGET_HELP)



def module():
  name = 'openpilot.tools.car_porting.byd_longcontrol_sim'
  assert find_spec(name) is not None, 'Missing hypothetical actuator simulation'
  return importlib.import_module(name)


def test_plant_delay_lag_and_speed_integration_have_literal_expected_values():
  m = module()
  plant = m.HypotheticalActuator(m.PlantConfig(delay_s=.02, lag_s=.04, watchdog_s=.05), initial_speed=10)
  assert plant.step(1).accel_mps2 == 0
  assert plant.step(1).accel_mps2 == 0
  state = plant.step(1)
  assert state.accel_mps2 == pytest.approx(.25)
  assert state.speed_mps == pytest.approx(10.0025)
  state = plant.step(1)
  assert state.accel_mps2 == pytest.approx(.4375)
  assert state.speed_mps == pytest.approx(10.006875)


def test_watchdog_drops_queued_commands_and_decay_is_not_instantaneous():
  m = module()
  plant = m.HypotheticalActuator(m.PlantConfig(delay_s=0, lag_s=.04, watchdog_s=.03), initial_speed=10)
  assert plant.step(1).accel_mps2 == pytest.approx(.25)
  assert plant.step(None).accel_mps2 == pytest.approx(.4375)
  assert plant.step(None).accel_mps2 == pytest.approx(.578125)
  state = plant.step(None)
  assert state.watchdog_expired and state.accel_mps2 == pytest.approx(.43359375)


def test_rejected_commands_do_not_reset_watchdog_or_change_speed():
  plant = module().HypotheticalActuator(initial_speed=10)
  for _ in range(100):
    state = plant.step(1, accept=False)
  assert state.accel_mps2 == 0 and state.speed_mps == 10
  assert state.watchdog_expired


@pytest.mark.parametrize('changes', [{'delay_s': -.1}, {'lag_s': 0}, {'lag_s': .001}, {'watchdog_s': 0},
                                     {'delay_s': .015}, {'accel_min': 1}, {'accel_max': float('nan')}])
def test_invalid_plant_configuration_is_rejected(changes):
  with pytest.raises(ValueError):
    module().PlantConfig(**changes)


def test_nonfinite_or_out_of_range_commands_never_enter_plant():
  plant = module().HypotheticalActuator()
  for value in (float('nan'), float('inf'), 50):
    with pytest.raises(ValueError):
      plant.step(value)


def test_synthetic_controller_tracks_a_small_speed_step_without_params_access(monkeypatch):
  def forbidden(*args, **kwargs):
    raise AssertionError('Production Params access')
  monkeypatch.setattr(longcontrol, 'Params', forbidden)
  result = module().simulate('tracking')
  assert result['mode'] == 'synthetic_closed_loop_not_byd_validation'
  assert result['control_ready'] is False
  assert abs(result['final_speed_mps'] - 11) < .5
  assert result['output_ticks'] > 1000
  assert -2 <= result['accel_range_mps2'][0] <= result['accel_range_mps2'][1] <= 1
  assert result == module().simulate('tracking')


@pytest.mark.parametrize('fault', ['brake', 'gas', 'aeb', 'stale_plan', 'nan_plan', 'invalid_vehicle', 'stop', 'disabled'])
def test_fault_scenarios_suppress_controller_output_during_fault(fault):
  result = module().simulate(fault)
  assert result['output_ticks'] > 0
  assert result['fault_window_output_ticks'] == 0
  assert result['resumed_after_disengagement_ticks'] > 0
  assert result['control_ready'] is False


def test_rejected_actuator_is_not_reported_as_tracking_success():
  result = module().simulate('reject_commands')
  assert result['output_ticks'] > 0
  assert result['final_speed_mps'] == 10
  assert result['plant_accepted_commands'] == 0
  assert result['saturation_ticks'] > 0


def test_synthetic_report_is_explicit_and_never_overwrites(tmp_path):
  path = tmp_path / 'sim.json'
  m = module()
  assert m.main(['--output', str(path)]) == 0
  before = path.read_bytes()
  result = json.loads(before)
  assert len(result['scenarios']) == 10
  assert all(not s['control_ready'] for s in result['scenarios'])
  with pytest.raises(SystemExit):
    m.main(['--output', str(path)])
  assert path.read_bytes() == before
