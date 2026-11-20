import io
import json
import os
import pickle
import shutil
import struct
import tempfile
from pathlib import Path

from openpilot.common.file_chunker import get_chunk_name, get_manifest_path, open_file_chunked
from openpilot.common.hardware import PC
from openpilot.common.hardware.hw import Paths
from openpilot.common.hardware.usb import CHESTNUT_USB_PRODUCT, USB_DEVICES_PATH, is_chestnut_usb_id

MODELS_DIR = Path(__file__).resolve().parent / 'models'
TG_INPUT_DEVICES_PATH = MODELS_DIR / 'tg_input_devices.json'
CHESTNUT_POWERED_VOLTAGE = 5000
CHESTNUT_PCIE_READY = 0x78
# Opt-in: run the big (chestnut-class) driving model on the desktop AMD iGPU.
# The artifact must be compiled for this GPU arch by tools/compile_modeld.py;
# without the env var or without the file, modeld keeps using the small model.
BIG_MODEL_ON_IGPU_ENV = "SUNNYPILOT_BIG_MODEL_ON_IGPU"
BIG_MODEL_ON_IGPU_NAME = "amd_driving_big_tinygrad.pkl"
# Synthetic ref prefix for a locally compiled small artifact. The suffix is the
# file name, so the pick resolves to MODELS_DIR/<name> without a catalogue entry.
LOCAL_SMALL_REF_PREFIX = "local:igpu-small:"
# Ref of the locally compiled big model. On the desktop the Big Model slot is
# the big/small switch: this ref means "drive with the big model", and removing
# the pick means "back to the small model".
LOCAL_BIG_REF = "local:igpu-big"


def get_tg_input_devices(process_name: str, chestnut: bool):
  with open(TG_INPUT_DEVICES_PATH) as f:
    return json.load(f)[process_name]['default' if not chestnut else 'chestnut']

def get_desktop_model_device() -> str:
  device = os.getenv("SUNNYPILOT_MODEL_DEVICE", "CPU").upper()
  if device not in ("CPU", "AMD"):
    raise ValueError(f"Unsupported SUNNYPILOT_MODEL_DEVICE: {device}")
  return device


def get_desktop_dm_device() -> str:
  device = os.getenv("SUNNYPILOT_DM_DEVICE", "CPU").upper()
  if device not in ("CPU", "AMD"):
    raise ValueError(f"Unsupported SUNNYPILOT_DM_DEVICE: {device}")
  return device


def dmonitoring_pkl_path():
  prefix = 'amd_' if PC and get_desktop_dm_device() == 'AMD' else ''
  return MODELS_DIR / f'{prefix}dmonitoring_model_tinygrad.pkl'


def dmonitoring_warp_path(width: int, height: int):
  prefix = 'amd_' if PC and get_desktop_dm_device() == 'AMD' else ''
  return MODELS_DIR / f'{prefix}dm_warp_{width}x{height}_tinygrad.pkl'


def artifact_on_disk(path: Path) -> bool:
  """True when modeld could actually open this artifact.

  Locally built artifacts are chunked by the build (`chunk_file` removes the
  plain file and leaves a manifest plus chunks), so `is_file()` is not enough:
  a manifest whose chunks are missing must not count, and a complete chunk set
  must.
  """
  if path.is_file():
    return True
  manifest = Path(get_manifest_path(path))
  if not manifest.is_file():
    return False
  try:
    num_chunks = int(manifest.read_text().strip())
  except (OSError, ValueError):
    return False
  return all(Path(get_chunk_name(path, i, num_chunks)).is_file() for i in range(num_chunks))


def big_model_on_igpu_path() -> Path:
  return Path(Paths.model_root()) / BIG_MODEL_ON_IGPU_NAME


# Name of the artifact the build system owns. It used to compile straight into
# the default slot (`driving_tinygrad.pkl`), which meant every rebuild of
# compile_modeld.py or the ONNX silently replaced whatever model was sitting
# there - including one the user had compiled and selected in Settings. The
# build now writes its own file and the default slot only falls back to the
# legacy name when the build artifact is absent.
STOCK_ARTIFACT_SUFFIX = 'driving_stock_tinygrad.pkl'


