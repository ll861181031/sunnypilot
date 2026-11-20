import { ApiClient, ApiResult } from '../model/ApiClient';
import {
  ConsoleInfo, ConsoleStatus, ErrorResponse, HostPowerBody, HostPowerResult, HostPowerStatus,
  emptyConsoleInfo, emptyConsoleStatus, emptyHostPowerStatus
} from '../model/Types';
import { Settings } from '../model/Settings';

function twoDigits(value: number): string {
  return value < 10 ? `0${value}` : `${value}`;
}

function clockText(): string {
  const now = new Date();
  return `${twoDigits(now.getHours())}:${twoDigits(now.getMinutes())}:${twoDigits(now.getSeconds())}`;
}

/**
 * Start and stop the sunnypilot stack, over the console's HTTP API
 * (/data/sunnypilot/tools/sp_console/server.py).
 *
 * Polling runs at 2 s because that is what the console's own page uses: fast
 * enough to watch a start complete, cheap enough to leave open in the car.
 *
 * Stopping asks for confirmation, starting does not. Starting is the normal act;
 * killing the stack while the car is moving is the one mistake worth a second tap.
 */
@Component
export struct ConsoleView {
  @StorageLink('host') host: string = '';
  @StorageLink('consolePort') consolePort: number = 8899;
  @StorageLink('consoleToken') consoleToken: string = '';
  @State status: ConsoleStatus = emptyConsoleStatus();
  @State statusLoaded: boolean = false;
  @State info: ConsoleInfo = emptyConsoleInfo();
  @State infoLoaded: boolean = false;
  @State error: string = '';
  @State notice: string = '';
  @State updatedAt: string = '';
  @State confirmStop: boolean = false;
  @State busy: boolean = false;
  @State power: HostPowerStatus = emptyHostPowerStatus();
  @State powerLoaded: boolean = false;
  /** Which of the two power actions is waiting for its second tap; '' for none. */
  @State confirmPower: string = '';
  /** Set once the host has accepted a reboot/shutdown: further poll failures are expected. */
  @State powerPending: string = '';
  private timerId: number = -1;

  aboutToAppear(): void {
    this.refresh();
    this.refreshPower();
    this.timerId = setInterval(() => {
      this.refresh();
    }, 2000);
  }

  aboutToDisappear(): void {
    if (this.timerId !== -1) {
      clearInterval(this.timerId);
      this.timerId = -1;
    }
  }

  /**
   * server.py reads the shared secret from the X-Console-Token header and compares
   * it with hmac.compare_digest. Absent token means the console is open.
   */
  private authHeaders(): Record<string, string> | undefined {
    if (this.consoleToken.length === 0) {
      return undefined;
    }
    const headers: Record<string, string> = {};
    headers['X-Console-Token'] = this.consoleToken;
    return headers;
  }

  private async refresh(): Promise<void> {
    if (this.host.length === 0) {
      this.statusLoaded = false;
      this.error = '';
      return;
    }
    const base = Settings.consoleBase();

    const infoResult: ApiResult = await ApiClient.get(`${base}/api/info`, undefined, 4000);
    if (infoResult.ok) {
      try {
        this.info = JSON.parse(infoResult.body) as ConsoleInfo;
        this.infoLoaded = true;
      } catch (err) {
        this.infoLoaded = false;
      }
    }

    const result: ApiResult = await ApiClient.get(`${base}/api/status`, this.authHeaders(), 4000);
    if (result.ok) {
      try {
        this.status = JSON.parse(result.body) as ConsoleStatus;
        this.statusLoaded = true;
        this.error = '';
        this.updatedAt = clockText();
        if (this.powerPending.length > 0) {
          // The host answered again, so the reboot/shutdown notice has served its
          // purpose; ask the power questions afresh instead of leaving a stale
          // "已受理" on a host that is clearly back.
          this.powerPending = '';
          this.notice = '主机已恢复响应。';
          this.refreshPower();
        }
      } catch (err) {
        this.statusLoaded = false;
        this.error = '/api/status 返回的不是合法 JSON';
      }
      return;
    }
    if (this.powerPending.length > 0) {
      // The host is already on its way down, so a failing poll is the expected
      // answer - not a second fault to stack on top of the notice the user got.
      return;
    }
    if (result.code === 401) {
      this.error = '这个控制台需要访问令牌，请在「设置」里填写。';
    } else if (result.code === 0) {
      this.error = `连不上控制台（${result.error}）`;
    } else {
      this.error = `控制台返回 HTTP ${result.code}`;
    }
  }

