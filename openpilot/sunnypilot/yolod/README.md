# yolod —— 可选的 YOLO 目标检测（**仅信息用途**）

`yolod` 是一个默认跑在 CPU 上的可选感知进程：从 `camerad` 的 VisionIPC 取最新一帧路面画面，
用 YOLO11n（ONNX Runtime，CPU）做目标检测，以 **10 Hz** 把结果作为 `yoloObjects` 消息发到 msgq，
供网页运维台 / 调试工具显示。另有**实验性**的 ncnn + Vulkan GPU 后端可选（`YOLO_BACKEND=ncnn`，
默认关闭，见第 5 节）。

> **它不是安全功能，也不参与任何控制。** 没有任何控制进程（`controlsd`、`plannerd`、
> `card`…）订阅 `yoloObjects`；该服务的 `should_log=False`，不写 rlog；进程只读取相机、
> 不写硬件、不发 CAN。默认关闭。

## 1. 打开与关闭

前提：模型文件在 `openpilot/sunnypilot/yolod/models/yolo11n.onnx`（仓库自带，见第 9 节许可）。

```bash
# 打开（Params 里的 YoloEnabled，PERSISTENT|BACKUP）
/data/sunnypilot/.venv/bin/python - <<'EOF'
from openpilot.common.params import Params
Params().put_bool("YoloEnabled", True)
EOF

# 关闭
# Params().put_bool("YoloEnabled", False)
```

`manager` 只在 **`started`（行车状态）且 `YoloEnabled` 为真**时启动 `yolod`：
停车时打开参数不会立刻起进程，进入行车状态后自然拉起；关闭参数后 manager 在下一轮停止它。
进程配置见 `openpilot/system/manager/process_config.py`（`PythonProcess("yolod", ...)`），
无需也不应手工起进程。

运行日志在 `~/.comma/log/swaglog.*` 里按 `YoloEnabled` / `yolod` 过滤；
终端只显示 WARNING 及以上，模型加载成功的信息只在日志文件里。

## 2. 数据流与行为

```
camerad ──VisionIPC(NARROW_ROAD, 1344x760@20Hz)──▶ yolod
                                                   │  recv(0) 非阻塞，只取最新帧
                                                   │  nv12_to_bgr（含 stride / uv_offset）
                                                   │  detector.detect()（letterbox→推理→解码/NMS，后端可选）
                                                   ▼
                                   PubMaster ── yoloObjects（log.Event）──▶ msgq
```

- **后端**：默认 ONNX Runtime CPU；`YOLO_BACKEND=cv2dnn` 强制 OpenCV，`ncnn` 使用实验性
  Vulkan GPU 后端。切换只改环境变量，发布的消息格式与 10 Hz 行为不变。
- **限速 10 Hz**（`Ratekeeper`）；相机 20 Hz，只处理最新帧，不排队、不补帧。
- **模型缺失 / 后端不可用**：日志只报一次具体错误，然后按 5s → 10s → … → 60s 指数退避重试，
  **不 crash-loop**（不会让 manager 每秒重启一次把日志刷掉）。
- **camerad 重启 / 断流**：连续 5 秒收不到帧会重新等待并连接 VisionIPC，进程不退出。
- **SIGINT / SIGTERM**：干净退出（manager 停止进程时用）。
- **`--video` 调试模式**：离线回放录像，不要求 `YoloEnabled`，默认不发 msgq：

  ```bash
  cd /data/sunnypilot
  .venv/bin/python -m openpilot.sunnypilot.yolod.yolod \
      --video ~/.comma/media/0/realdata/00000097--4f3f72a3b0--12 --max-frames 50
  # 加 --publish 可以把 yoloObjects 也发到 msgq（用于离线验证接收端）
  ```

## 3. 环境变量

进程的环境变量来自启动它的环境（用户启动脚本 `~/.local/bin/sunnypilot-launch` / manager）。
全部可选；非法值会**回退默认值并在日志里 warning**，不会让进程退出。

