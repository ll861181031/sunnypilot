# sunnypilot YOLO 标注器（Windows 轻量桌面版）

Tkinter + Pillow 的单文件桌面标注器：打开批次 → 浏览/筛选图片 → 画框/改类/复核 →
后台自动把标签回传 Linux 收件目录。**不需要 GPU，不加载 torch / ultralytics，
也不会占用显卡**；Windows 上常驻内存约 60–120 MB。

数据集格式与 `tools/yolo_dataset` 流水线一致，类别表从**批次的 `taxonomy.json` 读取**
（不写死类别名和数量，当前 17 类）。

---

## 1. 目录与文件

```
yolo_labeler/
  labeler.py            # 主程序（GUI + --selftest + --screenshot）
  dataset.py            # 数据集/标签/复核状态读写（原子保存）
  geometry.py           # 坐标变换、框命中、8 手柄缩放（纯函数）
  sync.py               # paramiko 后台上传（debounce / 退避 / 降级）
  undo.py               # 每张图独立的撤销栈
  demo.py               # 合成演示批次（自检/联调用）
  taxonomy.json         # 17 类类别表副本（仅演示批次需要；正式批次用数据集自带的那份）
  install.bat           # 建 venv + 安装 Pillow / paramiko / pytest
  run_labeler.bat       # 用 pythonw 启动（无控制台窗口）
  selftest.bat          # 无头自检 + 截图
  test.bat              # 跑单元测试
  sync.json.example     # 同步配置模板（复制成 sync.json 再改）
  labeler.log           # 运行日志（自动生成）
  sync.log              # 同步日志（自动生成）
```

Windows 部署目录：`C:\Users\lsr\sunnypilot-yolo\yolo_labeler`
默认批次：`C:\Users\lsr\sunnypilot-yolo\dataset\batch_001`（**12,000 张**，17 类，66,312 个预标框）
联调批次：`C:\Users\lsr\sunnypilot-yolo\dataset\pilot_200`（200 张）

实测性能（12,000 张批次 / 本机 Windows）：启动到首图可操作约 **1.2 s**（含 12,000 行图片列表填充），
筛选（未复核 / 有自动两轮 / 高优先级）**0.1–0.2 s**，切到第 5000 张约 **0.4 s**，缩放/平移每帧 **20–45 ms**。

---

## 2. 安装

**推荐**：双击 `install.bat`。它会：

1. 若 `C:\Users\lsr\sunnypilot-yolo\venv` 不存在则用 `python -m venv` 创建；
2. `pip install --upgrade pillow paramiko pytest`；
3. 打印 Python / Pillow / paramiko / Tk 版本做自检。

**手工等效命令**（PowerShell）：

```powershell
C:\Users\lsr\sunnypilot-yolo\venv\Scripts\python.exe -m pip install --upgrade pillow paramiko pytest
C:\Users\lsr\sunnypilot-yolo\venv\Scripts\python.exe -c "import PIL,paramiko,tkinter;print(PIL.__version__,paramiko.__version__,tkinter.TkVersion)"
```

要求：Python 3.12（venv 自带），Tk 8.6（Python 官方安装包已含），Pillow ≥ 10，paramiko ≥ 3。
`paramiko` 缺失时程序仍可运行，只是**仅本地保存、不上传**。

---

## 3. 启动

```bat
rem 默认批次
run_labeler.bat

rem 指定别的批次（参数会覆盖默认 --dataset）
run_labeler.bat --dataset C:\Users\lsr\sunnypilot-yolo\dataset\pilot_200

rem 只本地保存，不起同步线程
run_labeler.bat --no-sync
```

也可以用控制台方式运行以便看报错：

```powershell
C:\Users\lsr\sunnypilot-yolo\venv\Scripts\python.exe ^
  C:\Users\lsr\sunnypilot-yolo\yolo_labeler\labeler.py --dataset D:\data\batch_001
```

