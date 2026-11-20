"""Card's lead-bound learner wiring: brand gate, Params tolerance, and ordering.

The learner used to be opened inline at the top of ``Car.__init__``, where it read
``self.CP.brand`` before ``self.CP`` was assigned. That is not a ``None`` attribute -
the attribute does not exist yet - so ``AttributeError: 'Car' object has no attribute
'CP'`` killed card on every start, before it could publish carState or CarParams.
selfdrived then blocked on ``Params.get("CarParams", block=True)`` and never published
selfdriveState, which is what the UI renders as "sunnypilot Unavailable / Waiting to
start". The helper tests around this code all build a ``Car`` via ``__new__`` and call
one method, so none of them could see the ordering; ``test_cp_is_assigned_before_any_access``
does.
"""
import ast
import inspect
from types import SimpleNamespace

import pytest

from openpilot.selfdrive.car.card import Car
from openpilot.sunnypilot.selfdrive.controls.lib.lead_bound_learn import MODE_APPLY, MODE_OFF, MODE_SHADOW

CARD_SOURCE = inspect.getsourcefile(Car)


class _Params(dict):
  """Just the surface _init_lead_learn uses: get(key, return_default=True)."""

  def get(self, key, return_default=False):
    if dict.__contains__(self, key):
      return dict.__getitem__(self, key)
    if return_default:
      return None
    raise KeyError(key)


class _RaisingParams:
  """Stands in for a libparams_c built before the learner keys existed."""

  def get(self, key, return_default=False):
    raise RuntimeError(f"UnknownKeyName: {key}")


def _card_stub(brand, params=None):
  obj = Car.__new__(Car)
  obj.CP = SimpleNamespace(brand=brand)
  obj.params = _Params() if params is None else params if isinstance(params, _RaisingParams) else _Params(params)
  obj._lead_learn = None
  obj._lead_learn_last_save = 0.0
  obj._lead_learn_dirty = False
  return obj


def test_cp_is_assigned_before_any_access():
  """No ``self.CP.x`` in __init__ before the earliest ``self.CP = ...``.

  This is the regression for the startup crash that made the car show
  "sunnypilot Unavailable": the gate reads CP.brand, so CP has to exist first.
  """
  with open(CARD_SOURCE) as f:
    tree = ast.parse(f.read())

  init = None
  for node in ast.walk(tree):
    if isinstance(node, ast.ClassDef) and node.name == "Car":
      init = next((n for n in node.body if isinstance(n, ast.FunctionDef) and n.name == "__init__"), None)
  assert init is not None, "Car.__init__ not found"

  def is_self_cp(node):
    return isinstance(node, ast.Attribute) and node.attr == "CP" and isinstance(node.value, ast.Name) \
      and node.value.id == "self"

  assigned = [n.lineno for n in ast.walk(init) if is_self_cp(n) and isinstance(n.ctx, ast.Store)]
  # Any load counts, including a bare `self.CP` handed to getattr(): that is exactly
  # how the original code read the brand, and `getattr(self.CP, ...)` still has to
  # look up `self.CP` first.
  accessed = [n.lineno for n in ast.walk(init) if is_self_cp(n) and isinstance(n.ctx, ast.Load)]

  assert assigned, "Car.__init__ never assigns self.CP"
  first_assignment = min(assigned)
  early = sorted(line for line in accessed if line < first_assignment)
  assert not early, f"self.CP read before assignment at lines {early} (first assignment line {first_assignment})"


def test_init_lead_learn_is_called_exactly_once_from_init():
  with open(CARD_SOURCE) as f:
    tree = ast.parse(f.read())

  init = None
  for node in ast.walk(tree):
    if isinstance(node, ast.ClassDef) and node.name == "Car":
      init = next((n for n in node.body if isinstance(n, ast.FunctionDef) and n.name == "__init__"), None)
  assert init is not None

  calls = [n for n in ast.walk(init)
           if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
           and n.func.attr == "_init_lead_learn"]
  assert len(calls) == 1, "the learner must be opened once, from __init__"


@pytest.mark.parametrize("brand", ["toyota", "honda", ""])
def test_other_brands_leave_the_learner_closed(brand):
  card = _card_stub(brand, {"BydLeadLearnMode": "3"})
  card._init_lead_learn()
  assert card._lead_learn is None


