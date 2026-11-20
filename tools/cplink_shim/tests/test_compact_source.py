from types import SimpleNamespace

import compact_codec as codec
import compact_source as source


class LateralState:
    def __init__(self, torque=None):
        self._torque = torque

    def which(self):
        return "torqueState" if self._torque is not None else "pidState"

    @property
    def torqueState(self):
        return self._torque


def car_state(**overrides):
    values = dict(
        vEgo=12.3, aEgo=-0.5, vEgoCluster=44.3, vCruiseCluster=16.6667, vCruise=16.6667,
        steeringAngleDeg=1.2, brakeHoldActive=False, brakeLights=False,
        leftBlindspot=True, rightBlindspot=False, leftBlinker=False, rightBlinker=True,
        fuelGauge=0.7, gearShifter=2,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def car_state_sp(**overrides):
    values = dict(leftLaneLine=-1, rightLaneLine=0,
                  headlight=True, highBeam=False, autoHeadlight=True,
                  parkingBrake=True, doorFrontLeft=True, doorFrontRight=False,
                  doorRearLeft=False, doorRearRight=True, brakeLights=True)
    values.update(overrides)
    return SimpleNamespace(**values)


def car_control(**overrides):
    values = dict(
        latActive=True, longActive=False,
        actuators=SimpleNamespace(steeringAngleDeg=2.5, accel=0.4, curvature=0.003),
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def model_v2(**overrides):
    values = dict(
        frameId=123, frameIdExtra=456,
        position=SimpleNamespace(x=1.2, y=-0.3, z=0.0),
        velocity=SimpleNamespace(x=12.0),
        laneLines=[SimpleNamespace(x=0.1, y=-1.5, z=0.0)],
        laneLineProbs=[0.9],
        roadEdges=[SimpleNamespace(x=0.2, y=3.5, z=0.0)],
        roadEdgeStds=[0.1],
        leadsV3=[SimpleNamespace(prob=0.5, x=[20.0], y=[0.1], v=[8.0])],
        laneLineStds=[0.01],
        frameAge=2, frameDropPerc=0.0, modelExecutionTime=10.0,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def radar_state(**overrides):
    values = dict(
        leadOne=SimpleNamespace(dRel=30.0, yRel=0.1, vRel=-2.0, vLead=8.0,
                                present=True, radar=True, radarTrackId=7),
        leadTwo=None,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_car_state_units_and_lane_fields():
    values = source.build_car_state({
        "carState": car_state(),
        "carStateSP": car_state_sp(),
    })
    assert values["vEgo"] == 12.3
    assert abs(values["vCruiseCluster"] - 60.0) < 1e-3  # km/h, not m/s
    assert values["gearShifter"] == 2  # drive
    assert values["leftLaneLine"] == -1
    assert values["rightLaneLine"] == 0
    assert values["leftBlindspot"] is True
    assert values["rightBlinker"] is True
    assert values["tpms"] == {"fl": 0.0, "fr": 0.0, "rl": 0.0, "rr": 0.0}
    assert values["headlight"] is True
    assert values["highBeam"] is False
    assert values["autoHeadlight"] is True
    assert values["parkingBrake"] is True
    assert values["doorFrontLeft"] is True
    assert values["doorFrontRight"] is False
    assert values["doorRearLeft"] is False
    assert values["doorRearRight"] is True
    assert values["brakeLights"] is True


def test_car_state_without_car_state_sp_defaults_lamp_telemetry_to_false():
    values = source.build_car_state({"carState": car_state()})
    assert values["headlight"] is False
    assert values["parkingBrake"] is False
    assert values["doorFrontLeft"] is False
    assert values["brakeLights"] is False
    assert values["doorOpen"] is False


def test_controls_state_uses_car_control_and_torque_state():
    controls = SimpleNamespace(
        enabled=True, curvature=0.01, desiredCurvature=0.02,
        lateralControlState=LateralState(SimpleNamespace(actualLateralAccel=0.1,
                                                         desiredLateralAccel=0.2)),
    )
    values = source.build_controls_state({
        "controlsState": controls,
        "carControl": car_control(),
        "carState": car_state(),
    })
    assert values["enabled"] is True
    assert abs(values["vCruiseCluster"] - 60.0) < 1e-3
    assert values["activeLaneLine"] is True
    assert values["curvature"] == 0.01
    assert values["actualLateralAccel"] == 0.1
    assert values["desiredLateralAccel"] == 0.2


def test_controls_state_enabled_comes_from_selfdrive_state():
    controls = SimpleNamespace(enabled=False, curvature=0.0, desiredCurvature=0.0,
                               lateralControlState=LateralState(None))
    values = source.build_controls_state({
        "controlsState": controls,
        "selfdriveState": SimpleNamespace(enabled=True),
        "carState": car_state(),
    })
    assert values["enabled"] is True


def test_model_v2_axes_are_lists_not_scalars():
    values = source.build_model_v2({"modelV2": model_v2()})
    assert values["position"] == {"x": [1.2], "y": [-0.3], "z": [0.0]}
    assert values["velocity"] == {"x": [12.0]}  # cm is a list too
    assert values["laneLines"][0]["x"] == [0.1]
    assert values["roadEdges"][0]["y"] == [3.5]
    assert values["leadsV3"][0]["x"] == [20.0]
    assert values["leadsV3"][0]["y"] == [0.1]
    assert values["leadsV3"][0]["v"] == [8.0]
    assert values["laneLineProbs"] == [0.9]


def test_longitudinal_plan_source_and_units():
    plan = SimpleNamespace(accels=[0.5], speeds=[12.3], jerks=[0.0],
                           longitudinalPlanSource="lead0")
    values = source.build_longitudinal_plan({
        "longitudinalPlan": plan,
        "carState": car_state(vCruise=16.6667),
    })
    assert values["accels"] == [0.5]
    assert values["speeds"] == [12.3]
    assert values["jerks"] == [0.0]
    assert values["longitudinalPlanSource"] == 1  # lead0
    assert abs(values["cruiseTarget"] - 60.0) < 1e-3
    # fields with no source in this fork are explicit zeros, not invented values
    assert values["tFollow"] == 0.0
    assert values["xState"] == 0


def test_radar_state_missing_lead_is_zeroed_but_status_is_present():
    values = source.build_radar_state({"radarState": radar_state()})
    assert values["leadOne"]["dRel"] == 30.0
    assert values["leadOne"]["status"] is True
    assert values["leadOne"]["radarTrackId"] == 7
    assert values["leadTwo"]["status"] is False
    assert values["leadRight"]["dRel"] == 0.0
    assert values["leadsLeft"] == []


def test_device_state_and_selfdrive_state():
    device = SimpleNamespace(memoryUsagePercent=42, freeSpacePercent=80.0,
                             cpuTempC=[41.0, 55.0], deviceType="pc", started=True)
    values = source.build_device_state({"deviceState": device})
    assert values["memoryUsagePercent"] == 42
    assert values["cpuTempC"] == [41.0, 55.0]
    assert values["deviceType"] == 5  # pc

    state = SimpleNamespace(enabled=True, personality="relaxed",
                            alertStatus="critical", alertSize="full",
                            alertType="steerUnavailable")
    sd = source.build_selfdrive_state({"selfdriveState": state})
    assert sd["enabled"] is True
    assert sd["personality"] == 2  # relaxed
    assert sd["alertStatus"] == 2  # critical
    assert sd["alertSize"] == 3  # full
    assert sd["alertType"] == "steerUnavailable"


def test_gps_and_lateral_plan():
    gps = SimpleNamespace(latitude=31.2, longitude=121.4, speed=12.3,
                          bearingDeg=90.0, altitude=10.0, horizontalAccuracy=3.0,
                          verticalAccuracy=5.0, unixTimestampMillis=1700000000000)
    values = source.build_gps({"gpsLocationExternal": gps})
    assert values["latitude"] == 31.2
    assert values["longitude"] == 121.4
    assert values["hasFix"] is True
    assert values["unixTimestampMillis"] == 1700000000000.0

    model = model_v2(meta=SimpleNamespace(laneChangeState="preLaneChange",
                                          laneChangeDirection="left"))
    lp = source.build_lateral_plan({"modelV2": model})
    assert lp["useLaneLines"] is True
    assert lp["laneChangeState"] == 1  # preLaneChange
    assert lp["laneChangeDirection"] == 1  # left


def test_carrot_man_and_carrot_navi_use_available_sources():
    live = SimpleNamespace(speedLimitValid=True, speedLimit=22.2222,
                           speedLimitAheadValid=False, speedLimitAheadDistance=0.0,
                           roadName="G1")
    nav = SimpleNamespace(maneuverDistance=120.0, maneuverPrimaryText="Turn left")
    gps = SimpleNamespace(latitude=31.2, longitude=121.4, speed=12.3,
                          bearingDeg=90.0, altitude=10.0, horizontalAccuracy=3.0,
                          verticalAccuracy=5.0, unixTimestampMillis=1700000000000)
    messages = {
        "carState": car_state(vCruise=16.6667),
        "liveMapDataSP": live,
        "navInstruction": nav,
        "gpsLocationExternal": gps,
    }
    man = source.build_carrot_man(messages)
    assert man["nRoadLimitSpeed"] == 80  # 22.2222 m/s -> 80 km/h
    assert man["szTBTMainText"] == "Turn left"
    assert man["xDistToTurn"] == 120
    assert man["desiredSpeed"] == 60
    assert man["xPosLat"] == 31.2
    assert man["vehicleNaviActive"] is True

    navi = source.build_carrot_navi(messages)
    assert navi["schemaVersion"] == 2
    assert navi["guidanceCurrent"]["present"] is True
    assert navi["guidanceCurrent"]["mainText"] == "Turn left"
    assert navi["speed"]["roadLimitValid"] is True
    assert navi["speed"]["roadLimitKph"] == 80
    assert navi["vehicle"]["present"] is True
    assert navi["navigationStatus"]["guidanceActive"] is True


def test_build_snapshot_skips_missing_services():
    snapshot = source.build_snapshot({"carState": car_state()})
    assert set(snapshot) == {"carState", "carrotMan"}


def test_all_twelve_services_encode_and_decode():
    messages = {
        "carState": car_state(),
        "carStateSP": car_state_sp(),
        "controlsState": SimpleNamespace(
            enabled=False, curvature=0.01, desiredCurvature=0.02,
            lateralControlState=LateralState(None)),
        "carControl": car_control(),
        "modelV2": model_v2(),
        "longitudinalPlan": SimpleNamespace(accels=[0.5], speeds=[12.3], jerks=[0.0],
                                            longitudinalPlanSource=1),
        "radarState": radar_state(),
        "deviceState": SimpleNamespace(memoryUsagePercent=42, freeSpacePercent=80.0,
                                       cpuTempC=[41.0], deviceType="pc", started=True),
        "selfdriveState": SimpleNamespace(enabled=True, personality="standard",
                                          alertStatus="normal", alertSize="none",
                                          alertType=""),
        "gpsLocationExternal": SimpleNamespace(
            latitude=31.2, longitude=121.4, speed=12.3, bearingDeg=90.0,
            altitude=10.0, horizontalAccuracy=3.0, verticalAccuracy=5.0,
            unixTimestampMillis=1700000000000),
        "lateralPlan": SimpleNamespace(useLaneLines=True, dPathPoints=[-1.8, 1.8],
                                       laneChangeState="off",
                                       laneChangeDirection="none"),
        "liveMapDataSP": SimpleNamespace(speedLimitValid=True, speedLimit=22.2222,
                                         speedLimitAheadValid=False,
                                         speedLimitAheadDistance=0.0, roadName="G1"),
        "navInstruction": SimpleNamespace(maneuverDistance=120.0,
                                          maneuverPrimaryText="Turn left"),
    }
    snapshot = source.build_snapshot(messages)
    assert set(snapshot) == set(codec.SERVICE_IDS)
    frames = [codec.encode_frame(name, index, snapshot[name])
              for index, name in enumerate(snapshot, 1)]
    decoded = codec.decode_frames(codec.encode_batch(frames))
    assert {frame["name"] for frame in decoded} == set(codec.SERVICE_IDS)


def test_source_snapshot_encodes_and_decodes_for_the_app():
    snapshot = source.build_snapshot({
        "carState": car_state(),
        "carStateSP": car_state_sp(),
        "controlsState": SimpleNamespace(
            enabled=True, curvature=0.01, desiredCurvature=0.02,
            lateralControlState=LateralState(None)),
        "carControl": car_control(),
        "modelV2": model_v2(),
        "longitudinalPlan": SimpleNamespace(accels=[0.5], speeds=[12.3], jerks=[0.0],
                                            longitudinalPlanSource=1),
        "radarState": radar_state(),
    })
    frames = []
    for index, name in enumerate(snapshot, 1):
        frames.append(codec.encode_frame(name, index, snapshot[name]))
    decoded = codec.decode_frames(codec.encode_batch(frames))
    by_name = {frame["name"]: frame["values"] for frame in decoded}
    assert abs(by_name["carState"]["vEgo"] - 12.3) < 1e-4
    assert by_name["modelV2"]["position"]["x"] == [1.2]
    assert by_name["modelV2"]["leadsV3"][0]["v"] == [8.0]
    assert by_name["controlsState"]["activeLaneLine"] is True
    assert by_name["longitudinalPlan"]["longitudinalPlanSource"] == "lead0"
    assert by_name["radarState"]["leadOne"]["status"] is True
