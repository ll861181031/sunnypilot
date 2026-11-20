package com.jixiexiaoge.drivingassist.navdiag;

import android.Manifest;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.location.Location;
import android.location.LocationListener;
import android.location.LocationManager;
import android.os.Bundle;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.IBinder;
import android.os.PowerManager;
import android.os.SystemClock;

import org.json.JSONObject;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.AtomicMoveNotSupportedException;
import java.security.MessageDigest;
import java.nio.file.Files;
import java.nio.file.StandardCopyOption;
import java.util.Locale;
import java.util.UUID;

/**
 * 手动、限时的前台 GPS 桥。普通应用 UID 同时接收 GPS 直连和 passive
 * 中由 GPS 产生的位置回调，原子写入本应用私有目录；不接受 fused/network，
 * 不联网、不产生车辆控制输入。
 */
public final class GpsBridgeService extends Service {
  public static final String ACTION_START = "com.jixiexiaoge.drivingassist.navdiag.GPS_BRIDGE_START";
  public static final String ACTION_STOP = "com.jixiexiaoge.drivingassist.navdiag.GPS_BRIDGE_STOP";
  static final long MAX_DURATION_MS = 2L * 60L * 60L * 1000L;
  static final long MAX_AGE_NS = 2_000_000_000L;
  static final double MAX_ACCURACY_M = 25.0;
  static final int MAX_PUBLIC_BYTES = 16 * 1024;
  private static final String CHANNEL_ID = "navigation_gps_bridge";
  private static final int NOTIFICATION_ID = 4;
  private static final String BOOT_PATH = "/proc/sys/kernel/random/boot_id";
  private static volatile GpsBridgeSnapshot latestSnapshot;

  static final class GpsBridgeSnapshot {
    final String status;
    final String reason;
    final String sessionId;
    final long sequence;
    final long callbackCount;
    final long gpsCallbackCount;
    final long passiveCallbackCount;
    final long serviceStartedNs;
    final long serviceUpdatedNs;
    final long sourceElapsedNs;
    final long callbackAgeNs;
    final double latitude;
    final double longitude;
    final double accuracyM;
    final String bootId;
    private final boolean coordinatesPresent;

    private GpsBridgeSnapshot(String status, String reason, String sessionId, long sequence,
                              long callbackCount, long gpsCallbackCount, long passiveCallbackCount,
                              long serviceStartedNs, long serviceUpdatedNs,
                              long sourceElapsedNs, long callbackAgeNs, double latitude,
                              double longitude, double accuracyM, String bootId,
                              boolean coordinatesPresent) {
      this.status = status;
      this.reason = reason;
      this.sessionId = sessionId;
      this.sequence = sequence;
      this.callbackCount = callbackCount;
      this.gpsCallbackCount = gpsCallbackCount;
      this.passiveCallbackCount = passiveCallbackCount;
      this.serviceStartedNs = serviceStartedNs;
      this.serviceUpdatedNs = serviceUpdatedNs;
      this.sourceElapsedNs = sourceElapsedNs;
      this.callbackAgeNs = callbackAgeNs;
      this.latitude = latitude;
      this.longitude = longitude;
      this.accuracyM = accuracyM;
      this.bootId = bootId;
      this.coordinatesPresent = coordinatesPresent;
    }

    static GpsBridgeSnapshot starting(String sessionId, long startedNs, String bootId) {
      return new GpsBridgeSnapshot("starting", "waiting_for_gps", sessionId, 0, 0, 0, 0,
          startedNs, startedNs, 0, -1, 0, 0, 0, bootId, false);
    }

    static GpsBridgeSnapshot unavailable(String reason, String sessionId, long sequence,
                                         long callbacks, long gpsCallbacks,
                                         long passiveCallbacks, long startedNs, long updatedNs,
                                         long sourceElapsedNs, long callbackAgeNs, String bootId) {
      return new GpsBridgeSnapshot("unavailable", reason, sessionId, sequence, callbacks,
          gpsCallbacks, passiveCallbacks,
          startedNs, updatedNs, sourceElapsedNs, callbackAgeNs, 0, 0, 0, bootId, false);
    }

