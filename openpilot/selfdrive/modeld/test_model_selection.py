
import contextlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openpilot.selfdrive.modeld import helpers


class _Artifact:
  def __init__(self, file_name):
    self.fileName = file_name


class _Model:
  def __init__(self, file_name):
    self.artifact = _Artifact(file_name)


class _Bundle:
  def __init__(self, *file_names, ref=""):
    self.models = [_Model(n) for n in file_names]
    # A real capnp ModelBundle always carries a ref (possibly empty); the local
    # -artifact path keys off its prefix, so the double has to model it too.
    self.ref = ref


def _patch_bundle(bundle):
  return patch("openpilot.sunnypilot.models.helpers.get_active_bundle", return_value=bundle)


def _no_chestnut_pick():
  return patch("openpilot.sunnypilot.models.helpers.get_selected_bundle", return_value=None)


class TestSelectedBundleIsUsed(unittest.TestCase):
  @contextlib.contextmanager
  def _ctx(self, model_root, device="AMD"):
    with patch.object(helpers, "PC", True), \
         patch.object(helpers.Paths, "model_root", return_value=model_root), \
         patch.object(helpers, "MODELS_DIR", Path(model_root)), \
         patch.dict(os.environ, {"SUNNYPILOT_MODEL_DEVICE": device}):
      yield

  def test_selected_bundle_overrides_the_hardcoded_default(self):
    with tempfile.TemporaryDirectory() as root:
      picked = Path(root) / "driving_cd210_model_february_03_2026_tinygrad.pkl"
      picked.write_bytes(b"stub")
      with self._ctx(root), _patch_bundle(_Bundle(picked.name)), _no_chestnut_pick():
        self.assertEqual(helpers.modeld_pkl_path(False), picked)

  def test_no_selection_keeps_the_hardware_default(self):
    with tempfile.TemporaryDirectory() as root:
      with self._ctx(root), _patch_bundle(None), _no_chestnut_pick():
        self.assertEqual(helpers.modeld_pkl_path(False).name, "amd_driving_tinygrad.pkl")

  def test_selection_whose_file_is_missing_falls_back(self):
    with tempfile.TemporaryDirectory() as root:
      with self._ctx(root), _patch_bundle(_Bundle("driving_never_downloaded_tinygrad.pkl")), _no_chestnut_pick():
        self.assertEqual(helpers.modeld_pkl_path(False).name, "amd_driving_tinygrad.pkl")

  def test_amd_prefers_the_locally_compiled_sibling(self):
    with tempfile.TemporaryDirectory() as root:
      name = "driving_cd210_model_february_03_2026_tinygrad.pkl"
      (Path(root) / name).write_bytes(b"qcom-pkl")
      amd = Path(root) / f"amd_{name}"
      amd.write_bytes(b"amd-pkl")
      with self._ctx(root), _patch_bundle(_Bundle(name)):
        self.assertEqual(helpers.modeld_pkl_path(False), amd)

  def test_cpu_build_uses_the_downloaded_file_as_is(self):
    with tempfile.TemporaryDirectory() as root:
      name = "driving_cd210_model_february_03_2026_tinygrad.pkl"
      picked = Path(root) / name
      picked.write_bytes(b"pkl")
      with self._ctx(root, device="CPU"), _patch_bundle(_Bundle(name)):
        self.assertEqual(helpers.modeld_pkl_path(False), picked)

  def test_chestnut_slot_ignores_the_desktop_selection(self):
    with tempfile.TemporaryDirectory() as root:
      picked = Path(root) / "driving_cd210_model_february_03_2026_tinygrad.pkl"
      picked.write_bytes(b"stub")
      with self._ctx(root), _patch_bundle(_Bundle(picked.name)):
        self.assertEqual(helpers.modeld_pkl_path(True).name, "big_driving_tinygrad.pkl")

  def test_unreadable_selection_never_breaks_modeld(self):
    with tempfile.TemporaryDirectory() as root:
      with self._ctx(root), patch("openpilot.sunnypilot.models.helpers.get_active_bundle",
                                  side_effect=RuntimeError("param corrupted")), _no_chestnut_pick():
        self.assertEqual(helpers.modeld_pkl_path(False).name, "amd_driving_tinygrad.pkl")

  def test_bundle_without_artifacts_falls_back(self):
    with tempfile.TemporaryDirectory() as root:
      with self._ctx(root), _patch_bundle(_Bundle("")), _no_chestnut_pick():
        self.assertEqual(helpers.modeld_pkl_path(False).name, "amd_driving_tinygrad.pkl")

  def test_settings_pick_outranks_the_launcher_big_model_switch(self):
    with tempfile.TemporaryDirectory() as root:
      big = Path(root) / helpers.BIG_MODEL_ON_IGPU_NAME
      big.write_bytes(b"stub")
      picked = Path(root) / "driving_cd210_model_february_03_2026_tinygrad.pkl"
      picked.write_bytes(b"stub")
      big_env = {"SUNNYPILOT_MODEL_DEVICE": "AMD", helpers.BIG_MODEL_ON_IGPU_ENV: "1"}
      with self._ctx(root), patch.dict(os.environ, big_env), _patch_bundle(_Bundle(picked.name)), _no_chestnut_pick():
        self.assertEqual(helpers.modeld_pkl_path(False), picked)

      with self._ctx(root), patch.dict(os.environ, big_env), _patch_bundle(None), _no_chestnut_pick():
        self.assertEqual(helpers.modeld_pkl_path(False), big)


