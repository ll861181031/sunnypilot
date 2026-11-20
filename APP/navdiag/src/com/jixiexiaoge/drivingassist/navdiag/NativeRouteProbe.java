package com.jixiexiaoge.drivingassist.navdiag;

import android.os.SystemClock;

import org.json.JSONException;
import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.RandomAccessFile;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.charset.StandardCharsets;
import java.math.BigInteger;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.Comparator;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.TimeUnit;
import java.util.regex.Pattern;

/** 由 Root app_process 手动运行的只读探针；不是 Android 组件，不注入或调用目标函数。 */
public final class NativeRouteProbe {
  public static final String PROFILE = "amapauto-9.5.0.600013-arm64-v1";
  public static final String GBL_SHA256 = "934396f73adf760af02e77ae8bd650c4e659522acd28db35f501d7633675a419";
  public static final String DICE_SHA256 = "5884adf676cafb883f92372af11c0e1c67b8b9cb7da8fdd59d885e5b441e1b71";
  static final long MAX_ADDRESS = 1L << 48;
  static final int MAX_READ_BYTES = 16384;
  static final int MAX_READ_CALLS = 512;
  static final int GEOMETRY_READ_BYTES = 16 * 1024 * 1024;
  static final int GEOMETRY_READ_CALLS = 131072;
  static final int GEOMETRY_MAX_READ_SIZE = 8192;
  static final int GEOMETRY_MAX_PAYLOAD_BYTES = 8 * 1024 * 1024;
  static final int MAX_SEGMENTS = 8192;
  static final int MAX_LINKS = 100000;
  static final int MAX_POINTS = 500000;
  static final byte[] GEOMETRY_MAGIC = "AMAP_NAV_GEO_V2\0".getBytes(StandardCharsets.US_ASCII);
  static final int MAX_BUCKETS = 65536;
  static final int MAX_CHAIN_NODES = 64;
  static final int MAX_PATHS = 32;
  static final int MAX_NAVI_INFO_ITEMS = 32;
  static final int NAVI_INFO_ITEM_SIZE = 0x258;
  static final long DRIVE_PATH_VTABLE_RVA = 0x3d28040L;
  static final long DRIVE_IMPLEMENTATION_VTABLE_RVA = 0x3d29828L;
  static final long DRIVE_SEGMENT_ACCESSOR_VTABLE_RVA = 0x3d49f80L;
  static final long BASE_SEGMENT_ACCESSOR_VTABLE_RVA = 0x3d49310L;
  static final long DRIVE_PATH_GETTER_RVA = 0x37709a4L;
  static final long DRIVE_PATH_ID_GETTER_RVA = 0x379237cL;
  static final long DRIVE_SEGMENT_COUNT_GETTER_RVA = 0x3792638L;
  static final long SNAPSHOT_RETRY_WINDOW_NS = 50_000_000L;
  private static final String PACKAGE = "com.autonavi.amapauto";
  private static final Pattern LIBRARY_PATH = Pattern.compile(
      "/data/app/com\\.autonavi\\.amapauto-[A-Za-z0-9_+=-]+/lib/arm64/(libGbl\\.so|libAutoDice\\.so)");
  private static final Pattern MAP_FIELDS = Pattern.compile("\\s+");
  private static final Pattern MAP_PERMISSIONS = Pattern.compile("[r-][w-][x-][ps]");
  private static final Pattern MAP_DEVICE = Pattern.compile("[0-9a-fA-F]+:[0-9a-fA-F]+");

  private NativeRouteProbe() { }

  interface MemorySource { byte[] read(long address, int size) throws Exception; }
  interface EpochSource { String read() throws Exception; }

  static final class ProbeException extends Exception {
    ProbeException(String reason) { super(reason); }
  }

  static final class MemoryMap {
    final long start;
    final long end;
    final String permissions;
    final long offset;
    final String device;
    final long inode;
    final String path;

    MemoryMap(long start, long end, String permissions, long offset, String device, long inode, String path) {
      this.start = start;
      this.end = end;
      this.permissions = permissions;
      this.offset = offset;
      this.device = device;
      this.inode = inode;
      this.path = path;
    }
  }

  static List<MemoryMap> parseMaps(String text) throws ProbeException {
    if (text == null || text.length() > 2 * 1024 * 1024 || text.isEmpty()) throw new ProbeException("invalid_maps");
    String[] lines = text.split("\\n");
    if (lines.length == 0 || lines.length > 20000) throw new ProbeException("invalid_maps");
    ArrayList<MemoryMap> maps = new ArrayList<>();
    try {
      for (String line : lines) {
        String[] parts = MAP_FIELDS.split(line.trim(), 6);
        if (parts.length < 5) throw new ProbeException("invalid_maps");
        String[] bounds = parts[0].split("-", -1);
        if (bounds.length != 2) throw new ProbeException("invalid_maps");
        long start = Long.parseLong(bounds[0], 16);
        long end = Long.parseLong(bounds[1], 16);
        long offset = Long.parseLong(parts[2], 16);
        long inode = Long.parseLong(parts[4]);
        if (start <= 0 || end <= start || end > MAX_ADDRESS || offset < 0 || inode < 0
            || !MAP_PERMISSIONS.matcher(parts[1]).matches() || !MAP_DEVICE.matcher(parts[3]).matches()) {
          throw new ProbeException("invalid_maps");
        }
        maps.add(new MemoryMap(start, end, parts[1], offset, parts[3], inode, parts.length == 6 ? parts[5] : ""));
      }
    } catch (NumberFormatException error) { throw new ProbeException("invalid_maps"); }
    Collections.sort(maps, Comparator.comparingLong(map -> map.start));
    for (int i = 1; i < maps.size(); i++) {
      if (maps.get(i - 1).end > maps.get(i).start) throw new ProbeException("overlapping_maps");
    }
    return maps;
  }

  static long libraryBase(List<MemoryMap> maps, String name) throws ProbeException {
    Set<String> identities = new HashSet<>();
    ArrayList<MemoryMap> roots = new ArrayList<>();
    boolean found = false;
    for (MemoryMap map : maps) {
      if (!map.path.endsWith("/" + name) || !LIBRARY_PATH.matcher(map.path).matches()) continue;
      found = true;
      identities.add(map.path + "\n" + map.device + "\n" + map.inode);
      if (map.offset == 0 && map.permissions.indexOf('r') >= 0) roots.add(map);
    }
    if (!found) throw new ProbeException("library_mapping_missing");
    if (identities.size() != 1 || roots.size() != 1) throw new ProbeException("library_mapping_ambiguous");
    return roots.get(0).start;
  }

  static final class BoundedReader {
    final List<MemoryMap> maps;
    final MemorySource source;
    final Map<Long, Long> immutable = new HashMap<>();
    final int maximumCalls;
    final int maximumBytes;
    final int maximumReadSize;
    int calls;
    int bytes;
    long deadlineNs;
    String stage = "initializing";
    int geometryReadPass;
    JSONObject geometryDiagnosis;

    BoundedReader(List<MemoryMap> maps, MemorySource source) {
      this(maps, source, MAX_READ_CALLS, MAX_READ_BYTES, 64);
    }

    BoundedReader(List<MemoryMap> maps, MemorySource source, int maximumCalls, int maximumBytes,
                  int maximumReadSize) {
      if (maximumCalls <= 0 || maximumBytes <= 0 || maximumReadSize <= 0) {
        throw new IllegalArgumentException("invalid_reader_budget");
      }
      this.maps = maps;
      this.source = source;
      this.maximumCalls = maximumCalls;
      this.maximumBytes = maximumBytes;
      this.maximumReadSize = maximumReadSize;
    }

    void check(long address, int size, char permission) throws ProbeException {
      if (address <= 0 || address >= MAX_ADDRESS || size <= 0 || address + size > MAX_ADDRESS) {
        throw new ProbeException("invalid_address");
      }
      for (MemoryMap map : maps) {
        if (map.start <= address && address + size <= map.end && map.permissions.indexOf(permission) >= 0) return;
      }
      throw new ProbeException(permission == 'r' ? "unmapped_read" : "non_executable_target");
    }

    byte[] read(long address, int size) throws ProbeException {
      checkDeadline();
      if (size > maximumReadSize || calls >= maximumCalls || (long) bytes + size > maximumBytes) {
        throw new ProbeException("read_budget_exceeded");
      }
      check(address, size, 'r');
      calls++;
      bytes += size;
      byte[] raw;
      try { raw = source.read(address, size); }
      catch (Exception error) { throw new ProbeException("read_failed"); }
      checkDeadline();
      if (raw == null || raw.length != size) throw new ProbeException("short_read");
      return raw;
    }

    void checkDeadline() throws ProbeException {
      if (deadlineNs != 0 && SystemClock.elapsedRealtimeNanos() >= deadlineNs) {
        throw new ProbeException("snapshot_retry_deadline_exceeded");
      }
    }

    long validatePointer(long value, boolean nullable) throws ProbeException {
      if (value == 0 && nullable) return 0;
      if (value == 0) throw new ProbeException("null_pointer");
      if ((value & 7L) != 0) throw new ProbeException("unaligned_pointer");
      check(value, 8, 'r');
      return value;
    }

    long pointer(long address, boolean nullable) throws ProbeException { return validatePointer(u64(address), nullable); }
    long pointer(long address) throws ProbeException { return pointer(address, false); }
    long u64(long address) throws ProbeException { return little(read(address, 8)).getLong(); }
    long u32(long address) throws ProbeException { return little(read(address, 4)).getInt() & 0xffffffffL; }

    void checkMutableSegmentObject(long address) throws ProbeException {
      if (address <= 0 || address >= MAX_ADDRESS || (address & 7L) != 0) {
        throw new ProbeException("segment_object_invalid");
      }
      for (MemoryMap map : maps) {
        if (map.start <= address && address + 8 <= map.end && map.permissions.indexOf('r') >= 0
            && map.permissions.indexOf('w') >= 0
            && (map.path.isEmpty() || map.path.startsWith("[anon:"))) return;
      }
      throw new ProbeException("segment_object_mapping_invalid");
    }

