"""Reproducible local-only evidence analysis. No CAN transport or vehicle changes.

Run from the project root with PYTHONDONTWRITEBYTECODE=1 and Python -B.
Output is JSON on stdout; all input logs must already be complete local files.
Signal names and units are DBC hypotheses, not an accepted actuator contract.
"""
import hashlib
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from itertools import groupby
from pathlib import Path

from openpilot.tools.car_porting.byd_longitudinal_audit import decode_frame, read_local_events

ROOT = Path.cwd()
STREAMS = {(2, 0x32D): "hud", (2, 0x32E): "cmd", (2, 0x32F): "aeb",
           (0, 0x1F0): "wheel", (0, 0x342): "pedal", (0, 0x220): "vlc"}
FLAGS = ("STANDSTILL_STATE", "STANDSTILL_RESUME", "ACC_CONTROLLABLE_AND_ON",
         "ACC_OVERRIDE_OR_STANDSTILL", "ACC_REQ_NOT_STANDSTILL", "BRAKE_BEHAVIOR", "ESP_BEHAVIOR")
AGE_NS = 100_000_000  # Analysis join window only.
BJ = timezone(timedelta(hours=8))


def quantiles(values):
  if not values:
    return None
  ordered = sorted(values)
  return {str(q): ordered[round((len(ordered) - 1) * q)] for q in (0, .05, .5, .95, 1)}


def relationship(rows):
  if not rows:
    return {"n": 0}
  x = [r["accel"] for r in rows]
  y = [r["vlc"] for r in rows]
  mx, my = sum(x) / len(x), sum(y) / len(y)
  xx, yy = sum((v - mx) ** 2 for v in x), sum((v - my) ** 2 for v in y)
  xy = sum((a - mx) * (b - my) for a, b in zip(x, y, strict=True))
  err = [round(b - a, 8) for a, b in zip(x, y, strict=True)]
  return {"n": len(rows), "correlation": xy / math.sqrt(xx * yy) if xx > 1e-12 and yy > 1e-12 else None,
          "accel_range": [min(x), max(x)], "vlc_range": [min(y), max(y)],
          "difference_vlc_minus_cmd_quantiles": quantiles(err),
          "within_one_raw_step": sum(abs(e) <= .05000001 for e in err),
          "exact_raw_offset_40": sum(r["vlc_raw"] - r["cmd_raw"] == 40 for r in rows),
          "vlc_zero": sum(r["vlc_raw"] == 140 for r in rows),
          "raw_difference_top": Counter(str(r["vlc_raw"] - r["cmd_raw"]) for r in rows).most_common(8)}


def example(row):
  return {k: v for k, v in row.items() if k != "flags"}


