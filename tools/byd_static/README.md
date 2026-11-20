# BYD 纵向镜像工具已退役

`static_validate.py` 仅保留兼容入口。任何调用（包括无参数和帮助请求）
都会输出退役提示并以状态码 `2` 退出；旧参数不再受理。
入口不读取输入文件、不联网、不导入 Panda、不连接硬件，也不发送 CAN。

旧工具引用了当前不存在的 `opendbc.car.byd.longitudinal_mirror`。
历史镜像判决、逐字节一致性结果和实时抓包结果不能证明当前规划器、
控制器、报文构造或 safety 链路正确，也不能证明 ECU 接受及执行指令。

## 离线替代工具

当前离线审计入口是 `openpilot/tools/car_porting/byd_longitudinal_audit.py`。
它接收一个或多个已有的本地 rlog，不接收旧 CSV 镜像输入或网络地址。

```bash
cd /data/sunnypilot
.venv/bin/python openpilot/tools/car_porting/byd_longitudinal_audit.py --help
.venv/bin/python openpilot/tools/car_porting/byd_longitudinal_audit.py \
  /path/to/completed/rlog.zst --output /path/to/new-report.json
```

`--output` 可省略，省略时报告写入标准输出；指定时只创建新文件，
不会覆盖已有报告。输入必须是已完成的本地记录，带 `rlog.lock` 的记录会被拒绝。

报告仅提供 DBC 假设和接收帧的离线观察，`control_ready` 保持 `false`。
这些证据不能替代当前控制链测试或执行器验证。
