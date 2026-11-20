package com.jixiexiaoge.drivingassist.navdiag;

import android.app.Activity;
import android.app.Instrumentation;
import android.content.Context;
import android.location.Location;
import android.os.Bundle;
import android.os.SystemClock;

import org.json.JSONObject;

/** 前台 GPS 桥算法自测；只使用合成 Location，不读取真实位置也不启动服务。 */
public final class GpsBridgeInstrumentation extends Instrumentation {
  private interface Check { void run() throws Exception; }
  private static final long STARTED = 100_000_000_000L;
  private static final long NOW = 100_500_000_000L;
  private static final String BOOT = "01234567-89ab-cdef-0123-456789abcdef";
  private static final String SESSION = "0123456789abcdef0123456789abcdef";
  private int passed;
  private int failed;
  private final StringBuilder failures = new StringBuilder();

  @Override public void onCreate(Bundle arguments) { super.onCreate(arguments); start(); }

  @Override public void onStart() {
    check("gps_bridge_fresh_fix_roundtrip", this::fresh);
    check("gps_bridge_provider_rejected", this::provider);
    check("gps_bridge_mock_rejected", this::mock);
    check("gps_bridge_stale_rejected", this::stale);
    check("gps_bridge_future_rejected", this::future);
    check("gps_bridge_accuracy_rejected", this::accuracy);
    check("gps_bridge_coordinates_rejected", this::coordinates);
    check("gps_bridge_public_status_has_no_coordinates", this::privateOnly);
    check("gps_bridge_duplicate_fix_does_not_advance_source", this::duplicateFix);
    check("gps_bridge_provider_counters_are_reported", this::providerCounters);
    check("gps_bridge_limits_are_fixed", this::limits);
    check("gps_command_actions_map_to_matching_service_actions", this::commandActions);
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

  private static void require(boolean value, String message) {
    if (!value) throw new AssertionError(message);
  }

  private static Location location(long elapsedNs) {
    Location value = new Location("gps");
    value.setLatitude(22.5);
    value.setLongitude(11.25);
    value.setAccuracy(1);
    value.setElapsedRealtimeNanos(elapsedNs);
    return value;
  }

  private static GpsBridgeService.GpsBridgeSnapshot observe(Location value, String provider) throws Exception {
    return GpsBridgeService.GpsBridgeSnapshot.fromLocation(value, provider, SESSION, 7, 7, 4, 3,
        STARTED, NOW, BOOT);
  }

  private void fresh() throws Exception {
    GpsBridgeService.GpsBridgeSnapshot value = observe(location(NOW - 100_000_000L), "gps");
    require("observed".equals(value.status) && "fresh_gps".equals(value.reason), "新鲜GPS未被接受");
    JSONObject privateValue = value.privateJson();
    require(privateValue.getDouble("latitude") == 22.5 && privateValue.getDouble("longitude") == 11.25,
        "私有坐标丢失");
    require(!value.publicJson().has("latitude") && !value.publicJson().has("longitude"), "公开状态泄漏坐标");
    require(privateValue.getString("source_token").matches("[0-9a-f]{64}"), "源 token 格式错误");
    for (String key : new String[] {"gnss_hardware_authenticity_verified",
        "amap_native_pose_freshness_verified", "coordinate_datum_globally_verified",
        "control_input_allowed"}) require(!privateValue.getBoolean(key), "GP桥错误升级认证");
  }

  private void commandActions() {
    Context context = getTargetContext();
    require(GpsBridgeService.ACTION_START.equals(
        GpsBridgeCommandReceiver.serviceIntent(context, GpsBridgeCommandReceiver.ACTION_START).getAction()),
        "GPS启动广播被错误映射");
    require(GpsBridgeService.ACTION_STOP.equals(
        GpsBridgeCommandReceiver.serviceIntent(context, GpsBridgeCommandReceiver.ACTION_STOP).getAction()),
        "GPS停止广播被错误映射");
  }

  private void provider() throws Exception {
    for (String provider : new String[] {"fused", "network", "passive", null}) {
      require("gps_bridge_provider_invalid".equals(observe(location(NOW - 100_000_000L), provider).reason),
          "非GPS provider 未被拒绝");
    }
  }

  private void mock() throws Exception {
    Location value = location(NOW - 100_000_000L);
    java.lang.reflect.Method setter = Location.class.getDeclaredMethod("setIsFromMockProvider", boolean.class);
    setter.setAccessible(true);
    setter.invoke(value, true);
    require("gps_bridge_mock_rejected".equals(observe(value, "gps").reason), "Mock位置未被拒绝");
  }

  private void stale() throws Exception {
    require("gps_bridge_source_stale".equals(observe(location(NOW - GpsBridgeService.MAX_AGE_NS), "gps").reason),
        "两秒边界未拒绝");
  }

  private void future() throws Exception {
    for (long elapsed : new long[] {0, -1, NOW + 1}) {
      require("gps_bridge_source_time_invalid".equals(observe(location(elapsed), "gps").reason),
          "非法源时间未拒绝");
    }
  }

  private void accuracy() throws Exception {
    for (float accuracy : new float[] {0, -1, 26, Float.NaN, Float.POSITIVE_INFINITY}) {
      Location value = location(NOW - 100_000_000L);
      value.setAccuracy(accuracy);
      require("gps_bridge_accuracy_unusable".equals(observe(value, "gps").reason), "非法精度未拒绝");
    }
  }

  private void coordinates() throws Exception {
    for (double latitude : new double[] {91, -91, Double.NaN, Double.POSITIVE_INFINITY}) {
      Location value = location(NOW - 100_000_000L);
      value.setLatitude(latitude);
      require("gps_bridge_coordinates_invalid".equals(observe(value, "gps").reason), "非法坐标未拒绝");
    }
  }

  private void privateOnly() throws Exception {
    JSONObject value = observe(location(NOW - 100_000_000L), "gps").publicJson();
    require(!value.has("latitude") && !value.has("longitude") && !value.has("source_token"),
        "公开快照含坐标身份");
    require(value.getBoolean("service_thread_alive") && !value.getBoolean("control_input_allowed"),
        "公开状态字段错误");
  }

  private void duplicateFix() {
    Location value = location(NOW - 100_000_000L);
    require(GpsBridgeService.GpsBridgeSnapshot.isNewSource(value, 0), "首个源未被接受");
    require(!GpsBridgeService.GpsBridgeSnapshot.isNewSource(
        value, value.getElapsedRealtimeNanos()), "GPS与passive重复fix推进了源身份");
    require(GpsBridgeService.GpsBridgeSnapshot.isNewSource(
        location(NOW - 50_000_000L), value.getElapsedRealtimeNanos()), "新源时间被拒绝");
  }

  private void providerCounters() throws Exception {
    JSONObject value = observe(location(NOW - 100_000_000L), "gps").publicJson();
    require(value.getLong("callback_count") == 7 && value.getLong("gps_callback_count") == 4
            && value.getLong("passive_callback_count") == 3,
        "GPS/passive计数未正确报告");
  }

  private void limits() {
    require(GpsBridgeService.MAX_DURATION_MS == 7_200_000L, "两小时上限被改动");
    require(GpsBridgeService.MAX_AGE_NS == 2_000_000_000L, "源年龄上限被改动");
    require(GpsBridgeService.MAX_ACCURACY_M == 25.0, "精度上限被改动");
  }
}
