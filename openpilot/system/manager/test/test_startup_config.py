import importlib
import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from openpilot.common import camera_config
from openpilot.system.manager import process_config


class TestStartupConfig(unittest.TestCase):
  def test_esp32_is_the_only_desktop_imu_backend(self):
    cases = [
      (True, "Linux", "0", True, "esp32d"),
      (True, "Linux", "1", False, "esp32d"),
      (True, "Darwin", "0", False, "sensord"),
      (True, "Darwin", "1", False, "sensord"),
      (False, "Linux", "0", True, "sensord"),
      (False, "Linux", "1", False, "sensord"),
    ]
    try:
      for pc, system, disabled, enabled, module in cases:
        with self.subTest(pc=pc, system=system, disabled=disabled):
          with patch.dict(os.environ, {"SUNNYPILOT_DISABLE_IMU": disabled}), \
               patch("openpilot.common.hardware.PC", pc), patch("platform.system", return_value=system):
            proc = importlib.reload(process_config).managed_processes['sensord']
            self.assertEqual(proc.enabled, enabled)
            self.assertEqual(proc.module, f'openpilot.system.sensord.{module}')
            self.assertFalse(proc.should_run(False, None, None))
            self.assertTrue(proc.should_run(True, None, None))
    finally:
      importlib.reload(process_config)

  def test_camera_processes_are_mutually_exclusive(self):
    try:
      with patch.dict(os.environ, {"USE_WEBCAM": "1"}), patch("openpilot.common.hardware.PC", True):
        processes = importlib.reload(process_config).managed_processes
        self.assertFalse(processes["camerad"].enabled)
        self.assertTrue(processes["webcamerad"].enabled)
      with patch("openpilot.common.hardware.PC", True):
        os.environ.pop("USE_WEBCAM", None)
        processes = importlib.reload(process_config).managed_processes
        self.assertTrue(processes["camerad"].enabled)
        self.assertFalse(processes["webcamerad"].enabled)
    finally:
      importlib.reload(process_config)

  def test_driver_monitoring_gating_matches_selfdrived(self):
    # The decision is computed independently in two modules: process_config
    # decides which monitoring processes launch, selfdrived decides whether to
    # subscribe to and enforce them. They must agree for every configuration.
    from openpilot.selfdrive.selfdrived import selfdrived

    cases = [
      # pc, USE_WEBCAM, DRIVER_CAM, SUNNYPILOT_DISABLE_DRIVER_CAMERA, expected
      (True, None, None, None, False),   # PC without webcam mode has no cabin camera
      (True, "1", None, None, False),    # webcam but no driver camera
      (True, "1", "2", None, True),      # full desktop DM
      (True, "1", "2", "1", False),      # explicit kill switch wins
      (False, None, None, None, True),   # device always has the cabin camera
      (False, None, None, "1", False),   # kill switch on device
    ]
    try:
      for pc, webcam, dcam, disable, expected in cases:
        with self.subTest(pc=pc, webcam=webcam, dcam=dcam, disable=disable):
          env = {}
          if webcam is not None:
            env["USE_WEBCAM"] = webcam
          if dcam is not None:
            env["DRIVER_CAM"] = dcam
          if disable is not None:
            env["SUNNYPILOT_DISABLE_DRIVER_CAMERA"] = disable
          # The manager now asks camera_config.driver_camera_available(), which probes
          # the device. This test is about the PC/webcam/disable gating, so the probe is
          # stubbed and its cache cleared per case; the probe itself is covered by
          # openpilot/selfdrive/ui/tests/test_driver_camera_available.py.
          with patch.dict(os.environ, env, clear=False), patch("openpilot.common.hardware.PC", pc), \
               patch("openpilot.system.camerad.webcam.driver_camera_setup.validate_driver_camera"):
            for key in ("USE_WEBCAM", "DRIVER_CAM", "SUNNYPILOT_DISABLE_DRIVER_CAMERA"):
              if key not in env:
                os.environ.pop(key, None)
            camera_config.driver_camera_available.cache_clear()
            processes = importlib.reload(process_config).managed_processes
            sd = importlib.reload(selfdrived)
            launched = processes["dmonitoringmodeld"].enabled and processes["dmonitoringd"].enabled
            self.assertEqual(launched, expected, "process gating")
            self.assertEqual(sd.DM_ENABLED, expected, "selfdrived gating")
    finally:
      camera_config.driver_camera_available.cache_clear()
      importlib.reload(process_config)
      importlib.reload(selfdrived)

  def test_explicit_hardware_disables_survive_onroad_transition(self):
    try:
      with patch.dict(os.environ, {"SUNNYPILOT_DISABLE_IMU": "1", "SUNNYPILOT_DISABLE_DRIVER_CAMERA": "1", "USE_WEBCAM": "1", "DRIVER_CAM": "2"}), \
           patch("openpilot.common.hardware.PC", False):
        processes = importlib.reload(process_config).managed_processes
        for name in ("sensord", "dmonitoringmodeld", "dmonitoringd"):
          self.assertFalse(processes[name].enabled, name)
        self.assertTrue(processes["modeld"].enabled)
        self.assertTrue(processes["webcamerad"].enabled)
    finally:
      importlib.reload(process_config)

  def test_readonly_manager_does_not_start_controls_even_with_started_message(self):
    from openpilot.system.manager import manager

    class StopLoop(BaseException):
      pass

    sm = Mock()
    values = {"deviceState": SimpleNamespace(started=True), "carParams": Mock(), "pandaStates": []}
    sm.__getitem__ = Mock(side_effect=values.__getitem__)
    calls = []

    def record_start(procs, started, **kwargs):
      calls.append(started)
      if len(calls) == 2:
        raise StopLoop

    with patch.dict(os.environ, {"PANDA_USB_READONLY": "1"}), patch("openpilot.selfdrive.pandad.PC", True), \
         patch.object(manager, "Params"), patch.object(manager, "cloudlog"), \
         patch.object(manager.messaging, "SubMaster", return_value=sm), patch.object(manager.messaging, "PubMaster"), \
         patch.object(manager, "ensure_running", side_effect=record_start):
      with self.assertRaises(StopLoop):
        manager.manager_thread()
    self.assertEqual(calls, [False, False])

  def test_webcam_runs_offroad_only_in_explicit_readonly_mode_or_preview(self):
    params = Mock()
    params.get_bool.return_value = False
    with patch("openpilot.selfdrive.pandad.PC", True):
      with patch.dict(os.environ, {"PANDA_USB_READONLY": "1"}):
        self.assertTrue(process_config.webcam(False, params, None))
      with patch.dict(os.environ, {"PANDA_USB_READONLY": "0"}):
        self.assertFalse(process_config.webcam(False, params, None))
        self.assertTrue(process_config.webcam(True, params, None))
        params.get_bool.return_value = True
        self.assertTrue(process_config.webcam(False, params, None))

  def test_process_availability_preserves_panda_and_checks_mapd_binary(self):
    cases = [
      ("Linux", "x86_64", True, True),
      ("Linux", "x86_64", True, False),
      ("Linux", "aarch64", True, True),
      ("Linux", "aarch64", False, True),
      ("Darwin", "arm64", True, False),
    ]
    for system, machine, pc, mapd_enabled in cases:
      with self.subTest(system=system, machine=machine, pc=pc):
        try:
          with patch("platform.system", return_value=system), patch("platform.machine", return_value=machine), \
               patch("openpilot.common.hardware.PC", pc), \
               patch("openpilot.sunnypilot.mapd.runtime.mapd_unavailable_reason",
                     return_value=None if mapd_enabled else "test: mapd unavailable"):
            processes = importlib.reload(process_config).managed_processes
            self.assertTrue(processes["pandad"].enabled)
            self.assertIs(processes["mapd"].enabled, mapd_enabled)
            self.assertIs(processes["mapd_manager"].enabled, mapd_enabled)
        finally:
          importlib.reload(process_config)
