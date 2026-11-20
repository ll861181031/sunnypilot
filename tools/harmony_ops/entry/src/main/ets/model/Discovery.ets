import { connection } from '@kit.NetworkKit';
import { ApiClient, ApiResult } from './ApiClient';

/**
 * Find the rig without the user typing an address.
 *
 * Deliberately NOT mDNS: @ohos.net.mdns exists, but its discovery/resolve
 * signatures are version-sensitive and it would only help for the single name
 * the rig publishes (openpilot.local). A sweep of the device's own /24 against
 * the port the sunnypilot console already serves is dependency-free, needs no
 * extra permission, and covers a hotspot whose name resolution is unreliable —
 * which is exactly the case this app is for.
 */
export class Discovery {
  /** Probes in flight at once. 16 keeps a 254-address sweep near 10 s. */
  private static readonly CONCURRENCY = 16;
  private static readonly PROBE_TIMEOUT_MS = 600;

  /** This device's own IPv4 on the default network, or '' when it has none. */
  static async localIpv4(): Promise<string> {
    try {
      const net = await connection.getDefaultNet();
      const props = await connection.getConnectionProperties(net);
      for (const link of props.linkAddresses) {
        // NetAddress.family is a number: 1 = IPv4, 2 = IPv6.
        if (link.address.family === 1) {
          return link.address.address;
        }
      }
    } catch (err) {
      console.error('harmony_ops: could not read the local address');
    }
    return '';
  }

  /**
   * Addresses worth trying, best first: what the user configured, then the rig's
   * mDNS name, then every host on this device's subnet.
   */
  static async candidates(configuredHost: string): Promise<string[]> {
    const list: string[] = [];
    if (configuredHost.length > 0) {
      list.push(configuredHost);
    }
    list.push('openpilot.local');

    const own = await Discovery.localIpv4();
    const lastDot = own.lastIndexOf('.');
    if (lastDot > 0) {
      const prefix = own.substring(0, lastDot);
      for (let i = 1; i <= 254; i++) {
        list.push(`${prefix}.${i}`);
      }
    }
    return list;
  }

  /**
   * The first candidate that answers like the sunnypilot console, or ''.
   *
   * The check is a successful GET /api/info carrying `bind_host`, not just an
   * open port: plenty of things on a home LAN answer on 8899, and adopting one of
   * them would send "start the car stack" to the wrong host.
   */
  static async findConsole(configuredHost: string, port: number): Promise<string> {
    const candidates = await Discovery.candidates(configuredHost);
    for (let i = 0; i < candidates.length; i += Discovery.CONCURRENCY) {
      const chunk = candidates.slice(i, i + Discovery.CONCURRENCY);
      const results = await Promise.all(chunk.map((host: string) =>
        Discovery.probeConsole(host, port)));
      for (const hit of results) {
        if (hit.length > 0) {
          return hit;
        }
      }
    }
    return '';
  }

  private static async probeConsole(host: string, port: number): Promise<string> {
    const result: ApiResult = await ApiClient.get(
      `http://${host}:${port}/api/info`, undefined, Discovery.PROBE_TIMEOUT_MS);
    if (result.ok && result.body.includes('bind_host')) {
      return host;
    }
    return '';
  }
}