| 变量 | 默认 | 说明 |
|---|---|---|
| `YOLO_MODEL_PATH` | `openpilot/sunnypilot/yolod/models/yolo11n.onnx` | ONNX 模型路径（相对路径按启动时工作目录解析） |
| `YOLO_BACKEND` | `onnxruntime` | `onnxruntime`（默认 CPU）/ `cv2dnn`（强制 OpenCV）/ `ncnn`（实验性 Vulkan GPU） |
| `YOLO_NCNN_PARAM` | `.../models/yolo11n.ncnn.param` | ncnn 模型结构文件（仅 `YOLO_BACKEND=ncnn`） |
| `YOLO_NCNN_BIN` | `.../models/yolo11n.ncnn.bin` | ncnn 权重文件（仅 `YOLO_BACKEND=ncnn`） |
| `YOLO_NCNN_NAMES` | `.../models/yolo11n.ncnn.names.json` | ncnn 类名 JSON（导出工具生成，缺失时用 `class_N`） |
| `YOLO_CONF` | `0.25` | 置信度阈值（0.01–0.99） |
| `YOLO_IOU` | `0.45` | NMS IoU 阈值（0.01–0.99） |
| `YOLO_RATE` | `10` | 发布频率 Hz（0.5–60） |
| `YOLO_THREADS` | `2` | ONNX Runtime intra-op 线程数（1–32）；ncnn 时只影响 CPU 部分，GPU 实测 1–2 最快 |
| `YOLO_STREAM` | `narrow` | `narrow` 长焦 / `wide` 广角；指定的流不存在时自动回退另一路 |
| `YOLO_CLASSES` | 道路相关 7 类 | 见下 |

`YOLO_CLASSES` 的语义：

- **不设置 / 空字符串** → 默认只发布道路相关类：
  `person,bicycle,car,motorcycle,bus,truck,train`（避免 COCO 的沙发、背包、吹风机等噪声）。
- **`all`** → 发布模型全部类别（本模型 80 类）。
- **逗号分隔列表**（如 `person,car,motorcycle`）→ 只发布这些类。
  名字必须在模型里存在，否则会被忽略并在日志里报错；如果全部都不存在，则退化为「全部类别」并告警。

> **`classNames` 始终是模型的全量类名列表**（本模型 80 项，下标与 `classId` 对齐），
> 即使配置了过滤、`objects` 里只有一部分类别，也是这样。消费者应通过
> `classNames[classId]` 取名字，不要假设 `classNames` 只包含被发布的类。

## 4. `yoloObjects` 消息

定义在 `openpilot/cereal/custom.capnp`（`YoloObject` / `YoloObjects`），并作为
`log.Event` union 的字段 `yoloObjects @153` 发布 —— **msgq 上的 payload 是序列化后的
`log.Event`，不是裸的 `custom.YoloObjects`**。

| 字段 | 类型 | 含义 |
|---|---|---|
| `frameId` | UInt32 | VisionIPC 帧号 |
| `frameWidth` / `frameHeight` | UInt16 | 源帧尺寸（本机当前 1344x760；录像是 1928x1208） |
| `inferenceMs` | Float32 | `detect()` 总耗时：letterbox + 预处理 + 推理 + 解码 + NMS，**不含** NV12 转换和 msgq 发送 |
| `model` | Text | 模型文件名 |
| `classNames` | List(Text) | 模型全量类名，下标 = `classId` |
| `objects` | List(YoloObject) | 过滤后的检测结果，最多 100 条，按置信度降序 |

`YoloObject`：`classId`、`confidence`、`x`、`y`、`w`、`h` —— 坐标归一化到 `[0,1]`，
`(x, y)` 是左上角，`w/h` 是宽高。

接收示例（标准 cereal 写法）：

