/*
 * ESP32-C3 Super Mini IMU/GNSS bridge
 *
 * Wiring (matches the 2026-10-21 diagram):
 *   LSM6DS3  I2C: SDA=GPIO0, SCL=GPIO1, address 0x6A (SA0 low)
 *   UBLOC-M9N UART: ESP32 RX=GPIO4, TX=GPIO3, 38400 8N1
 *   Host link: native USB serial/JTAG at 115200 (CDC line rate is nominal)
 *
 * Output is newline-delimited JSON over USB:
 *   {"type":"hello",...}
 *   {"type":"status",...}          (USB only, every 10 s)
 *   {"type":"imu","seq":..,"t":..,"ax":..,"ay":..,"az":..,"gx":..,"gy":..,"gz":..,"temp":..}
 *   {"type":"gnss","t":..,"fix":..,"sats":..,"lat":..,"lon":..,"alt":..,"speed_mps":..,"course":..,"hdop":..,"utc":"...","stale":..,"age_ms":..}
 *   {"type":"gnss_status",...}
 *   {"type":"gnss_locked",...}
 *   {"type":"error","where":"imu","msg":"..."}
 *
 * BLE uses the compact fixed 37-byte binary IMU packet (type 0x01); JSON
 * messages are also mirrored to BLE when connected (low priority).
 *
 * Axes are in the same accel_gyro_y_inverted frame as the CH347 calibrator:
 * chip Y is negated for both accelerometer and gyroscope. X/Z are chip-native.
 *
 * 2026-10-23 v1.1.0 (optimization after the 17:43 reboot):
 *   - hello/status now carry reset reason, boot count, heap and counters, so
 *     the next unexplained reset can be classified (panic/WDT/brownout/...).
 *   - GNSS baud detection is non-blocking and cached in NVS; the old setup
 *     blocked up to 12 s before the first IMU sample/hello.
 *   - BLE is throttled to two notifications per 5 ms (was one per 10 ms), so
 *     the default 23-byte MTU can still carry 100 Hz IMU plus GNSS; the buffer
 *     has drop/overflow counters and a critical section.
 *   - GNSS data expires after 3 s; a dead UART no longer reports a stale fix
 *     forever.
 *   - USB hot-plug re-sends hello (the old firmware only sent it at boot).
 *   - USB TX ring is 4096 bytes and long lines are retried on short writes;
 *     the default 256-byte ring truncated status/GNSS JSON at ~247 bytes.
 *   - The first LSM6DS3 probe is retried after reset (it can need a few ms).
 *   - Loop task watchdog is armed; I2C transaction timeout is bounded.
 *   - Removed dead UBX-CFG-NAV5 helpers (the M9N stays at its known-good
 *     default configuration).
 */
#include <Arduino.h>
#include <Wire.h>
#include <math.h>
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <BLE2902.h>
#include <Preferences.h>
#include <esp_system.h>
#include <esp_task_wdt.h>

#define FW_NAME "esp32c3-imu-gnss"
#define FW_VERSION "1.1.0"
#define BUILD_STAMP __DATE__ " " __TIME__

static constexpr uint8_t LSM_ADDR = 0x6A;
static constexpr int PIN_I2C_SDA = 0;
static constexpr int PIN_I2C_SCL = 1;
static constexpr int PIN_GNSS_RX = 4;   // ESP32 RX <- M9N TX
static constexpr int PIN_GNSS_TX = 3;   // ESP32 TX -> M9N RX
static constexpr uint32_t SAMPLE_INTERVAL_MS = 10;  // ~100 Hz reports, sensor ODR 104 Hz
static constexpr uint32_t STATUS_INTERVAL_MS = 10000;
static constexpr uint32_t GNSS_EMIT_MS = 1000;
static constexpr uint32_t GNSS_STATUS_MS = 2000;
static constexpr uint32_t GNSS_STALE_MS = 3000;
static constexpr uint32_t GNSS_FIRST_DWELL_MS = 1800;
static constexpr uint32_t GNSS_SCAN_DWELL_MS = 1200;

// BLE Nordic UART Service (NUS). USB CDC remains available at the same time;
// the host is expected to prefer USB and use BLE only when no /dev/ttyACM0.
static constexpr char BLE_NAME[] = "ESP32C3-IMU";
static constexpr char BLE_SERVICE_UUID[] = "6E400001-B5A3-F393-E0A9-E50E24DCCA9E";
static constexpr char BLE_TX_UUID[] = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E";
static constexpr char BLE_RX_UUID[] = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E";
static BLECharacteristic *ble_tx = nullptr;
static volatile bool ble_connected = false;

