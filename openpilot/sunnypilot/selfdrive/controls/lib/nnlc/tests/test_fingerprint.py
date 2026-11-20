from opendbc.car import structs
from opendbc.car.byd.values import CAR
from openpilot.sunnypilot.selfdrive.controls.lib.nnlc import helpers


def test_model_lookup_exact_and_fuzzy(tmp_path, monkeypatch):
  name = "TEST_PLATFORM_V1"
  (tmp_path / f"{name}.json").touch()
  monkeypatch.setattr(helpers, "TORQUE_NN_MODEL_PATH", tmp_path)
  cp = structs.CarParams(carFingerprint=name, steerControlType="torque")
  assert helpers.get_nn_model_path(cp)[1:] == (name, True)
  cp.carFingerprint = name + "A"
  assert helpers.get_nn_model_path(cp)[1:] == (name, False)


def test_song_angle_control_has_no_torque_model():
  cp = structs.CarParams(carFingerprint=CAR.BYD_SONG_PLUS_DMI_23_ALT, steerControlType="angle")
  assert helpers.get_nn_model_path(cp)[:2] == (helpers.MOCK_MODEL_PATH, "MOCK")