```python
# 方式 A：SubMaster（服务已在 services.py 注册；未进 Event union 时才不能用）
from openpilot.cereal.messaging import SubMaster
sm = SubMaster(["yoloObjects"])
sm.update(0)
if sm.updated["yoloObjects"]:
  msg = sm["yoloObjects"]
  for obj in msg.objects:
    print(msg.classNames[obj.classId], obj.confidence, obj.x, obj.y, obj.w, obj.h)

# 方式 B：裸 socket（payload 是 log.Event；SubSocket 没有 with 上下文，靠 GC 回收）
from openpilot.cereal import log
from openpilot.cereal.messaging import sub_sock
sock = sub_sock("yoloObjects", conflate=True, timeout=100)
raw = sock.receive()
if raw is not None:
  with log.Event.from_bytes(raw) as event:
    print(event.yoloObjects.frameId)
```

## 5. 检测器实现（`detector.py` / `detector_ncnn.py` / `nv12.py`）

- **后端**：ONNX Runtime CPU 为主（`CPUExecutionProvider`，`intra_op=YOLO_THREADS`、
  `inter_op=1`、全图优化）；`onnxruntime` 不可用或 session 创建失败时回退 `cv2.dnn`
  （`YOLO_BACKEND=cv2dnn` 可强制走这条回退路径）。可选 ncnn + Vulkan GPU 后端见本节末尾。
- **letterbox**：保持宽高比缩放到 640x640，空白填 114（Ultralytics 惯例）；
  解码时用**实际缩放系数**（不是理想比例）做逆映射，角落误差 < 1e-3 像素。
- **解码**：支持 YOLOv8/11 的 `[1, 4+nc, N]` 及转置布局、YOLOv5 风格的 objectness 行、
  以及端到端导出的 `[N, 6]`（x1,y1,x2,y2,score,class）；类别分数取 argmax，逐类 NMS。
- **metadata**：从 ONNX 的 `names` / `imgsz` 读类名与输入尺寸（Python dict 文本或 JSON 均可）；
  输入尺寸优先静态 shape，其次 metadata，最后回退 640x640。`cv2.dnn` 没有 metadata API，
  此路径下标签退化为 `class_<id>`。
- **NV12**：按 `stride` 逐行取有效像素，UV 平面从 `uv_offset` 开始（camerad 会把 Y 平面高度
  对齐，`uv_offset` 因此大于 `stride*height`），U/V 交织的偶/奇列拆开后重排成 I420 再交给
  `cv2.cvtColor`。单元测试里与 camerad 实际使用的 PyAV NV12 打包方式做了逐像素交叉验证。

### 可选 GPU 后端（ncnn + Vulkan，实验性）

**默认仍然只用 CPU**，GPU 是显式 opt-in：

```bash
# 在启动 yolod 的环境里（例如启动脚本）设置：
export YOLO_BACKEND=ncnn
# 可选：YOLO_THREADS=1（GPU 路径实测 1 线程最快；只影响 ncnn 的 CPU 部分）
```

- 实现：`detector_ncnn.py` 的 `NcnnDetector`，与 `Detector` 同接口
  （`detect(bgr)`、`class_names`、`input_w/h`、`inference_ms`、`backend="ncnn-vulkan"`）。
  letterbox、解码与 NMS 直接复用 `detector.py`，所以两条后端的输出语义完全一致。
- 模型：仓库自带 pnnx 转换好的
  `models/yolo11n.ncnn.param` / `.bin` / `yolo11n.ncnn.names.json`（由同一份 ONNX 导出）。
  换模型时用 `tools/export_ncnn.py` 重新转换（需要 `uv pip install ncnn pnnx`）：

  ```bash
  cd /data/sunnypilot
  .venv/bin/python openpilot/sunnypilot/yolod/tools/export_ncnn.py \
      openpilot/sunnypilot/yolod/models/yolo11n.onnx \
      --out-dir openpilot/sunnypilot/yolod/models
  ```

  脚本会调用 `pnnx <onnx> inputshape=in0=[1,3,640,640]`，把生成的
  `<stem>.ncnn.param/.bin` 复制成 `--prefix` 指定名字，并把 ONNX metadata 的全量类名写成
  `<prefix>.ncnn.names.json`。`--skip-convert` 只重写 names，`--dry-run` 只打印命令。
