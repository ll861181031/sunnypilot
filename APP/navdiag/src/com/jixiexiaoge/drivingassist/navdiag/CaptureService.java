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

import org.json.JSONException;
import org.json.JSONObject;

/** 仅在手动开始后接收导航广播；不发广播、不联网、不查询车辆。 */
public final class CaptureService extends Service {
    public static final String ACTION_START = "com.jixiexiaoge.drivingassist.navdiag.START";
    public static final String ACTION_STOP = "com.jixiexiaoge.drivingassist.navdiag.STOP";
    public static final String ACTION_MARK = "com.jixiexiaoge.drivingassist.navdiag.MARK";
    private static final String CHANNEL_ID = "navigation_raw_capture";
    private static final int NOTIFICATION_ID = 1;
    static final String[] NAVIGATION_ACTIONS = {
        "AUTONAVI_STANDARD_BROADCAST_SEND",
        "AUTONAVI_STANDARD_BROADCAST_RECV",
        "AMAP_BROADCAST_SEND",
        "AUTONAVI_BROADCAST_SEND",
        "com.autonavi.minimap.broadcast",
        "com.autonavi.minimap.navigation.broadcast",
        "AMAP_NAVI_ACTION_UPDATE",
        "AMAP_NAVI_ACTION_TURN",
        "AMAP_NAVI_ACTION_ROUTE",
        "AMAP_NAVI_ACTION_LOCATION"
    };

    private static volatile String latestSnapshot = "{\"status\":\"idle\"}";
    private final Handler handler = new Handler(Looper.getMainLooper());
    private CaptureSession session;
    private BroadcastReceiver receiver;
    private boolean receiverRegistered;
    private PowerManager.WakeLock wakeLock;
    private NotificationManager notifications;
    private String startupError;

    private final Runnable poll = new Runnable() {
        @Override public void run() {
            if (session == null) return;
            if (!session.isAccepting()) unregisterCaptureReceiver();
            publishSnapshot();
            if (session.isStopped()) {
                releaseWakeLock();
                stopForeground(true);
                stopSelf();
                return;
            }
            notifications.notify(NOTIFICATION_ID, notification(session.isAccepting()
                    ? "正在接收导航广播，180秒自动结束" : "正在结束并保存记录"));
            handler.postDelayed(this, 500L);
        }
    };

    @Override public void onCreate() {
        super.onCreate();
        notifications = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        NotificationChannel channel = new NotificationChannel(CHANNEL_ID,
                "导航原始数据采集", NotificationManager.IMPORTANCE_LOW);
        channel.setDescription("手动采集期间显示状态，结束后自动关闭");
        channel.setShowBadge(false);
        channel.enableVibration(false);
        channel.setSound(null, null);
        notifications.createNotificationChannel(channel);
    }

    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        String action = intent == null ? null : intent.getAction();
        if (ACTION_START.equals(action)) {
            startCapture();
        } else if (ACTION_STOP.equals(action)) {
            if (session != null) {
                unregisterCaptureReceiver();
                session.stop("manual_stop");
                schedulePoll();
            } else stopSelf();
        } else if (ACTION_MARK.equals(action)) {
            if (session != null && session.isAccepting()) {
                String label = intent.getStringExtra("label");
                if (label != null && !label.trim().isEmpty()) {
                    session.mark(label.substring(0, Math.min(label.length(), 128)));
                    publishSnapshot();
                }
            } else if (session == null) stopSelf();
        } else if (session == null) {
            // 系统重建或未知命令都不自动恢复采集。
            stopSelf();
        }
        return START_NOT_STICKY;
    }

    private void startCapture() {
        if (session != null && !session.isStopped()) {
            publishSnapshot();
            return;
        }
        // 上一会话已结束但清理回调尚未运行时，先移除它的接收器和定时任务。
        handler.removeCallbacks(poll);
        unregisterCaptureReceiver();
        releaseWakeLock();
        startupError = null;
        startForeground(NOTIFICATION_ID, notification("正在准备180秒采集"));
        try {
            session = new CaptureSession(getApplicationContext());
            receiver = new BroadcastReceiver() {
                @Override public void onReceive(Context context, Intent intent) {
                    // 时间戳和原始Intent先交给记录器；此处不提取或规范化extras。
                    long elapsedNs = SystemClock.elapsedRealtimeNanos();
                    long wallMs = System.currentTimeMillis();
                    CaptureSession current = session;
                    if (current != null) current.recordIntent(intent, elapsedNs, wallMs);
                }
            };
            IntentFilter filter = new IntentFilter();
            for (String action : NAVIGATION_ACTIONS) filter.addAction(action);
            registerReceiver(receiver, filter);
            receiverRegistered = true;
            PowerManager power = (PowerManager) getSystemService(POWER_SERVICE);
            if (power != null) {
                wakeLock = power.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "navdiag:capture");
                wakeLock.acquire(185000L);
            }
            publishSnapshot();
            schedulePoll();
        } catch (Exception error) {
            unregisterCaptureReceiver();
            releaseWakeLock();
            if (session != null) session.stop("service_start_failed");
            startupError = error.getClass().getSimpleName() + ": " + error.getMessage();
            publishSnapshot();
            stopForeground(true);
            stopSelf();
        }
    }

    private void schedulePoll() {
        handler.removeCallbacks(poll);
        handler.post(poll);
    }

    private void publishSnapshot() {
        if (session == null && startupError == null) return;
        JSONObject state = session == null ? new JSONObject() : session.snapshot();
        if (startupError != null) {
            try {
                state.put("status", "failed");
                state.put("lastError", startupError);
            } catch (JSONException ignored) { }
        }
        latestSnapshot = state.toString();
    }

    public static JSONObject statusSnapshot() {
        try {
            return new JSONObject(latestSnapshot);
        } catch (JSONException ignored) {
            return new JSONObject();
        }
    }

    private Notification notification(String message) {
        Intent open = new Intent(this, MainActivity.class);
        PendingIntent openPending = PendingIntent.getActivity(this, 0, open,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        Intent end = new Intent(this, CaptureService.class).setAction(ACTION_STOP);
        PendingIntent stopPending = PendingIntent.getService(this, 1, end,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        return new Notification.Builder(this, CHANNEL_ID)
                .setSmallIcon(android.R.drawable.ic_menu_compass)
                .setContentTitle("CP导航原始采集")
                .setContentText(message)
                .setContentIntent(openPending)
                .setOngoing(true)
                .setOnlyAlertOnce(true)
                .addAction(new Notification.Action.Builder(
                        android.R.drawable.ic_menu_close_clear_cancel, "结束采集", stopPending).build())
                .build();
    }

    private void unregisterCaptureReceiver() {
        if (receiverRegistered) {
            try { unregisterReceiver(receiver); }
            catch (IllegalArgumentException ignored) { }
            receiverRegistered = false;
        }
    }

    private void releaseWakeLock() {
        if (wakeLock != null) {
            if (wakeLock.isHeld()) wakeLock.release();
            wakeLock = null;
        }
    }

    @Override public IBinder onBind(Intent intent) {
        return null;
    }

    @Override public void onDestroy() {
        handler.removeCallbacks(poll);
        unregisterCaptureReceiver();
        if (session != null && !session.isStopped()) session.stop("service_destroyed");
        publishSnapshot();
        releaseWakeLock();
        stopForeground(true);
        super.onDestroy();
    }
}
