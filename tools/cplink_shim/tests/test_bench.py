import bench_car
import bench_app
import lamp7711
from bench_state import BenchState


def test_scenarios_and_updates_are_thread_safe_snapshots():
    state = BenchState()
    assert state.snapshot()["scenario"] == "parked"
    driving = state.apply_scenario("driving")
    assert driving["speed_kph"] == 45.0
    assert driving["nav_active"] is True
    state.update({"speed_kph": 80, "traffic_color": 1, "bogus": 1})
    snap = state.snapshot()
    assert snap["speed_kph"] == 80
    assert snap["traffic_color"] == 1
    assert "bogus" not in snap
    assert snap["scenario"] == "driving"


def test_bench_can_frames_are_checksummed_and_bits_are_placed():
    state = BenchState().apply_scenario("turn_left")
    stalk = bench_car.stalks_frame(state, 3)
    assert len(stalk) == 8
    assert (sum(stalk[:7]) + stalk[7]) & 0xFF == 0xFF
    assert stalk[0] & (1 << 4)  # left blinker
    assert (stalk[3] >> 6) & 0x3 == 1  # normal light mode
    assert (stalk[4] >> 4) & 0x7 == 3  # left turn switch
    tla = bench_car.tla_frame(state)
    assert lamp7711._bits(tla, 34, 3) == 2
    assert lamp7711._bits(tla, 37, 3) == 2
    assert lamp7711._bits(tla, 40, 3) == 2


def test_fake_phone_values_follow_the_scenario():
    state = BenchState()
    app = bench_app.BenchApp(state, 17000, 17714, 17706, 17711)
    assert app._value("guidance_current") is None
    state.apply_scenario("driving")
    guidance = app._value("guidance_current")
    assert guidance["distance_m"] == 120
    assert guidance["turn_type"] == 12
    lane = app._value("lane_current")
    assert lane["count"] == 3
    assert lane["turn_info"] == [12, 51, 13]
    speed = app._value("speed")
    assert speed["road_limit_kph"] == 60
    vehicle = app._value("vehicle")
    assert vehicle["lat"] == 31.2 and vehicle["virtual_gps"] is True
    traffic = app._value("traffic_signal")
    assert traffic["lights"]["green"]["on"] is True
    state.update({"traffic_color": 0})
    assert app._value("traffic_signal") is None
