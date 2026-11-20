"""The missing Neural Network model is not an error unless it was asked for.

`get_nn_model_path` returns the name "MOCK" for any car outside the training
set, which is the normal outcome for a new port. The code used to log a red
ERROR for that case unconditionally, and because swaglog installs a plain
StreamHandler it went straight to stderr on every single boot - which made a
perfectly healthy BYD port look broken in the launcher output.

Only a driver who actually turned the feature on needs to hear about it.
"""
from unittest.mock import MagicMock, patch

import pytest

from opendbc.car.structs import CarParams, CarParamsSP

MOCK = ("/nonexistent/nn.json", "MOCK", False)


@pytest.fixture
def cp():
  params = CarParams.new_message()
  params.carFingerprint = "BYD_SONG_PLUS_DMI_23_ALT"
  params.brand = "byd"
  params.steerControlType = CarParams.SteerControlType.torque
  return params


def run(cp, nnlc_enabled: bool, model=MOCK):
  """Call the initializer with a stub params and capture what it logged."""
  import openpilot.sunnypilot.selfdrive.car.interfaces as interfaces

  fake_params = MagicMock()
  fake_params.get_bool.return_value = nnlc_enabled
  cp_sp = CarParamsSP()
  logged = []
  with patch.object(interfaces, "get_nn_model_path", return_value=model), \
       patch.object(interfaces, "cloudlog") as logger:
    logger.error.side_effect = lambda *a, **k: logged.append(a)
    enabled = interfaces._initialize_neural_network_lateral_control(cp, cp_sp, params=fake_params)
  return enabled, logged, cp_sp


class TestNoModelIsNotAnError:
  def test_quiet_when_the_feature_is_off(self, cp):
    enabled, logged, _ = run(cp, nnlc_enabled=False)
    assert logged == [], "a car without a model must not log an error while the feature is off"
    assert enabled is False

  def test_still_warns_when_the_driver_turned_it_on(self, cp):
    # If someone deliberately enables it, silence would be worse than noise.
    _, logged, _ = run(cp, nnlc_enabled=True)
    assert len(logged) == 1

  def test_the_feature_stays_disabled_without_a_model(self, cp):
    enabled, _, _ = run(cp, nnlc_enabled=True)
    assert enabled is False, "MOCK must never enable the feature"

  def test_a_real_model_still_enables_the_feature(self, cp):
    real = ("/models/hyundai.json", "HYUNDAI_SONATA", True)
    enabled, logged, cp_sp = run(cp, nnlc_enabled=True, model=real)
    assert enabled is True
    assert logged == []
    assert cp_sp.neuralNetworkLateralControl.model.name == "HYUNDAI_SONATA"

  def test_a_real_model_does_not_enable_itself(self, cp):
    # The param is the gate; a matching model alone must not switch it on.
    enabled, _, _ = run(cp, nnlc_enabled=False, model=("/models/x.json", "HYUNDAI_SONATA", True))
    assert enabled is False

  def test_the_model_name_is_reported_either_way(self, cp):
    _, _, cp_sp = run(cp, nnlc_enabled=False)
    assert cp_sp.neuralNetworkLateralControl.model.name == "MOCK"
    assert cp_sp.neuralNetworkLateralControl.fuzzyFingerprint is True