    void checkFixedLibraryReadOnly(long address, long libraryBase) throws ProbeException {
      if (address <= 0 || address >= MAX_ADDRESS || (address & 7L) != 0 || address < libraryBase) {
        throw new ProbeException("segment_vtable_invalid");
      }
      for (MemoryMap map : maps) {
        if (libraryBase <= map.start && map.start <= address && address + 8 <= map.end
            && map.permissions.indexOf('r') >= 0 && map.permissions.indexOf('w') < 0
            && map.path.endsWith("/libAutoDice.so")) return;
      }
      throw new ProbeException("segment_vtable_mapping_invalid");
    }

    void checkFixedLibraryExecutable(long address, long libraryBase) throws ProbeException {
      if (address <= 0 || address >= MAX_ADDRESS || address < libraryBase) {
        throw new ProbeException("segment_getter_invalid");
      }
      for (MemoryMap map : maps) {
        if (libraryBase <= map.start && map.start <= address && address + 4 <= map.end
            && map.permissions.indexOf('x') >= 0 && map.path.endsWith("/libAutoDice.so")) return;
      }
      throw new ProbeException("segment_getter_mapping_invalid");
    }

    long immutableU64(long address) throws ProbeException {
      boolean valid = false;
      for (MemoryMap map : maps) {
        if (map.start <= address && address + 8 <= map.end && map.permissions.indexOf('r') >= 0
            && map.permissions.indexOf('w') < 0
            && (map.path.endsWith("/libGbl.so") || map.path.endsWith("/libAutoDice.so"))) valid = true;
      }
      if (!valid) throw new ProbeException("immutable_mapping_not_read_only");
      if (!immutable.containsKey(address)) immutable.put(address, u64(address));
      return immutable.get(address);
    }
  }

  private static ByteBuffer little(byte[] raw) { return ByteBuffer.wrap(raw).order(ByteOrder.LITTLE_ENDIAN); }

  private static long manager(BoundedReader reader, long gbl, List<Long> chain) throws ProbeException {
    reader.stage = "registry_root";
    long registry = reader.pointer(gbl + 0x2aad120L);
    reader.stage = "registry_buckets";
    ByteBuffer header = little(reader.read(registry + 0x48, 16));
    long buckets = reader.validatePointer(header.getLong(), false);
    long count = header.getLong();
    if (count < 1 || count > MAX_BUCKETS) throw new ProbeException("registry_bucket_count");
    long key = gbl + 0x7e6024L;
    long previous = reader.pointer(buckets + (key % count) * 8, true);
    if (previous == 0) throw new ProbeException("manager_not_registered");
    long node = reader.pointer(previous, true);
    Set<Long> visited = new HashSet<>();
    for (int i = 0; i < MAX_CHAIN_NODES; i++) {
      reader.stage = "registry_node";
      if (node == 0) throw new ProbeException("manager_not_registered");
      if (!visited.add(node)) throw new ProbeException("registry_cycle");
      ByteBuffer data = little(reader.read(node, 40));
      long next = data.getLong();
      long hash = data.getLong();
      long typeInfo = data.getLong();
      data.getLong();
      long wrapper = data.getLong();
      if (Long.remainderUnsigned(hash, count) != key % count) throw new ProbeException("manager_not_registered");
      if (hash == key) {
        reader.validatePointer(typeInfo, false);
        long name = reader.immutableU64(typeInfo + 8);
        if (name == key) {
          if (typeInfo != gbl + 0x2985828L) throw new ProbeException("manager_type_mismatch");
          reader.stage = "manager_instance";
          reader.validatePointer(wrapper, false);
          long value = reader.pointer(wrapper + 0x130);
          for (long item : new long[] {registry, buckets, count, previous, node, wrapper, value}) chain.add(item);
          return value;
        }
      }
      node = reader.validatePointer(next, true);
    }
    throw new ProbeException("registry_chain_limit");
  }

  private static final class Observation {
    final List<Long> chain = new ArrayList<>();
    byte[] header;
    long navType;
    long navState;
    long count;
    long mainIndex;
    Long pathId;
    Long driveImplementation;
    JSONObject position;
    JSONObject naviInfo;

    boolean same(Observation other) {
      return chain.equals(other.chain) && Arrays.equals(header, other.header)
          && navType == other.navType && navState == other.navState && count == other.count
          && mainIndex == other.mainIndex && (pathId == null ? other.pathId == null : pathId.equals(other.pathId))
          && (position == null ? other.position == null
              : other.position != null && position.toString().equals(other.position.toString()))
          && (naviInfo == null ? other.naviInfo == null
              : other.naviInfo != null && naviInfo.toString().equals(other.naviInfo.toString()));
    }
  }

  private static final class GeometryObservation {
    final Observation path;
    final byte[] payload;

    GeometryObservation(Observation path, byte[] payload) {
      this.path = path;
      this.payload = payload;
    }
  }

  static final class GeometrySnapshot {
    final String status;
    final String reason;
    final byte[] payload;
    final int readCalls;
    final int readBytes;
    final long startedNs;
    final long finishedNs;
    final JSONObject diagnosis;

    GeometrySnapshot(String status, String reason, byte[] payload, int readCalls, int readBytes,
                     long startedNs, long finishedNs) {
      this(status, reason, payload, readCalls, readBytes, startedNs, finishedNs, null);
    }

    GeometrySnapshot(String status, String reason, byte[] payload, int readCalls, int readBytes,
                     long startedNs, long finishedNs, JSONObject diagnosis) {
      this.status = status;
      this.reason = reason;
      this.payload = payload;
      this.readCalls = readCalls;
      this.readBytes = readBytes;
      this.startedNs = startedNs;
      this.finishedNs = finishedNs;
      this.diagnosis = diagnosis;
      if (diagnosis != null) {
        try {
          diagnosis.put("observation_started_monotonic_ns", startedNs)
              .put("observation_finished_monotonic_ns", finishedNs)
              .put("read_calls", readCalls).put("read_bytes", readBytes);
        } catch (JSONException impossible) { throw new IllegalStateException(impossible); }
      }
    }
  }

  private static final String[] POSITION_DIAG_FIELDS = {
      "status", "reason", "coordinate_frame_verified", "source_freshness_verified", "raw_gps_verified",
      "route_anchor_verified", "route_render_input_allowed", "control_input_allowed", "source", "source_type_raw",
      "coordinate_frame_candidate", "source_clock", "native_st_pos_deg", "matched_st_pos_deg", "segment_index",
      "link_index", "point_index", "point_index_source_field", "match_index", "match_count", "is_on_guide_road_raw",
      "tick_time_raw", "input_tick_time_raw", "path_identity_hash"};

  private static final String[] NAVI_INFO_DIAG_FIELDS = {
      "schema_version", "status", "reason", "diagnostic_only", "control_input_allowed",
      "route_render_input_allowed", "source", "path_identity_hash", "route_remain_time_raw",
      "route_remain_distance_raw", "item_count", "item_index", "current_segment", "current_link",
      "current_point", "branch_flag_raw", "branches"};

  private static boolean samePositionField(JSONObject first, JSONObject second, String key) throws JSONException {
    boolean firstHas = first != null && first.has(key);
    boolean secondHas = second != null && second.has(key);
    if (firstHas != secondHas) return false;
    if (!firstHas) return true;
    Object before = first.get(key), after = second.get(key);
    // 使用与整份位置 JSON 相同的精确序列化比较，不引入坐标容差或数值舍入。
    return before.getClass().equals(after.getClass()) && before.toString().equals(after.toString());
  }

  private static JSONObject selectionDiagnosis(Observation first, Observation second,
                                                boolean includeNaviInfo) throws JSONException {
    JSONArray changed = new JSONArray();
    if (!first.chain.equals(second.chain)) changed.put("chain");
    if (!Arrays.equals(first.header, second.header)) changed.put("header");
    if (first.navType != second.navType) changed.put("nav_type");
    if (first.navState != second.navState) changed.put("nav_state");
    if (first.count != second.count) changed.put("path_count");
    if (first.mainIndex != second.mainIndex) changed.put("main_index");
    if (first.pathId == null ? second.pathId != null : !first.pathId.equals(second.pathId)) changed.put("path_id");
    boolean positionChanged = first.position == null ? second.position != null
        : second.position == null || !first.position.toString().equals(second.position.toString());
    if (positionChanged) changed.put("position");
    JSONArray positionFields = new JSONArray();
    if (positionChanged) {
      for (String key : POSITION_DIAG_FIELDS) {
        if (!samePositionField(first.position, second.position, key)) positionFields.put(key);
      }
    }
    boolean naviInfoChanged = first.naviInfo == null ? second.naviInfo != null
        : second.naviInfo == null || !first.naviInfo.toString().equals(second.naviInfo.toString());
    if (naviInfoChanged) changed.put("navi_info");
    JSONArray naviInfoFields = new JSONArray();
    if (naviInfoChanged) {
      for (String key : NAVI_INFO_DIAG_FIELDS) {
        if (!samePositionField(first.naviInfo, second.naviInfo, key)) naviInfoFields.put(key);
      }
    }
    // 只记录本轮已读快照的字段差异，不输出值、坐标、PathID 或内存地址，不追加内存读取。
    JSONObject result = new JSONObject().put("schema_version", includeNaviInfo ? 2 : 1)
        .put("source", "native_observation_pair")
        .put("changed_fields", changed).put("position_changed_fields", positionFields)
        .put("position_serialization_changed", positionChanged);
    if (includeNaviInfo) {
      result.put("navi_info_changed_fields", naviInfoFields)
          .put("navi_info_serialization_changed", naviInfoChanged);
    }
    return result;
  }

