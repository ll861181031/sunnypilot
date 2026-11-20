package com.jixiexiaoge.drivingassist.navdiag;

import android.app.Activity;
import android.app.Instrumentation;
import android.location.Location;
import android.os.Bundle;
import android.os.SystemClock;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.StringReader;
import java.lang.reflect.Method;

/** 仅用合成 Location 检查同一解析算法；不请求定位，不连接真实 GPS 或高德。 */
public final class AndroidGpsProbeInstrumentation extends Instrumentation {
  private interface Check { void run() throws Exception; }
  private static final long BEFORE = 100_000_000_000L;
  private static final long AFTER = 100_050_000_000L;
  private static final long SOURCE = 99_800_000_000L;
  private static final String BOOT = "01234567-89ab-cdef-0123-456789abcdef";
  private int passed;
  private int failed;
  private final StringBuilder failures = new StringBuilder();

  private static final class Clock implements AndroidGpsProbe.ClockSource {
    long before = BEFORE;
    long after = AFTER;
    String secondBoot = BOOT;
    int elapsedCalls;
    int bootCalls;
    @Override public long elapsed() { return elapsedCalls++ == 0 ? before : after; }
    @Override public String boot() { return bootCalls++ == 0 ? BOOT : secondBoot; }
  }

  private static Location location() {
    Location result = new Location("gps");
    result.setLatitude(22.5);
    result.setLongitude(11.25);
    result.setAccuracy(1);
    result.setElapsedRealtimeNanos(SOURCE);
    result.setTime(0);
    return result;
  }

  private static JSONObject observe(Location location) { return observe(location, new Clock()); }
  private static JSONObject observe(Location location, Clock clock) {
    return AndroidGpsProbe.observe(() -> location, clock);
  }

  private static void require(boolean value, String message) {
    if (!value) throw new AssertionError(message);
  }

  private static void untrusted(JSONObject result) throws Exception {
    for (String key : new String[] {"gnss_hardware_authenticity_verified", "amap_native_pose_freshness_verified",
        "coordinate_datum_globally_verified", "control_input_allowed"}) {
      require(!result.getBoolean(key), "合成位置被错误升级认证");
    }
  }

  private static void unavailable(JSONObject result, String reason) throws Exception {
    require(result.getString("status").equals("unavailable"), "错误源未拒绝");
    require(result.getString("reason").equals(reason), "固定拒绝原因不符");
    for (String key : new String[] {"latitude", "longitude", "elapsed_realtime_ns", "source_token", "phone_boot_id"}) {
      require(!result.has(key), "拒绝结果仍输出位置或源身份");
    }
    untrusted(result);
  }

  private static JSONObject liveDiagnostics(long callbacks, long cacheReads, long callbackAgeMs,
                                            long readAgeMs) throws Exception {
    return new JSONObject().put("registration", "request_accepted")
        .put("gps_registration", "request_accepted").put("passive_registration", "request_accepted")
        .put("callback_count", callbacks).put("gps_callback_count", callbacks / 2)
        .put("passive_callback_count", callbacks - callbacks / 2).put("cache_read_count", cacheReads)
        .put("callbacks_during_observation", callbacks).put("observation_elapsed_ms", callbackAgeMs)
        .put("callback_age_ms", callbackAgeMs).put("last_read_age_ms", readAgeMs)
        .put("callback_thread_alive", true);
  }

  @Override public void onCreate(Bundle arguments) { super.onCreate(arguments); start(); }