**数据集目录的查找顺序**：`--dataset` → 环境变量 `YOLO_DATASET` → 程序目录下的
`dataset_dir.txt`（里面写一行批次目录路径）→ 默认 `C:\Users\lsr\sunnypilot-yolo\dataset\batch_001`。
都找不到时弹窗提示，不会静默失败。

一个合法的批次目录必须含 `dataset.json` 与 `taxonomy.json`（或 `dataset.json` 里的
`classes`）。

> 图片统一 1344×760；其中约 1/3 来自 1928×1208 的源视频，用 letterbox 补了左右各 65 px 灰边
> （`source_width`/`source_height` 记录了源尺寸）。标注器一律用 `dataset.json` 的
> `width`/`height` 做坐标映射，灰边不需要特殊处理——按画面实际内容画框即可。

---

## 4. 界面

```
┌─ 顶部：批次/路径 · 已复核 N/M (百分比) · 保存 · 立即同步 · 统计 · 帮助 ─┐
│ 筛选+搜索 │           画布（缩放/平移/画框）          │ 17 类调色板 │
│ 图片列表  │                                           │ 选中框信息  │
└─ 底部：状态消息 · 缩放/尺寸/框数 · 快捷键提示 ──────────────────────────┘
```

- **左侧列表**：`✓`=已复核，`·`=未复核，`✎`=手工改过；行尾是 `P优先级` 与 route 短标签。
- **筛选**：全部 / 未复核 / 已复核 / 有自动两轮 / 高优先级 / 有自动预标 / 已手工修改。
  “高优先级”= `priority ≥ 3`（数据流水线里两轮车权重最高）。
- **画布**：**虚线框**=自动预标（未改动，带 conf）；**实线框**=手工框。
  选中框显示 8 个白色手柄。
- **右侧调色板**：每格是类别色块 + 中文名 + 快捷键；当前类别高亮（凹陷边框）。
  选中框时点类别 = 改这一类；没选中时 = 设为**新框的默认类别**。

---

## 5. 快捷键

| 按键 | 作用 |
| --- | --- |
| `A` / `←` | 上一张（自动保存当前图） |
| `D` / `→` | 下一张（自动保存） |
| `PageUp` / `PageDown` | 上/下 10 张 |
| 左键拖拽空白 | 画新框（用当前类别） |
| 左键点框 | 选中 |
| 左键拖动框内 | 移动框 |
| 拖动 8 个手柄 | 缩放框（四角 + 四边） |
| 双击框 / 右键单击框 | 弹出类别菜单（改类 / 删除） |
| `Del` / `Backspace` | 删除选中框 |
| `1`–`9`, `0` | 第 1–10 类 |
| `Shift+1`–`Shift+9`, `Shift+0` | 第 11–17 类 |
| `F1` / `F2` | 第 21 / 17 类 |
| 滚轮 | 以光标为中心缩放 |
| `+` / `-` | 放大 / 缩小 |
| `F` | 适应窗口 |
| `空格`+拖拽 / 中键拖拽 / 右键拖拽 | 平移画布 |
| `Ctrl+S` | 保存当前图 |
| `Ctrl+Z` | 撤销（每张图独立历史，最多 80 步） |
| `Ctrl+Y` / `Ctrl+Shift+Z` | 重做 |
| `N` | 标记“无目标”并复核（清空本图所有框后保存为空文件） |
| `R` | 切换本图“已复核” |
| `H` | 帮助窗口 |
| `Esc` | 取消选中 / 关帮助 |

> 焦点在搜索框里时，数字等按键不会触发类别快捷键（避免误改）。

---

## 6. 保存与复核

- 保存写 `labels/<id>.txt`（YOLO `class cx cy w h`，归一化、clamp 到 0–1、6 位小数）。
  **空文件 = 负样本（无目标）**，这是流水线约定的语义。
- 同时更新 `review_state.json`：`{"<id>": {"reviewed": bool, "edited": bool, "updated": unix}}`。
  `edited` = 当前标签与 `auto_labels/` 不一致（自动算，不需要手动维护）。
