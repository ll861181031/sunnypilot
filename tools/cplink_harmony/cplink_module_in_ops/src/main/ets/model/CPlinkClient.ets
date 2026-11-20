/**
 * CPlink client for HarmonyOS.
 *
 * Same three channels the Android app (CP搭子 / navipilot) opens against a Carrot
 * device; this file only owns transport and state, never UI:
 *
 *   1. discovery  UDP 7705 listen  (+ HTTP 7000 sweep as a fallback)
 *   2. link       HTTP 7000 /api/heartbeat_status handshake, then UDP 7706 heartbeat
 *   3. telemetry  WS 7000 /ws/compact_state
 *   4. navigation WS 7714 v2 control socket -> subscription_manifest -> 13 JSON streams
 *
 * The view polls `status`; the client never touches ArkUI state directly, so a
 * socket callback cannot run into a state-observation cycle.
 */
import { socket, http, webSocket } from '@kit.NetworkKit';
import { BusinessError } from '@kit.BasicServicesKit';
import { util } from '@kit.ArkTS';
import {
  CP_DISCOVERY_PORT, CP_DATA_PORT, CP_HTTP_PORT, CP_BRIDGE_PORT, CP_NAVI_PORT,
  CP_PROTOCOL_VERSION, CP_APP_VERSION, CP_JSON_STREAMS, CpDevice, CpStatus,
  CpManifest, CpManifestStream, CpBridgeFrame, CpBridgeData, CpBridgeCarState, CpSnap,
  cpHeartbeatFrame, cpCommandFrame, cpRequirementsQuery, cpManifestApplied, cpItemUpdate
} from './CPlink';

/** Latest published snapshot; the view polls it (AppStorage is not available in model files). */
export class CpLive {
  json: string = '';
}
export const CP_LIVE: CpLive = new CpLive();

class CpOpenStream {
  name: string = '';
  config: CpManifestStream;
  ws: webSocket.WebSocket | null = null;
  open: boolean = false;
  sequence: number = 0;

  constructor(config: CpManifestStream) {
    this.config = config;
    this.name = config.name;
  }
}

export class CPlinkClient {
  readonly status: CpStatus = new CpStatus();
  readonly devices: CpDevice[] = [];
  readonly logs: string[] = [];

  private discoverySocket: socket.UDPSocket | null = null;
  private dataSocket: socket.UDPSocket | null = null;
  private compactSocket: webSocket.WebSocket | null = null;
  private controlSocket: webSocket.WebSocket | null = null;
  private bridgeSocket: socket.TCPSocket | null = null;
  private bridgeBuffer: Uint8Array = new Uint8Array(0);
  private streams: CpOpenStream[] = [];
  private hbTimer: number = -1;
  private probeTimer: number = -1;
  private heartbeatIndex: number = 1;
  private publishTimer: number = -1;
  private tick: number = 0;
  private manifest: CpManifest | null = null;
  private wanted: boolean = false;

  log(line: string): void {
    const stamp: string = this.clock();
    this.logs.push(`${stamp} ${line}`);
    while (this.logs.length > 240) {
      this.logs.shift();
    }
  }

  /**
   * Push the whole snapshot into AppStorage.
   *
   * @State assignments made from a timer callback did not reach the render tree on
   * this device (the cards froze on their first frame while the sockets kept
   * working), so the view binds to this key instead.
   */
  publish(): void {
    const st: CpStatus = this.status;
    const s: CpSnap = new CpSnap();
    s.link = st.link;
    s.detail = st.detail;
    s.hbSent = st.hbSent;
    s.hbOk = st.hbOk;
    s.hbFail = st.hbFail;
    s.hbRtt = st.hbRttMs;
    s.compact = st.compact;
    s.compactFrames = st.compactFrames;
    s.compactLast = st.compactLast;
    s.navi = st.navi;
    s.naviSession = st.naviSession;
    s.naviOpen = st.naviStreamsOpen;
    s.naviTotal = st.naviStreamsTotal;
    s.naviFrames = st.naviFrames;
    s.params = st.params;
    s.lamp = st.lamp;
    s.lampColor = st.lampColor;
    s.lampStop = st.lampStopLine;
    s.lampFrames = st.lampFrames;
    s.lastCommand = st.lastCommand;
    s.devices = this.devices.slice();
    s.logs = this.logs.join('\n');
    s.cid = st.cid;
    this.tick++;
    s.tick = this.tick;
    CP_LIVE.json = JSON.stringify(s);
  }

