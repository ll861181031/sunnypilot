package com.jixiexiaoge.drivingassist.navdiag;

import android.content.Context;
import android.location.LocationListener;
import android.location.Location;
import android.location.LocationManager;
import android.os.HandlerThread;
import android.os.Looper;
import android.os.Process;
import android.os.SystemClock;
import android.system.Os;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.FileInputStream;
import java.io.InputStreamReader;
import java.lang.reflect.InvocationTargetException;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

/** 仅由 Root app_process 手动读取新鲜 GPS；缓存过期时发起一次有界实时更新。 */
public final class AndroidGpsProbe {
  static final long MAX_AGE_NS = 2_000_000_000L;
  static final long MAX_READ_NS = 1_000_000_000L;
  static final long MAX_LIVE_REQUEST_NS = 750_000_000L;
  static final double MAX_ACCURACY_M = 25.0;
  static final int MAX_COMMANDS = 144_000;
  static final long MAX_SESSION_NS = 2L * 60 * 60 * 1_000_000_000L;
  private static final String BOOT_PATH = "/proc/sys/kernel/random/boot_id";

  interface LocationSource {
    Location lastGps() throws Exception;
    default JSONObject diagnostics() { return null; }
  }
  interface GpsAccess {
    Location cached() throws Exception;
    Location requestOnce() throws Exception;
  }
  interface ClockSource {
    long elapsed();
    String boot() throws Exception;
  }

  private static final class Rejected extends Exception {
    final String reason;
    Rejected(String reason) { this.reason = reason; }
  }

  private AndroidGpsProbe() { }

  /**
   * 新鲜缓存直接复用；缺失或过期时才对固定 GPS provider 请求一次更新。
   * 请求受读取窗口约束，并且只接受 provider=gps 的真实回调；fused/network 不会被冒充。
   */
  static final class FreshGpsSource implements LocationSource {
    private final GpsAccess access;

    FreshGpsSource(LocationManager manager) {
      this.access = new GpsAccess() {
        @Override public Location cached() throws Exception {
          return manager.getLastKnownLocation(LocationManager.GPS_PROVIDER);
        }

        @Override public Location requestOnce() throws Exception {
          final CountDownLatch complete = new CountDownLatch(1);
          final AtomicReference<Location> received = new AtomicReference<>();
          LocationListener listener = new LocationListener() {
            @Override public void onLocationChanged(Location value) {
              if (value != null && LocationManager.GPS_PROVIDER.equals(value.getProvider())) {
                received.compareAndSet(null, new Location(value));
                complete.countDown();
              }
            }
            @Override public void onStatusChanged(String provider, int status, android.os.Bundle extras) { }
            @Override public void onProviderEnabled(String provider) { }
            @Override public void onProviderDisabled(String provider) { }
          };
          try {
            manager.requestLocationUpdates(LocationManager.GPS_PROVIDER, 0L, 0.0f, listener, Looper.myLooper());
            complete.await(MAX_LIVE_REQUEST_NS, TimeUnit.NANOSECONDS);
          } finally {
            manager.removeUpdates(listener);
          }
          return received.get();
        }
      };
    }

    FreshGpsSource(GpsAccess access) { this.access = access; }

    @Override public Location lastGps() throws Exception {
      Location cached = access.cached();
      long now = SystemClock.elapsedRealtimeNanos();
      if (cached != null) {
        long source = cached.getElapsedRealtimeNanos();
        if (source > 0 && source <= now && now - source < MAX_AGE_NS) return cached;
      }
      Location received = access.requestOnce();
      return received == null ? cached : received;
    }
  }

  /**
   * 在独立 Looper 上持续接收固定 GPS provider 的回调。
   *
   * 旧实现每轮都在主线程注册后阻塞等待，而 LocationManager 把回调投递到同一
   * Looper，主线程因此无法处理回调，只能反复退回可能已过期的缓存。独立 Looper
   * 保持回调可运行；OBSERVE 只读取最近回调和系统缓存中较新的一个。
   */
  static final class LiveGpsSource implements LocationSource, AutoCloseable {
    private final LocationManager manager;
    private final HandlerThread thread;
    private final Object lock = new Object();
    private final LocationListener gpsListener;
    private final LocationListener passiveListener;
    private Location latest;
    private Location lastRead;
    private boolean gpsRegistered;
    private boolean passiveRegistered;
    private long callbackCount;
    private long gpsCallbackCount;
    private long passiveCallbackCount;
    private long cacheReadCount;
    private long firstCacheReadNs;
    private long currentCallbacks;

