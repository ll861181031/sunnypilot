"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
from enum import Enum

from openpilot.cereal import messaging, log, custom
from opendbc.car.structs import car
from openpilot.common.params import Params
from openpilot.selfdrive.ui.sunnypilot.layouts.settings.display import OnroadBrightness
from openpilot.sunnypilot.models.helpers import ACTIVE_BUNDLE_KEYS, get_active_source
from openpilot.sunnypilot.sunnylink.sunnylink_state import SunnylinkState
from openpilot.system.ui.lib.application import gui_app
from openpilot.system.ui.sunnypilot.widgets.screen_saver import ScreenSaverSP

OpenpilotState = log.SelfdriveState.OpenpilotState
MADSState = custom.ModularAssistiveDrivingSystem.ModularAssistiveDrivingSystemState

ONROAD_BRIGHTNESS_TIMER_PAUSED = -1

# Runners that actually load a Blendful-enabled modeld. This fork wires Blendful into
# both the stock runner (openpilot/selfdrive/modeld) and the tinygrad runner
# (sunnypilot/modeld_v2), unlike upstream where only tinygrad has it.
BLENDFUL_RUNNERS = (custom.ModelManagerSP.Runner.stock, custom.ModelManagerSP.Runner.tinygrad)


class OnroadTimerStatus(Enum):
  NONE = 0
  PAUSE = 1
  RESUME = 2


