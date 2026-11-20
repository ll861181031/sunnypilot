"""``laned`` - optional lane-marking type daemon.

It reads the road camera from VisionIPC, runs the CarrotPilot YOLOv8-Seg lane
classifier on the CPU at a low rate (2.5 Hz by default), and publishes
``laneLineTypes`` on msgq. It is disabled by default
(``Params("LaneLineDetectionEnabled")``) and never touches an actuator.

The message is deliberately separate from ``modelV2``: the driving model keeps
owning lane geometry, while this daemon only classifies the ego-lane boundaries.
Consumers that want a lane-change policy can use ``CarStateSP.leftLaneLine`` /
``rightLaneLine``, which ``card.py`` fills from this message.

Run modes:
* manager / normal: ``Params("LaneLineDetectionEnabled")`` must be true, then the
  daemon waits for camerad, infers at ``LANE_RATE`` and publishes.
* ``--image PATH``: offline check of one image; prints the result as JSON.

Environment configuration (all optional):
  LANE_MODEL_PATH   ONNX file, default ``models/lane.onnx`` next to this module
  LANE_CONF         confidence threshold, default 0.25
  LANE_RATE         inference rate in Hz, default 2.5
  LANE_STREAM       ``narrow`` (default) or ``wide``; falls back to the other
"""

import argparse
import json
import os
import signal
import threading
import time
from dataclasses import dataclass
from collections.abc import Iterable

from msgq.visionipc import VisionIpcClient

from openpilot.cereal import messaging
from openpilot.cereal.messaging import PubMaster
from openpilot.cereal.visionipc import VisionStreamType
from openpilot.common.params import Params
from openpilot.common.realtime import Ratekeeper, config_realtime_process
from openpilot.common.swaglog import cloudlog
from openpilot.sunnypilot.laned.lane_inference import DEFAULT_LANE_MODEL_PATH, LaneInference
from openpilot.sunnypilot.laned.nv12 import nv12_y_plane

try:
  import cv2
  # The lane model is a low-rate side task; keep OpenCV from taking cores that
  # modeld/controlsd need on the device.
  cv2.setNumThreads(1)
except ImportError:
  cv2 = None

CAMERA = "camerad"
DEFAULT_RATE = 2.5
DEFAULT_CONF = 0.25
DEFAULT_STREAM = "narrow"
STREAM_CHOICES = {
  "narrow": VisionStreamType.VISION_STREAM_NARROW_ROAD,
  "wide": VisionStreamType.VISION_STREAM_WIDE_ROAD,
}
MODEL_RETRY_INITIAL_S = 5.0
MODEL_RETRY_MAX_S = 60.0
STREAM_POLL_INTERVAL_S = 0.5
FRAME_TIMEOUT_S = 5.0
ERROR_LOG_INTERVAL_S = 10.0


@dataclass(frozen=True)
class LaneConfig:
  """Daemon configuration, parsed from the environment with safe fallbacks."""

  model_path: str = str(DEFAULT_LANE_MODEL_PATH)
  conf: float = DEFAULT_CONF
  rate: float = DEFAULT_RATE
  stream: str = DEFAULT_STREAM

  @classmethod
  def from_env(cls, environ: dict | None = None, model_path: str | None = None) -> "LaneConfig":
    env = os.environ if environ is None else environ
    resolved_model = model_path or env.get("LANE_MODEL_PATH") or str(DEFAULT_LANE_MODEL_PATH)
    conf = _float_env(env, "LANE_CONF", DEFAULT_CONF, 0.01, 0.99)
    rate = _float_env(env, "LANE_RATE", DEFAULT_RATE, 0.2, 20.0)
    stream = env.get("LANE_STREAM", DEFAULT_STREAM)
    if stream not in STREAM_CHOICES:
      stream = DEFAULT_STREAM
    return cls(model_path=resolved_model, conf=conf, rate=rate, stream=stream)


def _float_env(env: dict, name: str, default: float, minimum: float, maximum: float) -> float:
  raw = env.get(name)
  if raw is None or raw == "":
    return default
  try:
    value = float(raw)
  except (TypeError, ValueError):
    return default
  return min(max(value, minimum), maximum)


def select_stream(available: Iterable[int], preferred: str = DEFAULT_STREAM) -> VisionStreamType | None:
  """Pick the preferred road stream, falling back to the other one."""
  available = set(available)
  order = [preferred] + [name for name in STREAM_CHOICES if name != preferred]
  for name in order:
    if STREAM_CHOICES[name] in available:
      return STREAM_CHOICES[name]
  return None


def connect_client(preferred: str, stop_event: threading.Event) -> VisionIpcClient | None:
  """Wait for camerad to publish a road stream and connect to it."""
  cloudlog.info("laned: waiting for camerad %s road stream", preferred)
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


def load_inference(config: LaneConfig, stop_event: threading.Event) -> LaneInference | None:
  """Load the model, retrying with exponential backoff instead of crash-looping."""
  delay = MODEL_RETRY_INITIAL_S
  last_error: str | None = None
  while not stop_event.is_set():
    inference = LaneInference(config.model_path)
    try:
      if inference.load():
        inference.warmup()
        cloudlog.info("laned: model=%s conf=%.2f rate=%.1fHz stream=%s",
                      config.model_path, config.conf, config.rate, config.stream)
        return inference
      message = inference.error or "unknown model load failure"
    except Exception as exc:
      # A model that loads but cannot run is just as unusable as a missing file;
      # keep the retry loop instead of crash-looping through the manager.
      message = f"{type(exc).__name__}: {exc}"
      inference.valid = False
      inference.error = message

    if message != last_error:
      cloudlog.error({"laned": "model unavailable", "model": config.model_path,
                      "error": message, "retry_s": delay})
      last_error = message
    stop_event.wait(delay)
    delay = min(delay * 2, MODEL_RETRY_MAX_S)
  return None


