import os

import numpy as np

from openpilot.selfdrive.modeld.compile_modeld import (
  BIG_MODEL_STATE_KEYS,
  numpy_dtype,
  MODELD_INPUTS,
  NAV_MODEL_QUEUES,
  NAV_MODEL_STATE_KEYS,
  apply_state_update,
  get_policy_npy_shapes,
  is_nav_model,
  modeld_inputs,
  packed_layout,
)
from openpilot.selfdrive.modeld.modeld import NavInputs


BIG_SHAPES = {
  'new_img': (2, 6, 128, 256),
  'desire': (8,),
  'traffic_convention': (1, 2),
  'action_t': (1, 2),
  'state_img_q': (2, 5, 6, 128, 256),
  'state_desire_q': (132, 1, 8),
  'state_feat_q': (128, 1, 16384),
}
NAV_SHAPES = BIG_SHAPES | {
  'nav_map': (1, 1, 256, 256),
  'navigation': (1, 47),
  'state_map_features_q': (32, 1, 256, 256),
  'state_navigation_features_q': (32, 47),
}
SMALL_SHAPES = {
  'img': (1, 12, 128, 256),
  'big_img': (1, 12, 128, 256),
  'desire_pulse': (1, 25, 8),
  'traffic_convention': (1, 2),
  'action_t': (1, 2),
  'features_buffer': (1, 24, 512),
}


class TestDetection:
  def test_nav_shapes_are_recognised(self):
    assert is_nav_model(NAV_SHAPES) is True

  def test_big_shapes_are_not_nav(self):
    assert is_nav_model(BIG_SHAPES) is False

  def test_small_shapes_are_not_nav(self):
    assert is_nav_model(SMALL_SHAPES) is False

  def test_needs_both_nav_state_keys(self):
    for key in NAV_MODEL_STATE_KEYS:
      shapes = {k: v for k, v in NAV_SHAPES.items() if k != key}
      assert is_nav_model(shapes) is False

  def test_nav_model_is_also_a_big_model(self):
    assert all(k in NAV_SHAPES for k in BIG_MODEL_STATE_KEYS)


class TestInputList:
  def test_stock_artifact_keeps_five_inputs(self):
    assert modeld_inputs(SMALL_SHAPES) == MODELD_INPUTS
    assert modeld_inputs(BIG_SHAPES) == MODELD_INPUTS

  def test_nav_artifact_adds_only_the_two_state_queues(self):
    assert modeld_inputs(NAV_SHAPES) == MODELD_INPUTS + list(NAV_MODEL_QUEUES)


class TestPolicyShapes:
  def test_nav_vector_is_last_and_full_width(self):
    shapes, sizes = get_policy_npy_shapes(NAV_SHAPES)
    assert list(shapes)[-1] == 'nav'
    assert shapes['nav'] == (1, 47)
    assert sizes[-1] == 47

  def test_nav_vector_size_adds_to_the_packed_region(self):
    _, base_sizes = get_policy_npy_shapes(BIG_SHAPES)
    _, nav_sizes = get_policy_npy_shapes(NAV_SHAPES)
    assert sum(nav_sizes) - sum(base_sizes) == 47

  def test_non_nav_shapes_are_untouched(self):
    shapes, _ = get_policy_npy_shapes(BIG_SHAPES)
    assert 'nav' not in shapes
    shapes, _ = get_policy_npy_shapes(SMALL_SHAPES)
    assert 'nav' not in shapes


class TestPackedLayout:
  def test_nav_map_bytes_match_the_input(self):
    _, _, nav_map_bytes = packed_layout(NAV_SHAPES, frame_copy_size=100)
    assert nav_map_bytes == 256 * 256

  def test_non_nav_has_no_map_region(self):
    _, _, nav_map_bytes = packed_layout(BIG_SHAPES, frame_copy_size=100)
    assert nav_map_bytes == 0

  def test_frame_size_is_passed_through(self):
    _, frame_bytes, _ = packed_layout(NAV_SHAPES, frame_copy_size=1234)
    assert frame_bytes == 1234

  def test_float_region_only_grows_by_the_nav_vector(self):
    base, _, _ = packed_layout(BIG_SHAPES, frame_copy_size=100)
    nav, _, _ = packed_layout(NAV_SHAPES, frame_copy_size=100)
    assert nav - base == 47 * np.dtype(np.float32).itemsize


class _Queue:
  def __init__(self, dtype=np.float32):
    self.dtype = np.dtype(dtype)
    self.value = None

  def assign(self, value):
    self.value = value


def _queues(names):
  return {name: _Queue() for name in names}


def _val(payload):
  return _Castable(payload)


