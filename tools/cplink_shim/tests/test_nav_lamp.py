import nav_lamp


class FakeNavi:
    def __init__(self):
        self.value = None
        self.seq = 0
        self.fresh = False

    def summary(self):
        return {"traffic_signal": {"fresh": self.fresh, "seq": self.seq, "age_s": 0.0}}

    def get(self, name):
        return self.value if self.fresh else None

    def push(self, value, seq=None):
        self.value = value
        self.seq = self.seq + 1 if seq is None else seq
        self.fresh = True


def _signal(state="red", distance=50.0, remain=12):
    return {
        "visible": True,
        "distance_m": distance,
        "source": "navigation",
        "lights": {state: {"valid": True, "on": True, "remain_sec": remain}},
    }


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, reading, frames, frame_id):
        self.calls.append((reading, frames, frame_id))
        return True


def _bridge(navi, recorder, enabled=lambda: True):
    return nav_lamp.NavLampBridge(
        navi, publish=recorder, enabled=enabled, clock=lambda: 100.0,
        min_confirmations=2, rate_hz=10.0,
    )


def test_map_red_with_distance_and_countdown():
    reading = nav_lamp.map_traffic_signal(_signal("red", 50, 12))
    assert reading is not None
    assert reading.state == "red"
    assert reading.distance_m == 50.0
    assert reading.remain_s == 12.0


def test_map_prefers_red_when_payload_contradicts_itself():
    value = _signal("green", 30)
    value["lights"]["red"] = {"valid": True, "on": True, "remain_sec": 4}
    reading = nav_lamp.map_traffic_signal(value)
    assert reading.state == "red"


def test_map_rejects_invisible_unknown_distance_or_no_active_lamp():
    assert nav_lamp.map_traffic_signal({"visible": False, "distance_m": 20,
                                        "lights": {"red": {"on": True}}}) is None
    # Some app versions omit ``visible`` when a light is present; the lights dict
    # is then the evidence.  An explicit false is still a rejection.
    assert nav_lamp.map_traffic_signal({"distance_m": 20,
                                        "lights": {"red": {"on": True}}}) is not None
    assert nav_lamp.map_traffic_signal({"visible": True, "distance_m": 0,
                                        "lights": {"red": {"on": True}}}) is None
    assert nav_lamp.map_traffic_signal({"visible": True, "distance_m": 20,
                                        "lights": {"red": {"on": False}}}) is None
    assert nav_lamp.map_traffic_signal({"visible": True, "distance_m": 20,
                                        "lights": {}}) is None


def test_two_navigation_updates_are_required_before_publishing():
    navi = FakeNavi()
    rec = Recorder()
    bridge = _bridge(navi, rec)

    navi.push(_signal("red", 50))
    assert bridge.poll_once() is None
    assert rec.calls == []

    navi.push(_signal("red", 49))
    assert bridge.poll_once() is not None
    assert len(rec.calls) == 1
    reading, frames, _ = rec.calls[0]
    assert reading.state == "red"
    assert frames == 2  # Independent phone updates, never a synthetic camera count.


def test_near_frames_report_actual_phone_updates():
    navi = FakeNavi()
    rec = Recorder()
    bridge = _bridge(navi, rec)
    navi.push(_signal("red", 30))
    bridge.poll_once()
    navi.push(_signal("red", 29))
    bridge.poll_once()
    assert rec.calls[-1][1] == 2


def test_same_sequence_keeps_republishing_without_adding_confirmations():
    navi = FakeNavi()
    rec = Recorder()
    bridge = _bridge(navi, rec)
    navi.push(_signal("red", 50))
    bridge.poll_once()
    navi.push(_signal("red", 50))
    bridge.poll_once()
    assert bridge.status()["confirmations"] == 2
    seq = navi.seq
    for _ in range(5):
        bridge.poll_once()
    assert navi.seq == seq
    assert len(rec.calls) == 6  # first confirmed + five 10 Hz repeats
    assert bridge.status()["confirmations"] == 2


def test_state_change_resets_confirmation_and_delays_release():
    navi = FakeNavi()
    rec = Recorder()
    bridge = _bridge(navi, rec)
    navi.push(_signal("red", 30))
    bridge.poll_once()
    navi.push(_signal("red", 29))
    bridge.poll_once()
    assert len(rec.calls) == 1

    navi.push(_signal("green", 28))
    assert bridge.poll_once() is None
    assert len(rec.calls) == 1  # do not release on one navigation update

    navi.push(_signal("green", 27))
    bridge.poll_once()
    assert rec.calls[-1][0].state == "green"


