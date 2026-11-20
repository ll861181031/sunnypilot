"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import contextlib
import json
import os
import pathlib
import time

from openpilot.common.hardware import PC
from openpilot.common.hardware.hw import Paths
from openpilot.selfdrive.ui.ui_state import ui_state, ChestnutState
from openpilot.sunnypilot.models.fetcher import get_cached_bundles
from openpilot.sunnypilot.models.helpers import (REQUIRED_JSON_VERSION, get_active_source,
                                                   get_selected_bundle, resolve_bundle_by_ref)
from openpilot.sunnypilot.models.model_name import DEFAULT_BIG_MODEL, DEFAULT_MODEL

# Desktop AMD iGPU runs a locally compiled big model through modeld's native path,
# which the SP bundle selector knows nothing about. The ref is shared with modeld
# (helpers.LOCAL_BIG_REF): on the desktop the Big Model slot is the big/small
# switch, so both sides must agree on what a "big pick" looks like.
from openpilot.selfdrive.modeld.helpers import LOCAL_BIG_REF as LOCAL_BUNDLE_REF
IGPU_BIG_ENV = "SUNNYPILOT_BIG_MODEL_ON_IGPU"
IGPU_BIG_NAME = "amd_driving_big_tinygrad.pkl"
_NON_DRIVING_PREFIXES = ("amd_dm_warp_", "amd_dmonitoring_")


def model_cache_size_mb() -> float:
  """Bytes on disk under the model cache directory, in MB."""
  model_root = Paths.model_root()
  total = 0
  if os.path.isdir(model_root):
    for name in os.listdir(model_root):
      with contextlib.suppress(OSError):
        total += os.path.getsize(os.path.join(model_root, name))
  return total / (1024 ** 2)


def active_source() -> str:
  return get_active_source(chestnut=ui_state.chestnut_present,
                           chestnut_active=ui_state.chestnut_active, chestnut_loading=ui_state.chestnut_loading,
                           offroad=ui_state.is_offroad())


def local_bundle():
  """A synthetic bundle for the locally compiled iGPU artifact.

  The artifact is produced by compile_modeld.py, never fetched from the model
  server, so it has no bundle in the catalogue. Surfacing it here lets the
  selector show what modeld is actually reading instead of pretending the
  bundle slots are the whole story. ref == LOCAL_BUNDLE_REF marks it as local."""
  from openpilot.selfdrive.modeld.helpers import big_model_artifact
  artifact = big_model_artifact()
  if artifact is None:
    return None
  from openpilot.cereal import custom
  size = 0
  with contextlib.suppress(OSError):
    size = artifact.stat().st_size
  b = custom.ModelManagerSP.ModelBundle.new_message()
  b.index = 0
  b.internalName = artifact.name
  b.displayName = f"{igpu_display_name()} ({size / (1024 ** 2):.0f} MB)"
  b.ref = LOCAL_BUNDLE_REF
  b.environment = "local"
  b.runner = "tinygrad"
  b.is20hz = True
  b.generation = 0
  b.minimumSelectorVersion = REQUIRED_JSON_VERSION
  b.status = "downloaded"
  return b


def _artifact_on_disk(root: str, name: str) -> bool:
  """True when this artifact is fully present under `root`.

  Chunked artifacts only keep a manifest plus the chunk files, so the plain path
  is not enough - and a manifest without its chunks must not count as offline.
  """
  from openpilot.selfdrive.modeld.helpers import artifact_on_disk
  return artifact_on_disk(pathlib.Path(root) / name)


def bundle_is_offline(bundle) -> bool:
  """True when every artifact of this bundle is already on disk.

  The selector only offers what can be loaded right now: picking a model must
  never start a download. On the desktop AMD backend the catalog pkl is built for
  the comma 3X, so only a locally compiled ``amd_`` sibling counts as usable -
  offering the raw file would let the user pick something that cannot run.
  """
  names = [m.artifact.fileName for m in bundle.models if m.artifact.fileName]
  if not names:
    return False
  root = Paths.model_root()
  desktop_amd = PC and os.getenv("SUNNYPILOT_MODEL_DEVICE", "CPU").upper() == "AMD"
  for name in names:
    if desktop_amd:
      if not _artifact_on_disk(root, f"amd_{name}"):
        return False
    elif not _artifact_on_disk(root, name):
      return False
  return True


