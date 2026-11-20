package com.jixiexiaoge.drivingassist.navdiag;

import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageInfo;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.SystemClock;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.LinkedHashMap;
import java.util.Locale;
import java.util.Map;
import java.util.TimeZone;
import java.util.UUID;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicLong;

/** 一次有界、本地、仅观测的采集。接收线程不进行 JSON 序列化和文件写入。 */
public final class CaptureSession {
  public static final class Config {
    public long durationMs = 180_000L;
    public long maxSessionBytes = 64L * 1024L * 1024L;
    public int queueCapacity = 32;
    public int maxRecordBytes = 2 * 1024 * 1024;
    public int maxDepth = 16;
    // 仅同包仪器测试设置，用于确定性复现写入线程尚未消费时的队列溢出。
    CountDownLatch testWorkerGate;

    Config copy() {
      Config out = new Config();
      out.durationMs = durationMs;
      out.maxSessionBytes = maxSessionBytes;
      out.queueCapacity = queueCapacity;
      out.maxRecordBytes = maxRecordBytes;
      out.maxDepth = maxDepth;
      out.testWorkerGate = testWorkerGate;
      return out;
    }

    void validate() {
      if (durationMs < 1 || maxSessionBytes < 65_536L || queueCapacity < 1 || queueCapacity > 1024
          || maxRecordBytes < 2048 || maxRecordBytes > maxSessionBytes || maxDepth < 0 || maxDepth > 64) {
        throw new IllegalArgumentException("采集限额无效");
      }
    }
  }

  private static final class Item {
    final Intent intent;
    final String mark;
    final long sequence;
    final long elapsedNs;
    final long wallMs;
    boolean accounted;

    Item(Intent intent, String mark, long sequence, long elapsedNs, long wallMs) {
      this.intent = intent;
      this.mark = mark;
      this.sequence = sequence;
      this.elapsedNs = elapsedNs;
      this.wallMs = wallMs;
    }
  }

  private final Config config;
  private final String sessionId;
  private final File directory;
  private final ArrayBlockingQueue<Item> queue;
  private final CountDownLatch finished = new CountDownLatch(1);
  private final Object intakeLock = new Object();
  private final Object dropsLock = new Object();
  private final Map<String, Long> dropReasons = new LinkedHashMap<>();
  private final Map<String, Long> reportedDrops = new LinkedHashMap<>();
  private final JSONObject metadata;
  private final long startedWallMs = System.currentTimeMillis();
  private final long startedElapsedNs = SystemClock.elapsedRealtimeNanos();
  private final AtomicLong received = new AtomicLong();
  private final AtomicLong written = new AtomicLong();
  private final AtomicLong incomplete = new AtomicLong();
  private final AtomicLong truncated = new AtomicLong();
  private final AtomicLong dropped = new AtomicLong();
  private final AtomicLong decodeErrors = new AtomicLong();
  private final AtomicLong eventDropped = new AtomicLong();
  private final AtomicLong bytesWritten = new AtomicLong();
  private volatile boolean accepting = true;
  private volatile String status = "collecting";
  private volatile String stopReason = "";
  private volatile String lastError = "";
  private volatile long endedWallMs;
  private volatile long endedElapsedNs;
  private final FileOutputStream broadcastOutput;
  private final FileOutputStream eventOutput;
  private final Thread writer;

  public CaptureSession(Context context) throws IOException {
    this(context, new File(context.getFilesDir(), "navdiag-captures"), new Config());
  }

  public CaptureSession(Context context, File rootDirectory, Config requested) throws IOException {
    config = requested.copy();
    config.validate();
    SimpleDateFormat format = new SimpleDateFormat("yyyyMMdd'T'HHmmssSSS'Z'", Locale.US);
    format.setTimeZone(TimeZone.getTimeZone("UTC"));
    sessionId = format.format(new Date(startedWallMs)) + "-" + UUID.randomUUID().toString().substring(0, 8);
    directory = new File(rootDirectory, sessionId);
    if (!directory.mkdirs()) throw new IOException("无法创建采集目录");
    queue = new ArrayBlockingQueue<>(config.queueCapacity);
    metadata = deviceMetadata(context);
    FileOutputStream first = new FileOutputStream(new File(directory, "broadcasts.jsonl"));
    FileOutputStream second;
    try {
      second = new FileOutputStream(new File(directory, "events.jsonl"));
    } catch (IOException error) {
      first.close();
      throw error;
    }
    broadcastOutput = first;
    eventOutput = second;
    try {
      writeAtomic(new File(directory, "manifest.json"), manifest("collecting", false));
    } catch (IOException error) {
      first.close();
      second.close();
      throw error;
    }
    writer = new Thread(this::runWriter, "navdiag-writer");
    writer.start();
  }

