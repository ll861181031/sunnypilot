package com.jixiexiaoge.drivingassist.navdiag;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.PowerManager;
import android.os.SystemClock;

import org.json.JSONObject;

import java.io.File;

/** 手动、限时的广播观测桥；没有网络或车辆控制入口。 */
public final class BridgeService extends Service {
  public static final String ACTION_START = "com.jixiexiaoge.drivingassist.navdiag.BRIDGE_START";
  public static final String ACTION_STOP = "com.jixiexiaoge.drivingassist.navdiag.BRIDGE_STOP";
  private static final String CHANNEL_ID = "navigation_broadcast_bridge";
  private static final int NOTIFICATION_ID = 2;
  private static volatile BridgeSession latestSession;
  private final Handler handler = new Handler(Looper.getMainLooper());
  private BridgeSession session;
  private BroadcastReceiver receiver;
  private boolean receiverRegistered;
  private PowerManager.WakeLock wakeLock;
  private boolean destroyed;
  private int generation;

  @Override public void onCreate() {
    super.onCreate();
    NotificationChannel channel = new NotificationChannel(CHANNEL_ID, "导航广播观测桥",
        NotificationManager.IMPORTANCE_LOW);
    channel.setDescription("手动开启，最多2小时，只保存最新导航广播");
    channel.setShowBadge(false);
    channel.enableVibration(false);
    channel.setSound(null, null);
    ((NotificationManager) getSystemService(NOTIFICATION_SERVICE)).createNotificationChannel(channel);
  }

  @Override public int onStartCommand(Intent intent, int flags, int startId) {
    String action = intent == null ? null : intent.getAction();
    if (ACTION_START.equals(action)) startBridge();
    else if (ACTION_STOP.equals(action)) stopBridge("manual_stop");
    else if (session == null) stopSelf();
    return START_NOT_STICKY;
  }

  private void startBridge() {
    if (session != null && !session.isStopped()) return;
    startForeground(NOTIFICATION_ID, notification());
    final int currentGeneration = ++generation;
    session = new BridgeSession(new File(getFilesDir(), "navdiag-bridge"), new BridgeSession.Config(),
        new BridgeSession.Listener() {
          @Override public void onInitialized() {
            handler.post(() -> { if (generation == currentGeneration) registerBridgeReceiver(); });
          }
          @Override public void onStopped() {
            handler.post(() -> { if (generation == currentGeneration) finishBridge(); });
          }
        });
    latestSession = session;
  }

  private void registerBridgeReceiver() {
    if (destroyed || session == null || !session.isAccepting()) return;
    try {
      receiver = new BroadcastReceiver() {
        @Override public void onReceive(Context context, Intent intent) {
          long elapsedNs = SystemClock.elapsedRealtimeNanos();
          long wallMs = System.currentTimeMillis();
          BridgeSession current = session;
          if (current != null) current.recordIntent(intent, elapsedNs, wallMs);
        }
      };
      IntentFilter filter = new IntentFilter("AUTONAVI_STANDARD_BROADCAST_SEND");
      registerReceiver(receiver, filter);
      receiverRegistered = true;
      PowerManager power = (PowerManager) getSystemService(POWER_SERVICE);
      if (power != null) {
        wakeLock = power.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "navdiag:bridge");
        wakeLock.acquire(BridgeSession.MAX_DURATION_MS + 5000L);
      }
      session.receiverRegistered();
    } catch (RuntimeException error) {
      unregisterBridgeReceiver();
      releaseWakeLock();
      session.registrationFailed();
    }
  }

  private void stopBridge(String reason) {
    unregisterBridgeReceiver();
    releaseWakeLock();
    if (session == null) {
      stopForeground(true);
      stopSelf();
    } else session.stop(reason);
  }

  private void finishBridge() {
    unregisterBridgeReceiver();
    releaseWakeLock();
    stopForeground(true);
    stopSelf();
  }

  private void unregisterBridgeReceiver() {
    if (!receiverRegistered) return;
    try { unregisterReceiver(receiver); }
    catch (IllegalArgumentException ignored) { }
    receiverRegistered = false;
    receiver = null;
  }

  private void releaseWakeLock() {
    if (wakeLock == null) return;
    if (wakeLock.isHeld()) wakeLock.release();
    wakeLock = null;
  }

  public static JSONObject statusSnapshot() {
    BridgeSession current = latestSession;
    return current == null ? new JSONObject() : current.snapshot();
  }

  private Notification notification() {
    PendingIntent open = PendingIntent.getActivity(this, 2, new Intent(this, MainActivity.class),
        PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
    PendingIntent stop = PendingIntent.getService(this, 3,
        new Intent(this, BridgeService.class).setAction(ACTION_STOP),
        PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
    return new Notification.Builder(this, CHANNEL_ID).setSmallIcon(android.R.drawable.ic_menu_compass)
        .setContentTitle("CP导航广播观测桥").setContentText("只读观测，最多2小时；可随时停止")
        .setContentIntent(open).setOngoing(true).setOnlyAlertOnce(true)
        .addAction(new Notification.Action.Builder(android.R.drawable.ic_menu_close_clear_cancel,
            "停止观测桥", stop).build()).build();
  }

  @Override public IBinder onBind(Intent intent) { return null; }

  @Override public void onDestroy() {
    destroyed = true;
    unregisterBridgeReceiver();
    releaseWakeLock();
    if (session != null && !session.isStopped()) session.stop("service_destroyed");
    stopForeground(true);
    super.onDestroy();
  }
}
