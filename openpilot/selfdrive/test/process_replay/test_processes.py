#!/usr/bin/env python3
import argparse
import concurrent.futures
import os
import sys
import traceback
from collections import defaultdict
from tqdm import tqdm
from typing import Any
from opendbc.car.car_helpers import interface_names, interfaces
from openpilot.common.git import get_commit
from openpilot.tools.lib.openpilotci import get_url
from openpilot.selfdrive.test.process_replay.compare_logs import compare_logs, format_diff
from openpilot.selfdrive.test.process_replay.diff_report import diff_process, diff_report
from openpilot.selfdrive.test.process_replay.process_replay import CONFIGS, PROC_REPLAY_DIR, FAKEDATA, replay_process, \
                                                                   check_most_messages_valid
from openpilot.tools.lib.filereader import FileReader
from openpilot.tools.lib.logreader import LogReader, save_log
from openpilot.tools.lib.url_file import URLFile

# 尚未配置宋 ALT 的公开参考日志；本地行车日志不得自动发布为 CI 素材。
source_segments: list[tuple[str, str]] = []
segments: list[tuple[str, str]] = []

# dashcamOnly makes don't need to be tested until a full port is done
excluded_interfaces = {brand for brand, platforms in interface_names.items()
                       if all(interfaces[platform].get_non_essential_params(platform).dashcamOnly for platform in platforms)}

BASE_URL = "https://raw.githubusercontent.com/sunnypilot/ci-artifacts/refs/heads/process-replay/"
REF_COMMIT_FN = os.path.join(PROC_REPLAY_DIR, "ref_commit")
EXCLUDED_PROCS = {"modeld", "dmonitoringmodeld"}


def run_test_process(data):
  segment, cfg, args, cur_log_fn, ref_log_path, lr_dat = data
  ref_log_msgs = list(LogReader(ref_log_path))
  lr = LogReader.from_bytes(lr_dat)
  res, log_msgs = test_process(cfg, lr, segment, ref_log_msgs, cur_log_fn, args.ignore_fields, args.ignore_msgs)
  # save logs so we can update refs
  save_log(cur_log_fn, log_msgs)
  try:
    diff_data = diff_process(cfg, ref_log_msgs, log_msgs)
  except Exception:
    diff_data = traceback.format_exc()
  return (segment, cfg.proc_name, res, diff_data)


def get_log_data(segment):
  r, n = segment.rsplit("--", 1)
  with FileReader(get_url(r, n, "rlog.zst")) as f:
    return (segment, f.read())


def test_process(cfg, lr, segment, ref_log_msgs, new_log_path, ignore_fields=None, ignore_msgs=None):
  if ignore_fields is None:
    ignore_fields = []
  if ignore_msgs is None:
    ignore_msgs = []

  try:
    log_msgs = replay_process(cfg, lr, disable_progress=True)
  except Exception as e:
    raise Exception("failed on segment: " + segment) from e

  if not check_most_messages_valid(log_msgs):
    return f"Route did not have enough valid messages: {new_log_path}", log_msgs

  # skip this check if the segment is using qcom gps
  if cfg.proc_name != 'ubloxd' or any(m.which() in cfg.pubs for m in lr):
    seen_msgs = {m.which() for m in log_msgs}
    expected_msgs = set(cfg.subs)
    if seen_msgs != expected_msgs:
      return f"Expected messages: {expected_msgs}, but got: {seen_msgs}", log_msgs

  try:
    return compare_logs(ref_log_msgs, log_msgs, ignore_fields + cfg.ignore, ignore_msgs, cfg.tolerance), log_msgs
  except Exception as e:
    return str(e), log_msgs


