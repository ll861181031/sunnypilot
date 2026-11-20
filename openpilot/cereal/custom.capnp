using Cxx = import "/include/c++.capnp";
$Cxx.namespace("cereal");

@0xb526ba661d550a59;

# custom.capnp: a home for empty structs reserved for custom forks
# These structs are guaranteed to remain reserved and empty in mainline
# cereal, so use these if you want custom events in your fork.

# DO rename the structs
# DON'T change the identifier (e.g. @0x81c2f05a394cf4af)

struct ModularAssistiveDrivingSystem {
  state @0 :ModularAssistiveDrivingSystemState;
  enabled @1 :Bool;
  active @2 :Bool;
  available @3 :Bool;

  enum ModularAssistiveDrivingSystemState {
    disabled @0;
    paused @1;
    enabled @2;
    softDisabling @3;
    overriding @4;
  }
}

struct IntelligentCruiseButtonManagement {
  state @0 :IntelligentCruiseButtonManagementState;
  sendButton @1 :SendButtonState;
  vTarget @2 :Float32;

  enum IntelligentCruiseButtonManagementState {
    inactive @0;      # No button press or default state
    preActive @1;     # Pre-active state before transitioning to increasing or decreasing
    increasing @2;    # Increasing speed
    decreasing @3;    # Decreasing speed
    holding @4;       # Holding steady speed
  }

  enum SendButtonState {
    none @0;
    increase @1;
    decrease @2;
  }
}

# Same struct as Log.RadarState.LeadData
struct LeadData {
  dRel @0 :Float32;
  yRel @1 :Float32;
  vRel @2 :Float32;
  aRel @3 :Float32;
  vLead @4 :Float32;
  dPath @6 :Float32;
  vLat @7 :Float32;
  vLeadK @8 :Float32;
  aLeadK @9 :Float32;
  fcw @10 :Bool;
  status @11 :Bool;
  aLeadTau @12 :Float32;
  modelProb @13 :Float32;
  radar @14 :Bool;
  radarTrackId @15 :Int32 = -1;

  aLeadDEPRECATED @5 :Float32;
}

struct SelfdriveStateSP @0x81c2f05a394cf4af {
  mads @0 :ModularAssistiveDrivingSystem;
  intelligentCruiseButtonManagement @1 :IntelligentCruiseButtonManagement;
  buttonsPressed @2 :UInt16;
  buttonsReleaseToggle @3 :UInt16;

  enum AudibleAlert {
    none @0;

    engage @1;
    disengage @2;
    refuse @3;

    warningSoft @4;
    warningImmediate @5;

    prompt @6;
    promptRepeat @7;
    promptDistracted @8;

    # unused, these are reserved for upstream events so we don't collide
    reserved9 @9;
    reserved10 @10;
    reserved11 @11;
    reserved12 @12;
    reserved13 @13;
    reserved14 @14;
    reserved15 @15;
    reserved16 @16;
    reserved17 @17;
    reserved18 @18;
    reserved19 @19;
    reserved20 @20;
    reserved21 @21;
    reserved22 @22;
    reserved23 @23;
    reserved24 @24;
    reserved25 @25;
    reserved26 @26;
    reserved27 @27;
    reserved28 @28;
    reserved29 @29;
    reserved30 @30;

    promptSingleLow @31;
    promptSingleHigh @32;
  }
}

struct ModelManagerSP @0xaedffd8f31e7b55d {
  activeBundle @0 :ModelBundle;
  selectedBundle @1 :ModelBundle;
  availableBundles @2 :List(ModelBundle);

  struct DownloadUri {
    uri @0 :Text;
    sha256 @1 :Text;
  }

  enum DownloadStatus {
    notDownloading @0;
    downloading @1;
    downloaded @2;
    cached @3;
    failed @4;
    verifying @5;
  }

  struct DownloadProgress {
    status @0 :DownloadStatus;
    progress @1 :Float32;
    eta @2 :UInt32;
  }

  struct Chunk {
    fileName @0 :Text;
    sha256 @1 :Text;
  }

