package com.jixiexiaoge.drivingassist.navdiag;

import android.app.Activity;
import android.app.Instrumentation;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Iterator;
import java.util.Locale;
import java.util.concurrent.CountDownLatch;

/** 在真机上测试本包编码和写盘，不发送导航广播，不混入真实采集目录。 */
public final class CaptureInstrumentation extends Instrumentation {
  private interface Check { void run() throws Exception; }
  private int passed;
  private int failed;
  private final StringBuilder failures = new StringBuilder();
  private final ArrayList<CaptureSession> sessions = new ArrayList<>();

  @Override public void onCreate(Bundle arguments) {
    super.onCreate(arguments);
    start();
  }

  @Override public void onStart() {
    check("nested_arrays", this::nestedArrays);
    check("numeric_fidelity", this::numericFidelity);
    check("unsupported_types", this::unsupportedTypes);
    check("depth_limit", this::depthLimit);
    check("message_size_limit", this::messageSizeLimit);
    check("session_roundtrip_hashes", this::sessionRoundtrip);
    check("queue_overflow_accounted", this::queueOverflow);
    check("duration_limit", this::durationLimit);
    check("session_size_limit", this::sessionSizeLimit);
    check("bad_record_recovery", this::badRecordRecovery);
    check("stopped_session_ignores_input", this::stoppedSession);
    check("failed_receiver_is_incomplete", this::failedReceiver);
    for (CaptureSession session : sessions) {
      session.stop("selftest_cleanup");
      try {
        if (!session.awaitStopped(6000)) throw new IllegalStateException("自测写入线程未结束");
      } catch (Exception error) {
        failed++;
        failures.append("cleanup:").append(error.getClass().getSimpleName()).append('\n');
      }
    }
    Bundle results = new Bundle();
    results.putInt("tests_passed", passed);
    results.putInt("tests_failed", failed);
    results.putString("selftest_directory", new File(getTargetContext().getFilesDir(), "navdiag-selftest").getAbsolutePath());
    if (failed != 0) results.putString("error", failures.toString());
    finish(failed == 0 ? Activity.RESULT_OK : Activity.RESULT_CANCELED, results);
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
      failures.append(name).append(':').append(error.getClass().getSimpleName())
          .append(':').append(error.getMessage()).append('\n');
      status.putString("result", "failed");
      status.putString("error", error.getClass().getSimpleName() + ": " + error.getMessage());
    }
    sendStatus(0, status);
  }

  private static void require(boolean value, String message) {
    if (!value) throw new AssertionError(message);
  }

  private static Intent fixture() {
    // 合成测试点只用于保真验证，不能作为高德真实路线证据。
    Bundle route = new Bundle();
    route.putDoubleArray("polyline", new double[] {116.300000123, 39.900000456, 116.301000789, 39.901000123});
    route.putString("source", "synthetic_selftest");
    route.putStringArray("labels", new String[] {"测试一", "测试二"});
    route.putIntegerArrayList("lanes", new ArrayList<>(Arrays.asList(1, 2, 3)));
    return new Intent("NAVDIAG_SELFTEST").putExtra("route", route).putExtra("KEY_TYPE", 10003);
  }

  private static JSONObject extras(CaptureCodec.Result result) throws Exception {
    return result.json.getJSONObject("extras").getJSONObject("value");
  }

  private void nestedArrays() throws Exception {
    CaptureCodec.Result result = CaptureCodec.encodeIntent(fixture(), 16, 65536);
    require(result.complete && !result.truncated, "嵌套数组被截断");
    JSONObject route = extras(result).getJSONObject("route").getJSONObject("value");
    JSONObject points = route.getJSONObject("polyline");
    require(points.getInt("length") == 4 && points.getInt("capturedLength") == 4, "坐标数组计数错误");
    JSONArray values = points.getJSONArray("value");
    require(values.length() == 4, "只保存数组长度，未保存坐标值");
    require(Double.doubleToRawLongBits(values.getJSONObject(0).getDouble("value"))
        == Double.doubleToRawLongBits(116.300000123), "坐标精度丢失");
    require(route.getJSONObject("labels").getJSONArray("value").getJSONObject(1).getString("value").equals("测试二"),
        "字符串数组未保留");
    require(route.getJSONObject("lanes").getJSONArray("value").length() == 3, "列表未保留");
  }

  private void numericFidelity() throws Exception {
    double nan = Double.longBitsToDouble(0x7ff8000000000042L);
    Intent intent = new Intent("NAVDIAG_SELFTEST").putExtra("long", Long.MAX_VALUE).putExtra("char", '中')
        .putExtra("nan", nan).putExtra("positive", Double.POSITIVE_INFINITY)
        .putExtra("negative", Float.NEGATIVE_INFINITY).putExtra("zero", -0.0d);
    CaptureCodec.Result result = CaptureCodec.encodeIntent(intent, 16, 65536);
    require(result.complete, "数值编码不完整");
    JSONObject values = extras(result);
    require(values.getJSONObject("long").getString("value").equals(Long.toString(Long.MAX_VALUE)), "long精度丢失");
    require(values.getJSONObject("nan").getString("bits").equals("7ff8000000000042"), "NaN位模式丢失");
    require(values.getJSONObject("positive").getString("value").equals("Infinity"), "正无穷丢失");
    require(values.getJSONObject("negative").getString("value").equals("-Infinity"), "负无穷丢失");
    require(values.getJSONObject("zero").getString("bits").equals("8000000000000000"), "负零丢失");
    require(values.getJSONObject("char").getString("value").equals("中"), "字符丢失");
  }

  private void unsupportedTypes() throws Exception {
    Intent intent = new Intent("NAVDIAG_SELFTEST").putExtra("parcel", Uri.parse("navdiag://synthetic"))
        .putExtra("serial", new java.util.Date(0));
    CaptureCodec.Result result = CaptureCodec.encodeIntent(intent, 16, 65536);
    require(!result.complete && result.decodeErrors == 2, "未知类型未报告不完整");
    require(extras(result).getJSONObject("parcel").getString("type").equals("unsupported"), "Parcelable被伪造解析");
  }

  private void depthLimit() {
    CaptureCodec.Result result = CaptureCodec.encodeIntent(fixture(), 1, 65536);
    require(!result.complete && result.truncated, "嵌套深度上限未生效");
  }

  private void messageSizeLimit() {
    char[] chars = new char[8000];
    Arrays.fill(chars, '测');
    CaptureCodec.Result result = CaptureCodec.encodeIntent(new Intent("NAVDIAG_SELFTEST")
        .putExtra("oversized", new String(chars)), 16, 2048);
    require(!result.complete && result.truncated && result.encodedBytes <= 2048, "消息限额未生效");
  }

  private CaptureSession session(CaptureSession.Config config) throws Exception {
    CaptureSession result = new CaptureSession(getTargetContext(),
        new File(getTargetContext().getFilesDir(), "navdiag-selftest"), config);
    sessions.add(result);
    return result;
  }

  private static JSONObject completed(CaptureSession session) throws Exception {
    require(session.awaitStopped(8000), "会话未及时结束");
    return readJson(new File(session.getDirectory(), "completed.json"));
  }

  private void sessionRoundtrip() throws Exception {
    CaptureSession capture = session(new CaptureSession.Config());
    capture.recordIntent(fixture());
    require(capture.mark("synthetic_selftest"), "标记未入队");
    capture.stop("selftest");
    JSONObject end = completed(capture);
    require(end.getBoolean("captureComplete"), "正常会话未完整结束");
    require(end.getJSONObject("counts").getInt("written") == 1, "广播写入计数错误");
    JSONObject hashes = end.getJSONObject("fileHashes");
    for (Iterator<String> keys = hashes.keys(); keys.hasNext();) {
      String key = keys.next();
      require(hashes.getString(key).equals(sha256(read(new File(capture.getDirectory(), key)))), "文件哈希不一致:" + key);
    }
    String line = new String(read(new File(capture.getDirectory(), "broadcasts.jsonl")), StandardCharsets.UTF_8).trim();
    JSONObject record = new JSONObject(line);
    require(record.getInt("sequence") == 1 && record.getLong("receivedElapsedNs") > 0, "记录序号或时间戳错误");
    require(!record.getJSONObject("sender").getBoolean("verified"), "错误认证广播发送方");
    require(record.getJSONObject("extras").getJSONObject("value").getJSONObject("route")
        .getJSONObject("value").getJSONObject("polyline").getJSONArray("value").length() == 4, "写盘丢失坐标值");
  }

  private void queueOverflow() throws Exception {
    CaptureSession.Config config = new CaptureSession.Config();
    config.queueCapacity = 1;
    config.testWorkerGate = new CountDownLatch(1);
    CaptureSession capture = session(config);
    try {
      capture.recordIntent(fixture());
      capture.recordIntent(fixture());
      capture.stop("selftest");
    } finally { config.testWorkerGate.countDown(); }
    JSONObject end = completed(capture);
    JSONObject counts = end.getJSONObject("counts");
    require(!end.getBoolean("captureComplete") && counts.getInt("received") == 2
        && counts.getInt("written") == 1 && counts.getInt("dropped") == 1, "队列丢包计数不闭合");
  }

  private void durationLimit() throws Exception {
    CaptureSession.Config config = new CaptureSession.Config();
    config.durationMs = 150;
    CaptureSession capture = session(config);
    completed(capture);
    require(capture.snapshot().getString("stopReason").equals("duration_limit"), "时长上限未结束会话");
    require(!capture.isAccepting(), "结束后仍然接收");
  }

  private void sessionSizeLimit() throws Exception {
    CaptureSession.Config config = new CaptureSession.Config();
    config.maxSessionBytes = 65536;
    config.maxRecordBytes = 32768;
    config.queueCapacity = 32;
    config.testWorkerGate = new CountDownLatch(1);
    CaptureSession capture = session(config);
    char[] chars = new char[3500];
    Arrays.fill(chars, 'x');
    try {
      for (int i = 0; i < 24; i++) capture.recordIntent(new Intent("NAVDIAG_SELFTEST").putExtra("padding", new String(chars)));
    } finally { config.testWorkerGate.countDown(); }
    JSONObject end = completed(capture);
    require(capture.snapshot().getString("stopReason").equals("size_limit"), "文件限额未结束会话");
    require(!end.getBoolean("captureComplete") && end.getJSONObject("counts").getInt("dropped") > 0, "文件限额未记录丢包");
    long size = 0;
    File[] files = capture.getDirectory().listFiles();
    require(files != null, "无法检查会话文件");
    for (File file : files) size += file.length();
    require(size <= config.maxSessionBytes, "最终文件超过会话限额");
  }

  private void badRecordRecovery() throws Exception {
    CaptureSession capture = session(new CaptureSession.Config());
    capture.recordIntent(new Intent("NAVDIAG_SELFTEST").putExtra("unknown", Uri.parse("navdiag://synthetic")));
    capture.recordIntent(fixture());
    capture.stop("selftest");
    JSONObject end = completed(capture);
    JSONObject counts = end.getJSONObject("counts");
    require(!end.getBoolean("captureComplete") && counts.getInt("written") == 2
        && counts.getInt("incomplete") == 1 && counts.getInt("decodeErrors") == 1, "错误阻断后续记录");
  }

  private void stoppedSession() throws Exception {
    CaptureSession capture = session(new CaptureSession.Config());
    capture.stop("selftest");
    completed(capture);
    capture.recordIntent(fixture());
    require(!capture.mark("late") && capture.snapshot().getInt("received") == 0, "结束后仍入队");
  }

  private void failedReceiver() throws Exception {
    CaptureSession capture = session(new CaptureSession.Config());
    capture.stop("service_start_failed");
    require(!completed(capture).getBoolean("captureComplete"), "接收器启动失败被误标完整采集");
  }

  private static byte[] read(File file) throws Exception {
    try (FileInputStream input = new FileInputStream(file); ByteArrayOutputStream output = new ByteArrayOutputStream()) {
      byte[] buffer = new byte[8192];
      int count;
      while ((count = input.read(buffer)) != -1) output.write(buffer, 0, count);
      return output.toByteArray();
    }
  }

  private static JSONObject readJson(File file) throws Exception {
    return new JSONObject(new String(read(file), StandardCharsets.UTF_8));
  }

  private static String sha256(byte[] bytes) throws Exception {
    StringBuilder result = new StringBuilder();
    for (byte value : MessageDigest.getInstance("SHA-256").digest(bytes)) result.append(String.format(Locale.US, "%02x", value & 255));
    return result.toString();
  }
}
