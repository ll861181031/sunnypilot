# tools/yolo_dataset — 道路目标检测数据集构建流水线

从车端录制（`~/.comma/media/0/realdata/`）到可直接标注的 YOLO 数据集，全流程离线、
可断点续跑、可复现。产出格式与 `tools/yolo_annotator/`（Windows 标注器）严格对齐。

```
抽取(extract.py) → 预标(autolabel.py) → 选帧组装(build.py) → 校验(validate.py)
                                                ↑
                          回传合并(merge_inbox.py) ← 标注器上传
```

## 目录约定

产物一律放仓库外（默认 `/data/yolo_dataset`），仓库里只有代码：

```
/data/yolo_dataset/
  _frames/<route>/<nnnn>.jpg   抽出的帧（临时，成功后自动清理）
  _meta/<route>.json           每帧的 t / 小时 / 亮度 / 源分辨率
  _auto/<route>.json           每帧的自动预标结果（含 conf）
  _extract_summary.json        抽帧统计
  _autolabel_summary.json      预标统计
  pilot_200/                   小批量联调集（200 帧，与正式批同格式）
  batch_001/                   正式批次
  batch_001.tar.gz             打包产物（scp 给 Windows 用）
```

`run_batch.sh` 在**校验通过后**才删 `_frames`（省 0.8–3 GB）；`_meta`/`_auto`（十几 MB）保留，
是自动预标的可追溯来源。代价是：删掉 `_frames` 后想重新选帧必须重新抽帧（12k 批约 6 分钟）。

## 四个阶段

### 1. `extract.py` — 抽帧

按 route 用 ffmpeg 解码 `fcamera.hevc`，默认**每 2 秒 1 帧**（`--fps 0.5`），
JPEG 质量 `-q:v 2`，统一输出 **1344x760**；默认 8 个进程并行，每段先写 `.part`
目录再整体改名，所以中途被杀不会留下半截数据。

```bash
.venv/bin/python tools/yolo_dataset/extract.py --work /data/yolo_dataset            # 全部 route
.venv/bin/python tools/yolo_dataset/extract.py --work /data/yolo_dataset --routes 12  # 小批量
.venv/bin/python tools/yolo_dataset/extract.py --work /data/yolo_dataset \
    --route 00000097--4f3f72a3b0--12 --route 00000047--fa9cbadc1b--4                # 指定段
```

- **跳过 `boot` 目录**；没有/读不出 `fcamera.hevc` 的段记为 `no_camera` / `unreadable`，不影响整批。
- **小时**取 `fcamera.hevc` 的 mtime（loggerd 边录边写，误差 <1 分钟），用于昼夜配额。
- **亮度**在交付用的 JPEG 上算（灰度均值），只做统计与抽查——它**不能**判定昼夜：
  实测夜间路灯路面可到 135，比阴天白天还亮。
- 幂等：`_meta` 完整且帧文件都在的 route 直接跳过（`--force` 可强制重抽）。

### 2. `autolabel.py` — 自动预标

用 `openpilot/sunnypilot/yolod/models/yolo11n.onnx`（与 `yolod` 同一个模型/同一个
`Detector`），ONNX Runtime CPU，**每进程 2 线程**、默认 8 进程（≈23 ms/帧）。

- `--conf 0.18`、`--iou 0.45`：**刻意偏低**，保召回；低分框由标注员删除而不是漏标。
- COCO → 17 类的映射只走 `taxonomy.json` 的 `coco_to_taxonomy`（8 个类）；
  其余 COCO 类（`backpack`、`clock`…）计入 `dropped.unmapped_coco` 后丢弃，
  **不猜类别**。
- **骑手去重**：COCO 会把骑手单独标成 `person`，而分类规范要求"两轮/三轮的框包含骑行者、
  `person` 只标不在车上的人"。所以：`person` 框**中心落在**任一 `bicycle/e_bike/motorcycle/tricycle_*`
  框内，**或**与它的 IoU > 0.1，就丢弃该 `person`；站在旁边的行人保留。

### 3. `build.py` — 选帧与组装

每帧打分 `priority = 5×两轮车 + 2×行人 + 1×机动车`（COCO 检出计数），然后：

