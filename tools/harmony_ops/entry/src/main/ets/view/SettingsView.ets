import { Settings } from '../model/Settings';
import { Discovery } from '../model/Discovery';

/**
 * Where the rig is, and the two shared secrets.
 *
 * Drafts exist so a half-typed address never overwrites the stored one; @Watch
 * pulls them back in sync whenever the stored value changes elsewhere, which is
 * what the auto-discovery button and the DSH tab's token field do.
 */
@Component
export struct SettingsView {
  @StorageLink('host') @Watch('syncDrafts') host: string = '';
  @StorageLink('consolePort') @Watch('syncDrafts') consolePort: number = 8899;
  @StorageLink('capturePort') @Watch('syncDrafts') capturePort: number = 8770;
  @StorageLink('dshPort') @Watch('syncDrafts') dshPort: number = 3080;
  @StorageLink('consoleToken') @Watch('syncDrafts') consoleToken: string = '';
  @StorageLink('dshToken') @Watch('syncDrafts') dshToken: string = '';

  @State hostDraft: string = '';
  @State consolePortDraft: string = '';
  @State capturePortDraft: string = '';
  @State dshPortDraft: string = '';
  @State consoleTokenDraft: string = '';
  @State dshTokenDraft: string = '';
  @State localIp: string = '';
  @State scanning: boolean = false;
  @State message: string = '';

  aboutToAppear(): void {
    this.syncDrafts();
    Discovery.localIpv4().then((ip: string) => {
      this.localIp = ip;
    });
  }

  syncDrafts(): void {
    this.hostDraft = this.host;
    this.consolePortDraft = `${this.consolePort}`;
    this.capturePortDraft = `${this.capturePort}`;
    this.dshPortDraft = `${this.dshPort}`;
    this.consoleTokenDraft = this.consoleToken;
    this.dshTokenDraft = this.dshToken;
  }

  /**
   * Parse digits by hand rather than with parseInt/Number: it keeps the app free
   * of a standard-library dependency that ArkTS is fussy about, and it cannot
   * return NaN into a port that is later spliced into a URL.
   */
  private parsePort(text: string, fallback: number): number {
    const trimmed = text.trim();
    if (trimmed.length === 0 || trimmed.length > 5) {
      return fallback;
    }
    let value = 0;
    for (let i = 0; i < trimmed.length; i++) {
      const code = trimmed.charCodeAt(i);
      if (code < 48 || code > 57) {
        return fallback;
      }
      value = value * 10 + (code - 48);
    }
    return value < 1 || value > 65535 ? fallback : value;
  }

  private save(): void {
    Settings.setString('host', Settings.normaliseHost(this.hostDraft));
    Settings.setNumber('consolePort', this.parsePort(this.consolePortDraft, Settings.DEFAULT_CONSOLE_PORT));
    Settings.setNumber('capturePort', this.parsePort(this.capturePortDraft, Settings.DEFAULT_CAPTURE_PORT));
    Settings.setNumber('dshPort', this.parsePort(this.dshPortDraft, Settings.DEFAULT_DSH_PORT));
    Settings.setString('consoleToken', this.consoleTokenDraft.trim());
    Settings.setString('dshToken', this.dshTokenDraft.trim());
    this.syncDrafts();
    this.message = '已保存。';
  }

  private async discover(): Promise<void> {
    this.scanning = true;
    this.message = '正在扫描本网段上的 sunnypilot 控制台…';
    const configured = Settings.normaliseHost(this.hostDraft);
    const port = this.parsePort(this.consolePortDraft, Settings.DEFAULT_CONSOLE_PORT);
    const found = await Discovery.findConsole(configured, port);
    this.scanning = false;
    if (found.length === 0) {
      this.message = '没找到控制台。确认手机和目标机在同一个网络，或手动填写地址。';
      return;
    }
    Settings.setString('host', found);
    this.hostDraft = found;
    this.message = `已找到并保存：${found}`;
  }

