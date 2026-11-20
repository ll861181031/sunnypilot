"""Keep the remote HIP runtime independent of modeld's loader environment."""
from pathlib import Path

import pytest


def test_environment_is_explicit_and_does_not_mutate_parent(tmp_path):
  from openpilot.selfdrive.modeld.mgxd_runtime import daemon_environment
  root = str(tmp_path)
  original = {'MODELD_MGXD_ROCM_ROOT': root, 'LD_LIBRARY_PATH': '/old/rocm:/shim',
              'PYTHONPATH': '/old/python', 'LD_PRELOAD': '/old/shim.so', 'PATH': '/usr/bin',
              'ROCBLAS_TENSILE_LIBPATH': '/old/kernels', 'HSA_OVERRIDE_GFX_VERSION': '11.0.2'}
  result = daemon_environment(original)
  assert original['LD_LIBRARY_PATH'] == '/old/rocm:/shim'
  assert result['LD_LIBRARY_PATH'] == f'{root}/lib:{root}/lib/migraphx/lib'
  assert result['PYTHONPATH'] == f'{root}/lib:/data/sunnypilot'
  assert result['ROCM_PATH'] == result['HIP_PATH'] == root
  assert 'LD_PRELOAD' not in result and 'ROCBLAS_TENSILE_LIBPATH' not in result
  assert result['HSA_OVERRIDE_GFX_VERSION'] == '11.0.2'


def test_legacy_without_explicit_root_is_unchanged():
  from openpilot.selfdrive.modeld.mgxd_runtime import daemon_environment
  original = {'LD_LIBRARY_PATH': '/legacy'}
  assert daemon_environment(original) == original


def test_capture_requires_explicit_runtime():
  from openpilot.selfdrive.modeld.mgxd_runtime import daemon_environment
  with pytest.raises(ValueError, match='ROCM_ROOT'):
    daemon_environment({'MODELD_MGXD_ENGINE': 'capture'})


def test_loaded_library_audit_rejects_mixed_or_missing_libraries(tmp_path):
  from openpilot.selfdrive.modeld.mgxd_runtime import validate_library_paths
  paths = [str(tmp_path / 'lib' / name) for name in
           ('libamdhip64.so.7.1', 'libhsa-runtime64.so.1', 'libmigraphx.so.2014000',
            'libMIOpen.so.1', 'librocblas.so.5', 'libhipblaslt.so.1')]
  assert validate_library_paths(paths, tmp_path) == sorted(paths)
  with pytest.raises(RuntimeError, match='runtime'):
    validate_library_paths(paths + ['/opt/rocm/lib/libamdhip64.so.6'], tmp_path)
  with pytest.raises(RuntimeError, match='runtime'):
    validate_library_paths(paths[1:], tmp_path)
  with pytest.raises(RuntimeError, match='runtime'):
    validate_library_paths(paths + [str(tmp_path / 'lib/libamdhip64.so.other')], tmp_path)


def test_remote_spawn_uses_daemon_environment():
  import ast
  source = Path(__file__).with_name('modeld.py').read_text()
  tree = ast.parse(source)
  cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'MigraphxRemoteState')
  ensure = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == '_ensure_server')
  assert 'daemon_environment(os.environ)' in ast.get_source_segment(source, ensure)
