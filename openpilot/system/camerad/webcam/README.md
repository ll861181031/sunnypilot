# Run openpilot with webcam on PC

## Setup openpilot
- Follow [this readme](/tools/README.md) to install and build the requirements
- Install the desktop capture dependencies with `uv pip install --python .venv/bin/python -r openpilot/system/camerad/webcam/requirements.txt`.

## Connect the hardware
- Connect the camera first
- Connect your computer to panda

## GO

```
USE_WEBCAM=1 openpilot/system/manager/manager.py
```
- Start the car, then the UI should show the road webcam's view
- Adjust and secure the webcam
- Finish calibration and engage!

## Specify Cameras

Use the `ROAD_CAM` (default 0) and optional `DRIVER_CAM`, `WIDE_CAM` environment variables to specify which camera is which (ie. `ROAD_CAM=1` uses `/dev/video1`, on Ubuntu, for the road camera):
```
USE_WEBCAM=1 ROAD_CAM=1 openpilot/system/manager/manager.py
```

On Linux, `NARROW_ROAD_CAM` also accepts a stable `/dev/v4l/by-id/...-video-index0` path.

The second road camera is optional and adaptive. `WIDE_CAM` selects it; if that
device is missing, or resolves to the same node as `NARROW_ROAD_CAM`, capture runs
single-camera and the stack stays up. The launcher exports `WIDE_CAM` only when the
configured device exists, so plugging the wide camera in before a restart (or
unplugging it) selects dual or single mode with no setting to edit.
`SUNNYPILOT_DISABLE_WIDE_CAMERA=1` is still the explicit force-single override.
The wide stream is never required for engagement; modeld falls back to a copy of the
narrow frame when the second input stops arriving.

On this desktop rig the 2026-11-04 lens swap exchanged the two lenses: the DECXIN
`1bcf:2cd1` body now carries the telephoto lens and is the main camera, while the
Realtek `0bda:5161` body carries the fisheye wide lens. The Realtek body is the
inverted one, so its wide stream sets `WIDE_WEBCAM_ROTATE_180=1` explicitly; the
DECXIN main runs with `WEBCAM_ROTATE_180=0`. A lens or port swap is not just a
`WIDE_CAM` change: if both names point at one device, capture refuses the second
stream instead of opening the camera twice.
`WEBCAM_WIDTH` and `WEBCAM_HEIGHT` select capture size (defaults: 1164, 874).
`WEBCAM_OUTPUT_WIDTH` and `WEBCAM_OUTPUT_HEIGHT` select the published size and
default to the model's compiled road resolution, 1344x760. They are deliberately
not the capture size: the model only has `run_model` entries for 1344x760 and
1928x1208, and a mismatch fails at startup instead of being scaled for you.
`WEBCAM_FPS` selects the publication rate (default: 20); `WEBCAM_CAPTURE_FPS`
can request a higher hardware capture rate to leave processing headroom.
MJPEG is requested before capture to avoid USB bandwidth limiting uncompressed video.
`WEBCAM_ROTATE_180` rotates the published image 180 degrees, for a camera module
mounted upside down; `0` preserves the camera's native orientation. The code default
is `1`; the local launcher overrides it to `0` because the current main camera
(DECXIN, after the swap) is upright. Set it from the physical mount, not from the
camera's product name, or the image will come out upside down. This is a true 180-degree
rotation (both axes), not a mirror, so left/right and lane chirality are preserved. It runs in software before VIPC publication, so the model, the UI and the
recorded video all see an upright frame and the model's intrinsics/warp are unchanged.
The wide and cabin cameras use `WIDE_WEBCAM_ROTATE_180` /
`DRIVER_WEBCAM_ROTATE_180`, falling back to the road value when unset. Set those
explicitly if the cameras are mounted differently. Expect roughly 2 ms/frame of
extra CPU at 1928x1208; recalibrate if the camera was physically moved while
flipping it.

The local desktop launcher captures the road camera at 1920x1080 / 20 fps and
publishes the native 1920x1080 / 20 fps, so the UI and recordings get 1:1 1080p
pixels instead of an upscale. The optional wide camera captures 1920x1080 / 30 fps
(its 20 fps mode underdelivers at ~18 fps) and publishes each decoded frame
without an additional 20 fps sleep. Both road streams publish one size because modeld pairs them with one
compiled geometry. That only works with the local uvcvideo USB2 bandwidth
patch (see `tools/kernel/README.md`): without it the wide camera reserves the whole
24.576 MB/s isochronous budget and gets 0 fps when the narrow camera is streaming.
When the backend supports raw MJPEG, PyAV decodes and scales in YUV, with explicit
full-range JPEG to limited-range NV12 conversion. This avoids intermediate BGR
images; backends that cannot supply raw MJPEG retain the BGR path. Both paths
preserve the VIPC stride/padding contract. The webcam process limits its OpenCV
worker pool to four threads to reduce contention with modeld and encoderd.