  private async act(path: string, label: string): Promise<void> {
    if (this.busy) {
      return;
    }
    this.busy = true;
    this.notice = `${label}中…`;
    const result: ApiResult = await ApiClient.action(
      `${Settings.consoleBase()}${path}`, this.authHeaders());
    this.busy = false;
    this.confirmStop = false;

    if (result.ok) {
      this.notice = `${label}已受理`;
      this.error = '';
    } else if (result.code === 409) {
      // The console serialises start/stop behind one lock and answers 409.
      this.notice = '';
      this.error = '主机上已有另一个启停操作在进行，请稍候。';
    } else if (result.code === 401) {
      this.notice = '';
      this.error = '令牌不对，操作被拒绝。';
    } else if (result.code === 0) {
      this.notice = '';
      this.error = `${label}失败：${result.error}`;
    } else {
      this.notice = '';
      this.error = `${label}失败：HTTP ${result.code}`;
    }
    await this.refresh();
  }

  /**
   * Whether the host will reboot or power itself off right now, and why not.
   *
   * Asked on demand rather than on the 2 s poll: the answer costs the host a couple
   * of D-Bus round trips, and it only changes when the stack or the car does.
   */
  private async refreshPower(): Promise<void> {
    if (this.host.length === 0) {
      this.powerLoaded = false;
      return;
    }
    const result: ApiResult = await ApiClient.get(
      `${Settings.consoleBase()}/api/host/power`, this.authHeaders(), 6000);
    if (!result.ok) {
      // Hidden rather than guessed: an unreachable host, or an older console with
      // no such endpoint, cannot tell us whether the action is allowed, and a
      // power button whose state is unknown must not be drawn.
      this.powerLoaded = false;
      return;
    }
    try {
      this.power = JSON.parse(result.body) as HostPowerStatus;
      this.powerLoaded = true;
    } catch (err) {
      this.powerLoaded = false;
    }
  }

  /**
   * Reboot or shut the host down.
   *
   * The console schedules the action a few seconds out so its own reply arrives
   * first, which is why this outcome reads "已受理" rather than "已重启": at the
   * moment of the tap the host is still up. The confirmation word is sent in the
   * body because the console refuses a bare request - the same reason the button
   * asks twice.
   */
  private async powerAct(action: string): Promise<void> {
    if (this.busy) {
      return;
    }
    this.busy = true;
    const label = action === 'reboot' ? '重启' : '关机';
    this.notice = `${label}指令发送中…`;
    this.error = '';
    const body: HostPowerBody = { confirm: action };
    const headers: Record<string, string> = {};
    headers['Content-Type'] = 'application/json';
    if (this.consoleToken.length > 0) {
      headers['X-Console-Token'] = this.consoleToken;
    }
    const result: ApiResult = await ApiClient.postJson(
      `${Settings.consoleBase()}/api/host/${action}`, headers, JSON.stringify(body), 15000);
    this.busy = false;
    this.confirmPower = '';

    if (result.ok) {
      // The reply arrives before the host goes down, so a parse failure here still
      // means "accepted" - the delay is only used for the wording.
      let seconds = 5;
      try {
        const accepted = JSON.parse(result.body) as HostPowerResult;
        seconds = accepted.execute_in;
      } catch (err) {
        seconds = 5;
      }
      this.powerPending = `${label}已受理：主机将在 ${seconds} 秒后${label}，届时本页会断开。`;
      this.notice = this.powerPending;
      return;
    }
    if (result.code === 409) {
      this.error = this.errorFrom(result.body, `主机现在不允许${label}。`);
    } else if (result.code === 503) {
      this.error = this.errorFrom(result.body, '主机没有权限执行电源操作。');
    } else if (result.code === 400) {
      this.error = this.errorFrom(result.body, '请求被拒绝（确认字段不符）。');
    } else if (result.code === 401) {
      this.error = '令牌不对，操作被拒绝。';
    } else if (result.code === 0) {
      this.error = `${label}失败：${result.error}`;
    } else {
      this.error = `${label}失败：HTTP ${result.code}`;
    }
    this.notice = '';
    await this.refreshPower();
  }

  /** The host writes these sentences itself; it knows why it said no. */
  private errorFrom(body: string, fallback: string): string {
    try {
      const parsed = JSON.parse(body) as ErrorResponse;
      return parsed.error.length > 0 ? parsed.error : fallback;
    } catch (err) {
      return fallback;
    }
  }

  private stateText(): string {
    if (!this.statusLoaded) {
      return '未知';
    }
    if (this.status.starting) {
      return '启动中';
    }
    return this.status.running ? '运行中' : '已停止';
  }