    LiveGpsSource(final LocationManager manager) {
      this.manager = manager;
      this.thread = new HandlerThread("navdiag-gps-callback");
      this.thread.start();
      this.gpsListener = new LocationListener() {
        @Override public void onLocationChanged(Location value) {
          receive(value, false);
        }
        @Override public void onStatusChanged(String provider, int status, android.os.Bundle extras) { }
        @Override public void onProviderEnabled(String provider) { }
        @Override public void onProviderDisabled(String provider) { }
      };
      this.passiveListener = new LocationListener() {
        @Override public void onLocationChanged(Location value) {
          receive(value, true);
        }
        @Override public void onStatusChanged(String provider, int status, android.os.Bundle extras) { }
        @Override public void onProviderEnabled(String provider) { }
        @Override public void onProviderDisabled(String provider) { }
      };

      Throwable gpsFailure = null;
      Throwable passiveFailure = null;
      try {
        manager.requestLocationUpdates(LocationManager.GPS_PROVIDER, 0L, 0.0f,
            gpsListener, thread.getLooper());
        gpsRegistered = true;
      } catch (Throwable error) {
        gpsFailure = error;
      }
      try {
        // passive 不主动打开定位硬件，只接收系统或其他应用已经产生的更新。
        // 回调仍须保持 provider=gps；network/fused 位置不会进入缓存。
        manager.requestLocationUpdates(LocationManager.PASSIVE_PROVIDER, 0L, 0.0f,
            passiveListener, thread.getLooper());
        passiveRegistered = true;
      } catch (Throwable error) {
        passiveFailure = error;
      }
      if (!gpsRegistered && !passiveRegistered) {
        thread.quitSafely();
        if (passiveFailure instanceof SecurityException) throw (SecurityException) passiveFailure;
        if (gpsFailure instanceof SecurityException) throw (SecurityException) gpsFailure;
        throw new IllegalStateException("gps_callbacks_unavailable");
      }
    }

    private void receive(Location value, boolean passive) {
      if (value == null || !LocationManager.GPS_PROVIDER.equals(value.getProvider())) return;
      synchronized (lock) {
        callbackCount++;
        if (passive) passiveCallbackCount++;
        else gpsCallbackCount++;
        if (newer(value, latest)) latest = new Location(value);
        lock.notifyAll();
      }
    }

    static boolean newer(Location candidate, Location previous) {
      return candidate != null
          && (previous == null || candidate.getElapsedRealtimeNanos() > previous.getElapsedRealtimeNanos());
    }

    static Location newest(Location first, Location second) {
      if (!newer(first, second)) return second;
      return first;
    }

    @Override public Location lastGps() throws Exception {
      Location value;
      synchronized (lock) {
        cacheReadCount++;
        if (firstCacheReadNs == 0) firstCacheReadNs = SystemClock.elapsedRealtimeNanos();
        currentCallbacks = callbackCount;
        value = newest(manager.getLastKnownLocation(LocationManager.GPS_PROVIDER), latest);
        lastRead = value;
        long now = SystemClock.elapsedRealtimeNanos();
        if (value != null && value.getElapsedRealtimeNanos() > 0
            && value.getElapsedRealtimeNanos() <= now
            && now - value.getElapsedRealtimeNanos() < MAX_AGE_NS) return value;

        // 回调通常每秒到达；只在缓存过期时等待下一次回调，不阻塞回调 Looper。
        long started = SystemClock.elapsedRealtimeNanos();
        while (true) {
          long remaining = MAX_LIVE_REQUEST_NS - (SystemClock.elapsedRealtimeNanos() - started);
          if (remaining <= 0) break;
          try {
            lock.wait(Math.max(1L, Math.min(remaining / 1_000_000L, 100L)));
          } catch (InterruptedException error) {
            Thread.currentThread().interrupt();
            break;
          }
          value = newest(value, latest);
          lastRead = value;
          now = SystemClock.elapsedRealtimeNanos();
          if (value != null && value.getElapsedRealtimeNanos() > 0
              && value.getElapsedRealtimeNanos() <= now
              && now - value.getElapsedRealtimeNanos() < MAX_AGE_NS) break;
        }
      }
      return value;
    }

    private static long ageMs(Location value, long nowNs) {
      if (value == null) return -1;
      long sourceNs = value.getElapsedRealtimeNanos();
      return sourceNs > 0 && sourceNs <= nowNs ? (nowNs - sourceNs) / 1_000_000L : -1;
    }

