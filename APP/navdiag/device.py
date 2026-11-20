#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tarfile
import time


ROOT = Path(__file__).resolve().parent
PACKAGE = "com.jixiexiaoge.drivingassist.navdiag"


def adb_client_argv(device, *args: str) -> list[str]:
  return [device.adb, "-H", "127.0.0.1", "-P", str(device.port), *args]


class Device:
  def __init__(self, adb: str, port: int, serial: str | None, *, ownership: str = "dedicated"):
    if ownership not in ("dedicated", "attach"):
      raise ValueError("adb_ownership_invalid")
    self.adb, self.port, self.serial = adb, port, serial
    self.ownership = ownership
    self.started = False

  def host(self, *args: str, timeout: int = 20, check: bool = True) -> subprocess.CompletedProcess:
    command = [self.adb, "-P", str(self.port), *args] if args == ("start-server",) else adb_client_argv(self, *args)
    return subprocess.run(command, capture_output=True, text=True,
                          timeout=timeout, check=check)

  def __enter__(self):
    with socket.socket() as probe:
      probe.settimeout(0.2)
      listening = probe.connect_ex(("127.0.0.1", self.port)) == 0
    if self.ownership == "attach":
      if not listening:
        raise ValueError("adb_server_unavailable")
    elif listening:
      raise RuntimeError("专用 ADB 端口已占用，拒绝接管或关闭他人服务")
    else:
      self.host("start-server")
      self.started = True
    try:
      rows = []
      for _ in range(10):
        rows = [line.split() for line in self.host("devices").stdout.splitlines()[1:] if line.strip()]
        ready = [row[0] for row in rows if len(row) > 1 and row[1] == "device"]
        if self.serial and self.serial in ready:
          return self
        if not self.serial and len(ready) == 1:
          self.serial = ready[0]
          return self
        if not self.serial and len(ready) > 1:
          raise RuntimeError("已连接多台设备，请通过 --serial 明确选择")
        time.sleep(1)
      raise RuntimeError(f"ADB 尚未连接或授权：{[row[1] for row in rows if len(row) > 1]}")
    except BaseException:
      self.__exit__(None, None, None)
      raise

  def __exit__(self, *_):
    if self.started:
      self.host("kill-server", check=False)
      self.started = False

  def call(self, *args: str, timeout: int = 30, check: bool = True) -> subprocess.CompletedProcess:
    return self.host("-s", self.serial, *args, timeout=timeout, check=check)

  def shell(self, *args: str, timeout: int = 30, check: bool = True) -> subprocess.CompletedProcess:
    return self.call("shell", *args, timeout=timeout, check=check)


def choose_adb(value: str | None) -> str:
  candidates = [value, os.environ.get("NAVDIAG_ADB"), shutil.which("adb"),
                str(ROOT / "runtime/platform-tools/adb"),
                "/tmp/sunnypilot-navmap-adb-2_jap1p9/platform-tools/adb",
                "/data/android-sdk/platform-tools/adb"]
  for candidate in candidates:
    if candidate and Path(candidate).is_file():
      return candidate
  raise FileNotFoundError("未找到 adb，请指定 --adb 或 NAVDIAG_ADB")


def extract_capture(archive: Path, output: Path, *, expected_root: str = "navdiag-captures") -> None:
  if expected_root not in {"navdiag-captures", "phone-route-captures"}:
    raise ValueError("未知采集目录")
  with tarfile.open(archive, "r:") as handle:
    members = handle.getmembers()
    total = 0
    for member in members:
      path = Path(member.name)
      if path.is_absolute() or ".." in path.parts or not (member.isdir() or member.isfile()):
        raise ValueError("采集归档包含非法路径或链接")
      if not path.parts or path.parts[0] != expected_root:
        raise ValueError("归档超出采集目录")
      total += member.size
    if total > 1024 * 1024 * 1024:
      raise ValueError("归档超过 1 GiB，请先选择性导出")
    handle.extractall(output, members=members, filter="data")