class UIStateSP:
  def __init__(self):
    self.params = Params()
    self.CP_SP: custom.CarParamsSP | None = None
    self.has_icbm: bool = False
    self.is_sp_release: bool = self.params.get_bool("IsReleaseSpBranch")
    self.sm_services_ext = [
      "modelManagerSP", "selfdriveStateSP", "longitudinalPlanSP", "backupManagerSP",
      "gpsLocation", "lateralTorqueParameters", "carStateSP", "liveMapDataSP", "carParamsSP", "lateralDelay",
      # modeld publishes the Blendful telemetry here; the onroad renderer draws the
      # lane-centre overlay from it. Subscribing to a service nobody publishes is
      # harmless (the message just never arrives).
      "modelDataV2SP",
    ]

    self.sunnylink_state = SunnylinkState()

    self.screensaver = ScreenSaverSP(params=self.params)
    self.screensaver_enabled: bool = False

    self.active_bundle = None
    self.model_runner_tinygrad: bool = False
    # Which runner the manager actually launches (process_config.is_tinygrad_model).
    # Distinct from model_runner_tinygrad, which only looks at the selected bundle.
    self.active_runner: int = custom.ModelManagerSP.Runner.stock
    self.active_runner_tinygrad: bool = False
    self.blindspot: bool = False
    self.chevron_metrics = None
    self.custom_interactive_timeout: int = 0
    self.developer_ui = None
    self.hide_v_ego_ui: bool = False
    self.onroad_brightness: int = 0
    self.onroad_brightness_timer: int = 0
    self.onroad_brightness_timer_param: int = 0
    self.rainbow_path: bool = False
    self.road_name_toggle: bool = False
    self.rocket_fuel: bool = False
    self.speed_limit_mode = None
    self.standstill_timer: bool = False
    self.sunnylink_enabled: bool = False
    self.torque_bar: bool = False
    self.enforce_torque_control: bool = False
    self.custom_torque_params: bool = False
    self.torque_override_enabled: bool = False
    self._sp_initialized: bool = False

  def update(self) -> None:
    if self.sunnylink_enabled:
      self.sunnylink_state.start()
    else:
      self.sunnylink_state.stop()

  def onroad_brightness_handle_alerts(self, _ui_state, alert):
    if _ui_state.sm.recv_frame["carState"] < _ui_state.started_frame:
      return

    has_alert = _ui_state.started and self.onroad_brightness != OnroadBrightness.AUTO and alert is not None

    self.update_onroad_brightness(has_alert)
    if has_alert:
      self.reset_onroad_sleep_timer()

  def update_onroad_brightness(self, has_alert: bool) -> None:
    if has_alert:
      return

    if self.onroad_brightness_timer > 0:
      self.onroad_brightness_timer -= 1

  def reset_onroad_sleep_timer(self, timer_status: OnroadTimerStatus = OnroadTimerStatus.NONE) -> None:
    # Toggling from active state to inactive
    if timer_status == OnroadTimerStatus.PAUSE and self.onroad_brightness_timer != ONROAD_BRIGHTNESS_TIMER_PAUSED:
      self.onroad_brightness_timer = ONROAD_BRIGHTNESS_TIMER_PAUSED
    # Toggling from a previously inactive state or resetting an active timer
    elif (self.onroad_brightness_timer_param >= 0 and self.onroad_brightness != OnroadBrightness.AUTO and
          self.onroad_brightness_timer != ONROAD_BRIGHTNESS_TIMER_PAUSED) or timer_status == OnroadTimerStatus.RESUME:
      if self.onroad_brightness == OnroadBrightness.AUTO_DARK:
        self.onroad_brightness_timer = 15 * gui_app.target_fps
      else:
        self.onroad_brightness_timer = self.onroad_brightness_timer_param * gui_app.target_fps

  @property
  def onroad_brightness_timer_expired(self) -> bool:
    return self.onroad_brightness != OnroadBrightness.AUTO and self.onroad_brightness_timer == 0

  @property
  def auto_onroad_brightness(self) -> bool:
    return self.onroad_brightness in (OnroadBrightness.AUTO, OnroadBrightness.AUTO_DARK)

  @staticmethod
  def lateral_active(_ui_state) -> bool:
    """Lateral is doing the steering right now, so a lateral-only overlay applies."""
    if _ui_state.sm.valid["selfdriveStateSP"]:
      mads = _ui_state.sm["selfdriveStateSP"].mads
      if mads.available:
        return mads.enabled and mads.state != MADSState.paused
    return _ui_state.status.value in ("engaged", "lat_only")

  def blendful_supported(self) -> bool:
    """Whether the runner the manager launched has Blendful wired into it.

    Upstream gates Blendful on `active_runner_tinygrad` because Blendful only exists in
    modeld_v2 there. This fork wires the same Blendful.update() into both
    openpilot.selfdrive.modeld.modeld (stock runner) and sunnypilot/modeld_v2/modeld
    (tinygrad runner), so gating on tinygrad alone would grey the toggle out on a car
    running the stock runner -- which is the normal case here (ModelRunnerTypeCache
    reads 2 == Runner.stock on this machine). Only snpe, where neither modeld process
    runs, genuinely cannot use it.
    """
    return self.active_runner in BLENDFUL_RUNNERS

  @staticmethod
  def update_status(ss, ss_sp, onroad_evt) -> str:
    state = ss.state
    mads = ss_sp.mads
    mads_state = mads.state

    if state == OpenpilotState.preEnabled:
      return "override"

    if state == OpenpilotState.overriding:
      if not mads.available:
        return "override"

      if any(e.overrideLongitudinal for e in onroad_evt):
        return "override"

    if mads_state in (MADSState.paused, MADSState.overriding):
      return "override"

    # MADS specific statuses
    if not mads.available:
      return "engaged" if ss.enabled else "disengaged"

    if not mads.enabled and not ss.enabled:
      return "disengaged"

    if mads.enabled and ss.enabled:
      return "engaged"

    if mads.enabled:
      return "lat_only"

    if ss.enabled:
      return "long_only"

    return "disengaged"

  def update_params(self) -> None:
    CP_SP_bytes = self.params.get("CarParamsSPPersistent")
    if CP_SP_bytes is not None:
      self.CP_SP = messaging.log_from_bytes(CP_SP_bytes, custom.CarParamsSP)
      self.has_icbm = self.CP_SP.intelligentCruiseButtonManagementAvailable and self.params.get_bool("IntelligentCruiseButtonManagement")

    self._enforce_constraints()
    source = get_active_source(chestnut=self.chestnut_present, chestnut_active=self.chestnut_active,
                               chestnut_loading=self.chestnut_loading, offroad=self.is_offroad())
    self.active_bundle = self.params.get(ACTIVE_BUNDLE_KEYS[source])
    self.model_runner_tinygrad = self.active_bundle is not None and self.active_bundle.get("runner") == "tinygrad"
    # Runner the manager actually launches (process_config.is_tinygrad_model / is_stock_model).
    # With a chestnut slot present the big slot decides, so this is not derivable from the
    # selected bundle alone. Upstream only keeps the tinygrad boolean; the raw value is kept
    # here too because this fork wires Blendful into the stock runner as well, and the
    # settings page needs to know which runner is live rather than just "is it tinygrad".
    runner = self.params.get("ModelRunnerTypeCache")
    self.active_runner = int(custom.ModelManagerSP.Runner.stock if runner is None else runner)
    self.active_runner_tinygrad = self.active_runner == custom.ModelManagerSP.Runner.tinygrad
    # stock only counts the default big model's compiled pkl. a downloaded big bundle runs on the
    # chestnut just the same, so ChestnutState has to see it as available too.
    self.chestnut_compiled = self.chestnut_compiled or self.model_runner_tinygrad
    self.blindspot = self.params.get_bool("BlindSpot")
    self.chevron_metrics = self.params.get("ChevronInfo")
    self.custom_interactive_timeout = self.params.get("InteractivityTimeout", return_default=True)
    self.developer_ui = self.params.get("DevUIInfo")
    self.hide_v_ego_ui = self.params.get_bool("HideVEgoUI")
    self.onroad_brightness = int(float(self.params.get("OnroadScreenOffBrightness", return_default=True)))
    self.onroad_brightness_timer_param = self.params.get("OnroadScreenOffTimer", return_default=True)
    self.rainbow_path = self.params.get_bool("RainbowMode")
    self.road_name_toggle = self.params.get_bool("RoadNameToggle")
    self.rocket_fuel = self.params.get_bool("RocketFuel")
    self.speed_limit_mode = self.params.get("SpeedLimitMode", return_default=True)
    self.standstill_timer = self.params.get_bool("StandstillTimer")
    self.sunnylink_enabled = self.params.get_bool("SunnylinkEnabled")
    self.torque_bar = self.params.get_bool("TorqueBar")
    self.enforce_torque_control = self.params.get_bool("EnforceTorqueControl")
    self.custom_torque_params = self.params.get_bool("CustomTorqueParams")
    self.torque_override_enabled = self.params.get_bool("TorqueParamsOverrideEnabled")
    self.torque_override_lat_accel_factor = float(self.params.get("TorqueParamsOverrideLatAccelFactor", return_default=True))
    self.torque_override_friction = float(self.params.get("TorqueParamsOverrideFriction", return_default=True))
    self.true_v_ego_ui = self.params.get_bool("TrueVEgoUI")
    self.turn_signals = self.params.get_bool("ShowTurnSignals")
    self.boot_offroad_mode = self.params.get("DeviceBootMode", return_default=True)
    self.always_offroad = self.params.get_bool("OffroadMode")
    self.screensaver_enabled = self.params.get_bool("ScreenSaverEnabled")

    if not self._sp_initialized:
      self._sp_initialized = True
      self.reset_onroad_sleep_timer()

  def _enforce_constraints(self) -> None:
    has_long = self.has_longitudinal_control
    CP = self.CP

    # 启动时尚无车型参数，不代表车型不支持；保留驾驶员上次的选择。
    if self.is_release or (CP is not None and CP.brand != "mock" and not CP.alphaLongitudinalAvailable):
      self.params.remove("AlphaLongitudinalEnabled")

    if CP is not None and CP.brand == "mock":
      self.has_icbm = False
      return

    if CP is not None:
      if self.params.get_bool("EnforceTorqueControl") and self.params.get_bool("NeuralNetworkLateralControl"):
        self.params.put_bool("EnforceTorqueControl", False, block=True)
        self.params.put_bool("NeuralNetworkLateralControl", False, block=True)

      if self.params.get_bool("LateralJerkTorqueController") and self.params.get_bool("NeuralNetworkLateralControl"):
        self.params.put_bool("LateralJerkTorqueController", False, block=True)
        self.params.put_bool("NeuralNetworkLateralControl", False, block=True)

      # Angle steering: no torque-based lateral controls
      if CP.steerControlType == car.CarParams.SteerControlType.angle:
        self.params.remove("EnforceTorqueControl")
        self.params.remove("NeuralNetworkLateralControl")
        self.params.remove("LateralJerkTorqueController")

      # BSM not available: clear BSM-dependent settings
      if not CP.enableBsm:
        self.params.remove("AutoLaneChangeBsmDelay")

    # No longitudinal control: no experimental mode or DEC
    if CP is not None and not has_long:
      self.params.remove("ExperimentalMode")
      self.params.remove("DynamicExperimentalControl")

    # ICBM: clear if not available or if full longitudinal control is active
    if has_long or (self.CP_SP is not None and not self.CP_SP.intelligentCruiseButtonManagementAvailable):
      self.params.remove("IntelligentCruiseButtonManagement")
      self.has_icbm = False
    elif self.CP_SP is None or CP is None:
      self.has_icbm = False

    # Cruise features requiring longitudinal or ICBM
    if CP is not None and self.CP_SP is not None and not (has_long or self.has_icbm):
      self.params.remove("CustomAccIncrementsEnabled")
      self.params.remove("SmartCruiseControlVision")
      self.params.remove("SmartCruiseControlMap")


