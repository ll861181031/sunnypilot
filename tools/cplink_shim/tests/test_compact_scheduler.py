import time

import compact_codec as codec
import shim


class FakeSource:
    available = True
    last_error = ""

    def __init__(self, snapshot, updated=None):
        self.snapshot_value = snapshot
        self.updated = time.monotonic() if updated is None else updated

    def snapshot(self):
        return self.snapshot_value, self.updated


def make_state(snapshot, updated=None):
    state = shim.ShimState(shim.Config())
    state.compact_source = FakeSource(snapshot, updated)
    return state


SNAPSHOT = {
    "carState": {"vEgo": 12.3, "gearShifter": 2},
    "modelV2": {"frameId": 1, "position": {"x": [1.0], "y": [0.0], "z": [0.0]}},
    "deviceState": {"deviceType": 5, "started": True},
    "carrotMan": {"nRoadLimitSpeed": 80},
}


def test_first_tick_emits_due_services_and_advances_sequence():
    state = make_state(SNAPSHOT)
    payloads = state.compact_payload()
    assert len(payloads) == 1
    frames = codec.decode_frames(payloads[0])
    names = [frame["name"] for frame in frames]
    assert names == ["carState", "deviceState", "carrotMan", "modelV2"]
    assert all(frame["sequence"] == 1 for frame in frames)
    assert state.compact_seq["carState"] == 1


def test_immediate_second_tick_does_not_duplicate_frames():
    state = make_state(SNAPSHOT)
    assert state.compact_payload()
    assert state.compact_payload() == []


def test_cadence_separates_fast_and_slow_services():
    state = make_state(SNAPSHOT)
    state.compact_payload()
    time.sleep(0.06)
    frames = codec.decode_frames(state.compact_payload()[0])
    names = {frame["name"] for frame in frames}
    assert "carState" in names
    assert "modelV2" in names
    assert "deviceState" not in names  # 1 s cadence
    assert "carrotMan" not in names  # 0.5 s cadence
    assert state.compact_seq["carState"] == 2


def test_stale_or_empty_source_sends_nothing():
    stale = make_state(SNAPSHOT, updated=time.monotonic() - 1.0)
    assert stale.compact_payload() == []
    empty = make_state({})
    assert empty.compact_payload() == []
    missing = make_state(SNAPSHOT)
    missing.compact_source = None
    assert missing.compact_payload() == []
