#!/usr/bin/env python3
"""
One object is shared by the fake car publisher, the fake phone client and the HTTP
control page.  Every accessor takes the lock, so the 20 Hz publishers never see a
half-written scenario.
"""
from __future__ import annotations

import threading
from dataclasses import asdict, dataclass, field, fields, replace


@dataclass
class BenchState:
    # ego
    speed_kph: float = 0.0
    accel_mps2: float = 0.0
    cruise_kph: float = 60.0
    gear: str = "drive"
    steering_deg: float = 0.0
    left_blinker: bool = False
    right_blinker: bool = False
    left_blindspot: bool = False
    right_blindspot: bool = False
    enabled: bool = False
    started: bool = True
    fuel: float = 0.7

    # lead / model
    lead_distance_m: float = 0.0
    lead_speed_mps: float = 0.0
    lane_change_state: str = "off"
    lane_change_direction: str = "none"
    lane_count: int = 3
    lane_width_m: float = 3.5

    # navigation (app -> car, 7714)
    nav_active: bool = False
    nav_distance_m: int = 0
    nav_turn_type: int = -1
    nav_text: str = ""
    nav_road: str = ""
    next_turn_type: int = -1
    next_distance_m: int = 0
    road_limit_kph: int = 60
    lane_turn_info: list[int] = field(default_factory=lambda: [12, 51, 13])
    current_lane: int = 2
    road_category: int = 1
    traffic_color: int = 0  # 0 none / 1 red / 2 green / 3 yellow
    signal_distance_m: int = 0

    # gps
    gps_lat: float = 31.2
    gps_lon: float = 121.4
    gps_bearing: float = 90.0
    gps_fix: bool = True

    # bench CAN (0x133 / 0x2B4)
    can_tla_raw: int = 0  # 1 red / 2 green / 3 yellow after the bench mapping
    can_light_mode: int = 1  # 1 normal / 2 high beam / 3 hazard
    can_turn_switch: int = 1  # 1 off / 3 left / 5 right
    scenario: str = "parked"

    def __post_init__(self) -> None:
        # Not a dataclass field: asdict() must never try to deep-copy a lock.
        self.__dict__["_lock"] = threading.Lock()

    def update(self, values: dict) -> dict:
        allowed = {f.name for f in fields(self) if not f.name.startswith("_")}
        clean = {key: value for key, value in values.items() if key in allowed}
        with self._lock:
            for key, value in clean.items():
                setattr(self, key, value)
            return asdict(self)

    def snapshot(self) -> dict:
        with self._lock:
            return asdict(self)

    def apply_scenario(self, name: str) -> dict:
        presets = {
            "parked": dict(
                speed_kph=0.0, accel_mps2=0.0, enabled=False, gear="park",
                nav_active=False, nav_distance_m=0, nav_turn_type=-1, nav_text="",
                left_blinker=False, right_blinker=False, traffic_color=0,
                can_tla_raw=0, can_light_mode=1, can_turn_switch=1,
                lane_change_state="off", lane_change_direction="none",
                lead_distance_m=0.0, lead_speed_mps=0.0, signal_distance_m=0,
            ),
            "driving": dict(
                speed_kph=45.0, accel_mps2=0.3, enabled=True, gear="drive",
                nav_active=True, nav_distance_m=120, nav_turn_type=12,
                nav_text="Turn left", nav_road="G1", next_turn_type=13,
                next_distance_m=420, road_limit_kph=60, lane_count=3,
                current_lane=2, lane_turn_info=[12, 51, 13], road_category=1,
                traffic_color=2, signal_distance_m=80, can_tla_raw=2,
                can_light_mode=1, can_turn_switch=1,
                lane_change_state="off", lane_change_direction="none",
                lead_distance_m=25.0, lead_speed_mps=10.0,
            ),
            "turn_left": dict(
                speed_kph=25.0, accel_mps2=-0.4, enabled=True, gear="drive",
                nav_active=True, nav_distance_m=18, nav_turn_type=12,
                nav_text="Turn left now", nav_road="G1", next_turn_type=13,
                next_distance_m=300, road_limit_kph=40, lane_count=2,
                current_lane=1, lane_turn_info=[12, 51], road_category=1,
                left_blinker=True, right_blinker=False, traffic_color=2,
                signal_distance_m=15, can_tla_raw=2, can_turn_switch=3,
                lane_change_state="preLaneChange", lane_change_direction="left",
                lead_distance_m=12.0, lead_speed_mps=5.0,
            ),
            "lane_change": dict(
                speed_kph=65.0, accel_mps2=0.2, enabled=True, gear="drive",
                nav_active=True, nav_distance_m=250, nav_turn_type=51,
                nav_text="Continue", nav_road="G2", road_limit_kph=80,
                lane_count=3, current_lane=2, lane_turn_info=[51, 51, 13],
                right_blinker=True, left_blinker=False,
                lane_change_state="laneChangeStarting", lane_change_direction="right",
                traffic_color=0, can_tla_raw=0, can_turn_switch=5,
                lead_distance_m=40.0, lead_speed_mps=18.0,
            ),
            "red_light": dict(
                speed_kph=0.0, accel_mps2=-2.0, enabled=True, gear="drive",
                nav_active=True, nav_distance_m=30, nav_turn_type=51,
                nav_text="Go straight", nav_road="G1", road_limit_kph=60,
                lane_count=3, current_lane=2, lane_turn_info=[12, 51, 13],
                traffic_color=1, signal_distance_m=12, can_tla_raw=1,
                can_light_mode=1, can_turn_switch=1,
                lead_distance_m=8.0, lead_speed_mps=0.0,
            ),
            "highway": dict(
                speed_kph=100.0, accel_mps2=0.0, enabled=True, gear="drive",
                nav_active=True, nav_distance_m=900, nav_turn_type=51,
                nav_text="Continue 12 km", nav_road="G15", road_limit_kph=120,
                lane_count=4, current_lane=2, lane_turn_info=[51, 51, 51, 13],
                road_category=6, traffic_color=0, can_tla_raw=0,
                lead_distance_m=55.0, lead_speed_mps=24.0,
            ),
            "no_gps": dict(
                speed_kph=20.0, enabled=False, nav_active=False,
                gps_fix=False, gps_lat=0.0, gps_lon=0.0, traffic_color=0,
                can_tla_raw=0,
            ),
        }
        preset = presets.get(name)
        if preset is None:
            raise ValueError(f"unknown scenario {name!r}")
        preset["scenario"] = name
        return self.update(preset)
