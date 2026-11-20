# CP 导航原始采集

本目录包含独立采集 APK，以及已接入 `navd` 的固定版本 Root 完整路线生产源。广播采集功能仍只保存自身数据；启用主机 `SUNNYPILOT_AMAP_ROOT_NAV=1` 后，独立只读通道读取当前选中路线和系统 GPS，由 `navd → mapsd → NavRuntime` 送入模型地图输入。**当前接入状态、验收证据、启用和回退方式见 [PHONE_ROUTE_INTEGRATION.md](PHONE_ROUTE_INTEGRATION.md)**。下文保留前期各阶段的原始采集方法，不代表当前完整路线能力上限。

## 手机独立采集（0.7.4）

“CP导航原始采集”现可直接在手机记录原始路线证据，开始后不需要 USB、小主机或电脑采样程序。此版本更新独立采集包，不更新 CP 搭子主应用。固定来源仍为同一手机上的高德车机版 `9.5.0.600013`，没有新增高德手机版适配。

0.7.4 针对真实移动时原始坐标、道路匹配坐标和时间戳同次更新的两种已记录组合，允许一次有界重读；后两次完整观测仍须完全一致，沿用原始50毫秒截止时间和累计读取预算。主机解析及诊断摘要同步支持新组合，不放行索引、选路或动作变化。保留0.7.3的原生坐标数组实际长度校验，以及整趟采集、自动分段与ZIP导出。历史换路后51.39秒几何缺口的深层原因仍未确定，本次没有修复该缺口；新增重读条件的自然恢复效果待正常出行验证，详情见接入文档末节。

1. 首次使用允许采集器自身的 Root 和精确定位权限，打开手机系统 GPS。ADB Shell 有 Root 不代表采集器已经获准，Magisk 中需为“CP导航原始采集”单独授权。
2. 停车时先在高德设好真实导航，再打开采集器，点击顶部 **“开始整趟采集（最多2小时）”**，确认记录数、原生观察和新鲜 GPS 计数增长后切回高德。需要短测时仍可选“开始完整采集（180秒）”。
3. 正常出行期间无需操作采集器；整趟模式每3分钟自动切换记录文件，帧号和会话身份连续，到2小时自动结束。停车后也可点“结束完整采集”或通知栏“结束并保存”。达到空间或文件限额会提前结束并保留记录；不自动开始、不自启、不在系统杀进程后续写旧会话。
4. 结束并保存后，点 **“导出最近完成的完整采集（ZIP）”**，通过系统文件选择器保存到本机位置。导出前校验每个文件的大小和 SHA256，原始记录仍留在应用私有目录。

不要误点下方旧的“开始180秒采集”：它只记录广播。完整采集额外保存 Native-before → GPS → Native-after、选中路线几何、位置索引和原始动作观测。GPS 为本应用的真实 `gps` 回调，按每次原生观察对保存最新 fix；250 ms 是请求采样间隔，不保证手机 GPS 以 4 Hz 更新。不接受网络、融合或模拟定位，不把发布时间当作定位源时间。

短测最多180秒，整趟最多2小时；每轮仍限256 MiB、1024个文件，私有采集累计上限仍为1 GiB。低空间时明确失败，不自动删除旧记录。归档目录为 `files/phone-route-captures/<会话ID>/`，包含 `manifest.json`、`frames.jsonl`、`broadcasts.jsonl`、按SHA去重的 `geometry/*.bin`、`summary.json` 和 `completed.json`。整趟模式另有 `segments.json` 与 `frames-0002.jsonl` / `broadcasts-0002.jsonl` 等后续文件，最多40段。分段按写入时刻划分，排队中的广播保留自己的原始接收时间；不会重新编号或刷新源时间。整趟ZIP包含全部分段与共用几何，不需要逐段导出。

`completed.json` 只表示归档正常收尾；`summary.failure`、源端失败原因、GPS新鲜度与Root关闭回执必须另行检查。没有导航或定位不合格时也保留真实失败记录。短测沿用schema1，整趟使用schema2；当前校验工具同时支持两者，旧版工具会明确拒绝schema2，不会误读成只含第一段。

手机 ZIP 或已导出的会话目录可离线校验，结果只输出计数和原因，不打印坐标或目的地：

```bash
/data/sunnypilot/.venv/bin/python /data/sunnypilot/APP/navdiag/phone_capture_archive.py <ZIP文件或会话目录>
```

