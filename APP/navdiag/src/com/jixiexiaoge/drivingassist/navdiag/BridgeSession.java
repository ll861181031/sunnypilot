package com.jixiexiaoge.drivingassist.navdiag;

import android.content.Intent;
import android.os.Bundle;
import android.os.SystemClock;
import android.system.ErrnoException;
import android.system.Os;

import org.json.JSONException;
import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.util.UUID;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicLong;

/** 有界广播观测桥：回调只复制入队，JSON 和原子写盘只在专用线程执行。 */
public final class BridgeSession {
  public static final long MAX_DURATION_MS = 2L * 60L * 60L * 1000L;
  public static final int MAX_FILE_BYTES = 2 * 1024 * 1024;

  interface Listener {
    void onInitialized();
    void onStopped();
  }

  interface Clock {
    long elapsedNs();
    long wallMs();
  }

  public static final class Config {
    public long durationMs = MAX_DURATION_MS;
    public int queueCapacity = 8;
    public int maxRecordBytes = 768 * 1024;
    public long publishIntervalMs = 500L;
    // 仅供同包仪器测试确定性地观察队列溢出和停止。
    CountDownLatch testWorkerGate;
    Clock clock = new Clock() {
      @Override public long elapsedNs() { return SystemClock.elapsedRealtimeNanos(); }
      @Override public long wallMs() { return System.currentTimeMillis(); }
    };

    Config copy() {
      Config result = new Config();
      result.durationMs = durationMs;
      result.queueCapacity = queueCapacity;
      result.maxRecordBytes = maxRecordBytes;
      result.publishIntervalMs = publishIntervalMs;
      result.testWorkerGate = testWorkerGate;
      result.clock = clock;
      return result;
    }

    void validate() {
      if (durationMs < 1 || durationMs > MAX_DURATION_MS || queueCapacity < 1 || queueCapacity > 32
          || maxRecordBytes < 2048 || maxRecordBytes > 768 * 1024
          || publishIntervalMs < 20 || publishIntervalMs > 1000 || clock == null) {
        throw new IllegalArgumentException("观测桥限额无效");
      }
    }
  }

  private static final class Item {
    final Intent intent;
    final long sequence;
    final long elapsedNs;
    final long wallMs;

    Item(Intent intent, long sequence, long elapsedNs, long wallMs) {
      this.intent = intent;
      this.sequence = sequence;
      this.elapsedNs = elapsedNs;
      this.wallMs = wallMs;
    }
  }

  private final Config config;
  private final File directory;
  private final File file;
  private final Listener listener;
  private final ArrayBlockingQueue<Item> queue;
  private final Object intakeLock = new Object();
  private final CountDownLatch initialized = new CountDownLatch(1);
  private final CountDownLatch finished = new CountDownLatch(1);
  private final AtomicLong received = new AtomicLong();
  private final AtomicLong ignored = new AtomicLong();
  private final AtomicLong written = new AtomicLong();
  private final String sessionId = UUID.randomUUID().toString();
  private final long startedElapsedNs;
  private final long startedWallMs;
  private volatile boolean accepting = true;
  private volatile boolean registered;
  private volatile String status = "starting";
  private volatile String reason = "starting";
  private volatile String lastError = "";
  private volatile String latestStatus = "{}";
  private JSONObject navigationRecord;
  private JSONObject lampRecord;
  private boolean changed = true;

  public BridgeSession(File directory, Config requested, Listener listener) {
    config = requested.copy();
    config.validate();
    this.directory = directory;
    this.file = new File(directory, "latest.json");
    this.listener = listener;
    queue = new ArrayBlockingQueue<>(config.queueCapacity);
    startedElapsedNs = config.clock.elapsedNs();
    startedWallMs = config.clock.wallMs();
    new Thread(this::runWriter, "navdiag-bridge-writer").start();
  }

  /** 仅成功注册 receiver 后调用；此前文件保持无效。 */
  public void receiverRegistered() {
    synchronized (intakeLock) {
      if (!accepting) return;
      registered = true;
      status = "receiving";
      reason = "receiver_registered";
    }
  }

  public void registrationFailed() { stop("receiver_registration_failed"); }

  public void recordIntent(Intent intent, long elapsedNs, long wallMs) {
    synchronized (intakeLock) {
      if (!accepting || !registered) return;
      long sequence = received.incrementAndGet();
      if (expired()) {
        stopLocked("duration_limit");
        return;
      }
      if (intent == null) {
        stopLocked("null_intent");
        return;
      }
      try {
        // 不在广播回调读取 extras，也不编码 JSON 或写文件。
        if (!queue.offer(new Item(new Intent(intent), sequence, elapsedNs, wallMs))) {
          stopLocked("queue_overflow");
        }
      } catch (RuntimeException error) {
        stopLocked("intent_copy_failed");
      }
    }
  }

  public void stop(String cause) {
    synchronized (intakeLock) { stopLocked(cause); }
  }

  private void stopLocked(String cause) {
    if (!accepting) return;
    accepting = false;
    registered = false;
    status = "stopping";
    reason = cause == null || cause.length() > 64 ? "manual_stop" : cause;
  }

  public boolean isAccepting() { return accepting; }
  public boolean isStopped() { return finished.getCount() == 0; }
  public File getFile() { return file; }
  public boolean awaitInitialized(long timeoutMs) throws InterruptedException {
    return initialized.await(timeoutMs, TimeUnit.MILLISECONDS);
  }
  public boolean awaitStopped(long timeoutMs) throws InterruptedException {
    return finished.await(timeoutMs, TimeUnit.MILLISECONDS);
  }
  public JSONObject snapshot() {
    try { return new JSONObject(latestStatus); }
    catch (JSONException impossible) { throw new IllegalStateException(impossible); }
  }

