import os
import shutil
import tempfile
import unittest

from tools.camera_debug import devices

# Trimmed from real `v4l2-ctl --list-formats-ext` output on this machine
# (DECXIN 1bcf:2cd1), plus one stepwise format that UVC drivers also produce.
LIST_FORMATS = """ioctl: VIDIOC_ENUM_FMT
\tType: Video Capture

\t[0]: 'MJPG' (Motion-JPEG, compressed)
\t\tSize: Discrete 3840x2160
\t\t\tInterval: Discrete 0.033s (30.000 fps)
\t\t\tInterval: Discrete 0.200s (5.000 fps)
\t\tSize: Discrete 1280x720
\t\t\tInterval: Discrete 0.033s (30.000 fps)
\t\t\tInterval: Discrete 0.050s (20.000 fps)
\t\t\tInterval: Discrete 0.100s (10.000 fps)
\t[1]: 'YUYV' (YUYV 4:2:2)
\t\tSize: Discrete 640x480
\t\t\tInterval: Discrete 0.033s (30.000 fps)
\t\tSize: Stepwise 640x480 - 1920x1080 (step 1x1)
\t\t\tInterval: Stepwise 0.033s (30.000 fps)
"""

# Copied from `v4l2-ctl --info` on this machine. Note that the device-wide
# `Capabilities` block lists Video Capture for BOTH nodes of a UVC camera; only
# `Device Caps` says which node can actually stream.
INFO_STREAMING = """Driver Info:
\tDriver name      : uvcvideo
\tCard type        : DECXIN CAMERA: DECXIN CAMERA
\tBus info         : usb-0000:c4:00.3-1.1
\tDriver version   : 7.0.14
\tCapabilities     : 0x84a00001
\t\tVideo Capture
\t\tMetadata Capture
\t\tStreaming
\t\tExtended Pix Format
\tDevice Capabilities
\tDevice Caps      : 0x04200001
\t\tVideo Capture
\t\tStreaming
\t\tExtended Pix Format
"""

INFO_METADATA = """Driver Info:
\tDriver name      : uvcvideo
\tCard type        : DECXIN CAMERA: DECXIN CAMERA
\tBus info         : usb-0000:c4:00.3-1.1
\tDriver version   : 7.0.14
\tCapabilities     : 0x84a00001
\t\tVideo Capture
\t\tMetadata Capture
\t\tStreaming
\t\tExtended Pix Format
\tDevice Capabilities
\tDevice Caps      : 0x04a00000
\t\tMetadata Capture
\t\tStreaming
\t\tExtended Pix Format
"""


class TestParseFormats(unittest.TestCase):
  def setUp(self):
    self.modes = devices.parse_list_formats_ext(LIST_FORMATS)

  def test_discrete_sizes_and_rates(self):
    mjpg = {(m.width, m.height): m for m in self.modes if m.fourcc == "MJPG"}
    self.assertEqual(set(mjpg), {(3840, 2160), (1280, 720)})
    self.assertEqual(mjpg[(1280, 720)].fps, [30.0, 20.0, 10.0])
    self.assertFalse(mjpg[(1280, 720)].stepwise)

  def test_second_format_is_kept_separate(self):
    yuyv = [m for m in self.modes if m.fourcc == "YUYV"]
    self.assertEqual(len(yuyv), 2)

  def test_stepwise_uses_maximum_size(self):
    step = [m for m in self.modes if m.stepwise]
    self.assertEqual(len(step), 1)
    self.assertEqual((step[0].width, step[0].height), (1920, 1080))

  def test_empty_output_is_not_an_error(self):
    self.assertEqual(devices.parse_list_formats_ext(""), [])

  def test_duplicate_modes_are_merged(self):
    text = LIST_FORMATS + "\t[0]: 'MJPG' (Motion-JPEG, compressed)\n\t\tSize: Discrete 1280x720\n\t\t\tInterval: Discrete 0.100s (10.000 fps)\n"
    merged = [m for m in devices.parse_list_formats_ext(text) if m.fourcc == "MJPG" and m.width == 1280]
    self.assertEqual(len(merged), 1)
    self.assertEqual(merged[0].fps, [30.0, 20.0, 10.0])


class TestCaptureCapability(unittest.TestCase):
  def test_streaming_node(self):
    self.assertTrue(devices.parse_capture_capability(INFO_STREAMING))

  def test_metadata_node_is_not_a_camera(self):
    # Both /dev/video* nodes of one UVC camera are listed by sysfs, and only the
    # streaming node can actually be opened as a camera. The device-wide
    # Capabilities block says "Video Capture" for both, so only Device Caps may
    # be trusted here.
    self.assertFalse(devices.parse_capture_capability(INFO_METADATA))

  def test_device_wide_capabilities_alone_would_be_wrong(self):
    # Guards the trap above: the top-level block does contain Video Capture.
    self.assertIn("Video Capture", INFO_METADATA)
    self.assertIn("Capabilities     : 0x84a00001", INFO_METADATA)

  def test_falls_back_to_capability_bits(self):
    text = "Device Caps      : 0x04200001\n\tStreaming\n"
    self.assertTrue(devices.parse_capture_capability(text))

  def test_falls_back_to_device_wide_caps_without_device_caps(self):
    text = "Capabilities     : 0x04200001\n\tVideo Capture\n"
    self.assertTrue(devices.parse_capture_capability(text))

  def test_unparsable_is_not_assumed_capturing(self):
    self.assertFalse(devices.parse_capture_capability("Driver name : uvcvideo\n"))


