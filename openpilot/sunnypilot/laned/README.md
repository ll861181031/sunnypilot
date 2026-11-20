# laned — optional lane-marking type classifier

This package ports CarrotPilot's `xiaoge` lane classifier to sunnypilot. It is a
separate model from the driving model: geometry still comes from
`modelV2.laneLines`, while this daemon answers one question per ego-lane
boundary: **solid, dashed, or unknown**.

## What runs

- `models/lane.onnx`: YOLOv8-Seg, 416x416 grayscale, 6 classes. It is the model
  shipped in CarrotPilot's `openpilot/selfdrive/carrot/xiaoge/assets/lane.onnx`.
- `laned.py`: reads the road camera from VisionIPC at 2.5 Hz and publishes the
  `laneLineTypes` msgq service. Disabled unless `LaneLineDetectionEnabled` is set.
- `lane_inference.py`: center-crop, resize, YOLOv8-Seg decode, NMS, and left/right
  boundary selection. Only luma is used, so no NV12 chroma conversion is needed.

## Class map

| class | embedded name | public type |
|---:|---|---:|
| 0 | `divider-line` | 1 solid |
| 1 | `dotted-line` | 0 dashed |
| 2 | `double-line` | 1 solid |
| 3 | `random-line` | -1 unknown (ignored) |
| 4 | `road-sign-line` | -1 unknown (ignored) |
| 5 | `solid-line` | 1 solid |

The model returns one type for the nearest left boundary and one for the nearest
right boundary. It does not classify all four `laneLines`, and it does not return
a colour. This port stores only the type in `CarStateSP.leftLaneLine/rightLaneLine`;
the BYD port has no OEM colour to merge, so `LaneLineCheck=0` does not implement
CarrotPilot's yellow-line gate.

## Enable

`laned` is off by default. After changing these params, restart the manager (or
the whole stack) so `process_config` and `modeld` pick them up:

```bash
python - <<'PY'
from openpilot.common.params import Params
p = Params()
p.put_bool("LaneLineDetectionEnabled", True)
p.put("LaneLineCheck", 1)   # optional, see below
PY
```

`LaneLineCheck` is read by `modeld` at startup:

- `0` — off, current behaviour (only the OEM colour gate, when available).
- `1` — block a lane change when the target boundary is solid or unknown.
- `2` — allow a solid/unknown boundary to enter `preLaneChange`, but require
  driver torque before starting the change.

Unknown (`-1`) is deliberately fail-closed for mode 1: if the classifier has not
produced a result, the check must not pretend the line is dashed.

## Message

`laneLineTypes` is a `log.Event` with the `Custom.LaneLineTypes` payload:

```capnp
struct LaneLineTypes {
  frameId @0 :UInt32;
  timestampEof @1 :UInt64;
  leftType @2 :Int8;              # -1 unknown, 0 dashed, 1 solid
  rightType @3 :Int8;
  leftConfidence @4 :Float32;
  rightConfidence @5 :Float32;
  inferenceMs @6 :Float32;
  candidatesCount @7 :UInt16;
  model @8 :Text;
}
```

`card.py` copies the result into `CarStateSP.leftLaneLine/rightLaneLine` and sets
`laneLineTypesValid`. The fields are `-1` when the service is absent or stale
(more than the 4-second liveness window). `modeld` passes them to `DesireHelper`.

## Tests

```bash
.venv/bin/python -m pytest -q openpilot/sunnypilot/laned/tests
.venv/bin/python -m pytest -q openpilot/sunnypilot/selfdrive/controls/lib/tests/test_lane_line_gate.py
```

The model smoke test builds a synthetic road image and checks that the ONNX file
loads and returns two candidates. A manual check without a camera:

```bash
.venv/bin/python -m openpilot.sunnypilot.laned.laned --image /path/to/frame.png
```

## Limitations

- One classification per ego boundary, not per lane line; the model is not a
  general lane-marking segmentation map.
- 416x416 center-square crop of the road camera; the outer edges of a wide frame
  are not visible to this model.
- 2.5 Hz by default; it is not suitable for frame-by-frame control.
- No colour output; colour still comes from the vehicle CAN (Hyundai/Kia) and is
  `0` (unknown) on the BYD port.
- The model was trained for CarrotPilot's use case; real-road accuracy on this
  camera and on Chinese roads is **not yet measured**. Treat it as an opt-in
  experiment until a route with solid/dashed ground truth is reviewed.
