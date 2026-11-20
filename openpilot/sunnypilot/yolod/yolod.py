"""``yolod`` - optional, informational-only YOLO object detection daemon.

It reads the road camera from VisionIPC, runs the detector on the CPU at a low
rate (10 Hz by default) and publishes ``yoloObjects`` on msgq. Nothing in the
driving stack subscribes to that message for control: the daemon exists so the
web console and offline tooling can show what the camera sees. It is disabled by
default (``Params("YoloEnabled")``) and never touches an actuator.

Run modes:
* manager / normal: ``Params("YoloEnabled")`` must be true, then the daemon waits
  for camerad, detects at ``YOLO_RATE`` and publishes ``yoloObjects``.
* ``--video PATH``: offline replay for debugging; prints per-class counts and
  timings. Pass ``--publish`` to exercise the msgq path as well.

Environment configuration (all optional):
  YOLO_MODEL_PATH   ONNX file, default ``models/yolo11n.onnx`` next to this module
  YOLO_BACKEND      ``onnxruntime`` (default CPU), ``cv2dnn`` or ``ncnn`` (experimental
                    Vulkan GPU); ``onnxruntime`` falls back to cv2.dnn when the
                    runtime is unusable, ``ncnn`` never falls back to the CPU
  YOLO_NCNN_PARAM   ncnn ``.param`` file, default ``models/yolo11n.ncnn.param``
  YOLO_NCNN_BIN     ncnn ``.bin`` file, default ``models/yolo11n.ncnn.bin``
  YOLO_NCNN_NAMES   names JSON next to the ncnn model, default
                    ``models/yolo11n.ncnn.names.json``
  YOLO_CONF         confidence threshold, default 0.25
  YOLO_IOU          NMS IoU threshold, default 0.45
  YOLO_RATE         publish rate in Hz, default 10
  YOLO_THREADS      ONNX Runtime intra-op / ncnn CPU thread count, default 2
                    (ncnn's GPU path measured fastest at 1-2)
  YOLO_STREAM       ``narrow`` (default) or ``wide``; falls back to the other
  YOLO_CLASSES      comma-separated class names, ``all`` for every model class;
                    unset/empty means the road-relevant default set
"""

import argparse
import os
import signal
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from collections.abc import Iterable

from msgq.visionipc import VisionIpcClient

from openpilot.cereal import messaging
from openpilot.cereal.messaging import PubMaster
from openpilot.cereal.visionipc import VisionStreamType
from openpilot.common.params import Params
from openpilot.common.realtime import Ratekeeper, config_realtime_process
from openpilot.common.swaglog import cloudlog
from openpilot.sunnypilot.yolod.detector import DEFAULT_CONF, DEFAULT_IOU, Detection, Detector
from openpilot.sunnypilot.yolod.nv12 import nv12_to_bgr
from openpilot.sunnypilot.yolod.paths import (
  DEFAULT_NCNN_BIN,
  DEFAULT_NCNN_NAMES,
  DEFAULT_NCNN_PARAM,
  DEFAULT_ONNX_MODEL,
  MODELS_DIR,
)

CAMERA = "camerad"
DEFAULT_RATE = 10.0
DEFAULT_THREADS = 2
DEFAULT_MODEL_PATH = DEFAULT_ONNX_MODEL
# Light-state classifier used to turn a crop into red/yellow/green. Optional: it is
# only loaded when the detector actually reports a traffic-light class, so a config
# that detects cars only never pays for it.
DEFAULT_LIGHT_STATE_MODEL = os.path.join(MODELS_DIR, "traffic_light_state.onnx")
TRAFFIC_LIGHT_CLASS_NAMES = ("traffic_light", "traffic light", "trafficlight")
BACKENDS = ("onnxruntime", "cv2dnn", "ncnn")
# COCO classes that matter on a road. Everything else in COCO (couch, hair drier,
# ...) is noise at 10 Hz over a driving scene.
DEFAULT_CLASSES = ("person", "bicycle", "car", "motorcycle", "bus", "truck", "train")
STREAM_CHOICES = {
  "narrow": VisionStreamType.VISION_STREAM_NARROW_ROAD,
  "wide": VisionStreamType.VISION_STREAM_WIDE_ROAD,
}
MAX_OBJECTS = 100
MODEL_RETRY_INITIAL_S = 5.0
MODEL_RETRY_MAX_S = 60.0
STREAM_POLL_INTERVAL_S = 0.5
FRAME_TIMEOUT_S = 5.0
ERROR_LOG_INTERVAL_S = 10.0


