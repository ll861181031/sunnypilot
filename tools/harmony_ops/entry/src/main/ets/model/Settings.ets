import { preferences } from '@kit.ArkData';
import { common } from '@kit.AbilityKit';
import { BusinessError } from '@kit.BasicServicesKit';

/**
 * Connection settings for the three services, persisted and mirrored into
 * AppStorage.
 *
 * Why AppStorage: the host and ports are needed by four different views, and
 * @StorageLink lets each one bind directly instead of threading fields through
 * every constructor.
 *
 * Why every port is stored separately: the three services are independent
 * processes with their own ports, so no single port can be derived. Defaults
 * match the systemd units on the rig (dsh-web 3080, sp-console 8899,
 * byd-acc-capture 8770).
 */
export class Settings {
  static readonly HOST = 'host';
  static readonly CONSOLE_PORT = 'consolePort';
  static readonly CAPTURE_PORT = 'capturePort';
  static readonly DSH_PORT = 'dshPort';
  static readonly CONSOLE_TOKEN = 'consoleToken';
  static readonly DSH_TOKEN = 'dshToken';

  static readonly DEFAULT_CONSOLE_PORT = 8899;
  static readonly DEFAULT_CAPTURE_PORT = 8770;
  static readonly DEFAULT_DSH_PORT = 3080;

  private static store: preferences.Preferences | null = null;

  /**
   * Load persisted values into AppStorage.
   *
   * Called from EntryAbility.onCreate, where a UIAbilityContext is at hand. Doing
   * it here is what keeps getContext() out of every view.
   */
  static init(context: common.UIAbilityContext): void {
    try {
      const options: preferences.Options = { name: 'harmony_ops' };
      Settings.store = preferences.getPreferencesSync(context, options);
    } catch (err) {
      // Losing the store must not block the app: defaults plus manual entry work.
      const be = err as BusinessError;
      console.error(`harmony_ops: preferences unavailable: ${be.message}`);
      Settings.store = null;
    }

    AppStorage.setOrCreate<string>(Settings.HOST, Settings.readString(Settings.HOST, ''));
    AppStorage.setOrCreate<string>(Settings.CONSOLE_TOKEN, Settings.readString(Settings.CONSOLE_TOKEN, ''));
    AppStorage.setOrCreate<string>(Settings.DSH_TOKEN, Settings.readString(Settings.DSH_TOKEN, ''));
    AppStorage.setOrCreate<number>(Settings.CONSOLE_PORT,
      Settings.readNumber(Settings.CONSOLE_PORT, Settings.DEFAULT_CONSOLE_PORT));
    AppStorage.setOrCreate<number>(Settings.CAPTURE_PORT,
      Settings.readNumber(Settings.CAPTURE_PORT, Settings.DEFAULT_CAPTURE_PORT));
    AppStorage.setOrCreate<number>(Settings.DSH_PORT,
      Settings.readNumber(Settings.DSH_PORT, Settings.DEFAULT_DSH_PORT));
  }

  /**
   * Normalise whatever the user typed into a bare authority: strip a scheme, any
   * path, a trailing slash and surrounding spaces, because the views build URLs
   * by concatenation and a stray "http://" would produce http://http://...
   */
  static normaliseHost(input: string): string {
    let host = input.trim();
    if (host.startsWith('http://')) {
      host = host.substring(7);
    } else if (host.startsWith('https://')) {
      host = host.substring(8);
    }
    const slash = host.indexOf('/');
    if (slash >= 0) {
      host = host.substring(0, slash);
    }
    return host.trim();
  }

  static getString(key: string): string {
    return AppStorage.get<string>(key) ?? '';
  }

  static getNumber(key: string): number {
    return AppStorage.get<number>(key) ?? 0;
  }

  static setString(key: string, value: string): void {
    AppStorage.setOrCreate<string>(key, value);
    Settings.persist(key, value);
  }

  static setNumber(key: string, value: number): void {
    AppStorage.setOrCreate<number>(key, value);
    Settings.persist(key, value);
  }

  static host(): string {
    return Settings.getString(Settings.HOST);
  }

  static hasHost(): boolean {
    return Settings.host().length > 0;
  }

  static baseUrl(key: string, fallbackPort: number): string {
    const port = Settings.getNumber(key);
    const effective = port > 0 && port < 65536 ? port : fallbackPort;
    return `http://${Settings.host()}:${effective}`;
  }

  static consoleBase(): string {
    return Settings.baseUrl(Settings.CONSOLE_PORT, Settings.DEFAULT_CONSOLE_PORT);
  }

  static captureBase(): string {
    return Settings.baseUrl(Settings.CAPTURE_PORT, Settings.DEFAULT_CAPTURE_PORT);
  }

  /** The DSH page URL, carrying the token dsh web prints at startup. */
  static dshUrl(): string {
    const base = Settings.baseUrl(Settings.DSH_PORT, Settings.DEFAULT_DSH_PORT);
    const token = Settings.getString(Settings.DSH_TOKEN);
    return token.length > 0 ? `${base}/?token=${token}` : `${base}/`;
  }

  private static readString(key: string, fallback: string): string {
    if (Settings.store === null) {
      return fallback;
    }
    try {
      const value = Settings.store.getSync(key, fallback);
      return typeof value === 'string' ? value : fallback;
    } catch (err) {
      return fallback;
    }
  }

  private static readNumber(key: string, fallback: number): number {
    if (Settings.store === null) {
      return fallback;
    }
    try {
      const value = Settings.store.getSync(key, fallback);
      return typeof value === 'number' ? value : fallback;
    } catch (err) {
      return fallback;
    }
  }

  /**
   * Writes are fire-and-forget on purpose: a settings change must never block the
   * UI thread, and putSync has already updated the in-memory store that the next
   * read sees.
   */
  private static persist(key: string, value: string | number): void {
    if (Settings.store === null) {
      return;
    }
    try {
      Settings.store.putSync(key, value);
      Settings.store.flush((err: BusinessError) => {
        if (err) {
          console.error(`harmony_ops: flush failed: ${err.message}`);
        }
      });
    } catch (err) {
      const be = err as BusinessError;
      console.error(`harmony_ops: put failed: ${be.message}`);
    }
  }
}