if __name__ == "__main__":
  all_cars = {car for car, _ in segments}
  all_procs = {cfg.proc_name for cfg in CONFIGS if cfg.proc_name not in EXCLUDED_PROCS}

  cpu_count = os.cpu_count() or 1

  parser = argparse.ArgumentParser(description="Regression test to identify changes in a process's output")
  parser.add_argument("--whitelist-procs", type=str, nargs="*", default=all_procs,
                      help="Whitelist given processes from the test (e.g. controlsd)")
  parser.add_argument("--whitelist-cars", type=str, nargs="*", default=all_cars,
                      help="Whitelist given cars from the test (e.g. BYD)")
  parser.add_argument("--blacklist-procs", type=str, nargs="*", default=[],
                      help="Blacklist given processes from the test (e.g. controlsd)")
  parser.add_argument("--blacklist-cars", type=str, nargs="*", default=[],
                      help="Blacklist given cars from the test (e.g. BYD)")
  parser.add_argument("--ignore-fields", type=str, nargs="*", default=[],
                      help="Extra fields or msgs to ignore (e.g. driverMonitoringState.events)")
  parser.add_argument("--ignore-msgs", type=str, nargs="*", default=[],
                      help="Msgs to ignore (e.g. carEvents)")
  parser.add_argument("--update-refs", action="store_true",
                      help="Updates reference logs using current commit")
  parser.add_argument("-j", "--jobs", type=int, default=max(cpu_count - 2, 1),
                      help="Max amount of parallel jobs")
  args = parser.parse_args()

  if not segments:
    parser.error("尚未配置 BYD_SONG_PLUS_DMI_23_ALT 的公开参考日志，无法执行公共回放回归")

  tested_procs = set(args.whitelist_procs) - set(args.blacklist_procs)
  tested_cars = set(args.whitelist_cars) - set(args.blacklist_cars)
  tested_cars = {c.upper() for c in tested_cars}

  if not tested_procs or not any(car in tested_cars for car, _ in segments):
    parser.error("筛选后没有可执行的车型或进程，无法执行回放回归")

  full_test = (tested_procs == all_procs) and (tested_cars == all_cars) and all(len(x) == 0 for x in (args.ignore_fields, args.ignore_msgs))
  os.makedirs(os.path.dirname(FAKEDATA), exist_ok=True)

  if args.update_refs:
    assert full_test, "Need to run full test when updating refs"

  try:
    with open(REF_COMMIT_FN) as f:
      ref_commit = f.read().strip()
  except FileNotFoundError:
    ref_commit = URLFile(BASE_URL + "ref_commit", cache=False).read().decode().strip()

  cur_commit = get_commit()
  if not cur_commit:
    raise Exception("Couldn't get current commit")

  print(f"***** testing against commit {ref_commit} *****")

  # check to make sure all car brands are tested
  if full_test:
    untested = (set(interface_names) - set(excluded_interfaces)) - {c.lower() for c in tested_cars}
    assert len(untested) == 0, f"Cars missing routes: {str(untested)}"

  log_paths: defaultdict[str, dict[str, dict[str, str]]] = defaultdict(lambda: defaultdict(dict))
  with concurrent.futures.ProcessPoolExecutor(max_workers=args.jobs) as pool:
    download_segments = [seg for car, seg in segments if car in tested_cars]
    log_data: dict[str, LogReader] = {}
    p1 = pool.map(get_log_data, download_segments)
    for segment, lr in tqdm(p1, desc="Getting Logs", total=len(download_segments)):
      log_data[segment] = lr

    pool_args: Any = []
    for car_brand, segment in segments:
      if car_brand not in tested_cars:
        continue

      for cfg in CONFIGS:
        if cfg.proc_name not in tested_procs:
          continue

        cur_log_fn = os.path.join(FAKEDATA, f"{segment}_{cfg.proc_name}_{cur_commit}.zst".replace("|", "_"))
        if args.update_refs:  # reference logs will not exist if routes were just regenerated
          route, seg_num = segment.rsplit("--", 1)
          ref_log_path = get_url(route, seg_num, "rlog.zst")
        else:
          ref_log_fn = os.path.join(FAKEDATA, f"{segment}_{cfg.proc_name}_{ref_commit}.zst".replace("|", "_"))
          ref_log_path = ref_log_fn if os.path.exists(ref_log_fn) else BASE_URL + os.path.basename(ref_log_fn)

        pool_args.append((segment, cfg, args, cur_log_fn, ref_log_path, log_data[segment]))

        log_paths[segment][cfg.proc_name]['ref'] = ref_log_path
        log_paths[segment][cfg.proc_name]['new'] = cur_log_fn

    results: Any = defaultdict(dict)
    diffs: list = []
    p2 = pool.map(run_test_process, pool_args)
    for (segment, proc, result, diff_data) in tqdm(p2, desc="Running Tests", total=len(pool_args)):
      results[segment][proc] = result
      diffs.append((segment, proc, diff_data))

  diff_short, diff_long, failed = format_diff(results, log_paths, ref_commit)
  if not args.update_refs:
    with open(os.path.join(PROC_REPLAY_DIR, "diff.txt"), "w") as f:
      f.write(diff_long)
    print(diff_short)

    try:
      diff_report(diffs, segments)
    except Exception:
      print(f"failed to generate diff report:\n{traceback.format_exc()}")

    if failed:
      print("TEST FAILED")
    else:
      print("TEST SUCCEEDED")

  else:
    with open(REF_COMMIT_FN, "w") as f:
      f.write(cur_commit)
    print(f"\n\nUpdated reference logs for commit: {cur_commit}")

  sys.exit(int(failed))
