#!/usr/bin/env python3
from collections.abc import Callable
from functools import partial
import ctypes
import contextlib
from functools import cached_property
import math
import os
os.environ['GMMU'] = '0' # for chestnut fast loading, noop for qcom
from tinygrad.device import Device
import usb1
import struct
from array import array
import sys
import subprocess
import socket
import threading
import time
import types
from pathlib import Path
import numpy as np
import openpilot.cereal.messaging as messaging
from openpilot.cereal import log, custom
from opendbc.car.structs import car
from openpilot.cereal.messaging import PubMaster, SubMaster
from openpilot.cereal.services import SERVICE_LIST
from openpilot.cereal.visionipc import VisionStreamType
from msgq.visionipc import VisionIpcClient, VisionBuf
from opendbc.car.car_helpers import get_demo_car_params
from openpilot.common.swaglog import cloudlog
from openpilot.common.params import Params
from openpilot.common.filter_simple import FirstOrderFilter
from openpilot.common.realtime import config_realtime_process, DT_MDL
from openpilot.common.transformations.camera import DEVICE_CAMERAS
from openpilot.system.camerad.cameras.nv12_info import get_nv12_info
from openpilot.common.transformations.model import get_warp_matrix
from openpilot.selfdrive.controls.lib.desire_helper import DesireHelper
from openpilot.selfdrive.controls.lib.drive_helpers import get_accel_from_plan, should_stop, smooth_value, get_curvature_from_plan
from openpilot.selfdrive.modeld.parse_model_outputs import Parser
from openpilot.sunnypilot.selfdrive.controls.lib.blendful import Blendful
from openpilot.selfdrive.modeld.nav_inputs import NavRuntime
from openpilot.selfdrive.navigation.cplink import amap_root_nav_enabled
from openpilot.selfdrive.modeld.nav_evidence import finish_remote_evidence
from openpilot.selfdrive.modeld.nav_evidence_protocol import (LEGACY_MAGIC, RECEIPT_MAGIC, RECEIPT_SIZE, REQUEST_NAV_SESSION, REQUEST_RECEIPT,
                                                           REQUEST_RESET_MAP, REQUEST_RESET_NAVIGATION, RESET_ACK_SIZE, RESET_FLAGS,
                                                           RESET_RECEIPT_MAGIC, crc32_buffer, decode_receipt, decode_reset_ack)
from openpilot.selfdrive.modeld.nav_reset import NAV_QUEUE_KEYS, NAV_STATE_KEYS, NavigationResetMixin
from openpilot.selfdrive.modeld.compile_modeld import (make_input_queues, nv12_copy_size,
                                                        apply_state_update, artifact_inputs, is_big_model, is_nav_model)
from openpilot.selfdrive.modeld.fill_model_msg import fill_model_msg, fill_driving_model_data, fill_pose_msg, PublishState
from openpilot.selfdrive.modeld.extra_frame_selection import ExtraFrameChanged, ExtraFrameSelector, copy_stable_frame
from openpilot.common.hardware.usb import CHESTNUT_USB_IDS
from openpilot.selfdrive.modeld.constants import ModelConstants, Plan
from openpilot.selfdrive.modeld.helpers import chestnut_present, chestnut_compiled, chestnut_ready, load_modeld_jits, model_swap_target

from openpilot.sunnypilot.livedelay.helpers import select_lat_delay, lateral_action_time, fill_lateral_delay_debug
from openpilot.sunnypilot.modeld_v2.camera_offset_helper import CameraOffsetHelper
from openpilot.sunnypilot.modeld_v2.modeld_base import ModelStateBase
from openpilot.sunnypilot.selfdrive.controls.lib.relc import RoadEdgeLaneChangeController

SEND_RAW_PRED = os.getenv('SEND_RAW_PRED')

CPLINK_DESIRE_GATE = "/tmp/cplink_navi_desire"


def cplink_navi_desire_enabled() -> bool:
  """Opt-in gate for CPlink navi turn desire; absent/stale file means off."""
  if amap_root_nav_enabled():
    return False
  try:
    # Compared against a file mtime, which is wall clock - a monotonic source here
    # would compare two different epochs.
    return time.time() - os.path.getmtime(CPLINK_DESIRE_GATE) < 5.0  # noqa: TID251
  except OSError:
    return False


def trusted_navi_turn(runtime, v_ego: float):
  """Map only the navigation action accepted by this frame's model-input gate."""
  if runtime is None or not getattr(runtime, "instruction_valid", False) or v_ego >= 20.0:
    return None
  evidence = runtime.evidence()
  if evidence.get("instructionReason") != "accepted":
    return None
  try:
    distance = float(evidence["adjustedDistance"])
  except (KeyError, TypeError, ValueError, OverflowError):
    return None
  if not math.isfinite(distance) or not 0.0 <= distance < 30.0:
    return None
  command = evidence.get("selectedCommand")
  if command == 15:
    return custom.ModelDataV2SP.TurnDirection.turnLeft
  if command in (9, 10):
    return custom.ModelDataV2SP.TurnDirection.turnRight
  return None


LAT_SMOOTH_SECONDS = 0.0
LONG_SMOOTH_SECONDS = 0.3
MIN_LAT_CONTROL_SPEED = 0.3
BIG_MODEL_TIMEOUT = 60
# Consecutive frames with no frame from the wide stream before the extra input is
# abandoned. camerad tolerates 1.5 s of missed reads before it drops the camera, so
# this sits beyond that: the input is only given up on once camerad has too.
MAX_EXTRA_MISSES = 60
# Two free-running UVC cameras cannot be phase-locked.  At 20 fps the closest
# available pair can still be half a frame apart; this is the achievable bound,
# not the old 10 ms value (which was below one frame and spammed the log).
EXTRA_SYNC_TOLERANCE_NS = 25_000_000


def extra_camera_geometry_matches_model(main_size: tuple[int, int], extra_size: tuple[int, int]) -> bool:
  """Whether a second camera's frames can be warped by a model built for the main one.

  The model is constructed once, from the main camera: frame_copy_size and the jit's
  run_model entry are keyed on its size, and the warp indexes every frame with the
  main camera's src_shape and stride_pad. A differently sized extra frame therefore
  does not raise - the copy still fits inside the larger extra buffer, and the
  pointer arithmetic walks out of the extra's Y plane into its UV plane. The model
  gets a misaligned image and nothing logs an error. Only an exact match avoids it,
  which is why the check is equality rather than "fits".
  """
  return tuple(main_size) == tuple(extra_size)


# There is deliberately no steering-angle or speed gate on the wide input. One was
# written and measured, then dropped: switching the second slot between the wide frame
# and a copy of the narrow one alternates that input's projection, and the model was
# trained on one consistent second view. Offline comparison against a true dual-camera
# run put the gate's own premise in doubt as well - the current continuous feeding is
# closer to the dual-camera output than the projection-corrected alternative, so there
# was nothing to switch to. The constants, the gate function, its test and the BYD
# setting that fed it are all removed rather than left unwired: a visible control that
# changes nothing is worse than no control. The wide input is continuous whenever a
# second stream exists; see the frame selection in main().


_NONFINITE_ACTIONS = 0

# Lateral lookahead distance, in metres of the model's own planned path.
# The action head answers "what curvature right now", which for a dual-output model is
# the plan sampled ~0.2 s ahead. At highway speed 0.2 s is already 5 m, but at walking
# pace it is 40 cm, so the head reports "straight" for as long as it takes the car to
# enter a tight corner. Route 000001af at 82 s (2.26 m/s) is the clean case: the plan
# there described a 13 m radius arc needing about -230 deg of wheel, the head said
# -45 deg, and the driver had to take over at 84.3 s.
# Sampling the same plan at a fixed 5 m ahead asks the question 2.5 s early at 2 m/s and
# 0.40 s early at 12 m/s, so highway behaviour is untouched while tight turns get the
# angle they need. Offline over both routes: |delta| against the action head has a
# median of 0.0002 and p95 of 0.0007 above 8 m/s, while the two takeovers reach -210 deg
# and +123 deg against the -230 deg and +102 deg the driver actually used.
LAT_LOOKAHEAD_DISTANCE_M = 5.0
LAT_LOOKAHEAD_MAX_S = 2.5


def lateral_lookahead_time(plan_x: np.ndarray, t_idxs: np.ndarray,
                           target_m: float = LAT_LOOKAHEAD_DISTANCE_M,
                           t_max: float = LAT_LOOKAHEAD_MAX_S) -> float | None:
  """Plan time at which the model expects to be `target_m` ahead of the car.

  Returns None when the plan never reaches that far, which happens at a standstill and
  at walking pace where the 10 s horizon covers a couple of metres. The caller must
  keep the action head in that case: sampling a degenerate horizon would divide by a
  near-zero yaw.
  """
  if plan_x.size == 0 or t_idxs.size != plan_x.size:
    return None
  # A NaN anywhere in the path would poison argmin, so treat non-finite as "never
  # reaches it" instead of letting it pick the NaN slot.
  safe_x = np.where(np.isfinite(plan_x), plan_x, np.inf)
  i = int(np.argmin(np.abs(safe_x - target_m)))
  if not np.isfinite(safe_x[i]) or safe_x[i] < 0.6 * target_m:
    return None
  tau = float(t_idxs[i])
  if not np.isfinite(tau) or tau <= 0.0:
    return None
  return min(tau, t_max)