  private ensurePublisher(): void {
    if (this.publishTimer !== -1) {
      return;
    }
    this.publishTimer = setInterval(() => {
      this.publish();
    }, 400);
    this.publish();
  }

  private clock(): string {
    const d: Date = new Date();
    const p = (n: number): string => n < 10 ? `0${n}` : `${n}`;
    return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  }

  private decode(buffer: ArrayBuffer): string {
    return this.decodeBytes(new Uint8Array(buffer));
  }

  private decodeBytes(bytes: Uint8Array): string {
    try {
      const decoder: util.TextDecoder = util.TextDecoder.create('utf-8');
      return decoder.decodeToString(bytes);
    } catch (err) {
      return '';
    }
  }

  /** Devices are de-duplicated by ip; a repeat broadcast only refreshes `seenAt`. */
  private remember(ip: string, name: string, source: string): void {
    if (ip.length === 0 || ip === '0.0.0.0') {
      return;
    }
    for (let i = 0; i < this.devices.length; i++) {
      if (this.devices[i].ip === ip) {
        this.devices[i].seenAt = Date.now();
        if (name.length > 0) {
          this.devices[i].name = name;
        }
        return;
      }
    }
    const device: CpDevice = new CpDevice(ip, source);
    device.name = name;
    this.devices.push(device);
    this.log(`发现设备 ${ip}${name.length > 0 ? ' (' + name + ')' : ''} ← ${source}`);
  }

  // ---------------------------------------------------------------- discovery

  async startDiscovery(): Promise<void> {
    this.ensurePublisher();
    if (this.discoverySocket !== null) {
      return;
    }
    this.status.link = 'discovering';
    try {
      const udp: socket.UDPSocket = socket.constructUDPSocketInstance();
      await udp.bind({ address: '0.0.0.0', port: CP_DISCOVERY_PORT, family: 1 });
      udp.on('message', (info: socket.SocketMessageInfo) => {
        const text: string = this.decode(info.message);
        const from: string = info.remoteInfo.address;
        let ip: string = from;
        let name: string = '';
        try {
          const obj: Record<string, string> = JSON.parse(text) as Record<string, string>;
          const declared: string | undefined = obj['ip'];
          if (declared !== undefined && declared.length > 0) {
            ip = declared;
          }
          const declaredName: string | undefined = obj['name'];
          if (declaredName !== undefined) {
            name = declaredName;
          }
        } catch (err) {
          // A text broadcast is still a real sighting; keep the source address.
        }
        this.remember(ip, name, 'broadcast');
      });
      udp.on('error', (err: BusinessError) => {
        this.log(`UDP ${CP_DISCOVERY_PORT} 错误 ${err.code}`);
      });
      this.discoverySocket = udp;
      this.log(`监听 UDP ${CP_DISCOVERY_PORT}（等待车端广播）`);
    } catch (err) {
      const be = err as BusinessError;
      this.log(`无法绑定 UDP ${CP_DISCOVERY_PORT}: ${be.message}`);
    }
  }

  stopDiscovery(): void {
    if (this.discoverySocket === null) {
      return;
    }
    const udp: socket.UDPSocket = this.discoverySocket;
    this.discoverySocket = null;
    udp.off('message');
    udp.off('error');
    udp.close().catch((err: BusinessError) => this.log(`关闭发现 socket 失败 ${err.code}`));
    this.log('停止监听广播');
  }

