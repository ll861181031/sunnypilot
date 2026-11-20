import os
import operator
import platform
from functools import reduce

from opendbc.car.structs import car
from openpilot.cereal import custom
from openpilot.common.params import Params
from openpilot.common.camera_config import driver_camera_available, driver_camera_enabled
from openpilot.common.hardware import PC, COMMA_HARDWARE
from openpilot.common.swaglog import cloudlog
from openpilot.system.manager.process import PythonProcess, NativeProcess, DaemonProcess
from openpilot.common.hardware.hw import Paths
from openpilot.selfdrive.pandad import usb_readonly_enabled

from openpilot.sunnypilot.mapd.runtime import mapd_unavailable_reason

from openpilot.sunnypilot.models.helpers import get_active_model_runner
from openpilot.sunnypilot.sunnylink.utils import sunnylink_need_register, sunnylink_ready, use_sunnylink_uploader

WEBCAM = os.getenv("USE_WEBCAM") is not None
# Headless bench switch: SKIP_UI=1 keeps the UI process out of the stack (saves ~1 core).
SKIP_UI = os.getenv("SKIP_UI") == "1"
IMU_ENABLED = os.getenv("SUNNYPILOT_DISABLE_IMU", "0") != "1"
# The desktop IMU is the ESP32-C3.
ESP32_IMU = PC and platform.system() == "Linux"
# Whether a driver-facing camera actually exists and can be opened. camerad drops a
# DRIVER_CAM it rejects, so starting the monitoring processes for a camera that will
# never publish cabin frames only produces a permanent camera fault. On PC the cabin
# stream only exists under USE_WEBCAM (the native camerad has no cabin input there).
DRIVER_CAMERA_AVAILABLE = driver_camera_available() and (not PC or WEBCAM)
# Evaluated once at startup: a missing/corrupt binary disables both the mapd and
# mapd_manager processes, so say why rather than dropping the feature silently.
MAPD_REASON = mapd_unavailable_reason()
MAPD_SUPPORTED = MAPD_REASON is None
if MAPD_REASON is not None:
  cloudlog.warning(f"mapd disabled: {MAPD_REASON}")

def driverview(started: bool, params: Params, CP: car.CarParams) -> bool:
  return started or (driver_camera_enabled() and params.get_bool("IsDriverViewEnabled"))

def webcam(started: bool, params: Params, CP: car.CarParams) -> bool:
  return usb_readonly_enabled() or driverview(started, params, CP)

def notcar(started: bool, params: Params, CP: car.CarParams) -> bool:
  return started and CP.notCar

def iscar(started: bool, params: Params, CP: car.CarParams) -> bool:
  return started and not CP.notCar

def logging(started: bool, params: Params, CP: car.CarParams) -> bool:
  run = (not CP.notCar) or not params.get_bool("DisableLogging")
  return started and run


def ui_enabled(started: bool, params: Params, CP: car.CarParams) -> bool:
  return not (SKIP_UI or params.get_bool("DisableUI"))


def encoderd_enabled(started: bool, params: Params, CP: car.CarParams) -> bool:
  # Bench / no-recording switch: encoderd is the single largest CPU consumer after modeld.
  return started and not params.get_bool("DisableEncoderd")

def ublox_available() -> bool:
  return os.path.exists('/dev/ttyHS0') and not os.path.exists('/persist/comma/use-quectel-gps')

def ublox(started: bool, params: Params, CP: car.CarParams) -> bool:
  use_ublox = ublox_available()
  if use_ublox != params.get_bool("UbloxAvailable"):
    params.put_bool("UbloxAvailable", use_ublox, block=True)
  return started and use_ublox

def esp32_gps(started: bool, params: Params, CP: car.CarParams) -> bool:
  # When the ESP32 IMU is enabled, esp32d publishes both IMU and GPS. This
  # sidecar is only for the IMU-disabled case and feeds the same USB GNSS stream
  # to locationd_llk.
  enabled = started and PC and platform.system() == "Linux" and not IMU_ENABLED
  if enabled and params is not None and params.get_bool("UbloxAvailable") and not ublox_available():
    # locationd_llk otherwise listens on gpsLocationExternal and ignores this sidecar.
    params.put_bool("UbloxAvailable", False, block=True)
  return enabled

def joystick(started: bool, params: Params, CP: car.CarParams) -> bool:
  return started and params.get_bool("JoystickDebugMode")

def not_joystick(started: bool, params: Params, CP: car.CarParams) -> bool:
  return started and not params.get_bool("JoystickDebugMode")