| 规则 | 参数 | 说明 |
|---|---|---|
| 昼夜配额 | `--day-frac 0.5` | 白天 08:00–17:59。某桶不够时缺口从另一桶补，实际比例写进 `stats.json` |
| 优先级 | 固定 | 桶内按 priority 从高到低取，同分按 `(route, t)` 稳定排序 |
| 负样本 | `--neg-frac 0.1` | 随机取"无任何检出"的帧；不够时用"只有信号灯/标志"的背景帧补 |
| 每段上限 | `--per-route-cap 40` | 防止单一路段刷屏 |
| train/val | `--val-frac 0.15` | **按 route 划分**，整段进 train 或 val，杜绝同路段泄漏 |

ID 为 6 位序号（`000001`…），按 `(route, t)` 排序分配，同一段的帧在标注器列表里相邻。

### 4. `merge_inbox.py` — 合并标注器回传

标注器（Windows）通过 SFTP 上传到 `/home/lsr/yolo_label_inbox/<batch>/`：

```
labels/<id>.txt      只传改动过的（空文件 = 负样本，照样合并）
review_state.json    全量
sync_manifest.json   参考信息（缺了也能合并，只用来打印 last_sync/pending）
```

**用法（先 dry-run，再真合并）**：

```bash
PY=/data/sunnypilot/.venv/bin/python

# 1) 只校验 + 报统计，一个字节都不写（推荐每次先跑）
$PY tools/yolo_dataset/merge_inbox.py --batch-dir /data/yolo_dataset/batch_001 --dry-run
# batch=batch_001 inbox=/home/lsr/yolo_label_inbox/batch_001 (dry-run)
#   manifest: last_sync=2026-10-26T19:14:16Z pending=1
#   files=1 merged=0 unchanged=1 rejected=0 negatives=0
#   reviewed=0 edited=0
#   dry-run: nothing written

# 2) 真合并（自动备份，可选 --json 输出机器可读报告）
$PY tools/yolo_dataset/merge_inbox.py --batch-dir /data/yolo_dataset/batch_001

# 3) 指定别处的 inbox / 批次名（批次名默认取自 dataset.json）
$PY tools/yolo_dataset/merge_inbox.py --batch-dir <dataset> --inbox <dir> --batch <name>
$PY tools/yolo_dataset/merge_inbox.py ... --json
```

**规则**

- **校验**：id 必须存在于 `dataset.json`；每行 5 列；class 为整数且在 `[0, 21]`；
  4 个坐标都在 `[0, 1]`；`w>0, h>0`。**任一行不合法 → 整个文件拒绝**（该 id 的旧 labels
  原样保留），输出里列出 id + 具体原因，退出码仍为 0（便于批量跑），但报告里 `rejected` 非 0。
- **备份**：真合并前把要覆盖的旧 `labels/<id>.txt` 复制到
  `<batch>/labels_backup/<YYYYmmdd-HHMMSS>/`，回滚就是把目录拷回去。
- **幂等**：与现有 labels 规范化后逐字节相同的文件算 `unchanged`，不重写、不备份。
- **review 状态**：`reviewed`/`edited`/`updated` 取自上传的 `review_state.json`，
  另外**用内容再核一遍** `edited`（与 `auto_labels` 不同即置 true），不单靠上传方标志。
- **护栏**：inbox 目录名必须等于批次名，否则拒绝合并（id 是流水号，串批会按 id 覆盖真值）；
  确实要合并改过名的目录时加 `--force-inbox`。
- **统计**：打印逐类框数 `before -> after (差)`、已复核/已编辑张数、负样本文件数。

## 交付格式（与标注器约定）

```
<batch>/
  images/<id>.jpg          1344x760
  labels/<id>.txt          当前标签，初始 = auto_labels；空文件 = 负样本
  auto_labels/<id>.txt     自动预标原样
  auto_meta.json           {"<id>": [{"cls","conf","xyxy"}]}  归一化 xyxy
  dataset.json             schema=1 / batch / classes / images[] / counts / split
  taxonomy.json            17 类（含中文名、颜色、coco 映射）
  review_state.json        {"<id>": {"reviewed","edited","updated"}}  初始全 false
  train.txt / val.txt      相对路径 images/<id>.jpg
  data.yaml                Ultralytics 配置（path/train/val/nc/names）
  stats.json               逐类/昼夜/route/priority/conf 分布
  README.md                中文说明
```

