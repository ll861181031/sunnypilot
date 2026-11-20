package com.jixiexiaoge.drivingassist.navdiag;

import android.os.SystemClock;
import org.json.JSONArray;
import org.json.JSONObject;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.*;
import java.util.function.LongSupplier;
import java.util.zip.ZipEntry;
import java.util.zip.ZipOutputStream;

/** 手机独立采集的私有归档；完成标记、限额和导出都不授予导航/控制有效性。 */
final class PhoneCaptureStore {
  static final long MAX_BYTES = 256L * 1024 * 1024;
  static final long MAX_RETAINED_BYTES = 1024L * 1024 * 1024;
  static final int MAX_FILES = 1024;
  static final long SEGMENT_INTERVAL_MS = 180000L;
  static final int MAX_SEGMENTS = 40;
  final File directory;
  final String session;
  private final LinkedHashMap<String, File> files = new LinkedHashMap<>();
  private FileOutputStream frames, broadcasts;
  private final boolean segmented;
  private final LongSupplier clock;
  private final JSONArray segments = new JSONArray();
  private JSONObject currentSegment;
  private long segmentOpenedNs;
  private long segmentFrames, segmentBroadcasts;
  private long written;
  private boolean closed;
  private boolean finishing;
  private final long retainedBytes;

  PhoneCaptureStore(File parent, JSONObject metadata) throws Exception {
    this(parent, metadata, SystemClock::elapsedRealtimeNanos);
  }

  PhoneCaptureStore(File parent, JSONObject metadata, LongSupplier clock) throws Exception {
    this.clock = clock;
    long interval = metadata.optLong("segment_interval_ms", 0);
    if (interval != 0 && interval != SEGMENT_INTERVAL_MS) throw new IOException("segment_interval_invalid");
    segmented = interval != 0;
    segmentOpenedNs = metadata.optLong("started_elapsed_realtime_ns", 0);
    if (segmented && segmentOpenedNs <= 0) throw new IOException("segment_clock_invalid");
    parent = parent.getCanonicalFile();
    retainedBytes = treeSize(parent);
    if (retainedBytes >= MAX_RETAINED_BYTES) throw new IOException("retained_storage_limit");
    if (!parent.isDirectory() && !parent.mkdirs()) throw new IOException("capture_parent_unavailable");
    session = String.format(Locale.US, "%013d-%s", System.currentTimeMillis(),
        UUID.randomUUID().toString().replace("-", ""));
    directory = new File(parent, session);
    if (!directory.mkdir()) throw new IOException("session_directory_unavailable");
    metadata.put("schema_version", segmented ? 2 : 1).put("session_id", session)
        .put("mode", "phone_only_raw_navigation_evidence")
        .put("clock_domain", "phone_elapsed_realtime")
        .put("source_clock_translated", false).put("host_pipeline_executed", false)
        .put("gpu_executed", false).put("control_input_allowed", false);
    write("manifest.json", json(metadata));
    openSegment();
  }

  static long treeSize(File file) throws IOException {
    return treeSize(file, 0, new int[] {0});
  }

  private static long treeSize(File file, int depth, int[] count) throws IOException {
    if (!file.exists()) return 0;
    if (depth > 5 || ++count[0] > 12000) throw new IOException("retained_file_limit");
    if (!file.getCanonicalFile().equals(file.getAbsoluteFile())) throw new IOException("archive_link_rejected");
    if (file.isFile()) return file.length();
    File[] children = file.listFiles();
    if (children == null) throw new IOException("archive_listing_failed");
    long size = 0;
    for (File child : children) size += treeSize(child, depth + 1, count);
    return size;
  }

  static byte[] json(JSONObject value) { return (value.toString() + "\n").getBytes(StandardCharsets.UTF_8); }

  static String sha(byte[] bytes) throws Exception {
    return hex(MessageDigest.getInstance("SHA-256").digest(bytes));
  }

  static String hex(byte[] bytes) {
    StringBuilder value = new StringBuilder();
    for (byte b : bytes) value.append(String.format(Locale.US, "%02x", b & 255));
    return value.toString();
  }

  static String hash(File file) throws Exception {
    MessageDigest digest = MessageDigest.getInstance("SHA-256");
    try (InputStream input = new FileInputStream(file)) {
      byte[] buffer = new byte[16384]; int count;
      while ((count = input.read(buffer)) != -1) digest.update(buffer, 0, count);
    }
    return hex(digest.digest());
  }

  static File relative(File base, String name) throws IOException {
    if (!name.matches("(?:geometry/)?[a-zA-Z0-9_.-]{1,100}") || name.contains(".."))
      throw new IOException("archive_path_invalid");
    File file = new File(base, name);
    if (!file.getCanonicalPath().equals(base.getCanonicalPath() + "/" + name))
      throw new IOException("archive_link_rejected");
    return file;
  }

