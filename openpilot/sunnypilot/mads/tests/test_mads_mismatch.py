from types import SimpleNamespace as NS

import pytest

from openpilot.sunnypilot.mads.mads import ModularAssistiveDrivingSystem, SafetyModel


class PandaSamples(dict):
  def __init__(self, allowed=(True,), *, valid=True, alive=True, modes=None):
    modes = modes or [SafetyModel.byd] * len(allowed)
    super().__init__(pandaStates=[NS(safetyModel=mode, controlsAllowedLateral=value)
                                 for mode, value in zip(modes, allowed, strict=True)])
    self.valid = {'pandaStates': valid}
    self.alive = {'pandaStates': alive}


def mads(samples, count=199):
  obj = object.__new__(ModularAssistiveDrivingSystem)
  obj.active = True
  obj.lateral_mismatch_counter = count
  obj.selfdrive = NS(enabled=False, sm=samples)
  return obj


def test_recovered_authorization_resets_consecutive_mismatches():
  obj = mads(PandaSamples())
  obj.data_sample()
  assert obj.lateral_mismatch_counter == 0
  obj.selfdrive.sm['pandaStates'][0].controlsAllowedLateral = False
  obj.data_sample()
  assert obj.lateral_mismatch_counter == 1


def test_continuous_denial_still_reaches_protection_threshold():
  obj = mads(PandaSamples((False,)), count=0)
  for _ in range(200):
    obj.data_sample()
  assert obj.lateral_mismatch_counter == 200


@pytest.mark.parametrize('samples', [PandaSamples((True, False)), PandaSamples((), modes=[]),
                                   PandaSamples(valid=False), PandaSamples(alive=False),
                                   PandaSamples((True,), modes=[SafetyModel.noOutput])])
def test_unhealthy_or_missing_authorization_does_not_reset_mismatch(samples):
  obj = mads(samples)
  obj.data_sample()
  assert obj.lateral_mismatch_counter == 200


def test_ignored_panda_does_not_override_an_authorized_control_panda():
  obj = mads(PandaSamples((False, True), modes=[SafetyModel.noOutput, SafetyModel.byd]))
  obj.data_sample()
  assert obj.lateral_mismatch_counter == 0


@pytest.mark.parametrize('active,enabled', [(False, False), (True, True)])
def test_inactive_mads_or_main_control_resets_counter(active, enabled):
  obj = mads(PandaSamples((False,)))
  obj.active, obj.selfdrive.enabled = active, enabled
  obj.data_sample()
  assert obj.lateral_mismatch_counter == 0
