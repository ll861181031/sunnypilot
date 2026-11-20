package com.jixiexiaoge.drivingassist.navdiag;

import android.app.Activity;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/**
 * 仅允许本机显式广播启停 GPS 桥，不切换当前前台应用。
 * 广播目标限定为本包，不注册 action filter，也不接受隐式广播。
 */
public final class GpsBridgeCommandReceiver extends BroadcastReceiver {
  static final String ACTION_START =
      "com.jixiexiaoge.drivingassist.navdiag.GPS_BRIDGE_COMMAND_START";
  static final String ACTION_STOP =
      "com.jixiexiaoge.drivingassist.navdiag.GPS_BRIDGE_COMMAND_STOP";
  static final String COMPONENT =
      "com.jixiexiaoge.drivingassist.navdiag/.GpsBridgeCommandReceiver";
  static final String EXTRA_RESULT = "gps_bridge_command_result";
  static final String RESULT_START_REQUESTED = "start_requested";
  static final String RESULT_STOP_REQUESTED = "stop_requested";

  static Intent serviceIntent(Context context, String command) {
    boolean start = ACTION_START.equals(command);
    return new Intent(context, GpsBridgeService.class)
        .setAction(start ? GpsBridgeService.ACTION_START : GpsBridgeService.ACTION_STOP);
  }

  @Override public void onReceive(Context context, Intent intent) {
    if (intent == null) return;
    setResultCode(Activity.RESULT_CANCELED);
    setResultData("rejected");
    if (!GpsBridgeCommandReceiver.class.getName()
        .equals(intent.getComponent() == null ? null : intent.getComponent().getClassName())
        || !getClass().getName().equals(intent.getComponent().getClassName())) return;
    String action = intent.getAction();
    boolean start = ACTION_START.equals(action);
    boolean stop = ACTION_STOP.equals(action);
    if (!start && !stop) return;
    Intent service = serviceIntent(context, action);
    try {
      if (start) context.startForegroundService(service);
      else context.startService(service);
      setResultCode(Activity.RESULT_OK);
      setResultData(start ? RESULT_START_REQUESTED : RESULT_STOP_REQUESTED);
    } catch (RuntimeException ignored) {
      // 主机随后通过私有状态和服务注册检查固定原因，不把异常正文暴露到公开日志。
    }
  }
}
