"""Opt-in offline end-to-end tests with the installed binary and China map cells.

MAPD_RUN_INTEGRATION=1 python -m pytest -s <this file>
No vehicle processes are started. GPS, Params and messaging use an isolated prefix.
"""
import math
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from openpilot.cereal import messaging
from openpilot.common.basedir import BASEDIR
from openpilot.common.hardware.hw import Paths
from openpilot.common.params import Params
from openpilot.common.prefix import OpenpilotPrefix
from openpilot.sunnypilot.mapd.runtime import mapd_available

pytestmark = pytest.mark.skipif(os.getenv("MAPD_RUN_INTEGRATION") != "1", reason="requires installed native mapd and China offline maps")

# Midpoints of real OSM road segments read from the downloaded v1.12.0 cells.
ROADS = [
  ("南五环", 100, 39.7919797, 116.2616175, 165.1032119906674),
  ("沪闵高架路", 80, 31.1710488, 121.4300976, 6.237846342499101),
  ("琶洲大桥", 60, 23.10986735, 113.368502, 183.30631958215),
]


class MapdHarness:
  def __init__(self, log_dir):
    self.log_dir = log_dir
    self.processes = {}
    self.logs = {}
    self.mem = Params("/dev/shm/params")
    messaging.reset_context()
    self.pm = messaging.PubMaster(["liveLocationKalman"])
    self.sm = messaging.SubMaster(["liveMapDataSP"])

  def start(self, name, module):
    self.logs[name] = (self.log_dir / f"{name}.log").open("ab")
    self.processes[name] = subprocess.Popen([sys.executable, "-m", module], cwd=BASEDIR,
                                            stdout=self.logs[name], stderr=subprocess.STDOUT)

  def stop(self, name):
    proc = self.processes.pop(name)
    proc.terminate()
    try:
      proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
      proc.kill()
      proc.wait(timeout=5)
    self.logs.pop(name).close()

  def send_location(self, road, gps_ok=True, status="valid"):
    _, _, latitude, longitude, bearing = road
    msg = messaging.new_message("liveLocationKalman")
    msg.valid = True
    location = msg.liveLocationKalman
    location.status = status
    location.gpsOK = gps_ok
    location.positionGeodetic.valid = True
    location.positionGeodetic.value = [latitude, longitude, 0.0]
    location.calibratedOrientationNED.valid = True
    location.calibratedOrientationNED.value = [0.0, 0.0, math.radians(bearing)]
    self.pm.send("liveLocationKalman", msg)

  def wait_for(self, predicate, road=None, timeout=12, **location_kwargs):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
      for name, proc in self.processes.items():
        assert proc.poll() is None, (name, (self.log_dir / f"{name}.log").read_text())
      if road:
        self.send_location(road, **location_kwargs)
      self.sm.update(0)
      if self.sm.updated["liveMapDataSP"]:
        last = self.sm["liveMapDataSP"].to_dict()
        last["valid"] = self.sm.valid["liveMapDataSP"]
        if predicate(last):
          return last
      time.sleep(0.05)
    logs = {name: (self.log_dir / f"{name}.log").read_text()[-6000:] for name in self.logs}
    pytest.fail(f"mapd timeout; last output={last}; logs={logs}")


@pytest.fixture
def mapd_harness(tmp_path):
  assert platform.machine() == "x86_64", "This test requires the native build with isolated path overrides"
  assert mapd_available(), "Build the native mapd first"
  maps = Path(os.getenv("MAPD_TEST_MAP_ROOT", Paths.mapd_root())) / "offline"
  assert maps.is_dir(), f"China offline maps missing: {maps}"
  with OpenpilotPrefix():
    root = Path(Paths.mapd_root())
    root.mkdir(parents=True)
    (root / "offline").symlink_to(maps, target_is_directory=True)
    harness = MapdHarness(tmp_path)
    mem_path = Path(harness.mem.get_param_path())
    try:
      harness.start("mapd", "openpilot.sunnypilot.mapd.runtime")
      harness.start("mapd_manager", "openpilot.sunnypilot.mapd.mapd_manager")
      yield harness
    finally:
      for name in list(harness.processes):
        harness.stop(name)
      if mem_path.is_symlink():
        shutil.rmtree(mem_path.resolve())
        mem_path.unlink()
      del harness
      messaging.reset_context()


def test_real_china_maps_bridge_and_restart(mapd_harness):
  h = mapd_harness
  h.wait_for(lambda m: not m["valid"] and not m["speedLimitValid"])
  for road in ROADS:
    result = h.wait_for(lambda m, road=road: m["valid"] and m["speedLimitValid"] and m["roadName"] == road[0]
                                 and abs(m["speedLimit"] * 3.6 - road[1]) < 0.01, road=road)
    assert h.mem.get("RoadName") == road[0]
    assert h.mem.get("MapSpeedLimit") == pytest.approx(road[1] / 3.6, abs=1e-3)
    print(f"{road[0]}: {result['speedLimit'] * 3.6:.2f} km/h, liveMapDataSP valid")

  # The real native process must reset and recover from the current GPS after restart.
  previous_pid = h.processes["mapd"].pid
  h.stop("mapd")
  h.start("mapd", "openpilot.sunnypilot.mapd.runtime")
  assert h.processes["mapd"].pid != previous_pid
  h.wait_for(lambda m: m["valid"] and m["speedLimitValid"] and m["roadName"] == ROADS[0][0]
                       and abs(m["speedLimit"] * 3.6 - ROADS[0][1]) < 0.01, road=ROADS[0])
  print("Native process restart: recovered road name and speed limit")

  h.wait_for(lambda m: not m["valid"], road=ROADS[0], gps_ok=False)
  print("GPS not OK: liveMapDataSP invalid")


def test_localizer_loss_and_missing_map(mapd_harness):
  h = mapd_harness
  h.wait_for(lambda m: m["valid"] and m["speedLimitValid"] and m["roadName"] == ROADS[0][0], road=ROADS[0])
  h.wait_for(lambda m: not m["valid"], road=ROADS[0], status="uninitialized")
  h.wait_for(lambda m: m["valid"] and m["speedLimitValid"], road=ROADS[0])
  h.wait_for(lambda m: not m["valid"], timeout=5)
  # A location outside the installed CN coverage must not retain the last road's limit.
  no_map = ("", 0, -33.8651, 151.2099, 0)
  h.wait_for(lambda m: m["valid"] and not m["speedLimitValid"] and m["roadName"] == "", road=no_map)
  print("Invalid/stale localizer and missing map: no valid stale limit")