  struct Artifact {
    fileName @0 :Text;
    downloadUri @1 :DownloadUri;
    downloadProgress @2 :DownloadProgress;
    chunks @3 :List(Chunk);
  }

  struct Model {
    type @0 :Type;
    artifact @1 :Artifact;  # Main artifact
    metadata @2 :Artifact;  # Metadata artifact

    enum Type {
      supercombo @0;
      navigation @1;
      vision @2;
      policy @3;
      offPolicy @4;
      onPolicy @5;
      chunked @6;
    }
  }

  enum Runner {
    snpe @0;
    tinygrad @1;
    stock @2;
  }

  struct Override {
    key @0 :Text;
    value @1 :Text;
  }

  struct ModelBundle {
    index @0 :UInt32;
    internalName @1 :Text;
    displayName @2 :Text;
    models @3 :List(Model);
    status @4 :DownloadStatus;
    generation @5 :UInt32;
    environment @6 :Text;
    runner @7 :Runner;
    is20hz @8 :Bool;
    ref @9 :Text;
    minimumSelectorVersion @10 :UInt32;
    overrides @11 :List(Override);
  }
}

struct LongitudinalPlanSP @0xf35cc4560bbf6ec2 {
  dec @0 :DynamicExperimentalControl;
  longitudinalPlanSource @1 :LongitudinalPlanSource;
  smartCruiseControl @2 :SmartCruiseControl;
  speedLimit @3 :SpeedLimit;
  vTarget @4 :Float32;
  aTarget @5 :Float32;
  events @6 :List(OnroadEventSP.Event);
  e2eAlerts @7 :E2eAlerts;
  trafficLightState @8 :Text;
  trafficLightReason @9 :Text;
  trafficLightTargetId @10 :Text;
  trafficLightDistance @11 :Float32 = -1;
  turnGuardActive @12 :Bool;
  turnGuardEnforced @13 :Bool;
  turnGuardReason @14 :Text;
  turnGuardAccelMax @15 :Float32;
  leadGuardActive @16 :Bool;
  leadGuardEnforced @17 :Bool;
  leadGuardReason @18 :Text;
  leadGuardAge @19 :Float32;

  struct DynamicExperimentalControl {
    state @0 :DynamicExperimentalControlState;
    enabled @1 :Bool;
    active @2 :Bool;

    enum DynamicExperimentalControlState {
      acc @0;
      blended @1;
    }
  }

  struct SmartCruiseControl {
    vision @0 :Vision;
    map @1 :Map;

    struct Vision {
      state @0 :VisionState;
      vTarget @1 :Float32;
      aTarget @2 :Float32;
      currentLateralAccel @3 :Float32;
      maxPredictedLateralAccel @4 :Float32;
      enabled @5 :Bool;
      active @6 :Bool;
    }

    struct Map {
      state @0 :MapState;
      vTarget @1 :Float32;
      aTarget @2 :Float32;
      enabled @3 :Bool;
      active @4 :Bool;
    }

    enum VisionState {
      disabled @0; # System disabled or inactive.
      enabled @1; # No predicted substantial turn on vision range.
      entering @2; # A substantial turn is predicted ahead, adapting speed to turn comfort levels.
      turning @3; # Actively turning. Managing acceleration to provide a roll on turn feeling.
      leaving @4; # Road ahead straightens. Start to allow positive acceleration.
      overriding @5; # System overriding with manual control.
    }

    enum MapState {
      disabled @0; # System disabled or inactive.
      enabled @1; # No predicted substantial turn on map range.
      turning @2; # Actively turning. Managing acceleration to provide a roll on turn feeling.
      overriding @3; # System overriding with manual control.
    }
  }

  struct SpeedLimit {
    resolver @0 :Resolver;
    assist @1 :Assist;

    struct Resolver {
      speedLimit @0 :Float32;
      distToSpeedLimit @1 :Float32;
      source @2 :Source;
      speedLimitOffset @3 :Float32;
      speedLimitLast @4 :Float32;
      speedLimitFinal @5 :Float32;
      speedLimitFinalLast @6 :Float32;
      speedLimitValid @7 :Bool;
      speedLimitLastValid @8 :Bool;
    }