HardwareSerial gnss(1);

struct GnssState {
  bool has_data = false;
  bool valid = false;
  uint8_t fix = 0;
  uint8_t sats = 0;
  double lat = 0, lon = 0, alt = 0, speed_mps = 0, course = 0, hdop = 0;
  char utc[16] = "";
  uint32_t last_update = 0;
} gps;

static bool imu_ok = false;
static uint32_t imu_seq = 0;
static uint32_t imu_window_start = 0;
static uint32_t imu_window_count = 0;
static float imu_hz = 0.0f;
static uint32_t imu_read_errors = 0;
static uint32_t imu_recoveries = 0;
static uint32_t next_imu_ms = 0;
static uint32_t next_imu_retry_ms = 0;

static uint32_t last_gnss_emit_ms = 0;
static uint32_t next_status_ms = 0;
static uint32_t gnss_baud = 38400;
static bool gnss_baud_detected = false;
static bool gnss_locked = false;
static uint32_t gnss_rx_bytes = 0;
static uint32_t gnss_baud_changes = 0;
static uint32_t gnss_rescans = 0;
static uint32_t nmea_lines = 0;
static uint32_t nmea_bad_checksum = 0;
static uint32_t nmea_gga = 0;
static uint32_t nmea_rmc = 0;
static char last_nmea[96] = "";
static char nmea[192];
static size_t nmea_len = 0;
static uint32_t gnss_open_ms = 0;
static uint32_t gnss_gga_rmc_at_open = 0;

static constexpr uint32_t GNSS_BAUDS[] = {38400, 9600, 115200, 57600, 4800, 230400};
static constexpr size_t GNSS_BAUD_COUNT = sizeof(GNSS_BAUDS) / sizeof(GNSS_BAUDS[0]);
static size_t gnss_candidate_index = 0;
static size_t gnss_initial_index = 0;
static bool gnss_scanned_initial = false;

static uint32_t boot_count = 0;
static uint32_t saved_baud = 0;
static bool usb_host = false;
static bool wdt_enabled = false;

static char ble_buffer[4096];
static uint8_t ble_chunk[520];
static size_t ble_buffer_len = 0;
static uint32_t ble_ok = 0;
static uint32_t ble_bytes = 0;
static uint32_t ble_drops = 0;
static uint32_t ble_skip = 0;
static uint32_t ble_overflow = 0;
static uint32_t ble_connects = 0;
static uint32_t ble_disconnects = 0;
static uint32_t ble_last_tx_ms = 0;
static portMUX_TYPE ble_mux = portMUX_INITIALIZER_UNLOCKED;

static bool lsm_write(uint8_t reg, uint8_t value) {
  Wire.beginTransmission(LSM_ADDR);
  Wire.write(reg);
  Wire.write(value);
  return Wire.endTransmission() == 0;
}

static bool lsm_read(uint8_t reg, uint8_t *buf, size_t len) {
  Wire.beginTransmission(LSM_ADDR);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom((uint8_t)LSM_ADDR, (uint8_t)len) != len) return false;
  for (size_t i = 0; i < len; i++) {
    if (!Wire.available()) return false;
    buf[i] = Wire.read();
  }
  return true;
}

static const char *reset_reason_str() {
  switch (esp_reset_reason()) {
    case ESP_RST_POWERON: return "poweron";
    case ESP_RST_EXT: return "ext";
    case ESP_RST_SW: return "sw";
    case ESP_RST_PANIC: return "panic";
    case ESP_RST_INT_WDT: return "int_wdt";
    case ESP_RST_TASK_WDT: return "task_wdt";
    case ESP_RST_WDT: return "wdt";
    case ESP_RST_DEEPSLEEP: return "deepsleep";
    case ESP_RST_BROWNOUT: return "brownout";
    case ESP_RST_SDIO: return "sdio";
    case ESP_RST_USB: return "usb";
    case ESP_RST_JTAG: return "jtag";
    case ESP_RST_EFUSE: return "efuse";
    case ESP_RST_PWR_GLITCH: return "power_glitch";
    case ESP_RST_CPU_LOCKUP: return "cpu_lockup";
    default: return "unknown";
  }
}

