import { ApiClient, ApiResult } from '../model/ApiClient';
import { CaptureFile, CaptureFiles, CaptureStatus, EpsRate, RateBucket, emptyCaptureFiles, emptyCaptureStatus, emptyEpsRate } from '../model/Types';
import { Settings } from '../model/Settings';

function twoDigits(value: number): string {
  return value < 10 ? `0${value}` : `${value}`;
}

function clockText(): string {
  const now = new Date();
  return `${twoDigits(now.getHours())}:${twoDigits(now.getMinutes())}:${twoDigits(now.getSeconds())}`;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) {
    return `${bytes} B`;
  }
  const kb = bytes / 1024;
  if (kb < 1024) {
    return `${kb.toFixed(1)} KB`;
  }
  return `${(kb / 1024).toFixed(1)} MB`;
}

function formatDuration(seconds: number): string {
  const total = Math.floor(seconds);
  const minutes = Math.floor(total / 60);
  const rest = total % 60;
  return `${minutes}:${twoDigits(rest)}`;
}

/** Percentiles arrive as null when a bucket has no samples; "-" reads better than 0. */
function stat(value: number | null): string {
  return value === null ? '—' : value.toFixed(1);
}

/** The three source modes capture.py accepts (`mode not in (...) -> HTTP 400`). */
const SOURCE_MODES: string[] = ['auto', 'cereal', 'panda'];
const SOURCE_LABELS: string[] = ['自动', 'cereal', 'panda'];

/**
 * The BYD ACC capture tool (~/.local/share/byd-acc-capture/capture.py).
 *
 * Polling is staggered on purpose: /api/status is cheap and drives the live
 * counters, /api/eps_rate recomputes four signals against the wall clock on every
 * request, and the file list only changes when a recording rolls over. Hitting all
 * three on one 2 s tick would waste the rig's CPU for no visible gain.
 */
@Component
export struct CaptureView {
  @StorageLink('host') host: string = '';
  @StorageLink('capturePort') capturePort: number = 8770;
  @State status: CaptureStatus = emptyCaptureStatus();
  @State statusLoaded: boolean = false;
  @State rate: EpsRate = emptyEpsRate();
  @State rateLoaded: boolean = false;
  @State files: CaptureFiles = emptyCaptureFiles();
  @State filesLoaded: boolean = false;
  @State error: string = '';
  @State notice: string = '';
  @State updatedAt: string = '';
  @State confirmStop: boolean = false;
  @State busy: boolean = false;
  private timerId: number = -1;
  private tick: number = 0;

  aboutToAppear(): void {
    this.refreshStatus();
    this.refreshRate();
    this.refreshFiles();
    this.timerId = setInterval(() => {
      this.tick = this.tick + 1;
      this.refreshStatus();
      if (this.tick % 2 === 0) {
        this.refreshRate();
      }
      if (this.tick % 5 === 0) {
        this.refreshFiles();
      }
    }, 2000);
  }

  aboutToDisappear(): void {
    if (this.timerId !== -1) {
      clearInterval(this.timerId);
      this.timerId = -1;
    }
  }

  private async refreshStatus(): Promise<void> {
    if (this.host.length === 0) {
      this.statusLoaded = false;
      return;
    }
    const result: ApiResult = await ApiClient.get(`${Settings.captureBase()}/api/status`, undefined, 5000);
    if (result.ok) {
      try {
        this.status = JSON.parse(result.body) as CaptureStatus;
        this.statusLoaded = true;
        this.error = '';
        this.updatedAt = clockText();
      } catch (err) {
        this.error = '/api/status 返回的不是合法 JSON';
      }
      return;
    }
    this.error = result.code === 0
      ? `连不上抓包工具（${result.error}）`
      : `抓包工具返回 HTTP ${result.code}`;
  }

  private async refreshRate(): Promise<void> {
    if (this.host.length === 0) {
      return;
    }
    const result: ApiResult = await ApiClient.get(`${Settings.captureBase()}/api/eps_rate`, undefined, 8000);
    if (result.ok) {
      try {
        this.rate = JSON.parse(result.body) as EpsRate;
        this.rateLoaded = true;
      } catch (err) {
        this.rateLoaded = false;
      }
    }
  }