工具检查 SHA、文件集合、原始手机时钟顺序、选路/进程身份、几何和 GPS 声明，并统计同一路线的 point/link/segment 变化。手机原始证据不平移为主机时钟，不代表已执行 `navd/mapsd/NavRuntime`、GPU 或车辆控制验收。

`paired_source_frames`沿用手机归档的原始计数口径；新增`stable_index_source_frames`要求同帧前后位置索引一致且为非负整数。动态比较还要求相邻帧使用同一份几何，缺口或索引不稳立即中断比较；这些计数仍不等同于空间门通过或模型实际接收。`link_transitions`包含跨segment导致的link键变化，与`segment_transitions`存在交集，不应直接相加。

`geometry_diagnostics`统计几何原因出现的帧数，解析采集器已保存的`_geometry_rejection`。它校验字段、原始手机时钟及帧内顺序，按相邻同一断点分组，输出段/link索引、端点近似间距、样本窗口跨度和最大读取耗时/预算；最多保留128组详细记录，超出数单列。报告不打印原始坐标、目的地或原生路线身份。旧记录缺断点上下文时明确计入`missing_context_frames`；几何原因帧数不是实际重试次数，帧身份匹配也不证明被拒源快照一致。

已有 5039 ADB 服务时，维护工具需显式附加，避免启动或关闭他人的服务：

```bash
/data/sunnypilot/.venv/bin/python /data/sunnypilot/APP/navdiag/device.py phone-selftest --port 5039 --serial <serial> --adb-ownership attach
/data/sunnypilot/.venv/bin/python /data/sunnypilot/APP/navdiag/device.py phone-pull --port 5039 --serial <serial> --adb-ownership attach --out <尚不存在的新目录>
```

自测使用隔离合成数据；更新、自测和导出均会拒绝正在运行的完整采集。普通 ADB 客户端固定使用 `-H 127.0.0.1 -P <port>`，现有服务消失时直接失败，不隐式启动另一个 daemon。

## 安装范围与采集边界

- 包名为 `com.jixiexiaoge.drivingassist.navdiag`，显示名称为“CP导航原始采集”。它与原 CP 搭子的正式包及 `.debug` 包分开安装，不替换原应用。
- 最低及目标 API 均为 29。APK 申请 `FOREGROUND_SERVICE`、`WAKE_LOCK`、`ACCESS_COARSE_LOCATION` 和 `ACCESS_FINE_LOCATION`，没有 `INTERNET`、外部存储或开机自启权限。
- 下述原始广播采集必须手动开始，每次最多采集 **180 秒、64 MiB**；单条记录上限 2 MiB，待写队列上限 32 条，嵌套深度上限 16。达到时间或容量上限后结束会话；顶部新增的完整采集使用上一节的独立限额。
- 接收线程只记时间戳并入队；后台线程负责带类型编码和写盘。数组会保存实际元素，未知类型、无法解码、深度/大小截断和丢包都会明确记录。
- 采集期间保持前台通知，可从界面或通知结束。退出界面后仍受所选模式时限约束；不会开机自启或自动恢复旧会话。下方原广播采集仍固定180秒。
- 接收广播、自测和通过 `run-as` 导出自身数据不需要 Root；完整路线另需已经授权的 Root 只读通道。完整采集和 GPS 桥会以本应用身份请求 GPS 更新，不修改高德内容或系统定位设置。

动态接收器监听以下 10 个 action：

```text
AUTONAVI_STANDARD_BROADCAST_SEND
AUTONAVI_STANDARD_BROADCAST_RECV
AMAP_BROADCAST_SEND
AUTONAVI_BROADCAST_SEND
com.autonavi.minimap.broadcast
com.autonavi.minimap.navigation.broadcast
AMAP_NAVI_ACTION_UPDATE
AMAP_NAVI_ACTION_TURN
AMAP_NAVI_ACTION_ROUTE
AMAP_NAVI_ACTION_LOCATION
```

**监听 action 不等于覆盖所有广播。** 发往其他包或组件的定向广播、要求额外权限的广播、未注册的 action 均不保证可见。清单固定记录 `targetedBroadcastCoverage=unknown`，发送者身份记录为 `unverified`；不能仅凭 action 名称认定来自高德。收到零条广播或未发现坐标数组，也不能推出“高德不发送路线”。

## 构建

使用主机现有 Java 和 Android 工具，不加载原 CP 搭子的 Gradle、Kotlin、MNN 或车辆控制组件：

```bash
/data/sunnypilot/.venv/bin/python /data/sunnypilot/APP/navdiag/build.py
```