  @Override public void onStart() {
    check("gps_fresh_source_and_exact_fields", this::fresh);
    check("gps_fresh_cache_skips_live_request", this::freshCacheSkipsRequest);
    check("gps_expired_cache_requests_once", this::expiredCacheRequests);
    check("gps_failed_refresh_keeps_stale_cache", this::failedRefreshKeepsCache);
    check("gps_latest_callback_selection", this::latestCallbackSelection);
    check("gps_source_time_not_wall_clock", this::wallClock);
    check("gps_token_not_refreshed_by_reread", this::repeated);
    check("gps_advancing_source_changes_token", this::advancing);
    check("gps_two_second_boundary_rejected", this::stale);
    check("gps_future_and_zero_source_rejected", this::sourceTime);
    check("gps_fused_and_missing_provider_rejected", this::provider);
    check("gps_true_mock_rejected", this::mock);
    check("gps_missing_mock_api_not_false", this::mockUnavailable);
    check("gps_accuracy_required_and_bounded", this::accuracy);
    check("gps_coordinates_finite_and_bounded", this::coordinates);
    check("gps_boot_change_rejected", this::boot);
    check("gps_clock_window_required_and_bounded", this::clock);
    check("gps_empty_cache_rejected", () -> unavailable(observe(null), "gps_cache_empty"));
    check("gps_security_details_private_and_bounded", this::security);
    check("gps_arbitrary_error_text_suppressed", this::error);
    check("gps_command_reader_fixed_and_bounded", this::commands);
    check("gps_command_budget_survives_180", this::budgetPast180);
    check("gps_command_budget_count_boundary", this::budgetCount);
    check("gps_command_budget_two_hour_boundary", this::budgetDuration);
    check("gps_command_budget_clock_anomaly_rejected", this::budgetClock);
    check("gps_live_diagnostics_are_secret_free", this::liveDiagnostics);
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
      failures.append(name).append(':').append(error.getClass().getSimpleName()).append('\n');
      status.putString("result", "failed");
      status.putString("error", error.getClass().getSimpleName());
    }
    sendStatus(0, status);
  }

  private void fresh() throws Exception {
    JSONObject value = observe(location());
    require(value.getString("status").equals("observed"), "新鲜合成源不可读");
    require(value.getString("provider").equals("gps") && !value.getBoolean("is_from_mock_provider"), "源身份错误");
    require(value.getDouble("latitude") == 22.5 && value.getDouble("longitude") == 11.25, "坐标未保留");
    require(value.getLong("elapsed_realtime_ns") == SOURCE && value.getLong("phone_before_ns") == BEFORE
        && value.getLong("phone_after_ns") == AFTER, "混用源时间与读取时间");
    require(value.getLong("age_lower_ns") == 200_000_000L && value.getLong("age_upper_ns") == 250_000_000L,
        "源年龄未来自 elapsedRealtime");
    require(value.getBoolean("source_elapsed_time_fresh"), "新鲜标志错误");
    untrusted(value);
  }

  private static Location current(long ageNs, double latitude) {
    Location value = location();
    value.setElapsedRealtimeNanos(SystemClock.elapsedRealtimeNanos() - ageNs);
    value.setLatitude(latitude);
    return value;
  }

  private void freshCacheSkipsRequest() throws Exception {
    final int[] calls = {0};
    Location cached = current(100_000_000L, 22.5);
    AndroidGpsProbe.GpsAccess access = new AndroidGpsProbe.GpsAccess() {
      @Override public Location cached() { return cached; }
      @Override public Location requestOnce() { calls[0]++; throw new AssertionError("新鲜缓存不应请求实时定位"); }
    };
    Location value = new AndroidGpsProbe.FreshGpsSource(access).lastGps();
    require(value == cached && calls[0] == 0, "新鲜缓存被重复请求或替换");
  }

  private void expiredCacheRequests() throws Exception {
    final int[] calls = {0};
    Location cached = current(AndroidGpsProbe.MAX_AGE_NS + 1, 22.5);
    Location renewed = current(0, 22.6);
    AndroidGpsProbe.GpsAccess access = new AndroidGpsProbe.GpsAccess() {
      @Override public Location cached() { return cached; }
      @Override public Location requestOnce() { calls[0]++; return renewed; }
    };
    Location value = new AndroidGpsProbe.FreshGpsSource(access).lastGps();
    require(value == renewed && calls[0] == 1, "过期缓存未触发且仅触发一次实时请求");
  }

  private void failedRefreshKeepsCache() throws Exception {
    final int[] calls = {0};
    Location cached = current(AndroidGpsProbe.MAX_AGE_NS + 1, 22.5);
    AndroidGpsProbe.GpsAccess access = new AndroidGpsProbe.GpsAccess() {
      @Override public Location cached() { return cached; }
      @Override public Location requestOnce() { calls[0]++; return null; }
    };
    Location value = new AndroidGpsProbe.FreshGpsSource(access).lastGps();
    require(value == cached && calls[0] == 1, "无实时回包时未保留原缓存供统一拒绝");
  }

  private void latestCallbackSelection() {
    Location older = current(400_000_000L, 22.5);
    older.setElapsedRealtimeNanos(SOURCE);
    Location newer = current(100_000_000L, 22.6);
    newer.setElapsedRealtimeNanos(SOURCE + 10);
    require(AndroidGpsProbe.LiveGpsSource.newest(older, newer) == newer, "较新回调未被选中");
    require(AndroidGpsProbe.LiveGpsSource.newest(newer, older) == newer, "较旧缓存覆盖了新回调");
    require(AndroidGpsProbe.LiveGpsSource.newest(null, newer) == newer, "唯一定位未被选中");
    require(AndroidGpsProbe.LiveGpsSource.newest(older, null) == older, "无回调时未保留缓存");
  }

  private void wallClock() throws Exception {
    Location first = location();
    Location second = location();
    second.setTime(Long.MAX_VALUE);
    require(observe(first).getString("source_token").equals(observe(second).getString("source_token")), "墙钟刷新源身份");
  }

  private void repeated() throws Exception {
    JSONObject first = observe(location());
    Clock later = new Clock();
    later.before += 100_000_000L;
    later.after += 100_000_000L;
    JSONObject second = observe(location(), later);
    require(first.getString("source_token").equals(second.getString("source_token")), "重读修改源 token");
    require(second.getLong("age_upper_ns") == first.getLong("age_upper_ns") + 100_000_000L, "重读续期旧 fix");
  }

  private void advancing() throws Exception {
    Location advanced = location();
    advanced.setElapsedRealtimeNanos(SOURCE + 1);
    require(!observe(location()).getString("source_token").equals(observe(advanced).getString("source_token")),
        "新源时间未更新 token");
  }

  private void stale() throws Exception {
    Location value = location();
    value.setElapsedRealtimeNanos(AFTER - AndroidGpsProbe.MAX_AGE_NS);
    unavailable(observe(value), "gps_source_stale");
  }

  private void sourceTime() throws Exception {
    for (long time : new long[] {0, -1, AFTER + 1}) {
      Location value = location();
      value.setElapsedRealtimeNanos(time);
      unavailable(observe(value), "gps_source_time_invalid_or_future");
    }
  }

  private void provider() throws Exception {
    for (String provider : new String[] {"fused", "passive", null}) {
      Location value = location();
      value.setProvider(provider);
      unavailable(observe(value), "gps_provider_invalid");
    }
  }

  private void mock() throws Exception {
    Location value = location();
    Method setter = Location.class.getDeclaredMethod("setIsFromMockProvider", boolean.class);
    setter.setAccessible(true);
    setter.invoke(value, true);
    unavailable(observe(value), "mock_gps_rejected");
  }

  private void mockUnavailable() throws Exception {
    JSONObject result = AndroidGpsProbe.observe(() -> { throw new NoSuchMethodError("synthetic unavailable mock flag"); }, new Clock());
    unavailable(result, "gps_cache_read_failed");
  }

  private void accuracy() throws Exception {
    for (float accuracy : new float[] {0, -1, 26, Float.NaN, Float.POSITIVE_INFINITY}) {
      Location value = location();
      value.setAccuracy(accuracy);
      unavailable(observe(value), "gps_accuracy_unusable");
    }
    Location degraded = location();
    degraded.setAccuracy(25);
    require(observe(degraded).getString("status").equals("observed"), "15米级真实精度无法进入地图采集链");
    Location missing = location();
    missing.removeAccuracy();
    unavailable(observe(missing), "gps_accuracy_unusable");
  }

  private void coordinates() throws Exception {
    for (double number : new double[] {Double.NaN, Double.POSITIVE_INFINITY, 181, -181}) {
      Location value = location();
      value.setLongitude(number);
      unavailable(observe(value), "gps_coordinates_invalid");
    }
    Location value = location();
    value.setLatitude(91);
    unavailable(observe(value), "gps_coordinates_invalid");
  }

  private void boot() throws Exception {
    Clock changed = new Clock();
    changed.secondBoot = "01234567-89ab-cdef-0123-456789abcdee";
    unavailable(observe(location(), changed), "phone_boot_changed_or_invalid");
    changed = new Clock();
    changed.secondBoot = null;
    unavailable(observe(location(), changed), "phone_boot_changed_or_invalid");
  }

  private void clock() throws Exception {
    for (long[] times : new long[][] {{0, AFTER}, {BEFORE, BEFORE - 1}, {BEFORE, BEFORE + AndroidGpsProbe.MAX_READ_NS + 1}}) {
      Clock clock = new Clock();
      clock.before = times[0];
      clock.after = times[1];
      unavailable(observe(location(), clock), "gps_read_clock_invalid");
    }
  }

  private void security() throws Exception {
    StringBuilder message = new StringBuilder();
    for (int index = 0; index < 2048; index++) message.append('x');
    JSONObject value = AndroidGpsProbe.observe(() -> { throw new SecurityException(message.toString()); }, new Clock());
    unavailable(value, "gps_cache_security_rejected");
    require(value.getString("_private_security_exception").length() == 1024, "私有安全原因未限长");
    require(!value.getString("reason").contains("xxx"), "私有错误进入公开原因");
  }

  private void error() throws Exception {
    JSONObject value = AndroidGpsProbe.observe(() -> { throw new IllegalStateException("synthetic private detail"); }, new Clock());
    unavailable(value, "gps_cache_read_failed");
    require(!value.toString().contains("synthetic private detail"), "任意异常正文泄露");
  }

  private void commands() throws Exception {
    BufferedReader reader = new BufferedReader(new StringReader("OBSERVE\nQUIT\n"));
    require(AndroidGpsProbe.readCommand(reader).equals("OBSERVE") && AndroidGpsProbe.readCommand(reader).equals("QUIT")
        && AndroidGpsProbe.readCommand(reader) == null, "固定命令未保持");
    for (String incomplete : new String[] {"", "OBSERVE", "OBS", "QUIT"}) {
      require(AndroidGpsProbe.readCommand(new BufferedReader(new StringReader(incomplete))) == null,
          "EOF 后仍执行半条命令");
    }
    for (String invalid : new String[] {"OBSERVE\r\n", "OBSERVE\u0000\n", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n"}) {
      boolean rejected = false;
      try { AndroidGpsProbe.readCommand(new BufferedReader(new StringReader(invalid))); }
      catch (Exception expected) { rejected = true; }
      require(rejected, "无界或控制字符命令未拒绝");
    }
  }

  private void budgetPast180() {
    long previous = BEFORE;
    for (int count = 0; count < 1024; count++) {
      long now = BEFORE + count * 1_000_000L;
      require(AndroidGpsProbe.budgetAllows(count, BEFORE, previous, now), "有效采集在旧180次边界附近中断");
      previous = now;
    }
    require(AndroidGpsProbe.budgetAllows(180, BEFORE, BEFORE, BEFORE + 1)
        && AndroidGpsProbe.budgetAllows(181, BEFORE, BEFORE, BEFORE + 1), "未明确放行第181次及之后命令");
  }

  private void budgetCount() {
    require(AndroidGpsProbe.MAX_COMMANDS == 144_000, "固定次数预算被改写");
    require(AndroidGpsProbe.budgetAllows(143_999, BEFORE, BEFORE, BEFORE + 1), "最后一个合法命令被拒绝");
    for (int count : new int[] {-1, 144_000, 144_001, Integer.MAX_VALUE, Integer.MIN_VALUE}) {
      require(!AndroidGpsProbe.budgetAllows(count, BEFORE, BEFORE, BEFORE + 1), "次数越界仍继续采集");
    }
  }

  private void budgetDuration() {
    require(AndroidGpsProbe.MAX_SESSION_NS == 7_200_000_000_000L, "固定两小时预算被改写");
    long boundary = BEFORE + AndroidGpsProbe.MAX_SESSION_NS;
    require(AndroidGpsProbe.budgetAllows(1, BEFORE, BEFORE, boundary - 1), "两小时边界前提前停止");
    require(!AndroidGpsProbe.budgetAllows(1, BEFORE, BEFORE, boundary)
        && !AndroidGpsProbe.budgetAllows(1, BEFORE, BEFORE, boundary + 1), "达到两小时仍继续采集");
    require(AndroidGpsProbe.budgetAllows(1, Long.MAX_VALUE - 100, Long.MAX_VALUE - 1, Long.MAX_VALUE),
        "通过截止时间加法导致溢出误判");
  }

  private void budgetClock() {
    for (long[] values : new long[][] {{0, 0, 0}, {-1, 1, 2}, {BEFORE, BEFORE - 1, BEFORE},
        {BEFORE, BEFORE + 2, BEFORE + 1}, {BEFORE, BEFORE, BEFORE - 1},
        {BEFORE, BEFORE, Long.MIN_VALUE}, {BEFORE, Long.MAX_VALUE, BEFORE}}) {
      require(!AndroidGpsProbe.budgetAllows(1, values[0], values[1], values[2]), "非法或倒退时钟未拒绝");
    }
    require(AndroidGpsProbe.budgetAllows(1, BEFORE, BEFORE + 1, BEFORE + 1), "同一单调时刻被误拒绝");
  }

  private void liveDiagnostics() throws Exception {
    JSONObject health = liveDiagnostics(7, 9, 120, 130);
    JSONObject rejected = AndroidGpsProbe.observe(() -> null, new Clock(), health);
    unavailable(rejected, "gps_cache_empty");
    require(rejected.getJSONObject("_live_source").getLong("callback_count") == 7,
        "拒绝回执未保留定位通道诊断");
    require(rejected.getJSONObject("_live_source").getInt("callbacks_during_observation") == 7,
        "观测窗口回调增量未保留");
    require(!rejected.toString().contains("latitude") && !rejected.toString().contains("source_token"),
        "定位通道诊断夹带源身份");
  }
}