def long_maneuver(started: bool, params: Params, CP: car.CarParams) -> bool:
  return started and params.get_bool("LongitudinalManeuverMode")

def lat_maneuver(started: bool, params: Params, CP: car.CarParams) -> bool:
  return started and params.get_bool("LateralManeuverMode")

def not_long_maneuver(started: bool, params: Params, CP: car.CarParams) -> bool:
  return started and not params.get_bool("LongitudinalManeuverMode")

def yolo_enabled(started: bool, params: Params, CP: car.CarParams) -> bool:
  # Informational object detection (fork-local). It only publishes yoloObjects on
  # msgq for the web console and for debugging; it is never read by the control
  # loop, so it is safe to leave off and safe to enable while driving.
  return started and params.get_bool("YoloEnabled")

def lane_line_detection_enabled(started: bool, params: Params, CP: car.CarParams) -> bool:
  # Optional lane-marking classifier (fork-local). It publishes laneLineTypes and
  # fills CarStateSP.leftLaneLine/rightLaneLine; the driving model still owns lane
  # geometry. Disabled by default.
  return started and params.get_bool("LaneLineDetectionEnabled")

def qcomgps(started: bool, params: Params, CP: car.CarParams) -> bool:
  return started and not ublox_available()

def not_simulated(started: bool, params: Params, CP: car.CarParams) -> bool:
  # Bench: with SimulateIgnition the fake carState/selfdriveState publisher owns
  # these topics, so the real card/selfdrived must stay out of the way.
  return started and not params.get_bool("SimulateIgnition")


def always_run(started: bool, params: Params, CP: car.CarParams) -> bool:
  return True

def only_onroad(started: bool, params: Params, CP: car.CarParams) -> bool:
  return started

def only_offroad(started: bool, params: Params, CP: car.CarParams) -> bool:
  return not started

def livestream(started: bool, params: Params, CP: car.CarParams) -> bool:
  return params.get_bool("IsLiveStreaming")


def cplink_video(started: bool, params: Params, CP: car.CarParams) -> bool:
  # ``IsLiveStreaming`` is normally off, so without this condition webrtcd would never
  # start.  The shim touches the file while the phone is connected.
  return started and os.path.exists("/tmp/cplink_video")

def use_copyparty(started, params, CP: car.CarParams) -> bool:
  return bool(params.get_bool("EnableCopyparty"))

def sunnylink_ready_shim(started, params, CP: car.CarParams) -> bool:
  """Shim for sunnylink_ready to match the process manager signature."""
  return sunnylink_ready(params)

def sunnylink_need_register_shim(started, params, CP: car.CarParams) -> bool:
  """Shim for sunnylink_need_register to match the process manager signature."""
  return sunnylink_need_register(params)

def use_sunnylink_uploader_shim(started, params, CP: car.CarParams) -> bool:
  """Shim for use_sunnylink_uploader to match the process manager signature."""
  return use_sunnylink_uploader(params)

def is_tinygrad_model(started, params, CP: car.CarParams) -> bool:
  """Check if the active model runner is tinygrad."""
  return bool(get_active_model_runner(params, not started) == custom.ModelManagerSP.Runner.tinygrad)

def is_stock_model(started, params, CP: car.CarParams) -> bool:
  """Check if the active model runner is stock."""
  return bool(get_active_model_runner(params, not started) == custom.ModelManagerSP.Runner.stock)

def mapd_ready(started: bool, params: Params, CP: car.CarParams) -> bool:
  return bool(os.path.exists(Paths.mapd_root()))

def uploader_ready(started: bool, params: Params, CP: car.CarParams) -> bool:
  if not params.get_bool("OnroadUploads"):
    return only_offroad(started, params, CP)

  return always_run(started, params, CP)

def or_(*fns):
  # reduce, not operator.or_(*...): a predicate list with three conditions
  # (livestream, notcar, cplink_video) would raise "or_ expected 2 arguments,
  # got 3" inside ensure_running() and take the whole manager down at startup.
  return lambda *args: reduce(operator.or_, (fn(*args) for fn in fns), False)

def and_(*fns):
  return lambda *args: reduce(operator.and_, (fn(*args) for fn in fns), True)