static void emit_usb_line(const char *line, size_t len) {
  if (!Serial) return;
  // HWCDC::write() returns a short count when the host is not draining its
  // ring fast enough (tx_timeout is 0 to avoid blocking the loop). A status or
  // GNSS line is longer than the default 256-byte ring, so retry the remainder
  // instead of silently truncating the JSON line.
  size_t sent = 0;
  uint32_t deadline = millis() + 200;
  while (sent < len && Serial) {
    size_t n = Serial.write((const uint8_t *)line + sent, len - sent);
    sent += n;
    if (sent < len) {
      if ((int32_t)(millis() - deadline) >= 0) break;
      delay(1);
    }
  }
  for (int i = 0; i < 20 && Serial; i++) {
    if (Serial.write('\n') != 0) break;
    delay(1);
  }
}

static void ble_append_raw(const uint8_t *data, size_t len, bool low_priority) {
  if (!ble_connected || ble_tx == nullptr || len == 0) return;
  portENTER_CRITICAL(&ble_mux);
  if (low_priority && ble_buffer_len + len > (sizeof(ble_buffer) * 3) / 4) {
    ble_skip += len;
    portEXIT_CRITICAL(&ble_mux);
    return;
  }
  if (ble_buffer_len + len > sizeof(ble_buffer)) {
    // Keep the newest half rather than blocking the IMU loop on a slow peer.
    size_t keep = sizeof(ble_buffer) / 2;
    size_t drop = ble_buffer_len > keep ? ble_buffer_len - keep : 0;
    if (drop > 0) {
      memmove(ble_buffer, ble_buffer + drop, keep);
      ble_drops += drop;
    }
    ble_buffer_len = keep;
    ble_overflow++;
  }
  if (len > sizeof(ble_buffer) - ble_buffer_len) {
    ble_skip += len;
    portEXIT_CRITICAL(&ble_mux);
    return;
  }
  memcpy(ble_buffer + ble_buffer_len, data, len);
  ble_buffer_len += len;
  portEXIT_CRITICAL(&ble_mux);
}

static void ble_append_text(const char *line, size_t len) {
  ble_append_raw((const uint8_t *)line, len, true);
  ble_append_raw((const uint8_t *)"\n", 1, true);
}

static void output_line(const char *line, size_t len) {
  emit_usb_line(line, len);
  ble_append_text(line, len);
}

static void ble_flush() {
  if (!ble_connected || ble_tx == nullptr) return;
  uint32_t now = millis();
  // Two notifications per 5 ms: enough for 100 Hz IMU plus GNSS even when the
  // peer negotiated the default 23-byte MTU, but not a notification flood.
  if (now - ble_last_tx_ms < 5) return;
  ble_last_tx_ms = now;
  uint16_t mtu = BLEDevice::getMTU();
  size_t chunk_max = mtu > 23 ? (size_t)(mtu - 3) : 20;
  if (chunk_max > sizeof(ble_chunk)) chunk_max = sizeof(ble_chunk);
  for (int i = 0; i < 2 && ble_connected; i++) {
    size_t count = 0;
    portENTER_CRITICAL(&ble_mux);
    if (ble_buffer_len == 0) {
      portEXIT_CRITICAL(&ble_mux);
      break;
    }
    count = ble_buffer_len < chunk_max ? ble_buffer_len : chunk_max;
    memcpy(ble_chunk, ble_buffer, count);
    memmove(ble_buffer, ble_buffer + count, ble_buffer_len - count);
    ble_buffer_len -= count;
    portEXIT_CRITICAL(&ble_mux);
    ble_tx->setValue(ble_chunk, count);
    ble_tx->notify();
    ble_ok++;
    ble_bytes += count;
  }
}

class BleServerCallbacks : public BLEServerCallbacks {
  void onConnect(BLEServer *server) override {
    portENTER_CRITICAL(&ble_mux);
    ble_buffer_len = 0;
    ble_connected = true;
    portEXIT_CRITICAL(&ble_mux);
    ble_connects++;
    ble_last_tx_ms = millis();
    // Ask for a 7.5-15 ms connection interval so batched IMU frames can get
    // through without a multi-second backlog.
    server->requestConnParams(server->getConnId(), 0x06, 0x0C, 0, 400);
  }

  void onDisconnect(BLEServer *server) override {
    (void)server;
    portENTER_CRITICAL(&ble_mux);
    ble_connected = false;
    ble_buffer_len = 0;
    portEXIT_CRITICAL(&ble_mux);
    ble_disconnects++;
    BLEDevice::startAdvertising();
  }
};

