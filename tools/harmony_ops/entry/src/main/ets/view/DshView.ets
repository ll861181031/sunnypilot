import { webview } from '@kit.ArkWeb';
import { ApiClient, ApiResult } from '../model/ApiClient';
import { Settings } from '../model/Settings';

/**
 * The DeepSeek Harness Web GUI, embedded.
 *
 * DSH is a full single-page app with a streaming session protocol, so it is the
 * one service that cannot be reimplemented natively: it is loaded as-is.
 *
 * The reachability probe in front of the Web component exists because DSH has a
 * failure mode that looks like a blank page. Its /api browser-trust fence
 * snapshots the host's LAN addresses once, when the web runtime activates, and
 * then rejects every request from an address it did not see at that moment
 * (verified 2026-10-14 on this rig). A 403 therefore means something specific
 * and fixable, so the app says what it is instead of rendering nothing.
 */
@Component
export struct DshView {
  @StorageLink('host') host: string = '';
  @StorageLink('dshPort') dshPort: number = 3080;
  @StorageLink('dshToken') dshToken: string = '';
  @State url: string = '';
  @State tokenDraft: string = '';
  @State problem: string = '';
  @State checking: boolean = false;
  private controller: webview.WebviewController = new webview.WebviewController();

  aboutToAppear(): void {
    this.tokenDraft = this.dshToken;
    this.open();
  }

  /**
   * Probe the page with plain HTTP first, then hand the URL to the Web component.
   * Any HTTP status proves the server is up; only a transport error means down.
   */
  private async open(): Promise<void> {
    if (!Settings.hasHost()) {
      this.problem = '还没有配置主机地址，请到「设置」里填写，或点「自动发现」。';
      this.url = '';
      return;
    }
    this.checking = true;
    const probe: ApiResult = await ApiClient.get(Settings.dshUrl(), undefined, 6000);
    this.checking = false;

    if (probe.code === 403) {
      this.problem = 'DSH 拒绝了这个地址的 /api 请求（403）。这是它的浏览器信任白名单在进程启动时'
        + '没能采样到局域网地址导致的。修复：在主机上执行 systemctl --user restart dsh-web，'
        + '再把它新打印的 ?token= 填到下面。';
      this.url = '';
      return;
    }
    if (probe.code === 0) {
      this.problem = `连不上 DSH（${probe.error}）。确认主机地址、端口，以及 dsh-web 服务在运行。`;
      this.url = '';
      return;
    }
    this.problem = '';
    this.url = Settings.dshUrl();
  }

  private applyToken(): void {
    Settings.setString('dshToken', this.tokenDraft.trim());
    this.open();
  }

  build() {
    Column() {
      Column({ space: 8 }) {
        Row({ space: 8 }) {
          Text('地址').fontSize(13).fontColor('#7A8699')
          Text(`${this.host.length > 0 ? this.host : '未配置'}:${this.dshPort}`)
            .fontSize(13)
            .fontColor('#E6EAF2')
            .layoutWeight(1)
          Button(this.checking ? '检测中' : '重新加载')
            .fontSize(13)
            .height(32)
            .backgroundColor('#1E2634')
            .fontColor('#E6EAF2')
            .enabled(!this.checking)
            .onClick(() => {
              this.open();
            })
        }
        .width('100%')

        Row({ space: 8 }) {
          TextInput({ text: this.tokenDraft, placeholder: 'dsh web 启动时打印的 ?token=' })
            .layoutWeight(1)
            .height(38)
            .fontSize(13)
            .backgroundColor('#151B24')
            .fontColor('#E6EAF2')
            .onChange((value: string) => {
              this.tokenDraft = value;
            })
          Button('保存并打开')
            .fontSize(13)
            .height(38)
            .backgroundColor('#818CF8')
            .fontColor('#0B0F14')
            .onClick(() => {
              this.applyToken();
            })
        }
        .width('100%')
      }
      .width('100%')
      .padding(12)
      .backgroundColor('#0F141B')

      if (this.problem.length > 0) {
        Text(this.problem)
          .fontSize(13)
          .fontColor('#FBBF24')
          .width('100%')
          .padding(12)
          .backgroundColor('#1A1408')
      }

      if (this.url.length > 0) {
        Web({ src: this.url, controller: this.controller })
          .javaScriptAccess(true)
          .domStorageAccess(true)
          .mixedMode(MixedMode.All)
          .layoutWeight(1)
          .width('100%')
      } else {
        Column() {
          Text('DSH 页面未加载')
            .fontSize(15)
            .fontColor('#7A8699')
        }
        .layoutWeight(1)
        .width('100%')
        .justifyContent(FlexAlign.Center)
      }
    }
    .width('100%')
    .height('100%')
    .backgroundColor('#0B0F14')
  }
}