@dataclass(frozen=True)
class YoloConfig:
  """Daemon configuration, parsed from the environment with safe fallbacks."""

  model_path: str = DEFAULT_MODEL_PATH
  conf: float = DEFAULT_CONF
  iou: float = DEFAULT_IOU
  rate: float = DEFAULT_RATE
  threads: int = DEFAULT_THREADS
  classes: tuple[str, ...] | None = DEFAULT_CLASSES
  stream: str = "narrow"
  backend: str = "onnxruntime"
  ncnn_param: str = DEFAULT_NCNN_PARAM
  ncnn_bin: str = DEFAULT_NCNN_BIN
  ncnn_names: str = DEFAULT_NCNN_NAMES
  imgsz: int | None = None
  # Reject detections whose centre sits further than this fraction of the frame
  # width from the middle. None keeps every detection: the side lights are useful
  # context (they say what the neighbouring lanes are doing) and only the ego
  # assignment decides what may stop the car. Set it when you want the published
  # list itself narrowed to what is straight ahead.
  centre_gate: float | None = None
  warnings: tuple[str, ...] = field(default_factory=tuple)

  @classmethod
  def from_env(cls, environ: dict | None = None, model_path: str | None = None) -> "YoloConfig":
    env = os.environ if environ is None else environ
    warnings: list[str] = []

    resolved_model = model_path or env.get("YOLO_MODEL_PATH") or DEFAULT_MODEL_PATH
    conf = _float_env(env, "YOLO_CONF", DEFAULT_CONF, 0.01, 0.99, warnings)
    iou = _float_env(env, "YOLO_IOU", DEFAULT_IOU, 0.01, 0.99, warnings)
    rate = _float_env(env, "YOLO_RATE", DEFAULT_RATE, 0.5, 60.0, warnings)
    threads = int(round(_float_env(env, "YOLO_THREADS", DEFAULT_THREADS, 1.0, 32.0, warnings)))

    raw_classes = env.get("YOLO_CLASSES")
    if raw_classes is None or not raw_classes.strip():
      classes: tuple[str, ...] | None = DEFAULT_CLASSES
    elif raw_classes.strip().lower() == "all":
      classes = None
    else:
      parsed = tuple(name.strip() for name in raw_classes.split(",") if name.strip())
      classes = parsed or DEFAULT_CLASSES
      if not parsed:
        warnings.append(f"YOLO_CLASSES={raw_classes!r} parsed to no class names; using the default set")

    raw_stream = (env.get("YOLO_STREAM") or "").strip().lower()
    stream = "narrow"
    if raw_stream in STREAM_CHOICES:
      stream = raw_stream
    elif raw_stream:
      warnings.append(f"YOLO_STREAM={raw_stream!r} is not one of {sorted(STREAM_CHOICES)}; using 'narrow'")

    raw_backend = (env.get("YOLO_BACKEND") or "onnxruntime").strip().lower()
    backend = raw_backend
    if raw_backend not in BACKENDS:
      backend = "onnxruntime"
      warnings.append(f"YOLO_BACKEND={raw_backend!r} is not one of {BACKENDS}; using 'onnxruntime'")

    ncnn_param = env.get("YOLO_NCNN_PARAM") or DEFAULT_NCNN_PARAM
    ncnn_bin = env.get("YOLO_NCNN_BIN") or DEFAULT_NCNN_BIN
    ncnn_names = env.get("YOLO_NCNN_NAMES") or DEFAULT_NCNN_NAMES

    imgsz = None
    raw_imgsz = env.get("YOLO_IMGSZ")
    if raw_imgsz:
      try:
        parsed = int(raw_imgsz)
        if parsed > 0:
          imgsz = parsed
        else:
          warnings.append(f"YOLO_IMGSZ={raw_imgsz!r} is not positive; using the backend default")
      except ValueError:
        warnings.append(f"YOLO_IMGSZ={raw_imgsz!r} is not an integer; using the backend default")

    centre_gate = None
    raw_gate = env.get("YOLO_CENTRE_GATE")
    if raw_gate:
      try:
        parsed_gate = float(raw_gate)
        if 0.0 < parsed_gate <= 0.5:
          centre_gate = parsed_gate
        else:
          warnings.append(f"YOLO_CENTRE_GATE={raw_gate!r} must be in (0, 0.5]; ignoring")
      except ValueError:
        warnings.append(f"YOLO_CENTRE_GATE={raw_gate!r} is not a number; ignoring")

    return cls(model_path=resolved_model, conf=conf, iou=iou, rate=rate, threads=threads,
               classes=classes, stream=stream, backend=backend, ncnn_param=ncnn_param,
               ncnn_bin=ncnn_bin, ncnn_names=ncnn_names, imgsz=imgsz,
               centre_gate=centre_gate, warnings=tuple(warnings))