static void ble_init() {
  BLEDevice::init(BLE_NAME);
  BLEDevice::setMTU(517);
  // Explicit +9 dBm; ESP32-C3 defaults to this, but keep it deterministic.
  BLEDevice::setPower(ESP_PWR_LVL_P9);
  BLEServer *server = BLEDevice::createServer();
  server->setCallbacks(new BleServerCallbacks());
  BLEService *service = server->createService(BLE_SERVICE_UUID);
  ble_tx = service->createCharacteristic(BLE_TX_UUID, BLECharacteristic::PROPERTY_NOTIFY);
  ble_tx->addDescriptor(new BLE2902());
  service->createCharacteristic(BLE_RX_UUID, BLECharacteristic::PROPERTY_WRITE | BLECharacteristic::PROPERTY_WRITE_NR);
  service->start();
  BLEAdvertising *advertising = BLEDevice::getAdvertising();
  advertising->addServiceUUID(BLE_SERVICE_UUID);
  advertising->setScanResponse(true);
  advertising->setMinPreferred(0x06);
  advertising->setMaxPreferred(0x12);
  BLEDevice::startAdvertising();
}

static bool lsm_init() {
  uint8_t who = 0;
  if (!lsm_read(0x0F, &who, 1)) return false;
  if (who != 0x69) {
    char msg[96];
    int n = snprintf(msg, sizeof(msg),
                     "{\"type\":\"error\",\"where\":\"imu\",\"msg\":\"WHO_AM_I=0x%02X (expected 0x69)\"}",
                     who);
    if (n > 0 && (size_t)n < sizeof(msg)) output_line(msg, n);
    return false;
  }
  bool ok = lsm_write(0x12, 0x44)  // BDU + auto-increment
         && lsm_write(0x10, 0x60)  // 104 Hz, +/-2 g
         && lsm_write(0x11, 0x60); // 104 Hz, +/-250 dps
  if (!ok) {
    const char err[] = "{\"type\":\"error\",\"where\":\"imu\",\"msg\":\"register init failed\"}";
    output_line(err, strlen(err));
    return false;
  }
  return true;
}

static bool lsm_sample(double &ax, double &ay, double &az,
                       double &gx, double &gy, double &gz, double &temp_c) {
  uint8_t b[14];  // 0x20 temp(2) + 0x22 gyro(6) + 0x28 accel(6), auto-increment
  if (!lsm_read(0x20, b, sizeof(b))) return false;
  int16_t temp_raw = (int16_t)((b[1] << 8) | b[0]);
  int16_t gx_raw = (int16_t)((b[3] << 8) | b[2]);
  int16_t gy_raw = (int16_t)((b[5] << 8) | b[4]);
  int16_t gz_raw = (int16_t)((b[7] << 8) | b[6]);
  int16_t ax_raw = (int16_t)((b[9] << 8) | b[8]);
  int16_t ay_raw = (int16_t)((b[11] << 8) | b[10]);
  int16_t az_raw = (int16_t)((b[13] << 8) | b[12]);
  ax = ax_raw * 0.000061;
  ay = -ay_raw * 0.000061;  // Y inverted calibration frame
  az = az_raw * 0.000061;
  gx = gx_raw * 0.00875;
  gy = -gy_raw * 0.00875;   // Y inverted calibration frame
  gz = gz_raw * 0.00875;
  temp_c = temp_raw / 16.0 + 25.0;
  return true;
}

static void emit_imu(double ax, double ay, double az, double gx, double gy, double gz, double temp_c) {
  uint32_t seq = imu_seq++;
  uint32_t now = millis();
  if (Serial) {
    char line[224];
    int n = snprintf(line, sizeof(line),
                     "{\"type\":\"imu\",\"seq\":%lu,\"t\":%lu,"
                     "\"ax\":%.5f,\"ay\":%.5f,\"az\":%.5f,"
                     "\"gx\":%.4f,\"gy\":%.4f,\"gz\":%.4f,\"temp\":%.2f}",
                     (unsigned long)seq, (unsigned long)now,
                     ax, ay, az, gx, gy, gz, temp_c);
    if (n > 0 && (size_t)n < sizeof(line)) emit_usb_line(line, n);
  }
  // BLE uses a compact fixed-size binary packet (type 0x01) so 100 Hz fits
  // comfortably; USB keeps the human-readable JSON above.
  uint8_t packet[37];
  packet[0] = 0x01;
  memcpy(packet + 1, &seq, 4);
  memcpy(packet + 5, &now, 4);
  float values[7] = {(float)ax, (float)ay, (float)az, (float)gx, (float)gy, (float)gz, (float)temp_c};
  memcpy(packet + 9, values, sizeof(values));
  ble_append_raw(packet, sizeof(packet), false);
}