YOLO 行格式 `cls cx cy w h`，6 位小数、UTF-8、`\n` 分隔。

## 几何：为什么是 letterbox 而不是直接拉伸

源数据两种分辨率：418 段 `1344x760`（16:9-ish）、156 段 `1928x1208`（1.596）。直接
resize 到 1344x760 会把后者**横向拉宽 10.8%**——物体长宽比失真，且同一数据集里 27% 的帧
和其余帧不是一个几何，属于人为域偏移。所以非等比例的源用 **letterbox**：等比缩放到高
760（宽 1213），左右各补 65px 灰边（114），**视野全保留、无拉伸**。每张图的原始分辨率
记在 `dataset.json` 的 `source_width/source_height`。

## 常用命令

```bash
PY=/data/sunnypilot/.venv/bin/python
$PY tools/yolo_dataset/extract.py   --work /data/yolo_dataset --routes 12
$PY tools/yolo_dataset/autolabel.py --work /data/yolo_dataset --workers 8
$PY tools/yolo_dataset/build.py     --work /data/yolo_dataset --out /data/yolo_dataset \
                                    --batch pilot_200 --target 200 --keep-frames --package
$PY tools/yolo_dataset/validate.py  --dataset /data/yolo_dataset/pilot_200
$PY tools/yolo_dataset/merge_inbox.py --batch-dir /data/yolo_dataset/batch_001 --dry-run

# 全量一条龙（后台跑，日志/PID 落在 /tmp）
setsid nohup tools/yolo_dataset/run_batch.sh > /tmp/yolo_batch_001.log 2>&1 &
echo $! > /tmp/yolo_batch_001.pid

# 夜间补充批：只用夜间 route、1 秒 1 帧、全夜间（day_frac=0），产物仍写到主目录
BATCH=batch_002_night WORK=/data/yolo_dataset_night OUT=/data/yolo_dataset \
  HOURS=night FPS=1.0 TARGET=5000 DAY_FRAC=0.0 PER_ROUTE_CAP=60 SAMPLE_FPS=1.0 \
  setsid nohup bash tools/yolo_dataset/run_batch.sh > /tmp/yolo_batch_002_night.log 2>&1 &
```

## 测试

```bash
.venv/bin/python -m pytest tools/yolo_dataset/tests -q      # 60 项，纯离线
.venv/bin/python -m ruff check tools/yolo_dataset
```

## 校验交付物

每批产出后都用 `validate.py` 验一遍（只读，不改数据），非 0 退出码即有问题：

```bash
.venv/bin/python tools/yolo_dataset/validate.py --dataset /data/yolo_dataset/pilot_200
# pilot_200: 1906 checks, 0 errors, 1 warnings
#   WARNING classes with zero boxes in this batch: ['e_bike', 'tricycle_cargo', ...]
# OK
```

它逐项检查：必需文件/目录齐全；`schema=1`；class id 连续 0..21 且 `taxonomy.json` 与
`dataset.json` 一致；id 是 6 位零填充且不重复；`auto_meta.json`/`review_state.json` 的 key
覆盖全部 id、初始 `reviewed` 全 false；逐图（JPEG 可解码、真实像素 == `dataset.json`
的 `width/height`、`labels` 与 `auto_labels` 存在且每行合法、`labels` 行数 == `auto_meta`
框数）；**同一 route 不得同时出现在 train/val**；`train.txt`+`val.txt` 覆盖全部图像且
无交叉、路径都是相对 `images/`；`data.yaml` 的类的数量与名字。类别为 0 框、昼夜比例异常
只报 WARNING 不报错（新批次首次出现空类是正常的）。