  public void recordIntent(Intent intent) {
    recordIntent(intent, SystemClock.elapsedRealtimeNanos(), System.currentTimeMillis());
  }

  public void recordIntent(Intent intent, long receivedElapsedNs, long receivedWallMs) {
    synchronized (intakeLock) {
      if (!accepting) return;
      if (elapsedMs() >= config.durationMs) {
        requestStop("duration_limit");
        return;
      }
      long sequence = received.incrementAndGet();
      try {
        if (intent == null) {
          decodeErrors.incrementAndGet();
          drop("null_intent");
          return;
        }
        // 此副本只供后台编码器读取，正常解析链不会持有它；不在这里读取 extras。
        Intent copy = new Intent(intent);
        if (!queue.offer(new Item(copy, null, sequence, receivedElapsedNs, receivedWallMs))) drop("queue_full");
      } catch (RuntimeException error) {
        decodeErrors.incrementAndGet();
        drop("intent_copy_error");
      }
    }
  }

  public boolean mark(String label) {
    synchronized (intakeLock) {
      if (!accepting) return false;
      if (label == null || label.length() > 512) {
        eventDropped.incrementAndGet();
        return false;
      }
      boolean accepted = queue.offer(new Item(null, label, 0,
          SystemClock.elapsedRealtimeNanos(), System.currentTimeMillis()));
      if (!accepted) eventDropped.incrementAndGet();
      return accepted;
    }
  }

  public void stop(String reason) {
    synchronized (intakeLock) {
      requestStop(reason == null || reason.length() > 64 ? "manual_stop" : reason);
    }
  }

  private void requestStop(String reason) {
    if (!accepting) return;
    accepting = false;
    stopReason = reason;
    status = "stopping";
  }

  public boolean awaitStopped(long timeoutMs) throws InterruptedException {
    return finished.await(Math.max(0, timeoutMs), TimeUnit.MILLISECONDS);
  }

  public boolean isStopped() { return finished.getCount() == 0; }
  public boolean isAccepting() { return accepting; }
  public File getDirectory() { return directory; }

  private long elapsedMs() {
    long end = endedElapsedNs == 0 ? SystemClock.elapsedRealtimeNanos() : endedElapsedNs;
    return Math.max(0, (end - startedElapsedNs) / 1_000_000L);
  }

  public JSONObject snapshot() {
    try {
      return counts().put("sessionId", sessionId).put("directory", directory.getAbsolutePath())
          .put("status", status).put("accepting", accepting).put("stopReason", stopReason)
          .put("lastError", lastError).put("startedWallMs", startedWallMs)
          .put("startedElapsedNs", startedElapsedNs).put("elapsedMs", elapsedMs())
          .put("maxDurationMs", config.durationMs).put("maxSessionBytes", config.maxSessionBytes);
    } catch (JSONException impossible) {
      throw new IllegalStateException(impossible);
    }
  }

  private JSONObject counts() throws JSONException {
    return new JSONObject().put("received", received.get()).put("written", written.get())
        .put("incomplete", incomplete.get()).put("truncated", truncated.get()).put("dropped", dropped.get())
        .put("decodeErrors", decodeErrors.get()).put("eventDropped", eventDropped.get())
        .put("bytesWritten", bytesWritten.get());
  }

  private void drop(String reason) {
    dropped.incrementAndGet();
    synchronized (dropsLock) {
      Long previous = dropReasons.get(reason);
      dropReasons.put(reason, (previous == null ? 0L : previous) + 1L);
    }
  }

  private JSONObject event(String kind) throws JSONException {
    return new JSONObject().put("schemaVersion", 1).put("sessionId", sessionId).put("kind", kind)
        .put("wallMs", System.currentTimeMillis()).put("elapsedNs", SystemClock.elapsedRealtimeNanos());
  }

  private void emitDrops() throws JSONException, IOException {
    Map<String, Long> copy;
    synchronized (dropsLock) { copy = new LinkedHashMap<>(dropReasons); }
    for (Map.Entry<String, Long> entry : copy.entrySet()) {
      long old = reportedDrops.containsKey(entry.getKey()) ? reportedDrops.get(entry.getKey()) : 0L;
      long delta = entry.getValue() - old;
      if (delta > 0) {
        writeEvent(event("drop").put("reason", entry.getKey()).put("countDelta", delta)
            .put("totalForReason", entry.getValue()), false);
        reportedDrops.put(entry.getKey(), entry.getValue());
      }
    }
  }