def test_stale_stream_stops_publishing_and_resets_evidence():
    navi = FakeNavi()
    rec = Recorder()
    bridge = _bridge(navi, rec)
    navi.push(_signal("red", 30))
    bridge.poll_once()
    navi.push(_signal("red", 29))
    bridge.poll_once()
    assert len(rec.calls) == 1

    navi.fresh = False
    bridge.poll_once()
    assert bridge.status()["confirmations"] == 0
    assert bridge.status()["state"] == ""

    navi.fresh = True
    navi.push(_signal("red", 20))
    bridge.poll_once()
    assert len(rec.calls) == 1  # one fresh update is not enough again

    navi.push(_signal("red", 19))
    bridge.poll_once()
    assert len(rec.calls) == 2


def test_yolo_gate_disables_the_bridge_without_killing_it():
    navi = FakeNavi()
    rec = Recorder()
    allow = {"value": False}
    bridge = _bridge(navi, rec, enabled=lambda: allow["value"])

    navi.push(_signal("red", 30))
    bridge.poll_once()
    navi.push(_signal("red", 29))
    assert bridge.poll_once() is None
    assert rec.calls == []
    assert bridge.status()["enabled"] is False

    allow["value"] = True
    navi.push(_signal("red", 28))
    bridge.poll_once()
    navi.push(_signal("red", 27))
    bridge.poll_once()
    assert len(rec.calls) == 1


def test_status_reports_the_last_reading_and_counts():
    navi = FakeNavi()
    rec = Recorder()
    bridge = _bridge(navi, rec)
    navi.push(_signal("yellow", 20, 3))
    bridge.poll_once()
    navi.push(_signal("yellow", 19, 2))
    bridge.poll_once()
    status = bridge.status()
    assert status["state"] == "yellow"
    assert status["distance_m"] == 19.0
    assert status["remain_s"] == 2.0
    assert status["published"] == 1
    assert status["confirmations"] == 2

def test_real_app_payload_without_distance_is_state_only():
    # Exact shape recorded from the shipping app on 2026-11-13: colour and
    # countdown, no distance.  It must publish the state, not be dropped.
    value = {
        "visible": True,
        "source": "navigation",
        "lights": {"red": {"valid": True, "on": True, "remain_sec": 66}},
        "ui_counter": {"remain_sec": 66},
    }
    reading = nav_lamp.map_traffic_signal(value)
    assert reading is not None
    assert reading.state == "red"
    assert reading.distance_m == -1.0
    assert reading.remain_s == 66.0

    navi = FakeNavi()
    rec = Recorder()
    bridge = _bridge(navi, rec)
    navi.push(value)
    bridge.poll_once()
    navi.push(value)
    bridge.poll_once()
    assert len(rec.calls) == 1
    _, frames, _ = rec.calls[0]
    assert frames == 2  # State-only evidence has its own source-specific threshold.
    assert bridge.status()["distance_m"] is None
    assert bridge.status()["state"] == "red"



class FakeStopLine:
    def __init__(self, distance=None):
        self.distance = distance

    def snapshot(self):
        if self.distance is None:
            return None
        class Fix:
            distance_m = self.distance
            row = 540
            coverage = 0.8
            confidence = 0.8
            at = 0.0
            confirmations = 3
            target_id = "camera-a:1"
            frame_id = 22
            capture_mono = 99.9
            detected_mono = 100.0
            context = '["",0,"",""]'
            reason = "ok"
        return Fix()

    def status(self):
        return {"distance": self.distance}


def _reader_without_distance(state="red"):
    return {"visible": True, "source": "navigation",
            "lights": {state: {"valid": True, "on": True, "remain_sec": 20}}}


def test_a_stopline_fix_turns_the_state_only_reading_into_a_distance():
    navi = FakeNavi()
    rec = Recorder()
    bridge = nav_lamp.NavLampBridge(navi, publish=rec, enabled=lambda: True,
                                    stopline=FakeStopLine(12.5),
                                    clock=lambda: 100.0, min_confirmations=2)
    navi.push(_reader_without_distance())
    bridge.poll_once()
    navi.push(_reader_without_distance())
    bridge.poll_once()
    assert len(rec.calls) == 1
    reading, frames, _ = rec.calls[0]
    assert reading.distance_m == 12.5
    assert reading.is_stopline is True
    assert frames == 2
    assert reading.stopline_confirmations == 3
    assert bridge.status()["is_stopline"] is True
    assert bridge.status()["stopline"] == {"distance": 12.5}


def test_no_stopline_fix_keeps_the_state_only_distance():
    navi = FakeNavi()
    rec = Recorder()
    bridge = nav_lamp.NavLampBridge(navi, publish=rec, enabled=lambda: True,
                                    stopline=FakeStopLine(None),
                                    clock=lambda: 100.0, min_confirmations=2)
    navi.push(_reader_without_distance())
    bridge.poll_once()
    navi.push(_reader_without_distance())
    bridge.poll_once()
    reading, _, _ = rec.calls[0]
    assert reading.distance_m == -1.0
    assert reading.is_stopline is False