class DeviceSP:
  def __init__(self):
    self._blocked_by_screensaver: bool = False

  def _set_awake(self, on: bool, _ui_state=None):
    self._blocked_by_screensaver = False

    if not on and _ui_state.screensaver_enabled:
      if _ui_state.screensaver.was_dismissed:
        self.dismiss_screensaver(_ui_state)
      elif _ui_state.screensaver.is_active:
        self._blocked_by_screensaver = True
      else:
        _ui_state.screensaver.initialize()
        gui_app.push_widget(_ui_state.screensaver)
        self._blocked_by_screensaver = True
    else:
      self.dismiss_screensaver(_ui_state)

    # blocked runs every frame, so write only when actually sleeping
    if _ui_state.boot_offroad_mode == 1 and not on and not self._blocked_by_screensaver:
      _ui_state.params.put_bool("OffroadMode", True)

  def dismiss_screensaver(self, _ui_state) -> None:
    if gui_app.get_active_widget() == _ui_state.screensaver:
      gui_app.pop_widget()
    self._blocked_by_screensaver = False

  @staticmethod
  def set_onroad_brightness(_ui_state, awake: bool, cur_brightness: float) -> float:
    if not awake or not _ui_state.started:
      return cur_brightness

    if _ui_state.onroad_brightness_timer != 0:
      if _ui_state.onroad_brightness == OnroadBrightness.AUTO_DARK:
        return max(30.0, cur_brightness)
      return cur_brightness

    # 0: Auto (Default), 1: Auto (Dark), 2: Screen Off
    if _ui_state.onroad_brightness == OnroadBrightness.AUTO:
      return cur_brightness
    if _ui_state.onroad_brightness == OnroadBrightness.AUTO_DARK:
      return cur_brightness
    if _ui_state.onroad_brightness == OnroadBrightness.SCREEN_OFF:
      return 0.0

    # 3-22: 5% - 100%
    return float((_ui_state.onroad_brightness - 2) * 5)

  @staticmethod
  def set_min_onroad_brightness(_ui_state, min_brightness: int) -> int:
    if _ui_state.onroad_brightness == OnroadBrightness.AUTO_DARK:
      min_brightness = 10

    return min_brightness

  @staticmethod
  def wake_from_dimmed_onroad_brightness(_ui_state, evs) -> None:
    if _ui_state.started and (_ui_state.onroad_brightness_timer_expired or _ui_state.onroad_brightness == OnroadBrightness.AUTO_DARK):
      if any(ev.left_down for ev in evs):
        if _ui_state.onroad_brightness_timer_expired:
          gui_app.mouse_events.clear()
        _ui_state.reset_onroad_sleep_timer()