    struct Assist {
      state @0 :AssistState;
      enabled @1 :Bool;
      active @2 :Bool;
      vTarget @3 :Float32;
      aTarget @4 :Float32;
    }

    enum Source {
      none @0;
      car @1;
      map @2;
    }

    enum AssistState {
      disabled @0;
      inactive @1; # No speed limit set or not enabled by parameter.
      preActive @2;
      pending @3; # Awaiting new speed limit.
      adapting @4; # Reducing speed to match new speed limit.
      active @5; # Cruising at speed limit.
    }
  }

  enum LongitudinalPlanSource {
    cruise @0;
    sccVision @1;
    sccMap @2;
    speedLimitAssist @3;
  }

  struct E2eAlerts {
    greenLightAlert @0 :Bool;
    leadDepartAlert @1 :Bool;
  }
}

struct OnroadEventSP @0xda96579883444c35 {
  events @0 :List(Event);

  struct Event {
    name @0 :EventName;

    # event types
    enable @1 :Bool;
    noEntry @2 :Bool;
    warning @3 :Bool;   # alerts presented only when  enabled or soft disabling
    userDisable @4 :Bool;
    softDisable @5 :Bool;
    immediateDisable @6 :Bool;
    preEnable @7 :Bool;
    permanent @8 :Bool; # alerts presented regardless of openpilot state
    overrideLateral @10 :Bool;
    overrideLongitudinal @9 :Bool;
  }

  enum EventName {
    lkasEnable @0;
    lkasDisable @1;
    manualSteeringRequired @2;
    manualLongitudinalRequired @3;
    silentLkasEnable @4;
    silentLkasDisable @5;
    silentBrakeHold @6;
    silentWrongGear @7;
    silentReverseGear @8;
    silentDoorOpen @9;
    silentSeatbeltNotLatched @10;
    silentParkBrake @11;
    controlsMismatchLateral @12;
    hyundaiRadarTracksConfirmed @13;
    experimentalModeSwitched @14;
    wrongCarModeAlertOnly @15;
    pedalPressedAlertOnly @16;
    laneTurnLeft @17;
    laneTurnRight @18;
    speedLimitPreActive @19;
    speedLimitActive @20;
    speedLimitChanged @21;
    speedLimitPending @22;
    e2eChime @23;
    laneChangeRoadEdge @24;
    bigModelReady @25;
  }
}

struct CarParamsSP @0x80ae746ee2596b11 {
  flags @0 :UInt32;        # flags for car specific quirks in sunnypilot
  safetyParam @1 : Int16;  # flags for sunnypilot's custom safety flags
  pcmCruiseSpeed @3 :Bool;
  intelligentCruiseButtonManagementAvailable @4 :Bool;
  enableGasInterceptor @5 :Bool;

  neuralNetworkLateralControl @2 :NeuralNetworkLateralControl;

  # BYD Song Plus DM-i 23 ALT port: the settings page can tune the lateral
  # envelope, but opendbc must not import openpilot's Params, so the values are
  # read on the openpilot side and carried here. Both are 0 when the page has
  # not overridden them, which means "use the platform default in
  # opendbc/car/byd/values.py" - an untouched install behaves exactly as before.
  steerAngleLeadTenths @6 :Int16;  # 0.1 deg, high-speed lead bound for the commanded angle
  steerRateTenths @7 :Int16;       # 0.1 deg per 100 Hz cycle, tightening cap on the rate table