    static GpsBridgeSnapshot fromLocation(Location location, String provider, String sessionId,
                                          long sequence, long callbacks, long gpsCallbacks,
                                          long passiveCallbacks, long startedNs, long nowNs,
                                          String bootId) throws Exception {
      if (!LocationManager.GPS_PROVIDER.equals(provider)) {
        return unavailable("gps_bridge_provider_invalid", sessionId, sequence, callbacks,
            gpsCallbacks, passiveCallbacks,
            startedNs, nowNs, 0, -1, bootId);
      }
      if (location == null) {
        return unavailable("gps_bridge_fix_missing", sessionId, sequence, callbacks,
            gpsCallbacks, passiveCallbacks,
            startedNs, nowNs, 0, -1, bootId);
      }
      boolean mock = location.isFromMockProvider();
      if (mock) {
        return unavailable("gps_bridge_mock_rejected", sessionId, sequence, callbacks,
            gpsCallbacks, passiveCallbacks,
            startedNs, nowNs, 0, -1, bootId);
      }
      long elapsed = location.getElapsedRealtimeNanos();
      if (elapsed <= 0 || elapsed > nowNs) {
        return unavailable("gps_bridge_source_time_invalid", sessionId, sequence, callbacks,
            gpsCallbacks, passiveCallbacks,
            startedNs, nowNs, elapsed, -1, bootId);
      }
      long age = nowNs - elapsed;
      if (age >= MAX_AGE_NS) {
        return unavailable("gps_bridge_source_stale", sessionId, sequence, callbacks,
            gpsCallbacks, passiveCallbacks,
            startedNs, nowNs, elapsed, age, bootId);
      }
      float accuracy = location.getAccuracy();
      if (!location.hasAccuracy() || Float.isNaN(accuracy) || Float.isInfinite(accuracy)
          || accuracy <= 0 || accuracy > MAX_ACCURACY_M) {
        return unavailable("gps_bridge_accuracy_unusable", sessionId, sequence, callbacks,
            gpsCallbacks, passiveCallbacks,
            startedNs, nowNs, elapsed, age, bootId);
      }
      double latitude = location.getLatitude();
      double longitude = location.getLongitude();
      if (Double.isNaN(latitude) || Double.isInfinite(latitude) || Math.abs(latitude) > 90
          || Double.isNaN(longitude) || Double.isInfinite(longitude) || Math.abs(longitude) > 180) {
        return unavailable("gps_bridge_coordinates_invalid", sessionId, sequence, callbacks,
            gpsCallbacks, passiveCallbacks,
            startedNs, nowNs, elapsed, age, bootId);
      }
      return new GpsBridgeSnapshot("observed", "fresh_gps", sessionId, sequence, callbacks,
          gpsCallbacks, passiveCallbacks,
          startedNs, nowNs, elapsed, age, latitude, longitude, accuracy, bootId, true);
    }

    static boolean isNewSource(Location location, long previousSourceElapsedNs) {
      long elapsed = location == null ? 0 : location.getElapsedRealtimeNanos();
      return elapsed > 0 && elapsed != previousSourceElapsedNs;
    }

    JSONObject privateJson() throws Exception {
      JSONObject value = publicJson();
      if (coordinatesPresent) {
        value.put("latitude", latitude).put("longitude", longitude).put("horizontal_accuracy_m", accuracyM)
            .put("source_token", token(bootId, sourceElapsedNs));
      }
      return value;
    }

    JSONObject publicJson() {
      JSONObject value = new JSONObject();
      try {
        value.put("schema_version", 1).put("source", "android_fine_live_gps_bridge")
            .put("status", status).put("reason", reason).put("session_id", sessionId)
            .put("sequence", sequence).put("callback_count", callbackCount)
            .put("gps_callback_count", gpsCallbackCount)
            .put("passive_callback_count", passiveCallbackCount).put("provider", "gps")
            .put("service_started_realtime_ns", serviceStartedNs)
            .put("service_updated_realtime_ns", serviceUpdatedNs)
            .put("source_elapsed_realtime_ns", sourceElapsedNs)
            .put("callback_age_ns", callbackAgeNs).put("service_thread_alive", true)
            .put("gnss_hardware_authenticity_verified", false)
            .put("amap_native_pose_freshness_verified", false)
            .put("coordinate_datum_globally_verified", false)
            .put("control_input_allowed", false);
      } catch (Exception ignored) { }
      return value;
    }
  }

