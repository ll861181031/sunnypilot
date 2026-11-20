"""The Blendful lane-centre overlay: where it is wired, and when it draws.

Ported from upstream sunnypilot, so the tests pin the two things that had to change
when it moved into this fork:

1. The constants come from sunnypilot/selfdrive/controls/lib/blendful.py, because
   that is where this fork keeps the algorithm (upstream has it in modeld_v2).
2. The availability gate asks "does the launched modeld have Blendful", not
   "is the runner tinygrad". This fork wires Blendful into the stock runner too,
   and the stock runner is what this car normally runs (ModelRunnerTypeCache == 2).
   Getting that wrong greys the toggle out on a working feature.

No GL context is needed: __init__ is not used, and draw_polygon is substituted.
"""
import pathlib

import numpy as np
import pytest

from openpilot.cereal import custom
from openpilot.selfdrive.ui.sunnypilot.onroad import blendful as blendful_ui
from openpilot.selfdrive.ui.sunnypilot.ui_state import BLENDFUL_RUNNERS, UIStateSP

REPO = pathlib.Path(__file__).resolve().parents[4]
ONROAD_RENDERER = REPO / "openpilot/selfdrive/ui/onroad/model_renderer.py"
MICI_RENDERER = REPO / "openpilot/selfdrive/ui/mici/onroad/model_renderer.py"
ONROAD_SP_RENDERER = REPO / "openpilot/selfdrive/ui/sunnypilot/onroad/model_renderer.py"
MICI_SP_RENDERER = REPO / "openpilot/selfdrive/ui/sunnypilot/mici/onroad/model_renderer.py"
SP_UI_STATE = REPO / "openpilot/selfdrive/ui/sunnypilot/ui_state.py"
SETTINGS_MODELS = REPO / "openpilot/selfdrive/ui/sunnypilot/layouts/settings/models.py"
MICI_MODELS = REPO / "openpilot/selfdrive/ui/sunnypilot/mici/layouts/models.py"


# ---------------------------------------------------------------- the gate


class TestTheGateMatchesThisForkNotUpstream:
  def test_stock_runner_can_use_blendful(self):
    """Upstream gates on tinygrad only; this car runs stock, so that would hide it."""
    assert custom.ModelManagerSP.Runner.stock in BLENDFUL_RUNNERS

  def test_tinygrad_runner_can_use_blendful(self):
    assert custom.ModelManagerSP.Runner.tinygrad in BLENDFUL_RUNNERS

  def test_snpe_cannot_because_neither_modeld_loads(self):
    """snpe matches neither is_stock_model nor is_tinygrad_model in process_config."""
    assert custom.ModelManagerSP.Runner.snpe not in BLENDFUL_RUNNERS

  @pytest.mark.parametrize("runner,expected", [
    (custom.ModelManagerSP.Runner.stock, True),
    (custom.ModelManagerSP.Runner.tinygrad, True),
    (custom.ModelManagerSP.Runner.snpe, False),
  ])
  def test_blendful_supported_reads_the_active_runner(self, runner, expected):
    state = object.__new__(UIStateSP)
    state.active_runner = int(runner)
    assert state.blendful_supported() is expected


class TestTheSettingsPageUsesTheGate:
  def test_the_toggle_is_gated_at_all(self):
    src = SETTINGS_MODELS.read_text()
    assert "ui_state.blendful_supported()" in src
    assert "self.blendful_toggle.action_item.set_enabled(blendful_allowed)" in src

  def test_it_is_labelled_alpha_like_upstream(self):
    assert 'tr("Blendful (Alpha)")' in SETTINGS_MODELS.read_text()

  def test_the_unsupported_note_is_shown_without_disabling_silently(self):
    src = SETTINGS_MODELS.read_text()
    assert "Not available with the current model runner." in src

  def test_mici_page_has_the_same_gate(self):
    src = MICI_MODELS.read_text()
    assert "ui_state.blendful_supported()" in src
    assert "self.blendful_toggle.set_enabled(blendful_allowed)" in src


# ------------------------------------------------------------- the wiring


class TestTheTelemetryIsSubscribed:
  def test_modelDataV2SP_is_in_the_extended_service_list(self):
    """Without this the renderer would read a service the UI never receives."""
    assert '"modelDataV2SP"' in SP_UI_STATE.read_text().split("sm_services_ext", 1)[1].split("]", 1)[0]