  private static boolean canRetryPosition(Observation first, Observation second, JSONObject diagnosis,
                                          boolean includeNaviInfo) throws JSONException {
    if (first.position == null || second.position == null
        || !first.position.getString("status").equals("observed")
        || !second.position.getString("status").equals("observed")
        || first.navType != 0 || second.navType != 0 || first.pathId == null || second.pathId == null
        || (first.navState != 1 && first.navState != 3) || (second.navState != 1 && second.navState != 3)) return false;
    JSONArray fields = diagnosis.getJSONArray("changed_fields");
    if (fields.length() != 1 || !fields.getString(0).equals("position")) return false;
    JSONArray positionFields = diagnosis.getJSONArray("position_changed_fields");
    if (includeNaviInfo && (diagnosis.getBoolean("navi_info_serialization_changed")
        || diagnosis.getJSONArray("navi_info_changed_fields").length() != 0)) return false;
    // 仅处理实测的更新竞争；B/C 仍须逐字段完全一致，两个 tick 均保留原值。
    // 位置后台同步两个时钟时，A/B 可能同时出现两个 tick 变化；这只放行
    // 一次有界 C 观察；同时更新的原始坐标不作容差接受，B/C 必须完全一致。
    // 实际移动时原始与匹配坐标也会同次更新；仅新增已记录的两种字段组合，
    // 不包含 segment/link/point、匹配项、选路或动作变化，仍核完整 B/C 快照。
    // PathID、匹配锚点、动作或其他字段变化仍立即拒绝。
    return positionFields.toString().equals("[\"tick_time_raw\"]")
        || positionFields.toString().equals("[\"tick_time_raw\",\"input_tick_time_raw\"]")
        || positionFields.toString().equals("[\"native_st_pos_deg\",\"tick_time_raw\"]")
        || positionFields.toString().equals("[\"native_st_pos_deg\",\"tick_time_raw\",\"input_tick_time_raw\"]")
        || positionFields.toString().equals("[\"native_st_pos_deg\",\"matched_st_pos_deg\",\"tick_time_raw\"]")
        || positionFields.toString().equals("[\"native_st_pos_deg\",\"matched_st_pos_deg\",\"tick_time_raw\",\"input_tick_time_raw\"]");
  }

  private static JSONObject positionUnavailable(String reason) {
    JSONObject result = new JSONObject();
    try {
      result.put("status", "unavailable").put("reason", reason).put("coordinate_frame_verified", false)
          .put("source_freshness_verified", false).put("raw_gps_verified", false).put("route_anchor_verified", false)
          .put("route_render_input_allowed", false).put("control_input_allowed", false);
    } catch (JSONException impossible) { throw new IllegalStateException(impossible); }
    return result;
  }

  private static boolean geographic(double[] values) {
    for (double value : values) if (!Double.isFinite(value)) return false;
    return values[0] >= -180 && values[0] <= 180 && values[1] >= -90 && values[1] <= 90
        && (values[0] != 0.0 || values[1] != 0.0);
  }

  private static Number unsignedLong(long value) {
    return value >= 0 ? Long.valueOf(value) : new BigInteger(Long.toUnsignedString(value));
  }

  private static JSONObject position(BoundedReader reader, long manager, Observation path) throws ProbeException {
    if (path.pathId == null || path.navType != 0 || (path.navState != 1 && path.navState != 3)) {
      return positionUnavailable("gps_navigation_not_observed");
    }
    reader.stage = "position_flags";
    if (reader.read(manager + 0x38, 1)[0] != 1) return positionUnavailable("position_missing");
    ByteBuffer flags = little(reader.read(manager + 0x40, 12));
    long usable = flags.getInt() & 0xffffffffL;
    long simulated = flags.getInt() & 0xffffffffL;
    long sourceType = flags.getInt() & 0xffffffffL;
    if (usable != 1) return positionUnavailable("position_not_usable");
    if (simulated != 0) return positionUnavailable("position_simulated");
    reader.stage = "position_match_header";
    long count = reader.u32(manager + 0x360);
    if (count < 1 || count > 3) return positionUnavailable("position_match_count");
    int selectedIndex = -1;
    long selectedItem = 0;
    int segment = 0, link = 0, point = 0;
    long onRoad = 0;
    for (int index = 0; index < count; index++) {
      long item = manager + 0x1c8 + index * 0x88L;
      ByteBuffer fields = little(reader.read(item + 0x68, 20));
      int itemSegment = fields.getInt(), itemLink = fields.getInt(), itemPoint = fields.getInt();
      long itemOnRoad = fields.getInt() & 0xffffffffL;
      long itemPath = fields.getInt() & 0xffffffffL;
      if (itemPath != path.pathId) continue;
      if (selectedIndex >= 0) return positionUnavailable("position_main_path_match_ambiguous");
      selectedIndex = index;
      selectedItem = item;
      segment = itemSegment;
      link = itemLink;
      point = itemPoint;
      onRoad = itemOnRoad;
    }
    if (selectedIndex < 0) return positionUnavailable("position_main_path_match_missing");
    // 精确值 1 的 OnRoute 消费链已验证；组合值、网络路和特殊状态仍全部拒绝。
    if (onRoad != 1) return positionUnavailable("position_not_on_route");
    if (segment < 0 || link < 0 || point < 0) return positionUnavailable("position_index_invalid");
    reader.stage = "position_coordinates";
    ByteBuffer nativeBuffer = little(reader.read(manager + 0x60, 24));
    ByteBuffer matchedBuffer = little(reader.read(selectedItem, 24));
    double[] nativePos = {nativeBuffer.getDouble(), nativeBuffer.getDouble(), nativeBuffer.getDouble()};
    double[] matchedPos = {matchedBuffer.getDouble(), matchedBuffer.getDouble(), matchedBuffer.getDouble()};
    if (!geographic(nativePos) || !geographic(matchedPos)) return positionUnavailable("position_coordinate_invalid");
    reader.stage = "position_source_ticks";
    ByteBuffer ticks = little(reader.read(manager + 0x1b0, 16));
    long tick = ticks.getLong(), inputTick = ticks.getLong();
    if (tick == 0 || inputTick == 0) return positionUnavailable("position_source_tick_missing");
    // 原生 tick 只作为源 token；尚未证明其时钟域，不能用本次读取时间替代。
    JSONObject result = positionUnavailable("unverified_position_semantics");
    try {
      result.put("status", "observed").put("source", "native_loc_info").put("source_type_raw", sourceType)
          .put("coordinate_frame_candidate", "amap_native_map").put("source_clock", "unknown")
          .put("native_st_pos_deg", new JSONArray().put(nativePos[0]).put(nativePos[1]))
          .put("matched_st_pos_deg", new JSONArray().put(matchedPos[0]).put(matchedPos[1]))
          .put("segment_index", segment).put("link_index", link).put("point_index", point)
          .put("point_index_source_field", "postCur").put("match_index", selectedIndex).put("match_count", count)
          .put("is_on_guide_road_raw", onRoad).put("tick_time_raw", unsignedLong(tick))
          .put("input_tick_time_raw", unsignedLong(inputTick));
    } catch (JSONException impossible) { throw new IllegalStateException(impossible); }
    return result;
  }

  private static JSONObject naviInfoUnavailable(String reason) {
    JSONObject result = new JSONObject();
    try {
      result.put("schema_version", 2).put("status", "unavailable").put("reason", reason)
          .put("diagnostic_only", true).put("control_input_allowed", false)
          .put("route_render_input_allowed", false);
    } catch (JSONException impossible) { throw new IllegalStateException(impossible); }
    return result;
  }

  private static int checkedNonnegativeInt(BoundedReader reader, long address, String reason) throws ProbeException {
    long value = reader.u32(address);
    if (value > Integer.MAX_VALUE) throw new ProbeException(reason);
    return (int) value;
  }

  // 高德的 32 位索引、距离和分叉字段在无对应对象时用 u32 的 -1 哨兵
  //（0xffffffff）表示。真实空分支会同时出现这些哨兵，必须保留原值。
  private static boolean invalidUnsignedField(long value) {
    return value != 0xffffffffL && value > Integer.MAX_VALUE;
  }

  private static JSONObject segmentBindingUnavailable(String reason) throws JSONException {
    return new JSONObject().put("status", "unavailable").put("reason", reason);
  }

  private static boolean segmentBindingFailure(String reason) {
    return "segment_vector_unavailable".equals(reason)
        || "drive_impl_vtable_mismatch".equals(reason)
        || "drive_impl_segment_count_getter_mismatch".equals(reason)
        || "segment_vector_invalid".equals(reason)
        || "segment_index_out_of_bounds".equals(reason)
        || "segment_object_invalid".equals(reason)
        || "segment_object_mapping_invalid".equals(reason)
        || "segment_wrapper_vtable_rejected".equals(reason)
        || "segment_vtable_invalid".equals(reason)
        || "segment_vtable_mapping_invalid".equals(reason)
        || "segment_getter_invalid".equals(reason)
        || "segment_getter_mapping_invalid".equals(reason)
        || "segment_getter_semantics_mismatch".equals(reason);
  }

  private static JSONObject segmentBinding(BoundedReader reader, long dice, Observation path,
                                           long segmentIndex) throws ProbeException {
    try {
      if (segmentIndex == 0xffffffffL) {
        return segmentBindingUnavailable("segment_index_unavailable");
      }
      if (path.driveImplementation == null) throw new ProbeException("segment_vector_unavailable");
      long implementation = path.driveImplementation;
      reader.stage = "navi_info_segment_count";
      reader.validatePointer(implementation, false);
      long implementationVtable = reader.u64(implementation);
      if (implementationVtable != dice + DRIVE_IMPLEMENTATION_VTABLE_RVA) {
        throw new ProbeException("drive_impl_vtable_mismatch");
      }
      long countGetter = reader.immutableU64(implementationVtable + 0xb8);
      if (countGetter != dice + DRIVE_SEGMENT_COUNT_GETTER_RVA) {
        throw new ProbeException("drive_impl_segment_count_getter_mismatch");
      }
      reader.checkFixedLibraryExecutable(countGetter, dice);

      reader.stage = "navi_info_segment_vector";
      ByteBuffer vector = little(reader.read(implementation + 0x60, 16));
      long begin = vector.getLong();
      long end = vector.getLong();
      if (begin <= 0 || end < begin || (begin & 7L) != 0 || (end & 7L) != 0) {
        throw new ProbeException("segment_vector_invalid");
      }
      long span = end - begin;
      if (span / 8 > MAX_SEGMENTS || segmentIndex >= span / 8) {
        throw new ProbeException("segment_index_out_of_bounds");
      }
      reader.check(begin, (int) span, 'r');

      reader.stage = "navi_info_segment_object";
      long segment = reader.u64(begin + segmentIndex * 8);
      reader.checkMutableSegmentObject(segment);
      long segmentVtable = reader.u64(segment);
      if (segmentVtable == dice + DRIVE_SEGMENT_ACCESSOR_VTABLE_RVA
          || segmentVtable == dice + BASE_SEGMENT_ACCESSOR_VTABLE_RVA) {
        throw new ProbeException("segment_wrapper_vtable_rejected");
      }
      reader.checkFixedLibraryReadOnly(segmentVtable, dice);

      reader.stage = "navi_info_segment_getters";
      long iconIdGetter = reader.immutableU64(segmentVtable + 0xb8);
      long iconGetter = reader.immutableU64(segmentVtable + 0x128);
      if (iconIdGetter == dice + DRIVE_SEGMENT_COUNT_GETTER_RVA
          || iconGetter == dice + DRIVE_SEGMENT_COUNT_GETTER_RVA) {
        throw new ProbeException("segment_getter_semantics_mismatch");
      }
      reader.checkFixedLibraryExecutable(iconIdGetter, dice);
      reader.checkFixedLibraryExecutable(iconGetter, dice);
      return new JSONObject().put("status", "verified").put("reason", "segment_object_verified")
          .put("segment_object_verified", true)
          .put("icon_id_getter_vtable_verified", true)
          .put("icon_getter_vtable_verified", true);
    } catch (JSONException impossible) {
      throw new IllegalStateException(impossible);
    }
  }

