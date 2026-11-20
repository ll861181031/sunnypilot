# 车机运维台（harmony_ops）

一个 HarmonyOS NEXT 应用，把这台主机上正在跑的**三个服务**装进一个手机 App。

> **Web 版已于 2026-10-20 上线**：控制台 `:8899` 本身就是同样的五页签车机运维台，
> 手机浏览器直接打开 `http://<当前IP>:8899` 即可，不需要 DevEco、签名或安装。
> 本 App 保留作原生备用；它依赖的 `/api/status`、`/api/start`、`/api/stop`、
> `/api/sp_settings` 接口没有变化。

| 服务 | 默认端口 | 作用 | 在 App 里的形态 |
|---|---|---|---|
| BYD 原车纵向 CAN 抓取 | 8770 | 只读抓 `0x32E` 等原车报文，实时算横向跟随误差、落 CSV | **原生页面** |
| sunnypilot 启停控制台 | 8899 | 启动 / 关闭行车栈 | **原生页面** |
| DeepSeek Harness Web GUI | 3080 | DSH 本体 | **Web 组件嵌入** |

为什么是混合形态：前两个服务的接口就几个 JSON 端点（`/api/status`、`/api/start`…），
原生重写体验更好、还能做「关闭行车栈」这类危险操作的两段式确认；而 DSH 是一个带流式会话
协议的完整单页应用，**不可能**原生重写，只能整体嵌入。

## 目录结构

```
harmony_ops/
├── AppScope/
│   ├── app.json5                     bundleName / 图标 / 应用名
│   └── resources/base/{element,media}  app_name、app_icon.png(1024)
├── build-profile.json5               products: HarmonyOS, compatibleSdkVersion "5.0.0(12)"
├── oh-package.json5                  无三方依赖
├── hvigorfile.ts                     appTasks
├── entry/
│   ├── build-profile.json5           apiType: stageMode
│   ├── hvigorfile.ts                 hapTasks
│   ├── oh-package.json5
│   └── src/main/
│       ├── module.json5              权限、network_security_config、EntryAbility
│       ├── ets/
│       │   ├── entryability/EntryAbility.ets
│       │   ├── pages/Index.ets       唯一页面：五个 Tab
│       │   ├── view/                 ConsoleView / CaptureView / DshView / SpSettingsView / SettingsView
│       │   └── model/                Types / Settings / ApiClient / Discovery
│       └── resources/base/
│           ├── element/{string,color}.json
│           ├── media/startIcon.png
│           └── profile/{main_pages,network_config}.json
└── tools/
    ├── preflight.py                  离线自检（见下）
    └── gen_icons.py                  重新生成图标
```

**只有一个页面**（`pages/Index`），五个 Tab 都在里面。这是刻意的：省掉整套路由
（`main_pages.json` 与页面注册的对应关系）能少一整类「白屏」故障。

## 在 DevEco Studio 里打开并构建

1. 安装 **DevEco Studio 5.0.x**（HarmonyOS NEXT），SDK 装 **API 12**。
2. `File > Open`，选择本目录（`tools/harmony_ops`）。
3. 配置签名：`File > Project Structure > Signing Configs`，勾选
   **Automatically generate signature**（需要登录华为账号；`build-profile.json5` 里
   `signingConfigs` 是空数组，靠 IDE 填）。
4. 等 `Sync` 完成后 `Run`。

## 首次使用

1. 打开 **设置** 标签。
2. 填主机地址，或直接点 **自动发现** —— 它会列出本机 IP、扫描同一 `/24` 网段的 8899 端口，
   并用 `GET /api/info` 里是否含 `bind_host` 来确认「这确实是 sunnypilot 控制台」而不是别的服务。
3. 控制台令牌：主机若设了 `SUNNYPILOT_CONSOLE_TOKEN` 才需要填（请求头 `X-Console-Token`）。
4. DSH 令牌：主机上执行

   ```bash
   grep -a 'token=' ~/.local/state/dsh/dsh-web.log | tail -1
   ```

   把 `?token=` 后面那串填进设置页。**每次重启 `dsh-web` 都会换新 token。**

## 各页面分别能做什么

