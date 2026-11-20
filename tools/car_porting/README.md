# 宋 PLUS DM-i ALT 离线检查

当前仅保留 `BYD_SONG_PLUS_DMI_23_ALT` 的车型实现。其他车型的移植示例和 ECU 禁用入口已移除。
本车型使用 CAN 指纹识别，不提供自动写入固件指纹的工具。

## 车型接口与安全回归

在项目根目录运行：

```bash
PYTHONPATH=/data/sunnypilot .venv/bin/python -m pytest \
  openpilot/selfdrive/car/tests \
  opendbc_repo/opendbc/car/byd/tests \
  opendbc_repo/opendbc/safety/tests/test_byd.py -q
```

## 本地日志分析

现有工具位于 `openpilot/tools/car_porting/`：

- `byd_route_audit.py`：检查本地日志中的报文与状态证据。
- `byd_longitudinal_audit.py`：分析纵向 ACC 请求及相关状态。
- `byd_lateral_replay.py`：离线分析横向控制与响应。

先查看相应工具的 `--help`，再传入本地日志。离线结果只覆盖已记录的数据。

## CAN 查看

`openpilot/tools/cabana/cabana` 可用 `byd_song_plus_dmi_23_alt` DBC 查看本车报文。
使用本地日志路径，避免将本地路线标识填入公开回放清单。

公共进程回放尚未配置本车型的参考日志，入口会明确报错退出。