def _float_env(env: dict, name: str, default: float, minimum: float, maximum: float,
               warnings: list[str]) -> float:
  raw = env.get(name)
  if raw is None or not raw.strip():
    return default
  try:
    value = float(raw)
  except ValueError:
    warnings.append(f"{name}={raw!r} is not a number; using {default}")
    return default
  if not minimum <= value <= maximum:
    clamped = min(max(value, minimum), maximum)
    warnings.append(f"{name}={value} is outside [{minimum}, {maximum}]; clamped to {clamped}")
    return clamped
  return value


def select_stream(available: Iterable[int], preferred: str = "narrow") -> VisionStreamType | None:
  """Pick the preferred road stream, falling back to the other one."""
  available = set(available)
  order = [preferred] + [name for name in STREAM_CHOICES if name != preferred]
  for name in order:
    if STREAM_CHOICES[name] in available:
      return STREAM_CHOICES[name]
  return None


def build_yolo_message(detections: list[Detection], class_names: list[str], frame_id: int,
                       frame_width: int, frame_height: int, inference_ms: float,
                       model_name: str, max_objects: int = MAX_OBJECTS):
  """Build one ``yoloObjects`` message (a ``log.Event`` with the union field set)."""
  message = messaging.new_message("yoloObjects")
  message.valid = True
  payload = message.yoloObjects
  payload.frameId = int(frame_id)
  payload.frameWidth = int(frame_width)
  payload.frameHeight = int(frame_height)
  payload.inferenceMs = float(inference_ms)
  payload.model = str(model_name)

  # classNames is always the model's full list: classId indexes into it even when
  # the configured filter only publishes a subset of the classes.
  names = list(class_names)
  payload.init("classNames", len(names))
  for index, name in enumerate(names):
    payload.classNames[index] = name

  selected = detections[:max_objects]
  payload.init("objects", len(selected))
  for index, detection in enumerate(selected):
    x, y, w, h = detection.xywh
    target = payload.objects[index]
    target.classId = int(detection.class_id)
    target.confidence = float(detection.score)
    target.x = float(x)
    target.y = float(y)
    target.w = float(w)
    target.h = float(h)
  return message


def traffic_light_class_ids(detector) -> set[int]:
  """Class ids the detector calls a traffic light, matched by name."""
  wanted = {name.replace(" ", "_").lower() for name in TRAFFIC_LIGHT_CLASS_NAMES}
  return {index for index, name in enumerate(detector.class_names)
          if name.replace(" ", "_").lower() in wanted}


