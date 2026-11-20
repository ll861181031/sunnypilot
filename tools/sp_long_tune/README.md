# 纵向打分（sp_long_tune）

横向已经有 `tools/sp_lat_tune`；这是它的纵向对应物：**把「纵向到底跟得怎么样」从一次
手工分析变成一条命令**，并且和 `tools/sp_drive_report.py` 的接管归因拼在一起，
构成「哪一段、哪个功能、需要动哪个参数」的完整链条。

## 指标定义（都对应 AGENTS.md Phase 9 手工算过的量）

| 指标 | 定义 | 用途 |
|---|---|---|
| `gain` | `aEgo` 对请求加速度的最小二乘斜率 | Phase 9 实测 0.853（原车自跟踪 0.852），说明车把 0x32E 当自己的指令执行 |
| `corr` | Pearson 相关系数 | Phase 9 实测 0.866；低相关 = 滞后，不是增益 |
| `err_p50/p90` | `\|aEgo − 请求\|` | 整趟偏 vs 单点偏 |
| `jerk_p99` | `\|d(aEgo)/dt\|` P99 | 对照 plan 自身 jerk，不是单一常数 |
| `stop_hold_s` | 静止（<0.3 m/s）且纵向接管的秒数 | Phase 10/11 的核心问题 |
| `resume_count` | 静止→运动 的次数 | 自动起步 |
| `stock_acc_events` | `stockAccStillActive` 次数 | 原车 ACC 抢回 |

## 两条必须遵守的对齐规则（都是横向踩过的坑）

1. **按 `logMonoTime` 对齐，绝不按索引。** `carControl` 和 `carState` 由不同进程发布，
   帧号配对会把不同时刻拼一起，误差直接减半。
2. **只统计运动帧。** 静止时请求是保持减速度（-2.0），`aEgo` 读到 0 —— 把那些帧算进
   跟踪误差会得到 `err_p90 = 2.01`、`gain = -0.55`，把一段完美的保持判成灾难。
   第一版就是这么错的，直到全库扫描出现 25 个「负增益」段才暴露。

## 用法

```bash
# 全库（qlog，10 Hz，3029 段约 2 分钟）
python -m tools.sp_long_tune.cli score ~/.comma/media/0/realdata

# 单条路线 / 单个段
python -m tools.sp_long_tune.cli score ~/.comma/media/0/realdata/00000199--40b22824ea
python -m tools.sp_long_tune.cli score ~/.comma/media/0/realdata/00000199--40b22824ea--2

# 100 Hz（jerk / 延迟问题才需要，慢约 10 倍）
python -m tools.sp_long_tune.cli score ~/.comma/media/0/realdata/000000e9--4979179d0e --full

# 看结果
python -m tools.sp_long_tune.cli report
```

## 为什么没有 `next` / `apply`

纵向目前只有一套配置（`stopAccel=-2.0`、`ki=0`、`actuatorDelay=0.15`），而且闭环——
离线录像里只有「实际发出的指令」对应的响应，扫不出新参数下车会怎么变。
先把这个模块当**诊断**用：`gain` 掉到 0.7 以下、`err_p90` 冲到 1.5 以上的段，
才是有东西可查的段。真要扫参数，得先像横向那样在 `card.py` 里做热切换。

## 测试

```bash
python -m pytest tools/sp_long_tune/tests/ -q
```

其中 `test_a_standstill_hold_is_not_reported_as_a_tracking_error` 就是上面第 2 条规则
的回归测试；把 `MIN_TRACKING_SPEED` 掩码去掉时它会变红。
