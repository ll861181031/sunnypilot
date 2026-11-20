import navi_state
import navi_publisher


def test_update_get_and_present_false_clears():
    state = navi_state.NaviState()
    state.update("guidance_current", True, {"distance_m": 120}, 1000, 1)
    assert state.get("guidance_current") == {"distance_m": 120}
    state.update("guidance_current", False, None, None, None)
    assert state.get("guidance_current") is None


def test_ttl_expiry_is_per_stream(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(navi_state.time, "monotonic", lambda: clock[0])
    state = navi_state.NaviState()
    state.update("navigation_status", True, {"mode": "guide"}, 1000, 1)
    state.update("guidance_current", True, {"distance_m": 1}, 1000, 1)
    clock[0] = 104.0
    assert state.get("guidance_current") is None  # 3 s TTL
    assert state.get("navigation_status") == {"mode": "guide"}  # 10 s TTL
    summary = state.summary()
    assert summary["guidance_current"]["fresh"] is False
    assert summary["navigation_status"]["fresh"] is True


def test_turn_code_mapping_is_conservative():
    assert navi_publisher.turn_for_code(12) == ("turn", "left")
    assert navi_publisher.turn_for_code(13) == ("turn", "right")
    assert navi_publisher.turn_for_code(14) == ("turn", "uturn")
    assert navi_publisher.turn_for_code(101) == ("off ramp", "right")
    assert navi_publisher.turn_for_code(201) == ("arrive", "straight")
    # Fork codes must not become turns.
    for code in (-1, 7, 17, 44, 75, 76, 118, 999):
        assert navi_publisher.turn_for_code(code) is None
    assert navi_publisher.direction_for_code(12) == "left"
    assert navi_publisher.direction_for_code(13) == "right"
    assert navi_publisher.direction_for_code(51) == "straight"
    assert navi_publisher.direction_for_code(44) == "none"
