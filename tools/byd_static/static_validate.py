#!/usr/bin/env python3
"""已退役的 BYD 纵向镜像工具兼容入口。"""

import sys


def main() -> int:
  print(
    "此 BYD 纵向镜像工具已退役，不再接受任何参数，退出状态为 2。",
    "本入口不读取输入文件、不联网、不连接 Panda，也不发送 CAN。",
    "请改用离线工具：openpilot/tools/car_porting/byd_longitudinal_audit.py",
    "运行该工具的 --help 查看本地 rlog 和 --output 用法。",
    "旧镜像结果不能证明当前控制链；离线审计也不证明 ECU 接受或执行指令。",
    sep="\n",
    file=sys.stderr,
  )
  return 2


if __name__ == "__main__":
  raise SystemExit(main())
