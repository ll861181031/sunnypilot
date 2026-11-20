#!/usr/bin/env python3
"""CPlink compact telemetry codec: CVS1 / CVB1.

The wire format is defined by the CPlink app's ``CompactStateDecoder.kt``, not by
cereal.  Every frame is::

    b"CVS1" | u8 service_id | u8 flags(0) | u16 sequence | fields...

A batch is::

    b"CVB1" | u16 count | (u32 length | CVS1 frame)*

All integers are little-endian.  This module is deliberately dependency-free so the
shim can import it even when cereal/msgq is unavailable, and so tests can round-trip
frames without a running stack.
"""
from __future__ import annotations

import math
import struct

SERVICE_IDS = {
    "carState": 1,
    "controlsState": 2,
    "deviceState": 3,
    "carrotMan": 5,
    "selfdriveState": 6,
    "gpsLocationExternal": 7,
    "longitudinalPlan": 8,
    "modelV2": 9,
    "lateralPlan": 12,
    "radarState": 13,
    "carControl": 14,
    "carrotNavi": 21,
}
SERVICE_NAMES = {v: k for k, v in SERVICE_IDS.items()}
ALL_SERVICES = list(SERVICE_IDS)

# Struct definitions, in wire order.
STRUCTS: dict[str, list[tuple[str, str]]] = {
    "xyz": [("x", "ucm"), ("y", "mm"), ("z", "mm")],
    "velocity": [("x", "cm")],
    "modelLead": [("prob", "f"), ("x", "ucm"), ("y", "first"), ("v", "first")],
    "tpms": [("fl", "f"), ("fr", "f"), ("rl", "f"), ("rr", "f")],
    "radarLead": [
        ("dRel", "f"), ("yRel", "f"), ("vRel", "f"), ("aRel", "f"), ("vLead", "f"),
        ("aLead", "f"), ("dPath", "f"), ("vLat", "f"), ("vLeadK", "f"), ("aLeadK", "f"),
        ("fcw", "b"), ("status", "b"), ("aLeadTau", "f"), ("modelProb", "f"),
        ("radar", "b"), ("radarTrackId", "i"), ("jLead", "f"), ("score", "f"),
    ],
    "actuators": [("steeringAngleDeg", "f"), ("accel", "f"), ("curvature", "f")],
    "vehicle": [
        ("present", "b"), ("latitude", "d"), ("longitude", "d"),
        ("headingDeg", "f"), ("speedKph", "f"), ("roadName", "t"),
    ],
    "guidance": [
        ("present", "b"), ("distanceM", "i"), ("timeSec", "i"), ("turnType", "i"),
        ("roadName", "t"), ("mainText", "t"), ("pointValid", "b"),
        ("latitude", "d"), ("longitude", "d"),
    ],
    "lane": [
        ("present", "b"), ("count", "h"), ("distanceM", "i"),
        ("visible", "b"), ("available", "hs"),
    ],
    "speed": [
        ("roadLimitValid", "b"), ("roadLimitKph", "h"),
        ("sdiPresent", "b"), ("sdiType", "i"), ("sdiDistanceM", "i"),
        ("sdiSpeedLimitKph", "h"), ("sdiSectionType", "i"),
        ("sdiBlockType", "i"), ("sdiBlockSpeedKph", "h"), ("sdiBlockDistanceM", "i"),
        ("secondarySdiPresent", "b"), ("secondarySdiType", "i"),
        ("secondarySdiDistanceM", "i"), ("secondarySdiSpeedLimitKph", "h"),
        ("secondarySdiSectionType", "i"), ("secondarySdiBlockType", "i"),
        ("secondarySdiBlockSpeedKph", "h"), ("secondarySdiBlockDistanceM", "i"),
        ("sectionPresent", "b"), ("sectionActive", "b"),
        ("sectionSpeedLimitKph", "h"), ("sectionAverageKph", "f"),
        ("sectionOverallAverageKph", "f"), ("sectionRemainingDistanceM", "f"),
        ("sectionRemainingTimeSec", "i"), ("sectionProgress", "f"),
        ("sectionSuspended", "b"), ("sectionOffRoute", "b"),
    ],
    "signal": [
        ("visible", "b"), ("distanceM", "i"),
        ("redValid", "b"), ("redOn", "b"), ("redRemainSec", "h"),
        ("leftValid", "b"), ("leftOn", "b"), ("leftRemainSec", "h"),
        ("greenValid", "b"), ("greenOn", "b"), ("greenRemainSec", "h"),
        ("rightValid", "b"), ("rightOn", "b"), ("rightRemainSec", "h"),
        ("uturnValid", "b"), ("uturnOn", "b"), ("uturnRemainSec", "h"),
        ("uiCounterValid", "b"), ("uiCounterRemainSec", "h"),
    ],
    "crossroad": [("visible", "b"), ("distanceM", "i"), ("imageCode", "i")],
    "route": [
        ("present", "b"), ("remainingDistanceM", "i"), ("remainingTimeSec", "i"),
        ("movedDistanceM", "i"), ("totalDistanceM", "i"), ("polyline", "coords"),
    ],
    "status": [
        ("guidanceActive", "b"), ("offRoute", "b"), ("routePresent", "b"),
    ],
}

