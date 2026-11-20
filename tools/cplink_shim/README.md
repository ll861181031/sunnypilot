# cplink_shim — CPlink（CP搭子 / navipilot）车端兼容层

一句话：这一步只做车端——用 `/data/sunnypilot/.venv/bin/python shim.py` 起一个进程，同时承担 **UDP 7705 每秒广播**、**UDP 7706 心跳/命令接收（落盘 `/tmp/cplink_shim_7706_last.json`）**、**7000 HTTP+WS（`/api/heartbeat_status` 等接口 + `/ws/compact_state` + `/api/compact_state.json`）**、**7714 navi v2 WS（control 通道 + 13 条 json 流）**，让鸿蒙「CP搭子」能发现并连上这台 sunnypilot。

## 端口

| 端口 | 方向 | 用途 |
|---|---|---|
| UDP 7705 | 车端 → 手机（`255.255.255.255` + `192.168.123.255`，每秒） | `{"ip":"192.168.123.70","name":"openpilot","port":7000,"proto":"cplink"}` |
| UDP 7706 | 手机 → 车端 | 心跳 `{"carrotIndex","epochTime","timezone"}`、命令（`carrotCmd*`）、目的地（`goalPos*`/`szGoalName`） |
| TCP 7000 | 手机 → 车端 | `GET /api/heartbeat_status`、`GET /api/params_bulk?names=A,B`、`POST /api/param_set`、`GET /api/egpu/model`、`GET /api/settings/snapshot`、`WS /ws/compact_state`、`GET /api/status`（shim 诊断）、`GET /api/compact_state.json`（compact 快照） |
| TCP 7714 | 手机 → 车端 | `WS /api/navi/ws/v2/control/amap_auto_1.0`、`WS /api/navi/ws/v2/json/{session_id}/{stream_name}` |

## 启动 / 停止

```bash
systemctl --user enable --now cplink-shim.service     # 开机自启（当前登录会话内）
systemctl --user status cplink-shim --no-pager | head -20
journalctl --user -u cplink-shim -f                   # 跟踪日志
systemctl --user restart cplink-shim                  # 改代码后重启
```

前台调试（不用 systemd，Ctrl-C 退出）：

```bash
cd /data/sunnypilot/tools/cplink_shim && /data/sunnypilot/.venv/bin/python shim.py --log-level DEBUG
```

可选：`sudo loginctl enable-linger lsr`，让 `--user` 服务在没人登录时也随开机启动（本层没替你改这个开关）。

## 自测

```bash
# 1) 端口与进程（7000/7705/7706/7714 只应出现本服务）
ss -lntup | grep -E ':(7000|7705|7706|7714)\b'

# 2) 心跳（必须 HTTP 200 且 ok=true，响应 ≤1024 字节）
curl -s -m 3 http://127.0.0.1:7000/api/heartbeat_status

# 3) 汇总诊断（会话、13 条流计数、7706 最后一条包）
curl -s http://127.0.0.1:7000/api/status | python3 -m json.tool | head -40

# 4) UDP 7706：发一条心跳，确认服务端收到并落盘
/data/sunnypilot/.venv/bin/python - <<'PY'
import json, socket, time
pkt = {"carrotIndex": 1, "epochTime": int(time.time()), "timezone": "Asia/Shanghai"}
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.sendto(json.dumps(pkt).encode(), ("127.0.0.1", 7706))
print("sent", pkt)
PY
cat /tmp/cplink_shim_7706_last.json

# 5) 7714 navi v2：连 control 通道发 requirements_query，打印 manifest 回执
/data/sunnypilot/.venv/bin/python - <<'PY'
import asyncio, json, time
import aiohttp
async def main():
    async with aiohttp.ClientSession() as sess:
        async with sess.ws_connect("ws://127.0.0.1:7714/api/navi/ws/v2/control/amap_auto_1.0") as ws:
            await ws.send_str(json.dumps({
                "type": "requirements_query", "protocol_version": 2,
                "timestamp_ms": int(time.time() * 1000), "app_version": "amap_auto_1.0",
                "catalog_revision": 1,
                "limits": {"max_binary_frame_bytes": 0, "max_total_bitrate_kbps": 12000},
                "streams": [],
            }))
            m = json.loads((await ws.receive()).data)
            print("session_id:", m["session_id"])
            print("enabled json streams:", sum(1 for s in m["streams"] if s["enabled"]), "/", len(m["streams"]))
asyncio.run(main())
PY

# 6) 7705：Windows 侧监听 6 秒确认收到广播
#    powershell -c "$u=[System.Net.Sockets.UdpClient]::new(7705);$u.Client.ReceiveTimeout=6000;..."
```