- 写文件是**原子**的（临时文件 + `os.replace`），断点/断电不会留下半个文件。
- 翻页、关闭窗口、每 20 秒都会自动保存有改动的图。

---

## 7. 同步（回传 Linux）

### 7.1 配置

把 `sync.json.example` 复制成 `sync.json`（与本程序同目录），按需修改：

```json
{
  "host": "192.168.123.70",
  "port": 22,
  "user": "lsr",
  "key_path": "C:\\Users\\lsr\\sunnypilot-yolo\\id_ed25519_labeler",
  "remote_dir": "/home/lsr/yolo_label_inbox/<batch>",
  "auto_upload": true,
  "timeout": 10
}
```

- `remote_dir` 里的 `<batch>`（或 `{batch}`）会替换成批次名，例如
  `/home/lsr/yolo_label_inbox/batch_001`。
- 私钥必须是 Windows 路径；也可以用 `password` 字段（不明文推荐用密钥）。
- `auto_upload: false` = 不自动上传，只由“立即同步”按钮/退出时上传。

### 7.2 上传内容与远端布局

```
/home/lsr/yolo_label_inbox/<batch>/
  labels/<id>.txt          # 只传“本次修改过”的标签（增量）
  review_state.json        # 全量复核状态
  sync_manifest.json       # 批次、counts、last_sync、app_version、文件清单、pending
```

`sync_manifest.json` **最后写**，合并脚本可以拿它判断这一批上传是否完整。

### 7.3 触发时机

- 保存后 **debounce 3 秒**（连续保存只传一次）；
- **退出程序时**（阻塞上限约 8 秒，失败也只记日志）；
- 每 **60 秒**兜底一次（仅当有未上传改动）；
- 右上“立即同步”按钮随时强制上传；
- 失败后**指数退避**（2、4、8 … 秒，上限 120 秒），状态栏显示失败原因与重试次数。

网络/认证失败不会影响标注：标签照常写本地，状态栏变红提示，恢复后自动补传。

### 7.4 日志

- `sync.log`：连接、上传、失败原因（每次尝试一行）。
- `labeler.log`：启动异常、保存异常等（pythonw 没有控制台，排查先看这里）。

### 7.5 同步自检（排查网络/权限）

```powershell
C:\Users\lsr\sunnypilot-yolo\venv\Scripts\python.exe labeler.py ^
  --sync-check --dataset C:\Users\lsr\sunnypilot-yolo\dataset\pilot_200
```

不打开界面，直接把**首图的标签 + review_state.json + sync_manifest.json** 上传一次，
打印 `sync.json` 路径、`remote_dir`、上传结果和 `SYNC CHECK OK/FAILED`。
失败时能直接看到具体原因（私钥、认证、目录权限、网络）。

### 7.6 拉取新批次（实验性）

```powershell
C:\Users\lsr\sunnypilot-yolo\venv\Scripts\python.exe labeler.py --pull batch_002.zip --dest C:\Users\lsr\sunnypilot-yolo\dataset
```

从 `remote_dir` 下载 zip 并安全解压（拒绝 `..`/绝对路径条目）。批次名用 `--pull` 的
文件名主干替换 `<batch>`；如果远端批次名与 zip 名不同，请把 zip 放在
`<remote_dir>/<batch>.zip`。

---

## 8. 自检与测试

```bat
rem 1) 无头自检（建窗、加载首图、缩放、画框、撤销/重做、保存、N 记负样本、翻页、筛选、截图）
selftest.bat
rem 结果在 selftest.png，控制台打印 SELFTEST OK / 每一步 OK

rem 2) 单元测试（dataset / geometry / undo / sync，paramiko 用假实现，不需要网络）
test.bat
```

Linux 上同样可跑：

```bash
xvfb-run -a /data/sunnypilot/.venv/bin/python -m pytest tools/yolo_annotator/tests -q
xvfb-run -a /data/sunnypilot/.venv/bin/python tools/yolo_annotator/labeler.py --selftest --dataset /tmp/demo_batch
```

