"""Offline phone navigation regressions; no sockets or vehicle processes."""
import json
from types import SimpleNamespace

import pytest

import navi_publisher
import navi_state
import shim
from openpilot.cereal import messaging


ACTIVE = {"mode": "guide", "guidance_active": True, "route_present": True, "off_route": False}
GUIDANCE = {"turn_type": 13, "distance_m": 20, "main_text": "right"}


@pytest.fixture
def clock(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(navi_state.time, "monotonic", lambda: clock[0])
    return clock


@pytest.fixture
def publisher(tmp_path, monkeypatch, clock):
    pub = navi_publisher.NaviPublisher(navi_state.NaviState(), tmp_path / "gate")
    pub._messaging = messaging
    pub.sent = []
    pub._pm = SimpleNamespace(send=lambda name, msg: pub.sent.append((name, msg)))
    monkeypatch.setattr(navi_publisher.time, "sleep", lambda _: pub._stop.set())
    return pub


def tick(pub):
    pub._stop.clear()
    pub._run()


def start_guidance(pub):
    pub.state.update("navigation_status", True, ACTIVE, 1)
    pub.state.update("guidance_current", True, GUIDANCE, 1)
    tick(pub)


def nav_messages(pub):
    return [msg for name, msg in pub.sent if name == "navInstruction"]


def test_recorded_phone_guiding_status_without_optional_off_route(publisher):
    status = {"mode": "guiding", "guidance_active": True, "route_present": True}
    publisher.state.update("navigation_status", True, status, 1)
    publisher.state.update("guidance_current", True, GUIDANCE, 1)
    tick(publisher)
    msg = nav_messages(publisher)[-1]
    assert msg.valid and msg.navInstruction.maneuverModifier == "right"
    assert msg.navInstruction.maneuverDistance == 20


@pytest.mark.parametrize("status", [
    {"mode": "unknown", "guidance_active": True, "route_present": True},
    {"guidance_active": True, "route_present": True},
    {"mode": "guiding", "guidance_active": True, "route_present": True, "off_route": True},
    {"mode": "guiding", "guidance_active": True, "route_present": True, "off_route": None},
])
def test_ambiguous_or_explicit_off_route_status_still_blocks_guidance(publisher, status):
    publisher.state.update("navigation_status", True, status, 1)
    publisher.state.update("guidance_current", True, GUIDANCE, 1)
    tick(publisher)
    assert not nav_messages(publisher)[-1].valid


def test_recorded_phone_route_remaining_distance_field(publisher):
    msg = publisher._instruction(GUIDANCE, None, None, None, {"remain_distance_m": 3679})
    assert msg.navInstruction.distanceRemaining == 3679


@pytest.mark.parametrize("revoke", ["stop", "expired"])
def test_recorded_phone_status_reaches_model_and_revokes_through_mailbox(publisher, clock, monkeypatch, revoke):
    from openpilot.selfdrive.modeld.nav_inputs import NavRuntime
    from openpilot.selfdrive.navigation import cplink, navd

    monkeypatch.setattr(cplink, "CPLINK_GATE", publisher.gate_path)
    monkeypatch.setattr(navd, "CPLINK_GATE", publisher.gate_path)
    publisher._pm = cplink.CplinkMailbox(publisher.gate_path)
    sent = []
    daemon = navd.Navd(None, SimpleNamespace(send=lambda service, msg: sent.append((service, msg))))
    runtime = NavRuntime()
    runtime._client_failed = True

    class ModelMessages:
        def __init__(self, message):
            self.message = message
            self.valid = {"navInstruction": message.valid}
            self.recv_time = {"navInstruction": clock[0]}
            self.logMonoTime = {"navInstruction": message.logMonoTime}

        def __getitem__(self, service):
            return getattr(self.message, service)

    def update():
        publisher.step(clock[0])
        daemon.step(clock[0])
        message = [msg for service, msg in sent if service == "navInstruction"][-1]
        return runtime.update(ModelMessages(message), 5.0, (1, 1, 256, 256), (1, 47))

    publisher.state.update("navigation_status", True,
                           {"mode": "guiding", "guidance_active": True, "route_present": True}, 1)
    publisher.state.update("guidance_current", True, GUIDANCE, 1)
    assert update() == (None, None)
    clock[0] += 0.6
    image, vector = update()
    assert image is None and vector is not None and vector[0, 10] == 1.0
    assert runtime.instruction_valid
    if revoke == "stop":
        publisher.state.update("navigation_status", False, None, 2)
        clock[0] += 0.3
    else:
        clock[0] += 3.1
    assert update() == (None, None)
    assert not runtime.instruction_valid


def test_lanes_preserve_one_entry_per_physical_lane(publisher):
    lane = {"count": 3, "turn_info": [12, 51, 13], "current_lane": 2}
    msg = publisher._instruction(GUIDANCE, None, lane, None, None)
    lanes = msg.navInstruction.lanes
    assert [[str(d) for d in lane.directions] for lane in lanes] == [["left"], ["straight"], ["right"]]
    assert [lane.active for lane in lanes] == [False, True, False]
    assert [str(lane.activeDirection) for lane in lanes] == ["none", "straight", "none"]


@pytest.mark.parametrize("lane", [
    {"count": 2, "turn_info": [12, 51, 13]},
    {"count": 0, "turn_info": [12]},
    {"count": 1.5, "turn_info": [12]},
    {"count": True, "turn_info": [12]},
    {"count": 1, "turn_info": [12], "visible": False},
])
def test_inconsistent_or_hidden_lanes_are_not_invented(publisher, lane):
    assert len(publisher._instruction(GUIDANCE, None, lane, None, None).navInstruction.lanes) == 0


@pytest.mark.parametrize("active", [0, 4, -1, "invalid", 1.5, True])
def test_invalid_lane_index_does_not_activate_a_lane(publisher, active):
    lane = {"count": 3, "turn_info": [12, 999, 13], "current_lane": active}
    lanes = publisher._instruction(GUIDANCE, None, lane, None, None).navInstruction.lanes
    assert len(lanes) == 3 and not any(lane.active for lane in lanes)
    assert str(lanes[1].directions[0]) == "none"


@pytest.mark.parametrize("status", [
    {**ACTIVE, "guidance_active": False, "route_present": False, "mode": "idle"},
    {**ACTIVE, "off_route": True},
    {**ACTIVE, "route_present": False},
    None,
])
def test_status_revocation_immediately_clears_instruction(publisher, clock, status):
    start_guidance(publisher)
    assert nav_messages(publisher)[-1].valid
    clock[0] += 0.25
    publisher.state.update("navigation_status", status is not None, status, 2)
    tick(publisher)
    assert not nav_messages(publisher)[-1].valid
    assert len(nav_messages(publisher)[-1].navInstruction.allManeuvers) == 0


def test_expired_status_blocks_even_fresh_guidance(publisher, clock):
    start_guidance(publisher)
    clock[0] += 10.1
    publisher.state.update("guidance_current", True, GUIDANCE, 2)
    tick(publisher)
    assert not nav_messages(publisher)[-1].valid


def test_restart_and_reroute_require_new_guidance(publisher, clock):
    start_guidance(publisher)
    publisher.state.update("navigation_status", True, {**ACTIVE, "off_route": True}, 2)
    publisher.state.update("guidance_current", True, GUIDANCE, 3)
    publisher.state.update("navigation_status", True, ACTIVE, 4)
    clock[0] += 0.25
    tick(publisher)
    assert not nav_messages(publisher)[-1].valid
    publisher.state.update("guidance_current", True, {**GUIDANCE, "turn_type": 12}, 5)
    clock[0] += 0.25
    tick(publisher)
    assert nav_messages(publisher)[-1].valid
    assert nav_messages(publisher)[-1].navInstruction.maneuverModifier == "left"


def test_speed_alone_does_not_publish_or_claim_navigation(publisher):
    publisher.state.update("speed", True, {"road_limit_kph": 60}, 1)
    tick(publisher)
    assert not nav_messages(publisher)
    assert publisher.gate_path.read_text() == "speed"
    assert publisher.sent[-1][0] == "liveMapDataSP"


def test_releasing_navigation_stops_publishing_before_native_resume(publisher, clock):
    start_guidance(publisher)
    publisher.state.update("navigation_status", False, None, 2)
    clock[0] += 0.25
    tick(publisher)
    clock[0] += 1.0
    tick(publisher)
    assert not publisher.gate_path.exists()
    count = len(publisher.sent)
    tick(publisher)
    assert len(publisher.sent) == count


@pytest.fixture
def state():
    state = shim.ShimState.__new__(shim.ShimState)
    state.cfg = SimpleNamespace(bench=True)
    state.navi_state = navi_state.NaviState()
    state.sessions = {}
    state.item_updates = state.stream_conns = 0
    state.stream_count, state.stream_conn_open, state.stream_last = {}, {}, {}
    state.new_session("first", "offline", {})
    return state


def peer_for(state, sid="first", name="guidance_current"):
    peer = shim.StreamPeer(state, sid, name, "offline")
    peer.on_open()
    return peer


def send(peer, seq, value=None, present=True, **extra):
    peer.handle_text(json.dumps({"type": "item_update", "sequence": seq, "present": present,
                                "value": value or GUIDANCE, **extra}))


@pytest.mark.parametrize("sequence", [9, 10, None, True, "11", -1])
def test_old_duplicate_or_invalid_sequence_cannot_replace_current(state, sequence):
    peer = peer_for(state)
    send(peer, 10)
    send(peer, sequence, {"turn_type": 12})
    assert state.navi_state.get("guidance_current") == GUIDANCE


def test_duplicate_does_not_refresh_ttl(state, clock):
    peer = peer_for(state)
    send(peer, 10)
    clock[0] += 2.9
    send(peer, 10)
    clock[0] += 0.2
    assert state.navi_state.get("guidance_current") is None


def test_old_clear_cannot_remove_new_data(state):
    peer = peer_for(state)
    send(peer, 10)
    send(peer, 9, present=False)
    assert state.navi_state.get("guidance_current") == GUIDANCE


def test_wrong_identity_does_not_advance_sequence(state):
    peer = peer_for(state)
    send(peer, 999, session_id="wrong")
    assert peer.last_sequence is None
    send(peer, 10)
    assert peer.last_sequence == 10
    assert state.navi_state.get("guidance_current") == GUIDANCE


def test_disconnect_clears_its_stream(state):
    peer = peer_for(state)
    send(peer, 10)
    peer.on_close()
    assert state.navi_state.get("guidance_current") is None


def test_replacement_socket_resets_sequence_and_rejects_old_owner(state):
    old = peer_for(state)
    send(old, 10)
    new = peer_for(state)
    send(new, 1, {"turn_type": 12})
    send(old, 11)
    old.on_close()
    assert state.navi_state.get("guidance_current") == {"turn_type": 12}
    new.on_close()
    assert state.navi_state.get("guidance_current") is None


def test_new_session_clears_old_and_rejects_old_close_and_packets(state):
    old = peer_for(state)
    send(old, 10)
    state.new_session("second", "offline", {})
    assert state.navi_state.get("guidance_current") is None
    new = peer_for(state, "second")
    send(new, 1, {"turn_type": 12})
    send(old, 11)
    old.on_close()
    state.drop_session("first")
    assert state.navi_state.get("guidance_current") == {"turn_type": 12}
    state.drop_session("second")
    assert state.navi_state.get("guidance_current") is None
    send(new, 2)
    assert state.navi_state.get("guidance_current") is None


def test_closed_control_session_cannot_open_a_stream(state):
    state.drop_session("first")
    assert shim.dispatch_ws(state, "/api/navi/ws/v2/json/first/guidance_current", "offline") is None


def test_no_status_means_no_navigation_even_with_guidance(publisher):
    publisher.state.update("guidance_current", True, GUIDANCE, 1)
    tick(publisher)
    assert not nav_messages(publisher) and not publisher.gate_path.exists()


def test_off_route_keeps_native_route_suppressed(publisher):
    publisher.state.update("navigation_status", True, {**ACTIVE, "off_route": True}, 1)
    publisher.state.update("guidance_current", True, GUIDANCE, 1)
    tick(publisher)
    assert publisher.gate_path.read_text() == "nav"
    assert not nav_messages(publisher)[-1].valid


def test_speed_release_does_not_release_navigation(publisher, clock):
    publisher.state.update("speed", True, {"road_limit_kph": 60}, 1)
    start_guidance(publisher)
    assert publisher.gate_path.read_text() == "nav+speed"
    publisher.state.update("speed", False, None, 2)
    clock[0] += 0.25
    tick(publisher)
    assert not [msg for name, msg in publisher.sent if name == "liveMapDataSP"][-1].valid
    clock[0] += 0.6
    tick(publisher)
    assert publisher.gate_path.read_text() == "nav"
    count = len([msg for name, msg in publisher.sent if name == "liveMapDataSP"])
    clock[0] += 0.25
    tick(publisher)
    assert len([msg for name, msg in publisher.sent if name == "liveMapDataSP"]) == count


def test_navigation_release_leaves_speed_owned(publisher, clock):
    publisher.state.update("speed", True, {"road_limit_kph": 60}, 1)
    start_guidance(publisher)
    publisher.state.update("navigation_status", False, None, 2)
    clock[0] += 0.25
    tick(publisher)
    clock[0] += 0.6
    tick(publisher)
    assert publisher.gate_path.read_text() == "speed"
    assert not nav_messages(publisher)[-1].valid


def test_gate_write_failure_cannot_compete_with_native_publisher(publisher, monkeypatch):
    monkeypatch.setattr(navi_publisher.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("offline fault")))
    start_guidance(publisher)
    assert publisher.sent == [] and not publisher.gate_path.exists()
    assert list(publisher.gate_path.parent.iterdir()) == []