  private final Handler handler = new Handler();
  private HandlerThread callbackThread;
  private LocationManager manager;
  private LocationListener gpsListener;
  private LocationListener passiveListener;
  private PowerManager.WakeLock wakeLock;
  private String sessionId;
  private String bootId;
  private long startedNs;
  private long callbacks;
  private long gpsCallbacks;
  private long passiveCallbacks;
  private boolean gpsRegistered;
  private boolean passiveRegistered;
  private long lastSourceElapsedNs;
  private GpsBridgeSnapshot latest;
  private boolean running;

  @Override public void onCreate() {
    super.onCreate();
    NotificationChannel channel = new NotificationChannel(CHANNEL_ID, "导航GPS桥",
        NotificationManager.IMPORTANCE_LOW);
    channel.setDescription("手动开启，最多2小时，只读取本机GPS");
    channel.setShowBadge(false);
    channel.enableVibration(false);
    channel.setSound(null, null);
    ((NotificationManager) getSystemService(NOTIFICATION_SERVICE)).createNotificationChannel(channel);
  }

  @Override public int onStartCommand(Intent intent, int flags, int startId) {
    String action = intent == null ? null : intent.getAction();
    if (ACTION_START.equals(action)) startBridge();
    else if (ACTION_STOP.equals(action)) stopBridge("manual_stop");
    else if (!running) stopSelf();
    return START_NOT_STICKY;
  }

