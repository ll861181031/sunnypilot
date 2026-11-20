package com.jixiexiaoge.drivingassist.navdiag;

import android.content.Intent;
import android.os.Bundle;
import android.os.Parcelable;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.Serializable;
import java.lang.reflect.Array;
import java.nio.charset.StandardCharsets;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Set;
import java.util.TreeSet;

/** 将广播值编码为有类型的 JSON；不可读取的值必须显式标记，不能伪装成完整记录。 */
public final class CaptureCodec {
  private CaptureCodec() {}

  public static final class Result {
    public final JSONObject json;
    public final boolean complete;
    public final boolean truncated;
    public final int decodeErrors;
    public final int encodedBytes;

    private Result(JSONObject json, boolean complete, boolean truncated, int decodeErrors) {
      this.json = json;
      this.complete = complete;
      this.truncated = truncated;
      this.decodeErrors = decodeErrors;
      this.encodedBytes = json.toString().getBytes(StandardCharsets.UTF_8).length;
    }
  }

  public static Result encodeIntent(Intent intent, int maxDepth, int maxBytes) {
    if (maxDepth < 0 || maxBytes < 512) throw new IllegalArgumentException("编码限额无效");
    Encoder encoder = new Encoder(maxDepth, maxBytes);
    JSONObject out = new JSONObject();
    try {
      out.put("action", encoder.header(intent == null ? null : intent.getAction(), "/action"));
      out.put("targetPackage", encoder.header(intent == null ? null : intent.getPackage(), "/targetPackage"));
      out.put("component", encoder.header(intent == null || intent.getComponent() == null
          ? null : intent.getComponent().flattenToString(), "/component"));
      if (intent == null) {
        encoder.issue("/", "null_intent", null, false);
        out.put("extras", unavailable("unreadable", "null_intent"));
      } else {
        try {
          Bundle extras = intent.getExtras();
          out.put("extras", extras == null ? typed("bundle", new JSONObject())
              : encoder.value(extras, "/extras", 0));
        } catch (RuntimeException error) {
          encoder.issue("/extras", "bundle_read_error", error.getClass().getName(), false);
          out.put("extras", unavailable("unreadable", "bundle_read_error"));
        }
      }
      out.put("complete", encoder.complete);
      out.put("truncated", encoder.truncated);
      out.put("issues", encoder.issues);
    } catch (JSONException error) {
      encoder.issue("/", "json_encode_error", error.getClass().getName(), false);
      out = minimal("json_encode_error", false);
    }
    int size = out.toString().getBytes(StandardCharsets.UTF_8).length;
    if (size > maxBytes) {
      out = minimal("record_size_limit", true);
      return new Result(out, false, true, encoder.decodeErrors);
    }
    return new Result(out, encoder.complete, encoder.truncated, encoder.decodeErrors);
  }

  private static JSONObject typed(String type, Object value) throws JSONException {
    return new JSONObject().put("type", type).put("value", value == null ? JSONObject.NULL : value);
  }

  private static JSONObject unavailable(String type, String reason) throws JSONException {
    return new JSONObject().put("type", type).put("reason", reason);
  }

  private static JSONObject minimal(String reason, boolean truncated) {
    JSONObject out = new JSONObject();
    try {
      out.put("complete", false).put("truncated", truncated)
          .put("extras", unavailable("unreadable", reason))
          .put("issues", new JSONArray().put(new JSONObject().put("path", "/").put("reason", reason)));
    } catch (JSONException impossible) {
      throw new IllegalStateException(impossible);
    }
    return out;
  }

  private static final class Encoder {
    final int maxDepth;
    long remaining;
    boolean complete = true;
    boolean truncated = false;
    int decodeErrors = 0;
    final JSONArray issues = new JSONArray();
    final IdentityHashMap<Object, Boolean> ancestors = new IdentityHashMap<>();

    Encoder(int maxDepth, int maxBytes) {
      this.maxDepth = maxDepth;
      this.remaining = Math.max(0, maxBytes - 512L);
    }

    Object header(String text, String path) {
      if (text == null) return JSONObject.NULL;
      if (text.length() > 256) {
        issue(path, "header_size_limit", null, true);
        return JSONObject.NULL;
      }
      return text;
    }

    boolean claim(long amount, String path) {
      if (amount > remaining || amount < 0) {
        issue(path, "record_size_limit", null, true);
        remaining = 0;
        return false;
      }
      remaining -= amount;
      return true;
    }

    void issue(String path, String reason, String className, boolean limit) {
      complete = false;
      truncated |= limit;
      if (!limit) decodeErrors++;
      if (issues.length() >= 64) return;
      try {
        JSONObject item = new JSONObject().put("path", path.length() > 512 ? path.substring(0, 512) : path)
            .put("reason", reason);
        if (className != null) item.put("className", className);
        issues.put(item);
      } catch (JSONException impossible) {
        throw new IllegalStateException(impossible);
      }
    }

