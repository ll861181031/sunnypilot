#!/usr/bin/env python3
"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""

from collections.abc import Callable
from functools import partial
import os
os.environ['GMMU'] = '0'
import numpy as np
import threading
import time
from setproctitle import setproctitle
from tinygrad.tensor import Tensor

import openpilot.cereal.messaging as messaging
from openpilot.common.hardware import COMMA_HARDWARE
from openpilot.selfdrive.modeld.helpers import chestnut_present
from openpilot.cereal import log
from opendbc.car.structs import car
from openpilot.cereal.services import SERVICE_LIST
from openpilot.cereal.messaging import PubMaster, SubMaster
from openpilot.cereal.visionipc import VisionStreamType
from msgq.visionipc import VisionIpcClient, VisionBuf
from opendbc.car.car_helpers import get_demo_car_params
from openpilot.common.file_chunker import open_file_chunked
from openpilot.common.swaglog import cloudlog
from openpilot.common.params import Params
from openpilot.common.filter_simple import FirstOrderFilter
from openpilot.common.realtime import config_realtime_process, DT_MDL
from openpilot.common.transformations.camera import device_camera_config
from openpilot.common.transformations.model import get_warp_matrix
from openpilot.system import sentry
from openpilot.system.camerad.cameras.nv12_info import get_nv12_info
from openpilot.selfdrive.controls.lib.desire_helper import DesireHelper
from openpilot.selfdrive.controls.lib.drive_helpers import get_accel_from_plan, get_curvature_from_plan, smooth_value
from openpilot.selfdrive.modeld.modeld import ChestnutState, extra_camera_geometry_matches_model, lateral_lookahead_time
from openpilot.selfdrive.modeld.extra_frame_selection import ExtraFrameChanged, ExtraFrameSelector, copy_stable_frame

from openpilot.selfdrive.modeld.compile_modeld import (
  MODELD_INPUTS,
  is_nav_model,
  make_input_queues as make_stock_input_queues,
)
from openpilot.sunnypilot.modeld_v2.fill_model_msg import fill_model_msg, fill_pose_msg, PublishState, get_curvature_from_output
from openpilot.sunnypilot.modeld_v2.parse_model_outputs import Parser
from openpilot.sunnypilot.modeld_v2.constants import ModelConstants, Plan
from openpilot.sunnypilot.modeld_v2.meta_helper import load_meta_constants
from openpilot.sunnypilot.modeld_v2.camera_offset_helper import CameraOffsetHelper
from openpilot.sunnypilot.modeld_v2.compile_modeld import (derive_frame_skip, make_split_input_queues,
                                                           make_supercombo_input_queues, nv12_copy_size,
                                                           WARP_INPUTS, POLICY_INPUTS)
from openpilot.sunnypilot.livedelay.helpers import select_lat_delay, lateral_action_time, fill_lateral_delay_debug
from openpilot.sunnypilot.modeld_v2.modeld_base import ModelStateBase
from openpilot.sunnypilot.modeld_v2.helpers import load_oob
from openpilot.sunnypilot.models.helpers import get_active_bundle
from openpilot.sunnypilot.selfdrive.controls.lib.blendful import Blendful
from openpilot.sunnypilot.selfdrive.controls.lib.relc import RoadEdgeLaneChangeController

PROCESS_NAME = "openpilot.selfdrive.modeld.modeld_tinygrad"
BIG_MODEL_TIMEOUT = 60
# Consecutive frames without a usable wide frame before the second input is
# abandoned. camerad tolerates 1.5 s of missed reads, so this sits beyond that.
MAX_EXTRA_MISSES = 60
# Two free-running UVC cameras cannot be phase-locked; half a frame at 20 fps is
# the achievable bound, not the 10 ms this file used to log against.
EXTRA_SYNC_TOLERANCE_NS = 25_000_000


def _pkl_exists(path):
  from openpilot.common.file_chunker import get_manifest_path
  return os.path.exists(path) or os.path.exists(get_manifest_path(path))


def _find_driving_pkl(bundle):
  if (override := os.environ.get('COMBINED_MODEL_PKL')) and _pkl_exists(override):
    return override
  if bundle is None or not bundle.models:
    return None
  from openpilot.common.hardware.hw import Paths
  model_root = Paths.model_root()

  pkl_name = bundle.models[0].artifact.fileName
  pkl_path = os.path.join(model_root, pkl_name)
  if _pkl_exists(pkl_path):
    return pkl_path
  return None


class FrameMeta:
  frame_id: int = 0
  timestamp_sof: int = 0
  timestamp_eof: int = 0

  def __init__(self, vipc=None):
    if vipc is not None:
      self.frame_id, self.timestamp_sof, self.timestamp_eof = vipc.frame_id, vipc.timestamp_sof, vipc.timestamp_eof


