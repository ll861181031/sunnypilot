# ESP32-C3 IMU/GNSS bridge

For the **ESP32-C3 Super Mini + LSM6DS3 + UBLOC-M9N** wiring (2026-10-21).

## Wiring

| Signal | ESP32-C3 |
|---|---|
| LSM6DS3 SDA | GPIO0 |
| LSM6DS3 SCL | GPIO1 |
| LSM6DS3 address | 0x6A (SA0 -> GND) |
| M9N TX -> ESP32 RX | GPIO4 |
| ESP32 TX -> M9N RX | GPIO3 |
| Host direct | native USB (CDC), 115200 nominal |
| Host wireless | BLE NUS, advertised as `ESP32C3-IMU` |

Power: ESP32 5V -> LSM6DS3 VIN (module LDO), GND common; M9N 5V/GND.
Do not power LSM6DS3 3V3 and VIN at the same time.

**BLE-only use requires separate 5V to the ESP32.** A power-only USB cable or
car 5V feed works; unplugging the only USB cable removes power and BLE dies.

## Build / flash

```bash
~/.local/bin/arduino-cli compile --fqbn esp32:esp32:esp32c3:CDCOnBoot=cdc,FlashSize=4M .
~/.local/bin/arduino-cli upload -p /dev/ttyACM0 --fqbn esp32:esp32:esp32c3:CDCOnBoot=cdc,FlashSize=4M .
```

(If the port changes after flashing, use `arduino-cli board list`.)

## Receiver configuration experiment (2026-10-21)

A UBX-CFG-NAV5 attempt (automotive dynamic model + 10° elevation mask) was tested.
The receiver then reported **fix=0 with 12 tracked satellites** and produced no
position. Sending a full CFG-NAV5 default set (portable model, 5° elevation mask)
restored **fix=2** immediately. The firmware now leaves the receiver at its
known-good default NAV5 configuration; do not apply the partial automotive/
elevation settings to this M9N unit.

## 2026-10-21 flash result

- Board: ESP32-C3 Super Mini (4 MB flash, MAC `8c:d0:b2:bc:ba:d8`)
- Original "digital_level" firmware was backed up **before** flashing:
  `~/.local/share/esp32c3-firmware/backup-digital-level-20261021.bin`
  (4,194,304 bytes, SHA-256 `c71c4bb9f7284b681ddcfc00b4753e96c8a7838ee4ea4889f87ecd8a331f86d1`)
- New firmware flashed and verified. Serial test: LSM6DS3 detected at 0x6A, ~96 IMU
  lines/s, gravity vector ≈ 1 g; GNSS UART detected NMEA at 38400, but the module
  had **no fix** indoors (sats=0, hdop=99.99) — connect the SMA antenna and give it
  a clear sky view, cold start can take minutes.
- The web 车机运维台 「陀螺仪」 page now uses IMU Studio with an ESP32 serial backend:
  the six-face/gyro-bias calibration UI opens in a large modal, the GNSS status is
  shown in the page header/card, and a Leaflet + Amap-tile map displays the live fix
  (WGS84 -> GCJ-02; no Amap API key required for tiles, internet needed).
  `sensord` still has no ESP32 serial backend, so SP itself does not consume this IMU yet.

Restore the original firmware if needed:

```bash
~/.arduino15/packages/esp32/tools/esptool_py/5.3.1/esptool --chip esp32c3 \
  --port /dev/ttyACM0 --baud 460800 write_flash 0x0 \
  ~/.local/share/esp32c3-firmware/backup-digital-level-20261021.bin
```

For a full rebuild:

```bash
cd /data/sunnypilot/tools/esp32c3_imu_bridge
~/.local/bin/arduino-cli compile --fqbn 'esp32:esp32:esp32c3:CDCOnBoot=cdc,FlashSize=4M,PartitionScheme=default,UploadSpeed=460800' --build-path /tmp/esp32-build .
~/.local/bin/arduino-cli upload -p /dev/ttyACM0 --fqbn 'esp32:esp32:esp32c3:CDCOnBoot=cdc,FlashSize=4M,PartitionScheme=default,UploadSpeed=460800' --input-dir /tmp/esp32-build .
```

The prebuilt images used for the flash are in `build/` with `SHA256SUMS`.

### 2026-10-23 v1.1.0 flash status (flashed + verified)

- Source, README and `host_test.py` updated; compiled clean with
  `esp32:esp32:esp32c3:CDCOnBoot=cdc,FlashSize=4M,PartitionScheme=default,UploadSpeed=460800`.
  Final image: **680,416 bytes (51%)**, globals 24,808 bytes (7%).