默认依赖位置：

| 工具 | 位置 |
| --- | --- |
| API 29 平台库 | `/data/android-sdk/android-10/android.jar` |
| `aapt2`、`aapt`、`d8`、`zipalign`、`apksigner` | `/data/android-sdk/android-14/` |
| `javac`、`java`、`keytool` | 主机 `PATH` |

可用 `--android-jar`、`--build-tools`、`--out`、`--keystore` 覆盖路径。默认产物为：

```text
/data/sunnypilot/APP/navdiag/build/navdiag-debug.apk
/data/sunnypilot/APP/navdiag/build/build-report.json
```

构建报告包含 APK 和源码 SHA256、权限列表及签名验证结果。安装工具会核对 APK 与相邻构建报告的包名和哈希。本地测试签名保存在 `~/.local/share/sunnypilot/navdiag/debug.keystore`，仅用于这个独立测试包。

## 连接检查、安装与自测

手机连接小主机并允许 USB 调试后执行：

```bash
/data/sunnypilot/.venv/bin/python /data/sunnypilot/APP/navdiag/device.py info
/data/sunnypilot/.venv/bin/python /data/sunnypilot/APP/navdiag/device.py info --root-check
/data/sunnypilot/.venv/bin/python /data/sunnypilot/APP/navdiag/device.py install
/data/sunnypilot/.venv/bin/python /data/sunnypilot/APP/navdiag/device.py selftest
```

`info` 只报告系统属性和相关应用版本；`--root-check` 额外执行 `su -c id` 并报告是否取得 `uid=0`，不读取其他应用的内容。

`selftest` 使用独立的 `files/navdiag-selftest/` 目录和合成输入，检查编码、记录及会话边界，不把合成数据混入真实采集目录。成功报告必须包含正数 `tests_passed`、`tests_failed=0` 和 `INSTRUMENTATION_CODE: -1`。它不会证明高德已发送完整路线。已有采集会话运行时，先正常结束再执行自测。

安装更新、运行自测和导出都会检查采集服务；正在记录时拒绝执行，避免中断采集或导出半写入文件。

设备工具默认使用专用 ADB 端口 **5039**，执行后只关闭本次启动的 ADB 服务；端口已被占用会拒绝接管。使用已有服务时必须增加 `--adb-ownership attach`，该模式不启动也不关闭服务。可通过 `--adb` 或 `NAVDIAG_ADB` 指定 ADB，通过 `--port` 指定端口。连接多台设备时必须用 `--serial` 指定目标。

## 采集真实导航广播

在车辆静止时打开“CP导航原始采集”，点击“开始180秒采集”，再切换到高德完成规划路线、开始导航、切换路线和取消导航。不要提前启动采集后长时间等待操作，以免耗尽采集窗口。

也可以从主机操作采集器：

```bash
/data/sunnypilot/.venv/bin/python /data/sunnypilot/APP/navdiag/device.py start
/data/sunnypilot/.venv/bin/python /data/sunnypilot/APP/navdiag/device.py mark --label route_plan
/data/sunnypilot/.venv/bin/python /data/sunnypilot/APP/navdiag/device.py mark --label route_start
/data/sunnypilot/.venv/bin/python /data/sunnypilot/APP/navdiag/device.py mark --label route_change
/data/sunnypilot/.venv/bin/python /data/sunnypilot/APP/navdiag/device.py mark --label route_cancel
/data/sunnypilot/.venv/bin/python /data/sunnypilot/APP/navdiag/device.py stop
```

每个 `mark` 应在相应操作附近执行，仅写入时间标记，不会操作高德或车辆。命令行标记允许 1–64 位字母、数字、下划线或短横线；界面也提供中文标记按钮。界面显示收到、写入、不完整、截断、丢弃和解码错误计数。

`stop` 请求结束并由后台线程排空队列。实验结束后使用 `close` 收尾：

```bash
/data/sunnypilot/.venv/bin/python /data/sunnypilot/APP/navdiag/device.py close
```

`close` 先结束采集，等待服务自行退出后才强停独立采集包。若 20 秒内仍未退出，它会报错并保留服务，不用强停截断正在保存的数据；稍后重试并检查导出结果。该命令不关闭原 CP 搭子或高德。

## 导出与离线分析

先结束会话再导出。`--out` 必须是尚不存在的新目录：

```bash
/data/sunnypilot/.venv/bin/python /data/sunnypilot/APP/navdiag/device.py pull --out /data/sunnypilot/APP/navdiag/captures/manual-001
```

