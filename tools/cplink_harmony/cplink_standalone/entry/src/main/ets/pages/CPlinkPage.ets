/**
 * Entry page of the standalone CP搭子 (CPlink) ability.
 *
 * It deliberately owns no state: the whole screen is the CPlink view, and the
 * title bar exists only so the app reads as its own product on the launcher
 * rather than a console tab.
 */
import { CPlinkView } from '../view/CPlinkView';

@Entry
@Component
struct CPlinkPage {
  build() {
    Column() {
      Row() {
        Text('CP搭子')
          .fontSize(20)
          .fontWeight(FontWeight.Bold)
          .fontColor('#E6EAF2')
        Blank()
        Text('Carrot · navipilot 协议')
          .fontSize(12)
          .fontColor('#7A8699')
      }
      .width('100%')
      .padding({ left: 16, right: 16, top: 10, bottom: 6 })

      Column() {
        CPlinkView()
      }
      .width('100%')
      .layoutWeight(1)
    }
    .width('100%')
    .height('100%')
    .backgroundColor('#0B0F14')
  }
}