procs = [
  DaemonProcess("manage_athenad", "openpilot.system.athena.manage_athenad", "AthenadPid"),

  NativeProcess("loggerd", "openpilot/system/loggerd", ["./loggerd"], logging),
  NativeProcess("encoderd", "openpilot/system/loggerd", ["./encoderd"], encoderd_enabled),
  NativeProcess("stream_encoderd", "openpilot/system/loggerd", ["./encoderd", "--stream"], or_(livestream, notcar, cplink_video)),
  PythonProcess("logmessaged", "openpilot.system.logmessaged", always_run),

  NativeProcess("camerad", "openpilot/system/camerad", ["./camerad"], or_(driverview, livestream), enabled=not WEBCAM),
  PythonProcess("webcamerad", "openpilot.system.camerad.webcam.camerad", webcam, enabled=WEBCAM),
  PythonProcess("proclogd", "openpilot.system.proclogd", only_onroad, enabled=platform.system() != "Darwin"),
  PythonProcess("journald", "openpilot.system.journald", only_onroad, platform.system() != "Darwin"),
  # Desktop runs generally have no ALSA capture device; micd would restart
  # continuously and obscure the vehicle/model diagnostics.
  PythonProcess("micd", "openpilot.system.micd", iscar, enabled=not PC),
  PythonProcess("timed", "openpilot.system.timed", always_run, enabled=not PC),

  PythonProcess("modeld", "openpilot.selfdrive.modeld.modeld", and_(only_onroad, is_stock_model),
                restart_limit=(3, 300.0) if PC and os.getenv("MODELD_BACKEND", "").lower() in ("migraphx", "mxr", "migraphx-remote", "mgxd") else None),
  PythonProcess("dmonitoringmodeld", "openpilot.selfdrive.modeld.dmonitoringmodeld", driverview,
                enabled=DRIVER_CAMERA_AVAILABLE),

  PythonProcess("sensord",
                "openpilot.system.sensord.esp32d" if ESP32_IMU else
                "openpilot.system.sensord.sensord",
                only_onroad, enabled=IMU_ENABLED and (not PC or ESP32_IMU)),
  # GPS-only sidecar: only useful when the IMU is disabled, because esp32d owns
  # the same serial port while the IMU is enabled.
  PythonProcess("esp32gpsd", "openpilot.system.sensord.esp32gpsd", esp32_gps,
                enabled=PC and platform.system() == "Linux" and not IMU_ENABLED),
  PythonProcess("ui", "openpilot.selfdrive.ui.ui", ui_enabled,
                restart_backoff=(1.0, 30.0) if PC else None),
  PythonProcess("soundd", "openpilot.selfdrive.ui.soundd", driverview),
  PythonProcess("locationd", "openpilot.selfdrive.locationd.locationd", only_onroad),
  NativeProcess("_pandad", "openpilot/selfdrive/pandad", ["./pandad"], always_run, enabled=False),
  PythonProcess("calibrationd", "openpilot.selfdrive.locationd.calibrationd", only_onroad),
  PythonProcess("torqued", "openpilot.selfdrive.locationd.torqued", only_onroad),
  PythonProcess("controlsd", "openpilot.selfdrive.controls.controlsd", and_(not_joystick, iscar)),
  PythonProcess("joystickd", "openpilot.tools.joystick.joystickd", or_(joystick, notcar)),
  PythonProcess("selfdrived", "openpilot.selfdrive.selfdrived.selfdrived", not_simulated),
  PythonProcess("card", "openpilot.selfdrive.car.card", not_simulated),
  PythonProcess("deleter", "openpilot.system.loggerd.deleter", always_run),
  PythonProcess("driverecordd", "openpilot.system.loggerd.driverecordd", always_run, enabled=PC),
  PythonProcess("dmonitoringd", "openpilot.selfdrive.monitoring.dmonitoringd", driverview,
                enabled=DRIVER_CAMERA_AVAILABLE),
  PythonProcess("qcomgpsd", "openpilot.system.qcomgpsd.qcomgpsd", qcomgps, enabled=COMMA_HARDWARE),
  PythonProcess("pandad", "openpilot.selfdrive.pandad.pandad", always_run),
  PythonProcess("paramsd", "openpilot.selfdrive.locationd.paramsd", only_onroad),
  PythonProcess("lagd", "openpilot.selfdrive.locationd.lagd", only_onroad),
  PythonProcess("ubloxd", "openpilot.system.ubloxd.ubloxd", ublox, enabled=COMMA_HARDWARE),
  PythonProcess("pigeond", "openpilot.system.ubloxd.pigeond", ublox, enabled=COMMA_HARDWARE),
  PythonProcess("plannerd", "openpilot.selfdrive.controls.plannerd", not_long_maneuver),
  PythonProcess("maneuversd", "openpilot.tools.longitudinal_maneuvers.maneuversd", long_maneuver),
  PythonProcess("lateral_maneuversd", "openpilot.tools.lateral_maneuvers.lateral_maneuversd", lat_maneuver),
  PythonProcess("radard", "openpilot.selfdrive.controls.radard", only_onroad),
  PythonProcess("hardwared", "openpilot.system.hardware.hardwared", always_run),
  PythonProcess("modem", "openpilot.common.hardware.comma.modem", always_run, enabled=COMMA_HARDWARE),
  PythonProcess("tombstoned", "openpilot.system.tombstoned", always_run, enabled=not PC),
  PythonProcess("updated", "openpilot.system.updated.updated", only_offroad, enabled=not PC),
  PythonProcess("uploader", "openpilot.system.loggerd.uploader", uploader_ready),
  PythonProcess("statsd", "openpilot.sunnypilot.system.statsd", always_run),

  # debug procs
  NativeProcess("bridge", "openpilot/cereal/messaging", ["./bridge"], notcar),
  PythonProcess("webrtcd", "openpilot.system.webrtc.webrtcd", or_(livestream, notcar, cplink_video)),
  PythonProcess("joystick", "openpilot.tools.joystick.joystick_control", and_(joystick, iscar)),

  # sunnylink <3
  DaemonProcess("manage_sunnylinkd", "openpilot.sunnypilot.sunnylink.athena.manage_sunnylinkd", "SunnylinkdPid"),
  PythonProcess("sunnylink_registration_manager", "openpilot.sunnypilot.sunnylink.registration_manager", sunnylink_need_register_shim),
  PythonProcess("statsd_sp", "openpilot.sunnypilot.sunnylink.statsd", and_(always_run, sunnylink_ready_shim)),
]