  private static JSONObject naviInfoData(BoundedReader reader, long dice, Observation path,
                                         long address) throws ProbeException {
    reader.check(address, 60, 'r');
    ByteBuffer data = little(reader.read(address, 60));
    long maneuverId = data.getInt(0) & 0xffffffffL;
    long maneuverSegment = data.getInt(4) & 0xffffffffL;
    long maneuverLink = data.getInt(8) & 0xffffffffL;
    long maneuverPoint = data.getInt(12) & 0xffffffffL;
    long exitNumber = data.getInt(16) & 0xffffffffL;
    long exitAngle = data.getInt(20) & 0xffffffffL;
    long obtainSupported = data.get(24) & 0xffL;
    long dataChanged = data.get(25) & 0xffL;
    long segmentRemainTime = data.getInt(28) & 0xffffffffL;
    long segmentRemainDistance = data.getInt(32) & 0xffffffffL;
    long nextRoadNameSegment = data.getInt(48) & 0xffffffffL;
    long nextRoadNameLink = data.getInt(52) & 0xffffffffL;
    long split = data.getInt(56) & 0xffffffffL;
    if (invalidUnsignedField(maneuverSegment)) throw new ProbeException("navi_info_maneuver_segment_invalid");
    if (invalidUnsignedField(maneuverLink)) throw new ProbeException("navi_info_maneuver_link_invalid");
    if (invalidUnsignedField(maneuverPoint)) throw new ProbeException("navi_info_maneuver_point_invalid");
    if (obtainSupported > 1) throw new ProbeException("navi_info_obtain_supported_invalid");
    if (dataChanged > 1) throw new ProbeException("navi_info_data_changed_invalid");
    if (invalidUnsignedField(segmentRemainTime)) throw new ProbeException("navi_info_segment_time_invalid");
    if (invalidUnsignedField(segmentRemainDistance)) throw new ProbeException("navi_info_segment_distance_invalid");
    // These signed display-name indices are not route/pose indices. Keep their raw
    // bits for full A/B comparison; naviInfo quarantines both action branches below.
    if (invalidUnsignedField(split)) throw new ProbeException("navi_info_split_invalid");
    try {
      JSONObject binding;
      try {
        binding = segmentBinding(reader, dice, path, maneuverSegment);
      } catch (ProbeException error) {
        // 只把 segment 自身的结构或语义失败降级为动作不可用。reader 预算、短读、
        // 映射、截止时间及完整性错误必须继续向上抛，不能把整份快照伪装成仅动作缺失。
        if (!segmentBindingFailure(error.getMessage())) throw error;
        binding = segmentBindingUnavailable(error.getMessage());
      }
      return new JSONObject().put("maneuver_id_raw", unsignedLong(maneuverId))
          .put("maneuver_segment_raw", maneuverSegment).put("maneuver_link_raw", maneuverLink)
          .put("maneuver_point_raw", maneuverPoint).put("exit_number_raw", exitNumber)
          .put("exit_angle_raw", exitAngle).put("obtain_supported_raw", obtainSupported)
          .put("data_changed_raw", dataChanged).put("segment_remain_time_raw", segmentRemainTime)
          .put("segment_remain_distance_raw", segmentRemainDistance)
          .put("next_road_name_segment_raw", nextRoadNameSegment)
          .put("next_road_name_link_raw", nextRoadNameLink).put("split_raw", split)
          .put("segment_binding", binding);
    } catch (JSONException impossible) { throw new IllegalStateException(impossible); }
  }

  private static JSONObject naviInfo(BoundedReader reader, long manager, Observation path,
                                     long dice) throws ProbeException {
    if (path.pathId == null || path.navType != 0 || (path.navState != 1 && path.navState != 3)) {
      return naviInfoUnavailable("gps_navigation_not_observed");
    }
    reader.stage = "navi_info_header";
    long items = reader.pointer(manager + 0xaa0, true);
    long count = reader.u32(manager + 0xaac);
    // An empty optional action list does not invalidate the independent route or pose.
    // The full observation pair still rejects empty/observed transitions.
    if (count == 0) return naviInfoUnavailable("navi_info_count_zero");
    if (count > MAX_NAVI_INFO_ITEMS) throw new ProbeException("navi_info_count_invalid");
    if (items == 0) throw new ProbeException("null_pointer");
    if ((items & 7L) != 0) throw new ProbeException("unaligned_pointer");
    reader.check(items, (int) count * NAVI_INFO_ITEM_SIZE, 'r');
    int matchedIndex = -1;
    long matchedItem = 0;
    for (int index = 0; index < count; index++) {
      long item = items + (long) index * NAVI_INFO_ITEM_SIZE;
      reader.stage = "navi_info_item";
      ByteBuffer header = little(reader.read(item, 16));
      long itemPathId = header.getInt(0) & 0xffffffffL;
      if (itemPathId != path.pathId) continue;
      if (matchedIndex >= 0) throw new ProbeException("navi_info_path_match_ambiguous");
      matchedIndex = index;
      matchedItem = item;
    }
    // No matching action is independent of route/position validity; snapshot comparison
    // still rejects any transition between missing and observed action data.
    if (matchedIndex < 0) return naviInfoUnavailable("navi_info_path_match_missing");
    reader.stage = "navi_info_fields";
    ByteBuffer itemHeader = little(reader.read(matchedItem, 16));
    long infoType = itemHeader.getInt(4) & 0xffffffffL;
    long routeRemainTime = itemHeader.getInt(8) & 0xffffffffL;
    long routeRemainDistance = itemHeader.getInt(12) & 0xffffffffL;
    int currentSegment = checkedNonnegativeInt(reader, matchedItem + 0x60, "navi_info_current_segment_invalid");
    int currentLink = checkedNonnegativeInt(reader, matchedItem + 0x64, "navi_info_current_link_invalid");
    int currentPoint = checkedNonnegativeInt(reader, matchedItem + 0x68, "navi_info_current_point_invalid");
    long flag = reader.u32(matchedItem + 0xd0);
    if (flag != 0 && flag != 1) throw new ProbeException("navi_info_branch_flag_invalid");
    // NaviInfoData 是内联在 NaviInfo item 里的两个 64 字节分支，不是堆上指针：
    // named serializer 记录 navi_info_data_0=0xd8 / navi_info_data_1=0x118，间隔 0x40，
    // 且 maneuver 字段为「相对 NaviInfoData 分支」的偏移（0x0..0x38）。
    JSONObject branch0 = naviInfoData(reader, dice, path, matchedItem + 0xd8);
    JSONObject branch1 = naviInfoData(reader, dice, path, matchedItem + 0x118);
    JSONObject result = naviInfoUnavailable("unverified_navi_info_semantics");
    try {
      String nextRoadFailure = null;
      for (JSONObject branch : new JSONObject[] {branch0, branch1}) {
        if (invalidUnsignedField(branch.getLong("next_road_name_segment_raw"))) {
          nextRoadFailure = "navi_info_next_segment_invalid";
        } else if (invalidUnsignedField(branch.getLong("next_road_name_link_raw"))) {
          nextRoadFailure = "navi_info_next_link_invalid";
        }
        if (nextRoadFailure != null) break;
      }
      if (nextRoadFailure != null) {
        // Retain both bindings exactly as observed, including any semantic failure.
        // Replacing them would hide a binding race during the full A/B comparison.
        result.put("reason", nextRoadFailure);
      }
      result.put("status", "observed").put("source", "native_navi_info")
          .put("route_remain_time_raw", routeRemainTime)
          .put("route_remain_distance_raw", routeRemainDistance)
          .put("navigation_info_type_raw", infoType).put("item_count", count)
          .put("item_index", matchedIndex).put("current_segment", currentSegment)
          .put("current_link", currentLink).put("current_point", currentPoint)
          .put("branch_flag_raw", flag)
          .put("branches", new JSONArray().put(branch0).put(branch1));
    } catch (JSONException impossible) { throw new IllegalStateException(impossible); }
    return result;
  }

  private static Observation observe(BoundedReader reader, long gbl, long dice, boolean includePosition,
                                     boolean includeNaviInfo) throws ProbeException {
    Observation value = new Observation();
    long manager = manager(reader, gbl, value.chain);
    reader.stage = "main_path_header";
    value.header = reader.read(manager + 0xb00, 36);
    ByteBuffer header = little(value.header);
    long paths = header.getLong(0);
    value.count = header.getInt(12) & 0xffffffffL;
    value.mainIndex = header.getInt(32) & 0xffffffffL;
    ByteBuffer nav = little(reader.read(manager + 0x1138, 8));
    value.navType = nav.getInt() & 0xffffffffL;
    value.navState = nav.getInt() & 0xffffffffL;
    if (includePosition) value.position = positionUnavailable("gps_navigation_not_observed");
    if (includeNaviInfo) value.naviInfo = naviInfoUnavailable("gps_navigation_not_observed");
    if (value.count > MAX_PATHS) throw new ProbeException("path_count_limit");
    if (value.count == 0) {
      if (value.mainIndex != 0) throw new ProbeException("main_index_out_of_bounds");
      return value;
    }
    if (value.mainIndex >= value.count) throw new ProbeException("main_index_out_of_bounds");
    if (paths == 0 || (paths & 7L) != 0) throw new ProbeException("unaligned_pointer");
    reader.check(paths, (int) value.count * 0x50, 'r');
    long info = paths + value.mainIndex * 0x50;
    reader.stage = "path_info";
    long a = reader.pointer(info + 0x48);
    long b = reader.pointer(a + 8);
    long drivePath = reader.pointer(b + 0x48);
    reader.stage = "drive_path";
    ByteBuffer path = little(reader.read(drivePath, 16));
    long vtable = path.getLong();
    long implementation = path.getLong();
    if (vtable != dice + DRIVE_PATH_VTABLE_RVA) throw new ProbeException("drive_path_vtable_mismatch");
    if (reader.immutableU64(dice + DRIVE_PATH_VTABLE_RVA + 0x20) != dice + DRIVE_PATH_GETTER_RVA) {
      throw new ProbeException("drive_path_getter_mismatch");
    }
    reader.check(dice + DRIVE_PATH_GETTER_RVA, 4, 'x');
    reader.validatePointer(implementation, false);
    reader.stage = "drive_path_implementation";
    ByteBuffer impl = little(reader.read(implementation, 24));
    if (impl.getLong(0) != dice + DRIVE_IMPLEMENTATION_VTABLE_RVA) throw new ProbeException("drive_impl_vtable_mismatch");
    if (reader.immutableU64(dice + DRIVE_IMPLEMENTATION_VTABLE_RVA + 0x20) != dice + DRIVE_PATH_ID_GETTER_RVA) {
      throw new ProbeException("drive_impl_getter_mismatch");
    }
    reader.check(dice + DRIVE_PATH_ID_GETTER_RVA, 4, 'x');
    value.pathId = impl.getInt(0x14) & 0xffffffffL;
    value.driveImplementation = implementation;
    if (value.pathId == 0) throw new ProbeException("invalid_path_id");
    for (long item : new long[] {info, a, b, drivePath, implementation}) value.chain.add(item);
    if (includePosition) value.position = position(reader, manager, value);
    if (includeNaviInfo) value.naviInfo = naviInfo(reader, manager, value, dice);
    return value;
  }

