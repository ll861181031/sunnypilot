# CP搭子 · HarmonyOS（navipilot 移植）

把 `https://github.com/jixiexiaoge/navipilot`（CP搭子）的车端链路层移植到 HarmonyOS NEXT /
OpenHarmony（ArkTS + ArkUI，Stage 模型）。这是一个**独立工程**，不属于「车机运维台」。

## 现在能做什么

| 通道 | 端口 | 状态 |
| --- | --- | --- |
| 设备发现（UDP 广播 / 主动扫段） | 7705 + 7000 | 已实现：广播解析 + `/24` 并发探测 |
| 握手 / 心跳 / 命令 | 7706 | 已实现：1 s 心跳、SPEED 命令按钮、`carrotIndex` 计数 |
| HTTP 探活 / 参数 | 7000 | 已实现：`/api/heartbeat_status`、`/api/params_bulk` |
| 遥测 | 7000 `/ws/compact_state` | 已实现：JSON 帧计数 + 最后一条原文 |
| 导航 v2 | 7714 | 已实现：`requirements_query` → `subscription_manifest`（28 条目 / 13 JSON 流）→ `manifest_applied` → 13 条流连接；`item_update` 探针 |
| 信号灯 / 停止线 | 7711 | 已实现：4 字节大端长度 + UTF-8 JSON，20 Hz 解析（灯色 1 红 / 2 绿 / 3 黄） |

## 鸿蒙上做不到的两块（平台限制，不是没写）

1. **高德车机版虚拟屏 / 投屏**：依赖 Android 运行时（VirtualDisplay + 跨进程 Surface），
   HarmonyOS NEXT 没有 Android 兼容层，无法承载。
2. **HTTP 5001 的 WebRTC 视频流**：HarmonyOS NEXT 无 WebRTC 原生 SDK，
   `ohos.webrtc` 不在公开 SDK 中，无法直接收流。

其余链路（发现、握手、心跳、命令、遥测、导航 manifest 与数据流、灯色）全部可用。

## 目录

```
AppScope/                     应用级配置（bundleName com.lsr.cplink，名称 CP搭子）
entry/                        唯一入口模块
  src/main/ets/cplinkability/ UIAbility：设置 AppStorage 后加载 pages/CPlinkPage
  src/main/ets/pages/         入口页（标题栏 + CPlinkView）
  src/main/ets/view/          UI：状态管理 V2（@ComponentV2 / @Local / @Param）
  src/main/ets/model/         协议常量与 CPlinkClient（socket / http / webSocket）
```

## 打包安装

当前仓库里没有签名配置，构建产物是 `entry-default-unsigned.hap`（未签名，手机不认）。
拿到自己的签名只要三步：

1. DevEco Studio 打开本目录（`D:\Desktop\HarmonyOS\cplink_standalone`）。
2. **File → Project Structure → Signing Configs → 勾选 Automatically generate signature**，
   用你的华为账号登录一次，DevEco 会生成 bundle 为 `com.lsr.cplink` 的调试证书与 profile。
3. 点 Run（或 `hvigorw --mode module -p product=default -p module=entry@default assembleHap`）。

装好后桌面会出现独立图标「CP搭子」，与「车机运维台」互不相干。

> 为什么不在现有的 `com.lsr.harmonyops` 里出第二个图标：鸿蒙桌面是**一个 bundle 一个图标**
> （图标取 entry HAP 的 mainElement），而且 `.p7b` 调试 profile 里写死了
> `bundle-name = com.lsr.harmonyops`。所以独立图标必须有独立 bundle + 独立 profile。

## 用法

1. 手机与车机（或跑着 `cplink_shim` 的机器）同一网段。
2. 输入车端 IP → 点「连接」；车端不广播时用「扫描 /24」按 7000 端口找。
3. 「链路」看握手/心跳/HTTP 探活，「遥测」看紧凑状态帧，「导航通道」看 manifest 与 13 条流，
   「信号灯」看灯色与停止线距离，「命令」发 SPEED。
4. 日志卡片按时间倒序列出每一步的失败原因（连接被拒、超时、协议错误都会写清楚）。
