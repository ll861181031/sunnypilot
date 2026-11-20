import importlib
import os
import unittest
from unittest.mock import patch

from openpilot.system.manager import process_config


class TestESP32GpsProcess(unittest.TestCase):
  def test_sidecar_only_runs_when_the_imu_is_disabled(self):
    try:
      with patch("openpilot.common.hardware.PC", True), patch("platform.system", return_value="Linux"):
        with patch.dict(os.environ, {"SUNNYPILOT_DISABLE_IMU": "1"}):
          processes = importlib.reload(process_config).managed_processes
          self.assertTrue(processes["esp32gpsd"].enabled)
          self.assertTrue(processes["esp32gpsd"].should_run(True, None, None))
          self.assertFalse(processes["esp32gpsd"].should_run(False, None, None))
        with patch.dict(os.environ, {"SUNNYPILOT_DISABLE_IMU": "0"}):
          processes = importlib.reload(process_config).managed_processes
          self.assertFalse(processes["esp32gpsd"].enabled)
    finally:
      importlib.reload(process_config)


if __name__ == "__main__":
  unittest.main()