def verify_selftest(output: str) -> None:
  passed = re.findall(r"^INSTRUMENTATION_RESULT: tests_passed=(\d+)\s*$", output, re.MULTILINE)
  failed = re.findall(r"^INSTRUMENTATION_RESULT: tests_failed=(\d+)\s*$", output, re.MULTILINE)
  codes = re.findall(r"^INSTRUMENTATION_CODE: (-?\d+)\s*$", output, re.MULTILINE)
  if (len(passed) != 1 or int(passed[0]) < 1 or failed != ["0"] or codes != ["-1"]
      or "INSTRUMENTATION_FAILED" in output or "INSTRUMENTATION_RESULT: error=" in output):
    raise RuntimeError("手机自测没有完整成功报告")


def capture_service_running(device: Device) -> bool:
  return _service_running(device, "CaptureService")


def bridge_service_running(device: Device) -> bool:
  return _service_running(device, "BridgeService")


def gps_bridge_service_running(device: Device) -> bool:
  return _service_running(device, "GpsBridgeService")


def phone_capture_service_running(device: Device) -> bool:
  return _service_running(device, "PhoneCaptureService")


def _service_running(device: Device, service: str) -> bool:
  result = device.shell("dumpsys", "activity", "services", PACKAGE)
  return any("ServiceRecord{" in line and any(
    component.rsplit(".", 1)[-1] == service
    for component in re.findall(re.escape(PACKAGE) + r"/([^}\s]+)", line))
    for line in result.stdout.splitlines())


