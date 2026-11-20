package com.jixiexiaoge.drivingassist.navdiag;

import android.os.SystemClock;
import org.json.JSONObject;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.TimeUnit;

/** 仅供手机手动采集的 Root 只读子进程；管道断开退出，另有硬性寿命上限。 */
public final class PhoneCaptureProbe {
  static final long MAX_LIFETIME_MS = 185000L;
  static final long LONG_LIFETIME_MS = 7205000L;
  static final String MAIN = "com.jixiexiaoge.drivingassist.navdiag.PhoneCaptureProbe";

  static final class GeometryRejection {
    private JSONObject pending;

    void record(NativeRouteProbe.GeometrySnapshot geometry) {
      pending = geometry != null && geometry.status.equals("unavailable") ? geometry.diagnosis : null;
    }

    void attach(JSONObject observation) throws org.json.JSONException {
      if (pending != null) observation.put("_geometry_rejection", pending);
      pending = null;
    }
  }

  public static void main(String[] args) {
    String token = System.getenv("NAVDIAG_PHONE_CAPTURE");
    if (args.length > 1 || (args.length == 1 && !args[0].equals("long"))
        || token == null || !token.matches("[0-9a-f]{32}")
        || android.os.Process.myUid() != 0) return;
    final long lifetimeMs = args.length == 1 ? LONG_LIFETIME_MS : MAX_LIFETIME_MS;
    Thread watchdog = new Thread(() -> {
      SystemClock.sleep(lifetimeMs);
      System.exit(73);
    }, "phone-probe-lifetime");
    watchdog.setDaemon(true); watchdog.start();
    System.out.println("NAVDIAG_PHONE_READY " + token + " " + android.os.Process.myPid());
    System.out.flush();
    NativeRouteProbe.LiveSource source = null;
    String sourceFailure = "provider_not_ready";
    long retryAfter = 0;
    GeometryRejection geometryRejection = new GeometryRejection();
    try {
      String command;
      while ((command = NativeRouteProbe.command(System.in)) != null) {
        if (command.equals("QUIT")) break;
        if (!command.equals("OBSERVE_GEOMETRY") && !command.equals("OBSERVE_POSITION_NAVI_INFO")) break;
        if (source == null && SystemClock.elapsedRealtime() >= retryAfter) {
          try { source = new NativeRouteProbe.LiveSource(); }
          catch (Exception error) {
            sourceFailure = error instanceof NativeRouteProbe.ProbeException ? error.getMessage() : "live_source_unavailable";
            retryAfter = SystemClock.elapsedRealtime() + 2000;
          }
        }
        if (command.equals("OBSERVE_GEOMETRY")) {
          NativeRouteProbe.GeometrySnapshot geometry = source == null ? null : source.observeGeometry();
          geometryRejection.record(geometry);
          if (geometry != null && geometry.status.equals("observed")) {
            System.out.print("NAVDIAG_GEOMETRY_V2 " + geometry.payload.length + " " + PhoneCaptureStore.sha(geometry.payload) + "\n");
            System.out.write(geometry.payload); System.out.print("\n");
          } else System.out.print("NAVDIAG_GEOMETRY_V2 ERROR " + (geometry == null ? sourceFailure : geometry.reason) + "\n");
        } else {
          JSONObject value = source == null
              ? new JSONObject().put("status", "unavailable").put("reason", sourceFailure)
                  .put("control_input_allowed", false)
              : source.observe(true, true);
          geometryRejection.attach(value);
          System.out.println(value.toString());
          if (source != null && ArraysForReset.shouldReset(value.optString("reason"))) {
            source.close(); source = null; retryAfter = SystemClock.elapsedRealtime() + 2000;
          }
        }
        System.out.flush();
      }
    } catch (Exception ignored) { }
    finally {
      if (source != null) try { source.close(); } catch (IOException ignored) { }
      System.out.println("NAVDIAG_PHONE_CLOSED " + token);
      System.out.flush();
    }
  }

  private static final class ArraysForReset {
    static boolean shouldReset(String reason) {
      return reason.equals("process_epoch_changed") || reason.equals("library_mapping_changed")
          || reason.equals("provider_identity_unavailable") || reason.equals("live_source_unavailable");
    }
  }

  static final class Client implements AutoCloseable {
    private final Process process;
    private final InputStream input;
    private final OutputStream output;
    private final String token;
    private volatile boolean cancelled;
    boolean closedCleanly;

    Client(String apk, String token) throws Exception {
      this(apk, token, 180000L);
    }