if __name__ == "__main__":
  unittest.main()


class TestLoadModeldJitsFallsBack(unittest.TestCase):

  def test_unloadable_pick_falls_back_to_the_default(self):
    picked = Path("/models/picked.pkl")
    default = Path("/models/default.pkl")
    with patch.object(helpers, "modeld_pkl_path", return_value=picked), \
         patch.object(helpers, "default_modeld_pkl_path", return_value=default), \
         patch.object(helpers, "open_file_chunked", side_effect=lambda p: p), \
         patch.object(helpers, "load_oob", side_effect=[FileNotFoundError("/dev/kgsl-3d0"), {"ok": True}]) as lo:
      jits, used = helpers.load_modeld_jits(False)
    self.assertEqual(jits, {"ok": True})
    self.assertEqual(used, default)
    self.assertEqual(lo.call_count, 2)

  def test_a_broken_default_still_raises(self):
    only = Path("/models/only.pkl")
    with patch.object(helpers, "modeld_pkl_path", return_value=only), \
         patch.object(helpers, "default_modeld_pkl_path", return_value=only), \
         patch.object(helpers, "open_file_chunked", side_effect=lambda p: p), \
         patch.object(helpers, "load_oob", side_effect=RuntimeError("corrupt")):
      with self.assertRaises(RuntimeError):
        helpers.load_modeld_jits(False)

  def test_a_good_pick_is_used_directly(self):
    picked = Path("/models/picked.pkl")
    with patch.object(helpers, "modeld_pkl_path", return_value=picked), \
         patch.object(helpers, "open_file_chunked", side_effect=lambda p: p), \
         patch.object(helpers, "load_oob", return_value={"ok": 1}) as lo:
      jits, used = helpers.load_modeld_jits(False)
    self.assertEqual(used, picked)
    self.assertEqual(lo.call_count, 1)


class TestBuildOwnsItsArtifact(unittest.TestCase):

  def _ctx(self, models_dir, device="AMD"):
    return (patch.object(helpers, "PC", True),
            patch.object(helpers, "MODELS_DIR", Path(models_dir)),
            patch.dict(os.environ, {"SUNNYPILOT_MODEL_DEVICE": device}))

  def test_stock_artifact_lives_outside_the_slot(self):
    p1, p2, p3 = self._ctx("/models")
    with p1, p2, p3, _no_chestnut_pick():
      stock = helpers.stock_modeld_pkl_path()
      legacy = helpers.legacy_default_pkl_path()
    self.assertEqual(stock.name, "amd_driving_stock_tinygrad.pkl")
    self.assertEqual(legacy.name, "amd_driving_tinygrad.pkl")
    self.assertNotEqual(stock, legacy)

  def test_default_prefers_the_build_artifact(self):
    with tempfile.TemporaryDirectory() as root:
      (Path(root) / "amd_driving_stock_tinygrad.pkl").write_bytes(b"stock")
      (Path(root) / "amd_driving_tinygrad.pkl").write_bytes(b"legacy")
      p1, p2, p3 = self._ctx(root)
      with p1, p2, p3, _no_chestnut_pick():
        self.assertEqual(helpers.modeld_pkl_path(False).name, "amd_driving_stock_tinygrad.pkl")

  def test_legacy_install_still_works(self):
    with tempfile.TemporaryDirectory() as root:
      (Path(root) / "amd_driving_tinygrad.pkl").write_bytes(b"legacy")
      p1, p2, p3 = self._ctx(root)
      with p1, p2, p3, _no_chestnut_pick():
        self.assertEqual(helpers.modeld_pkl_path(False).name, "amd_driving_tinygrad.pkl")