def stock_modeld_pkl_path() -> Path:
  """Build-owned road-model artifact (never a Settings slot)."""
  prefix = 'amd_' if PC and get_desktop_model_device() == "AMD" else ''
  return MODELS_DIR / f'{prefix}{STOCK_ARTIFACT_SUFFIX}'


def legacy_default_pkl_path() -> Path:
  """Where the build used to compile the road model, before the split.

  Kept as the fallback default and as the identity of "the stock model" for
  installs that have not been rebuilt yet.
  """
  prefix = 'amd_' if PC and get_desktop_model_device() == "AMD" else ''
  return MODELS_DIR / f'{prefix}driving_tinygrad.pkl'


def default_modeld_pkl_path(chestnut: bool):
  """Artifact modeld uses when Settings has no usable pick: the hardware default.

  This is the pre-existing behaviour and stays the fallback for every path that
  cannot honour a user selection. On the road-model path the build-owned
  artifact wins when it exists; the legacy name is kept for installs that have
  not been rebuilt since the split.
  """
  if chestnut:
    return MODELS_DIR / 'big_driving_tinygrad.pkl'
  if PC and get_desktop_model_device() == "AMD" and os.getenv(BIG_MODEL_ON_IGPU_ENV) == "1":
    big_igpu = big_model_on_igpu_path()
    if artifact_on_disk(big_igpu):
      return big_igpu
  stock = stock_modeld_pkl_path()
  if artifact_on_disk(stock):
    return stock
  return legacy_default_pkl_path()


def selected_bundle_pkl() -> Path | None:
  """Artifact of the model the user picked in Settings, if its file is on disk.

  Returns None when nothing is selected (hardware default), when the selection
  cannot be read, or when the file is missing - callers then fall back to
  default_modeld_pkl_path().

  On the desktop AMD backend the downloaded bundle is usually compiled for a
  different device (the sunnypilot catalog only ships QCOM pkls), so an
  ``amd_``-prefixed sibling is preferred when present: that is the locally
  compiled build of the same model and the only one that can actually run here.
  """
  if not PC:
    return None
  try:
    from openpilot.sunnypilot.models.helpers import get_active_bundle
    bundle = get_active_bundle()
  except Exception:
    # sunnypilot's model manager is optional on this build; never let a missing
    # module or an unreadable param stop modeld from starting.
    return None
  if bundle is None:
    return None
  ref = bundle.ref or ""
  if ref.startswith(LOCAL_SMALL_REF_PREFIX):
    # A locally compiled build: the ref carries the file name outright.
    local = MODELS_DIR / ref[len(LOCAL_SMALL_REF_PREFIX):]
    return local if artifact_on_disk(local) else None
  amd = get_desktop_model_device() == "AMD"
  for model in bundle.models:
    name = model.artifact.fileName
    if not name:
      continue
    if amd:
      amd_sibling = Path(Paths.model_root()) / f'amd_{name}'
      if artifact_on_disk(amd_sibling):
        return amd_sibling
    path = Path(Paths.model_root()) / name
    if artifact_on_disk(path):
      return path
  return None


def big_model_artifact() -> Path | None:
  """Locally compiled big model on disk, independent of the environment switch."""
  path = big_model_on_igpu_path()
  return path if artifact_on_disk(path) else None


def selected_big_pkl() -> Path | None:
  """Big model the user picked in Settings, when its artifact is on disk.

  The Big Model slot normally belongs to a chestnut device. On the desktop the
  same slot doubles as the big/small switch: picking the locally compiled big
  model there means "drive with the big model", and picking Default turns it
  back off. Only the local ref counts - a catalogue chestnut bundle describes a
  different device's model and must never hijack the desktop small slot.
  """
  try:
    from openpilot.sunnypilot.models.helpers import get_selected_bundle
    bundle = get_selected_bundle(source="chestnut")
  except Exception:
    # The model manager is optional on this build; never let a broken param or
    # a missing module stop modeld from resolving its artifact.
    return None
  if bundle is None or (bundle.ref or "") != LOCAL_BIG_REF:
    return None
  return big_model_artifact()


