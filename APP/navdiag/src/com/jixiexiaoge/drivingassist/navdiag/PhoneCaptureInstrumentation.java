package com.jixiexiaoge.drivingassist.navdiag;

import android.app.Activity;
import android.app.Instrumentation;
import android.os.Bundle;
import org.json.JSONObject;
import org.json.JSONArray;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.util.UUID;
import java.util.HashSet;
import java.util.function.LongSupplier;
import java.util.zip.ZipInputStream;
import java.util.zip.ZipEntry;

/** 仅用合成数据验证手机归档；不申请Root、不读取高德、不启动定位或采集服务。 */
public final class PhoneCaptureInstrumentation extends Instrumentation {
  interface Check { void run() throws Exception; }
  private int passed, failed;
  private File root;
  @Override public void onCreate(Bundle args) { super.onCreate(args); start(); }
  @Override public void onStart() {
    root = new File(getTargetContext().getFilesDir(), "phone-capture-selftest-" + UUID.randomUUID());
    root.mkdirs();
    check("archive_completed_files_verified", () -> { PhoneCaptureStore store = archive();
      require(PhoneCaptureStore.verify(store.directory).length() == 4, "文件清单错误"); });
    check("archive_tamper_rejected", () -> { PhoneCaptureStore store = archive();
      try (FileOutputStream out = new FileOutputStream(new File(store.directory, "frames.jsonl"), true)) { out.write(7); }
      reject(() -> PhoneCaptureStore.verify(store.directory)); });
    check("unfinished_archive_not_exportable", () -> {
      File directory = new File(root, "unfinished"); directory.mkdir();
      reject(() -> PhoneCaptureStore.exportZip(directory, new ByteArrayOutputStream())); });
    check("zip_contains_completed_and_raw_records", () -> {
      PhoneCaptureStore store = archive(); ByteArrayOutputStream output = new ByteArrayOutputStream();
      PhoneCaptureStore.exportZip(store.directory, output); int count = 0;
      try (ZipInputStream zip = new ZipInputStream(new ByteArrayInputStream(output.toByteArray()))) {
        while (zip.getNextEntry() != null) count++;
      }
      require(count == 5, "ZIP不完整"); });
    check("traversal_and_absolute_paths_rejected", () -> {
      for (String value : new String[] {"../x", "/tmp/x", "geometry/../../x", "a/b", ".."})
        reject(() -> PhoneCaptureStore.relative(root, value)); });
    check("geometry_is_content_addressed_and_deduplicated", () -> {
      PhoneCaptureStore store = new PhoneCaptureStore(root, new JSONObject());
      byte[] data = new byte[] {1, 2, 3}; String first = store.geometry(data);
      require(first.equals(store.geometry(data)), "同一几何未去重");
      store.finish(new JSONObject());
      require(PhoneCaptureStore.verify(store.directory).length() == 5, "重复几何写盘"); });
    check("oversize_geometry_rejected", () -> {
      PhoneCaptureStore store = new PhoneCaptureStore(root, new JSONObject());
      try { reject(() -> store.geometry(new byte[0])); } finally { store.finish(new JSONObject()); } });
    check("closed_archive_is_immutable", () -> {
      PhoneCaptureStore store = archive(); reject(() -> store.frame(new JSONObject())); });
    check("finish_is_idempotent", () -> {
      PhoneCaptureStore store = archive(); String hash = PhoneCaptureStore.hash(new File(store.directory, "completed.json"));
      store.finish(new JSONObject());
      require(hash.equals(PhoneCaptureStore.hash(new File(store.directory, "completed.json"))), "重复完成改写记录"); });
    check("missing_selection_never_matches", () -> require(
        !PhoneCaptureService.sameSelection(new JSONObject(), new JSONObject()), "缺失身份误接受"));
    check("changed_selection_and_epoch_rejected", () -> {
      JSONObject first = observation("a", "b");
      require(PhoneCaptureService.sameSelection(first, observation("a", "b")), "相同身份拒绝");
      require(!PhoneCaptureService.sameSelection(first, observation("c", "b")), "跨选路误接受");
      require(!PhoneCaptureService.sameSelection(first, observation("a", "c")), "跨进程误接受"); });
    check("manifest_does_not_claim_gpu_or_control", () -> {
      PhoneCaptureStore store = archive(); JSONObject manifest = PhoneCaptureStore.readJson(
          new File(store.directory, "manifest.json"), 16384);
      require(!manifest.getBoolean("gpu_executed") && !manifest.getBoolean("control_input_allowed")
          && !manifest.getBoolean("source_clock_translated"), "错误提升验收级别"); });
    check("manual_lifetime_and_budgets_are_bounded", () -> require(
        PhoneCaptureService.MAX_DURATION_MS == 180000L && PhoneCaptureProbe.MAX_LIFETIME_MS == 185000L
        && PhoneCaptureStore.MAX_BYTES == 268435456L, "限额变动"));
    check("latest_completed_ignores_incomplete_session", () -> {
      PhoneCaptureStore store = archive();
      new File(root, "zzzz-incomplete").mkdir();
      require(PhoneCaptureStore.latestCompleted(root).equals(store.directory), "误选未完成会话"); });
    check("long_capture_and_root_remain_bounded", () -> {
      require(PhoneCaptureService.class.getDeclaredField("LONG_DURATION_MS").getLong(null) == 7200000L,
          "整趟时长错误");
      require(PhoneCaptureProbe.class.getDeclaredField("LONG_LIFETIME_MS").getLong(null) == 7205000L,
          "Root硬寿命错误"); });
    check("segment_rotation_keeps_continuous_raw_records", () -> {
      PhoneCaptureStore store = segmented();
      store.frame(new JSONObject().put("sequence", 1));
      rotate(store, 180000000000L);
      require(!new File(store.directory, "frames-0002.jsonl").exists(), "提前分段");
      rotate(store, 180000000001L);
      store.frame(new JSONObject().put("sequence", 2));
      store.finish(new JSONObject());
      JSONArray parts = PhoneCaptureStore.readJson(new File(store.directory, "segments.json"), 32768)
          .getJSONArray("segments");
      require(parts.length() == 2 && parts.getJSONObject(0).getLong("frames") == 1
          && parts.getJSONObject(1).getLong("frames") == 1, "分段丢帧");
      require(PhoneCaptureStore.readJson(new File(store.directory, "summary.json"), 16384)
          .getInt("segments") == 2, "摘要分段丢失");
      require(PhoneCaptureStore.verify(store.directory).length() == 7, "分段文件不完整"); });
    check("segment_zip_contains_whole_trip", () -> {
      PhoneCaptureStore store = segmented(); rotate(store, 180000000001L);
      store.finish(new JSONObject()); ByteArrayOutputStream output = new ByteArrayOutputStream();
      PhoneCaptureStore.exportZip(store.directory, output);
      HashSet<String> names = new HashSet<>();
      try (ZipInputStream zip = new ZipInputStream(new ByteArrayInputStream(output.toByteArray()))) {
        ZipEntry entry; while ((entry = zip.getNextEntry()) != null)
          names.add(entry.getName().substring(store.session.length() + 1));
      }
      require(names.size() == 8 && names.contains("segments.json") && names.contains("frames-0002.jsonl")
          && names.contains("broadcasts-0002.jsonl"), "ZIP只包含最后一段"); });
    check("geometry_deduplicates_across_segments", () -> {
      PhoneCaptureStore store = segmented(); String name = store.geometry(new byte[] {7, 8});
      rotate(store, 180000000001L);
      require(name.equals(store.geometry(new byte[] {7, 8})), "跨段几何身份变化");
      store.finish(new JSONObject());
      require(PhoneCaptureStore.verify(store.directory).length() == 8, "跨段重复保存几何"); });
    check("segment_count_limit_preserves_finalization", () -> {
      PhoneCaptureStore store = segmented();
      for (int i = 1; i < 40; i++) rotate(store, 1L + i * 180000000000L);
      reject(() -> rotate(store, 1L + 40 * 180000000000L));
      store.finish(new JSONObject());
      require(PhoneCaptureStore.readJson(new File(store.directory, "segments.json"), 32768)
          .getJSONArray("segments").length() == 40, "分段上限错误");
      PhoneCaptureStore.verify(store.directory); });
    check("segment_clock_reversal_is_rejected", () -> {
      PhoneCaptureStore store = segmented();
      reject(() -> rotate(store, 0)); store.finish(new JSONObject()); });
    remove(root);
    Bundle result = new Bundle(); result.putInt("tests_passed", passed); result.putInt("tests_failed", failed);
    if (failed != 0) result.putString("error", "phone_capture_selftest_failed");
    finish(failed == 0 ? Activity.RESULT_OK : Activity.RESULT_CANCELED, result);
  }
  private PhoneCaptureStore archive() throws Exception {
    PhoneCaptureStore store = new PhoneCaptureStore(root, new JSONObject().put("synthetic", true));
    store.frame(new JSONObject().put("synthetic", true));
    store.finish(new JSONObject().put("synthetic", true)); return store;
  }
  private PhoneCaptureStore segmented() throws Exception {
    JSONObject metadata = new JSONObject().put("synthetic", true)
        .put("started_elapsed_realtime_ns", 1L).put("max_duration_ms", 7200000L)
        .put("segment_interval_ms", 180000L);
    LongSupplier clock = () -> 7200000000002L;
    return PhoneCaptureStore.class.getDeclaredConstructor(File.class, JSONObject.class, LongSupplier.class)
        .newInstance(root, metadata, clock);
  }
  private static void rotate(PhoneCaptureStore store, long nowNs) throws Exception {
    try { PhoneCaptureStore.class.getDeclaredMethod("rotateIfDue", long.class).invoke(store, nowNs); }
    catch (java.lang.reflect.InvocationTargetException error) {
      Throwable cause = error.getCause();
      if (cause instanceof Exception) throw (Exception) cause;
      throw error;
    }
  }
  private static JSONObject observation(String path, String epoch) throws Exception {
    return new JSONObject().put("status", "observed").put("sample_consistent", true)
        .put("path_identity_hash", repeat(path)).put("process_epoch_hash", repeat(epoch));
  }
  private static String repeat(String text) { StringBuilder out = new StringBuilder();
    for (int i = 0; i < 64; i++) out.append(text); return out.toString(); }
  private static void require(boolean value, String message) { if (!value) throw new AssertionError(message); }
  private static void reject(Check check) throws Exception {
    try { check.run(); } catch (IOException expected) { return; } throw new AssertionError("应拒绝却未拒绝");
  }
  private void check(String name, Check check) {
    Bundle status = new Bundle(); status.putString("test", name);
    try { check.run(); passed++; status.putString("result", "passed"); }
    catch (Throwable error) { failed++; status.putString("result", "failed");
      status.putString("error", error.getClass().getSimpleName() + ":" + error.getMessage()); }
    sendStatus(0, status);
  }
  private static void remove(File file) {
    File[] children = file.listFiles(); if (children != null) for (File child : children) remove(child);
    file.delete();
  }
}