  # The rest of the BYD settings page. Same contract: 0 means "use the platform
  # default in opendbc/car/byd/", so an untouched install is byte-for-byte the
  # old behaviour.
  #
  # Grouped the same way the page groups them. The override/rearm/torque block is
  # the safety-relevant one - it decides when openpilot yields the wheel back to
  # the driver, so a wrong value there means the wheel fights the driver.
  steerAngleMaxTenths @8 :Int16;          # 0.1 deg, absolute angle cap
  overrideTorqueRelease @9 :Int16;        # raw torque units, hysteresis for letting go
  releaseStableFrames @10 :Int16;         # 100 Hz cycles of stability before releasing
  rearmAngleErrorTenths @11 :Int16;       # 0.1 deg, how centred before taking over again
  epsRearmHoldMs @12 :Int16;              # ms held off after yielding
  epsRearmTimeoutMs @13 :Int16;           # ms fallback limit for re-arming
  driverTorqueBase @14 :Int16;            # raw torque units, "driver is pulling" threshold
  driverTorqueEpsMargin @15 :Int16;       # raw torque units added to the EPS torque
  driverTorqueFactorHundredths @16 :Int16;  # 0.01, raw torque to calibrated
  driverTorqueSamples @17 :Int16;         # 100 Hz cycles before a pull is believed
  pandaTxRejectLimit @18 :Int16;          # consecutive rejects before a lateral fault
  pandaTxRejectDecay @19 :Int16;          # 100 Hz cycles with no reject that clear the count
  pandaUnavailableLimit @20 :Int16;       # 100 Hz cycles without a snapshot before a fault
  pandaFaultClearCycles @21 :Int16;       # 100 Hz healthy cycles before releasing a fault
  blinkerHoldCycles @22 :Int16;           # 100 Hz cycles a tap keeps the indicator on

  # The learned lead-bound curve from lead_learn.py, as 'km/h:deg,...' pairs. Text
  # rather than tenths because it is a curve (a breakpoint per speed band) with a
  # 0.01 deg resolution. Empty means "use the shipped table in values.py", so a
  # device that has never learnt anything behaves exactly as before.
  leadBoundTable @23 :Text;

  # Automatic-high-beam request put on 0x2B4 (ADAS_HMA_STATE_HAL, three bits).
  # 0 = off, which is also the stock broadcast value; 1..7 are forwarded verbatim.
  # Non-zero makes the host the single source for that message: the panda
  # suppresses the stock copy while we keep sending and hands it back 200 ms
  # after we stop.
  bydHmaRequest @24 :Int8;

  struct NeuralNetworkLateralControl {
    model @0 :Model;
    fuzzyFingerprint @1 :Bool;

    struct Model {
      path @0 :Text;
      name @1 :Text;
    }
  }
}

struct CarControlSP @0xa5cd762cd951a455 {
  mads @0 :ModularAssistiveDrivingSystem;
  params @1 :List(Param);
  leadOne @2 :LeadData;
  leadTwo @3 :LeadData;
  intelligentCruiseButtonManagement @4 :IntelligentCruiseButtonManagement;

  struct Param {
    key @0 :Text;
    type @2 :ParamType;
    value @3 :Data;

    valueDEPRECATED @1 :Text; # The data type change may cause issues with backwards compatibility.
  }

  enum ParamType {
    string @0;
    bool @1;
    int @2;
    float @3;
    time @4;
    json @5;
    bytes @6;
  }
}

struct BackupManagerSP @0xf98d843bfd7004a3 {
  backupStatus @0 :Status;
  restoreStatus @1 :Status;
  backupProgress @2 :Float32;
  restoreProgress @3 :Float32;
  lastError @4 :Text;
  currentBackup @5 :BackupInfo;
  backupHistory @6 :List(BackupInfo);

  enum Status {
    idle @0;
    inProgress @1;
    completed @2;
    failed @3;
  }

  struct Version {
    major @0 :UInt16;
    minor @1 :UInt16;
    patch @2 :UInt16;
    build @3 :UInt16;
    branch @4 :Text;
  }

  struct MetadataEntry {
    key @0 :Text;
    value @1 :Text;
    tags @2 :List(Text);
  }

  struct BackupInfo {
    deviceId @0 :Text;
    version @1 :UInt32;
    config @2 :Text;
    isEncrypted @3 :Bool;
    createdAt @4 :Text;  # ISO timestamp
    updatedAt @5 :Text;  # ISO timestamp
    sunnypilotVersion @6 :Version;
    backupMetadata @7 :List(MetadataEntry);
  }
}