    /** 只公开定位通道运行指标；不包含坐标、PathID、进程地址或系统异常正文。 */
    @Override public JSONObject diagnostics() {
      JSONObject result = new JSONObject();
      try {
        synchronized (lock) {
          long nowNs = SystemClock.elapsedRealtimeNanos();
          long observedNs = firstCacheReadNs == 0 ? 0 : nowNs - firstCacheReadNs;
          result.put("registration", gpsRegistered || passiveRegistered ? "request_accepted" : "not_registered")
              .put("gps_registration", gpsRegistered ? "request_accepted" : "not_registered")
              .put("passive_registration", passiveRegistered ? "request_accepted" : "not_registered")
              .put("callback_count", callbackCount)
              .put("gps_callback_count", gpsCallbackCount)
              .put("passive_callback_count", passiveCallbackCount)
              .put("cache_read_count", cacheReadCount)
              .put("callbacks_during_observation", Math.max(0, callbackCount - currentCallbacks))
              .put("observation_elapsed_ms", Math.max(0, observedNs / 1_000_000L))
              .put("callback_age_ms", ageMs(latest, nowNs))
              .put("last_read_age_ms", ageMs(lastRead, nowNs))
              .put("callback_thread_alive", thread.isAlive());
        }
      } catch (Exception ignored) { }
      return result;
    }

    @Override public void close() {
      if (gpsRegistered) manager.removeUpdates(gpsListener);
      if (passiveRegistered) manager.removeUpdates(passiveListener);
      gpsRegistered = false;
      passiveRegistered = false;
      thread.quitSafely();
    }
  }

  /** 固定采集预算；不依赖墙钟，不通过加法构造可能溢出的截止时间。 */
  static boolean budgetAllows(int commands, long startedNs, long previousNs, long nowNs) {
    return commands >= 0 && commands < MAX_COMMANDS && startedNs > 0
        && previousNs >= startedNs && nowNs >= previousNs && nowNs - startedNs < MAX_SESSION_NS;
  }

  private static Thread startBudgetWatchdog(final long startedNs) {
    Thread watchdog = new Thread(() -> {
      long previousNs = startedNs;
      while (true) {
        long nowNs = SystemClock.elapsedRealtimeNanos();
        if (!budgetAllows(0, startedNs, previousNs, nowNs)) {
          // 只结束本探针；stdin 空闲或半条命令阻塞也不能无限延长会话。
          System.exit(0);
          return;
        }
        previousNs = nowNs;
        long remainingNs = MAX_SESSION_NS - (nowNs - startedNs);
        try {
          Thread.sleep(Math.min(1000L, (remainingNs + 999_999L) / 1_000_000L));
        } catch (InterruptedException finished) {
          return;
        }
      }
    }, "gps-probe-budget");
    watchdog.setDaemon(true);
    watchdog.start();
    return watchdog;
  }

  static JSONObject unavailable(String reason) {
    return unavailable(reason, null);
  }

  static JSONObject unavailable(String reason, JSONObject liveSource) {
    JSONObject result = new JSONObject();
    try {
      result.put("schema_version", 1).put("source", "android_fine_last_known_gps")
          .put("status", "unavailable").put("reason", reason);
      if (liveSource != null) result.put("_live_source", liveSource);
      untrusted(result);
    } catch (Exception ignored) { }
    return result;
  }

  private static JSONObject securityUnavailable(SecurityException error) {
    JSONObject result = unavailable("gps_cache_security_rejected");
    try {
      String message = error.getMessage();
      if (message != null) result.put("_private_security_exception", message.substring(0, Math.min(1024, message.length())));
    } catch (Exception ignored) { }
    return result;
  }

  private static void untrusted(JSONObject result) throws Exception {
    result.put("gnss_hardware_authenticity_verified", false)
        .put("amap_native_pose_freshness_verified", false)
        .put("coordinate_datum_globally_verified", false)
        .put("control_input_allowed", false);
  }

  private static String token(String boot, long sourceNs) throws Exception {
    byte[] bytes = ("android-gps-location-v1\u0000" + boot + "\u0000" + sourceNs)
        .getBytes(StandardCharsets.US_ASCII);
    byte[] digest = MessageDigest.getInstance("SHA-256").digest(bytes);
    StringBuilder result = new StringBuilder();
    for (byte value : digest) result.append(String.format(java.util.Locale.ROOT, "%02x", value & 255));
    return result.toString();
  }

  /** 合成 Location 自测和真实读取共用；任何拒绝结果均不携带坐标。 */
  static JSONObject observe(LocationSource source, ClockSource clock) {
    return observe(source, clock, null);
  }