  /**
   * Fallback discovery: probe `http://prefix.x:7000/api/heartbeat_status` over a /24.
   * 32 requests in flight, 700 ms each — the same shape the Android app uses when
   * broadcast is blocked by AP isolation.
   */
  async sweep(prefix: string): Promise<number> {
    const base: string = prefix.endsWith('.') ? prefix : `${prefix}.`;
    this.log(`扫描 ${base}1-254 的 ${CP_HTTP_PORT} 端口`);
    let found: number = 0;
    const batch: number = 32;
    for (let start = 1; start <= 254; start += batch) {
      const jobs: Promise<void>[] = [];
      for (let i = start; i < start + batch && i <= 254; i++) {
        const ip: string = `${base}${i}`;
        jobs.push(this.probe(ip).then((ok: boolean) => {
          if (ok) {
            found++;
            this.remember(ip, '', 'scan');
          }
        }));
      }
      await Promise.all(jobs);
    }
    this.log(`扫描结束，${found} 台应答`);
    return found;
  }

  private probe(ip: string): Promise<boolean> {
    return new Promise<boolean>((resolve: (value: boolean) => void) => {
      const req: http.HttpRequest = http.createHttp();
      req.request(`http://${ip}:${CP_HTTP_PORT}/api/heartbeat_status`, {
        method: http.RequestMethod.GET,
        connectTimeout: 700,
        readTimeout: 700,
        expectDataType: http.HttpDataType.STRING
      }).then((resp: http.HttpResponse) => {
        req.destroy();
        resolve(resp.responseCode === 200);
      }).catch(() => {
        req.destroy();
        resolve(false);
      });
    });
  }

  // ------------------------------------------------------------------ connect

  async connect(ip: string): Promise<boolean> {
    this.ensurePublisher();
    if (ip.length === 0) {
      return false;
    }
    this.stop();
    this.wanted = true;
    this.status.host = ip;
    this.status.link = 'handshaking';
    this.status.detail = '';
    this.log(`开始握手 ${ip}:${CP_HTTP_PORT}`);

    const ok: boolean = await this.handshake(ip);
    if (!ok) {
      this.status.link = 'error';
      this.status.detail = `握手失败（${ip}:${CP_HTTP_PORT}/api/heartbeat_status 未返回 ok=true）`;
      this.log(this.status.detail);
      return false;
    }
    this.status.link = 'connected';
    this.status.detail = '已连接';
    this.log('握手成功：车端心跳正常');
    this.startHeartbeat(ip);
    this.openCompact(ip);
    this.openNavi(ip);
    this.openBridge(ip);
    return true;
  }

  /** The only acceptance test the Android app trusts: HTTP 200 **and** JSON ok=true. */
  private handshake(ip: string): Promise<boolean> {
    return new Promise<boolean>((resolve: (value: boolean) => void) => {
      const req: http.HttpRequest = http.createHttp();
      req.request(`http://${ip}:${CP_HTTP_PORT}/api/heartbeat_status`, {
        method: http.RequestMethod.GET,
        connectTimeout: 1500,
        readTimeout: 1500,
        expectDataType: http.HttpDataType.STRING
      }).then((resp: http.HttpResponse) => {
        req.destroy();
        if (resp.responseCode !== 200) {
          resolve(false);
          return;
        }
        try {
          const body: Record<string, boolean> = JSON.parse(resp.result as string) as
            Record<string, boolean>;
          resolve(body['ok'] === true);
        } catch (err) {
          resolve(false);
        }
      }).catch((err: BusinessError) => {
        req.destroy();
        this.log(`握手请求失败 ${err.code}`);
        resolve(false);
      });
    });
  }

