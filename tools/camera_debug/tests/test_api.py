import json
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from tools.camera_debug import camera_debug


class ApiTestCase(unittest.TestCase):
  """Drives the real HTTP server on a fake-camera backend.

  The whole point of --fake is this test: the API, the rates, the MJPEG preview
  and the comparison table get exercised end to end without occupying the
  cameras the driving stack may be using.
  """

  @classmethod
  def setUpClass(cls):
    cls.httpd, cls.app = camera_debug.build_server("127.0.0.1", 0, fake_count=2)
    cls.port = cls.httpd.server_address[1]
    cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
    cls.thread.start()
    cls.base = f"http://127.0.0.1:{cls.port}"

  @classmethod
  def tearDownClass(cls):
    cls.app.stop()
    cls.httpd.shutdown()
    cls.httpd.server_close()
    cls.thread.join(timeout=5)

  def tearDown(self):
    # The server (and its session log) is shared by the whole class, so each
    # test must leave no running session and no archived run behind.
    self.app.stop()
    self.app.log.clear()

  def get(self, path):
    with urlopen(f"{self.base}{path}", timeout=15) as res:
      return res.status, json.loads(res.read().decode())

  def post(self, path, payload=None):
    body = json.dumps(payload or {}).encode()
    req = Request(f"{self.base}{path}", data=body, method="POST",
                  headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=15) as res:
      return res.status, json.loads(res.read().decode())

  def post_expect_error(self, path, payload):
    with self.assertRaises(HTTPError) as ctx:
      self.post(path, payload)
    return json.loads(ctx.exception.read().decode())["error"]

  def start_fake(self, count=2, fps=20, size=(320, 240)):
    cams = [{"device": f"fake:{i}", "width": size[0], "height": size[1], "fps": fps,
             "fourcc": "MJPG", "buffersize": 1, "preview": True} for i in range(count)]
    return self.post("/api/start", {"cameras": cams})


class TestDevicesAndValidation(ApiTestCase):
  def test_device_list_includes_fake_cameras(self):
    status, data = self.get("/api/devices")
    self.assertEqual(status, 200)
    nodes = [c["node"] for c in data["cameras"]]
    self.assertIn("fake:0", nodes)
    self.assertIn("fake:1", nodes)
    self.assertTrue(all(c["fake"] for c in data["cameras"] if c["node"].startswith("fake:")))

  def test_idle_stats_are_empty(self):
    _, data = self.get("/api/stats")
    self.assertFalse(data["running"])
    self.assertEqual(data["cameras"], [])

  def test_rejects_no_camera(self):
    self.assertIn("1 或 2", self.post_expect_error("/api/start", {"cameras": []}))

  def test_rejects_three_cameras(self):
    cams = [{"device": f"fake:{i}"} for i in range(3)]
    self.assertIn("1 或 2", self.post_expect_error("/api/start", {"cameras": cams}))

  def test_rejects_the_same_device_twice(self):
    cams = [{"device": "fake:0"}, {"device": "fake:0"}]
    self.assertIn("同一个设备", self.post_expect_error("/api/start", {"cameras": cams}))

  def test_rejects_impossible_settings(self):
    self.assertIn("帧率", self.post_expect_error("/api/start", {"cameras": [{"device": "fake:0", "fps": 500}]}))
    self.assertIn("偶数", self.post_expect_error("/api/start", {"cameras": [{"device": "fake:0", "width": 321, "height": 240}]}))
    self.assertIn("buffersize", self.post_expect_error("/api/start", {"cameras": [{"device": "fake:0", "buffersize": 99}]}))
    self.assertIn("fourcc", self.post_expect_error("/api/start", {"cameras": [{"device": "fake:0", "fourcc": "NV12"}]}))

  def test_unknown_device_is_reported_not_fatal(self):
    # A device that cannot be opened must come back as a per-camera failure,
    # because the same thing happens when camerad holds the real camera.
    status, data = self.start_fake(1)
    self.assertTrue(data["ok"])
    _, data = self.post("/api/start", {"cameras": [{"device": "/dev/video99"}]})
    self.assertFalse(data["ok"])
    self.assertEqual(data["cameras"][0]["state"], "failed")
    self.assertIn("无法打开", data["cameras"][0]["error"])

  def test_unknown_path_is_404(self):
    with self.assertRaises(HTTPError) as ctx:
      urlopen(f"{self.base}/api/nope", timeout=10)
    self.assertEqual(ctx.exception.code, 404)


