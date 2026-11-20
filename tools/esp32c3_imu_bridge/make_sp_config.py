#!/usr/bin/env python3
"""Create ~/.config/sunnypilot/esp32.json from an IMU Studio calibration.

The ESP32 backend deliberately does not guess the mounting matrix.  The matrix
can come from the IMU Studio six-face panel (stored in calibration.json as
mounting.device_from_sensor) or from --matrix; --confirm is still required after
checking the physical orientation.

The ESP32 backend undoes IMU Studio's Y-inverted calibration frame before
applying device_from_sensor, so the matrix written here is in physical sensor
(chip) axes.  A matrix read from calibration.json was recorded in the calibration
frame and is converted with diag(1, -1, 1) before being written.
"""
import argparse
import hashlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_CAL = Path.home() / "Documents/Codex/2026-09-26/new-chat/outputs/imu-calibrator/data/calibration.json"
DEFAULT_OUT = Path.home() / ".config/sunnypilot/esp32.json"
CALIBRATION_FROM_SENSOR = [[1.0, 0.0, 0.0], [0.0, -1.0, 0.0], [0.0, 0.0, 1.0]]


def parse_matrix(text: str) -> list[list[float]]:
  value = json.loads(text) if text.strip().startswith("[") else [float(v) for v in text.replace(";", ",").split(",")]
  if len(value) == 9 and all(isinstance(v, (int, float)) for v in value):
    value = [value[0:3], value[3:6], value[6:9]]
  if len(value) != 3 or any(len(row) != 3 for row in value):
    raise SystemExit("matrix must be 3x3 (JSON or 9 comma-separated numbers)")
  return [[float(v) for v in row] for row in value]


def matmul(a, b):
  return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def determinant(m):
  return (m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
          - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
          + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]))


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--calibration", type=Path, default=DEFAULT_CAL)
  parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
  parser.add_argument("--port", default="/dev/ttyACM0")
  parser.add_argument("--matrix", default=None,
                      help="device_from_sensor in physical sensor/chip axes, JSON or 9 comma-separated numbers; "
                           "defaults to calibration.json mounting converted from its calibration frame")
  parser.add_argument("--confirm", action="store_true", help="set mounting_confirmed=true after physical verification")
  parser.add_argument("--note", default="已按实际 ESP32 安装方向核对 device_from_sensor。")
  args = parser.parse_args()

  cal_path = args.calibration.expanduser()
  raw = cal_path.read_bytes()
  saved = json.loads(raw)
  device = saved.get("device", {})
  if saved.get("schema_version") != 1:
    raise SystemExit("unsupported calibration schema")
  if not str(device.get("bridge", "")).startswith("ESP32-C3"):
    raise SystemExit(f"calibration bridge is {device.get('bridge')!r}, not ESP32-C3; run IMU Studio over USB first")
  if device.get("coordinate_frame") != "accel_gyro_y_inverted":
    raise SystemExit("unexpected calibration coordinate frame")
  cal = saved.get("calibration", {})
  accel = cal.get("accel") or {}
  gyro = cal.get("gyro") or {}
  if not accel or not gyro or set(accel.get("faces", {})) != {"+X", "-X", "+Y", "-Y", "+Z", "-Z"}:
    raise SystemExit("calibration is missing six-face accel or gyro bias")

  mounting = saved.get("mounting") or {}
  if args.matrix is not None:
    matrix = parse_matrix(args.matrix)
    matrix_source = "--matrix"
  elif mounting.get("device_from_sensor"):
    raw_matrix = mounting["device_from_sensor"]
    if (not isinstance(raw_matrix, list) or len(raw_matrix) != 3
        or any(not isinstance(row, list) or len(row) != 3 for row in raw_matrix)):
      raise SystemExit("calibration.json mounting.device_from_sensor must be 3x3")
    # calibration.json stores mounting in the Y-inverted calibration frame;
    # convert it to physical chip axes expected by esp32d's corrected_event().
    matrix = matmul([[float(value) for value in row] for row in raw_matrix], CALIBRATION_FROM_SENSOR)
    matrix_source = (f"calibration.json mounting ({mounting.get('label') or mounting.get('preset') or 'custom'}) "
                     "converted Y-inverted -> chip")
  else:
    raise SystemExit("no mounting matrix: set one in IMU Studio 加速度计六面 panel or pass --matrix")
  det = determinant(matrix)
  if abs(det - 1.0) > 1e-5:
    raise SystemExit(f"matrix determinant is {det:.9f}, must be +1")
  identity = [[1.0 if i == j else 0.0 for j in range(3)] for i in range(3)]
  for i in range(3):
    for j in range(3):
      dot = sum(matrix[i][k] * matrix[j][k] for k in range(3))
      if abs(dot - identity[i][j]) > 1e-5:
        raise SystemExit("matrix must be orthonormal (rows unit length and perpendicular)")
  if args.confirm:
    # Keep the same basic limits as the SP backend.
    for name, values, limit in (("accel_bias", accel["bias_g"], .2), ("accel_scale", accel["scale"], 1.3),
                                ("gyro_bias", gyro["bias_dps"], 10.)):
      if not all(abs(float(v)) <= limit for v in values):
        raise SystemExit(f"{name} outside backend limit")

  identity = str(device.get("identity", ""))
  if not identity:
    raise SystemExit("calibration has no device identity")
  digest = hashlib.sha256(raw).hexdigest()
  config = {
    "schema_version": 1,
    "serial_port": args.port,
    "calibration_path": str(cal_path),
    "calibration_sha256": digest,
    "calibration_identity": identity,
    "device_from_sensor": matrix,
    "mounting_confirmed": bool(args.confirm),
    "mounting_note": args.note,
  }
  out = args.out.expanduser()
  out.parent.mkdir(parents=True, exist_ok=True)
  if out.exists():
    backup = out.with_name(out.name + ".bak-" + datetime.now(UTC).strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(out, backup)
    print("backup", backup)
  out.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
  print("wrote", out)
  print("calibration", cal_path)
  print("identity", identity)
  print("sha256", digest)
  print("matrix_source", matrix_source)
  print("mounting_confirmed", config["mounting_confirmed"])
  if not args.confirm:
    print("valid will stay false until --confirm after a physical orientation check")


if __name__ == "__main__":
  main()