static bool nmea_checksum_ok(const char *line) {
  const char *star = strchr(line, '*');
  if (star == nullptr) return true;  // tolerate stripped checksums
  uint8_t sum = 0;
  for (const char *p = line + 1; p < star; p++) sum ^= (uint8_t)*p;
  if (star[1] == '\0' || star[2] == '\0') return false;
  char hex[3] = {star[1], star[2], 0};
  return (uint8_t)strtol(hex, nullptr, 16) == sum;
}

static double nmea_deg(const char *value, const char *hemisphere) {
  if (value == nullptr || *value == '\0') return 0;
  double raw = atof(value);
  int degrees = (int)(raw / 100.0);
  double minutes = raw - degrees * 100.0;
  double result = degrees + minutes / 60.0;
  if (hemisphere != nullptr && (*hemisphere == 'S' || *hemisphere == 'W')) result = -result;
  return result;
}

static void emit_gnss() {
  char line[640];
  uint32_t now = millis();
  uint32_t age = now - gps.last_update;
  bool stale = age > GNSS_STALE_MS;
  uint32_t fix = stale ? 0 : gps.fix;
  uint32_t sats = stale ? 0 : gps.sats;
  int n = snprintf(line, sizeof(line),
                   "{\"type\":\"gnss\",\"t\":%lu,\"fix\":%lu,\"sats\":%lu,"
                   "\"lat\":%.7f,\"lon\":%.7f,\"alt\":%.2f,"
                   "\"speed_mps\":%.3f,\"course\":%.2f,\"hdop\":%.2f,"
                   "\"utc\":\"%s\",\"stale\":%s,\"age_ms\":%lu,"
                   "\"baud\":%lu,\"rx_bytes\":%lu,"
                   "\"ble_mtu\":%u,\"ble_ok\":%lu,\"ble_bytes\":%lu,"
                   "\"ble_drops\":%lu,\"ble_skip\":%lu,\"ble_overflow\":%lu,"
                   "\"ble_buf\":%lu,\"last\":\"%s\"}",
                   (unsigned long)now, (unsigned long)fix, (unsigned long)sats,
                   gps.lat, gps.lon, gps.alt, gps.speed_mps, gps.course, gps.hdop, gps.utc,
                   stale ? "true" : "false", (unsigned long)age,
                   (unsigned long)gnss_baud, (unsigned long)gnss_rx_bytes,
                   (unsigned)BLEDevice::getMTU(), (unsigned long)ble_ok,
                   (unsigned long)ble_bytes, (unsigned long)ble_drops,
                   (unsigned long)ble_skip, (unsigned long)ble_overflow,
                   (unsigned long)ble_buffer_len, last_nmea);
  if (n > 0 && (size_t)n < sizeof(line)) output_line(line, n);
}

static void emit_gnss_status() {
  char line[384];
  int n = snprintf(line, sizeof(line),
                   "{\"type\":\"gnss_status\",\"t\":%lu,\"fix\":0,\"sats\":0,\"stale\":true,"
                   "\"rx_bytes\":%lu,\"baud\":%lu,\"locked\":%s,\"detected\":%s,"
                   "\"lines\":%lu,\"bad\":%lu,\"gga\":%lu,\"rmc\":%lu,\"last\":\"%s\"}",
                   (unsigned long)millis(), (unsigned long)gnss_rx_bytes,
                   (unsigned long)gnss_baud, gnss_locked ? "true" : "false",
                   gnss_baud_detected ? "true" : "false",
                   (unsigned long)nmea_lines, (unsigned long)nmea_bad_checksum,
                   (unsigned long)nmea_gga, (unsigned long)nmea_rmc, last_nmea);
  if (n > 0 && (size_t)n < sizeof(line)) output_line(line, n);
}

static void emit_hello() {
  if (!Serial) return;
  char line[448];
  int n = snprintf(line, sizeof(line),
                   "{\"type\":\"hello\",\"fw\":\"%s\",\"version\":\"%s\",\"build\":\"%s\","
                   "\"i2c\":[%d,%d],\"imu_addr\":\"0x6A\",\"whoami\":\"0x69\","
                   "\"gnss_uart\":[%d,%d],\"gnss_baud\":%lu,\"gnss_baud_detected\":%s,"
                   "\"gnss_locked\":%s,\"frame\":\"accel_gyro_y_inverted\",\"rate_hz\":104,"
                   "\"boot\":%lu,\"reset\":\"%s\",\"heap\":%lu,\"min_heap\":%lu,\"idf\":\"%s\"}",
                   FW_NAME, FW_VERSION, BUILD_STAMP,
                   PIN_I2C_SDA, PIN_I2C_SCL, PIN_GNSS_RX, PIN_GNSS_TX,
                   (unsigned long)gnss_baud, gnss_baud_detected ? "true" : "false",
                   gnss_locked ? "true" : "false",
                   (unsigned long)boot_count, reset_reason_str(),
                   (unsigned long)ESP.getFreeHeap(), (unsigned long)ESP.getMinFreeHeap(),
                   ESP.getSdkVersion());
  if (n > 0 && (size_t)n < sizeof(line)) emit_usb_line(line, n);
}

