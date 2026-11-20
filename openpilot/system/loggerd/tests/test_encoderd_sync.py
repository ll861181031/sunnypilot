import os
from pathlib import Path
import subprocess
import time

import pytest
import zstandard

from msgq.visionipc import VisionIpcServer
from openpilot.cereal import log
from openpilot.cereal.visionipc import VisionStreamType
from openpilot.common.prefix import OpenpilotPrefix


LOGGERD_DIR = Path(__file__).resolve().parents[1]
CAMERAS = (
  (VisionStreamType.VISION_STREAM_NARROW_ROAD, "narrowRoadEncodeIdx"),
  (VisionStreamType.VISION_STREAM_WIDE_ROAD, "wideRoadEncodeIdx"),
)


def record_cameras(root, wide_fps, *, frame_id_offset=0, timestamps=True, timestamp_jump_ns=0, late_wide_s=0.0):
  """通过真实进程录制两路异步画面，检查持久化索引而非测试替身。"""
  if not all((LOGGERD_DIR / name).is_file() for name in ("encoderd", "loggerd")):
    pytest.skip("需要先编译 encoderd 和 loggerd")
  server = VisionIpcServer("camerad")
  for stream, _ in CAMERAS:
    server.create_buffers(stream, 20, 320, 240)
  server.start_listener()
  env = dict(os.environ, LOG_ROOT=str(root), PARAMS_ROOT=str(root / "params"),
             LOGGERD_TEST="1", LOGGERD_SEGMENT_LENGTH="2")
  children = []
  with (root / "processes.log").open("w+b") as output:
    try:
      for name in ("loggerd", "encoderd"):
        children.append(subprocess.Popen([str(LOGGERD_DIR / name)], cwd=LOGGERD_DIR,
                                         env=env, stdout=output, stderr=output))
      time.sleep(0.5)
      start = time.monotonic_ns()
      payload = bytes([100]) * (320 * 240 * 3 // 2)
      wide_step = 60 // wide_fps
      for tick in range(330):
        timestamp = start + tick * 1_000_000_000 // 60
        time.sleep(max(0, (timestamp - time.monotonic_ns()) / 1e9))
        if tick >= 180:
          timestamp += timestamp_jump_ns
        assert all(child.poll() is None for child in children)
        if tick % 3 == 0:
          ts = timestamp if timestamps else 0
          server.send(CAMERAS[0][0], payload, tick // 3, ts, ts)
        if tick % wide_step == 0 and tick >= late_wide_s * 60:
          ts = timestamp if timestamps else 0
          server.send(CAMERAS[1][0], payload, tick // wide_step + frame_id_offset, ts, ts)
      time.sleep(0.5)
    finally:
      for child in reversed(children):
        if child.poll() is None:
          child.terminate()
        try:
          child.wait(timeout=5)
        except subprocess.TimeoutExpired:
          child.kill()
          child.wait()
  segments = {}
  for rlog in root.glob("*/rlog.zst"):
    segment = int(rlog.parent.name.rsplit("--", 1)[1])
    frames = {name: [] for _, name in CAMERAS}
    with rlog.open("rb") as f, zstandard.ZstdDecompressor().stream_reader(f) as reader:
      for message in log.Event.read_multiple_bytes(reader.read()):
        if (name := message.which()) in frames:
          idx = getattr(message, name)
          frames[name].append((idx.frameId, idx.timestampSof, idx.segmentNum))
    segments[segment] = frames
  return segments


@pytest.mark.parametrize("wide_fps", (20, 30))
def test_different_camera_rates_share_segment_boundaries(tmp_path, wide_fps):
  with OpenpilotPrefix():
    segments = record_cameras(tmp_path, wide_fps)

  assert set(segments) == {0, 1, 2}
  for segment, frames in sorted(segments.items()):
    narrow, wide = (frames[name] for _, name in CAMERAS)
    assert narrow and wide, (segment, {name: len(items) for name, items in frames.items()})
    # 真实相机会在整秒边界附近连续出帧；首帧允许最多一个 20 Hz 主摄周期，
    # 但首尾都不再按“各自帧号的秒数”切段。
    assert abs(narrow[0][1] - wide[0][1]) <= 50_000_001, (segment, narrow[0], wide[0])
    assert abs(narrow[-1][1] - wide[-1][1]) <= 50_000_001, (segment, narrow[-1], wide[-1])
  for _, name in CAMERAS:
    ids = [frame[0] for _, frames in sorted(segments.items()) for frame in frames[name]]
    assert ids == list(range(ids[0], ids[-1] + 1)), name
  # 第 1 段完全位于连续发帧区，帧数应严格等于 2 秒。
  assert abs(len(segments[1]["narrowRoadEncodeIdx"]) - 40) <= 1
  assert abs(len(segments[1]["wideRoadEncodeIdx"]) - wide_fps * 2) <= 1


@pytest.mark.parametrize("timestamps,frame_id_offset", ((True, 10_000), (False, 0)))
def test_startup_supports_independent_ids_and_legacy_timestamps(tmp_path, timestamps, frame_id_offset):
  with OpenpilotPrefix():
    segments = record_cameras(tmp_path, 20, timestamps=timestamps, frame_id_offset=frame_id_offset)

  # 延后启动会减少前段可录帧数，末段是否跨过第 3 个切段点取决于进程启动抖动。
  # 本测试只验证共同起点和完整段的边界，不把尾段是否存在当作正确性条件。
  assert {0, 1} <= set(segments) <= {0, 1, 2}
  for frames in segments.values():
    narrow, wide = (frames[name] for _, name in CAMERAS)
    assert narrow and wide
    assert narrow[0][0] == wide[0][0] - frame_id_offset
    assert narrow[-1][0] == wide[-1][0] - frame_id_offset


def test_timestamp_gap_keeps_new_frames_in_the_correct_segment(tmp_path):
  with OpenpilotPrefix():
    segments = record_cameras(tmp_path, 30, timestamp_jump_ns=4_000_000_000)

  # 4 秒跳变后只剩约 0.5 秒的测试发帧，能不能跨进第 4 段受进程启动抖动影响。
  # 关键是跳变前的第 2 段仍然完整，跳变后的帧落到第 3 段而不被旧段号拖回去。
  assert {0, 1, 2, 3} <= set(segments) <= {0, 1, 2, 3, 4}
  for _, name in CAMERAS:
    ids = [frame[0] for _, frames in sorted(segments.items()) for frame in frames[name]]
    assert ids == list(range(ids[0], ids[-1] + 1)), name
    assert segments[2][name] and segments[3][name], (name, segments)


@pytest.mark.parametrize("late_wide_s", (0.4, 0.7))
def test_late_wide_start_still_uses_one_common_boundary(tmp_path, late_wide_s):
  with OpenpilotPrefix():
    segments = record_cameras(tmp_path, 30, late_wide_s=late_wide_s)

  assert set(segments) == {0, 1, 2}
  for segment, frames in sorted(segments.items()):
    narrow, wide = (frames[name] for _, name in CAMERAS)
    assert narrow and wide
    assert abs(narrow[0][1] - wide[0][1]) <= 50_000_001, (segment, narrow[0], wide[0])
    assert abs(narrow[-1][1] - wide[-1][1]) <= 50_000_001, (segment, narrow[-1], wide[-1])