  private startHeartbeat(ip: string): void {
    const udp: socket.UDPSocket = socket.constructUDPSocketInstance();
    this.dataSocket = udp;
    // HarmonyOS refuses to send from an unbound UDP socket (2301009), so bind an
    // ephemeral local port first; heartbeats are unicast and need no broadcast right.
    udp.bind({ address: '0.0.0.0', port: 0, family: 1 })
      .then(() => {
        this.status.detail = '已连接';
        this.log(`UDP ${CP_DATA_PORT} 本机端口已就绪`);
      })
      .catch((err: BusinessError) => {
        this.log(`UDP ${CP_DATA_PORT} 绑定失败 ${err.code}`);
      });
    this.hbTimer = setInterval(() => {
      this.heartbeatIndex = this.heartbeatIndex >= 2147483000 ? 1 : this.heartbeatIndex + 1;
      const frame: string = cpHeartbeatFrame(this.heartbeatIndex);
      udp.send({
        data: frame,
        address: { address: ip, port: CP_DATA_PORT, family: 1 }
      }).then(() => {
        this.status.hbSent++;
      }).catch((err: BusinessError) => {
        this.status.hbFail++;
        if (this.status.hbFail % 10 === 1) {
          this.log(`UDP ${CP_DATA_PORT} 发送失败 ${err.code}`);
        }
      });
    }, 1000);
    this.probeTimer = setInterval(() => {
      this.refreshProbe(ip);
    }, 5000);
    this.log(`开始 UDP ${CP_DATA_PORT} 心跳（1s）+ HTTP 探活（5s）`);
  }

  private refreshProbe(ip: string): void {
    const started: number = Date.now();
    const req: http.HttpRequest = http.createHttp();
    req.request(`http://${ip}:${CP_HTTP_PORT}/api/heartbeat_status`, {
      method: http.RequestMethod.GET,
      connectTimeout: 1200,
      readTimeout: 1200,
      expectDataType: http.HttpDataType.STRING
    }).then((resp: http.HttpResponse) => {
      req.destroy();
      const rtt: number = Date.now() - started;
      if (resp.responseCode === 200) {
        this.status.hbOk++;
        this.status.hbRttMs = rtt;
        this.status.link = 'connected';
        this.status.detail = `已连接 · RTT ${rtt} ms`;
      } else {
        this.status.hbFail++;
        this.status.link = 'error';
        this.status.detail = `车端 HTTP ${resp.responseCode}`;
      }
    }).catch(() => {
      req.destroy();
      this.status.hbFail++;
      this.status.link = 'error';
      this.status.detail = '车端无应答';
    });
  }

  private openCompact(ip: string): void {
    this.status.compact = 'connecting';
    const ws: webSocket.WebSocket = webSocket.createWebSocket();
    this.compactSocket = ws;
    ws.on('open', () => {
      this.status.compact = 'open';
      this.log(`WS ${CP_HTTP_PORT}/ws/compact_state 已打开`);
    });
    ws.on('message', (err: BusinessError, value: string | ArrayBuffer) => {
      if (typeof value !== 'string') {
        return;
      }
      this.status.compactFrames++;
      this.status.compactLast = value.length > 220 ? `${value.slice(0, 220)}…` : value;
    });
    ws.on('close', () => {
      this.status.compact = 'closed';
    });
    ws.on('error', (err: BusinessError) => {
      this.status.compact = 'closed';
      this.log(`compact_state 错误 ${err.code}`);
    });
    ws.connect(`ws://${ip}:${CP_HTTP_PORT}/ws/compact_state`,
      (err: BusinessError, ok: boolean) => {
        if (!ok) {
          this.status.compact = 'closed';
          this.log('compact_state 连接失败');
        }
      });
  }

  // --------------------------------------------------------------- navi v2

  private openNavi(ip: string): void {
    this.status.navi = 'connecting';
    const ws: webSocket.WebSocket = webSocket.createWebSocket();
    this.controlSocket = ws;
    ws.on('open', () => {
      this.status.navi = 'negotiating';
      this.log(`7714 控制通道已开，发 requirements_query`);
      ws.send(cpRequirementsQuery(), (err: BusinessError, ok: boolean) => {
        if (!ok) {
          this.log('requirements_query 发送失败');
        }
      });
    });
    ws.on('message', (err: BusinessError, value: string | ArrayBuffer) => {
      if (typeof value !== 'string') {
        return;
      }
      this.onControlMessage(ip, value);
    });
    ws.on('close', () => {
      if (this.status.navi !== 'error') {
        this.status.navi = 'idle';
      }
      this.status.naviStreamsOpen = 0;
    });
    ws.on('error', (err: BusinessError) => {
      this.status.navi = 'error';
      this.log(`7714 控制通道错误 ${err.code}`);
    });
    ws.connect(`ws://${ip}:${CP_NAVI_PORT}/api/navi/ws/v2/control/${CP_APP_VERSION}`,
      (err: BusinessError, ok: boolean) => {
        if (!ok) {
          this.status.navi = 'error';
          this.log('7714 控制通道连接失败（车端可能没有 Carrot navi 服务）');
        }
      });
  }