  private boolean expired() {
    return config.clock.elapsedNs() - startedElapsedNs >= config.durationMs * 1_000_000L;
  }

  private void process(Item item) throws JSONException {
    Object type;
    try {
      Bundle extras = item.intent.getExtras();
      type = extras == null ? null : extras.get("KEY_TYPE");
    } catch (RuntimeException error) {
      stop("intent_decode_failed");
      return;
    }
    if (!(type instanceof Integer) || ((Integer) type != 10001 && (Integer) type != 10019)) {
      ignored.incrementAndGet();
      return;
    }
    CaptureCodec.Result result = CaptureCodec.encodeIntent(item.intent, 16, config.maxRecordBytes);
    JSONObject record = new JSONObject().put("keyType", type).put("sequence", item.sequence)
        .put("receivedElapsedNs", item.elapsedNs).put("receivedWallMs", item.wallMs)
        .put("complete", result.complete).put("decodeErrors", result.decodeErrors)
        .put("senderVerified", false).put("rawIntent", result.json);
    if ((Integer) type == 10001) navigationRecord = record;
    else lampRecord = record;
    written.incrementAndGet();
    changed = true;
    // 不保留旧的完整记录来掩盖新收到的截断或解码错误。
    if (!result.complete || result.truncated) stop("incomplete_broadcast");
  }

  private JSONObject recordSnapshot(JSONObject record, long now) throws JSONException {
    if (record == null) return null;
    JSONObject copy = new JSONObject(record.toString());
    copy.put("sourceAgeMs", Math.max(0L, (now - copy.getLong("receivedElapsedNs")) / 1_000_000L));
    return copy;
  }

  private JSONObject envelope(boolean active) throws JSONException {
    long now = config.clock.elapsedNs();
    JSONObject records = new JSONObject();
    JSONObject navigation = recordSnapshot(navigationRecord, now);
    JSONObject lamp = recordSnapshot(lampRecord, now);
    if (navigation != null) records.put("10001", navigation);
    if (lamp != null) records.put("10019", lamp);
    return new JSONObject().put("schemaVersion", 1).put("sessionId", sessionId)
        .put("status", status).put("reason", reason).put("active", active)
        .put("valid", active && records.length() != 0)
        .put("validityScope", "receiver_transport_only_host_must_check_source_age")
        .put("activeRouteVerified", false).put("controlInputAllowed", false)
        .put("senderVerified", false).put("freshnessChecked", false)
        .put("sequence", received.get()).put("written", written.get()).put("ignored", ignored.get())
        .put("startedElapsedNs", startedElapsedNs).put("startedWallMs", startedWallMs)
        .put("publishedElapsedNs", now).put("publishedWallMs", config.clock.wallMs())
        .put("maxDurationMs", config.durationMs).put("maxFileBytes", MAX_FILE_BYTES)
        .put("maxRecordBytes", config.maxRecordBytes).put("queueCapacity", config.queueCapacity)
        .put("lastError", lastError).put("records", records);
  }

  private void publish(boolean active) throws JSONException, IOException {
    JSONObject value = envelope(active);
    byte[] bytes = (value.toString() + "\n").getBytes(StandardCharsets.UTF_8);
    if (bytes.length > MAX_FILE_BYTES) throw new IOException("snapshot_size_limit");
    File temporary = new File(directory, "latest.json.tmp");
    try {
      // 不使用旧 Android 的 AtomicFile 备份写法；ADB 直接读 base 文件也必须只见完整版本。
      try (FileOutputStream stream = new FileOutputStream(temporary)) {
        stream.write(bytes);
        stream.getFD().sync();
      }
      Os.rename(temporary.getAbsolutePath(), file.getAbsolutePath());
      latestStatus = value.toString();
    } catch (ErrnoException error) {
      temporary.delete();
      throw new IOException("atomic_rename_failed", error);
    } catch (IOException error) {
      temporary.delete();
      throw error;
    }
    changed = false;
  }

  private void runWriter() {
    try {
      if (!directory.isDirectory() && !directory.mkdirs()) throw new IOException("directory_unavailable");
      publish(false);
      initialized.countDown();
      if (listener != null) listener.onInitialized();
      if (config.testWorkerGate != null) config.testWorkerGate.await(5L, TimeUnit.SECONDS);
      long lastPublishNs = 0L;
      while (accepting) {
        if (expired()) {
          stop("duration_limit");
          break;
        }
        Item item = queue.poll(50L, TimeUnit.MILLISECONDS);
        if (!accepting) break;
        if (item != null) process(item);
        long now = config.clock.elapsedNs();
        if (accepting && registered && (changed || now - lastPublishNs >= config.publishIntervalMs * 1_000_000L)) {
          publish(true);
          lastPublishNs = now;
        }
      }
      queue.clear();
      status = "stopped";
      publish(false);
    } catch (Exception error) {
      synchronized (intakeLock) {
        accepting = false;
        registered = false;
        status = "failed";
        reason = "writer_failure";
        lastError = error.getClass().getSimpleName();
      }
      queue.clear();
      try { publish(false); }
      catch (Exception ignoredError) {
        // 无法替换为无效文件时删除旧快照；host 对缺失文件必须判不可用。
        file.delete();
        new File(directory, "latest.json.tmp").delete();
        try { latestStatus = envelope(false).toString(); }
        catch (JSONException impossible) { latestStatus = "{\"active\":false,\"valid\":false}"; }
      }
    } finally {
      accepting = false;
      registered = false;
      initialized.countDown();
      finished.countDown();
      if (listener != null) listener.onStopped();
    }
  }
}
