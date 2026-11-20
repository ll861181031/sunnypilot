package com.jixiexiaoge.drivingassist.navdiag;

import android.app.Activity;
import android.app.Instrumentation;
import android.os.Bundle;

import org.json.JSONObject;

import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.Map;

/** 用纯合成 bytearray 内存验证与真机完全共用的只读算法，不接触高德或 /proc。 */
public final class NativeProbeInstrumentation extends Instrumentation {
  private interface Check { void run() throws Exception; }
  private interface Mutation { void beforeRead(FakeMemory memory, long address, int size); }
  private static final long GBL = 0x1000000000L;
  private static final long DICE = 0x2000000000L;
  private static final long HEAP = 0x3000000000L;
  private static final String EPOCH = "amap-process-test-epoch";
  private int passed;
  private int failed;
  private final StringBuilder failures = new StringBuilder();

  private static final class Bank {
    final long address;
    final byte[] bytes;
    Bank(long address, int size) { this.address = address; bytes = new byte[size]; }
  }

  private static final class FakeMemory implements NativeRouteProbe.MemorySource {
    final ArrayList<Bank> banks = new ArrayList<>();
    final Map<String, Integer> reads = new HashMap<>();
    Mutation mutation;
    int calls;
    int maximumRead;

    FakeMemory() {
      banks.add(new Bank(HEAP, 0x100000));
      banks.add(new Bank(GBL + 0x2aad000, 0x2000));
      banks.add(new Bank(GBL + 0x2985000, 0x2000));
      banks.add(new Bank(DICE + 0x3700000, 0x100000));
      banks.add(new Bank(DICE + 0x3a70000, 0x20000));
      banks.add(new Bank(DICE + 0x3d28000, 0x30000));
      banks.add(new Bank(DICE + 0x3000000, 0x100000));
    }

    Bank bank(long address, int size) {
      for (Bank bank : banks) if (bank.address <= address && address + size <= bank.address + bank.bytes.length) return bank;
      throw new IllegalArgumentException("合成内存地址不在测试bank");
    }

    void u64(long address, long value) {
      Bank bank = bank(address, 8);
      ByteBuffer.wrap(bank.bytes).order(ByteOrder.LITTLE_ENDIAN).putLong((int) (address - bank.address), value);
    }

    void u32(long address, long value) {
      Bank bank = bank(address, 4);
      ByteBuffer.wrap(bank.bytes).order(ByteOrder.LITTLE_ENDIAN).putInt((int) (address - bank.address), (int) value);
    }

    void f64(long address, double value) {
      Bank bank = bank(address, 8);
      ByteBuffer.wrap(bank.bytes).order(ByteOrder.LITTLE_ENDIAN).putDouble((int) (address - bank.address), value);
    }

    void bytes(long address, byte[] value) {
      Bank bank = bank(address, value.length);
      System.arraycopy(value, 0, bank.bytes, (int) (address - bank.address), value.length);
    }

    @Override public byte[] read(long address, int size) {
      calls++;
      maximumRead = Math.max(maximumRead, size);
      String key = address + ":" + size;
      reads.put(key, reads.containsKey(key) ? reads.get(key) + 1 : 1);
      if (mutation != null) mutation.beforeRead(this, address, size);
      Bank bank = bank(address, size);
      byte[] result = new byte[size];
      System.arraycopy(bank.bytes, (int) (address - bank.address), result, 0, size);
      return result;
    }
  }

  private static String maps() {
    return Long.toHexString(GBL) + "-" + Long.toHexString(GBL + 0x2b00000)
        + " r-xp 00000000 08:01 11 /data/app/com.autonavi.amapauto-test/lib/arm64/libGbl.so\n"
        + Long.toHexString(DICE) + "-" + Long.toHexString(DICE + 0x3d28000)
        + " r-xp 00000000 08:01 12 /data/app/com.autonavi.amapauto-test/lib/arm64/libAutoDice.so\n"
        + Long.toHexString(DICE + 0x3d28000) + "-" + Long.toHexString(DICE + 0x3d51000)
        + " r--p 03d28000 08:01 12 /data/app/com.autonavi.amapauto-test/lib/arm64/libAutoDice.so\n"
        + Long.toHexString(HEAP) + "-" + Long.toHexString(HEAP + 0x100000)
        + " rw-p 00000000 00:00 0 [anon:libc_malloc]\n";
  }

  private static Map<String, String> hashes() {
    Map<String, String> result = new HashMap<>();
    result.put("libGbl.so", NativeRouteProbe.GBL_SHA256);
    result.put("libAutoDice.so", NativeRouteProbe.DICE_SHA256);
    return result;
  }

  private static FakeMemory fixture() {
    FakeMemory memory = new FakeMemory();
    long registry = HEAP + 0x1000;
    long buckets = HEAP + 0x2000;
    long previous = HEAP + 0x3000;
    long node = HEAP + 0x4000;
    long wrapper = HEAP + 0x5000;
    long manager = HEAP + 0x6000;
    long key = GBL + 0x7e6024;
    memory.u64(GBL + 0x2aad120, registry);
    memory.u64(registry + 0x48, buckets);
    memory.u64(registry + 0x50, 8);
    memory.u64(buckets + (key % 8) * 8, previous);
    memory.u64(previous, node);
    memory.u64(node, 0);
    memory.u64(node + 8, key);
    memory.u64(node + 0x10, GBL + 0x2985828);
    memory.u64(GBL + 0x2985830, key);
    memory.u64(node + 0x20, wrapper);
    memory.u64(wrapper + 0x130, manager);
    long paths = HEAP + 0x9000;
    long a = HEAP + 0xa000;
    long b = HEAP + 0xb000;
    long c = HEAP + 0xc000;
    long d = HEAP + 0xd000;
    memory.u64(manager + 0xb00, paths);
    memory.u32(manager + 0xb0c, 3);
    memory.u32(manager + 0xb20, 1);
    memory.u32(manager + 0x1138, 0);
    memory.u32(manager + 0x113c, 1);
    memory.u64(paths + 0x50 + 0x48, a);
    memory.u64(a + 8, b);
    memory.u64(b + 0x48, c);
    memory.u64(c, DICE + NativeRouteProbe.DRIVE_PATH_VTABLE_RVA);
    memory.u64(c + 8, d);
    memory.u64(d, DICE + NativeRouteProbe.DRIVE_IMPLEMENTATION_VTABLE_RVA);
    memory.u32(d + 0x14, 1234567);
    memory.u64(DICE + NativeRouteProbe.DRIVE_PATH_VTABLE_RVA + 0x20,
               DICE + NativeRouteProbe.DRIVE_PATH_GETTER_RVA);
    memory.u64(DICE + NativeRouteProbe.DRIVE_IMPLEMENTATION_VTABLE_RVA + 0x20,
               DICE + NativeRouteProbe.DRIVE_PATH_ID_GETTER_RVA);
    memory.u64(DICE + NativeRouteProbe.DRIVE_IMPLEMENTATION_VTABLE_RVA + 0xb8,
               DICE + NativeRouteProbe.DRIVE_SEGMENT_COUNT_GETTER_RVA);
    return memory;
  }

  private static JSONObject probe(FakeMemory memory) {
    return NativeRouteProbe.probeSnapshot(maps(), memory, hashes(), () -> EPOCH, NativeRouteProbe.PROFILE);
  }

  private static FakeMemory positionFixture() {
    FakeMemory memory = fixture();
    long manager = HEAP + 0x6000;
    memory.u32(manager + 0x38, 1);
    memory.u32(manager + 0x40, 1);
    memory.u32(manager + 0x44, 0);
    memory.u32(manager + 0x48, 0);
    memory.f64(manager + 0x60, 11.25);
    memory.f64(manager + 0x68, 22.5);
    memory.f64(manager + 0x70, 7);
    memory.u64(manager + 0x1b0, 1001000);
    memory.u64(manager + 0x1b8, 1000000);
    memory.u32(manager + 0x360, 2);
    for (int index = 0; index < 2; index++) {
      long item = manager + 0x1c8 + index * 0x88;
      memory.f64(item, 11.2501 + index / 10000.0);
      memory.f64(item + 8, 22.5001);
      memory.f64(item + 16, 7);
      memory.u32(item + 0x68, 2);
      memory.u32(item + 0x6c, 3);
      memory.u32(item + 0x70, 4);
      memory.u32(item + 0x74, 1);
      memory.u32(item + 0x78, index == 0 ? 7654321 : 1234567);
    }
    return memory;
  }

  private static JSONObject positionProbe(FakeMemory memory) {
    return NativeRouteProbe.probeSnapshot(maps(), memory, hashes(), () -> EPOCH, NativeRouteProbe.PROFILE, true);
  }

  private static FakeMemory naviInfoFixture() {
    FakeMemory memory = positionFixture();
    long manager = HEAP + 0x6000;
    // items 向量放在 HEAP+0x8000，避开 manager 字段区（0x6aa0/0x6aac/0x6b00/0x7138）
    // 以及 position 项区（0x61c8..），否则 branch 数据会覆盖 navType/navState。
    long items = HEAP + 0x8000;
    long segments = HEAP + 0x90000;
    long segment0 = HEAP + 0x92000;
    long segmentVtable = DICE + 0x3d50000;
    memory.u64(manager + 0xaa0, items);
    memory.u32(manager + 0xaac, 3);
    memory.u64(HEAP + 0xd000 + 0x60, segments);
    memory.u64(HEAP + 0xd000 + 0x68, segments + 24);
    memory.u64(segments, segment0);
    memory.u64(segments + 8, segment0);
    memory.u64(segments + 16, segment0);
    memory.u64(segment0, segmentVtable);
    memory.u64(segmentVtable + 0xb8, DICE + 0x3a7a0b8L);
    memory.u64(segmentVtable + 0x128, DICE + 0x3a86180L);
    for (int index = 0; index < 3; index++) {
      long item = items + index * NativeRouteProbe.NAVI_INFO_ITEM_SIZE;
      memory.u32(item, index == 0 ? 9000001L : index == 1 ? 1234567L : 9000002L);
      memory.u32(item + 4, 7);
      memory.u32(item + 8, 100 + index);
      memory.u32(item + 12, 1000 + index);
      memory.u32(item + 0x60, 2);
      memory.u32(item + 0x64, 3);
      memory.u32(item + 0x68, 4);
      memory.u32(item + 0xd0, index == 1 ? 1 : 0);
      // branch0 内联在 item+0xd8，branch1 内联在 item+0x118，各 64 字节。
      // maneuver 字段相对分支起点：id@0、seg@4、link@8、point@12、exit_num@16、
      // exit_angle@20、obtain_supported@24(byte)、data_changed@25(byte)、
      // segment_remain_time@28、segment_remain_distance@32、next_road_seg@48、
      // next_road_link@52、split@56。
      memory.u32(item + 0xd8 + 0x00, 14 + index); // branch0.maneuver_id
      memory.u32(item + 0xd8 + 0x04, 2);
      memory.u32(item + 0xd8 + 0x08, 3);
      memory.u32(item + 0xd8 + 0x0c, 4);
      memory.u32(item + 0xd8 + 0x10, 2);
      memory.u32(item + 0xd8 + 0x14, 90);
      memory.bank(item + 0xd8, 60).bytes[(int) (item + 0xd8 - memory.bank(item + 0xd8, 60).address) + 24] = 1;
      memory.u32(item + 0xd8 + 0x1c, 40);
      memory.u32(item + 0xd8 + 0x20, 500);
      memory.u32(item + 0xd8 + 0x30, 5);
      memory.u32(item + 0xd8 + 0x34, 6);
      memory.u32(item + 0xd8 + 0x38, 0);
      memory.u32(item + 0x118 + 0x00, 14 + index); // branch1.maneuver_id
      memory.u32(item + 0x118 + 0x04, 2);
      memory.u32(item + 0x118 + 0x08, 3);
      memory.u32(item + 0x118 + 0x0c, 4);
      memory.u32(item + 0x118 + 0x10, 2);
      memory.u32(item + 0x118 + 0x14, 90);
      memory.bank(item + 0x118, 60).bytes[(int) (item + 0x118 - memory.bank(item + 0x118, 60).address) + 24] = 1;
      memory.u32(item + 0x118 + 0x1c, 40);
      memory.u32(item + 0x118 + 0x20, 500);
      memory.u32(item + 0x118 + 0x30, 5);
      memory.u32(item + 0x118 + 0x34, 6);
      memory.u32(item + 0x118 + 0x38, 0);
    }
    return memory;
  }

  private static JSONObject naviInfoProbe(FakeMemory memory) {
    return NativeRouteProbe.probeSnapshot(maps(), memory, hashes(), () -> EPOCH, NativeRouteProbe.PROFILE,
        false, true);
  }

  private static JSONObject naviInfoProbe(NativeRouteProbe.MemorySource source) {
    return NativeRouteProbe.probeSnapshot(maps(), source, hashes(), () -> EPOCH, NativeRouteProbe.PROFILE,
        false, true);
  }

  private static void untrustedPosition(JSONObject value) throws Exception {
    for (String key : new String[] {"coordinate_frame_verified", "source_freshness_verified", "raw_gps_verified",
        "route_anchor_verified", "route_render_input_allowed", "control_input_allowed"}) {
      require(!value.getBoolean(key), "私有位置被错误升级认证：" + key);
    }
    require(!value.has("path_id") && !value.has("chain"), "私有位置泄露原始身份或地址");
  }

  private static void positionUnavailable(JSONObject result, String reason) throws Exception {
    require(result.getString("status").equals("observed"), "位置不可用破坏已观测路线身份");
    JSONObject value = result.getJSONObject("_position");
    require(value.getString("status").equals("unavailable") && value.getString("reason").equals(reason),
        "位置没有按固定原因拒绝：" + reason);
    require(!value.has("native_st_pos_deg") && !value.has("matched_st_pos_deg"), "拒绝时仍输出位置");
    untrustedPosition(value);
  }

  private static void untrustedNaviInfo(JSONObject result) throws Exception {
    JSONObject value = result.getJSONObject("_navi_info");
    require(value.getBoolean("diagnostic_only") && !value.getBoolean("control_input_allowed")
        && !value.getBoolean("route_render_input_allowed"), "动作影子被错误放行");
    String text = result.toString();
    require(!text.contains(Long.toString(HEAP)) && !text.contains("1234567")
        && !text.contains("9000001") && !text.contains("9000002")
        && !text.contains("11.25") && !text.contains("22.5"), "动作影子泄露PathID、地址或坐标");
  }

  private static void require(boolean value, String message) {
    if (!value) throw new AssertionError(message);
  }

  private static void unavailable(JSONObject value, String reason) throws Exception {
    require(value.getString("status").equals("unavailable") && value.getString("reason").equals(reason),
        "错误没有封闭返回：" + reason + " / " + value.getString("reason"));
    require(value.isNull("path_identity_hash") && !value.getBoolean("gps_navigation_observed")
        && !value.getBoolean("active_route_verified") && !value.getBoolean("control_input_allowed"), "失败时仍放行身份或控制");
  }

  private static void naviBindingUnavailable(JSONObject result, String reason) throws Exception {
    JSONObject navi = result.getJSONObject("_navi_info");
    JSONObject binding = navi.getJSONArray("branches").getJSONObject(1)
        .getJSONObject("segment_binding");
    require(result.getString("status").equals("observed") && navi.getString("status").equals("observed")
        && navi.getString("reason").equals("unverified_navi_info_semantics")
        && navi.getBoolean("diagnostic_only") && !navi.getBoolean("control_input_allowed")
        && !navi.getBoolean("route_render_input_allowed")
        && binding.getString("status").equals("unavailable")
        && binding.getString("reason").equals(reason),
        "分支绑定失败没有局部降级：" + reason + " / " + result);
  }