或者从 Windows 直接验：`curl.exe -s -m 3 http://192.168.123.70:7000/api/heartbeat_status`。

## 车架模式（bench，2026-11-12）

没有实车、也不想让假数据污染生产 shim 时用它：一个独立进程同时扮演**假车**和**假手机**。

```bash
systemctl --user start cplink-bench      # 已存在的 user unit；不开机自启
systemctl --user status cplink-bench --no-pager | head -20
journalctl --user -u cplink-bench -f
systemctl --user stop cplink-bench
```

- 控制页：`http://192.168.123.70:17000/bench`（本机 `http://127.0.0.1:17000/bench`）
- 端口：HTTP/WS **17000**、navi WS **17714**、UDP **17706**、TCP 7711 **17711**；
  7705 广播在 bench 下**禁用**，真手机不会误发现。
- 隔离：进程内设置 `OPENPILOT_PREFIX=cplink-bench`，假车/假手机只读写
  `/dev/shm/msgq_cplink-bench`；生产 shim（7000/7706/7711/7714）和行车栈完全不受影响。
- 假车发布：`carState`/`carStateSP`/`selfdriveState`/`controlsState`/`carControl`/
  `modelV2`/`longitudinalPlan`/`radarState`/`deviceState`/`gpsLocationExternal`/`can`
  （0x133、0x2B4）；假手机走 7706 心跳、7714 八条导航流、7000 compact、7711 灯态。
- 页面可切场景：`parked`/`driving`/`turn_left`/`lane_change`/`red_light`/`highway`/`no_gps`，
  并可实时改车速、巡航、限速、前车、导航距离/转向码/车道数/灯色/GPS。
- bench 下自动 `CPLINK_TLA_CONFIRMED=1` 且 1/2/3→红/绿/黄，因此 7711 能看到
  假车 0x2B4 的灯色；生产环境仍然要求先 survey 标定。
- 5001 视频在 bench 中标记为 `skipped`：没有 `camerad/encoderd`，视频仍需真栈。
- 直接前台跑：`/data/sunnypilot/.venv/bin/python shim.py --bench --log-level INFO`。

## 真实手机 + 假车（attach，2026-11-12）

手机上已经装好 CP搭子时用这个：**只模拟车，不模拟手机**；假车直接接在正在跑的
生产 shim 上，真手机照常通过 7705/7000/7714/7711 连接。

```bash
systemctl --user start cplink-bench-car   # 只发假 carState/can，控制页 17001
systemctl --user stop cplink-bench-car
journalctl --user -u cplink-bench-car -f
```

- 控制页：`http://192.168.123.70:17001/bench`（本机 `http://127.0.0.1:17001/bench`）
- 假车发布到**默认 msgq**（生产 shim 的 CompactSource/LampSource 直接消费）；
  不占 7000/7706/7711/7714，不需要停生产 shim。
- 手机正常连生产 shim；`/api/status.compact.clients` 变成 1 后，手机实时页
  就能看到假车速/挡位/车道/模型数据；手机发 7714 导航时，`navInstruction` 由
  shim 发布，`/api/navi_state.json` 应出现 guidance/speed/lane。
- attach 启动时写 `/tmp/cplink_tla_map.json`（1/2/3→红/绿/黄），让生产 shim
  不用重启就能解假车 0x2B4；停止 attach 时删除，生产 shim 回到“未标定=不显示灯”。
- 真车/行车栈在跑时**不要**启动 attach（假 `carState`/`can` 会污染栈）；脚本会检测
  `manager.py`/`pandad` 并拒绝，除非显式 `--force`。
