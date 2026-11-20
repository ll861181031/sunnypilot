
import pathlib
import tempfile

from openpilot.common.params import Params

REPO = pathlib.Path(__file__).resolve().parents[3]
MODELD = REPO / "openpilot/selfdrive/modeld/modeld.py"
KEYS_H = REPO / "openpilot/common/params_keys.h"
MODELS_UI = REPO / "openpilot/selfdrive/ui/sunnypilot/layouts/settings/models.py"


class TestModeldReadsParams:
  def test_it_no_longer_reads_the_environment_variable(self):
    src = MODELD.read_text()
    assert 'os.getenv("SUNNYPILOT_BLENDFUL")' not in src

  def test_it_reads_the_param(self):
    src = MODELD.read_text()
    assert 'params.get_bool("Blendful")' in src

  def test_the_param_is_refreshed_while_running(self):
    src = MODELD.read_text()
    assert src.count('params.get_bool("Blendful")') == 2, \
      "应该有一处启动读取 + 一处慢速循环刷新"


class TestTheKeyExists:
  def test_it_is_declared_bool_and_persistent(self):
    src = KEYS_H.read_text()
    assert '{"Blendful", {PERSISTENT | BACKUP, BOOL}}' in src

  def test_defaults_to_off(self):
    with tempfile.TemporaryDirectory() as d:
      p = Params(d)
      assert p.get_bool("Blendful") is False

  def test_it_round_trips(self):
    with tempfile.TemporaryDirectory() as d:
      p = Params(d)
      p.put_bool("Blendful", True, block=True)
      assert p.get_bool("Blendful") is True
      p.put_bool("Blendful", False, block=True)
      assert p.get_bool("Blendful") is False


class TestTheSettingsPageCanToggleIt:
  def test_a_toggle_is_bound_to_the_same_key(self):
    src = MODELS_UI.read_text()
    assert 'param="Blendful"' in src

  def test_the_toggle_is_reachable(self):
    src = MODELS_UI.read_text()
    assert "self.blendful_toggle" in src
    items = src.split("self.items = [", 1)[1].split("]", 1)[0]
    assert "self.blendful_toggle" in items
