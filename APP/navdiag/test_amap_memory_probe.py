import struct

import pytest

from APP.navdiag.amap_memory_probe import (
  AUTO_DICE_SHA256, GBL_SHA256, ProbeError, identity_hash, parse_maps, probe_snapshot,
)


GBL = 0x1000000000
DICE = 0x2000000000
HEAP = 0x3000000000
EPOCH = "amap-process-test-epoch"


class FakeMemory:
  def __init__(self):
    self.data = {}
    self.reads = []
    self.mutate = None

  def put(self, addr, raw):
    for index, value in enumerate(raw):
      self.data[addr + index] = value

  def u64(self, addr, value):
    self.put(addr, struct.pack("<Q", value))

  def u32(self, addr, value):
    self.put(addr, struct.pack("<I", value))

  def read(self, addr, size):
    self.reads.append((addr, size))
    if self.mutate is not None:
      self.mutate(self, addr, size)
    return bytes(self.data.get(addr + index, 0) for index in range(size))


def fixture():
  memory = FakeMemory()
  maps = "\n".join([
    f"{GBL:x}-{GBL + 0x2b00000:x} r-xp 00000000 08:01 11 /data/app/com.autonavi.amapauto-test/lib/arm64/libGbl.so",
    f"{DICE:x}-{DICE + 0x3f00000:x} r-xp 00000000 08:01 12 /data/app/com.autonavi.amapauto-test/lib/arm64/libAutoDice.so",
    f"{HEAP:x}-{HEAP + 0x100000:x} rw-p 00000000 00:00 0 [anon:libc_malloc]",
  ])
  registry, buckets, previous, node, wrapper, manager = [HEAP + value for value in (0x1000, 0x2000, 0x3000, 0x4000, 0x5000, 0x6000)]
  memory.u64(GBL + 0x2aad120, registry)
  memory.u64(registry + 0x48, buckets)
  memory.u64(registry + 0x50, 8)
  key = GBL + 0x7e6024
  memory.u64(buckets + (key % 8) * 8, previous)
  memory.u64(previous, node)
  memory.u64(node, 0)
  memory.u64(node + 8, key)
  memory.u64(node + 0x10, GBL + 0x2985828)
  memory.u64(GBL + 0x2985830, key)
  memory.u64(node + 0x20, wrapper)
  memory.u64(wrapper + 0x130, manager)
  paths, a, b, c, d = [HEAP + value for value in (0x9000, 0xa000, 0xb000, 0xc000, 0xd000)]
  memory.u64(manager + 0xb00, paths)
  memory.u32(manager + 0xb0c, 3)
  memory.u32(manager + 0xb20, 1)
  memory.u32(manager + 0x1138, 0)
  memory.u32(manager + 0x113c, 1)
  memory.u64(paths + 0x50 + 0x48, a)
  memory.u64(a + 8, b)
  memory.u64(b + 0x48, c)
  memory.u64(c, DICE + 0x3d28040)
  memory.u64(c + 8, d)
  memory.u64(d, DICE + 0x3d29828)
  memory.u32(d + 0x14, 1234567)
  memory.u64(DICE + 0x3d28040 + 0x20, DICE + 0x37709a4)
  memory.u64(DICE + 0x3d29828 + 0x20, DICE + 0x379237c)
  return memory, maps


def probe(memory, maps, **kwargs):
  return probe_snapshot(maps, memory.read,
    library_sha256={"libGbl.so": GBL_SHA256, "libAutoDice.so": AUTO_DICE_SHA256},
    process_epoch=lambda: EPOCH, **kwargs)


def test_exact_selected_identity_uses_main_index():
  memory, maps = fixture()
  result = probe(memory, maps)
  assert result["status"] == "observed"
  assert result["navigation_state"] == "started"
  assert result["navigation_running_observed"] is True
  assert result["gps_navigation_observed"] is True
  assert result["path_identity_hash"] == identity_hash(1234567, EPOCH)
  assert result["main_index"] == 1
  assert result["path_count"] == 3
  assert result["active_route_verified"] is False
  assert result["sample_consistent"] is True
  assert result["read_calls"] == 29
  assert all(size <= 64 for _, size in memory.reads)
  assert sum(size for _, size in memory.reads) < 4096
  text = str(result)
  assert str(GBL) not in text and str(HEAP) not in text and "1234567" not in text


