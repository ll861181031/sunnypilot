import contextlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openpilot.selfdrive.modeld import helpers


def _isolated(models_dir=None):
  """Patch MODELS_DIR and the chestnut slot so this machine's state cannot leak in.

  Without the second patch, having picked the local big model in Settings makes every
  "default is the small model" assertion fail on the developer's own machine."""
  stack = contextlib.ExitStack()
  stack.enter_context(patch.object(helpers, "MODELS_DIR", Path(models_dir or "/nonexistent-models")))
  stack.enter_context(patch("openpilot.sunnypilot.models.helpers.get_selected_bundle", return_value=None))
  return stack


class TestDesktopModelBackend(unittest.TestCase):
  def test_amd_model_does_not_replace_cpu_or_chestnut_artifacts(self):
    with _isolated(), patch.object(helpers, "PC", True), patch.dict(os.environ, {"SUNNYPILOT_MODEL_DEVICE": "AMD"}):
      self.assertEqual(helpers.modeld_pkl_path(False).name, "amd_driving_tinygrad.pkl")
      self.assertEqual(helpers.modeld_pkl_path(True).name, "big_driving_tinygrad.pkl")
    with _isolated(), patch.object(helpers, "PC", True), patch.dict(os.environ, {"SUNNYPILOT_MODEL_DEVICE": "CPU"}):
      self.assertEqual(helpers.modeld_pkl_path(False).name, "driving_tinygrad.pkl")

  def test_device_hardware_keeps_stock_model_path(self):
    with _isolated(), patch.object(helpers, "PC", False), patch.dict(os.environ, {"SUNNYPILOT_MODEL_DEVICE": "AMD"}):
      self.assertEqual(helpers.modeld_pkl_path(False).name, "driving_tinygrad.pkl")

  def test_unknown_desktop_backend_fails_explicitly(self):
    with patch.dict(os.environ, {"SUNNYPILOT_MODEL_DEVICE": "misspelled"}):
      with self.assertRaisesRegex(ValueError, "SUNNYPILOT_MODEL_DEVICE"):
        helpers.get_desktop_model_device()

  def test_driver_monitoring_backend_is_independent_and_keeps_stock_artifacts(self):
    with _isolated(), patch.object(helpers, 'PC', True), patch.dict(os.environ, {'SUNNYPILOT_DM_DEVICE': 'AMD', 'SUNNYPILOT_MODEL_DEVICE': 'CPU'}):
      self.assertEqual(helpers.dmonitoring_pkl_path().name, 'amd_dmonitoring_model_tinygrad.pkl')
      self.assertEqual(helpers.dmonitoring_warp_path(1344, 760).name, 'amd_dm_warp_1344x760_tinygrad.pkl')
      self.assertEqual(helpers.modeld_pkl_path(False).name, 'driving_tinygrad.pkl')
    with patch.object(helpers, 'PC', True), patch.dict(os.environ, {'SUNNYPILOT_DM_DEVICE': 'CPU'}):
      self.assertEqual(helpers.dmonitoring_pkl_path().name, 'dmonitoring_model_tinygrad.pkl')
    with patch.object(helpers, 'PC', False), patch.dict(os.environ, {'SUNNYPILOT_DM_DEVICE': 'AMD'}):
      self.assertEqual(helpers.dmonitoring_pkl_path().name, 'dmonitoring_model_tinygrad.pkl')
      self.assertEqual(helpers.dmonitoring_warp_path(1928, 1208).name, 'dm_warp_1928x1208_tinygrad.pkl')

  def test_unknown_driver_monitoring_backend_fails_explicitly(self):
    with patch.dict(os.environ, {'SUNNYPILOT_DM_DEVICE': 'misspelled'}):
      with self.assertRaisesRegex(ValueError, 'SUNNYPILOT_DM_DEVICE'):
        helpers.get_desktop_dm_device()

  def test_big_model_on_igpu_is_opt_in_and_falls_back(self):
    with tempfile.TemporaryDirectory() as model_root:
      with _isolated(model_root), patch.object(helpers, 'PC', True), patch.object(helpers.Paths, 'model_root', return_value=model_root), \
           patch.dict(os.environ, {'SUNNYPILOT_MODEL_DEVICE': 'AMD'}):
        # default: never picks the big model
        self.assertEqual(helpers.modeld_pkl_path(False).name, 'amd_driving_tinygrad.pkl')

        # opt-in without a compiled artifact still falls back to the small model
        with patch.dict(os.environ, {helpers.BIG_MODEL_ON_IGPU_ENV: '1'}):
          self.assertEqual(helpers.modeld_pkl_path(False).name, 'amd_driving_tinygrad.pkl')

          # opt-in with the artifact present switches over
          big = Path(model_root) / helpers.BIG_MODEL_ON_IGPU_NAME
          big.write_bytes(b'stub')
          self.assertEqual(helpers.modeld_pkl_path(False), big)

          # chestnut keeps its own artifact regardless of the opt-in
          self.assertEqual(helpers.modeld_pkl_path(True).name, 'big_driving_tinygrad.pkl')

        # a non-PC or CPU build never looks at the iGPU artifact
        with patch.object(helpers, 'PC', False):
          self.assertEqual(helpers.modeld_pkl_path(False).name, 'driving_tinygrad.pkl')