  @Override public void onCreate(Bundle arguments) { super.onCreate(arguments); start(); }

  @Override public void onStart() {
    check("native_exact_selected_identity_and_29_reads", this::selectedIdentity);
    check("native_mutable_fields_read_twice", this::mutableReads);
    check("native_states_are_observed_only", this::states);
    check("native_non_gps_is_not_gps", this::nonGps);
    check("native_empty_path_is_inactive", this::emptyPath);
    check("native_wrong_hash_never_reads", this::wrongHash);
    check("native_writable_constants_rejected", this::writableConstants);
    check("native_pointer_alignment_and_bounds", this::badPointers);
    check("native_vtables_and_getters_checked", this::vtableChecks);
    check("native_registry_cycle_bounded", this::registryCycle);
    check("native_index_not_clamped", this::indexBounds);
    check("native_path_id_race_rejected", this::pathRace);
    check("native_header_race_diagnosed_without_values", this::headerRaceDiagnosis);
    check("native_navigation_state_race_diagnosed", this::stateRaceDiagnosis);
    check("native_process_restart_rejected", this::epochRace);
    check("native_read_budgets_enforced", this::readBudgets);
    check("native_short_and_failed_reads_sanitized", this::shortReads);
    check("native_maps_and_package_checked", this::mapChecks);
    check("native_unsigned_path_id_matches_python", this::unsignedIdentity);
    check("position_opt_in_preserves_29_reads", this::positionOptIn);
    check("position_selects_unique_current_path", this::positionSelected);
    check("position_usage_and_simulation_flags", this::positionFlags);
    check("position_match_count_and_identity", this::positionMatchChecks);
    check("position_exact_onroad_one_only", this::positionOnRoad);
    check("position_negative_indices_rejected", this::positionIndices);
    check("position_invalid_coordinates_rejected", this::positionCoordinates);
    check("position_source_ticks_required", this::positionTicks);
    check("position_unsigned_ticks_preserved", this::positionUnsignedTicks);
    check("position_race_invalidates_whole_snapshot", this::positionRace);
    check("position_retry_tick_recovers_exact_third_observation", this::positionRetryTick);
    check("position_retry_both_ticks_recovers_exact_third_observation", this::positionRetryBothTicks);
    check("position_retry_coordinate_and_tick_recovers", this::positionRetryCoordinate);
    check("position_retry_coordinate_and_both_ticks_recovers", this::positionRetryCoordinateBothTicks);
    check("position_retry_native_matched_tick_recovers", this::positionRetryNativeMatchedTick);
    check("position_retry_native_matched_both_ticks_recovers", this::positionRetryNativeMatchedBothTicks);
    check("navi_info_retry_native_matched_tick_recovers", this::naviInfoRetryNativeMatchedTick);
    check("navi_info_retry_native_matched_both_ticks_recovers", this::naviInfoRetryNativeMatchedBothTicks);
    check("position_retry_native_matched_third_changes_rejected", this::positionRetryNativeMatchedThirdChanges);
    check("position_retry_native_matched_other_fields_rejected", this::positionRetryNativeMatchedOtherFields);
    check("position_retry_native_matched_limits_preserved", this::positionRetryNativeMatchedLimits);
    check("position_retry_native_matched_requires_tick", this::positionRetryNativeMatchedRequiresTick);
    check("position_retry_both_ticks_third_change_rejected", this::positionRetryBothTicksThirdChanges);
    check("position_retry_both_ticks_extra_anchor_change_rejected", this::positionRetryBothTicksExtraFields);
    check("position_retry_uses_one_cumulative_read_budget", this::positionRetryThreeMatches);
    check("position_retry_continuous_tick_rejects_after_third", this::positionRetryContinuous);
    check("position_retry_does_not_cover_other_fields", this::positionRetryOtherFields);
    check("position_retry_third_route_or_position_change_rejected", this::positionRetryThirdChanges);
    check("position_retry_third_epoch_change_rejected", this::positionRetryEpoch);
    check("position_retry_third_read_failure_rejected", this::positionRetryReadFailure);
    check("position_retry_deadline_before_third_read", this::positionRetryDeadlineBefore);
    check("position_retry_deadline_after_blocking_read", this::positionRetryDeadlineAfter);
    check("position_retry_deadline_after_final_epoch", this::positionRetryDeadlineEpoch);
    check("position_no_retry_keeps_original_slow_path", this::positionNoRetrySlow);
    check("position_stopped_and_non_gps_hide_coordinates", this::positionInactive);
    check("position_repeat_does_not_create_freshness", this::positionRepeated);
    check("position_first_match_not_assumed", this::positionMatchOrder);
    check("position_read_failure_hides_partial_coordinates", this::positionReadFailure);
    check("navi_info_selects_unique_current_path", this::naviInfoSelected);
    check("navi_info_counts_and_range_are_bounded", this::naviInfoCounts);
    check("navi_info_missing_match_keeps_independent_route_and_position", this::naviInfoMissingIsolated);
    check("navi_info_missing_match_transition_rejects_whole_snapshot", this::naviInfoMissingRace);
    check("navi_info_missing_match_does_not_hide_parent_failure", this::naviInfoMissingParentFailure);
    check("navi_info_empty_list_keeps_independent_route_and_position", this::naviInfoEmptyIsolated);
    check("navi_info_empty_list_transition_rejects_whole_snapshot", this::naviInfoEmptyRace);
    check("navi_info_empty_list_does_not_hide_parent_failure", this::naviInfoEmptyParentFailure);
    check("navi_info_branch_pointers_are_checked", this::naviInfoPointers);
    check("navi_info_fields_are_nonnegative_and_exact", this::naviInfoFields);
    check("navi_info_next_road_rejects_actions_without_losing_route", this::naviInfoNextRoadIsolated);
    check("navi_info_next_road_raw_changes_reject_whole_snapshot", this::naviInfoNextRoadRace);
    check("navi_info_next_road_does_not_hide_read_or_epoch_failure", this::naviInfoNextRoadFailures);
    check("navi_info_next_road_recovers_without_sticky_quarantine", this::naviInfoNextRoadRecovery);
    check("navi_info_next_road_preserves_existing_boundaries", this::naviInfoNextRoadBoundaries);
    check("navi_info_next_road_cannot_mask_binding_race", this::naviInfoNextRoadBindingRace);
    check("navi_info_segment_binding_fails_closed", this::naviInfoSegmentBinding);
    check("navi_info_race_is_never_retried", this::naviInfoRace);
    check("navi_info_position_tick_retry_recovers", this::naviInfoPositionRetryTick);
    check("navi_info_position_coordinate_tick_retry_recovers", this::naviInfoPositionRetryCoordinate);
    check("navi_info_position_coordinate_retry_limits", this::naviInfoPositionRetryLimits);
    check("navi_info_position_retry_rejects_other_fields", this::naviInfoPositionRetryOtherFields);
    check("navi_info_position_retry_rejects_third_changes", this::naviInfoPositionRetryThirdChanges);
    check("navi_info_opt_in_preserves_default_reads", this::naviInfoOptIn);
    check("navi_info_inactive_navigation_hides_actions", this::naviInfoInactive);
    check("geometry_v2_full_window", this::geometryWindow);
    check("geometry_v2_compact_shared_endpoints", this::geometryCompact);
    check("geometry_v2_deep_overlap_compact", this::geometryDeepOverlap);
    check("geometry_v2_overlap_content_rejected", this::geometryOverlapMismatch);
    check("geometry_v2_empty_link_rejected", this::geometryEmptyLink);
    check("geometry_v2_discontinuity_rejected", this::geometryDiscontinuity);
    check("geometry_rejection_records_exact_join", this::geometryRejectionJoin);
    check("geometry_rejection_identifies_second_read", this::geometryRejectionSecondRead);
    check("geometry_rejection_context_is_bounded", this::geometryRejectionBounded);
    check("geometry_rejection_does_not_survive_recovery", this::geometryRejectionRecovery);
    check("phone_geometry_rejection_is_consumed_once", this::geometryRejectionCaptureOnce);
    check("geometry_v2_out_of_range_rejected", this::geometryRange);
    check("geometry_backing_count_zero_rejected_before_points", this::geometryBackingZero);
    check("geometry_backing_window_overrun_rejected_before_points", this::geometryBackingOverrun);
    check("geometry_backing_nonzero_start_overrun_rejected", this::geometryBackingOffsetOverrun);
    check("geometry_backing_shrink_second_read_rejected", this::geometryBackingShrink);
    check("geometry_backing_exact_end_and_unsigned_count", this::geometryBackingExactEnd);
    check("geometry_v2_two_reads_must_match", this::geometryReadRace);
    check("geometry_v2_selection_race_rejected", this::geometrySelectionRace);
    check("geometry_v2_epoch_race_rejected", this::geometryEpochRace);
    check("geometry_v2_inactive_navigation_rejected", this::geometryInactive);
    check("maps_parser_preserves_fields_and_sorting", this::mapParserFields);
    Bundle result = new Bundle();
    result.putInt("tests_passed", passed);
    result.putInt("tests_failed", failed);
    if (failed != 0) result.putString("error", failures.toString());
    finish(failed == 0 ? Activity.RESULT_OK : Activity.RESULT_CANCELED, result);
  }

  private void check(String name, Check action) {
    Bundle status = new Bundle();
    status.putString("test", name);
    try {
      action.run();
      passed++;
      status.putString("result", "passed");
    } catch (Throwable error) {
      failed++;
      String message = error.getClass().getSimpleName() + ": " + error.getMessage();
      failures.append(name).append(':').append(message).append('\n');
      status.putString("result", "failed");
      status.putString("error", message);
    }
    sendStatus(0, status);
  }

  private void selectedIdentity() throws Exception {
    FakeMemory memory = fixture();
    JSONObject result = probe(memory);
    require(result.getString("status").equals("observed") && result.getInt("main_index") == 1
        && result.getInt("path_count") == 3 && result.getBoolean("gps_navigation_observed"), "选中路线读取错误");
    require(result.getString("path_identity_hash").equals("deb576695affddd90ec337f246e5e0c0bbc77251c89ecdc9ecc6bca1f0a6e411"),
        "与Python相同的合成PathID哈希不一致");
    require(result.getInt("read_calls") == 29 && memory.calls == 29 && memory.maximumRead <= 64
        && result.getInt("read_bytes") < 4096, "固定读取链或预算变化");
    String text = result.toString();
    require(!text.contains("1234567") && !text.contains(Long.toString(GBL))
        && !text.contains(Long.toString(HEAP)) && !result.getBoolean("active_route_verified")
        && !result.getBoolean("control_input_allowed") && !result.getBoolean("atomic_snapshot"), "输出泄露原始身份或错误认证控制");
  }

  private void mutableReads() {
    FakeMemory memory = fixture();
    probe(memory);
    for (long[] pair : new long[][] {{HEAP + 0x1048, 16}, {HEAP + 0x4000, 40}, {HEAP + 0xc000, 16},
        {HEAP + 0xd000, 24}, {HEAP + 0x6b00, 36}, {HEAP + 0x7138, 8}}) {
      require(memory.reads.get(pair[0] + ":" + pair[1]) == 2, "可变对象没有双读");
    }
    require(memory.reads.get((GBL + 0x2985830) + ":8") == 1, "只读常量未按轮缓存");
  }

  private void states() throws Exception {
    String[] names = {"unknown", "started", "paused", "resumed", "stopped", "arrived", "unknown"};
    for (int state = 0; state <= 6; state++) {
      FakeMemory memory = fixture();
      memory.u32(HEAP + 0x713c, state);
      JSONObject result = probe(memory);
      require(result.getString("navigation_state").equals(names[state])
          && result.getBoolean("navigation_running_observed") == (state == 1 || state == 3), "导航状态被推测或混淆");
    }
  }

  private void nonGps() throws Exception {
    FakeMemory memory = fixture();
    memory.u32(HEAP + 0x7138, 1);
    JSONObject result = probe(memory);
    require(result.getBoolean("navigation_running_observed") && !result.getBoolean("gps_navigation_observed"), "非GPS导航误标GPS");
  }

  private void emptyPath() throws Exception {
    FakeMemory memory = fixture();
    memory.u64(HEAP + 0x6b00, 0);
    memory.u32(HEAP + 0x6b0c, 0);
    memory.u32(HEAP + 0x6b20, 0);
    memory.u32(HEAP + 0x713c, 4);
    JSONObject result = probe(memory);
    require(result.isNull("path_identity_hash") && !result.getBoolean("navigation_running_observed")
        && result.getInt("read_calls") == 17, "无路线时误报身份");
  }

  private void wrongHash() throws Exception {
    FakeMemory memory = fixture();
    Map<String, String> hash = hashes();
    hash.put("libGbl.so", "unknown");
    unavailable(NativeRouteProbe.probeSnapshot(maps(), memory, hash, () -> EPOCH, NativeRouteProbe.PROFILE), "unsupported_binary_profile");
    require(memory.calls == 0, "未知版本仍读取内存");
  }

  private void writableConstants() throws Exception {
    unavailable(NativeRouteProbe.probeSnapshot(maps().replace("r-xp", "rwxp"), fixture(), hashes(), () -> EPOCH,
        NativeRouteProbe.PROFILE), "immutable_mapping_not_read_only");
  }

  private void badPointers() throws Exception {
    long[][] cases = {{GBL + 0x2aad120, 1}, {HEAP + 0x1050, 0}, {HEAP + 0x1050, 10000000},
        {HEAP + 0x5130, HEAP + 0x200000}};
    String[] reasons = {"unaligned_pointer", "registry_bucket_count", "registry_bucket_count", "unmapped_read"};
    for (int i = 0; i < cases.length; i++) {
      FakeMemory memory = fixture();
      memory.u64(cases[i][0], cases[i][1]);
      unavailable(probe(memory), reasons[i]);
    }
  }

  private void vtableChecks() throws Exception {
    long[][] cases = {{HEAP + 0xc000, DICE + 0x3d28048}, {HEAP + 0xd000, DICE + 0x3d29830},
        {DICE + 0x3d28060, DICE + 0x37709a8}, {DICE + 0x3d29848, DICE + 0x3792380}};
    String[] reasons = {"drive_path_vtable_mismatch", "drive_impl_vtable_mismatch", "drive_path_getter_mismatch", "drive_impl_getter_mismatch"};
    for (int i = 0; i < cases.length; i++) {
      FakeMemory memory = fixture();
      memory.u64(cases[i][0], cases[i][1]);
      unavailable(probe(memory), reasons[i]);
    }
  }

  private void registryCycle() throws Exception {
    FakeMemory memory = fixture();
    memory.u64(HEAP + 0x4010, GBL + 0x2985840);
    memory.u64(GBL + 0x2985848, GBL + 0x7e604d);
    memory.u64(HEAP + 0x4000, HEAP + 0x4000);
    unavailable(probe(memory), "registry_cycle");
    require(memory.calls < 50, "注册表循环未限界");
  }

  private void indexBounds() throws Exception {
    FakeMemory memory = fixture();
    memory.u32(HEAP + 0x6b20, 3);
    unavailable(probe(memory), "main_index_out_of_bounds");
  }

  private void pathRace() throws Exception {
    FakeMemory memory = fixture();
    final int[] seen = {0};
    memory.mutation = (fake, address, size) -> {
      if (address == HEAP + 0xd000 && ++seen[0] == 2) fake.u32(HEAP + 0xd014, 7654321);
    };
    JSONObject result = probe(memory);
    unavailable(result, "selection_changed_during_read");
    JSONObject diagnosis = result.getJSONObject("_selection_diagnosis");
    require(diagnosis.getInt("schema_version") == 1
        && diagnosis.getString("source").equals("native_observation_pair")
        && diagnosis.getJSONArray("changed_fields").toString().equals("[\"path_id\"]")
        && diagnosis.getJSONArray("position_changed_fields").length() == 0
        && !diagnosis.getBoolean("position_serialization_changed"), "路线身份差异诊断不准确");
    require(memory.calls == 29 && !diagnosis.toString().contains("7654321"), "诊断增加读取或泄露PathID");
  }

