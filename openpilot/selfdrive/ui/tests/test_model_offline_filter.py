
import os
from types import SimpleNamespace
from unittest.mock import patch

from openpilot.selfdrive.ui.sunnypilot import model_info


def _bundle(*file_names):
  return SimpleNamespace(models=[SimpleNamespace(artifact=SimpleNamespace(fileName=n)) for n in file_names])


def _offline(root, bundle, *, device="CPU", pc=True):
  with patch.object(model_info, "PC", pc), \
       patch.dict(os.environ, {"SUNNYPILOT_MODEL_DEVICE": device}), \
       patch.object(model_info.Paths, "model_root", return_value=str(root)):
    return model_info.bundle_is_offline(bundle)


class TestPlainArtifact:
  def test_present_file_is_offline(self, tmp_path):
    (tmp_path / "driving_x_tinygrad.pkl").write_bytes(b"pkl")
    assert _offline(tmp_path, _bundle("driving_x_tinygrad.pkl"))

  def test_missing_file_is_not_offline(self, tmp_path):
    assert not _offline(tmp_path, _bundle("driving_x_tinygrad.pkl"))

  def test_bundle_without_artifacts_is_not_offline(self, tmp_path):
    assert not _offline(tmp_path, _bundle(""))


class TestChunkedArtifact:
  def _write_chunks(self, root, name, num):
    (root / f"{name}.chunkmanifest").write_text(str(num))
    for i in range(1, num + 1):
      (root / f"{name}.chunk{i:02d}of{num:02d}").write_bytes(b"c")

  def test_complete_chunk_set_is_offline(self, tmp_path):
    self._write_chunks(tmp_path, "driving_x_tinygrad.pkl", 3)
    assert _offline(tmp_path, _bundle("driving_x_tinygrad.pkl"))

  def test_manifest_without_chunks_is_not_offline(self, tmp_path):
    (tmp_path / "driving_x_tinygrad.pkl.chunkmanifest").write_text("2")
    assert not _offline(tmp_path, _bundle("driving_x_tinygrad.pkl"))

  def test_partially_downloaded_is_not_offline(self, tmp_path):
    self._write_chunks(tmp_path, "driving_x_tinygrad.pkl", 3)
    (tmp_path / "driving_x_tinygrad.pkl.chunk02of03").unlink()
    assert not _offline(tmp_path, _bundle("driving_x_tinygrad.pkl"))


class TestDesktopAmdNeedsTheCompiledSibling:

  def test_raw_catalog_pkl_alone_is_not_offline_on_amd(self, tmp_path):
    (tmp_path / "driving_x_tinygrad.pkl").write_bytes(b"qcom")
    assert not _offline(tmp_path, _bundle("driving_x_tinygrad.pkl"), device="AMD")

  def test_locally_compiled_sibling_makes_it_offline(self, tmp_path):
    (tmp_path / "driving_x_tinygrad.pkl").write_bytes(b"qcom")
    (tmp_path / "amd_driving_x_tinygrad.pkl").write_bytes(b"amd")
    assert _offline(tmp_path, _bundle("driving_x_tinygrad.pkl"), device="AMD")

  def test_cpu_build_accepts_the_plain_file(self, tmp_path):
    (tmp_path / "driving_x_tinygrad.pkl").write_bytes(b"pkl")
    assert _offline(tmp_path, _bundle("driving_x_tinygrad.pkl"), device="CPU")

  def test_non_pc_ignores_the_amd_rule(self, tmp_path):
    (tmp_path / "driving_x_tinygrad.pkl").write_bytes(b"pkl")
    assert _offline(tmp_path, _bundle("driving_x_tinygrad.pkl"), device="AMD", pc=False)


