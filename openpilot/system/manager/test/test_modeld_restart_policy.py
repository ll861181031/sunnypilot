import importlib

import pytest

from openpilot.common import hardware
from openpilot.system.manager import process_config
from openpilot.system.manager.process import PythonProcess


@pytest.mark.parametrize("pc,backend,expected", [
  (True, "migraphx-remote", (3, 300.0)),
  (True, "mgxd", (3, 300.0)),
  (True, "migraphx", (3, 300.0)),
  (True, "MXR", (3, 300.0)),
  (True, "tinygrad", None),
  (False, "migraphx-remote", None),
])
def test_restart_limit_only_applies_to_pc_migraphx_modeld(monkeypatch, pc, backend, expected):
  try:
    with monkeypatch.context() as patch:
      patch.setenv("MODELD_BACKEND", backend)
      patch.setattr(hardware, "PC", pc)
      processes = importlib.reload(process_config).managed_processes
      assert processes["modeld"].restart_limit == expected
      for name, proc in processes.items():
        if name != "modeld" and isinstance(proc, PythonProcess):
          assert proc.restart_limit is None
  finally:
    importlib.reload(process_config)