  private static int checkedGeometryU32(ByteBuffer buffer, int offset, String reason) throws ProbeException {
    long value = buffer.getInt(offset) & 0xffffffffL;
    if (value > Integer.MAX_VALUE) throw new ProbeException(reason);
    return (int) value;
  }

  private static final class GeometryLink {
    final int segment;
    final int compactStart;
    final int compactCount;
    final int[] points;
    final int linkIndex;
    final int localStart;

    GeometryLink(int segment, int compactStart, int compactCount, int[] points, int linkIndex, int localStart) {
      this.segment = segment;
      this.compactStart = compactStart;
      this.compactCount = compactCount;
      this.points = points;
      this.linkIndex = linkIndex;
      this.localStart = localStart;
    }
  }

  private static JSONArray geometryPointContext(int[] points, boolean tail) throws JSONException {
    JSONArray result = new JSONArray();
    int count = points.length / 2;
    int begin = tail ? Math.max(0, count - 4) : 0;
    int end = tail ? count : Math.min(count, 4);
    for (int index = begin; index < end; index++) {
      result.put(new JSONArray().put(points[index * 2]).put(points[index * 2 + 1]));
    }
    return result;
  }

  private static JSONObject geometryJoinDiagnosis(Observation path, String epoch, int readPass,
                                                   GeometryLink previous, int nextSegment, int nextLink,
                                                   int localStart, int[] points) throws ProbeException {
    try {
      return new JSONObject().put("schema_version", 1).put("reason", "geometry_point_discontinuity")
          .put("diagnostic_only", true).put("source_snapshot_consistent", false)
          .put("control_input_allowed", false).put("route_render_input_allowed", false)
          .put("observation_clock_domain", "phone_elapsed_realtime")
          .put("path_identity_hash", identityHash(path.pathId, epoch))
          .put("process_epoch_hash", sha256(epoch.getBytes(StandardCharsets.UTF_8)))
          .put("geometry_read_pass", readPass).put("source_coordinate_scale", 3600000)
          .put("previous_segment_index", previous.segment).put("previous_link_index", previous.linkIndex)
          .put("previous_link_local_start", previous.localStart)
          .put("previous_link_point_count", previous.points.length / 2)
          .put("next_segment_index", nextSegment).put("next_link_index", nextLink)
          .put("next_link_local_start", localStart).put("next_link_point_count", points.length / 2)
          .put("previous_link_tail_native", geometryPointContext(previous.points, true))
          .put("next_link_head_native", geometryPointContext(points, false));
    } catch (JSONException impossible) { throw new IllegalStateException(impossible); }
  }

  private static int[] geometryLinkPoints(BoundedReader reader, long object, int localStart,
                                          int count) throws ProbeException {
    reader.stage = "geometry_points";
    long backing = reader.pointer(object + 0xa0);
    reader.stage = "geometry_backing_window";
    ByteBuffer backingHeader = little(reader.read(backing + 0x50, 16));
    long backingPointCount = backingHeader.getInt(0) & 0xffffffffL;
    // Match the fixed library's getPoints/getPointCount bounds before touching coordinates.
    if (localStart < 0 || count < 2 || localStart >= backingPointCount
        || (long) localStart + count > backingPointCount) {
      throw new ProbeException("geometry_backing_window_out_of_bounds");
    }
    long pointsAddress = reader.validatePointer(backingHeader.getLong(8), false);
    reader.stage = "geometry_point_range";
    reader.check(pointsAddress + (long) localStart * 8, Math.min(count * 8, GEOMETRY_MAX_READ_SIZE), 'r');
    int[] points = new int[count * 2];
    int sourceOffset = 0;
    while (sourceOffset < count) {
      int chunkCount = Math.min(count - sourceOffset, GEOMETRY_MAX_READ_SIZE / 8);
      reader.stage = "geometry_points";
      ByteBuffer part = little(reader.read(
          pointsAddress + (long) (localStart + sourceOffset) * 8, chunkCount * 8));
      for (int chunkIndex = 0; chunkIndex < chunkCount; chunkIndex++) {
        int x = part.getInt(chunkIndex * 8);
        int y = part.getInt(chunkIndex * 8 + 4);
        if (x == 0 && y == 0 || x < -180 * 3600000 || x > 180 * 3600000
            || y < -90 * 3600000 || y > 90 * 3600000) {
          throw new ProbeException("geometry_point_invalid");
        }
        int output = (sourceOffset + chunkIndex) * 2;
        points[output] = x;
        points[output + 1] = y;
      }
      sourceOffset += chunkCount;
    }
    return points;
  }

  private static byte[] geometryPayload(BoundedReader reader, Observation path, long manager,
                                        long dice, String epoch) throws ProbeException {
    if (path.pathId == null || path.navType != 0 || (path.navState != 1 && path.navState != 3)) {
      throw new ProbeException("gps_navigation_not_observed");
    }
    reader.stage = "geometry_path_header";
    ByteBuffer header = little(reader.read(manager + 0xb00, 36));
    long paths = header.getLong(0);
    int count = checkedGeometryU32(header, 12, "path_count_limit");
    int mainIndex = checkedGeometryU32(header, 32, "main_index_out_of_bounds");
    if (mainIndex >= count || (paths & 7L) != 0) throw new ProbeException("geometry_path_header_invalid");
    long info = paths + (long) mainIndex * 0x50;
    reader.stage = "geometry_path_wrapping";
    long a = reader.pointer(info + 0x48);
    long b = reader.pointer(a + 8);
    long c = reader.pointer(b + 0x48);
    long drivePath = reader.pointer(c + 8);
    ByteBuffer rawPath = little(reader.read(drivePath, 16));
    if (rawPath.getLong(0) != dice + 0x3d29828L) throw new ProbeException("drive_impl_vtable_mismatch");
    reader.stage = "geometry_drive_path";
    long segmentBegin = reader.pointer(drivePath + 0x60, true);
    long segmentEnd = reader.pointer(drivePath + 0x68, true);
    if (segmentBegin == 0 || segmentEnd < segmentBegin || ((segmentEnd - segmentBegin) & 7L) != 0) {
      throw new ProbeException("geometry_segment_vector_invalid");
    }
    long segmentBytes = segmentEnd - segmentBegin;
    if (segmentBytes / 8 > MAX_SEGMENTS) throw new ProbeException("geometry_segment_limit");
    int segmentCount = (int) (segmentBytes / 8);
    if (segmentCount < 1) throw new ProbeException("geometry_segment_vector_invalid");
    reader.check(segmentBegin, (int) Math.min(segmentBytes, 64), 'r');

    ArrayList<GeometryLink> links = new ArrayList<>();
    ArrayList<Integer> routePoints = new ArrayList<>();
    long rawPointCount = 0;
    for (int segmentIndex = 0; segmentIndex < segmentCount; segmentIndex++) {
      reader.stage = "geometry_segment";
      long segment = reader.pointer(segmentBegin + (long) segmentIndex * 8);
      long linkBegin = reader.pointer(segment + 0x70, true);
      long linkEnd = reader.pointer(segment + 0x78, true);
      if (linkBegin == 0 || linkEnd < linkBegin || ((linkEnd - linkBegin) & 7L) != 0) {
        throw new ProbeException("geometry_link_vector_invalid");
      }
      long linkBytes = linkEnd - linkBegin;
      if (linkBytes / 8 > MAX_LINKS - links.size()) throw new ProbeException("geometry_link_limit");
      int linkCount = (int) (linkBytes / 8);
      if (linkCount < 1) throw new ProbeException("geometry_link_vector_invalid");
      reader.check(linkBegin, (int) Math.min(linkBytes, 64), 'r');
      int previousLocalStart = -1;
      int[] previousLocalPoints = null;
      int previousGlobalStart = -1;
      for (int linkIndex = 0; linkIndex < linkCount; linkIndex++) {
        reader.stage = "geometry_link";
        long link = reader.pointer(linkBegin + (long) linkIndex * 8);
        ByteBuffer fields = little(reader.read(link + 0x24, 16));
        int pointCount = checkedGeometryU32(fields, 0, "geometry_point_count_invalid");
        int localStart = checkedGeometryU32(fields, 4, "geometry_point_window_invalid");
        if (!validGeometryCount(0, pointCount)) throw new ProbeException("geometry_link_point_count_invalid");
        if (!validGeometryCount(localStart, pointCount)) {
          throw new ProbeException("geometry_link_point_window_invalid");
        }
        if ((linkIndex == 0 && segmentIndex == 0 && localStart != 0)
            || (linkIndex == 0 && segmentIndex > 0 && localStart < 0)
            || (linkIndex > 0 && localStart <= previousLocalStart)) {
          throw new ProbeException("geometry_link_index_discontinuity");
        }
        if (rawPointCount + pointCount > (long) MAX_LINKS * MAX_POINTS) {
          throw new ProbeException("geometry_point_limit");
        }
        int[] points = geometryLinkPoints(reader, link, localStart, pointCount);
        int overlap = 0;
        int globalStart;
        if (linkIndex == 0) {
          globalStart = routePoints.size() / 2;
          if (segmentIndex > 0) {
            if (routePoints.size() < 2) throw new ProbeException("geometry_point_limit");
            int previous = routePoints.size() - 2;
            if (routePoints.get(previous) != points[0]
                || routePoints.get(previous + 1) != points[1]) {
              reader.geometryDiagnosis = geometryJoinDiagnosis(path, epoch, reader.geometryReadPass,
                  links.get(links.size() - 1), segmentIndex, linkIndex, localStart, points);
              throw new ProbeException("geometry_point_discontinuity");
            }
            globalStart--;
            overlap = 1;
          }
        } else {
          if (previousLocalPoints == null || localStart <= previousLocalStart
              || localStart >= previousLocalStart + previousLocalPoints.length / 2) {
            throw new ProbeException("geometry_link_overlap_out_of_bounds");
          }
          overlap = previousLocalStart + previousLocalPoints.length / 2 - localStart;
          if (overlap < 1 || overlap >= pointCount || overlap > previousLocalPoints.length / 2) {
            throw new ProbeException("geometry_link_overlap_out_of_bounds");
          }
          int previousOffset = (localStart - previousLocalStart) * 2;
          for (int point = 0; point < overlap; point++) {
            if (previousLocalPoints[previousOffset + point * 2] != points[point * 2]
                || previousLocalPoints[previousOffset + point * 2 + 1] != points[point * 2 + 1]) {
              throw new ProbeException("geometry_link_overlap_mismatch");
            }
          }
          globalStart = previousGlobalStart + localStart - previousLocalStart;
        }
        int compactCount = pointCount;
        if (pointCount - overlap < 1 || globalStart < 0
            || globalStart + compactCount > MAX_POINTS) {
          throw new ProbeException("geometry_point_limit");
        }
        links.add(new GeometryLink(segmentIndex, globalStart, compactCount, points, linkIndex, localStart));
        for (int point = overlap; point < pointCount; point++) {
          int x = points[point * 2], y = points[point * 2 + 1];
          routePoints.add(x);
          routePoints.add(y);
        }
        rawPointCount += pointCount;
        previousLocalStart = localStart;
        previousLocalPoints = points;
        previousGlobalStart = globalStart;
      }
    }

    int outputPointCount = routePoints.size() / 2;
    if (outputPointCount < 2) throw new ProbeException("geometry_point_limit");

    int frameSize = 112 + links.size() * 12 + outputPointCount * 8;
    if (frameSize > GEOMETRY_MAX_PAYLOAD_BYTES) throw new ProbeException("geometry_payload_limit");
    ByteBuffer frame = ByteBuffer.allocate(frameSize).order(ByteOrder.LITTLE_ENDIAN);
    frame.put(GEOMETRY_MAGIC);
    frame.putInt(1).putInt((int) path.navType).putInt((int) path.navState).putInt((int) count)
        .putInt(mainIndex).putInt(segmentCount).putInt(links.size()).putInt(outputPointCount);
    frame.put(hexBytes(identityHash(path.pathId, epoch)));
    frame.put(sha256Bytes(epoch.getBytes(StandardCharsets.UTF_8)));

    for (GeometryLink item : links) {
      frame.putInt(item.segment).putInt(item.compactStart).putInt(item.compactCount);
    }
    for (int value : routePoints) frame.putInt(value);
    if (frame.position() != 112 + links.size() * 12 + outputPointCount * 8) {
      throw new ProbeException("geometry_point_limit");
    }
    return Arrays.copyOf(frame.array(), frame.position());
  }