def get_action_from_model(model_output: dict[str, np.ndarray], prev_action: log.ModelDataV2.Action,
                          lat_action_t: float, long_action_t: float, v_ego: float,
                          blendful: Callable[[float], float] | None = None) -> log.ModelDataV2.Action:
  if 'action' not in model_output:
    plan = model_output['plan'][0]
    desired_accel = get_accel_from_plan(plan[:,Plan.VELOCITY][:,0],
                                        plan[:,Plan.ACCELERATION][:,0],
                                        ModelConstants.T_IDXS,
                                        action_t=long_action_t)
    desired_curvature = get_curvature_from_plan(plan[:,Plan.T_FROM_CURRENT_EULER][:,2],
                                                plan[:,Plan.ORIENTATION_RATE][:,2],
                                                ModelConstants.T_IDXS,
                                                v_ego,
                                                lat_action_t)
  else:
    desired_accel = model_output['action'][0,1]
    # Prefer the plan at a fixed distance ahead over the head's "right now" answer, so a
    # low-speed corner is entered when it is 5 m away instead of when it is 40 cm away.
    # Falls back to the head whenever the plan cannot be sampled (standstill, short
    # horizon, missing plan), which keeps this a no-op in every case the head handles.
    tau = None
    plan = model_output.get('plan')
    if plan is not None and len(plan) and np.ndim(plan) == 3:
      tau = lateral_lookahead_time(np.asarray(plan[0][:,Plan.POSITION][:,0], dtype=np.float64),
                                   np.asarray(ModelConstants.T_IDXS, dtype=np.float64))
      if tau is not None:
        # Never look less far ahead than the head already did: 5 m is shorter than the
        # head's own horizon above roughly 13 m/s, and sampling earlier than the old
        # code would quietly reduce steering exactly where it was working.
        tau = max(tau, float(lat_action_t))
    if tau is None:
      desired_curvature = model_output['action'][0,0] / (max(1.0, v_ego))**2
    else:
      desired_curvature = get_curvature_from_plan(plan[0][:,Plan.T_FROM_CURRENT_EULER][:,2],
                                                  plan[0][:,Plan.ORIENTATION_RATE][:,2],
                                                  ModelConstants.T_IDXS,
                                                  v_ego,
                                                  tau)

  # A single non-finite model output poisons everything below: the smoothed value is
  # fed back through prev_action on the next frame, so one NaN becomes a permanent NaN
  # and every lateral command silently turns into 0 while latActive still reads True.
  # That is exactly what the MIGraphX remote backend did on route 00000199 (1201/1201
  # frames of NaN desiredCurvature, wheel never steered). Substitute the last good
  # value instead, so a bad frame costs one frame instead of the whole drive.
  # prev_action can itself be NaN from a session that ran before this guard existed,
  # which is why both the fallback source and the smoothing reference go through here.
  prev_curv = float(prev_action.desiredCurvature) if np.isfinite(prev_action.desiredCurvature) else 0.0
  prev_acc = float(prev_action.desiredAcceleration) if np.isfinite(prev_action.desiredAcceleration) else 0.0

  global _NONFINITE_ACTIONS
  if not (np.isfinite(desired_curvature) and np.isfinite(desired_accel)):
    _NONFINITE_ACTIONS += 1
    if _NONFINITE_ACTIONS % 100 == 1:
      cloudlog.warning("model action was non-finite, reusing the previous value (count=%d)", _NONFINITE_ACTIONS)
    if not np.isfinite(desired_curvature):
      desired_curvature = prev_curv
    if not np.isfinite(desired_accel):
      desired_accel = prev_acc

  # Blendful nudges the planned curvature toward the lane centre. It runs before the
  # smoothing so the correction gets smoothed like any other curvature source, and it
  # is skipped below MIN_SPEED internally, so passing a callable unconditionally is safe.
  if blendful is not None:
    desired_curvature += blendful(desired_curvature)
    # The correction is applied outside this module, so re-check it rather than assume.
    if not np.isfinite(desired_curvature):
      desired_curvature = prev_curv

  stop = should_stop(v_ego, desired_accel)
  desired_accel = smooth_value(desired_accel, prev_acc, LONG_SMOOTH_SECONDS)
  if v_ego > MIN_LAT_CONTROL_SPEED:
    desired_curvature = smooth_value(desired_curvature, prev_curv, LAT_SMOOTH_SECONDS)
  else:
    desired_curvature = prev_curv

  return log.ModelDataV2.Action(desiredCurvature=float(desired_curvature),
                                desiredAcceleration=float(desired_accel),
                                shouldStop=bool(stop))


class ChestnutState:
  # only modeld can access chestnut
  def __init__(self, pm: PubMaster, big: bool):
    self.pm = pm
    self.big = big
    self.valid = True
    self.sends = 0
    self.metrics = {}
    self._asm_usb = None

  def _close_asm_usb(self) -> None:
    if self._asm_usb is not None:
      self._asm_usb.close()
      self._asm_usb = None

  def _open_asm_usb(self):
    context = usb1.USBContext()
    for vendor_id, product_id in CHESTNUT_USB_IDS:
      if (handle := context.openByVendorIDAndProductID(vendor_id, product_id, skip_on_error=True)) is not None:
        return handle
    context.close()

  def _read_ina(self) -> tuple[int, int, bool]:
    if "AMD" in Device._opened_devices and self._asm_usb is None:
      try:
        raw = Device["AMD"].iface.pci_dev.usb.usb.control_read(0xC0, 5)
        return struct.unpack('<Hh?', bytes(raw))
      except Exception:
        pass
    if self._asm_usb is None:
      self._asm_usb = self._open_asm_usb()
    if self._asm_usb is None:
      raise usb1.USBErrorNoDevice
    try:
      raw = self._asm_usb.controlRead(0xC0, 0xC0, 0, 0, 5, timeout=100)
    except usb1.USBError:
      self._close_asm_usb()
      raise
    return struct.unpack('<Hh?', bytes(raw))

  @cached_property
  def power_limit(self) -> int:
    smu = Device["AMD"].iface.dev_impl.smu
    return smu._send_msg(smu.smu_mod.PPSMC_MSG_GetPptLimit, 0, read_back_arg=True, timeout=100)

  def send(self) -> None:
    msg = messaging.new_message('chestnutState')
    state = msg.chestnutState
    self.sends += 1
    if self.big and "AMD" in Device._opened_devices and self.sends % 100 == 1:
      try:
        smu = Device["AMD"].iface.dev_impl.smu
        metrics_t = smu.smu_mod.SmuMetricsExternal_t
        smu._send_msg(smu.smu_mod.PPSMC_MSG_TransferTableSmu2Dram, smu.smu_mod.TABLE_SMU_METRICS, timeout=100)
        metrics_buf = bytearray(smu.adev.vram.view(smu.driver_table_paddr, ctypes.sizeof(metrics_t))[:])
        metrics = metrics_t.from_buffer(metrics_buf).SmuMetrics
        self.metrics = {'tempC': metrics.AvgTemperature[smu.smu_mod.TEMP_HOTSPOT],
                        'memoryTempC': metrics.AvgTemperature[smu.smu_mod.TEMP_MEM],
                        'powerDrawW': metrics.AverageSocketPower,
                        'powerLimitW': self.power_limit,
                        'gpuUsagePercent': metrics.AverageGfxActivity,
                        'gpuClockMhz': metrics.AverageGfxclkFrequencyPostDs,
                        'fanSpeedRpm': metrics.AvgFanRpm}
        self.valid = True
      except Exception:
        if self.valid:
          cloudlog.exception("chestnut state read failed")
        self.valid = False
        self.metrics.clear()
    if self.big:
      for k, v in self.metrics.items():
        setattr(state, k, v)

    asm_valid = False
    try:
      # ASM runs on USB-C power, these still read without a gpu
      state.supplyVoltage, state.supplyCurrent, state.supplyFault = self._read_ina()
      asm_valid = True
    except Exception:
      pass
    if "AMD" in Device._opened_devices:
      try:
        state.pcieLtssm = Device["AMD"].iface.pci_dev.usb.read(0xB450, 1)[0]
      except Exception:
        pass

    msg.valid = asm_valid and (not self.big or self.valid)
    self.pm.send('chestnutState', msg)


class FrameMeta:
  frame_id: int = 0
  timestamp_sof: int = 0
  timestamp_eof: int = 0

  def __init__(self, vipc=None):
    if vipc is not None:
      self.frame_id, self.timestamp_sof, self.timestamp_eof = vipc.frame_id, vipc.timestamp_sof, vipc.timestamp_eof


class NavInputs:

  ENV = "SUNNYPILOT_NAV_SOURCE"

  def __init__(self, input_shapes):
    self.map_shape = tuple(input_shapes['nav_map'])
    self.vec_shape = tuple(input_shapes['navigation'])
    self.map = np.zeros(self.map_shape, dtype=np.uint8)
    self.vec = np.zeros(self.vec_shape, dtype=np.float32)
    self._prefix: str | None = None
    self._stamps: tuple[float, float] | None = None
    spec = os.getenv(self.ENV, "")
    if spec.startswith("files:"):
      self._prefix = spec[len("files:"):]
      self.refresh()
      cloudlog.warning("nav inputs from files (prefix %s)", self._prefix)
    else:
      cloudlog.warning("nav inputs are zeros: no map/route source on this build")

  def _read(self, path: str, shape, dtype) -> np.ndarray | None:
    try:
      data = np.fromfile(path, dtype=dtype)
    except OSError:
      return None
    return data.reshape(shape) if data.size == math.prod(shape) else None

  def refresh(self) -> None:
    if self._prefix is None:
      return
    try:
      stamps = (os.stat(self._prefix + ".map").st_mtime, os.stat(self._prefix + ".vec").st_mtime)
    except OSError:
      return
    if stamps == self._stamps:
      return
    new_map = self._read(self._prefix + ".map", self.map_shape, np.uint8)
    new_vec = self._read(self._prefix + ".vec", self.vec_shape, np.float32)
    if new_map is not None and new_vec is not None:
      self.map, self.vec = new_map, new_vec
      self._stamps = stamps