struct CarStateSP @0xb86e6369214c01c8 {
  speedLimit @0 :Float32;

  # True while the active turn signal came from a latched press rather than a light
  # tap - that is, the driver means to turn, not to change lanes. Only cars whose
  # stalk reports the two separately set it. The polarity is deliberate: false is
  # what every other car leaves it at, and false means "not latched", so lane change
  # behavior is unchanged for any car that cannot tell the difference.
  blinkerLatched @1 :Bool;
  bydLateral @2 :BydLateralDiagnostics;

  # Lane-marking types from the optional laned process. Geometry remains in
  # carState/modelV2; these fields only classify the ego-lane boundaries.
  # -1 unknown/not running, 0 dashed, 1 solid.
  leftLaneLine @3 :Int16;
  rightLaneLine @4 :Int16;
  laneLineTypesValid @5 :Bool;

  # True when this side's blind-spot monitor is fitted and expected, but its
  # current reading cannot be trusted (no packet, unknown code, or recovering).
  #
  # The polarity is deliberate, for the same reason as blinkerLatched above: false
  # is the capnp default and means "nothing to report", so every car that does not
  # fill these fields keeps lane changes working. A "valid" field would have read
  # false on all of them and silently disabled auto lane change everywhere.
  #
  # Consumers use it to distinguish "no car in the blind spot" from "I cannot see
  # the blind spot", which is the stronger constraint: unknown is not clear.
  leftBlindspotUnavailable @6 :Bool;
  rightBlindspotUnavailable @7 :Bool;

  # One control cycle's worth of the raw EPS refusal pulse: true if any 0x1FC
  # message received this cycle carried TORQUE_TEMP_FAILED = 2 (TooFast). This is
  # deliberately NOT CarState.steerFaultTemporary, which is debounced by
  # EPS_TEMP_FAULT_SAMPLES so a 1-4 message pulse never raises a takeover prompt -
  # that debounce is right for the UI and hides exactly the events the commanded
  # lead is responsible for, so the learner needs the raw pulse.
  epsTooFast @8 :Bool;

  # Real vehicle lamp/body reports parsed from the BYD CAN bus. These are display
  # telemetry, not commands, and default false so non-BYD cars are unaffected.
  headlight @9 :Bool;       # low-beam headlight switch/BCM state (real, not modeled)
  highBeam @10 :Bool;       # high beam
  autoHeadlight @11 :Bool;  # auto headlight switch
  parkingBrake @12 :Bool;   # electronic parking brake applied
  doorFrontLeft @13 :Bool;  # per-door states, independent of CarState.doorOpen
  doorFrontRight @14 :Bool;
  doorRearLeft @15 :Bool;
  doorRearRight @16 :Bool;
  brakeLights @17 :Bool;    # actual brake-light request, distinct from brakePressed
}

struct BydLateralDiagnostics {
  valid @0 :Bool;
  sampleMonoTime @1 :UInt64;
  frame @2 :UInt64;
  desiredAngleDeg @3 :Float32;
  measuredAngleDeg @4 :Float32;
  leadLimitedAngleDeg @5 :Float32;
  rateLimitedAngleDeg @6 :Float32;
  finalAngleDeg @7 :Float32;
  leadBoundDeg @8 :Float32;
  limitMask @9 :UInt32;
  state @10 :UInt8;
  entryReason @11 :Text;
  enteredStateMonoTime @12 :UInt64;
  gateMask @13 :UInt32;
  requestedActive @14 :Bool;
  submitted @15 :Bool;
  active @16 :Bool;
  prepare @17 :Bool;
  lastSubmittedMonoTime @18 :UInt64;
  epsPrepare @19 :Bool;
  epsBit45 @20 :Bool;
  epsAngleState @21 :Bool;
  cycleReason @22 :Text;
}

struct LiveMapDataSP @0xf416ec09499d9d19 {
  speedLimitValid @0 :Bool;
  speedLimit @1 :Float32;
  speedLimitAheadValid @2 :Bool;
  speedLimitAhead @3 :Float32;
  speedLimitAheadDistance @4 :Float32;
  roadName @5 :Text;
}

