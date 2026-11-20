import asyncio
import json
import struct
import time

import lamp7711


def make_2b4(left=0, straight=0, right=0):
    data = bytearray(8)
    for start, value in ((34, left), (37, straight), (40, right)):
        for index in range(3):
            bit = start + index
            if (value >> index) & 1:
                data[bit // 8] |= 1 << (bit % 8)
    return bytes(data)


def test_bits_and_tla_decode():
    assert lamp7711._bits(make_2b4(left=3), 34, 3) == 3
    assert lamp7711._bits(make_2b4(straight=5), 37, 3) == 5
    assert lamp7711._bits(make_2b4(right=2), 40, 3) == 2
    assert lamp7711.decode_tla_2b4(make_2b4(1, 2, 3)) == {"left": 1, "straight": 2, "right": 3}


def test_stalks_decode_and_checksum():
    data = bytearray(bytes.fromhex("0131005090002bc2"))
    decoded = lamp7711.decode_stalks(data)
    assert decoded["light_mode"] == 1
    assert decoded["turn_switch"] == 1
    assert decoded["checksum_ok"] is True
    data[7] ^= 0xFF
    assert lamp7711.decode_stalks(data)["checksum_ok"] is False


def test_tla_color_requires_confirmation(monkeypatch, tmp_path):
    monkeypatch.setattr(lamp7711, "TLA_MAP_PATH", tmp_path / "map.json")
    monkeypatch.delenv("CPLINK_TLA_CONFIRMED", raising=False)
    assert lamp7711.tla_color(1) == 0
    monkeypatch.setenv("CPLINK_TLA_CONFIRMED", "1")
    monkeypatch.setenv("CPLINK_TLA_RED", "1")
    monkeypatch.setenv("CPLINK_TLA_GREEN", "2")
    monkeypatch.setenv("CPLINK_TLA_YELLOW", "3")
    assert lamp7711.tla_color(1) == 1
    assert lamp7711.tla_color(2) == 2
    assert lamp7711.tla_color(3) == 3
    assert lamp7711.tla_color(9) == 0


def test_source_snapshot_ttl():
    source = lamp7711.LampSource()
    source._color = 2
    source._updated = time.monotonic()
    assert source.snapshot()[0] == 2
    source._updated = time.monotonic() - 2.0
    assert source.snapshot() == (0, None)


class FakeSource:
    def snapshot(self):
        return 1, None


def test_server_frames_are_length_prefixed_json():
    server = lamp7711.LampServer(FakeSource(), "192.168.123.70", 0)

    async def run():
        await server.start()
        port = server.server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        head = await asyncio.wait_for(reader.readexactly(4), 2)
        length = struct.unpack(">I", head)[0]
        payload = await asyncio.wait_for(reader.readexactly(length), 2)
        data = json.loads(payload)
        writer.close()
        await server.stop()
        return data

    data = asyncio.run(run())
    assert data["version"] == 1
    assert data["sequence"] == 1
    assert data["data"]["carState"] == {"trafficLightColor": 1}
    assert "stopLineDist" not in data["data"]["carState"]