class TestBothLayoutsInstantiateTheOverlay:
  def test_tici_gets_the_overlay_without_the_readout(self):
    src = ONROAD_SP_RENDERER.read_text()
    assert "BlendfulRenderer(readout=False)" in src

  def test_mici_gets_it_rect_relative(self):
    assert "BlendfulRenderer(rect_relative=True)" in MICI_SP_RENDERER.read_text()

  @pytest.mark.parametrize("path", [ONROAD_RENDERER, MICI_RENDERER])
  def test_both_renderers_update_and_draw_it(self, path):
    src = path.read_text()
    assert "self.blendful.update(self, max_idx" in src
    assert "self.blendful.draw(self._rect)" in src

  def test_the_constants_come_from_where_this_fork_keeps_the_algorithm(self):
    src = (REPO / "openpilot/selfdrive/ui/sunnypilot/onroad/blendful.py").read_text()
    assert "from openpilot.sunnypilot.selfdrive.controls.lib.blendful import" in src
    assert "modeld_v2.blendful import" not in src


# --------------------------------------------------------- the projection


class _Msg(dict):
  """Attribute + item access, like capnp structs in a SubMaster."""

  def __getattr__(self, name):
    try:
      return self[name]
    except KeyError:
      raise AttributeError(name) from None


class _SM:
  def __init__(self, msgs, valid=None, updated=None):
    self._msgs = msgs
    self.valid = valid or dict.fromkeys(msgs, True)
    self.updated = updated or dict.fromkeys(msgs, True)

  def __getitem__(self, key):
    return self._msgs[key]


class _FakeRenderer:
  """Only what BlendfulRenderer.update touches."""
  def __init__(self, n=33):
    self._lane_lines = []
    for _ in range(4):
      line = type("L", (), {})()
      line.raw_points = np.zeros((n, 3), dtype=np.float32)
      line.raw_points[:, 0] = np.linspace(0.0, 32.0, n, dtype=np.float32)
      self._lane_lines.append(line)
    # left line at +1.75 m, right at -1.75 m -> a 3.5 m lane centred on 0
    self._lane_lines[1].raw_points[:, 1] = 1.75
    self._lane_lines[2].raw_points[:, 1] = -1.75
    self.calls = []

  def _map_line_to_polygon(self, line, y_off, z_off, max_idx, max_distance=0.0):
    self.calls.append((line.copy(), y_off, z_off, max_idx, max_distance))
    pts = np.stack([line[:max_idx + 1, 0], line[:max_idx + 1, 1]], axis=1)
    return pts.astype(np.float32)


def _renderer(**kwargs):
  """Build without __init__ so no GL context or font atlas is needed."""
  r = object.__new__(blendful_ui.BlendfulRenderer)
  r._readout = kwargs.get("readout", False)
  r._rect_relative = kwargs.get("rect_relative", False)
  r._center_points = np.empty((0, 2), dtype=np.float32)
  r._trust = 0.0
  r._font = None
  from openpilot.common.filter_simple import FirstOrderFilter
  from openpilot.common.realtime import DT_MDL
  r._pull_display = FirstOrderFilter(0.0, 0.5, DT_MDL)
  r._trust_display = FirstOrderFilter(0.0, 0.5, DT_MDL)
  r._alpha = FirstOrderFilter(0.0, 0.3, DT_MDL)
  return r


def _sm(trust=0.9, left=0.9, right=0.9, lane_width=3.5, valid=True, v_ego=20.0):
  msgs = {
    'modelDataV2SP': _Msg(blendfulTrust=trust, blendfulCurvature=0.002, blendfulGoingAround=False,
                          blendfulLeftProb=left, blendfulRightProb=right, blendfulLaneWidth=lane_width),
    'modelV2': _Msg(laneLineStds=[0.0] + [0.2] * 3 + [0.0] * (len(range(0)) + 4)),
    'carState': _Msg(vEgo=v_ego),
  }
  return _SM(msgs, valid={'modelDataV2SP': valid, 'modelV2': True, 'carState': True})