class TestModeValidation(unittest.TestCase):
  def setUp(self):
    self.node = devices.VideoNode(node="/dev/video2", index=2, card="test",
                                  capture=True,
                                  modes=devices.parse_list_formats_ext(LIST_FORMATS))

  def test_advertised_mode_is_accepted(self):
    ok, note = devices.mode_supported(self.node, "MJPG", 1280, 720, 20.0)
    self.assertTrue(ok, note)

  def test_unadvertised_size_is_reported_with_alternatives(self):
    ok, note = devices.mode_supported(self.node, "MJPG", 1344, 760, 20.0)
    self.assertFalse(ok)
    self.assertIn("1280x720", note)

  def test_unadvertised_format_is_reported(self):
    ok, note = devices.mode_supported(self.node, "NV12", 1280, 720, 20.0)
    self.assertFalse(ok)
    self.assertIn("MJPG", note)

  def test_unadvertised_rate_is_reported(self):
    ok, note = devices.mode_supported(self.node, "MJPG", 1280, 720, 15.0)
    self.assertFalse(ok)
    self.assertIn("fps", note)

  def test_mode_is_reported_not_enforced_when_list_is_empty(self):
    # Without v4l2-ctl there is nothing to validate against; the request still
    # goes to the driver and the negotiated values are shown instead.
    node = devices.VideoNode(node="/dev/video9", index=9, card="x", capture=True, modes=[])
    ok, note = devices.mode_supported(node, "MJPG", 1280, 720, 30.0)
    self.assertTrue(ok)
    self.assertIn("v4l2-ctl", note)


class TestUsbTopology(unittest.TestCase):
  """Where a camera sits on the USB tree decides whether two can stream at once."""

  def setUp(self):
    self.tmp = tempfile.mkdtemp()
    self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

  def _device(self, port, **attrs):
    path = os.path.join(self.tmp, port)
    os.makedirs(path, exist_ok=True)
    for key, value in attrs.items():
      with open(os.path.join(path, key), "w") as f:
        f.write(str(value))
    return path

  def test_reads_bus_and_port(self):
    dev = self._device("1-2", busnum=1, devnum=8, speed=480, product="RGB Camera")
    info = devices.parse_usb_topology(dev, "1-2")
    self.assertEqual(info["bus"], 1)
    self.assertEqual(info["dev"], 8)
    self.assertEqual(info["port"], "1-2")
    self.assertEqual(info["root_port"], "1-2")
    self.assertFalse(info["behind_hub"])
    self.assertEqual(info["product"], "RGB Camera")

  def test_hub_port_keeps_the_root_port(self):
    # "1-1.2" is port 2 of the hub on root port 1: the root port is what two
    # cameras must differ on to stop sharing a bus segment.
    dev = self._device("1-1.2", busnum=1, devnum=7)
    info = devices.parse_usb_topology(dev, "1-1.2")
    self.assertEqual(info["root_port"], "1-1")
    self.assertTrue(info["behind_hub"])

  def test_missing_attributes_do_not_raise(self):
    dev = self._device("3-1")
    info = devices.parse_usb_topology(dev, "3-1")
    self.assertIsNone(info["bus"])
    self.assertEqual(info["product"], "")

  def test_fake_nodes_have_no_usb(self):
    self.assertEqual(devices.usb_topology("fake:0"), {})
    self.assertEqual(devices.usb_topology("/dev/video99"), {})


class TestEnumeration(unittest.TestCase):
  def test_enumerates_this_machine(self):
    # Environment probe, not a spec: cameras get unplugged and drop off the USB
    # bus on this rig (one did mid-session), so assert the *shape* of what comes
    # back rather than requiring hardware to be plugged in.
    nodes = devices.enumerate_nodes()
    self.assertTrue(all(n.node.startswith("/dev/video") for n in nodes))
    self.assertTrue(all(isinstance(n.capture, bool) for n in nodes))
    for node in nodes:
      if node.capture and devices.have_v4l2_ctl():
        self.assertTrue(node.modes, f"{node.node} 支持采集却没解析出任何模式")

  def test_cache_can_be_cleared(self):
    devices.clear_cache()
    first = devices.enumerate_nodes()
    self.assertIs(first, devices.enumerate_nodes())


if __name__ == "__main__":
  unittest.main()