static void emit_status(uint32_t now) {
  if (!Serial) return;
  uint32_t gnss_age = gps.has_data ? (now - gps.last_update) : 0;
  char line[768];
  int n = snprintf(line, sizeof(line),
                   "{\"type\":\"status\",\"t\":%lu,\"up_ms\":%lu,\"boot\":%lu,\"reset\":\"%s\","
                   "\"heap\":%lu,\"min_heap\":%lu,\"wdt\":%s,"
                   "\"imu_ok\":%s,\"imu_hz\":%.1f,\"imu_seq\":%lu,\"imu_err\":%lu,\"imu_rec\":%lu,"
                   "\"ble\":%s,\"ble_mtu\":%u,\"ble_connects\":%lu,\"ble_disconnects\":%lu,"
                   "\"ble_ok\":%lu,\"ble_bytes\":%lu,\"ble_drops\":%lu,\"ble_skip\":%lu,"
                   "\"ble_overflow\":%lu,\"ble_buf\":%lu,"
                   "\"gnss_baud\":%lu,\"gnss_locked\":%s,\"gnss_detected\":%s,"
                   "\"gnss_has\":%s,\"gnss_age_ms\":%lu,\"gnss_fix\":%u,\"gnss_sats\":%u,"
                   "\"nmea\":%lu,\"nmea_bad\":%lu,\"gga\":%lu,\"rmc\":%lu,"
                   "\"baud_changes\":%lu,\"gnss_rescans\":%lu}",
                   (unsigned long)now, (unsigned long)now, (unsigned long)boot_count,
                   reset_reason_str(), (unsigned long)ESP.getFreeHeap(),
                   (unsigned long)ESP.getMinFreeHeap(), wdt_enabled ? "true" : "false",
                   imu_ok ? "true" : "false", (double)imu_hz, (unsigned long)imu_seq,
                   (unsigned long)imu_read_errors, (unsigned long)imu_recoveries,
                   ble_connected ? "true" : "false", (unsigned)BLEDevice::getMTU(),
                   (unsigned long)ble_connects, (unsigned long)ble_disconnects,
                   (unsigned long)ble_ok, (unsigned long)ble_bytes, (unsigned long)ble_drops,
                   (unsigned long)ble_skip, (unsigned long)ble_overflow,
                   (unsigned long)ble_buffer_len,
                   (unsigned long)gnss_baud, gnss_locked ? "true" : "false",
                   gnss_baud_detected ? "true" : "false",
                   gps.has_data ? "true" : "false", (unsigned long)gnss_age,
                   (unsigned)gps.fix, (unsigned)gps.sats,
                   (unsigned long)nmea_lines, (unsigned long)nmea_bad_checksum,
                   (unsigned long)nmea_gga, (unsigned long)nmea_rmc,
                   (unsigned long)gnss_baud_changes, (unsigned long)gnss_rescans);
  if (n > 0 && (size_t)n < sizeof(line)) emit_usb_line(line, n);
}

static void lock_gnss() {
  if (gnss_locked) return;
  gnss_locked = true;
  gnss_baud_detected = true;
  Preferences prefs;
  if (prefs.begin("imu", false)) {
    prefs.putUInt("baud", gnss_baud);
    prefs.end();
  }
  char line[160];
  int n = snprintf(line, sizeof(line),
                   "{\"type\":\"gnss_locked\",\"t\":%lu,\"baud\":%lu}",
                   (unsigned long)millis(), (unsigned long)gnss_baud);
  if (n > 0 && (size_t)n < sizeof(line)) output_line(line, n);
}

