/**
 * Wire types for the two JSON services this app drives, plus the DSH web page.
 *
 * Field names mirror the servers' JSON exactly, snake_case included, so a payload
 * can be cast straight from the wire with no mapping layer:
 *   console  - /data/sunnypilot/tools/sp_console/server.py
 *   capture  - ~/.local/share/byd-acc-capture/capture.py
 *
 * Only the fields this app reads are declared. Extra keys on the wire are
 * ignored by the cast, which is deliberate: the capture tool's `rows` and
 * `per_message` payloads are large and change often, and nothing here needs them.
 */

/** GET /api/info on the sunnypilot console. */
export interface ConsoleInfo {
  token_required: boolean;
  lan_addresses: string[];
  port: number;
  bind_host: string;
  mdns_name: string;
}

/** GET /api/status on the sunnypilot console. */
export interface ConsoleStatus {
  running: boolean;
  pids: number[];
  starting: boolean;
  launcher: string;
  log: string;
}

/** GET /api/status on the BYD ACC capture tool. */
export interface CaptureStatus {
  capturing: boolean;
  source: string;
  source_detail: string;
  source_error: string;
  usb_held: boolean;
  mode: string;
  cereal_state: string;
  usb_always: boolean;
  rolling: boolean;
  elapsed: number;
  total: number;
  decoded_errors: number;
}

/**
 * GET /api/eps_rate, `samples`: live per-topic counters. Declared field by field
 * rather than as a Record so nothing has to index by a dynamic key.
 */
export interface RateSamples {
  wheel: number;
  cmd_tx: number;
  cmd_stock: number;
  cmd_active: number;
  eps: number;
  speed: number;
  paired: number;
}

/** One histogram row of GET /api/eps_rate. */
export interface RateBucket {
  n: number;
  p50: number | null;
  p90: number | null;
  p99: number | null;
  max: number | null;
  label: string;
  faults: number;
}

/** GET /api/eps_rate. `faults.events` is intentionally not declared (unused). */
export interface EpsRate {
  samples: RateSamples;
  commander: string;
  by_speed: RateBucket[];
  by_lead: RateBucket[];
  faults: FaultReport;
}

export interface FaultReport {
  n: number;
}

/** One recorded CSV in GET /api/captures. */
export interface CaptureFile {
  name: string;
  size: number;
  mtime: string;
  active: boolean;
}

/** GET /api/captures. */
export interface CaptureFiles {
  files: CaptureFile[];
  rolling: boolean;
  keep: number;
  addresses: string[];
  mdns_name: string;
}

/*
 * Empty values for the view state.
 *
 * The views hold these in @State instead of a `T | null` union: ArkUI documents a
 * fixed set of types its state decorators accept, and a union is the one form
 * whose acceptance could not be confirmed without a build. A separate `loaded`
 * flag carries the "no data yet" meaning that the union used to.
 */
export function emptyConsoleInfo(): ConsoleInfo {
  return { token_required: false, lan_addresses: [], port: 0, bind_host: '', mdns_name: '' };
}

export function emptyConsoleStatus(): ConsoleStatus {
  return { running: false, pids: [], starting: false, launcher: '', log: '' };
}

export function emptyCaptureStatus(): CaptureStatus {
  return {
    capturing: false, source: '', source_detail: '', source_error: '', usb_held: false,
    mode: 'auto', cereal_state: '', usb_always: false, rolling: false, elapsed: 0,
    total: 0, decoded_errors: 0
  };
}

export function emptyEpsRate(): EpsRate {
  return {
    samples: { wheel: 0, cmd_tx: 0, cmd_stock: 0, cmd_active: 0, eps: 0, speed: 0, paired: 0 },
    commander: '', by_speed: [], by_lead: [], faults: { n: 0 }
  };
}

export function emptyCaptureFiles(): CaptureFiles {
  return { files: [], rolling: false, keep: 0, addresses: [], mdns_name: '' };
}

/*
 * The mirrored sunnypilot settings (GET /api/sp_settings).
 *
 * The console parses these out of the car's own settings pages, so this app offers
 * exactly what the car's screen offers. Each item carries its own current value
 * rather than a second, key-addressed table: one structure to render, and no lookup
 * by a dynamic key.
 */

/** One control on a mirrored settings page. */
export interface SpSettingItem {
  key: string;
  /** 'bool' | 'int' | 'text'. A `text` row is read-only information. */
  type: string;
  title: string;
  /** For a `text` row this is the value the car's own page displays. */
  description: string;
  min: number | null;
  max: number | null;
  step: number;
  unit: string;
  /** Decimal shift between the stored integer and the shown value (10 = one decimal). */
  scale: number;
  /** Button labels for a selector; the stored value is the button's index. */
  options: string[];
  read_only: boolean;
  writable: boolean;
  default: string;
  flags: string;
  unknown_key: boolean;
  source: string;
  value: boolean | number | null;
  unset: boolean;
  at_default: boolean;
}

export interface SpSettingGroup {
  id: string;
  title: string;
  subtitle: string;
  source: string;
  items: SpSettingItem[];
}

export interface SpManifest {
  repo: string;
  settings_dir: string;
  params_keys_h: string;
  known_keys: number;
  groups: SpSettingGroup[];
}

/** GET /api/sp_settings. */
export interface SpSettingsPayload {
  stack_running: boolean;
  editable: boolean;
  editable_hint: string;
  manifest: SpManifest;
}

/** The body of POST /api/sp_settings. `value: null` unsets the key. */
export interface SpWriteBody {
  key: string;
  value: boolean | number | null;
}

/** POST /api/sp_settings `result`: what was actually stored. */
export interface SpWriteResult {
  key: string;
  requested: boolean | number | null;
  applied: boolean;
  clamped: boolean;
  value: boolean | number | null;
  type: string;
}

export interface SpWriteResponse {
  result: SpWriteResult;
}

/** Every failing endpoint answers with this field. */
export interface ErrorResponse {
  error: string;
}

export function emptySpSettings(): SpSettingsPayload {
  return {
    stack_running: false, editable: false, editable_hint: '',
    manifest: { repo: '', settings_dir: '', params_keys_h: '', known_keys: 0, groups: [] }
  };
}

/*
 * The host's own power (GET /api/host/power, POST /api/host/reboot|poweroff).
 *
 * Two flattenings are deliberate. The console answers with one readiness flag and
 * one reason per action instead of a map, because ArkTS cannot index an object by
 * a dynamic key; and the reason is already a finished Chinese sentence, written by
 * the host, because only the host can tell "the car is moving" from "logind wants a
 * password" - the app must not guess either.
 */
export interface HostPowerStatus {
  can_reboot: boolean;
  reason_reboot: string;
  can_poweroff: boolean;
  reason_poweroff: string;
  delay_s: number;
  stack_running: boolean;
}

/** The console accepted the request and has scheduled the action. */
export interface HostPowerResult {
  action: string;
  label: string;
  accepted: boolean;
  execute_in: number;
  method: string;
  scheduled: string;
  cancel: string;
  stack_running: boolean;
}

/** POST body: the typed confirmation the console demands. */
export interface HostPowerBody {
  confirm: string;
}

export function emptyHostPowerStatus(): HostPowerStatus {
  return {
    can_reboot: true, reason_reboot: '', can_poweroff: true, reason_poweroff: '',
    delay_s: 5, stack_running: false
  };
}
