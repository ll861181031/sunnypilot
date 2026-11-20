package com.jixiexiaoge.drivingassist.navdiag;

import android.Manifest;
import android.app.*;
import android.content.*;
import android.content.pm.PackageManager;
import android.location.*;
import android.os.*;
import org.json.JSONObject;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.util.*;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicLong;

/** 手动手机独立采集；只记录原始来源，不启动主机链路或生成车辆控制输入。 */
public final class PhoneCaptureService extends Service {
  static final String START = "com.jixiexiaoge.drivingassist.navdiag.PHONE_CAPTURE_START";
  static final String STOP = "com.jixiexiaoge.drivingassist.navdiag.PHONE_CAPTURE_STOP";
  static final long MAX_DURATION_MS = 180000L;
  static final long LONG_DURATION_MS = 7200000L;
  static final String LONG_CAPTURE = "long_capture";
  private static final String CHANNEL = "phone_full_route_capture";
  private static volatile String latest = "{\"status\":\"idle\"}";
  private final Handler handler = new Handler(Looper.getMainLooper());
  private final ArrayBlockingQueue<BroadcastItem> queue = new ArrayBlockingQueue<>(32);
  private final AtomicLong received = new AtomicLong(), dropped = new AtomicLong();
  private final AtomicLong incomplete = new AtomicLong();
  private volatile boolean stopping;
  private volatile String stopReason;
  private volatile PhoneCaptureProbe.Client client;
  private volatile Location lastGps;
  private volatile long gpsCallbacks, gpsSequence;
  private volatile String gpsFailure = "waiting_for_gps";
  private long lastSourceTime;
  private boolean active, receiverRegistered, gpsRegistered;
  private BroadcastReceiver receiver;
  private LocationManager locationManager;
  private PowerManager.WakeLock wakeLock;
  private Thread worker;
  private long started;
  private long durationMs = MAX_DURATION_MS;
  private volatile int segments;
  private final Runnable timeout = () -> requestStop("duration_limit");
  private final LocationListener gpsListener = new LocationListener() {
    @Override public void onLocationChanged(Location location) {
      if (stopping || location == null || !"gps".equals(location.getProvider())) return;
      gpsCallbacks++;
      if (location.getElapsedRealtimeNanos() != lastSourceTime) {
        gpsSequence++; lastSourceTime = location.getElapsedRealtimeNanos();
      }
      lastGps = new Location(location); gpsFailure = "gps_received";
    }
    @Override public void onStatusChanged(String provider, int status, Bundle extras) { }
    @Override public void onProviderEnabled(String provider) { }
    @Override public void onProviderDisabled(String provider) {
      if ("gps".equals(provider)) { lastGps = null; gpsFailure = "gps_provider_disabled"; }
    }
  };

  static final class BroadcastItem {
    final Intent intent; final long sequence, elapsed, wall;
    BroadcastItem(Intent intent, long sequence) {
      this.intent = new Intent(intent); this.sequence = sequence;
      elapsed = SystemClock.elapsedRealtimeNanos(); wall = System.currentTimeMillis();
    }
  }

  @Override public void onCreate() {
    super.onCreate();
    NotificationChannel channel = new NotificationChannel(CHANNEL, "手机完整导航采集", NotificationManager.IMPORTANCE_LOW);
    channel.setSound(null, null); channel.enableVibration(false);
    ((NotificationManager) getSystemService(NOTIFICATION_SERVICE)).createNotificationChannel(channel);
  }

  @Override public int onStartCommand(Intent intent, int flags, int startId) {
    String action = intent == null ? null : intent.getAction();
    if (START.equals(action)) startCapture(intent.getBooleanExtra(LONG_CAPTURE, false));
    else if (STOP.equals(action)) { if (active) requestStop("manual_stop"); else stopSelf(); }
    else if (!active) stopSelf();
    return START_NOT_STICKY;
  }

  static File parent(Context context) { return new File(context.getFilesDir(), "phone-route-captures"); }

  static JSONObject statusSnapshot() {
    try { return new JSONObject(latest); }
    catch (Exception error) { return new JSONObject(); }
  }

  private void state(String status, String reason, long frames, long nativeFrames, long gpsFrames, String directory) {
    try {
      latest = new JSONObject().put("status", status).put("reason", reason)
          .put("frames", frames).put("native_observed_frames", nativeFrames).put("fresh_gps_frames", gpsFrames)
          .put("received_broadcasts", received.get()).put("dropped_broadcasts", dropped.get())
          .put("elapsed_ms", Math.max(0, SystemClock.elapsedRealtime() - started))
          .put("max_duration_ms", durationMs).put("segments", segments)
          .put("directory", directory).put("control_input_allowed", false).toString();
    } catch (Exception ignored) { }
  }