def position_fixture():
  memory, maps = fixture()
  manager = HEAP + 0x6000
  memory.put(manager + 0x38, b"\x01" + b"\0" * 7)
  memory.put(manager + 0x40, struct.pack("<III", 1, 0, 0))
  memory.put(manager + 0x60, struct.pack("<ddd", 11.25, 22.5, 7.0))
  memory.put(manager + 0x1b0, struct.pack("<QQ", 1001000, 1000000))
  memory.u32(manager + 0x360, 2)
  for index, path_id in enumerate((7654321, 1234567)):
    item = manager + 0x1c8 + index * 0x88
    memory.put(item, struct.pack("<ddd", 11.2501 + index / 10000, 22.5001, 7.0))
    memory.put(item + 0x68, struct.pack("<iiiII", 2, 3, 4, 1, path_id))
  return memory, maps


def test_position_is_opt_in_and_preserves_default_read_budget():
  memory, maps = position_fixture()
  result = probe(memory, maps)
  assert "_position" not in result
  assert result["read_calls"] == 29
  assert not any(HEAP + 0x6000 + 0x38 <= address < HEAP + 0x6000 + 0x364 for address, _ in memory.reads)


def test_private_position_selects_current_path_without_claiming_freshness():
  memory, maps = position_fixture()
  result = probe(memory, maps, include_position=True)
  position = result["_position"]
  assert position["status"] == "observed"
  assert position["native_st_pos_deg"] == (11.25, 22.5)
  assert position["matched_st_pos_deg"] == (11.2502, 22.5001)
  assert position["segment_index"] == 2
  assert position["link_index"] == 3
  assert position["point_index"] == 4
  assert position["tick_time_raw"] == 1001000
  assert position["input_tick_time_raw"] == 1000000
  assert position["path_identity_hash"] == result["path_identity_hash"]
  assert position["coordinate_frame_verified"] is False
  assert position["source_freshness_verified"] is False
  assert position["raw_gps_verified"] is False
  assert position["route_render_input_allowed"] is False
  assert position["control_input_allowed"] is False
  assert "path_id" not in position and "chain" not in position
  assert result["read_calls"] <= 50
  assert result["read_bytes"] < 1024


@pytest.mark.parametrize(("offset", "value", "reason"), [
  (0x38, 0, "position_missing"), (0x40, 0, "position_not_usable"),
  (0x44, 1, "position_simulated"), (0x44, 2, "position_simulated"),
  (0x360, 0, "position_match_count"), (0x360, 4, "position_match_count"),
  (0x23c + 0x88, 0, "position_not_on_route"),
  (0x23c + 0x88, 4, "position_not_on_route"),
  (0x23c + 0x88, 8, "position_not_on_route"),
  (0x23c + 0x88, 0x20, "position_not_on_route"),
  (0x23c + 0x88, 0x80, "position_not_on_route"),
  (0x23c + 0x88, 0x81, "position_not_on_route"),
  (0x23c + 0x88, 0x100, "position_not_on_route"),
  (0x240 + 0x88, 17, "position_main_path_match_missing"),
  (0x240, 1234567, "position_main_path_match_ambiguous"),
  (0x230 + 0x88, 0xffffffff, "position_index_invalid"),
])
def test_private_position_rejects_invalid_native_evidence(offset, value, reason):
  memory, maps = position_fixture()
  memory.u32(HEAP + 0x6000 + offset, value)
  result = probe(memory, maps, include_position=True)
  assert result["status"] == "observed"
  assert result["_position"]["status"] == "unavailable"
  assert result["_position"]["reason"] == reason
  assert "native_st_pos_deg" not in result["_position"]
  assert not result["_position"]["control_input_allowed"]


@pytest.mark.parametrize("coords", [(float("nan"), 22.5, 0), (11.25, float("inf"), 0),
  (181, 22, 0), (11, -91, 0), (0, 0, 0)])
@pytest.mark.parametrize("offset", [0x60, 0x1c8 + 0x88])
def test_private_position_rejects_non_geographic_values(coords, offset):
  memory, maps = position_fixture()
  memory.put(HEAP + 0x6000 + offset, struct.pack("<ddd", *coords))
  result = probe(memory, maps, include_position=True)
  assert result["_position"]["reason"] == "position_coordinate_invalid"
  assert "native_st_pos_deg" not in result["_position"]


@pytest.mark.parametrize("offset", [0x1b0, 0x1b8])
def test_private_position_requires_nonzero_source_ticks(offset):
  memory, maps = position_fixture()
  memory.u64(HEAP + 0x6000 + offset, 0)
  result = probe(memory, maps, include_position=True)
  assert result["_position"]["reason"] == "position_source_tick_missing"


