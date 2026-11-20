import os
from unittest.mock import patch


from openpilot.selfdrive.modeld import helpers
from openpilot.selfdrive.modeld.compile_modeld import is_big_model, BIG_MODEL_STATE_KEYS


def _no_chestnut():
  return patch("openpilot.sunnypilot.models.helpers.get_selected_bundle", return_value=None)


BIG_SHAPES = dict.fromkeys(BIG_MODEL_STATE_KEYS, (1,)) | {"new_img": (2, 6, 128, 256)}
SMALL_SHAPES = {"img": (1, 12, 128, 256), "desire": (8,), "traffic_convention": (1, 2), "action_t": (1, 2)}


class TestIsBigModel:
  def test_big_shapes_are_recognised(self):
    assert is_big_model(BIG_SHAPES) is True

  def test_small_shapes_are_not(self):
    assert is_big_model(SMALL_SHAPES) is False

  def test_needs_every_state_key(self):
    for key in BIG_MODEL_STATE_KEYS:
      partial = {k: v for k, v in BIG_SHAPES.items() if k != key}
      assert is_big_model(partial) is False, f"缺 {key} 时不应判定为大模型"


class TestModelPathSelection:
  def test_big_path_returned_when_enabled(self, tmp_path):
    fake = tmp_path / helpers.BIG_MODEL_ON_IGPU_NAME
    fake.write_bytes(b"x")
    with patch.object(helpers, "PC", True), \
         patch.object(helpers, "get_desktop_model_device", return_value="AMD"), \
         patch.object(helpers, "big_model_on_igpu_path", return_value=fake), \
         patch.dict(os.environ, {helpers.BIG_MODEL_ON_IGPU_ENV: "1"}), _no_chestnut():
      assert helpers.modeld_pkl_path(False) == fake

  def test_falls_back_when_env_off(self, tmp_path):
    fake = tmp_path / helpers.BIG_MODEL_ON_IGPU_NAME
    fake.write_bytes(b"x")
    with patch.object(helpers, "PC", True), \
         patch.object(helpers, "get_desktop_model_device", return_value="AMD"), \
         patch.object(helpers, "big_model_on_igpu_path", return_value=fake), \
         patch.object(helpers, "MODELS_DIR", tmp_path), \
         patch.dict(os.environ, {helpers.BIG_MODEL_ON_IGPU_ENV: "0"}), _no_chestnut():
      got = helpers.modeld_pkl_path(False)
      assert got != fake
      assert "amd_driving_tinygrad.pkl" in str(got)

  def test_falls_back_when_artifact_missing(self, tmp_path):
    missing = tmp_path / helpers.BIG_MODEL_ON_IGPU_NAME
    with patch.object(helpers, "PC", True), \
         patch.object(helpers, "get_desktop_model_device", return_value="AMD"), \
         patch.object(helpers, "big_model_on_igpu_path", return_value=missing), \
         patch.object(helpers, "MODELS_DIR", tmp_path), \
         patch.dict(os.environ, {helpers.BIG_MODEL_ON_IGPU_ENV: "1"}), _no_chestnut():
      got = helpers.modeld_pkl_path(False)
      assert "amd_driving_tinygrad.pkl" in str(got)

  def test_chestnut_flag_bypasses_the_igpu_branch(self, tmp_path):
    fake = tmp_path / helpers.BIG_MODEL_ON_IGPU_NAME
    fake.write_bytes(b"x")
    with patch.object(helpers, "PC", True), \
         patch.object(helpers, "get_desktop_model_device", return_value="AMD"), \
         patch.object(helpers, "big_model_on_igpu_path", return_value=fake), \
         patch.dict(os.environ, {helpers.BIG_MODEL_ON_IGPU_ENV: "1"}), _no_chestnut():
      assert helpers.modeld_pkl_path(True) != fake