- **控制台**：每 2 秒轮询 `/api/status`，显示运行状态、PID、启动脚本、日志路径；
  「关闭」需要二次确认（因为行车中停栈是唯一值得多点一下的操作），「启动」不需要。
  下面另有 **「小主机电源」** 一张卡：重启 / 关机，各自也是二次确认。
  - 卡片只在主机**答得上来**的时候才画：先 `GET /api/host/power` 问一次"现在能不能"，
    主机说不能就把原因原样显示（那些句子是主机写的 —— 只有它分得清"车在动"和
    "logind 要密码"），按钮同时置灰；问不到（老版本控制台、网络不通）就整张卡不显示，
    绝不画一个状态不明的电源键。
  - 主机侧还有两道闸门：body 必须带确认字段（`{"confirm": "reboot"}`），
    行车栈运行时还必须能证明车是停着的（接管中、车速 > 0.5 m/s、读不到状态都拒绝）。
  - 按下之后主机**延迟约 5 秒**才动手，所以回复是"已受理"而不是"已重启"：那一刻主机
    还活着。之后本页的轮询会失败，这是预期的，页面按"主机正在重启"处理，不再叠加报错。
- **抓包**：状态卡（来源/栈状态/已采帧/时长/解码错误）、开关（滚动录像、常占 USB）、
  来源切换（`auto` / `cereal` / `panda`）、横向跟随分档表（按车速、按超前角，
  含 p50/p90/p99）、录像文件列表与当前缓冲的 CSV 导出链接（长按复制）。
  轮询是错开的：状态 2 秒、速率 4 秒、文件列表 10 秒 —— `/api/eps_rate` 每次请求都要
  在服务端重算四个信号，没必要跟着 2 秒的节奏打。
- **DSH**：先用自己的 HTTP 探测一次，再交给 Web 组件。**探测能区分出一个关键故障**：
  403 说明 DSH 的 `/api` 白名单拒绝了本机地址（见下），此时页面会直接给出修复命令，
  而不是渲染一个白屏。
- **SP 设置**：把车机自己的设置页镜像过来（见下一节）。**它不是第二份设置**：
  读写走的都是车机同一个 Params 后端，所以两边永远一致。
- **设置**：主机、三个端口、两个令牌、自动发现、当前地址一览。

## SP 设置页：镜像，不是副本

车机上的设置页有 21 组、93 项，其中 84 项可改。App 的这一页把同一份清单渲染出来：
顶部是分组选择条（横向滑动），下面是当前分组，每项按自己的类型渲染成开关、按钮选择器
或加减步进器，并显示车机页面写的说明与单位（例如 `BydSteerRateMax` 显示度数/帧）。

- **清单来自源码，不是硬编码**。主机端 `tools/sp_console/sp_settings.py` 用 AST 解析
  车机 UI 的设置源码，取出控件标题、说明、上下限、单位和参数键，再读
  `params_keys.h` 校验键名。**车机加一个设置，App 不用改一行代码。**
- **写入必须停栈**。多数参数在行车栈启动时读取，所以栈在跑时接口直接返回 409，
  App 显示「行车栈运行中，写入被拒绝」并禁用所有控件。**这比车机自己的门槛更严**：
  车机的设置页在行驶中同样不让改。
- **重置 = 写 `null`**，对应车机的恢复默认（后端 `params.remove`）；
  仍处于默认值的项会显示一个「默认」标记。
- **选择器存的是按钮下标**。像 `MadsSteeringMode`，值是下标而不是字面量，
  点哪个按钮就写哪个下标；当前 8 个选择器都是这个约定。

写这一页时在车机上发现并修掉了一个真实缺陷：BYD 面板的 `AutoLaneChangeTimer`
快捷项原本用按钮行，而这个参数的值域是 **-1（关闭）.. 5（3 秒）**，按钮行存的却是
自己按钮的下标 —— **每个标签都比它实际写入的值错开一格**（点「0.5 s」写进去的是 1，
而 1 是「Nudgeless」），存值 5 时也没有第六个按钮可以高亮。现在它和车机原来的 MADS
页面一样用选项行，两个页面逐值一致（`test_byd_shortcuts.py` 钉住）。

已知限制：少数项的标签由 Python 回调生成（上面这个键：`-1` 显示 `Off`、`2` 显示 `0.5 s`）。
解析器**不去执行回调**——猜函数体正是镜像开始编造标签的方式。这类项只带值域进入 App，
渲染成步进器并显示原始数值：宁可显示 `5`，也不要显示一个车机上并不存在的名字。

## ⚠️ 未验证项（务必先读）

**本工程没有经过编译验证。** 这台主机上没有 DevEco Studio、hvigor、ohpm、Java 和
HarmonyOS SDK，所以无法产出 `.hap`，也无法做真机签名。已经做的只有离线静态自检：

```bash
/data/sunnypilot/.venv/bin/python tools/harmony_ops/tools/preflight.py
```