  private void headerRaceDiagnosis() throws Exception {
    FakeMemory memory = fixture();
    final int[] seen = {0};
    memory.mutation = (fake, address, size) -> {
      if (address == HEAP + 0x6b00 && ++seen[0] == 2) fake.u32(HEAP + 0x6b10, 987654321);
    };
    JSONObject result = probe(memory);
    unavailable(result, "selection_changed_during_read");
    JSONObject diagnosis = result.getJSONObject("_selection_diagnosis");
    require(diagnosis.getJSONArray("changed_fields").toString().equals("[\"header\"]")
        && !diagnosis.toString().contains("987654321") && memory.calls == 29, "原生header差异诊断错误");
  }

  private void stateRaceDiagnosis() throws Exception {
    FakeMemory memory = fixture();
    final int[] seen = {0};
    memory.mutation = (fake, address, size) -> {
      if (address == HEAP + 0x7138 && ++seen[0] == 2) fake.u32(HEAP + 0x713c, 4);
    };
    JSONObject result = probe(memory);
    unavailable(result, "selection_changed_during_read");
    require(result.getJSONObject("_selection_diagnosis").getJSONArray("changed_fields").toString()
        .equals("[\"nav_state\"]") && memory.calls == 29, "导航状态变化被错误识别或增加读取");
  }

  private void epochRace() throws Exception {
    final int[] count = {0};
    unavailable(NativeRouteProbe.probeSnapshot(maps(), fixture(), hashes(),
        () -> ++count[0] == 1 ? "before" : "after", NativeRouteProbe.PROFILE), "process_epoch_changed");
  }

  private void readBudgets() throws Exception {
    NativeRouteProbe.BoundedReader calls = new NativeRouteProbe.BoundedReader(NativeRouteProbe.parseMaps(maps()), fixture());
    for (int i = 0; i < NativeRouteProbe.MAX_READ_CALLS; i++) calls.read(HEAP, 1);
    try { calls.read(HEAP, 1); throw new AssertionError("读取次数预算未生效"); }
    catch (NativeRouteProbe.ProbeException expected) { require(expected.getMessage().equals("read_budget_exceeded"), "次数错误原因丢失"); }
    NativeRouteProbe.BoundedReader bytes = new NativeRouteProbe.BoundedReader(NativeRouteProbe.parseMaps(maps()), fixture());
    for (int i = 0; i < NativeRouteProbe.MAX_READ_BYTES / 64; i++) bytes.read(HEAP, 64);
    try { bytes.read(HEAP, 1); throw new AssertionError("读取字节预算未生效"); }
    catch (NativeRouteProbe.ProbeException expected) { require(expected.getMessage().equals("read_budget_exceeded"), "字节错误原因丢失"); }
    try { bytes.read(HEAP, 65); throw new AssertionError("单读限额未生效"); }
    catch (NativeRouteProbe.ProbeException expected) { require(expected.getMessage().equals("read_budget_exceeded"), "单读错误原因丢失"); }
  }

  private void shortReads() throws Exception {
    unavailable(NativeRouteProbe.probeSnapshot(maps(), (address, size) -> new byte[0], hashes(), () -> EPOCH,
        NativeRouteProbe.PROFILE), "short_read");
    JSONObject value = NativeRouteProbe.probeSnapshot(maps(), (address, size) -> {
      throw new IOExceptionForTest();
    }, hashes(), () -> EPOCH, NativeRouteProbe.PROFILE);
    unavailable(value, "read_failed");
    require(!value.toString().contains("sensitive"), "底层异常泄露");
  }

  private static final class IOExceptionForTest extends Exception {
    IOExceptionForTest() { super("sensitive synthetic address"); }
  }

  private void mapChecks() throws Exception {
    unavailable(NativeRouteProbe.probeSnapshot(maps().replace("com.autonavi.amapauto-test", "com.other-test"),
        fixture(), hashes(), () -> EPOCH, NativeRouteProbe.PROFILE), "library_mapping_missing");
    for (String text : new String[] {"bad", "20-10 r--p 0000 00:00 0", "10-20 z--p 0000 00:00 0",
        "10-30 r--p 0000 00:00 0\n20-40 r--p 0000 00:00 0"}) {
      try { NativeRouteProbe.parseMaps(text); throw new AssertionError("畸形maps未拒绝"); }
      catch (NativeRouteProbe.ProbeException expected) { }
    }
  }

  private void unsignedIdentity() throws Exception {
    FakeMemory memory = fixture();
    memory.u32(HEAP + 0xd014, 0xf1234567L);
    require(probe(memory).getString("path_identity_hash").equals("35afc138509f9f8b1b4939a7a3faa6b0c6000184410c30dd5a21d3cba3f472ae"),
        "uint32 PathID符号扩展或Python哈希不一致");
  }

  private void positionOptIn() throws Exception {
    FakeMemory memory = positionFixture();
    JSONObject result = probe(memory);
    require(!result.has("_position") && memory.calls == 29, "默认命令泄露位置或改变读取链");
    for (String key : memory.reads.keySet()) {
      long address = Long.parseLong(key.split(":")[0]);
      require(address < HEAP + 0x6038 || address >= HEAP + 0x6364, "默认命令读取了LocInfo");
    }
  }

  private void positionSelected() throws Exception {
    JSONObject result = positionProbe(positionFixture());
    JSONObject value = result.getJSONObject("_position");
    require(result.getString("status").equals("observed") && value.getString("status").equals("observed"), "位置未被观测");
    require(value.getJSONArray("native_st_pos_deg").getDouble(0) == 11.25
        && value.getJSONArray("native_st_pos_deg").getDouble(1) == 22.5
        && value.getJSONArray("matched_st_pos_deg").getDouble(0) == 11.2502, "坐标字段被混用或缩放");
    require(value.getInt("segment_index") == 2 && value.getInt("link_index") == 3 && value.getInt("point_index") == 4
        && value.getInt("match_index") == 1 && value.getInt("match_count") == 2, "主路匹配项或索引读取错误");
    require(value.getLong("tick_time_raw") == 1001000 && value.getLong("input_tick_time_raw") == 1000000
        && value.getString("source_clock").equals("unknown"), "源tick被换算或更新");
    require(value.getString("path_identity_hash").equals(result.getString("path_identity_hash")), "位置未关联本轮主路线");
    require(result.getInt("read_calls") == 45 && result.getInt("read_bytes") < 1024, "位置读取超出固定链预算");
    untrustedPosition(value);
  }

  private void positionFlags() throws Exception {
    long[][] cases = {{0x38, 0}, {0x40, 0}, {0x44, 1}, {0x44, 2}};
    String[] reasons = {"position_missing", "position_not_usable", "position_simulated", "position_simulated"};
    for (int index = 0; index < cases.length; index++) {
      FakeMemory memory = positionFixture();
      memory.u32(HEAP + 0x6000 + cases[index][0], cases[index][1]);
      positionUnavailable(positionProbe(memory), reasons[index]);
    }
  }

  private void positionMatchChecks() throws Exception {
    long[][] cases = {{0x360, 0}, {0x360, 4}, {0x2c8, 17}, {0x240, 1234567}};
    String[] reasons = {"position_match_count", "position_match_count", "position_main_path_match_missing",
        "position_main_path_match_ambiguous"};
    for (int index = 0; index < cases.length; index++) {
      FakeMemory memory = positionFixture();
      memory.u32(HEAP + 0x6000 + cases[index][0], cases[index][1]);
      positionUnavailable(positionProbe(memory), reasons[index]);
    }
  }

  private void positionOnRoad() throws Exception {
    for (long value : new long[] {0, 4, 8, 0x20, 0x80, 0x81, 0x100}) {
      FakeMemory memory = positionFixture();
      memory.u32(HEAP + 0x62c4, value);
      positionUnavailable(positionProbe(memory), "position_not_on_route");
    }
  }

  private void positionIndices() throws Exception {
    for (long offset : new long[] {0x2b8, 0x2bc, 0x2c0}) {
      FakeMemory memory = positionFixture();
      memory.u32(HEAP + 0x6000 + offset, 0xffffffffL);
      positionUnavailable(positionProbe(memory), "position_index_invalid");
    }
  }

  private void positionCoordinates() throws Exception {
    double[][] cases = {{Double.NaN, 22, 0}, {11, Double.POSITIVE_INFINITY, 0}, {181, 22, 0},
        {11, -91, 0}, {0, 0, 0}, {11, 22, Double.NaN}};
    for (long offset : new long[] {0x60, 0x250}) {
      for (double[] coordinates : cases) {
        FakeMemory memory = positionFixture();
        for (int index = 0; index < 3; index++) memory.f64(HEAP + 0x6000 + offset + index * 8, coordinates[index]);
        positionUnavailable(positionProbe(memory), "position_coordinate_invalid");
      }
    }
  }

  private void positionTicks() throws Exception {
    for (long offset : new long[] {0x1b0, 0x1b8}) {
      FakeMemory memory = positionFixture();
      memory.u64(HEAP + 0x6000 + offset, 0);
      positionUnavailable(positionProbe(memory), "position_source_tick_missing");
    }
  }

  private void positionUnsignedTicks() throws Exception {
    FakeMemory memory = positionFixture();
    memory.u64(HEAP + 0x61b0, -1L);
    require(positionProbe(memory).getJSONObject("_position").get("tick_time_raw").toString()
        .equals("18446744073709551615"), "uint64源tick发生符号扩展或精度丢失");
  }

  private void positionRace() throws Exception {
    for (long address : new long[] {HEAP + 0x6060, HEAP + 0x62b8}) {
      FakeMemory memory = positionFixture();
      final int[] reads = {0};
      memory.mutation = (fake, current, size) -> {
        if (current == address && ++reads[0] == 2) {
          if (address == HEAP + 0x6060) fake.f64(address, 11.3);
          else fake.u32(address, 7);
        }
      };
      JSONObject result = positionProbe(memory);
      unavailable(result, "selection_changed_during_read");
      JSONObject value = result.getJSONObject("_position");
      require(!value.has("native_st_pos_deg") && value.getString("status").equals("unavailable"), "竞态仍泄露部分位置");
      JSONObject diagnosis = result.getJSONObject("_selection_diagnosis");
      String expected = address == HEAP + 0x6060 ? "native_st_pos_deg" : "segment_index";
      require(diagnosis.getJSONArray("changed_fields").toString().equals("[\"position\"]")
          && diagnosis.getBoolean("position_serialization_changed")
          && diagnosis.getJSONArray("position_changed_fields").toString().equals("[\"" + expected + "\"]"),
          "本轮位置差异字段不准确：" + diagnosis);
      require(memory.calls == positionProbe(positionFixture()).getInt("read_calls")
          && !diagnosis.toString().contains("11.3") && !diagnosis.toString().contains("1002000"),
          "位置诊断增加读取或泄露字段值");
    }
  }

  private static void changeTickOnSecond(FakeMemory memory) {
    memory.mutation = (fake, address, size) -> {
      if (address == HEAP + 0x61b0 && fake.reads.get(address + ":" + size) == 2) fake.u64(address, 1002000);
    };
  }

  private static void retryEvidence(JSONObject result, boolean recovered, String fields) throws Exception {
    JSONObject retry = result.getJSONObject("_snapshot_retry");
    require(retry.length() == 5 && retry.getInt("schema_version") == 1
        && retry.getString("source").equals("native_observation_pair")
        && retry.getInt("extra_observations") == 1 && retry.getBoolean("recovered") == recovered,
        "有界重读证据字段不准确：" + retry);
    JSONObject initial = retry.getJSONObject("initial_diagnosis");
    require(initial.getJSONArray("changed_fields").toString().equals("[\"position\"]")
        && initial.getJSONArray("position_changed_fields").toString().equals(fields)
        && initial.getBoolean("position_serialization_changed"), "重读初始原因不准确：" + initial);
    if (recovered) require(!result.has("_selection_diagnosis"), "恢复成功仍带顶层拒绝诊断");
    else require(!result.getJSONObject("_position").has("native_st_pos_deg"), "重读拒绝仍输出部分位置");
  }

  private void positionRetryTick() throws Exception {
    FakeMemory memory = positionFixture();
    changeTickOnSecond(memory);
    JSONObject result = positionProbe(memory);
    require(result.getString("status").equals("observed"), "单次tick竞争没有恢复");
    retryEvidence(result, true, "[\"tick_time_raw\"]");
    require(result.getJSONObject("_position").getLong("tick_time_raw") == 1002000
        && result.getJSONObject("_position").getLong("input_tick_time_raw") == 1000000,
        "没有返回第三次真实观测或重标输入时间");
    require(result.getInt("read_calls") == 66 && result.getInt("read_bytes") == 975
        && memory.calls == 66 && memory.reads.get((HEAP + 0x61b0) + ":16") == 3,
        "重读重置了预算或超过三次观察");
  }

  private void positionRetryBothTicks() throws Exception {
    FakeMemory memory = positionFixture();
    memory.mutation = (fake, address, size) -> {
      if (address == HEAP + 0x61b0 && fake.reads.get(address + ":" + size) == 2) {
        fake.u64(HEAP + 0x61b0, 1002000);
        fake.u64(HEAP + 0x61b8, 1001500);
      }
    };
    JSONObject result = positionProbe(memory);
    require(result.getString("status").equals("observed"), "两个tick同时更新没有恢复");
    retryEvidence(result, true, "[\"tick_time_raw\",\"input_tick_time_raw\"]");
    require(result.getJSONObject("_position").getLong("tick_time_raw") == 1002000
        && result.getJSONObject("_position").getLong("input_tick_time_raw") == 1001500,
        "没有返回第三次真实双tick观测");
    require(result.getInt("read_calls") == 66 && result.getInt("read_bytes") == 975
        && memory.calls == 66 && memory.reads.get((HEAP + 0x61b0) + ":16") == 3,
        "双tick重读重置了预算或超过三次观察");
  }

  private void positionRetryCoordinate() throws Exception {
    FakeMemory memory = positionFixture();
    memory.mutation = (fake, address, size) -> {
      if (address == HEAP + 0x6060 && fake.reads.get(address + ":" + size) == 2) fake.f64(address, 11.3);
      if (address == HEAP + 0x61b0 && fake.reads.get(address + ":" + size) == 2) fake.u64(address, 1002000);
    };
    JSONObject result = positionProbe(memory);
    require(result.getString("status").equals("observed")
        && result.getJSONObject("_position").getJSONArray("native_st_pos_deg").getDouble(0) == 11.3,
        "坐标与tick同次竞争没有恢复到第三次位置");
    retryEvidence(result, true, "[\"native_st_pos_deg\",\"tick_time_raw\"]");
  }

  private static void updateCoordinateAndBothTicks(FakeMemory memory) {
    memory.f64(HEAP + 0x6060, 11.3);
    memory.u64(HEAP + 0x61b0, 1002000);
    memory.u64(HEAP + 0x61b8, 1001500);
  }