class LightContext:
  """Turns light boxes into an ego-lane verdict, once per frame.

  Kept out of the detector so the detection path stays identical whether or not
  lights are being watched. Every failure is swallowed into "no verdict": the
  consumer treats an empty state as no information, and the guard then behaves
  exactly as it does without this feature.
  """

  def __init__(self, model_path: str = DEFAULT_LIGHT_STATE_MODEL, watch=None,
               sub_master=None):
    from openpilot.sunnypilot.yolod.light_veto import EgoLightWatch
    from openpilot.sunnypilot.yolod.traffic_light_classifier import TrafficLightStateClassifier

    self.classifier = TrafficLightStateClassifier(model_path)
    self.watch = watch if watch is not None else EgoLightWatch()
    self.sm = sub_master
    self.axis_slope = None
    self._rpy = None

  @property
  def available(self) -> bool:
    return self.classifier.available

  def _lane_lines_and_calibration(self):
    """Latest lane lines and calibration, or (None, None) when unavailable."""
    if self.sm is None:
      return None, None
    try:
      self.sm.update()
    except Exception:
      return None, None
    lane_lines = None
    if self.sm.seen.get('modelV2', False) and self.sm.valid.get('modelV2', False):
      model = self.sm['modelV2']
      if len(model.laneLines) >= 4 and len(model.laneLines[1].x) > 5:
        from openpilot.sunnypilot.yolod.lane_association import lane_lines_from_model
        lane_lines = lane_lines_from_model(model)
    if self.sm.seen.get('extrinsicsCalibration', False) and self.sm.valid.get('extrinsicsCalibration', False):
      calib = self.sm['extrinsicsCalibration']
      if len(calib.rpyCalib) == 3:
        self._rpy = list(calib.rpyCalib)
    return lane_lines, self._rpy

  def evaluate(self, detections, bgr, width: int, height: int,
               light_ids: set[int]) -> tuple[str, int, float]:
    """Return (state, agreeing_frames, distance); distance -1.0 when unknown."""
    if not self.available or not light_ids or bgr is None:
      return "", 0, -1.0
    lane_lines, rpy = self._lane_lines_and_calibration()
    if lane_lines is None:
      # Without lane lines there is no lane to assign to. Reporting nothing is
      # correct: guessing "dead ahead" would put a left-turn light in our lane.
      return "", 0, -1.0

    boxes, states = [], []
    for detection in detections:
      if detection.class_id not in light_ids:
        continue
      x1, y1, x2, y2 = detection.box
      px1, py1 = int(x1 * width), int(y1 * height)
      px2, py2 = int(x2 * width), int(y2 * height)
      if px2 - px1 < 4 or py2 - py1 < 4:
        continue
      crop = bgr[py1:py2, px1:px2]
      if crop.size == 0:
        continue
      prediction = self.classifier.classify(crop)
      boxes.append((float(px1), float(py1), float(px2), float(py2)))
      states.append(prediction.label if prediction is not None else None)
    if not boxes:
      return "", 0, -1.0

    light = self.watch.observe(boxes, states, lane_lines, width, rpy=rpy)
    if light is None:
      return "", 0, -1.0
    distance = light.distance_m if light.distance_m is not None else -1.0
    return light.state, int(light.frames_agreeing), float(distance)


def create_detector(config: YoloConfig, class_filter: Iterable[str | int] | None = None,
                    strict_filter: bool = False):
  """Instantiate the configured backend.

  ``ncnn`` is imported lazily: the default CPU path must keep working on a machine
  where the optional ncnn package is not installed. ``class_filter=None`` means all
  model classes; the daemon passes ``config.classes``.
  """
  if config.backend == "ncnn":
    from openpilot.sunnypilot.yolod.detector_ncnn import NcnnDetector
    return NcnnDetector(config.ncnn_param, config.ncnn_bin, names_path=config.ncnn_names,
                        conf=config.conf, iou=config.iou, threads=config.threads,
                        class_filter=class_filter, strict_filter=strict_filter,
                        imgsz=config.imgsz)
  return Detector(config.model_path, conf=config.conf, iou=config.iou, threads=config.threads,
                  class_filter=class_filter, strict_filter=strict_filter,
                  backend="cv2dnn" if config.backend == "cv2dnn" else "onnxruntime")


def connect_client(preferred: str, stop_event: threading.Event) -> VisionIpcClient | None:
  """Wait for camerad to publish a road stream and connect to it."""
  cloudlog.info("yolod: waiting for camerad %s road stream", preferred)
  while not stop_event.is_set():
    available = VisionIpcClient.available_streams(CAMERA, block=False)
    stream = select_stream(available, preferred)
    if stream is not None:
      client = VisionIpcClient(CAMERA, stream, True)
      while not stop_event.is_set() and not client.connect(False):
        stop_event.wait(0.1)
      if client.is_connected():
        return client
    stop_event.wait(STREAM_POLL_INTERVAL_S)
  return None


