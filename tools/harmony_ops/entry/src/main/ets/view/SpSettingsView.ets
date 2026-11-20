import { ApiClient, ApiResult } from '../model/ApiClient';
import {
  ErrorResponse, SpSettingGroup, SpSettingItem, SpSettingsPayload, SpWriteBody, SpWriteResponse,
  emptySpSettings
} from '../model/Types';
import { Settings } from '../model/Settings';

function twoDigits(value: number): string {
  return value < 10 ? `0${value}` : `${value}`;
}

function clockText(): string {
  const now = new Date();
  return `${twoDigits(now.getHours())}:${twoDigits(now.getMinutes())}:${twoDigits(now.getSeconds())}`;
}

/*
 * The wire value is a `boolean | number | null` union, and the two readers below keep
 * every use site from narrowing a field by hand. `asNumber` deliberately answers 0 for
 * an unset value: that is what the car's own backend hands out, and the stepper then
 * walks up into the declared range on the first tap.
 */
function asNumber(value: boolean | number | null): number {
  return typeof value === 'number' ? value : 0;
}

function asBool(value: boolean | number | null): boolean {
  return value === true;
}

/** A value as the car's own page would label it, honouring the stored integer's shift. */
function valueText(item: SpSettingItem): string {
  if (item.unset) {
    return item.default.length > 0 ? `未设置（默认 ${item.default}）` : '未设置';
  }
  if (item.type === 'bool') {
    return asBool(item.value) ? '开' : '关';
  }
  const raw = asNumber(item.value);
  const unit = item.unit.length > 0 ? ` ${item.unit}` : '';
  if (item.scale <= 1) {
    return `${raw}${unit}`;
  }
  const decimals = item.scale === 10 ? 1 : 2;
  return `${(raw / item.scale).toFixed(decimals)}${unit}`;
}

/** The message a failing endpoint sent, or '' when it did not send one. */
function errorText(body: string): string {
  try {
    const parsed = JSON.parse(body) as ErrorResponse;
    return parsed.error.length > 0 ? parsed.error : '';
  } catch (err) {
    return '';
  }
}

/**
 * The car's own settings pages, mirrored over the console's API.
 *
 * This is a remote control for the real Params backend, not a second settings store:
 * the console parses the list out of the car's settings sources and writes through the
 * same backend the car uses. Two rules belong to the console, not to this view: it
 * refuses every write while the stack is running (these keys are read when the stack
 * starts), and it clamps integers to the range the car's page declares.
 */
@Component
export struct SpSettingsView {
  @StorageLink('host') host: string = '';
  @StorageLink('consolePort') consolePort: number = 8899;
  @StorageLink('consoleToken') consoleToken: string = '';
  @State payload: SpSettingsPayload = emptySpSettings();
  @State loaded: boolean = false;
  @State editable: boolean = false;
  @State hint: string = '';
  @State error: string = '';
  @State notice: string = '';
  @State groupIndex: number = 0;
  @State busy: boolean = false;
  @State updatedAt: string = '';
  private timerId: number = -1;

  aboutToAppear(): void {
    this.refresh();
    // The manifest is tens of kilobytes, so this polls far slower than the console's
    // 2 s: the stack's running state is the only thing here that changes on its own.
    this.timerId = setInterval(() => {
      this.refresh();
    }, 8000);
  }

  aboutToDisappear(): void {
    if (this.timerId !== -1) {
      clearInterval(this.timerId);
      this.timerId = -1;
    }
  }

  private authHeaders(): Record<string, string> | undefined {
    if (this.consoleToken.length === 0) {
      return undefined;
    }
    const headers: Record<string, string> = {};
    headers['X-Console-Token'] = this.consoleToken;
    return headers;
  }

  private writeHeaders(): Record<string, string> {
    const headers: Record<string, string> = {};
    headers['Content-Type'] = 'application/json';
    if (this.consoleToken.length > 0) {
      headers['X-Console-Token'] = this.consoleToken;
    }
    return headers;
  }