  private static boolean validGeometryCount(int start, int count) {
    return start >= 0 && count >= 2 && start <= MAX_POINTS - count;
  }

  private static GeometryObservation observeGeometry(BoundedReader reader, long gbl, long dice,
                                                     String epoch) throws ProbeException {
    Observation before = observe(reader, gbl, dice, false, false);
    if (before.pathId == null) throw new ProbeException("gps_navigation_not_observed");
    long manager = manager(reader, gbl, new ArrayList<>());
    byte[] payload = geometryPayload(reader, before, manager, dice, epoch);
    Observation after = observe(reader, gbl, dice, false, false);
    if (!before.same(after)) throw new ProbeException("route_changed_during_geometry_read");
    return new GeometryObservation(after, payload);
  }

  /**
   * 仅用于 instrumentation 证明双读差异；生产入口始终使用同一 live reader。
   * 两个 reader 共享底层只读内存视图，但各自重新读取，因此能在第二遍前注入变化。
   */
  static GeometryObservation geometryObservationForTest(List<MemoryMap> maps, MemorySource source,
                                                        long gbl, long dice, String epoch)
      throws ProbeException {
    BoundedReader reader = new BoundedReader(maps, source, GEOMETRY_READ_CALLS, GEOMETRY_READ_BYTES,
        GEOMETRY_MAX_READ_SIZE);
    return observeGeometry(reader, gbl, dice, epoch);
  }

  static GeometrySnapshot probeGeometry(String mapsText, MemorySource source,
                                        Map<String, String> hashes, EpochSource epoch,
                                        String profile) {
    long started = SystemClock.elapsedRealtimeNanos();
    BoundedReader reader = null;
    GeometrySnapshot result;
    try {
      if (!PROFILE.equals(profile) || !GBL_SHA256.equals(hashes.get("libGbl.so"))
          || !DICE_SHA256.equals(hashes.get("libAutoDice.so"))) {
        throw new ProbeException("unsupported_binary_profile");
      }
      List<MemoryMap> maps = parseMaps(mapsText);
      long gbl = libraryBase(maps, "libGbl.so");
      long dice = libraryBase(maps, "libAutoDice.so");
      String before;
      try { before = epoch.read(); }
      catch (Exception error) { throw new ProbeException("process_epoch_failed"); }
      if (before == null || before.isEmpty() || before.length() > 256) {
        throw new ProbeException("invalid_process_epoch");
      }
      reader = new BoundedReader(maps, source, GEOMETRY_READ_CALLS, GEOMETRY_READ_BYTES,
          GEOMETRY_MAX_READ_SIZE);
      reader.geometryReadPass = 1;
      GeometryObservation first = observeGeometry(reader, gbl, dice, before);
      reader.geometryReadPass = 2;
      GeometryObservation second = observeGeometry(reader, gbl, dice, before);
      if (!Arrays.equals(first.payload, second.payload)
          || first.path.pathId == null || second.path.pathId == null
          || !first.path.pathId.equals(second.path.pathId)) {
        throw new ProbeException("geometry_changed_during_read");
      }
      String after;
      try { after = epoch.read(); }
      catch (Exception error) { throw new ProbeException("process_epoch_failed"); }
      if (!before.equals(after)) throw new ProbeException("process_epoch_changed");
      long finished = SystemClock.elapsedRealtimeNanos();
      result = new GeometrySnapshot("observed", "consistent_read_only_geometry", second.payload,
          reader.calls, reader.bytes, started, finished);
    } catch (ProbeException error) {
      long finished = SystemClock.elapsedRealtimeNanos();
      result = new GeometrySnapshot("unavailable", error.getMessage(), new byte[0],
          reader == null ? 0 : reader.calls, reader == null ? 0 : reader.bytes, started, finished,
          reader == null ? null : reader.geometryDiagnosis);
    } catch (Exception error) {
      long finished = SystemClock.elapsedRealtimeNanos();
      result = new GeometrySnapshot("unavailable", "probe_internal_failure", new byte[0],
          reader == null ? 0 : reader.calls, reader == null ? 0 : reader.bytes, started, finished);
    }
    return result;
  }

  static JSONObject unavailable(String reason) {
    JSONObject result = new JSONObject();
    try {
      result.put("schema_version", 1).put("profile", PROFILE).put("status", "unavailable").put("reason", reason)
          .put("path_identity_hash", JSONObject.NULL).put("navigation_state", "unknown")
          .put("navigation_running_observed", false).put("sample_consistent", false)
          .put("gps_navigation_observed", false).put("active_route_verified", false)
          .put("control_input_allowed", false).put("source", "bounded_native_memory")
          .put("implementation", "java_read_only_no_injection").put("atomic_snapshot", false)
          .put("observation_clock_domain", "phone_elapsed_realtime");
    } catch (JSONException impossible) { throw new IllegalStateException(impossible); }
    return result;
  }

  static JSONObject probeSnapshot(String mapsText, MemorySource source, Map<String, String> hashes,
                                  EpochSource epoch, String profile) {
    return probeSnapshot(mapsText, source, hashes, epoch, profile, false);
  }

  static JSONObject probeSnapshot(String mapsText, MemorySource source, Map<String, String> hashes,
                                  EpochSource epoch, String profile, boolean includePosition) {
    return probeSnapshot(mapsText, source, hashes, epoch, profile, includePosition, false);
  }

  static JSONObject probeSnapshot(String mapsText, MemorySource source, Map<String, String> hashes,
                                  EpochSource epoch, String profile, boolean includePosition,
                                  boolean includeNaviInfo) {
    return probeSnapshot(mapsText, source, hashes, epoch, profile, includePosition, includeNaviInfo, null, null);
  }