  private void startBridge() {
    if (running) return;
    startForeground(NOTIFICATION_ID, notification());
    running = true;
    sessionId = UUID.randomUUID().toString().replace("-", "");
    startedNs = SystemClock.elapsedRealtimeNanos();
    callbacks = 0;
    gpsCallbacks = 0;
    passiveCallbacks = 0;
    gpsRegistered = false;
    passiveRegistered = false;
    lastSourceElapsedNs = 0;
    try {
      bootId = readBootId();
      latest = GpsBridgeSnapshot.starting(sessionId, startedNs, bootId);
    } catch (Exception error) {
      latest = GpsBridgeSnapshot.unavailable("gps_bridge_boot_id_unavailable", sessionId, 0, 0, 0, 0,
          startedNs, startedNs, 0, -1, "");
    }
    latestSnapshot = latest;
    writeSnapshot(latest);
    acquireWakeLock();
    if (checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) != PackageManager.PERMISSION_GRANTED) {
      fail("gps_bridge_permission_missing");
      return;
    }
    manager = (LocationManager) getSystemService(LOCATION_SERVICE);
    if (manager == null || !manager.isProviderEnabled(LocationManager.GPS_PROVIDER)) {
      fail("gps_bridge_provider_unavailable");
      return;
    }
    callbackThread = new HandlerThread("navdiag-live-gps");
    callbackThread.start();
    gpsListener = new LocationListener() {
      @Override public void onLocationChanged(Location location) {
        handleLocation(location, false);
      }
      @Override public void onStatusChanged(String provider, int status, Bundle extras) { }
      @Override public void onProviderEnabled(String provider) { }
      @Override public void onProviderDisabled(String provider) {
        if (LocationManager.GPS_PROVIDER.equals(provider)) fail("gps_bridge_provider_disabled");
      }
    };
    passiveListener = new LocationListener() {
      @Override public void onLocationChanged(Location location) {
        handleLocation(location, true);
      }
      @Override public void onStatusChanged(String provider, int status, Bundle extras) { }
      @Override public void onProviderEnabled(String provider) { }
      @Override public void onProviderDisabled(String provider) { }
    };
    try {
      manager.requestLocationUpdates(LocationManager.GPS_PROVIDER, 0L, 0.0f, gpsListener,
          callbackThread.getLooper());
      gpsRegistered = true;
    } catch (SecurityException error) {
      fail("gps_bridge_permission_rejected");
      return;
    } catch (RuntimeException error) {
      fail("gps_bridge_registration_failed");
      return;
    }
    try {
      // passive 不主动打开定位硬件；只有高德等应用已经产生 GPS fix 时才回调。
      manager.requestLocationUpdates(LocationManager.PASSIVE_PROVIDER, 0L, 0.0f, passiveListener,
          callbackThread.getLooper());
      passiveRegistered = true;
    } catch (RuntimeException ignored) {
      // GPS 直连已经成功时，passive 注册失败不降低现有可用性。
    }
    handler.postDelayed(this::durationExpired, MAX_DURATION_MS);
  }

  private void handleLocation(Location location, boolean passive) {
    if (!running) return;
    // passive 也会发送 network/fused 位置；必须在计数和写文件前拒绝。
    if (location == null || !LocationManager.GPS_PROVIDER.equals(location.getProvider())) return;
    long sourceElapsedNs = location.getElapsedRealtimeNanos();
    // GPS 直连与 passive 可能交付同一个 fix。重复回调只保留诊断，不推进源身份。
    if (!GpsBridgeSnapshot.isNewSource(location, lastSourceElapsedNs)) return;
    lastSourceElapsedNs = sourceElapsedNs;
    callbacks++;
    if (passive) passiveCallbacks++;
    else gpsCallbacks++;
    long nowNs = SystemClock.elapsedRealtimeNanos();
    GpsBridgeSnapshot next;
    try {
      String currentBoot = readBootId();
      if (!currentBoot.equals(bootId)) {
        next = GpsBridgeSnapshot.unavailable("gps_bridge_boot_changed", sessionId,
            callbacks, callbacks, gpsCallbacks, passiveCallbacks, startedNs, nowNs, 0, -1, "");
      } else {
        next = GpsBridgeSnapshot.fromLocation(location, location.getProvider(), sessionId,
            callbacks, callbacks, gpsCallbacks, passiveCallbacks, startedNs, nowNs, bootId);
      }
    } catch (Exception error) {
      next = GpsBridgeSnapshot.unavailable("gps_bridge_record_failed", sessionId,
          callbacks, callbacks, gpsCallbacks, passiveCallbacks, startedNs, nowNs, 0, -1, bootId);
    }
    latest = next;
    latestSnapshot = next;
    writeSnapshot(next);
  }

  private void fail(String reason) {
    latest = GpsBridgeSnapshot.unavailable(reason, sessionId, callbacks, callbacks,
        gpsCallbacks, passiveCallbacks, startedNs, SystemClock.elapsedRealtimeNanos(), 0, -1,
        bootId == null ? "" : bootId);
    latestSnapshot = latest;
    writeSnapshot(latest);
    stopInput();
    stopForeground(true);
    stopSelf();
  }

  private void durationExpired() {
    if (running) stopBridge("duration_limit");
  }

  private void stopBridge(String reason) {
    if (!running) {
      stopForeground(true);
      stopSelf();
      return;
    }
    long now = SystemClock.elapsedRealtimeNanos();
    GpsBridgeSnapshot stopped = GpsBridgeSnapshot.unavailable(reason, sessionId,
        callbacks, callbacks,
        gpsCallbacks, passiveCallbacks,
        startedNs, now, latest == null ? 0 : latest.sourceElapsedNs,
        latest == null ? -1 : latest.callbackAgeNs, bootId == null ? "" : bootId);
    latest = stopped;
    latestSnapshot = stopped;
    writeSnapshot(stopped);
    stopInput();
    stopForeground(true);
    stopSelf();
  }

  private void stopInput() {
    running = false;
    handler.removeCallbacksAndMessages(null);
    if (manager != null && gpsListener != null && gpsRegistered) {
      try { manager.removeUpdates(gpsListener); }
      catch (RuntimeException ignored) { }
    }
    if (manager != null && passiveListener != null && passiveRegistered) {
      try { manager.removeUpdates(passiveListener); }
      catch (RuntimeException ignored) { }
    }
    gpsRegistered = false;
    passiveRegistered = false;
    gpsListener = null;
    passiveListener = null;
    if (callbackThread != null) callbackThread.quitSafely();
    callbackThread = null;
    releaseWakeLock();
  }

  private void acquireWakeLock() {
    PowerManager power = (PowerManager) getSystemService(POWER_SERVICE);
    if (power == null) return;
    wakeLock = power.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "navdiag:gps-bridge");
    wakeLock.acquire(MAX_DURATION_MS + 5_000L);
  }

  private void releaseWakeLock() {
    if (wakeLock == null) return;
    if (wakeLock.isHeld()) wakeLock.release();
    wakeLock = null;
  }

  private void writeSnapshot(GpsBridgeSnapshot snapshot) {
    if (snapshot == null) return;
    File directory = new File(getFilesDir(), "navdiag-gps");
    File output = new File(directory, "latest.json");
    File temporary = new File(directory, "latest.json.tmp");
    FileOutputStream stream = null;
    try {
      if (!directory.isDirectory() && !directory.mkdirs()) throw new SecurityException("directory");
      byte[] payload = snapshot.privateJson().toString().getBytes(StandardCharsets.UTF_8);
      if (payload.length > MAX_PUBLIC_BYTES) throw new SecurityException("oversize");
      stream = new FileOutputStream(temporary, false);
      stream.write(payload);
      stream.flush();
      stream.getFD().sync();
      stream.close();
      stream = null;
      try {
        Files.move(temporary.toPath(), output.toPath(), StandardCopyOption.ATOMIC_MOVE,
            StandardCopyOption.REPLACE_EXISTING);
      } catch (AtomicMoveNotSupportedException unsupported) {
        // 某些 Android 文件系统不支持 renameat2；完整写入并 fsync 后允许非原子替换。
        Files.move(temporary.toPath(), output.toPath(), StandardCopyOption.REPLACE_EXISTING);
      }
    } catch (Exception error) {
      // 写失败不会发布坐标；状态仍由进程内诊断与停止后的 0600 记录区分。
    } finally {
      if (stream != null) {
        try { stream.close(); }
        catch (Exception ignored) { }
      }
      if (temporary.exists()) temporary.delete();
    }
  }

  private static String readBootId() throws Exception {
    FileInputStream stream = new FileInputStream(BOOT_PATH);
    try {
      byte[] bytes = new byte[64];
      int size = stream.read(bytes);
      if (size < 1 || size == bytes.length || stream.read() != -1) {
        throw new IllegalStateException("boot id invalid");
      }
      String value = new String(bytes, 0, size, StandardCharsets.US_ASCII).trim();
      if (!value.matches("[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")) {
        throw new IllegalStateException("boot id invalid");
      }
      return value;
    } finally {
      stream.close();
    }
  }

  private static String token(String bootId, long sourceElapsedNs) throws Exception {
    MessageDigest digest = MessageDigest.getInstance("SHA-256");
    byte[] value = digest.digest(("android-gps-location-v1\u0000" + bootId + "\u0000" + sourceElapsedNs)
        .getBytes(StandardCharsets.US_ASCII));
    StringBuilder result = new StringBuilder();
    for (byte item : value) result.append(String.format(Locale.ROOT, "%02x", item & 255));
    return result.toString();
  }

  public static JSONObject statusSnapshot() {
    GpsBridgeSnapshot current = latestSnapshot;
    return current == null ? new JSONObject() : current.publicJson();
  }

  private Notification notification() {
    PendingIntent open = PendingIntent.getActivity(this, 4, new Intent(this, MainActivity.class),
        PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
    PendingIntent stop = PendingIntent.getService(this, 5,
        new Intent(this, GpsBridgeService.class).setAction(ACTION_STOP),
        PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
    return new Notification.Builder(this, CHANNEL_ID)
        .setSmallIcon(android.R.drawable.ic_menu_mylocation)
        .setContentTitle("CP导航GPS桥")
        .setContentText("只读本机GPS，最多2小时；可随时停止")
        .setContentIntent(open).setOngoing(true).setOnlyAlertOnce(true)
        .addAction(new Notification.Action.Builder(android.R.drawable.ic_menu_close_clear_cancel,
            "停止GPS桥", stop).build()).build();
  }

  @Override public IBinder onBind(Intent intent) { return null; }

  @Override public void onDestroy() {
    if (running) stopBridge("service_destroyed");
    else stopInput();
    stopForeground(true);
    super.onDestroy();
  }
}
