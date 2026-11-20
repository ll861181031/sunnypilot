"""Offscreen UI fixture for settings-widget tests.

Constructing a settings widget calls gui_app.font(), and that dict is only
populated by init_window() after a live GL context exists. On a bare headless
box there is no display, so these tests bring up their own X server instead of
being skipped - a fold that silently stops folding is exactly the kind of
regression nobody notices by reading the diff.

If DISPLAY is already set the existing display is reused and nothing is started
or torn down by us.
"""
import os
import shutil
import subprocess
import time

import pytest

_DISPLAY = ":97"


@pytest.fixture(scope="session")
def ui_window():
  """Load the fonts and return the gui app, with an X server if needed."""
  from openpilot.system.ui.lib.application import gui_app

  own_xvfb = None
  saved_display = os.environ.get("DISPLAY")

  if not saved_display:
    if shutil.which("Xvfb") is None:
      pytest.skip("needs DISPLAY or Xvfb to build a GL context")
    own_xvfb = subprocess.Popen(["Xvfb", _DISPLAY, "-screen", "0", "1920x1080x24"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.environ["DISPLAY"] = _DISPLAY
    # Xvfb needs a moment before it accepts connections.
    for _ in range(50):
      if own_xvfb.poll() is not None:
        pytest.skip("Xvfb exited immediately")
      if os.path.exists(f"/tmp/.X11-unix/X{_DISPLAY.lstrip(':')}"):
        break
      time.sleep(0.1)

  try:
    gui_app.init_window("byd panel tests", fps=60)
  except Exception as exc:  # pragma: no cover - environment problem, not logic
    pytest.skip(f"could not create a GL context: {exc}")

  yield gui_app

  try:
    gui_app.close()
  except Exception:
    pass
  if own_xvfb is not None:
    own_xvfb.terminate()
    try:
      own_xvfb.wait(timeout=5)
    except subprocess.TimeoutExpired:
      own_xvfb.kill()
    if saved_display is None:
      os.environ.pop("DISPLAY", None)