  /** liveSource 是可选的本进程诊断快照，不能改变固定新鲜度或安全判定。 */
  static JSONObject observe(LocationSource source, ClockSource clock, JSONObject liveSource) {
    JSONObject diagnostics = liveSource;
    try {
      String bootBefore = clock.boot();
      long before = clock.elapsed();
      Location value = source.lastGps();
      JSONObject sourceDiagnostics = source.diagnostics();
      if (sourceDiagnostics != null) diagnostics = sourceDiagnostics;
      Location location = value == null ? null : new Location(value);
      long after = clock.elapsed();
      String bootAfter = clock.boot();
      if (bootBefore == null || !bootBefore.matches("[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
          || !bootBefore.equals(bootAfter)) throw new Rejected("phone_boot_changed_or_invalid");
      if (before <= 0 || after < before || after - before > MAX_READ_NS) throw new Rejected("gps_read_clock_invalid");
      if (location == null) throw new Rejected("gps_cache_empty");
      if (!LocationManager.GPS_PROVIDER.equals(location.getProvider())) throw new Rejected("gps_provider_invalid");
      // 必须成功调用系统 mock 标志 API；方法不可用或异常时不将其默认为 false。
      boolean mock = location.isFromMockProvider();
      if (mock) throw new Rejected("mock_gps_rejected");
      long elapsedNs = location.getElapsedRealtimeNanos();
      if (elapsedNs <= 0 || elapsedNs > after) throw new Rejected("gps_source_time_invalid_or_future");
      long ageUpper = after - elapsedNs;
      if (ageUpper >= MAX_AGE_NS) throw new Rejected("gps_source_stale");
      float accuracy = location.getAccuracy();
      if (!location.hasAccuracy() || Float.isNaN(accuracy) || Float.isInfinite(accuracy)
          || accuracy <= 0 || accuracy > MAX_ACCURACY_M) throw new Rejected("gps_accuracy_unusable");
      double latitude = location.getLatitude();
      double longitude = location.getLongitude();
      if (Double.isNaN(latitude) || Double.isInfinite(latitude) || Math.abs(latitude) > 90
          || Double.isNaN(longitude) || Double.isInfinite(longitude) || Math.abs(longitude) > 180) {
        throw new Rejected("gps_coordinates_invalid");
      }
      JSONObject result = new JSONObject().put("schema_version", 1)
          .put("source", "android_fine_last_known_gps").put("status", "observed")
          .put("provider", location.getProvider()).put("latitude", latitude).put("longitude", longitude)
          .put("horizontal_accuracy_m", accuracy).put("elapsed_realtime_ns", elapsedNs)
          .put("is_from_mock_provider", mock).put("phone_before_ns", before).put("phone_after_ns", after)
          .put("phone_boot_id", bootBefore).put("source_token", token(bootBefore, elapsedNs))
          .put("age_lower_ns", Math.max(0, before - elapsedNs)).put("age_upper_ns", ageUpper)
          .put("source_elapsed_time_fresh", true);
      if (diagnostics != null) result.put("_live_source", diagnostics);
      untrusted(result);
      return result;
    } catch (Rejected error) {
      JSONObject sourceDiagnostics = source.diagnostics();
      if (sourceDiagnostics != null) diagnostics = sourceDiagnostics;
      return unavailable(error.reason, diagnostics);
    } catch (SecurityException error) {
      JSONObject result = securityUnavailable(error);
      JSONObject sourceDiagnostics = source.diagnostics();
      if (sourceDiagnostics != null) diagnostics = sourceDiagnostics;
      if (diagnostics != null) {
        try { result.put("_live_source", diagnostics); } catch (Exception ignored) { }
      }
      return result;
    } catch (Throwable error) {
      JSONObject sourceDiagnostics = source.diagnostics();
      if (sourceDiagnostics != null) diagnostics = sourceDiagnostics;
      return unavailable("gps_cache_read_failed", diagnostics);
    }
  }

  private static String readBoot() throws Exception {
    try (FileInputStream stream = new FileInputStream(BOOT_PATH)) {
      byte[] bytes = new byte[64];
      int size = stream.read(bytes);
      if (size < 1 || size == bytes.length || stream.read() != -1) throw new Rejected("phone_boot_changed_or_invalid");
      return new String(bytes, 0, size, StandardCharsets.US_ASCII).trim();
    }
  }

  private static LocationManager locationManager() throws Exception {
    if (Looper.myLooper() == null) Looper.prepareMainLooper();
    Class<?> activityThread = Class.forName("android.app.ActivityThread");
    Object thread = activityThread.getDeclaredMethod("systemMain").invoke(null);
    Object context = activityThread.getDeclaredMethod("getSystemContext").invoke(thread);
    if (!(context instanceof Context)) throw new Rejected("root_system_context_unavailable");
    Object service = ((Context) context).getSystemService(Context.LOCATION_SERVICE);
    if (!(service instanceof LocationManager)) throw new Rejected("gps_service_unavailable");
    return (LocationManager) service;
  }

  private static void useFixedSystemIdentity() throws Exception {
    // 框架拒绝 UID 0；只降低本探针的身份，与 systemContext 的固定 android 包身份一致。
    if (Os.getuid() != 0 || Os.geteuid() != 0) throw new Rejected("root_required");
    Os.setgid(1000);
    Os.setuid(1000);
    if (Os.getuid() != 1000 || Os.geteuid() != 1000 || Os.getgid() != 1000 || Os.getegid() != 1000) {
      throw new Rejected("fixed_system_identity_unavailable");
    }
  }

  static String readCommand(BufferedReader reader) throws Exception {
    StringBuilder value = new StringBuilder();
    for (int count = 0; count <= 32; count++) {
      int next = reader.read();
      if (next == -1) return null;
      if (next == '\n') return value.toString();
      if (next == '\r' || next < 32 || next > 126 || count == 32) throw new Rejected("unsupported_command");
      value.append((char) next);
    }
    throw new Rejected("unsupported_command");
  }

  /** stdout 仅是主机捕获的私有通道，调用方不得将正文打印到公开 CLI。 */
  public static void main(String[] args) {
    Thread watchdog = null;
    LiveGpsSource liveGps = null;
    try {
      if (args.length != 0) throw new Rejected("arguments_not_supported");
      if (Process.myUid() != 0) throw new Rejected("root_required");
      useFixedSystemIdentity();
      final long startedNs = SystemClock.elapsedRealtimeNanos();
      if (!budgetAllows(0, startedNs, startedNs, startedNs)) throw new Rejected("gps_probe_session_clock_invalid");
      watchdog = startBudgetWatchdog(startedNs);
      LocationManager manager = null;
      String startupFailure = null;
      try {
        manager = locationManager();
      } catch (InvocationTargetException error) {
        startupFailure = error.getCause() instanceof SecurityException
            ? "root_system_context_security_rejected" : "root_system_context_unavailable";
      } catch (SecurityException error) {
        startupFailure = "root_system_context_security_rejected";
      } catch (Throwable error) {
        startupFailure = "root_system_context_unavailable";
      }
      final LocationManager availableManager = manager;
      ClockSource clock = new ClockSource() {
        @Override public long elapsed() { return SystemClock.elapsedRealtimeNanos(); }
        @Override public String boot() throws Exception { return readBoot(); }
      };
      if (availableManager != null) {
        try {
          liveGps = new LiveGpsSource(availableManager);
        } catch (SecurityException error) {
          startupFailure = "root_system_context_security_rejected";
        } catch (Throwable error) {
          startupFailure = "gps_live_update_unavailable";
        }
      }
      final LiveGpsSource availableLiveGps = liveGps;
      try (BufferedReader reader = new BufferedReader(new InputStreamReader(System.in, StandardCharsets.US_ASCII))) {
        long previousNs = startedNs;
        for (int count = 0; count < MAX_COMMANDS; count++) {
          long beforeCommandNs = clock.elapsed();
          if (!budgetAllows(count, startedNs, previousNs, beforeCommandNs)) break;
          previousNs = beforeCommandNs;
          String command = readCommand(reader);
          if (command == null || command.equals("QUIT")) break;
          long afterCommandNs = clock.elapsed();
          if (!budgetAllows(count, startedNs, previousNs, afterCommandNs)) break;
          previousNs = afterCommandNs;
          JSONObject result = !command.equals("OBSERVE") ? unavailable("unsupported_command")
              : availableManager == null ? unavailable(startupFailure)
              : observe(availableLiveGps != null ? availableLiveGps : new FreshGpsSource(availableManager),
                  clock, null);
          System.out.println(result.toString());
          System.out.flush();
        }
      }
    } catch (Rejected error) {
      System.out.println(unavailable(error.reason).toString());
    } catch (Throwable error) {
      System.out.println(unavailable("root_system_context_unavailable").toString());
    } finally {
      if (liveGps != null) {
        try {
          liveGps.close();
        } catch (Throwable ignored) { }
      }
      if (watchdog != null) watchdog.interrupt();
      System.out.flush();
    }
  }
}
