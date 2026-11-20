#!/usr/bin/env python3
"""cereal -> CPlink compact snapshots (M3a: the six services the app needs first).

The subscriber runs in its own thread: ``SubMaster.update()`` is blocking enough that
running it inside the aiohttp event loop would stall the WebSocket pumps.  The async
side only reads an immutable snapshot under a lock and encodes it with
``compact_codec``.

No fabricated values: a service absent from cereal (or ``sm.valid == False``) is simply
not in the snapshot, and the app's per-service TTL clears it.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

from compact_codec import (
    ALERT_SIZE, ALERT_STATUS, DEVICE_TYPE, GEAR_SHIFTER, LANE_CHANGE_DIRECTION,
    LANE_CHANGE_STATE, PERSONALITY, PLAN_SOURCE,
)
from compact_codec import _enum_index, _finite, _get, _int

LOG = logging.getLogger("cplink_shim.compact")

SOURCE_SERVICES = [
    "carState", "carStateSP", "controlsState", "carControl",
    "modelV2", "longitudinalPlan", "radarState", "selfdriveState",
    "deviceState", "gpsLocationExternal", "gpsLocation",
    "liveMapDataSP", "navInstruction",
]
# Kept for callers/tests that expect the M3a name.
M3A_SERVICES = SOURCE_SERVICES


def _list(value) -> list:
    if value is None:
        return []
    try:
        return list(value)
    except TypeError:
        return []


def _item(value, index: int, default=0.0):
    items = _list(value)
    if index < len(items):
        return items[index]
    return default


def build_car_state(messages: dict[str, Any]) -> dict[str, Any]:
    cs = messages["carState"]
    cs_sp = messages.get("carStateSP")
    gear = _get(cs, "gearShifter", 0)
    if not isinstance(gear, int):
        gear = _enum_index(gear, GEAR_SHIFTER)
    v_ego = _finite(_get(cs, "vEgo", 0.0))
    v_ego_cluster = _finite(_get(cs, "vEgoCluster", 0.0)) or v_ego
    # compact cruise speeds are km/h (the app divides controlsState.vCruiseCluster by
    # 3.6); cereal stores m/s.
    v_cruise_cluster = (_finite(_get(cs, "vCruiseCluster", 0.0))
                        or _finite(_get(cs, "vCruise", 0.0))) * 3.6
    return {
        "vEgo": v_ego,
        "aEgo": _finite(_get(cs, "aEgo", 0.0)),
        "vEgoCluster": v_ego_cluster,
        "vCruiseCluster": v_cruise_cluster,
        "steeringAngleDeg": _finite(_get(cs, "steeringAngleDeg", 0.0)),
        "brakeHoldActive": bool(_get(cs, "brakeHoldActive", False)),
        "softHoldActive": 0,
        "carrotCruise": 0,
        "gearStep": 0,
        "useLaneLineSpeed": 0.0,
        # CarState.brakeLights is a deprecated cereal group that capnp no longer
        # exposes; the real lamp state lives on CarStateSP.brakeLights (BYD CAN).
        "brakeLights": bool(
            _get(cs_sp, "brakeLights", False) if cs_sp is not None
            else _get(cs, "brakeLights", False)
        ),
        "leftBlindspot": bool(_get(cs, "leftBlindspot", False)),
        "rightBlindspot": bool(_get(cs, "rightBlindspot", False)),
        "leftLaneLine": _int(_get(cs_sp, "leftLaneLine", -1) if cs_sp is not None else -1),
        "rightLaneLine": _int(_get(cs_sp, "rightLaneLine", -1) if cs_sp is not None else -1),
        "gearShifter": gear,
        "leftBlinker": bool(_get(cs, "leftBlinker", False)),
        "rightBlinker": bool(_get(cs, "rightBlinker", False)),
        "fuelGauge": _finite(_get(cs, "fuelGauge", 0.0)),
        "ureaGauge": 0.0,
        "tpms": {"fl": 0.0, "fr": 0.0, "rl": 0.0, "rr": 0.0},
        "evModeValid": False,
        "evModeActive": False,
        "headlight": bool(_get(cs_sp, "headlight", False) if cs_sp is not None else False),
        "highBeam": bool(_get(cs_sp, "highBeam", False) if cs_sp is not None else False),
        "autoHeadlight": bool(_get(cs_sp, "autoHeadlight", False) if cs_sp is not None else False),
        "parkingBrake": bool(_get(cs_sp, "parkingBrake", False) if cs_sp is not None else False),
        "doorFrontLeft": bool(_get(cs_sp, "doorFrontLeft", False) if cs_sp is not None else False),
        "doorFrontRight": bool(_get(cs_sp, "doorFrontRight", False) if cs_sp is not None else False),
        "doorRearLeft": bool(_get(cs_sp, "doorRearLeft", False) if cs_sp is not None else False),
        "doorRearRight": bool(_get(cs_sp, "doorRearRight", False) if cs_sp is not None else False),
        # CarStateSP has no tailgate signal on this car (DBC lacks a trunk message),
        # and CarState.doorOpen is the whole-car aggregate: keep them false here and
        # let the app derive per-door animation from the four doorFront/doorRear bits.
        "tailgateOpen": False,
        "doorOpen": bool(_get(cs, "doorOpen", False)),
    }


def _lateral_accels(controls_state) -> tuple[float, float]:
    state = _get(controls_state, "lateralControlState", None)
    if state is None:
        return 0.0, 0.0
    try:
        which = state.which()
    except AttributeError:
        which = ""
    if which == "torqueState":
        torque = _get(state, "torqueState", None)
        return (_finite(_get(torque, "actualLateralAccel", 0.0)),
                _finite(_get(torque, "desiredLateralAccel", 0.0)))
    return 0.0, 0.0


def build_controls_state(messages: dict[str, Any]) -> dict[str, Any]:
    controls = messages["controlsState"]
    car_control = messages.get("carControl")
    cs = messages.get("carState")
    selfdrive = messages.get("selfdriveState")
    actual_accel, desired_accel = _lateral_accels(controls)
    v_cruise_cluster = (_finite(_get(cs, "vCruiseCluster", 0.0))
                        or _finite(_get(cs, "vCruise", 0.0))) * 3.6
    return {
        # ControlsState has no enabled field in this fork; SelfdriveState does.
        "enabled": bool(_get(selfdrive, "enabled", False))
        if selfdrive is not None else bool(_get(controls, "enabled", False)),
        "vCruiseCluster": v_cruise_cluster,
        "activeLaneLine": bool(_get(car_control, "latActive", False)) if car_control is not None else False,
        "curvature": _finite(_get(controls, "curvature", 0.0)),
        "desiredCurvature": _finite(_get(controls, "desiredCurvature", 0.0)),
        "actualLateralAccel": actual_accel,
        "desiredLateralAccel": desired_accel,
        "lateralOutput": 0.0,
    }


def build_car_control(messages: dict[str, Any]) -> dict[str, Any]:
    cc = messages["carControl"]
    actuators = _get(cc, "actuators", None)
    return {
        "latActive": bool(_get(cc, "latActive", False)),
        "longActive": bool(_get(cc, "longActive", False)),
        "actuators": {
            "steeringAngleDeg": _finite(_get(actuators, "steeringAngleDeg", 0.0)),
            "accel": _finite(_get(actuators, "accel", 0.0)),
            "curvature": _finite(_get(actuators, "curvature", 0.0)),
        },
    }


def _axis(item, name: str) -> float:
    value = _get(item, name, 0.0)
    # cereal XYZTData axes are capnp lists (_DynamicListBuilder), which are not
    # instances of list/tuple but do support len()/indexing.
    if not isinstance(value, (str, bytes)) and hasattr(value, "__len__") and hasattr(value, "__getitem__"):
        items = _list(value)
        value = items[0] if items else 0.0
    return _finite(value)


def _xyz(item) -> dict[str, list[float]]:
    # cereal ModelDataV2 uses XYZTData (x/y/z are lists); the compact ``xyz`` struct
    # also encodes each axis as a u16-counted list.  Keep one sample per axis.
    return {
        "x": [_axis(item, "x")],
        "y": [_axis(item, "y")],
        "z": [_axis(item, "z")],
    }


def build_model_v2(messages: dict[str, Any]) -> dict[str, Any]:
    model = messages["modelV2"]
    lane_lines = [_xyz(item) for item in _list(_get(model, "laneLines", []))]
    road_edges = [_xyz(item) for item in _list(_get(model, "roadEdges", []))]
    leads = []
    for lead in _list(_get(model, "leadsV3", [])):
        leads.append({
            "prob": _finite(_get(lead, "prob", 0.0)),
            "x": [_finite(_item(_get(lead, "x", []), 0))],
            "y": [_finite(_item(_get(lead, "y", []), 0))],
            "v": [_finite(_item(_get(lead, "v", []), 0))],
        })
    return {
        "frameId": _int(_get(model, "frameId", 0)) & 0xFFFFFFFF,
        "frameIdExtra": _int(_get(model, "frameIdExtra", 0)) & 0xFFFFFFFF,
        "position": _xyz(_get(model, "position", None)),
        "velocity": {"x": [_axis(_get(model, "velocity", None), "x")]},
        "laneLines": lane_lines,
        "laneLineProbs": [_finite(v) for v in _list(_get(model, "laneLineProbs", []))],
        "roadEdges": road_edges,
        "roadEdgeStds": [_finite(v) for v in _list(_get(model, "roadEdgeStds", []))],
        "leadsV3": leads,
        "laneLineStds": [_finite(v) for v in _list(_get(model, "laneLineStds", []))],
        "frameAge": _int(_get(model, "frameAge", 0)),
        "frameDropPerc": _finite(_get(model, "frameDropPerc", 0.0)),
        "modelExecutionTime": _finite(_get(model, "modelExecutionTime", 0.0)),
    }


def _plan_source(value) -> int:
    if isinstance(value, int):
        return min(value, len(PLAN_SOURCE) - 1)
    text = str(value)
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    if text == "trafficLight":
        return PLAN_SOURCE.index("e2e")
    try:
        return PLAN_SOURCE.index(text)
    except ValueError:
        return 0


def build_longitudinal_plan(messages: dict[str, Any]) -> dict[str, Any]:
    plan = messages["longitudinalPlan"]
    cs = messages.get("carState")
    return {
        "accels": [_finite(_item(_get(plan, "accels", []), 0))],
        "speeds": [_finite(_item(_get(plan, "speeds", []), 0))],
        "jerks": [_finite(_item(_get(plan, "jerks", []), 0))],
        "tFollow": 0.0,
        "desiredDistance": 0.0,
        "myDrivingMode": 0,
        "xState": 0,
        "trafficState": 0,
        "longitudinalPlanSource": _plan_source(_get(plan, "longitudinalPlanSource", 0)),
        # compact cruise target is km/h
        "cruiseTarget": _finite(_get(cs, "vCruise", 0.0)) * 3.6,
    }


def _radar_lead(lead) -> dict[str, Any]:
    if lead is None:
        return {
            "dRel": 0.0, "yRel": 0.0, "vRel": 0.0, "aRel": 0.0, "vLead": 0.0,
            "aLead": 0.0, "dPath": 0.0, "vLat": 0.0, "vLeadK": 0.0, "aLeadK": 0.0,
            "fcw": False, "status": False, "aLeadTau": 0.0, "modelProb": 0.0,
            "radar": False, "radarTrackId": -1, "jLead": 0.0, "score": 0.0,
        }
    return {
        "dRel": _finite(_get(lead, "dRel", 0.0)),
        "yRel": _finite(_get(lead, "yRel", 0.0)),
        "vRel": _finite(_get(lead, "vRel", 0.0)),
        "aRel": _finite(_get(lead, "aRel", 0.0)),
        "vLead": _finite(_get(lead, "vLead", 0.0)),
        "aLead": _finite(_get(lead, "aLead", 0.0)),
        "dPath": _finite(_get(lead, "dPath", 0.0)),
        "vLat": _finite(_get(lead, "vLat", 0.0)),
        "vLeadK": _finite(_get(lead, "vLeadK", 0.0)),
        "aLeadK": _finite(_get(lead, "aLeadK", 0.0)),
        "fcw": bool(_get(lead, "fcw", False)),
        "status": bool(_get(lead, "present", False)),
        "aLeadTau": _finite(_get(lead, "aLeadTau", 0.0)),
        "modelProb": _finite(_get(lead, "modelProb", 0.0)),
        "radar": bool(_get(lead, "radar", False)),
        "radarTrackId": _int(_get(lead, "radarTrackId", -1)),
        "jLead": 0.0,
        "score": 0.0,
    }


def build_radar_state(messages: dict[str, Any]) -> dict[str, Any]:
    radar = messages["radarState"]
    empty = _radar_lead(None)
    return {
        "leadOne": _radar_lead(_get(radar, "leadOne", None)),
        "leadTwo": _radar_lead(_get(radar, "leadTwo", None)),
        "leadRight": dict(empty),
        "leadLeft": dict(empty),
        "leadsLeft": [],
        "leadsCenter": [],
        "leadsRight": [],
    }


def _gps(messages: dict[str, Any]):
    gps = messages.get("gpsLocationExternal")
    if gps is None:
        gps = messages.get("gpsLocation")
    return gps


def _has_fix(gps) -> bool:
    if gps is None:
        return False
    explicit = _get(gps, "hasFix", None)
    if explicit is not None:
        return bool(explicit)
    lat = _finite(_get(gps, "latitude", 0.0))
    lon = _finite(_get(gps, "longitude", 0.0))
    return lat != 0.0 and lon != 0.0 and -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0


def build_device_state(messages: dict[str, Any]) -> dict[str, Any]:
    device = messages["deviceState"]
    return {
        "memoryUsagePercent": max(-128, min(127, _int(_get(device, "memoryUsagePercent", 0)))),
        "freeSpacePercent": _finite(_get(device, "freeSpacePercent", 0.0)),
        "cpuTempC": [_finite(v) for v in _list(_get(device, "cpuTempC", []))],
        "deviceType": _enum_index(_get(device, "deviceType", "unknown"), DEVICE_TYPE),
        "started": bool(_get(device, "started", False)),
    }


def build_selfdrive_state(messages: dict[str, Any]) -> dict[str, Any]:
    state = messages["selfdriveState"]
    return {
        "enabled": bool(_get(state, "enabled", False)),
        "personality": _enum_index(_get(state, "personality", "standard"), PERSONALITY),
        "alertStatus": _enum_index(_get(state, "alertStatus", "normal"), ALERT_STATUS),
        "alertSize": _enum_index(_get(state, "alertSize", "none"), ALERT_SIZE),
        "alertType": _get(state, "alertType", ""),
        "alertText1": str(_get(state, "alertText1", "") or ""),
        "alertText2": str(_get(state, "alertText2", "") or ""),
    }


def build_gps(messages: dict[str, Any]) -> dict[str, Any]:
    gps = _gps(messages)
    lat = _finite(_get(gps, "latitude", 0.0))
    lon = _finite(_get(gps, "longitude", 0.0))
    return {
        "latitude": lat,
        "longitude": lon,
        "speed": _finite(_get(gps, "speed", 0.0)),
        "bearingDeg": _finite(_get(gps, "bearingDeg", 0.0)),
        "bearingAccuracyDeg": 0.0,
        "speedAccuracy": 0.0,
        "hasFix": _has_fix(gps),
        "altitude": _finite(_get(gps, "altitude", 0.0)),
        "horizontalAccuracy": _finite(_get(gps, "horizontalAccuracy", 0.0)),
        "verticalAccuracy": _finite(_get(gps, "verticalAccuracy", 0.0)),
        "unixTimestampMillis": float(_int(_get(gps, "unixTimestampMillis", 0))),
    }


def _zero_xyz() -> dict[str, list[float]]:
    return {"x": [0.0], "y": [0.0], "z": [0.0]}


def build_lateral_plan(messages: dict[str, Any]) -> dict[str, Any]:
    # This fork no longer publishes a LateralPlan service; modeld puts the lane-change
    # state into modelV2.meta, which is what the app's compact card needs.
    model = messages.get("modelV2")
    meta = _get(model, "meta", None)
    return {
        "useLaneLines": model is not None,
        "latDebugText": "",
        "position": _zero_xyz(),
        "distances": [],
        "laneChangeState": _enum_index(_get(meta, "laneChangeState", "off"),
                                       LANE_CHANGE_STATE),
        "laneChangeDirection": _enum_index(_get(meta, "laneChangeDirection", "none"),
                                           LANE_CHANGE_DIRECTION),
    }


def build_carrot_man(messages: dict[str, Any]) -> dict[str, Any]:
    cs = messages.get("carState")
    gps = _gps(messages)
    live = messages.get("liveMapDataSP")
    nav = messages.get("navInstruction")
    road_limit = _finite(_get(live, "speedLimit", 0.0)) * 3.6
    road_limit_valid = bool(_get(live, "speedLimitValid", False))
    desired_speed = _finite(_get(cs, "vCruise", 0.0)) * 3.6
    return {
        "activeCarrot": 0,
        "nRoadLimitSpeed": _int(round(road_limit)) if road_limit_valid else 0,
        "xSpdType": 0,
        "xSpdLimit": _int(round(road_limit)) if road_limit_valid else 0,
        "xSpdDist": _int(_get(live, "speedLimitAheadDistance", 0.0))
        if _get(live, "speedLimitAheadValid", False) else 0,
        "xSpdCountDown": 0,
        "xTurnInfo": 0,
        "xDistToTurn": _int(_get(nav, "maneuverDistance", 0.0)),
        "xTurnCountDown": 0,
        "atcType": "",
        "szPosRoadName": _get(live, "roadName", ""),
        "szTBTMainText": _get(nav, "maneuverPrimaryText", ""),
        "desiredSpeed": _int(round(desired_speed)),
        "xPosLat": _finite(_get(gps, "latitude", 0.0)),
        "xPosLon": _finite(_get(gps, "longitude", 0.0)),
        "xPosAngle": _finite(_get(gps, "bearingDeg", 0.0)),
        "xPosSpeed": _finite(_get(gps, "speed", 0.0)) * 3.6,
        "trafficState": 0,
        "nGoPosDist": 0,
        "nGoPosTime": 0,
        "szSdiDescr": "",
        "naviPaths": "",
        "desiredSource": "shim" if nav is not None else "",
        "vehicleNaviActive": nav is not None,
        "vehicleNaviSpeed": _int(round(road_limit)) if road_limit_valid else 0,
        "vehicleNaviSectionActive": False,
        "vehicleNaviAvailable": nav is not None,
    }


def _zero_guidance(present: bool = False, nav=None) -> dict[str, Any]:
    return {
        "present": present,
        "distanceM": _int(_get(nav, "maneuverDistance", 0.0)),
        "timeSec": 0,
        "turnType": 0,
        "roadName": "",
        "mainText": _get(nav, "maneuverPrimaryText", "") if present else "",
        "pointValid": False,
        "latitude": 0.0,
        "longitude": 0.0,
    }


def _zero_lane(present: bool = False) -> dict[str, Any]:
    return {
        "present": present,
        "count": 0,
        "distanceM": 0,
        "visible": False,
        "available": [],
    }


def _zero_speed(live=None) -> dict[str, Any]:
    valid = bool(_get(live, "speedLimitValid", False))
    limit = _finite(_get(live, "speedLimit", 0.0)) * 3.6
    return {
        "roadLimitValid": valid,
        "roadLimitKph": _int(round(limit)) if valid else 0,
        "sdiPresent": False, "sdiType": 0, "sdiDistanceM": 0, "sdiSpeedLimitKph": 0,
        "sdiSectionType": 0, "sdiBlockType": 0, "sdiBlockSpeedKph": 0,
        "sdiBlockDistanceM": 0,
        "secondarySdiPresent": False, "secondarySdiType": 0,
        "secondarySdiDistanceM": 0, "secondarySdiSpeedLimitKph": 0,
        "secondarySdiSectionType": 0, "secondarySdiBlockType": 0,
        "secondarySdiBlockSpeedKph": 0, "secondarySdiBlockDistanceM": 0,
        "sectionPresent": False, "sectionActive": False, "sectionSpeedLimitKph": 0,
        "sectionAverageKph": 0.0, "sectionOverallAverageKph": 0.0,
        "sectionRemainingDistanceM": 0.0, "sectionRemainingTimeSec": 0,
        "sectionProgress": 0.0, "sectionSuspended": False, "sectionOffRoute": False,
    }


def build_carrot_navi(messages: dict[str, Any]) -> dict[str, Any]:
    gps = _gps(messages)
    live = messages.get("liveMapDataSP")
    nav = messages.get("navInstruction")
    present = nav is not None
    return {
        "schemaVersion": 2,
        "generation": 1,
        "sessionId": "shim",
        "publishMonoTimeNanos": time.monotonic_ns(),
        "connected": True,
        "vehicle": {
            "present": _has_fix(gps),
            "latitude": _finite(_get(gps, "latitude", 0.0)),
            "longitude": _finite(_get(gps, "longitude", 0.0)),
            "headingDeg": _finite(_get(gps, "bearingDeg", 0.0)),
            "speedKph": _finite(_get(gps, "speed", 0.0)) * 3.6,
            "roadName": _get(live, "roadName", ""),
        },
        "guidanceCurrent": _zero_guidance(present, nav),
        "guidanceNext": _zero_guidance(False),
        "laneCurrent": _zero_lane(False),
        "laneAhead": [],
        "speed": _zero_speed(live),
        "trafficSignal": {
            "visible": False, "distanceM": 0,
            "redValid": False, "redOn": False, "redRemainSec": 0,
            "leftValid": False, "leftOn": False, "leftRemainSec": 0,
            "greenValid": False, "greenOn": False, "greenRemainSec": 0,
            "rightValid": False, "rightOn": False, "rightRemainSec": 0,
            "uturnValid": False, "uturnOn": False, "uturnRemainSec": 0,
            "uiCounterValid": False, "uiCounterRemainSec": 0,
        },
        "crossroad": {"visible": False, "distanceM": 0, "imageCode": 0},
        "route": {
            "present": False, "remainingDistanceM": 0, "remainingTimeSec": 0,
            "movedDistanceM": 0, "totalDistanceM": 0, "polyline": [],
        },
        "navigationStatus": {
            "guidanceActive": present,
            "offRoute": False,
            "routePresent": present,
        },
    }


BUILDERS = {
    "carState": build_car_state,
    "controlsState": build_controls_state,
    "carControl": build_car_control,
    "modelV2": build_model_v2,
    "longitudinalPlan": build_longitudinal_plan,
    "radarState": build_radar_state,
    "deviceState": build_device_state,
    "selfdriveState": build_selfdrive_state,
    "gpsLocationExternal": build_gps,
    "lateralPlan": build_lateral_plan,
    "carrotMan": build_carrot_man,
    "carrotNavi": build_carrot_navi,
}


# Services that are derived from other messages instead of having their own cereal
# service.  The tuple is the set of messages that must be present (any one of them).
DERIVED_REQUIREMENTS = {
    "carrotMan": ("carState",),
    "carrotNavi": ("gpsLocationExternal", "gpsLocation", "liveMapDataSP", "navInstruction"),
    "lateralPlan": ("modelV2",),
}


def build_snapshot(messages: dict[str, Any]) -> dict[str, dict[str, Any]]:
    snapshot: dict[str, dict[str, Any]] = {}
    for name, builder in BUILDERS.items():
        if name not in messages:
            required = DERIVED_REQUIREMENTS.get(name)
            if required is None or not any(key in messages for key in required):
                continue
        try:
            snapshot[name] = builder(messages)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("build %s failed: %s", name, exc)
    return snapshot


class CompactSource:
    """Owns the cereal SubMaster thread and publishes immutable snapshots."""

    def __init__(self, services: list[str] | None = None) -> None:
        self.services = list(services or SOURCE_SERVICES)
        self._lock = threading.Lock()
        self._snapshot: dict[str, dict[str, Any]] = {}
        self._updated_mono = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sm = None
        self.available = False
        self.last_error = ""

    def start(self) -> bool:
        try:
            from openpilot.cereal import messaging
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"import cereal failed: {exc}"
            LOG.warning("compact source unavailable: %s", self.last_error)
            return False
        try:
            self._sm = messaging.SubMaster(
                self.services,
                ignore_alive=self.services,
                ignore_avg_freq=self.services,
            )
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"SubMaster failed: {exc}"
            LOG.warning("compact source unavailable: %s", self.last_error)
            return False
        self.available = True
        self._thread = threading.Thread(target=self._run, name="compact-source", daemon=True)
        self._thread.start()
        LOG.info("compact source started: %s", ",".join(self.services))
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _run(self) -> None:
        assert self._sm is not None
        empty_ticks = 0
        while not self._stop.is_set():
            try:
                self._sm.update(0)
                # Keep supporting services (carStateSP/selfdriveState) in the message
                # dict; build_snapshot only emits the ones with a compact builder.
                messages = {
                    name: self._sm[name]
                    for name in self.services
                    if self._sm.valid.get(name)
                }
                snapshot = build_snapshot(messages)
                with self._lock:
                    self._snapshot = snapshot
                    self._updated_mono = time.monotonic()
                if not snapshot:
                    empty_ticks += 1
                    if empty_ticks % 600 == 0:
                        LOG.info("compact source waiting: no fresh cereal services")
                else:
                    empty_ticks = 0
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
                LOG.warning("compact source update failed: %s", exc)
                time.sleep(0.5)
            time.sleep(0.01)

    def snapshot(self) -> tuple[dict[str, dict[str, Any]], float]:
        with self._lock:
            return dict(self._snapshot), self._updated_mono