导出使用 `run-as com.jixiexiaoge.drivingassist.navdiag`，仅读取本包的 `files/navdiag-captures/`。它会留下 `captures.tar` 和解压后的 `navdiag-captures/<会话ID>/`，拒绝归档中的路径逃逸、符号链接及超出采集范围的内容。

每个会话包含：

| 文件 | 内容 |
| --- | --- |
| `manifest.json` | 会话、设备及相关应用版本、边界、计数和完成状态 |
| `broadcasts.jsonl` | action、目标包/组件、类型化 extras、双时间戳、序列号及编码问题 |
| `events.jsonl` | 开始、标记、丢包、截断、解码错误和结束事件 |
| `completed.json` | 正常收尾后的完成标记、最终计数和文件 SHA256 |

将下面命令中的 `<会话ID>` 替换为导出目录内的实际名称：

```bash
/data/sunnypilot/.venv/bin/python /data/sunnypilot/tools/cplink_shim/nav_capture_analyzer.py "/data/sunnypilot/APP/navdiag/captures/manual-001/navdiag-captures/<会话ID>"
```

分析器校验完成标记、哈希、记录计数和序列连续性，再检查嵌套 Bundle、数组及 JSON 字符串内的路线几何候选。输出保留字段路径、候选点数和失败原因，不打印原始坐标或目的地值。

- `integrity.status=complete` 只表示导出中的已接收记录通过完整性检查，不表示接收器看到了高德的所有数据。
- `status=available` 只表示完整采集中存在符合坐标数组结构的候选；`complete_route_verified` 仍为 `false`，坐标系和来源归属仍待核实。
- `status=insufficient_evidence` 表示当前证据不足，应检查 `integrity.reasons`、计数及候选原因。不能据此伪造 `polyline` 或直接解除主机地图输入隔离。

后续接入 `nav_map` 前，需要用实际导航切换验证路线身份、坐标系、完整路线范围和更新时序，再实现独立路线几何传输与主机门控。

## 限时广播观测桥（0.2.0）

原有 180 秒全量采集和 12 项 `CaptureInstrumentation` 保持独立。新增的
`BridgeService` 仅监听 `AUTONAVI_STANDARD_BROADCAST_SEND`，仅保存最新 `10001`、`10019`
的原始 typed Intent，不解读当前路线身份，也不发送网络请求。界面和通知可手动停止，
每次最多 **2 小时**，不支持开机自启或系统杀进程后自动恢复。

只读主机入口为本包私有文件：

```text
files/navdiag-bridge/latest.json
```

每次写入先同步临时文件，再通过同目录 `rename` 原子替换；最终文件硬上限 **2 MiB**。
接收回调只保存本次接收时间和 Intent 副本到 8 条有界队列，后台线程才解析和写盘。
队列溢出、解码不完整、注册失败、写入失败、手动停止或时长到期均停止会话并写入
`active=false, valid=false`。无法写入无效快照时删除旧快照，主机对文件缺失必须判不可用。

`sessionId` 是每次新建的 UUID；每条记录均有原始 `sequence`、`receivedElapsedNs`、
`receivedWallMs`、typed `rawIntent`、`complete` 和 `decodeErrors`，编码问题在
`rawIntent.issues` 中保留。两个广播类型各自保存最后一次真实接收时间。
500 ms 定时发布仅更新 `publishedElapsedNs`、`publishedWallMs` 和 `sourceAgeMs`，
**不会更新源时间戳或接收序号**。主机必须用源时间检查时效，不能用文件 mtime 或发布时间续期。

这里 `active` 只表示 receiver 会话开启，`valid` 只表示已收到的传输记录完整且会话未失败；
不表示高德正在导航或记录仍然新鲜。文件始终有 `freshnessChecked=false`、
`senderVerified=false`、`activeRouteVerified=false`、`controlInputAllowed=false`。
广播 `10001` 自带的状态不能将这些字段提升为 true。Root 路线身份和坐标系验证在主机另行完成。

先停止原始采集和观测桥，再更新 APK 或运行仪器测试。可直接在界面操作；已有专用 ADB
连接时也可发送界面命令（将 `<serial>` 替换为明确选定的设备）：

