#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import zipfile
import xml.etree.ElementTree as ET

try:
  from .android_gps_source import GPS_BRIDGE_MAX_BYTES, GPS_BRIDGE_PATH
except ImportError:
  from android_gps_source import GPS_BRIDGE_MAX_BYTES, GPS_BRIDGE_PATH


ROOT = Path(__file__).resolve().parent
PACKAGE = "com.jixiexiaoge.drivingassist.navdiag"
ANDROID = "{http://schemas.android.com/apk/res/android}"


def run(args: list[str | Path]) -> str:
  result = subprocess.run([str(x) for x in args], text=True, capture_output=True, check=True)
  return (result.stdout + result.stderr).strip()


def digest(path: Path) -> str:
  return hashlib.sha256(path.read_bytes()).hexdigest()


def check_manifest(path: Path) -> None:
  manifest = ET.parse(path).getroot()
  if manifest.get("package") != PACKAGE:
    raise ValueError("采集包包名不匹配，拒绝构建")
  permissions = {node.get(ANDROID + "name") for node in manifest.findall("uses-permission")}
  approved_permissions = {"android.permission.FOREGROUND_SERVICE", "android.permission.WAKE_LOCK",
                          "android.permission.ACCESS_COARSE_LOCATION",
                          "android.permission.ACCESS_FINE_LOCATION"}
  if permissions - approved_permissions:
    raise ValueError(f"采集包包含未批准权限：{sorted(permissions)}")
  for node in manifest.iter("action"):
    if node.get(ANDROID + "name") == "android.intent.action.BOOT_COMPLETED":
      raise ValueError("采集包不能开机自启")
  application = manifest.find("application")
  if application is None:
    raise ValueError("采集包缺少 application")
  receivers = application.findall("receiver")
  if (len(receivers) > 1
      or (receivers and (receivers[0].get(ANDROID + "name") != ".GpsBridgeCommandReceiver"
                         or receivers[0].get(ANDROID + "exported") != "true"
                         or receivers[0].findall("intent-filter")))):
    raise ValueError("采集包只允许一个无 filter 的显式 GPS 启停接收器")
  if any(node.get(ANDROID + "name") in {".NativeRouteProbe", PACKAGE + ".NativeRouteProbe",
                                        ".AndroidGpsProbe", PACKAGE + ".AndroidGpsProbe",
                                        ".PhoneCaptureProbe", PACKAGE + ".PhoneCaptureProbe"}
         for node in application.iter()):
    raise ValueError("只读原生探针只能由手动 app_process 运行，不能声明为应用组件")
  for service in application.findall("service"):
    name = service.get(ANDROID + "name")
    if name in {".CaptureService", ".BridgeService", ".PhoneCaptureService"}:
      if service.get(ANDROID + "exported") != "false" or service.findall("intent-filter"):
        raise ValueError("采集与广播观测服务必须是明确禁用导出的本地服务")
      if name == ".PhoneCaptureService" and service.get(ANDROID + "foregroundServiceType") != "location":
        raise ValueError("手机独立采集必须声明 location 前台类型")
    elif name == ".GpsBridgeService":
      if (service.get(ANDROID + "exported") != "true"
          or service.get(ANDROID + "permission") != "android.permission.DUMP"
          or service.get(ANDROID + "foregroundServiceType") != "location"
          or service.findall("intent-filter")):
        raise ValueError("GPS 桥必须仅以 DUMP 权限受限显式启动，并声明 location 前台类型")
    else:
      raise ValueError("采集包包含未知服务")
  for node in manifest.findall("instrumentation"):
    if node.get(ANDROID + "targetPackage") != PACKAGE:
      raise ValueError("采集自测不能指向其他应用")