  private async refreshFiles(): Promise<void> {
    if (this.host.length === 0) {
      return;
    }
    const result: ApiResult = await ApiClient.get(`${Settings.captureBase()}/api/captures`, undefined, 8000);
    if (result.ok) {
      try {
        this.files = JSON.parse(result.body) as CaptureFiles;
        this.filesLoaded = true;
      } catch (err) {
        this.filesLoaded = false;
      }
    }
  }

  private async act(path: string, label: string): Promise<void> {
    if (this.busy) {
      return;
    }
    this.busy = true;
    this.notice = `${label}中…`;
    const result: ApiResult = await ApiClient.action(`${Settings.captureBase()}${path}`, undefined);
    this.busy = false;
    this.confirmStop = false;
    if (result.ok) {
      this.notice = `${label}完成`;
      this.error = '';
    } else if (result.code === 0) {
      this.notice = '';
      this.error = `${label}失败：${result.error}`;
    } else {
      this.notice = '';
      this.error = `${label}失败：HTTP ${result.code}`;
    }
    await this.refreshStatus();
    await this.refreshFiles();
  }

  private isCapturing(): boolean {
    return this.statusLoaded && this.status.capturing;
  }

  private isRolling(): boolean {
    return this.statusLoaded && this.status.rolling;
  }

  private isUsbAlways(): boolean {
    return this.statusLoaded && this.status.usb_always;
  }

  private currentMode(): string {
    return !this.statusLoaded ? 'auto' : this.status.mode;
  }

  /**
   * `last_saved` arrives as JSON null when nothing has been saved yet, so the
   * parameter is widened on purpose: comparing a plain `string` to null would be a
   * no-overlap comparison the strict compiler rejects.
   */
  private str(value: string | null): string {
    return value === null || value.length === 0 ? '—' : value;
  }

  private bucketList(): RateBucket[] {
    return this.rateLoaded ? this.rate.by_speed : [];
  }

  private leadList(): RateBucket[] {
    return this.rateLoaded ? this.rate.by_lead : [];
  }

  private fileList(): CaptureFile[] {
    return this.filesLoaded ? this.files.files : [];
  }