def bundles_for_source(source: str):
  bundles = list(get_cached_bundles(ui_state.params, source) if source != active_source()
                 else ui_state.sm["modelManagerSP"].availableBundles)
  # Only offer models that are already on disk. Picking a model must never start
  # a download - anything still missing is hidden instead of queued.
  bundles = [b for b in bundles if b.ref == LOCAL_BUNDLE_REF or bundle_is_offline(b)]
  # The local build is a big-model artifact, so it belongs in the big slot's list.
  if source == "chestnut" and (local := local_bundle()) is not None:
    bundles = [local] + [b for b in bundles if b.ref != LOCAL_BUNDLE_REF]
  if source == "qcom":
    # Every locally compiled small build gets its own entry, so several can be
    # kept side by side and switched. The default slot already represents one of
    # them, so it is not repeated here. local_small_bundles() already skips the
    # default artifact and any copy of the same model.
    bundles = local_small_bundles() + bundles
  return bundles


def default_model(source: str) -> str:
  return DEFAULT_BIG_MODEL if source == 'chestnut' else DEFAULT_MODEL


def _sidecar_meta(artifact: pathlib.Path) -> dict:
  """Metadata compile_modeld writes next to an artifact (empty when absent)."""
  with contextlib.suppress(OSError, ValueError):
    meta = json.loads(artifact.with_suffix(".json").read_text())
    if isinstance(meta, dict):
      return meta
  return {}


def _model_identity(meta: dict) -> tuple[str, str, str] | None:
  """Which model an artifact was compiled from: (run, step, export ref prefix).

  Older or hand-written sidecars only carry model_checkpoint, so the fields are
  derived from it when the individual keys are missing. hidden_state is not part
  of the identity: it is not recorded everywhere and the same export is what the
  user is choosing between.
  """
  parts = str(meta.get("model_checkpoint") or "").split("/")
  run = str(meta.get("train_run") or (parts[0] if len(parts) > 0 else ""))
  step = str(meta.get("train_step") or (parts[1] if len(parts) > 1 else ""))
  ref = str(meta.get("export_ref") or (parts[2] if len(parts) > 2 else ""))
  if not (run and step and len(ref) >= 8):
    return None
  return run, step, ref[:8]


_STOCK_IDENTITY: tuple[str, str, str] | None = None
_STOCK_IDENTITY_KEY: tuple | None = None


def stock_model_identity() -> tuple[str, str, str] | None:
  """Identity of the model the build compiles from the repo ONNX (the default).

  Read from the build-owned artifact's sidecar, falling back to the legacy
  default artifact. Cached on mtime: the settings page asks every frame.
  """
  global _STOCK_IDENTITY, _STOCK_IDENTITY_KEY
  from openpilot.selfdrive.modeld.helpers import legacy_default_pkl_path, stock_modeld_pkl_path
  candidates = (stock_modeld_pkl_path(), legacy_default_pkl_path())

  def sidecar_mtime(path: pathlib.Path) -> int | None:
    with contextlib.suppress(OSError):
      return path.with_suffix(".json").stat().st_mtime_ns
    return None

  key = tuple((str(p), sidecar_mtime(p)) for p in candidates)
  if key == _STOCK_IDENTITY_KEY:
    return _STOCK_IDENTITY
  identity = None
  for candidate in candidates:
    if (identity := _model_identity(_sidecar_meta(candidate))) is not None:
      break
  _STOCK_IDENTITY, _STOCK_IDENTITY_KEY = identity, key
  return identity


def _artifact_stem(artifact: pathlib.Path, meta: dict) -> str:
  """Model name recorded for this artifact, stripped of build-name noise."""
  name = str(meta.get("artifact") or artifact.name)
  stem = pathlib.PurePath(name).stem
  return stem.removeprefix("amd_").removeprefix("driving_").removesuffix("_tinygrad")


