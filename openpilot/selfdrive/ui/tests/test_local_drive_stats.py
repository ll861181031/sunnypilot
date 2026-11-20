import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from openpilot.selfdrive.ui.lib.local_drive_stats import (METERS_PER_MILE, CACHE_PARAM,
                                                          group_routes, list_segments,
                                                          local_drive_stats_cached,
                                                          to_cloud_shape)


def wall_now() -> float:
  # Route/session math compares against file mtimes, i.e. the wall clock.
  return time.time()  # noqa: TID251


def make_seg(route, index, path, wall_start, span=60.0, dist=1000.0):
  seg = mock.MagicMock()
  seg.route, seg.index, seg.path = route, index, Path(path)
  seg.wall_start, seg.wall_end = wall_start, wall_start + span
  seg.span, seg.dist = span, dist
  return seg


def make_route(route, segs):
  r = mock.MagicMock()
  r.route = route
  r.segs = segs
  r.wall_start = min(s.wall_start for s in segs)
  r.wall_end = max(s.wall_end for s in segs)
  r.span = sum(s.span for s in segs)
  r.dist = sum(s.dist for s in segs)
  return r


class TestLocalDriveStats(unittest.TestCase):
  def write_recording(self, root, route, wall_end):
    import zstandard
    from openpilot.cereal import messaging

    directory = root / f"{route}--0"
    directory.mkdir()
    init = messaging.new_message("initData")
    init.logMonoTime = 0
    init.initData.wallTimeNanos = int((wall_end - 1) * 1e9)
    messages = [init.to_bytes()]
    for t in (0, 1):
      state = messaging.new_message("carState")
      state.logMonoTime = t * 10**9
      state.carState.vEgo = 10
      messages.append(state.to_bytes())
    path = directory / "qlog.zst"
    path.write_bytes(zstandard.compress(b"".join(messages)))
    os.utime(path, (wall_end, wall_end))
    return path

  def test_removed_recordings_are_not_counted(self):
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      now = wall_now()
      older = self.write_recording(root, "aa--bb", now - 20)
      newer = self.write_recording(root, "cc--dd", now - 10)
      params = mock.MagicMock()
      self.assertEqual(local_drive_stats_cached(params, root)["all"]["routes"], 2)
      params.get.return_value = params.put.call_args.args[1]
      older.unlink()
      self.assertEqual(local_drive_stats_cached(params, root)["all"]["routes"], 1)
      newer.unlink()
      self.assertEqual(local_drive_stats_cached(params, root)["all"]["routes"], 0)

  def test_week_moves_even_without_new_recordings(self):
    end = 1_700_000_000
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      self.write_recording(root, "aa--bb", end)
      params = mock.MagicMock()
      with mock.patch("time.time", return_value=end + 7 * 86400):
        self.assertEqual(local_drive_stats_cached(params, root)["week"]["routes"], 1)
      params.get.return_value = params.put.call_args.args[1]
      with mock.patch("time.time", return_value=end + 7 * 86400 + 1):
        stats = local_drive_stats_cached(params, root)
        self.assertEqual((stats["all"]["routes"], stats["week"]["routes"]), (1, 0))
        self.assertEqual(local_drive_stats_cached(params, root, week_days=14)["week"]["routes"], 1)

  def test_cloud_shape_counts_routes_distance_and_minutes(self):
    now = wall_now()
    routes = [
      make_route("aa--bb", [make_seg("aa--bb", 0, "a", now - 2 * 86400)]),
      make_route("cc--dd", [make_seg("cc--dd", 0, "b", now - 20 * 86400)]),
    ]
    stats = to_cloud_shape(routes, now=now)
    self.assertEqual(stats["all"]["routes"], 2)
    self.assertEqual(stats["all"]["minutes"], 2.0)
    self.assertAlmostEqual(stats["all"]["distance"], 2000.0 / METERS_PER_MILE, places=2)
    # only the recent drive counts towards the past week
    self.assertEqual(stats["week"]["routes"], 1)
    self.assertEqual(stats["week"]["minutes"], 1.0)

  def test_distance_is_reported_in_miles(self):
    now = wall_now()
    one_mile = [make_route("aa--bb", [make_seg("aa--bb", 0, "a", now, dist=METERS_PER_MILE)])]
    self.assertAlmostEqual(to_cloud_shape(one_mile, now=now)["all"]["distance"], 1.0, places=3)

  def test_list_segments_skips_boot_and_requires_a_log(self):
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      good = root / "00000001--aabbccdd--0"
      good.mkdir()
      (good / "qlog.zst").touch()
      (root / "boot").mkdir()                       # not a route segment
      (root / "00000002--aabbccdd--1").mkdir()      # no log at all
      found = list_segments(root)
      self.assertEqual([(r, i) for r, i, _ in found], [("00000001--aabbccdd", 0)])

  def test_cached_stats_rescan_only_when_a_new_recording_appears(self):
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      seg_dir = root / "00000003--aabbccdd--0"
      seg_dir.mkdir()
      (seg_dir / "qlog.zst").touch()

      params = mock.MagicMock()
      params.get.return_value = None

      with mock.patch("openpilot.selfdrive.ui.lib.local_drive_stats.analyse_segment",
                      return_value=make_seg("00000003--aabbccdd", 0, seg_dir / "qlog.zst",
                                            wall_now())):
        first = local_drive_stats_cached(params, root=root)
        self.assertEqual(first["all"]["routes"], 1)
        self.assertEqual(params.put.call_args[0][0], CACHE_PARAM)

      # A fresh param value that is not newer than the cached "newest" must be
      # served from the cache without rescanning.
      cached = params.put.call_args[0][1]
      params.get.return_value = cached
      with mock.patch("openpilot.selfdrive.ui.lib.local_drive_stats.analyse_segment",
                      side_effect=AssertionError("should not rescan")):
        second = local_drive_stats_cached(params, root=root)
      self.assertEqual(second, first)

      # Touching a newer recording invalidates the cache.
      new_dir = root / "00000004--aabbccdd--0"
      new_dir.mkdir()
      (new_dir / "qlog.zst").touch()
      os.utime(new_dir / "qlog.zst", (wall_now() + 10, wall_now() + 10))
      with mock.patch("openpilot.selfdrive.ui.lib.local_drive_stats.analyse_segment",
                      return_value=make_seg("00000004--aabbccdd", 0, new_dir / "qlog.zst",
                                            wall_now())) as scan:
        third = local_drive_stats_cached(params, root=root)
      self.assertTrue(scan.called)
      self.assertEqual(third["all"]["routes"], 1)

  def test_group_routes_orders_segments_by_index(self):
    now = wall_now()
    segs = [make_seg("aa--bb", 2, "c", now), make_seg("aa--bb", 0, "a", now - 120),
            make_seg("aa--bb", 1, "b", now - 60)]
    routes = group_routes(segs)
    self.assertEqual(len(routes), 1)
    self.assertEqual([s.index for s in routes[0].segs], [0, 1, 2])


if __name__ == "__main__":
  unittest.main()
