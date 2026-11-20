import time
import unittest

from tools.camera_debug import capture


class TestHelpers(unittest.TestCase):
  def test_fourcc_roundtrip(self):
    self.assertEqual(capture.fourcc_to_str(capture.fourcc_int("MJPG")), "MJPG")
    self.assertEqual(capture.fourcc_to_str(capture.fourcc_int("YUYV")), "YUYV")
    self.assertEqual(capture.fourcc_to_str(0), "")

  def test_percentile_of_empty_is_none(self):
    self.assertIsNone(capture.percentile([], 50))

  def test_spec_from_dict_defaults(self):
    spec = capture.CameraSpec.from_dict({"device": "/dev/video2"}, 0)
    self.assertEqual(spec.label, "相机1")
    self.assertEqual((spec.width, spec.height), (1280, 720))
    self.assertEqual(spec.fourcc, "MJPG")
    self.assertEqual(spec.buffersize, 2)
    self.assertFalse(spec.decode)
    self.assertTrue(spec.preview)

  def test_spec_from_dict_overrides(self):
    spec = capture.CameraSpec.from_dict({"device": "fake:1", "width": 1920, "height": 1080, "fps": 25,
                                         "fourcc": "yuyv", "buffersize": 3, "decode": True,
                                         "preview": False, "label": "长焦"}, 1)
    self.assertEqual(spec.label, "长焦")
    self.assertEqual(spec.fourcc, "YUYV")
    self.assertEqual((spec.width, spec.height, spec.fps, spec.buffersize), (1920, 1080, 25, 3))
    self.assertTrue(spec.decode)
    self.assertFalse(spec.preview)


class TestRateStats(unittest.TestCase):
  def feed(self, stats, t0, fps, count, nbytes=1000):
    step = int(1e9 / fps)
    for i in range(count):
      stats.on_frame(t0 + i * step, read_ms=2.0, nbytes=nbytes)
    return t0 + count * step

  def test_window_rate_matches_the_measured_intervals(self):
    # 20 frames spaced 50 ms apart inside a 1 s window is exactly 20 fps. The
    # rate is counted from read() timestamps, never from the requested value.
    stats = capture.RateStats()
    t0 = time.monotonic_ns()
    stats.started_ns = t0
    self.feed(stats, t0, 20, 20)
    snap = stats.snapshot(t0 + int(1e9))
    self.assertAlmostEqual(snap["fps_window"], 20.0, delta=0.51)
    self.assertEqual(snap["frames"], 20)

  def test_interval_percentiles_and_jitter(self):
    stats = capture.RateStats()
    t0 = time.monotonic_ns()
    stats.started_ns = t0
    self.feed(stats, t0, 20, 20)
    snap = stats.snapshot(t0 + int(1e9))
    for key in ("dt_p50", "dt_p90", "dt_p99", "dt_max"):
      self.assertAlmostEqual(snap[key], 50.0, delta=0.01)
    self.assertAlmostEqual(snap["jitter"], 0.0, delta=0.01)

  def test_bandwidth_uses_the_window(self):
    stats = capture.RateStats()
    t0 = time.monotonic_ns()
    stats.started_ns = t0
    self.feed(stats, t0, 20, 20, nbytes=5000)
    snap = stats.snapshot(t0 + int(1e9))
    # 20 frames x 5000 B over 1 s
    self.assertAlmostEqual(snap["mbps_window"], 0.1, delta=0.005)
    self.assertEqual(snap["kb_avg"], 4.9)
    self.assertAlmostEqual(snap["mb_total"], 0.1, delta=0.001)

  def test_rate_is_none_until_the_window_is_wide_enough(self):
    # Dividing by a 10 ms window would report a wildly inflated rate for the
    # first frames of a run, so the tool reports nothing instead.
    stats = capture.RateStats()
    t0 = time.monotonic_ns()
    stats.started_ns = t0
    self.feed(stats, t0, 100, 3)
    snap = stats.snapshot(t0 + int(10e6))
    self.assertIsNone(snap["fps_window"])
    self.assertIsNone(snap["mbps_window"])

  def test_stale_samples_leave_the_window(self):
    stats = capture.RateStats()
    t0 = time.monotonic_ns()
    stats.started_ns = t0
    self.feed(stats, t0, 20, 20)          # a second of frames
    # Then nothing for 3 s: the live rate must drop to zero, while the average
    # still describes the whole run.
    snap = stats.snapshot(t0 + int(4e9))
    self.assertEqual(snap["fps_window"], 0.0)
    self.assertAlmostEqual(snap["fps_avg"], 5.0, delta=0.1)
    self.assertEqual(snap["frames"], 20)

  def test_failures_are_counted_separately(self):
    stats = capture.RateStats()
    t0 = time.monotonic_ns()
    stats.started_ns = t0
    self.feed(stats, t0, 20, 5)
    stats.on_failure()
    stats.on_failure()
    self.assertEqual(stats.snapshot(t0 + int(1e9))["failures"], 2)


class TestSessionValidation(unittest.TestCase):
  def test_rejects_zero_or_three_cameras(self):
    log = capture.SessionLog()
    for specs in ([], [capture.CameraSpec("a"), capture.CameraSpec("b"), capture.CameraSpec("c")]):
      with self.assertRaises(ValueError):
        capture.CaptureSession(specs, log)

  def test_rejects_the_same_device_twice(self):
    log = capture.SessionLog()
    with self.assertRaises(ValueError):
      capture.CaptureSession([capture.CameraSpec("/dev/video2"), capture.CameraSpec("/dev/video2")], log)


