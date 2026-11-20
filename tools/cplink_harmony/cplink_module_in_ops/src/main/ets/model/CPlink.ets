/**
 * CPlink (CP搭子 / navipilot) wire constants and frame builders.
 *
 * Everything here is lifted from jixiexiaoge/navipilot so the HarmonyOS client
 * speaks exactly the same protocol as the Android app:
 *   UDP 7705  device -> phone   discovery broadcast (JSON, carries `ip`)
 *   UDP 7706  phone -> device   heartbeat / command / destination
 *   HTTP 7000 /api/heartbeat_status  handshake, must answer 200 + ok=true
 *   WS   7000 /ws/compact_state      vehicle telemetry
 *   WS   7714 /api/navi/ws/v2/...    navigation v2 (control + per-stream sockets)
 *
 * No byte of this file talks to the network; it only builds and parses frames, so
 * the client below stays readable and the frames stay testable.
 */

export const CP_DISCOVERY_PORT: number = 7705;
export const CP_DATA_PORT: number = 7706;
export const CP_HTTP_PORT: number = 7000;
export const CP_BRIDGE_PORT: number = 7711;
export const CP_NAVI_PORT: number = 7714;

export const CP_PROTOCOL_VERSION: number = 2;
export const CP_CATALOG_REVISION: number = 1;
export const CP_APP_VERSION: string = 'amap_auto_1.0';

/** The 13 JSON streams the sender actually publishes (carrot_navi catalog, json kind). */
export const CP_JSON_STREAMS: string[] = [
  'vehicle', 'guidance_current', 'guidance_next', 'lane_current',
  'lane_ahead', 'speed', 'traffic_signal', 'crossroad', 'route',
  'navigation_status', 'app_status', 'camera_state', 'composition_state'
];

/** Image streams: the receiver's manifest requires the full catalog, all disabled. */
export const CP_IMAGE_STREAMS: string[] = [
  'tbt_current_compact', 'tbt_current_full', 'tbt_next',
  'traffic_signal', 'lane_top', 'lane_bottom',
  'safety_primary', 'safety_secondary', 'safety_section',
  'crossroad_minimized', 'crossroad_expanded',
  'center_tbt_icon', 'center_tbt_text', 'center_tbt_fee'
];

/** Render streams: same rule as image. */
export const CP_RENDER_STREAMS: string[] = ['map_main'];

const CP_DELIVERY_MODES: string[] = ['on_change', 'periodic', 'on_change_with_heartbeat'];

/** One device found by the 7705 broadcast or by the fallback HTTP sweep. */
export class CpDevice {
  ip: string = '';
  name: string = '';
  port: number = CP_HTTP_PORT;
  source: string = 'broadcast';
  seenAt: number = 0;

  constructor(ip: string, source: string) {
    this.ip = ip;
    this.source = source;
    this.seenAt = Date.now();
  }
}

/** Mutable snapshot the view polls; the client owns the single instance. */
export class CpStatus {
  /** Diagnostic: distinguishes two client instances if one ever exists twice. */
  cid: number = Math.floor(Math.random() * 100000);
  /** idle | discovering | handshaking | connected | error */
  link: string = 'idle';
  detail: string = '';
  host: string = '';
  hbSent: number = 0;
  hbOk: number = 0;
  hbFail: number = 0;
  hbRttMs: number = 0;
  /** closed | connecting | open */
  compact: string = 'closed';
  compactFrames: number = 0;
  compactLast: string = '';
  /** idle | connecting | negotiating | connected | error */
  navi: string = 'idle';
  naviSession: string = '';
  naviStreamsOpen: number = 0;
  naviStreamsTotal: number = 0;
  naviFrames: number = 0;
  params: string = '';
  /** 7711 CAN bridge: closed | connecting | open | error */
  lamp: string = 'closed';
  lampColor: number = 0;
  lampStopLine: number = 0;
  lampFrames: number = 0;
  /** Last command pushed over UDP 7706, for the operator's own eyes only. */
  lastCommand: string = '';
}

/** 7711 frames: 4-byte big-endian length, then UTF-8 JSON. */
export interface CpBridgeCarState {
  stopLineDist?: number;
  trafficLightColor?: number;
}

export interface CpBridgeData {
  carState?: CpBridgeCarState;
}

export interface CpBridgeFrame {
  version?: number;
  sequence?: number;
  data?: CpBridgeData;
}

/** UDP 7706 heartbeat: the only field set carrot_man insists on besides the clock. */
export function cpHeartbeatFrame(index: number): string {
  const now: number = Math.floor(Date.now() / 1000);
  return `{"carrotIndex":${index},"epochTime":${now},"timezone":"Asia/Shanghai"}`;
}

/** UDP 7706 explicit command packet (`carrotCmd`), e.g. "SPEED 80". */
export function cpCommandFrame(index: number, command: string, arg: string): string {
  const now: number = Math.floor(Date.now() / 1000);
  return `{"carrotIndex":${index},"epochTime":${now},"timezone":"Asia/Shanghai",` +
    `"carrotCmdIndex":${index},"carrotCmd":${JSON.stringify(command)},` +
    `"carrotArg":${JSON.stringify(arg)}}`;
}

