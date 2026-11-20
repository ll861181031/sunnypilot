import subprocess
import sys
import textwrap


def test_song_runtime_imports_without_other_platform_packages():
  script = """
    import importlib
    import sys

    class PlatformGuard:
      def find_spec(self, fullname, path, target=None):
        for prefix in ('opendbc.car.', 'opendbc.sunnypilot.car.'):
          if fullname.startswith(prefix) and fullname[len(prefix):].split('.')[0] in {
            'body', 'chrysler', 'ford', 'gm', 'honda', 'hyundai', 'mazda', 'nissan', 'psa',
            'rivian', 'subaru', 'tesla', 'toyota', 'volkswagen',
          }:
            raise ModuleNotFoundError(fullname)

    sys.meta_path.insert(0, PlatformGuard())
    for name in ('selfdrive.car.card', 'selfdrive.selfdrived.selfdrived', 'selfdrive.controls.radard',
                 'sunnypilot.sunnylink.capabilities', 'selfdrive.test.process_replay.migration',
                 'selfdrive.ui.sunnypilot.layouts.settings.vehicle', 'selfdrive.ui.sunnypilot.layouts.settings.steering_sub_layouts.mads_settings'):
      importlib.import_module('openpilot.' + name)
  """
  subprocess.run([sys.executable, "-c", textwrap.dedent(script)], check=True)


def test_public_replay_requires_song_reference_logs():
  result = subprocess.run(
    [sys.executable, "-m", "openpilot.selfdrive.test.process_replay.test_processes"],
    capture_output=True, text=True, timeout=30,
  )
  assert result.returncode == 2
  assert "BYD_SONG_PLUS_DMI_23_ALT" in result.stderr
  assert "TEST SUCCEEDED" not in result.stdout