def test_rereading_old_position_does_not_create_source_freshness():
  memory, maps = position_fixture()
  first = probe(memory, maps, include_position=True)
  second = probe(memory, maps, include_position=True)
  assert first["_position"] == second["_position"]
  assert second["_position"]["source_freshness_verified"] is False
  assert second["_position"]["route_render_input_allowed"] is False


def test_position_tick_change_invalidates_whole_two_pass_snapshot():
  memory, maps = position_fixture()
  reads = 0
  def mutate(fake, address, size):
    nonlocal reads
    if address == HEAP + 0x6000 + 0x1b0:
      reads += 1
      if reads == 2:
        fake.u64(address, 1002000)
  memory.mutate = mutate
  result = probe(memory, maps, include_position=True)
  assert result["status"] == "unavailable"
  assert result["reason"] == "selection_changed_during_read"
  assert "native_st_pos_deg" not in result["_position"]


@pytest.mark.parametrize(("nav_type", "state"), [(1, 1), (0, 2), (0, 4), (0, 5)])
def test_private_position_never_exposes_stale_coordinates_without_gps_navigation(nav_type, state):
  memory, maps = position_fixture()
  memory.u32(HEAP + 0x6000 + 0x1138, nav_type)
  memory.u32(HEAP + 0x6000 + 0x113c, state)
  result = probe(memory, maps, include_position=True)
  assert result["_position"]["reason"] == "gps_navigation_not_observed"
  assert "native_st_pos_deg" not in result["_position"]


@pytest.mark.parametrize(("state", "name", "running"), [(0, "unknown", False), (1, "started", True),
  (2, "paused", False), (3, "resumed", True), (4, "stopped", False), (5, "arrived", False), (99, "unknown", False)])
def test_state_is_not_inferred_from_file_presence(state, name, running):
  memory, maps = fixture()
  memory.u32(HEAP + 0x6000 + 0x113c, state)
  result = probe(memory, maps)
  assert result["navigation_state"] == name
  assert result["navigation_running_observed"] is running


def test_no_path_is_valid_inactive_snapshot():
  memory, maps = fixture()
  memory.u64(HEAP + 0x6000 + 0xb00, 0)
  memory.u32(HEAP + 0x6000 + 0xb0c, 0)
  memory.u32(HEAP + 0x6000 + 0xb20, 0)
  memory.u32(HEAP + 0x6000 + 0x113c, 4)
  result = probe(memory, maps)
  assert result["path_identity_hash"] is None
  assert not result["navigation_running_observed"]
  assert not result["gps_navigation_observed"]
  assert result["read_calls"] == 17


def test_mutable_fields_are_read_again_in_second_observation():
  memory, maps = fixture()
  probe(memory, maps)
  for address, size in [(HEAP + 0x1000 + 0x48, 16), (HEAP + 0x4000, 40),
                        (HEAP + 0xc000, 16), (HEAP + 0xd000, 24),
                        (HEAP + 0x6000 + 0xb00, 36), (HEAP + 0x6000 + 0x1138, 8)]:
    assert memory.reads.count((address, size)) == 2
  assert memory.reads.count((GBL + 0x2985830, 8)) == 1
  assert memory.reads.count((DICE + 0x3d28040 + 0x20, 8)) == 1
  assert memory.reads.count((DICE + 0x3d29828 + 0x20, 8)) == 1


def test_writable_library_constants_are_not_cached_or_trusted():
  memory, maps = fixture()
  result = probe(memory, maps.replace("r-xp", "rwxp"))
  assert result["status"] == "unavailable"
  assert result["reason"] == "immutable_mapping_not_read_only"


@pytest.mark.parametrize("nav_type", [1, 2, 99])
def test_non_gps_type_is_never_reported_as_gps(nav_type):
  memory, maps = fixture()
  memory.u32(HEAP + 0x6000 + 0x1138, nav_type)
  result = probe(memory, maps)
  assert result["navigation_running_observed"] is True
  assert result["gps_navigation_observed"] is False


@pytest.mark.parametrize(("address", "value", "reason"), [
  (GBL + 0x2aad120, 1, "unaligned_pointer"),
  (HEAP + 0x1000 + 0x50, 0, "registry_bucket_count"),
  (HEAP + 0x1000 + 0x50, 10000000, "registry_bucket_count"),
  (HEAP + 0x5000 + 0x130, HEAP + 0x200000, "unmapped_read"),
  (HEAP + 0xc000, DICE + 0x3d28048, "drive_path_vtable_mismatch"),
  (HEAP + 0xd000, DICE + 0x3d29830, "drive_impl_vtable_mismatch"),
])
def test_bad_pointer_chain_fails_closed(address, value, reason):
  memory, maps = fixture()
  memory.u64(address, value)
  result = probe(memory, maps)
  assert result["status"] == "unavailable"
  assert result["reason"] == reason
  assert result["path_identity_hash"] is None
  assert not result["navigation_running_observed"]