  private File register(String name) throws IOException {
    if (files.containsKey(name)) throw new IOException("duplicate_archive_file");
    if (files.size() >= MAX_FILES - (finishing ? 0 : 3)) throw new IOException("archive_file_limit");
    File file = relative(directory, name);
    if (!file.getParentFile().isDirectory() && !file.getParentFile().mkdir())
      throw new IOException("archive_directory_failed");
    if (!file.createNewFile()) throw new IOException("archive_file_exists");
    files.put(name, file);
    return file;
  }

  private void reserve(long size) throws IOException {
    if (closed || size < 0 || written + size > MAX_BYTES - (finishing ? 0 : 512 * 1024))
      throw new IOException("archive_size_limit");
    if (retainedBytes + written + size > MAX_RETAINED_BYTES) throw new IOException("retained_storage_limit");
    if (directory.getUsableSpace() < size + 16L * 1024 * 1024) throw new IOException("storage_low");
    written += size;
  }

  private void write(String name, byte[] bytes) throws Exception {
    reserve(bytes.length);
    try (FileOutputStream out = new FileOutputStream(register(name))) {
      out.write(bytes); out.getFD().sync();
    }
  }

  synchronized void frame(JSONObject value) throws Exception {
    byte[] data = json(value); reserve(data.length); frames.write(data); frames.flush();
    segmentFrames++;
  }

  synchronized void broadcast(JSONObject value) throws Exception {
    byte[] data = json(value); reserve(data.length); broadcasts.write(data); broadcasts.flush();
    segmentBroadcasts++;
  }

  private void openSegment() throws Exception {
    int index = segments.length() + 1;
    String suffix = index == 1 ? "" : String.format(Locale.US, "-%04d", index);
    String frameName = "frames" + suffix + ".jsonl", broadcastName = "broadcasts" + suffix + ".jsonl";
    frames = new FileOutputStream(register(frameName));
    try { broadcasts = new FileOutputStream(register(broadcastName)); }
    catch (Exception error) { frames.close(); throw error; }
    segmentFrames = 0; segmentBroadcasts = 0;
    currentSegment = new JSONObject().put("index", index).put("frames_file", frameName)
        .put("broadcasts_file", broadcastName).put("opened_elapsed_realtime_ns", segmentOpenedNs);
  }

  private void closeSegment(long nowNs) throws Exception {
    if (nowNs < segmentOpenedNs) throw new IOException("segment_clock_invalid");
    frames.getFD().sync(); broadcasts.getFD().sync();
    frames.close(); broadcasts.close();
    currentSegment.put("closed_elapsed_realtime_ns", nowNs).put("frames", segmentFrames)
        .put("broadcasts", segmentBroadcasts);
    segments.put(currentSegment);
  }

  synchronized void rotateIfDue(long nowNs) throws Exception {
    if (closed) throw new IOException("archive_closed");
    if (!segmented) return;
    if (nowNs < segmentOpenedNs) throw new IOException("segment_clock_invalid");
    if (nowNs - segmentOpenedNs < SEGMENT_INTERVAL_MS * 1000000L) return;
    if (segments.length() + 1 >= MAX_SEGMENTS) throw new IOException("archive_segment_limit");
    if (files.size() + 2 > MAX_FILES - 3) throw new IOException("archive_file_limit");
    reserve(0);
    closeSegment(nowNs); segmentOpenedNs = nowNs;
    openSegment();
  }

  synchronized int segmentCount() { return segments.length() + (closed ? 0 : 1); }

  synchronized String geometry(byte[] data) throws Exception {
    if (data.length <= 0 || data.length > NativeRouteProbe.GEOMETRY_MAX_PAYLOAD_BYTES)
      throw new IOException("geometry_size_invalid");
    String name = "geometry/" + sha(data) + ".bin";
    if (!files.containsKey(name)) write(name, data);
    return name;
  }

  synchronized void finish(JSONObject summary) throws Exception {
    if (closed) return;
    finishing = true;
    try {
      long finishedNs = clock.getAsLong();
      closeSegment(finishedNs);
      if (segmented) {
        write("segments.json", json(new JSONObject().put("schema_version", 1).put("partition", "writer_rotation")
            .put("interval_ms", SEGMENT_INTERVAL_MS).put("segments", segments)));
        summary.put("segments", segments.length());
      }
      summary.put("session_id", session).put("finished_elapsed_realtime_ns", finishedNs)
          .put("archive_complete", true).put("control_input_allowed", false)
          .put("host_pipeline_executed", false).put("gpu_executed", false);
      write("summary.json", json(summary));
      JSONArray entries = new JSONArray();
      for (Map.Entry<String, File> entry : files.entrySet()) {
        entries.put(new JSONObject().put("path", entry.getKey()).put("bytes", entry.getValue().length())
            .put("sha256", hash(entry.getValue())));
      }
      write("completed.json", json(new JSONObject().put("schema_version", segmented ? 2 : 1).put("session_id", session)
          .put("archive_complete", true).put("files", entries)));
    } finally {
      closed = true; frames.close(); broadcasts.close();
    }
  }

