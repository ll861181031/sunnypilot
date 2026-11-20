import os
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from openpilot.common.basedir import BASEDIR


class TestLaunchSingleInstance(unittest.TestCase):
  def test_active_legacy_video_bench_blocks_launcher(self):
    with tempfile.TemporaryDirectory() as directory:
      root = Path(directory)
      launcher = root / "launch_openpilot.sh"
      shutil.copy(Path(BASEDIR) / "launch_openpilot.sh", launcher)
      child = root / "launch_chffrplus.sh"
      child.write_text('#!/usr/bin/env bash\ntouch started\n')
      child.chmod(0o755)
      systemctl = root / "systemctl"
      systemctl.write_text('#!/usr/bin/env bash\n[ "$*" = "--user is-active --quiet cplink-bench-video.service" ]\n')
      systemctl.chmod(0o755)
      env = dict(os.environ, XDG_RUNTIME_DIR=directory, PATH=f"{root}:{os.environ['PATH']}")
      result = subprocess.run(["bash", str(launcher)], env=env, capture_output=True, text=True, timeout=2)
      self.assertNotEqual(result.returncode, 0)
      self.assertIn("cplink-bench-video.service", result.stderr)
      self.assertFalse((root / "started").exists())

  def test_second_launcher_cannot_start_another_manager(self):
    with tempfile.TemporaryDirectory() as directory:
      root = Path(directory)
      launcher = root / "launch_openpilot.sh"
      shutil.copy(Path(BASEDIR) / "launch_openpilot.sh", launcher)
      child = root / "launch_chffrplus.sh"
      child.write_text('#!/usr/bin/env bash\nprintf "started\\n" >> starts\nsleep 10\n')
      child.chmod(0o755)
      systemctl = root / "systemctl"
      systemctl.write_text('#!/usr/bin/env bash\nexit 1\n')
      systemctl.chmod(0o755)
      env = dict(os.environ, XDG_RUNTIME_DIR=directory, PATH=f"{root}:{os.environ['PATH']}")
      first = subprocess.Popen(["bash", str(launcher)], env=env, start_new_session=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
      try:
        deadline = time.monotonic() + 2
        while not (root / "starts").exists():
          self.assertLess(time.monotonic(), deadline, "first launcher did not start")
          time.sleep(0.01)
        second = subprocess.Popen(["bash", str(launcher)], env=env, start_new_session=True,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
          try:
            result = second.wait(timeout=1)
          except subprocess.TimeoutExpired:
            self.fail("second launcher started a concurrent manager")
          self.assertNotEqual(result, 0)
          self.assertEqual((root / "starts").read_text().splitlines(), ["started"])
        finally:
          if second.poll() is None:
            os.killpg(second.pid, signal.SIGTERM)
            second.wait(timeout=2)
      finally:
        os.killpg(first.pid, signal.SIGTERM)
        first.wait(timeout=2)