def inspect_segment(path, meta):
  stats = Counter()
  stream_stats = defaultdict(Counter)
  counters, cache = {}, {}
  rows = []
  events = read_local_events(path)
  stats["event_order_reversals"] = sum(a.logMonoTime > b.logMonoTime for a, b in zip(events, events[1:], strict=False))
  can = sorted((e for e in events if e.which() == "can"), key=lambda e: e.logMonoTime)
  source = str(path.relative_to(ROOT))
  start = datetime.fromisoformat(meta["started_utc"])
  for nanos, envelopes_iter in groupby(can, key=lambda e: e.logMonoTime):
    envelopes = list(envelopes_iter)
    stats["can_envelopes"] += len(envelopes)
    if len(envelopes) > 1:
      stats["multiple_envelopes_same_timestamp"] += 1
    if nanos <= 0 or any(not e.valid for e in envelopes):
      stats["invalid_timestamp_groups"] += 1
      counters.clear()
      cache.clear()
      continue
    batches = defaultdict(list)
    for envelope in envelopes:
      for frame in envelope.can:
        key = STREAMS.get((frame.src, frame.address))
        if key:
          batches[key].append(frame)
    for key, frames in batches.items():
      cache.pop(key, None)
      stream_stats[key]["raw_frames"] += len(frames)
      eligible = []
      for frame in frames:
        if key == "vlc" and len(frame.dat) == 8:
          payload = bytes(frame.dat)
          stream_stats[key]["candidate_additive_checksum_matches" if sum(payload) % 256 == 255 else "candidate_additive_checksum_mismatches"] += 1
          guess = payload[6] & 15
          if "vlc_candidate_counter" in counters and guess != (counters["vlc_candidate_counter"] + 1) % 16:
            stream_stats[key]["candidate_low_nibble_counter_discontinuities"] += 1
          counters["vlc_candidate_counter"] = guess
        try:
          values = decode_frame(frame.address, frame.dat)
        except ValueError as exc:
          stream_stats[key][str(exc) + "_errors"] += 1
          counters.pop(key, None)
          continue
        continuous = True
        if "COUNTER" in values:
          counter = int(values["COUNTER"])
          if key in counters and counter != (counters[key] + 1) % 16:
            stream_stats[key]["counter_discontinuities"] += 1
            continuous = False
          counters[key] = counter
        if key == "wheel" and any(values["FAULT_" + w] for w in ("FL", "FR", "BL", "BR")):
          stream_stats[key]["wheel_fault_frames"] += 1
          continuous = False
        if continuous:
          eligible.append((nanos, values, bytes(frame.dat)))
      if len(frames) > 1:
        stream_stats[key]["ambiguous_timestamp_groups"] += 1
      elif len(eligible) == 1:
        cache[key] = eligible[0]
    if "cmd" not in batches or len(batches["cmd"]) != 1 or "cmd" not in cache:
      continue
    stats["unique_clean_command_samples"] += 1
    fresh = {k: v for k, v in cache.items() if 0 <= nanos - v[0] < AGE_NS}
    if not all(k in fresh for k in ("cmd", "hud", "wheel", "pedal", "vlc")):
      stats["missing_or_ambiguous_context"] += 1
      continue
    cmd, hud, wheel, pedal, vlc = (fresh[k][1] for k in ("cmd", "hud", "wheel", "pedal", "vlc"))
    raw = fresh["cmd"][2]
    speed = sum(wheel["WHEELSPEED_" + w] for w in ("FL", "FR", "BL", "BR")) / 14.4
    observed_time = start + timedelta(microseconds=(nanos - meta["started_monotonic_ns"]) / 1000)
    rows.append({"source": source, "logMonoTime": nanos,
                 "beijing_time": observed_time.astimezone(BJ).isoformat(timespec="milliseconds"),
                 "hud": int(hud["CRUISE_STATE"]), "speed_mps": speed,
                 "gas": pedal["GAS_PEDAL"], "brake": pedal["BRAKE_PEDAL"],
                 "accel": round(cmd["ACCEL_CMD"], 8), "cmd_raw": raw[0],
                 "cmd_hex": raw.hex(), "aux_hex": raw[1:6].hex(),
                 "vlc": round(vlc["VLC_SENSOR"], 8), "vlc_raw": fresh["vlc"][2][3],
                 "vlc_hex": fresh["vlc"][2].hex(), "vlc_byte1_bit2": (fresh["vlc"][2][1] >> 2) & 1,
                 "hold": int(vlc["BRAKE_HOLD_ACTIVE"]), "brake_active_low": int(vlc["BRAKE_APPLIED_ACTIVE_LOW"]),
                 "flags": {f: int(cmd[f]) for f in FLAGS},
                 "ages_ms": {k: (nanos - fresh[k][0]) / 1e6 for k in ("hud", "wheel", "pedal", "vlc")},
                 "hud_aeb": int(hud["AEB"]), "hud_fcw": int(hud["FCW"])})
  stats["fully_joined_samples"] = len(rows)
  return {"source": source, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
          "stats": dict(stats), "streams": {k: dict(v) for k, v in stream_stats.items()}}, rows