- 预处理必须用 `ncnn.Mat.from_pixels(letterboxed_bgr, PIXEL_BGR2RGB, 640, 640)` +
  `substract_mean_normalize([], [1/255.]*3)`；直接构造 float HWC Mat 会得到错误结果
  （开发时出现过 0.5 的假 person 框）。
- **Vulkan 失败时明确报错，不会静默回 CPU**：无 Vulkan 设备、只有软件设备（llvmpipe）、
  设备索引越界、模型加载或推理失败都会抛 `RuntimeError`，由 `yolod` 的模型重试循环
  记录错误并退避。这样才能避免"以为在跑 GPU，其实在用 CPU"。
- 一个进程只建一个 Vulkan net；ncnn 的 CPU net 与 Vulkan net 同进程混用曾在开发时
  segfault，与 ONNX Runtime 同进程共存也未验证，所以测试把 GPU 部分放在子进程里跑。
- 快速验证：

  ```bash
  # 离线评测（自动使用 GPU）
  .venv/bin/python openpilot/sunnypilot/yolod/tools/eval_video.py <route> --backend ncnn --max-frames 120

  # 回放调试（打印 backend/device/每帧耗时）
  YOLO_BACKEND=ncnn .venv/bin/python -m openpilot.sunnypilot.yolod.yolod --video <route> --max-frames 20
  ```

**风险 / 未验证**（详见第 10 节）：这块 780M 是**集显**，`modeld` 也在同一块 GPU 上跑
（`SUNNYPILOT_MODEL_DEVICE=AMD`），两者争用 GPU 的实际影响**没有在实车上验证过**；
当前所有数字都是桌面负载下的离线测量。建议先在停车/离线状态观察，不要在不了解影响的情况下
长时间行车开启。

**回退**：`unset YOLO_BACKEND`（或显式设 `YOLO_BACKEND=onnxruntime`）后重启 stack 即回到
CPU 后端；不需要改代码、删模型或重装依赖。

## 6. 实测性能（本机，16 核，ONNX Runtime 1.31，yolo11n 640x640）

环境：2 线程、`conf=0.25`、桌面环境（ToDesk + Chrome + Xorg）有后台负载；每项 10–60 次采样。

| 阶段 | p50 | p90 | 说明 |
|---|---|---|---|
| `VisionIpcClient.recv(0)` | 0.005 ms | 0.015 ms | 共享内存取帧，几乎无成本 |
| NV12 → BGR，1344x760（当前发布尺寸） | 0.49 ms | 0.85 ms | 含 stride/uv_offset 处理 |
| NV12 → BGR，1928x1208（录像尺寸） | 0.84 ms | 2.34 ms | 同上，像素多 2.3 倍 |
| `Detector.detect()`，1928x1208 | 39.6–41.0 ms | 42–45 ms | 预处理 + 推理 + 解码 + NMS |
| 其中纯 `session.run` | ~36 ms | ~37 ms | 只算 ONNX 推理 |
| 端到端（recv + NV12 + detect + 构造消息） | 41.7 ms | 45.5 ms | 1344x760 帧，`--publish` 之前 |

空闲基线（Lead 独立实测，2 线程）：wall ≈ **37 ms/帧**、CPU ≈ **73 ms/帧**（约 0.73 个核）；
1 线程 wall 62 ms、4 线程 wall 24 ms。**10 Hz 目标下 2 线程约有 2.5 倍余量**，
但机器同时跑 `modeld` 等进程时仍建议先看实测再调 `YOLO_THREADS`。

注意：`inferenceMs` 只统计 `detect()`，NV12 转换（约 0.5 ms）与 msgq 发送不计入。

### GPU（ncnn + Vulkan）实测

同一台机器、桌面有 ToDesk / Chrome 负载，YOLO11n 640x640、`conf=0.25`：

| 指标 | CPU（ORT，2 线程） | GPU（ncnn，1 线程） |
|---|---|---|
| 120 帧夜间 route 的 `detect()` p50 | 40.5–41.3 ms | **20.7 ms** |
| p90 | 43.2–48.3 ms | 22.1 ms |
| max | 50.8–55.0 ms | 26.0 ms |
| 整段评测吞吐（含 1928x1208 解码 + 标注 + 编码） | 16.2–16.9 fps | **25.7 fps** |