覆盖：taxonomy/COCO 映射、昼夜边界、letterbox 几何与补边、YOLO 行格式与非法输入、
骑手去重（7 种两轮类 + IoU 路径 + 邻居保留）、优先级、昼夜配额与缺口、每段上限、
负样本、seed 可复现、route 级 split、build 全链路契约（含 labels==auto_labels、
route 不泄漏、stats 数值）、打包、merge_inbox（合法/非法/未知 id/缺 inbox/dry-run/备份）、
validate 能抓出缺图/尺寸错/泄漏、`--help` 独立性。

## 多批次一起训练：`--split-from`（重要）

`train/val` 是按 route 划的，但**按批次各自划**。若两批包含相同的 route（例如
`batch_002_night` 的 128 条夜间 route 全都在 `batch_001` 里），各自独立划分会把同一条
路放进一批的 train、另一批的 val，**拼起来训练就泄漏**（实测过：128 条共享 route 里
34 条两边 split 不一致）。

所以后续批次一律沿用已有划分：

```bash
SPLIT_FROM=/data/yolo_dataset/batch_001/dataset.json \
BATCH=batch_002_night WORK=/data/yolo_dataset_night OUT=/data/yolo_dataset \
HOURS=night FPS=1.0 TARGET=5000 DAY_FRAC=0.0 PER_ROUTE_CAP=60 SAMPLE_FPS=1.0 \
setsid nohup bash tools/yolo_dataset/run_batch.sh > /tmp/yolo_batch_002_night.log 2>&1 &
```

`--split-from` 可以给多次；它只提供"已定好的 route→split"，参考里没有的新 route 再由
`--seed` 决定。合并多个批次训练时，用各批 `dataset.json` 的 `split` 字段（或 train.txt /
val.txt）拼文件列表，**不要重新随机划分**。

## 设计取舍（写给以后改它的人）

- **不追求"凑数"**：`conf=0.18` 的召回优先策略 + `auto_meta` 的 conf 让标注员能一眼看出
  哪些框可疑；宁可多标几个假框让删，也不要漏掉电动车。
- **预标不伪造类别**：COCO 没有 `e_bike`/三轮类，预标里这些类恒为 0，由人工补。
- **route 是切分单位**：同一路段的帧高度相似，按帧随机切会让 val 虚高。
- **昼夜按小时而非亮度**：实测夜间路灯路面亮度可达 135，比阴天白天更亮，亮度阈值不可靠。

## GPU 自动预标（2026-10-27）

按用户要求，离线自动预标改在 Windows 主机（RTX 4070 SUPER）上跑 CUDA：

```powershell
C:\Users\lsr\sunnypilot-yolo\venv\Scripts\python.exe ^
  C:\Users\lsr\sunnypilot-yolo\yolo_dataset\autolabel_gpu.py ^
  C:\Users\lsr\sunnypilot-yolo\dataset\batch_001 --out-dir C:\Users\lsr\sunnypilot-yolo\gpu_auto --batch 32
```

输入是已抽好的 `images/`（源仓库里的 `autolabel_gpu.py` 同一份）。输出 `auto_labels/` +
`auto_meta.json`，格式与 CPU 版一致。**源视频抽帧仍在本机（数据准备）**，模型算力全部放到
Windows；若要连抽帧也搬，需要先把 234GB 源视频拷到 Windows。

## 国旗误标清理（2026-10-27）

COCO YOLO11n 会把中国国旗（红底黄星）误标成 `traffic_light`。`fix_flag_traffic_lights.py`
按 HSV 红色占比 + 框面积清理**自动标签**，默认跳过 `review_state` 里 reviewed/edited 的图：

```powershell
C:\Users\lsr\sunnypilot-yolo\venv\Scripts\python.exe ^
  C:\Users\lsr\sunnypilot-yolo\yolo_dataset\fix_flag_traffic_lights.py ^
  C:\Users\lsr\sunnypilot-yolo\dataset\batch_001 --report ...\flagfix_b1.json
```

- 阈值默认 `red>=0.75`、`area>=0.0001`；实测国旗红色占比 0.83–0.97，真实信号灯远低于此。
- 只改 `labels/`、`auto_labels/`、`auto_meta.json`；**已人工 edited/reviewed 的图整张跳过**。
- 2026-10-27 实跑：batch_001 577 图/717 框、batch_002_night 313 图/358 框；备份在
  `<batch>/labels_backup_flagfix_*`。
