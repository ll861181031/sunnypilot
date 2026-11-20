
import json

from stopline_source import StopLineSource


class FakeNavi:
    def __init__(self, signal=None, vehicle=None):
        self.signal = signal
        self.vehicle = vehicle

    def get(self, name):
        if name == "traffic_signal":
            return self.signal
        if name == "vehicle":
            return self.vehicle
        return None


class FakeSM:
    def __init__(self, speed=None, lead_present=None, lead_drel=None):
        self.seen = {}
        self.valid = {}
        self.recv_time = {"carState": 100.0, "radarState": 100.0}
        self.data = {}
        if speed is not None:
            self.seen["carState"] = True
            self.valid["carState"] = True
            self.data["carState"] = type("CS", (), {"vEgo": speed})()
        if lead_present is not None:
            self.seen["radarState"] = True
            self.valid["radarState"] = True
            self.data["radarState"] = type("RS", (), {
                "leadOne": type("Lead", (), {"present": lead_present, "dRel": lead_drel if lead_present else 0.0})(),
                "leadTwo": type("Lead", (), {"present": False, "dRel": 0.0})()})()

    def __getitem__(self, key):
        return self.data[key]


def _red():
    return {"visible": True, "source": "navigation",
            "lights": {"red": {"valid": True, "on": True, "remain_sec": 20}}}


def _source(signal=None, vehicle=None):
    return StopLineSource(FakeNavi(signal, vehicle), clock=lambda: 100.0)


def test_gate_accepts_red_slow_and_no_lead():
    src = _source(_red(), {"speed_kph": 20.0})
    sm = FakeSM(speed=5.5, lead_present=False)
    assert src._gates(sm) == (True, "ok")


def test_gate_rejects_high_speed_and_close_lead_and_green():
    src = _source(_red(), {"speed_kph": 20.0})
    assert src._gates(FakeSM(speed=12.0, lead_present=False))[1] == "fast"
    assert src._gates(FakeSM(speed=5.0, lead_present=True, lead_drel=10.0))[1] == "lead"
    green = {"visible": True, "lights": {"green": {"valid": True, "on": True}}}
    assert src._gates(FakeSM(speed=5.0, lead_present=False))[1] == "lead"  # short lead hold
    assert _source(green, {"speed_kph": 20.0})._gates(FakeSM(speed=5.0, lead_present=False))[1] == "no red/yellow"


def test_gate_refuses_unknown_lead_and_unknown_speed():
    src = _source(_red(), None)
    assert src._gates(FakeSM(speed=None, lead_present=False))[1] == "no speed"
    src = _source(_red(), {"speed_kph": 20.0})
    assert src._gates(FakeSM(speed=5.0, lead_present=None))[1] == "lead unknown"


def test_snapshot_reads_a_fresh_worker_fix(tmp_path):
    src = StopLineSource(FakeNavi(), clock=lambda: 100.0,
                         gate_path=tmp_path / "gate.json",
                         fix_path=tmp_path / "fix.json")
    src._enabled = True
    src._gate_at = 100.0
    (tmp_path / "fix.json").write_text(json.dumps({
        "valid": True, "distance_m": 12.0, "row": 540,
        "coverage": 0.9, "confidence": 0.9, "at": 100.0,
        "capture_mono": 99.9, "detected_mono": 100.0, "confirmations": 3,
        "frame_id": 10, "target_id": "target-1", "context": "",
    }), encoding="utf-8")
    assert src.snapshot() is not None
    (tmp_path / "fix.json").write_text(json.dumps({
        "valid": True, "distance_m": 12.0, "row": 540,
        "coverage": 0.9, "confidence": 0.9, "at": 99.0,
        "capture_mono": 98.9, "detected_mono": 99.0, "confirmations": 3,
        "frame_id": 9, "target_id": "target-1", "context": "",
    }), encoding="utf-8")
    assert src.snapshot() is None
    src._enabled = False
    assert src.snapshot() is None