（CPU 一列是两次 120 帧评测的区间，桌面负载不同会有约 10% 波动；GPU 一列是一次测量。）


GPU 全流程分项（route 9，1928x1208，24 帧）：letterbox 0.4 ms、`from_pixels` + 归一化 0.77 ms、
Vulkan 推理 17.46 ms、解码 + NMS 1.17 ms，`detect()` 总 p50 **20.99 ms**。
Lead 独立实测全流程 **18.3 ms/帧（55 fps，1 线程）**，与以上数字同量级；
ORT CPU 4 线程约 37–40 ms（本机空闲基线）。

**等价性**（同一帧、同类别、IoU ≥ 0.5 才计匹配）：

| 数据 | ORT | ncnn | 匹配率 | 平均 IoU |
|---|---|---|---|---|
| 夜间 route 9，24 帧（1928x1208） | 336 | 335 | 99.1% | 0.982 |
| 夜间 route 25，24 帧（1344x760，当前发布尺寸） | 68 | 67 | 98.5% | 0.984 |
| 夜间 route 12，120 帧（整段逐类计数） | 605 | 600 | — | — |
| Lead 实测夜间 24 帧 | 160 | 159 | 158/160 = 98.8% | 0.981 |
| Lead 实测白天 24 帧 | 238 | 236 | 235/238 = 98.7% | 0.985 |

route 12 的 120 帧整段计数差异只有 5 个（person 318→317、car 251→248、traffic light 9→8，
motorcycle / truck 相同），符合"同一模型、不同推理后端"的预期，不是功能差异。

## 7. 离线评测工具 `tools/eval_video.py`

对录像（`fcamera.hevc` / 任意视频）逐帧检测，输出**标注 mp4** 和 **JSON 报告**：

```bash
cd /data/sunnypilot
.venv/bin/python openpilot/sunnypilot/yolod/tools/eval_video.py \
    ~/.comma/media/0/realdata/00000097--4f3f72a3b0--12 \
    --max-frames 120 \
    --out /tmp/night_yolo.mp4 --json /tmp/night_yolo.json

# 只评测道路相关类，并和默认类别过滤对齐：
... --classes person,bicycle,car,motorcycle,bus,truck,train
```

参数：`--conf/--iou/--threads/--every/--max-frames/--classes/--model/--out/--json`，
以及后端选择 `--backend {onnxruntime,cv2dnn,ncnn}` 与 `--ncnn-param/--ncnn-bin/--ncnn-names`。
默认输出在当前目录 `<视频名>.yolo.mp4/.json`；JSON 的 `backend`/`device` 字段记录实际使用的后端。
解码优先 PyAV，失败自动退回 `ffmpeg` 原始帧管道（`ffmpeg -f rawvideo -pix_fmt bgr24`）。

JSON 主要字段：

```text
input/video/model/backend/device/conf/iou/every
class_names          模型全量类名（80）
class_filter         本次使用的类别过滤（null = 全部）
frames_processed/objects_total/objects_per_frame/max_objects_in_frame
counts               逐类检出次数（降序）
frames_with          逐类「出现该类的帧数」
inference_ms         mean/p50/p90/max
elapsed_s/fps
annotated_video      mp4 路径
```

### 真实夜间 route 实测（`00000097--4f3f72a3b0--12`，1928x1208@20fps）

120 帧（全类，`conf=0.25`）：

| 类别 | 检出次数 | 出现帧数 |
|---|---|---|
| person | 318 | 103 |
| car | 251 | 106 |
| motorcycle | 21 | 20 |
| traffic light | 9 | 9 |
| truck | 6 | 6 |
| 合计 | 605（5.04 个/帧） | 114/120 帧有检出，单帧最多 12 个 |

耗时：`inference_ms` mean 40.9 / p50 40.5 / p90 43.2 / max 50.8；含解码、标注和 1928x1208
mp4 编码的整段吞吐 16.9 fps。默认道路 7 类过滤下为 596 个目标（4.97 个/帧，去掉 9 个红绿灯），
person 318、car 251、motorcycle 21、truck 6。