def build_lane_message(result: dict, frame_id: int, timestamp_eof: int, inference_ms: float,
                       model_name: str):
  """Build one ``laneLineTypes`` message (a ``log.Event`` with the union field set)."""
  message = messaging.new_message("laneLineTypes")
  message.valid = bool(result.get("valid", False))
  payload = message.laneLineTypes
  payload.frameId = int(frame_id)
  payload.timestampEof = int(timestamp_eof)
  payload.leftType = int(result.get("leftLine", -1))
  payload.rightType = int(result.get("rightLine", -1))
  payload.leftConfidence = float(result.get("leftConf", 0.0))
  payload.rightConfidence = float(result.get("rightConf", 0.0))
  payload.inferenceMs = float(inference_ms)
  payload.candidatesCount = int(result.get("candidatesCount", 0))
  payload.model = str(model_name)
  return message


def run_daemon(config: LaneConfig, stop_event: threading.Event) -> None:
  params = Params()
  if not params.get_bool("LaneLineDetectionEnabled"):
    cloudlog.info("laned: LaneLineDetectionEnabled is false, not starting")
    return

  config_realtime_process(7, 54)
  inference = load_inference(config, stop_event)
  if inference is None:
    return
  client = connect_client(config.stream, stop_event)
  if client is None:
    return
  cloudlog.info("laned: connected to %s %dx%d", CAMERA, client.width, client.height)

  pm = PubMaster(["laneLineTypes"])
  ratekeeper = Ratekeeper(config.rate, print_delay_threshold=0.5)
  model_name = os.path.basename(config.model_path)
  last_frame_time = None
  last_error_time = 0.0

  while not stop_event.is_set():
    buffer = client.recv(0)
    if buffer is None:
      now = time.monotonic()
      if last_frame_time is None:
        last_frame_time = now
      elif now - last_frame_time > FRAME_TIMEOUT_S:
        cloudlog.warning("laned: no camera frames for %.1fs, reconnecting", now - last_frame_time)
        client = connect_client(config.stream, stop_event)
        if client is None:
          break
        last_frame_time = None
      ratekeeper.keep_time()
      continue
    last_frame_time = None

    try:
      frame = nv12_y_plane(buffer.data, buffer.width, buffer.height, buffer.stride)
      started = time.monotonic()
      result = inference.infer(frame, buffer.width, buffer.height, conf_thresh=config.conf)
      inference_ms = (time.monotonic() - started) * 1000.0
      # FrameMeta-style timestamps live on the client, not on VisionBuf:
      # VisionBuf exposes frame_id/data/geometry only, so buffer.timestamp_eof
      # raised AttributeError and laned dropped every frame.
      pm.send("laneLineTypes", build_lane_message(
        result, client.frame_id, client.timestamp_eof, inference_ms, model_name,
      ))
      if not result["valid"]:
        now = time.monotonic()
        if now - last_error_time > ERROR_LOG_INTERVAL_S:
          cloudlog.error({"laned": "inference failed", "error": result.get("error", "")})
          last_error_time = now
    except Exception:
      now = time.monotonic()
      if now - last_error_time > ERROR_LOG_INTERVAL_S:
        cloudlog.exception("laned: frame processing failed")
        last_error_time = now

    ratekeeper.keep_time()


def run_image(config: LaneConfig, path: str, publish: bool) -> int:
  """Run one image through the classifier; returns a process exit code."""
  if cv2 is None:
    raise SystemExit("laned: OpenCV is required for --image")
  image = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
  if image is None:
    raise SystemExit(f"laned: cannot read image: {path}")

  inference = LaneInference(config.model_path)
  if not inference.load():
    raise SystemExit(f"laned: model unavailable: {inference.error}")

  started = time.monotonic()
  result = inference.infer(image, image.shape[1], image.shape[0], conf_thresh=config.conf)
  inference_ms = (time.monotonic() - started) * 1000.0
  print(json.dumps({
    "image": path,
    "width": int(image.shape[1]),
    "height": int(image.shape[0]),
    "inference_ms": round(inference_ms, 1),
    **result,
  }, indent=2))

  if publish:
    pm = PubMaster(["laneLineTypes"])
    pm.send("laneLineTypes", build_lane_message(
      result, 0, time.monotonic_ns(), inference_ms, os.path.basename(config.model_path),
    ))
  return 0 if result.get("valid") else 1


def main() -> None:
  parser = argparse.ArgumentParser(description="Optional lane-marking type daemon")
  parser.add_argument("--image", help="run one image instead of the camera loop")
  parser.add_argument("--model", help="override LANE_MODEL_PATH")
  parser.add_argument("--conf", type=float, help="override LANE_CONF")
  parser.add_argument("--stream", choices=tuple(STREAM_CHOICES), help="override LANE_STREAM")
  parser.add_argument("--publish", action="store_true", help="with --image, also publish on msgq")
  args = parser.parse_args()

  config = LaneConfig.from_env(model_path=args.model)
  if args.conf is not None:
    config = LaneConfig(config.model_path, min(max(args.conf, 0.01), 0.99), config.rate, config.stream)
  if args.stream is not None:
    config = LaneConfig(config.model_path, config.conf, config.rate, args.stream)

  if args.image:
    raise SystemExit(run_image(config, args.image, args.publish))

  stop_event = threading.Event()
  signal.signal(signal.SIGINT, lambda *_: stop_event.set())
  signal.signal(signal.SIGTERM, lambda *_: stop_event.set())
  run_daemon(config, stop_event)


if __name__ == "__main__":
  main()