  private void positionRetryCoordinateBothTicks() throws Exception {
    FakeMemory memory = positionFixture();
    memory.mutation = (fake, address, size) -> {
      if (address == HEAP + 0x6b00 && fake.reads.get(address + ":" + size) == 2) updateCoordinateAndBothTicks(fake);
    };
    JSONObject result = positionProbe(memory);
    require(result.getString("status").equals("observed"), "位置与两个时钟同次更新没有恢复");
    retryEvidence(result, true, "[\"native_st_pos_deg\",\"tick_time_raw\",\"input_tick_time_raw\"]");
    JSONObject position = result.getJSONObject("_position");
    require(position.getJSONArray("native_st_pos_deg").getDouble(0) == 11.3
        && position.getLong("tick_time_raw") == 1002000 && position.getLong("input_tick_time_raw") == 1001500,
        "三字段重读没有返回完整第三次观测");
    require(memory.calls == 66 && result.getInt("read_calls") == 66 && result.getInt("read_bytes") == 975
        && memory.reads.get((HEAP + 0x6b00) + ":36") == 3, "三字段重读重置累计预算或观察次数");
  }

  private void positionRetryBothTicksThirdChanges() throws Exception {
    for (long target : new long[] {0x6060, 0x61b0, 0x61b8, 0x6250, 0x62b8, 0x713c, 0xd014}) {
      FakeMemory memory = positionFixture();
      memory.mutation = (fake, address, size) -> {
        if (address != HEAP + 0x6b00) return;
        int count = fake.reads.get(address + ":" + size);
        if (count == 2) updateCoordinateAndBothTicks(fake);
        if (count == 3) {
          if (target == 0x6060 || target == 0x6250) fake.f64(HEAP + target, 11.4);
          else if (target == 0x61b0 || target == 0x61b8) fake.u64(HEAP + target, 1005000);
          else fake.u32(HEAP + target, target == 0xd014 ? 7654321 : 3);
        }
      };
      JSONObject result = positionProbe(memory);
      unavailable(result, "selection_changed_during_read");
      retryEvidence(result, false, "[\"native_st_pos_deg\",\"tick_time_raw\",\"input_tick_time_raw\"]");
      require(memory.reads.get((HEAP + 0x6b00) + ":36") == 3, "三字段触发后继续第四次观察");
      JSONObject diagnosis = result.getJSONObject("_selection_diagnosis");
      String expected = target == 0x6060 ? "[\"native_st_pos_deg\"]"
          : target == 0x61b0 ? "[\"tick_time_raw\"]" : target == 0x61b8 ? "[\"input_tick_time_raw\"]"
          : target == 0x6250 ? "[\"matched_st_pos_deg\"]" : target == 0x62b8 ? "[\"segment_index\"]"
          : target == 0x713c ? "[]" : "[\"matched_st_pos_deg\",\"match_index\"]";
      require(diagnosis.getJSONArray("position_changed_fields").toString().equals(expected),
          "三字段触发后的最终差异不准确：" + diagnosis);
    }
  }

  private void positionRetryBothTicksExtraFields() throws Exception {
    for (long target : new long[] {0x62b8, 0x713c, 0xd014}) {
      FakeMemory memory = positionFixture();
      memory.mutation = (fake, address, size) -> {
        if (address == HEAP + 0x6b00 && fake.reads.get(address + ":" + size) == 2) {
          updateCoordinateAndBothTicks(fake);
          if (target == 0x6250) fake.f64(HEAP + target, 11.4);
          else fake.u32(HEAP + target, target == 0xd014 ? 7654321 : 3);
        }
      };
      JSONObject result = positionProbe(memory);
      unavailable(result, "selection_changed_during_read");
      require(!result.has("_snapshot_retry") && memory.reads.get((HEAP + 0x6b00) + ":36") == 2,
          "三字段以外的锚点或路线变化触发重读");
    }
  }

  private static void changeNativeMatchedOnSecond(FakeMemory memory, boolean bothTicks) {
    memory.mutation = (fake, address, size) -> {
      if (address == HEAP + 0x6b00 && fake.reads.get(address + ":" + size) == 2) {
        fake.f64(HEAP + 0x6060, 11.3);
        fake.f64(HEAP + 0x6068, 22.6);
        fake.f64(HEAP + 0x6250, 11.3001);
        fake.f64(HEAP + 0x6258, 22.6001);
        fake.u64(HEAP + 0x61b0, 1002000);
        if (bothTicks) fake.u64(HEAP + 0x61b8, 1001500);
      }
    };
  }

  private void nativeMatchedRetryRecovers(boolean includeNavi, boolean bothTicks) throws Exception {
    FakeMemory memory = includeNavi ? naviInfoFixture() : positionFixture();
    changeNativeMatchedOnSecond(memory, bothTicks);
    JSONObject result = NativeRouteProbe.probeSnapshot(maps(), memory, hashes(), () -> EPOCH,
        NativeRouteProbe.PROFILE, true, includeNavi);
    require(result.getString("status").equals("observed"), "双坐标时钟更新未恢复");
    retryEvidence(result, true, bothTicks
        ? "[\"native_st_pos_deg\",\"matched_st_pos_deg\",\"tick_time_raw\",\"input_tick_time_raw\"]"
        : "[\"native_st_pos_deg\",\"matched_st_pos_deg\",\"tick_time_raw\"]");
    JSONObject position = result.getJSONObject("_position");
    require(position.getJSONArray("native_st_pos_deg").getDouble(0) == 11.3
        && position.getJSONArray("native_st_pos_deg").getDouble(1) == 22.6
        && position.getJSONArray("matched_st_pos_deg").getDouble(0) == 11.3001
        && position.getJSONArray("matched_st_pos_deg").getDouble(1) == 22.6001
        && position.getLong("tick_time_raw") == 1002000
        && position.getLong("input_tick_time_raw") == (bothTicks ? 1001500 : 1000000),
        "双坐标重读改变精度或没有保留第三次真实观测");
    require(memory.reads.get((HEAP + 0x6b00) + ":36") == 3
        && memory.reads.get((HEAP + 0x6060) + ":24") == 3
        && memory.reads.get((HEAP + 0x6250) + ":24") == 3
        && result.getInt("read_calls") == memory.calls
        && result.getInt("read_calls") <= NativeRouteProbe.MAX_READ_CALLS
        && result.getInt("read_bytes") <= NativeRouteProbe.MAX_READ_BYTES
        && result.getLong("observation_finished_monotonic_ns")
            - result.getLong("observation_started_monotonic_ns") < 50000000L,
        "双坐标重读增加次数、重置预算或重置时钟");
    if (includeNavi) {
      JSONObject initial = result.getJSONObject("_snapshot_retry").getJSONObject("initial_diagnosis");
      require(!initial.getBoolean("navi_info_serialization_changed")
          && initial.getJSONArray("navi_info_changed_fields").length() == 0,
          "双坐标重读掩盖动作变化");
      untrustedNaviInfo(new JSONObject().put("_navi_info", result.getJSONObject("_navi_info")));
    }
  }

  private void positionRetryNativeMatchedTick() throws Exception { nativeMatchedRetryRecovers(false, false); }
  private void positionRetryNativeMatchedBothTicks() throws Exception { nativeMatchedRetryRecovers(false, true); }
  private void naviInfoRetryNativeMatchedTick() throws Exception { nativeMatchedRetryRecovers(true, false); }
  private void naviInfoRetryNativeMatchedBothTicks() throws Exception { nativeMatchedRetryRecovers(true, true); }

  private void positionRetryNativeMatchedThirdChanges() throws Exception {
    for (boolean includeNavi : new boolean[] {false, true}) {
      for (long target : new long[] {0x6060, 0x6250, 0x61b0, 0x62b8, 0x713c, 0xd014}) {
        FakeMemory memory = includeNavi ? naviInfoFixture() : positionFixture();
        changeNativeMatchedOnSecond(memory, true);
        Mutation initial = memory.mutation;
        memory.mutation = (fake, address, size) -> {
          initial.beforeRead(fake, address, size);
          if (address == HEAP + 0x6b00 && fake.reads.get(address + ":" + size) == 3) {
            if (target == 0x6060 || target == 0x6250) fake.f64(HEAP + target, 11.4);
            else if (target == 0x61b0) fake.u64(HEAP + target, 1005000);
            else fake.u32(HEAP + target, target == 0xd014 ? 7654321 : 3);
            if (includeNavi && target == 0xd014) fake.u32(HEAP + 0x8000 + NativeRouteProbe.NAVI_INFO_ITEM_SIZE, 7654321);
          }
        };
        JSONObject result = NativeRouteProbe.probeSnapshot(maps(), memory, hashes(), () -> EPOCH,
            NativeRouteProbe.PROFILE, true, includeNavi);
        unavailable(result, "selection_changed_during_read");
        retryEvidence(result, false,
            "[\"native_st_pos_deg\",\"matched_st_pos_deg\",\"tick_time_raw\",\"input_tick_time_raw\"]");
        require(memory.reads.get((HEAP + 0x6b00) + ":36") == 3,
            "双坐标第三次变化触发第四次观察");
        if (includeNavi) require(!result.getJSONObject("_navi_info").has("branches"), "拒绝泄露部分动作");
      }
    }
  }

  private void positionRetryNativeMatchedOtherFields() throws Exception {
    for (boolean includeNavi : new boolean[] {false, true}) {
      for (long target : new long[] {0x62b8, 0x62bc, 0x62c0, 0x713c, 0xd014}) {
        FakeMemory memory = includeNavi ? naviInfoFixture() : positionFixture();
        changeNativeMatchedOnSecond(memory, true);
        Mutation initial = memory.mutation;
        memory.mutation = (fake, address, size) -> {
          initial.beforeRead(fake, address, size);
          if (address == HEAP + 0x6b00 && fake.reads.get(address + ":" + size) == 2) {
            fake.u32(HEAP + target, target == 0xd014 ? 7654321 : target == 0x713c ? 3 : 7);
            if (includeNavi && target == 0xd014) fake.u32(HEAP + 0x8000 + NativeRouteProbe.NAVI_INFO_ITEM_SIZE, 7654321);
          }
        };
        JSONObject result = NativeRouteProbe.probeSnapshot(maps(), memory, hashes(), () -> EPOCH,
            NativeRouteProbe.PROFILE, true, includeNavi);
        unavailable(result, "selection_changed_during_read");
        require(!result.has("_snapshot_retry") && memory.reads.get((HEAP + 0x6b00) + ":36") == 2,
            "双坐标重读覆盖锚点或路线变化");
      }
    }
  }

  private void positionRetryNativeMatchedLimits() throws Exception {
    for (String mode : new String[] {"before", "after", "final_epoch", "epoch_changed", "epoch_failed", "read_failed"}) {
      FakeMemory memory = naviInfoFixture();
      changeNativeMatchedOnSecond(memory, true);
      final int[] epochs = {0};
      JSONObject result = NativeRouteProbe.probeSnapshot(maps(), (address, size) -> {
        boolean thirdHeader = address == HEAP + 0x6b00
            && Integer.valueOf(2).equals(memory.reads.get(address + ":" + size));
        if ((mode.equals("before") && memory.calls == 0) || (mode.equals("after") && thirdHeader)) {
          android.os.SystemClock.sleep(65);
        }
        if (mode.equals("read_failed") && thirdHeader) throw new IllegalStateException("test_failure");
        return memory.read(address, size);
      }, hashes(), () -> {
        if (++epochs[0] == 3) {
          if (mode.equals("final_epoch")) android.os.SystemClock.sleep(65);
          if (mode.equals("epoch_changed")) return "restarted";
          if (mode.equals("epoch_failed")) throw new IllegalStateException("test_failure");
        }
        return EPOCH;
      }, NativeRouteProbe.PROFILE, true, true);
      String reason = mode.equals("epoch_changed") ? "process_epoch_changed"
          : mode.equals("epoch_failed") ? "process_epoch_failed"
          : mode.equals("read_failed") ? "read_failed" : "snapshot_retry_deadline_exceeded";
      unavailable(result, reason);
      retryEvidence(result, false,
          "[\"native_st_pos_deg\",\"matched_st_pos_deg\",\"tick_time_raw\",\"input_tick_time_raw\"]");
      require(memory.reads.get((HEAP + 0x6b00) + ":36") <= 3
          && result.getInt("read_calls") <= NativeRouteProbe.MAX_READ_CALLS
          && result.getInt("read_bytes") <= NativeRouteProbe.MAX_READ_BYTES
          && !result.getJSONObject("_navi_info").has("branches"), "双坐标重读突破累计预算");
    }
  }

  private void positionRetryNativeMatchedRequiresTick() throws Exception {
    for (boolean includeNavi : new boolean[] {false, true}) {
      FakeMemory memory = includeNavi ? naviInfoFixture() : positionFixture();
      memory.mutation = (fake, address, size) -> {
        if (address == HEAP + 0x6b00 && fake.reads.get(address + ":" + size) == 2) {
          fake.f64(HEAP + 0x6060, 11.3);
          fake.f64(HEAP + 0x6250, 11.3001);
        }
      };
      JSONObject result = NativeRouteProbe.probeSnapshot(maps(), memory, hashes(), () -> EPOCH,
          NativeRouteProbe.PROFILE, true, includeNavi);
      unavailable(result, "selection_changed_during_read");
      require(!result.has("_snapshot_retry") && memory.reads.get((HEAP + 0x6b00) + ":36") == 2,
          "无时钟的双坐标变化触发重读");
    }
  }

  private void positionRetryThreeMatches() throws Exception {
    FakeMemory memory = positionFixture();
    memory.u32(HEAP + 0x6360, 3);
    memory.u32(HEAP + 0x6350, 7654322);
    changeTickOnSecond(memory);
    JSONObject result = positionProbe(memory);
    require(result.getString("status").equals("observed") && result.getInt("read_calls") == 69
        && result.getInt("read_bytes") == 1035, "三匹配项累计读取预算不准确");
    retryEvidence(result, true, "[\"tick_time_raw\"]");
  }

  private void positionRetryContinuous() throws Exception {
    FakeMemory memory = positionFixture();
    memory.mutation = (fake, address, size) -> {
      if (address == HEAP + 0x61b0) fake.u64(address, 1000000 + 1000 * fake.reads.get(address + ":" + size));
    };
    JSONObject result = positionProbe(memory);
    unavailable(result, "selection_changed_during_read");
    retryEvidence(result, false, "[\"tick_time_raw\"]");
    require(memory.reads.get((HEAP + 0x61b0) + ":16") == 3 && memory.calls == 66,
        "持续竞争未在第三次观察停止");
    require(result.getJSONObject("_selection_diagnosis").getJSONArray("position_changed_fields").toString()
        .equals("[\"tick_time_raw\"]"), "末次B/C差异诊断丢失");
  }

  private void positionRetryOtherFields() throws Exception {
    // input_tick_time_raw 属于已批准的第三种单字段重试，不属于本负向清单。
    for (long target : new long[] {0x6048, 0x6250, 0x62b8, 0x62bc, 0x62c0, 0x713c, 0xd014}) {
      FakeMemory memory = positionFixture();
      memory.mutation = (fake, address, size) -> {
        if (address == HEAP + 0x6b00 && fake.reads.get(address + ":" + size) == 2) {
          if (target == 0x6250) fake.f64(HEAP + target, 11.3);
          else fake.u32(HEAP + target, target == 0xd014 ? 7654321 : target == 0x713c ? 3 : 7);
          fake.u64(HEAP + 0x61b0, 1002000);
        }
      };
      JSONObject result = positionProbe(memory);
      unavailable(result, "selection_changed_during_read");
      require(!result.has("_snapshot_retry") && memory.reads.get((HEAP + 0x6b00) + ":36") == 2,
          "未经批准的字段竞争触发重读：" + target);
    }
  }

