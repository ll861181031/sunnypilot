package com.jixiexiaoge.drivingassist.navdiag;

import android.app.Activity;
import android.app.Instrumentation;
import android.content.Intent;
import android.os.Bundle;
import android.os.SystemClock;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.UUID;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicLong;

/** 独立测试观测桥，不发送真实导航广播，不改动原始采集自测。 */
public final class BridgeInstrumentation extends Instrumentation {
  private interface Check { void run() throws Exception; }
  private final ArrayList<BridgeSession> sessions = new ArrayList<>();
  private int passed;
  private int failed;
  private final StringBuilder failures = new StringBuilder();

  @Override public void onCreate(Bundle arguments) {
    super.onCreate(arguments);
    start();
  }

  @Override public void onStart() {
    check("bridge_typed_raw_roundtrip", this::typedRoundtrip);
    check("bridge_latest_per_type", this::latestPerType);
    check("bridge_stop_invalidates", this::stopInvalidates);
    check("bridge_source_time_not_renewed", this::sourceTimeNotRenewed);
    check("bridge_overflow_invalidates", this::queueOverflow);
    check("bridge_oversize_replaces_old_record", this::oversize);
    check("bridge_registration_failure_invalidates", this::registrationFailure);
    check("bridge_duration_limit", this::durationLimit);
    check("bridge_atomic_readable_json", this::atomicRead);
    check("bridge_two_hour_limit_enforced", this::durationValidation);
    check("bridge_unrelated_type_ignored", this::unrelatedType);
    check("bridge_session_identity_changes", this::sessionIdentity);
    for (BridgeSession session : sessions) {
      session.stop("selftest_cleanup");
      try {
        if (!session.awaitStopped(6000L)) throw new IllegalStateException("观测桥写入线程未结束");
      } catch (Exception error) {
        failed++;
        failures.append("cleanup:").append(error.getClass().getSimpleName()).append('\n');
      }
    }
    Bundle result = new Bundle();
    result.putInt("tests_passed", passed);
    result.putInt("tests_failed", failed);
    result.putString("selftest_directory", root().getAbsolutePath());
    if (failed != 0) result.putString("error", failures.toString());
    finish(failed == 0 ? Activity.RESULT_OK : Activity.RESULT_CANCELED, result);
  }

