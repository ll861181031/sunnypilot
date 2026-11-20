# 横向参数扫描（sp_lat_tune）

把「调横向参数」从手艺变成流水线：**每趟自动打分 → 记进试验库 → 出下一组参数 → 实车验证**。

配套的运行时改动在 `openpilot/selfdrive/car/card.py` 和
`opendbc/car/byd/carcontroller.py`：两个跟踪参数现在**不用重启栈**就能换，
所以**一趟行驶可以覆盖多组参数**。

## 为什么需要它

AGENTS.md 记录的调参史是「一次改一个维度」：`STEER_ANGLE_ERROR_MAX`
改过 6 次（4.0 → 6.0 → 回退 → 分档表），速率表改过 3 次，中间还有一次
把 28~30 km/h 的结论当成「全线不可行」。每一次都是**一趟实车 + 几小时手工分析**。

设置页能改的只有 2 维，而且**只能收紧、不能放宽**（车厂表是上界，写大了是空操作）：

| 参数 | Params 键 | 有效范围 | 作用 |
|---|---|---|---|
| 高速端超前上限 | `BydSteerLeadMaxDeg` | 5–34（0.5–3.4°） | 只作用于 ≥11 m/s 的两段；≤15 km/h 平段不受它控制 |
| 速率上限 | `BydSteerRateMax` | 50%、60%、70%、80%、90%、默认 | 默认保留完整的 200/160/100°/s 速率表；查表节点为 5/15/30 m/s |

所以**"扫设置页找更优横向参数"在数学上不存在**：不收窄就是空操作。想变好只有放宽车厂表
（`values.py`，主机侧、改完重启栈）或者走"测试专用放宽表"通路。分带证据见
`artifacts/lat-bands-20261118/REPORT.md`。

## 用法

```bash
# 1. 给已有录像打分（离线，不起栈）
python -m tools.sp_lat_tune.cli score ~/.comma/media/0/realdata/00000051--201b6e1171--0

# 2. 看试验表
python -m tools.sp_lat_tune.cli report

# 3. 要下一组参数
python -m tools.sp_lat_tune.cli next -n 3

# 4. 写进 Params（下一次横向重新接管时生效）
python -m tools.sp_lat_tune.cli apply 5.5 2.4
```

## 分带诊断（`bands`）

参数是不是瓶颈，取决于**哪一段**：同一趟里 15–40 km/h 可能有 8% 的帧被上限削掉，而 40–72 km/h
只有 0.5%。`bands` 把一段录像按 15/40/72 km/h 切开：

```bash
# 单段 / 单条路线
python -m tools.sp_lat_tune.bands score ~/.comma/media/0/realdata/000001ba--60fdc565ec--1
# 全库样本（按 mtime 取最新 N 段，合并统计）
python -m tools.sp_lat_tune.bands scan ~/.comma/media/0/realdata --limit 120 --json out.json
```

每带输出：激活帧、中位车速、触到上限比例、被削比例、`lead` p50/p90/p95、削掉量 p90/p95、
`ay_n`（达到需求门槛的帧数）、欠转与严重欠转、EPS 拒绝（**按上限/驾驶员/未知三类分开**）。

`请求帧` 统计 `carControl.latActive`，不等于提交数。`实测/估算` 分别给出两种限幅样本数：
有 `bydLateral` 的分段只用有效、已提交且激活的诊断，同一采样时间只算一次；请求角、实测角和
`leadBoundDeg` 均取自同一条诊断，按采样时刻就近匹配有效车速（最多相差 50 ms）。
缺失部分不补算。只有整个分段不含该诊断字段时才按当前默认表估算。
`at_bound` 和 `cut` 的分母是两种限幅样本之和。`LEAD/实测` 则只统计控制器真实标记；
没有实测样本时显示「无诊断样本」，不表示没有发生限幅。提交也不等于 EPS 已执行。

**欠转要在有需求的路段上算**。默认门槛 0.3 m/s² 是历史口径，直路小缓弯上这个量级一半是横摆噪声；
要判断"车跟不跟得住"，用：

```bash
python -m tools.sp_lat_tune.bands score <route> --min-ay 1.0 --min-ay-frames 500
```

`--min-ay` 只影响欠转样本（触限/`lead`/拒绝照旧统计），`--min-ay-frames` 让低于 500 帧的带直接判"无结论"。
2026-11-18 实测：最近 60 段里 `ay≥1.0` 的 15–40 km/h 帧只有 **11 帧**，那种路上算出的欠转 1.1 是噪声。

两条口径：