  static JSONObject readJson(File file, int limit) throws Exception {
    if (!file.isFile() || file.length() > limit) throw new IOException("json_size_invalid");
    ByteArrayOutputStream out = new ByteArrayOutputStream();
    try (InputStream input = new FileInputStream(file)) {
      byte[] buffer = new byte[8192]; int n;
      while ((n = input.read(buffer)) != -1) {
        if (out.size() + n > limit) throw new IOException("json_size_invalid");
        out.write(buffer, 0, n);
      }
    }
    return new JSONObject(new String(out.toByteArray(), StandardCharsets.UTF_8));
  }

  static File latestCompleted(File parent) {
    File[] dirs = parent.listFiles();
    if (dirs == null) return null;
    Arrays.sort(dirs, Comparator.comparing(File::getName).reversed());
    for (File dir : dirs) if (dir.isDirectory() && new File(dir, "completed.json").isFile()) {
      try { return dir.getCanonicalFile(); } catch (IOException ignored) { }
    }
    return null;
  }

  static JSONArray verify(File directory) throws Exception {
    JSONObject completed = readJson(relative(directory, "completed.json"), 256 * 1024);
    if (!completed.getBoolean("archive_complete")
        || !directory.getName().equals(completed.getString("session_id"))) throw new IOException("archive_not_complete");
    JSONArray entries = completed.getJSONArray("files");
    if (entries.length() < 4 || entries.length() > MAX_FILES) throw new IOException("archive_file_limit");
    HashSet<String> names = new HashSet<>(); long total = 0;
    for (int i = 0; i < entries.length(); i++) {
      JSONObject entry = entries.getJSONObject(i); String name = entry.getString("path");
      File file = relative(directory, name);
      total += file.length();
      if (total > MAX_BYTES) throw new IOException("archive_size_limit");
      if (!names.add(name) || name.equals("completed.json") || !file.isFile()
          || entry.getLong("bytes") != file.length() || !hash(file).equals(entry.getString("sha256")))
        throw new IOException("archive_hash_mismatch");
    }
    if (!names.containsAll(Arrays.asList("manifest.json", "summary.json", "frames.jsonl", "broadcasts.jsonl")))
      throw new IOException("archive_required_file_missing");
    int schema = completed.optInt("schema_version");
    if (schema != 1 && schema != 2) throw new IOException("archive_schema_invalid");
    JSONObject manifest = readJson(relative(directory, "manifest.json"), 16384);
    if (manifest.optInt("schema_version") != schema) throw new IOException("archive_schema_invalid");
    if (schema == 2) {
      if (!names.contains("segments.json")) throw new IOException("segment_index_missing");
      JSONObject index = readJson(relative(directory, "segments.json"), 32768);
      JSONArray parts = index.getJSONArray("segments");
      JSONObject summary = readJson(relative(directory, "summary.json"), 16384);
      if (index.optInt("schema_version") != 1 || !index.optString("partition").equals("writer_rotation")
          || index.optLong("interval_ms") != SEGMENT_INTERVAL_MS
          || manifest.optLong("segment_interval_ms") != SEGMENT_INTERVAL_MS
          || manifest.optLong("max_duration_ms") != 7200000L || parts.length() < 1
          || parts.length() > MAX_SEGMENTS || summary.optInt("segments") != parts.length())
        throw new IOException("segment_contract_invalid");
      HashSet<String> streams = new HashSet<>(); long opened = manifest.getLong("started_elapsed_realtime_ns");
      for (int i = 0; i < parts.length(); i++) {
        JSONObject part = parts.getJSONObject(i);
        String suffix = i == 0 ? "" : String.format(Locale.US, "-%04d", i + 1);
        String frameName = "frames" + suffix + ".jsonl", broadcastName = "broadcasts" + suffix + ".jsonl";
        long end = part.getLong("closed_elapsed_realtime_ns");
        if (part.getInt("index") != i + 1 || !part.getString("frames_file").equals(frameName)
            || !part.getString("broadcasts_file").equals(broadcastName) || !names.contains(frameName)
            || !names.contains(broadcastName) || opened <= 0 || end < opened
            || part.getLong("opened_elapsed_realtime_ns") != opened)
          throw new IOException("segment_order_or_file_invalid");
        streams.add(frameName); streams.add(broadcastName); opened = end;
      }
      if (opened != summary.getLong("finished_elapsed_realtime_ns")) throw new IOException("segment_end_clock_invalid");
      for (String name : names) if (name.endsWith(".jsonl") && !streams.contains(name))
        throw new IOException("segment_unindexed_stream");
    }
    return entries;
  }

  static void exportZip(File directory, OutputStream output) throws Exception {
    JSONArray entries = verify(directory);
    try (ZipOutputStream zip = new ZipOutputStream(output)) {
      byte[] buffer = new byte[16384];
      for (int i = -1; i < entries.length(); i++) {
        String name = i < 0 ? "completed.json" : entries.getJSONObject(i).getString("path");
        zip.putNextEntry(new ZipEntry(directory.getName() + "/" + name));
        try (InputStream input = new FileInputStream(relative(directory, name))) {
          int n; while ((n = input.read(buffer)) != -1) zip.write(buffer, 0, n);
        }
        zip.closeEntry();
      }
    }
  }
}
