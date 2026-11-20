import { AbilityConstant, UIAbility, Want } from '@kit.AbilityKit';
import { window } from '@kit.ArkUI';

/**
 * Launcher entry for CP搭子.
 *
 * The bundle also hosts the 车机运维台 ability; this one gives the CPlink client
 * its own icon, its own window and its own task, so the two never share a tab
 * strip. `cplinkHost` is seeded here because the page's @StorageLink resolves
 * against AppStorage, not against the console's `host` key.
 */
export default class CPlinkAbility extends UIAbility {
  onCreate(want: Want, launchParam: AbilityConstant.LaunchParam): void {
  }

  onWindowStageCreate(windowStage: window.WindowStage): void {
    AppStorage.setOrCreate<string>('cplinkHost', '192.168.123.70');
    windowStage.loadContent('pages/CPlinkPage', () => {
    });
  }

  onWindowStageDestroy(): void {
  }

  onForeground(): void {
  }

  onBackground(): void {
  }

  onDestroy(): void {
  }
}