@pytest.fixture
def patched_ui(monkeypatch):
  """Point the module's ui_state at a fake and record what would be drawn."""
  drawn = []

  class _UI:
    def __init__(self, sm):
      self.sm = sm

  monkeypatch.setattr(blendful_ui, "draw_polygon", lambda rect, pts, color: drawn.append((pts.copy(), color)))
  monkeypatch.setattr(blendful_ui.UIStateSP, "lateral_active", staticmethod(lambda _ui: True))
  return drawn, _UI


class TestTheOverlayStaysOutOfTheWay:
  def test_no_telemetry_means_no_points(self, patched_ui):
    drawn, _UI = patched_ui
    r = _renderer()
    sm = _sm(valid=False)
    blendful_ui.ui_state = _UI(sm)
    r.update(_FakeRenderer(), 20, 32.0)
    assert r._center_points.size == 0

  def test_zero_trust_means_no_points(self, patched_ui):
    drawn, _UI = patched_ui
    r = _renderer()
    blendful_ui.ui_state = _UI(_sm(trust=0.0))
    r.update(_FakeRenderer(), 20, 32.0)
    assert r._center_points.size == 0

  def test_nothing_is_drawn_when_lateral_is_not_active(self, patched_ui, monkeypatch):
    drawn, _UI = patched_ui
    monkeypatch.setattr(blendful_ui.UIStateSP, "lateral_active", staticmethod(lambda _ui: False))
    r = _renderer()
    blendful_ui.ui_state = _UI(_sm())
    r.update(_FakeRenderer(), 20, 32.0)
    r._center_points = np.zeros((4, 2), dtype=np.float32)
    r._trust = 0.9
    import pyray as rl
    r.draw(rl.Rectangle(0, 0, 100, 100))
    assert drawn == []

  def test_a_low_trust_overlay_is_hidden(self, patched_ui):
    drawn, _UI = patched_ui
    r = _renderer()
    blendful_ui.ui_state = _UI(_sm())
    r._center_points = np.zeros((4, 2), dtype=np.float32)
    r._trust = 0.01
    import pyray as rl
    r.draw(rl.Rectangle(0, 0, 100, 100))
    assert drawn == []


class TestTheOverlayShowsTheLaneCentre:
  def test_trust_and_both_lines_produce_a_centre_line(self, patched_ui):
    drawn, _UI = patched_ui
    r = _renderer()
    fake = _FakeRenderer()
    blendful_ui.ui_state = _UI(_sm())
    r.update(fake, 20, 32.0)
    assert fake.calls, "the centre line was never projected"
    line, y_off, z_off, max_idx, max_distance = fake.calls[0]
    # centre of a 3.5 m lane between +1.75 and -1.75 is 0, so the projected y is ~0
    assert np.allclose(line[:, 1], 0.0, atol=1e-3)
    assert y_off == pytest.approx(0.15)
    assert z_off == 0.0

  def test_the_caller_projection_args_are_forwarded(self, patched_ui):
    drawn, _UI = patched_ui
    r = _renderer()
    fake = _FakeRenderer()
    blendful_ui.ui_state = _UI(_sm())
    r.update(fake, 12, 55.5)
    assert fake.calls[0][3] == 12
    assert fake.calls[0][4] == pytest.approx(55.5)

  def test_the_centre_is_drawn_once_trust_is_real(self, patched_ui):
    drawn, _UI = patched_ui
    import pyray as rl
    r = _renderer()
    blendful_ui.ui_state = _UI(_sm())
    r.update(_FakeRenderer(), 20, 32.0)
    r.draw(rl.Rectangle(0, 0, 100, 100))
    assert len(drawn) == 1

  def test_rect_relative_shifts_into_screen_space(self, patched_ui):
    """mici draws in rect-relative coordinates; the tici screen does not."""
    drawn, _UI = patched_ui
    import pyray as rl
    r = _renderer(rect_relative=True)
    blendful_ui.ui_state = _UI(_sm())
    r.update(_FakeRenderer(), 20, 32.0)
    r._center_points = np.full((3, 2), 5.0, dtype=np.float32)
    r._trust = 0.9
    r.draw(rl.Rectangle(40, 70, 100, 100))
    assert np.allclose(drawn[0][0][:, 0], 45.0)
    assert np.allclose(drawn[0][0][:, 1], 75.0)