function cpSupportedParams(isJson: boolean): string {
  if (!isJson) {
    return '{}';
  }
  const modes: string[] = [];
  for (let i = 0; i < CP_DELIVERY_MODES.length; i++) {
    modes.push(JSON.stringify(CP_DELIVERY_MODES[i]));
  }
  return `{"delivery_mode":[${modes.join(',')}],` +
    `"interval_ms":{"min":200,"max":5000,"default":1000},` +
    `"stale_timeout_ms":{"min":1,"max":2147483647,"default":10000}}`;
}

/**
 * `requirements_query` sent on the control socket right after it opens.
 *
 * The receiver answers this with a `subscription_manifest`; it never pushes a
 * manifest on its own, so a client that stays silent looks connected and does
 * nothing.
 */
export function cpRequirementsQuery(): string {
  const entries: string[] = [];
  for (let i = 0; i < CP_JSON_STREAMS.length; i++) {
    const name: string = CP_JSON_STREAMS[i];
    entries.push(`{"kind":"json","name":${JSON.stringify(name)},"schema_version":1,` +
      `"nullable":true,"supported":true,"supported_params":${cpSupportedParams(true)}}`);
  }
  for (let i = 0; i < CP_IMAGE_STREAMS.length; i++) {
    const name: string = CP_IMAGE_STREAMS[i];
    entries.push(`{"kind":"image","name":${JSON.stringify(name)},"schema_version":1,` +
      `"nullable":true,"supported":false,"supported_params":{}}`);
  }
  for (let i = 0; i < CP_RENDER_STREAMS.length; i++) {
    const name: string = CP_RENDER_STREAMS[i];
    entries.push(`{"kind":"render","name":${JSON.stringify(name)},"schema_version":1,` +
      `"nullable":true,"supported":false,"supported_params":{}}`);
  }
  return `{"type":"requirements_query","protocol_version":${CP_PROTOCOL_VERSION},` +
    `"timestamp_ms":${Date.now()},"app_version":${JSON.stringify(CP_APP_VERSION)},` +
    `"catalog_revision":${CP_CATALOG_REVISION},` +
    `"limits":{"max_binary_frame_bytes":0,"max_total_bitrate_kbps":12000},` +
    `"streams":[${entries.join(',')}]}`;
}

/** `manifest_applied`: the receiver closes the control socket if this never arrives. */
export function cpManifestApplied(sessionId: string, revision: number,
                                  streams: CpManifestStream[]): string {
  const applied: string[] = [];
  for (let i = 0; i < streams.length; i++) {
    const s: CpManifestStream = streams[i];
    applied.push(`{"kind":${JSON.stringify(s.kind)},"name":${JSON.stringify(s.name)},` +
      `"schema_version":${s.schema_version},"stream_handle":${s.stream_handle},` +
      `"enabled":${s.enabled},"params":{}}`);
  }
  return `{"type":"manifest_applied","protocol_version":${CP_PROTOCOL_VERSION},` +
    `"timestamp_ms":${Date.now()},"session_id":${JSON.stringify(sessionId)},` +
    `"revision":${revision},"effective_config":[${applied.join(',')}]}`;
}

/** One entry of the receiver's `subscription_manifest`. */
export interface CpManifestStream {
  kind: string;
  name: string;
  schema_version: number;
  stream_handle: number;
  enabled: boolean;
}

/** The manifest itself, in the shape carrier carrots send. */
export interface CpManifest {
  type: string;
  protocol_version: number;
  session_id: string;
  revision: number;
  streams: CpManifestStream[];
}

/**
 * `item_update` envelope for one JSON stream.
 *
 * `present=false` is the cheap handshake probe: the receiver counts a frame, the
 * car sees nothing, and the required `reason` documents why the value is absent.
 */
export function cpItemUpdate(sessionId: string, revision: number, stream: CpManifestStream,
                             sequence: number, present: boolean, valueJson: string,
                             reason: string): string {
  const value: string = present ? valueJson : 'null';
  const tail: string = present ? '' : `,"reason":${JSON.stringify(reason.slice(0, 64))}`;
  return `{"type":"item_update","protocol_version":${CP_PROTOCOL_VERSION},` +
    `"session_id":${JSON.stringify(sessionId)},"kind":"json",` +
    `"name":${JSON.stringify(stream.name)},"manifest_revision":${revision},` +
    `"stream_handle":${stream.stream_handle},"schema_version":1,` +
    `"sequence":${sequence},"source_timestamp_ms":${Date.now()},"sent_at_ms":${Date.now()},` +
    `"present":${present},"value":${value}${tail}}`;
}


/**
 * Flat snapshot of everything the page draws.
 *
 * The client serialises it into AppStorage and the view watches that key: writes
 * to @State from a timer callback were silently dropped by this runtime, while
 * AppStorage updates always re-render.
 */
export class CpSnap {
  link: string = 'idle';
  detail: string = '';
  hbSent: number = 0;
  hbOk: number = 0;
  hbFail: number = 0;
  hbRtt: number = 0;
  compact: string = 'closed';
  compactFrames: number = 0;
  compactLast: string = '';
  navi: string = 'idle';
  naviSession: string = '';
  naviOpen: number = 0;
  naviTotal: number = 0;
  naviFrames: number = 0;
  params: string = '';
  lamp: string = 'closed';
  lampColor: number = 0;
  lampStop: number = 0;
  lampFrames: number = 0;
  lastCommand: string = '';
  devices: CpDevice[] = [];
  logs: string = '';
  tick: number = 0;
  cid: number = 0;
}
