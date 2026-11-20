#pragma once

#include <string>
#include <unordered_map>

#include "openpilot/cereal/gen/cpp/log.capnp.h"

inline static std::unordered_map<std::string, ParamKeyAttributes> keys = {
    {"AccessToken", {CLEAR_ON_MANAGER_START | DONT_LOG, STRING}},
    {"AdbEnabled", {PERSISTENT | BACKUP, BOOL}},
    {"AlwaysOnDM", {PERSISTENT | BACKUP, BOOL}},
    {"ApiCache_Device", {PERSISTENT, STRING}},
    {"ApiCache_FirehoseStats", {PERSISTENT, JSON}},
    {"AssistNowToken", {PERSISTENT, STRING}},
    {"AthenadPid", {PERSISTENT, INT}},
    {"AthenadUploadQueue", {PERSISTENT, JSON}},
    {"AthenadRecentlyViewedRoutes", {PERSISTENT, STRING}},
    {"BootCount", {PERSISTENT, INT}},
    {"CalibrationParams", {PERSISTENT, BYTES}},
    {"CameraDebugExpGain", {CLEAR_ON_MANAGER_START, STRING}},
    {"CameraDebugExpTime", {CLEAR_ON_MANAGER_START, STRING}},
    {"CarBatteryCapacity", {PERSISTENT, INT}},
    {"CarParams", {CLEAR_ON_MANAGER_START | CLEAR_ON_ONROAD_TRANSITION, BYTES}},
    {"CarParamsCache", {CLEAR_ON_MANAGER_START, BYTES}},
    {"CarParamsPersistent", {PERSISTENT, BYTES}},
    {"CarParamsPrevRoute", {PERSISTENT, BYTES}},
    {"CompletedTrainingVersion", {PERSISTENT, STRING, "0"}},
    {"ControlsReady", {CLEAR_ON_MANAGER_START | CLEAR_ON_ONROAD_TRANSITION, BOOL}},
    {"CurrentBootlog", {PERSISTENT, STRING}},
    {"CurrentRoute", {CLEAR_ON_MANAGER_START | CLEAR_ON_ONROAD_TRANSITION, STRING}},
    {"DisableLogging", {CLEAR_ON_MANAGER_START | CLEAR_ON_ONROAD_TRANSITION, BOOL}},
    {"DisableEncoderd", {PERSISTENT, BOOL, "0"}},
    {"SimulateIgnition", {PERSISTENT, BOOL, "0"}},
    {"SimulateNav", {PERSISTENT, BOOL, "0"}},
    {"DisableUI", {PERSISTENT, BOOL, "0"}},
    {"DisablePowerDown", {PERSISTENT | BACKUP, BOOL}},
    {"DisableUpdates", {PERSISTENT | BACKUP, BOOL, "0"}},
    {"DisengageOnAccelerator", {PERSISTENT | BACKUP, BOOL, "0"}},
    {"DongleId", {PERSISTENT, STRING}},
    {"DoReboot", {CLEAR_ON_MANAGER_START, BOOL}},
    {"DoShutdown", {CLEAR_ON_MANAGER_START, BOOL}},
    {"DoUninstall", {CLEAR_ON_MANAGER_START, BOOL}},
    {"DriverTooDistracted", {CLEAR_ON_MANAGER_START | CLEAR_ON_IGNITION_ON, BOOL}},
    {"DriverLockoutCount", {CLEAR_ON_MANAGER_START | CLEAR_ON_IGNITION_ON, INT, "0"}},
    {"AlphaLongitudinalEnabled", {PERSISTENT | DEVELOPMENT_ONLY | BACKUP, BOOL}},
    {"ExperimentalMode", {PERSISTENT | BACKUP, BOOL}},
    {"ExperimentalModeConfirmed", {PERSISTENT | BACKUP, BOOL}},
    {"FirmwareQueryDone", {CLEAR_ON_MANAGER_START | CLEAR_ON_ONROAD_TRANSITION, BOOL}},
    {"ForcePowerDown", {PERSISTENT, BOOL}},
    {"GitBranch", {PERSISTENT, STRING}},
    {"GitCommit", {PERSISTENT, STRING}},
    {"GitCommitDate", {PERSISTENT, STRING}},
    {"GitDiff", {PERSISTENT, STRING}},
    {"GithubSshKeys", {PERSISTENT | BACKUP, STRING}},
    {"GithubUsername", {PERSISTENT | BACKUP, STRING}},
    {"GitRemote", {PERSISTENT, STRING}},
    {"GsmApn", {PERSISTENT | BACKUP, STRING}},
    {"GsmMetered", {PERSISTENT | BACKUP, BOOL, "1"}},
    {"GsmRoaming", {PERSISTENT | BACKUP, BOOL}},
    {"HardwareSerial", {PERSISTENT, STRING}},
    {"HasAcceptedTerms", {PERSISTENT, STRING, "0"}},
    {"InstallDate", {PERSISTENT, TIME}},
    {"IsDriverViewEnabled", {CLEAR_ON_MANAGER_START, BOOL}},
    {"IsEngaged", {PERSISTENT, BOOL}},
    {"IsLdwEnabled", {PERSISTENT | BACKUP, BOOL}},
    {"IsLiveStreaming", {CLEAR_ON_MANAGER_START | CLEAR_ON_IGNITION_ON, BOOL}},
    {"IsMetric", {PERSISTENT | BACKUP, BOOL}},
    {"IsOffroad", {CLEAR_ON_MANAGER_START, BOOL}},
    {"IsRhdDetected", {PERSISTENT, BOOL}},
    {"IsReleaseBranch", {CLEAR_ON_MANAGER_START, BOOL}},
    {"IsTestedBranch", {CLEAR_ON_MANAGER_START, BOOL}},
    {"JoystickDebugMode", {CLEAR_ON_MANAGER_START | CLEAR_ON_OFFROAD_TRANSITION, BOOL}},
    {"LanguageSetting", {PERSISTENT | BACKUP, STRING, "en"}},
    {"LastAthenaPingTime", {CLEAR_ON_MANAGER_START, INT}},
    {"LastGPSPosition", {PERSISTENT, STRING}},
    {"LastManagerExitReason", {CLEAR_ON_MANAGER_START, STRING}},
    {"LastOffroadStatusPacket", {CLEAR_ON_MANAGER_START | CLEAR_ON_OFFROAD_TRANSITION, JSON}},
    {"LastAgnosPowerMonitorShutdown", {CLEAR_ON_MANAGER_START, STRING}},
    {"LastPowerDropDetected", {CLEAR_ON_MANAGER_START, STRING}},
    {"LastUpdateException", {CLEAR_ON_MANAGER_START, STRING}},
    {"LastUpdateRouteCount", {PERSISTENT, INT, "0"}},
    {"LastUpdateTime", {PERSISTENT, TIME}},
    {"LastUpdateUptimeOnroad", {PERSISTENT, FLOAT, "0.0"}},
    {"LiveDelay", {PERSISTENT | BACKUP, BYTES}},
    {"LiveParameters", {PERSISTENT, JSON}},
    {"LiveParametersV2", {PERSISTENT, BYTES}},
    {"LivestreamEncoderBitrate", {CLEAR_ON_MANAGER_START | DONT_LOG, INT}},
    {"LivestreamRequestKeyframe", {CLEAR_ON_MANAGER_START | DONT_LOG, BOOL}},
    {"LiveTorqueParameters", {PERSISTENT | DONT_LOG, BYTES}},
    {"LocationFilterInitialState", {PERSISTENT, BYTES}},
    {"LateralManeuverMode", {CLEAR_ON_MANAGER_START | CLEAR_ON_OFFROAD_TRANSITION, BOOL}},
    {"LongitudinalManeuverMode", {CLEAR_ON_MANAGER_START | CLEAR_ON_OFFROAD_TRANSITION, BOOL}},
    {"LongitudinalPersonality", {PERSISTENT | BACKUP, INT, std::to_string(static_cast<int>(cereal::LongitudinalPersonality::STANDARD))}},
    {"NetworkMetered", {PERSISTENT | BACKUP, BOOL}},
    {"ObdMultiplexingChanged", {CLEAR_ON_MANAGER_START | CLEAR_ON_ONROAD_TRANSITION, BOOL}},
    {"ObdMultiplexingEnabled", {CLEAR_ON_MANAGER_START | CLEAR_ON_ONROAD_TRANSITION, BOOL}},
    {"Offroad_CarUnrecognized", {CLEAR_ON_MANAGER_START | CLEAR_ON_ONROAD_TRANSITION, JSON}},
    {"Offroad_ChestnutBranch", {CLEAR_ON_MANAGER_START, JSON}},
    {"Offroad_ChestnutNotDetected", {CLEAR_ON_MANAGER_START | CLEAR_ON_ONROAD_TRANSITION, JSON}},
    {"Offroad_ChestnutOverheated", {CLEAR_ON_MANAGER_START, JSON}},
    {"Offroad_ChestnutPcieUnavailable", {CLEAR_ON_MANAGER_START, JSON}},
    {"Offroad_ChestnutUncompiled", {CLEAR_ON_MANAGER_START | CLEAR_ON_ONROAD_TRANSITION, JSON}},
    {"Offroad_ChestnutUpdateFailed", {CLEAR_ON_MANAGER_START | CLEAR_ON_ONROAD_TRANSITION, JSON}},
    {"Offroad_ChestnutUsbSlow", {CLEAR_ON_MANAGER_START | CLEAR_ON_ONROAD_TRANSITION, JSON}},
    {"Offroad_ConnectivityNeeded", {CLEAR_ON_MANAGER_START, JSON}},
    {"Offroad_ConnectivityNeededPrompt", {CLEAR_ON_MANAGER_START, JSON}},
    {"Offroad_ExcessiveActuation", {PERSISTENT, JSON}},
    {"Offroad_NoFirmware", {CLEAR_ON_MANAGER_START | CLEAR_ON_ONROAD_TRANSITION, JSON}},
    {"Offroad_Recalibration", {CLEAR_ON_MANAGER_START | CLEAR_ON_ONROAD_TRANSITION, JSON}},
    {"Offroad_TemperatureTooHigh", {CLEAR_ON_MANAGER_START, JSON}},
    {"Offroad_UnregisteredHardware", {CLEAR_ON_MANAGER_START, JSON}},
    {"Offroad_UpdateFailed", {CLEAR_ON_MANAGER_START, JSON}},
    {"Offroad_DriverMonitoringUncertain", {CLEAR_ON_MANAGER_START | CLEAR_ON_ONROAD_TRANSITION, JSON}},
    {"OnroadCycleRequested", {CLEAR_ON_MANAGER_START, BOOL}},
    {"OpenpilotEnabledToggle", {PERSISTENT | BACKUP, BOOL, "1"}},
    {"PandaHeartbeatLost", {CLEAR_ON_MANAGER_START | CLEAR_ON_OFFROAD_TRANSITION, BOOL}},
    {"PrimeType", {PERSISTENT, INT}},
    {"RecordAudio", {PERSISTENT | BACKUP, BOOL}},
    {"RecordFront", {PERSISTENT | BACKUP, BOOL}},
    {"RecordFrontLock", {PERSISTENT, BOOL}},  // for the internal fleet
    {"SecOCKey", {PERSISTENT | DONT_LOG | BACKUP, STRING}},
    {"ShowDebugInfo", {PERSISTENT, BOOL}},
    {"RouteCount", {PERSISTENT, INT, "0"}},
    {"SnoozeUpdate", {CLEAR_ON_MANAGER_START | CLEAR_ON_OFFROAD_TRANSITION, BOOL}},
    {"SshEnabled", {PERSISTENT | BACKUP, BOOL}},
    {"TermsVersion", {PERSISTENT, STRING}},
    {"TorqueBar", {PERSISTENT | BACKUP, BOOL, "0"}},
    {"TrainingVersion", {PERSISTENT, STRING}},
    {"UbloxAvailable", {PERSISTENT, BOOL}},
    {"UpdateAvailable", {CLEAR_ON_MANAGER_START | CLEAR_ON_ONROAD_TRANSITION, BOOL}},
    {"UpdateFailedCount", {CLEAR_ON_MANAGER_START, INT}},
    {"UpdaterAvailableBranches", {PERSISTENT, STRING}},
    {"UpdaterCurrentDescription", {CLEAR_ON_MANAGER_START, STRING}},
    {"UpdaterCurrentReleaseNotes", {CLEAR_ON_MANAGER_START, BYTES}},
    {"UpdaterFetchAvailable", {CLEAR_ON_MANAGER_START, BOOL}},
    {"UpdaterNewDescription", {CLEAR_ON_MANAGER_START, STRING}},
    {"UpdaterNewReleaseNotes", {CLEAR_ON_MANAGER_START, BYTES}},
    {"UpdaterState", {CLEAR_ON_MANAGER_START, STRING}},
    {"UpdaterTargetBranch", {CLEAR_ON_MANAGER_START, STRING}},
    {"UpdaterLastFetchTime", {PERSISTENT, TIME}},
    {"UptimeOffroad", {PERSISTENT, FLOAT, "0.0"}},
    {"UptimeOnroad", {PERSISTENT, FLOAT, "0.0"}},
    {"ChestnutActive", {CLEAR_ON_MANAGER_START | CLEAR_ON_OFFROAD_TRANSITION | CLEAR_ON_IGNITION_ON, BOOL}},
    {"ChestnutLoading", {CLEAR_ON_MANAGER_START | CLEAR_ON_OFFROAD_TRANSITION | CLEAR_ON_IGNITION_ON, BOOL}},
    {"ChestnutModelError", {CLEAR_ON_MANAGER_START | CLEAR_ON_OFFROAD_TRANSITION | CLEAR_ON_IGNITION_ON, BOOL}},
    // Written by modeld, read by the onroad UI to show that the wide camera is the
    // model's second input right now. Runtime state, not a setting: it must not
    // survive a restart, or the indicator would claim a camera that is not running.
    {"WideCameraActive", {CLEAR_ON_MANAGER_START | CLEAR_ON_OFFROAD_TRANSITION | CLEAR_ON_IGNITION_ON, BOOL}},
    {"Version", {PERSISTENT, STRING}},

    // --- sunnypilot params --- //
    // Predictive acceleration boost. Default on, matching upstream: the planner falls
    // back to the gas-pedal-only AccelBoost when this is off or longitudinal is stock.
    {"AccelBoostSP", {PERSISTENT | BACKUP, BOOL, "1"}},
    {"ApiCache_DriveStats", {PERSISTENT, JSON}},
    {"LocalDriveStats", {PERSISTENT, JSON}},
    {"AutoLaneChangeBsmDelay", {PERSISTENT | BACKUP, BOOL, "0"}},
    {"AutoLaneChangeTimer", {PERSISTENT | BACKUP, INT, "0"}},
    {"BlinkerLateralReengageDelay", {PERSISTENT | BACKUP, INT, "0"}},  // seconds
    {"BlinkerMinLateralControlSpeed", {PERSISTENT | BACKUP, INT, "20"}},  // MPH or km/h
    {"BlinkerPauseLateralControl", {PERSISTENT | BACKUP, INT, "0"}},
    {"Brightness", {PERSISTENT | BACKUP, INT, "0"}},
    {"CarList", {PERSISTENT, JSON}},
    {"CarParamsSP", {CLEAR_ON_MANAGER_START | CLEAR_ON_ONROAD_TRANSITION, BYTES}},
    {"CarParamsSPCache", {CLEAR_ON_MANAGER_START, BYTES}},
    {"CarParamsSPPersistent", {PERSISTENT, BYTES}},
    {"CarPlatformBundle", {PERSISTENT | BACKUP, JSON}},
    {"ChevronInfo", {PERSISTENT | BACKUP, INT, "4"}},
    {"CompletedSunnylinkConsentVersion", {PERSISTENT, STRING, "0"}},
    {"CustomAccIncrementsEnabled", {PERSISTENT | BACKUP, BOOL, "0"}},
    {"CustomAccLongPressIncrement", {PERSISTENT | BACKUP, INT, "5"}},
    {"CustomAccShortPressIncrement", {PERSISTENT | BACKUP, INT, "1"}},
    {"DeviceBootMode", {PERSISTENT | BACKUP, INT, "0"}},
    {"DevUIInfo", {PERSISTENT | BACKUP, INT, "0"}},
    {"EnableCopyparty", {PERSISTENT | BACKUP, BOOL}},
    // No consumer in this repository: nothing starts or stops a GitHub runner, and
    // neither the car UI nor the web mirror offers the switch any more (the sunnylink
    // schema entry was removed on 2026-11-04). The key itself is kept so an existing
    // value on a device still parses instead of being reported as unknown; remove it
    // only after a migration that deletes the stored value.
    {"EnableGithubRunner", {PERSISTENT | BACKUP, BOOL}},
    {"GreenLightAlert", {PERSISTENT | BACKUP, BOOL, "0"}},
    {"GithubRunnerSufficientVoltage", {CLEAR_ON_MANAGER_START , BOOL}},
    {"HasAcceptedTermsSP", {PERSISTENT, STRING, "0"}},
    {"HideVEgoUI", {PERSISTENT | BACKUP, BOOL, "0"}},
    {"IntelligentCruiseButtonManagement", {PERSISTENT | BACKUP , BOOL}},
    {"InteractivityTimeout", {PERSISTENT | BACKUP, INT, "0"}},
    {"IsDevelopmentBranch", {CLEAR_ON_MANAGER_START, BOOL}},
    {"IsReleaseSpBranch", {CLEAR_ON_MANAGER_START, BOOL}},
    {"LastGPSPositionLLK", {PERSISTENT, STRING}},
    {"LeadDepartAlert", {PERSISTENT | BACKUP, BOOL, "0"}},
    {"MaxTimeOffroad", {PERSISTENT | BACKUP, INT, "1800"}},
    {"ModelRunnerTypeCache", {CLEAR_ON_ONROAD_TRANSITION, INT}},
    {"OffroadMode", {CLEAR_ON_MANAGER_START, BOOL}},
    {"Offroad_TiciSupport", {CLEAR_ON_MANAGER_START, JSON}},
    {"OnroadScreenOffBrightness", {PERSISTENT | BACKUP, INT, "0"}},
    {"OnroadScreenOffBrightnessMigrated", {PERSISTENT | BACKUP, STRING, "0.0"}},
    {"OnroadScreenOffTimer", {PERSISTENT | BACKUP, INT, "15"}},
    {"OnroadScreenOffTimerMigrated", {PERSISTENT | BACKUP, STRING, "0.0"}},
    {"OnroadUploads", {PERSISTENT | BACKUP, BOOL, "1"}},
    {"QuickBootToggle", {PERSISTENT | BACKUP, BOOL, "0"}},
    {"QuietMode", {PERSISTENT | BACKUP, BOOL, "0"}},
    {"RainbowMode", {PERSISTENT | BACKUP, BOOL, "0"}},
    {"RoadEdgeLaneChangeEnabled", {PERSISTENT | BACKUP, BOOL, "0"}},
    {"RocketFuel", {PERSISTENT | BACKUP, BOOL, "0"}},
    {"ScreenSaverEnabled", {PERSISTENT | BACKUP, BOOL, "1"}},
    {"ScreenSaverTimeout", {PERSISTENT | BACKUP, INT, "300"}},
    {"ShowAdvancedControls", {PERSISTENT | BACKUP, BOOL, "0"}},
    {"ShowTurnSignals", {PERSISTENT | BACKUP, BOOL, "0"}},
    {"StandstillTimer", {PERSISTENT | BACKUP, BOOL, "0"}},
    {"TrueVEgoUI", {PERSISTENT | BACKUP, BOOL, "0"}},

    // MADS params
    {"Mads", {PERSISTENT | BACKUP, BOOL, "1"}},
    {"MadsMainCruiseAllowed", {PERSISTENT | BACKUP, BOOL, "1"}},
    {"MadsSteeringMode", {PERSISTENT | BACKUP, INT, "0"}},
    {"MadsUnifiedEngagementMode", {PERSISTENT | BACKUP, BOOL, "1"}},

    // Model Manager params
    {"ModelManager_ActiveBundle", {PERSISTENT, JSON}},
    {"ModelManager_ActiveBundleUSBGPU", {PERSISTENT, JSON}}, //TODO-SP: kept for migration, remove on next sync?
    {"ModelManager_ActiveBundleChestnut", {PERSISTENT, JSON}},
    {"ModelManager_ActiveJson", {CLEAR_ON_MANAGER_START, JSON}},
    {"ModelManager_ClearCache", {CLEAR_ON_MANAGER_START, BOOL}},
    {"ModelManager_DownloadRef", {CLEAR_ON_MANAGER_START | CLEAR_ON_ONROAD_TRANSITION, STRING}},
    {"ModelManager_Favs", {PERSISTENT | BACKUP, STRING}},
    {"ModelManager_LastSyncTime", {CLEAR_ON_MANAGER_START | CLEAR_ON_OFFROAD_TRANSITION, INT, "0"}},
    {"ModelManager_LastSyncTime_Chestnut", {CLEAR_ON_MANAGER_START | CLEAR_ON_OFFROAD_TRANSITION, INT, "0"}},
    {"ModelManager_ModelsCache", {PERSISTENT | BACKUP, JSON}},
    {"ModelManager_ModelsCache_Chestnut", {PERSISTENT | BACKUP, JSON}},

    // Neural Network Lateral Control
    {"NeuralNetworkLateralControl", {PERSISTENT | BACKUP, BOOL, "0"}},

    // sunnylink params
    {"EnableSunnylinkUploader", {PERSISTENT | BACKUP, BOOL}},
    {"LastSunnylinkPingTime", {CLEAR_ON_MANAGER_START, INT}},
    {"ParamsVersion", {PERSISTENT, INT}},
    {"SunnylinkCache_Roles", {PERSISTENT, STRING}},
    {"SunnylinkCache_Users", {PERSISTENT, STRING}},
    {"SunnylinkDongleId", {PERSISTENT, STRING}},
    {"NavDestination", {PERSISTENT, JSON}},
  // Navigation tab: saved places, recent destinations, and the optional AMap web key.
  {"NavFavorites", {PERSISTENT, JSON}},
  {"NavHistory", {PERSISTENT, JSON}},
  {"NavAmapKey", {PERSISTENT, STRING}},
  // Basemap archive for mapsd: a local .pmtiles path or an https:// URL.
  {"NavTiles", {PERSISTENT, STRING}},
    {"SunnylinkdPid", {PERSISTENT, INT}},
    {"SunnylinkEnabled", {PERSISTENT, BOOL, "1"}},
    {"SunnylinkTempFault", {CLEAR_ON_MANAGER_START | CLEAR_ON_OFFROAD_TRANSITION, BOOL, "0"}},

    // Backup Manager params
    {"BackupManager_CreateBackup", {PERSISTENT, BOOL}},
    {"BackupManager_RestoreVersion", {PERSISTENT, STRING}},

    // sunnypilot car specific params
    {"HyundaiLongitudinalTuning", {PERSISTENT | BACKUP, INT, "0"}},
    {"SubaruStopAndGo", {PERSISTENT | BACKUP, BOOL, "0"}},
    {"SubaruStopAndGoManualParkingBrake", {PERSISTENT | BACKUP, BOOL, "0"}},
    {"TeslaCoopSteering", {PERSISTENT | BACKUP, BOOL, "0"}},
    {"TeslaMadsScreenButton", {PERSISTENT | BACKUP, INT, "0"}},
    {"ToyotaEnforceStockLongitudinal", {PERSISTENT | BACKUP, BOOL, "0"}},
    {"ToyotaStopAndGoHack", {PERSISTENT | BACKUP, BOOL, "0"}},

    {"DynamicExperimentalControl", {PERSISTENT | BACKUP, BOOL, "0"}},
    {"BlindSpot", {PERSISTENT | BACKUP, BOOL, "0"}},

    // sunnypilot model params
    {"CameraOffset", {PERSISTENT | BACKUP, FLOAT, "0.0"}},
    {"LagdToggle", {PERSISTENT | BACKUP, BOOL, "1"}},
    {"LagdToggleDelay", {PERSISTENT | BACKUP, FLOAT, "0.2"}},
    {"LagdValueCache", {PERSISTENT, FLOAT, "0.2"}},
    {"LaneTurnDesire", {PERSISTENT | BACKUP, BOOL, "0"}},
    {"LaneTurnValue", {PERSISTENT | BACKUP, FLOAT, "19.0"}},
    {"PlanplusControl", {PERSISTENT | BACKUP, FLOAT, "1.0"}},

    // mapd
    {"MapAdvisorySpeedLimit", {CLEAR_ON_ONROAD_TRANSITION, FLOAT}},
    {"Mapd_ClearCache", {CLEAR_ON_MANAGER_START, BOOL}},
    {"MapdVersion", {PERSISTENT, STRING}},
    {"MapSpeedLimit", {CLEAR_ON_ONROAD_TRANSITION, FLOAT, "0.0"}},
    {"NextMapSpeedLimit", {CLEAR_ON_ONROAD_TRANSITION, JSON}},
    {"Offroad_OSMUpdateRequired", {CLEAR_ON_MANAGER_START, JSON}},
    {"OsmDbUpdatesCheck", {CLEAR_ON_MANAGER_START, BOOL}},  // mapd database update happens with device ON, reset on boot
    {"OSMDownloadBounds", {PERSISTENT, STRING}},
    {"OsmDownloadedDate", {PERSISTENT, STRING, "0.0"}},
    {"OSMDownloadLocations", {PERSISTENT, JSON}},
    {"OSMDownloadProgress", {CLEAR_ON_MANAGER_START, JSON}},
    {"OsmLocal", {PERSISTENT, BOOL}},
    {"OsmLocationName", {PERSISTENT, STRING}},
    {"OsmLocationTitle", {PERSISTENT, STRING}},
    {"OsmLocationUrl", {PERSISTENT, STRING}},
    {"OsmStateName", {PERSISTENT, STRING, "All"}},
    {"OsmStateTitle", {PERSISTENT, STRING}},
    {"OsmWayTest", {PERSISTENT, STRING}},
    {"RoadName", {CLEAR_ON_ONROAD_TRANSITION, STRING}},
    {"RoadNameToggle", {PERSISTENT | BACKUP, BOOL, "0"}},

    // Speed Limit
    {"SpeedLimitMode", {PERSISTENT | BACKUP, INT, "1"}},
    {"SpeedLimitOffsetType", {PERSISTENT | BACKUP, INT, "0"}},
    {"SpeedLimitPolicy", {PERSISTENT | BACKUP, INT, "3"}},
    {"SpeedLimitValueOffset", {PERSISTENT | BACKUP, INT, "0"}},

    // Smart Cruise Control
    {"MapTargetVelocities", {CLEAR_ON_ONROAD_TRANSITION, STRING}},
    {"SmartCruiseControlMap", {PERSISTENT | BACKUP, BOOL, "0"}},
    {"SmartCruiseControlVision", {PERSISTENT | BACKUP, BOOL, "0"}},

    // Torque lateral control custom params
    {"CustomTorqueParams", {PERSISTENT | BACKUP , BOOL}},
    {"EnforceTorqueControl", {PERSISTENT | BACKUP, BOOL}},
    {"LateralJerkTorqueController", {PERSISTENT | BACKUP, BOOL, "0"}},
    {"LiveTorqueParamsToggle", {PERSISTENT | BACKUP , BOOL}},
    {"LiveTorqueParamsRelaxedToggle", {PERSISTENT | BACKUP , BOOL}},
    {"TorqueControlTune", {PERSISTENT | BACKUP, FLOAT, "0.0"}},
    {"TorqueParamsOverrideEnabled", {PERSISTENT | BACKUP, BOOL, "0"}},
    {"TorqueParamsOverrideFriction", {PERSISTENT | BACKUP, FLOAT, "0.1"}},
    {"TorqueParamsOverrideLatAccelFactor", {PERSISTENT | BACKUP, FLOAT, "2.5"}},

    // --- BYD Song Plus DM-i 23 ALT port: user-tunable settings --- //
    // Knobs that would otherwise require editing source, exposed so the tuning
    // can be changed from the car without a rebuild. Every entry uses 0 as
    // "unset", so a fresh install behaves exactly like the hardcoded defaults
    // and the porting tests keep covering the default path.
    //
    // BydSteerLeadMaxDeg: how far the commanded steering angle may lead the
    // measured one at high speed, in 0.1 degree units. This is the bound that
    // actually limits curve tracking (see the project conventions). The low-speed segment is
    // deliberately NOT exposed: that is where the EPS reports
    // TORQUE_TEMP_FAILED, and widening it is known to cause real disengagements.
    {"BydSteerLeadMaxDeg", {PERSISTENT | BACKUP, INT, "0"}},
    // BydSteerRateMax: steering rate ceiling in 0.1 degree per 100 Hz control
    // cycle. The panda keeps its own table at exactly twice this value, so a
    // value raised here must stay below half the panda envelope or every
    // steering frame gets rejected.
    {"BydSteerRateMax", {PERSISTENT | BACKUP, INT, "0"}},
    // Camera focal length in pixels at the published width. Overrides the
    // launcher environment variable only when non-zero.
    {"BydCameraFocalPx", {PERSISTENT | BACKUP, INT, "0"}},
    {"BydCameraWideFocalPx", {PERSISTENT | BACKUP, INT, "0"}},
    // Horizontal field of view in 0.1 degree units, the form the driver can
    // actually measure: park facing a wall, note where the left and right frame
    // edges land, then HFOV = 2*atan(span / (2 * distance)). When non-zero this
    // WINS over the focal length above, because it is the measured quantity and
    // the focal is only derived from it - see the comment in camera.py.
    {"BydCameraFovTenthsDeg", {PERSISTENT | BACKUP, INT, "0"}},
    {"BydCameraWideFovTenthsDeg", {PERSISTENT | BACKUP, INT, "0"}},
    // Diagonal field of view in 0.1 degree units. This is the number printed on
    // a lens or camera datasheet, so it is the one a driver can copy across
    // without doing any trigonometry: f_px = diag_px / (2*tan(DFOV/2)), and the
    // physical focal length cancels out of it. Ranks below a measured
    // horizontal field of view and above the focal + sensor pair, because the
    // pair needs an assumption about the sensor aspect ratio that this does not.
    {"BydCameraDiagFovTenthsDeg", {PERSISTENT | BACKUP, INT, "0"}},
    {"BydCameraWideDiagFovTenthsDeg", {PERSISTENT | BACKUP, INT, "0"}},
    // Physical focal length in 0.1 mm units. Useless on its own: turning mm into
    // pixels needs the sensor size as well (f_px = f_mm * width_px /
    // sensor_width_mm), so this is only consulted when the matching sensor width
    // below is also set. Both spec-sheet numbers are approximate - on this lens
    // the datasheet pair (8 mm, 1/2.7 inch) resolves to 2040 px while the
    // measured field of view resolves to 1864 px, a 9% disagreement - which is
    // exactly why the measured field of view outranks them.
    {"BydCameraFocalTenthMm", {PERSISTENT | BACKUP, INT, "0"}},
    {"BydCameraSensorWidthTenthMm", {PERSISTENT | BACKUP, INT, "0"}},
    {"BydCameraWideFocalTenthMm", {PERSISTENT | BACKUP, INT, "0"}},
    {"BydCameraWideSensorWidthTenthMm", {PERSISTENT | BACKUP, INT, "0"}},
    // BydWideCamCurveAngleDeg is gone: the wide-camera curve gate was measured and
    // dropped (modeld.py:74). Do not add it back - the wide input is continuous
    // whenever a second stream exists, and a switch for it would change nothing.
    // The rest of the BYD knobs that used to live only in source. Every one of
    // them uses 0 as "leave the ported default alone", so an untouched install
    // behaves exactly like the code did before this page existed, and a mistap
    // can always be undone by returning to 0.
    //
    // Split into the group that only shapes the steering envelope and the group
    // that decides when openpilot hands control back to the driver. The second
    // group is genuinely safety relevant: too high a driver-torque threshold and
    // the wheel fights the driver instead of yielding.
    {"BydSteerAngleMaxTenths", {PERSISTENT | BACKUP, INT, "0"}},       // 0.1 deg, absolute angle cap
    {"BydOverrideTorqueRelease", {PERSISTENT | BACKUP, INT, "0"}},     // raw torque units
    {"BydReleaseStableFrames", {PERSISTENT | BACKUP, INT, "0"}},       // 100 Hz cycles
    {"BydRearmAngleErrorTenths", {PERSISTENT | BACKUP, INT, "0"}},     // 0.1 deg
    {"BydEpsRearmHoldMs", {PERSISTENT | BACKUP, INT, "0"}},            // ms
    {"BydEpsRearmTimeoutMs", {PERSISTENT | BACKUP, INT, "0"}},         // ms
    {"BydDriverTorqueBase", {PERSISTENT | BACKUP, INT, "0"}},          // raw torque units
    {"BydDriverTorqueEpsMargin", {PERSISTENT | BACKUP, INT, "0"}},     // raw torque units
    {"BydDriverTorqueFactorHundredths", {PERSISTENT | BACKUP, INT, "0"}},  // 0.01
    {"BydDriverTorqueSamples", {PERSISTENT | BACKUP, INT, "0"}},       // 100 Hz cycles
    {"BydPandaTxRejectLimit", {PERSISTENT | BACKUP, INT, "0"}},        // consecutive rejects
    {"BydPandaTxRejectDecay", {PERSISTENT | BACKUP, INT, "0"}},        // 100 Hz cycles
    {"BydPandaUnavailableLimit", {PERSISTENT | BACKUP, INT, "0"}},     // 100 Hz cycles
    {"BydPandaFaultClearCycles", {PERSISTENT | BACKUP, INT, "0"}},     // 100 Hz cycles
    {"BydBlinkerHoldCycles", {PERSISTENT | BACKUP, INT, "0"}},         // 100 Hz cycles
    // BydHmaRequest: high-beam request put on 0x2B4 (ADAS_HMA_STATE_HAL, 3 bits).
    // 0 = off / stock value; 1..7 are forwarded verbatim. Non-zero also makes the
    // panda suppress the stock 0x2B4 so ours is the only source, with a 200 ms
    // handoff back to stock when the host stops sending.
    {"BydHmaRequest", {PERSISTENT | BACKUP, INT, "0"}},
    // BydLeadBoundTable: the learned lead-bound curve, 'km/h:deg,...' pairs. Written
    // by the lead-bound learner and validated in opendbc/car/byd/values.py before
    // anything uses it (unsorted, widening, or out-of-envelope tables are dropped).
    // Empty means "use the shipped table", which is what a fresh install has.
    {"BydLeadBoundTable", {PERSISTENT | BACKUP, STRING}},
    // BydLeadLearnMode: 0 = unset (the settings page's "default", which is shadow),
    // 1 = off, 2 = shadow (record only, the car is untouched), 3 = apply. Shadow is
    // the default because a raise can only be validated on a live drive, not in a
    // replay. The 0 slot is the page's convention for every knob on this panel.
    {"BydLeadLearnMode", {PERSISTENT | BACKUP, INT, "0"}},
    // BydLeadBoundShadow: what the learner WOULD set. Written every 60 s of driving
    // in shadow mode; nothing reads it for control.
    {"BydLeadBoundShadow", {PERSISTENT | BACKUP, STRING}},
    {"BydCarSpecsSteerRatioTenths", {PERSISTENT | BACKUP, INT, "0"}},  // 0.1, seed only
    {"BydCarSpecsStiffnessHundredths", {PERSISTENT | BACKUP, INT, "0"}},  // 0.01, seed only
    // Fork-local: enable the informational YOLO detector process (yolod). Off by
    // default. yolod only publishes "yoloObjects" on msgq for the console and for
    // debugging; nothing in the control loop reads it.
    {"YoloEnabled", {PERSISTENT | BACKUP, BOOL}},
    // Fork-local: enable the optional lane-marking classifier (laned). Off by
    // default; laned publishes "laneLineTypes" and fills CarStateSP lane types.
    {"LaneLineDetectionEnabled", {PERSISTENT | BACKUP, BOOL}},
    // Fork-local: 0 disabled, 1 block solid/unknown, 2 solid/unknown need torque.
    {"LaneLineCheck", {PERSISTENT | BACKUP, INT, "0"}},
    // Fork-local: nudge modeld's planned curvature toward the lane centre. Off by
    // default. Read by modeld, so a change takes effect on the next model frame.
    {"Blendful", {PERSISTENT | BACKUP, BOOL}},

    // Fork-local: keep the e2e model's "straight-line stop" prior out of the brake
    // candidate pool. Off by default because the same signature also appears when
    // stopping for a red light with no queue ahead. See longitudinal_planner.py.
    {"E2ECrosswalkGuard", {PERSISTENT | BACKUP, BOOL}},
    // Fork-local: brake to a stop for a red/yellow light over ego's lane, and
    // release the brake when it turns green. Off by default - it can command a
    // stop the driver did not ask for, so it is opt-in like the crosswalk guard.
    {"TrafficLightStop", {PERSISTENT | BACKUP, BOOL}},
    // How far before the lamp to aim the stop, in metres. 5 m is a proxy: in China
    // the stop line sits at the crosswalk and the lamp is mounted at or just past
    // it, so aiming 5 m short puts the car before the line. Tune this after the
    // first drive rather than editing code - it is the one number that depends on
    // the junctions you actually drive through.
    {"TrafficLightStopOffset", {PERSISTENT | BACKUP, FLOAT, "5.0"}},
};
