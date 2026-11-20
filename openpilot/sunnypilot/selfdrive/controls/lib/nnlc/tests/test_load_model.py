import os
import shutil
import tempfile

from opendbc.car.car_helpers import interfaces
from opendbc.car.mock.values import CAR as MOCK
from openpilot.sunnypilot.selfdrive.controls.lib.nnlc.helpers import MOCK_MODEL_PATH
from openpilot.common.params import Params
from openpilot.common.realtime import DT_CTRL
from openpilot.selfdrive.car.helpers import convert_to_capnp
from openpilot.selfdrive.controls.lib.latcontrol_torque import LatControlTorque
from openpilot.sunnypilot.selfdrive.car import interfaces as sunnypilot_interfaces
from openpilot.common.test import OpenpilotTestCase


class TestNNTorqueModel(OpenpilotTestCase):

  def test_load_model(self):
    car_name = MOCK.MOCK
    with tempfile.NamedTemporaryFile(prefix="TEST_TORQUE_MODEL_", suffix=".json", delete=False) as model_file:
      model_path = model_file.name
    self.addCleanup(os.unlink, model_path)
    shutil.copyfile(MOCK_MODEL_PATH, model_path)

    params = Params()
    params.put_bool("NeuralNetworkLateralControl", True, block=True)

    CarInterface = interfaces[car_name]
    CP = CarInterface.get_non_essential_params(car_name)
    CP_SP = CarInterface.get_non_essential_params_sp(CP, car_name)
    CI = CarInterface(CP, CP_SP)

    sunnypilot_interfaces.setup_interfaces(CI, params)
    CI.configure_torque_tune(car_name, CP.lateralTuning)
    CP_SP.neuralNetworkLateralControl.model.path = model_path

    CP_SP = convert_to_capnp(CP_SP)

    controller = LatControlTorque(CP.as_reader(), CP_SP.as_reader(), CI, DT_CTRL)

    assert controller.extension.has_nn_model