- 不带 `--bench` 的 `shim.py` 就是生产模式；`cplink-bench`（假手机+假车，17000）
  和 `cplink-bench-car`（假车+真手机，17001）不要同时开。

### 台架视频（2026-11-12）

台架不需要完整行车栈也能出 5001 视频；用现在这台机器的 USB 摄像头：

```bash
systemctl --user start cplink-bench-video
systemctl --user status cplink-bench-video --no-pager | head -20
journalctl --user -u cplink-bench-video -f
systemctl --user stop cplink-bench-video
```

流水线：

```text
/dev/video0
  -> webcamerad（openpilot.system.camerad.webcam.camerad）
  -> encoderd --stream（H.264）
  -> livestreamNarrowRoadEncodeData
  -> webrtcd 0.0.0.0:5001（CPlink /stream，road 轨）
```

- 默认摄像头 `/dev/video0`；换索引：`CPLINK_BENCH_CAM=1` 或 `bench_video.py --camera 1`。
- 手机 App 的 5001 `/stream` 直接可用；占用时仍返回 409 `carrot_vision_busy`。
- attach 控制页会显示“视频 5001 up (/stream road)”；没起来时先看
  `journalctl --user -u cplink-bench-video`。
- 该服务用默认 msgq，和 `cplink-bench-car`/生产 shim 配合；不要在真实行车栈占着
  摄像头时启动（会争 `/dev/videoN`）。

## 导航灯态接入（2026-11-13，方案 A）

`nav_lamp.py` 把 7714 的 `traffic_signal`（手机导航的红绿灯：`visible` /
`distance_m` / `lights.*`）桥接到 `yoloObjects.egoLightState/Frames/Distance`，
planner 现有的灯停候选直接消费，**`YoloEnabled` 可以保持 False**。

- 默认开启；`CPLINK_NAV_LAMP=0` 关闭，重启 `cplink-shim` 生效。
- 单发布者：`YoloEnabled=True` 时桥自动停发，避免和 `yolod` 抢 `yoloObjects`；
  恢复 False 后自动继续（每 10 s 检查一次 Params）。
- 2 次独立导航更新确认同一灯态后才发布；发布频率 10 Hz，
  `egoLightFrames/egoLightConfirmations` 都记录真实更新次数；停止线相机确认另记
  `stopLineConfirmations`，不再把两条手机更新伪装成 3/6 个相机帧。
- 同会话重复/逆序 `sequence` 不刷新接收时间，也不增加确认；换会话清空旧证据。
  灯态 TTL 仍为 3 s；记录会话、序号、原始源时间与本机接收时间，源时间不冒充本机时钟。
- `visible=false`、没有有效灯、流过期 → **不发布**；不会伪造绿灯。
  （部分 App 版本省略 `visible`，此时以 `lights` 里是否有有效灯为准。）
- **实测的 App 载荷没有 `distance_m`**（2026-11-13，652 条 `present=true` 全部只有
  `visible/source/lights/ui_counter`）。桥仍会发布红灯状态，但 `distance=-1`：
  planner 可以执行有界预减速，但不能据此承诺完整停线。
  完整停线与自动放行还需要真实路口/方向关联，不能从倒计时或缺失消息推断。
- 可选协议字段为 `intersection_id`、`direction`（`straight/left/right/uturn`）和
  `relevant=true`；三者缺失或未知时 `egoLightRelevant=false`。
  仓内 `tools/cplink_harmony` 只有 `present=false` 探针发送器和车辆灯态显示，
  不能证明实际手机导航生产端已经支持真实绿灯、路口 ID 或方向关联；这些仍需补上游。
- 有 `distance_m` 时默认按“到灯”处理（planner 再减 `TrafficLightStopOffset`，默认
  5 m）。若实测它是到停止线，设 `CPLINK_NAV_LAMP_DISTANCE_BIAS_M=5` 抵消。
- 调试：`curl http://127.0.0.1:7000/api/nav_lamp.json`；`/api/status` 里也有
  `nav_lamp`。日志：`journalctl --user -u cplink-shim -f | grep 'nav lamp'`，
  只在状态变化或距离变化 ≥5 m 时打印。
