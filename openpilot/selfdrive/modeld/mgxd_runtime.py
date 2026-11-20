"""Configure and verify the isolated mgxd ROCm loader environment."""
import os
from pathlib import Path


LIBRARY_KEYS = ('migraphx', 'amdhip', 'hsa-runtime', 'MIOpen', 'rocblas', 'hipblas', 'hiprtc', 'amd_comgr')


def daemon_environment(parent):
  env = dict(parent)
  root = env.get('MODELD_MGXD_ROCM_ROOT')
  if not root:
    if env.get('MODELD_MGXD_ENGINE') == 'capture':
      raise ValueError('Capture requires MODELD_MGXD_ROCM_ROOT')
    return env
  root = str(Path(root).resolve())
  for key in ('LD_PRELOAD', 'LD_AUDIT', 'ROCBLAS_TENSILE_LIBPATH', 'HIPBLASLT_TENSILE_LIBPATH'):
    env.pop(key, None)
  env.update(MODELD_MGXD_ROCM_ROOT=root, ROCM_PATH=root, HIP_PATH=root,
             LD_LIBRARY_PATH=f'{root}/lib:{root}/lib/migraphx/lib',
             PYTHONPATH=f'{root}/lib:{Path(__file__).resolve().parents[3]}')
  return env


def validate_library_paths(paths, root):
  root = Path(root).resolve()
  paths = sorted(set(paths))
  required = ('libamdhip64.so.', 'libhsa-runtime64.so.', 'libmigraphx.so.',
              'libMIOpen.so.', 'librocblas.so.', 'libhipblaslt.so.')
  if (not paths or any(not Path(path).resolve().is_relative_to(root) for path in paths)
      or any(sum(key in Path(path).name for path in paths) != 1 for key in required)):
    raise RuntimeError(f'mgxd runtime library mismatch: {paths}')
  return paths


def audit_runtime(migraphx):
  root = os.environ.get('MODELD_MGXD_ROCM_ROOT')
  if not root:
    if os.environ.get('MODELD_MGXD_ENGINE') == 'capture':
      raise RuntimeError('Capture requires MODELD_MGXD_ROCM_ROOT')
    return None
  if os.environ.get('HSA_OVERRIDE_GFX_VERSION') != '11.0.2':
    raise RuntimeError('mgxd capture runtime requires the validated gfx1102 target')
  if not str(migraphx.__version__).startswith('2.14.0') or not Path(migraphx.__file__).resolve().is_relative_to(Path(root).resolve()):
    raise RuntimeError('mgxd MIGraphX runtime version or module path mismatch')
  paths = {line.split(maxsplit=5)[-1] for line in Path('/proc/self/maps').read_text().splitlines()
           if any(key in line for key in LIBRARY_KEYS) and '/' in line}
  return {'root': root, 'migraphx_version': migraphx.__version__,
          'libraries': validate_library_paths(paths, root), 'target_override': '11.0.2'}