```bash
adb -P 5039 -s <serial> shell am start -W -n com.jixiexiaoge.drivingassist.navdiag/.MainActivity --es command bridge_start
adb -P 5039 -s <serial> shell am start -W -n com.jixiexiaoge.drivingassist.navdiag/.MainActivity --es command bridge_stop
adb -P 5039 -s <serial> shell run-as com.jixiexiaoge.drivingassist.navdiag cat files/navdiag-bridge/latest.json
adb -P 5039 -s <serial> shell am instrument -w com.jixiexiaoge.drivingassist.navdiag/.BridgeInstrumentation
```

最后一条运行新增的 12 项合成仪器测试，覆盖大数据、停止、源时间不续期、队列溢出、
注册失败结束状态、会话限时及并发读取原子 JSON。使用独立
`files/navdiag-bridge-selftest/`，不向高德发广播，也不把合成数据写进观测桥正式文件。
原始采集的 `device.py selftest` 继续运行原来的 `CaptureInstrumentation`；两组应分别验收。

## 本地数据与回退

原始广播可能包含位置、目的地和路线信息。手机数据只写入采集包私有目录，工具没有网络权限；导出后原始 JSONL 和归档仍是本地敏感数据，不应加入 Git 或上传。`build/`、`captures/` 和 `__pycache__/` 已由本目录 `.gitignore` 排除。

本机离线工具回归命令：

```bash
cd /data/sunnypilot
/data/sunnypilot/.venv/bin/python -m pytest -q APP/navdiag/test_tools.py tools/cplink_shim/tests/test_nav_capture_analyzer.py
/data/sunnypilot/.venv/bin/ruff check APP/navdiag tools/cplink_shim/nav_capture_analyzer.py tools/cplink_shim/tests/test_nav_capture_analyzer.py
```

回退时先运行 `device.py close`。需要彻底移除时，在手机系统设置中仅卸载“CP导航原始采集”（`com.jixiexiaoge.drivingassist.navdiag`）；卸载会删除该包私有采集数据，因此需要保留的会话应先导出。不要卸载原 `com.jixiexiaoge.drivingassist`、`com.jixiexiaoge.drivingassist.debug` 或高德。该采集工具没有修改车辆控制、Panda 固件、模型或生产 `nav_map` 配置，无需为移除它重启行车栈。

## 2026-11-15 真机验收

- MXW-AN00 / Android 10 API 29：USB 调试可用，`su -c id` 确认 Root 可用。
- APK 已构建、签名校验并安装，独立包版本 `0.1.0`；原 CP `.debug` 和高德车机版保留。
- 12 项真机 instrumentation 自测全部通过，结果在 `build/selftest-final.txt`。
- 接收器启动失败误报完整的问题，修复前同一回归测试确实失败，见 `build/selftest-before-fix.txt`；修复后通过。
- 真机产生的最后 7 组自测会话导出后，主机分析器验证所有文件哈希匹配，3 组完整、4 组主动制造的不完整均判定正确，报告在 `build/android-selftest-export/crosscheck.json`。
- Python 回归 47 项通过，Ruff 通过。上述合成数据不证明高德已提供完整路线，真实广播另行采集分析。

### 第一轮真实广播

2026-11-15 本地时间采集，导出目录 `captures/20261115-013947/`：39.467 秒，152 条收到并写入，0 丢包、0 截断、0 解码错误，所有文件哈希匹配。`analysis.json` 为完整性与几何检查结果，`field-schema.json` 只列字段和类型。

- 57 条 `10001`，每条 66 个字段：导航指令、距离/时间、当前点与终点等；没有路线折线。
- 6 条 `13011`：JSON 内 `tmc_info[]` 为路况分段摘要，每项仅编号、拥堵状态、距离、时间、百分比。
- 1 条 `10056`：两条备选路线的统计及道路名摘要，只有端点类标量坐标，没有完整道路坐标序列。
- 1 条 `12102`：途经点数组为空。`10110` 为单个入口/POI 点。

这些 JSON 字符串已实际展开检查，不是数组被截断或分析器没有解码。本轮没有 `10003`，事件文件没有逐项操作标记，定向广播覆盖仍不确定；结论限于“本轮收到的广播没有完整路线几何”。原始广播可能含个人位置，不在文档展示具体值。生产 `nav_map` 仍未启用。

## Root 只读采样临时导航文件

`root_probe.py` 用于在导航仍进行时采样可能短暂存在的文件，唯一允许的手机路径为：

```text
/data/user/0/com.autonavi.amapauto/files/naviPathInfo.json
```