  private void check(String name, Check check) {
    Bundle status = new Bundle();
    status.putString("test", name);
    try {
      check.run();
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

  private File root() { return new File(getTargetContext().getFilesDir(), "navdiag-bridge-selftest"); }

  private BridgeSession session(BridgeSession.Config config) throws Exception {
    BridgeSession result = new BridgeSession(new File(root(), UUID.randomUUID().toString()), config, null);
    sessions.add(result);
    require(result.awaitInitialized(3000L), "观测桥初始化超时");
    result.receiverRegistered();
    return result;
  }

  private static void require(boolean value, String message) {
    if (!value) throw new AssertionError(message);
  }

  private static Intent fixture(int type, String value) {
    return new Intent("NAVDIAG_BRIDGE_SELFTEST").putExtra("KEY_TYPE", type)
        .putExtra("payload", value).putExtra("longValue", Long.MAX_VALUE)
        .putExtra("array", new int[] {1, 2, 3});
  }

  private static void send(BridgeSession session, Intent intent) {
    session.recordIntent(intent, SystemClock.elapsedRealtimeNanos(), System.currentTimeMillis());
  }

  private static JSONObject read(File file) throws Exception {
    try (FileInputStream input = new FileInputStream(file);
         ByteArrayOutputStream output = new ByteArrayOutputStream()) {
      byte[] buffer = new byte[8192];
      int count;
      while ((count = input.read(buffer)) != -1) output.write(buffer, 0, count);
      return new JSONObject(new String(output.toByteArray(), StandardCharsets.UTF_8));
    }
  }

  private static JSONObject waitWritten(BridgeSession session, int count) throws Exception {
    long deadline = SystemClock.elapsedRealtime() + 5000L;
    JSONObject value;
    do {
      value = read(session.getFile());
      if (value.optLong("written") >= count) return value;
      Thread.sleep(10L);
    } while (SystemClock.elapsedRealtime() < deadline);
    throw new AssertionError("等待广播写入超时");
  }

  private static JSONObject stopped(BridgeSession session) throws Exception {
    require(session.awaitStopped(6000L), "停止观测桥超时");
    JSONObject value = read(session.getFile());
    require(!value.getBoolean("active") && !value.getBoolean("valid"), "结束文件仍声明有效");
    require(!value.getBoolean("activeRouteVerified") && !value.getBoolean("controlInputAllowed"), "观测桥错误放行路线或控制");
    return value;
  }

  private void typedRoundtrip() throws Exception {
    BridgeSession capture = session(new BridgeSession.Config());
    send(capture, fixture(10001, "navigation"));
    send(capture, fixture(10019, "lamp"));
    JSONObject value = waitWritten(capture, 2);
    JSONObject record = value.getJSONObject("records").getJSONObject("10001");
    JSONObject raw = record.getJSONObject("rawIntent").getJSONObject("extras").getJSONObject("value");
    require(value.getBoolean("active") && value.getBoolean("valid"), "完整广播未标为传输有效");
    require(raw.getJSONObject("KEY_TYPE").getString("type").equals("int"), "原始类型丢失");
    require(raw.getJSONObject("longValue").getString("value").equals(Long.toString(Long.MAX_VALUE)), "long值丢失精度");
    require(raw.getJSONObject("array").getJSONArray("value").length() == 3, "原始数组丢失");
    require(record.getLong("receivedElapsedNs") > 0 && record.getLong("receivedWallMs") > 0, "源时间缺失");
    require(!record.getBoolean("senderVerified") && !value.getBoolean("freshnessChecked"), "错误认证来源或新鲜度");
  }

  private void latestPerType() throws Exception {
    BridgeSession capture = session(new BridgeSession.Config());
    send(capture, fixture(10001, "old"));
    send(capture, fixture(10019, "lamp"));
    send(capture, fixture(10001, "new"));
    JSONObject value = waitWritten(capture, 3);
    JSONObject records = value.getJSONObject("records");
    require(records.length() == 2 && records.getJSONObject("10001").getInt("sequence") == 3
        && records.getJSONObject("10019").getInt("sequence") == 2, "未按广播类型保留最新记录");
    require(!value.toString().contains("\"value\":\"old\""), "旧广播残留");
  }

  private void stopInvalidates() throws Exception {
    BridgeSession capture = session(new BridgeSession.Config());
    send(capture, fixture(10001, "before"));
    waitWritten(capture, 1);
    capture.stop("manual_stop");
    JSONObject value = stopped(capture);
    require(value.getString("reason").equals("manual_stop"), "手动停止原因丢失");
    send(capture, fixture(10001, "after"));
    require(read(capture.getFile()).getInt("sequence") == 1, "停止后仍更新源数据");
  }

  private void sourceTimeNotRenewed() throws Exception {
    final AtomicLong elapsed = new AtomicLong(1_000_000_000L);
    BridgeSession.Config config = new BridgeSession.Config();
    config.publishIntervalMs = 20L;
    config.clock = new BridgeSession.Clock() {
      @Override public long elapsedNs() { return elapsed.get(); }
      @Override public long wallMs() { return 10000L + elapsed.get() / 1_000_000L; }
    };
    BridgeSession capture = session(config);
    capture.recordIntent(fixture(10001, "once"), elapsed.get(), config.clock.wallMs());
    JSONObject first = waitWritten(capture, 1).getJSONObject("records").getJSONObject("10001");
    elapsed.addAndGet(10_000_000_000L);
    long deadline = SystemClock.elapsedRealtime() + 3000L;
    JSONObject value;
    do {
      value = read(capture.getFile());
      if (value.getLong("publishedElapsedNs") == elapsed.get()) break;
      Thread.sleep(10L);
    } while (SystemClock.elapsedRealtime() < deadline);
    JSONObject second = value.getJSONObject("records").getJSONObject("10001");
    require(second.getLong("receivedElapsedNs") == first.getLong("receivedElapsedNs")
        && second.getLong("sequence") == first.getLong("sequence")
        && second.getLong("sourceAgeMs") == 10000L, "周期发布伪造源时间续期");
    require(!value.getBoolean("freshnessChecked"), "观测桥错误声称已核验广播时效");
  }

  private void queueOverflow() throws Exception {
    BridgeSession.Config config = new BridgeSession.Config();
    config.queueCapacity = 1;
    config.testWorkerGate = new CountDownLatch(1);
    BridgeSession capture = session(config);
    try {
      send(capture, fixture(10001, "first"));
      send(capture, fixture(10001, "overflow"));
    } finally { config.testWorkerGate.countDown(); }
    JSONObject value = stopped(capture);
    require(value.getString("reason").equals("queue_overflow") && value.getInt("sequence") == 2,
        "队列溢出未结束会话或序号不闭合");
    require(value.getJSONObject("records").length() == 0, "溢出后继续发布排队的旧记录");
  }

  private void oversize() throws Exception {
    BridgeSession.Config config = new BridgeSession.Config();
    config.maxRecordBytes = 2048;
    BridgeSession capture = session(config);
    send(capture, fixture(10001, "old_complete"));
    waitWritten(capture, 1);
    char[] chars = new char[100000];
    Arrays.fill(chars, '大');
    send(capture, fixture(10001, new String(chars)));
    JSONObject value = stopped(capture);
    JSONObject record = value.getJSONObject("records").getJSONObject("10001");
    require(value.getString("reason").equals("incomplete_broadcast") && !record.getBoolean("complete")
        && record.getInt("sequence") == 2 && record.getJSONObject("rawIntent").getJSONArray("issues").length() > 0,
        "超大广播未显式替换旧记录为不完整记录");
    require(!value.toString().contains("old_complete") && capture.getFile().length() <= BridgeSession.MAX_FILE_BYTES,
        "超大记录保留旧有效值或突破文件上限");
  }

  private void registrationFailure() throws Exception {
    BridgeSession capture = session(new BridgeSession.Config());
    capture.registrationFailed();
    JSONObject value = stopped(capture);
    require(value.getString("reason").equals("receiver_registration_failed"), "注册失败没有可靠的无效结束记录");
  }

  private void durationLimit() throws Exception {
    BridgeSession.Config config = new BridgeSession.Config();
    config.durationMs = 120L;
    BridgeSession capture = session(config);
    require(stopped(capture).getString("reason").equals("duration_limit"), "最长会话未自动结束");
  }

  private void atomicRead() throws Exception {
    BridgeSession capture = session(new BridgeSession.Config());
    final Throwable[] error = {null};
    final AtomicBoolean done = new AtomicBoolean();
    Thread reader = new Thread(() -> {
      try {
        for (int i = 0; i < 500 && !done.get(); i++) {
          JSONObject value = read(capture.getFile());
          require(value.getInt("schemaVersion") == 1, "读到不完整schema");
        }
      } catch (Throwable failure) { error[0] = failure; }
    }, "navdiag-selftest-reader");
    reader.start();
    try {
      char[] padding = new char[20000];
      Arrays.fill(padding, 'x');
      for (int i = 0; i < 12; i++) {
        send(capture, fixture(i % 2 == 0 ? 10001 : 10019, new String(padding)));
        waitWritten(capture, i + 1);
      }
    } finally {
      done.set(true);
      reader.join(5000L);
    }
    require(!reader.isAlive() && error[0] == null, "原子文件读到半份或读线程未退出");
    require(capture.getFile().length() <= BridgeSession.MAX_FILE_BYTES, "文件超过硬上限");
  }

  private void durationValidation() throws Exception {
    BridgeSession.Config config = new BridgeSession.Config();
    config.durationMs = BridgeSession.MAX_DURATION_MS + 1L;
    try {
      new BridgeSession(new File(root(), UUID.randomUUID().toString()), config, null);
      throw new AssertionError("接受了超过2小时的会话");
    } catch (IllegalArgumentException expected) { }
  }

  private void unrelatedType() throws Exception {
    BridgeSession capture = session(new BridgeSession.Config());
    send(capture, fixture(10003, "unrelated"));
    Thread.sleep(150L);
    capture.stop("manual_stop");
    JSONObject value = stopped(capture);
    require(value.getJSONObject("records").length() == 0 && value.getInt("ignored") == 1,
        "观测桥保存了范围外广播");
  }

  private void sessionIdentity() throws Exception {
    BridgeSession first = session(new BridgeSession.Config());
    BridgeSession second = session(new BridgeSession.Config());
    first.stop("manual_stop");
    second.stop("manual_stop");
    require(!stopped(first).getString("sessionId").equals(stopped(second).getString("sessionId")), "不同会话UUID复用");
  }
}
