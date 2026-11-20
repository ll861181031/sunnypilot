import json
from types import SimpleNamespace

import pytest

import nav_lamp
import navi_state
import stopline_source
import stopline_worker
from openpilot.sunnypilot.stopline.detector import StopLine, StopLineTracker


def signal(state="red", **extra):
    return {"visible": True, "lights": {state: {"valid": True, "on": True}}, **extra}


def line(distance=15.0, row=540):
    return StopLine(row, row - 3, row + 3, 7, 0.9, 30.0, distance, 0.5)


@pytest.mark.parametrize("age, expected", [(0.1, True), (0.4, False), (-0.1, False), (float("nan"), False)])
def test_input_requires_finite_fresh_receive_time(age, expected):
    sm = SimpleNamespace(seen={"carState": True}, valid={"carState": True}, recv_time={"carState": 10.0})
    assert stopline_source._fresh_input(sm, "carState", 10.0 + age, 0.3) is expected


@pytest.mark.parametrize("at", [None, 9.6, 10.1, float("nan"), float("inf")])
def test_frozen_or_malformed_enabled_gate_expires(at):
    gate = {"enabled": True} if at is None else {"enabled": True, "at": at}
    assert not stopline_worker._fresh_gate(gate, 10.0, 0.3)


def test_gate_boundary_does_not_refresh_when_read_twice():
    gate = {"enabled": True, "at": 10.0}
    assert stopline_worker._fresh_gate(gate, 10.2, 0.3)
    assert not stopline_worker._fresh_gate(gate, 10.4, 0.3)


@pytest.mark.parametrize("seq", [1, 0])
def test_duplicate_and_reordered_input_do_not_refresh_ttl(monkeypatch, seq):
    now = [10.0]
    monkeypatch.setattr(navi_state.time, "monotonic", lambda: now[0])
    state = navi_state.NaviState()
    state.start_session("phone-a")
    assert state.update("traffic_signal", True, signal(), 1000, 1)
    now[0] = 12.9
    assert not state.update("traffic_signal", True, signal("green"), 2000, seq)
    assert state.get("traffic_signal") == signal()
    now[0] = 13.1
    assert state.get("traffic_signal") is None


def test_navigation_future_clock_is_not_fresh(monkeypatch):
    now = [10.0]
    monkeypatch.setattr(navi_state.time, "monotonic", lambda: now[0])
    state = navi_state.NaviState()
    state.update("traffic_signal", True, signal(), 1000, 1)
    now[0] = 9.0
    assert state.get("traffic_signal") is None
    assert not state.summary()["traffic_signal"]["fresh"]


def test_session_change_restarts_light_confirmations(monkeypatch):
    monkeypatch.setattr(navi_state.time, "monotonic", lambda: 10.0)
    state = navi_state.NaviState()
    bridge = nav_lamp.NavLampBridge(state, clock=lambda: 10.0)
    state.start_session("phone-a")
    for seq in (1, 2):
        state.update("traffic_signal", True, signal(), 1000, seq)
        bridge.poll_once()
    assert bridge.status()["confirmations"] == 2
    state.start_session("phone-b")
    state.update("traffic_signal", True, signal(), 1000, 1)
    assert bridge.poll_once() is None
    assert bridge.status()["confirmations"] == 1


def test_lamp_publication_reports_actual_independent_confirmations(monkeypatch):
    monkeypatch.setattr(navi_state.time, "monotonic", lambda: 10.0)
    state = navi_state.NaviState()
    calls = []
    bridge = nav_lamp.NavLampBridge(state, clock=lambda: 10.0, publish=lambda *args: calls.append(args) or True)
    for seq in (1, 2):
        state.update("traffic_signal", True, signal(distance_m=50), 1000, seq)
        bridge.poll_once()
    assert calls[-1][1] == 2
    assert calls[-1][0].lamp_confirmations == 2
    assert not calls[-1][0].relevant


@pytest.mark.parametrize("at", [101.0, float("nan"), float("inf")])
def test_future_or_nonfinite_worker_fix_is_rejected(tmp_path, at):
    src = stopline_source.StopLineSource(None, clock=lambda: 100.0, fix_path=tmp_path / "fix.json")
    src._enabled = True
    src._gate_at = 100.0
    src.fix_path.write_text(json.dumps({"valid": True, "at": at, "distance_m": 12.0, "row": 540,
        "capture_mono": at, "detected_mono": at, "confirmations": 3,
        "frame_id": 1, "target_id": "target-a", "context": ""}), encoding="utf-8")
    assert src.snapshot() is None


def test_duplicate_camera_timestamp_cannot_confirm_a_track():
    tracker = StopLineTracker()
    for _ in range(5):
        assert tracker.update([line()], 10.0) is None
    assert tracker.count == 1