夜间画面里大量电动车骑手被 COCO 标成 `person`（有时是 `motorcycle`），这正是第 8 节要解决的。

## 8. 换模型与自训练（电动车 / 三轮车）

### 换模型

1. 把 ONNX 放到任意路径，设 `YOLO_MODEL_PATH`；或直接替换
   `openpilot/sunnypilot/yolod/models/yolo11n.onnx`。
2. 建议保留 ONNX metadata 里的 `names` 和 `imgsz`（Ultralytics `yolo export` 会写入）。
3. 输出布局需是 YOLO 家族：`[1, 4+nc, N]`（或转置）、可带 objectness、或已 NMS 的 `[N, 6]`。
4. **默认类别过滤按名字匹配**：新模型如果没有 `train` 之类的名字，该名字会被忽略并在日志报
   `class filter names not in model`；若一个都不匹配会退化为全部类别。换模型时建议显式设置
   `YOLO_CLASSES`（例如 `person,car,e_bike,tricycle`）。
5. 换了输入尺寸也能跑：优先用模型静态 shape，动态维度回退 metadata 的 `imgsz`，再回退 640。

### 本车数据实测（2026-10-27，YOLO11n COCO）

40 帧人工核对（白天 20 / 夜间 20，按 1 fps 均匀抽样，不挑有检出的帧）：

| 指标 | 白天 | 夜间 |
|---|---|---|
| 运动态两轮骑行者实例（人工计） | 11 | 17 |
| 标成 `motorcycle` | 8（73%） | 6（35%） |
| 只标成 `person`（车体未检出） | 0 | 7（41%） |
| 完全漏检 | 3（27%） | 4（24%） |
| 停放两轮车检出 | 8/19 | 0/15 |

- 电动三轮车：COCO 标成 `motorcycle`（0.27–0.39）或漏检；BDD100K 微调模型标成 `motor`/`car`；
  VisDrone 航拍模型能给出 `tricycle`（0.27），但地面视角误检多，不能直接用。
- BDD100K 的 `rider` 类能把「骑手被标成 person」从 31 个框降到 4–6 个 —— 这是类别定义的收益，
  不是模型容量的收益。
- `imgsz=(384, 640)` 导出：2 线程 **22.8 ms** vs 640x640 的 38.8 ms（−41%），两轮车检出数不变，
  `person` 74→65（远小行人略少）。模型示例见 `docs/model_evaluation_2026-10-27.md`。
- 完整评估（采样方法、逐类统计、模型对比、标注量估计）：`docs/model_evaluation_2026-10-27.md`。

### 自训练电动车 / 三轮车

COCO 只有 `motorcycle` / `bicycle`，没有「电动车」「三轮车」这两个语义类别，所以现成模型
对国内常见的两轮电动车、三轮车只能尽力猜（常见结果：`person` + 有时 `motorcycle`）。要真正识别，
需要自训练：

1. **采数据**：用本机 route 的 `fcamera.hevc` 抽帧（白天 + 夜间、近/远、侧面/背面、遮挡、
   雨天/逆光都要覆盖）；夜间样本尤其重要，这是当前最弱场景。
2. **标注**：YOLO txt 格式（`class xc yc w h`，归一化）。建议类别先收敛到
   `e_bike`（两轮电动车/电摩）、`tricycle`（三轮车），并保留 `person`、`car`、`truck`、`bus`；
   不要一开始切太细（外卖箱、自行车、电摩分开会导致样本不足）。
3. **训练**：`yolo detect train data=... model=yolo11n.pt imgsz=640 epochs=...`，
   小数据集建议冻结 backbone 先训 head，再全量微调。
4. **导出**：`yolo export model=best.pt format=onnx imgsz=640 opset=12`（metadata 自带 `names`）。
5. **验证**：用第 7 节的 `eval_video.py --model best.onnx --classes e_bike,tricycle,person`，
   对比各类计数与误检；再用 `yolod --video ... --publish` 验证在线路径。