class TestFakeSession(unittest.TestCase):
  """End-to-end on the synthetic backend: no hardware is touched."""

  def setUp(self):
    self.log = capture.SessionLog()
    self.session = capture.CaptureSession([
      capture.CameraSpec(device="fake:0", label="虚拟A", width=320, height=240, fps=20, fake=True),
      capture.CameraSpec(device="fake:1", label="虚拟B", width=320, height=240, fps=20, fake=True),
    ], self.log)
    self.session.start()
    time.sleep(2.0)

  def tearDown(self):
    self.session.stop()

  def test_both_cameras_run_and_report_their_own_rate(self):
    snap = self.session.snapshot()
    self.assertTrue(snap["running"])
    for cam in snap["cameras"]:
      self.assertEqual(cam["state"], "running", cam["error"])
      self.assertGreater(cam["metrics"]["frames"], 20)
      self.assertGreater(cam["metrics"]["fps_window"], 12)
      self.assertLess(cam["metrics"]["fps_window"], 28)
      self.assertEqual(cam["negotiated"]["width"], 320)

  def test_combined_rate_is_the_sum_of_both(self):
    snap = self.session.snapshot()
    per_cam = [c["metrics"]["fps_window"] for c in snap["cameras"]]
    # Exactly the sum of the numbers shown next to each camera: the combined
    # value is computed from the same snapshot, not from a second reading.
    self.assertAlmostEqual(snap["combined"]["read_fps_window"], sum(per_cam), delta=0.011)
    self.assertGreater(snap["combined"]["read_fps_window"], 30)
    self.assertEqual(snap["combined"]["n_cameras"], 2)

  def test_preview_is_a_jpeg_passthrough(self):
    stream = self.session.streams[0]
    frame_id, jpeg = stream.encode_preview()
    self.assertTrue(stream._passthrough)
    self.assertGreater(frame_id, 0)
    self.assertEqual(jpeg[:2], b"\xff\xd8")  # JPEG SOI, straight from the driver

  def test_a_finished_session_is_archived_with_its_summary(self):
    self.session.stop()
    self.assertEqual(len(self.log.sessions), 1)
    summary = self.log.sessions[0]
    self.assertEqual(len(summary.cameras), 2)
    self.assertGreater(summary.duration_s, 1.0)
    self.assertIn("fps_avg", summary.cameras[0]["metrics"])
    self.assertNotIn("series", summary.cameras[0]["metrics"])

  def test_archive_happens_once(self):
    self.session.stop()
    self.session.stop()
    self.assertEqual(len(self.log.sessions), 1)


class TestComparison(unittest.TestCase):
  """The question this tool exists to answer: does the other camera cost fps?"""

  @staticmethod
  def camera(device, fps, fourcc="MJPG"):
    return {
      "label": "相机", "device": device,
      "requested": {"width": 1920, "height": 1080, "fourcc": fourcc},
      "negotiated": {"width": 1920, "height": 1080, "fourcc": fourcc, "fps": 30},
      "metrics": {"fps_avg": fps, "fps_window": fps, "mbps_window": 10.0},
      "error": "",
    }

  def log_with(self, sessions):
    log = capture.SessionLog()
    for started_at, cams in sessions:
      log.add(capture.SessionSummary(started_at=started_at, duration_s=10.0,
                                     cameras=cams, combined={}))
    return log

  def test_alone_against_together(self):
    log = self.log_with([
      (100.0, [self.camera("/dev/video2", 20.0)]),
      (200.0, [self.camera("/dev/video2", 14.0), self.camera("/dev/video0", 12.0)]),
    ])
    rows = {row["device"]: row for row in log.comparisons()}
    # /dev/video0 only ever ran alongside, so it gets its own row with no delta;
    # /dev/video2 is the one with a single-camera baseline.
    self.assertEqual(set(rows), {"/dev/video2", "/dev/video0"})
    row = rows["/dev/video2"]
    self.assertAlmostEqual(row["alone"]["fps_avg"], 20.0)
    self.assertAlmostEqual(row["together"]["fps_avg"], 14.0)
    self.assertAlmostEqual(row["delta_pct"], -30.0)
    self.assertIsNone(rows["/dev/video0"]["alone"])

  def test_a_dual_run_alone_has_no_delta(self):
    log = self.log_with([(100.0, [self.camera("/dev/video2", 14.0), self.camera("/dev/video0", 12.0)])])
    row = log.comparisons()[0]
    self.assertIsNone(row["alone"])
    self.assertEqual(row["together"]["n_cameras"], 2)
    self.assertIsNone(row["delta_pct"])

  def test_different_modes_are_not_paired(self):
    # A run at 1920x1080 MJPG and a run at YUYV are different measurements;
    # pairing them would invent a delta that does not exist.
    log = self.log_with([
      (100.0, [self.camera("/dev/video2", 20.0, "MJPG")]),
      (200.0, [self.camera("/dev/video2", 5.0, "YUYV"), self.camera("/dev/video0", 12.0, "MJPG")]),
    ])
    rows = {(row["device"], row["fourcc"]): row for row in log.comparisons()}
    self.assertEqual(set(rows), {("/dev/video2", "MJPG"), ("/dev/video2", "YUYV"), ("/dev/video0", "MJPG")})
    self.assertIsNone(rows[("/dev/video2", "MJPG")]["together"])
    self.assertIsNone(rows[("/dev/video2", "YUYV")]["alone"])
    for row in rows.values():
      self.assertIsNone(row["delta_pct"])

  def test_clear_empties_the_log(self):
    log = self.log_with([(100.0, [self.camera("/dev/video2", 20.0)])])
    log.clear()
    self.assertEqual(log.all(), [])
    self.assertEqual(log.comparisons(), [])


if __name__ == "__main__":
  unittest.main()