  build() {
    Scroll() {
      Column({ space: 12 }) {
        if (this.host.length === 0) {
          this.banner('先在「设置」里填写主机地址，或用「自动发现」。', '#FBBF24', '#1A1408')
        } else {
          this.statusCard()
          this.controlCard()
          if (this.error.length > 0) {
            this.banner(this.error, '#F87171', '#2A1416')
          }
          if (this.notice.length > 0) {
            this.banner(this.notice, '#2DD4BF', '#0F1F1D')
          }
          this.rateCard()
          this.fileCard()
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
        Text('BYD 原车纵向 CAN 抓取')
          .fontSize(15)
          .fontColor('#E6EAF2')
          .fontWeight(FontWeight.Medium)
        Blank()
        Text(this.isCapturing() ? '采集中' : '已停止')
          .fontSize(13)
          .fontColor(this.isCapturing() ? '#2DD4BF' : '#94A3B8')
          .padding({ left: 10, right: 10, top: 3, bottom: 3 })
          .backgroundColor('#151B24')
          .borderRadius(10)
      }
      .width('100%')

      this.kv('来源', !this.statusLoaded ? '—' : `${this.status.source}（${this.status.mode}）`)
      this.kv('栈状态', !this.statusLoaded ? '—' : this.str(this.status.cereal_state))
      this.kv('已采帧', !this.statusLoaded ? '—' : `${this.status.total}`)
      this.kv('时长', !this.statusLoaded ? '—' : formatDuration(this.status.elapsed))
      this.kv('解码错误', !this.statusLoaded ? '—' : `${this.status.decoded_errors}`)
      this.kv('最近保存', !this.statusLoaded ? '—' : this.str(this.status.last_saved === null ? '' : this.status.last_saved))
      this.kv('刷新于', this.updatedAt.length > 0 ? this.updatedAt : '—')

      if (this.statusLoaded && this.status.source_detail.length > 0) {
        Text(this.status.source_detail)
          .fontSize(12)
          .fontColor('#A9B4C4')
          .width('100%')
      }
      if (this.statusLoaded && this.status.source_error.length > 0) {
        Text(this.status.source_error)
          .fontSize(12)
          .fontColor('#FBBF24')
          .width('100%')
      }
    }
    .width('100%')
    .padding(14)
    .backgroundColor('#0F141B')
    .borderRadius(12)
  }

  @Builder
  controlCard() {
    Column({ space: 12 }) {
      if (this.confirmStop) {
        Text('停止采集会结束当前录像。确认吗？')
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
          Button('确认停止')
            .layoutWeight(1)
            .height(44)
            .backgroundColor('#7F1D1D')
            .fontColor('#FEE2E2')
            .enabled(!this.busy)
            .onClick(() => {
              this.act('/api/stop', '停止采集');
            })
        }
        .width('100%')
      } else {
        Row({ space: 10 }) {
          Button(this.isCapturing() ? '停止采集' : '开始采集')
            .layoutWeight(1)
            .height(44)
            .backgroundColor(this.isCapturing() ? '#7F1D1D' : '#2DD4BF')
            .fontColor(this.isCapturing() ? '#FEE2E2' : '#06251F')
            .enabled(!this.busy)
            .onClick(() => {
              if (this.isCapturing()) {
                this.confirmStop = true;
              } else {
                this.act('/api/start', '开始采集');
              }
            })
          Button('清空')
            .width(88)
            .height(44)
            .backgroundColor('#1E2634')
            .fontColor('#E6EAF2')
            .enabled(!this.busy)
            .onClick(() => {
              this.act('/api/clear', '清空缓冲');
            })
        }
        .width('100%')
      }

      this.toggleRow('滚动录像', this.isRolling(), '/api/rolling', '滚动录像')
      this.toggleRow('常占 USB', this.isUsbAlways(), '/api/usbalways', 'USB 占用')

      Row({ space: 8 }) {
        Text('来源').fontSize(13).fontColor('#7A8699').width(72)
        ForEach(SOURCE_MODES, (mode: string, index: number) => {
          Button(SOURCE_LABELS[index])
            .layoutWeight(1)
            .height(38)
            .fontSize(13)
            .backgroundColor(this.currentMode() === mode ? '#2DD4BF' : '#1E2634')
            .fontColor(this.currentMode() === mode ? '#06251F' : '#E6EAF2')
            .enabled(!this.busy)
            .onClick(() => {
              this.act(`/api/source?mode=${mode}`, `切换来源为 ${mode}`);
            })
        }, (mode: string) => mode)
      }
      .width('100%')

      Text('自动 = 栈在跑就用 cereal，否则直读 panda。')
        .fontSize(12)
        .fontColor('#7A8699')
        .width('100%')
    }
    .width('100%')
    .padding(14)
    .backgroundColor('#0F141B')
    .borderRadius(12)
  }

  @Builder
  rateCard() {
    Column({ space: 10 }) {
      Text('横向跟随（按车速 / 按超前角）')
        .fontSize(15)
        .fontColor('#E6EAF2')
        .fontWeight(FontWeight.Medium)
        .width('100%')

      if (!this.rateLoaded) {
        Text('还没有速率数据。')
          .fontSize(12)
          .fontColor('#7A8699')
          .width('100%')
      } else {
        this.kv('指令来源', this.rate.commander)
        this.kv('故障数', `${this.rate.faults.n}`)
        this.kv('轮速/命令 样本', `${this.rate.samples.wheel} / ${this.rate.samples.cmd_active}`)

        Text('按车速分档（p50 / p90 / p99，度）')
          .fontSize(12)
          .fontColor('#7A8699')
          .width('100%')
        this.bucketHeader()
        ForEach(this.bucketList(), (bucket: RateBucket) => {
          this.bucketRow(bucket)
        }, (bucket: RateBucket) => `speed-${bucket.label}`)

        Text('按超前角分档（度）')
          .fontSize(12)
          .fontColor('#7A8699')
          .width('100%')
        this.bucketHeader()
        ForEach(this.leadList(), (bucket: RateBucket) => {
          this.bucketRow(bucket)
        }, (bucket: RateBucket) => `lead-${bucket.label}`)
      }
    }
    .width('100%')
    .padding(14)
    .backgroundColor('#0F141B')
    .borderRadius(12)
  }