    JSONObject value(Object input, String path, int depth) throws JSONException {
      if (depth > maxDepth) {
        issue(path, "depth_limit", input == null ? null : input.getClass().getName(), true);
        return unavailable("truncated", "depth_limit");
      }
      if (!claim(96, path)) return unavailable("truncated", "record_size_limit");
      if (input == null || input == JSONObject.NULL) return typed("null", JSONObject.NULL);
      if (input instanceof String) {
        String text = (String) input;
        // 使用保守的转义上界，避免为了测量长度先分配一个超大 UTF-8 缓冲区。
        if (!claim((long) text.length() * 6L + 2L, path)) return unavailable("truncated", "string_size_limit");
        return typed("string", text);
      }
      if (input instanceof Boolean) return typed("boolean", input);
      if (input instanceof Byte) return typed("byte", input);
      if (input instanceof Short) return typed("short", input);
      if (input instanceof Integer) return typed("int", input);
      // 使用十进制字符串，避免导出由 JavaScript 阅读时丢失长整数精度。
      if (input instanceof Long) return typed("long", input.toString());
      if (input instanceof Character) return typed("char", String.valueOf(input));
      if (input instanceof Float) {
        float number = (Float) input;
        JSONObject out = typed("float", Float.isNaN(number) || Float.isInfinite(number) ? Float.toString(number) : number);
        return out.put("bits", Integer.toHexString(Float.floatToRawIntBits(number)));
      }
      if (input instanceof Double) {
        double number = (Double) input;
        JSONObject out = typed("double", Double.isNaN(number) || Double.isInfinite(number) ? Double.toString(number) : number);
        return out.put("bits", Long.toHexString(Double.doubleToRawLongBits(number)));
      }
      if (ancestors.containsKey(input)) {
        issue(path, "cyclic_reference", input.getClass().getName(), false);
        return unavailable("unreadable", "cyclic_reference");
      }
      ancestors.put(input, true);
      try {
        if (input instanceof Bundle) return bundle((Bundle) input, path, depth);
        if (input.getClass().isArray()) {
          int count = Array.getLength(input);
          JSONArray values = new JSONArray();
          for (int i = 0; i < count; i++) {
            if (remaining < 96) {
              issue(path + "/" + i, "record_size_limit", input.getClass().getName(), true);
              break;
            }
            values.put(value(Array.get(input, i), path + "/" + i, depth + 1));
          }
          return typed("array", values).put("className", input.getClass().getName())
              .put("length", count).put("capturedLength", values.length());
        }
        if (input instanceof List<?>) {
          List<?> list = (List<?>) input;
          JSONArray values = new JSONArray();
          int count;
          try {
            count = list.size();
            for (int i = 0; i < count; i++) {
              if (remaining < 96) {
                issue(path + "/" + i, "record_size_limit", input.getClass().getName(), true);
                break;
              }
              values.put(value(list.get(i), path + "/" + i, depth + 1));
            }
          } catch (RuntimeException error) {
            issue(path, "list_read_error", error.getClass().getName(), false);
            return unavailable("unreadable", "list_read_error");
          }
          return typed("list", values).put("className", input.getClass().getName())
              .put("length", count).put("capturedLength", values.length());
        }
        String reason = input instanceof Parcelable ? "unsupported_parcelable"
            : input instanceof Serializable ? "unsupported_serializable" : "unsupported_type";
        issue(path, reason, input.getClass().getName(), false);
        return unavailable("unsupported", reason).put("className", input.getClass().getName());
      } finally {
        ancestors.remove(input);
      }
    }

    JSONObject bundle(Bundle bundle, String path, int depth) throws JSONException {
      JSONObject values = new JSONObject();
      Set<String> keys;
      try {
        keys = new TreeSet<>(bundle.keySet());
      } catch (RuntimeException error) {
        issue(path, "bundle_keys_error", error.getClass().getName(), false);
        return unavailable("unreadable", "bundle_keys_error");
      }
      for (String key : keys) {
        if (key == null || !claim(key.length() * 6L + 16L, path)) break;
        String child = path + "/" + key.replace("~", "~0").replace("/", "~1");
        try {
          values.put(key, value(bundle.get(key), child, depth + 1));
        } catch (RuntimeException error) {
          issue(child, "bundle_value_error", error.getClass().getName(), false);
          values.put(key, unavailable("unreadable", "bundle_value_error"));
        }
      }
      return typed("bundle", values).put("length", keys.size()).put("capturedLength", values.length());
    }
  }
}
