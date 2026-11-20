from types import SimpleNamespace

import pytest

from openpilot.system.ui.lib import application


@pytest.fixture
def window_setup(monkeypatch):
  # Keep the real startup method; replace only native graphics and process hooks.
  monkeypatch.setattr(application.GuiApplication, "_set_log_callback", lambda self: None)
  app = application.GuiApplication(64, 64)
  monkeypatch.setattr(application.atexit, "register", lambda callback: None)
  monkeypatch.setattr(application.signal, "signal", lambda *args: None)
  monkeypatch.setattr(application, "PC", True)
  monkeypatch.setattr(application, "BURN_IN_MODE", False)
  monkeypatch.setattr(application, "RECORD", False)
  monkeypatch.delenv("PROFILE_STARTUP", raising=False)
  state = SimpleNamespace(ready=False, events=[])

  def native(name, result=None):
    def call(*args):
      state.events.append(name)
      if not state.ready:
        pytest.fail(f"Graphics called after window initialization failed: {name}")
      return result
    return call

  monkeypatch.setattr(application.rl, "set_config_flags", lambda flags: None)
  monkeypatch.setattr(application.rl, "init_window", lambda *args: state.events.append("init_window"))

  def window_ready():
    state.events.append("is_window_ready")
    return state.ready

  monkeypatch.setattr(application.rl, "is_window_ready", window_ready)
  for name in ("set_mouse_scale", "load_render_texture", "set_texture_filter", "set_target_fps", "load_shader_from_memory"):
    monkeypatch.setattr(application.rl, name, native(name))
  monkeypatch.setattr(app, "_load_fonts", native("fonts"))
  monkeypatch.setattr(app, "_patch_text_functions", native("text"))
  monkeypatch.setattr(app, "_patch_scissor_mode", native("scissor"))
  return app, state


@pytest.mark.parametrize("title", ["UI", "Spinner"])
@pytest.mark.parametrize("scale,record,burn_in", [(1.0, False, False), (0.5, False, False), (1.0, True, False), (1.0, False, True)])
def test_failed_window_exits_before_any_graphics_setup(monkeypatch, window_setup, title, scale, record, burn_in):
  app, state = window_setup
  app._scale = scale
  monkeypatch.setattr(application, "RECORD", record)
  monkeypatch.setattr(application, "BURN_IN_MODE", burn_in)

  with pytest.raises(SystemExit) as exc:
    app.init_window(title, fps=20)

  assert exc.value.code == 75
  assert state.events == ["init_window", "is_window_ready"]
  app.close()
  assert state.events == ["init_window", "is_window_ready", "is_window_ready"]


def test_ready_window_continues_normal_initialization(window_setup):
  app, state = window_setup
  state.ready = True
  app._scale = 1.0

  app.init_window("UI", fps=20)

  assert state.events[:2] == ["init_window", "is_window_ready"]
  assert "fonts" in state.events
  assert "text" in state.events
  assert "scissor" in state.events
  assert app.target_fps == 20
