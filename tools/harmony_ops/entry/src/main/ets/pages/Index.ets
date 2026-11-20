import { ConsoleView } from '../view/ConsoleView';
import { CaptureView } from '../view/CaptureView';
import { DshView } from '../view/DshView';
import { SpSettingsView } from '../view/SpSettingsView';
import { SettingsView } from '../view/SettingsView';

/**
 * The whole app is one page with five tabs.
 *
 * Deliberately no router: a single page removes the main_pages / routing surface
 * entirely, and the tabs share nothing that would need a back stack. The decorator
 * arguments below are literal strings because @StorageLink takes a compile-time
 * constant.
 */
@Entry
@Component
struct Index {
  @State currentIndex: number = 0;
  @StorageLink('host') host: string = '';

  build() {
    Column() {
      Row() {
        Text('车机运维台')
          .fontSize(20)
          .fontWeight(FontWeight.Bold)
          .fontColor('#E6EAF2')
        Blank()
        Text(this.host.length > 0 ? this.host : '未配置主机')
          .fontSize(13)
          .fontColor(this.host.length > 0 ? '#2DD4BF' : '#F87171')
      }
      .width('100%')
      .padding({ left: 16, right: 16, top: 10, bottom: 10 })

      Tabs({ barPosition: BarPosition.End, index: this.currentIndex }) {
        TabContent() {
          ConsoleView()
        }.tabBar(this.tabItem('控制台', 0, '#FBBF24'))

        TabContent() {
          CaptureView()
        }.tabBar(this.tabItem('抓包', 1, '#2DD4BF'))

        TabContent() {
          DshView()
        }.tabBar(this.tabItem('DSH', 2, '#818CF8'))

        TabContent() {
          SpSettingsView()
        }.tabBar(this.tabItem('SP 设置', 3, '#4ADE80'))

        TabContent() {
          SettingsView()
        }.tabBar(this.tabItem('设置', 4, '#94A3B8'))
      }
      .barMode(BarMode.Fixed)
      .barHeight(56)
      .layoutWeight(1)
      .onChange((index: number) => {
        this.currentIndex = index;
      })
    }
    .width('100%')
    .height('100%')
    .backgroundColor('#0B0F14')
  }

  @Builder
  tabItem(label: string, index: number, accent: string) {
    Column({ space: 4 }) {
      Text(label)
        .fontSize(13)
        .fontColor(this.currentIndex === index ? accent : '#7A8699')
        .fontWeight(this.currentIndex === index ? FontWeight.Bold : FontWeight.Normal)
      Divider()
        .width(18)
        .strokeWidth(2)
        .color(this.currentIndex === index ? accent : '#00000000')
    }
    .justifyContent(FlexAlign.Center)
    .height('100%')
    .padding({ top: 6 })
  }
}