  private void positionRetryThirdChanges() throws Exception {
    for (long target : new long[] {0xd014, 0x713c, 0x6060, 0x61b8, 0x6250, 0x62b8}) {
      FakeMemory memory = positionFixture();
      memory.mutation = (fake, address, size) -> {
        if (address == HEAP + 0x61b0 && fake.reads.get(address + ":" + size) == 2) fake.u64(address, 1002000);
        if (address == HEAP + 0x6b00 && fake.reads.get(address + ":" + size) == 3) {
          if (target == 0x6060 || target == 0x6250) fake.f64(HEAP + target, 11.3);
          else if (target == 0x61b8) fake.u64(HEAP + target, 1005000);
          else fake.u32(HEAP + target, target == 0xd014 ? 7654321 : 3);
        }
      };
      JSONObject result = positionProbe(memory);
      unavailable(result, "selection_changed_during_read");
      retryEvidence(result, false, "[\"tick_time_raw\"]");
      require(memory.reads.get((HEAP + 0x6b00) + ":36") == 3, "第三次变化继续重试");
      JSONObject diagnosis = result.getJSONObject("_selection_diagnosis");
      String expectedPosition = target == 0xd014 ? "[\"matched_st_pos_deg\",\"match_index\"]"
          : target == 0x713c ? "[]" : target == 0x6060 ? "[\"native_st_pos_deg\"]"
          : target == 0x61b8 ? "[\"input_tick_time_raw\"]" : target == 0x6250 ? "[\"matched_st_pos_deg\"]"
          : "[\"segment_index\"]";
      String expectedFields = target == 0xd014 ? "[\"path_id\",\"position\"]"
          : target == 0x713c ? "[\"nav_state\"]" : "[\"position\"]";
      require(diagnosis.getJSONArray("position_changed_fields").toString().equals(expectedPosition)
          && diagnosis.getJSONArray("changed_fields").toString().equals(expectedFields)
          && diagnosis.getBoolean("position_serialization_changed") == (target != 0x713c),
          "B/C精确差异不符，合成地址=" + target + "；诊断=" + diagnosis);
    }
  }

  private void positionRetryEpoch() throws Exception {
    FakeMemory memory = positionFixture();
    changeTickOnSecond(memory);
    final int[] epochs = {0};
    JSONObject result = NativeRouteProbe.probeSnapshot(maps(), memory, hashes(),
        () -> ++epochs[0] == 3 ? "restarted" : EPOCH, NativeRouteProbe.PROFILE, true);
    unavailable(result, "process_epoch_changed");
    retryEvidence(result, false, "[\"tick_time_raw\"]");
    require(epochs[0] == 3 && !result.has("_selection_diagnosis"), "epoch失败携带错误拒绝诊断或漏复核");
  }

  private void positionRetryReadFailure() throws Exception {
    FakeMemory memory = positionFixture();
    changeTickOnSecond(memory);
    JSONObject result = NativeRouteProbe.probeSnapshot(maps(), (address, size) -> {
      if (memory.calls == 45) throw new IllegalStateException("test_failure");
      return memory.read(address, size);
    }, hashes(), () -> EPOCH, NativeRouteProbe.PROFILE, true);
    unavailable(result, "read_failed");
    retryEvidence(result, false, "[\"tick_time_raw\"]");
    require(!result.has("_selection_diagnosis") && result.getInt("read_calls") == 46,
        "额外读取失败丢失累计预算或误报选择变化");
  }

  private void positionRetryDeadlineBefore() throws Exception {
    FakeMemory memory = positionFixture();
    changeTickOnSecond(memory);
    JSONObject result = NativeRouteProbe.probeSnapshot(maps(), (address, size) -> {
      if (memory.calls == 0) android.os.SystemClock.sleep(65);
      return memory.read(address, size);
    }, hashes(), () -> EPOCH, NativeRouteProbe.PROFILE, true);
    unavailable(result, "snapshot_retry_deadline_exceeded");
    retryEvidence(result, false, "[\"tick_time_raw\"]");
    require(result.getInt("read_calls") == 45 && !result.has("_selection_diagnosis"), "过期后仍启动第三次读取");
  }

  private void positionRetryDeadlineAfter() throws Exception {
    FakeMemory memory = positionFixture();
    changeTickOnSecond(memory);
    JSONObject result = NativeRouteProbe.probeSnapshot(maps(), (address, size) -> {
      if (memory.calls == 45) android.os.SystemClock.sleep(65);
      return memory.read(address, size);
    }, hashes(), () -> EPOCH, NativeRouteProbe.PROFILE, true);
    unavailable(result, "snapshot_retry_deadline_exceeded");
    retryEvidence(result, false, "[\"tick_time_raw\"]");
    require(result.getInt("read_calls") == 46 && !result.has("_selection_diagnosis"), "阻塞读取迟到仍被接受或继续读");
  }

  private void positionRetryDeadlineEpoch() throws Exception {
    FakeMemory memory = positionFixture();
    changeTickOnSecond(memory);
    final int[] epochs = {0};
    JSONObject result = NativeRouteProbe.probeSnapshot(maps(), memory, hashes(), () -> {
      if (++epochs[0] == 3) android.os.SystemClock.sleep(65);
      return EPOCH;
    }, NativeRouteProbe.PROFILE, true);
    unavailable(result, "snapshot_retry_deadline_exceeded");
    retryEvidence(result, false, "[\"tick_time_raw\"]");
    require(result.getInt("read_calls") == 66 && epochs[0] == 3 && !result.has("_selection_diagnosis"),
        "最终epoch读取超时没有拒绝");
  }

  private void positionNoRetrySlow() throws Exception {
    FakeMemory memory = positionFixture();
    JSONObject result = NativeRouteProbe.probeSnapshot(maps(), (address, size) -> {
      if (memory.calls == 0) android.os.SystemClock.sleep(65);
      return memory.read(address, size);
    }, hashes(), () -> EPOCH, NativeRouteProbe.PROFILE, true);
    require(result.getString("status").equals("observed") && !result.has("_snapshot_retry")
        && result.getInt("read_calls") == 45 && result.getDouble("observation_window_ms") >= 50,
        "正常无竞争慢路径被新截止规则改变");
  }

  private void positionInactive() throws Exception {
    for (int[] pair : new int[][] {{1, 1}, {0, 2}, {0, 4}, {0, 5}}) {
      FakeMemory memory = positionFixture();
      memory.u32(HEAP + 0x7138, pair[0]);
      memory.u32(HEAP + 0x713c, pair[1]);
      positionUnavailable(positionProbe(memory), "gps_navigation_not_observed");
      require(memory.calls == 29, "非GPS导航仍读取LocInfo");
    }
  }

  private void positionRepeated() throws Exception {
    JSONObject first = positionProbe(positionFixture()).getJSONObject("_position");
    JSONObject second = positionProbe(positionFixture()).getJSONObject("_position");
    require(first.toString().equals(second.toString()), "重复读取改变原生位置源token");
    untrustedPosition(second);
  }

  private void positionMatchOrder() throws Exception {
    FakeMemory memory = positionFixture();
    memory.u32(HEAP + 0x6240, 1234567);
    memory.u32(HEAP + 0x62c8, 7654321);
    JSONObject value = positionProbe(memory).getJSONObject("_position");
    require(value.getInt("match_index") == 0 && value.getJSONArray("matched_st_pos_deg").getDouble(0) == 11.2501,
        "错误假定主路永远是某一匹配项");
  }

  private void positionReadFailure() throws Exception {
    FakeMemory memory = positionFixture();
    JSONObject result = NativeRouteProbe.probeSnapshot(maps(), (address, size) -> {
      if (address == HEAP + 0x6250) return new byte[0];
      return memory.read(address, size);
    }, hashes(), () -> EPOCH, NativeRouteProbe.PROFILE, true);
    unavailable(result, "short_read");
    require(!result.getJSONObject("_position").has("native_st_pos_deg"), "部分读取失败仍输出位置");
  }

  private void naviInfoSelected() throws Exception {
    FakeMemory memory = naviInfoFixture();
    JSONObject result = naviInfoProbe(memory);
    JSONObject value = result.getJSONObject("_navi_info");
    require(result.getString("status").equals("observed") && value.getString("status").equals("observed"),
        "动作影子未被观测：" + result);
    require(value.getInt("item_index") == 1 && value.getInt("route_remain_time_raw") == 101
        && value.getInt("route_remain_distance_raw") == 1001 && value.getInt("branch_flag_raw") == 1,
        "没有按当前PathID唯一选择动作项");
    require(value.getJSONArray("branches").getJSONObject(0).getInt("maneuver_id_raw") == 15
        && value.getJSONArray("branches").getJSONObject(1).getInt("next_road_name_link_raw") == 6,
        "动作字段偏移或分支映射错误");
    require(result.getInt("read_calls") == 72 && result.getInt("read_bytes") == 1024,
        "动作影子读取预算不准确：" + result.getInt("read_calls") + "/" + result.getInt("read_bytes"));
    untrustedNaviInfo(result);
  }

  private void naviInfoCounts() throws Exception {
    long[][] cases = {{33, HEAP + 0x8000},
        {2, HEAP + 0x8000 + NativeRouteProbe.NAVI_INFO_ITEM_SIZE + 3}};
    String[] reasons = {"navi_info_count_invalid",
        "unaligned_pointer"};
    for (int index = 0; index < cases.length; index++) {
      FakeMemory memory = naviInfoFixture();
      memory.u32(HEAP + 0x6aac, cases[index][0]);
      memory.u64(HEAP + 0x6aa0, cases[index][1]);
      JSONObject result = naviInfoProbe(memory);
      unavailable(result, reasons[index]);
      require(result.getJSONObject("_navi_info").getString("status").equals("unavailable"),
          "畸形动作头仍发布部分字段：" + reasons[index]);
    }
    FakeMemory duplicate = naviInfoFixture();
    duplicate.u32(HEAP + 0x8000, 1234567L);
    unavailable(naviInfoProbe(duplicate), "navi_info_path_match_ambiguous");
    FakeMemory shortVector = naviInfoFixture();
    // items 向量指向 HEAP 映射末尾之前，3 项 stride 越过映射尾，应整份拒绝为 unmapped_read。
    shortVector.u64(HEAP + 0x6aa0, HEAP + 0xff900);
    unavailable(naviInfoProbe(shortVector), "unmapped_read");
  }

  private void naviInfoPointers() throws Exception {
    // items 向量指针（manager+0xaa0）仍是指针：校验 null/非对齐/未映射。
    long[][] cases = {{0xaa0, 0}, {0xaa0, HEAP + 0x7004}, {0xaa0, HEAP + 0x200000}};
    String[] reasons = {"null_pointer", "unaligned_pointer", "unmapped_read"};
    for (int index = 0; index < cases.length; index++) {
      FakeMemory memory = naviInfoFixture();
      memory.u64(HEAP + 0x6000 + cases[index][0], cases[index][1]);
      unavailable(naviInfoProbe(memory), reasons[index]);
    }
  }

  private static FakeMemory missingNaviInfoFixture() {
    FakeMemory memory = naviInfoFixture();
    memory.u32(HEAP + 0x8000 + NativeRouteProbe.NAVI_INFO_ITEM_SIZE, 7654321L);
    return memory;
  }

  private static FakeMemory emptyNaviInfoFixture(boolean nullVector) {
    FakeMemory memory = naviInfoFixture();
    memory.u32(HEAP + 0x6aac, 0);
    if (nullVector) memory.u64(HEAP + 0x6aa0, 0);
    return memory;
  }

  private void naviInfoEmptyIsolated() throws Exception {
    for (boolean nullVector : new boolean[] {true, false}) {
      JSONObject result = positionNaviInfoProbe(emptyNaviInfoFixture(nullVector));
      JSONObject action = result.getJSONObject("_navi_info");
      require(result.getString("status").equals("observed") && result.getBoolean("sample_consistent")
          && result.getBoolean("gps_navigation_observed")
          && result.getJSONObject("_position").getString("status").equals("observed")
          && result.getString("path_identity_hash").equals(
              result.getJSONObject("_position").getString("path_identity_hash")),
          "空动作列表不应撤销独立核验的路线和位置：" + result);
      require(action.length() == 6 && action.getInt("schema_version") == 2
          && action.getString("status").equals("unavailable")
          && action.getString("reason").equals("navi_info_count_zero")
          && action.getBoolean("diagnostic_only") && !action.getBoolean("control_input_allowed")
          && !action.getBoolean("route_render_input_allowed") && !result.has("_snapshot_retry"),
          "空动作列表不得发布部分动作、绑定或额外重读：" + action);
    }
  }

  private void naviInfoEmptyRace() throws Exception {
    for (boolean startsEmpty : new boolean[] {true, false}) {
      FakeMemory memory = startsEmpty ? emptyNaviInfoFixture(false) : naviInfoFixture();
      memory.mutation = (fake, address, size) -> {
        if (address == HEAP + 0x6b00 && fake.reads.get(address + ":" + size) == 2) {
          fake.u32(HEAP + 0x6aac, startsEmpty ? 3 : 0);
        }
      };
      JSONObject result = positionNaviInfoProbe(memory);
      unavailable(result, "selection_changed_during_read");
      require(result.getJSONObject("_selection_diagnosis").getJSONArray("changed_fields")
              .toString().equals("[\"navi_info\"]") && !result.has("_snapshot_retry")
          && result.getJSONObject("_position").getString("status").equals("unavailable"),
          "空列表与有值的跨读变化必须整份拒绝：" + result);
    }
  }

  private void naviInfoEmptyParentFailure() throws Exception {
    for (boolean changesPath : new boolean[] {true, false}) {
      FakeMemory memory = emptyNaviInfoFixture(false);
      memory.mutation = (fake, address, size) -> {
        if (address == HEAP + 0x6b00 && fake.reads.get(address + ":" + size) == 2) {
          if (changesPath) fake.u32(HEAP + 0xd014, 7654321L);
          else fake.f64(HEAP + 0x6060, 11.3);
        }
      };
      unavailable(positionNaviInfoProbe(memory), "selection_changed_during_read");
    }
    int[] epochs = {0};
    unavailable(NativeRouteProbe.probeSnapshot(maps(), emptyNaviInfoFixture(false), hashes(),
        () -> ++epochs[0] == 1 ? EPOCH : "changed", NativeRouteProbe.PROFILE, true, true),
        "process_epoch_changed");
    for (boolean unaligned : new boolean[] {true, false}) {
      FakeMemory unreadable = emptyNaviInfoFixture(false);
      unreadable.u64(HEAP + 0x6aa0, HEAP + (unaligned ? 0x8004 : 0x200000));
      unavailable(positionNaviInfoProbe(unreadable), unaligned ? "unaligned_pointer" : "unmapped_read");
    }
  }

  private void naviInfoMissingIsolated() throws Exception {
    JSONObject result = positionNaviInfoProbe(missingNaviInfoFixture());
    JSONObject action = result.getJSONObject("_navi_info");
    require(result.getString("status").equals("observed") && result.getBoolean("sample_consistent")
        && result.getBoolean("gps_navigation_observed")
        && result.getJSONObject("_position").getString("status").equals("observed")
        && result.getString("path_identity_hash").equals(
            result.getJSONObject("_position").getString("path_identity_hash")),
        "动作缺匹配不应撤销已独立双读核验的路线和位置：" + result);
    require(action.length() == 6 && action.getInt("schema_version") == 2
        && action.getString("status").equals("unavailable")
        && action.getString("reason").equals("navi_info_path_match_missing")
        && action.getBoolean("diagnostic_only") && !action.getBoolean("control_input_allowed")
        && !action.getBoolean("route_render_input_allowed") && !result.has("_snapshot_retry"),
        "缺匹配动作不得携带部分字段、绑定或触发额外重读：" + action);
  }