  private stateColor(): string {
    if (!this.statusLoaded) {
      return '#7A8699';
    }
    if (this.status.starting) {
      return '#FBBF24';
    }
    return this.status.running ? '#2DD4BF' : '#94A3B8';
  }

  private isRunning(): boolean {
    return this.statusLoaded && this.status.running;
  }

  private pidsText(): string {
    if (!this.statusLoaded || this.status.pids.length === 0) {
      return '—';
    }
    return this.status.pids.join(', ');
  }

  private launcherText(): string {
    return !this.statusLoaded ? '—' : this.status.launcher;
  }

  private logText(): string {
    return !this.statusLoaded ? '—' : this.status.log;
  }

  private infoPortText(): string {
    return !this.infoLoaded ? '—' : `${this.info.port}`;
  }

  private infoTokenText(): string {
    return !this.infoLoaded ? '—' : (this.info.token_required ? '需要令牌' : '不需要令牌');
  }

  private infoMdnsText(): string {
    return !this.infoLoaded ? '—' : this.info.mdns_name;
  }

  private infoLanText(): string {
    if (!this.infoLoaded || this.info.lan_addresses.length === 0) {
      return '—';
    }
    return this.info.lan_addresses.join(', ');
  }

  build() {
    Scroll() {
      Column({ space: 12 }) {
        if (this.host.length === 0) {
          this.banner('先在「设置」里填写主机地址，或用「自动发现」。', '#FBBF24', '#1A1408')
        } else {
          this.statusCard()
          this.actionCard()
          if (this.powerLoaded) {
            this.powerCard()
          }
          if (this.error.length > 0) {
            this.banner(this.error, '#F87171', '#2A1416')
          }
          if (this.notice.length > 0) {
            this.banner(this.notice, '#2DD4BF', '#0F1F1D')
          }
          if (this.infoLoaded) {
            this.infoCard()
          }
        }
      }
      .width('100%')
      .padding(12)
    }
    .width('100%')
    .height('100%')
    .backgroundColor('#0B0F14')
    .scrollBar(BarState.Auto)
  }

  @Builder
  statusCard() {
    Column({ space: 10 }) {
      Row() {
        Text('sunnypilot 引擎')
          .fontSize(15)
          .fontColor('#E6EAF2')
          .fontWeight(FontWeight.Medium)
        Blank()
        Text(this.stateText())
          .fontSize(13)
          .fontColor(this.stateColor())
          .padding({ left: 10, right: 10, top: 3, bottom: 3 })
          .backgroundColor('#151B24')
          .borderRadius(10)
      }
      .width('100%')

      this.kv('主机', `${this.host}:${this.consolePort}`)
      this.kv('进程 PID', this.pidsText())
      this.kv('启动脚本', this.launcherText())
      this.kv('日志', this.logText())
      this.kv('刷新于', this.updatedAt.length > 0 ? this.updatedAt : '—')
    }
    .width('100%')
    .padding(14)
    .backgroundColor('#0F141B')
    .borderRadius(12)
  }

  @Builder
  actionCard() {
    Column({ space: 10 }) {
      if (this.confirmStop) {
        Text('关闭会把行车栈整体停掉。确认吗？')
          .fontSize(13)
          .fontColor('#FBBF24')
          .width('100%')
        Row({ space: 10 }) {
          Button('取消')
            .layoutWeight(1)
            .height(44)
            .backgroundColor('#1E2634')
            .fontColor('#E6EAF2')
            .onClick(() => {
              this.confirmStop = false;
            })
          Button('确认关闭')
            .layoutWeight(1)
            .height(44)
            .backgroundColor('#7F1D1D')
            .fontColor('#FEE2E2')
            .enabled(!this.busy)
            .onClick(() => {
              this.act('/api/stop', '关闭');
            })
        }
        .width('100%')
      } else {
        Row({ space: 10 }) {
          Button(this.isRunning() ? '停止' : '启动')
            .layoutWeight(1)
            .height(44)
            .backgroundColor(this.isRunning() ? '#7F1D1D' : '#2DD4BF')
            .fontColor(this.isRunning() ? '#FEE2E2' : '#06251F')
            .enabled(!this.busy)
            .onClick(() => {
              if (this.isRunning()) {
                this.confirmStop = true;
              } else {
                this.act('/api/start', '启动');
              }
            })
          Button('刷新')
            .width(88)
            .height(44)
            .backgroundColor('#1E2634')
            .fontColor('#E6EAF2')
            .onClick(() => {
              this.refresh();
              this.refreshPower();
            })
        }
        .width('100%')
      }
    }
    .width('100%')
    .padding(14)
    .backgroundColor('#0F141B')
    .borderRadius(12)
  }

