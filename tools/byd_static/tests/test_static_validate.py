import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "static_validate.py"
AUDIT_TOOL = "openpilot/tools/car_porting/byd_longitudinal_audit.py"
GUARDED_CLI = """
import sys

with open(sys.argv[1], encoding="utf-8") as source:
  code = compile(source.read(), sys.argv[1], "exec")
sys.argv = sys.argv[1:]

def refuse_external_access(event, args):
  if event == "open" or event.startswith(("socket.", "subprocess.")) or event in (
    "os.system", "os.exec", "os.posix_spawn", "ctypes.dlopen"
  ):
    raise AssertionError(f"禁止外部访问: {event}")
  if event == "import" and args[0].partition(".")[0] in ("panda", "usb1", "serial", "opendbc", "openpilot"):
    raise AssertionError(f"禁止加载车辆依赖: {args[0]}")

sys.addaudithook(refuse_external_access)
exec(code, {"__name__": "__main__", "__file__": sys.argv[0]})
"""


@pytest.mark.parametrize("args", [
  (),
  ("--help",),
  ("--live",),
  ("--send",),
  ("--live", "--send"),
  ("--replay", "/must-not-open/capture.csv"),
  ("--replay", "https://example.invalid/capture.csv"),
])
@pytest.mark.parametrize("guard_io", [False, True])
def test_retired_cli_rejects_invocation_without_external_access(args, guard_io):
  # 守卫在读取入口源码后安装，覆盖入口运行期间的文件、网络和硬件访问。
  command = [sys.executable, "-I", "-S"]
  if guard_io:
    command += ["-c", GUARDED_CLI]
  result = subprocess.run(command + [str(SCRIPT), *args], capture_output=True, text=True, timeout=5)

  assert result.returncode == 2
  assert result.stdout == ""
  assert "已退役" in result.stderr
  assert AUDIT_TOOL in result.stderr
  assert "Traceback" not in result.stderr
