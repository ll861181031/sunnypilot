/**
 * CP搭子 main view — state-management V2 throughout.
 *
 * Two runtime facts shaped this file:
 *   1. V1 @State assignments made from a setInterval callback never repainted on
 *      this device, so state is @Local (V2).
 *   2. Values rendered through a @Builder with value parameters are static — the
 *      telemetry text updated while every "label / value" row stayed frozen — so
 *      each row is its own @ComponentV2 with @Param instead.
 */
import { CP_CLIENT, CP_LIVE, CPlinkClient } from '../model/CPlinkClient';
import { CpDevice, CpSnap, CP_DATA_PORT, CP_HTTP_PORT, CP_NAVI_PORT } from '../model/CPlink';

@ComponentV2
struct CpCard {
  @Param title: string = '';
  @BuilderParam body: () => void;

  build() {
    Column({ space: 8 }) {
      Text(this.title)
        .fontSize(13)
        .fontColor('#2DD4BF')
        .fontWeight(FontWeight.Bold)
      this.body()
    }
    .width('100%')
    .padding(12)
    .backgroundColor('#151B24')
    .borderRadius(10)
    .alignItems(HorizontalAlign.Start)
  }
}

/** One label/value row. @Param inputs re-render with the parent's @Local snapshot. */
@ComponentV2
struct CpMetric {
  @Param label: string = '';
  @Param value: string = '';
  @Param color: string = '#E6EAF2';

  build() {
    Row() {
      Text(this.label)
        .fontSize(12)
        .fontColor('#7A8699')
        .width(110)
      Text(this.value)
        .fontSize(12)
        .fontColor(this.color)
        .layoutWeight(1)
        .textAlign(TextAlign.End)
        .maxLines(2)
    }
    .width('100%')
  }
}

@ComponentV2
export struct CPlinkView {
  @Local snap: CpSnap = new CpSnap();
  @Local target: string = '192.168.123.70';
  @Local scanning: boolean = false;
  private client: CPlinkClient = CP_CLIENT;
  private timerId: number = -1;
  private lastJson: string = '';

  aboutToAppear(): void {
    this.onSnap();
    this.timerId = setInterval(() => {
      this.onSnap();
    }, 500);
  }

  aboutToDisappear(): void {
    if (this.timerId !== -1) {
      clearInterval(this.timerId);
      this.timerId = -1;
    }
    // The link belongs to the process-wide client: leaving the page must not
    // tear down a session the car is using.
  }

  /** Pull the client's latest snapshot; a no-op until the first publish lands. */
  onSnap(): void {
    const txt: string = CP_LIVE.json;
    if (txt.length === 0 || txt === this.lastJson) {
      return;
    }
    this.lastJson = txt;
    try {
      this.snap = JSON.parse(txt) as CpSnap;
    } catch (err) {
      console.error(`CPLINK snapshot parse failed ${err}`);
    }
  }

  private linkColor(): string {
    if (this.snap.link === 'connected') {
      return '#2DD4BF';
    }
    if (this.snap.link === 'error') {
      return '#F87171';
    }
    if (this.snap.link === 'handshaking' || this.snap.link === 'discovering') {
      return '#FBBF24';
    }
    return '#7A8699';
  }

  private linkText(): string {
    if (this.snap.link === 'connected') {
      return '已连接';
    }
    if (this.snap.link === 'handshaking') {
      return '握手中';
    }
    if (this.snap.link === 'discovering') {
      return '发现中';
    }
    if (this.snap.link === 'error') {
      return '失败';
    }
    return '未连接';
  }

  /** 1 red / 2 green / 3 yellow, matching the device's TL_COLOR_MAP; 0 = no lamp. */
  private lampText(): string {
    if (this.snap.lampColor === 1) {
      return '红';
    }
    if (this.snap.lampColor === 2) {
      return '绿';
    }
    if (this.snap.lampColor === 3) {
      return '黄';
    }
    return '—';
  }

  private lampTint(): string {
    if (this.snap.lampColor === 1) {
      return '#F87171';
    }
    if (this.snap.lampColor === 2) {
      return '#4ADE80';
    }
    if (this.snap.lampColor === 3) {
      return '#FBBF24';
    }
    return '#7A8699';
  }