def main() -> None:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--android-jar", type=Path,
                      default=Path(os.environ.get("NAVDIAG_ANDROID_JAR", "/data/android-sdk/android-10/android.jar")))
  parser.add_argument("--build-tools", type=Path,
                      default=Path(os.environ.get("NAVDIAG_BUILD_TOOLS", "/data/android-sdk/android-14")))
  parser.add_argument("--out", type=Path, default=ROOT / "build")
  parser.add_argument("--keystore", type=Path,
                      default=Path.home() / ".local/share/sunnypilot/navdiag/debug.keystore")
  args = parser.parse_args()
  manifest = ROOT / "AndroidManifest.xml"
  check_manifest(manifest)
  for path in [args.android_jar, *(args.build_tools / n for n in ("aapt2", "aapt", "d8", "zipalign", "apksigner"))]:
    if not path.is_file():
      raise FileNotFoundError(f"缺少 Android 构建文件：{path}")
  for command in ("javac", "java", "keytool"):
    if not shutil.which(command):
      raise FileNotFoundError(f"缺少工具：{command}")
  sources = sorted((ROOT / "src").rglob("*.java"))
  if not sources:
    raise ValueError("采集包源码为空")
  out = args.out.resolve()
  out.mkdir(parents=True, exist_ok=True)
  classes = out / "classes"
  dex = out / "dex"
  for folder in (classes, dex):
    if folder.exists():
      shutil.rmtree(folder)
    folder.mkdir()
  print(run(["javac", "-encoding", "UTF-8", "-source", "8", "-target", "8", "-g",
             "-classpath", args.android_jar, "-d", classes, *sources]))
  print(run([args.build_tools / "d8", "--debug", "--min-api", "29", "--lib", args.android_jar,
             "--output", dex, *sorted(classes.rglob("*.class"))]))
  resource_apk = out / "resources.apk"
  print(run([args.build_tools / "aapt2", "link", "--manifest", manifest, "-I", args.android_jar,
             "--min-sdk-version", "29", "--target-sdk-version", "29", "-o", resource_apk]))
  unsigned = out / "navdiag-unsigned.apk"
  shutil.copyfile(resource_apk, unsigned)
  with zipfile.ZipFile(unsigned, "a", compression=zipfile.ZIP_DEFLATED) as archive:
    for file in sorted(dex.glob("*.dex")):
      archive.write(file, file.name)
  aligned = out / "navdiag-aligned.apk"
  run([args.build_tools / "zipalign", "-f", "4", unsigned, aligned])
  args.keystore.parent.mkdir(parents=True, exist_ok=True)
  if not args.keystore.exists():
    run(["keytool", "-genkeypair", "-noprompt", "-keystore", args.keystore,
         "-storepass", "android", "-keypass", "android", "-alias", "navdiag-debug",
         "-keyalg", "RSA", "-keysize", "2048", "-validity", "3650",
         "-dname", "CN=CP Nav Diagnostics,O=Local Development,C=CN"])
    args.keystore.chmod(0o600)
  apk = out / "navdiag-debug.apk"
  run([args.build_tools / "apksigner", "sign", "--ks", args.keystore,
       "--ks-key-alias", "navdiag-debug", "--ks-pass", "pass:android", "--key-pass", "pass:android",
       "--out", apk, aligned])
  signature = run([args.build_tools / "apksigner", "verify", "--verbose", apk])
  permissions = run([args.build_tools / "aapt", "dump", "permissions", apk])
  if "android.permission.INTERNET" in permissions:
    raise ValueError("最终 APK 意外包含 INTERNET 权限")
  badging = run([args.build_tools / "aapt2", "dump", "badging", apk])
  if f"name='{PACKAGE}'" not in badging.splitlines()[0]:
    raise ValueError("最终 APK 包名验证失败")
  report = {
    "package": PACKAGE, "apk": str(apk), "sha256": digest(apk), "bytes": apk.stat().st_size,
    "min_api": 29, "target_api": 29, "android_jar_sha256": digest(args.android_jar),
    "permissions": permissions.splitlines(), "signature_verification": signature.splitlines(),
    "bridge": {"manual_start_only": True, "max_duration_ms": 7_200_000,
               "max_atomic_file_bytes": 2_097_152, "network_permission": False,
               "latest_private_file": "files/navdiag-bridge/latest.json",
               "instrumentation": PACKAGE + "/.BridgeInstrumentation"},
    "gps_bridge": {"manual_start_only": True, "max_duration_ms": 7_200_000,
                   "provider": "gps", "latest_private_file": GPS_BRIDGE_PATH,
                   "max_file_bytes": GPS_BRIDGE_MAX_BYTES, "network_permission": False,
                   "instrumentation": PACKAGE + "/.GpsBridgeInstrumentation"},
    "phone_capture": {"manual_start_only": True, "max_duration_ms": 180_000,
                      "max_session_bytes": 268_435_456, "max_retained_bytes": 1_073_741_824,
                      "private_directory": "files/phone-route-captures",
                      "requested_sample_interval_ms": 250,
                      "gps_capture_mode": "latest_fix_at_each_native_pair",
                      "root_permission_required": True, "adb_required_during_capture": False,
                      "export": "user_selected_zip", "network_permission": False,
                      "host_pipeline_executed": False, "control_input_allowed": False,
                      "instrumentation": PACKAGE + "/.PhoneCaptureInstrumentation"},
    "native_route_probe": {"main_class": PACKAGE + ".NativeRouteProbe", "application_component": False,
                           "fixed_source_package": "com.autonavi.amapauto", "memory_mode": "read_only",
                           "stdin_commands": ["OBSERVE", "OBSERVE_POSITION", "OBSERVE_NAVI_INFO",
                                              "OBSERVE_POSITION_NAVI_INFO", "OBSERVE_GEOMETRY", "QUIT"],
                           "geometry_v2": {"magic": "AMAP_NAV_GEO_V2", "protocol_version": 1,
                                           "current_selected_path_only": True,
                                           "binary_single_read": True,
                                           "declared_full_route": False,
                                           "application_component": False,
                                           "network_permission": False},
                           "position_observation": {"opt_in_only": True, "private_diagnostic_only": True,
                                                    "coordinate_frame_verified": False,
                                                    "source_freshness_verified": False,
                                                    "route_anchor_verified": False,
                                                    "route_render_input_allowed": False,
                                                    "control_input_allowed": False},
                           "navi_info_observation": {"opt_in_only": True, "private_diagnostic_only": True,
                                                     "bound_to_current_path": True,
                                                     "route_render_input_allowed": False,
                                                     "control_input_allowed": False},
                           "instrumentation": PACKAGE + "/.NativeProbeInstrumentation"},
    "android_gps_probe": {"main_class": PACKAGE + ".AndroidGpsProbe", "application_component": False,
                          "manual_root_start_only": True, "fixed_lookup_uid": 1000,
                          "fixed_provider": "gps",
                          "method": "persistent_gps_callback_with_passive_gps_fallback_and_fresh_cache",
                          "requests_new_location": True,
                          "callback_looper": "dedicated_handler_thread",
                          "callback_providers": ["gps", "passive"],
                          "accepted_location_provider": "gps",
                          "passive_network_or_fused_rejected": True,
                          "max_live_request_ns": 750_000_000,
                          "changes_system_permissions_or_settings": False,
                          "stdin_commands": ["OBSERVE", "QUIT"], "private_coordinates_only": True,
                          "max_source_age_ns": 2_000_000_000, "max_accuracy_m": 25,
                          "max_session_ns": 7_200_000_000_000, "max_observation_commands": 144_000,
                          "instrumentation": PACKAGE + "/.AndroidGpsProbeInstrumentation"},
    "android_gps_bridge": {"service": PACKAGE + ".GpsBridgeService",
                           "application_component": True, "exported": True,
                           "caller_permission": "android.permission.DUMP",
                           "manual_start_only": True, "max_duration_ms": 7_200_000,
                           "command_receiver": PACKAGE + ".GpsBridgeCommandReceiver",
                           "provider": "gps", "callback_providers": ["gps", "passive"],
                           "accepted_location_provider": "gps",
                           "passive_network_or_fused_rejected": True,
                           "coordinates_private_only": True,
                           "latest_private_file": GPS_BRIDGE_PATH,
                           "max_file_bytes": GPS_BRIDGE_MAX_BYTES,
                           "network_permission": False, "control_input_allowed": False,
                           "instrumentation": PACKAGE + "/.GpsBridgeInstrumentation"},
    "sources": {str(p.relative_to(ROOT)): digest(p) for p in [manifest, Path(__file__).resolve(), *sources]},
  }
  (out / "build-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
  print(json.dumps({k: report[k] for k in ("package", "apk", "sha256", "bytes", "permissions")}, ensure_ascii=False))


if __name__ == "__main__":
  try:
    main()
  except subprocess.CalledProcessError as error:
    raise SystemExit(f"构建命令失败：{error.cmd}\n{error.stdout}\n{error.stderr}") from error
