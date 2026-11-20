#!/usr/bin/env python3
import copy
import os
import time
import threading
from dataclasses import asdict

import openpilot.cereal.messaging as messaging

from openpilot.cereal import log, custom
from opendbc.car.structs import car

from openpilot.common.params import Params
from openpilot.common.realtime import config_realtime_process, Priority, Ratekeeper
from openpilot.common.swaglog import cloudlog, ForwardingHandler

from opendbc.car import DT_CTRL, structs
from opendbc.car.can_definitions import CanData, CanRecvCallable, CanSendCallable
from opendbc.car.carlog import carlog
from opendbc.car.fw_versions import ObdCallback
from opendbc.car.car_helpers import get_car, interfaces
from opendbc.car.interfaces import CarInterfaceBase, RadarInterfaceBase
from openpilot.selfdrive.pandad import can_capnp_to_list, can_list_to_can_capnp
from openpilot.selfdrive.car.cruise import VCruiseHelper
from openpilot.selfdrive.car.helpers import convert_carControlSP, convert_to_capnp

from openpilot.sunnypilot.mads.helpers import set_alternative_experience, set_car_specific_params
from openpilot.sunnypilot.selfdrive.controls.lib.lead_bound_learn import (
  LeadBoundLearning, mode_from_param, resume_keys, table_key, MODE_OFF)
from openpilot.sunnypilot.selfdrive.car import interfaces as sunnypilot_interfaces

# How long lateral must stay inactive before a tuning change is applied. Two seconds
# is long enough for the last commanded angle to have left the bus and short enough
# that a driver who lifts and re-engages does not wait.
LATERAL_TUNE_SETTLE_S = 2.0

REPLAY = "REPLAY" in os.environ

EventName = log.OnroadEvent.EventName

# forward
carlog.addHandler(ForwardingHandler(cloudlog))


def obd_callback(params: Params) -> ObdCallback:
  def set_obd_multiplexing(obd_multiplexing: bool):
    if params.get_bool("ObdMultiplexingEnabled") != obd_multiplexing:
      cloudlog.warning(f"Setting OBD multiplexing to {obd_multiplexing}")
      params.remove("ObdMultiplexingChanged")
      params.put_bool("ObdMultiplexingEnabled", obd_multiplexing, block=True)
      params.get_bool("ObdMultiplexingChanged", block=True)
      cloudlog.warning("OBD multiplexing set successfully")
  return set_obd_multiplexing


def can_comm_callbacks(logcan: messaging.SubSocket, sendcan: messaging.PubSocket) -> tuple[CanRecvCallable, CanSendCallable]:
  def can_recv(wait_for_one: bool = False) -> list[list[CanData]]:
    """
    wait_for_one: wait the normal logcan socket timeout for a CAN packet, may return empty list if nothing comes

    Returns: CAN packets comprised of CanData objects for easy access
    """
    ret = []
    for can in messaging.drain_sock(logcan, wait_for_one=wait_for_one):
      ret.append([CanData(msg.address, msg.dat, msg.src) for msg in can.can])
    return ret

  def can_send(msgs: list[CanData]) -> None:
    sendcan.send(can_list_to_can_capnp(msgs, msgtype='sendcan'))

  return can_recv, can_send