### Capture size vs published size

`WEBCAM_WIDTH` / `WEBCAM_HEIGHT` are the UVC capture mode; `WEBCAM_OUTPUT_WIDTH`
/ `WEBCAM_OUTPUT_HEIGHT` are what `camerad` publishes to VIPC, and therefore what
`encoderd` records and the UI renders. They do not have to match, but the
published size must be one the road model was compiled for. When
`WEBCAM_OUTPUT_*` is unset the road streams publish 1344x760 (the cabin stream
keeps its compiled 1928x1208 DM size). The wide stream is forced to the narrow
road size if `WIDE_WEBCAM_OUTPUT_*` disagrees with it, because `modeld` pairs the
two road frames and indexes one compiled resolution for both. On this checkout the
model artifact contains three source sizes: 1344x760, 1928x1208 and 1920x1080
(`modeld.py` indexes `run_model[(cam_w, cam_h)]`); any other size fails at modeld
startup, not silently.

`WEBCAM_CROP_TO_OUTPUT=1` (the road default) scales the capture to *cover* the
published rectangle and then centre-crops the excess whenever the two aspect
ratios differ by more than 1%. This is what makes a 4:3 sensor (2592x1944)
usable at the model's 16:10 size: straight scaling would compress the image
vertically by 16% and bend every lane line. Matching aspects, including the
historical 1920x1080 -> 1344x760 path (0.5% apart), keep the old single-resize
behaviour. Cabin capture keeps stretching by default because its compiled DM
warp has never been validated with a cropped USB image; opt in with
`DRIVER_WEBCAM_CROP_TO_OUTPUT=1`.

Changing the published width also changes the intrinsics in *buffer pixels*:
`common/transformations/camera.py` derives the focal from `WEBCAM_OUTPUT_WIDTH`
and the configured field of view (the `BydCameraFovTenthsDeg=397` setting, or
`SUNNYPILOT_PC_CAMERA_FOCAL` when that setting is unset). The 39.7 degree
horizontal field of view resolves to 1864 px at 1344 wide, 2670 px at 1928 wide,
and 3594 px at the full 2592 capture width. Keep the fallback env var in sync if
you move the published width without the settings-page FOV.

Publishing the full 2592x1944 instead would also require rebuilding the model
runtime with `--camera-resolutions 2592x1944` (the JIT bakes the source shape),
and its 4:3 aspect would show more vertical field than the stock road camera.
The model input itself is only 256x128, so capture resolution above the
published size buys encoding/UI sharpness, not model detail.

The optional `PANDA_USB_READONLY=1` mode starts camera capture while remaining
offroad. This is for connection testing and image transport, not calibrated
driving/model use.
The metadata node `/dev/video1` is not a second camera or a driver-facing stream.

## Restore a separate desktop driver camera

The desktop launcher selects the inward-facing camera with the `DRIVER_CAM`
environment variable (an index like `2`, or a stable
`/dev/v4l/by-id/...-video-index0` path). Without `DRIVER_CAM`, cabin capture
stays disabled. `SUNNYPILOT_DISABLE_DRIVER_CAMERA=1` disables driver monitoring
and cabin capture entirely; it defaults to `0` (enabled). On PC, cabin capture
also requires `USE_WEBCAM=1`, matching the road camera. The launcher does not
select the first unused video node automatically.

Cabin settings can independently override `WEBCAM_*` using `DRIVER_WEBCAM_*`
(width, height, output dimensions, capture/publication fps and 180-degree rotation).
Use actual supported capture modes. Output size must match a compiled DM warp;
supported warp sizes alone do not validate a webcam's lens, placement or field of view.
The existing DM transform assumes comma fisheye intrinsics. Generic USB camera
intrinsics/cropping and real face/attention detection require validation before driving.

`SUNNYPILOT_DM_DEVICE=AMD` selects separate AMD driver-monitoring and warp artifacts.
The default remains CPU, and original CPU/device files remain available.
Build with the same setting using SCons. AMD uses fresh host-to-GPU copies for USB
frames. This backend is an opt-in candidate: synthetic throughput checks passed,
but CPU/AMD numerical differences and real-camera detection still need acceptance
testing. The local launcher does not enable the candidate automatically.
Synthetic inference checks only establish software execution and throughput;
they do not certify a working camera, driver detection, or vehicle engagement.
