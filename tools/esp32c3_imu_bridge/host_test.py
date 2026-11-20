#!/usr/bin/env python3
"""Read the ESP32-C3 bridge JSON stream and print basic health information.

Usage:
  python3 host_test.py [/dev/ttyACM0] [seconds]

Requires pyserial (python3-serial / pip install pyserial).
"""
import json
import sys
import time

import serial

port = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyACM0"
seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 5.0

link = serial.Serial(port, 115200, timeout=0.5)
link.dtr = False
link.rts = False
start = time.monotonic()
last_print = 0.0
imu = 0
gnss = 0
hello = None
last_imu = None
last_gnss = None
last_status = None
last_gnss_status = None
errors = []

while time.monotonic() - start < seconds:
    raw = link.readline()
    if not raw:
        continue
    line = raw.decode("utf-8", "replace").strip()
    if not line.startswith("{"):
        continue
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        continue
    kind = msg.get("type")
    if kind == "hello":
        hello = msg
        print("hello:", json.dumps(msg, ensure_ascii=False))
    elif kind == "imu":
        imu += 1
        last_imu = msg
    elif kind == "gnss":
        gnss += 1
        last_gnss = msg
        print("gnss:", json.dumps(msg, ensure_ascii=False))
    elif kind == "status":
        last_status = msg
        print("status:", json.dumps(msg, ensure_ascii=False))
    elif kind == "gnss_status":
        last_gnss_status = msg
        print("gnss_status:", json.dumps(msg, ensure_ascii=False))
    elif kind == "gnss_locked":
        print("gnss_locked:", json.dumps(msg, ensure_ascii=False))
    elif kind == "error":
        errors.append(msg)
        print("error:", json.dumps(msg, ensure_ascii=False))

elapsed = time.monotonic() - start
link.close()
print()
print("window_s", round(elapsed, 2))
print("imu_lines", imu, "rate_hz", round(imu / elapsed, 1) if elapsed else 0)
print("gnss_lines", gnss, "last_gnss", json.dumps(last_gnss, ensure_ascii=False))
print("last_imu", json.dumps(last_imu, ensure_ascii=False))
if last_status:
    print("status", json.dumps(last_status, ensure_ascii=False))
if last_gnss_status:
    print("gnss_status", json.dumps(last_gnss_status, ensure_ascii=False))
if hello is None:
    print("WARNING: no hello line (wrong baud/protocol or firmware not running)")
if errors:
    print("errors", json.dumps(errors[-3:], ensure_ascii=False))