6. **注意**：训练数据来自行车记录，包含人脸 / 车牌，**不要外传**；训练/导出的模型同样受
   Ultralytics AGPL-3.0 约束。

## 9. 许可（重要）

- **模型** `models/yolo11n.onnx` 是 Ultralytics **YOLO11n**（COCO），许可证 **AGPL-3.0**
  （ONNX metadata 的 `license` 字段为 `AGPL-3.0 License (https://ultralytics.com/license)`），
  与仓库其它部分的 MIT 许可证不同。文件 sha256：
  `7d8fd1717d9d5bbab6986cd134afb620649c7a394303d55b1e09fc00804cc5c1`。
- **ncnn 模型** `models/yolo11n.ncnn.param/.bin` 是同一份 AGPL-3.0 YOLO11n 权重经 pnnx 转换的
  产物，**继承同样的 AGPL-3.0 约束**；`.names.json` 只是类名列表。
- ncnn / pnnx（Tencent）为 **BSD-3-Clause** 许可；本目录只以依赖方式使用，不包含其源码。
- 本目录新增的 Python 代码是独立实现（只通过 ONNX Runtime / ncnn 运行时使用模型，
  不包含 Ultralytics 代码）。
- **分发本仓库或通过网络提供本模型能力时，请自行确认 AGPL 义务**（例如提供对应源码）；
  仅本机自用一般无额外负担。自训练的派生模型同理。

## 10. 已知限制

- **只有 COCO 80 类**：没有电动车 / 三轮车类；夜间骑手多被标成 `person`。
- **夜间与远小目标**：暗光、逆光、远处目标置信度低，`conf=0.25` 下会漏检；
  画面里能看到但检不出的情况正常存在。
- **没有距离 / 深度**：只有 2D 归一化框，不能直接得到米制距离；需要距离就得结合相机内参与
  目标在地面上的假设自行估算，且误差大。
- **没有跟踪**：每帧独立检测，没有 track id；帧间抖动明显。
- **10 Hz 非实时**：p50 ≈ 40 ms（2 线程），高负载时 p90 ≈ 45+ ms；`Ratekeeper` 只限速，
  不保证时序，CPU 与 `modeld` 争用时可能掉到 10 Hz 以下。
- **不支持任意分辨率/任意导出**：解码器覆盖常见 YOLO 布局，但非 YOLO 检测模型需要改 `detector.py`。
- **`cv2.dnn` 回退无 metadata**：类名退化为 `class_<id>`；优先保证 `onnxruntime` 可用。
- **广角（`YOLO_STREAM=wide`）未做质量验证**：鱼眼畸变对检测的影响没有量化，
  目前只作为「对比长焦/广角视野」的开关。
- **GPU 后端是实验性功能**：ncnn + Vulkan 只在桌面 PC 的 Radeon 780M 上离线验证过，
  **未实车、未与 `modeld` 同跑验证**。780M 是集显，`modeld` 也在用它；GPU 争用可能让
  `modeldLagging` 变差，进而触发接管提醒。默认关闭，开启前请自行评估。
- **GPU 失败不会自动回退 CPU**（有意为之）：Vulkan/模型/推理失败会让 `yolod` 记录错误并退避
  重试；若要回 CPU，显式设置 `YOLO_BACKEND=onnxruntime`。
- **ncnn 与 ORT 的检出不是逐位相同**：同类 IoU≥0.5 匹配率 98.5–99.1%、平均 IoU 0.98，
  框位置与个别小目标会有差异；需要与 CPU 结果严格一致的对比实验时应固定后端。
- **ncnn 依赖 Vulkan 驱动**：只有软件 Vulkan（llvmpipe/SwiftShader）时会被明确拒绝；
  不同 GPU / 驱动版本的速度会不同，换机器需重测。
- **camerad 重启**时最多有 5 秒无帧（之后自动重连），期间不发消息。
- **不写 rlog**：`should_log=False`，历史路线里没有 `yoloObjects`，离线分析要看实时 msgq
  或自己录包。