- 前提：手机导航处于引导中，且 App 的 `traffic_signal` 为 `present=true`。
  空闲时 App 发 `present=false reason=signal_source_missing_unverified_or_expired`，
  桥不会工作（这是 App 侧没有可用信号源，不是桥丢了数据）。
- 测试：`/data/sunnypilot/.venv/bin/python -m pytest tools/cplink_shim/tests/test_nav_lamp.py -q`。

## 低速停止线距离（2026-11-14，非 YOLO）

`stopline_source.py` 只做门控和文件读写；**相机/OpenCV 在独立子进程
`stopline_worker.py`**（避免 VisionIPC 卡住 shim 的 HTTP/WS）。父进程把门控写到
`/dev/shm/cplink_stopline_gate.json`，子进程把确认结果写到
`/dev/shm/cplink_stopline_fix.json`。只在**导航红/黄 + 车速 <32 km/h + 无前车**
时使用；planner 用 0.5 m 小间隙停在停止线前，而不是 `TrafficLightStopOffset` 的 5 m。

- 默认关闭；`CPLINK_STOPLINE_ASSIST=1` 开启，重启 `cplink-shim` 生效。
- `CPLINK_STOPLINE_MAX_SPEED_KMH` 默认 32；`CPLINK_STOPLINE_MAX_LEAD_M` 默认 30。
- 10 Hz 工作；3 帧确认；检测距离约 10–23 m，高速不适用（见
  [stopline/README.md](../../openpilot/sunnypilot/stopline/README.md)）。
- 调试：`curl /api/nav_lamp.json` 的 `bridge.stopline` 字段；日志
  `journalctl --user -u cplink-shim -f | grep -E 'stop line|停止线'`。
- 摄像头未起时 worker 自动等待，不影响导航灯态/7705/7706/7714。
- 没有距离时 planner 另有**红灯预减速**：>8 m/s 降到 8 m/s 后保持；
  它不是停车，只把车送进停止线可工作的低速窗口。
- `carState/modelV2/radarState` 必须同时满足 seen、valid 与接收年龄 ≤0.3 s；
  已过期车速不能由手机速度替代。前车判断合并 model 与 radard 短时保持结果。
- 父门控与相机观测有效期均为 0.4 s；重复读门控文件不延长时间。父进程冻结、
  相机时间异常或会话切换时清空 worker 确认。相机时间取 VisionIPC SOF，另记检测完成时间。
- 目标有稳定 ID；不相符的距离跳变、换会话/路口/方向重新确认。任何门控失败不再提供距离；
  已承诺停车后的退化/保持由 planner 状态机处理，不会默认绿灯或猜距离。

## 设计边界（2026-11-11：M3 看板已接真数据）

- **7000 compact_state 已接 cereal**：`carState`/`controlsState`/`carControl`/`modelV2`/
  `longitudinalPlan`/`radarState`/`lateralPlan`/`selfdriveState`/`deviceState`/
  `gpsLocationExternal`/`carrotMan`/`carrotNavi` 共 12 个服务，按 app 的
  **CVS1/CVB1 二进制**发送（不是 JSON）；每个服务独立 `u16 sequence`，按
  `compact_source.py` 的快照 + `shim.py` 的 `COMPACT_CADENCE` 发送。
- **没有 cereal 时不伪造**：栈没起时只发 hello，不发二进制帧，app 会按 TTL 把卡片置灰。
- **7714 导航已落地（2026-11-11）**：13 条 json 流仍是 `app → 车端`，但
  `guidance_current`/`guidance_next`/`lane_current`/`speed`/`vehicle`/`route` 会进入
  `navi_state.py`（每流 TTL），再由 `navi_publisher.py` 以 5 Hz 发 `navInstruction`、
  2 Hz 发 `liveMapDataSP`。失联后发 `valid=false`，不拿旧 TBT/限速续命。
- **单发布者门控**：shim 每 tick 维护 `/tmp/cplink_navi_source`（`nav`/`nav+speed`）；
  `navd.publish_instruction()` 在文件新鲜时直接 return，`mapd` 的
  `BaseMapData.publish()` 在 `nav+speed` 时 return，避免和 shim 抢同一条服务。