class TestStateWriteback:
  def test_big_model_still_writes_three_states(self):
    queues = _queues(('img_q', 'feat_q', 'desire_q', 'state_map_q', 'state_nav_q'))
    apply_state_update(queues, ('out', _val('img'), _val('feat'), _val('desire')))
    assert (queues['img_q'].value.payload, queues['feat_q'].value.payload, queues['desire_q'].value.payload) == ('img', 'feat', 'desire')
    assert queues['state_map_q'].value is None
    assert queues['state_nav_q'].value is None

  def test_nav_model_writes_map_then_navigation(self):
    queues = _queues(('img_q', 'feat_q', 'desire_q', 'state_map_q', 'state_nav_q'))
    apply_state_update(queues, ('out', _val('img'), _val('feat'), _val('desire'), _val('map'), _val('nav')), nav=True)
    assert queues['state_map_q'].value.payload == 'map'
    assert queues['state_nav_q'].value.payload == 'nav'

  def test_extra_outputs_are_ignored_without_the_nav_flag(self):
    queues = _queues(('img_q', 'feat_q', 'desire_q'))
    apply_state_update(queues, ('out', _val('img'), _val('feat'), _val('desire'), _val('map'), _val('nav')))
    assert queues['desire_q'].value.payload == 'desire'


class TestNavInputs:
  def test_defaults_are_zeros_with_model_shapes(self):
    src = NavInputs(NAV_SHAPES)
    assert src.map.shape == (1, 1, 256, 256)
    assert src.vec.shape == (1, 47)
    assert not src.map.any() and not src.vec.any()

  def test_files_source_is_loaded(self, tmp_path, monkeypatch):
    prefix = str(tmp_path / 'nav')
    np.full((1, 1, 256, 256), 7, dtype=np.uint8).tofile(prefix + '.map')
    np.arange(47, dtype=np.float32).reshape(1, 47).tofile(prefix + '.vec')
    monkeypatch.setenv(NavInputs.ENV, f'files:{prefix}')
    src = NavInputs(NAV_SHAPES)
    assert int(src.map.max()) == 7 and int(src.map.min()) == 7
    assert src.vec.dtype == np.float32 and src.vec[0, 46] == 46.0

  def test_wrong_sized_file_keeps_zeros(self, tmp_path, monkeypatch):
    prefix = str(tmp_path / 'bad')
    np.zeros(10, dtype=np.uint8).tofile(prefix + '.map')
    np.zeros(47, dtype=np.float32).tofile(prefix + '.vec')
    monkeypatch.setenv(NavInputs.ENV, f'files:{prefix}')
    src = NavInputs(NAV_SHAPES)
    assert not src.map.any() and not src.vec.any()

  def test_missing_files_are_survivable(self, tmp_path, monkeypatch):
    monkeypatch.setenv(NavInputs.ENV, f'files:{tmp_path / "nope"}')
    src = NavInputs(NAV_SHAPES)
    src.refresh()
    assert not src.map.any() and not src.vec.any()

  def test_refresh_only_rereads_on_change(self, tmp_path, monkeypatch):
    prefix = str(tmp_path / 'nav')
    np.zeros((1, 1, 256, 256), dtype=np.uint8).tofile(prefix + '.map')
    np.zeros(47, dtype=np.float32).tofile(prefix + '.vec')
    monkeypatch.setenv(NavInputs.ENV, f'files:{prefix}')
    src = NavInputs(NAV_SHAPES)
    src.refresh()
    np.ones(47, dtype=np.float32).tofile(prefix + '.vec')
    os.utime(prefix + '.vec', (0, 1_700_000_000))
    src.refresh()
    assert src.vec[0, 0] == 1.0


class TestDtypeMapping:

  def test_float_spec_maps_to_float32(self):
    assert numpy_dtype('dtypes.float', 'uint8') == np.dtype(np.float32)

  def test_uchar_spec_maps_to_uint8(self):
    assert numpy_dtype('dtypes.uchar', 'float32') == np.dtype(np.uint8)

  def test_missing_spec_uses_fallback(self):
    assert numpy_dtype(None, 'float32') == np.dtype(np.float32)
    assert numpy_dtype(None, 'uint8') == np.dtype(np.uint8)

  def test_unknown_spec_uses_fallback(self):
    assert numpy_dtype('dtypes.mystery', 'uint8') == np.dtype(np.uint8)


class _TypedQueue:
  def __init__(self, dtype):
    self.dtype = dtype
    self.value = None

  def assign(self, value):
    self.value = value


class _Castable:
  def __init__(self, payload):
    self.payload = payload
    self.cast_to = None

  def cast(self, dtype):
    self.cast_to = dtype
    return self


class TestWritebackDtype:
  def test_values_are_cast_to_the_queue_dtype(self):
    import numpy as np
    queues = {'img_q': _TypedQueue(np.uint8), 'feat_q': _TypedQueue(np.float32),
              'desire_q': _TypedQueue(np.float32)}
    value = _Castable('img')
    apply_state_update(queues, ('out', value))
    assert value.cast_to is queues['img_q'].dtype
    assert queues['img_q'].value is value


class TestArtifactInputs:

  def test_top_level_wins(self):
    from openpilot.selfdrive.modeld.compile_modeld import artifact_inputs
    art = {'modeld_inputs': ['a', 'b'], 'metadata': {'modeld_inputs': ['c']}}
    assert artifact_inputs(art) == ['a', 'b']

  def test_metadata_fallback(self):
    from openpilot.selfdrive.modeld.compile_modeld import artifact_inputs
    assert artifact_inputs({'metadata': {'modeld_inputs': ['c']}}) == ['c']

  def test_legacy_artifact_gets_the_default_list(self):
    from openpilot.selfdrive.modeld.compile_modeld import artifact_inputs
    assert artifact_inputs({'metadata': {}}) == MODELD_INPUTS
