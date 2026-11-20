import itertools
import math
import os

import numpy as np
from dataclasses import dataclass

import openpilot.common.transformations.orientation as orient

## -- hardcoded hardware params --
@dataclass(frozen=True)
class CameraConfig:
  width: int
  height: int
  focal_length: float

  @property
  def size(self):
    return (self.width, self.height)

  @property
  def intrinsics(self):
    # aka 'K' aka camera_frame_from_view_frame
    return np.array([
      [self.focal_length,  0.0, float(self.width)/2],
      [0.0, self.focal_length, float(self.height)/2],
      [0.0,  0.0, 1.0]
    ])

  @property
  def intrinsics_inv(self):
    # aka 'K_inv' aka view_frame_from_camera_frame
    return np.linalg.inv(self.intrinsics)

@dataclass(frozen=True)
class _NoneCameraConfig(CameraConfig):
  width: int = 0
  height: int = 0
  focal_length: float = 0

@dataclass(frozen=True)
class DeviceCameraConfig:
  narrow_road: CameraConfig
  cabin: CameraConfig
  wide_road: CameraConfig

  def all_cams(self):
    for cam in ['narrow_road', 'cabin', 'wide_road']:
      if not isinstance(getattr(self, cam), _NoneCameraConfig):
        yield cam, getattr(self, cam)