class TestSelectorOnlyOffersOfflineBundles:
  def test_online_bundles_are_filtered_out(self, tmp_path):
    offline = _bundle("driving_offline_tinygrad.pkl")
    online = _bundle("driving_online_tinygrad.pkl")
    (tmp_path / "driving_offline_tinygrad.pkl").write_bytes(b"pkl")
    offline.ref, online.ref = "ref-offline", "ref-online"

    with patch.object(model_info, "PC", True), \
         patch.dict(os.environ, {"SUNNYPILOT_MODEL_DEVICE": "CPU"}), \
         patch.object(model_info.Paths, "model_root", return_value=str(tmp_path)), \
         patch.object(model_info, "get_cached_bundles", return_value=[offline, online]), \
         patch.object(model_info, "active_source", return_value="other"), \
         patch.object(model_info, "local_bundle", return_value=None), \
         patch.object(model_info, "local_small_bundles", return_value=[]):
      got = model_info.bundles_for_source("qcom")

    assert [b.ref for b in got] == ["ref-offline"]


class TestDefaultEntryNamesTheRealArtifact:

  def _label(self, tmp_path, artifact_name, sidecar_artifact, size_mb=100, sidecar=None):
    (tmp_path / artifact_name).write_bytes(b"x" * (1024 * 1024 * size_mb))
    if sidecar is not None:
      (tmp_path / "amd_driving_tinygrad.json").write_text(__import__("json").dumps(sidecar))
    elif sidecar_artifact is not None:
      (tmp_path / "amd_driving_tinygrad.json").write_text(
        __import__("json").dumps({"artifact": sidecar_artifact}))
    with patch("openpilot.selfdrive.modeld.helpers.MODELS_DIR", tmp_path), \
         patch.dict(os.environ, {"SUNNYPILOT_MODEL_DEVICE": "AMD"}):
      return model_info.local_small_artifact_label()

  def test_custom_build_is_named_after_its_source_artifact(self, tmp_path):
    label = self._label(tmp_path, "amd_driving_tinygrad.pkl", "amd_tsfdom.pkl")
    assert label == "TSFDOM (100 MB, local)"

  def test_stock_build_keeps_the_catalogue_name(self, tmp_path):
    assert self._label(tmp_path, "amd_driving_tinygrad.pkl", "amd_driving_tinygrad.pkl") is None

  def test_missing_sidecar_keeps_the_catalogue_name(self, tmp_path):
    assert self._label(tmp_path, "amd_driving_tinygrad.pkl", None) is None

  def test_missing_artifact_returns_none(self, tmp_path):
    with patch("openpilot.selfdrive.modeld.helpers.MODELS_DIR", tmp_path), \
         patch.dict(os.environ, {"SUNNYPILOT_MODEL_DEVICE": "AMD"}):
      assert model_info.local_small_artifact_label() is None

  def test_default_entry_reports_the_local_build(self, tmp_path):
    (tmp_path / "amd_driving_tinygrad.pkl").write_bytes(b"x" * (1024 * 1024 * 100))
    (tmp_path / "amd_driving_tinygrad.json").write_text(
      __import__("json").dumps({"artifact": "amd_tsfdom.pkl"}))
    with patch("openpilot.selfdrive.modeld.helpers.MODELS_DIR", tmp_path), \
         patch.dict(os.environ, {"SUNNYPILOT_MODEL_DEVICE": "AMD"}):
      assert model_info.default_model_name("qcom") == "TSFDOM (100 MB, local)"
      assert model_info.default_model_name("chestnut").endswith("(Default)")


STOCK_SIDECAR = {
  "model_checkpoint": "6a7d09ad-bcc9-43bc-916d-29287e60cee2/200/a27b3122-733e-4a65-938b-acfebebbe5e8/100",
  "train_run": "6a7d09ad-bcc9-43bc-916d-29287e60cee2",
  "train_step": "200",
  "export_ref": "a27b3122-733e-4a65-938b-acfebebbe5e8",
  "hidden_state": "100",
}