- **Flashed 2026-10-23 23:16** via `/dev/ttyACM0` (hash verified by esptool).
  `host_test.py` then reported:
  `hello.version=1.1.0`, `boot=5`, `reset=usb`, `wdt=true`,
  `status.imu_hz=100.0`, `imu_seq=1000 at 10.4 s`, `ble_drops=0`,
  `gnss_locked=true`, `gnss_baud=38400`, `fix=2/sats=12`.
  IMU Studio (over USB) measured **99.95 Hz** after the reboot.
- **Two bugs were found only by the first USB verification and fixed before the
  final flash**:
  1. the default HWCDC TX ring is **256 bytes**, so `status` (~710 B) and
     `gnss` (~500 B) lines were truncated at ~247 B; fixed with
     `Serial.setTxBufferSize(4096)` plus retrying short `Serial.write()` calls;
  2. the LSM6DS3 probe right after reset could fail once; `setup()` now retries
     it 3x/120 ms before reporting an error.
- `build/esp32c3_imu_bridge.ino.bin` / `.merged.bin` and `build/SHA256SUMS`
  contain this flashed image.
- Rollback image for the previous firmware: `build/esp32c3_imu_bridge-1.0.1.bin`
  (`.merged.bin`), with `build/SHA256SUMS.rollback-1.0.1` and `build/ROLLBACK.txt`.
  Flash the merged rollback at `0x0` with `esptool` if v1.1.0 misbehaves.
- Note: the flashed image does not include the final comment-only edit about
  the USB TX buffer; the code is identical, only the source comments changed.

## GPS into sunnypilot

`openpilot/system/sensord/esp32d.py` and the GPS-only sidecar
`openpilot/system/sensord/esp32gpsd.py` convert the firmware's 1 Hz `gnss`
JSON into the cereal `gpsLocation` service. The sidecar runs when the IMU is
disabled, so the same USB GNSS can feed `locationd_llk`/`mapd` without the IMU
sensor. The two readers are mutually exclusive on `/dev/ttyACM0`; stop IMU
Studio before starting the SP stack.

Only GGA/RMC are used: latitude/longitude/altitude, GGA fix quality, satellite
count, HDOP, and RMC speed/course.  The event stays `valid=true` while waiting
for a fix (`hasFix=false`), which lets `locationd_llk` initialize and become
alive; a position is used only after a fix.  GSV/UBX are not parsed.

## BLE transport

- Name: `ESP32C3-IMU`
- Service: `6E400001-B5A3-F393-E0A9-E50E24DCCA9E`
- TX notify: `6E400003-B5A3-F393-E0A9-E50E24DCCA9E`
- RX write: `6E400002-B5A3-F393-E0A9-E50E24DCCA9E`

USB and BLE run at the same time. USB remains the primary path. The IMU Studio
host backend uses USB when `/dev/ttyACM0` exists, otherwise it scans/connects
to this BLE name. BLE uses a compact binary IMU packet so 100 Hz fits reliably:

```text
byte 0      = 0x01
bytes 1..4  = seq (uint32 LE)
bytes 5..8  = t_ms (uint32 LE)
bytes 9..36 = ax, ay, az, gx, gy, gz, temp (7 x float32 LE)
```

GNSS/status/error messages over BLE remain newline-delimited JSON. `hello` and
the periodic `status` line are USB-only (the BLE client does not need them).

Host fallback is implemented in IMU Studio: `/dev/ttyACM0` present -> USB,
otherwise scan/connect `ESP32C3-IMU`. Measured before v1.1.0: **100.6 Hz over
BLE** (forced `IMU_BACKEND=ble`) and **99.7 Hz over USB** (auto, port present).
A physical USB-unplug test still requires the ESP32 to be powered separately.

## v1.1.0 robustness / diagnostics

Firmware `1.1.0` (2026-10-23) was written after an unexplained ESP32 reboot
caused a ~1 minute BLE data gap. It changes only diagnostics, boot timing and
buffering; the USB/BLE packet formats are unchanged.

- `hello` and `status` now include `reset` (`poweron`/`panic`/`task_wdt`/
  `brownout`/...), `boot` count (NVS), `heap`, `min_heap`, `wdt`, IMU/GNSS/BLE
  counters and drop/overflow counters. The next reset can be classified.
- GNSS baud detection is non-blocking: the old code could block setup for up to
  12 s (six bauds x 2 s) before the first `hello`/IMU sample. It now starts at
  the NVS-cached baud, scans the remaining bauds in 1.2-1.8 s slices while the
  IMU keeps running, caches the detected baud in NVS, and rescans after 30 s
  without NMEA.
- BLE TX is throttled to at most 2 notifications per 5 ms (enough for the
  100 Hz IMU plus GNSS even at the default 23-byte MTU), the buffer is drained
  in bursts instead of one chunk per 10 ms, and drop/overflow/skip counters are
  exposed in `status`/`gnss`. `ble_buffer_len` is protected by a critical
  section shared with the connect/disconnect callbacks.