  private static JSONObject probeSnapshot(String mapsText, MemorySource source, Map<String, String> hashes,
                                         EpochSource epoch, String profile, boolean includePosition,
                                         boolean includeNaviInfo, JSONObject performance, List<MemoryMap> preparedMaps) {
    long started = SystemClock.elapsedRealtimeNanos();
    JSONObject result = unavailable("unknown");
    if (includePosition) {
      try { result.put("_position", positionUnavailable("snapshot_unavailable")); }
      catch (JSONException impossible) { throw new IllegalStateException(impossible); }
    }
    if (includeNaviInfo) {
      try { result.put("_navi_info", naviInfoUnavailable("snapshot_unavailable")); }
      catch (JSONException impossible) { throw new IllegalStateException(impossible); }
    }
    BoundedReader reader = null;
    try {
      if (!PROFILE.equals(profile) || !GBL_SHA256.equals(hashes.get("libGbl.so"))
          || !DICE_SHA256.equals(hashes.get("libAutoDice.so"))) throw new ProbeException("unsupported_binary_profile");
      long parseStarted = SystemClock.elapsedRealtimeNanos();
      // preparedMaps 仅由本轮 LiveSource 完整解析后传入，不缓存跨轮 maps 或跳过任何字段校验。
      List<MemoryMap> maps = preparedMaps == null ? parseMaps(mapsText) : preparedMaps;
      if (performance != null) performance.put("snapshot_maps_parse_ns",
          preparedMaps == null ? SystemClock.elapsedRealtimeNanos() - parseStarted : 0);
      long gbl = libraryBase(maps, "libGbl.so");
      long dice = libraryBase(maps, "libAutoDice.so");
      String before;
      try { before = epoch.read(); }
      catch (Exception error) { throw new ProbeException("process_epoch_failed"); }
      if (before == null || before.isEmpty() || before.length() > 256) throw new ProbeException("invalid_process_epoch");
      reader = new BoundedReader(maps, source);
      Observation first = observe(reader, gbl, dice, includePosition, includeNaviInfo);
      Observation second = observe(reader, gbl, dice, includePosition, includeNaviInfo);
      String after;
      try { after = epoch.read(); }
      catch (Exception error) { throw new ProbeException("process_epoch_failed"); }
      if (!before.equals(after)) throw new ProbeException("process_epoch_changed");
      if (!first.same(second)) {
        JSONObject diagnosis = selectionDiagnosis(first, second, includeNaviInfo);
        // 动作必须完全不变；仅已知位置更新组合可触发一次完整 C 观察。
        if (!canRetryPosition(first, second, diagnosis, includeNaviInfo)) {
          result.put("_selection_diagnosis", diagnosis);
          throw new ProbeException("selection_changed_during_read");
        }
        JSONObject retry = new JSONObject().put("schema_version", 1).put("source", "native_observation_pair")
            .put("extra_observations", 1).put("recovered", false).put("initial_diagnosis", diagnosis);
        result.put("_snapshot_retry", retry);
        // 原始开始时间与同一 reader 的累计预算均不重置；只允许一次额外完整观察。
        reader.deadlineNs = started + SNAPSHOT_RETRY_WINDOW_NS;
        reader.checkDeadline();
        Observation third = observe(reader, gbl, dice, includePosition, includeNaviInfo);
        reader.checkDeadline();
        String finalEpoch;
        try { finalEpoch = epoch.read(); }
        catch (Exception error) { throw new ProbeException("process_epoch_failed"); }
        reader.checkDeadline();
        if (!before.equals(finalEpoch)) throw new ProbeException("process_epoch_changed");
        if (!second.same(third)) {
          result.put("_selection_diagnosis", selectionDiagnosis(second, third, includeNaviInfo));
          throw new ProbeException("selection_changed_during_read");
        }
        second = third;
        retry.put("recovered", true);
      }
      String state = second.navState == 1 ? "started" : second.navState == 2 ? "paused"
          : second.navState == 3 ? "resumed" : second.navState == 4 ? "stopped"
          : second.navState == 5 ? "arrived" : "unknown";
      boolean running = second.pathId != null && (second.navState == 1 || second.navState == 3);
      result.put("status", "observed").put("reason", "consistent_read_only_observation")
          .put("sample_consistent", true).put("navigation_state", state)
          .put("navi_type_raw", second.navType).put("navi_state_raw", second.navState)
          .put("main_index", second.mainIndex).put("path_count", second.count)
          .put("navigation_running_observed", running).put("gps_navigation_observed", running && second.navType == 0)
          .put("path_identity_hash", second.pathId == null ? JSONObject.NULL : identityHash(second.pathId, before))
          .put("process_epoch_hash", sha256(before.getBytes(StandardCharsets.UTF_8)));
      if (includePosition) {
        if (second.position.getString("status").equals("observed")) {
          second.position.put("path_identity_hash", result.getString("path_identity_hash"));
        }
        result.put("_position", second.position);
      }
      if (includeNaviInfo) {
        if (second.naviInfo.getString("status").equals("observed")) {
          second.naviInfo.put("path_identity_hash", result.getString("path_identity_hash"));
        }
        result.put("_navi_info", second.naviInfo);
      }
    } catch (ProbeException error) {
      try { result.put("reason", error.getMessage()).put("failure_stage", reader == null ? "profile_or_maps" : reader.stage); }
      catch (JSONException impossible) { throw new IllegalStateException(impossible); }
    } catch (Exception error) {
      try { result.put("reason", "probe_internal_failure").put("failure_stage", reader == null ? "profile_or_maps" : reader.stage); }
      catch (JSONException impossible) { throw new IllegalStateException(impossible); }
    }
    try {
      long finished = SystemClock.elapsedRealtimeNanos();
      if (reader != null && reader.deadlineNs != 0 && finished >= reader.deadlineNs
          && result.getString("status").equals("observed")) {
        JSONObject retry = result.getJSONObject("_snapshot_retry").put("recovered", false);
        result = unavailable("snapshot_retry_deadline_exceeded");
        result.put("_snapshot_retry", retry).put("_position", positionUnavailable("snapshot_unavailable"))
            .put("failure_stage", reader.stage);
        if (includeNaviInfo) result.put("_navi_info", naviInfoUnavailable("snapshot_unavailable"));
      }
      result.put("read_calls", reader == null ? 0 : reader.calls).put("read_bytes", reader == null ? 0 : reader.bytes)
          .put("observation_started_monotonic_ns", started).put("observation_finished_monotonic_ns", finished)
          .put("observation_window_ms", Math.round((finished - started) / 1000.0) / 1000.0);
    } catch (JSONException impossible) { throw new IllegalStateException(impossible); }
    return result;
  }

  static String identityHash(long pathId, String epoch) throws ProbeException {
    if (pathId <= 0 || pathId > 0xffffffffL || epoch == null || epoch.isEmpty() || epoch.length() > 256) {
      throw new ProbeException("invalid_path_identity");
    }
    ByteArrayOutputStream out = new ByteArrayOutputStream();
    try {
      out.write("amap-path-observation-v1\0".getBytes(StandardCharsets.UTF_8));
      out.write(epoch.getBytes(StandardCharsets.UTF_8));
      out.write(0);
      out.write(ByteBuffer.allocate(4).order(ByteOrder.LITTLE_ENDIAN).putInt((int) pathId).array());
    } catch (IOException impossible) { throw new IllegalStateException(impossible); }
    return sha256(out.toByteArray());
  }

  private static String hex(byte[] bytes) {
    StringBuilder result = new StringBuilder();
    final char[] digits = "0123456789abcdef".toCharArray();
    for (byte value : bytes) {
      result.append(digits[(value >> 4) & 15]);
      result.append(digits[value & 15]);
    }
    return result.toString();
  }

  private static byte[] hexBytes(String value) throws ProbeException {
    if (value == null || value.length() != 64) throw new ProbeException("geometry_identity_invalid");
    byte[] result = new byte[32];
    for (int index = 0; index < result.length; index++) {
      int high = Character.digit(value.charAt(index * 2), 16);
      int low = Character.digit(value.charAt(index * 2 + 1), 16);
      if (high < 0 || low < 0) throw new ProbeException("geometry_identity_invalid");
      result[index] = (byte) ((high << 4) | low);
    }
    return result;
  }

  static byte[] sha256Bytes(byte[] raw) throws ProbeException {
    try { return MessageDigest.getInstance("SHA-256").digest(raw); }
    catch (Exception error) { throw new ProbeException("sha256_unavailable"); }
  }

  static String sha256(byte[] raw) throws ProbeException {
    return hex(sha256Bytes(raw));
  }

  private static byte[] readLimited(InputStream input, int maximum) throws IOException, ProbeException {
    ByteArrayOutputStream out = new ByteArrayOutputStream();
    byte[] buffer = new byte[8192];
    int count;
    while ((count = input.read(buffer, 0, Math.min(buffer.length, maximum + 1 - out.size()))) != -1) {
      out.write(buffer, 0, count);
      if (out.size() > maximum) throw new ProbeException("source_size_limit");
    }
    return out.toByteArray();
  }

  private static String readText(String path, int maximum) throws IOException, ProbeException {
    try (FileInputStream input = new FileInputStream(path)) {
      return new String(readLimited(input, maximum), StandardCharsets.UTF_8);
    }
  }

  private static String fileHash(String path) throws Exception {
    if (!LIBRARY_PATH.matcher(path).matches()) throw new ProbeException("library_path_not_supported");
    MessageDigest digest = MessageDigest.getInstance("SHA-256");
    try (FileInputStream input = new FileInputStream(path)) {
      byte[] buffer = new byte[16384];
      long size = 0;
      int count;
      while ((count = input.read(buffer)) != -1) {
        size += count;
        if (size > 128L * 1024L * 1024L) throw new ProbeException("library_file_size_limit");
        digest.update(buffer, 0, count);
      }
    }
    return hex(digest.digest());
  }

  private static int providerPid() throws Exception {
    Process process = new ProcessBuilder("/system/bin/pidof", PACKAGE).redirectErrorStream(true).start();
    try {
      if (!process.waitFor(2, TimeUnit.SECONDS)) throw new ProbeException("provider_pid_unavailable");
      String value = new String(readLimited(process.getInputStream(), 128), StandardCharsets.US_ASCII).trim();
      if (process.exitValue() != 0 || !value.matches("[1-9][0-9]{0,9}")) throw new ProbeException("provider_pid_unavailable");
      int pid = Integer.parseInt(value);
      if (pid < 1) throw new ProbeException("provider_pid_unavailable");
      return pid;
    } finally {
      process.destroy();
      if (!process.waitFor(1, TimeUnit.SECONDS)) {
        process.destroyForcibly();
        process.waitFor(1, TimeUnit.SECONDS);
      }
      process.getInputStream().close();
      process.getOutputStream().close();
      process.getErrorStream().close();
    }
  }

  static final class LiveSource implements AutoCloseable {
    final int pid;
    final String epoch;
    final String boot;
    final Map<String, String> hashes = new HashMap<>();
    final Map<String, String> libraryIdentities = new HashMap<>();
    final RandomAccessFile memory;