class TestStockCopiesAreNamedCd210:

  def _write(self, tmp_path, name, meta):
    (tmp_path / name).write_bytes(b"x")
    (tmp_path / f"{name.rsplit('.', 1)[0]}.json").write_text(__import__("json").dumps(meta))

  def test_renamed_stock_copy_reads_as_cd210(self, tmp_path):
    self._write(tmp_path, "driving_tinygrad.pkl", STOCK_SIDECAR)   # stock reference
    self._write(tmp_path, "amd_stock.pkl", STOCK_SIDECAR | {"artifact": "amd_stock.pkl"})
    with patch("openpilot.selfdrive.modeld.helpers.MODELS_DIR", tmp_path):
      assert model_info.local_artifact_label(tmp_path / "amd_stock.pkl") == "CD210"

  def test_partial_sidecar_still_matches_by_checkpoint(self, tmp_path):
    self._write(tmp_path, "driving_tinygrad.pkl", STOCK_SIDECAR)
    self._write(tmp_path, "amd_driving_tinygrad.pkl",
                {"model_checkpoint": STOCK_SIDECAR["model_checkpoint"], "artifact": "amd_driving_tinygrad.pkl"})
    with patch("openpilot.selfdrive.modeld.helpers.MODELS_DIR", tmp_path):
      assert model_info.local_artifact_label(tmp_path / "amd_driving_tinygrad.pkl") == "CD210"

  def test_other_model_keeps_its_own_name(self, tmp_path):
    self._write(tmp_path, "driving_tinygrad.pkl", STOCK_SIDECAR)
    other = STOCK_SIDECAR | {"model_checkpoint": "aaaa/300/bbbbbbbb-0000-1111-2222-333333333333/100",
                             "train_run": "aaaa", "train_step": "300",
                             "export_ref": "bbbbbbbb-0000-1111-2222-333333333333",
                             "artifact": "amd_tsfdom.pkl"}
    self._write(tmp_path, "amd_tsfdom.pkl", other)
    with patch("openpilot.selfdrive.modeld.helpers.MODELS_DIR", tmp_path):
      assert model_info.local_artifact_label(tmp_path / "amd_tsfdom.pkl") == "TSFDOM"

  def test_copies_of_the_default_are_not_listed_twice(self, tmp_path):
    self._write(tmp_path, "amd_driving_tinygrad.pkl", STOCK_SIDECAR | {"artifact": "amd_driving_tinygrad.pkl"})
    self._write(tmp_path, "amd_stock.pkl", STOCK_SIDECAR | {"artifact": "amd_stock.pkl"})
    with patch("openpilot.selfdrive.modeld.helpers.MODELS_DIR", tmp_path), \
         patch.dict(os.environ, {"SUNNYPILOT_MODEL_DEVICE": "AMD"}):
      assert model_info.local_small_bundles() == []

  def test_a_different_model_is_still_listed(self, tmp_path):
    self._write(tmp_path, "amd_driving_tinygrad.pkl", STOCK_SIDECAR | {"artifact": "amd_driving_tinygrad.pkl"})
    other = STOCK_SIDECAR | {"train_run": "aaaa", "train_step": "300",
                             "export_ref": "bbbbbbbb-0000-1111-2222-333333333333",
                             "artifact": "amd_tsfdom.pkl"}
    self._write(tmp_path, "amd_tsfdom.pkl", other)
    with patch("openpilot.selfdrive.modeld.helpers.MODELS_DIR", tmp_path), \
         patch.dict(os.environ, {"SUNNYPILOT_MODEL_DEVICE": "AMD"}):
      bundles = model_info.local_small_bundles()
    assert [b.ref for b in bundles] == ["local:igpu-small:amd_tsfdom.pkl"]
    assert bundles[0].displayName.startswith("TSFDOM")