class TestRunningModelSwap(unittest.TestCase):

  def test_unchanged_pick_reports_nothing(self):
    current = Path("/models/amd_driving_tinygrad.pkl")
    with patch.object(helpers, "modeld_pkl_path", return_value=current):
      self.assertIsNone(helpers.model_swap_target(current, False))

  def test_changed_pick_is_reported(self):
    current = Path("/models/amd_driving_tinygrad.pkl")
    picked = Path("/models/amd_tsfdom.pkl")
    with patch.object(helpers, "modeld_pkl_path", return_value=picked):
      self.assertEqual(helpers.model_swap_target(current, False), picked)

  def test_chestnut_slot_never_swaps(self):
    big = Path("/models/big_driving_tinygrad.pkl")
    with patch.object(helpers, "selected_bundle_pkl", return_value=Path("/models/pick.pkl")), \
         patch.object(helpers, "default_modeld_pkl_path", return_value=big):
      self.assertIsNone(helpers.model_swap_target(big, True))


class TestBigModelSlotIsTheSwitch(unittest.TestCase):

  def _ctx(self, root, device="AMD"):
    return (patch.object(helpers, "PC", True),
            patch.object(helpers, "MODELS_DIR", Path(root)),
            patch.object(helpers.Paths, "model_root", return_value=root),
            patch.dict(os.environ, {"SUNNYPILOT_MODEL_DEVICE": device}))

  def _bundle(self, ref):
    from types import SimpleNamespace
    return SimpleNamespace(ref=ref, internalName=ref, displayName=ref)

  def _selected(self, chestnut_ref, small_ref=None):
    from types import SimpleNamespace
    def fake(params=None, source="qcom"):
      ref = chestnut_ref if source == "chestnut" else small_ref
      return SimpleNamespace(ref=ref, internalName=ref or "", displayName=ref or "") if ref else None
    return patch("openpilot.sunnypilot.models.helpers.get_selected_bundle", side_effect=fake)

  def test_big_pick_outranks_the_small_pick(self):
    with tempfile.TemporaryDirectory() as root:
      big = Path(root) / helpers.BIG_MODEL_ON_IGPU_NAME
      big.write_bytes(b"big")
      small = Path(root) / "amd_tsfdom.pkl"
      small.write_bytes(b"small")
      with self._ctx(root)[0], self._ctx(root)[1], self._ctx(root)[2], self._ctx(root)[3], \
           self._selected(helpers.LOCAL_BIG_REF, helpers.LOCAL_SMALL_REF_PREFIX + small.name):
        self.assertEqual(helpers.modeld_pkl_path(False), big)

  def test_clearing_the_big_pick_returns_to_the_small_model(self):
    with tempfile.TemporaryDirectory() as root:
      (Path(root) / helpers.BIG_MODEL_ON_IGPU_NAME).write_bytes(b"big")
      small = Path(root) / "amd_tsfdom.pkl"
      small.write_bytes(b"small")
      with self._ctx(root)[0], self._ctx(root)[1], self._ctx(root)[2], self._ctx(root)[3], \
           self._selected(None, helpers.LOCAL_SMALL_REF_PREFIX + small.name):
        self.assertEqual(helpers.modeld_pkl_path(False), small)

  def test_catalogue_chestnut_bundle_does_not_hijack_the_slot(self):
    with tempfile.TemporaryDirectory() as root:
      (Path(root) / helpers.BIG_MODEL_ON_IGPU_NAME).write_bytes(b"big")
      small = Path(root) / "amd_tsfdom.pkl"
      small.write_bytes(b"small")
      with self._ctx(root)[0], self._ctx(root)[1], self._ctx(root)[2], self._ctx(root)[3], \
           self._selected("0394d7284cfc229f772ee4fa73ff724ba8245110", helpers.LOCAL_SMALL_REF_PREFIX + small.name):
        self.assertEqual(helpers.modeld_pkl_path(False), small)

  def test_missing_big_artifact_falls_back_to_the_small_model(self):
    with tempfile.TemporaryDirectory() as root:
      small = Path(root) / "amd_tsfdom.pkl"
      small.write_bytes(b"small")
      with self._ctx(root)[0], self._ctx(root)[1], self._ctx(root)[2], self._ctx(root)[3], \
           self._selected(helpers.LOCAL_BIG_REF, helpers.LOCAL_SMALL_REF_PREFIX + small.name):
        self.assertEqual(helpers.modeld_pkl_path(False), small)