class ModelState(NavigationResetMixin, ModelStateBase):
  prev_desire: np.ndarray  # for tracking the rising edge of the pulse

  def __init__(self, cam_w: int, cam_h: int, chestnut: bool):
    ModelStateBase.__init__(self)
    jits, self.pkl_path = load_modeld_jits(chestnut)
    input_devices = jits['input_devices']
    self.model_device = input_devices['model']
    metadata = jits['metadata']
    self.input_shapes = metadata['input_shapes']
    self.output_slices = metadata['output_slices']
    self.modeld_inputs = artifact_inputs(jits)
    self.input_dtypes = metadata.get('input_dtypes')
    self.nav = is_nav_model(self.input_shapes)
    self.nav_src = NavInputs(self.input_shapes) if self.nav else None

    self.prev_desire = np.zeros(ModelConstants.DESIRE_LEN, dtype=np.float32)
    self.chestnut = chestnut

    self.frame_skip = ModelConstants.MODEL_RUN_FREQ // ModelConstants.MODEL_CONTEXT_FREQ
    self.frame_copy_size = nv12_copy_size(*get_nv12_info(cam_w, cam_h)[:3])
    self.input_queues, self.npy, self.frame_views = make_input_queues(
      self.input_shapes, self.frame_skip, device=self.model_device, frame_copy_size=self.frame_copy_size,
      input_dtypes=self.input_dtypes)
    self.vision_input_names = list(self.frame_views)
    self.parser = Parser()
    self.run_model = jits['run_model'][(cam_w,cam_h)]

  def slice_outputs(self, model_outputs: np.ndarray, output_slices: dict[str, slice]) -> dict[str, np.ndarray]:
    parsed_model_outputs = {k: model_outputs[np.newaxis, v] for k,v in output_slices.items()}
    return parsed_model_outputs

  def _apply_pending_nav_reset(self):
    pending = self.pending_nav_resets()
    for channel in pending:
      self.input_queues[NAV_QUEUE_KEYS[channel]].assign(0).realize()
    self._commit_nav_reset(pending)

  def run(self, bufs: dict[str, VisionBuf], transforms: dict[str, np.ndarray],
          inputs: dict[str, np.ndarray], after_enqueue: Callable[[], None] | None = None,
          frame_ids: dict[str, int] | None = None) -> dict[str, np.ndarray]:
    frame_ids = frame_ids or {}
    frame_copies = {}
    for key, buf in bufs.items():
      data = (copy_stable_frame(buf, frame_ids[key], self.frame_copy_size) if key in frame_ids
              else np.array(np.frombuffer(buf.data, dtype=np.uint8, count=self.frame_copy_size), copy=True))
      frame_copies[key] = data
    for key, data in frame_copies.items():
      np.copyto(self.frame_views[key], data)

    # Model decides when action is completed, so desire input is just a pulse triggered on rising edge
    inputs['desire_pulse'][0] = 0
    self.npy['desire'][:] = np.where(inputs['desire_pulse'] - self.prev_desire > .99, inputs['desire_pulse'], 0)
    self.prev_desire[:] = inputs['desire_pulse']
    self.npy['traffic_convention'][:] = inputs['traffic_convention']
    self.npy['action_t'][:] = inputs['action_t']
    self.npy['tfm'][:,:] = transforms['img'][:,:]
    self.npy['big_tfm'][:,:] = transforms['big_img'][:,:]
    if self.nav:
      self.nav_src.refresh()
      self.npy['nav_map'][:] = np.asarray(inputs.get('nav_map', self.nav_src.map), dtype=np.uint8).reshape(self.npy['nav_map'].shape)
      self.npy['nav'][:] = np.asarray(inputs.get('navigation', self.nav_src.vec), dtype=np.float32).reshape(self.npy['nav'].shape)

    self._apply_pending_nav_reset()
    outs = self.run_model(**{k: self.input_queues[k] for k in self.modeld_inputs})
    if after_enqueue is not None:
      after_enqueue()
    apply_state_update(self.input_queues, outs, nav=self.nav)
    model_output = outs[0].numpy()[0]
    if self.chestnut and not np.all(np.isfinite(model_output)):
      raise RuntimeError("model output not finite")
    outputs_dict = self.parser.parse_outputs(self.slice_outputs(model_output, self.output_slices))
    self.npy['prev_feat'][:] = model_output[self.output_slices['hidden_state']]

    if SEND_RAW_PRED:
      outputs_dict['raw_pred'] = model_output.copy()
    return outputs_dict

  def warmup(self) -> None:
    dummy_frames = {k: np.zeros(self.frame_copy_size, dtype=np.uint8) for k in self.vision_input_names}
    eye = np.eye(3, dtype=np.float32)
    dims = {'desire_pulse': ModelConstants.DESIRE_LEN, 'traffic_convention': 2, 'action_t': 2}
    self.run(dummy_frames, dict.fromkeys(self.vision_input_names, eye), {k: np.zeros(v, dtype=np.float32) for k, v in dims.items()})
    self.input_queues, self.npy, self.frame_views = make_input_queues(
      self.input_shapes, self.frame_skip, device=self.model_device, frame_copy_size=self.frame_copy_size,
      input_dtypes=self.input_dtypes)
    self.prev_desire[:] = 0



_MGX_PROGRAM = None