def main() -> None:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("command", choices=["info", "install", "selftest", "bridge-selftest", "gps-selftest",
                                          "bridge-start", "bridge-stop", "gps-start", "gps-stop",
                                          "phone-selftest", "phone-pull",
                                          "start", "mark", "stop", "pull", "close"])
  parser.add_argument("--adb")
  parser.add_argument("--port", type=int, default=5039)
  parser.add_argument("--serial")
  parser.add_argument("--adb-ownership", choices=("dedicated", "attach"), default="dedicated")
  parser.add_argument("--label", default="manual")
  parser.add_argument("--apk", type=Path, default=ROOT / "build/navdiag-debug.apk")
  parser.add_argument("--out", type=Path)
  parser.add_argument("--root-check", action="store_true")
  args = parser.parse_args()
  if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", args.label):
    raise ValueError("标记仅支持 1–64 位字母、数字、下划线或短横线")
  options = {"ownership": "attach"} if args.adb_ownership == "attach" else {}
  with Device(choose_adb(args.adb), args.port, args.serial, **options) as device:
    if args.command == "info":
      report = {key: device.shell("getprop", key).stdout.strip() for key in
                ["ro.product.model", "ro.build.version.release", "ro.build.version.sdk", "ro.product.cpu.abilist"]}
      packages = {}
      for name in ["com.autonavi.amapauto", "com.autonavi.minimap", "com.jixiexiaoge.drivingassist",
                   "com.jixiexiaoge.drivingassist.debug", PACKAGE]:
        raw = device.shell("dumpsys", "package", name).stdout
        packages[name] = [line.strip() for line in raw.splitlines()
                          if "versionName=" in line or "versionCode=" in line]
      report["packages"] = packages
      if args.root_check:
        result = device.shell("su", "-c", "id", timeout=30, check=False)
        report["root"] = {"passed": result.returncode == 0 and bool(re.search(r"\buid=0\b", result.stdout)),
                          "returncode": result.returncode}
      print(json.dumps(report, ensure_ascii=False, indent=2))
    elif args.command == "install":
      if (capture_service_running(device) or bridge_service_running(device) or gps_bridge_service_running(device)
          or phone_capture_service_running(device)):
        raise RuntimeError("已有采集或观测桥运行，拒绝在记录过程中更新采集包")
      report = json.loads(args.apk.with_name("build-report.json").read_text())
      if report.get("package") != PACKAGE or report.get("sha256") != hashlib.sha256(args.apk.read_bytes()).hexdigest():
        raise ValueError("APK 与已验证的构建清单不一致")
      result = device.call("install", "--no-incremental", "-r", "-t", str(args.apk.resolve()), timeout=90)
      if "Success" not in result.stdout:
        raise RuntimeError(f"安装未成功：{result.stdout}\n{result.stderr}")
      print(result.stdout.strip())
    elif args.command in ("selftest", "bridge-selftest", "gps-selftest", "phone-selftest"):
      if (capture_service_running(device) or bridge_service_running(device) or gps_bridge_service_running(device)
          or phone_capture_service_running(device)):
        raise RuntimeError("已有采集或观测桥运行，请先正常结束再执行自测")
      try:
        runner = ("PhoneCaptureInstrumentation" if args.command == "phone-selftest" else
                  "GpsBridgeInstrumentation" if args.command == "gps-selftest" else
                  "BridgeInstrumentation" if args.command == "bridge-selftest" else
                  "CaptureInstrumentation")
        result = device.shell("am", "instrument", "-w", f"{PACKAGE}/.{runner}", timeout=90)
        print(result.stdout.strip())
        verify_selftest(result.stdout)
      finally:
        device.shell("am", "force-stop", PACKAGE, check=False)
    elif args.command in ("start", "mark", "stop", "bridge-start", "bridge-stop", "gps-start", "gps-stop"):
      result = device.shell("am", "start", "-W", "-n", f"{PACKAGE}/.MainActivity",
                            "--es", "command", args.command.replace("-", "_"), "--es", "label", args.label)
      print(result.stdout.strip())
      if "Error:" in result.stdout or "Exception" in result.stdout:
        raise RuntimeError("采集界面命令未成功")
    elif args.command == "close":
      if phone_capture_service_running(device):
        device.shell("am", "start", "-W", "-n", f"{PACKAGE}/.MainActivity", "--es", "command", "phone_stop")
      device.shell("am", "start", "-W", "-n", f"{PACKAGE}/.MainActivity", "--es", "command", "stop")
      if bridge_service_running(device):
        device.shell("am", "start", "-W", "-n", f"{PACKAGE}/.MainActivity", "--es", "command", "bridge_stop")
      if gps_bridge_service_running(device):
        device.shell("am", "start", "-W", "-n", f"{PACKAGE}/.MainActivity", "--es", "command", "gps_stop")
      deadline = time.monotonic() + 20
      while (capture_service_running(device) or bridge_service_running(device) or gps_bridge_service_running(device)
             or phone_capture_service_running(device)):
        if time.monotonic() >= deadline:
          raise RuntimeError("采集服务尚未完成保存，未强停；请稍后重试 close")
        time.sleep(0.25)
      device.shell("am", "force-stop", PACKAGE)
      print("采集包已停止")
    elif args.command in ("pull", "phone-pull"):
      if capture_service_running(device) or phone_capture_service_running(device):
        raise RuntimeError("采集服务仍在运行，请正常结束后导出，避免取得半写入文件")
      output = args.out or (ROOT / "captures" / datetime.now().strftime("%Y%m%d-%H%M%S"))
      output.mkdir(mode=0o700, parents=True, exist_ok=False)
      archive = output / "captures.tar"
      capture_root = "phone-route-captures" if args.command == "phone-pull" else "navdiag-captures"
      command = adb_client_argv(device, "-s", device.serial, "exec-out", "run-as", PACKAGE,
                                "tar", "-C", "files", "-cf", "-", capture_root)
      with archive.open("wb") as stream:
        result = subprocess.run(command, stdout=stream, stderr=subprocess.PIPE, timeout=90, check=True)
      archive.chmod(0o600)
      extract_capture(archive, output, expected_root=capture_root)
      print(json.dumps({"output": str(output.resolve()), "archive_bytes": archive.stat().st_size}, ensure_ascii=False))


if __name__ == "__main__":
  try:
    main()
  except subprocess.CalledProcessError as error:
    raise SystemExit(f"ADB 操作失败：{error.stdout}\n{error.stderr}") from error
