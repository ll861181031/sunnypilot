import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openpilot.common.hardware import PC
from openpilot.common.hardware.hw import DEFAULT_DOWNLOAD_CACHE_ROOT, Paths
from openpilot.common.prefix import OpenpilotPrefix


@unittest.skipUnless(PC, "仅在桌面运行")
class TestPrefix(unittest.TestCase):
  def test_external_log_root_survives_cleanup(self):
    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, HOME=tmp, PARAMS_ROOT=tmp + "/params", LOG_ROOT=tmp + "/logs"):
      log = Path(Paths.log_root()) / "recording"
      log.parent.mkdir()
      log.touch()
      with OpenpilotPrefix():
        home = Path(Paths.comma_home())
        home.mkdir(exist_ok=True)
      self.assertTrue(log.is_file())
      self.assertFalse(home.exists())

  def test_cleanup_targets_own_prefix_when_environment_changes(self):
    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, HOME=tmp, PARAMS_ROOT=tmp + "/params", OPENPILOT_PREFIX="outer"):
      outer = Path(Paths.comma_home())
      outer.mkdir()
      with OpenpilotPrefix():
        inner = Path(Paths.comma_home())
        inner.mkdir(exist_ok=True)
        os.environ["OPENPILOT_PREFIX"] = "outer"
      self.assertTrue(outer.is_dir())
      self.assertFalse(inner.exists())

  def test_shared_cache_override_is_scoped(self):
    for previous in (None, "/previous/cache"):
      with self.subTest(previous=previous), patch.dict(os.environ):
        if previous is None:
          os.environ.pop("COMMA_CACHE", None)
        else:
          os.environ["COMMA_CACHE"] = previous
        with OpenpilotPrefix(create_dirs_on_enter=False, clean_dirs_on_exit=False, shared_download_cache=True):
          self.assertEqual(os.environ["COMMA_CACHE"], DEFAULT_DOWNLOAD_CACHE_ROOT)
        self.assertEqual(os.environ.get("COMMA_CACHE"), previous)