def load_detector(config: YoloConfig, stop_event: threading.Event) -> Detector | None:
  """Load the model, retrying with exponential backoff instead of crash-looping.

  A missing model or a broken inference backend is a real condition (wrong path,
  half-copied file, missing runtime). Exiting would make the manager restart the
  process every second and bury the one useful log line, so the daemon logs the
  error once per distinct message and waits.
  """
  delay = MODEL_RETRY_INITIAL_S
  last_error: str | None = None
  while not stop_event.is_set():
    try:
      detector = create_detector(config, config.classes, strict_filter=False)
      if detector.unmatched_classes:
        cloudlog.error({"yolod": "class filter names not in model", "model": config.model_path,
                        "missing": detector.unmatched_classes, "publishing_all_classes": detector.class_filter is None})
      detector.warmup()
      cloudlog.info("yolod: backend=%s device=%s input=%dx%d classes=%d filter=%s inference_ms=%.1f",
                    detector.backend, getattr(detector, "device_name", ""), detector.input_w,
                    detector.input_h, len(detector.class_names), detector.class_filter,
                    detector.inference_ms)
      return detector
    except Exception as error:
      message = f"{type(error).__name__}: {error}"
      if message != last_error:
        cloudlog.error({"yolod": "model unavailable", "backend": config.backend,
                        "model": config.ncnn_param if config.backend == "ncnn" else config.model_path,
                        "error": message, "retry_s": delay})
        last_error = message
      stop_event.wait(delay)
      delay = min(delay * 2, MODEL_RETRY_MAX_S)
  return None


def centre_gate_filter(detections: list[Detection], centre_gate: float | None) -> list[Detection]:
  """Drop detections that are not roughly straight ahead.

  ``detection.box`` is normalised to the frame, so the gate needs no focal length:
  ``centre_gate`` is the largest allowed |x_centre - 0.5|. On this camera the
  published frame is 1344 px wide at a 1864 px focal length, so 0.10 is about
  4.1 degrees off axis and 0.21 is about 8.5.
  """
  if centre_gate is None:
    return detections
  kept = []
  for detection in detections:
    x1, _, x2, _ = detection.box
    if abs(0.5 * (x1 + x2) - 0.5) <= centre_gate:
      kept.append(detection)
  return kept


def _publish_frame(pm: PubMaster, detector: Detector, bgr, frame_id: int, width: int, height: int,
                   model_name: str, light: LightContext | None = None,
                   light_ids: set[int] | None = None,
                   centre_gate: float | None = None) -> list[Detection]:
  detections = detector.detect(bgr)
  # Association sees every light; the gate only narrows what gets published.
  # Filtering first would hide the neighbouring lane's light from the lane
  # geometry that decides which light is ego's.
  state, frames, distance = "", 0, -1.0
  if light is not None and light_ids:
    state, frames, distance = light.evaluate(detections, bgr, width, height, light_ids)
  published = centre_gate_filter(detections, centre_gate)
  message = build_yolo_message(published, detector.class_names, frame_id, width, height,
                               detector.inference_ms, model_name)
  message.yoloObjects.egoLightState = state
  message.yoloObjects.egoLightFrames = frames
  message.yoloObjects.egoLightDistance = distance
  pm.send("yoloObjects", message)
  return published


def run_daemon(config: YoloConfig, stop_event: threading.Event) -> None:
  params = Params()
  if not params.get_bool("YoloEnabled"):
    cloudlog.info("yolod: YoloEnabled is false, not starting detection")
    return

  config_realtime_process(7, 54)
  detector = load_detector(config, stop_event)
  if detector is None:
    return
  client = connect_client(config.stream, stop_event)
  if client is None:
    return
  cloudlog.info("yolod: connected to %s %dx%d", CAMERA, client.width, client.height)

  pm = PubMaster(["yoloObjects"])
  light_ids = traffic_light_class_ids(detector)
  light = None
  if light_ids:
    sm = messaging.SubMaster(['modelV2', 'extrinsicsCalibration'], poll='modelV2')
    light = LightContext(sub_master=sm)
    cloudlog.info({"yolod": "watching ego-lane lights", "class_ids": sorted(light_ids),
                   "classifier_available": light.available})
  ratekeeper = Ratekeeper(config.rate, print_delay_threshold=0.5)
  model_name = os.path.basename(config.model_path)
  client_width, client_height = client.width, client.height
  last_frame_time = None
  last_error_time = 0.0

  while not stop_event.is_set():
    buffer = client.recv(0)
    if buffer is None:
      now = time.monotonic()
      if last_frame_time is None:
        last_frame_time = now
      elif now - last_frame_time > FRAME_TIMEOUT_S:
        cloudlog.warning("yolod: no camera frames for %.1fs, reconnecting", now - last_frame_time)
        client = connect_client(config.stream, stop_event)
        if client is None:
          break
        client_width, client_height = client.width, client.height
        last_frame_time = None
      ratekeeper.keep_time()
      continue
    last_frame_time = None

    try:
      if (buffer.width, buffer.height) != (client_width, client_height):
        client_width, client_height = buffer.width, buffer.height
        cloudlog.warning("yolod: camera geometry changed to %dx%d", client_width, client_height)
      bgr = nv12_to_bgr(buffer.data, buffer.width, buffer.height, buffer.stride, buffer.uv_offset)
      _publish_frame(pm, detector, bgr, buffer.frame_id, buffer.width, buffer.height, model_name,
                     light=light, light_ids=light_ids, centre_gate=config.centre_gate)
    except Exception:
      now = time.monotonic()
      if now - last_error_time > ERROR_LOG_INTERVAL_S:
        cloudlog.exception("yolod: frame processing failed")
        last_error_time = now

    ratekeeper.keep_time()