static void process_nmea(char *line) {
  if (line[0] != '$') return;
  nmea_lines++;
  strncpy(last_nmea, line, sizeof(last_nmea) - 1);
  last_nmea[sizeof(last_nmea) - 1] = '\0';
  if (!nmea_checksum_ok(line)) {
    nmea_bad_checksum++;
    return;
  }
  char *star = strchr(line, '*');
  if (star != nullptr) *star = '\0';
  if (strlen(line) < 7) return;
  const char *kind = line + 3;
  char *fields[24] = {nullptr};
  int count = 0;
  char *p = line;
  while (count < 24) {
    fields[count++] = p;
    char *comma = strchr(p, ',');
    if (comma == nullptr) break;
    *comma = '\0';
    p = comma + 1;
  }
  bool valid_sentence = false;
  if (!strncmp(kind, "GGA", 3) && count >= 10) {
    gps.fix = (uint8_t)atoi(fields[6]);
    gps.sats = (uint8_t)atoi(fields[7]);
    gps.hdop = atof(fields[8]);
    gps.alt = atof(fields[9]);
    gps.lat = nmea_deg(fields[2], fields[3]);
    gps.lon = nmea_deg(fields[4], fields[5]);
    if (fields[1] != nullptr) {
      strncpy(gps.utc, fields[1], sizeof(gps.utc) - 1);
      gps.utc[sizeof(gps.utc) - 1] = '\0';
    }
    gps.valid = gps.fix > 0;
    gps.has_data = true;
    gps.last_update = millis();
    nmea_gga++;
    valid_sentence = true;
  } else if (!strncmp(kind, "RMC", 3) && count >= 9) {
    bool active = fields[2] != nullptr && fields[2][0] == 'A';
    gps.speed_mps = atof(fields[7]) * 0.514444;
    gps.course = atof(fields[8]);
    if (active) {
      gps.lat = nmea_deg(fields[3], fields[4]);
      gps.lon = nmea_deg(fields[5], fields[6]);
      gps.valid = true;
    }
    gps.has_data = true;
    gps.last_update = millis();
    nmea_rmc++;
    valid_sentence = true;
  }
  if (valid_sentence && !gnss_locked) lock_gnss();
}

static void poll_gnss() {
  // Bound the work per loop so a wrong baud / noisy receiver cannot starve
  // the IMU, BLE and watchdog paths.
  uint16_t budget = 512;
  while (budget > 0 && gnss.available() > 0) {
    budget--;
    char c = (char)gnss.read();
    gnss_rx_bytes++;
    if (c == '\r') continue;
    if (c == '\n') {
      nmea[nmea_len] = '\0';
      if (nmea_len > 0) process_nmea(nmea);
      nmea_len = 0;
      continue;
    }
    if (nmea_len < sizeof(nmea) - 1) nmea[nmea_len++] = c;
    else nmea_len = 0;  // drop a runaway binary/UBX stream
  }
}

static size_t initial_baud_index() {
  for (size_t i = 0; i < GNSS_BAUD_COUNT; i++) {
    if (GNSS_BAUDS[i] == saved_baud) return i;
  }
  return 0;
}

static void gnss_open(uint32_t baud) {
  gnss.end();
  delay(20);
  gnss.begin(baud, SERIAL_8N1, PIN_GNSS_RX, PIN_GNSS_TX);
  gnss_baud = baud;
  gnss_open_ms = millis();
  nmea_len = 0;
  gnss_gga_rmc_at_open = nmea_gga + nmea_rmc;
  gnss_baud_changes++;
}

static void gnss_scan_tick() {
  if (gnss_locked) return;
  uint32_t now = millis();
  uint32_t dwell = gnss_scanned_initial ? GNSS_SCAN_DWELL_MS : GNSS_FIRST_DWELL_MS;
  if (now - gnss_open_ms < dwell) return;
  if (nmea_gga + nmea_rmc > gnss_gga_rmc_at_open) {
    lock_gnss();
    return;
  }
  gnss_scanned_initial = true;
  gnss_candidate_index = (gnss_candidate_index + 1) % GNSS_BAUD_COUNT;
  gnss_open(GNSS_BAUDS[gnss_candidate_index]);
}

static void load_boot_info() {
  Preferences prefs;
  if (!prefs.begin("imu", false)) return;
  boot_count = prefs.getUInt("boots", 0) + 1;
  prefs.putUInt("boots", boot_count);
  saved_baud = prefs.getUInt("baud", 0);
  prefs.end();
}