- GNSS data expires after 3 s (`stale:true`, `fix:0`, `age_ms`); a dead UART no
  longer reports a forever-stale fix.
- USB hot-plug re-sends `hello` (previously it was only sent at boot, so opening
  the port after boot made the host wait for its 20 s timeout).
- **USB TX ring is 4096 bytes and long lines retry on short writes.** The core's
  default HWCDC ring is only 256 bytes; `status` (~710 B) and `gnss` (~500 B)
  were truncated at ~247 B during the first USB verification. Fixed with
  `Serial.setTxBufferSize(4096)` and a short-write retry loop.
- The first LSM6DS3 probe after reset is retried 3x/120 ms; a single missed
  probe used to print a spurious "LSM6DS3 not found" line.
- IMU Studio's 「加速度计六面」tab now has an **安装方向（X/Y 反转）** selector
  (`正常` / `X/Y 都反转` / `X/Y 互换`). It stores
  `mounting.device_from_sensor` + `mounting.preset` in `calibration.json`
  without changing the six-face bias/scale. `make_sp_config.py` uses that matrix
  automatically when `--matrix` is omitted and converts it from the Y-inverted
  calibration frame to physical chip axes; `--confirm` is still required.
- The Arduino loop task is armed on the 5 s task watchdog; `Wire` transaction
  timeout is 50 ms; `poll_gnss` is bounded to 512 bytes per loop.
- Removed the unused UBX-CFG-NAV5 helpers. The M9N remains at its known-good
  default NAV5 configuration.

## Output protocol

USB is newline-delimited JSON:

```json
{"type":"hello","fw":"esp32c3-imu-gnss","version":"1.1.0","build":"Oct 23 2026 23:00:00","i2c":[0,1],"imu_addr":"0x6A","whoami":"0x69","gnss_uart":[4,3],"gnss_baud":38400,"gnss_baud_detected":false,"gnss_locked":false,"frame":"accel_gyro_y_inverted","rate_hz":104,"boot":3,"reset":"poweron","heap":300000,"min_heap":280000,"idf":"v5.5.0"}
{"type":"status","t":123456,"up_ms":123456,"boot":3,"reset":"poweron","heap":300000,"min_heap":280000,"wdt":true,"imu_ok":true,"imu_hz":99.8,"imu_seq":12345,"imu_err":0,"imu_rec":0,"ble":false,"ble_mtu":23,"ble_connects":0,"ble_disconnects":0,"ble_ok":0,"ble_bytes":0,"ble_drops":0,"ble_skip":0,"ble_overflow":0,"ble_buf":0,"gnss_baud":38400,"gnss_locked":true,"gnss_detected":true,"gnss_has":true,"gnss_age_ms":120,"gnss_fix":2,"gnss_sats":12,"nmea":1234,"nmea_bad":0,"gga":600,"rmc":600,"baud_changes":1,"gnss_rescans":0}
{"type":"imu","seq":1,"t":1234,"ax":0.01,"ay":-0.02,"az":1.00,"gx":0.1,"gy":0.2,"gz":-0.3,"temp":31.5}
{"type":"gnss","t":2345,"fix":1,"sats":9,"lat":39.9,"lon":116.4,"alt":45.2,"speed_mps":0.0,"course":0.0,"hdop":0.8,"utc":"123519.00","stale":false,"age_ms":80,"baud":38400,"rx_bytes":12345,"ble_mtu":517,"ble_ok":100,"ble_bytes":3700,"ble_drops":0,"ble_skip":0,"ble_overflow":0,"ble_buf":0,"last":"$GNGGA,..."}
{"type":"gnss_status","t":2345,"fix":0,"sats":0,"stale":true,"rx_bytes":12345,"baud":38400,"locked":true,"detected":true,"lines":1234,"bad":0,"gga":600,"rmc":600,"last":"$GNGGA,..."}
{"type":"gnss_locked","t":2345,"baud":38400}
{"type":"error","where":"imu","msg":"..."}
```

- Axes use the existing calibrator frame `accel_gyro_y_inverted` (chip Y is negated for accel and gyro);
  X/Z stay chip-native.
- IMU at ~100 Hz (sensor ODR 104 Hz), GNSS emitted once per second once NMEA data arrives.
- `status` is USB-only, every 10 s. `gnss` is mirrored to BLE; `hello` is USB-only.
- On IMU failure, a `{"type":"error","where":"imu",...}` line is emitted every 2 s; GNSS keeps running.
- NMEA parsing accepts GGA/RMC from any talker (`$GNGGA`, `$GPRMC`, ...). UBX-only output would need
  a UBX parser; the M9N defaults to NMEA.
- `hello.gnss_baud_detected`/`gnss_locked` turn true once a valid NMEA sentence has been seen on the
  current baud. `hello` is re-sent when a USB host appears after boot.
