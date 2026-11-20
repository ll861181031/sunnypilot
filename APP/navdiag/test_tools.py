import io
from pathlib import Path
import tarfile

import pytest

from build import PACKAGE, check_manifest
from device import extract_capture, verify_selftest


def manifest(path: Path, permissions: str = "", package: str = PACKAGE) -> Path:
  path.write_text('<manifest xmlns:android="http://schemas.android.com/apk/res/android" '
                  + f'package="{package}">{permissions}<application /></manifest>')
  return path


def test_capture_manifest_accepts_only_local_service_permission(tmp_path):
  check_manifest(manifest(tmp_path / "m.xml", '<uses-permission android:name="android.permission.FOREGROUND_SERVICE"/>'))


def test_capture_manifest_accepts_only_foreground_and_location_permissions(tmp_path):
  path = manifest(tmp_path / "location.xml",
                  '<uses-permission android:name="android.permission.FOREGROUND_SERVICE"/>'
                  '<uses-permission android:name="android.permission.WAKE_LOCK"/>'
                  '<uses-permission android:name="android.permission.ACCESS_COARSE_LOCATION"/>'
                  '<uses-permission android:name="android.permission.ACCESS_FINE_LOCATION"/>')
  path.write_text(path.read_text().replace("<application />",
      '<application><service android:name=".GpsBridgeService" android:exported="true" '
      'android:permission="android.permission.DUMP" '
      'android:foregroundServiceType="location"/></application>'))
  check_manifest(path)


def test_gps_bridge_manifest_requires_location_foreground_type(tmp_path):
  path = manifest(tmp_path / "no-location-type.xml")
  path.write_text(path.read_text().replace("<application />",
      '<application><service android:name=".GpsBridgeService" android:exported="true" '
      'android:permission="android.permission.DUMP"/></application>'))
  with pytest.raises(ValueError, match="location"):
    check_manifest(path)


@pytest.mark.parametrize("attributes", [
  'android:exported="false" android:permission="android.permission.DUMP"',
  'android:exported="true"',
  'android:exported="true" android:permission="android.permission.INTERNET"',
])
def test_gps_bridge_service_requires_dump_gated_export(tmp_path, attributes):
  path = manifest(tmp_path / "gps-service.xml")
  path.write_text(path.read_text().replace("<application />",
      f'<application><service android:name=".GpsBridgeService" {attributes} '
      'android:foregroundServiceType="location"/></application>'))
  with pytest.raises(ValueError, match="DUMP"):
    check_manifest(path)


@pytest.mark.parametrize("permission", ["android.permission.INTERNET", "android.permission.RECEIVE_BOOT_COMPLETED",
                                       "android.permission.WRITE_EXTERNAL_STORAGE"])
def test_production_permissions_are_rejected(tmp_path, permission):
  with pytest.raises(ValueError):
    check_manifest(manifest(tmp_path / "m.xml", f'<uses-permission android:name="{permission}"/>'))


def test_cannot_build_over_existing_production_package(tmp_path):
  with pytest.raises(ValueError):
    check_manifest(manifest(tmp_path / "m.xml", package="com.jixiexiaoge.drivingassist.debug"))


@pytest.mark.parametrize("component", [
  '<receiver android:name=".AutomaticReceiver" android:exported="false"/>',
  '<receiver android:name=".GpsBridgeCommandReceiver" android:exported="true"><intent-filter>'
  + '<action android:name="BOOT"/></intent-filter></receiver>',
  '<service android:name=".BridgeService" android:exported="true"/>',
  '<service android:name=".BridgeService"/>',
  '<service android:name=".UnknownService" android:exported="false"/>',
  '<service android:name=".BridgeService" android:exported="false"><intent-filter>'
  + '<action android:name="BRIDGE_START"/></intent-filter></service>',
])
def test_bridge_manifest_rejects_automatic_or_exported_components(tmp_path, component):
  path = manifest(tmp_path / "m.xml")
  path.write_text(path.read_text().replace("<application />", f"<application>{component}</application>"))
  with pytest.raises(ValueError):
    check_manifest(path)


def test_manifest_accepts_only_explicit_gps_command_receiver(tmp_path):
  path = manifest(tmp_path / "receiver.xml")
  path.write_text(path.read_text().replace("<application />",
      '<application><receiver android:name=".GpsBridgeCommandReceiver" android:exported="true"/>'
      '</application>'))
  check_manifest(path)


def test_actual_bridge_manifest_remains_local_and_manual():
  check_manifest(Path(__file__).with_name("AndroidManifest.xml"))


def make_tar(path: Path, member: str, *, symlink: bool = False) -> Path:
  with tarfile.open(path, "w") as archive:
    info = tarfile.TarInfo(member)
    if symlink:
      info.type = tarfile.SYMTYPE
      info.linkname = "/tmp/outside"
      archive.addfile(info)
    else:
      data = b'{"complete":true}\n'
      info.size = len(data)
      archive.addfile(info, io.BytesIO(data))
  return path


@pytest.mark.parametrize("member", ["../escape.json", "/tmp/escape.json", "other-package/file.json"])
def test_untrusted_archive_cannot_escape_capture_directory(tmp_path, member):
  with pytest.raises(ValueError):
    extract_capture(make_tar(tmp_path / "bad.tar", member), tmp_path / "out")


def test_archive_symlink_is_rejected(tmp_path):
  with pytest.raises(ValueError):
    extract_capture(make_tar(tmp_path / "bad.tar", "navdiag-captures/link", symlink=True), tmp_path / "out")


def test_own_capture_archive_extracts_without_rewriting_content(tmp_path):
  archive = make_tar(tmp_path / "ok.tar", "navdiag-captures/session/manifest.json")
  output = tmp_path / "out"
  extract_capture(archive, output)
  assert (output / "navdiag-captures/session/manifest.json").read_bytes() == b'{"complete":true}\n'


def test_selftest_requires_complete_positive_result():
  verify_selftest("INSTRUMENTATION_RESULT: tests_passed=11\nINSTRUMENTATION_RESULT: tests_failed=0\nINSTRUMENTATION_CODE: -1\n")


@pytest.mark.parametrize("report", ["", "INSTRUMENTATION_CODE: -1\n",
  "INSTRUMENTATION_RESULT: tests_passed=0\nINSTRUMENTATION_RESULT: tests_failed=0\nINSTRUMENTATION_CODE: -1\n",
  "INSTRUMENTATION_RESULT: tests_passed=10\nINSTRUMENTATION_RESULT: tests_failed=1\nINSTRUMENTATION_CODE: 0\n",
  "INSTRUMENTATION_RESULT: tests_passed=11\nINSTRUMENTATION_RESULT: tests_failed=0\nINSTRUMENTATION_CODE: 0\n"])
def test_selftest_rejects_missing_or_failed_report(report):
  with pytest.raises(RuntimeError):
    verify_selftest(report)