void setup() {
  Serial.begin(115200);
  // The default HWCDC TX ring is 256 bytes, smaller than a status/GNSS JSON
  // line; without this, long lines are truncated on the wire.
  Serial.setTxBufferSize(4096);
  Serial.setTxTimeoutMs(0);  // never block the loop when USB has no host
  delay(100);

  Wire.begin(PIN_I2C_SDA, PIN_I2C_SCL, 100000);
  Wire.setClock(100000);
  Wire.setTimeOut(50);  // a stuck I2C bus must not hang the loop forever

  load_boot_info();
  ble_init();

  // The LSM6DS3 can need a few ms after the ESP32 leaves reset; retry before
  // declaring it missing (the loop would otherwise emit a spurious error).
  imu_ok = false;
  for (int attempt = 0; attempt < 3 && !imu_ok; attempt++) {
    imu_ok = lsm_init();
    if (!imu_ok) delay(120);
  }

  gnss_initial_index = initial_baud_index();
  gnss_candidate_index = gnss_initial_index;
  gnss_open(GNSS_BAUDS[gnss_initial_index]);

  usb_host = (bool)Serial;
  if (usb_host) emit_hello();

  // Add the Arduino loop task to the task watchdog (5 s default). If the core
  // already subscribed it, esp_task_wdt_reset() succeeding tells us so.
  wdt_enabled = (esp_task_wdt_add(nullptr) == ESP_OK);
  if (!wdt_enabled) wdt_enabled = (esp_task_wdt_reset() == ESP_OK);

  next_imu_ms = millis();
  next_imu_retry_ms = millis() + 2000;
  next_status_ms = millis() + STATUS_INTERVAL_MS;
  imu_window_start = millis();
  last_gnss_emit_ms = millis();

  if (!imu_ok) {
    const char err[] = "{\"type\":\"error\",\"where\":\"imu\",\"msg\":\"LSM6DS3 not found at 0x6A (check SDA=GPIO0/SCL=GPIO1)\"}";
    output_line(err, strlen(err));
  }
}

void loop() {
  if (wdt_enabled) esp_task_wdt_reset();

  bool usb_now = (bool)Serial;
  if (usb_now && !usb_host) {
    usb_host = true;
    emit_hello();  // USB host appeared after boot: re-announce the protocol
  } else if (!usb_now) {
    usb_host = false;
  }

  poll_gnss();
  gnss_scan_tick();
  ble_flush();

  uint32_t now = millis();

  // A locked receiver that has produced no NMEA for 30 s (unplugged UART,
  // power glitch, or a different baud after a module swap) is rescanned.
  // A live receiver with no fix still emits NMEA every second, so it is not
  // disturbed by this path.
  if (gnss_locked && gps.has_data && (now - gps.last_update > 30000)) {
    gnss_locked = false;
    gnss_scanned_initial = false;
    gnss_candidate_index = gnss_initial_index;
    gnss_rescans++;
    gnss_open(GNSS_BAUDS[gnss_candidate_index]);
  }

  if ((int32_t)(now - next_imu_ms) >= 0) {
    next_imu_ms = now + SAMPLE_INTERVAL_MS;
    if (imu_ok) {
      double ax, ay, az, gx, gy, gz, temp_c;
      if (lsm_sample(ax, ay, az, gx, gy, gz, temp_c)) {
        emit_imu(ax, ay, az, gx, gy, gz, temp_c);
        imu_window_count++;
        uint32_t window = now - imu_window_start;
        if (window >= 1000) {
          imu_hz = (imu_window_count * 1000.0f) / (float)window;
          imu_window_count = 0;
          imu_window_start = now;
        }
      } else {
        imu_read_errors++;
        imu_ok = false;
        next_imu_retry_ms = now + 2000;
        const char err[] = "{\"type\":\"error\",\"where\":\"imu\",\"msg\":\"read failed\"}";
        output_line(err, strlen(err));
      }
    } else if ((int32_t)(now - next_imu_retry_ms) >= 0) {
      next_imu_retry_ms = now + 2000;
      imu_ok = lsm_init();
      if (imu_ok) {
        imu_recoveries++;
        const char note[] = "{\"type\":\"error\",\"where\":\"imu\",\"msg\":\"reconnected\"}";
        output_line(note, strlen(note));
      }
    }
  }

  if ((int32_t)(now - next_status_ms) >= 0) {
    next_status_ms = now + STATUS_INTERVAL_MS;
    emit_status(now);
  }

  if (gps.has_data) {
    if (now - last_gnss_emit_ms >= GNSS_EMIT_MS) {
      last_gnss_emit_ms = now;
      emit_gnss();
    }
  } else if (now - last_gnss_emit_ms >= GNSS_STATUS_MS) {
    last_gnss_emit_ms = now;
    emit_gnss_status();
  }

}