GEAR_SHIFTER = ["unknown", "park", "drive", "neutral", "reverse", "sport", "low",
                "brake", "eco", "manumatic"]
PERSONALITY = ["aggressive", "standard", "relaxed", "moreRelaxed"]
ALERT_STATUS = ["normal", "userPrompt", "critical"]
ALERT_SIZE = ["none", "small", "mid", "full"]
DEVICE_TYPE = ["unknown", "neo", "chffrAndroid", "chffrIos", "tici", "pc", "tizi", "mici"]
LANE_CHANGE_STATE = ["off", "preLaneChange", "laneChangeStarting", "laneChangeFinishing"]
LANE_CHANGE_DIRECTION = ["none", "left", "right"]
PLAN_SOURCE = ["cruise", "lead0", "lead1", "lead2", "e2e"]


def _enum(name: str, values: list[str]) -> str:
    return "e_" + ",".join(values)


SCHEMAS: dict[int, list[tuple[str, str]]] = {
    1: [
        ("vEgo", "f"), ("aEgo", "f"), ("vEgoCluster", "f"), ("vCruiseCluster", "f"),
        ("steeringAngleDeg", "f"), ("brakeHoldActive", "b"), ("softHoldActive", "h"),
        ("carrotCruise", "h"), ("gearStep", "h"), ("useLaneLineSpeed", "f"),
        ("brakeLights", "b"), ("leftBlindspot", "b"), ("rightBlindspot", "b"),
        ("leftLaneLine", "h"), ("rightLaneLine", "h"),
        ("gearShifter", _enum("gearShifter", GEAR_SHIFTER)),
        ("leftBlinker", "b"), ("rightBlinker", "b"), ("fuelGauge", "f"),
        ("ureaGauge", "f"), ("tpms", "s_tpms"), ("evModeValid", "b"),
        ("evModeActive", "b"),
        ("headlight", "b"), ("highBeam", "b"), ("autoHeadlight", "b"),
        ("parkingBrake", "b"),
        ("doorFrontLeft", "b"), ("doorFrontRight", "b"),
        ("doorRearLeft", "b"), ("doorRearRight", "b"),
        ("doorOpen", "b"), ("tailgateOpen", "b"),
    ],
    2: [
        ("enabled", "b"), ("vCruiseCluster", "f"), ("activeLaneLine", "b"),
        ("curvature", "f"), ("desiredCurvature", "f"), ("actualLateralAccel", "f"),
        ("desiredLateralAccel", "f"), ("lateralOutput", "f"),
    ],
    3: [
        ("memoryUsagePercent", "a"), ("freeSpacePercent", "f"),
        ("cpuTempC", "fs"), ("deviceType", _enum("deviceType", DEVICE_TYPE)),
        ("started", "b"),
    ],
    5: [
        ("activeCarrot", "i"), ("nRoadLimitSpeed", "i"), ("xSpdType", "i"),
        ("xSpdLimit", "i"), ("xSpdDist", "i"), ("xSpdCountDown", "i"),
        ("xTurnInfo", "i"), ("xDistToTurn", "i"), ("xTurnCountDown", "i"),
        ("atcType", "t"), ("szPosRoadName", "t"), ("szTBTMainText", "t"),
        ("desiredSpeed", "i"), ("xPosLat", "f"), ("xPosLon", "f"),
        ("xPosAngle", "f"), ("xPosSpeed", "f"), ("trafficState", "i"),
        ("nGoPosDist", "i"), ("nGoPosTime", "i"), ("szSdiDescr", "t"),
        ("naviPaths", "t"), ("desiredSource", "t"), ("vehicleNaviActive", "b"),
        ("vehicleNaviSpeed", "i"), ("vehicleNaviSectionActive", "b"),
        ("vehicleNaviAvailable", "b"),
    ],
    6: [
        ("enabled", "b"), ("personality", _enum("personality", PERSONALITY)),
        ("alertStatus", _enum("alertStatus", ALERT_STATUS)),
        ("alertSize", _enum("alertSize", ALERT_SIZE)),
        ("alertType", "t"), ("alertText1", "t"), ("alertText2", "t"),
    ],
    7: [
        ("latitude", "d"), ("longitude", "d"), ("speed", "f"),
        ("bearingDeg", "f"), ("bearingAccuracyDeg", "f"), ("speedAccuracy", "f"),
        ("hasFix", "b"), ("altitude", "d"), ("horizontalAccuracy", "f"),
        ("verticalAccuracy", "f"), ("unixTimestampMillis", "d"),
    ],
    8: [
        ("accels", "first"), ("speeds", "first"), ("jerks", "first"),
        ("tFollow", "f"), ("desiredDistance", "f"), ("myDrivingMode", "i"),
        ("xState", "i"), ("trafficState", "i"),
        ("longitudinalPlanSource", _enum("longitudinalPlanSource", PLAN_SOURCE)),
        ("cruiseTarget", "f"),
    ],
    9: [
        ("frameId", "u"), ("frameIdExtra", "u"), ("position", "s_xyz"),
        ("velocity", "s_velocity"), ("laneLines", "l_xyz"),
        ("laneLineProbs", "fs"), ("roadEdges", "l_xyz"),
        ("roadEdgeStds", "fs"), ("leadsV3", "l_modelLead"),
        ("laneLineStds", "fs"), ("frameAge", "i"), ("frameDropPerc", "f"),
        ("modelExecutionTime", "f"),
    ],
    12: [
        ("useLaneLines", "b"), ("latDebugText", "t"), ("position", "s_xyz"),
        ("distances", "fs"),
        ("laneChangeState", _enum("laneChangeState", LANE_CHANGE_STATE)),
        ("laneChangeDirection", _enum("laneChangeDirection", LANE_CHANGE_DIRECTION)),
    ],
    13: [
        ("leadOne", "s_radarLead"), ("leadTwo", "s_radarLead"),
        ("leadRight", "s_radarLead"), ("leadLeft", "s_radarLead"),
        ("leadsLeft", "l_radarLead"), ("leadsCenter", "l_radarLead"),
        ("leadsRight", "l_radarLead"),
    ],
    14: [
        ("latActive", "b"), ("longActive", "b"), ("actuators", "s_actuators"),
    ],
    21: [
        ("schemaVersion", "H"), ("generation", "q"), ("sessionId", "t"),
        ("publishMonoTimeNanos", "q"), ("connected", "b"),
        ("vehicle", "s_vehicle"), ("guidanceCurrent", "s_guidance"),
        ("guidanceNext", "s_guidance"), ("laneCurrent", "s_lane"),
        ("laneAhead", "l_lane"), ("speed", "s_speed"),
        ("trafficSignal", "s_signal"), ("crossroad", "s_crossroad"),
        ("route", "s_route"), ("navigationStatus", "s_status"),
    ],
}  # type: ignore[assignment]