## 11. 测试与验证（全离线）

```bash
cd /data/sunnypilot
.venv/bin/python -m pytest openpilot/sunnypilot/yolod/tests -q      # 81 passed
.venv/bin/python -m ruff check openpilot/sunnypilot/yolod/
```

覆盖内容：

- `test_nv12.py`：stride padding / `uv_offset` 空隙 / 奇数尺寸报错 / 与 camerad 的 PyAV NV12
  打包方式逐像素交叉验证。
- `test_detector.py`：letterbox 几何与逆映射、置信度阈值、逐类 NMS、类别过滤、
  转置 / objectness / 端到端布局、越界裁剪、metadata 解析。
- `test_messages.py`：`yoloObjects` 作为 `log.Event` 的序列化往返、字段映射、条数上限、
  全量 `classNames` 语义。
- `test_yolod_config.py`：环境变量默认值/覆盖/非法值回退/范围钳制、`YOLO_STREAM` 选择与回退、
  `YOLO_BACKEND` 解析与后端工厂（onnxruntime / cv2dnn / ncnn 路径与过滤器传递）。
- `test_ncnn_backend.py`：ncnn 缺失或模型缺失时 skip；导出工具（pnnx 命令、names JSON 往返、
  `--skip-convert`/`--dry-run`）、软件 Vulkan 拒绝、Vulkan 失败不退化为 CPU、names 与 ONNX
  metadata 一致；**GPU 推理放在子进程**（`tests/ncnn_helper.py`），与 ORT 在 10 帧真实夜间画面上
  对比检出匹配率 / IoU / 计数差与耗时。
- `test_model_smoke.py`：真实模型加载、metadata（80 类）、一次真实推理、过滤器解析，
  以及 `YOLO_BACKEND=cv2dnn` 强制回退路径（无 metadata → `class_<id>`）。
- `test_video.py`：PyAV 与 ffmpeg 管道两条解码路径结果一致、`every`/`max_frames`、route 目录解析。
- `test_yolod_daemon.py`：用进程内 `VisionIpcServer`（名字不是 `camerad`，不碰行车栈）模拟相机，
  验证 `YoloEnabled` 门控、VisionIPC 取帧、10 Hz 循环、msgq 发布 `log.Event` 以及 stop event 退出。

未做/不能做的验证：本任务**没有启动 sunnypilot 栈**，所以没有在真实运行中的
`camerad` → `yolod` → `yoloObjects` 链路里实测（用 VisionIpcServer 离线模拟过 recv/转换/检测/发布，
recv 与端到端数字见第 6 节）。上车验证时应确认：`YoloEnabled` 打开后进程常驻、
msgq 上有 10 Hz 的 `yoloObjects`、CPU 占用符合预期、没有影响 `modeld` 的 `modeldLagging`。

## 12. 文件

```text
openpilot/sunnypilot/yolod/
├── detector.py          # Detection / letterbox / 解码 / NMS / Detector（ORT + cv2.dnn）
├── detector_ncnn.py     # NcnnDetector（ncnn + Vulkan，实验性 GPU 后端）
├── paths.py             # models/ 下各模型文件的默认路径
├── nv12.py              # NV12（含 stride/uv_offset）→ BGR
├── video.py             # 录像读取（PyAV，失败回退 ffmpeg 管道）
├── yolod.py             # 守护进程 + --video 调试模式 + YOLO_BACKEND 选择
├── models/yolo11n.onnx  # YOLO11n COCO（AGPL-3.0，见第 9 节）
├── models/yolo11n.ncnn.param/.bin  # 同一模型的 pnnx 转换产物（ncnn 后端）
├── models/yolo11n.ncnn.names.json  # 80 类类名（导出工具生成）
├── tools/eval_video.py  # 离线评测：标注 mp4 + JSON（--backend onnxruntime/cv2dnn/ncnn）
├── tools/export_ncnn.py # ONNX → ncnn param/bin + names JSON（pnnx）
├── tests/               # 离线单元测试（GPU 部分用子进程，见 ncnn_helper.py）
└── README.md
```