def test_stop_withdraws_nav_and_speed_before_releasing_gate(publisher):
    publisher.state.update("speed", True, {"road_limit_kph": 60}, 1)
    start_guidance(publisher)
    publisher.stop()
    assert not publisher.gate_path.exists()
    assert not nav_messages(publisher)[-1].valid
    assert not publisher.sent[-1][1].valid


def test_status_socket_disconnect_revokes_guidance(state, publisher, clock):
    status = peer_for(state, name="navigation_status")
    send(status, 1, ACTIVE)
    guidance = peer_for(state)
    send(guidance, 1)
    publisher.state = state.navi_state
    tick(publisher)
    assert nav_messages(publisher)[-1].valid
    status.on_close()
    clock[0] += 0.25
    tick(publisher)
    assert not nav_messages(publisher)[-1].valid


def test_clear_keeps_sequence_tombstone(state):
    peer = peer_for(state)
    send(peer, 10)
    send(peer, 11, present=False)
    send(peer, 10)
    assert state.navi_state.get("guidance_current") is None


@pytest.mark.parametrize("extra", [
    {"name": "speed"}, {"session_id": "wrong"}, {"stream_handle": -1},
    {"manifest_revision": -1}, {"kind": "video"}, {"present": "false"}, {"value": [13]},
])
def test_rejected_packet_cannot_poison_sequence_or_ttl(state, clock, extra):
    peer = peer_for(state)
    send(peer, 10)
    clock[0] += 2.9
    payload = {"type": "item_update", "sequence": 999, "present": True, "value": GUIDANCE, **extra}
    peer.handle_text(json.dumps(payload))
    assert peer.last_sequence == 10
    clock[0] += 0.2
    assert state.navi_state.get("guidance_current") is None