  private async refresh(): Promise<void> {
    if (this.host.length === 0) {
      this.loaded = false;
      return;
    }
    const result: ApiResult = await ApiClient.get(
      `${Settings.consoleBase()}/api/sp_settings`, this.authHeaders(), 15000);
    if (result.ok) {
      try {
        const next = JSON.parse(result.body) as SpSettingsPayload;
        this.payload = next;
        this.editable = next.editable;
        this.hint = next.editable_hint;
        this.loaded = true;
        this.error = '';
        this.updatedAt = clockText();
        if (this.groupIndex >= next.manifest.groups.length) {
          this.groupIndex = 0;
        }
      } catch (err) {
        this.loaded = false;
        this.error = '/api/sp_settings 返回的不是合法 JSON';
      }
      return;
    }
    this.loaded = false;
    if (result.code === 401) {
      this.error = '这个控制台需要访问令牌，请在「设置」里填写。';
    } else if (result.code === 503) {
      this.error = '主机上的控制台读不到 Params 后端（可能不是车机本体）。';
    } else if (result.code === 404) {
      this.error = '控制台还没有 /api/sp_settings 接口，主机上的 server.py 需要更新。';
    } else if (result.code === 0) {
      this.error = `连不上控制台（${result.error}）`;
    } else {
      this.error = `控制台返回 HTTP ${result.code}`;
    }
  }

  private async write(item: SpSettingItem, value: boolean | number | null): Promise<void> {
    if (this.busy || !this.editable) {
      return;
    }
    this.busy = true;
    this.notice = '写入中…';
    const body: SpWriteBody = { key: item.key, value: value };
    const result: ApiResult = await ApiClient.postJson(
      `${Settings.consoleBase()}/api/sp_settings`, this.writeHeaders(),
      JSON.stringify(body), 15000);
    this.busy = false;

    if (result.ok) {
      let clamped = false;
      try {
        const parsed = JSON.parse(result.body) as SpWriteResponse;
        clamped = parsed.result.clamped;
      } catch (err) {
        clamped = false;
      }
      this.notice = clamped
        ? `${item.title}：已写入（被限制在允许范围内）`
        : `${item.title}：已写入`;
      this.error = '';
    } else if (result.code === 409) {
      this.notice = '';
      this.error = '行车栈正在运行，写入被拒绝。请先停止行车栈。';
    } else if (result.code === 401) {
      this.notice = '';
      this.error = '令牌不对，写入被拒绝。';
    } else if (result.code === 400 || result.code === 500) {
      const message = errorText(result.body);
      this.notice = '';
      this.error = message.length > 0 ? message : `写入失败：HTTP ${result.code}`;
    } else if (result.code === 0) {
      this.notice = '';
      this.error = `写入失败：${result.error}`;
    } else {
      this.notice = '';
      this.error = `写入失败：HTTP ${result.code}`;
    }
    await this.refresh();
  }

  private currentItems(): SpSettingItem[] {
    const groups = this.payload.manifest.groups;
    if (groups.length === 0 || this.groupIndex >= groups.length) {
      return [];
    }
    return groups[this.groupIndex].items;
  }

  private itemCount(): number {
    let total = 0;
    for (const group of this.payload.manifest.groups) {
      total += group.items.length;
    }
    return total;
  }

  private writableCount(): number {
    let total = 0;
    for (const group of this.payload.manifest.groups) {
      for (const item of group.items) {
        if (item.writable) {
          total += 1;
        }
      }
    }
    return total;
  }

  private canStep(item: SpSettingItem, direction: number): boolean {
    if (!this.editable || this.busy) {
      return false;
    }
    const raw = asNumber(item.value);
    if (direction < 0 && item.min !== null && raw <= item.min) {
      return false;
    }
    if (direction > 0 && item.max !== null && raw >= item.max) {
      return false;
    }
    return true;
  }

  private step(item: SpSettingItem, direction: number): void {
    const step = item.step > 0 ? item.step : 1;
    let next = asNumber(item.value) + direction * step;
    // An unset knob reads back as 0, which is usually outside its own range (the BYD
    // lead bound starts at 40), so the first tap lands on the range rather than being
    // refused as out of bounds.
    if (item.min !== null && next < item.min) {
      next = item.min;
    }
    if (item.max !== null && next > item.max) {
      next = item.max;
    }
    this.write(item, next);
  }

  private toggle(item: SpSettingItem, isOn: boolean): void {
    // Guarded against a write loop: a rebuild that sets the switch programmatically
    // must not read as the driver having tapped it.
    if (isOn === asBool(item.value)) {
      return;
    }
    this.write(item, isOn);
  }