class Car:
  CI: CarInterfaceBase
  RI: RadarInterfaceBase
  CP: car.CarParams
  CP_SP: structs.CarParamsSP
  CP_SP_capnp: custom.CarParamsSP

  def __init__(self, CI=None, RI=None) -> None:
    self.can_sock = messaging.sub_sock('can', timeout=20)
    self.sm = messaging.SubMaster(['pandaStates', 'carControl', 'onroadEvents'] + ['carControlSP', 'longitudinalPlanSP'] + ['laneLineTypes'],
                                  ignore_alive=['laneLineTypes'], ignore_avg_freq=['laneLineTypes'], ignore_valid=['laneLineTypes'])
    self.pm = messaging.PubMaster(['sendcan', 'carState', 'carParams', 'carOutput', 'radarTracks'] + ['carParamsSP', 'carStateSP'])

    self.can_rcv_cum_timeout_counter = 0

    self.CC_prev = car.CarControl.new_message()
    self.CS_prev = car.CarState.new_message()
    self.CS_SP_prev = custom.CarStateSP.new_message()
    self.initialized_prev = False

    self.last_actuators_output = structs.CarControl.Actuators()

    # Structured diagnostics: one event on each lateral-fault edge, not per frame.
    # The raw values are already in carStateSP.bydLateral/rlog; this event makes the
    # onset and its context searchable in swaglog without spamming the control loop.
    self._prev_lateral_fault = False
    self._lateral_fault_start = None
    self._last_panda_tx_blocked = None

    # Lateral tuning hot-swap. A parameter sweep changes the two tracking tables
    # between engagements, so one drive can cover several pairs instead of costing
    # one drive per point. The switch is only applied after lateral has been off
    # for LATERAL_TUNE_SETTLE_S: the driver picks the moment by letting go.
    self._lat_inactive_since: float | None = None
    self._lat_tune_segment = 0

    self.params = Params()

    # Lead-bound learner state is opened by _init_lead_learn() *after* self.CP
    # exists. It needs self.CP.brand, and self.CP is only assigned past the
    # fingerprinting branch below - reading it here raised AttributeError on every
    # start, which took card down before it could publish carState/CarParams and
    # left the UI on "sunnypilot Unavailable / Waiting to start".
    self._lead_learn = None
    self._lead_learn_last_save = 0.0
    self._lead_learn_dirty = False

    self.can_callbacks = can_comm_callbacks(self.can_sock, self.pm.sock['sendcan'])

    is_release_sp = self.params.get_bool("IsReleaseSpBranch")
    is_release = self.params.get_bool("IsReleaseBranch") or is_release_sp

    if CI is None:
      alpha_long_allowed = self.params.get_bool("AlphaLongitudinalEnabled")

      fixed_fingerprint = (self.params.get("CarPlatformBundle") or {}).get("platform", None)
      self.CI = get_car(*self.can_callbacks, obd_callback(self.params), alpha_long_allowed, is_release,
                        fixed_fingerprint=fixed_fingerprint, is_release_sp=is_release_sp)
      sunnypilot_interfaces.setup_interfaces(self.CI, self.params)
      self.RI = interfaces[self.CI.CP.carFingerprint].RadarInterface(self.CI.CP, self.CI.CP_SP)
      self.CP = self.CI.CP
      self.CP_SP = self.CI.CP_SP

      # continue onto next fingerprinting step in pandad
      self.params.put_bool("FirmwareQueryDone", True, block=True)
    else:
      self.CI, self.CP, self.CP_SP = CI, CI.CP, CI.CP_SP
      self.RI = RI

    # self.CP exists from here on. Opened here rather than in a daemon so the learner
    # shares the control loop's clock; it only runs on the brand whose table it
    # models. Shadow mode (the default) records what it would set under
    # BydLeadBoundShadow and never touches the curve in force - see
    # lead_bound_learn.py for why a replay cannot validate a raise.
    self._init_lead_learn()

    self.CP.alternativeExperience = 0
    # mads
    set_alternative_experience(self.CP, self.CP_SP, self.params)
    set_car_specific_params(self.CP, self.CP_SP, self.params)

    # Dynamic Experimental Control
    self.dynamic_experimental_control = self.params.get_bool("DynamicExperimentalControl")

    openpilot_enabled_toggle = self.params.get_bool("OpenpilotEnabledToggle")
    controller_available = self.CI.CC is not None and openpilot_enabled_toggle and not self.CP.dashcamOnly
    self.CP.passive = not controller_available or self.CP.dashcamOnly
    if self.CP.passive:
      safety_config = structs.CarParams.SafetyConfig()
      safety_config.safetyModel = structs.CarParams.SafetyModel.noOutput
      self.CP.safetyConfigs = [safety_config]

    if self.CP.secOcRequired:
      # Copy user key if available
      try:
        with open("/cache/params/SecOCKey") as f:
          user_key = f.readline().strip()
          if len(user_key) == 32:
            self.params.put("SecOCKey", user_key, block=True)
      except Exception:
        pass

      secoc_key = self.params.get("SecOCKey")
      if secoc_key is not None:
        saved_secoc_key = bytes.fromhex(secoc_key.strip())
        if len(saved_secoc_key) == 16:
          self.CP.secOcKeyAvailable = True
          self.CI.CS.secoc_key = saved_secoc_key
          if controller_available:
            self.CI.CC.secoc_key = saved_secoc_key
        else:
          cloudlog.warning("Saved SecOC key is invalid")

    # Write previous route's CarParams
    prev_cp = self.params.get("CarParamsPersistent")
    if prev_cp is not None:
      self.params.put("CarParamsPrevRoute", prev_cp, block=True)

    # Write CarParams for controls and radard
    cp_bytes = self.CP.to_bytes()
    self.params.put("CarParams", cp_bytes, block=True)
    self.params.put("CarParamsCache", cp_bytes)
    self.params.put("CarParamsPersistent", cp_bytes)

    # Write CarParamsSP for controls
    # convert to pycapnp representation for caching and logging
    self.CP_SP_capnp = convert_to_capnp(self.CP_SP)
    cp_sp_bytes = self.CP_SP_capnp.to_bytes()
    self.params.put("CarParamsSP", cp_sp_bytes, block=True)
    self.params.put("CarParamsSPCache", cp_sp_bytes)
    self.params.put("CarParamsSPPersistent", cp_sp_bytes)

    self.v_cruise_helper = VCruiseHelper(self.CP, self.CP_SP)

    self.is_metric = self.params.get_bool("IsMetric")
    self.experimental_mode = self.params.get_bool("ExperimentalMode")

    # card is driven by can recv, expected at 100Hz
    self.rk = Ratekeeper(100, print_delay_threshold=None)

    # log fingerprint in sentry
    sunnypilot_interfaces.log_fingerprint(self.CP)

  def state_update(self) -> tuple[car.CarState, custom.CarStateSP, structs.RadarDataT | None]:
    """carState update loop, driven by can"""

    can_strs = messaging.drain_sock_raw(self.can_sock, wait_for_one=True)
    can_list = can_capnp_to_list(can_strs)

    # Update carState from CAN
    CS, CS_SP = self.CI.update(can_list)
    CS_SP = convert_to_capnp(CS_SP)

    # Update radar tracks from CAN
    RD: structs.RadarDataT | None = self.RI.update(can_list)

    self.sm.update(0)
    self._update_lane_line_types(CS_SP)

    # Capability-based rather than brand-based: any CarState that opts into panda
    # liveness tracking (currently BYD) derives its own lateral-control fault from
    # the panda snapshot, so the generic loop needs no brand-specific branch.
    if hasattr(self.CI.CS, "update_panda_state"):
      panda_fresh = self.sm.all_checks(["pandaStates"])
      self.CI.CS.update_panda_state(self.sm["pandaStates"], panda_fresh)
      CS.steerFaultTemporary = bool(CS.steerFaultTemporary or self.CI.CS.lateral_control_fault)

    can_rcv_valid = len(can_strs) > 0

    # Check for CAN timeout
    if not can_rcv_valid:
      self.can_rcv_cum_timeout_counter += 1

    if can_rcv_valid and REPLAY:
      self.can_log_mono_time = messaging.log_from_bytes(can_strs[0]).logMonoTime

    self.v_cruise_helper.update_speed_limit_assist(self.is_metric, self.sm['longitudinalPlanSP'])
    self.v_cruise_helper.update_v_cruise(CS, self.sm['carControl'].enabled, self.is_metric)
    if self.sm['carControl'].enabled and not self.CC_prev.enabled:
      # Use CarState w/ buttons from the step selfdrived enables on
      self.v_cruise_helper.initialize_v_cruise(self.CS_prev, self.experimental_mode, self.dynamic_experimental_control)

    # TODO: mirror the carState.cruiseState struct?
    CS.vCruise = float(self.v_cruise_helper.v_cruise_kph)
    CS.vCruiseCluster = float(self.v_cruise_helper.v_cruise_cluster_kph)

    return CS, CS_SP, RD

  def _init_lead_learn(self) -> None:
    """Open the lead-bound learner for the brand whose curve it models.

    Split out of __init__ and called only once self.CP exists: the brand gate reads
    self.CP.brand, so the ordering is the whole point of the method - reading it
    earlier raised AttributeError and killed card before it published carState.
    """
    if str(getattr(self.CP, "brand", "")) != "byd":
      return
    from opendbc.car.byd.values import CarControllerParams as _BydParams, parse_lead_bound_table
    try:
      mode = mode_from_param(self.params.get("BydLeadLearnMode", return_default=True))
      # Resume from the curve this mode owns, then from the other one as a fallback
      # for a mode switch. Reading only BydLeadBoundTable meant shadow mode - the
      # default - restarted from the shipped curve on every boot and discarded the
      # proposal the previous drive had already written to BydLeadBoundShadow.
      own_key, fallback_key = resume_keys(mode)
      stored = self.params.get(own_key, return_default=True) or ""
      if not stored:
        stored = self.params.get(fallback_key, return_default=True) or ""
    except Exception:
      # Params keys live in libparams_c, which is built from params_keys.h. A
      # checkout whose library predates the keys must not take the car down over
      # an optional learner: stay off and say why.
      cloudlog.event("bydLeadLearn.unavailable", error=True,
                     reason="params keys missing - rebuild openpilot/common/libparams_c.so")
      return
    if mode == MODE_OFF:
      return
    parsed = parse_lead_bound_table(stored) if stored else None
    bp, values = parsed if parsed is not None else _BydParams.STEER_ANGLE_ERROR_MAX
    self._lead_learn = LeadBoundLearning(bp, values, mode)

  def _update_lane_line_types(self, CS_SP: custom.CarStateSP) -> None:
    """Copy the optional laned result into CarStateSP.

    A missing service means "unknown", not "dashed": -1 keeps a consumer from
    acting on a line type that was never measured. laned is optional and its
    service is absent whenever LaneLineDetectionEnabled is false.
    """
    seen = bool(getattr(self.sm, "seen", {}).get("laneLineTypes", False))
    alive = bool(getattr(self.sm, "alive", {}).get("laneLineTypes", False))
    valid_map = getattr(self.sm, "valid", {})
    valid = seen and alive and bool(valid_map.get("laneLineTypes", False))
    if valid:
      payload = self.sm["laneLineTypes"]
      CS_SP.leftLaneLine = int(payload.leftType)
      CS_SP.rightLaneLine = int(payload.rightType)
    else:
      CS_SP.leftLaneLine = -1
      CS_SP.rightLaneLine = -1
    CS_SP.laneLineTypesValid = valid

  def _log_lateral_fault(self, CS: car.CarState) -> None:
    """Emit a structured event when the lateral fault changes state.

    This is observability only: it does not influence control. The event carries
    the controller's own diagnostic snapshot plus the EPS/CAN values that are not
    otherwise preserved as a single record at the fault edge.
    """
    if not hasattr(CS, "steerFaultTemporary"):
      return
    # ``"pandaStates" in self.sm`` looks harmless and is not: SubMaster defines neither
    # __contains__ nor __iter__, so Python falls back to the legacy integer protocol and
    # calls sm[0], sm[1], ... - and self.data[0] raises KeyError: 0, which is not
    # IndexError, so it escapes instead of ending the loop. That crash took card down on
    # every start, which in turn left selfdrived with no carState and surfaced on screen
    # as "Unknown Vehicle Variant". Use the bookkeeping SubMaster actually exposes.
    try:
      panda_states = self.sm["pandaStates"] if self.sm.seen.get("pandaStates", False) else []
      panda = panda_states[0] if len(panda_states) else None
    except (KeyError, IndexError, AttributeError, TypeError):
      panda = None
    panda_tx_blocked = int(panda.safetyTxBlocked) if panda is not None else None
    tx_blocked_delta = (None if panda_tx_blocked is None or self._last_panda_tx_blocked is None
                        else panda_tx_blocked - self._last_panda_tx_blocked)
    self._last_panda_tx_blocked = panda_tx_blocked

    fault = bool(CS.steerFaultTemporary or CS.steerFaultPermanent)

    if fault and not self._prev_lateral_fault:
      # This snapshot is only consumed on the fault edge; building it every 10 ms
      # was a pure allocation/CPU cost on the card thread.
      diag = getattr(self.CI.CS, "byd_lateral", None)
      diag_dict = None
      if diag is not None and getattr(diag, "valid", False):
        try:
          diag_dict = asdict(diag)
        except (TypeError, ValueError):
          diag_dict = None

      panda_dict = None
      if panda is not None:
        panda_dict = {
          "controlsAllowed": bool(panda.controlsAllowed),
          "safetyTxBlocked": panda_tx_blocked,
          "safetyRxInvalid": int(panda.safetyRxInvalid),
          "safetyRxChecksInvalid": bool(panda.safetyRxChecksInvalid),
          "faults": [str(f) for f in panda.faults],
          "safetyModel": str(panda.safetyModel),
          "safetyTxBlockedReason": int(getattr(panda, "safetyTxBlockedReason", 0)),
          "lastTxBlockedAddr": int(getattr(panda, "lastTxBlockedAddr", 0)),
          "lastTxBlockedBus": int(getattr(panda, "lastTxBlockedBus", 0)),
          "lastTxBlockedLen": int(getattr(panda, "lastTxBlockedLen", 0)),
          "lastTxBlockedData0": int(getattr(panda, "lastTxBlockedData0", 0)),
          "lastTxBlockedData1": int(getattr(panda, "lastTxBlockedData1", 0)),
        }
      self._lateral_fault_start = time.monotonic()
      cloudlog.event(
        "bydLateralFault",
        error=True,
        epsTemporary=bool(CS.steerFaultTemporary),
        epsPermanent=bool(CS.steerFaultPermanent),
        lateralControlFault=str(getattr(self.CI.CS, "lateral_control_fault", "")),
        speedMs=round(float(CS.vEgo), 3),
        steeringAngleDeg=round(float(CS.steeringAngleDeg), 3),
        steeringRateDeg=round(float(CS.steeringRateDeg), 3),
        steeringTorque=round(float(CS.steeringTorque), 3),
        steeringTorqueEps=round(float(CS.steeringTorqueEps), 3),
        steeringPressed=bool(CS.steeringPressed),
        canValid=bool(CS.canValid),
        panda=panda_dict,
        pandaTxBlockedDelta=tx_blocked_delta,
        bydLateral=diag_dict,
      )
    elif not fault and self._prev_lateral_fault:
      duration = (time.monotonic() - self._lateral_fault_start) if self._lateral_fault_start else None
      cloudlog.event(
        "bydLateralRecovered",
        durationS=round(duration, 3) if duration is not None else None,
        speedMs=round(float(CS.vEgo), 3),
        steeringAngleDeg=round(float(CS.steeringAngleDeg), 3),
        steeringRateDeg=round(float(CS.steeringRateDeg), 3),
        pandaTxBlocked=panda_tx_blocked,
      )
      self._lateral_fault_start = None

    self._prev_lateral_fault = fault

  def _lead_learn_step(self, CS, CS_SP) -> None:
    """Feed the learner one 100 Hz sample and persist its curve once a minute.

    In shadow mode (`BydLeadLearnMode = 1`, the default) this writes
    BydLeadBoundShadow and changes nothing about the car. In apply mode it writes
    BydLeadBoundTable instead, and the ordinary tuning-refresh path picks it up
    while lateral is inactive - the learner itself never touches CP_SP.
    """
    learn = self._lead_learn
    if learn is None or not learn.enabled:
      return

    diag = CS_SP.bydLateral
    valid = bool(diag.valid)
    mask = int(diag.limitMask) if valid else 0
    at_bound = bool(mask & 1) and not (mask & 8)   # Limit.LEAD and not INACTIVE
    starved = valid and abs(diag.desiredAngleDeg - diag.measuredAngleDeg) > diag.leadBoundDeg + 0.5
    if learn.step(CS.vEgo, at_bound, starved, bool(getattr(CS_SP, "epsTooFast", False))):
      self._lead_learn_dirty = True

    now = time.monotonic()
    if not self._lead_learn_dirty or now - self._lead_learn_last_save < 60.0:
      return
    self._lead_learn_last_save = now
    self._lead_learn_dirty = False
    text = learn.table_text()
    key = table_key(learn.mode)
    self.params.put(key, text)
    cloudlog.event("bydLeadLearn", **learn.summary(), key=key)

  def _maybe_refresh_lateral_tuning(self) -> None:
    """Re-read the settings-page tracking knobs and apply them between engagements.

    These two numbers used to be resolved once at construction, so a parameter
    sweep cost one drive per point. They are now re-read every 50 s -- the same
    cadence carParamsSP is published at, so the log always shows the pair that was
    in force -- and applied only while lateral has been inactive for
    LATERAL_TUNE_SETTLE_S.

    Deliberately narrow: only the lead bound and the angle/rate limits move. The
    driver-interaction knobs stay fixed for the life of the controller.

    The CarParamsSP Params entries are left untouched on purpose. Rewriting them
    here would let a mid-drive switch leak into every other process that reads
    CarParamsSP; the next stack start re-reads Params anyway.
    """
    lat_active = bool(self.sm['carControl'].latActive) if self.sm.all_checks(['carControl']) else True
    now = time.monotonic()
    if lat_active:
      self._lat_inactive_since = None
      return
    if self._lat_inactive_since is None:
      self._lat_inactive_since = now
      return
    if now - self._lat_inactive_since < LATERAL_TUNE_SETTLE_S:
      return

    try:
      fresh = copy.deepcopy(self.CP_SP)
      set_car_specific_params(copy.deepcopy(self.CP), fresh, self.params)
    except Exception:
      cloudlog.exception("lat_tune.refresh_failed")
      return

    fields = ("steerAngleLeadTenths", "steerRateTenths", "steerAngleMaxTenths")
    changed = {f: int(getattr(fresh, f, 0) or 0) for f in fields
               if int(getattr(fresh, f, 0) or 0) != int(getattr(self.CP_SP, f, 0) or 0)}
    # The learned curve is text, so it compares separately; the value carried in
    # `changed` is only for the log line.
    if str(getattr(fresh, "leadBoundTable", "") or "") != str(getattr(self.CP_SP, "leadBoundTable", "") or ""):
      changed["leadBoundTable"] = 1
    if not changed:
      return

    self.CP_SP = fresh
    self.CP_SP_capnp = convert_to_capnp(self.CP_SP)
    applied = False
    refresh = getattr(self.CI.CC, "refresh_tuning", None)
    if refresh is not None:
      try:
        applied = bool(refresh(self.CP_SP))
      except Exception:
        cloudlog.exception("lat_tune.apply_failed")
    if applied:
      self._lat_tune_segment += 1
      cloudlog.event("lat_tune.segment", segment=self._lat_tune_segment, **changed)

  def state_publish(self, CS: car.CarState, CS_SP: custom.CarStateSP, RD: structs.RadarDataT | None):
    """carState and carParams publish loop"""

    # carParams - logged every 50 seconds (> 1 per segment)
    if self.sm.frame % int(50. / DT_CTRL) == 0:
      cp_send = messaging.new_message('carParams')
      cp_send.valid = True
      cp_send.carParams = self.CP
      self.pm.send('carParams', cp_send)

    # publish new carOutput
    co_send = messaging.new_message('carOutput')
    co_send.valid = self.sm.all_checks(['carControl'])
    co_send.carOutput.actuatorsOutput = self.last_actuators_output
    self.pm.send('carOutput', co_send)

    # kick off controlsd step while we actuate the latest carControl packet
    cs_send = messaging.new_message('carState')
    cs_send.valid = CS.canValid
    cs_send.carState = CS
    cs_send.carState.canErrorCounter = self.can_rcv_cum_timeout_counter
    cs_send.carState.cumLagMs = -self.rk.remaining * 1000.
    self.pm.send('carState', cs_send)

    if RD is not None:
      tracks_msg = messaging.new_message('radarTracks')
      tracks_msg.valid = not any(RD.errors.to_dict().values())
      tracks_msg.radarTracks = RD
      self.pm.send('radarTracks', tracks_msg)

    # carParamsSP - logged every 50 seconds (> 1 per segment)
    if self.sm.frame % int(50. / DT_CTRL) == 0:
      self._maybe_refresh_lateral_tuning()
      cp_sp_send = messaging.new_message('carParamsSP')
      cp_sp_send.valid = True
      cp_sp_send.carParamsSP = self.CP_SP_capnp
      self.pm.send('carParamsSP', cp_sp_send)

    cs_sp_send = messaging.new_message('carStateSP')
    cs_sp_send.valid = CS.canValid
    cs_sp_send.carStateSP = CS_SP
    self.pm.send('carStateSP', cs_sp_send)

  def controls_update(self, CS: car.CarState, CC: car.CarControl, CC_SP: custom.CarControlSP):
    """control update loop, driven by carControl"""

    if not self.initialized_prev:
      # Initialize CarInterface, once controls are ready
      # TODO: this can make us miss at least a few cycles when doing an ECU knockout
      self.CI.init(self.CP, self.CP_SP, *self.can_callbacks)
      # signal pandad to switch to car safety mode
      self.params.put_bool("ControlsReady", True)

    if self.sm.all_alive(['carControl']):
      # send car controls over can
      now_nanos = self.can_log_mono_time if REPLAY else int(time.monotonic() * 1e9)
      self.last_actuators_output, can_sends = self.CI.apply(CC, convert_carControlSP(CC_SP), now_nanos)
      self.pm.send('sendcan', can_list_to_can_capnp(can_sends, msgtype='sendcan', valid=CS.canValid))

      self.CC_prev = CC

  def step(self):
    CS, CS_SP, RD = self.state_update()

    self._log_lateral_fault(CS)
    self._lead_learn_step(CS, CS_SP)
    self.state_publish(CS, CS_SP, RD)

    initialized = (not any(e.name == EventName.selfdriveInitializing for e in self.sm['onroadEvents']) and
                   self.sm.seen['onroadEvents'])
    if not self.CP.passive and initialized:
      self.controls_update(CS, self.sm['carControl'], self.sm['carControlSP'])

    self.initialized_prev = initialized
    self.CS_prev = CS
    self.CS_SP_prev = CS_SP

  def params_thread(self, evt):
    while not evt.is_set():
      self.is_metric = self.params.get_bool("IsMetric")
      self.experimental_mode = self.params.get_bool("ExperimentalMode") and self.CP.openpilotLongitudinalControl

      # sunnypilot
      self.dynamic_experimental_control = self.params.get_bool("DynamicExperimentalControl")
      self.v_cruise_helper.read_custom_set_speed_params()

      time.sleep(0.1)

  def card_thread(self):
    e = threading.Event()
    t = threading.Thread(target=self.params_thread, args=(e, ))
    try:
      t.start()
      while True:
        self.step()
        self.rk.monitor_time()
    finally:
      e.set()
      t.join()


def main():
  config_realtime_process(4, Priority.CTRL_HIGH)
  car = Car()
  car.card_thread()


if __name__ == "__main__":
  main()