def _get(obj, name: str, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _finite(value, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _enum_index(value, names: list[str]) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    text = str(value)
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    try:
        return names.index(text)
    except ValueError:
        return 0


def _text_bytes(value) -> bytes:
    raw = str(value if value is not None else "").encode("utf-8", "replace")
    if len(raw) <= 0xFFFF:
        return raw
    raw = raw[:0xFFFF]
    # Do not cut a multi-byte character in half.
    while raw and (raw[-1] & 0xC0) == 0x80:
        raw = raw[:-1]
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        raw = raw[:-1]
    return raw


def _pack_u16_length(payload: bytes) -> bytes:
    return struct.pack("<H", len(payload)) + payload


def _encode_scaled(value, scale: int, signed: bool) -> bytes:
    number = _finite(value)
    scaled = int(round(number * scale))
    if signed:
        scaled = max(-32768, min(32767, scaled))
        return struct.pack("<h", scaled)
    scaled = max(0, min(0xFFFF, scaled))
    return struct.pack("<H", scaled)


def _encode_list(typ: str, values) -> bytes:
    if values is None:
        values = []
    items = list(values)
    if typ == "first":
        items = items[:1]
        out = struct.pack("<H", len(items))
        for item in items:
            out += struct.pack("<f", _finite(item))
        return out
    if typ == "fs":
        out = struct.pack("<H", len(items))
        for item in items:
            out += struct.pack("<f", _finite(item))
        return out
    if typ == "hs":
        out = struct.pack("<H", len(items))
        for item in items:
            out += struct.pack("<h", max(-32768, min(32767, _int(item))))
        return out
    if typ == "ucm":
        out = struct.pack("<H", len(items))
        for item in items:
            out += _encode_scaled(item, 100, False)
        return out
    if typ == "cm":
        out = struct.pack("<H", len(items))
        for item in items:
            out += _encode_scaled(item, 100, True)
        return out
    if typ == "mm":
        out = struct.pack("<H", len(items))
        for item in items:
            out += _encode_scaled(item, 1000, True)
        return out
    raise ValueError(f"unsupported list type {typ}")


def _encode_coords(values) -> bytes:
    items = list(values or [])[:64]
    if not items:
        return b"\x00"
    first = items[0]
    lat = _finite(_get(first, "latitude", 0.0))
    lon = _finite(_get(first, "longitude", 0.0))
    out = bytearray([len(items)])
    out += struct.pack("<d", lat) + struct.pack("<d", lon)
    prev_lat, prev_lon = lat, lon
    for item in items[1:]:
        lat = _finite(_get(item, "latitude", prev_lat))
        lon = _finite(_get(item, "longitude", prev_lon))
        out += struct.pack("<f", lat - prev_lat) + struct.pack("<f", lon - prev_lon)
        prev_lat, prev_lon = lat, lon
    return bytes(out)


def encode_value(typ: str, value) -> bytes:
    if typ == "b":
        return b"\x01" if bool(value) else b"\x00"
    if typ == "a":
        return struct.pack("<b", max(-128, min(127, _int(value))))
    if typ == "h":
        return struct.pack("<h", max(-32768, min(32767, _int(value))))
    if typ == "H":
        return struct.pack("<H", max(0, min(0xFFFF, _int(value))))
    if typ == "i":
        return struct.pack("<i", max(-2147483648, min(2147483647, _int(value))))
    if typ == "u":
        return struct.pack("<I", max(0, min(0xFFFFFFFF, _int(value))))
    if typ == "q":
        return struct.pack("<Q", max(0, min(0xFFFFFFFFFFFFFFFF, _int(value))))
    if typ == "f":
        return struct.pack("<f", _finite(value))
    if typ == "d":
        return struct.pack("<d", _finite(value))
    if typ == "t":
        return _pack_u16_length(_text_bytes(value))
    if typ.startswith("e_"):
        names = typ[2:].split(",")
        return struct.pack("<B", max(0, min(255, _enum_index(value, names))))
    if typ.startswith("s_"):
        return encode_struct(typ[2:], value if isinstance(value, dict) else value)
    if typ.startswith("l_"):
        name = typ[2:]
        items = list(value or [])
        out = bytearray([min(255, len(items))])
        for item in items[:255]:
            out += encode_struct(name, item)
        return bytes(out)
    if typ == "coords":
        return _encode_coords(value)
    if typ in ("fs", "hs", "ucm", "cm", "mm", "first"):
        return _encode_list(typ, value)
    raise ValueError(f"unknown compact type {typ}")


def encode_struct(name: str, values) -> bytes:
    schema = STRUCTS[name]
    out = bytearray()
    for field, typ in schema:
        out += encode_value(typ, _get(values, field, None))
    return bytes(out)


def encode_frame(service: str, sequence: int, values) -> bytes:
    service_id = SERVICE_IDS[service]
    schema = SCHEMAS[service_id]
    out = bytearray(b"CVS1")
    out.append(service_id)
    out.append(0)  # flags
    out += struct.pack("<H", sequence & 0xFFFF)
    for field, typ in schema:
        out += encode_value(typ, _get(values, field, None))
    return bytes(out)


def encode_batch(frames: list[bytes]) -> bytes:
    out = bytearray(b"CVB1")
    out += struct.pack("<H", len(frames))
    for frame in frames:
        out += struct.pack("<I", len(frame)) + frame
    return bytes(out)


# ─────────────────────────────── test decoder ───────────────────────────────

class CompactDecodeError(ValueError):
    pass


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    @property
    def remaining(self) -> int:
        return len(self.data) - self.pos

    def need(self, size: int) -> None:
        if size < 0 or self.remaining < size:
            raise CompactDecodeError(f"truncated: need {size}, have {self.remaining}")

    def u8(self) -> int:
        self.need(1)
        value = self.data[self.pos]
        self.pos += 1
        return value

    def u16(self) -> int:
        self.need(2)
        value = struct.unpack_from("<H", self.data, self.pos)[0]
        self.pos += 2
        return value

    def u32(self) -> int:
        self.need(4)
        value = struct.unpack_from("<I", self.data, self.pos)[0]
        self.pos += 4
        return value

    def i32(self) -> int:
        self.need(4)
        value = struct.unpack_from("<i", self.data, self.pos)[0]
        self.pos += 4
        return value

    def i16(self) -> int:
        self.need(2)
        value = struct.unpack_from("<h", self.data, self.pos)[0]
        self.pos += 2
        return value

    def f32(self) -> float:
        self.need(4)
        value = struct.unpack_from("<f", self.data, self.pos)[0]
        self.pos += 4
        if not math.isfinite(value):
            raise CompactDecodeError("nonfinite float")
        return value

    def f64(self) -> float:
        self.need(8)
        value = struct.unpack_from("<d", self.data, self.pos)[0]
        self.pos += 8
        if not math.isfinite(value):
            raise CompactDecodeError("nonfinite double")
        return value

    def text(self) -> str:
        length = self.u16()
        self.need(length)
        raw = self.data[self.pos:self.pos + length]
        self.pos += length
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CompactDecodeError("invalid UTF-8") from exc

    def struct(self, name: str):
        return {field: self.value(typ) for field, typ in STRUCTS[name]}

    def list_of(self, typ: str, count: int):
        return [self.value(typ) for _ in range(count)]

    def value(self, typ: str):
        if typ == "b":
            value = self.u8()
            if value > 1:
                raise CompactDecodeError("invalid boolean")
            return value == 1
        if typ == "a":
            return struct.unpack("<b", bytes([self.u8()]))[0]
        if typ == "h":
            return self.i16()
        if typ == "H":
            return self.u16()
        if typ == "i":
            return self.i32()
        if typ == "u":
            return self.u32()
        if typ == "q":
            self.need(8)
            value = struct.unpack_from("<Q", self.data, self.pos)[0]
            self.pos += 8
            return value
        if typ == "f":
            return self.f32()
        if typ == "d":
            return self.f64()
        if typ == "t":
            return self.text()
        if typ == "fs":
            return self.list_of("f", self.u16())
        if typ == "hs":
            return self.list_of("h", self.u16())
        if typ == "first":
            return self.list_of("f", self.u16())
        if typ == "ucm":
            return [self.u16() / 100.0 for _ in range(self.u16())]
        if typ == "cm":
            return [self.i16() / 100.0 for _ in range(self.u16())]
        if typ == "mm":
            return [self.i16() / 1000.0 for _ in range(self.u16())]
        if typ == "coords":
            count = self.u8()
            if count == 0:
                return []
            lat, lon = self.f64(), self.f64()
            out = [{"latitude": lat, "longitude": lon}]
            for _ in range(1, count):
                lat += self.f32()
                lon += self.f32()
                out.append({"latitude": lat, "longitude": lon})
            return out
        if typ.startswith("e_"):
            names = typ[2:].split(",")
            index = self.u8()
            return names[index] if index < len(names) else ""
        if typ.startswith("s_"):
            return self.struct(typ[2:])
        if typ.startswith("l_"):
            count = self.u8()
            return [self.struct(typ[2:]) for _ in range(count)]
        raise CompactDecodeError(f"unknown type {typ}")


def decode_frame(reader: _Reader) -> dict:
    if reader.remaining < 8:
        raise CompactDecodeError("frame too short")
    magic = reader.data[reader.pos:reader.pos + 4]
    if magic != b"CVS1":
        raise CompactDecodeError("unsupported compact magic")
    reader.pos += 4
    service_id = reader.u8()
    flags = reader.u8()
    if flags != 0:
        raise CompactDecodeError("unsupported flags")
    sequence = reader.u16()
    schema = SCHEMAS.get(service_id)
    if schema is None:
        raise CompactDecodeError(f"unsupported service id {service_id}")
    values = {field: reader.value(typ) for field, typ in schema}
    return {"service": service_id, "name": SERVICE_NAMES[service_id],
            "sequence": sequence, "values": values}


def decode_frames(data: bytes) -> list[dict]:
    if len(data) < 4:
        raise CompactDecodeError("message too short")
    magic = data[:4]
    reader = _Reader(data)
    reader.pos = 4
    if magic == b"CVS1":
        reader.pos = 0
        return [decode_frame(reader)]
    if magic == b"CVB1":
        count = reader.u16()
        frames = []
        for _ in range(count):
            length = reader.u32()
            if length < 8 or length > reader.remaining:
                raise CompactDecodeError("invalid embedded frame length")
            inner = _Reader(data[reader.pos:reader.pos + length])
            reader.pos += length
            frames.append(decode_frame(inner))
        if reader.remaining:
            raise CompactDecodeError("trailing batch bytes")
        return frames
    raise CompactDecodeError("unsupported compact magic")