def extended_analysis(rows):
  from bisect import bisect_left, bisect_right
  def corr_pairs(x, y):
    n = len(x)
    if not n:
      return {"n": 0}
    mx, my = sum(x) / n, sum(y) / n
    xx = sum((v - mx) ** 2 for v in x)
    yy = sum((v - my) ** 2 for v in y)
    xy = sum((a - mx) * (b - my) for a, b in zip(x, y, strict=True))
    return {"n": n, "correlation": xy / math.sqrt(xx * yy) if xx > 1e-12 and yy > 1e-12 else None,
            "y_on_x_slope": xy / xx if xx > 1e-12 else None,
            "y_on_x_intercept": my - xy / xx * mx if xx > 1e-12 else None,
            "difference_y_minus_x_quantiles": quantiles([b - a for a, b in zip(x, y, strict=True)])}

  # Approximately one-second central differences with non-overlapping windows.
  # Wheel data remain noisy proxies, not an accelerometer or actuator contract.
  windows = defaultdict(list)
  by_source = defaultdict(list)
  for r in rows:
    by_source[r["source"]].append(r)
  for source, source_rows in by_source.items():
    chunks, chunk = [], []
    for row in source_rows:
      pedal = "both" if row["gas"] > .01 and row["brake"] > .01 else "gas" if row["gas"] > .01 else "brake" if row["brake"] > .01 else "none"
      category = (row["hud"], pedal)
      eligible = row["speed_mps"] >= 1 and row["ages_ms"]["wheel"] <= 25
      if (not eligible or (chunk and (
          row["logMonoTime"] - chunk[-1]["logMonoTime"] > 100_000_000 or
          category != chunk[-1]["category"]))):
        if chunk:
          chunks.append(chunk)
        chunk = []
      if eligible:
        chunk.append(dict(row, category=category))
    if chunk:
      chunks.append(chunk)
    for chunk in chunks:
      times = [r["logMonoTime"] for r in chunk]
      last_right = -1
      last_wheel_end = -1
      for i, row in enumerate(chunk):
        left = bisect_right(times, times[i] - 500_000_000) - 1
        right = bisect_left(times, times[i] + 500_000_000)
        if left <= last_right or left < 0 or right >= len(chunk):
          continue
        a, b = chunk[left], chunk[right]
        ta = a["logMonoTime"] - round(a["ages_ms"]["wheel"] * 1e6)
        tb = b["logMonoTime"] - round(b["ages_ms"]["wheel"] * 1e6)
        dt = (tb - ta) / 1e9
        if ta <= last_wheel_end or not .9 <= dt <= 1.25:
          continue
        derivative = (b["speed_mps"] - a["speed_mps"]) / dt
        windows[f"hud={row['hud']},pedal={row['category'][1]}"].append(
          {"source": source, "beijing_time": row["beijing_time"], "wheel_derivative": derivative,
           "vlc": row["vlc"], "cmd": row["accel"], "seconds": dt,
           "cmd_hex": row["cmd_hex"], "vlc_hex": row["vlc_hex"]})
        last_right = right
        last_wheel_end = tb
  derivative_results = {}
  for key, rr in sorted(windows.items()):
    derivative_results[key] = {
      "wheel_vs_vlc": corr_pairs([r["wheel_derivative"] for r in rr], [r["vlc"] for r in rr]),
      "wheel_vs_cmd": corr_pairs([r["wheel_derivative"] for r in rr], [r["cmd"] for r in rr]),
      "cmd_vs_vlc": corr_pairs([r["cmd"] for r in rr], [r["vlc"] for r in rr]),
      "examples": rr[:3]}

  jerk = defaultdict(list)
  comfort = defaultdict(list)
  flag_count = defaultdict(Counter)
  for row in rows:
    raw = bytes.fromhex(row["cmd_hex"])
    if row["hud"] in (2, 3) and row["flags"]["STANDSTILL_RESUME"] == 0:
      jerk[(raw[3] & 127, raw[4] & 127)].append(row)
    comfort[raw[1:3].hex()].append(row)
    flag_key = (f'on={row["flags"]["ACC_CONTROLLABLE_AND_ON"]},req={row["flags"]["ACC_REQ_NOT_STANDSTILL"]},' +
                f'override={row["flags"]["ACC_OVERRIDE_OR_STANDSTILL"]}')
    flag_count[row["hud"]][flag_key] += 1

  return {
    "wheel_derivative_method": (
      "Non-overlapping ~1 s centered finite differences; no cross-segment joins; speed>=1 m/s; wheel age<=25 ms; " +
      "all matched rows within each window share HUD state and pedal category; excluded instants are not interpolated; " +
      "adjacent matched sample gap<=100 ms; not calibrated actual acceleration."),
    "wheel_derivative_results": derivative_results,
    "jerk_raw_pairs_hud2_or_3_resume0": [
      {"upper_raw": key[0], "lower_raw": key[1], "n": len(rr),
       "speed_kph_quantiles": quantiles([r["speed_mps"] * 3.6 for r in rr]),
       "accel_quantiles": quantiles([r["accel"] for r in rr]),
       "hud_counts": dict(Counter(r["hud"] for r in rr))}
      for key, rr in sorted(jerk.items())],
    "vlc_byte1_bit2_by_hud": {str(h): dict(Counter(str(r["vlc_byte1_bit2"]) for r in rows if r["hud"] == h)) for h in sorted({r["hud"] for r in rows})},
    "comfort_bytes": {key: {"n": len(rr), "speed_kph_quantiles": quantiles([r["speed_mps"] * 3.6 for r in rr]),
                            "hud_counts": dict(Counter(r["hud"] for r in rr)), "example": example(rr[0])}
                      for key, rr in sorted(comfort.items())},
    "hud_on_req_override_counts": {str(key): dict(value) for key, value in sorted(flag_count.items())},
    "manual_context_examples": {
      key: [example(rr[0]), example(min(rr, key=lambda r:r["vlc"])), example(max(rr, key=lambda r:r["vlc"]))]
      for key, rr in {
        "hud2_gas_moving_cmd0": [r for r in rows if r["hud"] == 2 and r["speed_mps"] >= 1 and r["gas"] > .01 and r["brake"] <= .01 and r["accel"] == 0],
        "hud1_brake_moving_cmd0": [r for r in rows if r["hud"] == 1 and r["speed_mps"] >= 1 and r["brake"] > .01 and r["gas"] <= .01 and r["accel"] == 0]
      }.items() if rr
    },
    "raw_zero_speed_kph_quantiles": quantiles([r["speed_mps"] * 3.6 for r in rows if r["cmd_raw"] == 0]),
  }