def test_registry_cycle_is_bounded():
  memory, maps = fixture()
  memory.u64(HEAP + 0x4000 + 0x10, GBL + 0x2985840)
  memory.u64(GBL + 0x2985848, GBL + 0x7e604d)
  memory.u64(HEAP + 0x4000, HEAP + 0x4000)
  result = probe(memory, maps)
  assert result["reason"] == "registry_cycle"
  assert len(memory.reads) < 50


def test_index_out_of_bounds_is_not_clamped():
  memory, maps = fixture()
  memory.u32(HEAP + 0x6000 + 0xb20, 3)
  assert probe(memory, maps)["reason"] == "main_index_out_of_bounds"


def test_selection_race_invalidates_entire_snapshot():
  memory, maps = fixture()
  reads = 0
  def change(fake, address, size):
    nonlocal reads
    if address == HEAP + 0x6000 + 0xb00:
      reads += 1
      if reads == 2:
        fake.u32(HEAP + 0x6000 + 0xb20, 2)
  memory.mutate = change
  result = probe(memory, maps)
  assert result["status"] == "unavailable"
  assert result["path_identity_hash"] is None


def test_path_id_change_is_not_hidden_by_constant_cache():
  memory, maps = fixture()
  seen = 0
  def change(fake, address, size):
    nonlocal seen
    if address == HEAP + 0xd000:
      seen += 1
      if seen == 2:
        fake.u32(HEAP + 0xd000 + 0x14, 7654321)
  memory.mutate = change
  result = probe(memory, maps)
  assert result["reason"] == "selection_changed_during_read"
  assert result["path_identity_hash"] is None
  assert result["gps_navigation_observed"] is False


def test_window_reports_both_ends_without_extending_lease():
  memory, maps = fixture()
  result = probe(memory, maps)
  assert result["observation_finished_monotonic_ns"] >= result["observation_started_monotonic_ns"]
  duration = result["observation_finished_monotonic_ns"] - result["observation_started_monotonic_ns"]
  assert result["observation_window_ms"] == round(duration / 1_000_000, 3)
  assert "lease_expires_at" not in result


def test_epoch_change_is_rejected():
  memory, maps = fixture()
  epochs = iter(("before", "after"))
  result = probe_snapshot(maps, memory.read,
    library_sha256={"libGbl.so": GBL_SHA256, "libAutoDice.so": AUTO_DICE_SHA256},
    process_epoch=lambda: next(epochs))
  assert result["reason"] == "process_epoch_changed"


def test_unknown_binary_never_reads_memory():
  memory, maps = fixture()
  result = probe_snapshot(maps, memory.read, library_sha256={"libGbl.so": "bad"}, process_epoch=lambda: EPOCH)
  assert result["reason"] == "unsupported_binary_profile"
  assert memory.reads == []


def test_missing_library_never_reads_memory():
  memory, maps = fixture()
  result = probe(memory, "\n".join(maps.splitlines()[1:]))
  assert result["reason"] == "library_mapping_missing"
  assert memory.reads == []


def test_wrong_package_mapping_is_rejected():
  memory, maps = fixture()
  result = probe(memory, maps.replace("com.autonavi.amapauto-test", "com.other-test"))
  assert result["reason"] == "library_mapping_missing"


@pytest.mark.parametrize("line", ["bad", "20-10 r--p 0000 00:00 0", "10-20 z--p 0000 00:00 0"])
def test_malformed_maps_fail_closed(line):
  with pytest.raises(ProbeError):
    parse_maps(line)


def test_short_read_does_not_decode_zero_padding():
  memory, maps = fixture()
  result = probe_snapshot(maps, lambda address, size: b"", library_sha256={
    "libGbl.so": GBL_SHA256, "libAutoDice.so": AUTO_DICE_SHA256}, process_epoch=lambda: EPOCH)
  assert result["reason"] == "short_read"


def test_read_exception_is_sanitized():
  _, maps = fixture()
  def fail(address, size):
    raise OSError("sensitive address and contents")
  result = probe_snapshot(maps, fail, library_sha256={
    "libGbl.so": GBL_SHA256, "libAutoDice.so": AUTO_DICE_SHA256}, process_epoch=lambda: EPOCH)
  assert result["reason"] == "read_failed"
  assert "sensitive" not in str(result)