    Client(String apk, String token, long durationMs) throws Exception {
      if (durationMs != 180000L && durationMs != 7200000L) throw new IOException("probe_duration_invalid");
      if (!token.matches("[0-9a-f]{32}") || !apk.matches("/data/app/[A-Za-z0-9_+./=~-]+/base\\.apk"))
        throw new IOException("probe_launch_identity_invalid");
      this.token = token;
      String script = "export CLASSPATH='" + apk + "'; export NAVDIAG_PHONE_CAPTURE=" + token
          + "; exec /system/bin/app_process /system/bin " + MAIN + (durationMs == 7200000L ? " long" : "");
      process = new ProcessBuilder("su", "-c", script).redirectError(new File("/dev/null")).start();
      input = process.getInputStream(); output = process.getOutputStream();
    }

    void ready() throws Exception {
      String header = line(200, SystemClock.elapsedRealtime() + 30000);
      if (!header.matches("NAVDIAG_PHONE_READY " + token + " [1-9][0-9]{0,9}"))
        throw new IOException("root_probe_not_authorized");
    }

    private long deadline() { return SystemClock.elapsedRealtime() + 5000; }

    private int read(long until) throws Exception {
      while (input.available() == 0) {
        if (cancelled || Thread.currentThread().isInterrupted()) throw new IOException("probe_cancelled");
        if (SystemClock.elapsedRealtime() >= until) throw new IOException("probe_response_timeout");
        if (!process.isAlive()) {
          int value = input.read();
          if (value < 0) throw new EOFException(process.exitValue() == 13 ? "root_permission_denied" : "root_probe_exited");
          return value;
        }
        Thread.sleep(5);
      }
      int value = input.read();
      if (value < 0) throw new EOFException("root_probe_exited");
      return value;
    }

    private String line(int maximum, long until) throws Exception {
      ByteArrayOutputStream bytes = new ByteArrayOutputStream(); int b;
      while ((b = read(until)) != '\n') {
        if (bytes.size() >= maximum) throw new IOException("probe_result_size_limit");
        bytes.write(b);
      }
      return new String(bytes.toByteArray(), StandardCharsets.UTF_8);
    }

    private void command(String value) throws Exception {
      output.write((value + "\n").getBytes(StandardCharsets.US_ASCII)); output.flush();
    }

    JSONObject observe() throws Exception {
      command("OBSERVE_POSITION_NAVI_INFO");
      return new JSONObject(line(16384, deadline()));
    }

    byte[] geometry() throws Exception {
      command("OBSERVE_GEOMETRY"); long until = deadline();
      String header = line(200, until);
      if (header.startsWith("NAVDIAG_GEOMETRY_V2 ERROR "))
        throw new IOException(header.substring("NAVDIAG_GEOMETRY_V2 ERROR ".length()));
      String[] parts = header.split(" ");
      if (parts.length != 3 || !parts[0].equals("NAVDIAG_GEOMETRY_V2")
          || !parts[1].matches("[1-9][0-9]{0,7}") || !parts[2].matches("[0-9a-f]{64}"))
        throw new IOException("geometry_header_invalid");
      int size = Integer.parseInt(parts[1]);
      if (size > NativeRouteProbe.GEOMETRY_MAX_PAYLOAD_BYTES) throw new IOException("geometry_size_invalid");
      byte[] bytes = new byte[size];
      int offset = 0;
      while (offset < size) {
        bytes[offset++] = (byte) read(until);
        int available = Math.min(size - offset, input.available());
        if (available > 0) {
          int n = input.read(bytes, offset, available);
          if (n < 0) throw new EOFException("geometry_truncated");
          offset += n;
        }
      }
      if (read(until) != '\n' || !PhoneCaptureStore.sha(bytes).equals(parts[2]))
        throw new IOException("geometry_hash_mismatch");
      return bytes;
    }

    void cancel() { cancelled = true; }

    @Override public void close() {
      Thread.interrupted();
      cancelled = false;
      try {
        command("QUIT");
        byte[] marker = ("\nNAVDIAG_PHONE_CLOSED " + token + "\n").getBytes(StandardCharsets.US_ASCII);
        int matched = 1;
        long until = SystemClock.elapsedRealtime() + 6000;
        while (matched < marker.length) {
          int b = read(until);
          matched = b == (marker[matched] & 255) ? matched + 1 : b == '\n' ? 1 : 0;
        }
        closedCleanly = true;
      } catch (Exception ignored) { closedCleanly = false; }
      try { output.close(); } catch (IOException ignored) { }
      try {
        if (!process.waitFor(2, TimeUnit.SECONDS)) {
          process.destroy();
          if (!process.waitFor(1, TimeUnit.SECONDS)) process.destroyForcibly();
        }
      } catch (InterruptedException error) { Thread.currentThread().interrupt(); }
      try { input.close(); process.getErrorStream().close(); } catch (IOException ignored) { }
    }
  }
}