def recorded_firmware():
  manifest = json.loads((ROOT / "data/byd-longitudinal-shadow-20260924-op-final.json").read_text())
  output = []
  for segment in manifest["segments"]:
    source = Path(segment["source"])
    records = []
    for event in read_local_events(source):
      if event.which() == "carParams":
        cp = event.carParams
        records.append({"fingerprint": cp.carFingerprint, "fingerprint_source": str(cp.fingerprintSource),
                        "openpilotLongitudinalControl": cp.openpilotLongitudinalControl,
                        "car_fw_count": len(cp.carFw), "ecu_types": dict(Counter(str(fw.ecu) for fw in cp.carFw))})
    output.append({"source": str(source.relative_to(ROOT)), "sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "records": records})
  return output


def main():
  segments, rows, sessions = [], [], []
  for session_dir in sorted((ROOT / "data/byd-captures").iterdir()):
    summary_path = session_dir / "summary.json"
    if not summary_path.is_file():
      continue
    summary = json.loads(summary_path.read_text())
    if not summary.get("successful"):
      continue
    if not summary.get("archive_complete"):
      raise ValueError("Incomplete capture archive: " + str(session_dir))
    paths = sorted(session_dir.glob("*/rlog.zst"))
    if len(paths) != summary["segments"]:
      raise ValueError("Segment count differs from archive: " + str(session_dir))
    meta = json.loads((session_dir / "session.json").read_text())
    sessions.append({"source": str(session_dir.relative_to(ROOT)), "segment_count": len(paths)})
    for path in paths:
      segment, observed = inspect_segment(path, meta)
      segments.append(segment)
      rows.extend(observed)
      print(path.parent.name, len(observed), "joined samples", file=sys.stderr, flush=True)

  groups = defaultdict(list)
  aux = defaultdict(list)
  for row in rows:
    pressed = ("gas+brake" if row["gas"] > .01 and row["brake"] > .01 else
               "gas" if row["gas"] > .01 else "brake" if row["brake"] > .01 else "none")
    motion = "stationary" if row["speed_mps"] < .1 else "moving"
    groups[f'hud={row["hud"]},pedal={pressed},motion={motion}'].append(row)
    aux[(row["hud"], row["aux_hex"])].append(row)

  moving = [r for r in rows if r["hud"] == 3 and r["speed_mps"] >= .1 and r["gas"] <= .01 and r["brake"] <= .01]
  aux_patterns = []
  for (hud, payload), grouped in sorted(aux.items(), key=lambda kv: -len(kv[1])):
    aux_patterns.append({"hud": hud, "aux_bytes_1_to_5": payload, "n": len(grouped),
                         "flags": grouped[0]["flags"],
                         "accel_range": [min(r["accel"] for r in grouped), max(r["accel"] for r in grouped)],
                         "speed_range": [min(r["speed_mps"] for r in grouped), max(r["speed_mps"] for r in grouped)],
                         "negative_accel": sum(r["accel"] < 0 for r in grouped),
                         "zero_accel": sum(r["accel"] == 0 for r in grouped),
                         "positive_accel": sum(r["accel"] > 0 for r in grouped),
                         "pedal_pressed": sum(r["gas"] > .01 or r["brake"] > .01 for r in grouped),
                         "example": example(grouped[0])})
  sign = {"negative": [r for r in moving if r["accel"] < 0],
          "zero": [r for r in moving if r["accel"] == 0],
          "positive": [r for r in moving if r["accel"] > 0]}
  flag_sign_counts = {}
  for flag in FLAGS:
    flag_sign_counts[flag] = {name: dict(Counter(str(r["flags"][flag]) for r in rr)) for name, rr in sign.items()}

  extreme = sorted(moving, key=lambda r: abs(r["vlc"] - r["accel"]), reverse=True)
  spaced_examples, seen = [], set()
  for row in extreme:
    key = (row["source"], row["logMonoTime"] // 1_000_000_000)
    if key not in seen:
      spaced_examples.append(example(row))
      seen.add(key)
    if len(spaced_examples) == 10:
      break
  # Examples are separated by host second, not independent trials or causality.
  result = {
    "control_ready": False, "mode": "local_passive_evidence_only",
    "context_join_window_ms": 100, "stationary_threshold_mps": .1,
    "method_notes": [
      "DBC fields are hypotheses. This analysis neither transmits nor certifies commands.",
      "Only exact RX src 0/2; never count mirrored frames as independent evidence.",
      "Complete compressed local logs; per-segment contexts; no future samples.",
      "Reject bad checksum, counter discontinuity, wheel fault and repeated stream at one timestamp.",
      "0x220 and 0x342 have no DBC checksum and remain auxiliary evidence.",
      "Equal host timestamps do not establish on-wire order; ages are not ECU timing.",
      "0.1 m/s and 100 ms are analysis categories/windows, not validated actuator limits."
    ],
    "sessions": sessions, "segments": segments, "fully_joined_samples": len(rows),
    "context_groups": {k: relationship(v) for k, v in sorted(groups.items())},
    "moving_hud3_no_pedal": {k: relationship(v) for k, v in sign.items()},
    "moving_hud3_no_pedal_aux_flag_sign_counts": flag_sign_counts,
    "aux_patterns": [{k: v for k, v in p.items() if k != "example" or i < 8} for i, p in enumerate(aux_patterns)],
    "largest_differences_moving_hud3_no_pedal": spaced_examples,
    "all_raw_zero": relationship([r for r in rows if r["cmd_raw"] == 0]),
    "raw_zero_context_counts": dict(Counter(f'hud={r["hud"]},hold={r["hold"]},moving={r["speed_mps"] >= .1}' for r in rows if r["cmd_raw"] == 0)),
    "all_hud_aeb_fcw_candidates": dict(Counter(f'{r["hud_aeb"]}:{r["hud_fcw"]}' for r in rows)),
  }
  result.update(extended_analysis(rows))
  result['recorded_firmware'] = recorded_firmware()
  print(json.dumps(result, separators=(",", ":"), allow_nan=False))


if __name__ == "__main__":
  main()