  private onControlMessage(ip: string, text: string): void {
    let manifest: CpManifest;
    try {
      manifest = JSON.parse(text) as CpManifest;
    } catch (err) {
      this.log(`控制通道收到非 JSON 消息`);
      return;
    }
    if (manifest.type === 'protocol_error') {
      this.status.navi = 'error';
      this.log('7714 protocol_error');
      return;
    }
    if (manifest.type !== 'subscription_manifest') {
      return;
    }
    if (manifest.protocol_version !== CP_PROTOCOL_VERSION) {
      this.status.navi = 'error';
      this.log(`协议版本不符：${manifest.protocol_version}`);
      return;
    }
    const streams: CpManifestStream[] = manifest.streams === undefined ? [] : manifest.streams;
    this.manifest = manifest;
    this.status.naviSession = manifest.session_id;
    this.status.naviStreamsTotal = 0;
    for (let i = 0; i < streams.length; i++) {
      if (streams[i].kind === 'json' && streams[i].enabled) {
        this.status.naviStreamsTotal++;
      }
    }
    this.log(`manifest：session ${manifest.session_id}，启用 JSON 流 ` +
      `${this.status.naviStreamsTotal} 个`);

    if (this.controlSocket !== null) {
      this.controlSocket.send(cpManifestApplied(manifest.session_id, manifest.revision, streams),
        (err: BusinessError, ok: boolean) => {
          if (!ok) {
            this.log('manifest_applied 发送失败');
          }
        });
    }
    this.openJsonStreams(ip, manifest, streams);
  }

  private openJsonStreams(ip: string, manifest: CpManifest,
                          streams: CpManifestStream[]): void {
    this.streams = [];
    for (let i = 0; i < streams.length; i++) {
      const cfg: CpManifestStream = streams[i];
      if (cfg.kind !== 'json' || !cfg.enabled) {
        continue;
      }
      const slot: CpOpenStream = new CpOpenStream(cfg);
      this.streams.push(slot);
      const ws: webSocket.WebSocket = webSocket.createWebSocket();
      slot.ws = ws;
      ws.on('open', () => {
        slot.open = true;
        this.status.naviStreamsOpen = this.countOpen();
        if (this.status.naviStreamsOpen >= this.status.naviStreamsTotal) {
          this.status.navi = 'connected';
          this.log(`7714 全部 ${this.status.naviStreamsOpen} 个 JSON 流就绪`);
        }
      });
      ws.on('message', (err: BusinessError, value: string | ArrayBuffer) => {
        if (typeof value === 'string') {
          this.status.naviFrames++;
        }
      });
      ws.on('close', () => {
        slot.open = false;
        this.status.naviStreamsOpen = this.countOpen();
      });
      ws.on('error', () => {
        slot.open = false;
        this.status.naviStreamsOpen = this.countOpen();
      });
      ws.connect(`ws://${ip}:${CP_NAVI_PORT}/api/navi/ws/v2/json/${manifest.session_id}/${cfg.name}`,
        (err: BusinessError, ok: boolean) => {
          if (!ok) {
            this.log(`流 ${cfg.name} 连接失败`);
          }
        });
    }
  }

  private countOpen(): number {
    let n: number = 0;
    for (let i = 0; i < this.streams.length; i++) {
      if (this.streams[i].open) {
        n++;
      }
    }
    return n;
  }

