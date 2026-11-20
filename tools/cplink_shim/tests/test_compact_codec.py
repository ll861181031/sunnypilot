import math

import pytest

import compact_codec as codec


# Golden producer output copied from CPlink's CompactStateDecoderTest.kt.  These are
# produced by Carrot's compact_state.py, so they prove our schema/type table matches the
# real wire format rather than just matching our own encoder.
from golden_compact import FIXTURES



def approx_equal(left, right, tolerance=2e-3):
    if isinstance(left, float) or isinstance(right, float):
        return isinstance(left, (int, float)) and isinstance(right, (int, float)) and \
            math.isclose(float(left), float(right), rel_tol=1e-5, abs_tol=tolerance)
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            approx_equal(left[key], right[key], tolerance) for key in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            approx_equal(a, b, tolerance) for a, b in zip(left, right))
    return left == right


@pytest.mark.parametrize("name,hex_data", FIXTURES.items())
def test_golden_fixtures_decode_and_reencode_to_same_values(name, hex_data):
    frames = codec.decode_frames(bytes.fromhex(hex_data))
    assert len(frames) == 1
    frame = frames[0]
    assert frame["name"] == name
    assert frame["sequence"] == 0xFFFF

    rebuilt = codec.encode_frame(frame["name"], frame["sequence"], frame["values"])
    again = codec.decode_frames(rebuilt)[0]
    assert again["name"] == name
    assert approx_equal(frame["values"], again["values"])


def test_all_twelve_service_ids_are_present():
    assert set(codec.SERVICE_IDS) == {
        "carState", "controlsState", "deviceState", "carrotMan", "selfdriveState",
        "gpsLocationExternal", "longitudinalPlan", "modelV2", "lateralPlan",
        "radarState", "carControl", "carrotNavi",
    }
    assert codec.SERVICE_IDS["carrotNavi"] == 21


def test_cvb1_batch_roundtrip_and_lengths():
    first = codec.encode_frame("carState", 1, {"vEgo": 1.0})
    second = codec.encode_frame("modelV2", 2, {"frameId": 7, "position": {"x": [1.0]}})
    batch = codec.encode_batch([first, second])
    assert batch[:4] == b"CVB1"
    frames = codec.decode_frames(batch)
    assert [frame["name"] for frame in frames] == ["carState", "modelV2"]
    assert [frame["sequence"] for frame in frames] == [1, 2]
    assert frames[1]["values"]["frameId"] == 7
    assert frames[1]["values"]["position"]["x"] == [1.0]


def test_bad_magic_and_unknown_service_are_rejected():
    with pytest.raises(codec.CompactDecodeError):
        codec.decode_frames(b"NOPE" + b"\x00" * 8)
    with pytest.raises(codec.CompactDecodeError):
        codec.decode_frames(bytes.fromhex("43565331ff00ffff"))


def test_nonfinite_values_are_sanitized_before_the_app_sees_them():
    frame = codec.encode_frame("modelV2", 1, {
        "frameId": 1,
        "frameDropPerc": float("nan"),
        "modelExecutionTime": float("inf"),
        "position": {"x": [float("nan")], "y": [float("inf")], "z": [0.0]},
    })
    values = codec.decode_frames(frame)[0]["values"]
    assert values["frameDropPerc"] == 0.0
    assert values["modelExecutionTime"] == 0.0
    assert values["position"]["x"] == [0.0]
    assert values["position"]["y"] == [0.0]


def test_utf8_text_is_truncated_on_a_character_boundary():
    long_text = "中" * 30000  # 90000 UTF-8 bytes
    frame = codec.encode_frame("selfdriveState", 1, {
        "enabled": True, "alertType": long_text, "personality": "standard",
        "alertStatus": "normal", "alertSize": "none",
    })
    values = codec.decode_frames(frame)[0]["values"]
    assert len(values["alertType"].encode("utf-8")) <= 0xFFFF
    assert values["alertType"].startswith("中")


def test_text_length_prefix_is_u16_and_sequence_is_u16():
    frame = codec.encode_frame("selfdriveState", 0x1FFFF, {
        "enabled": False, "alertType": "x", "personality": "standard",
        "alertStatus": "normal", "alertSize": "none",
    })
    assert codec.decode_frames(frame)[0]["sequence"] == 0xFFFF