- **没有饱和就没有信息**：`at_bound < 10%` 的带会被标"无结论"，不要拿它的欠转说事。
- **拒绝要分类**：只有 `limitMask & LEAD` 且无驾驶员输入（`steeringPressed` 或原始力矩 ≥200）
  才算"上限导致"。驾驶员与 EPS 对顶造成的 `TORQUE_TEMP_FAILED` 记到驾驶员桶。

## 指标定义（就是 AGENTS.md 里那些手工算过的东西）

| 指标 | 定义 | 用途 |
|---|---|---|
| `lead_p50/p90/p99` | `\|请求角 − 实测车轮角\|` | 请求超前量，**限幅前的值** |
| `sat_ratio` | 请求超前 ≥ 上限的帧占比 | **上限有没有在卡住车** |
| `undershoot_aggregate` | `Σ(模型要的 ay) / Σ(实际 ay)` | 目标函数主项 |
| `undershoot_mean` | 逐帧 `ay_des/ay_act` 的均值 | 参考，受小分母影响大 |
| `severe_ratio` | `ay_des/ay_act > 2.0` 的占比 | 严重欠转 |
| `jerk_p99` | `v · d(yaw_rate)/dt` 的 P99 | 对照 `MAX_LATERAL_JERK = 3.6` |
| `toofast_frames` | `carState.steerFaultTemporary` 帧数 | **硬约束，不是成本项** |

普通 `cli score` 的 `sat_ratio` 是按表估算的指标；判断历史行程的实际限幅应使用上面的 `bands score`。
估算时默认上限表从 `opendbc.car.byd.values.CarControllerParams.STEER_ANGLE_ERROR_MAX` 读取，
不再手抄（2026-11-18 修：手抄的 2026-10 表让此前所有 `sat_ratio` 都按旧表算）。

两个必须知道的实现细节：

- **横摆角速度从 CAN `0x222` 解**，不是 `carState.yawRate`。这个移植**从不填**
  `yawRate`（实测每帧都是 0.0），用它算欠转会得到无穷大。
- **`lead` 是限幅前的请求**。`carControl.actuators.steeringAngleDeg` 是
  controlsd 的原始请求，BYD 的 carcontroller 之后才把它夹到 `实测 ± 上限`。
  所以 `sat_ratio` 的含义正是「这一帧被上限切掉了」。

## 硬约束（这是 6.0 那次失败的教训）

```python
toofast_frames == 0
steer_fault_permanent_frames == 0
jerk_p99 <= 3.6
```

**不是加权项。** 6.0° 那次把 `TooFast 12 帧` 当成「可以接受的成本」，
结果 `steerTempUnavailable`（SOFT_DISABLE + NO_ENTRY）从 0 变成 3 次 —— 方向是反的。
优化器现在把任何 TooFast 帧直接淘汰。

## 一趟跑多组

`card.py` 每 50 秒重新读一次设置页的跟踪参数，**只在横向连续 2 秒未激活时应用**。
切换点由司机决定：过完一个弯、松开 LKAS、再接管，参数就换好了。
每次切换打一条 `lat_tune.segment` 事件，方便在 rlog 里切干净的数据段。

**不动的参数**：接管力矩、rearm 角度、rearm 保持时间。它们决定什么时候把方向盘
交还给人，扫描不该能改它们 —— `test_lateral_tuning_refresh.py` 有专门的测试钉住。

## 趟数

| 方案 | 趟数 |
|---|---|
| 一次一组（旧） | 6–10+ |
| 一次一组 + 贝叶斯 | 8–12 |
| **一趟 6–10 组 + 贝叶斯** | **2–3** |

## 已知限制

1. **横向是闭环，离线预测不了新参数下车的响应。** 录制里只有「实际发出的指令」
   对应的响应。离线只能做**可行性预筛**，不能替代实车。
2. **历史数字复现不了。** AGENTS.md 的「route 51 欠转 32.1%」是当时一次性脚本算的，
   定义没记录。现在模块里给三种定义（`aggregate` / `mean` / `deficit`），
   并明确 `aggregate` 是优化器用的那个。**相对排序能复现**：route 50 比 route 51 差，
   与 AGENTS.md 的 47.1% vs 32.1% 一致。
3. **低速段不可调。** 0–8.4 m/s 的上限锁在 4.0°，那是 EPS 报 TooFast 的区间
   （7° 时 16.33% 的帧故障），优化器碰不到。
4. **`steerActuatorDelay` 仍然测不出来**，还是 0.10。

## 测试

```bash
python -m pytest tools/sp_lat_tune/tests/ opendbc/car/byd/tests/test_lateral_tuning_refresh.py -q
```

`test_metrics.py` 里有一组针对真实录像的集成测试，没有录像时自动跳过。