def run_video(config: YoloConfig, video: str, publish: bool, every: int, max_frames: int | None,
              stop_event: threading.Event) -> int:
  """Offline replay of a recorded video; returns the number of processed frames."""
  from openpilot.sunnypilot.yolod.video import find_video_path, iter_frames, probe_video

  detector = create_detector(config, config.classes, strict_filter=False)
  detector.warmup()
  path = find_video_path(video)
  info = probe_video(path)
  model_name = os.path.basename(config.ncnn_param if config.backend == "ncnn" else config.model_path)
  device = getattr(detector, "device_name", "")
  device_text = f" ({device})" if device else ""
  print(f"yolod replay: {path} ({info.width}x{info.height} @ {info.fps:.1f} fps), backend={detector.backend}{device_text}, model={model_name}")

  pm = PubMaster(["yoloObjects"]) if publish else None
  counts: Counter[str] = Counter()
  inference_ms: list[float] = []
  processed = 0
  started = time.monotonic()

  for frame_index, bgr in iter_frames(path, every=every, max_frames=max_frames):
    if stop_event.is_set():
      break
    detections = centre_gate_filter(detector.detect(bgr), config.centre_gate)
    processed += 1
    inference_ms.append(detector.inference_ms)
    counts.update(detection.label for detection in detections)
    if pm is not None:
      message = build_yolo_message(detections, detector.class_names, frame_index, info.width,
                                   info.height, detector.inference_ms, model_name)
      pm.send("yoloObjects", message)
    if processed % 50 == 0:
      print(f"  {processed} frames, {sum(inference_ms[-50:]) / len(inference_ms[-50:]):.1f} ms/frame")

  elapsed = max(1e-6, time.monotonic() - started)
  mean_ms = sum(inference_ms) / len(inference_ms) if inference_ms else 0.0
  summary = dict(counts.most_common())
  print(f"yolod replay done: {processed} frames in {elapsed:.2f}s ({processed / elapsed:.1f} fps), {mean_ms:.1f} ms/frame, detections={summary}")
  return processed


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
  parser = argparse.ArgumentParser(description="Informational YOLO object detection daemon")
  parser.add_argument("--video", type=str, default=None,
                      help="offline debug mode: replay a video file or route directory instead of the camera")
  parser.add_argument("--publish", action="store_true",
                      help="with --video, also publish yoloObjects on msgq")
  parser.add_argument("--every", type=int, default=1, help="with --video, process one frame out of N")
  parser.add_argument("--max-frames", type=int, default=None, help="with --video, stop after N frames")
  return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
  args = _parse_args(argv)
  config = YoloConfig.from_env()
  for warning in config.warnings:
    cloudlog.warning("yolod: %s", warning)

  stop_event = threading.Event()

  def _handle_signal(signum, frame) -> None:
    cloudlog.warning("yolod: got signal %s, stopping", signum)
    stop_event.set()

  signal.signal(signal.SIGINT, _handle_signal)
  signal.signal(signal.SIGTERM, _handle_signal)

  if args.video is not None:
    if args.every < 1:
      parser_error = f"--every must be >= 1, got {args.every}"
      cloudlog.error("yolod: %s", parser_error)
      return
    run_video(config, args.video, args.publish, args.every, args.max_frames, stop_event)
    return

  run_daemon(config, stop_event)


if __name__ == "__main__":
  try:
    main()
  except KeyboardInterrupt:
    cloudlog.warning("yolod: interrupted")