struct ModelDataV2SP @0xa1680744031fdb2d {
  laneTurnDirection @0 :TurnDirection;
  leftLaneChangeEdgeBlock @1 :Bool;
  rightLaneChangeEdgeBlock @2 :Bool;
  lateralDelayDebug @3 :LateralDelayDebug;

  # Blendful telemetry. Numbers start at 4 because upstream dev uses 3 for
  # blendfulCurvature while this fork already spent 3 on lateralDelayDebug; the two
  # schemas are therefore NOT wire-compatible for this struct. That only matters if
  # a message crosses between builds, which it does not here.
  blendfulCurvature @4 :Float32;      # curvature the correction added, 1/m
  blendfulTrust @5 :Float32;          # 0..1, how much the lane fit is believed
  modelCurvature @6 :Float32;         # curvature the model asked for, 1/m
  blendfulGoingAround @7 :Bool;       # model is deliberately off the lane centre
  blendfulLeftProb @8 :Float32;
  blendfulRightProb @9 :Float32;
  blendfulLaneWidth @10 :Float32;

  # nav driving model inputs, so a drive can be checked from the log alone: whether the
  # model was actually given a map image and an instruction vector, and how old they were.
  navMapValid @11 :Bool;
  navInstructionValid @12 :Bool;
  navMapAge @13 :Float32;         # s since the last accepted map frame
  navInstructionAge @14 :Float32; # s since the last accepted instruction
  navEvidence @15 :NavModelEvidence;

  enum TurnDirection {
    none @0;
    turnLeft @1;
    turnRight @2;
  }
}

# version=0 means unknown legacy evidence. CRCs cover exact float32 bytes, not route semantics.
# receiptVerified covers worker feed/staging/output; gpuMatch is meaningful only when gpuSampled.
struct NavModelEvidence {
  version @0 :UInt16;
  frameId @1 :UInt32;
  frameIdExtra @2 :UInt32;
  timestampEof @3 :UInt64;
  source @4 :Text;
  sourceChangedMonoTime @5 :UInt64;
  acceptAfterMonoTime @6 :UInt64;
  sourceTransition @7 :Bool;
  instructionMonoTime @8 :UInt64;
  instructionReceivedMonoTime @9 :UInt64;
  instructionReason @10 :Text;
  mapReason @11 :Text;
  mapPollReason @12 :Text;
  mapMonoTime @13 :UInt64;
  mapFrameId @14 :UInt32;
  mapReceivedMonoTime @15 :UInt64;
  maneuverCount @16 :UInt32;
  selectedManeuverIndex @17 :Int32 = -1;
  selectedCommand @18 :Int16 = -1;
  rawDistance @19 :Float32;
  adjustedDistance @20 :Float32;
  traveledDistance @21 :Float32;
  maneuverType @22 :Text;
  maneuverModifier @23 :Text;
  nextManeuverType @24 :Text;
  nextManeuverModifier @25 :Text;
  nextManeuverDistance @26 :Float32;
  navigation @27 :List(Float32);
  desire @28 :List(Float32);
  mapInputSource @29 :Text;
  instructionInputSource @30 :Text;
  requestMonoTime @31 :UInt64;
  responseMonoTime @32 :UInt64;
  clientSequence @33 :UInt64;
  retryCount @34 :UInt16;
  receiptAvailable @35 :Bool;
  receiptVerified @36 :Bool;
  receiptReason @37 :Text;
  workerSession @38 :UInt64;
  workerSequence @39 :UInt64;
  workerCompletedMonoTime @40 :UInt64;
  workerStepNs @41 :UInt64;
  inputNavCrc32 @42 :UInt32;
  inputMapCrc32 @43 :UInt32;
  feedNavCrc32 @44 :UInt32;
  feedMapCrc32 @45 :UInt32;
  stagingNavCrc32 @46 :UInt32;
  stagingMapCrc32 @47 :UInt32;
  outputCrc32 @48 :UInt32;
  workerOutputCrc32 @49 :UInt32;
  capture @50 :Bool;
  synchronized @51 :Bool;
  feedValid @52 :Bool;
  stagingValid @53 :Bool;
  outputValid @54 :Bool;
  gpuSampled @55 :Bool;
  gpuMatch @56 :Bool;
  gpuError @57 :Bool;
  gpuNavCrc32 @58 :UInt32;
  gpuMapCrc32 @59 :UInt32;
  gpuSampleMonoTime @60 :UInt64;
  gpuSampleDurationNs @61 :UInt64;
  diagnosticError @62 :Bool;
  diagnosticDurationNs @63 :UInt64;
  workerErrorCode @64 :UInt32;
  receiptVersion @65 :UInt16;
  rpcNs @66 :UInt64;
  mapBinding @67 :NavRouteBinding;
  mapRendererInstanceId @68 :Text;
  mapCorrelationReason @69 :Text;
  mapPixelCrc32 @70 :UInt32;
  navigationResetSupported @71 :Bool;
  mapResetRequestedEpoch @72 :UInt64;
  instructionResetRequestedEpoch @73 :UInt64;
  mapResetAppliedEpoch @74 :UInt64;
  instructionResetAppliedEpoch @75 :UInt64;
  navigationSessionResetConfirmed @76 :Bool;
  phonePositionQuality @77 :Text;
  phoneGpsAccuracyM @78 :Float32;
  phoneNativePoseErrorM @79 :Float32;
  controlInputAllowed @80 :Bool;
}