  private boolean writeLine(FileOutputStream stream, JSONObject value, boolean footer) throws IOException {
    byte[] bytes = (value.toString() + "\n").getBytes(StandardCharsets.UTF_8);
    // 给停止事件、最终清单和完成文件预留空间，数据文件不能吃满整个会话限额。
    long dataLimit = config.maxSessionBytes - 16_384L;
    long limit = footer ? config.maxSessionBytes - 12_288L : dataLimit;
    if (bytesWritten.get() + bytes.length > limit) return false;
    stream.write(bytes);
    bytesWritten.addAndGet(bytes.length);
    return true;
  }

  private void writeEvent(JSONObject value, boolean footer) throws IOException {
    if (!writeLine(eventOutput, value, footer)) eventDropped.incrementAndGet();
  }

  private void process(Item item) throws JSONException, IOException {
    if (item.mark != null) {
      writeEvent(event("mark").put("label", item.mark).put("receivedWallMs", item.wallMs)
          .put("receivedElapsedNs", item.elapsedNs), false);
      return;
    }
    if ("size_limit".equals(stopReason)) {
      drop("session_size_limit");
      item.accounted = true;
      return;
    }
    CaptureCodec.Result result = CaptureCodec.encodeIntent(item.intent, config.maxDepth, config.maxRecordBytes - 1024);
    JSONObject record = result.json;
    record.put("schemaVersion", 1).put("sessionId", sessionId).put("sequence", item.sequence)
        .put("receivedWallMs", item.wallMs).put("receivedElapsedNs", item.elapsedNs)
        .put("sender", new JSONObject().put("verified", false).put("package", JSONObject.NULL).put("uid", JSONObject.NULL));
    if (record.toString().getBytes(StandardCharsets.UTF_8).length > config.maxRecordBytes) {
      drop("record_envelope_limit");
      item.accounted = true;
      return;
    }
    if (!writeLine(broadcastOutput, record, false)) {
      drop("session_size_limit");
      item.accounted = true;
      stop("size_limit");
      return;
    }
    written.incrementAndGet();
    item.accounted = true;
    if (!result.complete) incomplete.incrementAndGet();
    if (result.truncated) {
      truncated.incrementAndGet();
      writeEvent(event("truncation").put("sequence", item.sequence).put("issues", record.optJSONArray("issues")), false);
    }
    if (result.decodeErrors > 0) {
      decodeErrors.addAndGet(result.decodeErrors);
      writeEvent(event("decode_error").put("sequence", item.sequence).put("count", result.decodeErrors)
          .put("issues", record.optJSONArray("issues")), false);
    }
  }

  private void runWriter() {
    boolean succeeded = false;
    Item active = null;
    try {
      if (config.testWorkerGate != null) config.testWorkerGate.await(5, TimeUnit.SECONDS);
      writeEvent(event("start"), false);
      while (true) {
        if (accepting && elapsedMs() >= config.durationMs) stop("duration_limit");
        active = queue.poll(100, TimeUnit.MILLISECONDS);
        emitDrops();
        if (active != null) process(active);
        active = null;
        if (!accepting && queue.isEmpty()) break;
      }
      emitDrops();
      endedWallMs = System.currentTimeMillis();
      endedElapsedNs = SystemClock.elapsedRealtimeNanos();
      writeEvent(event("stop").put("reason", stopReason).put("counts", counts()), true);
      broadcastOutput.getFD().sync();
      eventOutput.getFD().sync();
      broadcastOutput.close();
      eventOutput.close();
      JSONObject finalManifest = manifest("completed", captureComplete());
      JSONObject hashes = new JSONObject().put("broadcasts.jsonl", sha256(new File(directory, "broadcasts.jsonl")))
          .put("events.jsonl", sha256(new File(directory, "events.jsonl")));
      finalManifest.put("fileHashes", hashes);
      writeAtomic(new File(directory, "manifest.json"), finalManifest);
      hashes.put("manifest.json", sha256(new File(directory, "manifest.json")));
      JSONObject completed = new JSONObject().put("schemaVersion", 1).put("sessionId", sessionId)
          .put("status", "completed").put("captureComplete", captureComplete()).put("counts", counts())
          .put("fileHashes", hashes).put("endedWallMs", endedWallMs).put("endedElapsedNs", endedElapsedNs);
      writeAtomic(new File(directory, "completed.json"), completed);
      status = "completed";
      succeeded = true;
    } catch (Exception error) {
      lastError = error.getClass().getName();
      synchronized (intakeLock) { accepting = false; }
      status = "failed";
      stopReason = "writer_failure";
      if (active != null && active.intent != null && !active.accounted) drop("writer_failure");
      Item pending;
      while ((pending = queue.poll()) != null) {
        if (pending.intent != null) drop("writer_failure"); else eventDropped.incrementAndGet();
      }
      endedWallMs = System.currentTimeMillis();
      endedElapsedNs = SystemClock.elapsedRealtimeNanos();
      try { writeAtomic(new File(directory, "manifest.json"), manifest("failed", false)); }
      catch (Exception ignored) { /* 无法写盘时，缺少 completed.json 本身就是不完整证据。 */ }
    } finally {
      accepting = false;
      if (!succeeded) {
        try { broadcastOutput.close(); } catch (IOException ignored) { }
        try { eventOutput.close(); } catch (IOException ignored) { }
      }
      finished.countDown();
    }
  }