  private void naviInfoMissingRace() throws Exception {
    for (boolean startsMissing : new boolean[] {true, false}) {
      FakeMemory memory = startsMissing ? missingNaviInfoFixture() : naviInfoFixture();
      memory.mutation = (fake, address, size) -> {
        if (address == HEAP + 0x6b00 && fake.reads.get(address + ":" + size) == 2) {
          fake.u32(HEAP + 0x8000 + NativeRouteProbe.NAVI_INFO_ITEM_SIZE,
              startsMissing ? 1234567L : 7654321L);
        }
      };
      JSONObject result = positionNaviInfoProbe(memory);
      unavailable(result, "selection_changed_during_read");
      require(result.getJSONObject("_selection_diagnosis").getJSONArray("changed_fields")
              .toString().equals("[\"navi_info\"]") && !result.has("_snapshot_retry")
          && result.getJSONObject("_position").getString("status").equals("unavailable"),
          "动作缺失与可用的跨读变化必须整份拒绝：" + result);
    }
  }

  private void naviInfoMissingParentFailure() throws Exception {
    for (boolean changesPath : new boolean[] {true, false}) {
      FakeMemory memory = missingNaviInfoFixture();
      memory.mutation = (fake, address, size) -> {
        if (address == HEAP + 0x6b00 && fake.reads.get(address + ":" + size) == 2) {
          if (changesPath) fake.u32(HEAP + 0xd014, 7654321L);
          else fake.f64(HEAP + 0x6060, 11.3);
        }
      };
      unavailable(positionNaviInfoProbe(memory), "selection_changed_during_read");
    }
    int[] epochs = {0};
    unavailable(NativeRouteProbe.probeSnapshot(maps(), missingNaviInfoFixture(), hashes(),
        () -> ++epochs[0] == 1 ? EPOCH : "changed", NativeRouteProbe.PROFILE, true, true),
        "process_epoch_changed");
    FakeMemory unreadable = missingNaviInfoFixture();
    unreadable.u64(HEAP + 0x6aa0, HEAP + 0x200000);
    unavailable(positionNaviInfoProbe(unreadable), "unmapped_read");
  }

  private void naviInfoFields() throws Exception {
    long[][] cases = {{0x60, 0xffffffffL}, {0x64, 0xffffffffL}, {0x68, 0xffffffffL},
        {0xd0, 2}, {0xd0, 0xffffffffL}};
    String[] reasons = {"navi_info_current_segment_invalid", "navi_info_current_link_invalid",
        "navi_info_current_point_invalid", "navi_info_branch_flag_invalid",
        "navi_info_branch_flag_invalid"};
    for (int index = 0; index < cases.length; index++) {
      FakeMemory memory = naviInfoFixture();
      memory.u32(HEAP + 0x8000 + NativeRouteProbe.NAVI_INFO_ITEM_SIZE + cases[index][0], cases[index][1]);
      unavailable(naviInfoProbe(memory), reasons[index]);
    }
    FakeMemory branchFlag = naviInfoFixture();
    long branch = HEAP + 0x8000 + NativeRouteProbe.NAVI_INFO_ITEM_SIZE + 0xd8;
    branchFlag.u32(branch + 4, 0x80000000L);
    unavailable(naviInfoProbe(branchFlag), "navi_info_maneuver_segment_invalid");
  }

  private static long naviBranch(int index) {
    return HEAP + 0x8000 + NativeRouteProbe.NAVI_INFO_ITEM_SIZE + 0xd8 + index * 0x40;
  }

  private void naviInfoNextRoadIsolated() throws Exception {
    JSONObject baseline = positionNaviInfoProbe(naviInfoFixture());
    for (int branchIndex : new int[] {0, 1}) {
      for (int offset : new int[] {48, 52}) {
        for (long raw : new long[] {0x80000000L, 0x80000001L, 0xfffffffeL}) {
          FakeMemory memory = naviInfoFixture();
          memory.u32(naviBranch(branchIndex) + offset, raw);
          JSONObject result = positionNaviInfoProbe(memory);
          require(result.getString("status").equals("observed"), "道路名索引连带撤销路线");
          require(result.getJSONObject("_position").toString().equals(baseline.getJSONObject("_position").toString()),
              "独立位置发生变化");
          JSONObject navi = result.getJSONObject("_navi_info");
          String field = offset == 48 ? "next_road_name_segment_raw" : "next_road_name_link_raw";
          String reason = offset == 48 ? "navi_info_next_segment_invalid" : "navi_info_next_link_invalid";
          require(navi.getJSONArray("branches").getJSONObject(branchIndex).getLong(field) == raw,
              "失败原值没有保留");
          require(navi.getString("reason").equals(reason), "动作不可用原因丢失");
          for (int index = 0; index < 2; index++) {
            JSONObject binding = navi.getJSONArray("branches").getJSONObject(index).getJSONObject("segment_binding");
            require(binding.toString().equals(baseline.getJSONObject("_navi_info").getJSONArray("branches")
                .getJSONObject(index).getJSONObject("segment_binding").toString()), "原始绑定观测被覆盖");
          }
          require(result.getInt("read_calls") == baseline.getInt("read_calls")
              && result.getInt("read_bytes") == baseline.getInt("read_bytes"), "动作隔离改变读取预算");
          require(!result.has("_snapshot_retry"), "稳定异常触发了重试");
          untrustedNaviInfo(new JSONObject().put("_navi_info", navi));
        }
      }
    }
  }

  private void naviInfoNextRoadRace() throws Exception {
    for (int branchIndex : new int[] {0, 1}) {
      for (long next : new long[] {0x80000001L, 5L}) {
        FakeMemory memory = naviInfoFixture();
        long branch = naviBranch(branchIndex);
        memory.u32(branch + 48, 0x80000000L);
        memory.mutation = (fake, address, size) -> {
          if (address == branch && size == 60 && fake.reads.get(address + ":" + size) == 2) {
            fake.u32(branch + 48, next);
          }
        };
        JSONObject result = positionNaviInfoProbe(memory);
        unavailable(result, "selection_changed_during_read");
        require(!result.has("_snapshot_retry"), "动作原值变化错误重试");
      }
    }
  }

  private void naviInfoNextRoadFailures() throws Exception {
    FakeMemory memory = naviInfoFixture();
    memory.u32(naviBranch(0) + 48, 0x80000000L);
    unavailable(naviInfoProbe((address, size) -> {
      if (address == HEAP + 0xd000 && size == 8) return new byte[0];
      return memory.read(address, size);
    }), "short_read");
    int[] epochs = {0};
    unavailable(NativeRouteProbe.probeSnapshot(maps(), memory, hashes(),
        () -> ++epochs[0] == 1 ? EPOCH : "changed", NativeRouteProbe.PROFILE, true, true),
        "process_epoch_changed");
    memory.u32(naviBranch(1) + 4, 0x80000000L);
    unavailable(positionNaviInfoProbe(memory), "navi_info_maneuver_segment_invalid");
  }

  private void naviInfoNextRoadRecovery() throws Exception {
    FakeMemory memory = naviInfoFixture();
    memory.u32(naviBranch(0) + 48, 0xfffffffeL);
    require(positionNaviInfoProbe(memory).getString("status").equals("observed"), "稳定异常丢路线");
    memory.u32(naviBranch(0) + 48, 5);
    JSONObject navi = positionNaviInfoProbe(memory).getJSONObject("_navi_info");
    require(navi.getString("reason").equals("unverified_navi_info_semantics"), "动作异常原因未恢复");
    for (int index = 0; index < 2; index++) {
      require(navi.getJSONArray("branches").getJSONObject(index).getJSONObject("segment_binding")
          .getString("status").equals("verified"), "异常状态污染后续快照");
    }
  }

  private void naviInfoNextRoadBindingRace() throws Exception {
    FakeMemory memory = naviInfoFixture();
    memory.u32(naviBranch(0) + 48, 0xfffffffeL);
    memory.mutation = (fake, address, size) -> {
      if (address == naviBranch(0) && size == 60 && fake.reads.get(address + ":" + size) == 2) {
        fake.u64(HEAP + 0x90000 + 16, 0);
      }
    };
    unavailable(positionNaviInfoProbe(memory), "selection_changed_during_read");
  }

  private void naviInfoNextRoadBoundaries() throws Exception {
    for (long raw : new long[] {0L, 0x7fffffffL, 0xffffffffL}) {
      FakeMemory memory = naviInfoFixture();
      memory.u32(naviBranch(0) + 48, raw);
      memory.u32(naviBranch(1) + 52, raw);
      JSONObject navi = positionNaviInfoProbe(memory).getJSONObject("_navi_info");
      for (int index = 0; index < 2; index++) {
        require(navi.getJSONArray("branches").getJSONObject(index).getJSONObject("segment_binding")
            .getString("status").equals("verified"), "既有合法边界被改变");
      }
    }
  }

  private void naviInfoSegmentBinding() throws Exception {
    // fixture 的 branch_flag=1；动作门实际读取 branch1，故障必须注入同一分支。
    long branch = HEAP + 0x8000 + NativeRouteProbe.NAVI_INFO_ITEM_SIZE + 0x118;

    FakeMemory sentinel = naviInfoFixture();
    sentinel.u32(branch + 4, 0xffffffffL);
    naviBindingUnavailable(naviInfoProbe(sentinel), "segment_index_unavailable");

    FakeMemory segments = naviInfoFixture();
    unavailable(naviInfoProbe((address, size) -> {
      if (address == HEAP + 0xd000 && size == 8) return new byte[0];
      return segments.read(address, size);
    }), "short_read");

    long[][] vectorCases = {
        {HEAP + 0xd060 + 8, HEAP + 0xd060 + 8},
        {HEAP + 0xd060 + 8, HEAP + 0xd060},
        {HEAP + 0xd040, HEAP + 0xd040 + 8 * (NativeRouteProbe.MAX_SEGMENTS + 1L)},
    };
    String[] reasons = {"segment_index_out_of_bounds", "segment_vector_invalid", "segment_index_out_of_bounds"};
    for (int index = 0; index < vectorCases.length; index++) {
      FakeMemory memory = naviInfoFixture();
      memory.u64(HEAP + 0xd060, vectorCases[index][0]);
      memory.u64(HEAP + 0xd068, vectorCases[index][1]);
      naviBindingUnavailable(naviInfoProbe(memory), reasons[index]);
    }

    long segmentPointer = HEAP + 0x90000;
    long[] objectValues = {0, HEAP + 0x92004, HEAP + 0x200000};
    String[] objectReasons = {
        "segment_object_invalid", "segment_object_invalid", "segment_object_mapping_invalid"};
    for (int index = 0; index < objectValues.length; index++) {
      FakeMemory memory = naviInfoFixture();
      memory.u64(segmentPointer + 16, objectValues[index]);
      naviBindingUnavailable(naviInfoProbe(memory), objectReasons[index]);
    }

    long segment = HEAP + 0x92000;
    FakeMemory empty = naviInfoFixture();
    empty.u64(segment, 0);
    naviBindingUnavailable(naviInfoProbe(empty), "segment_vtable_invalid");

    FakeMemory writable = naviInfoFixture();
    writable.u64(segment, HEAP + 0x93000);
    writable.u64(HEAP + 0x93000 + 0xb8, DICE + 0x3a7a0b8L);
    writable.u64(HEAP + 0x93000 + 0x128, DICE + 0x3a86180L);
    naviBindingUnavailable(naviInfoProbe(writable), "segment_vtable_mapping_invalid");

    FakeMemory wrapper = naviInfoFixture();
    wrapper.u64(segment, DICE + NativeRouteProbe.DRIVE_SEGMENT_ACCESSOR_VTABLE_RVA);
    naviBindingUnavailable(naviInfoProbe(wrapper), "segment_wrapper_vtable_rejected");
    FakeMemory baseWrapper = naviInfoFixture();
    baseWrapper.u64(segment, DICE + NativeRouteProbe.BASE_SEGMENT_ACCESSOR_VTABLE_RVA);
    naviBindingUnavailable(naviInfoProbe(baseWrapper), "segment_wrapper_vtable_rejected");

    long segmentVtable = DICE + 0x3d50000;
    FakeMemory misalignedVtable = naviInfoFixture();
    misalignedVtable.u64(segment, segmentVtable + (DICE - HEAP));
    naviBindingUnavailable(naviInfoProbe(misalignedVtable), "segment_vtable_invalid");

    FakeMemory badIconGetter = naviInfoFixture();
    badIconGetter.u64(segmentVtable + 0x128, segmentVtable);
    naviBindingUnavailable(naviInfoProbe(badIconGetter), "segment_getter_mapping_invalid");

    FakeMemory countGetterAsIcon = naviInfoFixture();
    countGetterAsIcon.u64(segmentVtable + 0xb8,
        DICE + NativeRouteProbe.DRIVE_SEGMENT_COUNT_GETTER_RVA);
    naviBindingUnavailable(naviInfoProbe(countGetterAsIcon), "segment_getter_semantics_mismatch");
  }

  private void naviInfoRace() throws Exception {
    // 两个 A/B 变化源：current_segment（单独 u32 读）与 branch0 的 segment_remain_time
    // （60 字节整块读内的偏移 28）。触发地址必须是 read 的起始地址。
    long[][] cases = {{HEAP + 0x8000 + NativeRouteProbe.NAVI_INFO_ITEM_SIZE + 0x60, 5, 0},
        {HEAP + 0x8000 + NativeRouteProbe.NAVI_INFO_ITEM_SIZE + 0xd8, 41, 28}};
    for (long[] pair : cases) {
      FakeMemory memory = naviInfoFixture();
      long trigger = pair[0], value = pair[1], offset = pair[2];
      memory.mutation = (fake, address, size) -> {
        if (address == trigger && fake.reads.get(address + ":" + size) == 2) fake.u32(trigger + offset, value);
      };
      JSONObject result = naviInfoProbe(memory);
      unavailable(result, "selection_changed_during_read");
      JSONObject diagnosis = result.getJSONObject("_selection_diagnosis");
      require(diagnosis.getInt("schema_version") == 2
          && diagnosis.getJSONArray("changed_fields").toString().equals("[\"navi_info\"]")
          && diagnosis.getBoolean("navi_info_serialization_changed")
          && diagnosis.getJSONArray("navi_info_changed_fields").toString()
              .equals(offset == 0 ? "[\"current_segment\"]" : "[\"branches\"]")
          && result.getJSONObject("_navi_info").getString("status").equals("unavailable"),
          "动作影子A/B差异没有整份拒绝或诊断不准确");
    require(!result.has("_snapshot_retry") && result.getInt("read_calls") == 72,
          "动作影子变化错误启用了位置重读");
    }
  }

  private static JSONObject positionNaviInfoProbe(FakeMemory memory) {
    return NativeRouteProbe.probeSnapshot(maps(), memory, hashes(), () -> EPOCH, NativeRouteProbe.PROFILE,
        true, true);
  }

  private void naviInfoPositionRetryTick() throws Exception {
    for (boolean bothTicks : new boolean[] {false, true}) {
      FakeMemory memory = naviInfoFixture();
      memory.mutation = (fake, address, size) -> {
        if (address == HEAP + 0x61b0 && fake.reads.get(address + ":" + size) == 2) {
          fake.u64(address, 1002000);
          if (bothTicks) fake.u64(address + 8, 1001500);
        }
      };
      JSONObject result = positionNaviInfoProbe(memory);
      require(result.getString("status").equals("observed"), "navi_info_tick_retry_not_recovered");
      retryEvidence(result, true, bothTicks ? "[\"tick_time_raw\",\"input_tick_time_raw\"]" : "[\"tick_time_raw\"]");
      JSONObject initial = result.getJSONObject("_snapshot_retry").getJSONObject("initial_diagnosis");
      require(initial.getInt("schema_version") == 2
          && !initial.getBoolean("navi_info_serialization_changed")
          && initial.getJSONArray("navi_info_changed_fields").length() == 0, "navi_info_retry_evidence_invalid");
      require(result.getJSONObject("_position").getLong("tick_time_raw") == 1002000
          && result.getJSONObject("_position").getLong("input_tick_time_raw") == (bothTicks ? 1001500 : 1000000)
          && memory.reads.get((HEAP + 0x61b0) + ":16") == 3, "navi_info_retry_not_exactly_one_observation");
      require(result.getInt("read_calls") == memory.calls && memory.calls <= NativeRouteProbe.MAX_READ_CALLS
          && result.getInt("read_bytes") <= NativeRouteProbe.MAX_READ_BYTES, "navi_info_retry_reset_budget");
      untrustedNaviInfo(new JSONObject().put("_navi_info", result.getJSONObject("_navi_info")));
    }
  }