def modeld_pkl_path(chestnut: bool):
  """Artifact modeld should load: the Settings pick when usable, else the default.

  Chestnut keeps the hardware default - its slot is a different device and the
  desktop model manager has no say there. On the desktop the Big Model slot is
  the big/small switch, so a local big pick outranks the small-model pick; the
  small pick is kept and used again as soon as the big pick is cleared.
  """
  if chestnut:
    return default_modeld_pkl_path(True)
  if (big := selected_big_pkl()) is not None:
    return big
  if (selected := selected_bundle_pkl()) is not None:
    return selected
  return default_modeld_pkl_path(False)


def model_swap_target(current_pkl: Path, chestnut: bool) -> Path | None:
  """New artifact a running modeld should switch to, or None when nothing changed.

  modeld loads its artifact once at startup, so a Settings pick used to be
  written but never loaded - the pick looked ignored until the stack was
  restarted. This is the check the model loop runs to notice the change; it is
  a plain function so the decision is testable without cameras.
  """
  target = modeld_pkl_path(chestnut)
  return target if target != current_pkl else None


def load_modeld_jits(chestnut: bool) -> tuple[dict, Path]:
  """Load the driving-model jits, falling back to the hardware default.

  A model picked in Settings can be unusable on this device: the sunnypilot
  catalog ships pkls compiled for the comma 3X, and loading one here raises
  (e.g. FileNotFoundError: /dev/kgsl-3d0). Falling back keeps modeld alive
  instead of turning a bad pick into a dead stack.

  Returns the jits together with the path actually loaded, so callers can log
  which artifact is really driving.
  """
  path = modeld_pkl_path(chestnut)
  try:
    return load_oob(open_file_chunked(path)), path
  except Exception:
    fallback = default_modeld_pkl_path(chestnut)
    if path == fallback:
      raise
    from openpilot.common.swaglog import cloudlog
    cloudlog.exception("model %s failed to load, falling back to %s", path, fallback)
    return load_oob(open_file_chunked(fallback)), fallback

def dump_oob(obj, f):
  with tempfile.TemporaryFile(dir=".") as tmp:
    def buffer_callback(pb: pickle.PickleBuffer):
      m = pb.raw()
      tmp.write(struct.pack('<q', m.nbytes))
      tmp.write(m)
      pb.release() # keep peak ram at ~1 buffer
    stream = io.BytesIO()
    pickle.Pickler(stream, protocol=5, buffer_callback=buffer_callback).dump(obj)
    opcodes = stream.getvalue()
    f.write(struct.pack('<q', len(opcodes)))
    f.write(opcodes)
    tmp.seek(0)
    shutil.copyfileobj(tmp, f)

def load_oob(f):
  opcodes = f.read(struct.unpack('<q', f.read(8))[0])
  def buffers():
    while (h := f.read(8)):
      pb = pickle.PickleBuffer(bytearray(struct.unpack('<q', h)[0]))
      f.readinto(pb)
      yield pb
  return pickle.load(io.BytesIO(opcodes), buffers=buffers())

def chestnut_present() -> bool:
  for d in USB_DEVICES_PATH.glob("*"):
    try:
      usb_id = (int((d / "idVendor").read_text(), 16), int((d / "idProduct").read_text(), 16))
      product = (d / "product").read_text().strip()
      if is_chestnut_usb_id(*usb_id) and product == CHESTNUT_USB_PRODUCT:
        return True
    except Exception:
      pass
  return False

def chestnut_compiled() -> bool:
  return Path(get_manifest_path(modeld_pkl_path(chestnut=True))).is_file()


def chestnut_ready(state) -> bool:
  return state.supplyVoltage >= CHESTNUT_POWERED_VOLTAGE and not state.supplyFault and state.pcieLtssm == CHESTNUT_PCIE_READY
