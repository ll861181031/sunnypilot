# Linux x86_64 / AMD64 mapd

本机使用与中国离线地图匹配的 **mapd v1.12.0**。本地构建是静态链接的
Linux x86_64 可执行文件，使用通用 GOAMD64=v1 指令集，不需要 ARM 模拟器。

## 安装位置和启动

- 本机程序：`$OPENPILOT_ROOT/openpilot/third_party/mapd_pfeiferj/mapd-linux-amd64`
- 构建记录：同目录 `mapd-linux-amd64.json`，包含源码、补丁、二进制 SHA256 和编译器版本。
- 原 ARM64 文件：同目录 `mapd`，保留给 comma 设备及原有版本校验。
- 本机地图：`$HOME/.comma/media/0/osm/offline`

正常启动本项目时，manager 会校验本机程序的 ELF 架构、版本和 SHA256，
然后启用 `mapd` 和 `mapd_manager`。安装器在 x86_64 上不会下载 ARM64 文件来覆盖本机版本。
程序缺失、损坏或版本不匹配时不会启动，应重新构建。

需要单独运行 mapd 时，使用项目的路径适配入口，Ctrl+C 停止：

```bash
cd "$OPENPILOT_ROOT"
"$OPENPILOT_ROOT/.venv/bin/python" -m openpilot.sunnypilot.mapd.runtime
```

此入口只启动地图计算程序。正常 sunnypilot 的 `mapd_manager` 负责把
`liveLocationKalman` 写入 mapd 的输入，并把结果发布成 `liveMapDataSP`。
不要在完整 sunnypilot 已启动时额外启动第二个实例。

适配入口通过以下环境变量传递当前路径：

| 变量 | 内容 |
| --- | --- |
| `MAPD_ROOT` | `Paths.mapd_root()`，包含 `offline/` |
| `MAPD_PARAMS_PATH` | `Params().get_param_path()`，持久参数目录 |
| `MAPD_MEM_PARAMS_PATH` | `Params("/dev/shm/params").get_param_path()`，共享内存参数目录 |

参数和消息隔离支持 `OPENPILOT_PREFIX`，不假定目录名始终为 `d`。

## 重新构建

已校验的 Go 编译器位于本机缓存，可直接复用：

```bash
cd "$OPENPILOT_ROOT"
"$OPENPILOT_ROOT/.venv/bin/python" -m openpilot.sunnypilot.mapd.build_native \
  --go "$HOME/.cache/sunnypilot/mapd/v1.12.0/go1.27.1/go/bin/go"
```

也可将 `--go` 改成其他已安装 Go 编译器的绝对路径。构建脚本固定源码版本和
SHA256，校验依赖，应用 `native_paths.patch`，通过 Go 测试后才安装。
补丁增加路径环境变量，并把两项依赖 ARM 浮点文本快照的测试改成原期望值的容差比较。
地图匹配算法未修改。构建产物被 Git 忽略，可用上述命令重新生成。

## 实际离线验证

以下测试使用已安装的程序和中国地图，在独立的参数目录和消息通道中运行
`mapd` 与 `mapd_manager`，输入模拟定位，不启动车辆控制进程，也不修改正式 GPS 参数：

```bash
cd "$OPENPILOT_ROOT"
MAPD_RUN_INTEGRATION=1 "$OPENPILOT_ROOT/.venv/bin/python" -m pytest -q -s \
  openpilot/sunnypilot/mapd/tests/test_native_integration.py
```

测试检查地图数据中的三个公开路段：北京南五环 100 km/h、上海沪闵高架路
80 km/h、广州琶洲大桥 60 km/h；这些数值是地图数据中的限速，不代表对现场标志的验证。
同时检查重启恢复、无 GPS、定位未初始化、定位消息停止及缺少地图时的输出。

地图消息只有在定位消息及时、有效、定位器已初始化且 GPS 正常时才有效；
定位失效时清空路名和限速输出。真实实时查询仍需本机持续提供有效的
`liveLocationKalman`。上述测试不验证实际 GNSS 硬件、实车定位精度或车辆控制。