  /**
   * Push one probe frame on a JSON stream: present=false means "this source has
   * nothing to say", so the receiver counts a frame and the car gets no data.
   */
  sendProbe(streamName: string): boolean {
    const manifest: CpManifest | null = this.manifest;
    if (manifest === null) {
      this.log('还没有会话，先等 manifest');
      return false;
    }
    for (let i = 0; i < this.streams.length; i++) {
      const slot: CpOpenStream = this.streams[i];
      if (slot.name !== streamName || !slot.open || slot.ws === null) {
        continue;
      }
      slot.sequence++;
      const frame: string = cpItemUpdate(manifest.session_id, manifest.revision, slot.config,
        slot.sequence, false, '', 'cplink_harmony_probe');
      slot.ws.send(frame, (err: BusinessError, ok: boolean) => {
        if (!ok) {
          this.log(`探针帧发送失败 ${streamName}`);
        }
      });
      this.log(`已发探针帧 ${streamName} #${slot.sequence}`);
      return true;
    }
    this.log(`流 ${streamName} 未就绪`);
    return false;
  }

  /** Read a couple of Params the way the Android client's CarrotParamClient does. */
  async readParams(ip: string, names: string): Promise<string> {
    return new Promise<string>((resolve: (value: string) => void) => {
      const req: http.HttpRequest = http.createHttp();
      req.request(`http://${ip}:${CP_HTTP_PORT}/api/params_bulk?names=${encodeURIComponent(names)}`, {
        method: http.RequestMethod.GET,
        connectTimeout: 1500,
        readTimeout: 1500,
        expectDataType: http.HttpDataType.STRING
      }).then((resp: http.HttpResponse) => {
        req.destroy();
        const text: string = resp.responseCode === 200 ? (resp.result as string) : '';
        this.status.params = text.length > 160 ? `${text.slice(0, 160)}…` : text;
        resolve(text);
      }).catch(() => {
        req.destroy();
        resolve('');
      });
    });
  }

  // ------------------------------------------------------------ commands (7706)

  /**
   * One explicit command packet. `carrotCmd` is a short verb the device's
   * carrot_man parses by hand ("SPEED 80"), so the caller decides what to send;
   * this client only counts and echoes it in the log.
   */
  sendCommand(command: string, arg: string): boolean {
    const udp: socket.UDPSocket | null = this.dataSocket;
    const host: string = this.status.host;
    if (udp === null || host.length === 0) {
      this.log('未连接，命令没有发出去');
      return false;
    }
    this.heartbeatIndex = this.heartbeatIndex >= 2147483000 ? 1 : this.heartbeatIndex + 1;
    const frame: string = cpCommandFrame(this.heartbeatIndex, command, arg);
    udp.send({ data: frame, address: { address: host, port: CP_DATA_PORT, family: 1 } })
      .then(() => {
        this.status.lastCommand = `${command} ${arg}`;
        this.log(`7706 命令已发：${command} ${arg}`);
      })
      .catch((err: BusinessError) => {
        this.log(`7706 命令发送失败 ${err.code}`);
      });
    return true;
  }

  // --------------------------------------------------------- CAN bridge (7711)

  /**
   * 7711 is a push-only TCP feed: the device sends `len(4B BE) + JSON` at ~20 Hz
   * with the junction lamp colour and the stop-line distance. Nothing is requested.
   */
  private openBridge(ip: string): void {
    this.status.lamp = 'connecting';
    this.bridgeBuffer = new Uint8Array(0);
    const tcp: socket.TCPSocket = socket.constructTCPSocketInstance();
    this.bridgeSocket = tcp;
    tcp.on('message', (info: socket.SocketMessageInfo) => {
      this.onBridgeBytes(new Uint8Array(info.message));
    });
    tcp.on('close', () => {
      this.status.lamp = 'closed';
    });
    tcp.on('error', (err: BusinessError) => {
      this.status.lamp = 'error';
      this.log(`7711 错误 ${err.code}`);
    });
    tcp.connect({ address: { address: ip, port: CP_BRIDGE_PORT, family: 1 }, timeout: 3000 })
      .then(() => {
        this.status.lamp = 'open';
        this.log(`7711 灯色通道已连接`);
      })
      .catch(() => {
        this.status.lamp = 'error';
        this.log('7711 灯色通道连接失败（车端无 xiaoge_data）');
      });
  }

