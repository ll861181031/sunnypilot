#!/usr/bin/env python3
"""Fake car for the CPlink bench: cereal services + CAN 0x133/0x2B4.

It publishes only into the process's msgq namespace.  ``shim.py --bench`` sets
``OPENPILOT_PREFIX=cplink-bench`` before starting this, so the real stack and the
production shim never see these messages.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time

from bench_state import BenchState

LOG = logging.getLogger("cplink_shim.bench.car")

SERVICES = [
    "carState", "carStateSP", "selfdriveState", "controlsState", "carControl",
    "modelV2", "longitudinalPlan", "radarState", "deviceState", "gpsLocationExternal",
    "can",
]


def _set_bits(data: bytearray, start: int, width: int, value: int) -> None:
    for index in range(width):
        bit = start + index
        byte = bit // 8
        if byte >= len(data):
            return
        mask = 1 << (bit % 8)
        if (value >> index) & 1:
            data[byte] |= mask
        else:
            data[byte] &= ~mask


def stalks_frame(state: dict, counter: int) -> bytes:
    data = bytearray(8)
    data[0] = 0x01
    data[1] = 0x31
    if state.get("left_blinker"):
        data[0] |= 1 << 4
    if state.get("right_blinker"):
        data[0] |= 1 << 5
    data[3] = (int(state.get("can_light_mode", 1)) & 0x3) << 6
    data[4] = (int(state.get("can_turn_switch", 1)) & 0x7) << 4
    data[6] = counter & 0x0F
    data[7] = (0xFF - sum(data[:7])) & 0xFF
    return bytes(data)


def tla_frame(state: dict) -> bytes:
    raw = int(state.get("can_tla_raw", 0)) & 0x7
    data = bytearray(8)
    # Stock 0x2B4 has other content; only the TLA fields matter to the bench decoder.
    _set_bits(data, 34, 3, raw)  # left
    _set_bits(data, 37, 3, raw)  # straight
    _set_bits(data, 40, 3, raw)  # right
    return bytes(data)


class BenchCar:
    def __init__(self, state: BenchState) -> None:
        self.state = state
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pm = None
        self.frame_id = 0
        self.counter = 0
        self.published = 0
        self.available = False
        self.last_error = ""

    def start(self) -> bool:
        try:
            from openpilot.cereal import messaging
            self._messaging = messaging
            self._pm = messaging.PubMaster(SERVICES)
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"msgq unavailable: {exc}"
            LOG.warning("bench car unavailable: %s", self.last_error)
            return False
        self.available = True
        self._thread = threading.Thread(target=self._run, name="bench-car", daemon=True)
        self._thread.start()
        LOG.info("bench car started: %s", ",".join(SERVICES))
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _send(self, service: str, msg) -> None:
        self._pm.send(service, msg)
        self.published += 1

    def _run(self) -> None:
        tick = 0
        while not self._stop.is_set():
            try:
                s = self.state.snapshot()
                self._publish_fast(s)
                if tick % 10 == 0:
                    self._publish_slow(s)
                if tick % 2 == 0:
                    self._publish_plans(s)
                if tick % 5 == 0:
                    self._publish_gps(s)
                self.frame_id += 1
                self.counter = (self.counter + 1) & 0x0F
                self.published += 1
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
                LOG.warning("bench car publish failed: %s", exc)
                time.sleep(0.5)
            tick += 1
            time.sleep(0.05)

    # ── messages ──
    def _publish_fast(self, s: dict) -> None:
        m = self._messaging
        v_ego = float(s["speed_kph"]) / 3.6
        cruise = float(s["cruise_kph"]) / 3.6

        cs = m.new_message("carState", valid=True)
        c = cs.carState
        c.vEgo = v_ego
        c.vEgoRaw = v_ego
        c.vEgoCluster = v_ego
        c.aEgo = float(s["accel_mps2"])
        c.vCruise = cruise
        c.vCruiseCluster = cruise
        c.steeringAngleDeg = float(s["steering_deg"])
        c.steeringRateDeg = 0.0
        c.steeringTorque = 0.0
        c.steeringPressed = False
        c.gasPressed = False
        c.brakePressed = False
        c.standstill = v_ego < 0.1
        c.gearShifter = str(s["gear"])
        c.leftBlinker = bool(s["left_blinker"])
        c.rightBlinker = bool(s["right_blinker"])
        c.leftBlindspot = bool(s["left_blindspot"])
        c.rightBlindspot = bool(s["right_blindspot"])
        c.fuelGauge = float(s["fuel"])
        c.brakeHoldActive = v_ego < 0.1 and bool(s["enabled"])
        c.cruiseState.enabled = bool(s["enabled"])
        c.cruiseState.available = True
        c.cruiseState.speed = cruise
        c.cruiseState.speedCluster = cruise
        self._send("carState", cs)

        sp = m.new_message("carStateSP", valid=True)
        sp.carStateSP.leftLaneLine = 0
        sp.carStateSP.rightLaneLine = 0
        sp.carStateSP.laneLineTypesValid = True
        sp.carStateSP.blinkerLatched = False
        self._send("carStateSP", sp)

        controls = m.new_message("controlsState", valid=True)
        controls.controlsState.curvature = float(s["steering_deg"]) * 0.0005
        controls.controlsState.desiredCurvature = float(s["steering_deg"]) * 0.0005
        self._send("controlsState", controls)

        cc = m.new_message("carControl", valid=True)
        cc.carControl.enabled = bool(s["enabled"])
        cc.carControl.latActive = bool(s["enabled"])
        cc.carControl.longActive = bool(s["enabled"])
        cc.carControl.actuators.steeringAngleDeg = float(s["steering_deg"])
        cc.carControl.actuators.accel = float(s["accel_mps2"])
        cc.carControl.actuators.curvature = float(s["steering_deg"]) * 0.0005
        self._send("carControl", cc)

        model = m.new_message("modelV2", valid=True)
        mv = model.modelV2
        mv.frameId = self.frame_id
        mv.frameIdExtra = self.frame_id
        mv.position.x = [0.0]
        mv.position.y = [0.0]
        mv.position.z = [0.0]
        mv.velocity.x = [v_ego]
        lanes = max(1, min(5, int(s["lane_count"])))
        width = float(s["lane_width_m"])
        half = lanes * width / 2.0
        mv.laneLines = [
            {"x": [0.0, 20.0], "y": [y, y], "z": [0.0, 0.0]}
            for y in (-half + i * width for i in range(lanes + 1))
        ]
        mv.laneLineProbs = [0.9] * (lanes + 1)
        mv.laneLineStds = [0.05] * (lanes + 1)
        mv.roadEdges = [
            {"x": [0.0, 20.0], "y": [-half - width, -half - width], "z": [0.0, 0.0]},
            {"x": [0.0, 20.0], "y": [half + width, half + width], "z": [0.0, 0.0]},
        ]
        mv.roadEdgeStds = [0.1, 0.1]
        lead = float(s["lead_distance_m"])
        if lead > 0:
            mv.leadsV3 = [{
                "prob": 0.8,
                "x": [lead],
                "y": [0.0],
                "v": [float(s["lead_speed_mps"])],
            }]
        else:
            mv.leadsV3 = []
        mv.frameAge = 0
        mv.frameDropPerc = 0.0
        mv.modelExecutionTime = 10.0
        mv.meta.laneChangeState = str(s["lane_change_state"])
        mv.meta.laneChangeDirection = str(s["lane_change_direction"])
        self._send("modelV2", model)

        # "can" is a List(CanData) service: new_message needs an explicit size.
        can = m.new_message("can", 2, valid=True)
        can.can[0].address = 0x133
        can.can[0].src = 0
        can.can[0].dat = stalks_frame(s, self.counter)
        can.can[1].address = 0x2B4
        can.can[1].src = 2
        can.can[1].dat = tla_frame(s)
        self._send("can", can)

    def _publish_plans(self, s: dict) -> None:
        m = self._messaging
        v_ego = float(s["speed_kph"]) / 3.6
        plan = m.new_message("longitudinalPlan", valid=True)
        plan.longitudinalPlan.accels = [float(s["accel_mps2"])]
        plan.longitudinalPlan.speeds = [v_ego]
        plan.longitudinalPlan.jerks = [0.0]
        plan.longitudinalPlan.longitudinalPlanSource = "cruise"
        self._send("longitudinalPlan", plan)

        radar = m.new_message("radarState", valid=True)
        lead = radar.radarState.leadOne
        distance = float(s["lead_distance_m"])
        lead.dRel = distance
        lead.yRel = 0.0
        lead.vRel = float(s["lead_speed_mps"]) - v_ego
        lead.vLead = float(s["lead_speed_mps"])
        lead.present = distance > 0
        lead.radar = True
        lead.radarTrackId = 1
        self._send("radarState", radar)

    def _publish_slow(self, s: dict) -> None:
        m = self._messaging
        state = m.new_message("selfdriveState", valid=True)
        sd = state.selfdriveState
        sd.enabled = bool(s["enabled"])
        sd.active = bool(s["enabled"])
        sd.state = "enabled" if s["enabled"] else "disabled"
        sd.personality = "standard"
        sd.alertStatus = "normal"
        sd.alertSize = "none"
        sd.alertType = ""
        self._send("selfdriveState", state)

        device = m.new_message("deviceState", valid=True)
        d = device.deviceState
        d.deviceType = "pc"
        d.memoryUsagePercent = 42
        d.freeSpacePercent = 80.0
        d.cpuTempC = [45.0, 50.0]
        d.started = bool(s["started"])
        self._send("deviceState", device)

    def _publish_gps(self, s: dict) -> None:
        m = self._messaging
        gps = m.new_message("gpsLocationExternal", valid=True)
        g = gps.gpsLocationExternal
        g.latitude = float(s["gps_lat"])
        g.longitude = float(s["gps_lon"])
        g.altitude = 10.0
        g.speed = float(s["speed_kph"]) / 3.6
        g.bearingDeg = float(s["gps_bearing"])
        g.horizontalAccuracy = 2.0 if s["gps_fix"] else 125.0
        g.verticalAccuracy = 5.0
        g.bearingAccuracyDeg = 1.0
        g.speedAccuracy = 0.2
        g.hasFix = bool(s["gps_fix"])
        g.satelliteCount = 12 if s["gps_fix"] else 0
        g.unixTimestampMillis = int(time.time() * 1000)
        self._send("gpsLocationExternal", gps)



def _stack_running() -> tuple[bool, str]:
    """Refuse to inject fake cereal while the real manager/pandad is running."""
    try:
        pids = os.listdir("/proc")
    except OSError:
        return False, ""
    for pid in pids:
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        if "manager.py" in cmd or "pandad" in cmd:
            return True, f"{pid}: {cmd[:160]}"
    return False, ""


def _shim_status() -> dict:
    import urllib.request
    try:
        with urllib.request.urlopen("http://127.0.0.1:7000/api/status", timeout=2) as resp:
            return json.loads(resp.read())
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def _port_up(port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _attach_payload(state: BenchState, car: BenchCar) -> dict:
    shim = _shim_status()
    compact = shim.get("compact", {}) if isinstance(shim, dict) else {}
    navi = shim.get("navi_state", {}) if isinstance(shim, dict) else {}
    lamp = shim.get("lamp", {}) if isinstance(shim, dict) else {}
    streams = navi.get("streams", {}) if isinstance(navi, dict) else {}
    fresh = [name for name, info in streams.items() if info.get("fresh")]
    return {
        "ok": True,
        "mode": "real-phone",
        "state": state.snapshot(),
        "car": {
            "available": car.available,
            "published": car.published,
            "error": car.last_error,
        },
        "app": {
            "compact": {
                "connected": int(compact.get("clients", 0)) > 0,
                "frames": compact.get("frames", 0),
                "seen": [name for name, seq in compact.get("sequence", {}).items() if seq],
                "last": {},
                "error": "",
            },
            "navi": {
                "connected": bool(fresh),
                "sent": 0,
                "streams": fresh,
                "error": "",
            },
            "lamp": {
                "connected": int(lamp.get("clients", 0)) > 0,
                "frames": lamp.get("sent", 0),
                "last": {"color": lamp.get("color")},
                "error": "",
            },
            "video": "webrtcd 5001 up (/stream road)" if _port_up(5001)
            else "webrtcd 5001 down",
        },
        "shim": {
            "compact": compact,
            "navi": {"streams": streams},
            "lamp": {"color": lamp.get("color")},
            "error": shim.get("error", "") if isinstance(shim, dict) else "",
        },
    }


def attach_main(argv: list[str] | None = None) -> int:
    import argparse
    import asyncio
    import json

    parser = argparse.ArgumentParser(description="CPlink bench: fake car only, real phone")
    parser.add_argument("--attach", action="store_true", help="attach to the default msgq (default)")
    parser.add_argument("--scenario", default="driving")
    parser.add_argument("--control-port", type=int, default=17001)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--force", action="store_true",
                        help="allow fake cereal while manager/pandad is running")
    args = parser.parse_args(argv)

    running, detail = _stack_running()
    if running and not args.force:
        print("real stack is running; refusing to inject fake carState:\n  " + detail)
        print("stop the stack first, or pass --force if you know what you are doing.")
        return 2

    # The production shim has no prefix; fake cereal must land in the same namespace.
    os.environ.pop("OPENPILOT_PREFIX", None)
    state = BenchState()
    state.apply_scenario(args.scenario)
    car = BenchCar(state)
    if not car.start():
        print(f"bench car failed: {car.last_error}")
        return 2
    # Let the already-running production shim map the fake 0x2B4 without a restart.
    from lamp7711 import TLA_MAP_PATH
    try:
        TLA_MAP_PATH.write_text(json.dumps(
            {"confirmed": True, "red": 1, "green": 2, "yellow": 3}), encoding="utf-8")
    except OSError:
        pass

    from aiohttp import web

    async def index(_request):
        from bench_page import INDEX_HTML
        return web.Response(text=INDEX_HTML, content_type="text/html")

    async def get_state(_request):
        return web.json_response(_attach_payload(state, car))

    async def post_set(request):
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("body must be an object")
            state.update(payload)
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        return web.json_response({"ok": True, "state": state.snapshot()})

    async def post_scenario(request):
        try:
            payload = await request.json()
            name = payload.get("name")
            if not isinstance(name, str):
                raise ValueError("name required")
            state.apply_scenario(name)
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        return web.json_response({"ok": True, "state": state.snapshot()})

    app = web.Application()
    app.router.add_get("/bench", index)
    app.router.add_get("/api/bench/state", get_state)
    app.router.add_post("/api/bench/set", post_set)
    app.router.add_post("/api/bench/scenario", post_scenario)
    print(f"fake car attached to the real shim; control page on "
          f"http://{args.host}:{args.control_port}/bench", flush=True)
    try:
        web.run_app(app, host=args.host, port=args.control_port)
    finally:
        car.stop()
        try:
            TLA_MAP_PATH.unlink()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(attach_main())