它检查：所有 `.json5` 能否解析、每个 `$r()`/`$string:`/`$media:` 引用是否有对应资源、
`main_pages.json` 与页面文件是否互相一致、ability 的 `srcEntry` 与每个相对 import
是否解析得到、**每个 import 的名字是否真的被目标文件导出**（拼错的导入名只有
ArkTS 编译时才报，正是这里做不到的事）、
`mainElement` 是否指向存在的 ability、每个 PNG 是否合法、每个 `.ets` 括号是否平衡且
没用 ArkTS 明令禁止的写法（`any` / `eval` / `require` / `Object.assign`）。
**它不能证明语义、API 签名、hvigor 构建图、签名和 UI 渲染。**

写代码时已经主动绕开了两类已知会被严格模式拒绝的写法，所以下列问题**不会**出现：

- `@State` 不再使用 `T | null` 联合类型（ArkUI 文档只列了固定几种支持类型），
  改为在 `model/Types.ets` 里提供返回空值的工厂函数 + 独立的 `loaded` 布尔标志。
- 不存在「把非空类型拿去和 `null`/`undefined` 比较」的地方（严格模式下是
  no-overlap 比较错误）。`Settings.store` 是真正的联合类型；`stat()` / `str()` 的
  参数已显式放宽；`last_saved` 是接口字段。

打开工程后如果报错，优先看这几处（按我估计的风险从高到低）：

1. **`MixedMode.All`**（`DshView.ets`）：按官方示例它来自 ArkUI 全局声明，没有 import。
   若报未定义，改成 `import { MixedMode } from '@kit.ArkWeb';`。
2. **`network_config.json` 的结构**（`base-config.cleartextTrafficPermitted`）：
   这是为了放行局域网明文 HTTP。若 HarmonyOS 校验不过，按其要求调整键名——
   文件路径 `resources/base/profile/network_config.json` 与 `module.json5` 里的
   `metadata` 名 `ohos.net.network_security_config` 是对的。
3. **`compatibleSdkVersion: "5.0.0(12)"`**：随 DevEco 版本可能要写成 `12` 或 `"5.0.5(17)"` 之类。
4. **ArkWeb 加载明文 HTTP**：还有 `ERR_ACCESS_DENIED -10` 一类的额外限制，本工程未实测。
5. **`@ohos/hvigor-ohos-plugin` 版本**：`hvigorfile.ts` 只引用了 `appTasks` / `hapTasks`，
   没有锁版本；DevEco 首次 Sync 时若提示插件缺失，按 IDE 建议安装即可。

## 排障

**DSH 页面能加载、但所有接口 403 / 白屏**
根因已查清（2026-10-14）：`dsh web` 只在 Web runtime **激活那一刻**用
`networkInterfaces()` 采一次局域网地址，`/api` 的浏览器信任白名单就是这份快照，之后不变。
这台机开机 2 秒后就拉起了 `dsh-web`，**那时 Wi-Fi 还没拿到地址**，于是快照是空的，
只剩 patch 层里写死的一个旧网段。实测证据：白名单拒绝 403、信任的返回 401，当时被信任的
集合恰好是 `{127.0.0.1, 192.168.43.13}`，而本机真实地址是 `192.168.123.70`（还被 clash 的
`198.18.0.1` 干扰）。

已修的（在主机侧，不在本 App 内）：
- 新增 `~/.local/bin/dsh-web-wait-for-lan`，作为 `dsh-web.service` 的 `ExecStartPre`：
  等主机真的拿到非回环 IPv4 再启动，快照就采得到，换任何网络都不用改配置。
- `~/.dsh/profiles/web/cordis.patch.yml` 的 `trustedHosts` 字面量从旧网段改成当前地址。
- 新增 `~/.local/bin/dsh-lan-trust`：只读诊断，直接告诉你哪些 authority 被信任。

**换了 Wi-Fi / 热点之后 DSH 又 403**：快照是启动时采的，所以

```bash
systemctl --user restart dsh-web
dsh-lan-trust          # 确认新地址已被信任
```

然后再把新 token 填进 App 设置页。App 的 DSH 页会直接提示这一步。

**控制台 401**：主机设了 `SUNNYPILOT_CONSOLE_TOKEN`，把同一个值填进设置页。

**抓包页连不上，但网页能开**：注意抓包工具在主机栈没跑时 `source=can` 会报
「can 主题存在但无活跃写入者”，这是正常状态提示，不是 App 的问题。

## 辅助工具

```bash
# 离线自检（应在任何改动后重跑）
/data/sunnypilot/.venv/bin/python tools/harmony_ops/tools/preflight.py

# 重新生成图标（改配色/形状后）
/data/sunnypilot/.venv/bin/python tools/harmony_ops/tools/gen_icons.py
```

图标是脚本生成的，仓库里只存 PNG。原因：这是工程里仅有的二进制资产，
用脚本生成才能让它在以文本为主的仓库里可复现、可 review。