    LiveSource() throws Exception {
      pid = providerPid();
      boot = readText("/proc/sys/kernel/random/boot_id", 64).trim();
      if (!boot.matches("[0-9a-f-]{36}")) throw new ProbeException("provider_identity_unavailable");
      epoch = processEpoch();
      List<MemoryMap> maps = parseMaps(readText("/proc/" + pid + "/maps", 2 * 1024 * 1024));
      for (String name : new String[] {"libGbl.so", "libAutoDice.so"}) {
        libraryBase(maps, name);
        MemoryMap root = null;
        for (MemoryMap map : maps) {
          if (map.path.endsWith("/" + name) && map.offset == 0 && LIBRARY_PATH.matcher(map.path).matches()) root = map;
        }
        if (root == null) throw new ProbeException("library_mapping_missing");
        String digest = fileHash(root.path);
        if (!(name.equals("libGbl.so") ? GBL_SHA256 : DICE_SHA256).equals(digest)) {
          throw new ProbeException("unsupported_binary_profile");
        }
        hashes.put(name, digest);
        libraryIdentities.put(name, root.path + "\n" + root.device + "\n" + root.inode + "\n" + root.start);
      }
      if (!epoch.equals(processEpoch())) throw new ProbeException("process_epoch_changed");
      memory = new RandomAccessFile("/proc/" + pid + "/mem", "r");
    }

    String processEpoch() throws Exception {
      String cmdline = readText("/proc/" + pid + "/cmdline", 256);
      if (!cmdline.replaceAll("\\x00+$", "").equals(PACKAGE)) throw new ProbeException("provider_identity_unavailable");
      String stat = readText("/proc/" + pid + "/stat", 4096).trim();
      int closing = stat.lastIndexOf(')');
      if (!stat.startsWith(pid + " (") || closing < 0 || closing + 2 >= stat.length()) {
        throw new ProbeException("provider_identity_unavailable");
      }
      String[] fields = stat.substring(closing + 2).split("\\s+");
      if (fields.length < 20 || !fields[19].matches("[0-9]+")) throw new ProbeException("provider_identity_unavailable");
      if (boot != null && !boot.equals(readText("/proc/sys/kernel/random/boot_id", 64).trim())) {
        throw new ProbeException("process_epoch_changed");
      }
      return pid + ":" + fields[19];
    }

    JSONObject observe(boolean includePosition, boolean includeNaviInfo) {
      long started = SystemClock.elapsedRealtimeNanos();
      JSONObject performance = new JSONObject();
      final long[] identityTiming = {0, 0};
      final long[] memoryTiming = {0, 0};
      JSONObject result;
      try {
        performance.put("clock_domain", "phone_elapsed_realtime");
        for (String key : new String[] {"maps_read_ns", "maps_parse_ns", "mapping_verification_ns", "identity_read_ns",
            "identity_read_calls", "memory_read_ns", "memory_read_calls", "snapshot_maps_parse_ns", "snapshot_ns", "live_total_ns"}) {
          performance.put(key, 0);
        }
        long phase = SystemClock.elapsedRealtimeNanos();
        String maps = readText("/proc/" + pid + "/maps", 2 * 1024 * 1024);
        performance.put("maps_read_ns", SystemClock.elapsedRealtimeNanos() - phase);
        phase = SystemClock.elapsedRealtimeNanos();
        List<MemoryMap> parsed = parseMaps(maps);
        performance.put("maps_parse_ns", SystemClock.elapsedRealtimeNanos() - phase);
        phase = SystemClock.elapsedRealtimeNanos();
        for (String name : new String[] {"libGbl.so", "libAutoDice.so"}) {
          libraryBase(parsed, name);
          boolean matched = false;
          for (MemoryMap map : parsed) {
            if (map.path.endsWith("/" + name) && map.offset == 0) {
              matched |= libraryIdentities.get(name).equals(map.path + "\n" + map.device + "\n" + map.inode + "\n" + map.start);
            }
          }
          if (!matched) throw new ProbeException("library_mapping_changed");
        }
        performance.put("mapping_verification_ns", SystemClock.elapsedRealtimeNanos() - phase);
        EpochSource observedEpoch = () -> {
          long before = SystemClock.elapsedRealtimeNanos();
          try { return processEpoch(); }
          finally { identityTiming[0] += SystemClock.elapsedRealtimeNanos() - before; identityTiming[1]++; }
        };
        if (!epoch.equals(observedEpoch.read())) throw new ProbeException("process_epoch_changed");
        phase = SystemClock.elapsedRealtimeNanos();
        result = probeSnapshot(maps, (address, size) -> {
          long before = SystemClock.elapsedRealtimeNanos();
          try {
            byte[] raw = new byte[size];
            memory.seek(address);
            memory.readFully(raw);
            return raw;
          } finally { memoryTiming[0] += SystemClock.elapsedRealtimeNanos() - before; memoryTiming[1]++; }
        }, hashes, observedEpoch, PROFILE, includePosition, includeNaviInfo, performance, parsed);
        performance.put("snapshot_ns", SystemClock.elapsedRealtimeNanos() - phase);
      } catch (ProbeException error) {
        result = liveUnavailable(error.getMessage(), includePosition, includeNaviInfo);
      } catch (Exception error) {
        result = liveUnavailable("live_source_unavailable", includePosition, includeNaviInfo);
      }
      try {
        performance.put("identity_read_ns", identityTiming[0]).put("identity_read_calls", identityTiming[1])
            .put("memory_read_ns", memoryTiming[0]).put("memory_read_calls", memoryTiming[1])
            .put("live_total_ns", SystemClock.elapsedRealtimeNanos() - started);
        result.put("_performance", performance);
      } catch (JSONException impossible) { throw new IllegalStateException(impossible); }
      return result;
    }

    GeometrySnapshot observeGeometry() {
      try {
        String maps = readText("/proc/" + pid + "/maps", 2 * 1024 * 1024);
        List<MemoryMap> parsed = parseMaps(maps);
        for (String name : new String[] {"libGbl.so", "libAutoDice.so"}) {
          libraryBase(parsed, name);
          boolean matched = false;
          for (MemoryMap map : parsed) {
            if (map.path.endsWith("/" + name) && map.offset == 0) {
              matched |= libraryIdentities.get(name).equals(
                  map.path + "\n" + map.device + "\n" + map.inode + "\n" + map.start);
            }
          }
          if (!matched) throw new ProbeException("library_mapping_changed");
        }
        if (!epoch.equals(processEpoch())) throw new ProbeException("process_epoch_changed");
        return probeGeometry(maps, (address, size) -> {
          byte[] raw = new byte[size];
          memory.seek(address);
          memory.readFully(raw);
          return raw;
        }, hashes, this::processEpoch, PROFILE);
      } catch (ProbeException error) {
        long now = SystemClock.elapsedRealtimeNanos();
        return new GeometrySnapshot("unavailable", error.getMessage(), new byte[0], 0, 0, now, now);
      } catch (Exception error) {
        long now = SystemClock.elapsedRealtimeNanos();
        return new GeometrySnapshot("unavailable", "live_source_unavailable", new byte[0], 0, 0, now, now);
      }
    }

    @Override public void close() throws IOException { memory.close(); }
  }

  private static JSONObject liveUnavailable(String reason, boolean includePosition, boolean includeNaviInfo) {
    JSONObject result = unavailable(reason);
    if (includePosition) {
      try { result.put("_position", positionUnavailable("snapshot_unavailable")); }
      catch (JSONException impossible) { throw new IllegalStateException(impossible); }
    }
    if (includeNaviInfo) {
      try { result.put("_navi_info", naviInfoUnavailable("snapshot_unavailable")); }
      catch (JSONException impossible) { throw new IllegalStateException(impossible); }
    }
    return result;
  }

  static String command(InputStream input) throws IOException, ProbeException {
    ByteArrayOutputStream out = new ByteArrayOutputStream();
    int value;
    while ((value = input.read()) != -1) {
      if (value == '\n') return new String(out.toByteArray(), StandardCharsets.US_ASCII);
      if (value < 32 || value > 126 || out.size() >= 32) throw new ProbeException("invalid_command");
      out.write(value);
    }
    if (out.size() != 0) throw new ProbeException("invalid_command");
    return null;
  }

  /** stdin 仅接受固定观测命令/QUIT；不接受 PID、地址、路径或任意函数。 */
  public static void main(String[] args) {
    LiveSource source = null;
    String startupFailure = null;
    try {
      if (args.length != 0) throw new ProbeException("unexpected_arguments");
      try { source = new LiveSource(); }
      catch (ProbeException error) { startupFailure = error.getMessage(); }
      catch (Exception error) { startupFailure = "live_source_unavailable"; }
      String value;
      while ((value = command(System.in)) != null) {
        if (value.equals("QUIT")) break;
        if (value.equals("OBSERVE_GEOMETRY")) {
          GeometrySnapshot geometry = source == null
              ? new GeometrySnapshot("unavailable", startupFailure, new byte[0], 0, 0, 0, 0)
              : source.observeGeometry();
          try {
            if (geometry.status.equals("observed")) {
              String digest = sha256(geometry.payload);
              System.out.print("NAVDIAG_GEOMETRY_V2 " + geometry.payload.length + " " + digest + "\n");
              System.out.write(geometry.payload);
              System.out.print("\n");
            } else {
              System.out.print("NAVDIAG_GEOMETRY_V2 ERROR " + geometry.reason + "\n");
            }
            System.out.flush();
          } catch (ProbeException error) {
            System.out.print("NAVDIAG_GEOMETRY_V2 ERROR " + error.getMessage() + "\n");
            System.out.flush();
          }
          continue;
        }
        boolean includePosition = value.equals("OBSERVE_POSITION")
            || value.equals("OBSERVE_POSITION_NAVI_INFO");
        boolean includeNaviInfo = value.equals("OBSERVE_NAVI_INFO")
            || value.equals("OBSERVE_POSITION_NAVI_INFO");
        JSONObject result = !value.equals("OBSERVE") && !includePosition && !includeNaviInfo
            ? unavailable("unsupported_command")
            : source == null ? liveUnavailable(startupFailure, includePosition, includeNaviInfo)
            : source.observe(includePosition, includeNaviInfo);
        System.out.println(result.toString());
        System.out.flush();
      }
    } catch (ProbeException error) {
      System.out.println(unavailable(error.getMessage()).toString());
    } catch (Exception error) {
      System.out.println(unavailable("probe_internal_failure").toString());
    } finally {
      if (source != null) {
        try { source.close(); } catch (IOException ignored) { }
      }
      System.out.flush();
    }
  }
}