  private scan(): void {
    if (this.scanning) {
      return;
    }
    this.scanning = true;
    const parts: string[] = this.target.split('.');
    const prefix: string = parts.length >= 3
      ? `${parts[0]}.${parts[1]}.${parts[2]}` : '192.168.123';
    this.client.sweep(prefix).then(() => {
      this.scanning = false;
    }).catch(() => {
      this.scanning = false;
    });
  }

  build() {
    Column() {
      Column({ space: 8 }) {
        Row({ space: 8 }) {
          TextInput({ text: $$this.target, placeholder: '车端 IP，例如 192.168.123.70' })
            .layoutWeight(1)
            .height(38)
            .fontSize(14)
            .fontColor('#E6EAF2')
            .backgroundColor('#0F141B')
            .borderRadius(8)
          Button('发现')
            .height(38)
            .fontSize(14)
            .backgroundColor('#1E2634')
            .fontColor('#2DD4BF')
            .onClick(() => {
              this.client.startDiscovery();
            })
        }.width('100%')

        Row({ space: 8 }) {
          Button(this.scanning ? '扫描中…' : '扫描 /24')
            .layoutWeight(1)
            .height(38)
            .fontSize(14)
            .backgroundColor('#1E2634')
            .fontColor('#E6EAF2')
            .enabled(!this.scanning)
            .onClick(() => this.scan())
          Button('连接')
            .layoutWeight(1)
            .height(38)
            .fontSize(14)
            .backgroundColor('#2DD4BF')
            .fontColor('#06251F')
            .onClick(() => {
              this.client.connect(this.target);
            })
          Button('断开')
            .layoutWeight(1)
            .height(38)
            .fontSize(14)
            .backgroundColor('#7F1D1D')
            .fontColor('#FEE2E2')
            .onClick(() => {
              this.client.stop();
            })
        }.width('100%')

        if (this.snap.devices.length > 0) {
          List({ space: 2 }) {
            ForEach(this.snap.devices, (device: CpDevice) => {
              ListItem() {
                Row() {
                  Text(`${device.ip}${device.name.length > 0 ? '  ' + device.name : ''}`)
                    .fontSize(13)
                    .fontColor('#E6EAF2')
                  Blank()
                  Text(device.source)
                    .fontSize(11)
                    .fontColor('#7A8699')
                }
                .width('100%')
                .padding({ top: 5, bottom: 5 })
              }
              .onClick(() => {
                this.target = device.ip;
              })
            }, (device: CpDevice) => `${device.ip}-${device.seenAt}`)
          }
          .width('100%')
          .height(Math.min(this.snap.devices.length, 3) * 34)
        } else {
          Text('还没有设备。车端不广播时用「扫描 /24」按 7000 端口找。')
            .fontSize(12)
            .fontColor('#7A8699')
            .width('100%')
        }
      }
      .width('100%')
      .padding(12)
      .backgroundColor('#151B24')
      .borderRadius(10)
      .alignItems(HorizontalAlign.Start)
      .margin({ left: 12, right: 12, top: 10 })

      Scroll() {
        Column({ space: 10 }) {
          CpCard({ title: '链路' }) {
            Column({ space: 6 }) {
              CpMetric({ label: '状态', value: this.linkText(), color: this.linkColor() })
              CpMetric({ label: '说明', value: this.snap.detail.length > 0 ? this.snap.detail : '—' })
              CpMetric({ label: '心跳 (UDP 7706)', value: `${this.snap.hbSent} 已发 / ${this.snap.hbFail} 失败` })
              CpMetric({ label: 'HTTP 探活', value: `${this.snap.hbOk} 次成功 · RTT ${this.snap.hbRtt} ms` })
            }.width('100%')
          }

          CpCard({ title: '遥测 (WS 7000)' }) {
            Column({ space: 6 }) {
              CpMetric({
                label: '连接',
                value: this.snap.compact,
                color: this.snap.compact === 'open' ? '#2DD4BF' : '#F87171'
              })
              CpMetric({ label: '帧数', value: `${this.snap.compactFrames}` })
              Text(this.snap.compactLast.length > 0 ? this.snap.compactLast : '（等待车端 compact_state 数据）')
                .fontSize(11)
                .fontColor('#7A8699')
                .width('100%')
                .maxLines(3)
            }.width('100%')
          }

          CpCard({ title: '信号灯 (TCP 7711)' }) {
            Column({ space: 6 }) {
              CpMetric({
                label: '连接',
                value: this.snap.lamp,
                color: this.snap.lamp === 'open' ? '#2DD4BF' : '#F87171'
              })
              CpMetric({ label: '灯色', value: this.lampText(), color: this.lampTint() })
              CpMetric({
                label: '停止线',
                value: this.snap.lampStop > 0 ? `${this.snap.lampStop.toFixed(1)} m` : '—'
              })
              CpMetric({ label: '帧数', value: `${this.snap.lampFrames}` })
            }.width('100%')
          }

          CpCard({ title: '命令 (UDP 7706)' }) {
            Column({ space: 8 }) {
              Row({ space: 8 }) {
                Button('SPEED 60')
                  .layoutWeight(1)
                  .height(36)
                  .fontSize(13)
                  .backgroundColor('#1E2634')
                  .fontColor('#E6EAF2')
                  .onClick(() => {
                    this.client.sendCommand('SPEED', '60');
                  })
                Button('SPEED 80')
                  .layoutWeight(1)
                  .height(36)
                  .fontSize(13)
                  .backgroundColor('#1E2634')
                  .fontColor('#E6EAF2')
                  .onClick(() => {
                    this.client.sendCommand('SPEED', '80');
                  })
                Button('SPEED 100')
                  .layoutWeight(1)
                  .height(36)
                  .fontSize(13)
                  .backgroundColor('#1E2634')
                  .fontColor('#E6EAF2')
                  .onClick(() => {
                    this.client.sendCommand('SPEED', '100');
                  })
              }.width('100%')
              CpMetric({
                label: '最近命令',
                value: this.snap.lastCommand.length > 0 ? this.snap.lastCommand : '—'
              })
            }.width('100%')
          }

          CpCard({ title: '导航通道 (WS 7714 v2)' }) {
            Column({ space: 6 }) {
              CpMetric({
                label: '状态',
                value: this.snap.navi,
                color: this.snap.navi === 'connected' ? '#2DD4BF' : '#FBBF24'
              })
              CpMetric({ label: '会话', value: this.snap.naviSession.length > 0 ? this.snap.naviSession : '—' })
              CpMetric({ label: 'JSON 流', value: `${this.snap.naviOpen} / ${this.snap.naviTotal}` })
              CpMetric({ label: '收到帧', value: `${this.snap.naviFrames}` })
              Row({ space: 8 }) {
                Button('探针 vehicle')
                  .layoutWeight(1)
                  .height(36)
                  .fontSize(13)
                  .backgroundColor('#1E2634')
                  .fontColor('#E6EAF2')
                  .onClick(() => {
                    this.client.sendProbe('vehicle');
                  })
                Button('探针 guidance')
                  .layoutWeight(1)
                  .height(36)
                  .fontSize(13)
                  .backgroundColor('#1E2634')
                  .fontColor('#E6EAF2')
                  .onClick(() => {
                    this.client.sendProbe('guidance_current');
                  })
                Button('读参数')
                  .layoutWeight(1)
                  .height(36)
                  .fontSize(13)
                  .backgroundColor('#1E2634')
                  .fontColor('#2DD4BF')
                  .onClick(() => {
                    this.client.readParams(this.target, 'CarrotMan,EnableSpeedLimit');
                  })
              }.width('100%')
              if (this.snap.params.length > 0) {
                Text(this.snap.params)
                  .fontSize(11)
                  .fontColor('#7A8699')
                  .width('100%')
                  .maxLines(3)
              }
            }.width('100%')
          }

          CpCard({ title: '日志' }) {
            Text(this.snap.logs.length > 0 ? this.snap.logs : '（空）')
              .fontSize(11)
              .fontColor('#93A1B5')
              .width('100%')
              .maxLines(24)
          }

          Text(`端口：发现 7705 / 数据 ${CP_DATA_PORT} / HTTP ${CP_HTTP_PORT} / 导航 ${CP_NAVI_PORT} / 灯色 7711`)
            .fontSize(11)
            .fontColor('#5A6779')
            .width('100%')
            .textAlign(TextAlign.Center)
            .padding({ bottom: 12 })
        }
        .width('100%')
        .padding({ left: 12, right: 12, top: 10 })
      }
      .layoutWeight(1)
      .scrollBar(BarState.Auto)
      .align(Alignment.Top)
    }
    .width('100%')
    .height('100%')
    .backgroundColor('#0B0F14')
  }
}