# Wire identity is not authority: consumers must compare it with an independently
# observed active route. Version 0 means legacy/unbound, never selected or verified.
struct NavRouteBinding {
  version @0 :UInt16;
  bindingId @1 :Text;
  transportSessionId @2 :Text;
  sourceSessionId @3 :Text;
  provider @4 :Text;
  routeId @5 :Text;
  revision @6 :UInt64;
  cacheSha256 @7 :Text;
  sourceObjectId @8 :Text;
  sourceGeometrySha256 @9 :Text;
  geometrySha256 @10 :Text;
  sourceCoordinateSystem @11 :Text;
  coordinateSystem @12 :Text;
  transform @13 :Text;
  observedSequence @14 :UInt64;
  observedMonoTime @15 :UInt64;
  nativeLengthEvidence @16 :NavNativeLengthEvidence;
}

struct NavLongLinkLength {
  globalPointStart @0 :UInt32;
  pointCount @1 :UInt32;
  distanceCentiM @2 :UInt32;
}

struct NavNativeLengthEvidence {
  version @0 :UInt16;
  decoderId @1 :Text;
  profile @2 :Text;
  sourceCacheSha256 @3 :Text;
  sourceGeometrySha256 @4 :Text;
  pointCount @5 :UInt32;
  routeDistanceCentiM @6 :UInt32;
  longLinks @7 :List(NavLongLinkLength);
}

struct NavPhonePose {
  version @0 :UInt16;
  positionMonoTime @1 :UInt64;
  gpsSourceToken @2 :Text;
  coordinateSystem @3 :Text;
  positionTransform @4 :Text;
  longitude @5 :Float64;
  latitude @6 :Float64;
  horizontalAccuracyM @7 :Float32;
  globalSegment @8 :UInt32;
  matchedLongitude @9 :Float64;
  matchedLatitude @10 :Float64;
  nativePoseErrorM @11 :Float32;
  sourceSegment @12 :UInt32;
  sourceLink @13 :UInt32;
  sourcePoint @14 :UInt32;
  reason @15 :Text;
}

