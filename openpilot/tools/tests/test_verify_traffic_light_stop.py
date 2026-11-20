#!/usr/bin/env python3
"""The post-drive check must find a red-light stop and flag the failure modes.

This script is what decides whether the feature worked on the car, so its own logic
has to be pinned. The log reader is stubbed with a synthetic drive: approach on red,
stop for 3 s, leave on green.
"""

import importlib.util
import sys
import types
from pathlib import Path



class _Msg:
  def __init__(self, which, t, payload=None):
    self._which = which
    self.logMonoTime = int(t * 1e9)
    if payload is not None:
      setattr(self, which, payload)

  def which(self):
    return self._which


def _load_tool():
  path = Path(__file__).resolve().parents[3] / "tools" / "verify_traffic_light_stop.py"
  spec = importlib.util.spec_from_file_location("verify_tls", path)
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


def _drive(approach_red=20, stop_frames=60, leave_green=20, stop_light="red"):
  """Approach at 10 m/s, stop, then leave at 2 m/s."""
  seq, t = [], 0.0

  def add(dt, which, payload=None):
    nonlocal t
    t += dt
    seq.append(_Msg(which, t, payload))

  for _ in range(approach_red):
    add(0.01, "yoloObjects", types.SimpleNamespace(egoLightState="red"))
    add(0.01, "carState", types.SimpleNamespace(vEgo=10.0))
    add(0.01, "longitudinalPlan", types.SimpleNamespace(longitudinalPlanSource=5))
  for i in range(stop_frames):
    add(0.01, "carState", types.SimpleNamespace(vEgo=0.0))
    if i % 10 == 0:
      add(0.01, "yoloObjects", types.SimpleNamespace(egoLightState=stop_light))
  for _ in range(leave_green):
    add(0.01, "yoloObjects", types.SimpleNamespace(egoLightState="green"))
    add(0.01, "carState", types.SimpleNamespace(vEgo=2.0))
    add(0.01, "longitudinalPlan", types.SimpleNamespace(longitudinalPlanSource=4))
  return seq


def _run(monkeypatch, seq, capsys, min_stop_s=0.5):
  fake = types.ModuleType("openpilot.tools.lib.logreader")
  fake.LogReader = lambda path, sort_by_time=True: iter(seq)
  monkeypatch.setitem(sys.modules, "openpilot.tools.lib.logreader", fake)
  tool = _load_tool()
  rc = tool.analyse([Path("/tmp/fake--aa--0/rlog.zst")], min_stop_s=min_stop_s)
  return rc, capsys.readouterr().out


def test_it_finds_the_red_light_stop(monkeypatch, capsys):
  rc, out = _run(monkeypatch, _drive(), capsys)
  assert rc == 0
  assert "trafficLight won  20 frames" in out
  assert "stops taken on red  : 1 / 1" in out
  assert "no red flags" in out


def test_a_route_without_yolod_says_so(monkeypatch, capsys):
  seq = [_Msg("carState", 0.01, types.SimpleNamespace(vEgo=5.0))]
  rc, out = _run(monkeypatch, seq, capsys)
  assert rc == 1
  assert "no yoloObjects at all" in out


def test_it_flags_a_light_stop_that_never_engaged(monkeypatch, capsys):
  """If trafficLight never wins the merge the feature is not doing anything."""
  seq = _drive()
  for msg in seq:
    if msg.which() == "longitudinalPlan":
      msg.longitudinalPlan = types.SimpleNamespace(longitudinalPlanSource=0)
  rc, out = _run(monkeypatch, seq, capsys)
  assert "light stop never engaged" in out


def test_it_counts_braking_while_green(monkeypatch, capsys):
  """Braking on green is the failure this whole feature exists to prevent."""
  seq, t = [], 0.0
  v = 10.0
  for _ in range(40):
    t += 0.01
    seq.append(_Msg("yoloObjects", t, types.SimpleNamespace(egoLightState="green")))
    seq.append(_Msg("carState", t, types.SimpleNamespace(vEgo=v)))
    v -= 0.2                     # decelerating under a green light, stays above 0.3
  rc, out = _run(monkeypatch, seq, capsys)
  assert "braking while green" in out
  assert "braking at green - check E2ECrosswalkGuard" in out


def test_a_short_stop_is_not_reported(monkeypatch, capsys):
  rc, out = _run(monkeypatch, _drive(stop_frames=20), capsys, min_stop_s=1.0)
  assert "stops (>= 1.0s): 0" in out