  build() {
    Scroll() {
      Column({ space: 12 }) {
        if (this.host.length === 0) {
          this.banner('先在「设置」里填写主机地址，或用「自动发现」。', '#FBBF24', '#1A1408')
        } else {
          this.stateCard()
          if (this.error.length > 0) {
            this.banner(this.error, '#F87171', '#2A1416')
          }
          if (this.notice.length > 0) {
            this.banner(this.notice, '#2DD4BF', '#0F1F1D')
          }
          if (this.payload.manifest.groups.length > 0) {
            this.groupPicker()
            this.itemsCard()
          } else if (this.loaded) {
            this.banner('控制台没有返回任何设置页。', '#FBBF24', '#1A1408')
          }
          if (this.hint.length > 0) {
            this.banner(this.hint, '#FBBF24', '#1A1408')
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
  stateCard() {
    Column({ space: 8 }) {
      Row() {
        Text('SP 设置镜像')
          .fontSize(15)
          .fontColor('#E6EAF2')
          .fontWeight(FontWeight.Medium)
        Blank()
        Text(this.editable ? '可修改' : (this.payload.stack_running ? '行车栈运行中' : '只读'))
          .fontSize(13)
          .fontColor(this.editable ? '#2DD4BF' : '#FBBF24')
          .padding({ left: 10, right: 10, top: 3, bottom: 3 })
          .backgroundColor('#151B24')
          .borderRadius(10)
      }
      .width('100%')

      this.kv('主机', `${this.host}:${this.consolePort}`)
      this.kv('设置页', `${this.payload.manifest.groups.length} 组 / ${this.itemCount()} 项`)
      this.kv('其中可改', `${this.writableCount()} 项`)
      this.kv('已声明参数键', `${this.payload.manifest.known_keys}`)
      this.kv('刷新于', this.updatedAt.length > 0 ? this.updatedAt : '—')
      Text('多数参数在行车栈启动时读取，改完要重启行车栈才生效。')
        .fontSize(11)
        .fontColor('#7A8699')
        .width('100%')
    }
    .width('100%')
    .padding(14)
    .backgroundColor('#0F141B')
    .borderRadius(12)
  }

  @Builder
  groupPicker() {
    Scroll() {
      Row({ space: 8 }) {
        ForEach(this.payload.manifest.groups, (group: SpSettingGroup, index: number) => {
          Text(group.title)
            .fontSize(12)
            .fontColor(index === this.groupIndex ? '#0B0F14' : '#C3CCDA')
            .backgroundColor(index === this.groupIndex ? '#2DD4BF' : '#151B24')
            .padding({ left: 10, right: 10, top: 6, bottom: 6 })
            .borderRadius(14)
            .onClick(() => {
              this.groupIndex = index;
            })
        }, (group: SpSettingGroup) => group.id)
      }
    }
    .scrollable(ScrollDirection.Horizontal)
    .scrollBar(BarState.Off)
    .width('100%')
  }

  @Builder
  itemsCard() {
    Column({ space: 10 }) {
      Text(this.payload.manifest.groups[this.groupIndex].title)
        .fontSize(14)
        .fontColor('#E6EAF2')
        .fontWeight(FontWeight.Medium)
        .width('100%')
      if (this.payload.manifest.groups[this.groupIndex].subtitle.length > 0) {
        Text(this.payload.manifest.groups[this.groupIndex].subtitle)
          .fontSize(11)
          .fontColor('#7A8699')
          .width('100%')
      }
      ForEach(this.currentItems(), (item: SpSettingItem, index: number) => {
        this.itemCard(item)
      }, (item: SpSettingItem, index: number) =>
        `${this.groupIndex}-${index}-${item.unset}-${item.value}`)
    }
    .width('100%')
    .padding(14)
    .backgroundColor('#0F141B')
    .borderRadius(12)
  }

  @Builder
  itemCard(item: SpSettingItem) {
    Column({ space: 8 }) {
      Row() {
        Text(item.title)
          .fontSize(14)
          .fontColor(item.read_only ? '#C3CCDA' : '#E6EAF2')
          .layoutWeight(1)
        if (item.read_only) {
          Text('只读')
            .fontSize(11)
            .fontColor('#7A8699')
            .padding({ left: 8, right: 8, top: 2, bottom: 2 })
            .backgroundColor('#151B24')
            .borderRadius(8)
        } else if (item.at_default && item.key.length > 0) {
          Text('默认')
            .fontSize(11)
            .fontColor('#2DD4BF')
            .padding({ left: 8, right: 8, top: 2, bottom: 2 })
            .backgroundColor('#0F1F1D')
            .borderRadius(8)
        }
      }
      .width('100%')

      if (item.read_only) {
        // A `text` row's description is its value: the page shows the firmware constant
        // or a device identifier that no Params read can produce.
        Text(item.description.length > 0 ? item.description : '需在车机上查看')
          .fontSize(12)
          .fontColor('#C3CCDA')
          .width('100%')
      } else {
        if (item.description.length > 0) {
          Text(item.description)
            .fontSize(11)
            .fontColor('#7A8699')
            .width('100%')
        }
        if (item.type === 'bool') {
          Row() {
            Text(valueText(item))
              .fontSize(13)
              .fontColor('#2DD4BF')
            Blank()
            Toggle({ type: ToggleType.Switch, isOn: asBool(item.value) })
              .enabled(this.editable && !this.busy)
              .onChange((isOn: boolean) => {
                this.toggle(item, isOn);
              })
          }
          .width('100%')
        } else if (item.options.length > 0) {
          this.selectorRow(item)
        } else {
          this.stepperRow(item)
        }
        Text(item.key)
          .fontSize(10)
          .fontColor('#4B5563')
          .width('100%')
      }
    }
    .width('100%')
    .padding(12)
    .backgroundColor('#0B0F14')
    .borderRadius(10)
  }

  @Builder
  selectorRow(item: SpSettingItem) {
    Scroll() {
      Row({ space: 8 }) {
        ForEach(item.options, (label: string, optionIndex: number) => {
          Button(label)
            .fontSize(12)
            .height(34)
            .backgroundColor(optionIndex === asNumber(item.value) ? '#2DD4BF' : '#1E2634')
            .fontColor(optionIndex === asNumber(item.value) ? '#0B0F14' : '#E6EAF2')
            .enabled(this.editable && !this.busy)
            .onClick(() => {
              this.write(item, optionIndex);
            })
        }, (label: string, optionIndex: number) =>
          `${item.key}-${optionIndex}-${optionIndex === asNumber(item.value)}`)
      }
    }
    .scrollable(ScrollDirection.Horizontal)
    .scrollBar(BarState.Off)
    .width('100%')
  }

  @Builder
  stepperRow(item: SpSettingItem) {
    Row({ space: 8 }) {
      Button('−')
        .width(44)
        .height(36)
        .backgroundColor('#1E2634')
        .fontColor('#E6EAF2')
        .enabled(this.canStep(item, -1))
        .onClick(() => {
          this.step(item, -1);
        })
      Text(valueText(item))
        .fontSize(14)
        .fontColor('#E6EAF2')
        .layoutWeight(1)
        .textAlign(TextAlign.Center)
      Button('+')
        .width(44)
        .height(36)
        .backgroundColor('#1E2634')
        .fontColor('#E6EAF2')
        .enabled(this.canStep(item, 1))
        .onClick(() => {
          this.step(item, 1);
        })
      Button('重置')
        .height(36)
        .fontSize(12)
        .backgroundColor('#151B24')
        .fontColor('#7A8699')
        .enabled(this.editable && !this.busy && !item.at_default)
        .onClick(() => {
          // null unsets the key, which is how the car's own page returns a knob to its
          // built-in default.
          this.write(item, null);
        })
    }
    .width('100%')
  }

  @Builder
  kv(label: string, value: string) {
    Row() {
      Text(label)
        .fontSize(12)
        .fontColor('#7A8699')
        .width(96)
      Text(value)
        .fontSize(12)
        .fontColor('#C3CCDA')
        .layoutWeight(1)
    }
    .width('100%')
  }

  @Builder
  banner(text: string, color: string, background: string) {
    Text(text)
      .fontSize(12)
      .fontColor(color)
      .width('100%')
      .padding(10)
      .backgroundColor(background)
      .borderRadius(8)
  }
}