def test_start_never_creates_competing_msgq_publishers(tmp_path, monkeypatch):
    def forbidden(*_):
        pytest.fail("CPlink must not create a second navInstruction/liveMapDataSP publisher")

    monkeypatch.setattr(messaging, "PubMaster", forbidden)
    monkeypatch.setattr(navi_publisher.threading, "Thread", lambda **_: SimpleNamespace(start=lambda: None))
    pub = navi_publisher.NaviPublisher(navi_state.NaviState(), tmp_path / "gate")
    assert pub.start()


def test_phone_to_native_publisher_to_model_and_cancel(publisher, monkeypatch, clock):
    from openpilot.selfdrive.modeld import nav_inputs
    from openpilot.selfdrive.navigation import cplink, navd

    monkeypatch.setattr(cplink, "CPLINK_GATE", publisher.gate_path)
    monkeypatch.setattr(navd, "CPLINK_GATE", publisher.gate_path)
    publisher._pm = cplink.CplinkMailbox(publisher.gate_path)
    received = {}
    native = navd.Navd(None, SimpleNamespace(send=lambda name, msg: received.update({name: msg})))
    runtime = nav_inputs.NavRuntime()
    runtime._client_failed = True

    class SM:
        valid, recv_time, logMonoTime = {}, {}, {}

        def __getitem__(self, service):
            return getattr(received[service], service)

    def update():
        tick(publisher)
        native.step(clock[0])
        message = received["navInstruction"]
        sm.valid["navInstruction"] = message.valid
        sm.recv_time["navInstruction"] = clock[0]
        sm.logMonoTime["navInstruction"] = message.logMonoTime
        return runtime.update(sm, 0.0, (1, 1, 256, 256), (1, 47))

    sm = SM()
    publisher.state.update("navigation_status", True, ACTIVE, 1)
    publisher.state.update("guidance_current", True, GUIDANCE, 1)
    image, vector = update()
    assert image is None and vector is None
    clock[0] += 0.6
    image, vector = update()
    assert image is None and vector is not None and vector[0, 10] == 1.0
    publisher.state.update("navigation_status", False, None, 2)
    clock[0] += 0.3
    image, vector = update()
    assert image is None and vector is None