- **desire 默认关**：`/tmp/cplink_navi_desire` 存在且新鲜时，`modeld` 才把
  `navInstruction` 的 turn/left|right（<30 m、vEgo<20 m/s）传给 `DesireHelper`；
  不建这个文件就完全不改变控车行为。
- 会话/清单完全按协议 v2：`catalog_revision=1`，`streams` 固定 28 项（13 json + 14 image + 1 render），`stream_handle` 1..28 唯一，`enabled` 只对 13 条 json 为 true，`delivery_mode=on_change_with_heartbeat`、`interval_ms=1000`、`stale_timeout_ms=10000`；image/render 一律 `enabled=false`。
- `/ws/compact_state` 的文本 hello 是
  `{"mode":"carrot-state-v1","services":[12 个服务]}`；之后只发二进制。
  `lateralPlan` 在本 fork 没有独立服务，由 `modelV2.meta.laneChangeState/Direction` 派生。
- `/api/compact_state.json` 返回同一份快照（调试/对拍用）；`/api/status` 里有
  `compact.frames/batches/sequence`。
- 测试：`/data/sunnypilot/.venv/bin/python -m pytest tools/cplink_shim/tests -q`
  （当前 37 项通过，含 app 的 golden CVS1 fixtures 往返）。
- **参数接口是 shim 本地假值**（默认 `AlwaysLateral=0`、`ExperimentalMode=0`、`ShareData=1`、`SpeedFromPCM=0`、`EnableSpeedLimit=0`，存 `/tmp/cplink_shim_params.json`），**不读写 sunnypilot 的 Params**，`/api/egpu/model` 的 `jetlink` 返回 `waiting`（本机没有 Jetlink 后端）。
- 依赖探测：有 `aiohttp`（本机 3.14.5）就用；没有则自动回落到内置的 asyncio HTTP/1.1 + RFC6455 实现，两条传输共用同一套协议层（`CPLINK_FORCE_STDLIB=1` 可强制走兜底路径）。
- `systemd` 单元**没有** `PrivateTmp`（否则 `/tmp/cplink_shim_7706_last.json` 落在私有 tmp 里看不到）也**没有** `KillMode`，因此 7706 的落盘文件就是宿主 `/tmp` 里那一份。
- 只碰本目录：不写 `/data/sunnypilot` 其它文件，不动 manager/sunnypilot 栈、sp_console、capture、caddy，不占用 8899/8770/3080/3081，也不做 git commit。

## 可调环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `CPLINK_IP` | 自动探测 | 广播里携带的 `ip` |
| `CPLINK_BROADCAST_ADDRS` | `255.255.255.255,<网段>.255` | 逗号分隔的广播目标 |
| `CPLINK_DISCOVERY_INTERVAL` | `1.0` | 广播周期（秒） |
| `CPLINK_HTTP_PORT` / `CPLINK_NAVI_PORT` | `7000` / `7714` | HTTP / navi WS 端口 |
| `CPLINK_CMD_PORT` / `CPLINK_DISCOVERY_PORT` | `7706` / `7705` | UDP 端口 |
| `CPLINK_LAST_7706_FILE` | `/tmp/cplink_shim_7706_last.json` | 7706 最后一条包落盘路径 |
| `CPLINK_LOG_LEVEL` | `INFO` | `DEBUG` 可看到每条 7705 广播和 7706 心跳 |
| `CPLINK_FORCE_STDLIB` | 未设 | `1` = 强制不用 aiohttp |
| `CPLINK_NAV_LAMP` | `1` | `0` = 关闭导航灯态桥 |
| `CPLINK_NAV_LAMP_DISTANCE_BIAS_M` | `0` | 导航距离偏置（米），见上节 |
| `CPLINK_STOPLINE_ASSIST` | `0` | `1` = 开启低速停止线距离源 |
| `CPLINK_STOPLINE_MAX_SPEED_KMH` | `32` | 停止线源最高车速 |
| `CPLINK_STOPLINE_MAX_LEAD_M` | `30` | 前车小于该距离时禁用 |