  @Builder
  bucketHeader() {
    Row() {
      Text('区间').fontSize(11).fontColor('#5C6878').width(76)
      Text('n').fontSize(11).fontColor('#5C6878').width(44).textAlign(TextAlign.End)
      Text('p50').fontSize(11).fontColor('#5C6878').layoutWeight(1).textAlign(TextAlign.End)
      Text('p90').fontSize(11).fontColor('#5C6878').layoutWeight(1).textAlign(TextAlign.End)
      Text('p99').fontSize(11).fontColor('#5C6878').layoutWeight(1).textAlign(TextAlign.End)
    }
    .width('100%')
  }

  @Builder
  bucketRow(bucket: RateBucket) {
    Row() {
      Text(bucket.label).fontSize(12).fontColor('#A9B4C4').width(76)
      Text(`${bucket.n}`).fontSize(12).fontColor('#C7D0DE').width(44).textAlign(TextAlign.End)
      Text(stat(bucket.p50)).fontSize(12).fontColor('#C7D0DE').layoutWeight(1).textAlign(TextAlign.End)
      Text(stat(bucket.p90)).fontSize(12).fontColor('#C7D0DE').layoutWeight(1).textAlign(TextAlign.End)
      Text(stat(bucket.p99)).fontSize(12).fontColor('#C7D0DE').layoutWeight(1).textAlign(TextAlign.End)
    }
    .width('100%')
  }

  @Builder
  fileCard() {
    Column({ space: 10 }) {
      Row() {
        Text('录像文件')
          .fontSize(15)
          .fontColor('#E6EAF2')
          .fontWeight(FontWeight.Medium)
        Blank()
        Text(!this.filesLoaded ? '—' : `保留 ${this.files.keep} 段`)
          .fontSize(12)
          .fontColor('#7A8699')
      }
      .width('100%')

      if (this.fileList().length === 0) {
        Text('这个目录里还没有 CSV。')
          .fontSize(12)
          .fontColor('#7A8699')
          .width('100%')
      } else {
        ForEach(this.fileList(), (file: CaptureFile) => {
          Row() {
            Column({ space: 2 }) {
              Text(file.name)
                .fontSize(12)
                .fontColor(file.active ? '#2DD4BF' : '#C7D0DE')
                .maxLines(1)
                .textOverflow({ overflow: TextOverflow.Ellipsis })
              Text(`${formatBytes(file.size)} · ${file.mtime}`)
                .fontSize(11)
                .fontColor('#5C6878')
            }
            .alignItems(HorizontalAlign.Start)
            .layoutWeight(1)

            if (file.active) {
              Text('录制中')
                .fontSize(11)
                .fontColor('#06251F')
                .padding({ left: 8, right: 8, top: 2, bottom: 2 })
                .backgroundColor('#2DD4BF')
                .borderRadius(8)
            }
          }
          .width('100%')
          .padding({ top: 6, bottom: 6 })
        }, (file: CaptureFile) => file.name)
      }

      Divider().color('#1E2634')

      Text('当前缓冲导出（长按可复制链接，用手机浏览器下载）')
        .fontSize(12)
        .fontColor('#7A8699')
        .width('100%')
      Text(`${Settings.captureBase()}/api/export.csv`)
        .fontSize(12)
        .fontColor('#C7D0DE')
        .width('100%')
        .copyOption(CopyOptions.LocalDevice)
    }
    .width('100%')
    .padding(14)
    .backgroundColor('#0F141B')
    .borderRadius(12)
  }

  /**
   * A @Builder rather than two inline Toggles, but note the parameters: a plain
   * path and a label, never a callback. Huawei documents restrictions on what
   * @Builder accepts, so the action is rebuilt from the path here instead of
   * being handed in as a function.
   */
  @Builder
  toggleRow(label: string, on: boolean, actionPath: string, what: string) {
    Row() {
      Text(label).fontSize(13).fontColor('#C7D0DE').layoutWeight(1)
      Toggle({ type: ToggleType.Switch, isOn: on })
        .selectedColor('#2DD4BF')
        .enabled(!this.busy)
        .onChange((isOn: boolean) => {
          this.act(`${actionPath}?on=${isOn ? '1' : '0'}`, isOn ? `开启${what}` : `关闭${what}`);
        })
    }
    .width('100%')
  }

  @Builder
  kv(label: string, value: string) {
    Row() {
      Text(label)
        .fontSize(13)
        .fontColor('#7A8699')
        .width(96)
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
