package com.jixiexiaoge.drivingassist.navdiag;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONObject;

import java.io.File;
import java.util.Locale;

/** 独立采集界面；不会创建正式应用的网络或车辆控制组件。 */
public final class MainActivity extends Activity {
    private final Handler handler = new Handler(Looper.getMainLooper());
    private TextView statusView;
    private TextView pathView;
    private TextView bridgeView;
    private TextView gpsView;
    private TextView phoneCaptureView;
    private File exportDirectory;
    private boolean exporting;
    private boolean longPhoneCapture;

    private final Runnable refreshTask = new Runnable() {
        @Override public void run() {
            refreshStatus();
            handler.postDelayed(this, 500L);
        }
    };

    @Override public void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        createInterface();
        consumeCommand(getIntent());
    }

    @Override protected void onStart() {
        super.onStart();
        handler.removeCallbacks(refreshTask);
        handler.post(refreshTask);
    }

    @Override protected void onStop() {
        handler.removeCallbacks(refreshTask);
        super.onStop();
    }

    @Override protected void onDestroy() {
        handler.removeCallbacks(refreshTask);
        super.onDestroy();
    }

    @Override protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        consumeCommand(intent);
    }

    private void createInterface() {
        ScrollView scroll = new ScrollView(this);
        LinearLayout layout = new LinearLayout(this);
        layout.setOrientation(LinearLayout.VERTICAL);
        layout.setPadding(dp(20), dp(20), dp(20), dp(28));
        scroll.addView(layout);
        setContentView(scroll);

        TextView title = text("CP导航原始采集", 24);
        layout.addView(title);
        layout.addView(text("手机独立完整采集 · 0.7.4", 21));
        layout.addView(text("先在高德设置导航，停车时返回这里开始。首次允许本应用Root和精确定位；开始后可切回高德，无需USB或小主机。整趟采集最多2小时，每3分钟自动分段；停车后结束并导出一个ZIP。固定适配高德车机版9.5.0.600013。", 15));
        Button trip = new Button(this);
        trip.setText("开始整趟采集（最多2小时）");
        trip.setOnClickListener(view -> requestPhoneCapture(true));
        layout.addView(trip);
        LinearLayout phoneControls = new LinearLayout(this);
        phoneControls.setOrientation(LinearLayout.HORIZONTAL);
        addButton(phoneControls, "开始完整采集（180秒）", () -> requestPhoneCapture(false));
        addButton(phoneControls, "结束完整采集", () -> dispatchPhoneCapture(false));
        layout.addView(phoneControls);
        phoneCaptureView = text("尚未开始完整采集", 16);
        layout.addView(phoneCaptureView);
        Button export = new Button(this);
        export.setText("导出最近完成的完整采集（ZIP）");
        export.setOnClickListener(view -> choosePhoneExport());
        layout.addView(export);
        layout.addView(text("记录路线几何、路段/点索引、动作、GPS和原始时间；只保存手机原始证据，不代表小主机或GPU已验收。数据保留在本机，导出时可保存到你选择的位置。", 14));
        layout.addView(text("原有广播采集与诊断桥", 21));
        layout.addView(text("手动采集180秒，或随时结束。可切换到高德规划和操作导航；本应用只接收广播并保存到自身私有目录。", 16));
        layout.addView(text("无网络连接、无车辆控制。广播发送者身份标记为未验证，不根据action名称认定来自高德。", 14));

        LinearLayout controls = new LinearLayout(this);
        controls.setOrientation(LinearLayout.HORIZONTAL);
        addButton(controls, "开始180秒采集", () -> dispatchCommand("start", null));
        addButton(controls, "结束采集", () -> dispatchCommand("stop", null));
        layout.addView(controls);

        statusView = text("尚未开始采集", 17);
        statusView.setPadding(0, dp(14), 0, dp(10));
        layout.addView(statusView);
        layout.addView(text("下列按钮只写入时间标记，不操作导航或车辆。", 14));
        LinearLayout markers1 = new LinearLayout(this);
        markers1.setOrientation(LinearLayout.HORIZONTAL);
        addButton(markers1, "标记：规划路线", () -> dispatchCommand("mark", "规划路线"));
        addButton(markers1, "标记：开始导航", () -> dispatchCommand("mark", "开始导航"));
        layout.addView(markers1);
        LinearLayout markers2 = new LinearLayout(this);
        markers2.setOrientation(LinearLayout.HORIZONTAL);
        addButton(markers2, "标记：更换路线", () -> dispatchCommand("mark", "更换路线"));
        addButton(markers2, "标记：取消导航", () -> dispatchCommand("mark", "取消导航"));
        layout.addView(markers2);

        pathView = text("", 14);
        pathView.setTextIsSelectable(true);
        pathView.setPadding(0, dp(16), 0, 0);
        layout.addView(pathView);
        layout.addView(text("没有广播时计数为0，这是采集结果，不会补造路线。退出界面后，已开始的采集仍会在180秒上限自动结束。", 14));
        layout.addView(text("限时广播观测桥", 21));
        layout.addView(text("手动开启后最多运行2小时，只保存最新10001/10019原始广播，供小主机通过ADB读取。源时间戳不会随文件刷新续期；导航是否生效、当前路线与坐标系仍由小主机独立核验。", 14));
        LinearLayout bridgeControls = new LinearLayout(this);
        bridgeControls.setOrientation(LinearLayout.HORIZONTAL);
        addButton(bridgeControls, "开启观测桥（最多2小时）", () -> dispatchBridge(true));
        addButton(bridgeControls, "停止观测桥", () -> dispatchBridge(false));
        layout.addView(bridgeControls);
        bridgeView = text("尚未开启观测桥", 14);
        bridgeView.setTextIsSelectable(true);
        layout.addView(bridgeView);
        layout.addView(text("限时前台GPS桥", 21));
        layout.addView(text("手动开启并授权后，本应用以自身身份直接接收GPS_PROVIDER；只写私有文件并用通知栏持续显示，最多2小时。不接受网络、融合或被动定位，不联网、不控制车辆。", 14));
        LinearLayout gpsControls = new LinearLayout(this);
        gpsControls.setOrientation(LinearLayout.HORIZONTAL);
        addButton(gpsControls, "启动并授权GPS桥", () -> requestGpsBridge());
        addButton(gpsControls, "停止GPS桥", () -> dispatchGpsBridge(false));
        layout.addView(gpsControls);
        gpsView = text("尚未开启GPS桥", 14);
        gpsView.setTextIsSelectable(true);
        layout.addView(gpsView);
        refreshStatus();
    }

    private TextView text(String value, int size) {
        TextView view = new TextView(this);
        view.setText(value);
        view.setTextSize(size);
        view.setTextColor(Color.rgb(35, 42, 50));
        view.setPadding(0, dp(6), 0, dp(8));
        return view;
    }

    private void addButton(LinearLayout row, String label, final Runnable action) {
        Button button = new Button(this);
        button.setText(label);
        button.setAllCaps(false);
        button.setOnClickListener(view -> action.run());
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(0,
                LinearLayout.LayoutParams.WRAP_CONTENT, 1f);
        params.setMargins(dp(2), dp(4), dp(2), dp(4));
        row.addView(button, params);
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }

    /** ADB入口只操纵本应用：command=start/stop/mark，标记正文使用label。 */
    private void consumeCommand(Intent intent) {
        if (intent == null) return;
        String command;
        String label;
        try {
            command = intent.getStringExtra("command");
            label = intent.getStringExtra("label");
            intent.removeExtra("command");
            intent.removeExtra("label");
        } catch (RuntimeException error) {
            Toast.makeText(this, "命令参数格式错误，未执行", Toast.LENGTH_SHORT).show();
            return;
        }
        if ("phone_stop".equals(command)) dispatchPhoneCapture(false);
        else if ("bridge_start".equals(command)) dispatchBridge(true);
        else if ("bridge_stop".equals(command)) dispatchBridge(false);
        else if ("gps_start".equals(command)) requestGpsBridge();
        else if ("gps_stop".equals(command)) dispatchGpsBridge(false);
        else if (command != null) dispatchCommand(command, label);
    }

    private void dispatchBridge(boolean start) {
        Intent intent = new Intent(this, BridgeService.class)
                .setAction(start ? BridgeService.ACTION_START : BridgeService.ACTION_STOP);
        try {
            if (start) startForegroundService(intent);
            else startService(intent);
        } catch (RuntimeException error) {
            Toast.makeText(this, "无法执行观测桥命令：" + error.getClass().getSimpleName(), Toast.LENGTH_LONG).show();
        }
        refreshStatus();
    }

    private void requestGpsBridge() {
        if (Build.VERSION.SDK_INT >= 23
                && checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[] {
                Manifest.permission.ACCESS_FINE_LOCATION,
                Manifest.permission.ACCESS_COARSE_LOCATION
            }, 4101);
            return;
        }
        dispatchGpsBridge(true);
    }

    private void requestPhoneCapture(boolean longCapture) {
        longPhoneCapture = longCapture;
        if (checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[] {Manifest.permission.ACCESS_FINE_LOCATION,
                    Manifest.permission.ACCESS_COARSE_LOCATION}, 4102);
            return;
        }
        dispatchPhoneCapture(true);
    }

    private void dispatchPhoneCapture(boolean start) {
        try {
            Intent intent = new Intent(this, PhoneCaptureService.class)
                    .setAction(start ? PhoneCaptureService.START : PhoneCaptureService.STOP)
                    .putExtra(PhoneCaptureService.LONG_CAPTURE, longPhoneCapture);
            if (start) startForegroundService(intent);
            else startService(intent);
        } catch (RuntimeException error) {
            Toast.makeText(this, "无法执行完整采集：" + error.getClass().getSimpleName(), Toast.LENGTH_LONG).show();
        }
        refreshStatus();
    }

    private void choosePhoneExport() {
        if (exporting) return;
        String status = PhoneCaptureService.statusSnapshot().optString("status");
        if ("starting".equals(status) || "collecting".equals(status)) {
            Toast.makeText(this, "请先结束完整采集并等待保存", Toast.LENGTH_LONG).show();
            return;
        }
        exportDirectory = PhoneCaptureStore.latestCompleted(PhoneCaptureService.parent(this));
        if (exportDirectory == null) {
            Toast.makeText(this, "还没有正常保存的完整采集", Toast.LENGTH_LONG).show();
            return;
        }
        Intent intent = new Intent(Intent.ACTION_CREATE_DOCUMENT);
        intent.addCategory(Intent.CATEGORY_OPENABLE);
        intent.setType("application/zip");
        intent.putExtra(Intent.EXTRA_TITLE, "navdiag-phone-" + exportDirectory.getName() + ".zip");
        startActivityForResult(intent, 4201);
    }

    @Override protected void onActivityResult(int request, int result, Intent data) {
        super.onActivityResult(request, result, data);
        if (request != 4201 || result != RESULT_OK || data == null || data.getData() == null || exportDirectory == null) return;
        final File directory = exportDirectory;
        final android.net.Uri uri = data.getData();
        exporting = true;
        Toast.makeText(this, "正在校验并导出，请稍候", Toast.LENGTH_LONG).show();
        new Thread(() -> {
            String message = "导出完成；手机私有原始记录仍保留";
            try {
                java.io.OutputStream output = getContentResolver().openOutputStream(uri, "w");
                if (output == null) throw new java.io.IOException("export_stream_unavailable");
                try (java.io.OutputStream owned = output) { PhoneCaptureStore.exportZip(directory, owned); }
            } catch (Exception error) { message = "导出失败：" + error.getClass().getSimpleName() + "，原始记录保留"; }
            final String notification = message;
            runOnUiThread(() -> {
                exporting = false;
                Toast.makeText(this, notification, Toast.LENGTH_LONG).show();
            });
        }, "phone-capture-export").start();
    }

    @Override public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == 4102) {
            if (checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED)
                dispatchPhoneCapture(true);
            else Toast.makeText(this, "未授予精确定位，完整采集不会启动", Toast.LENGTH_LONG).show();
            return;
        }
        if (requestCode != 4101) return;
        if (checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED) {
            dispatchGpsBridge(true);
        } else {
            Toast.makeText(this, "未授予精确定位，GPS桥不会启动", Toast.LENGTH_LONG).show();
        }
    }

    private void dispatchGpsBridge(boolean start) {
        Intent intent = new Intent(this, GpsBridgeService.class)
                .setAction(start ? GpsBridgeService.ACTION_START : GpsBridgeService.ACTION_STOP);
        try {
            if (start) startForegroundService(intent);
            else startService(intent);
        } catch (RuntimeException error) {
            Toast.makeText(this, "无法执行GPS桥命令：" + error.getClass().getSimpleName(), Toast.LENGTH_LONG).show();
        }
        refreshStatus();
    }

    private void dispatchCommand(String command, String label) {
        Intent request = new Intent(this, CaptureService.class);
        switch (command) {
            case "start":
                request.setAction(CaptureService.ACTION_START);
                break;
            case "stop":
                request.setAction(CaptureService.ACTION_STOP);
                break;
            case "mark":
                if (label == null || label.trim().isEmpty()) {
                    Toast.makeText(this, "请提供标记内容", Toast.LENGTH_SHORT).show();
                    return;
                }
                request.setAction(CaptureService.ACTION_MARK);
                request.putExtra("label", label.substring(0, Math.min(label.length(), 128)));
                break;
            default:
                Toast.makeText(this, "未知命令；独立自测请使用CaptureInstrumentation", Toast.LENGTH_LONG).show();
                return;
        }
        try {
            if ("start".equals(command)) startForegroundService(request);
            else startService(request);
        } catch (RuntimeException error) {
            Toast.makeText(this, "无法执行采集命令：" + error.getClass().getSimpleName(), Toast.LENGTH_LONG).show();
        }
        refreshStatus();
    }

    private void refreshStatus() {
        if (statusView == null) return;
        if (phoneCaptureView != null) {
            JSONObject phone = PhoneCaptureService.statusSnapshot();
            String phase = phone.optString("status", "idle");
            String label = "starting".equals(phase) ? "正在准备，首次请允许Root"
                    : "collecting".equals(phase) ? "正在完整采集，可切回高德"
                    : "completed".equals(phase) ? "本轮已结束，原始记录已保存"
                    : "failed".equals(phase) ? "采集异常，请保留记录供排查" : "尚未开始完整采集";
            File saved = PhoneCaptureStore.latestCompleted(PhoneCaptureService.parent(this));
            phoneCaptureView.setText(label + "\n已用 " + phone.optLong("elapsed_ms") / 1000
                    + " / " + phone.optLong("max_duration_ms", 180000L) / 1000 + "秒；记录：" + phone.optLong("frames")
                    + "；分段：" + phone.optInt("segments")
                    + "\n原生观察：" + phone.optLong("native_observed_frames")
                    + "；新鲜GPS：" + phone.optLong("fresh_gps_frames")
                    + "\n广播：" + phone.optLong("received_broadcasts") + "；丢弃：" + phone.optLong("dropped_broadcasts")
                    + "\n原因：" + phone.optString("reason", "未开始")
                    + "\n最近已保存：" + (saved == null ? "无" : saved.getName()));
        }
        JSONObject state = CaptureService.statusSnapshot();
        String status = state.optString("status", "idle");
        String title;
        switch (status) {
            case "collecting": title = "正在采集"; break;
            case "stopping": title = "正在结束并写入文件"; break;
            case "completed": title = "采集已结束，记录已保存"; break;
            case "failed": title = "采集失败，请查看错误"; break;
            default: title = "尚未开始采集";
        }
        long elapsed = Math.max(0, state.optLong("elapsedMs", 0));
        long duration = state.optLong("maxDurationMs", 180000L);
        String counts = String.format(Locale.CHINA,
                "%s\n已用 %.1f 秒 / 上限 %.0f 秒\n收到广播：%d　已写记录：%d\n不完整：%d　截断：%d　丢弃：%d\n解码错误：%d　事件丢弃：%d\n已写 %.1f KiB",
                title, elapsed / 1000.0, duration / 1000.0,
                state.optLong("received"), state.optLong("written"),
                state.optLong("incomplete"), state.optLong("truncated"), state.optLong("dropped"),
                state.optLong("decodeErrors"), state.optLong("eventDropped"),
                state.optLong("bytesWritten") / 1024.0);
        String reason = state.optString("stopReason", "");
        if (!reason.isEmpty()) counts += "\n结束原因：" + reason;
        String error = state.optString("lastError", "");
        if (!error.isEmpty()) counts += "\n错误：" + error;
        statusView.setText(counts);
        String directory = state.optString("directory", "");
        if (directory.isEmpty()) directory = new File(getFilesDir(), "navdiag-captures").getAbsolutePath();
        pathView.setText("本应用私有记录目录（可通过ADB run-as导出）：\n" + directory);
        if (bridgeView != null) {
            JSONObject bridge = BridgeService.statusSnapshot();
            String bridgeStatus = bridge.optString("status", "idle");
            String label = "receiving".equals(bridgeStatus) ? "正在接收广播"
                    : "starting".equals(bridgeStatus) ? "正在准备"
                    : "stopped".equals(bridgeStatus) ? "观测桥已停止"
                    : "failed".equals(bridgeStatus) ? "观测桥失败"
                    : "尚未开启观测桥";
            bridgeView.setText(label + "\n上限：2小时；接收序号：" + bridge.optLong("sequence")
                    + "；已保存：" + bridge.optLong("written")
                    + "\n原因：" + bridge.optString("reason", "未开始")
                    + "\n最新文件：" + new File(getFilesDir(), "navdiag-bridge/latest.json").getAbsolutePath()
                    + "\nactive只表示接收器会话；valid只表示传输完整，不代表导航或车辆控制有效。");
        }
        if (gpsView != null) {
            JSONObject gps = GpsBridgeService.statusSnapshot();
            String gpsStatus = gps.optString("status", "idle");
            String label = "observed".equals(gpsStatus) ? "GPS桥正在接收新鲜位置"
                    : "starting".equals(gpsStatus) ? "GPS桥等待首个位置"
                    : "unavailable".equals(gpsStatus) ? "GPS桥已停止或当前不可用"
                    : "尚未开启GPS桥";
            gpsView.setText(label + "\n接收序号：" + gps.optLong("sequence")
                    + "；GPS回调：" + gps.optLong("callback_count")
                    + "\n原因：" + gps.optString("reason", "未开始")
                    + "\n最新私有文件：" + new File(getFilesDir(), "navdiag-gps/latest.json").getAbsolutePath()
                    + "\n服务只读取固定GPS provider；源年龄超过2秒或精度超过25米会被拒绝。");
        }
    }
}