def test_track_identity_and_motion_consistency_require_new_confirmation():
    tracker = StopLineTracker()
    for i in range(3):
        out = tracker.update([line(15 - 0.5 * i, 540 + 5 * i)], 10 + 0.1 * i,
                             speed_mps=5.0, context="phone-a/road-a", frame_id=i)
    assert out is not None
    first_id = tracker.target_id
    assert tracker.update([line(10.0, 580)], 10.3, speed_mps=5.0,
                          context="phone-a/road-a", frame_id=3) is None
    assert tracker.target_id != first_id
    assert tracker.count == 1
    assert tracker.update([line(9.5, 585)], 10.4, speed_mps=5.0,
                          context="phone-b/road-a", frame_id=4) is None
    assert tracker.count == 1


def test_worker_freeze_clears_confirmation_and_preserves_capture_time():
    processor = stopline_worker.StopLineProcessor()
    gate = {"enabled": True, "at": 10.0, "speed": 0.0, "context": "phone-a"}
    for i in range(3):
        result = processor.process([line()], gate, 10.0 + i * 0.1,
                                   10.02 + i * 0.1, i)
    assert result["valid"]
    assert result["confirmations"] == 3
    assert result["capture_mono"] == 10.2
    assert result["detected_mono"] == pytest.approx(10.22)
    assert not processor.process([line()], gate, 10.5, 10.52, 3)["valid"]
    assert processor.tracker.count == 0


def test_duplicate_changed_payload_cannot_replace_accepted_lamp():
    class Input:
        def record(self, name):
            return {"fresh": True, "value": signal(self.state, distance_m=self.distance),
                    "seq": self.seq, "session": "phone-a", "recv_mono": 10.0}
    source = Input()
    bridge = nav_lamp.NavLampBridge(source, clock=lambda: 10.0)
    source.state, source.distance, source.seq = "red", 20.0, 1
    bridge.poll_once()
    source.seq = 2
    assert bridge.poll_once().state == "red"
    source.state, source.distance = "green", 40.0
    reading = bridge.poll_once()
    assert reading.state == "red"
    assert reading.distance_m == 20.0
    assert bridge.status()["confirmations"] == 2
    source.seq = 1
    assert bridge.poll_once() is None
    assert bridge.status()["reason"] == "out_of_order"


def test_traffic_sequence_watermark_survives_same_session_socket_replacement():
    state = navi_state.NaviState()
    state.start_session("a")
    old, new = object(), object()
    assert state.open_stream("traffic_signal", "a", old)
    assert state.update("traffic_signal", True, signal(), 1000, 10, session="a", owner=old)
    assert state.open_stream("traffic_signal", "a", new)
    assert not state.update("traffic_signal", True, signal("green"), 2000, 1, session="a", owner=new)
    assert state.record("traffic_signal")["last_rejection"] == "out_of_order"
    assert state.update("traffic_signal", True, signal("green"), 2000, 11, session="a", owner=new)


@pytest.mark.parametrize("capture, detected, reason", [
    (10.0, 10.5, "frame_stale"),
    (10.2, 10.1, "frame_clock_abnormal"),
    (float("nan"), 10.1, "frame_clock_abnormal"),
])
def test_worker_rejects_queued_and_invalid_camera_time(capture, detected, reason):
    processor = stopline_worker.StopLineProcessor()
    result = processor.process([line()], {"enabled": True, "at": detected, "speed": 0.0}, capture, detected, 1)
    assert not result["valid"]
    assert result["reason"] == reason
    assert processor.tracker.count == 0


def test_worker_rejects_camera_capture_before_changed_session():
    result = stopline_worker.StopLineProcessor().process([line()],
        {"enabled": True, "at": 10.2, "context_at": 10.15, "speed": 0.0}, 10.1, 10.2, 1)
    assert result["reason"] == "frame_before_context"


def test_source_rejects_stale_speed_and_preserves_held_lead():
    class SM:
        seen = valid = {"carState": True, "modelV2": True, "radarState": True}
        recv_time = {"carState": 10.0, "modelV2": 10.0, "radarState": 10.0}
        data = {
            "carState": SimpleNamespace(vEgo=5.0),
            "modelV2": SimpleNamespace(leadsV3=[SimpleNamespace(prob=0.1, x=[12.0]), SimpleNamespace(prob=0.1, x=[20.0])]),
            "radarState": SimpleNamespace(leadOne=SimpleNamespace(present=True, dRel=12.0),
                                           leadTwo=SimpleNamespace(present=False, dRel=0.0)),
        }
        def __getitem__(self, key):
            return self.data[key]
    navi = SimpleNamespace(get=lambda key: signal())
    src = stopline_source.StopLineSource(navi, clock=lambda: 10.1)
    assert src._gates(SM()) == (False, "lead")
    SM.recv_time = {**SM.recv_time, "carState": 9.0}
    assert src._gates(SM()) == (False, "carState stale")