class TestMultipleLocalSmallModels:

  def _models(self, tmp_path, *names):
    for n in names:
      (tmp_path / n).write_bytes(b"x")
    with patch("openpilot.selfdrive.modeld.helpers.MODELS_DIR", tmp_path):
      return model_info.local_small_artifacts()

  def test_each_driving_artifact_becomes_its_own_entry(self, tmp_path):
    got = self._models(tmp_path, "amd_driving_tinygrad.pkl", "amd_tsfdom.pkl")
    assert [p.name for p in got] == ["amd_driving_tinygrad.pkl", "amd_tsfdom.pkl"]

  def test_driver_monitoring_artifacts_are_not_driving_models(self, tmp_path):
    got = self._models(tmp_path, "amd_driving_tinygrad.pkl", "amd_dm_warp_1344x760_tinygrad.pkl",
                       "amd_dmonitoring_model_tinygrad.pkl")
    assert [p.name for p in got] == ["amd_driving_tinygrad.pkl"]

  def test_bundle_ref_carries_the_file_name(self, tmp_path):
    self._models(tmp_path, "amd_tsfdom.pkl")
    with patch("openpilot.selfdrive.modeld.helpers.MODELS_DIR", tmp_path):
      bundles = model_info.local_small_bundles()
    assert len(bundles) == 1
    assert bundles[0].ref == "local:igpu-small:amd_tsfdom.pkl"
    assert bundles[0].internalName == "amd_tsfdom.pkl"


class TestLocalRefResolvesInModeld:
  def _resolve(self, tmp_path, ref):
    from openpilot.selfdrive.modeld import helpers
    with patch.object(helpers, "PC", True), \
         patch.dict(os.environ, {"SUNNYPILOT_MODEL_DEVICE": "AMD"}), \
         patch.object(helpers, "MODELS_DIR", tmp_path), \
         patch("openpilot.sunnypilot.models.helpers.get_active_bundle",
               return_value=SimpleNamespace(ref=ref, models=[])):
      return helpers.selected_bundle_pkl()

  def test_local_ref_resolves_to_the_named_file(self, tmp_path):
    (tmp_path / "amd_tsfdom.pkl").write_bytes(b"x")
    got = self._resolve(tmp_path, "local:igpu-small:amd_tsfdom.pkl")
    assert got == tmp_path / "amd_tsfdom.pkl"

  def test_missing_local_file_falls_back(self, tmp_path):
    assert self._resolve(tmp_path, "local:igpu-small:gone.pkl") is None


class TestLocalBundleStaysOnTheStockRunner:

  def test_local_bundle_uses_the_stock_runner(self, tmp_path):
    (tmp_path / "amd_tsfdom.pkl").write_bytes(b"x")
    (tmp_path / "amd_tsfdom.json").write_text(
      __import__("json").dumps({"artifact": "amd_tsfdom.pkl"}))
    with patch("openpilot.selfdrive.modeld.helpers.MODELS_DIR", tmp_path), \
         patch.dict(os.environ, {"SUNNYPILOT_MODEL_DEVICE": "AMD"}):
      bundles = model_info.local_small_bundles()
    assert len(bundles) == 1
    assert bundles[0].runner == "stock"


class TestChunkedLocalArtifacts:

  def _chunks(self, root, name, num=2):
    (root / f"{name}.chunkmanifest").write_text(str(num))
    for i in range(1, num + 1):
      (root / f"{name}.chunk{i:02d}of{num:02d}").write_bytes(b"c")

  def test_chunked_artifact_is_listed(self, tmp_path):
    self._chunks(tmp_path, "amd_tsfdom.pkl")
    (tmp_path / "amd_tsfdom.json").write_text(__import__("json").dumps({"artifact": "amd_tsfdom.pkl"}))
    with patch("openpilot.selfdrive.modeld.helpers.MODELS_DIR", tmp_path), \
         patch.dict(os.environ, {"SUNNYPILOT_MODEL_DEVICE": "AMD"}):
      names = [p.name for p in model_info.local_small_artifacts()]
    assert names == ["amd_tsfdom.pkl"]

  def test_incomplete_chunk_set_is_hidden(self, tmp_path):
    self._chunks(tmp_path, "amd_tsfdom.pkl")
    (tmp_path / "amd_tsfdom.pkl.chunk02of02").unlink()
    with patch("openpilot.selfdrive.modeld.helpers.MODELS_DIR", tmp_path), \
         patch.dict(os.environ, {"SUNNYPILOT_MODEL_DEVICE": "AMD"}):
      assert model_info.local_small_artifacts() == []