class TestCaptureCycle(ApiTestCase):
  def test_two_cameras_measured_then_archived(self):
    _, started = self.start_fake(count=2, fps=20)
    self.assertTrue(started["ok"], started)
    time.sleep(2.0)

    _, stats = self.get("/api/stats")
    self.assertTrue(stats["running"])
    self.assertEqual(len(stats["cameras"]), 2)
    for cam in stats["cameras"]:
      self.assertEqual(cam["state"], "running")
      self.assertGreater(cam["metrics"]["fps_window"], 12)
      self.assertLess(cam["metrics"]["fps_window"], 28)
      self.assertEqual(cam["negotiated"]["width"], 320)
    per_cam = [c["metrics"]["fps_window"] for c in stats["cameras"]]
    self.assertAlmostEqual(stats["combined"]["read_fps_window"], sum(per_cam), delta=0.05)
    self.assertEqual(stats["combined"]["n_cameras"], 2)

    self.post("/api/stop")
    _, stats = self.get("/api/stats")
    self.assertFalse(stats["running"])

    _, sessions = self.get("/api/sessions")
    self.assertEqual(len(sessions["sessions"]), 1)
    self.assertEqual(len(sessions["sessions"][0]["cameras"]), 2)


  def test_alone_then_together_produces_a_comparison(self):
    _, first = self.start_fake(count=1, fps=20)
    self.assertTrue(first["ok"], first)
    time.sleep(1.5)
    self.post("/api/stop")

    _, second = self.start_fake(count=2, fps=20)
    self.assertTrue(second["ok"], second)
    time.sleep(1.5)
    self.post("/api/stop")

    _, data = self.get("/api/sessions")
    rows = {(r["device"], r["fourcc"]): r for r in data["comparisons"]}
    self.assertIn(("fake:0", "MJPG"), rows)
    row = rows[("fake:0", "MJPG")]
    self.assertIsNotNone(row["alone"])
    self.assertIsNotNone(row["together"])
    self.assertEqual(row["together"]["n_cameras"], 2)
    self.assertIsNotNone(row["delta_pct"])
    self.assertEqual(len(data["sessions"]), 2)

  def test_preview_stream_serves_jpeg_frames(self):
    self.start_fake(count=1, fps=20)
    time.sleep(0.6)
    with urlopen(f"{self.base}/preview/0?fps=10", timeout=10) as res:
      self.assertIn("multipart/x-mixed-replace", res.headers["Content-Type"])
      chunk = res.read(8192)
    self.assertIn(b"--frame", chunk)
    self.assertIn(b"image/jpeg", chunk)
    self.assertIn(b"\xff\xd8", chunk)

  def test_restarting_replaces_the_previous_session(self):
    self.start_fake(count=1)
    time.sleep(0.4)
    self.start_fake(count=1)
    time.sleep(0.4)
    _, stats = self.get("/api/stats")
    self.assertEqual(len(stats["cameras"]), 1)
    self.assertTrue(stats["running"])

  def test_clearing_the_log(self):
    self.start_fake(count=1)
    time.sleep(0.4)
    self.post("/api/stop")
    _, data = self.get("/api/sessions")
    self.assertEqual(len(data["sessions"]), 1)
    self.post("/api/sessions/clear")
    _, data = self.get("/api/sessions")
    self.assertEqual(data["sessions"], [])
    self.assertEqual(data["comparisons"], [])

  def test_shared_bus_warning_names_the_bus(self):
    # Both cameras on one USB 2.0 bus share a single isochronous budget. That is a
    # possibility, not a verdict - measured 2026-10-16 on this rig, 1080p + 1080p
    # streamed together at 20.0 / 15.0 fps - so the API reports the sharing and
    # where to look if a stream fails, instead of prescribing a resolution.
    fake = {"port": "1-2", "root_port": "1-2", "behind_hub": False, "bus": 1,
            "dev": 8, "speed_mbps": 480, "product": "X"}
    node = lambda name: type("N", (), {"node": name, "usb": dict(fake)})()  # noqa: E731
    nodes = {"/dev/video0": node("/dev/video0"), "/dev/video2": node("/dev/video2")}
    specs = [type("S", (), {"device": "/dev/video0"})(), type("S", (), {"device": "/dev/video2"})()]
    warnings = self.app._shared_bus_warnings(specs, nodes)
    self.assertEqual(len(warnings), 1)
    self.assertIn("usb1", warnings[0])
    self.assertIn("1080p+1080p", warnings[0])
    self.assertIn("Not enough bandwidth", warnings[0])

  def test_no_shared_bus_warning_on_separate_buses_or_one_camera(self):
    def node(name, bus):
      return {name: type("N", (), {"node": name, "usb": {"bus": bus, "root_port": f"{bus}-1"}})()}
    specs = [type("S", (), {"device": "/dev/video0"})(), type("S", (), {"device": "/dev/video2"})()]
    nodes = {**node("/dev/video0", 1), **node("/dev/video2", 3)}
    self.assertEqual(self.app._shared_bus_warnings(specs, nodes), [])
    self.assertEqual(self.app._shared_bus_warnings(specs[:1], nodes), [])

  def test_stats_expose_usb_placement(self):
    # The live card shows which bus a camera is on; fake cameras have no USB.
    self.start_fake(count=1)
    time.sleep(0.4)
    _, stats = self.get("/api/stats")
    self.assertIn("usb", stats["cameras"][0])
    self.assertEqual(stats["cameras"][0]["usb"], {})


if __name__ == "__main__":
  unittest.main()