# sunnypilot
procs += [
  # Models
  PythonProcess("models_manager", "openpilot.sunnypilot.models.manager", only_offroad),
  NativeProcess("modeld_tinygrad", "openpilot/sunnypilot/modeld_v2", ["./modeld"], and_(only_onroad, is_tinygrad_model)),

  # Optional YOLO object detection. Runs next to modeld on the CPU at 10 Hz; it
  # publishes a small message and never feeds any actuator.
  PythonProcess("yolod", "openpilot.sunnypilot.yolod.yolod", yolo_enabled),
  PythonProcess("laned", "openpilot.sunnypilot.laned.laned", lane_line_detection_enabled),

  # Backup
  PythonProcess("backup_manager", "openpilot.sunnypilot.sunnylink.backups.manager", and_(only_offroad, sunnylink_ready_shim)),

  # navigation feeding the nav driving model (no-op without a route)
  PythonProcess("navd", "openpilot.selfdrive.navigation.navd", only_onroad),
  PythonProcess("mapsd", "openpilot.selfdrive.navigation.mapsd", only_onroad),

  # mapd
  PythonProcess("mapd", "openpilot.sunnypilot.mapd.runtime", mapd_ready, enabled=MAPD_SUPPORTED),
  PythonProcess("mapd_manager", "openpilot.sunnypilot.mapd.mapd_manager", always_run, enabled=MAPD_SUPPORTED),

  # locationd
  NativeProcess("locationd_llk", "openpilot/sunnypilot/selfdrive/locationd", ["./locationd"], only_onroad),
]

if os.path.exists("../../sunnypilot/sunnylink/uploader.py"):
  procs += [PythonProcess("sunnylink_uploader", "openpilot.sunnypilot.sunnylink.uploader", use_sunnylink_uploader_shim)]

if os.path.exists("../../third_party/copyparty/copyparty-sfx.py"):
  sunnypilot_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
  copyparty_args = [f"-v{Paths.crash_log_root()}:/swaglogs:r"]
  copyparty_args += [f"-v{Paths.log_root()}:/routes:r"]
  copyparty_args += [f"-v{Paths.model_root()}:/models:rw"]
  copyparty_args += [f"-v{sunnypilot_root}:/sunnypilot:rw"]
  copyparty_args += ["-p8080"]
  copyparty_args += ["-z"]
  copyparty_args += ["-q"]
  procs += [NativeProcess("copyparty-sfx", "openpilot/third_party/copyparty", ["./copyparty-sfx.py", *copyparty_args], and_(only_offroad, use_copyparty))]

managed_processes = {p.name: p for p in procs}