@pytest.mark.parametrize("stopline", [False, True])
def test_cereal_publication_roundtrip_keeps_independent_evidence(stopline):
    from openpilot.cereal import log, messaging

    sent = []
    publisher = nav_lamp.CerealLampPublisher.__new__(nav_lamp.CerealLampPublisher)
    publisher._messaging = messaging
    publisher._pm = SimpleNamespace(send=lambda name, message: sent.append(message.to_bytes()))
    publisher.sent = 0
    publisher.last_error = ""
    reading = nav_lamp.LampReading(
        "red", 12.0 if stopline else -1.0, None, "navigation", is_stopline=stopline,
        source_session="phone-a", source_sequence=42, source_recv_mono=10.0,
        intersection_id="crossing-1", direction="straight", relevant=True,
        lamp_confirmations=2, stopline_confirmations=3 if stopline else 0,
        target_id="camera-a:1" if stopline else "", camera_frame_id=99 if stopline else 0,
        capture_mono=9.9 if stopline else 0.0, detected_mono=9.95 if stopline else 0.0,
        gate_reason="ok" if stopline else "no_detection")
    assert publisher(reading, 2, 5)
    with log.Event.from_bytes(sent[0]) as event:
        payload = event.yoloObjects
        assert payload.egoLightSource == "navigation"
        assert payload.egoLightSession == "phone-a"
        assert payload.egoLightSequence == 42
        assert payload.egoLightReceivedMonoTime == 10_000_000_000
        assert payload.egoLightFrames == payload.egoLightConfirmations == 2
        assert payload.stopLineConfirmations == (3 if stopline else 0)
        assert payload.stopLineFrameId == (99 if stopline else 0)
        assert payload.stopLineCaptureMonoTime == (9_900_000_000 if stopline else 0)
        assert payload.stopLineDetectedMonoTime == (9_950_000_000 if stopline else 0)
        assert payload.egoLightRelevant


def test_explicit_intersection_and_direction_are_required_for_relevance():
    assert not nav_lamp.map_traffic_signal(signal(relevant=True)).relevant
    assert not nav_lamp.map_traffic_signal(signal(intersection_id="a", direction="unknown", relevant=True)).relevant
    assert nav_lamp.map_traffic_signal(signal(intersection_id="a", direction="straight", relevant=True)).relevant


def test_source_stops_returning_fix_when_parent_gate_loop_freezes(tmp_path):
    now = [100.0]
    src = stopline_source.StopLineSource(None, clock=lambda: now[0], fix_path=tmp_path / "fix.json")
    src._enabled, src._gate_at = True, 100.0
    def write_fix(at):
        src.fix_path.write_text(json.dumps({"valid": True, "at": at, "distance_m": 12.0, "row": 540,
            "capture_mono": at, "detected_mono": at, "confirmations": 3,
            "frame_id": 1, "target_id": "target-a", "context": ""}), encoding="utf-8")
    write_fix(100.0)
    assert src.snapshot() is not None
    now[0] = 100.5
    write_fix(100.5)
    assert src.snapshot() is None


def two_slot_sm():
    class SM:
        seen = valid = {"modelV2": True, "radarState": True}
        recv_time = {"modelV2": 10.0, "radarState": 10.0}
        def __init__(self):
            self.data = {
                "modelV2": SimpleNamespace(leadsV3=[SimpleNamespace(prob=0.1, x=[20.0]), SimpleNamespace(prob=0.1, x=[20.0])]),
                "radarState": SimpleNamespace(leadOne=SimpleNamespace(present=False, dRel=0.0),
                                               leadTwo=SimpleNamespace(present=False, dRel=0.0)),
            }
        def __getitem__(self, key):
            return self.data[key]
    return SM()


@pytest.mark.parametrize("service", ["modelV2", "radarState"])
def test_second_lead_slot_blocks_false_empty_lane_gate(service):
    sm = two_slot_sm()
    if service == "modelV2":
        sm.data[service].leadsV3[1] = SimpleNamespace(prob=0.9, x=[8.0])
    else:
        sm.data[service].leadTwo = SimpleNamespace(present=True, dRel=8.0)
    src = stopline_source.StopLineSource(None, clock=lambda: 10.1)
    assert src._lead_drel_now(sm) == 8.0


@pytest.mark.parametrize("damage", ["model_missing", "radar_missing", "model_nan", "radar_nan"])
def test_invalid_second_slot_cannot_be_disproved_by_empty_other_slot(damage):
    sm = two_slot_sm()
    if damage == "model_missing":
        sm.data["modelV2"].leadsV3 = sm.data["modelV2"].leadsV3[:1]
    elif damage == "radar_missing":
        del sm.data["radarState"].leadTwo
    elif damage == "model_nan":
        sm.data["modelV2"].leadsV3[1].prob = float("nan")
    else:
        sm.data["radarState"].leadTwo = SimpleNamespace(present=True, dRel=float("nan"))
    src = stopline_source.StopLineSource(None, clock=lambda: 10.1)
    assert src._lead_drel_now(sm) is None


def test_native_cereal_second_slot_is_checked_without_list_slicing():
    from openpilot.cereal import log

    sm = two_slot_sm()
    model = log.ModelDataV2.new_message()
    model.init("leadsV3", 2)
    model.leadsV3[0].prob, model.leadsV3[0].x = 0.1, [20.0]
    model.leadsV3[1].prob, model.leadsV3[1].x = 0.9, [8.0]
    sm.data["modelV2"] = model.as_reader()
    sm.data["radarState"] = log.RadarState.new_message().as_reader()
    assert stopline_source.StopLineSource(None, clock=lambda: 10.1)._lead_drel_now(sm) == 8.0