def local_artifact_label(artifact: pathlib.Path) -> str:
  """Readable model name for one locally compiled artifact.

  A compile of the repo ONNX is the catalogue default and is reported under that
  name, so the settings list says "CD210" rather than the file stem. Anything
  else keeps its own name: the sidecar records the artifact it was compiled as
  (e.g. amd_tsfdom.pkl -> TSFDOM), which survives a rename into the default slot.
  """
  meta = _sidecar_meta(artifact)
  identity = _model_identity(meta)
  if identity is not None:
    return DEFAULT_MODEL if identity == stock_model_identity() else _artifact_stem(artifact, meta).upper()
  # No provenance recorded. Only an in-place build of the default slot is known
  # to be the stock model; any other nameless artifact is reported by its stem.
  from openpilot.selfdrive.modeld.helpers import legacy_default_pkl_path, stock_modeld_pkl_path
  in_default_slot = artifact in (legacy_default_pkl_path(), stock_modeld_pkl_path())
  if in_default_slot and (not meta or str(meta.get("artifact") or "") == artifact.name):
    return DEFAULT_MODEL
  return _artifact_stem(artifact, meta).upper() or artifact.stem.upper()


def _model_key(artifact: pathlib.Path) -> tuple:
  """Identity used to notice two files holding the same model."""
  identity = _model_identity(_sidecar_meta(artifact))
  return identity if identity is not None else ("label", local_artifact_label(artifact))


def local_small_artifacts() -> list[pathlib.Path]:
  """Every locally compiled small artifact that could actually be loaded.

  The build chunks its artifacts (`chunk_file` removes the plain file and leaves
  a manifest plus chunks), so a plain `amd_*.pkl` glob would miss exactly the
  builds the build system produces. Chunk manifests are mapped back to the
  logical file name, and `artifact_on_disk` rejects an incomplete chunk set.
  """
  from openpilot.selfdrive.modeld.helpers import MODELS_DIR, artifact_on_disk
  root = pathlib.Path(MODELS_DIR)
  found = list(root.glob("amd_*.pkl")) + [m.with_suffix("") for m in root.glob("amd_*.pkl.chunkmanifest")]
  # amd_dm_warp_* and amd_dmonitoring_* live in the same folder but are driver
  # monitoring artifacts, not driving models - listing them would let the user
  # pick something modeld can never load as a driving model.
  names = sorted({p.name for p in found
                  if not p.name.startswith(_NON_DRIVING_PREFIXES) and artifact_on_disk(p)})
  return [root / name for name in names]


def local_small_artifact_label() -> str | None:
  """Label for the small artifact modeld uses by default, when it is custom.

  `default_model_name()` used to answer from the hardcoded catalogue default, so a
  machine running a self-compiled small model still displayed "CD210 (Default)" -
  the row named one model while modeld loaded another.
  """
  from openpilot.selfdrive.modeld.helpers import artifact_on_disk, big_model_on_igpu_path, default_modeld_pkl_path
  artifact = default_modeld_pkl_path(False)
  # The iGPU big model is reported on the big row; naming it here would claim the
  # small slot is driving something it is not.
  if not artifact_on_disk(artifact) or artifact == big_model_on_igpu_path():
    return None
  label = local_artifact_label(artifact)
  if label == DEFAULT_MODEL:
    return None  # stock build: the catalogue default name is accurate
  size = 0
  with contextlib.suppress(OSError):
    size = artifact.stat().st_size
  return f"{label} ({size / (1024 ** 2):.0f} MB, local)"


def local_small_bundles() -> list:
  """One synthetic bundle per locally compiled small artifact.

  The small slot used to be a single fixed file, so keeping two builds meant
  overwriting one with the other and the selector had nothing to switch between.
  Each ``amd_*.pkl`` now becomes its own entry; the ref carries the file name so
  modeld can resolve the pick without any catalogue round trip.

  The default artifact is skipped (the "Default" row already is that pick) and so
  is any copy of the same model, which is what a rebuild of the stock model left
  behind next to an older build.
  """
  from openpilot.cereal import custom
  from openpilot.selfdrive.modeld.helpers import LOCAL_SMALL_REF_PREFIX, artifact_on_disk, default_modeld_pkl_path
  default_artifact = default_modeld_pkl_path(False)
  # The default artifact is chunked by the build, so `is_file()` is not enough.
  default_key = _model_key(default_artifact) if artifact_on_disk(default_artifact) else None
  out, seen = [], set()
  for artifact in local_small_artifacts():
    if artifact == default_artifact:
      continue
    key = _model_key(artifact)
    if key == default_key or key in seen:
      continue
    seen.add(key)
    size = 0
    with contextlib.suppress(OSError):
      size = artifact.stat().st_size
    b = custom.ModelManagerSP.ModelBundle.new_message()
    b.index = 0
    b.internalName = artifact.name
    b.displayName = f"{local_artifact_label(artifact)} ({size / (1024 ** 2):.0f} MB, local)"
    b.ref = LOCAL_SMALL_REF_PREFIX + artifact.name
    b.environment = "local"
    # "stock" keeps the road model on openpilot's modeld, which is the process
    # that knows how to resolve a local ref (modeld_v2 only looks in the
    # download cache and would assert "No driving pkl found"). The runner field
    # picks the process, it does not describe the pkl format.
    b.runner = "stock"
    b.is20hz = True
    b.generation = 0
    b.minimumSelectorVersion = REQUIRED_JSON_VERSION
    b.status = "downloaded"
    out.append(b)
  return out


