"""Small, local provenance snapshots; never query hardware or mutate Params."""

import hashlib
import json
import os
import subprocess
import time
import zipfile
from pathlib import Path

from openpilot.system.loggerd.drive_store import atomic_json, fingerprint, load_json, route_name, sha256_file, store_root

SOURCE_PATTERNS = (
  'openpilot/selfdrive/controls/**/*.py', 'openpilot/selfdrive/modeld/*.py', 'openpilot/selfdrive/car/*.py',
  'openpilot/sunnypilot/modeld_v2/*.py', 'openpilot/sunnypilot/livedelay/*.py',
  'openpilot/sunnypilot/selfdrive/controls/**/*.py', 'openpilot/sunnypilot/mads/*.py',
  'openpilot/cereal/*.capnp', 'openpilot/cereal/services.py', 'opendbc_repo/opendbc/car/byd/*.py',
  'opendbc_repo/opendbc/car/vehicle_model.py', 'opendbc_repo/opendbc/safety/modes/byd.h',
  'openpilot/system/manager/process_config.py', 'openpilot/system/loggerd/drive_*.py',
)
ENV_KEYS = ('MODELD_BACKEND', 'MODELD_MIGRAPHX_ONNX', 'MODELD_MIGRAPHX_MXR', 'COMBINED_MODEL_PKL',
            'MODELD_MIGRAPHX_INVERT', 'MODELD_MIGRAPHX_UV', 'MODELD_MIGRAPHX_SHIFT', 'HSA_OVERRIDE_GFX_VERSION',
            'SUNNYPILOT_BIG_MODEL_ON_IGPU', 'USE_WEBCAM', 'NARROW_ROAD_CAM', 'WIDE_CAM', 'DRIVER_CAM', 'BLOCK')
PARAM_KEYS = ('DisableLogging', 'DisableEncoderd', 'RecordFront', 'RecordAudio', 'LaneTurnDesire', 'LaneTurnValue',
              'BlinkerPauseLateralControl', 'SmartCruiseControlVision', 'SmartCruiseControlMap', 'ModelRunnerTypeCache',
              'ModelManager_ActiveBundle', 'ModelManager_ActiveBundleChestnut', 'ExperimentalMode')


def configuration(params) -> dict:
  keys = [k.decode() if isinstance(k, bytes) else k for k in params.all_keys()]
  selected = [k for k in keys if k in PARAM_KEYS or k.startswith(('Byd', 'Lagd'))]
  values = {}
  for key in sorted(selected):
    value = params.get(key)
    values[key] = {'sha256': hashlib.sha256(value).hexdigest()} if isinstance(value, bytes) else value
  return values


def git_identity(path: Path) -> dict:
  def run(*args):
    try:
      return subprocess.run(['git', '-C', str(path), *args], capture_output=True, text=True, timeout=5, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
      return 'unknown'
  return {'head': run('rev-parse', 'HEAD'), 'workingTree': run('status', '--porcelain', '--untracked-files=no')}


def capture(root: Path, route: str, params, repo: Path, environ=None) -> dict:
  route_name(route)
  destination = store_root(root) / 'routes' / route / 'provenance.json'
  if destination.exists():
    return load_json(destination, {'status': 'unreadable'})
  env = os.environ if environ is None else environ
  files = {}
  contents = {}
  for pattern in SOURCE_PATTERNS:
    for path in repo.glob(pattern):
      if path.is_file() and not path.is_symlink() and path.stat().st_size <= 2 * 1024 * 1024:
        name = str(path.relative_to(repo))
        data = path.read_bytes()
        files[name] = hashlib.sha256(data).hexdigest()
        contents[name] = data
  identity = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
  snapshot = identity + '.zip'
  archive = store_root(root) / 'snapshots' / snapshot
  archive.parent.mkdir(parents=True, exist_ok=True)
  if not archive.exists():
    temporary = archive.with_suffix('.tmp')
    try:
      with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, data in sorted(contents.items()):
          z.writestr(name, data)
      os.replace(temporary, archive)
    finally:
      temporary.unlink(missing_ok=True)
  artifacts = []
  candidates = {env[k] for k in ('MODELD_MIGRAPHX_ONNX', 'MODELD_MIGRAPHX_MXR', 'COMBINED_MODEL_PKL') if env.get(k)}
  if env.get('MODELD_BACKEND', '').startswith('migraphx'):
    candidates.add(env.get('MODELD_MIGRAPHX_MXR', str(Path.home() / 'fwtest/nav.mxr')))
  for value in sorted(candidates):
    path = Path(value).expanduser()
    artifacts.append({'path': str(path), 'fingerprint': fingerprint(path) if path.is_file() else None,
                      'evidence': 'configured_artifact_not_proof_of_loaded_bytes'})
  result = {'status': 'captured', 'capturedMonoNs': time.monotonic_ns(), 'capturedWallNs': time.time_ns(),
            'sourceSnapshot': snapshot, 'sourceHashes': files, 'git': git_identity(repo),
            'opendbcGit': git_identity(repo / 'opendbc_repo'), 'parameters': configuration(params),
            'environment': {k: env[k] for k in ENV_KEYS if k in env}, 'configuredModelArtifacts': artifacts,
            'deviceFirmwareIdentity': 'unknown', 'kernel': os.uname().release,
            'evidence': 'on_disk_source_at_observation_not_a_dump_of_loaded_process_memory'}
  atomic_json(destination, result)
  return result


def observe_configuration(root: Path, route: str, params) -> None:
  directory = store_root(root) / 'routes' / route_name(route)
  values = configuration(params)
  previous = load_json(directory / 'latest-configuration.json', {})
  if previous.get('parameters') != values:
    record = {'monoTimeNs': time.monotonic_ns(), 'wallTimeNs': time.time_ns(), 'parameters': values}
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'configuration-changes.jsonl').open('a') as f:
      f.write(json.dumps(record, ensure_ascii=False) + '\n')
    atomic_json(directory / 'latest-configuration.json', record)


def hash_artifacts_offroad(root: Path, should_stop=None) -> None:
  for path in (store_root(root) / 'routes').glob('*/provenance.json'):
    if should_stop and should_stop():
      return
    data = load_json(path, {})
    changed = False
    for artifact in data.get('configuredModelArtifacts', []):
      if artifact.get('sha256') or artifact.get('hashStatus'):
        continue
      source = Path(artifact['path'])
      before = artifact.get('fingerprint')
      try:
        if before is None or fingerprint(source) != before:
          artifact['hashStatus'] = 'changed_or_missing_since_capture'
        else:
          digest = sha256_file(source, should_stop)
          if fingerprint(source) == before:
            artifact.update(sha256=digest, hashStatus='verified_observed_file')
          else:
            artifact['hashStatus'] = 'changed_while_hashing'
      except InterruptedError:
        return
      except OSError:
        artifact['hashStatus'] = 'unavailable'
      changed = True
    if changed:
      atomic_json(path, data)