  build() {
    Scroll() {
      Column({ space: 12 }) {
        Column({ space: 10 }) {
          Text('主机')
            .fontSize(15)
            .fontColor('#E6EAF2')
            .fontWeight(FontWeight.Medium)
            .width('100%')
          TextInput({ text: this.hostDraft, placeholder: '192.168.123.70 或 openpilot.local' })
            .height(44)
            .fontSize(14)
            .backgroundColor('#151B24')
            .fontColor('#E6EAF2')
            .onChange((value: string) => {
              this.hostDraft = value;
            })
          Row({ space: 10 }) {
            Button(this.scanning ? '扫描中…' : '自动发现')
              .layoutWeight(1)
              .height(42)
              .backgroundColor('#1E2634')
              .fontColor('#E6EAF2')
              .enabled(!this.scanning)
              .onClick(() => {
                this.discover();
              })
            Button('保存')
              .layoutWeight(1)
              .height(42)
              .backgroundColor('#2DD4BF')
              .fontColor('#06251F')
              .onClick(() => {
                this.save();
              })
          }
          .width('100%')
          Text(this.localIp.length > 0 ? `本机地址：${this.localIp}` : '本机地址：未知（未联网？）')
            .fontSize(12)
            .fontColor('#7A8699')
            .width('100%')
        }
        .width('100%')
        .padding(14)
        .backgroundColor('#0F141B')
        .borderRadius(12)

        Column({ space: 10 }) {
          Text('端口')
            .fontSize(15)
            .fontColor('#E6EAF2')
            .fontWeight(FontWeight.Medium)
            .width('100%')
          this.portRow('SP 控制台', this.consolePortDraft, 8899)
          this.portRow('抓包工具', this.capturePortDraft, 8770)
          this.portRow('DSH', this.dshPortDraft, 3080)
        }
        .width('100%')
        .padding(14)
        .backgroundColor('#0F141B')
        .borderRadius(12)

        Column({ space: 10 }) {
          Text('令牌')
            .fontSize(15)
            .fontColor('#E6EAF2')
            .fontWeight(FontWeight.Medium)
            .width('100%')
          Text('SP 控制台令牌（X-Console-Token；主机没设 SUNNYPILOT_CONSOLE_TOKEN 就留空）')
            .fontSize(12)
            .fontColor('#7A8699')
            .width('100%')
          TextInput({ text: this.consoleTokenDraft, placeholder: '留空表示控制台不需要令牌' })
            .height(42)
            .fontSize(13)
            .backgroundColor('#151B24')
            .fontColor('#E6EAF2')
            .onChange((value: string) => {
              this.consoleTokenDraft = value;
            })
          Text('DSH 令牌（dsh web 启动时打印的 ?token=）')
            .fontSize(12)
            .fontColor('#7A8699')
            .width('100%')
          TextInput({ text: this.dshTokenDraft, placeholder: '例如 6Fse3YqGC0Qkux0gixPLUZFLpgD2eXm2Xt-GPYfpZPc' })
            .height(42)
            .fontSize(13)
            .backgroundColor('#151B24')
            .fontColor('#E6EAF2')
            .onChange((value: string) => {
              this.dshTokenDraft = value;
            })
        }
        .width('100%')
        .padding(14)
        .backgroundColor('#0F141B')
        .borderRadius(12)

        if (this.message.length > 0) {
          Text(this.message)
            .fontSize(13)
            .fontColor('#2DD4BF')
            .width('100%')
            .padding(12)
            .backgroundColor('#0F1F1D')
            .borderRadius(10)
        }

        Column({ space: 8 }) {
          Text('当前地址')
            .fontSize(15)
            .fontColor('#E6EAF2')
            .fontWeight(FontWeight.Medium)
            .width('100%')
          this.urlRow('抓包工具', `http://${this.host}:${this.capturePort}/`)
          this.urlRow('SP 控制台', `http://${this.host}:${this.consolePort}/`)
          this.urlRow('DSH', `http://${this.host}:${this.dshPort}/`)
        }
        .width('100%')
        .padding(14)
        .backgroundColor('#0F141B')
        .borderRadius(12)

        Column({ space: 6 }) {
          Text('注意')
            .fontSize(13)
            .fontColor('#FBBF24')
            .fontWeight(FontWeight.Medium)
            .width('100%')
          Text('DSH 的 /api 白名单只在进程启动时采样一次局域网地址。'
            + '换了 Wi-Fi 或热点之后，DSH 页面会加载但接口全部 403 —— '
            + '在主机上执行 systemctl --user restart dsh-web 即可修复，'
            + '然后用它新打印的 token。')
            .fontSize(12)
            .fontColor('#A9B4C4')
            .width('100%')
        }
        .width('100%')
        .padding(14)
        .backgroundColor('#1A1408')
        .borderRadius(12)
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
  portRow(label: string, draft: string, fallback: number) {
    Row({ space: 10 }) {
      Text(label)
        .fontSize(13)
        .fontColor('#C7D0DE')
        .width(88)
      TextInput({ text: draft, placeholder: `${fallback}` })
        .layoutWeight(1)
        .height(40)
        .fontSize(13)
        .type(InputType.Number)
        .backgroundColor('#151B24')
        .fontColor('#E6EAF2')
        .onChange((value: string) => {
          if (label === 'SP 控制台') {
            this.consolePortDraft = value;
          } else if (label === '抓包工具') {
            this.capturePortDraft = value;
          } else {
            this.dshPortDraft = value;
          }
        })
    }
    .width('100%')
  }

  @Builder
  urlRow(label: string, url: string) {
    Row() {
      Text(label)
        .fontSize(13)
        .fontColor('#7A8699')
        .width(80)
      Text(url)
        .fontSize(12)
        .fontColor('#C7D0DE')
        .layoutWeight(1)
        .copyOption(CopyOptions.LocalDevice)
    }
    .width('100%')
  }
}