class MigraphxModelState(NavigationResetMixin, ModelStateBase):
  """MIGraphX backend for the big nav model (experimental, env-gated).

  The model graph runs through MIGraphX/ROCm; camera warp is done on the CPU with
  OpenCV. State queues are kept in numpy and updated from the next_state_* outputs.
  Selected with MODELD_BACKEND=migraphx; falls back to the tinygrad ModelState if
  anything fails to load.
  """

  # nav_bigv2 output layout (18580 floats)
  OUTPUT_SLICES = {
    'lane_lines': slice(0, 528), 'lane_lines_prob': slice(528, 536), 'road_edges': slice(536, 800),
    'meta': slice(800, 855), 'desire_pred': slice(855, 887), 'pose': slice(887, 899),
    'wide_from_device_euler': slice(899, 905), 'road_transform': slice(905, 917),
    'plan': slice(917, 1907), 'lead': slice(1907, 2051), 'lead_prob': slice(2051, 2054),
    'desire_state': slice(2054, 2062), 'action': slice(2062, 2066), 'hidden_state': slice(2066, 18450),
    'map_features': slice(18450, 18514), 'navigation_features': slice(18514, 18578), 'pad': slice(18578, 18580),
  }

  def __init__(self, cam_w: int, cam_h: int, chestnut: bool):
    ModelStateBase.__init__(self)
    import cv2
    import onnx
    import migraphx
    self._cv2 = cv2
    self._mgx = migraphx
    self.chestnut = chestnut
    self.model_device = 'migraphx'
    self.nav = True
    self.vision_input_names = ['img', 'big_img']
    self.frame_skip = ModelConstants.MODEL_RUN_FREQ // ModelConstants.MODEL_CONTEXT_FREQ
    self.prev_desire = np.zeros(ModelConstants.DESIRE_LEN, dtype=np.float32)
    self.output_slices = dict(self.OUTPUT_SLICES)
    self.parser = Parser()
    self._invert = os.getenv("MODELD_MIGRAPHX_INVERT", "1") == "1"
    self._uv_mode = os.getenv("MODELD_MIGRAPHX_UV", "col2")
    _sh = os.getenv("MODELD_MIGRAPHX_SHIFT", "0.5,0.0").split(",")
    self._shift = (float(_sh[0]), float(_sh[1]))
    self._interp = None

    path = os.getenv("MODELD_MIGRAPHX_ONNX", os.path.expanduser("~/fwtest/nav_v2_mgx.onnx"))
    self.pkl_path = Path(path)
    m = onnx.load(path, load_external_data=False)
    self.input_shapes = {}
    self._input_dtypes = {}
    for i in m.graph.input:
      dims = tuple(d.dim_value for d in i.type.tensor_type.shape.dim)
      self.input_shapes[i.name] = dims
      self._input_dtypes[i.name] = np.uint8 if i.type.tensor_type.elem_type == 2 else np.float32
    self._output_names = [o.name for o in m.graph.output]
    # Prefer a pre-compiled program: compiling the nav graph takes ~4 minutes,
    # loading the saved .mxr takes ~4 seconds.
    mxr = os.getenv("MODELD_MIGRAPHX_MXR", os.path.expanduser("~/fwtest/nav.mxr"))
    global _MGX_PROGRAM
    if _MGX_PROGRAM is not None:
      self._prog = _MGX_PROGRAM
    elif os.path.exists(mxr):
      last_exc = None
      for attempt in range(10):
        try:
          self._prog = migraphx.load(mxr)
          last_exc = None
          break
        except Exception as e:
          last_exc = e
          cloudlog.warning("MIGraphX load attempt %d failed: %s", attempt + 1, e)
          time.sleep(2.0)
      if last_exc is not None:
        raise last_exc
      _MGX_PROGRAM = self._prog
    else:
      self._prog = migraphx.parse_onnx(path)
      self._prog.compile(migraphx.get_target('gpu'), offload_copy=True)
      with contextlib.suppress(Exception):
        migraphx.save(self._prog, mxr)
    self._state = {k: np.zeros(self.input_shapes[k], dtype=self._input_dtypes[k])
                   for k in self.input_shapes if k.startswith('state_')}
    self._nav_src = NavInputs(self.input_shapes)
    self.nav_src = self._nav_src  # modeld reads inputs from nav runtime, kept for parity

  def slice_outputs(self, model_outputs: np.ndarray, output_slices: dict[str, slice]) -> dict[str, np.ndarray]:
    return {k: model_outputs[np.newaxis, v] for k, v in output_slices.items()}

  def _warp_plane(self, plane: np.ndarray, M: np.ndarray, out_w: int, out_h: int) -> np.ndarray:
    M_use = np.linalg.inv(M) if self._invert else M
    return self._cv2.warpPerspective(plane, M_use, (out_w, out_h), flags=self._cv2.INTER_LINEAR)

  def _warp_camera(self, buf, transform: np.ndarray) -> np.ndarray:
    w, h, stride, uv = buf.width, buf.height, buf.stride, buf.uv_offset
    data = buf.data
    y = np.frombuffer(data, dtype=np.uint8, count=stride * h).reshape(h, stride)[:, :w]
    uv_plane = np.frombuffer(data, dtype=np.uint8, offset=uv, count=stride * (h // 2)).reshape(h // 2, stride)[:, :w]
    u = uv_plane[:, 0::2]
    v = uv_plane[:, 1::2]
    # dst model is 512x256; U/V live at half resolution 256x128
    if self._invert:
      try:
        M_cv = np.linalg.inv(transform)
      except np.linalg.LinAlgError:
        # extrinsicsCalibration has not produced a real warp yet (zero matrix).
        M_cv = np.eye(3, dtype=np.float32)
    else:
      M_cv = transform
    interp = self._interp if self._interp is not None else self._cv2.INTER_LINEAR
    if self._shift != (0.0, 0.0):
      dx, dy = self._shift
      M_cv = M_cv.copy()
      M_cv[:, 2] -= M_cv[:, :2] @ np.array([dx, dy], dtype=np.float32)
    yw = self._cv2.warpPerspective(y, M_cv, (512, 256), flags=interp)
    if self._uv_mode == 'elem':
      E = np.array([[1.0, 1.0, 0.5], [1.0, 1.0, 0.5], [2.0, 2.0, 1.0]], dtype=np.float32)
      M_uv = np.linalg.inv(transform * E) if self._invert else (transform * E)
    elif self._uv_mode == 'same':
      M_uv = M_cv
    else:
      M_uv = M_cv.copy()
      M_uv[:, 2] *= 0.5
    uw = self._cv2.warpPerspective(u, M_uv, (256, 128), flags=interp)
    vw = self._cv2.warpPerspective(v, M_uv, (256, 128), flags=interp)
    yuv = np.concatenate([yw.reshape(-1), uw.reshape(-1), vw.reshape(-1)]).reshape(384, 512).astype(np.uint8)
    H, W = yuv.shape[0] * 2 // 3, yuv.shape[1]  # 256, 512
    ch = [yuv[0:H:2, 0::2], yuv[1:H:2, 0::2], yuv[0:H:2, 1::2], yuv[1:H:2, 1::2],
          yuv[H:H + H // 4].reshape(H // 2, W // 2), yuv[H + H // 4:H + H // 2].reshape(H // 2, W // 2)]
    return np.concatenate(ch, axis=0).reshape(6, H // 2, W // 2)

  def _apply_pending_nav_reset(self):
    pending = self.pending_nav_resets()
    for channel in pending:
      self._state[NAV_STATE_KEYS[channel]].fill(0)
    self._commit_nav_reset(pending)

  def run(self, bufs: dict[str, VisionBuf], transforms: dict[str, np.ndarray],
          inputs: dict[str, np.ndarray], after_enqueue: Callable[[], None] | None = None,
          frame_ids: dict[str, int] | None = None) -> dict[str, np.ndarray]:
    frame_ids = frame_ids or {}
    # MIGraphX 的 CPU warp 直接读取共享帧。只在 warp 前后共检查两次会漏掉
    # “前一次校验通过、拷贝中途被覆盖、拷贝结束后又换回来”的窗口。先复制一份
    # 稳定画面，warp 再使用该副本。
    stable_frames = {key: copy_stable_frame(bufs[key], frame_ids[key], len(bufs[key].data))
                     for key in frame_ids}
    _pf = None
    if os.getenv("MODELD_PROFILE") == "1":
      _pf = getattr(self, "_prof", None) or {k: [] for k in ("warp", "inputs", "mgx", "state")}
      self._prof = _pf
      _t0 = time.perf_counter()
    img = self._warp_camera(stable_frames.get('img', bufs['img']), transforms['img'])
    big = self._warp_camera(stable_frames.get('big_img', bufs['big_img']), transforms['big_img'])
    _t1 = time.perf_counter()
    self._apply_pending_nav_reset()
    feed = dict(self._state)
    feed['new_img'] = np.ascontiguousarray(np.stack([img, big]).astype(np.uint8))
    inputs['desire_pulse'][0] = 0
    feed['desire'] = np.where(inputs['desire_pulse'] - self.prev_desire > .99, inputs['desire_pulse'], 0).astype(np.float32)
    self.prev_desire[:] = inputs['desire_pulse']
    feed['traffic_convention'] = np.asarray(inputs['traffic_convention'], dtype=np.float32).reshape(1, 2)
    feed['action_t'] = np.asarray(inputs['action_t'], dtype=np.float32).reshape(1, 2)
    self._nav_src.refresh()
    feed['nav_map'] = np.asarray(inputs.get('nav_map', self._nav_src.map), dtype=np.float32).reshape(1, 1, 256, 256)
    feed['navigation'] = np.asarray(inputs.get('navigation', self._nav_src.vec), dtype=np.float32).reshape(1, 47)

    _t2 = time.perf_counter()
    out = self._prog.run({k: self._mgx.argument(v) for k, v in feed.items()})
    _t3 = time.perf_counter()
    if after_enqueue is not None:
      after_enqueue()
    for name in self._output_names:
      if name.startswith('next_state_'):
        key = 'state_' + name[len('next_state_'):]
        if key in self._state:
          self._state[key] = np.asarray(out[self._output_names.index(name)], dtype=self._state[key].dtype)
    model_output = np.asarray(out[0], dtype=np.float32).reshape(-1)
    outputs_dict = self.parser.parse_outputs(self.slice_outputs(model_output, self.output_slices))
    if _pf is not None:
      _t4 = time.perf_counter()
      _pf["warp"].append(_t1 - _t0)
      _pf["inputs"].append(_t2 - _t1)
      _pf["mgx"].append(_t3 - _t2)
      _pf["state"].append(_t4 - _t3)
      if len(_pf["mgx"]) >= 100:
        import json as _json
        _o = {}
        for _k, _v in _pf.items():
          _a = np.array(_v) * 1000.0
          _o[_k] = {"p50": round(float(np.percentile(_a, 50)), 2), "p95": round(float(np.percentile(_a, 95)), 2),
                    "mean": round(float(_a.mean()), 2), "n": len(_v)}
        with open("/tmp/modeld_mgx_profile.json", "w") as _f:
          _json.dump(_o, _f)
        self._prof = {k: [] for k in _pf}
    return outputs_dict

  def warmup(self) -> None:
    dummy = {k: types.SimpleNamespace(data=bytes(1408 * 760 + 1408 * 380), width=1344, height=760,
                                      stride=1408, uv_offset=1408 * 760) for k in self.vision_input_names}
    eye = np.eye(3, dtype=np.float32)
    dims = {'desire_pulse': ModelConstants.DESIRE_LEN, 'traffic_convention': 2, 'action_t': 2}
    self.run(dummy, dict.fromkeys(self.vision_input_names, eye),
             {k: np.zeros(v, dtype=np.float32) for k, v in dims.items()})
    for key in NAV_STATE_KEYS.values():
      self._state[key].fill(0)


class MigraphxRemoteState(MigraphxModelState):
  """MIGraphX backend that talks to a separate mgxd process over a Unix socket.

  Keeps HIP out of modeld's address space (tinygrad + HIP in one process hangs
  the GPU). Camera warp still happens here; the recurrent state lives in mgxd.
  """

  nav_reset_supported = False
  nav_session_reset_confirmed = False

  def __init__(self, cam_w: int, cam_h: int, chestnut: bool):
    import cv2
    ModelStateBase.__init__(self)
    self._cv2 = cv2
    self.chestnut = chestnut
    self.model_device = 'mgxd'
    self.nav = True
    self.vision_input_names = ['img', 'big_img']
    self.frame_skip = ModelConstants.MODEL_RUN_FREQ // ModelConstants.MODEL_CONTEXT_FREQ
    self.prev_desire = np.zeros(ModelConstants.DESIRE_LEN, dtype=np.float32)
    self.output_slices = dict(self.OUTPUT_SLICES)
    self.parser = Parser()
    self._invert = os.getenv("MODELD_MIGRAPHX_INVERT", "1") == "1"
    self._uv_mode = os.getenv("MODELD_MIGRAPHX_UV", "col2")
    _sh = os.getenv("MODELD_MIGRAPHX_SHIFT", "0.5,0.0").split(",")
    self._shift = (float(_sh[0]), float(_sh[1]))
    self._interp = None
    self.pkl_path = Path(os.getenv("MODELD_MIGRAPHX_ONNX", os.path.expanduser("~/fwtest/nav_v2_mgx.onnx")))
    self.input_shapes = {
      'new_img': (2, 6, 128, 256), 'desire': (8,), 'traffic_convention': (1, 2), 'action_t': (1, 2),
      'nav_map': (1, 1, 256, 256), 'navigation': (1, 47),
      'state_img_q': (2, 5, 6, 128, 256), 'state_desire_q': (132, 1, 8), 'state_feat_q': (128, 1, 16384),
      'state_map_features_q': (128, 1, 64), 'state_navigation_features_q': (128, 1, 64),
    }
    self._nav_src = NavInputs(self.input_shapes)
    self.nav_src = self._nav_src
    self._sock = None
    self._ensure_server()

  def _sock_path(self) -> str:
    return os.getenv("MODELD_MGXD_SOCK", "/tmp/mgxd.sock")

  def _try_connect(self):
    import socket
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
      s.connect(self._sock_path())
      s.settimeout(5.0)
      return s
    except OSError:
      s.close()
      return None

  def _ensure_server(self) -> None:
    self._hello_sent = False
    self.nav_reset_supported = False
    self.nav_session_reset_confirmed = False
    self._last_map = None
    self._last_nav = None
    self._sock = self._try_connect()
    if self._sock is not None:
      return
    from openpilot.selfdrive.modeld.mgxd_runtime import daemon_environment
    env = daemon_environment(os.environ)
    env.setdefault("MODELD_MIGRAPHX_MXR", os.path.expanduser("~/fwtest/nav.mxr"))
    state_dir = Path(os.getenv("XDG_STATE_HOME") or Path.home() / ".local/state")
    log_path = Path(os.getenv("MODELD_MGXD_LOG") or state_dir / "sunnypilot/mgxd.log").expanduser()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "ab", buffering=0) as log:
      log.write(f"\n{time.strftime('%Y-%m-%d %H:%M:%S %z')} starting mgxd\n".encode())
      subprocess.Popen([sys.executable, "-u", "-m", "openpilot.selfdrive.modeld.mgxd"],
                       cwd=str(Path(__file__).resolve().parents[4]), env=env,
                       stdout=log, stderr=log)
    deadline = time.monotonic() + 40.0
    while time.monotonic() < deadline:
      self._sock = self._try_connect()
      if self._sock is not None:
        return self._sock
      time.sleep(0.2)
    raise RuntimeError("mgxd did not come up")

  def run(self, bufs, transforms, inputs, after_enqueue=None, frame_ids=None):
    tf_img = np.asarray(transforms['img'], np.float32)
    tf_big = np.asarray(transforms['big_img'], np.float32)
    inputs['desire_pulse'][0] = 0
    desire = np.where(inputs['desire_pulse'] - self.prev_desire > .99, inputs['desire_pulse'], 0).astype(np.float32)
    self.prev_desire[:] = inputs['desire_pulse']
    tc = np.asarray(inputs['traffic_convention'], np.float32).reshape(2)
    action = np.asarray(inputs['action_t'], np.float32).reshape(2)
    self._nav_src.refresh()
    nav_map = np.asarray(inputs.get('nav_map', self._nav_src.map), np.float32).reshape(1, 1, 256, 256)
    nav = np.asarray(inputs.get('navigation', self._nav_src.vec), np.float32).reshape(47)

    map_b = nav_map.tobytes()
    nav_b = nav.tobytes()
    self._nav_client_sequence = getattr(self, '_nav_client_sequence', 0) + 1
    fallback = 'files' if getattr(self._nav_src, '_prefix', None) and getattr(self._nav_src, '_stamps', None) else 'zeros'
    self.last_nav_evidence = {
      'clientSequence': self._nav_client_sequence, 'requestMonoTime': time.monotonic_ns(),
      'inputNavCrc32': crc32_buffer(nav_b), 'inputMapCrc32': crc32_buffer(map_b),
      'navigation': nav.tolist(), 'desire': desire.tolist(),
      'mapInputSource': 'runtime' if 'nav_map' in inputs else fallback,
      'instructionInputSource': 'runtime' if 'navigation' in inputs else fallback,
      'receiptAvailable': False, 'receiptVerified': False, 'receiptReason': 'pending',
    }
    fixed = struct.pack("<30f", *tf_img.reshape(-1), *tf_big.reshape(-1), *desire, *tc, *action)
    pending_reset = self.pending_nav_resets()
    reset_flags = (REQUEST_RESET_MAP if 'map' in pending_reset else 0) | (REQUEST_RESET_NAVIGATION if 'navigation' in pending_reset else 0)
    # Retry the entire exchange on a new connection, keeping the same desire pulse.
    # Reading a reply before resending the hello/frame deadlocks both peers.
    for attempt in range(2):
      self.last_nav_evidence['retryCount'] = attempt
      try:
        if attempt:
          self._ensure_server()
        out = self._exchange_frame(bufs, fixed, map_b, nav_b, reset_flags=reset_flags)
        break
      except (OSError, RuntimeError):
        session_ack_started = (getattr(self, '_nav_response_magic', None) == RESET_RECEIPT_MAGIC
                               or self.nav_reset_supported)
        if self._sock is not None:
          self._sock.close()
          self._sock = None
        self.nav_reset_supported = False
        self.nav_session_reset_confirmed = False
        if attempt or pending_reset or session_ack_started:
          self.last_nav_evidence.update(receiptReason='rpcFailure', responseMonoTime=time.monotonic_ns())
          self.last_nav_evidence['rpcNs'] = self.last_nav_evidence['responseMonoTime'] - self.last_nav_evidence['requestMonoTime']
          raise
        cloudlog.warning("mgxd frame exchange failed; reconnecting once")
    self.last_nav_evidence.update(responseMonoTime=time.monotonic_ns(), retryCount=attempt)
    self.last_nav_evidence['rpcNs'] = self.last_nav_evidence['responseMonoTime'] - self.last_nav_evidence['requestMonoTime']
    session_requested = self._nav_session_requested
    session_response = self.nav_reset_supported or self._nav_response_magic == RESET_RECEIPT_MAGIC
    if pending_reset or (session_requested and session_response):
      self._acknowledge_nav_reset(pending_reset, RESET_FLAGS if session_requested else reset_flags)
      if session_requested:
        self.nav_session_reset_confirmed = True
    self.last_nav_evidence = finish_remote_evidence(self.last_nav_evidence, self._nav_receipt, out,
                                                   getattr(self, '_nav_previous_worker', None), self._nav_receipt_invalid)
    if self.last_nav_evidence['receiptVerified']:
      self._nav_previous_worker = (self._nav_receipt['worker_session'], self._nav_receipt['step_seq'])
    if after_enqueue is not None:
      after_enqueue()
    outputs_dict = self.parser.parse_outputs(self.slice_outputs(out, self.output_slices))
    return outputs_dict

  def _acknowledge_nav_reset(self, pending, flags):
    ack = self._nav_reset_ack
    previous = getattr(self, '_nav_previous_worker', None)
    prior_reset = getattr(self, '_nav_reset_previous_ack', None)
    stale = any(old is not None and ack is not None and old[0] == ack['worker_session'] and old[1] >= ack['step_seq']
                for old in (previous, prior_reset))
    receipt = self._nav_receipt
    mismatch = (ack is not None and receipt is not None
                and (ack['worker_session'], ack['step_seq']) != (receipt['worker_session'], receipt['step_seq']))
    if ack is None or ack['flags'] != flags or stale or mismatch or receipt is None or not self.nav_reset_supported:
      self.last_nav_evidence['receiptReason'] = 'navigationResetUnconfirmed'
      self.nav_session_reset_confirmed = False
      if self._sock is not None:
        self._sock.close()
        self._sock = None
      raise RuntimeError('mgxd navigation reset was not acknowledged; current frame will not be retried')
    self._nav_reset_previous_ack = (ack['worker_session'], ack['step_seq'])
    self._commit_nav_reset(pending)

  def _exchange_frame(self, bufs, fixed: bytes, map_b: bytes, nav_b: bytes, *, reset_flags=0) -> np.ndarray:
    self._nav_receipt = None
    self._nav_receipt_invalid = False
    self._nav_reset_ack = None
    self._nav_response_magic = None
    self.nav_reset_supported = False
    self._nav_session_requested = not self._hello_sent
    if self._nav_session_requested:
      self.nav_session_reset_confirmed = False
      b = bufs['img']
      self._sock.sendall(b"MGX3" + struct.pack("<4ii", b.width, b.height, b.stride, b.uv_offset, len(b.data)))
      self._hello_sent = True
    flags = REQUEST_RECEIPT | reset_flags | (REQUEST_NAV_SESSION if self._nav_session_requested else 0)
    if map_b != getattr(self, "_last_map", None):
      flags |= 1
      self._last_map = map_b
    if nav_b != getattr(self, "_last_nav", None):
      flags |= 2
      self._last_nav = nav_b
    anc = [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
            array("i", [bufs["img"].fd, bufs["big_img"].fd]).tobytes())]
    self._sock.sendmsg([bytes([flags]) + fixed], anc)
    if flags & 1:
      self._sock.sendall(map_b)
    if flags & 2:
      self._sock.sendall(nav_b)
    magic = self._recv_exact(4)
    self._nav_response_magic = magic
    if magic == RESET_RECEIPT_MAGIC:
      payload = self._recv_exact(RESET_ACK_SIZE)
      try:
        self._nav_reset_ack = decode_reset_ack(payload)
      except ValueError:
        pass
    if magic in (RECEIPT_MAGIC, RESET_RECEIPT_MAGIC):
      payload = self._recv_exact(RECEIPT_SIZE)
      try:
        self._nav_receipt = decode_receipt(payload)
        self.nav_reset_supported = bool(self._nav_receipt['navigation_reset_supported'])
      except ValueError:
        # The fixed-size extension is consumed. Bad telemetry must not re-run the
        # model or advance its recurrent state twice for the same camera frame.
        self._nav_receipt_invalid = True
    elif magic != LEGACY_MAGIC:
      raise RuntimeError("mgxd protocol error")
    if not self.nav_reset_supported:
      self.nav_session_reset_confirmed = False
    return np.frombuffer(self._recv_exact(18580 * 4), np.float32).copy()

  def _recv_exact(self, n: int) -> bytes:
    buf = bytearray(n)
    view = memoryview(buf)
    got = 0
    while got < n:
      k = self._sock.recv_into(view[got:], n - got)
      if k == 0:
        raise RuntimeError("mgxd closed")
      got += k
    return bytes(buf)

  def warmup(self) -> None:
    # mgxd is already compiled and holds the recurent state; running a dummy
    # frame here would seed the state with black images.
    return None


def _backend_name() -> str:
  return os.getenv("MODELD_BACKEND", "tinygrad").lower()


def _using_migraphx() -> bool:
  return _backend_name() in ("migraphx", "mxr", "migraphx-remote", "mgxd")


def create_model_state(cam_w: int, cam_h: int, chestnut: bool) -> ModelStateBase:
  backend = _backend_name()
  cloudlog.warning("create_model_state: requested backend=%s", backend)
  if backend in ("migraphx-remote", "mgxd"):
    # A failed HIP daemon may have left the shared GPU unhealthy. Do not start
    # another GPU backend implicitly; let the manager apply its restart limit.
    m = MigraphxRemoteState(cam_w, cam_h, chestnut)
    cloudlog.warning("MIGraphX remote backend active: %s", m.pkl_path)
    return m
  elif backend in ("migraphx", "mxr"):
    # No tinygrad fallback here: once HIP is up, creating a tinygrad KFD device
    # in the same process hangs the GPU (ACQUIRE_VM EBUSY). Let modeld exit and
    # let the manager restart it with a clean address space instead.
    m = MigraphxModelState(cam_w, cam_h, chestnut)
    cloudlog.warning("MIGraphX backend active: %s", m.pkl_path)
    return m
  return ModelState(cam_w, cam_h, chestnut)

def fill_nav_status(model_data_sp, status: dict | None) -> None:
  """Write the nav-input health into modelDataV2SP (false/zero when the model has no nav)."""
  model_data_sp.navMapValid = bool(status["map_valid"]) if status is not None else False
  model_data_sp.navInstructionValid = bool(status["instruction_valid"]) if status is not None else False
  model_data_sp.navMapAge = float(status["map_age"]) if status is not None else 0.0
  model_data_sp.navInstructionAge = float(status["instruction_age"]) if status is not None else 0.0


def fill_nav_evidence(model_data_sp, runtime, model, frame_id: int, frame_id_extra: int, timestamp_eof: int) -> None:
  evidence = {'version': 1, 'frameId': frame_id, 'frameIdExtra': frame_id_extra, 'timestampEof': timestamp_eof,
              'receiptReason': 'backendUnsupported'}
  if runtime is not None:
    evidence.update(runtime.evidence())
  evidence.update(getattr(model, 'last_nav_evidence', {}))
  applied = getattr(model, 'nav_reset_applied', {})
  pending = model.pending_nav_resets() if callable(getattr(model, 'pending_nav_resets', None)) else {}
  requested = applied | pending
  evidence.update(navigationResetSupported=getattr(model, 'nav_reset_supported', False) is True,
                  mapResetRequestedEpoch=requested.get('map', 0), instructionResetRequestedEpoch=requested.get('navigation', 0),
                  mapResetAppliedEpoch=applied.get('map', 0), instructionResetAppliedEpoch=applied.get('navigation', 0),
                  navigationSessionResetConfirmed=getattr(model, 'nav_session_reset_confirmed', False) is True)
  model_data_sp.navEvidence = evidence


def log_nav_model_failure(runtime, model, frame_id: int, frame_id_extra: int, timestamp_eof: int, error: Exception) -> None:
  try:
    cloudlog.event('navigationModelFailure', error=f'{type(error).__name__}: {error}', frameId=frame_id,
                   frameIdExtra=frame_id_extra, timestampEof=timestamp_eof,
                   runtime=runtime.evidence() if runtime is not None else {},
                   remote=getattr(model, 'last_nav_evidence', {}))
  except Exception:
    # Preserve the original inference exception if the logging path is unavailable.
    pass


def _preload_migraphx() -> None:
  """Load the MIGraphX program before any VisionIPC/dmabuf client exists.

  Once a VisionIPC client has mapped camera buffers, HIP refuses MIGraphX's
  parameter upload ('write_to_gpu: invalid argument'), so the program has to be
  resident first.
  """
  global _MGX_PROGRAM
  if os.getenv("MODELD_BACKEND", "").lower() not in ("migraphx", "mxr") or _MGX_PROGRAM is not None:
    return
  import migraphx
  # Give Xorg/GLFW (ui) time to finish initialising: creating the HIP context while the
  # desktop brings up its GL context is what has been triggering MES queue errors.
  time.sleep(float(os.getenv("MODELD_MGX_START_DELAY", "8")))
  mxr = os.getenv("MODELD_MIGRAPHX_MXR", os.path.expanduser("~/fwtest/nav.mxr"))
  onnx_path = os.getenv("MODELD_MIGRAPHX_ONNX", os.path.expanduser("~/fwtest/nav_v2_mgx.onnx"))
  for attempt in range(4):
    try:
      if os.path.exists(mxr):
        _MGX_PROGRAM = migraphx.load(mxr)
      else:
        p = migraphx.parse_onnx(onnx_path)
        p.compile(migraphx.get_target('gpu'), offload_copy=True)
        _MGX_PROGRAM = p
      cloudlog.warning("MIGraphX preloaded: %s", mxr)
      return
    except Exception as e:
      cloudlog.warning("MIGraphX preload attempt %d failed: %s", attempt + 1, e)
      time.sleep(2.0)
  cloudlog.exception("MIGraphX preload failed, backend will fall back")


def main(demo=False):
  cloudlog.warning("modeld init")
  _preload_migraphx()

  # Bench tuning: optional CPU affinity and priority. Affinity works unprivileged;
  # negative nice needs CAP_SYS_NICE, so a failure is logged and ignored.
  if (cpu := os.getenv("MODELD_CPU")):
    try:
      os.sched_setaffinity(0, {int(cpu)})
      cloudlog.warning(f"modeld affinity: cpu {cpu}")
    except Exception as e:
      cloudlog.warning(f"modeld affinity failed: {e}")
  if (nice := os.getenv("MODELD_NICE")):
    try:
      os.nice(int(nice))
      cloudlog.warning(f"modeld nice: {nice}")
    except Exception as e:
      cloudlog.warning(f"modeld nice failed: {e}")

  chestnut_available = chestnut_present() and chestnut_compiled()
  CHESTNUT = False
  if chestnut_available:
    poller = messaging.Poller()
    sock = messaging.sub_sock("chestnutState", poller=poller, conflate=True)
    deadline = time.monotonic() + 4. / SERVICE_LIST['deviceState'].frequency
    while not CHESTNUT and (remaining := deadline - time.monotonic()) > 0.:
      if not poller.poll(round(remaining * 1000)):
        break
      msg = messaging.recv_one_or_none(sock)
      CHESTNUT = msg is not None and msg.valid and chestnut_ready(msg.chestnutState)
  if CHESTNUT:
    os.environ['HCQDEV_WAIT_TIMEOUT_MS'] = '3000'
  params = Params()
  params.put_bool("ChestnutLoading", CHESTNUT)
  if chestnut_available and not CHESTNUT:
    params.put_bool("ChestnutActive", False)
  else:
    params.remove("ChestnutActive")

  config_realtime_process(7, 54)

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
  # Conflation drops all but the newest wide-frame notification. With a 30 fps
  # wide sensor and a 20 fps main camera, that can discard the closest arrived
  # pair before the selector gets a chance to inspect it.
  vipc_client_extra = VisionIpcClient("camerad", VisionStreamType.VISION_STREAM_WIDE_ROAD, False)
  cloudlog.warning(f"vision stream set up, main_wide_camera: {main_wide_camera}, use_extra_client: {use_extra_client}")

  while not vipc_client_main.connect(False):
    time.sleep(0.1)
  while use_extra_client and not vipc_client_extra.connect(False):
    time.sleep(0.1)

  cloudlog.warning(f"connected main cam with buffer size: {vipc_client_main.buffer_len} ({vipc_client_main.width} x {vipc_client_main.height})")
  if use_extra_client:
    cloudlog.warning(f"connected extra cam with buffer size: {vipc_client_extra.buffer_len} ({vipc_client_extra.width} x {vipc_client_extra.height})")
    # Both feeds share one ModelState, so they must share one geometry.
    # frame_copy_size and the jit's run_model key are both derived from the main
    # camera alone, and the warp indexes the extra frame with the main camera's
    # src_shape and stride_pad. A differently sized extra frame does not raise: the
    # copy still fits inside the extra buffer, and the warp reads past the extra's Y
    # plane into its UV plane, so the model silently gets a misaligned second image.
    # Refusing the input is the only honest option - a corrupted frame is worse than
    # the single-camera path, which the model supports and camerad also falls back to.
    if not extra_camera_geometry_matches_model((vipc_client_main.width, vipc_client_main.height),
                                               (vipc_client_extra.width, vipc_client_extra.height)):
      cloudlog.error("extra cam %dx%d does not match the model's %dx%d; dropping the extra input, make the wide camera match the narrow one",
                     vipc_client_extra.width, vipc_client_extra.height,
                     vipc_client_main.width, vipc_client_main.height)
      use_extra_client = False

  st = time.monotonic()
  cloudlog.warning("loading model")
  model = None
  if CHESTNUT:
    big_model = None
    def load_big():
      nonlocal big_model
      try:
        m = ModelState(vipc_client_main.width, vipc_client_main.height, True)
        m.warmup()
        big_model = m
      except Exception:
        cloudlog.exception("big model load failed")
    loader = threading.Thread(target=load_big, daemon=True)
    loader.start()
    loader.join(BIG_MODEL_TIMEOUT)
    model = big_model
    if model is None:
      params.put_bool("ChestnutModelError", True)
    params.put_bool("ChestnutActive", model is not None)
    if model is not None:
      params.remove("ChestnutModelError")

  small_model = create_model_state(vipc_client_main.width, vipc_client_main.height, False) if model is None or CHESTNUT else None
  if model is None:
    model = small_model
  params.put_bool("ChestnutLoading", False)
  assert model is not None
  # model.pkl_path is what was actually loaded; it differs from the requested
  # path when a Settings pick had to be abandoned.
  cloudlog.info("model backend: %s, artifact: %s, big: %s", model.model_device,
                model.pkl_path, is_big_model(model.input_shapes))
  cloudlog.warning(f"models loaded in {time.monotonic() - st:.1f}s, modeld starting")

  # messaging
  pub_socks = ["modelV2", "drivingModelData", "cameraOdometry", "modelDataV2SP"] + (["chestnutState"] if CHESTNUT else [])
  pm = PubMaster(pub_socks)
  sm = SubMaster(["deviceState", "carState", "narrowRoadCameraState", "extrinsicsCalibration", "driverMonitoringState",
                  "carControl", "lateralDelay", "carStateSP", "navInstruction", "navRoute", "navMapFrameSP"])

  publish_state = PublishState()
  params = Params()
  try:
    lane_line_check = int(params.get("LaneLineCheck") or 0)
  except (TypeError, ValueError):
    lane_line_check = 0
  chestnut_state = ChestnutState(pm, model.chestnut) if CHESTNUT else None
  nav_runtime = NavRuntime()
  nav_status = None

  # setup filter to track dropped frames
  frame_dropped_filter = FirstOrderFilter(0., 10., 1. / ModelConstants.MODEL_RUN_FREQ)
  frame_id = 0
  last_vipc_frame_id = 0
  run_count = 0
  # Settings-driven model swap. modeld used to load its artifact only at startup,
  # so a pick in Settings was written to Params and then never loaded - the
  # selection looked ignored until the whole stack was restarted. The resolved
  # path is checked at 1 Hz while offroad and the model is reloaded in place.
  # A pick that cannot be loaded is remembered, so a broken artifact cannot make
  # modeld retry (and stall) every second.
  model_pick_check_ts = 0.0
  failed_model_pick: str | None = None

  model_transform_main = np.zeros((3, 3), dtype=np.float32)
  model_transform_extra = np.zeros((3, 3), dtype=np.float32)
  model_transform_extra_fallback = np.zeros((3, 3), dtype=np.float32)
  camera_offset_helper = CameraOffsetHelper()
  extrinsics_calibration_seen = False
  buf_main, buf_extra = None, None
  meta_main = FrameMeta()
  meta_extra = FrameMeta()
  extra_frame_selector = ExtraFrameSelector(EXTRA_SYNC_TOLERANCE_NS)

  if demo:
    CP = get_demo_car_params()
  else:
    CP = messaging.log_from_bytes(params.get("CarParams", block=True), car.CarParams)
  cloudlog.info("modeld got CarParams: %s", CP.brand)

  # TODO this needs more thought, use .2s extra for now to estimate other delays
  # TODO Move smooth seconds to action function
  long_delay = CP.longitudinalActuatorDelay + LONG_SMOOTH_SECONDS
  prev_action = log.ModelDataV2.Action()

  DH = DesireHelper()
  RELC = RoadEdgeLaneChangeController()

  # Blendful: nudges the planned curvature toward the lane centre, which is what keeps
  # the car from cutting corners and hugging the inside line on gentle bends. Off by
  # default. This used to be the SUNNYPILOT_BLENDFUL environment variable because a
  # Params key needed libparams_c rebuilt and scons could not run here; scons works
  # again, so it is a normal key and the settings page can toggle it.
  blendful = Blendful()
  blendful_enabled = params.get_bool("Blendful")
  if blendful_enabled:
    cloudlog.warning("blendful enabled")

  # The wide camera on this desktop build is a fisheye, and it feeds the model as a
  # second input rather than as an extra picture. It is continuous: gating it was
  # tried, measured and dropped (see the note where the gate used to live, above).
  # When no synchronised wide frame arrives the main frame fills the second slot, so
  # the graph always sees two cameras.
  # The onroad UI shows a plain-text indicator while the wide camera is the second
  # model input. Publish on change only: this is a param write, and doing it per frame
  # would put file I/O in the model loop for no reason.
  wide_cam_reported = None
  wide_cam_misses = 0
  extra_is_wide = False
  extra_wide_prev = None
  # Frames the wide camera has actually contributed to the model's second slot.
  # `use_extra_client` only says the stream is configured; a stream that delivers
  # frames outside the sync window keeps that True while the second slot is filled
  # with a copy of the narrow frame, and the HUD would claim the model is looking
  # through the wide camera when it is not. Two counters give the on/off decision
  # 0.5 s of hysteresis at 20 Hz without reacting to a single late pair.
  wide_on_frames = 0
  wide_off_frames = 0
  wide_active = False

  _prof = ({k: [] for k in ("recv", "prep", "run", "post", "loop")}
           if os.getenv("MODELD_PROFILE") == "1" else None)

  while True:
    _t_loop0 = time.perf_counter()
    # The main camera is the clock.  Never block on the optional extra stream:
    # doing that lets main VIPC fill up and is what produced frameDropPerc > 1%.
    buf_main = vipc_client_main.recv()
    _t_recv = time.perf_counter()
    meta_main = FrameMeta(vipc_client_main)
    if buf_main is None:
      cloudlog.debug("vipc_client_main no frame")
      continue

    extra_is_wide = False
    if use_extra_client:
      # Select from frames that have actually arrived. Keeping only the newest
      # candidate discards an older frame that may be closer to this main SOF.
      # The selector never waits and never reuses a frame.
      selected_extra = extra_frame_selector.select(vipc_client_extra, meta_main.timestamp_sof)
      if selected_extra is not None:
        buf_extra = selected_extra.buffer
        meta_extra = selected_extra.meta
        extra_is_wide = True
        wide_cam_misses = 0
      else:
        # No usable partner: feed the narrow frame on the extra input for this
        # cycle rather than stalling the main camera.
        buf_extra = buf_main
        meta_extra = meta_main
        # An out-of-window frame does not mean the stream stopped. Count only
        # consecutive receive failures towards abandoning the wide stream.
        wide_cam_misses = extra_frame_selector.consecutive_misses
        if wide_cam_misses > MAX_EXTRA_MISSES:
          cloudlog.error("wide camera stopped delivering; falling back to single camera")
          use_extra_client = False
          wide_cam_misses = 0
    else:
      buf_extra = buf_main
      meta_extra = meta_main

    # The indicator follows the frames the model actually received, not the stream
    # configuration: a configured wide camera whose pairs all miss the sync window
    # contributes nothing, and saying otherwise would be a lie on the HUD. Publish
    # on change only - this is a Params write, not something to do every frame.
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

    if not CHESTNUT and not _using_migraphx() and not sm["deviceState"].started:
      now = time.monotonic()
      if now - model_pick_check_ts > 1.0:
        model_pick_check_ts = now
        target = model_swap_target(model.pkl_path, CHESTNUT)
        if target is not None and str(target) != failed_model_pick:
          try:
            # Big <-> small is a normal swap now: the Big Model slot is the
            # desktop big/small switch, so the target can be the big artifact.
            # Loading it takes a few seconds; this only runs offroad.
            cloudlog.warning("model swap: loading %s", target)
            swapped = ModelState(vipc_client_main.width, vipc_client_main.height, CHESTNUT)
            if swapped.pkl_path != target:
              raise RuntimeError(f"artifact fell back to {swapped.pkl_path.name}")
            swapped.warmup()
          except Exception:
            failed_model_pick = str(target)
            cloudlog.exception("model swap to %s failed, keeping %s", target, model.pkl_path)
          else:
            cloudlog.event("modeld swapped model", path=str(target), previous=str(model.pkl_path))
            model = swapped
            failed_model_pick = None

    desire = DH.desire
    is_rhd = sm["driverMonitoringState"].isRHD
    frame_id = sm["narrowRoadCameraState"].frameId
    v_ego = max(sm["carState"].vEgo, 0.)
    model.lat_delay, model.lat_delay_source = select_lat_delay(params, sm["lateralDelay"].lateralDelay)
    if sm.frame % 60 == 0:
      # Persistent params, read on a slow cadence on purpose: one read per model frame
      # would put file I/O in the model loop for nothing. Three seconds between reads is
      # fast enough for anything the settings page can change. The offset helper smooths
      # its own step, so a jump here does not reach the plan.
      camera_offset_helper.set_offset(params.get("CameraOffset", return_default=True))
      blendful_enabled = params.get_bool("Blendful")
    if ((sm.updated["extrinsicsCalibration"] or extra_wide_prev is None or extra_is_wide != extra_wide_prev) and
        sm.seen['narrowRoadCameraState'] and sm.seen['deviceState']):
      device_from_calib_euler = np.array(sm["extrinsicsCalibration"].rpyCalib, dtype=np.float32)
      if device_from_calib_euler.shape != (3,):
        # calibrationd publishes an extrinsicsCalibration message before it has solved
        # anything, with rpyCalib empty. Reacting to that message shape crashed startup
        # outright: rot_from_euler unpacks three values and raises
        # "ValueError: not enough values to unpack (expected 3, got 0)".
        # Fall back to a neutral (uncalibrated) warp, which is exactly what the stock
        # agent uses before calibration converges, instead of skipping the update -
        # model_transform_main/extra start as zero matrices, and a zero warp would feed
        # the model garbage rather than a slightly mis-pitched camera image.
        device_from_calib_euler = np.zeros(3, dtype=np.float32)
      dc = DEVICE_CAMERAS[(str(sm['deviceState'].deviceType), str(sm['narrowRoadCameraState'].sensor))]
      main_intrinsics = dc.wide_road.intrinsics if main_wide_camera else dc.narrow_road.intrinsics
      model_transform_main = get_warp_matrix(device_from_calib_euler, main_intrinsics, False).astype(np.float32)
      # Must follow the same flag that chose the buffer: feeding the narrow frame on
      # the extra input while warping it with wide intrinsics would project it wrong.
      has_wide_camera = extra_is_wide or main_wide_camera
      extra_intrinsics = dc.wide_road.intrinsics if has_wide_camera else dc.narrow_road.intrinsics
      # The sbigmodel destination frame stays on even when the narrow frame is what
      # actually lands in this slot. That looks inconsistent - it magnifies the narrow
      # camera 4.09x and only 46.7% of the result is real pixels - so it was measured
      # against the alternative of dropping to the medmodel frame (2.05x, 91.4% real):
      # over 400 frames of segment 0000006c the current form was closer to a true
      # dual-camera run on every control-relevant measure - lane line |dy| 0.058 m vs
      # 0.077 m, plan lateral 0.243 m vs 0.416 m, and no near-field bias (+0.02 m vs
      # +0.075 m inside 10 m). The medmodel alternative also reported lane confidence
      # *higher* than the dual-camera reference while planning further away from it.
      # So: keep sbigmodel, and do not "fix" this flag to follow has_wide_camera.
      model_transform_extra = get_warp_matrix(device_from_calib_euler, extra_intrinsics, True).astype(np.float32)
      fallback_intrinsics = dc.wide_road.intrinsics if main_wide_camera else dc.narrow_road.intrinsics
      model_transform_extra_fallback = get_warp_matrix(device_from_calib_euler, fallback_intrinsics, True).astype(np.float32)
      # Lateral-origin compensation: the same shear the tinygrad path applies in
      # CameraOffsetHelper.update(). The stock path had no equivalent, so a camera
      # mounted off the vehicle centreline biased every lane line the model saw -
      # this car measured 12-15 cm left of lane centre across four routes. At the
      # param's 0.0 default the shear is the identity, so behaviour is unchanged.
      # Each transform is sheared with the intrinsics it was built from, not with a
      # fixed pair, because the extra one falls back to the narrow intrinsics above.
      calib_height = sm["extrinsicsCalibration"].height
      height = calib_height[0] if calib_height else 1.22
      model_transform_main, model_transform_extra, model_transform_extra_fallback = camera_offset_helper.update_transforms(
        ((model_transform_main, main_intrinsics), (model_transform_extra, extra_intrinsics),
         (model_transform_extra_fallback, fallback_intrinsics)),
        sm["extrinsicsCalibration"].rpyCalib, height)
      extrinsics_calibration_seen = True
      extra_wide_prev = extra_is_wide

    traffic_convention = np.zeros(2)
    traffic_convention[int(is_rhd)] = 1

    vec_desire = np.zeros(ModelConstants.DESIRE_LEN, dtype=np.float32)
    if desire >= 0 and desire < ModelConstants.DESIRE_LEN:
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
      # The extra stream keeps publishing while the main frame is prepared. Its
      # VisionBuf is shared memory, so the selected pixels can be replaced before
      # inference. Falling back for this one cycle is safer than a mismatched pair.
      buf_extra = buf_main
      bufs = dict.fromkeys(model.vision_input_names, buf_main)
      meta_extra = meta_main
      extra_is_wide = False
      wide_active = False
      wide_on_frames = 0
      wide_off_frames += 1
      frame_is_wide = False
      if use_extra_client and wide_off_frames < 10:
        # 第二路已经退回主摄，下一次标定也必须按主摄内参重算。
        extra_wide_prev = None
    elif extra_wide_prev is True:
      # The fallback frame must use the main camera's intrinsics. Force the
      # calibration block below to rebuild the second-slot warp for this frame.
      extra_wide_prev = None
      frame_is_wide = False
    transforms = {name: model_transform_extra if 'big' in name else model_transform_main for name in model.vision_input_names}
    frame_delay = DT_MDL # compensate for time passed since the frame was captured: current_time - timestamp_eof is 50ms on average
    action_delay = DT_MDL / 2 # middle of the interval between model output (current state) and next frame (expected state)
    lat_action_t = lateral_action_time(model.lat_delay, LAT_SMOOTH_SECONDS, frame_delay, action_delay)
    long_action_t = long_delay + frame_delay + action_delay
    inputs: dict[str, np.ndarray] = {
      'desire_pulse': vec_desire,
      'traffic_convention': traffic_convention,
      'action_t': np.array([lat_action_t, long_action_t], dtype=np.float32),
    }
    if getattr(model, 'nav', False):
      inputs.update(nav_runtime.inputs_for_model(sm, v_ego, model))
      nav_status = nav_runtime.status()
    else:
      nav_status = None

    mt1 = time.perf_counter()
    if hasattr(model, 'last_nav_evidence'):
      model.last_nav_evidence = {}
    frame_ids = {'img': meta_main.frame_id, 'big_img': meta_extra.frame_id}
    try:
      send_chestnut = (chestnut_state is not None and
                       run_count % round(ModelConstants.MODEL_RUN_FREQ / SERVICE_LIST['chestnutState'].frequency) == 0)
      model_output = model.run(bufs, transforms, inputs, chestnut_state.send if send_chestnut else None,
                               frame_ids=frame_ids)
    except ExtraFrameChanged:
      # 共享帧在拷贝期间被更新：重读同一对主摄和宽摄一次，仍失败则本轮退回主摄。
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
    except Exception as error:
      log_nav_model_failure(nav_runtime if nav_status is not None else None, model,
                            meta_main.frame_id, meta_extra.frame_id, meta_main.timestamp_eof, error)
      if not params.get_bool("ChestnutActive"):
        raise
      # fallback to small model
      cloudlog.exception("big model failed, fall back to small")
      params.put_bool("ChestnutModelError", True)
      params.put_bool("ChestnutActive", False)
      assert small_model is not None
      model = small_model
      if chestnut_state is not None:
        chestnut_state.big = False
      run_count = 0
      model_output = None
    mt2 = time.perf_counter()
    model_execution_time = mt2 - mt1

    if model_output is not None:
      modelv2_send = messaging.new_message('modelV2')
      drivingdata_send = messaging.new_message('drivingModelData')
      posenet_send = messaging.new_message('cameraOdometry')

      mdv2sp_send = messaging.new_message('modelDataV2SP')
      fill_lateral_delay_debug(mdv2sp_send.modelDataV2SP, model, LAT_SMOOTH_SECONDS, lat_action_t, meta_main.frame_id,
                              model_output, prev_action.desiredCurvature, v_ego, stock=True)
      # Only correct while the driver is not asking for a manoeuvre: a lane change or a
      # turn has the model deliberately leaving the lane centre, and pulling it back
      # would fight the intent.
      blendful_active = desire == log.Desire.none and DH.lane_change_state == log.LaneChangeState.off
      action = get_action_from_model(model_output, prev_action, lat_action_t, long_action_t, v_ego,
                                     partial(blendful.update, model_output, v_ego, blendful_active,
                                             enabled=blendful_enabled))
      # Telemetry so the correction can be inspected onroad instead of inferred from
      # desiredCurvature. All of it is read-only state left by update().
      mdv2sp_send.modelDataV2SP.blendfulCurvature = float(blendful.curvature)
      mdv2sp_send.modelDataV2SP.blendfulTrust = float(blendful.trust)
      # The curvature update() actually received this frame, before any correction.
      mdv2sp_send.modelDataV2SP.modelCurvature = float(blendful.model_curvature)
      mdv2sp_send.modelDataV2SP.blendfulGoingAround = bool(blendful.going_around)
      mdv2sp_send.modelDataV2SP.blendfulLeftProb = float(blendful.l_prob)
      mdv2sp_send.modelDataV2SP.blendfulRightProb = float(blendful.r_prob)
      mdv2sp_send.modelDataV2SP.blendfulLaneWidth = float(blendful.lane_width.x)
      prev_action = action
      fill_model_msg(modelv2_send, model_output, action,
                     publish_state, meta_main.frame_id, frame_id if frame_is_wide else meta_main.frame_id, frame_id,
                     frame_drop_ratio, meta_main.timestamp_eof, model_execution_time, extrinsics_calibration_seen)
      modelv2_send.modelV2.big = is_big_model(model.input_shapes)

      desire_state = modelv2_send.modelV2.meta.desireState
      l_lane_change_prob = desire_state[log.Desire.laneChangeLeft]
      r_lane_change_prob = desire_state[log.Desire.laneChangeRight]
      lane_change_prob = l_lane_change_prob + r_lane_change_prob
      left_edge, right_edge = RELC.update_and_fill(modelv2_send.modelV2, mdv2sp_send.modelDataV2SP, v_ego)
      navi_turn = None
      if cplink_navi_desire_enabled():
        navi_turn = trusted_navi_turn(nav_runtime, v_ego)
      DH.update(sm['carState'], sm['carControl'].latActive, lane_change_prob, left_edge, right_edge,
                blinker_latched=sm['carStateSP'].blinkerLatched,
                left_lane_line=sm['carStateSP'].leftLaneLine,
                right_lane_line=sm['carStateSP'].rightLaneLine,
                lane_line_check=lane_line_check,
                blindspot_unavailable_left=sm['carStateSP'].leftBlindspotUnavailable,
                blindspot_unavailable_right=sm['carStateSP'].rightBlindspotUnavailable,
                navi_turn_direction=navi_turn)
      modelv2_send.modelV2.meta.laneChangeState = DH.lane_change_state
      modelv2_send.modelV2.meta.laneChangeDirection = DH.lane_change_direction
      mdv2sp_send.valid = modelv2_send.valid
      mdv2sp_send.modelDataV2SP.laneTurnDirection = DH.lane_turn_direction
      # Runtime validity is separate from actual RPC input, worker receipts and the
      # recurrent navigation history. Keep all evidence tied to this camera frame.
      fill_nav_status(mdv2sp_send.modelDataV2SP, nav_status)
      fill_nav_evidence(mdv2sp_send.modelDataV2SP, nav_runtime if nav_status is not None else None,
                        model, meta_main.frame_id, meta_extra.frame_id, meta_main.timestamp_eof)

      fill_driving_model_data(drivingdata_send, modelv2_send)
      fill_pose_msg(posenet_send, model_output, meta_main.frame_id, vipc_dropped_frames, meta_main.timestamp_eof, extrinsics_calibration_seen)
      pm.send('modelV2', modelv2_send)
      pm.send('drivingModelData', drivingdata_send)
      pm.send('cameraOdometry', posenet_send)
      pm.send('modelDataV2SP', mdv2sp_send)
    if _prof is not None:
      _t_end = time.perf_counter()
      _prof["recv"].append(_t_recv - _t_loop0)
      _prof["prep"].append(mt1 - _t_recv)
      _prof["run"].append(mt2 - mt1)
      _prof["post"].append(_t_end - mt2)
      _prof["loop"].append(_t_end - _t_loop0)
      if len(_prof["loop"]) >= 100:
        import json as _json
        _o = {}
        for _k, _v in _prof.items():
          _a = np.array(_v) * 1000.0
          _o[_k] = {"p50": round(float(np.percentile(_a, 50)), 2), "p95": round(float(np.percentile(_a, 95)), 2),
                    "mean": round(float(_a.mean()), 2), "n": len(_v)}
        with open("/tmp/modeld_profile.json", "w") as _f:
          _json.dump(_o, _f)
        _prof = {k: [] for k in _prof}
    last_vipc_frame_id = meta_main.frame_id

if __name__ == "__main__":
  try:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--demo', action='store_true', help='A boolean for demo mode.')
    args = parser.parse_args()
    main(demo=args.demo)
  except KeyboardInterrupt:
    cloudlog.warning("got SIGINT")