没有真实批次时，`--selftest` 会自动生成一个合成演示批次（`demo.py`），所以自检
永远可跑。**`--selftest` 结束后会恢复首图标签与 `review_state.json` 的原始字节**，
因此对正式批次跑也不会改数据（已用 pilot_200 校验 md5 一致）。

---

## 9. 故障排查

| 现象 | 原因 / 处理 |
| --- | --- |
| 双击没反应 / 一闪而过 | 用控制台跑 `python labeler.py --dataset ...` 看报错；或看 `labeler.log` |
| 弹窗“未找到数据集” | `--dataset` 指错、`dataset_dir.txt` 内容不对，或批次里没有 `dataset.json` |
| 弹窗“缺少 dataset.json / 类别表” | 批次不完整，让流水线重新导出 |
| 状态栏“同步: 关闭（未配置 sync.json）” | 没有 `sync.json` 或 `paramiko` 没装 —— 只本地保存，属正常降级 |
| 状态栏“同步: 关闭（paramiko 未安装）” | 跑 `install.bat` |
| 状态栏“同步: 失败 … 私钥不存在” | `key_path` 写错；确认 `C:\Users\lsr\sunnypilot-yolo\id_ed25519_labeler` 存在 |
| 同步失败“SSH 连接失败” | Linux 主机没开机/网络不通；`ssh -i <key> lsr@192.168.123.70` 手工验证 |
| 上传目录没权限 | 远端 `~/yolo_label_inbox` 需要可写；`chmod u+w` |
| 中文显示成方块 | 系统缺中文字体；Tk 默认字体会回退，通常不影响操作 |
| 通过 SSH 跑 `--screenshot` 失败 | SSH 进程在 Windows **Session 0**（隔离会话），窗口不会被合成，`ImageGrab`/`PrintWindow` 都拿不到画面；这是系统限制。在交互桌面双击 `selftest.bat` 即可正常截图（程序会优先用 `PrintWindow` 只抓本窗口，不会截到别的程序） |
| 日志里中文乱码 | `labeler.log`/`sync.log` 是 UTF-8；PowerShell 5 默认按 ANSI 读会乱码，用 `Get-Content -Encoding UTF8` 或记事本打开 |
| 界面太大/太小 | 用鼠标拖窗口边缘；程序按屏幕分辨率自适应（最小 880×620） |
| 图片很糊 | 那是缩放比例低于 100%；滚轮放大即可（标注框坐标不受显示缩放影响） |
| 标签写不进去 | `labels/` 只读，或磁盘满；错误会弹窗并写 `labeler.log` |
| 首次启动较慢（约 1–2 s） | 12,000 张批次要读 `dataset.json` 并填充左侧列表，属正常 |
| 预标框很少/没有 | 该图可能本来就是负样本（12,000 张里有 1,720 张空标签）；`e_bike` 与 4 个三轮类 COCO 不出框，需要人工改类 |

---

## 10. 已知限制

- 一次只打开一个批次；切换批次需重启程序（`--dataset` 参数）。
- 撤销历史只保留最近 200 张图（每张 80 步），重启程序后清空（标签已落盘）。
- 同步是“增量 + 全量状态”，不做删除同步；远端多余文件不会被清理。
- 拉取批次是实验性功能（`--pull`），只做了安全解压与基本错误提示。
- 未做多显示器 DPI 适配（Windows 高 DPI 下若字体偏小，可调系统缩放）；
  窗口尺寸按 Tk 报告的屏幕尺寸自适应（1200×780 上限），1024×768 也能用。
- 截图功能：交互桌面下用 Win32 `PrintWindow` 只抓标注器窗口（不会截到其它程序）；
  SSH / 服务会话（Session 0）做不到，会退化成"图片 + 框"的离线合成预览。
- 单元测试需要 `pytest`（`install.bat` 会装；Windows venv 里没装时 `test.bat` 会报缺模块）。
- 本工具只写数据集目录与自己的日志/配置，不接触 sunnypilot 运行栈、不写硬件。