  /**
   * Reboot and power off, two taps each.
   *
   * These are the only controls in the app that can make the host unreachable, so
   * they are the only ones that ask twice even though stopping the stack does not:
   * a mis-tap here cannot be undone from the phone that made it. The card is only
   * drawn once the host has answered what is allowed right now - buttons that
   * cannot work are not shown at all, and a refusal is printed in the host's own
   * words, because only the host can tell "the car is moving" from "logind wants a
   * password".
   */
  @Builder
  powerCard() {
    Column({ space: 10 }) {
      Row() {
        Text('小主机电源')
          .fontSize(15)
          .fontColor('#E6EAF2')
          .fontWeight(FontWeight.Medium)
        Blank()
        Text(this.power.stack_running ? '行车栈在运行' : '行车栈已停止')
          .fontSize(11)
          .fontColor(this.power.stack_running ? '#FBBF24' : '#7A8699')
      }
      .width('100%')

      if (this.confirmPower.length > 0) {
        Text(this.confirmPower === 'reboot'
          ? '主机会重启，行车栈随之停止，连接断开约半分钟。确认吗？'
          : '主机关机后断电，手机就再也连不上，只能到车上重新开机。确认吗？')
          .fontSize(13)
          .fontColor('#F87171')
          .width('100%')
        Row({ space: 10 }) {
          Button('取消')
            .layoutWeight(1)
            .height(44)
            .backgroundColor('#1E2634')
            .fontColor('#E6EAF2')
            .onClick(() => {
              this.confirmPower = '';
            })
          Button(this.confirmPower === 'reboot' ? '确认重启' : '确认关机')
            .layoutWeight(1)
            .height(44)
            .backgroundColor('#7F1D1D')
            .fontColor('#FEE2E2')
            .enabled(!this.busy)
            .onClick(() => {
              this.powerAct(this.confirmPower);
            })
        }
        .width('100%')
      } else {
        Row({ space: 10 }) {
          Button('重启主机')
            .layoutWeight(1)
            .height(44)
            .backgroundColor('#1E2634')
            .fontColor(this.power.can_reboot ? '#E6EAF2' : '#4B5563')
            .enabled(this.power.can_reboot && !this.busy && this.powerPending.length === 0)
            .onClick(() => {
              this.confirmPower = 'reboot';
            })
          Button('关闭主机')
            .layoutWeight(1)
            .height(44)
            .backgroundColor('#7F1D1D')
            .fontColor('#FEE2E2')
            .enabled(this.power.can_poweroff && !this.busy && this.powerPending.length === 0)
            .onClick(() => {
              this.confirmPower = 'poweroff';
            })
        }
        .width('100%')

        if (!this.power.can_reboot && this.power.reason_reboot.length > 0) {
          Text(this.power.reason_reboot)
            .fontSize(11)
            .fontColor('#7A8699')
            .width('100%')
        }
        if (!this.power.can_poweroff && this.power.reason_poweroff.length > 0) {
          Text(this.power.reason_poweroff)
            .fontSize(11)
            .fontColor('#7A8699')
            .width('100%')
        }
        Text('动作延迟几秒执行，授权途径与命令行记在主机 ~/.local/share/sp-console/host-power.log。')
          .fontSize(11)
          .fontColor('#7A8699')
          .width('100%')
      }
    }
    .width('100%')
    .padding(14)
    .backgroundColor('#0F141B')
    .borderRadius(12)
  }

  @Builder
  infoCard() {
    Column({ space: 10 }) {
      Text('控制台信息')
        .fontSize(15)
        .fontColor('#E6EAF2')
        .fontWeight(FontWeight.Medium)
        .width('100%')
      this.kv('监听端口', this.infoPortText())
      this.kv('访问令牌', this.infoTokenText())
      this.kv('mDNS 名', this.infoMdnsText())
      this.kv('局域网地址', this.infoLanText())
    }
    .width('100%')
    .padding(14)
    .backgroundColor('#0F141B')
    .borderRadius(12)
  }

  @Builder
  kv(label: string, value: string) {
    Row() {
      Text(label)
        .fontSize(13)
        .fontColor('#7A8699')
        .width(80)
      Text(value)
        .fontSize(13)
        .fontColor('#C7D0DE')
        .layoutWeight(1)
        .maxLines(3)
        .textOverflow({ overflow: TextOverflow.Ellipsis })
    }
    .width('100%')
    .alignItems(VerticalAlign.Top)
  }

  @Builder
  banner(text: string, colour: string, background: string) {
    Text(text)
      .fontSize(13)
      .fontColor(colour)
      .width('100%')
      .padding(12)
      .backgroundColor(background)
      .borderRadius(10)
  }
}