  private static void changeNaviPositionOnSecond(FakeMemory memory, boolean bothTicks) {
    memory.mutation = (fake, address, size) -> {
      if (address == HEAP + 0x6b00 && fake.reads.get(address + ":" + size) == 2) {
        fake.f64(HEAP + 0x6060, 11.3);
        fake.f64(HEAP + 0x6068, 22.6);
        fake.u64(HEAP + 0x61b0, 1002000);
        if (bothTicks) fake.u64(HEAP + 0x61b8, 1001500);
      }
    };
  }

  private void naviInfoPositionRetryCoordinate() throws Exception {
    for (boolean bothTicks : new boolean[] {false, true}) {
      FakeMemory memory = naviInfoFixture();
      changeNaviPositionOnSecond(memory, bothTicks);
      JSONObject result = positionNaviInfoProbe(memory);
      require(result.getString("status").equals("observed"), "navi_info_coordinate_retry_not_recovered");
      retryEvidence(result, true, bothTicks
          ? "[\"native_st_pos_deg\",\"tick_time_raw\",\"input_tick_time_raw\"]"
          : "[\"native_st_pos_deg\",\"tick_time_raw\"]");
      JSONObject initial = result.getJSONObject("_snapshot_retry").getJSONObject("initial_diagnosis");
      JSONObject position = result.getJSONObject("_position");
      require(initial.getInt("schema_version") == 2
          && !initial.getBoolean("navi_info_serialization_changed")
          && initial.getJSONArray("navi_info_changed_fields").length() == 0, "navi_info_retry_action_evidence_invalid");
      require(position.getJSONArray("native_st_pos_deg").toString().equals("[11.3,22.6]")
          && position.getLong("tick_time_raw") == 1002000
          && position.getLong("input_tick_time_raw") == (bothTicks ? 1001500 : 1000000)
          && memory.reads.get((HEAP + 0x6b00) + ":36") == 3, "navi_info_retry_changed_values_or_repeated");
      require(result.getInt("read_calls") == memory.calls && memory.calls <= NativeRouteProbe.MAX_READ_CALLS
          && result.getInt("read_bytes") <= NativeRouteProbe.MAX_READ_BYTES
          && result.getLong("observation_finished_monotonic_ns") - result.getLong("observation_started_monotonic_ns")
              < 50000000L, "navi_info_retry_reset_time_or_read_budget");
      untrustedNaviInfo(new JSONObject().put("_navi_info", result.getJSONObject("_navi_info")));
    }
  }

  private void naviInfoPositionRetryLimits() throws Exception {
    for (String mode : new String[] {"before", "after", "final_epoch", "epoch_changed", "epoch_failed", "read_failed"}) {
      FakeMemory memory = naviInfoFixture();
      changeNaviPositionOnSecond(memory, true);
      final int[] epochs = {0};
      JSONObject result = NativeRouteProbe.probeSnapshot(maps(), (address, size) -> {
        boolean thirdHeader = address == HEAP + 0x6b00
            && Integer.valueOf(2).equals(memory.reads.get(address + ":" + size));
        if ((mode.equals("before") && memory.calls == 0) || (mode.equals("after") && thirdHeader)) {
          android.os.SystemClock.sleep(65);
        }
        if (mode.equals("read_failed") && thirdHeader) throw new IllegalStateException("test_failure");
        return memory.read(address, size);
      }, hashes(), () -> {
        if (++epochs[0] == 3) {
          if (mode.equals("final_epoch")) android.os.SystemClock.sleep(65);
          if (mode.equals("epoch_changed")) return "restarted";
          if (mode.equals("epoch_failed")) throw new IllegalStateException("test_failure");
        }
        return EPOCH;
      }, NativeRouteProbe.PROFILE, true, true);
      String reason = mode.equals("epoch_changed") ? "process_epoch_changed"
          : mode.equals("epoch_failed") ? "process_epoch_failed"
          : mode.equals("read_failed") ? "read_failed" : "snapshot_retry_deadline_exceeded";
      unavailable(result, reason);
      retryEvidence(result, false, "[\"native_st_pos_deg\",\"tick_time_raw\",\"input_tick_time_raw\"]");
      require(!result.has("_selection_diagnosis") && !result.getJSONObject("_navi_info").has("branches")
          && memory.reads.get((HEAP + 0x6b00) + ":36") <= 3
          && result.getInt("read_calls") >= memory.calls
          && result.getInt("read_calls") <= NativeRouteProbe.MAX_READ_CALLS
          && result.getInt("read_bytes") <= NativeRouteProbe.MAX_READ_BYTES, "navi_info_retry_limit_leaked_or_reset");
      if (mode.equals("before")) require(memory.reads.get((HEAP + 0x6b00) + ":36") == 2,
          "navi_info_retry_read_after_deadline");
    }
  }

  private void naviInfoPositionRetryOtherFields() throws Exception {
    for (long target : new long[] {0x6060, 0x61b8, 0xd014, 0x713c, 0x6250, 0x62b8}) {
      FakeMemory memory = naviInfoFixture();
      memory.mutation = (fake, address, size) -> {
        if (address == HEAP + 0x6b00 && fake.reads.get(address + ":" + size) == 2) {
          if (target == 0x6060 || target == 0x6250) fake.f64(HEAP + target, 11.3);
          else if (target == 0x61b8) fake.u64(HEAP + target, 1001500);
          else fake.u32(HEAP + target, target == 0xd014 ? 7654321 : 3);
          if (target == 0xd014) fake.u32(HEAP + 0x8000 + NativeRouteProbe.NAVI_INFO_ITEM_SIZE, 7654321);
          if (target != 0x6060 && target != 0x61b8) fake.u64(HEAP + 0x61b0, 1002000);
        }
      };
      JSONObject result = positionNaviInfoProbe(memory);
      unavailable(result, "selection_changed_during_read");
      require(!result.has("_snapshot_retry") && memory.reads.get((HEAP + 0x6b00) + ":36") == 2,
          "navi_info_retry_accepted_non_clock_change");
    }
    FakeMemory actions = naviInfoFixture();
    actions.mutation = (fake, address, size) -> {
      if (address == HEAP + 0x61b0 && fake.reads.get(address + ":" + size) == 2) {
        fake.u64(address, 1002000);
        fake.u32(HEAP + 0x8000 + NativeRouteProbe.NAVI_INFO_ITEM_SIZE + 0x60, 5);
      }
    };
    JSONObject result = positionNaviInfoProbe(actions);
    unavailable(result, "selection_changed_during_read");
    require(!result.has("_snapshot_retry"), "navi_info_retry_accepted_action_change");
  }

  private void naviInfoPositionRetryThirdChanges() throws Exception {
    for (long target : new long[] {0xd014, 0x713c, 0x6060, 0x61b8, 0x8000 + NativeRouteProbe.NAVI_INFO_ITEM_SIZE + 0x60}) {
      FakeMemory memory = naviInfoFixture();
      memory.mutation = (fake, address, size) -> {
        if (address == HEAP + 0x6b00 && fake.reads.get(address + ":" + size) == 2) {
          fake.f64(HEAP + 0x6060, 11.3);
          fake.u64(HEAP + 0x61b0, 1002000);
        }
        if (address == HEAP + 0x6b00 && fake.reads.get(address + ":" + size) == 3) {
          if (target == 0x6060) fake.f64(HEAP + target, 11.4);
          else if (target == 0x61b8) fake.u64(HEAP + target, 1005000);
          else fake.u32(HEAP + target, target == 0xd014 ? 7654321 : 3);
          if (target == 0xd014) fake.u32(HEAP + 0x8000 + NativeRouteProbe.NAVI_INFO_ITEM_SIZE, 7654321);
        }
      };
      JSONObject result = positionNaviInfoProbe(memory);
      unavailable(result, "selection_changed_during_read");
      retryEvidence(result, false, "[\"native_st_pos_deg\",\"tick_time_raw\"]");
      require(result.getJSONObject("_navi_info").getString("status").equals("unavailable")
          && memory.reads.get((HEAP + 0x6b00) + ":36") == 3, "navi_info_retry_leaked_partial_or_retried_again");
    }
  }

  private void naviInfoOptIn() throws Exception {
    FakeMemory memory = naviInfoFixture();
    JSONObject result = probe(memory);
    require(!result.has("_navi_info") && memory.calls == 29, "默认命令读取或输出动作影子");
    for (String key : memory.reads.keySet()) {
      long address = Long.parseLong(key.split(":")[0]);
      require(address < HEAP + 0x6aa0 || address >= HEAP + 0x6ab0, "默认命令读取NaviInfo头");
    }
  }

  private void naviInfoInactive() throws Exception {
    for (int[] pair : new int[][] {{1, 1}, {1, 2}, {0, 4}}) {
      FakeMemory memory = naviInfoFixture();
      memory.u32(HEAP + 0x7138, pair[0]);
      memory.u32(HEAP + 0x713c, pair[1]);
      JSONObject value = naviInfoProbe(memory).getJSONObject("_navi_info");
      require(value.getString("status").equals("unavailable")
          && value.getString("reason").equals("gps_navigation_not_observed")
          && !value.has("branches"), "非活动GPS导航仍发布动作");
    }
  }

  private static FakeMemory geometryFixture(boolean sharedEndpoint) {
    FakeMemory memory = fixture();
    long drivePath = HEAP + 0xd000;
    long segmentBegin = HEAP + 0xe000;
    long segment0 = HEAP + 0xe100;
    long segment1 = HEAP + 0xe200;
    long links0 = HEAP + 0xe300;
    long links1 = HEAP + 0xe400;
    long link0 = HEAP + 0xe500;
    long link1 = HEAP + 0xe600;
    long link2 = HEAP + 0xe700;
    long backing0 = HEAP + 0xe800;
    long backing1 = HEAP + 0xe900;
    long backing2 = HEAP + 0xea00;
    long points0 = HEAP + 0xec00;
    long points1 = HEAP + 0xed00;
    long points2 = HEAP + 0xee00;
    // 后一个 link 的二维点窗口从自己的 backing 起点开始；共享点是坐标相同，
    // 不是指针对象相同。让两个 array 落在重叠地址会把一方写成另一方的点。
    memory.u64(drivePath + 0x60, segmentBegin);
    memory.u64(drivePath + 0x68, segmentBegin + 16);
    memory.u64(segmentBegin, segment0);
    memory.u64(segmentBegin + 8, segment1);
    memory.u64(segment0 + 0x70, links0);
    memory.u64(segment0 + 0x78, links0 + 16);
    memory.u64(segment1 + 0x70, links1);
    memory.u64(segment1 + 0x78, links1 + 8);
    memory.u64(links0, link0);
    memory.u64(links0 + 8, link1);
    memory.u64(links1, link2);
    memory.u32(link0 + 0x24, 4);
    memory.u32(link0 + 0x28, 0);
    memory.u64(link0 + 0xa0, backing0);
    memory.u32(link1 + 0x24, 4);
    memory.u32(link1 + 0x28, 1);
    memory.u64(link1 + 0xa0, backing2);
    memory.u32(link2 + 0x24, 2);
    memory.u32(link2 + 0x28, 0);
    memory.u64(link2 + 0xa0, backing1);
    memory.u64(backing0 + 0x58, points0);
    memory.u64(backing1 + 0x58, points1);
    memory.u64(backing2 + 0x58, points2);
    memory.u32(backing0 + 0x50, 4);
    memory.u32(backing1 + 0x50, 2);
    memory.u32(backing2 + 0x50, 5);
    int baseX = 3_600_000, baseY = 7_200_000;
    int[] shared = {baseX, baseX + 100, baseX + 200, baseX + 300, baseX + 400};
    // link0 从自己的 backing 局部起点 0 读取 0..3。
    for (int index = 0; index < 4; index++) {
      writeGeometryPoint(memory, points0, index, shared[index], baseY);
    }
    // link1 从自己的 backing 局部起点 1 读取 1..4；前三项与 link0 逐点重叠。
    for (int index = 0; index < 5; index++) {
      writeGeometryPoint(memory, points2, index, shared[index], baseY);
    }
    // 下一段首点按是否共享端点验证段间连续性；link2 从自己的 backing 起点读取两点。
    writeGeometryPoint(memory, points1, 0, shared[4] + (sharedEndpoint ? 0 : 1), baseY);
    writeGeometryPoint(memory, points1, 1, shared[4] + 100, baseY);
    return memory;
  }

  private static void writeGeometryPoint(FakeMemory memory, long array, int index, int x, int y) {
    memory.u32(array + index * 8, x);
    memory.u32(array + index * 8 + 4, y);
  }

  private static NativeRouteProbe.GeometrySnapshot geometryProbe(FakeMemory memory) {
    return NativeRouteProbe.probeGeometry(maps(), memory, hashes(), () -> EPOCH, NativeRouteProbe.PROFILE);
  }

  private static void geometryObserved(NativeRouteProbe.GeometrySnapshot value) {
    require(value.status.equals("observed") && value.reason.equals("consistent_read_only_geometry"),
        "原生几何没有完整观测：" + value.reason);
    require(value.payload.length >= 112 && value.readCalls > 0 && value.readBytes > 0
        && value.finishedNs >= value.startedNs, "几何证据窗口或读取预算无效");
  }

  private static void geometryUnavailable(NativeRouteProbe.GeometrySnapshot value, String reason) {
    require(value.status.equals("unavailable") && value.reason.equals(reason) && value.payload.length == 0,
        "几何错误没有封闭返回：" + reason + " / " + value.reason);
  }

  private void geometryWindow() {
    NativeRouteProbe.GeometrySnapshot value = geometryProbe(geometryFixture(true));
    geometryObserved(value);
    ByteBuffer frame = ByteBuffer.wrap(value.payload).order(ByteOrder.LITTLE_ENDIAN);
    require(new String(value.payload, 0, 16, java.nio.charset.StandardCharsets.US_ASCII)
        .equals("AMAP_NAV_GEO_V2\0"), "几何magic错误");
    require(frame.getInt(16) == 1 && frame.getInt(20) == 0 && frame.getInt(24) == 1
        && frame.getInt(28) == 3 && frame.getInt(32) == 1 && frame.getInt(36) == 2
        && frame.getInt(40) == 3 && frame.getInt(44) == 6, "几何固定头字段错误");
    require(value.payload.length == 112 + 3 * 12 + 6 * 8, "几何payload长度错误");
  }

  private void geometryCompact() {
    NativeRouteProbe.GeometrySnapshot value = geometryProbe(geometryFixture(true));
    geometryObserved(value);
    ByteBuffer frame = ByteBuffer.wrap(value.payload).order(ByteOrder.LITTLE_ENDIAN);
    long[][] records = {{0, 0, 4}, {0, 1, 4}, {1, 4, 2}};
    for (int index = 0; index < records.length; index++) {
      int offset = 112 + index * 12;
      require(frame.getInt(offset) == records[index][0] && frame.getInt(offset + 4) == records[index][1]
          && frame.getInt(offset + 8) == records[index][2], "段间共享端点没有只删除一次");
    }
  }