  private boolean captureComplete() {
    // 接收器未成功启动或服务被销毁，不能仅因零丢包就把接收窗口标成完整。
    return !"service_start_failed".equals(stopReason) && !"service_destroyed".equals(stopReason)
        && received.get() == written.get() + dropped.get() && dropped.get() == 0 && incomplete.get() == 0
        && truncated.get() == 0 && decodeErrors.get() == 0 && eventDropped.get() == 0;
  }

  private JSONObject manifest(String state, boolean complete) {
    JSONObject out = snapshot();
    try {
      out.put("schemaVersion", 1).put("status", state).put("captureComplete", complete)
          .put("endedWallMs", endedWallMs).put("endedElapsedNs", endedElapsedNs).put("metadata", metadata)
          .put("scope", new JSONObject().put("receiverScope", "registered_actions")
              .put("targetedBroadcastCoverage", "unknown").put("senderIdentity", "unverified"))
          .put("limits", new JSONObject().put("durationMs", config.durationMs)
              .put("maxSessionBytes", config.maxSessionBytes).put("queueCapacity", config.queueCapacity)
              .put("maxRecordBytes", config.maxRecordBytes).put("maxDepth", config.maxDepth));
      synchronized (dropsLock) { out.put("dropReasons", new JSONObject(dropReasons)); }
      return out;
    } catch (JSONException impossible) {
      throw new IllegalStateException(impossible);
    }
  }

  private static JSONObject deviceMetadata(Context context) {
    JSONObject out = new JSONObject();
    try {
      out.put("manufacturer", Build.MANUFACTURER).put("model", Build.MODEL)
          .put("androidRelease", Build.VERSION.RELEASE).put("androidApi", Build.VERSION.SDK_INT)
          .put("collectorPackage", context.getPackageName());
      JSONArray packages = new JSONArray();
      String[] names = {context.getPackageName(), "com.jixiexiaoge.drivingassist", "com.jixiexiaoge.drivingassist.debug",
          "com.autonavi.amapauto", "com.autonavi.minimap"};
      for (String name : names) {
        JSONObject entry = new JSONObject().put("package", name);
        try {
          PackageInfo info = context.getPackageManager().getPackageInfo(name, 0);
          entry.put("installed", true).put("versionName", info.versionName)
              .put("versionCode", Long.toString(info.getLongVersionCode()));
        } catch (PackageManager.NameNotFoundException error) {
          entry.put("installed", false).put("reason", "not_visible_or_not_installed");
        } catch (RuntimeException error) {
          entry.put("installed", JSONObject.NULL).put("reason", error.getClass().getName());
        }
        packages.put(entry);
      }
      out.put("packages", packages);
    } catch (JSONException impossible) {
      throw new IllegalStateException(impossible);
    }
    return out;
  }

  private static void writeAtomic(File destination, JSONObject value) throws IOException {
    File temporary = new File(destination.getParentFile(), destination.getName() + ".tmp");
    try (FileOutputStream stream = new FileOutputStream(temporary)) {
      stream.write((value.toString() + "\n").getBytes(StandardCharsets.UTF_8));
      stream.getFD().sync();
    }
    if (!temporary.renameTo(destination)) throw new IOException("无法完成清单原子写入");
  }

  private static String sha256(File file) throws Exception {
    MessageDigest digest = MessageDigest.getInstance("SHA-256");
    try (FileInputStream input = new FileInputStream(file)) {
      byte[] buffer = new byte[8192];
      int count;
      while ((count = input.read(buffer)) >= 0) if (count > 0) digest.update(buffer, 0, count);
    }
    StringBuilder out = new StringBuilder(64);
    for (byte value : digest.digest()) out.append(String.format(Locale.US, "%02x", value & 255));
    return out.toString();
  }
}