  private onBridgeBytes(chunk: Uint8Array): void {
    const merged: Uint8Array = new Uint8Array(this.bridgeBuffer.length + chunk.length);
    merged.set(this.bridgeBuffer, 0);
    merged.set(chunk, this.bridgeBuffer.length);
    this.bridgeBuffer = merged;
    let guard: number = 0;
    while (this.bridgeBuffer.length >= 4 && guard < 64) {
      guard++;
      const b: Uint8Array = this.bridgeBuffer;
      const len: number = ((b[0] << 24) | (b[1] << 16) | (b[2] << 8) | b[3]) >>> 0;
      if (len === 0 || len > 65536) {
        this.bridgeBuffer = new Uint8Array(0);
        return;
      }
      if (b.length < 4 + len) {
        return;
      }
      const payload: Uint8Array = b.slice(4, 4 + len);
      this.bridgeBuffer = b.slice(4 + len);
      this.onBridgeJson(this.decodeBytes(payload));
    }
  }

  private onBridgeJson(text: string): void {
    let frame: CpBridgeFrame;
    try {
      frame = JSON.parse(text) as CpBridgeFrame;
    } catch (err) {
      return;
    }
    this.status.lampFrames++;
    const data: CpBridgeData | undefined = frame.data;
    if (data === undefined) {
      return;
    }
    const car: CpBridgeCarState | undefined = data.carState;
    if (car === undefined) {
      return;
    }
    if (car.trafficLightColor !== undefined) {
      this.status.lampColor = car.trafficLightColor;
    }
    if (car.stopLineDist !== undefined) {
      this.status.lampStopLine = car.stopLineDist;
    }
  }

  // ------------------------------------------------------------------ shutdown

  stop(): void {
    this.wanted = false;
    if (this.hbTimer !== -1) {
      clearInterval(this.hbTimer);
      this.hbTimer = -1;
    }
    if (this.probeTimer !== -1) {
      clearInterval(this.probeTimer);
      this.probeTimer = -1;
    }
    if (this.dataSocket !== null) {
      const udp: socket.UDPSocket = this.dataSocket;
      this.dataSocket = null;
      udp.close().catch(() => {
        // Closing a socket that never sent is allowed to fail quietly.
      });
    }
    for (let i = 0; i < this.streams.length; i++) {
      const ws: webSocket.WebSocket | null = this.streams[i].ws;
      if (ws !== null) {
        ws.close(() => {
          // Closing an already-dead stream is not an error worth a toast.
        });
      }
    }
    this.streams = [];
    if (this.controlSocket !== null) {
      const ws: webSocket.WebSocket = this.controlSocket;
      this.controlSocket = null;
      ws.close(() => {
      });
    }
    if (this.compactSocket !== null) {
      const ws: webSocket.WebSocket = this.compactSocket;
      this.compactSocket = null;
      ws.close(() => {
      });
    }
    if (this.bridgeSocket !== null) {
      const tcp: socket.TCPSocket = this.bridgeSocket;
      this.bridgeSocket = null;
      tcp.off('message');
      tcp.off('close');
      tcp.off('error');
      tcp.close().catch(() => {
        // A closed feed is not an error state worth surfacing.
      });
    }
    this.bridgeBuffer = new Uint8Array(0);
    this.manifest = null;
    this.status.link = 'idle';
    this.status.compact = 'closed';
    this.status.navi = 'idle';
    this.status.lamp = 'closed';
    this.status.lampColor = 0;
    this.status.lampStopLine = 0;
    this.status.naviStreamsOpen = 0;
    this.status.naviSession = '';
    this.status.compactFrames = 0;
    this.status.naviFrames = 0;
    this.log('已断开');
  }
}

/**
 * One client per process. ArkUI may rebuild the page (rotation, window resize,
 * ability restart) and a per-struct instance would then own a second socket set
 * while the first one keeps the link alive — the UI would read zeros forever.
 */
export const CP_CLIENT: CPlinkClient = new CPlinkClient();