struct NavMapFrameSP {
  version @0 :UInt16;
  rendererInstanceId @1 :Text;
  frameId @2 :UInt32;
  timestampSof @3 :UInt64;
  timestampEof @4 :UInt64;
  routeBinding @5 :NavRouteBinding;
  locationMonoTime @6 :UInt64;
  width @7 :UInt16;
  height @8 :UInt16;
  pixelFormat @9 :Text;
  imageCrc32 @10 :UInt32;  # Exact gray8 bytes; distinct from model float32 input CRC.
  valid @11 :Bool;
  reason @12 :Text;
  hasProjectionEvidence @13 :Bool;
  rawGpsLateralM @14 :Float64;
  rawGpsHeadingErrorDeg @15 :Float64;
  projectionSegment @16 :UInt32;
  progressM @17 :Float64;
  rawGpsHeadingValid @18 :Bool;
}

struct LateralDelayDebug {
  valid @0 :Bool;
  source @1 :Text;
  effectiveDelay @2 :Float32;
  smoothSeconds @3 :Float32;
  actionTime @4 :Float32;
  frameId @5 :UInt32;
  modelOutputType @6 :Text;
  actionExtractor @7 :Text;
  previousCurvature @8 :Float32;
  minControlSpeed @9 :Float32;
  curvatureSmoothing @10 :Bool;
  modelSpeed @11 :Float32;
}

struct CustomReserved10 @0xcb9fd56c7057593a {
}

struct CustomReserved11 @0xc2243c65e0340384 {
}

struct CustomReserved12 @0x9ccdc8676701b412 {
}

struct CustomReserved13 @0xcd96dafb67a082d0 {
}

struct CustomReserved14 @0xb057204d7deadf3f {
}

struct CustomReserved15 @0xbd443b539493bc68 {
}

struct CustomReserved16 @0xfc6241ed8877b611 {
}

struct CustomReserved17 @0xa30662f84033036c {
}

struct CustomReserved18 @0xc86a3d38d13eb3ef {
}

struct CustomReserved19 @0xa4f1eb3323f5f582 {
}

# YOLO object detection (fork-local). Published by the optional yolod process on
# the "yoloObjects" msgq service. The boxes are normalised to the published
# camera frame ([0, 1], x/y = top-left). The ego-light summary is also consumed
# by the opt-in traffic-light planner and may be published by the navigation bridge.
struct YoloObject @0xdff6cd6432970e9e {
  classId @0 :UInt16;
  confidence @1 :Float32;
  x @2 :Float32;
  y @3 :Float32;
  w @4 :Float32;
  h @5 :Float32;
}

struct YoloObjects @0x86492737ab74e512 {
  frameId @0 :UInt32;
  frameWidth @1 :UInt16;
  frameHeight @2 :UInt16;
  inferenceMs @3 :Float32;
  model @4 :Text;
  classNames @5 :List(Text);
  objects @6 :List(YoloObject);

  # Fork-local: the light governing ego's own lane, after lane association and
  # temporal arbitration inside yolod. Stays empty until the assignment has held
  # for several frames, because a single frame is not evidence enough to act on.
  # Consumers must treat an empty state as "no information", never as "proceed".
  egoLightState @7 :Text;
  egoLightFrames @8 :UInt16;      # consecutive agreeing frames behind the state
  egoLightDistance @9 :Float32;   # metres, or -1 when the box cannot be ranged
  egoLightIsStopLine @10 :Bool;   # distance is to the painted stop line, not the lamp
  egoLightSource @11 :Text;
  egoLightSession @12 :Text;
  egoLightSequence @13 :UInt64;
  egoLightReceivedMonoTime @14 :UInt64;
  egoLightIntersectionId @15 :Text;
  egoLightDirection @16 :Text;
  egoLightRelevant @17 :Bool;
  egoLightConfirmations @18 :UInt16;
  stopLineConfirmations @19 :UInt16;
  stopLineTargetId @20 :Text;
  stopLineFrameId @21 :UInt32;
  stopLineCaptureMonoTime @22 :UInt64;
  stopLineDetectedMonoTime @23 :UInt64;
  stopLineReason @24 :Text;
}

# Lane-marking type per ego-lane boundary, published by the optional laned
# process on the "laneLineTypes" msgq service. Geometry still comes from the
# driving model's laneLines; this struct only classifies the marking.
struct LaneLineTypes @0x9f2b6c1d4e5a7b93 {
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
