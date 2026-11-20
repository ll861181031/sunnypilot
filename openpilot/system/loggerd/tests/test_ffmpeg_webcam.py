import os
import subprocess
import tempfile
import time
import unittest

import numpy as np
from msgq.visionipc import VisionIpcServer

from openpilot.cereal import messaging
from openpilot.cereal.visionipc import VisionStreamType
from openpilot.common.basedir import BASEDIR
from openpilot.common.hardware import PC
from openpilot.common.prefix import OpenpilotPrefix

try:
  import av
  HAS_AV = True
except ImportError:
  av = None
  HAS_AV = False

ENCODERD = os.path.join(BASEDIR, "openpilot/system/loggerd/encoderd")
HAS_ENCODERD = os.path.exists(ENCODERD)

# V4L2 UAPI value, declared the same way in tools/camerastream/compressed_vipc.py.
V4L2_BUF_FLAG_KEYFRAME = 0x8


@unittest.skipUnless(HAS_AV, "requires PyAV (av)")
@unittest.skipUnless(HAS_ENCODERD, "encoderd binary is not built")
@unittest.skipUnless(PC, "FFmpeg encoder is the desktop backend")
class TestFfmpegWebcam(unittest.TestCase):
  def test_camera_replacement_exits_encoder_and_restart_uses_new_buffers(self):
    with OpenpilotPrefix(), tempfile.TemporaryFile(mode="w+") as output:
      stream = VisionStreamType.VISION_STREAM_NARROW_ROAD
      server = VisionIpcServer("camerad")
      server.create_buffers(stream, 20, 320, 240)
      server.start_listener()
      sock = messaging.sub_sock("qNarrowRoadEncodeData")
      proc = subprocess.Popen([ENCODERD], stdout=output, stderr=output, env=os.environ.copy())

      def publish_frames(width, count):
        received = []
        for i in range(count):
          frame = bytes([40]) * (width * 240) + bytes([128]) * (width * 120)
          stamp = 1_000_000_000 + i * 50_000_000
          server.send(stream, frame, i, stamp, stamp)
          time.sleep(.02)
          received.extend(messaging.drain_sock(sock))
        return received

      try:
        self.assertGreater(len(publish_frames(320, 50)), 10)
        # A replacement camera has different buffers, dimensions, and frame IDs.
        del server
        server = VisionIpcServer("camerad")
        server.create_buffers(stream, 20, 352, 240)
        server.start_listener()
        publish_frames(352, 20)
        try:
          self.assertNotEqual(proc.wait(timeout=2), 0)
        except subprocess.TimeoutExpired:
          self.fail("encoderd kept running on the disconnected camera buffers")
        sock = messaging.sub_sock("qNarrowRoadEncodeData")
        proc = subprocess.Popen([ENCODERD], stdout=output, stderr=output, env=os.environ.copy())
        self.assertGreater(len(publish_frames(352, 50)), 10)
        self.assertIsNone(proc.poll())
      finally:
        if proc.poll() is None:
          proc.terminate()
          try:
            proc.wait(timeout=5)
          except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)

  def test_h264_accepts_unique_timestamps_without_buffering_metadata(self):
    # A real isolated encoderd exercises x264's initial buffering behavior.
    with OpenpilotPrefix(), tempfile.TemporaryFile(mode="w+") as output:
      stream = VisionStreamType.VISION_STREAM_NARROW_ROAD
      server = VisionIpcServer("camerad")
      server.create_buffers(stream, 20, 320, 240)
      server.start_listener()
      sock = messaging.sub_sock("qNarrowRoadEncodeData")
      proc = subprocess.Popen([ENCODERD], stdout=output, stderr=output, env=os.environ.copy())
      received = []
      try:
        for i in range(100):
          self.assertIsNone(proc.poll(), "encoderd exited during the test")
          frame = bytes([i + 16]) * (320 * 240) + bytes([128]) * (320 * 120)
          timestamp = 1_000_000_000 + i * 50_000_000
          server.send(stream, frame, i, timestamp, timestamp)
          time.sleep(0.02)
          received.extend(messaging.drain_sock(sock))
      finally:
        proc.terminate()
        try:
          proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
          proc.kill()
          proc.wait(timeout=5)
      output.seek(0)
      log = output.read()
      self.assertNotIn("non-strictly-monotonic PTS", log)
      self.assertNotIn("avcodec_send_frame error", log)
      self.assertGreater(len(received), 60, log)
      ids = [msg.qNarrowRoadEncodeData.idx.frameId for msg in received]
      self.assertEqual(ids, sorted(set(ids)))
      decoder = av.CodecContext.create("h264", "r")
      primed = False
      for msg in received:
        idx = msg.qNarrowRoadEncodeData.idx
        self.assertEqual(idx.timestampEof, 1_000_000_000 + idx.frameId * 50_000_000)
        data = bytes(msg.qNarrowRoadEncodeData.data)
        if not primed and (idx.flags & V4L2_BUF_FLAG_KEYFRAME):
          # The SPS/PPS now travel in the published header instead of in-band - that
          # is what lets matroska start an H.264 track. Prepend them to the first
          # keyframe the way a player does with the container's codec private data.
          data = bytes(msg.qNarrowRoadEncodeData.header) + data
          primed = True
        frames = decoder.decode(av.Packet(data))
        self.assertEqual(len(frames), 1, "a delayed picture would have the wrong frame metadata")
        yuv = frames[0].to_ndarray(format="yuv420p")
        self.assertAlmostEqual(float(np.mean(yuv[:frames[0].height])), idx.frameId + 16, delta=2)

  def test_keyframe_publishes_the_sps_pps_matroska_needs(self):
    # loggerd hands this header to VideoWriter, which passes it to
    # avformat_write_header(). Matroska will not start an H.264 track without the
    # SPS/PPS, so the encoder has to publish them. When the header was empty, that
    # assertion in video_writer.cc aborted loggerd on the very first keyframe and
    # selfdrived reported process_not_running.
    with OpenpilotPrefix(), tempfile.TemporaryFile(mode="w+") as output:
      stream = VisionStreamType.VISION_STREAM_NARROW_ROAD
      server = VisionIpcServer("camerad")
      server.create_buffers(stream, 20, 320, 240)
      server.start_listener()
      sock = messaging.sub_sock("qNarrowRoadEncodeData")
      proc = subprocess.Popen([ENCODERD], stdout=output, stderr=output, env=os.environ.copy())
      received = []
      try:
        for i in range(30):
          self.assertIsNone(proc.poll(), "encoderd exited during the test")
          frame = bytes([i + 16]) * (320 * 240) + bytes([128]) * (320 * 120)
          timestamp = 1_000_000_000 + i * 50_000_000
          server.send(stream, frame, i, timestamp, timestamp)
          time.sleep(0.02)
          received.extend(messaging.drain_sock(sock))
      finally:
        proc.terminate()
        try:
          proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
          proc.kill()
          proc.wait(timeout=5)

    keyframes = [m for m in received
                 if m.qNarrowRoadEncodeData.idx.flags & V4L2_BUF_FLAG_KEYFRAME]
    self.assertTrue(keyframes, "encoderd published no keyframe")
    header = bytes(keyframes[0].qNarrowRoadEncodeData.header)
    self.assertGreater(len(header), 0,
                       "keyframe carried no SPS/PPS; loggerd cannot write the matroska header")
    self.assertGreaterEqual(len(header), 5, "header is too short to hold an Annex-B SPS")
    self.assertEqual(header[:4], b"\x00\x00\x00\x01", "the header must start with an Annex-B start code")
    self.assertEqual(header[4] & 0x1F, 7, "the first NAL unit must be an SPS")