  private static FakeMemory geometryDeepOverlapFixture() {
    FakeMemory memory = geometryFixture(true);
    // 只保留一个 segment，让 link1 与 link0 使用同一个 backing 的局部窗口。
    memory.u64(HEAP + 0xd060, HEAP + 0xe000);
    memory.u64(HEAP + 0xd068, HEAP + 0xe008);
    memory.u32(HEAP + 0xe500 + 0x24, 4);
    // 深层重叠仍是同一 backing 内的局部窗口：首 link 0..3，次 link 从 1 开始，
    // 因此存在 3 个已覆盖点且只追加 1 个新点。
    memory.u32(HEAP + 0xe500 + 0x28, 0);
    memory.u32(HEAP + 0xe600 + 0x24, 4);
    memory.u32(HEAP + 0xe600 + 0x28, 1);
    memory.u64(HEAP + 0xe600 + 0xa0, HEAP + 0xea00);
    int baseX = 3_600_000, baseY = 7_200_000;
    int[] xs = {baseX, baseX + 100, baseX + 200, baseX + 300, baseX + 400};
    for (int index = 0; index < xs.length; index++) {
      memory.u32(HEAP + 0xea00 + index * 8, xs[index]);
      memory.u32(HEAP + 0xea00 + index * 8 + 4, baseY);
    }
    return memory;
  }

  private void geometryDeepOverlap() {
    NativeRouteProbe.GeometrySnapshot value = geometryProbe(geometryDeepOverlapFixture());
    geometryObserved(value);
    ByteBuffer frame = ByteBuffer.wrap(value.payload).order(ByteOrder.LITTLE_ENDIAN);
    require(frame.getInt(28) == 3 && frame.getInt(36) == 1 && frame.getInt(40) == 2
        && frame.getInt(44) == 5, "多共享点压缩的固定头错误: segments=" + frame.getInt(36)
        + " links=" + frame.getInt(40) + " points=" + frame.getInt(44));
    long[][] records = {{0, 0, 4}, {0, 1, 4}};
    for (int index = 0; index < records.length; index++) {
      int offset = 112 + index * 12;
      require(frame.getInt(offset) == records[index][0] && frame.getInt(offset + 4) == records[index][1]
          && frame.getInt(offset + 8) == records[index][2], "多共享点没有逐点校验并只追加新点");
    }
  }

  private void geometryOverlapMismatch() {
    FakeMemory memory = geometryDeepOverlapFixture();
    // 篡改 link1 自己的重叠窗口；link0 的 backing 保持原值。
    memory.u32(HEAP + 0xee00 + 2 * 8, 3_600_201);
    NativeRouteProbe.GeometrySnapshot value = geometryProbe(memory);
    if (value.status.equals("observed")) {
      ByteBuffer frame = ByteBuffer.wrap(value.payload).order(ByteOrder.LITTLE_ENDIAN);
      require(false, "重叠点篡改没有被拒绝: segments=" + frame.getInt(36)
          + " links=" + frame.getInt(40) + " points=" + frame.getInt(44));
    }
    geometryUnavailable(value, "geometry_link_overlap_mismatch");
  }

  private void geometryEmptyLink() {
    FakeMemory memory = geometryFixture(true);
    memory.u32(HEAP + 0xe600 + 0x24, 1);
    geometryUnavailable(geometryProbe(memory), "geometry_link_point_count_invalid");
  }

  private void geometryDiscontinuity() {
    FakeMemory memory = geometryFixture(false);
    geometryUnavailable(geometryProbe(memory), "geometry_point_discontinuity");
  }

  private void geometryBackingZero() {
    FakeMemory memory = geometryFixture(true);
    memory.u32(HEAP + 0xe850, 0);
    geometryUnavailable(geometryProbe(memory), "geometry_backing_window_out_of_bounds");
    require(!memory.reads.containsKey((HEAP + 0xec00) + ":32"), "空数组仍读取坐标内存");
  }

  private void geometryBackingOverrun() {
    FakeMemory memory = geometryFixture(true);
    memory.u32(HEAP + 0xe850, 3);
    geometryUnavailable(geometryProbe(memory), "geometry_backing_window_out_of_bounds");
    require(!memory.reads.containsKey((HEAP + 0xec00) + ":32"), "越界窗口仍读取坐标内存");
  }

  private void geometryBackingOffsetOverrun() {
    FakeMemory memory = geometryFixture(true);
    memory.u32(HEAP + 0xea50, 4);
    geometryUnavailable(geometryProbe(memory), "geometry_backing_window_out_of_bounds");
    require(!memory.reads.containsKey((HEAP + 0xee08) + ":32"), "非零起点越界仍读取坐标内存");
  }

  private void geometryBackingShrink() {
    FakeMemory memory = geometryFixture(true);
    memory.mutation = (fake, address, size) -> {
      if (address == HEAP + 0xe5a0 && size == 8 && fake.reads.get(address + ":" + size) == 2)
        fake.u32(HEAP + 0xe850, 3);
    };
    geometryUnavailable(geometryProbe(memory), "geometry_backing_window_out_of_bounds");
    require(memory.reads.get((HEAP + 0xec00) + ":32") == 1, "第二遍未重新检查数组长度");
  }

  private void geometryBackingExactEnd() {
    FakeMemory memory = geometryFixture(true);
    geometryObserved(geometryProbe(memory));
    memory.u32(HEAP + 0xe850, 0xffffffffL);
    geometryObserved(geometryProbe(memory));
  }

  private static JSONObject geometryDiagnosis(NativeRouteProbe.GeometrySnapshot value) throws Exception {
    try {
      return (JSONObject) value.getClass().getDeclaredField("diagnosis").get(value);
    } catch (NoSuchFieldException missing) {
      throw new AssertionError("geometry_rejection_evidence_missing");
    }
  }

  private void geometryRejectionJoin() throws Exception {
    FakeMemory memory = geometryFixture(false);
    NativeRouteProbe.GeometrySnapshot value = geometryProbe(memory);
    geometryUnavailable(value, "geometry_point_discontinuity");
    JSONObject diagnosis = geometryDiagnosis(value);
    require(diagnosis != null, "拒绝时丢失断点证据");
    require(diagnosis.getInt("schema_version") == 1
        && diagnosis.getString("reason").equals("geometry_point_discontinuity")
        && diagnosis.getInt("geometry_read_pass") == 1
        && diagnosis.getInt("source_coordinate_scale") == 3600000, "断点单位或读取轮次错误");
    require(diagnosis.getInt("previous_segment_index") == 0
        && diagnosis.getInt("previous_link_index") == 1
        && diagnosis.getInt("previous_link_local_start") == 1
        && diagnosis.getInt("previous_link_point_count") == 4
        && diagnosis.getInt("next_segment_index") == 1
        && diagnosis.getInt("next_link_index") == 0
        && diagnosis.getInt("next_link_local_start") == 0
        && diagnosis.getInt("next_link_point_count") == 2, "断点源窗口错误");
    require(diagnosis.getJSONArray("previous_link_tail_native").toString().equals(
            "[[3600100,7200000],[3600200,7200000],[3600300,7200000],[3600400,7200000]]")
        && diagnosis.getJSONArray("next_link_head_native").toString().equals(
            "[[3600401,7200000],[3600500,7200000]]"), "断点原始整数坐标被舍入或补连");
    require(diagnosis.getString("path_identity_hash").equals(
            "deb576695affddd90ec337f246e5e0c0bbc77251c89ecdc9ecc6bca1f0a6e411")
        && diagnosis.getString("process_epoch_hash").matches("[0-9a-f]{64}")
        && diagnosis.getLong("observation_started_monotonic_ns") == value.startedNs
        && diagnosis.getLong("observation_finished_monotonic_ns") == value.finishedNs
        && diagnosis.getInt("read_calls") == value.readCalls
        && diagnosis.getInt("read_bytes") == value.readBytes, "拒绝证据未绑定本次原始读取");
    int backingHeaderReads = memory.reads.getOrDefault((HEAP + 0xe850) + ":16", 0)
        + memory.reads.getOrDefault((HEAP + 0xe950) + ":16", 0)
        + memory.reads.getOrDefault((HEAP + 0xea50) + ":16", 0);
    require(value.readCalls == 51 && value.readBytes == 656 + backingHeaderReads * 8,
        "断点诊断额外读取目标内存");
    require(diagnosis.getBoolean("diagnostic_only")
        && !diagnosis.getBoolean("source_snapshot_consistent")
        && !diagnosis.getBoolean("control_input_allowed")
        && !diagnosis.getBoolean("route_render_input_allowed")
        && diagnosis.getString("observation_clock_domain").equals("phone_elapsed_realtime")
        && diagnosis.toString().length() < 2048, "拒绝证据错误认证或未有界");
  }

  private void geometryRejectionSecondRead() throws Exception {
    FakeMemory memory = geometryFixture(true);
    memory.mutation = (fake, address, size) -> {
      if (address == HEAP + 0xed00 && size == 16 && fake.reads.get(address + ":" + size) == 2)
        fake.u32(address, 3_600_401);
    };
    NativeRouteProbe.GeometrySnapshot value = geometryProbe(memory);
    geometryUnavailable(value, "geometry_point_discontinuity");
    JSONObject diagnosis = geometryDiagnosis(value);
    require(diagnosis != null && diagnosis.getInt("geometry_read_pass") == 2
        && diagnosis.getJSONArray("next_link_head_native").getJSONArray(0).getInt(0) == 3_600_401
        && !diagnosis.getBoolean("source_snapshot_consistent"), "第二遍断点冒充稳定双读");
  }

  private void geometryRejectionRecovery() throws Exception {
    NativeRouteProbe.GeometrySnapshot rejected = geometryProbe(geometryFixture(false));
    require(geometryDiagnosis(rejected) != null, "拒绝证据缺失");
    NativeRouteProbe.GeometrySnapshot recovered = geometryProbe(geometryFixture(true));
    geometryObserved(recovered);
    require(geometryDiagnosis(recovered) == null, "成功几何泄漏上次拒绝");
    FakeMemory invalid = geometryFixture(true);
    invalid.u32(HEAP + 0xed00 + 8, 180L * 3_600_000L + 1L);
    NativeRouteProbe.GeometrySnapshot other = geometryProbe(invalid);
    geometryUnavailable(other, "geometry_point_invalid");
    require(geometryDiagnosis(other) == null, "其他错误冒充段间断点");
  }

  private void geometryRejectionBounded() throws Exception {
    FakeMemory memory = geometryFixture(false);
    memory.u32(HEAP + 0xe600 + 0x24, 1024);
    memory.u32(HEAP + 0xea50, 1025);
    for (int index = 0; index <= 1024; index++) {
      writeGeometryPoint(memory, HEAP + 0xee00, index, 3_600_000 + index * 100, 7_200_000);
    }
    NativeRouteProbe.GeometrySnapshot value = geometryProbe(memory);
    geometryUnavailable(value, "geometry_point_discontinuity");
    JSONObject diagnosis = geometryDiagnosis(value);
    require(diagnosis != null && diagnosis.getInt("previous_link_point_count") == 1024
        && diagnosis.getJSONArray("previous_link_tail_native").toString().equals(
            "[[3702100,7200000],[3702200,7200000],[3702300,7200000],[3702400,7200000]]")
        && diagnosis.getJSONArray("next_link_head_native").length() == 2
        && diagnosis.toString().length() < 2048, "长link断点保存无界或取错尾部");
  }

  private void geometryRejectionCaptureOnce() throws Exception {
    Class<?> type;
    try {
      type = Class.forName("com.jixiexiaoge.drivingassist.navdiag.PhoneCaptureProbe$GeometryRejection");
    } catch (ClassNotFoundException missing) {
      throw new AssertionError("phone_geometry_rejection_not_archived");
    }
    Object pending = type.getDeclaredConstructor().newInstance();
    java.lang.reflect.Method record = type.getDeclaredMethod("record", NativeRouteProbe.GeometrySnapshot.class);
    java.lang.reflect.Method attach = type.getDeclaredMethod("attach", JSONObject.class);
    record.invoke(pending, geometryProbe(geometryFixture(false)));
    JSONObject first = new JSONObject().put("status", "unavailable").put("reason", "selection_changed_during_read");
    attach.invoke(pending, first);
    require(first.getJSONObject("_geometry_rejection").getString("reason").equals("geometry_point_discontinuity")
        && first.getString("status").equals("unavailable")
        && first.getString("reason").equals("selection_changed_during_read"), "记录拒绝时改变导航观察");
    JSONObject next = new JSONObject(); attach.invoke(pending, next);
    require(!next.has("_geometry_rejection"), "同次断点重复贴到后续采样");
    record.invoke(pending, geometryProbe(geometryFixture(false)));
    record.invoke(pending, geometryProbe(geometryFixture(true)));
    JSONObject recovered = new JSONObject(); attach.invoke(pending, recovered);
    require(!recovered.has("_geometry_rejection"), "恢复后携带陈旧断点");
  }

  private void geometryRange() {
    FakeMemory memory = geometryFixture(true);
    memory.u32(HEAP + 0xed00 + 8, 180L * 3_600_000L + 1L);
    geometryUnavailable(geometryProbe(memory), "geometry_point_invalid");
  }

  private void geometryReadRace() {
    FakeMemory memory = geometryFixture(true);
    final int[] reads = {0};
    memory.mutation = (fake, address, size) -> {
      if (address == HEAP + 0xed00 && size == 16 && ++reads[0] == 2) {
        fake.u32(address + 8, 3_600_600);
      }
    };
    geometryUnavailable(geometryProbe(memory), "geometry_changed_during_read");
  }

  private void geometrySelectionRace() {
    FakeMemory memory = geometryFixture(true);
    final int[] pathReads = {0};
    memory.mutation = (fake, address, size) -> {
      if (address == HEAP + 0xd000 && size == 24 && ++pathReads[0] == 2) {
        fake.u32(address + 0x14, 7_654_321);
      }
    };
    geometryUnavailable(geometryProbe(memory), "route_changed_during_geometry_read");
  }

  private void geometryEpochRace() {
    NativeRouteProbe.GeometrySnapshot value = NativeRouteProbe.probeGeometry(
        maps(), geometryFixture(true), hashes(), new NativeRouteProbe.EpochSource() {
          private int reads;
          @Override public String read() {
            reads++;
            return reads == 2 ? "changed" : EPOCH;
          }
        },
        NativeRouteProbe.PROFILE);
    geometryUnavailable(value, "process_epoch_changed");
  }

  private void geometryInactive() {
    FakeMemory memory = geometryFixture(true);
    memory.u32(HEAP + 0x713c, 4);
    geometryUnavailable(geometryProbe(memory), "gps_navigation_not_observed");
  }

  private void mapParserFields() throws Exception {
    java.util.List<NativeRouteProbe.MemoryMap> result = NativeRouteProbe.parseMaps(
        "30-40\tr--p 0010 00:ff 1 /system/lib with spaces.so\n10-20 rw-p 0000 01:AB 2\n");
    require(result.size() == 2 && result.get(0).start == 0x10 && result.get(1).start == 0x30, "maps排序或数量变化");
    NativeRouteProbe.MemoryMap first = result.get(0), second = result.get(1);
    require(first.end == 0x20 && first.permissions.equals("rw-p") && first.offset == 0
        && first.device.equals("01:AB") && first.inode == 2 && first.path.isEmpty(), "无路径maps字段变化");
    require(second.end == 0x40 && second.permissions.equals("r--p") && second.offset == 0x10
        && second.device.equals("00:ff") && second.inode == 1 && second.path.equals("/system/lib with spaces.so"),
        "预编译匹配器改变完整字段或路径空格");
  }
}