@pytest.mark.parametrize("raw,expected", [
  (None, MODE_SHADOW),   # key unset -> shipped behaviour
  ("0", MODE_SHADOW),    # page domain: 0 is "unset"
  ("2", MODE_SHADOW),
  ("3", MODE_APPLY),
])
def test_param_domain_selects_the_mode(raw, expected):
  card = _card_stub("byd", {} if raw is None else {"BydLeadLearnMode": raw})
  card._init_lead_learn()
  assert card._lead_learn is not None
  assert card._lead_learn.mode == expected


def test_off_mode_builds_nothing():
  card = _card_stub("byd", {"BydLeadLearnMode": "1"})
  card._init_lead_learn()
  assert card._lead_learn is None


def test_missing_params_keys_do_not_take_card_down():
  """An old libparams_c must degrade to "learner off", never to a dead card."""
  card = _card_stub("byd", _RaisingParams())
  card._init_lead_learn()
  assert card._lead_learn is None


def test_stored_table_replaces_the_shipped_one():
  stored = "0.0:4.50,5.0:4.50,11.0:3.77,20.0:1.50"
  card = _card_stub("byd", {"BydLeadLearnMode": "2", "BydLeadBoundTable": stored})
  card._init_lead_learn()
  assert card._lead_learn is not None
  _, bounds = card._lead_learn.table()
  assert bounds[0] == pytest.approx(4.5)


@pytest.mark.parametrize("stored", ["0.0:1.50,20.0:5.50", "0:4.50,35:nan,75:1.50", "0:4.50,inf:1.50"])
def test_garbage_table_falls_back_to_the_shipped_curve(stored):
  card = _card_stub("byd", {"BydLeadLearnMode": "2", "BydLeadBoundTable": stored})
  card._init_lead_learn()
  assert card._lead_learn is not None
  _, bounds = card._lead_learn.table()
  assert bounds[0] == pytest.approx(5.0)  # CarControllerParams.STEER_ANGLE_ERROR_MAX at 0 km/h


def test_learner_starts_disabled_and_off_by_default_in_a_fresh_card():
  """The attributes step() reads must exist even when the brand gate returns early."""
  card = _card_stub("toyota")
  card._init_lead_learn()
  assert card._lead_learn is None
  assert card._lead_learn_dirty is False
  assert card._lead_learn_last_save == 0.0
  assert MODE_OFF != MODE_SHADOW


def _bound_at(card, v_kmh):
  """Lead bound this card's learner would act on at the given speed."""
  import numpy as np
  bp, bounds = card._lead_learn.table()
  return float(np.interp(v_kmh / 3.6, bp, bounds))


def test_shadow_mode_resumes_from_its_own_proposal():
  """A restart must not throw the proposal away.

  The learner writes BydLeadBoundShadow in shadow mode and BydLeadBoundTable only in
  apply mode. `_init_lead_learn` used to read BydLeadBoundTable in both, so the
  default (shadow) mode restarted from the shipped curve every boot: the 2026-11-17
  logs show 0.0:4.89 at 08:44:50 and 0.0:5.00 again at 08:49:51 after the restart.
  """
  shadow = "0.0:4.60,5.0:4.60,15.0:4.30,25.0:3.90,35.0:3.20,45.0:2.80,75.0:1.50"
  card = _card_stub("byd", {"BydLeadLearnMode": "2", "BydLeadBoundShadow": shadow})
  card._init_lead_learn()
  assert card._lead_learn is not None
  assert card._lead_learn.mode == MODE_SHADOW
  assert _bound_at(card, 35.0) == pytest.approx(3.20, abs=0.01)


def test_apply_mode_resumes_from_the_applied_table():
  table = "0.0:4.20,15.0:4.00,35.0:3.10,75.0:1.50"
  shadow = "0.0:4.60,5.0:4.60,15.0:4.30,25.0:3.90,35.0:3.20,45.0:2.80,75.0:1.50"
  card = _card_stub("byd", {"BydLeadLearnMode": "3", "BydLeadBoundTable": table, "BydLeadBoundShadow": shadow})
  card._init_lead_learn()
  # The applied table's value at 35 km/h, not the shadow's 3.20.
  assert _bound_at(card, 35.0) == pytest.approx(3.10, abs=0.01)


def test_the_two_modes_never_share_a_key():
  from openpilot.sunnypilot.selfdrive.controls.lib.lead_bound_learn import table_key
  assert table_key(MODE_SHADOW) != table_key(MODE_APPLY)
  assert table_key(MODE_OFF) == table_key(MODE_SHADOW)