class ModelState(ModelStateBase):
  inputs: dict[str, np.ndarray]
  prev_desire: np.ndarray

  def __init__(self, cam_w: int, cam_h: int, chestnut: bool = False):
    ModelStateBase.__init__(self)

    env_pkl = os.environ.get('COMBINED_MODEL_PKL')
    if env_pkl and os.path.exists(env_pkl):
      model_bundle = None
    else:
      model_bundle = get_active_bundle(chestnut=chestnut)
    self.generation = model_bundle.generation if model_bundle is not None else None
    overrides = {override.key: override.value for override in model_bundle.overrides} if model_bundle else {}

    self.LAT_SMOOTH_SECONDS = float(overrides.get('lat', ".0"))
    self.LONG_SMOOTH_SECONDS = float(overrides.get('long', ".0"))
    self.MIN_LAT_CONTROL_SPEED = 0.3
    self.PLANPLUS_CONTROL: float = 1.0
    self.chestnut = chestnut

    pkl_path = _find_driving_pkl(model_bundle)
    assert pkl_path is not None, f"No driving pkl found for {'chestnut' if chestnut else 'small model'} — all models must be compiled with compile_modeld.py"
    self._init_combined(pkl_path, cam_w, cam_h, model_bundle)

  def _init_combined(self, pkl_path, cam_w, cam_h, bundle):
    cloudlog.warning(f"loading combined pkl: {pkl_path}")
    jits = load_oob(open_file_chunked(pkl_path))

    metadata = jits['metadata']
    self.WARP_DEV = metadata.get('warp_dev', 'QCOM') if COMMA_HARDWARE else 'CPU'
    self.DEV = ('AMD' if self.chestnut else 'QCOM') if COMMA_HARDWARE else 'CPU'
    self.QUEUE_DEV = self.DEV
    self.is_run_model = 'run_model' in jits

    nv12_info = get_nv12_info(cam_w, cam_h)
    self.frame_copy_size = nv12_copy_size(*nv12_info[:3])
    self.full_frames: dict = {}
    self._blob_cache: dict = {}
    self.frame_buffers: dict = {}

    if self.is_run_model or 'model' in metadata:
      model_metadata = metadata.get('model', metadata)
      self.input_shapes = model_metadata['input_shapes']
      self.modeld_inputs = model_metadata.get('modeld_inputs', MODELD_INPUTS)
      if is_nav_model(self.input_shapes):
        raise RuntimeError("nav (map/navigation) models need the stock modeld; modeld_v2 cannot feed nav_map/navigation")
      self.vision_output_slices = model_metadata['output_slices']
      self.policy_output_slices = {}
      self._policy_slices_list = []
      self._combined_model_type = 'supercombo'
      self._vision_input_names = [key for key in self.input_shapes if 'img' in key]
      self.frame_skip = derive_frame_skip({}, self.input_shapes)
      if self.is_run_model:
        self.input_queues, self.numpy_inputs, self.frame_buffers = make_stock_input_queues(
          self.input_shapes, self.frame_skip, device=self.DEV, frame_copy_size=self.frame_copy_size,
          input_dtypes=model_metadata.get('input_dtypes'))
        self.frame_views, self.npy = self.frame_buffers, self.numpy_inputs
        self.run_model, self.run_policy, self.warp = jits['run_model'][(cam_w, cam_h)], None, None
      else:
        self.input_queues, self.numpy_inputs = make_supercombo_input_queues(self.input_shapes, self.frame_skip, device=self.QUEUE_DEV)
        self.run_model, self.run_policy, self.warp = None, jits['run_policy'], jits[(cam_w, cam_h)]
    else:
      self.run_model, self.run_policy, self.warp = None, jits['run_policy'], jits[(cam_w, cam_h)]
      vision_metadata = metadata['vision']
      policy_keys = [k for k in metadata if k not in ('vision', 'warp_dev')]
      self._combined_model_type = 'split' if policy_keys == ['policy'] else 'multi_policy'
      self.vision_output_slices = vision_metadata['output_slices']
      self._policy_keys = policy_keys
      self._policy_slices_list = [metadata[k]['output_slices'] for k in policy_keys]
      self.policy_output_slices = self._policy_slices_list[0]
      self._has_on_policy = any('on' in k.lower() for k in policy_keys)
      self._vision_input_names = [key for key in vision_metadata['input_shapes'] if 'img' in key]
      first_policy_meta = metadata[policy_keys[0]]
      frame_skip = derive_frame_skip(vision_metadata['input_shapes'], first_policy_meta['input_shapes'])
      self.input_queues, self.numpy_inputs = make_split_input_queues(vision_metadata['input_shapes'],
                                                                     first_policy_meta['input_shapes'],
                                                                     frame_skip, device=self.QUEUE_DEV)

    self._desire_key = next(key for key in self.numpy_inputs if key.startswith('desire'))
    self._road_key = next(key for key in self._vision_input_names if 'big' not in key)
    self._wide_key = next(key for key in self._vision_input_names if 'big' in key)
    self.frame_buf_params = dict.fromkeys(self._vision_input_names, nv12_info)

    is_20hz = bundle.is20hz if bundle else self._combined_model_type in ('split', 'multi_policy')
    if is_20hz:
      from openpilot.sunnypilot.models.split_model_constants import SplitModelConstants
      self.constants = SplitModelConstants()
    else:
      self.constants = ModelConstants()

    self.parser = Parser()
    self.prev_desire = np.zeros(self.constants.DESIRE_LEN, dtype=np.float32)

    if self.warp is not None:
      self.full_frames = {k: Tensor(np.zeros(nv12_info[3], dtype=np.uint8), device=self.WARP_DEV).contiguous().realize() for k in self._vision_input_names}
      self.warp(**{k: self.input_queues[k] for k in WARP_INPUTS}, frame=self.full_frames[self._road_key], big_frame=self.full_frames[self._wide_key])

  def warmup(self) -> None:
    dummy_size = self.frame_copy_size if self.is_run_model else self.frame_buf_params[self._road_key][3]
    dummy_frames = {k: np.zeros(dummy_size, dtype=np.uint8) for k in self._vision_input_names}
    transforms = {k: np.eye(3, dtype=np.float32) for k in [self._road_key, self._wide_key] if k}
    dummy_inputs = {k: np.zeros(v.shape, dtype=v.dtype) for k, v in self.numpy_inputs.items() if k not in ['tfm', 'big_tfm', 'prev_feat']}
    self.run(dummy_frames, transforms, dummy_inputs)
    if self.is_run_model:
      self.input_queues, self.numpy_inputs, self.frame_buffers = make_stock_input_queues(
        self.input_shapes, self.frame_skip, device=self.DEV, frame_copy_size=self.frame_copy_size)
      self.frame_views = self.frame_buffers
      self.npy = self.numpy_inputs
    else:
      for v in self.numpy_inputs.values():
        v[:] = 0
      self.full_frames.clear()
      self._blob_cache.clear()
    self.prev_desire[:] = 0

  @property
  def mlsim(self) -> bool:
    return bool(self.generation is not None and self.generation >= 11)

  @property
  def vision_input_names(self) -> list[str]:
    return self._vision_input_names

  @property
  def desire_key(self) -> str:
    return self._desire_key

  def run(self, bufs: dict[str, VisionBuf], transforms: dict[str, np.ndarray],
          inputs: dict[str, np.ndarray],
          after_enqueue: Callable[[], None] | None = None,
          frame_ids: dict[str, int] | None = None) -> dict[str, np.ndarray] | None:
    frame_ids = frame_ids or {}
    if self.is_run_model:
      frame_copies = {}
      for key, buf in bufs.items():
        if key in frame_ids:
          frame_copies[key] = copy_stable_frame(buf, frame_ids[key], self.frame_copy_size)
        else:
          data = buf.data if hasattr(buf, 'data') else buf
          frame_copies[key] = np.array(np.frombuffer(data, dtype=np.uint8, count=self.frame_copy_size), copy=True)
      for key, data in frame_copies.items():
        np.copyto(self.frame_buffers[key], data)
    else:
      for key, buf in bufs.items():
        ptr = np.frombuffer(buf.data, dtype=np.uint8).ctypes.data
        cache_key = (key, ptr)
        if cache_key not in self._blob_cache:
          self._blob_cache[cache_key] = Tensor.from_blob(ptr, (self.frame_buf_params[key][3],), dtype='uint8', device=self.WARP_DEV)
        self.full_frames[key] = self._blob_cache[cache_key]

    desire_key = self.desire_key
    inputs[desire_key][0] = 0
    self.numpy_inputs[desire_key][:] = np.where(inputs[desire_key] - self.prev_desire > .99, inputs[desire_key], 0)
    self.prev_desire[:] = inputs[desire_key]

    for key in ('traffic_convention', 'lateral_control_params', 'action_t'):
      if key in self.numpy_inputs and key in inputs:
        self.numpy_inputs[key][:] = inputs[key]

    self.numpy_inputs['tfm'][:, :] = transforms[self._road_key].reshape(3, 3)
    self.numpy_inputs['big_tfm'][:, :] = transforms[self._wide_key].reshape(3, 3)

    if self.run_model is not None:
      outs, = self.run_model(**{k: self.input_queues[k] for k in self.modeld_inputs})
      raw_outputs = outs
    else:
      assert self.warp is not None and self.run_policy is not None
      warped = self.warp(**{k: self.input_queues[k] for k in WARP_INPUTS}, frame=self.full_frames[self._road_key], big_frame=self.full_frames[self._wide_key])
      raw_outputs = self.run_policy(**{k: self.input_queues[k] for k in POLICY_INPUTS if k in self.input_queues}, warped=warped)

    if after_enqueue is not None:
      after_enqueue()

    if self._combined_model_type == 'supercombo':
      model_output = raw_outputs.numpy().flatten()
      if self.chestnut and not np.all(np.isfinite(model_output)):
        raise RuntimeError("model output not finite")
      sliced = {k: model_output[np.newaxis, v] for k, v in self.vision_output_slices.items()}
      outputs = self.parser.parse_outputs(sliced)
      if 'prev_feat' in self.numpy_inputs and 'hidden_state' in self.vision_output_slices:
        self.numpy_inputs['prev_feat'][:] = model_output[self.vision_output_slices['hidden_state']]
    else:
      vision_output = raw_outputs[0].numpy().flatten()
      vision_sliced = {k: vision_output[np.newaxis, v] for k, v in self.vision_output_slices.items()}
      outputs = self.parser.parse_vision_outputs(vision_sliced)

      if 'prev_feat' in self.numpy_inputs and 'hidden_state' in self.vision_output_slices:
        self.numpy_inputs['prev_feat'][:] = vision_output[self.vision_output_slices['hidden_state']]

      for i, policy_slices in enumerate(self._policy_slices_list):
        policy_output = raw_outputs[i + 1].numpy().flatten()
        policy_sliced = {k: policy_output[np.newaxis, v] for k, v in policy_slices.items()}
        parsed = self.parser.parse_policy_outputs(policy_sliced)
        if ('off' in self._policy_keys[i]
          and self._has_on_policy
          and any('plan' in self._policy_slices_list[j] for j, k in enumerate(self._policy_keys) if 'on' in k.lower())):

          parsed.pop('plan', None)

        outputs.update(parsed)

      if 'planplus' in outputs and 'plan' in outputs:
        outputs['plan'] = outputs['plan'] + outputs['planplus']

    if 'desired_curvature' in outputs and 'prev_desired_curv' in self.numpy_inputs:
      buf = self.numpy_inputs['prev_desired_curv']
      buf[0, :-1] = buf[0, 1:]
      buf[0, -1, :] = outputs['desired_curvature'][0, :] if not self.mlsim else 0

    return outputs

  def get_action_from_model(self, model_output: dict[str, np.ndarray], prev_action: log.ModelDataV2.Action,
                            lat_action_t: float, long_action_t: float, v_ego: float,
                            blendful: Callable[[float], float] | None = None) -> log.ModelDataV2.Action:
    if 'action' not in model_output:
      plan = model_output['plan'][0]
      desired_accel = get_accel_from_plan(plan[:, Plan.VELOCITY][:, 0], plan[:, Plan.ACCELERATION][:, 0], self.constants.T_IDXS,
                                          action_t=long_action_t)

      curvature_plan = (plan + (self.PLANPLUS_CONTROL - 1.0) * model_output['planplus'][0]
                        if 'planplus' in model_output and self.PLANPLUS_CONTROL != 1.0 else plan)
      desired_curvature = get_curvature_from_output(model_output, curvature_plan, v_ego, lat_action_t, self.mlsim)
    else:
      desired_accel = model_output['action'][0, 1]
      # Same lateral lookahead as the stock modeld path: sample the plan 5 m ahead so a
      # low-speed corner is entered while it is still 5 m away. See the constants and
      # the route 000001af/000001b0 evidence next to lateral_lookahead_time.
      tau = None
      plan = model_output.get('plan')
      if plan is not None and len(plan) and np.ndim(plan) == 3:
        tau = lateral_lookahead_time(np.asarray(plan[0][:, Plan.POSITION][:, 0], dtype=np.float64),
                                     np.asarray(self.constants.T_IDXS, dtype=np.float64))
        if tau is not None:
          # Never look less far ahead than the head already did; see the stock path.
          tau = max(tau, float(lat_action_t))
      if tau is None:
        desired_curvature = model_output['action'][0, 0] / (max(1.0, v_ego))**2
      else:
        # Same planplus blend as the branch above: only the horizon changes here, the
        # curvature source stays whatever the model and PlanplusControl ask for.
        curvature_plan = (plan[0] + (self.PLANPLUS_CONTROL - 1.0) * model_output['planplus'][0]
                          if 'planplus' in model_output and self.PLANPLUS_CONTROL != 1.0 else plan[0])
        # get_curvature_from_plan directly, not get_curvature_from_output: the latter
        # prefers a baked-in `desired_curvature`, which would silently ignore the
        # horizon this branch just chose and make the two backends disagree again.
        desired_curvature = get_curvature_from_plan(curvature_plan[:, Plan.T_FROM_CURRENT_EULER][:, 2],
                                                    curvature_plan[:, Plan.ORIENTATION_RATE][:, 2],
                                                    self.constants.T_IDXS, v_ego, tau)

    # Same position as the stock modeld path: before smoothing, so the correction is
    # smoothed like any other curvature source. Blendful skips low speeds internally,
    # so passing the callable unconditionally is safe.
    if blendful is not None:
      desired_curvature += blendful(desired_curvature)

    stop = v_ego < 0.3 and desired_accel < 0.1
    desired_accel = smooth_value(desired_accel, prev_action.desiredAcceleration, self.LONG_SMOOTH_SECONDS)

    if self.generation is not None and self.generation >= 10: # smooth curvature for post FOF models
      if v_ego > self.MIN_LAT_CONTROL_SPEED:
        desired_curvature = smooth_value(desired_curvature, prev_action.desiredCurvature, self.LAT_SMOOTH_SECONDS)
      else:
        desired_curvature = prev_action.desiredCurvature

    return log.ModelDataV2.Action(desiredCurvature=float(desired_curvature), desiredAcceleration=float(desired_accel), shouldStop=bool(stop))


