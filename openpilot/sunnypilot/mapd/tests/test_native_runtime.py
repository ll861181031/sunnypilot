import hashlib
import json
from unittest.mock import Mock, patch

import pytest

from openpilot.sunnypilot.mapd import VERSION
from openpilot.sunnypilot.mapd import runtime
from openpilot.sunnypilot.mapd.mapd_installer import MapdInstallManager


@pytest.fixture
def native_binary(tmp_path, monkeypatch):
  path = tmp_path / "mapd-linux-amd64"
  header = bytearray(64)
  header[:6] = b"\x7fELF\x02\x01"
  header[18:20] = (62).to_bytes(2, "little")
  path.write_bytes(header)
  path.chmod(0o755)
  path.with_suffix(".json").write_text(json.dumps({"version": VERSION, "sha256": hashlib.sha256(header).hexdigest()}))
  monkeypatch.setattr(runtime, "NATIVE_MAPD_PATH", path)
  monkeypatch.setattr("platform.system", lambda: "Linux")
  monkeypatch.setattr("platform.machine", lambda: "x86_64")
  return path


def test_native_binary_must_match_version_architecture_and_checksum(native_binary):
  assert runtime.mapd_available()
  header = bytearray(native_binary.read_bytes())
  header[18:20] = (183).to_bytes(2, "little")
  native_binary.write_bytes(header)
  assert not runtime.mapd_available()


def _apply_damage(binary, damage):
  manifest = binary.with_suffix(".json")
  if damage == "missing":
    binary.unlink()
  elif damage == "not_executable":
    binary.chmod(0o644)
  elif damage == "checksum":
    binary.write_bytes(binary.read_bytes() + b"changed")
  elif damage == "version":
    metadata = json.loads(manifest.read_text())
    metadata["version"] = "v0.0.0"
    manifest.write_text(json.dumps(metadata))
  elif damage == "no_manifest":
    manifest.unlink()
  elif damage == "bad_manifest":
    manifest.write_text("{")


@pytest.mark.parametrize("damage", ["missing", "not_executable", "checksum", "version", "no_manifest", "bad_manifest"])
def test_incomplete_native_install_is_disabled(native_binary, damage):
  _apply_damage(native_binary, damage)
  assert not runtime.mapd_available()


@pytest.mark.parametrize("damage,expected", [
  ("missing", "is missing"),
  ("not_executable", "is not executable"),
  ("checksum", "checksum does not match its manifest"),
  ("version", "does not match"),
  ("no_manifest", "could not be validated"),
  ("bad_manifest", "could not be validated"),
])
def test_unavailable_reason_names_the_defect(native_binary, damage, expected):
  _apply_damage(native_binary, damage)
  reason = runtime.mapd_unavailable_reason()
  assert reason is not None and expected in reason, reason


def test_healthy_install_has_no_reason(native_binary):
  assert runtime.mapd_unavailable_reason() is None


@pytest.mark.parametrize("system,machine,expected", [
  ("Darwin", "arm64", "unsupported OS"),
  ("Linux", "riscv64", "unsupported architecture"),
])
def test_platform_reason_is_specific(system, machine, expected):
  with patch("platform.system", return_value=system), patch("platform.machine", return_value=machine):
    reason = runtime.mapd_unavailable_reason()
  assert reason is not None and expected in reason, reason


@pytest.mark.parametrize("system,machine,expected", [("Linux", "aarch64", True), ("Darwin", "arm64", False),
                                                   ("Darwin", "x86_64", False), ("Linux", "riscv64", False)])
def test_bundled_arm_and_other_platforms(system, machine, expected):
  with patch("platform.system", return_value=system), patch("platform.machine", return_value=machine):
    assert runtime.mapd_available() is expected


def test_launcher_uses_real_params_paths_including_prefix(native_binary, tmp_path):
  from openpilot.common.hardware.hw import Paths
  from openpilot.common.params import Params
  from openpilot.common.prefix import OpenpilotPrefix

  with OpenpilotPrefix():
    params_path = Params().get_param_path()
    mem_params = Params(str(tmp_path / "shm"))
    # Use a temporary shared-memory root while retaining the real Params prefix handling.
    with patch("openpilot.common.params.Params", side_effect=[Params(), mem_params]), patch.object(runtime.os, "execve") as execute, \
         patch.object(runtime.os, "chdir"):
      runtime.main()
      path, argv, env = execute.call_args.args
      assert path == str(native_binary)
      assert argv == [path]
      assert env["MAPD_ROOT"] == Paths.mapd_root()
      assert env["MAPD_PARAMS_PATH"] == params_path
      assert env["MAPD_MEM_PARAMS_PATH"] == mem_params.get_param_path()


def test_native_installer_never_downloads_arm_asset(native_binary):
  with patch("openpilot.sunnypilot.mapd.mapd_installer.Params") as params:
    params.return_value.get.return_value = "old-version"
    installer = MapdInstallManager(Mock())
    with patch.object(installer, "ensure_directories_exist"), patch.object(installer, "_download_file") as download:
      installer.check_and_download()
      download.assert_not_called()
      params.return_value.put.assert_called_once_with("MapdVersion", VERSION, block=True)
    with pytest.raises(RuntimeError, match="ARM64-only"):
      installer._download_file()


def test_missing_native_binary_has_actionable_install_error(native_binary):
  native_binary.unlink()
  with patch("openpilot.sunnypilot.mapd.mapd_installer.Params"):
    installer = MapdInstallManager(Mock())
    with patch.object(installer, "ensure_directories_exist"), patch.object(installer, "_download_file") as download:
      with pytest.raises(RuntimeError, match="build_native"):
        installer.check_and_download()
      download.assert_not_called()


def test_version_tool_tracks_shared_version_file():
  from openpilot.sunnypilot.mapd.update_version import MAPD_VERSION_PATH, get_current_mapd_version
  assert get_current_mapd_version(MAPD_VERSION_PATH) == VERSION