_ar_ox_fisheye = CameraConfig(1928, 1208, 567.0)  # focal length probably wrong? magnification is not consistent across frame
_os_fisheye = CameraConfig(2688 // 2, 1520 // 2, 567.0 / 4 * 3)
_ar_ox_config = DeviceCameraConfig(CameraConfig(1928, 1208, 2648.0), _ar_ox_fisheye, _ar_ox_fisheye)
_os_config = DeviceCameraConfig(CameraConfig(2688 // 2, 1520 // 2, 1522.0 * 3 / 4), _os_fisheye, _os_fisheye)
_neo_config = DeviceCameraConfig(CameraConfig(1164, 874, 910.0), CameraConfig(816, 612, 650.0), _NoneCameraConfig())


def _param_override(key: str) -> float:
  """Numeric value set from the on-device settings page, or 0 when unset.

  camera.py is imported by the UI, the replay tools and both model processes,
  so the params backend has to be treated as optional: if it is unavailable
  (standalone tools, tests) we fall back to the environment and built-in path
  instead of raising during import.
  """
  try:
    from openpilot.common.params import Params
    value = Params().get(key, return_default=True)
    return float(value) if value else 0.0
  except Exception:
    return 0.0


def focal_from_hfov(hfov_deg: float, width_px: int) -> float:
  """Focal length in pixels for a horizontal field of view.

  This is the inverse of the FOV the driver can actually measure: park facing a
  wall, note where the left and right frame edges land, and

    HFOV = 2*atan(span / (2*distance))

  The physical focal length in mm is deliberately absent: it cancels out of the
  conversion, so any (focal_mm, sensor_size) pair describing the same view gives
  the same answer here. See the project conventions for the derivation and the measurements
  that pin this camera at 39.65 degrees / 1864 px.
  """
  return (width_px / 2.0) / math.tan(math.radians(hfov_deg) / 2.0)


def focal_from_dfov(dfov_deg: float, width_px: int, height_px: int) -> float:
  """Focal length in pixels for a diagonal field of view.

  Datasheets quote the diagonal, so this is the entry that needs no arithmetic
  from whoever is holding the spec sheet:

    f_px = sqrt(width^2 + height^2) / (2*tan(DFOV/2))

  The physical focal length cancels out here too, which is why a datasheet DFOV
  on its own is enough - the focal and the sensor size are only ever another way
  of writing the same view.
  """
  diag_px = math.hypot(width_px, height_px)
  return diag_px / (2.0 * math.tan(math.radians(dfov_deg) / 2.0))


def _spec_focal_px(focal_key: str, sensor_key: str, width_px: int) -> float:
  """Focal in pixels from a physical focal length plus the sensor width.

  A focal length in mm says nothing about pixels until you know how wide the
  imaging area is:

    f_px = f_mm * width_px / sensor_width_mm

  Both parameters are required. Offering the focal alone would let someone enter
  a number that silently does nothing, so a half-filled pair is treated as unset.
  """
  focal_mm = _param_override(focal_key) / 10.0
  sensor_mm = _param_override(sensor_key) / 10.0
  if focal_mm <= 0 or sensor_mm <= 0:
    return 0.0
  return focal_mm * width_px / sensor_mm


def _pc_camera_config() -> DeviceCameraConfig:
  """Intrinsics for the desktop USB camera.

  The webcam publishes `WEBCAM_OUTPUT_WIDTH x WEBCAM_OUTPUT_HEIGHT` (falling back
  to the capture size) and `modeld` builds its input warp from these intrinsics
  in *buffer pixels*. Describing a 1928x1208 device camera while the driver
  publishes 1344x760 leaves about 70% of the model input outside the frame, so
  the intrinsics follow the published size and keep the device field of view
  until a real focal length is configured. In precedence order:

    BydCameraFovTenthsDeg / BydCameraWideFovTenthsDeg
                                             measured horizontal FOV, wins
    BydCameraDiagFovTenthsDeg / BydCameraWideDiagFovTenthsDeg
                                             datasheet diagonal FOV, the number
                                             printed on the lens
    BydCameraFocalTenthMm / BydCameraSensorWidthTenthMm (and the Wide pair)
                                             spec-sheet focal + sensor size,
                                             consulted only when BOTH are set
    BydCameraFocalPx / BydCameraWideFocalPx  settings page focal, wins when non-zero
    SUNNYPILOT_PC_CAMERA_FOCAL               narrow-road focal in pixels at that width
    SUNNYPILOT_PC_CAMERA_WIDE_FOCAL          wide-road/cabin focal, defaults to the
                                             device camera's field of view

  A measured field of view outranks a datasheet one because it is the quantity
  the driver can check with a tape measure. A datasheet diagonal outranks the
  focal + sensor pair because the pair only becomes pixels once someone assumes
  an aspect ratio for the imaging area, whereas the diagonal FOV describes the
  frame directly. Both spec-sheet routes are approximate: on this lens the
  datasheet pair (8 mm, 1/2.7 inch) resolves to 2040 px while the measured field
  of view resolves to 1864 px. Whatever source wins overrides the built-in value
  only, never the published frame size.
  """
  width = int(os.getenv("WEBCAM_OUTPUT_WIDTH", os.getenv("WEBCAM_WIDTH", "1344")))
  height = int(os.getenv("WEBCAM_OUTPUT_HEIGHT", os.getenv("WEBCAM_HEIGHT", "760")))
  scale = width / _ar_ox_config.narrow_road.width

  narrow_fov = _param_override("BydCameraFovTenthsDeg") / 10.0
  wide_fov = _param_override("BydCameraWideFovTenthsDeg") / 10.0
  narrow_dfov = _param_override("BydCameraDiagFovTenthsDeg") / 10.0
  wide_dfov = _param_override("BydCameraWideDiagFovTenthsDeg") / 10.0

  if narrow_fov > 0:
    narrow = focal_from_hfov(narrow_fov, width)
  elif narrow_dfov > 0:
    narrow = focal_from_dfov(narrow_dfov, width, height)
  else:
    narrow = (_spec_focal_px("BydCameraFocalTenthMm", "BydCameraSensorWidthTenthMm", width) or
              _param_override("BydCameraFocalPx") or
              float(os.getenv("SUNNYPILOT_PC_CAMERA_FOCAL", _ar_ox_config.narrow_road.focal_length * scale)))

  if wide_fov > 0:
    wide = focal_from_hfov(wide_fov, width)
  elif wide_dfov > 0:
    wide = focal_from_dfov(wide_dfov, width, height)
  else:
    wide = (_spec_focal_px("BydCameraWideFocalTenthMm", "BydCameraWideSensorWidthTenthMm", width) or
            _param_override("BydCameraWideFocalPx") or
            float(os.getenv("SUNNYPILOT_PC_CAMERA_WIDE_FOCAL", _ar_ox_config.wide_road.focal_length * scale)))

  return DeviceCameraConfig(CameraConfig(width, height, narrow), CameraConfig(width, height, wide), CameraConfig(width, height, wide))

DEVICE_CAMERAS: dict[tuple[str, str], DeviceCameraConfig] = {
  # A "device camera" is defined by a device type and sensor

  # sensor type was never set on eon/neo/two
  ("neo", "unknown"): _neo_config,
  # unknown here is AR0231, field was added with OX03C10 support
  ("tici", "unknown"): _ar_ox_config,

  # before deviceState.deviceType was set, assume tici AR config
  ("unknown", "ar0231"): _ar_ox_config,
  ("unknown", "ox03c10"): _ar_ox_config,

  # desktop: intrinsics follow the webcam output size, see _pc_camera_config
  ("pc", "unknown"): _pc_camera_config(),
}
prods = itertools.product(('tici', 'tizi', 'mici'), (('ar0231', _ar_ox_config), ('ox03c10', _ar_ox_config), ('os04c10', _os_config)))
DEVICE_CAMERAS.update({(d, c[0]): c[1] for d, c in prods})


def device_camera_config(device_type: str, sensor: str) -> DeviceCameraConfig:
  """Camera intrinsics for a (device, sensor) pair, with a safe fallback.

  A direct ``DEVICE_CAMERAS[key]`` lookup raises KeyError for a new device or an
  unknown sensor string, which kills modeld on the first frame. The fallback is
  the AR/OX configuration the pre-deviceType path already used; it is a better
  default than no camera at all.
  """
  return DEVICE_CAMERAS.get((str(device_type), str(sensor)), DEVICE_CAMERAS[("tici", "unknown")])

# device/mesh : x->forward, y-> right, z->down
# view : x->right, y->down, z->forward
device_frame_from_view_frame = np.array([
  [ 0.,  0.,  1.],
  [ 1.,  0.,  0.],
  [ 0.,  1.,  0.]
])
view_frame_from_device_frame = device_frame_from_view_frame.T


# aka 'extrinsic_matrix'
# road : x->forward, y -> left, z->up
def get_view_frame_from_road_frame(roll, pitch, yaw, height):
  device_from_road = orient.rot_from_euler([roll, pitch, yaw]).dot(np.diag([1, -1, -1]))
  view_from_road = view_frame_from_device_frame.dot(device_from_road)
  return np.hstack((view_from_road, [[0], [height], [0]]))



# aka 'extrinsic_matrix'
def get_view_frame_from_calib_frame(roll, pitch, yaw, height):
  device_from_calib= orient.rot_from_euler([roll, pitch, yaw])
  view_from_calib = view_frame_from_device_frame.dot(device_from_calib)
  return np.hstack((view_from_calib, [[0], [height], [0]]))


def vp_from_ke(m):
  """
  Computes the vanishing point from the product of the intrinsic and extrinsic
  matrices C = KE.

  The vanishing point is defined as lim x->infinity C (x, 0, 0, 1).T
  """
  return (m[0, 0]/m[2, 0], m[1, 0]/m[2, 0])


def roll_from_ke(m):
  # note: different from calibration.h/RollAnglefromKE: i think that one's just wrong
  return np.arctan2(-(m[1, 0] - m[1, 1] * m[2, 0] / m[2, 1]),
                    -(m[0, 0] - m[0, 1] * m[2, 0] / m[2, 1]))


def normalize(img_pts, intrinsics):
  # normalizes image coordinates
  # accepts single pt or array of pts
  intrinsics_inv = np.linalg.inv(intrinsics)
  img_pts = np.array(img_pts)
  input_shape = img_pts.shape
  img_pts = np.atleast_2d(img_pts)
  img_pts = np.hstack((img_pts, np.ones((img_pts.shape[0], 1))))
  img_pts_normalized = img_pts.dot(intrinsics_inv.T)
  img_pts_normalized[(img_pts < 0).any(axis=1)] = np.nan
  return img_pts_normalized[:, :2].reshape(input_shape)


def denormalize(img_pts, intrinsics, width=np.inf, height=np.inf):
  # denormalizes image coordinates
  # accepts single pt or array of pts
  img_pts = np.array(img_pts)
  input_shape = img_pts.shape
  img_pts = np.atleast_2d(img_pts)
  img_pts = np.hstack((img_pts, np.ones((img_pts.shape[0], 1), dtype=img_pts.dtype)))
  img_pts_denormalized = img_pts.dot(intrinsics.T)
  if np.isfinite(width):
    img_pts_denormalized[img_pts_denormalized[:, 0] > width] = np.nan
    img_pts_denormalized[img_pts_denormalized[:, 0] < 0] = np.nan
  if np.isfinite(height):
    img_pts_denormalized[img_pts_denormalized[:, 1] > height] = np.nan
    img_pts_denormalized[img_pts_denormalized[:, 1] < 0] = np.nan
  return img_pts_denormalized[:, :2].reshape(input_shape)


def get_calib_from_vp(vp, intrinsics):
  vp_norm = normalize(vp, intrinsics)
  yaw_calib = np.arctan(vp_norm[0])
  pitch_calib = -np.arctan(vp_norm[1]*np.cos(yaw_calib))
  roll_calib = 0
  return roll_calib, pitch_calib, yaw_calib


def device_from_ecef(pos_ecef, orientation_ecef, pt_ecef):
  # device from ecef frame
  # device frame is x -> forward, y-> right, z -> down
  # accepts single pt or array of pts
  input_shape = pt_ecef.shape
  pt_ecef = np.atleast_2d(pt_ecef)
  ecef_from_device_rot = orient.rotations_from_quats(orientation_ecef)
  device_from_ecef_rot = ecef_from_device_rot.T
  pt_ecef_rel = pt_ecef - pos_ecef
  pt_device = np.einsum('jk,ik->ij', device_from_ecef_rot, pt_ecef_rel)
  return pt_device.reshape(input_shape)


def img_from_device(pt_device):
  # img coordinates from pts in device frame
  # first transforms to view frame, then to img coords
  # accepts single pt or array of pts
  input_shape = pt_device.shape
  pt_device = np.atleast_2d(pt_device)
  pt_view = np.einsum('jk,ik->ij', view_frame_from_device_frame, pt_device)

  # This function should never return negative depths
  pt_view[pt_view[:, 2] < 0] = np.nan

  pt_img = pt_view/pt_view[:, 2:3]
  return pt_img.reshape(input_shape)[:, :2]