脚本没有任意私有路径参数，不扫描其他目录、不修改或删除手机文件，也不启动、停止高德。需要手机已授权 ADB shell 使用 `su`。虽然文件名以 `.json` 结尾，采样始终按原始字节处理；不能假定其是 UTF-8 或 JSON。

在手机保持导航时，从主机启动：

```bash
/data/sunnypilot/.venv/bin/python /data/sunnypilot/APP/navdiag/root_probe.py --duration 60 --out /data/sunnypilot/APP/navdiag/captures/root-probe-001
```

`--out` 必须是不存在的新目录，默认在本目录的 `captures/root-probe-<时间>/` 下创建。时长默认 60 秒，可选 1–180 秒；正常每秒最多读取一次，慢读或超时不会补发密集读取。复用 `device.Device` 的专用 5039 端口与退出清理，支持 `--adb`、`--port`、`--serial`。

每次读取使用禁用 PTY 的 `adb shell -T` 和 Python `bytes`，检查本地及远端读取返回码。本机实测 `exec-out su` 会将 LF 改成 CRLF，不能用于这个 Root 二进制通道。远端 `head` 最多读取 4 MiB 加 1 字节，以识别读取过程中增长或超限的文件。大于 4 MiB 的样本不保存为截断副本。缺失、空文件、不可读、非普通文件、符号链接、超时、读取失败和超限均记为事件，不伪造空 JSON 或完整路线。

不同 SHA256 的样本保存为 `sample-<序号>-<SHA256>.bin`；重复内容只记事件，不重复保存。总目录预算为 64 MiB，其中预留 128 KiB 给报告；目录权限为 `0700`，样本和 `report.json` 为 `0600`。报告包含事件、样本哈希、大小和本地文件名；终端只输出统计和报告路径，不打印内容或坐标。手机原文件可能在读取时发生变化，所以报告始终保留 `source_complete_verified=false` 和 `complete_route_verified=false`，取得字节本身不代表已确认完整路线。

`Ctrl+C` 会停止采样、保存现有事件报告并退出本次 ADB 服务。已导出的本地原始样本可能包含个人位置，应和广播原始文件一样仅在本地保留。

离线测试命令（不连接手机）：

```bash
cd /data/sunnypilot
/data/sunnypilot/.venv/bin/python -m pytest -q APP/navdiag/test_root_probe.py
/data/sunnypilot/.venv/bin/ruff check APP/navdiag/root_probe.py APP/navdiag/test_root_probe.py
```

### Root 通道与缓存实测

2026-11-15 在同一台手机完成二进制对照：预期字节 `ff000a0d00410a` 经 `shell -T` 原样返回；先前 `exec-out su` 会将 LF 转成 CRLF，产生的无效采样不作为文件证据。

有效采样在 `captures/root-probe-valid-20261115/`：临时导航文件连续 10 秒取得 10 次成功读取，SHA256 相同，去重后保存 1 份 448 字节样本。该文件不是普通 JSON，尚未解析为路线。

另只读导出了本轮发现的 `amapauto9/path_cache/964634650.dat`，121444 字节，读取前后大小/修改时间一致。其 `BL-CRRD` 容器声明载荷 121428 字节，与实际一致；声明 CRC32 `def22b98` 与实际载荷 CRC32 一致。文件完整性可检查，但当前路线身份、坐标解码和完整路线范围尚未确认，不能接入控制输入。

可重跑本地容器检查（不会连接手机）：

```bash
/data/sunnypilot/.venv/bin/python /data/sunnypilot/tools/cplink_shim/nav_cache_inspector.py /data/sunnypilot/APP/navdiag/captures/root-probe-valid-20261115/route-path-cache.dat
```

后续应先完成缓存内路线结构的解码及与真实规划/换路线事件的身份匹配，再实现手机传输与主机 `nav_map` 门控；不能把容器 CRC 校验通过当作可用路线的证明。

离线检查当前安装 APK 的 `libGbl.so` 后，已定位临时导航文件的 JSON 序列化→字节转换→写盘，以及读盘→逆向转换→JSON 解析调用边界；未认定具体转换算法。路线缓存中的三个 Zstandard 帧可正常解压，但解压结果仍是未识别二进制。细节保存在本地 `captures/root-probe-valid-20261115/format-check-summary.json` 与 `build/amap-source-check/native-review/native-format-summary.json`，没有将未验证字段映射为坐标。

本轮最终联合回归：84 项 Python 测试通过，Ruff 通过；手机 instrumentation 12 项通过。临时 ADB 与构建进程已检查无残留，未做 commit/push。