def default_model_name(source: str) -> str:
  if source == "qcom" and (label := local_small_artifact_label()) is not None:
    return label
  return f"{default_model(source)} (Default)"


def default_choice_label(source: str) -> str:
  """Label of the "Default" entry in the model selector.

  On the desktop the Big Model slot doubles as the big/small switch, so its
  Default entry means "use the small model" - not the catalogue big model, which
  this device cannot run.
  """
  if source == "chestnut" and not ui_state.chestnut_present:
    return "Small model (Default)"
  return default_model_name(source)


def big_model_state() -> str | None:
  """'failed' | 'loading' | None, from the same state the icons render."""
  return {ChestnutState.UNCOMPILED: 'failed',
          ChestnutState.FAILED: 'failed',
          ChestnutState.LOADING: 'loading'}.get(ui_state.chestnut_state)


_ARTIFACT_META: dict | None = None
_ARTIFACT_META_KEY: str | None = None


def igpu_artifact_meta() -> dict:
  """Metadata for the local iGPU artifact, read from the sidecar compile_modeld
  writes next to it. Cached: the UI calls this every frame and the sidecar only
  changes when someone recompiles."""
  global _ARTIFACT_META, _ARTIFACT_META_KEY
  from openpilot.selfdrive.modeld.helpers import big_model_artifact
  artifact = big_model_artifact()
  if artifact is None:
    return {}
  path = os.path.splitext(str(artifact))[0] + ".json"
  if _ARTIFACT_META_KEY == path and _ARTIFACT_META is not None:
    return _ARTIFACT_META
  import json
  meta: dict = {}
  with contextlib.suppress(OSError, ValueError):
    with open(path) as f:
      meta = json.load(f)
  _ARTIFACT_META, _ARTIFACT_META_KEY = meta, path
  return meta


def igpu_display_name() -> str | None:
  """Human name for the local artifact: the supercombo export, not the filename.

  Falls back to the filename when the sidecar is missing (older builds)."""
  from openpilot.selfdrive.modeld.helpers import big_model_artifact
  artifact = big_model_artifact()
  if artifact is None:
    return None
  ref = (igpu_artifact_meta().get("export_ref") or "")[:8]
  if not ref:
    return artifact.name
  hidden = igpu_artifact_meta().get("hidden_state") or ""
  suffix = f" h{hidden}" if hidden else ""
  return f"big_driving_supercombo @{ref}{suffix}"


def igpu_big_artifact() -> str | None:
  """Name of the locally compiled big model on the desktop AMD iGPU, or None.

  This mirrors modeld_pkl_path(): the switch only applies after the AMD device
  gate passes, and the artifact must actually exist on disk. When modeld falls
  back to the small model the UI must say so, not keep claiming the big one."""
  from openpilot.common.hardware import PC
  if not PC:
    return None
  if os.getenv("SUNNYPILOT_MODEL_DEVICE") != "AMD":
    return None
  if os.getenv(IGPU_BIG_ENV) != "1":
    return None
  path = os.path.join(Paths.model_root(), IGPU_BIG_NAME)
  return IGPU_BIG_NAME if os.path.isfile(path) else None