def main(demo=False):
  cloudlog.warning("modeld init")

  sentry.set_tag("daemon", PROCESS_NAME)
  cloudlog.bind(daemon=PROCESS_NAME)
  setproctitle(PROCESS_NAME)
  config_realtime_process(7, 54)

  CHESTNUT = chestnut_present()
  if CHESTNUT:
    os.environ['HCQDEV_WAIT_TIMEOUT_MS'] = '3000'

  params = Params()
  params.put_bool("ChestnutLoading", CHESTNUT)
  params.remove("ChestnutActive")

  # visionipc clients
  while True:
    available_streams = VisionIpcClient.available_streams("camerad", block=False)
    if available_streams:
      use_extra_client = VisionStreamType.VISION_STREAM_WIDE_ROAD in available_streams and VisionStreamType.VISION_STREAM_NARROW_ROAD in available_streams
      main_wide_camera = VisionStreamType.VISION_STREAM_NARROW_ROAD not in available_streams
      break
    time.sleep(.1)

  vipc_client_main_stream = VisionStreamType.VISION_STREAM_WIDE_ROAD if main_wide_camera else VisionStreamType.VISION_STREAM_NARROW_ROAD
  vipc_client_main = VisionIpcClient("camerad", vipc_client_main_stream, True)
  vipc_client_extra = VisionIpcClient("camerad", VisionStreamType.VISION_STREAM_WIDE_ROAD, False)
  cloudlog.warning(f"vision stream set up, main_wide_camera: {main_wide_camera}, use_extra_client: {use_extra_client}")

  while not vipc_client_main.connect(False):
    time.sleep(0.1)
  while use_extra_client and not vipc_client_extra.connect(False):
    time.sleep(0.1)

  cloudlog.warning(f"connected main cam with buffer size: {vipc_client_main.buffer_len} ({vipc_client_main.width} x {vipc_client_main.height})")
  if use_extra_client:
    cloudlog.warning(f"connected extra cam with buffer size: {vipc_client_extra.buffer_len} ({vipc_client_extra.width} x {vipc_client_extra.height})")
    # 两路共用按主摄尺寸编译的图像输入，尺寸不同会使第二路读到错误的像素平面。
    if not extra_camera_geometry_matches_model((vipc_client_main.width, vipc_client_main.height),
                                               (vipc_client_extra.width, vipc_client_extra.height)):
      cloudlog.error("extra cam %dx%d does not match the model's %dx%d; dropping the extra input",
                     vipc_client_extra.width, vipc_client_extra.height,
                     vipc_client_main.width, vipc_client_main.height)
      use_extra_client = False

  cloudlog.warning("loading model")
  st = time.monotonic()

  model = None
  if CHESTNUT:
    big_model = None
    def load_big():
      nonlocal big_model
      try:
        m = ModelState(cam_w=vipc_client_main.width, cam_h=vipc_client_main.height, chestnut=True)
        m.warmup()
        big_model = m
      except Exception:
        cloudlog.exception("chestnut load failed")
    loader = threading.Thread(target=load_big, daemon=True)
    loader.start()
    loader.join(BIG_MODEL_TIMEOUT)
    model = big_model
    if model is None:
      params.put_bool("ChestnutModelError", True)
    params.put_bool("ChestnutActive", model is not None)
    if model is not None:
      params.remove("ChestnutModelError")

  small_model = ModelState(cam_w=vipc_client_main.width, cam_h=vipc_client_main.height, chestnut=False) if model is None or CHESTNUT else None
  if model is None:
    model = small_model
  params.put_bool("ChestnutLoading", False)
  assert model is not None
  cloudlog.warning(f"models loaded in {time.monotonic() - st:.1f}s, modeld starting")

  # messaging
  pub_socks = ["modelV2", "drivingModelData", "cameraOdometry", "modelDataV2SP"] + (["chestnutState"] if CHESTNUT else [])
  pm = PubMaster(pub_socks)
  sm = SubMaster(["deviceState", "carState", "narrowRoadCameraState", "extrinsicsCalibration",
                  "driverMonitoringState", "carControl", "lateralDelay", "carStateSP"])

  publish_state = PublishState()
  try:
    lane_line_check = int(params.get("LaneLineCheck") or 0)
  except (TypeError, ValueError):
    lane_line_check = 0
  chestnut_state = ChestnutState(pm, model.chestnut) if CHESTNUT else None

  # setup filter to track dropped frames
  frame_dropped_filter = FirstOrderFilter(0., 10., 1. / model.constants.MODEL_FREQ)
  frame_id = 0
  last_vipc_frame_id = 0
  run_count = 0

  model_transform_main = np.zeros((3, 3), dtype=np.float32)
  model_transform_extra = np.zeros((3, 3), dtype=np.float32)
  model_transform_extra_fallback = np.zeros((3, 3), dtype=np.float32)
  extra_wide_prev = None
  live_calib_seen = False
  buf_main, buf_extra = None, None
  meta_main = FrameMeta()
  meta_extra = FrameMeta()
  extra_frame_selector = ExtraFrameSelector(EXTRA_SYNC_TOLERANCE_NS)
  camera_offset_helper = CameraOffsetHelper()
  # Second-input bookkeeping, same semantics as the stock modeld path: a configured
  # wide stream only counts as "in use" once its frames actually reach the model.
  wide_cam_misses = 0
  wide_cam_reported = None
  wide_on_frames = 0
  wide_off_frames = 0
  wide_active = False


  if demo:
    CP = get_demo_car_params()
  else:
    CP = messaging.log_from_bytes(params.get("CarParams", block=True), car.CarParams)
  cloudlog.info("modeld got CarParams: %s", CP.brand)

  # TODO Move smooth seconds to action function
  long_delay = CP.longitudinalActuatorDelay + model.LONG_SMOOTH_SECONDS
  prev_action = log.ModelDataV2.Action()

  DH = DesireHelper()
  meta_constants = load_meta_constants()
  RELC = RoadEdgeLaneChangeController()
  blendful = Blendful()
  blendful_enabled = params.get_bool("Blendful")
  if blendful_enabled:
    cloudlog.warning("blendful enabled")

  while True:
    # The main camera is the clock. Never block on the optional extra stream: doing
    # that lets main VIPC fill up and is what produced frameDropPerc > 1% on this rig.
    buf_main = vipc_client_main.recv()
    meta_main = FrameMeta(vipc_client_main)
    if buf_main is None:
      cloudlog.debug("vipc_client_main no frame")
      continue

    extra_is_wide = False
    if use_extra_client:
      # Choose the closest arrived candidate; retaining only the newest frame
      # unnecessarily falls back when an older candidate is the better pair.
      selected_extra = extra_frame_selector.select(vipc_client_extra, meta_main.timestamp_sof)
      if selected_extra is not None:
        buf_extra = selected_extra.buffer
        meta_extra = selected_extra.meta
        extra_is_wide = True
        wide_cam_misses = 0
      else:
        buf_extra = buf_main
        meta_extra = meta_main
        wide_cam_misses = extra_frame_selector.consecutive_misses
        if wide_cam_misses > MAX_EXTRA_MISSES:
          cloudlog.error("wide camera stopped delivering; falling back to single camera")
          use_extra_client = False
          wide_cam_misses = 0
    else:
      # Use single camera
      buf_extra = buf_main
      meta_extra = meta_main

    # Publish what the model actually received, not the stream configuration. A wide
    # camera whose pairs all miss the sync window contributes nothing, and the HUD
    # would otherwise claim the model is looking through it. 10 frames of hysteresis
    # at 20 Hz keeps a single late pair from flapping the indicator.
    if extra_is_wide:
      wide_on_frames += 1
      wide_off_frames = 0
    else:
      wide_off_frames += 1
      wide_on_frames = 0

    if wide_on_frames >= 10:
      wide_active = True
    elif wide_off_frames >= 10 or not use_extra_client:
      wide_active = False

    if wide_active != wide_cam_reported:
      params.put_bool("WideCameraActive", wide_active)
      wide_cam_reported = wide_active

    sm.update(0)
    desire = DH.desire
    is_rhd = sm["driverMonitoringState"].isRHD
    frame_id = sm["narrowRoadCameraState"].frameId
    v_ego = max(sm["carState"].vEgo, 0.)
    if sm.frame % 60 == 0:
      model.lat_delay, model.lat_delay_source = select_lat_delay(params, sm["lateralDelay"].lateralDelay)
      model.PLANPLUS_CONTROL = params.get("PlanplusControl", return_default=True)
      camera_offset_helper.set_offset(params.get("CameraOffset", return_default=True))
    lat_delay = model.lat_delay + model.LAT_SMOOTH_SECONDS
    if ((sm.updated["extrinsicsCalibration"] or extra_wide_prev is None or extra_is_wide != extra_wide_prev) and
        sm.seen['narrowRoadCameraState'] and sm.seen['deviceState']):
      device_from_calib_euler = np.array(sm["extrinsicsCalibration"].rpyCalib, dtype=np.float32)
      # 启动时标定可能为空；使用中性投影，避免崩溃或把零矩阵送入模型。
      if device_from_calib_euler.shape != (3,) or not np.isfinite(device_from_calib_euler).all():
        device_from_calib_euler = np.zeros(3, dtype=np.float32)
      dc = device_camera_config(sm['deviceState'].deviceType, sm['narrowRoadCameraState'].sensor)
      main_intrinsics = dc.wide_road.intrinsics if main_wide_camera else dc.narrow_road.intrinsics
      model_transform_main = get_warp_matrix(device_from_calib_euler, main_intrinsics, False).astype(np.float32)
      # 第二路回退主摄时，镜头内参跟随实际画面；模型的 sbigmodel 目标坐标仍保持不变。
      has_wide_camera = extra_is_wide or main_wide_camera
      extra_intrinsics = dc.wide_road.intrinsics if has_wide_camera else dc.narrow_road.intrinsics
      model_transform_extra = get_warp_matrix(device_from_calib_euler, extra_intrinsics, True).astype(np.float32)
      # 无论这一帧是否真的拿到宽摄，都预先算好“主摄填第二路”的投影。
      # 拷贝阶段发现宽帧被覆盖时可在同一轮立即回退，不必等下一帧重算。
      fallback_intrinsics = dc.wide_road.intrinsics if main_wide_camera else dc.narrow_road.intrinsics
      model_transform_extra_fallback = get_warp_matrix(device_from_calib_euler, fallback_intrinsics, True).astype(np.float32)
      calib_height = sm["extrinsicsCalibration"].height
      height = calib_height[0] if calib_height else 1.22
      model_transform_main, model_transform_extra, model_transform_extra_fallback = camera_offset_helper.update_transforms(
        ((model_transform_main, main_intrinsics), (model_transform_extra, extra_intrinsics),
         (model_transform_extra_fallback, fallback_intrinsics)),
        device_from_calib_euler, height)
      live_calib_seen = True
      extra_wide_prev = extra_is_wide

    traffic_convention = np.zeros(2)
    traffic_convention[int(is_rhd)] = 1

    vec_desire = np.zeros(model.constants.DESIRE_LEN, dtype=np.float32)
    if desire >= 0 and desire < model.constants.DESIRE_LEN:
      vec_desire[desire] = 1

    # tracked dropped frames
    vipc_dropped_frames = max(0, meta_main.frame_id - last_vipc_frame_id - 1)
    frames_dropped = frame_dropped_filter.update(min(vipc_dropped_frames, 10))
    if run_count < 10: # let frame drops warm up
      frame_dropped_filter.x = 0.
      frames_dropped = 0.
    run_count = run_count + 1

    frame_drop_ratio = frames_dropped / (1 + frames_dropped)

    bufs = {name: buf_extra if 'big' in name else buf_main for name in model.vision_input_names}
    frame_is_wide = extra_is_wide
    if extra_is_wide and not extra_frame_selector.is_current(selected_extra):
      # VisionIPC 的宽摄帧是共享内存，主摄准备期间可能已被下一张覆盖。
      # 编号已经变化时整轮退回主摄，避免把新画面配旧时间戳送进模型。
      buf_extra = buf_main
      bufs = dict.fromkeys(model.vision_input_names, buf_main)
      meta_extra = meta_main
      extra_is_wide = False
      wide_active = False
      wide_on_frames = 0
      wide_off_frames += 1
      frame_is_wide = False
      if use_extra_client and wide_off_frames < 10:
        extra_wide_prev = None
    elif extra_wide_prev is True:
      # 回退主摄时第二路必须重新按主摄内参投影，不能沿用上一轮的宽摄矩阵。
      extra_wide_prev = None
      frame_is_wide = False
    transforms = {name: model_transform_extra if 'big' in name else model_transform_main for name in model.vision_input_names}

    frame_delay = DT_MDL # compensate for time passed since the frame was captured: current_time - timestamp_eof is 50ms on average
    action_delay = DT_MDL / 2 # middle of the interval between model output (current state) and next frame (expected state)
    lat_action_t = lateral_action_time(model.lat_delay, model.LAT_SMOOTH_SECONDS, frame_delay, action_delay)
    long_action_t = long_delay + frame_delay + action_delay

    inputs:dict[str, np.ndarray] = {
      model.desire_key: vec_desire,
      'traffic_convention': traffic_convention,
    }

    if 'lateral_control_params' in model.numpy_inputs:
      inputs['lateral_control_params'] = np.array([v_ego, lat_delay], dtype=np.float32)

    if 'action_t' in model.numpy_inputs:
      inputs['action_t'] = np.array([lat_action_t, long_action_t], dtype=np.float32)

    mt1 = time.perf_counter()
    frame_ids = {'img': meta_main.frame_id, 'big_img': meta_extra.frame_id}
    try:
      send_chestnut = (chestnut_state is not None and
                       run_count % round(model.constants.MODEL_FREQ / SERVICE_LIST['chestnutState'].frequency) == 0)
      model_output = model.run(bufs, transforms, inputs, chestnut_state.send if send_chestnut else None,
                               frame_ids=frame_ids)
    except ExtraFrameChanged:
      frame_is_wide = False
      buf_extra = buf_main
      transforms = {name: model_transform_extra_fallback if 'big' in name else model_transform_main
                    for name in model.vision_input_names}
      frame_ids['big_img'] = buf_main.frame_id
      model_output = model.run(dict.fromkeys(model.vision_input_names, buf_main), transforms, inputs,
                               chestnut_state.send if send_chestnut else None, frame_ids=frame_ids)
      meta_extra = meta_main
      extra_is_wide = False
      wide_active = False
      wide_on_frames = 0
      wide_off_frames += 1
      extra_wide_prev = None
    except Exception:
      if not params.get_bool("ChestnutActive"):
        raise
      cloudlog.exception("chestnut failed, falling back to small")
      params.put_bool("ChestnutModelError", True)
      params.put_bool("ChestnutActive", False)
      assert small_model is not None
      model = small_model
      if chestnut_state is not None:
        chestnut_state.big = False
      # Recompute everything derived from the model bundle; otherwise the small
      # model keeps the big model's smoothing constant and metadata.
      long_delay = CP.longitudinalActuatorDelay + model.LONG_SMOOTH_SECONDS
      meta_constants = load_meta_constants()
      blendful_enabled = params.get_bool("Blendful")
      run_count = 0
      model_output = None
    mt2 = time.perf_counter()
    model_execution_time = mt2 - mt1

    if model_output is not None:
      modelv2_send = messaging.new_message('modelV2')
      drivingdata_send = messaging.new_message('drivingModelData')
      posenet_send = messaging.new_message('cameraOdometry')
      mdv2sp_send = messaging.new_message('modelDataV2SP')
      fill_lateral_delay_debug(mdv2sp_send.modelDataV2SP, model, model.LAT_SMOOTH_SECONDS, lat_action_t, meta_main.frame_id,
                              model_output, prev_action.desiredCurvature, v_ego)

      # Only correct while the driver is not asking for a manoeuvre: a lane change or
      # a turn has the model deliberately leaving the lane centre, and pulling it back
      # would fight the intent.
      blendful_active = desire == log.Desire.none and DH.lane_change_state == log.LaneChangeState.off
      action = model.get_action_from_model(model_output, prev_action, lat_action_t, long_action_t, v_ego,
                                           blendful=partial(blendful.update, model_output, v_ego,
                                                            blendful_active, enabled=blendful_enabled))
      # Telemetry so the correction can be inspected onroad instead of inferred from
      # desiredCurvature. All of it is read-only state left by update().
      mdv2sp_send.modelDataV2SP.blendfulCurvature = float(blendful.curvature)
      mdv2sp_send.modelDataV2SP.blendfulTrust = float(blendful.trust)
      mdv2sp_send.modelDataV2SP.modelCurvature = float(blendful.model_curvature)
      mdv2sp_send.modelDataV2SP.blendfulGoingAround = bool(blendful.going_around)
      mdv2sp_send.modelDataV2SP.blendfulLeftProb = float(blendful.l_prob)
      mdv2sp_send.modelDataV2SP.blendfulRightProb = float(blendful.r_prob)
      mdv2sp_send.modelDataV2SP.blendfulLaneWidth = float(blendful.lane_width.x)
      prev_action = action
      fill_model_msg(drivingdata_send, modelv2_send, model_output, action,
                     publish_state, meta_main.frame_id, frame_id if frame_is_wide else meta_main.frame_id, frame_id,
                     frame_drop_ratio, meta_main.timestamp_eof, model_execution_time, live_calib_seen, meta_constants)
      modelv2_send.modelV2.big = model.chestnut

      desire_state = modelv2_send.modelV2.meta.desireState
      l_lane_change_prob = desire_state[log.Desire.laneChangeLeft]
      r_lane_change_prob = desire_state[log.Desire.laneChangeRight]
      lane_change_prob = l_lane_change_prob + r_lane_change_prob
      left_edge, right_edge = RELC.update_and_fill(modelv2_send.modelV2, mdv2sp_send.modelDataV2SP, v_ego)
      DH.update(sm['carState'], sm['carControl'].latActive, lane_change_prob, left_edge, right_edge,
                blinker_latched=sm['carStateSP'].blinkerLatched,
                left_lane_line=sm['carStateSP'].leftLaneLine,
                right_lane_line=sm['carStateSP'].rightLaneLine,
                lane_line_check=lane_line_check,
                blindspot_unavailable_left=sm['carStateSP'].leftBlindspotUnavailable,
                blindspot_unavailable_right=sm['carStateSP'].rightBlindspotUnavailable)
      modelv2_send.modelV2.meta.laneChangeState = DH.lane_change_state
      modelv2_send.modelV2.meta.laneChangeDirection = DH.lane_change_direction
      mdv2sp_send.valid = modelv2_send.valid
      mdv2sp_send.modelDataV2SP.laneTurnDirection = DH.lane_turn_direction
      drivingdata_send.drivingModelData.meta.laneChangeState = DH.lane_change_state
      drivingdata_send.drivingModelData.meta.laneChangeDirection = DH.lane_change_direction

      fill_pose_msg(posenet_send, model_output, meta_main.frame_id, vipc_dropped_frames, meta_main.timestamp_eof, live_calib_seen)
      pm.send('modelV2', modelv2_send)
      pm.send('drivingModelData', drivingdata_send)
      pm.send('cameraOdometry', posenet_send)
      pm.send('modelDataV2SP', mdv2sp_send)
    last_vipc_frame_id = meta_main.frame_id

if __name__ == "__main__":
  try:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--demo', action='store_true', help='A boolean for demo mode.')
    args = parser.parse_args()
    main(demo=args.demo)
  except KeyboardInterrupt:
    cloudlog.warning(f"child {PROCESS_NAME} got SIGINT")
  except Exception:
    sentry.capture_exception()
    raise