  private void startCapture(boolean longCapture) {
    if (active) return;
    durationMs = longCapture ? LONG_DURATION_MS : MAX_DURATION_MS; segments = 0;
    active = true; stopping = false; stopReason = "duration_limit";
    started = SystemClock.elapsedRealtime(); lastGps = null; lastSourceTime = 0;
    gpsCallbacks = 0; gpsSequence = 0; received.set(0); dropped.set(0); incomplete.set(0); queue.clear();
    startForeground(5, notification(longCapture ? "整趟采集，3分钟分段，最多2小时；停车后结束并保存"
        : "正在准备，请完成Root授权；180秒自动结束"));
    state("starting", "root_and_gps_initializing", 0, 0, 0, "");
    try {
      if (checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) != PackageManager.PERMISSION_GRANTED)
        throw new IOException("fine_location_permission_required");
      locationManager = (LocationManager) getSystemService(LOCATION_SERVICE);
      if (locationManager == null || !locationManager.isProviderEnabled("gps"))
        throw new IOException("gps_provider_disabled");
      receiver = new BroadcastReceiver() {
        @Override public void onReceive(Context context, Intent intent) {
          if (!stopping && !queue.offer(new BroadcastItem(intent, received.incrementAndGet()))) dropped.incrementAndGet();
        }
      };
      IntentFilter filter = new IntentFilter();
      for (String action : CaptureService.NAVIGATION_ACTIONS) filter.addAction(action);
      registerReceiver(receiver, filter); receiverRegistered = true;
      locationManager.requestLocationUpdates("gps", 250L, 0f, gpsListener, Looper.getMainLooper());
      gpsRegistered = true;
      try { locationManager.requestLocationUpdates("passive", 250L, 0f, gpsListener, Looper.getMainLooper()); }
      catch (RuntimeException ignored) { }
      PowerManager power = (PowerManager) getSystemService(POWER_SERVICE);
      if (power != null) {
        wakeLock = power.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "navdiag:phone-full-capture");
        wakeLock.acquire(durationMs + 10000);
      }
      handler.postDelayed(timeout, durationMs);
      worker = new Thread(this::collect, "phone-full-capture");
      worker.start();
    } catch (Exception error) {
      state("failed", reason(error), 0, 0, 0, "");
      releaseInputs(); active = false; stopForeground(true); stopSelf();
    }
  }

  private static String reason(Exception error) {
    String value = error.getMessage();
    return value != null && value.matches("[a-z0-9_]{1,100}") ? value : error.getClass().getSimpleName();
  }

  static boolean sameSelection(JSONObject before, JSONObject after) {
    return "observed".equals(before.optString("status")) && "observed".equals(after.optString("status"))
        && before.optBoolean("sample_consistent") && after.optBoolean("sample_consistent")
        && before.optString("path_identity_hash").matches("[0-9a-f]{64}")
        && before.optString("path_identity_hash").equals(after.optString("path_identity_hash"))
        && before.optString("process_epoch_hash").matches("[0-9a-f]{64}")
        && before.optString("process_epoch_hash").equals(after.optString("process_epoch_hash"));
  }

  private JSONObject gps(String session, String boot) throws Exception {
    long now = SystemClock.elapsedRealtimeNanos();
    Location location = lastGps;
    JSONObject value = GpsBridgeService.GpsBridgeSnapshot.fromLocation(location, "gps", session,
        gpsSequence, gpsCallbacks, gpsCallbacks, 0, started * 1000000L, now, boot).privateJson();
    value.put("captured_elapsed_realtime_ns", now).put("last_callback_reason", gpsFailure);
    if (location != null) {
      value.put("raw_source_elapsed_realtime_ns", location.getElapsedRealtimeNanos())
          .put("raw_source_wall_ms", location.getTime()).put("mock", location.isFromMockProvider());
      if (location.hasSpeed() && !Float.isNaN(location.getSpeed()) && !Float.isInfinite(location.getSpeed()))
        value.put("speed_mps", location.getSpeed());
      if (location.hasBearing() && !Float.isNaN(location.getBearing()) && !Float.isInfinite(location.getBearing()))
        value.put("bearing_deg", location.getBearing());
    }
    return value;
  }

  private void drain(PhoneCaptureStore store) throws Exception {
    BroadcastItem item;
    while ((item = queue.poll()) != null) {
      CaptureCodec.Result encoded = CaptureCodec.encodeIntent(item.intent, 16, 2 * 1024 * 1024);
      if (!encoded.complete) incomplete.incrementAndGet();
      store.broadcast(new JSONObject().put("sequence", item.sequence).put("received_elapsed_realtime_ns", item.elapsed)
          .put("received_wall_ms", item.wall).put("sender_verified", false).put("raw_intent", encoded.json));
    }
  }

  private void collect() {
    PhoneCaptureStore store = null;
    long frames = 0, nativeFrames = 0, gpsFrames = 0, pairedFrames = 0;
    String failure = null, directory = "", geometryRef = null, geometryIdentity = "";
    String geometryReason = "not_observed";
    long geometryAt = 0, lastGeometryRead = 0;
    boolean probeClosed = true;
    try {
      String boot;
      try (FileInputStream input = new FileInputStream("/proc/sys/kernel/random/boot_id")) {
        byte[] bytes = new byte[64]; int n = input.read(bytes);
        boot = new String(bytes, 0, Math.max(0, n), StandardCharsets.US_ASCII).trim();
      }
      if (!boot.matches("[0-9a-f-]{36}")) throw new IOException("phone_boot_identity_missing");
      JSONObject metadata = new JSONObject().put("app_version", getPackageManager().getPackageInfo(getPackageName(), 0).versionName)
          .put("amap_version", getPackageManager().getPackageInfo("com.autonavi.amapauto", 0).versionName)
          .put("source_package", "com.autonavi.amapauto")
          .put("app_apk_sha256", PhoneCaptureStore.hash(new File(getPackageCodePath())))
          .put("native_profile", NativeRouteProbe.PROFILE).put("phone_boot_id", boot)
          .put("device_model", Build.MODEL).put("android_sdk", Build.VERSION.SDK_INT)
          .put("started_elapsed_realtime_ns", started * 1000000L).put("started_wall_ms", System.currentTimeMillis())
          .put("max_duration_ms", durationMs).put("max_bytes", PhoneCaptureStore.MAX_BYTES)
          .put("requested_sample_interval_ms", 250).put("gps_capture_mode", "latest_fix_at_each_native_pair")
          .put("expected_gbl_sha256", NativeRouteProbe.GBL_SHA256)
          .put("expected_dice_sha256", NativeRouteProbe.DICE_SHA256);
      if (durationMs == LONG_DURATION_MS) metadata.put("segment_interval_ms", PhoneCaptureStore.SEGMENT_INTERVAL_MS);
      store = new PhoneCaptureStore(parent(this), metadata); directory = store.directory.getAbsolutePath();
      segments = store.segmentCount();
      String token = UUID.randomUUID().toString().replace("-", "");
      client = new PhoneCaptureProbe.Client(getPackageCodePath(), token, durationMs); probeClosed = false;
      client.ready();
      while (!stopping && SystemClock.elapsedRealtime() - started < durationMs) {
        long begin = SystemClock.elapsedRealtimeNanos();
        store.rotateIfDue(begin); segments = store.segmentCount();
        drain(store);
        JSONObject before = client.observe();
        String identity = before.optString("path_identity_hash") + ":" + before.optString("process_epoch_hash");
        if ("observed".equals(before.optString("status"))
            && (!identity.equals(geometryIdentity) || SystemClock.elapsedRealtime() - lastGeometryRead >= 2000)) {
          lastGeometryRead = SystemClock.elapsedRealtime();
          try {
            byte[] geometry = client.geometry();
            geometryRef = store.geometry(geometry);
            if (geometry.length < 112 || !Arrays.equals(Arrays.copyOf(geometry, 16), NativeRouteProbe.GEOMETRY_MAGIC))
              throw new IOException("geometry_header_invalid");
            geometryAt = SystemClock.elapsedRealtimeNanos();
            geometryIdentity = PhoneCaptureStore.hex(Arrays.copyOfRange(geometry, 48, 80)) + ":"
                + PhoneCaptureStore.hex(Arrays.copyOfRange(geometry, 80, 112));
            geometryReason = "raw_geometry_archived";
          } catch (IOException error) {
            if (reason(error).startsWith("archive_") || reason(error).equals("storage_low")) throw error;
            geometryRef = null; geometryIdentity = ""; geometryReason = reason(error);
          }
          before = client.observe();
        }
        JSONObject fix = gps(store.session, boot);
        JSONObject after = client.observe();
        long finish = SystemClock.elapsedRealtimeNanos();
        boolean same = sameSelection(before, after);
        String currentIdentity = before.optString("path_identity_hash") + ":" + before.optString("process_epoch_hash");
        String reference = same && currentIdentity.equals(geometryIdentity) ? geometryRef : null;
        JSONObject frame = new JSONObject().put("sequence", frames + 1)
            .put("sample_started_elapsed_realtime_ns", begin).put("sample_finished_elapsed_realtime_ns", finish)
            .put("native_before", before).put("gps", fix).put("native_after", after)
            .put("geometry_reference", reference == null ? JSONObject.NULL : reference)
            .put("geometry_archived_elapsed_realtime_ns", geometryAt).put("geometry_reason", geometryReason)
            .put("source_pair_same_selection", same).put("broadcast_binding_verified", false)
            .put("host_pipeline_executed", false).put("control_input_allowed", false);
        store.frame(frame); frames++;
        if ("observed".equals(before.optString("status"))) nativeFrames++;
        if ("observed".equals(fix.optString("status"))) gpsFrames++;
        if (same && "observed".equals(fix.optString("status")) && reference != null) pairedFrames++;
        state("collecting", before.optString("reason", "unknown"), frames, nativeFrames, gpsFrames, directory);
        long sleep = 250 - (SystemClock.elapsedRealtimeNanos() - begin) / 1000000L;
        if (sleep > 0) Thread.sleep(sleep);
      }
    } catch (Exception error) {
      if (!stopping) { failure = reason(error); stopReason = failure; }
    } finally {
      stopping = true;
      CountDownLatch inputsReleased = new CountDownLatch(1);
      handler.post(() -> { releaseInputs(); inputsReleased.countDown(); });
      try { if (!inputsReleased.await(2, TimeUnit.SECONDS)) failure = "input_cleanup_timeout"; }
      catch (InterruptedException error) { failure = "input_cleanup_interrupted"; }
      PhoneCaptureProbe.Client owned = client;
      if (owned != null) { owned.close(); probeClosed = owned.closedCleanly; client = null; }
      try {
        if (store != null) {
          drain(store);
          JSONObject summary = new JSONObject().put("stop_reason", stopReason)
              .put("failure", failure == null ? JSONObject.NULL : failure).put("frames", frames)
              .put("native_observed_frames", nativeFrames).put("fresh_gps_frames", gpsFrames)
              .put("paired_source_frames", pairedFrames).put("received_broadcasts", received.get())
              .put("dropped_broadcasts", dropped.get()).put("probe_closed_cleanly", probeClosed)
              .put("incomplete_broadcasts", incomplete.get())
              .put("raw_broadcast_lossless", dropped.get() == 0 && incomplete.get() == 0)
              .put("duration_ms", SystemClock.elapsedRealtime() - started)
              .put("dynamic_coverage_verified", false).put("broadcast_sender_verified", false);
          store.finish(summary);
        }
      } catch (Exception error) { failure = reason(error); }
      String status = failure == null && probeClosed ? "completed" : "failed";
      String finalReason = failure == null ? (probeClosed ? stopReason : "probe_cleanup_unconfirmed") : failure;
      state(status, finalReason, frames, nativeFrames, gpsFrames, directory);
      handler.post(() -> {
        handler.removeCallbacks(timeout); releaseInputs(); active = false;
        stopForeground(true); stopSelf();
      });
    }
  }

  private void requestStop(String reason) {
    if (!active) return;
    stopReason = reason; stopping = true;
    PhoneCaptureProbe.Client current = client;
    if (current != null) current.cancel();
    releaseInputs();
  }

  private void releaseInputs() {
    if (receiverRegistered) {
      try { unregisterReceiver(receiver); } catch (RuntimeException ignored) { }
      receiverRegistered = false;
    }
    if (gpsRegistered && locationManager != null) {
      try { locationManager.removeUpdates(gpsListener); } catch (RuntimeException ignored) { }
      gpsRegistered = false;
    }
    if (wakeLock != null) { if (wakeLock.isHeld()) wakeLock.release(); wakeLock = null; }
  }

  private Notification notification(String text) {
    PendingIntent open = PendingIntent.getActivity(this, 50, new Intent(this, MainActivity.class),
        PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
    PendingIntent stop = PendingIntent.getService(this, 51, new Intent(this, PhoneCaptureService.class).setAction(STOP),
        PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
    return new Notification.Builder(this, CHANNEL).setSmallIcon(android.R.drawable.ic_menu_compass)
        .setContentTitle("手机完整导航采集中").setContentText(text).setContentIntent(open)
        .setOngoing(true).setOnlyAlertOnce(true)
        .addAction(new Notification.Action.Builder(android.R.drawable.ic_menu_close_clear_cancel, "结束并保存", stop).build())
        .build();
  }

  @Override public IBinder onBind(Intent intent) { return null; }
  @Override public void onDestroy() {
    handler.removeCallbacks(timeout); requestStop("service_destroyed"); releaseInputs(); super.onDestroy();
  }
}