def carrying_model() -> tuple[str | None, str | None, str | None]:
  """(source, internal name, display name) of what actually drives.

  Order mirrors modeld.helpers.modeld_pkl_path(): a Settings pick whose artifact
  is on disk wins, then the locally compiled big model, then the hardware
  default. Keeping the two in step matters - this row is the only place the user
  can see whether their pick actually took effect."""
  from openpilot.selfdrive.modeld.helpers import selected_big_pkl, selected_bundle_pkl
  # A local big pick in the Big Model slot means the big model drives; the small
  # pick stays stored and takes over again once the big pick is cleared.
  if (big := selected_big_pkl()) is not None:
    bundle = get_selected_bundle(ui_state.params, "chestnut")
    name = bundle.internalName if bundle else big.name
    display = bundle.displayName if bundle else igpu_display_name()
    return "igpu", name, display
  if selected_bundle_pkl() is not None:
    bundle = get_selected_bundle(ui_state.params, "qcom")
    if bundle:
      return "qcom", bundle.internalName, bundle.displayName
  if artifact := igpu_big_artifact():
    return "igpu", artifact, igpu_display_name()
  source = active_source()
  if source == "chestnut":
    bundle = get_selected_bundle(ui_state.params, "chestnut")
    if bundle:
      return "chestnut", bundle.internalName, bundle.displayName
    name = default_model_name("chestnut")
    return "chestnut", name, name
  if ui_state.chestnut_present:
    if get_selected_bundle(ui_state.params, "chestnut") is None:
      name = default_model_name("qcom")
      return "qcom", name, name
    return None, None, None
  bundle = get_selected_bundle(ui_state.params, "qcom")
  if bundle:
    return "qcom", bundle.internalName, bundle.displayName
  name = default_model_name("qcom")
  return "qcom", name, name


def _slot_bundle_name(source: str) -> str:
  bundle = get_selected_bundle(ui_state.params, source)
  return bundle.internalName if bundle else default_model_name(source)


def slot_entry(source: str) -> tuple[str, bool]:
  """(name, driving) for one bundle slot as the Model Status row shows it.

  driving means modeld loads that slot's pick. A local iGPU build bypasses both
  slots: modeld reads it as the big model, so the big slot reports it (and goes
  green) while the small slot stays idle. Naming the bundle there instead would
  make the status row contradict the Big Model row, which already reports the
  local artifact -- on this device no bundle is even downloaded."""
  carry_source, carry_internal, carry_display = carrying_model()
  if carry_source == "igpu":
    if source == "chestnut":
      return (carry_display or carry_internal or ""), True
    return _slot_bundle_name(source), False
  name = _slot_bundle_name(source)
  return name, source == carry_source and name == carry_internal


def queued_name(current_ref) -> str | None:
  ref = ui_state.params.get("ModelManager_DownloadRef")
  if ref and ref != current_ref:
    source_bundles = {source: bundles_for_source(source) for source in ("qcom", "chestnut")}
    if resolved := resolve_bundle_by_ref(ref, source_bundles):
      return resolved[0].internalName
  return None


def model_info() -> tuple[str, str, str]:
  """returns (active source, active model name, other model name)

  Names come from the params slots, never modelManagerSP.activeBundle — the
  manager republishes a tick after a chestnut change, so the stale bundle
  would flash the wrong model."""
  source = active_source()
  other = "qcom" if source == "chestnut" else "chestnut"
  active_bundle = get_selected_bundle(ui_state.params, source)
  other_bundle = get_selected_bundle(ui_state.params, other)

  active_name = active_bundle.displayName if active_bundle else default_model_name(source)
  other_name = other_bundle.displayName if other_bundle else default_model_name(other)
  return source, active_name, other_name


# mirrors the manager's ModelCache keys; the manager restamps them on a successful fetch
MODEL_SYNC_KEYS = ("ModelManager_LastSyncTime", "ModelManager_LastSyncTime_Chestnut")
MODEL_SYNC_TIMEOUT = 20.0


def refresh_model_list() -> None:
  # zeroing the sync keys makes the manager refetch each manifest on its next tick
  for key in MODEL_SYNC_KEYS:
    ui_state.params.put(key, 0)


def refresh_in_progress(started_at: float | None) -> bool:
  """Whether a user refresh is still outstanding. A failed fetch never restamps the
  sync keys, so the spinner is bounded by MODEL_SYNC_TIMEOUT rather than sticking."""
  if started_at is None or time.monotonic() - started_at > MODEL_SYNC_TIMEOUT:
    return False
  return not all(ui_state.params.get(key) for key in MODEL_SYNC_KEYS)
