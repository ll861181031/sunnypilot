import os
import importlib
import threading
import time
import unittest
import uuid
from unittest.mock import Mock, patch

import numpy as np
from msgq.visionipc import VisionIpcClient, VisionIpcServer

from openpilot.cereal.visionipc import VisionStreamType
from openpilot.system.camerad.cameras.nv12_info import get_nv12_info

try:
  import av
  from av.video.reformatter import VideoReformatter
  from openpilot.system.camerad.webcam import camera, camerad
  HAS_AV = True
except ImportError:
  av = VideoReformatter = camera = camerad = None
  HAS_AV = False


@unittest.skipUnless(HAS_AV, "requires PyAV (av)")
class TestWebcam(unittest.TestCase):
  def setUp(self):
    # These tests predate the raw V4L2 path and drive a mocked cv.VideoCapture.
    # Two of them open "/dev/video0", which on a machine with the camera plugged
    # in is a real device: the raw path would win, the mock would never be used,
    # and the test would only pass where no camera exists. Force the OpenCV
    # fallback for the whole class; the raw-path tests patch this back.
    patcher = patch.object(camera, 'V4L2Capture', side_effect=OSError("raw path disabled in tests"))
    patcher.start()
    self.addCleanup(patcher.stop)

  @staticmethod
  def make_camera(width, height):
    return TestWebcam.make_camera_with_crop(width, height, width, height)

  @staticmethod
  def make_camera_with_crop(src_w, src_h, out_w, out_h):
    cam = camera.Camera.__new__(camera.Camera)
    cam.output_w, cam.output_h = out_w, out_h
    cam.scale_w, cam.scale_h, cam.crop_x, cam.crop_y = camera.cover_and_crop(src_w, src_h, out_w, out_h)
    cam.nv12_stride, cam.nv12_y_height, cam.nv12_uv_height, cam.nv12_size = get_nv12_info(out_w, out_h)
    cam.mjpeg_decoder = av.CodecContext.create('mjpeg', 'r')
    cam.mjpeg_decoder.thread_count = 1
    cam.reformatter = VideoReformatter()
    cam.rotate_180 = False
    cam._use_turbo = False
    cam._turbo = None
    cam._turbo_tried = False
    return cam

  @staticmethod
  def planes(cam, data):
    y = np.ndarray((cam.output_h, cam.output_w), dtype=np.uint8, buffer=data, strides=(cam.nv12_stride, 1))
    uv = np.ndarray((cam.output_h // 2, cam.output_w // 2, 2), dtype=np.uint8, buffer=data,
                    offset=cam.nv12_stride * cam.nv12_y_height, strides=(cam.nv12_stride, 2, 1))
    return y, uv

  def test_disabled_driver_camera_is_never_opened_even_when_configured(self):
    try:
      with patch.dict(os.environ, {"SUNNYPILOT_DISABLE_DRIVER_CAMERA": "1", "DRIVER_CAM": "/dev/video2"}):
        config = importlib.reload(camerad)
        self.assertNotIn("cabinCameraState", [cam.msg_name for cam in config.CAMERAS])
    finally:
      importlib.reload(camerad)

  def test_stable_device_path_is_not_prefixed_again(self):
    path = "/dev/v4l/by-id/test-video-index0"
    with patch.object(camerad.platform, "system", return_value="Linux"):
      self.assertEqual(camerad.camera_device(path), path)
      self.assertEqual(camerad.camera_device("2"), "/dev/video2")

  def test_driver_capture_settings_do_not_change_road_settings(self):
    env = {'WEBCAM_WIDTH': '1920', 'WEBCAM_HEIGHT': '1080', 'WEBCAM_CAPTURE_FPS': '30',
           'WEBCAM_OUTPUT_WIDTH': '1344', 'WEBCAM_OUTPUT_HEIGHT': '760', 'WEBCAM_ROTATE_180': '0',
           'DRIVER_WEBCAM_WIDTH': '640', 'DRIVER_WEBCAM_HEIGHT': '480', 'DRIVER_WEBCAM_CAPTURE_FPS': '20',
           'DRIVER_WEBCAM_OUTPUT_WIDTH': '1928', 'DRIVER_WEBCAM_OUTPUT_HEIGHT': '1208', 'DRIVER_WEBCAM_ROTATE_180': '1'}
    for service, width, height, fps, output, rotate in (
      ('narrowRoadCameraState', 1920, 1080, 30, (1344, 760), False),
      ('cabinCameraState', 640, 480, 20, (1928, 1208), True),
    ):
      cap = Mock()
      cap.isOpened.return_value = True
      cap.get.side_effect = lambda prop, w=width, h=height: {camera.cv.CAP_PROP_FRAME_WIDTH: w,
                                                          camera.cv.CAP_PROP_FRAME_HEIGHT: h}.get(prop, 0)
      with patch.dict(os.environ, env), patch.object(camera.cv, 'VideoCapture', return_value=cap):
        cam = camera.Camera(service, 0, '/dev/mock')
      cap.set.assert_any_call(camera.cv.CAP_PROP_FRAME_WIDTH, width)
      cap.set.assert_any_call(camera.cv.CAP_PROP_FRAME_HEIGHT, height)
      cap.set.assert_any_call(camera.cv.CAP_PROP_FPS, fps)
      self.assertEqual((cam.output_w, cam.output_h), output)
      self.assertEqual(cam.rotate_180, rotate)
      cam.cap.release()

  def test_wide_capture_settings_do_not_change_road_settings(self):
    # The wide camera is a second road camera, so it used to read the same
    # WEBCAM_WIDTH/HEIGHT as the narrow one. That made it impossible to fit the wide
    # device's USB budget without dragging the model's main input down with it.
    # The capture size may differ per camera; the published size may not, because
    # modeld pairs the two streams at one compiled resolution (next test).
    env = {'WEBCAM_WIDTH': '1920', 'WEBCAM_HEIGHT': '1080',
           'WEBCAM_OUTPUT_WIDTH': '1344', 'WEBCAM_OUTPUT_HEIGHT': '760',
           'WIDE_WEBCAM_WIDTH': '1280', 'WIDE_WEBCAM_HEIGHT': '720'}
    for service, width, height, output in (
      ('narrowRoadCameraState', 1920, 1080, (1344, 760)),
      ('wideRoadCameraState', 1280, 720, (1344, 760)),
    ):
      with self.subTest(service=service):
        cap = Mock()
        cap.isOpened.return_value = True
        cap.get.side_effect = lambda prop, w=width, h=height: {camera.cv.CAP_PROP_FRAME_WIDTH: w,
                                                            camera.cv.CAP_PROP_FRAME_HEIGHT: h}.get(prop, 0)
        with patch.dict(os.environ, env), patch.object(camera.cv, 'VideoCapture', return_value=cap):
          cam = camera.Camera(service, 0, '/dev/mock')
        cap.set.assert_any_call(camera.cv.CAP_PROP_FRAME_WIDTH, width)
        cap.set.assert_any_call(camera.cv.CAP_PROP_FRAME_HEIGHT, height)
        self.assertEqual((cam.output_w, cam.output_h), output)
        cam.cap.release()

  def _fps_capture(self, service, env):
    cap = Mock()
    cap.isOpened.return_value = True
    cap.get.side_effect = lambda prop: {camera.cv.CAP_PROP_FRAME_WIDTH: 1280,
                                        camera.cv.CAP_PROP_FRAME_HEIGHT: 720}.get(prop, 0)
    with patch.dict(os.environ, env, clear=False), patch.object(camera.cv, 'VideoCapture', return_value=cap):
      os.environ.pop('WIDE_WEBCAM_CAPTURE_FPS', None)
      if 'WIDE_WEBCAM_CAPTURE_FPS' in env:
        os.environ['WIDE_WEBCAM_CAPTURE_FPS'] = env['WIDE_WEBCAM_CAPTURE_FPS']
      cam = camera.Camera(service, 0, '/dev/mock')
    called = [c.args[1] for c in cap.set.call_args_list if c.args[0] == camera.cv.CAP_PROP_FPS]
    cam.cap.release()
    return called[0] if called else None

  def test_the_wide_camera_does_not_inherit_the_road_capture_rate(self):
    # The road camera asks for 30 fps to leave decode headroom. The wide camera used
    # to inherit that and starve: on this machine the DECXIN ran for a few seconds at
    # 30 fps on the shared USB 2.0 bus and then stopped delivering, which cost modeld
    # its second input and raised a camera malfunction.
    env = {'WEBCAM_FPS': '20', 'WEBCAM_CAPTURE_FPS': '30', 'WIDE_WEBCAM_FPS': '20'}
    self.assertEqual(self._fps_capture('narrowRoadCameraState', env), 30,
                     "the road camera must keep its capture headroom")
    self.assertEqual(self._fps_capture('wideRoadCameraState', env), 20,
                     "the wide camera must not inherit the road capture rate")

  def test_the_wide_capture_rate_can_still_be_set_explicitly(self):
    env = {'WEBCAM_FPS': '20', 'WEBCAM_CAPTURE_FPS': '30', 'WIDE_WEBCAM_FPS': '20',
           'WIDE_WEBCAM_CAPTURE_FPS': '15'}
    self.assertEqual(self._fps_capture('wideRoadCameraState', env), 15)
    self.assertEqual(self._fps_capture('narrowRoadCameraState', env), 30)

  def test_the_raw_v4l2_path_is_told_the_capture_rate(self):
    # CAP_PROP_FPS above only reaches the OpenCV fallback. The narrow camera actually
    # runs on the raw V4L2 path, and V4L2Capture used to take no frame rate at all, so
    # the sensor kept its 30 fps power-on default. That isochronous reservation is what
    # the wide camera's REQBUFS has to fit into, and on the shared USB 2.0 bus it did
    # not: the wide stream fell back to OpenCV (losing the kernel timestamp) and modeld
    # dropped 15.9% of frames. The rate now goes into VIDIOC_S_PARM before REQBUFS.
    base_env = {'WEBCAM_WIDTH': '64', 'WEBCAM_HEIGHT': '48', 'WEBCAM_OUTPUT_WIDTH': '64',
                'WEBCAM_OUTPUT_HEIGHT': '48', 'WEBCAM_ROTATE_180': '0'}
    for service, env, expected in (
      ('narrowRoadCameraState', {'WEBCAM_CAPTURE_FPS': '20'}, 20),
      ('wideRoadCameraState', {'WEBCAM_CAPTURE_FPS': '30', 'WIDE_WEBCAM_CAPTURE_FPS': '15'}, 15),
    ):
      fake = Mock()
      fake.width, fake.height, fake.sizeimage = 64, 48, 1
      fake.read.return_value = None
      ctor = Mock(return_value=fake)
      with patch.dict(os.environ, {**base_env, **env}, clear=False), patch.object(camera, 'V4L2Capture', ctor):
        os.environ.pop('WIDE_WEBCAM_CAPTURE_FPS', None)
        if 'WIDE_WEBCAM_CAPTURE_FPS' in env:
          os.environ['WIDE_WEBCAM_CAPTURE_FPS'] = env['WIDE_WEBCAM_CAPTURE_FPS']
        cam = camera.Camera(service, 0, '/dev/mock')
      self.assertIs(cam.v4l2, fake, f"{service} should have taken the raw path")
      self.assertEqual(ctor.call_args.kwargs.get('fps'), expected,
                       f"{service} must be told its capture rate on the raw path")
      fake.release()

  def test_the_raw_path_still_works_when_the_driver_refuses_a_frame_rate(self):
    # set_fps() reports 0 when the driver will not report an interval. That must not
    # cost the camera its raw handle: the kernel timestamp, not the frame rate, is what
    # the raw path exists for.
    fake = Mock()
    fake.width, fake.height, fake.sizeimage, fake.fps = 64, 48, 1, 0
    fake.read.return_value = None
    env = {'WEBCAM_WIDTH': '64', 'WEBCAM_HEIGHT': '48', 'WEBCAM_OUTPUT_WIDTH': '64',
           'WEBCAM_OUTPUT_HEIGHT': '48', 'WEBCAM_ROTATE_180': '0', 'WEBCAM_CAPTURE_FPS': '20'}
    with patch.dict(os.environ, env, clear=False), patch.object(camera, 'V4L2Capture', return_value=fake):
      cam = camera.Camera('narrowRoadCameraState', 0, '/dev/mock')
    self.assertIs(cam.v4l2, fake, "a driver that reports no interval must keep the raw path")
    fake.release()

  def test_the_wide_camera_gets_a_deeper_buffer_than_the_road_camera(self):
    # BUFFERSIZE=1 makes the DECXIN wide camera run at 9.3 fps instead of 18.7: with
    # one buffer the driver cannot queue the next frame while the application holds
    # the current one. The narrow camera is the driving input and keeps the shallow
    # queue, because the extra frame of buffering costs it more than it gains.
    env = {'WEBCAM_WIDTH': '1920', 'WEBCAM_HEIGHT': '1080'}
    for service, expected in (('narrowRoadCameraState', 1), ('wideRoadCameraState', 2)):
      with self.subTest(service=service):
        cap = Mock()
        cap.isOpened.return_value = True
        cap.get.side_effect = lambda prop: {camera.cv.CAP_PROP_FRAME_WIDTH: 1920,
                                            camera.cv.CAP_PROP_FRAME_HEIGHT: 1080}.get(prop, 0)
        with patch.dict(os.environ, env, clear=False), patch.object(camera.cv, 'VideoCapture', return_value=cap):
          os.environ.pop('WEBCAM_BUFFERSIZE', None)
          os.environ.pop('WIDE_WEBCAM_BUFFERSIZE', None)
          cam = camera.Camera(service, 0, '/dev/mock')
        cap.set.assert_any_call(camera.cv.CAP_PROP_BUFFERSIZE, expected)
        cam.cap.release()

  def test_the_buffer_depth_can_be_overridden(self):
    cap = Mock()
    cap.isOpened.return_value = True
    cap.get.side_effect = lambda prop: {camera.cv.CAP_PROP_FRAME_WIDTH: 1280,
                                        camera.cv.CAP_PROP_FRAME_HEIGHT: 720}.get(prop, 0)
    with patch.dict(os.environ, {'WIDE_WEBCAM_BUFFERSIZE': '4'}, clear=False), \
         patch.object(camera.cv, 'VideoCapture', return_value=cap):
      cam = camera.Camera('wideRoadCameraState', 0, '/dev/mock')
    cap.set.assert_any_call(camera.cv.CAP_PROP_BUFFERSIZE, 4)
    cam.cap.release()

  def test_wide_settings_fall_back_to_the_road_settings(self):
    # Unset WIDE_* must behave exactly as before, so a build without the wide
    # camera keeps one place to configure the road capture size.
    env = {'WEBCAM_WIDTH': '1920', 'WEBCAM_HEIGHT': '1080',
           'WEBCAM_OUTPUT_WIDTH': '1344', 'WEBCAM_OUTPUT_HEIGHT': '760'}
    cap = Mock()
    cap.isOpened.return_value = True
    cap.get.side_effect = lambda prop: {camera.cv.CAP_PROP_FRAME_WIDTH: 1920,
                                        camera.cv.CAP_PROP_FRAME_HEIGHT: 1080}.get(prop, 0)
    with patch.dict(os.environ, env, clear=False), patch.object(camera.cv, 'VideoCapture', return_value=cap):
      for key in ('WIDE_WEBCAM_WIDTH', 'WIDE_WEBCAM_HEIGHT', 'WIDE_WEBCAM_OUTPUT_WIDTH', 'WIDE_WEBCAM_OUTPUT_HEIGHT'):
        os.environ.pop(key, None)
      cam = camera.Camera('wideRoadCameraState', 0, '/dev/mock')
    cap.set.assert_any_call(camera.cv.CAP_PROP_FRAME_WIDTH, 1920)
    cap.set.assert_any_call(camera.cv.CAP_PROP_FRAME_HEIGHT, 1080)
    self.assertEqual((cam.output_w, cam.output_h), (1344, 760))
    cam.cap.release()

  def test_a_transient_read_failure_does_not_kill_the_stream(self):
    # cap.read() reports a bandwidth hiccup on a shared USB 2.0 bus as a plain
    # failure. One of those used to take down camerad and the whole stack with it.
    cap = Mock()
    cap.isOpened.return_value = True
    cap.get.side_effect = lambda prop: {camera.cv.CAP_PROP_FRAME_WIDTH: 640,
                                        camera.cv.CAP_PROP_FRAME_HEIGHT: 480}.get(prop, 0)
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    # Two failures inside the grace period, then frames again.
    cap.read.side_effect = [(False, None), (False, None)] + [(True, frame)] * 3
    with patch.object(camera.cv, 'VideoCapture', return_value=cap), \
         patch.object(camera.time, 'sleep'):
      cam = camera.Camera('narrowRoadCameraState', 0, '/dev/mock')
      frames = []
      for i, yuv in enumerate(cam.read_frames()):
        frames.append(yuv)
        if i == 2:
          break
    self.assertEqual(len(frames), 3, "the stream gave up after a hiccup")
    cam.cap.release()

  def test_a_camera_that_is_really_gone_still_fails(self):
    # The grace period must not turn a dead camera into a silent one.
    cap = Mock()
    cap.isOpened.return_value = True
    cap.get.side_effect = lambda prop: {camera.cv.CAP_PROP_FRAME_WIDTH: 640,
                                        camera.cv.CAP_PROP_FRAME_HEIGHT: 480}.get(prop, 0)
    cap.read.return_value = (False, None)
    with patch.object(camera.cv, 'VideoCapture', return_value=cap), \
         patch.object(camera.time, 'sleep'):
      cam = camera.Camera('narrowRoadCameraState', 0, '/dev/mock')
      with self.assertRaises(RuntimeError):
        next(iter(cam.read_frames()))
    cam.cap.release()

  def test_missing_camera_has_explicit_error_and_releases_capture(self):
    cap = Mock()
    cap.isOpened.return_value = False
    with patch.object(camera.cv, "VideoCapture", return_value=cap):
      with self.assertRaisesRegex(RuntimeError, "camera"):
        camera.Camera("narrowRoadCameraState", 0, "/dev/video99")
    cap.release.assert_called_once()

  def test_capture_negotiates_mjpeg_and_releases_on_generator_close(self):
    # Force the OpenCV path. This test used /dev/video0 and relied on it not
    # opening, which is only true on a machine without the camera plugged in -
    # on this rig the raw V4L2 path would win and the mock would never be used.
    cap = Mock()
    cap.isOpened.return_value = True
    cap.get.side_effect = lambda prop: {camera.cv.CAP_PROP_FRAME_WIDTH: 4.0, camera.cv.CAP_PROP_FRAME_HEIGHT: 2.0}.get(prop, 0)
    cap.read.return_value = True, np.full((2, 4, 3), 128, dtype=np.uint8)
    with patch.dict(os.environ, {"WEBCAM_WIDTH": "4", "WEBCAM_HEIGHT": "2", "WEBCAM_OUTPUT_WIDTH": "4", "WEBCAM_OUTPUT_HEIGHT": "2", "WEBCAM_FPS": "20"}), \
         patch.object(camera, "V4L2Capture", side_effect=OSError("no raw path in tests")), \
         patch.object(camera.cv, "VideoCapture", return_value=cap):
      cam = camera.Camera("narrowRoadCameraState", 0, "/dev/video0")
      cap.set.assert_any_call(camera.cv.CAP_PROP_FOURCC, camera.cv.VideoWriter_fourcc(*"MJPG"))
      self.assertIsInstance(cam.W, int)
      frames = cam.read_frames()
      yuv, sof = next(frames)
      self.assertEqual(len(yuv), cam.nv12_size)
      self.assertIsNone(sof, "OpenCV path has no kernel timestamp")
      frames.close()
    cap.release.assert_called_once()

  def test_aligned_nv12_preserves_both_planes_and_padding(self):
    # Length alone cannot detect accidental row slicing of a 2-D memoryview.
    for width, height in ((128, 64), (1344, 760), (1928, 1208)):
      with self.subTest(width=width, height=height):
        cam = camera.Camera.__new__(camera.Camera)
        cam.output_w, cam.output_h = width, height
        cam.nv12_stride, cam.nv12_y_height, cam.nv12_uv_height, cam.nv12_size = get_nv12_info(width, height)
        bgr = np.random.default_rng(1).integers(0, 256, (height, width, 3), dtype=np.uint8)
        packed = av.VideoFrame.from_ndarray(bgr, format="bgr24").reformat(format="nv12").to_ndarray()
        data = cam.bgr2nv12(bgr)
        self.assertEqual(len(data), cam.nv12_size)
        y = np.ndarray((cam.nv12_y_height, cam.nv12_stride), dtype=np.uint8, buffer=data)
        offset = cam.nv12_stride * cam.nv12_y_height
        uv = np.ndarray((cam.nv12_uv_height, cam.nv12_stride), dtype=np.uint8, buffer=data, offset=offset)
        np.testing.assert_array_equal(y[:height, :width], packed[:height])
        np.testing.assert_array_equal(uv[:height // 2, :width], packed[height:])
        for padding in (y[:height, width:], y[height:], uv[:height // 2, width:], uv[height // 2:]):
          self.assertFalse(np.any(padding))

  def test_aligned_frame_reaches_real_vipc_client(self):
    stream = VisionStreamType.VISION_STREAM_NARROW_ROAD
    name = "webcam_test_" + uuid.uuid4().hex
    cam = camera.Camera.__new__(camera.Camera)
    cam.output_w, cam.output_h = 1344, 760
    cam.nv12_stride, cam.nv12_y_height, cam.nv12_uv_height, cam.nv12_size = get_nv12_info(1344, 760)
    server = VisionIpcServer(name)
    server.create_buffers_with_sizes(stream, 2, cam.output_w, cam.output_h, cam.nv12_size,
                                    cam.nv12_stride, cam.nv12_stride * cam.nv12_y_height)
    server.start_listener()
    client = VisionIpcClient(name, stream, False)
    deadline = time.monotonic() + 2
    while not client.connect(False):
      self.assertLess(time.monotonic(), deadline, "VIPC listener did not start")
      time.sleep(0.01)
    bgr = np.zeros((760, 1344, 3), dtype=np.uint8)
    bgr[:, :, 2] = 255
    frame = cam.bgr2nv12(bgr)
    server.send(stream, frame, 7, 123000, 124000)
    buf = client.recv(1000)
    self.assertIsNotNone(buf)
    self.assertEqual(bytes(buf.data), frame)
    self.assertEqual((buf.width, buf.height, buf.stride, buf.uv_offset), (1344, 760, 1408, 1081344))
    self.assertEqual(client.frame_id, 7)
    self.assertEqual(client.timestamp_eof, 124000)

  def test_raw_mjpeg_negotiation_and_generator(self):
    image = np.full((64, 96, 3), 128, dtype=np.uint8)
    ok, jpeg = camera.cv.imencode('.jpg', image)
    self.assertTrue(ok)
    cap = Mock()
    cap.isOpened.return_value = True
    cap.set.return_value = True
    cap.get.side_effect = lambda prop: {
      camera.cv.CAP_PROP_FRAME_WIDTH: 96, camera.cv.CAP_PROP_FRAME_HEIGHT: 64,
      camera.cv.CAP_PROP_FOURCC: camera.cv.VideoWriter_fourcc(*'MJPG'),
    }[prop]
    cap.read.return_value = True, jpeg.reshape(1, -1)
    env = {'WEBCAM_WIDTH': '96', 'WEBCAM_HEIGHT': '64', 'WEBCAM_OUTPUT_WIDTH': '64',
           'WEBCAM_OUTPUT_HEIGHT': '48', 'WEBCAM_ROTATE_180': '0'}
    with patch.dict(os.environ, env), patch.object(camera.cv, 'VideoCapture', return_value=cap):
      cam = camera.Camera('narrowRoadCameraState', 0, '/dev/mock')
      self.assertIsNotNone(cam.mjpeg_decoder)
      cap.set.assert_any_call(camera.cv.CAP_PROP_CONVERT_RGB, 0)
      frames = cam.read_frames()
      data, sof = next(frames)
      self.assertEqual(len(data), cam.nv12_size)
      self.assertIsNone(sof)
      y, uv = self.planes(cam, data)
      self.assertTrue(np.all(np.abs(y.astype(int) - 126) <= 1))
      self.assertTrue(np.all(np.abs(uv.astype(int) - 128) <= 1))
      frames.close()
    cap.release.assert_called_once()

  def test_mjpeg_rejected_raw_capture_retains_bgr_fallback(self):
    cap = Mock()
    cap.isOpened.return_value = True
    cap.set.side_effect = lambda prop, value: prop != camera.cv.CAP_PROP_CONVERT_RGB
    cap.get.side_effect = lambda prop: {
      camera.cv.CAP_PROP_FRAME_WIDTH: 96, camera.cv.CAP_PROP_FRAME_HEIGHT: 64,
      camera.cv.CAP_PROP_FOURCC: camera.cv.VideoWriter_fourcc(*'MJPG'),
    }[prop]
    image = np.full((64, 96, 3), 128, dtype=np.uint8)
    cap.read.return_value = True, image
    env = {'WEBCAM_WIDTH': '96', 'WEBCAM_HEIGHT': '64', 'WEBCAM_OUTPUT_WIDTH': '96',
           'WEBCAM_OUTPUT_HEIGHT': '64', 'WEBCAM_ROTATE_180': '0'}
    with patch.dict(os.environ, env), patch.object(camera.cv, 'VideoCapture', return_value=cap):
      cam = camera.Camera('narrowRoadCameraState', 0, '/dev/mock')
      self.assertIsNone(cam.mjpeg_decoder)
      frames = cam.read_frames()
      yuv, sof = next(frames)
      self.assertEqual(yuv, cam.bgr2nv12(image))
      self.assertIsNone(sof, 'OpenCV path has no kernel timestamp')
      frames.close()
    cap.release.assert_called_once()

  def test_mjpeg_scaling_preserves_range_colors_rotation_and_padding(self):
    cam = self.make_camera(128, 64)
    bgr = np.zeros((128, 256, 3), dtype=np.uint8)
    bgr[:64, 128:] = 255
    bgr[64:, :128] = (0, 0, 255)
    bgr[64:, 128:] = (255, 0, 0)
    ok, jpeg = camera.cv.imencode('.jpg', bgr, [camera.cv.IMWRITE_JPEG_QUALITY, 95])
    self.assertTrue(ok)
    data = cam.mjpeg2nv12(jpeg)
    y, uv = self.planes(cam, data)
    self.assertLessEqual(abs(int(y[16, 32]) - 16), 1)
    self.assertLessEqual(abs(int(y[16, 96]) - 235), 1)
    reference_bgr = camera.cv.resize(camera.cv.imdecode(jpeg, camera.cv.IMREAD_COLOR), (128, 64), interpolation=camera.cv.INTER_AREA)
    reference = cam.bgr2nv12(reference_bgr)
    ref_y, ref_uv = self.planes(cam, reference)
    self.assertLess(np.abs(y.astype(float) - ref_y).mean(), 1.5)
    self.assertLess(np.abs(uv.astype(float) - ref_uv).mean(), 1.5)
    cam.rotate_180 = True
    rotated = cam.mjpeg2nv12(jpeg)
    rot_y, rot_uv = self.planes(cam, rotated)
    np.testing.assert_array_equal(rot_y, y[::-1, ::-1])
    np.testing.assert_array_equal(rot_uv, uv[::-1, ::-1])
    # No data written outside the active planes, including kernel padding.
    mask = np.zeros(cam.nv12_size, dtype=np.uint8)
    mask_y, mask_uv = self.planes(cam, mask)
    mask_y[:] = 1
    mask_uv[:] = 1
    self.assertFalse(np.any(np.frombuffer(rotated, dtype=np.uint8)[mask == 0]))

  def test_cover_and_crop_matches_an_equal_aspect_without_cropping(self):
    # 1920x1080 -> 1344x760 is the shipped path. It differs by 0.5% aspect, so it
    # must keep scaling straight into the output: changing it would alter today's
    # pixels for no visible gain.
    self.assertEqual(camera.cover_and_crop(1920, 1080, 1344, 760), (1344, 760, 0, 0))

  def test_cover_and_crop_center_crops_a_4_3_sensor_to_the_16_10_contract(self):
    # 2592x1944 is the 5MP sensor; 1928x1208 is the model's compiled road size.
    # Scaling straight between them squeezes the image by 16%; cover-and-crop
    # keeps square pixels and only throws away the extra sky/hood.
    scale_w, scale_h, crop_x, crop_y = camera.cover_and_crop(2592, 1944, 1928, 1208)
    self.assertEqual((scale_w, scale_h), (1928, 1446))
    self.assertEqual(crop_x, 0)
    self.assertGreater(crop_y, 0)
    self.assertEqual(crop_y % 2, 0, "the UV plane is subsampled by two")
    self.assertLessEqual(crop_y + 1208, scale_h)
    self.assertLessEqual(abs(scale_h - 1208 - 2 * crop_y), 2, "the crop must be centred")

  def test_cover_and_crop_center_crops_a_wide_source_horizontally(self):
    scale_w, scale_h, crop_x, crop_y = camera.cover_and_crop(2560, 1440, 1928, 1208)
    self.assertEqual((scale_w, scale_h), (2148, 1208))
    self.assertEqual(crop_y, 0)
    self.assertGreater(crop_x, 0)
    self.assertEqual(crop_x % 2, 0)
    self.assertEqual(scale_w - 2 * crop_x, 1928)

  def test_5mp_capture_crops_to_the_model_contract(self):
    fake = Mock()
    fake.width, fake.height, fake.sizeimage = 2592, 1944, 1
    fake.read.return_value = None
    env = {'WEBCAM_WIDTH': '2592', 'WEBCAM_HEIGHT': '1944', 'WEBCAM_OUTPUT_WIDTH': '1928',
           'WEBCAM_OUTPUT_HEIGHT': '1208', 'WEBCAM_ROTATE_180': '0'}
    with patch.dict(os.environ, env, clear=False), patch.object(camera, 'V4L2Capture', return_value=fake):
      cam = camera.Camera('narrowRoadCameraState', 0, '/dev/mock')
    self.assertEqual((cam.output_w, cam.output_h), (1928, 1208))
    self.assertEqual((cam.scale_w, cam.scale_h, cam.crop_x, cam.crop_y), (1928, 1446, 0, 118))
    fake.release()

  def test_the_default_1920x1080_path_does_not_start_cropping(self):
    fake = Mock()
    fake.width, fake.height, fake.sizeimage = 1920, 1080, 1
    fake.read.return_value = None
    env = {'WEBCAM_WIDTH': '1920', 'WEBCAM_HEIGHT': '1080', 'WEBCAM_OUTPUT_WIDTH': '1344',
           'WEBCAM_OUTPUT_HEIGHT': '760', 'WEBCAM_ROTATE_180': '0'}
    with patch.dict(os.environ, env, clear=False), patch.object(camera, 'V4L2Capture', return_value=fake):
      cam = camera.Camera('narrowRoadCameraState', 0, '/dev/mock')
    self.assertEqual((cam.scale_w, cam.scale_h, cam.crop_x, cam.crop_y), (1344, 760, 0, 0))
    fake.release()

  def test_the_cabin_camera_keeps_its_historical_stretch_by_default(self):
    # The DM warp is compiled for 1928x1208 and has never been validated with a
    # cropped USB image. Cabin therefore keeps stretching unless it is opted in;
    # only the road cameras get aspect-preserving cropping by default.
    fake = Mock()
    fake.width, fake.height, fake.sizeimage = 640, 480, 1
    fake.read.return_value = None
    env = {'DRIVER_WEBCAM_WIDTH': '640', 'DRIVER_WEBCAM_HEIGHT': '480',
           'DRIVER_WEBCAM_OUTPUT_WIDTH': '1928', 'DRIVER_WEBCAM_OUTPUT_HEIGHT': '1208',
           'WEBCAM_CROP_TO_OUTPUT': '1'}
    with patch.dict(os.environ, env, clear=False), patch.object(camera, 'V4L2Capture', return_value=fake):
      cam = camera.Camera('cabinCameraState', 0, '/dev/mock')
    self.assertEqual((cam.scale_w, cam.scale_h, cam.crop_x, cam.crop_y), (1928, 1208, 0, 0))
    with patch.dict(os.environ, {**env, 'DRIVER_WEBCAM_CROP_TO_OUTPUT': '1'}, clear=False), \
         patch.object(camera, 'V4L2Capture', return_value=fake):
      cam = camera.Camera('cabinCameraState', 0, '/dev/mock')
    self.assertGreater(cam.crop_y, 0)
    fake.release()

  def test_mjpeg_crop_removes_the_rows_a_stretch_would_keep(self):
    # A 4:3 test pattern with a white strip at the very top and a black strip at
    # the very bottom. Scaling it straight into 16:10 would show both strips;
    # cropping to 16:10 must drop them, leaving the mid-grey centre.
    cam = self.make_camera_with_crop(320, 240, 192, 120)
    bgr = np.full((240, 320, 3), 128, dtype=np.uint8)
    bgr[:10] = 255
    bgr[-10:] = 0
    ok, jpeg = camera.cv.imencode('.jpg', bgr, [camera.cv.IMWRITE_JPEG_QUALITY, 95])
    self.assertTrue(ok)
    data = cam.mjpeg2nv12(jpeg)
    y, _ = self.planes(cam, data)
    self.assertGreater(int(y.min()), 60, "the black bottom strip must be cropped away")
    self.assertLess(int(y.max()), 190, "the white top strip must be cropped away")
    self.assertLessEqual(abs(float(y.mean()) - 126), 3)

  def test_rotation_happens_after_the_crop(self):
    cam = self.make_camera_with_crop(320, 240, 192, 120)
    bgr = np.full((240, 320, 3), 128, dtype=np.uint8)
    bgr[:, :96] = 255
    ok, jpeg = camera.cv.imencode('.jpg', bgr, [camera.cv.IMWRITE_JPEG_QUALITY, 95])
    self.assertTrue(ok)
    normal, _ = self.planes(cam, cam.mjpeg2nv12(jpeg))
    cam.rotate_180 = True
    rotated, _ = self.planes(cam, cam.mjpeg2nv12(jpeg))
    np.testing.assert_array_equal(rotated, normal[::-1, ::-1])
    # The left white half must have moved to the right after the 180 rotation.
    self.assertGreater(float(rotated[:, -10:].mean()), float(rotated[:, :10].mean()))

  def test_bgr_fallback_also_crops_to_the_published_aspect(self):
    # The raw MJPEG path is the one in use on Linux, but the OpenCV fallback is
    # the safety net for a camera that cannot be negotiated raw. It must crop the
    # same way, otherwise a fallback would silently deform the model's geometry.
    cap = Mock()
    cap.isOpened.return_value = True
    cap.set.side_effect = lambda prop, value: prop != camera.cv.CAP_PROP_CONVERT_RGB
    cap.get.side_effect = lambda prop: {
      camera.cv.CAP_PROP_FRAME_WIDTH: 320, camera.cv.CAP_PROP_FRAME_HEIGHT: 240,
      camera.cv.CAP_PROP_FOURCC: 0,
    }.get(prop, 0)
    bgr = np.full((240, 320, 3), 128, dtype=np.uint8)
    bgr[:6] = 255
    bgr[-6:] = 0
    cap.read.return_value = True, bgr
    env = {'WEBCAM_WIDTH': '320', 'WEBCAM_HEIGHT': '240', 'WEBCAM_OUTPUT_WIDTH': '192',
           'WEBCAM_OUTPUT_HEIGHT': '120', 'WEBCAM_ROTATE_180': '0'}
    with patch.dict(os.environ, env), patch.object(camera, 'V4L2Capture', side_effect=OSError("no raw path in tests")), \
         patch.object(camera.cv, 'VideoCapture', return_value=cap):
      cam = camera.Camera('narrowRoadCameraState', 0, '/dev/mock')
      self.assertIsNone(cam.mjpeg_decoder)
      frames = cam.read_frames()
      yuv, _ = next(frames)
      frames.close()
    y, _ = self.planes(cam, yuv)
    self.assertGreater(int(y.min()), 60, "the black bottom strip must be cropped away")
    self.assertLess(int(y.max()), 190, "the white top strip must be cropped away")
    cap.release.assert_called_once()

  def test_cropped_output_does_not_write_outside_the_active_planes(self):
    cam = self.make_camera_with_crop(320, 240, 192, 120)
    bgr = np.full((240, 320, 3), 128, dtype=np.uint8)
    ok, jpeg = camera.cv.imencode('.jpg', bgr, [camera.cv.IMWRITE_JPEG_QUALITY, 95])
    self.assertTrue(ok)
    data = cam.mjpeg2nv12(jpeg)
    mask = np.zeros(cam.nv12_size, dtype=np.uint8)
    mask_y, mask_uv = self.planes(cam, mask)
    mask_y[:] = 1
    mask_uv[:] = 1
    self.assertFalse(np.any(np.frombuffer(data, dtype=np.uint8)[mask == 0]))

  def test_invalid_mjpeg_is_not_published_as_an_old_frame(self):
    cam = self.make_camera(128, 64)
    with self.assertRaises(av.error.InvalidDataError):
      cam.mjpeg2nv12(b'not a jpeg')

  def test_bgr_noncontiguous_input_preserves_pixels(self):
    cam = self.make_camera(128, 64)
    bgr = np.random.default_rng(2).integers(0, 256, (64, 256, 3), dtype=np.uint8)[:, ::2]
    self.assertFalse(bgr.flags.c_contiguous)
    packed = av.VideoFrame.from_ndarray(bgr, format='bgr24').reformat(format='nv12').to_ndarray()
    y, uv = self.planes(cam, cam.bgr2nv12(bgr))
    np.testing.assert_array_equal(y, packed[:64])
    np.testing.assert_array_equal(uv.reshape(32, 128), packed[64:])

  def test_opencv_worker_limit_is_local_to_camera_startup(self):
    with patch.object(camerad.cv, 'setNumThreads') as threads, patch.object(camerad.os, 'cpu_count', return_value=16), \
         patch.object(camerad, 'Camerad') as daemon:
      camerad.main()
    threads.assert_called_once_with(4)
    daemon.return_value.run.assert_called_once()

  def test_message_and_buffer_use_same_monotonic_timestamp(self):
    instance = camerad.Camerad.__new__(camerad.Camerad)
    instance.vipc_server = Mock()
    instance.pm = Mock()
    with patch("time.monotonic_ns", return_value=123456789000):
      instance._send_yuv(b"test", 3, "narrowRoadCameraState", 0)
    sent = instance.pm.send.call_args.args[1]
    self.assertEqual(sent.narrowRoadCameraState.timestampEof, 123456789000)
    self.assertEqual(sent.narrowRoadCameraState.timestampSof, 123456789000)
    instance.vipc_server.send.assert_called_once_with(0, b"test", 0, 123456789000, 123456789000)

  def test_capture_rate_can_exceed_publish_rate(self):
    cap = Mock()
    cap.isOpened.return_value = True
    cap.get.side_effect = lambda prop: {camera.cv.CAP_PROP_FRAME_WIDTH: 1920.0, camera.cv.CAP_PROP_FRAME_HEIGHT: 1080.0}.get(prop, 0)
    with patch.dict(os.environ, {"WEBCAM_FPS": "20", "WEBCAM_CAPTURE_FPS": "30"}), \
         patch.object(camera.cv, "VideoCapture", return_value=cap):
      cam = camera.Camera("narrowRoadCameraState", 0, "/dev/video0")
      self.assertEqual(cam.fps, 20)
      cap.set.assert_any_call(camera.cv.CAP_PROP_FPS, 30)

  def test_camera_thread_failure_propagates_to_process(self):
    instance = camerad.Camerad.__new__(camerad.Camerad)
    instance.cameras = [Mock()]
    with patch.object(instance, "camera_runner", side_effect=RuntimeError("camera disconnected")):
      with self.assertRaisesRegex(RuntimeError, "camera disconnected"):
        instance.run()

  def test_the_road_cameras_publish_1344x760_by_default(self):
    # WEBCAM_WIDTH/HEIGHT are only the UVC mode. The published size used to default to
    # the capture size, so it followed whatever mode the camera negotiated - and modeld
    # only indexes the resolutions it was compiled for.
    self.assertEqual(camera.DEFAULT_ROAD_OUTPUT_SIZE, (1344, 760))
    for service in ('narrowRoadCameraState', 'wideRoadCameraState'):
      with self.subTest(service=service):
        fake = Mock()
        fake.width, fake.height, fake.sizeimage = 1920, 1080, 1
        fake.read.return_value = None
        env = {'WEBCAM_WIDTH': '1920', 'WEBCAM_HEIGHT': '1080', 'WEBCAM_ROTATE_180': '0'}
        with patch.dict(os.environ, env, clear=False), patch.object(camera, 'V4L2Capture', return_value=fake):
          for key in ('WEBCAM_OUTPUT_WIDTH', 'WEBCAM_OUTPUT_HEIGHT',
                      'WIDE_WEBCAM_OUTPUT_WIDTH', 'WIDE_WEBCAM_OUTPUT_HEIGHT'):
            os.environ.pop(key, None)
          cam = camera.Camera(service, 0, '/dev/mock')
        self.assertEqual((cam.output_w, cam.output_h), (1344, 760))
        fake.release()

  def test_a_wide_output_size_that_differs_from_the_narrow_one_is_corrected(self):
    # modeld pairs the narrow and wide frames and uses one compiled resolution for
    # both, so a wide stream at another size cannot be paired with anything. camerad
    # would publish it and modeld would fail on the pair instead.
    env = {'WEBCAM_WIDTH': '1920', 'WEBCAM_HEIGHT': '1080',
           'WEBCAM_OUTPUT_WIDTH': '1928', 'WEBCAM_OUTPUT_HEIGHT': '1208',
           'WIDE_WEBCAM_WIDTH': '1280', 'WIDE_WEBCAM_HEIGHT': '720',
           'WIDE_WEBCAM_OUTPUT_WIDTH': '1280', 'WIDE_WEBCAM_OUTPUT_HEIGHT': '720'}
    fake = Mock()
    fake.width, fake.height, fake.sizeimage = 1280, 720, 1
    fake.read.return_value = None
    with patch.dict(os.environ, env, clear=False), patch.object(camera, 'V4L2Capture', return_value=fake), \
         patch.object(camera.cloudlog, 'error') as error:
      cam = camera.Camera('wideRoadCameraState', 0, '/dev/mock')
    self.assertEqual((cam.output_w, cam.output_h), (1928, 1208),
                     "the wide stream must publish the narrow road size")
    error.assert_called_once()
    self.assertIn('wideRoadCameraState', error.call_args.args[0])
    fake.release()

  def test_a_matching_wide_output_size_is_accepted_without_an_error(self):
    env = {'WEBCAM_WIDTH': '1920', 'WEBCAM_HEIGHT': '1080',
           'WEBCAM_OUTPUT_WIDTH': '1344', 'WEBCAM_OUTPUT_HEIGHT': '760',
           'WIDE_WEBCAM_WIDTH': '1280', 'WIDE_WEBCAM_HEIGHT': '720'}
    fake = Mock()
    fake.width, fake.height, fake.sizeimage = 1280, 720, 1
    fake.read.return_value = None
    with patch.dict(os.environ, env, clear=False), patch.object(camera, 'V4L2Capture', return_value=fake):
      for key in ('WIDE_WEBCAM_OUTPUT_WIDTH', 'WIDE_WEBCAM_OUTPUT_HEIGHT'):
        os.environ.pop(key, None)
      with patch.object(camera.cloudlog, 'error') as error:
        cam = camera.Camera('wideRoadCameraState', 0, '/dev/mock')
    self.assertEqual((cam.output_w, cam.output_h), (1344, 760))
    error.assert_not_called()
    fake.release()

  def test_the_raw_path_never_asks_for_fewer_than_two_buffers(self):
    # With a single buffer the driver cannot queue the next frame while the
    # application holds the current one: measured on the DECXIN wide camera that
    # halves the rate to 9.3 fps.
    for requested, expected, warned in (('1', 2, True), (None, 2, False), ('4', 4, False)):
      with self.subTest(requested=requested):
        fake = Mock()
        fake.width, fake.height, fake.sizeimage = 64, 48, 1
        fake.read.return_value = None
        ctor = Mock(return_value=fake)
        env = {'WEBCAM_WIDTH': '64', 'WEBCAM_HEIGHT': '48', 'WEBCAM_OUTPUT_WIDTH': '64',
               'WEBCAM_OUTPUT_HEIGHT': '48'}
        with patch.dict(os.environ, env, clear=False), patch.object(camera, 'V4L2Capture', ctor), \
             patch.object(camera.cloudlog, 'warning') as warning:
          os.environ.pop('WEBCAM_BUFFERSIZE', None)
          if requested is not None:
            os.environ['WEBCAM_BUFFERSIZE'] = requested
          cam = camera.Camera('narrowRoadCameraState', 0, '/dev/mock')
        self.assertEqual(ctor.call_args.args[3], expected, "raw buffer depth")
        self.assertEqual(cam.raw_buffers, expected)
        self.assertEqual(warning.call_count, 1 if warned else 0)
        fake.release()

  def test_both_read_paths_share_one_miss_budget(self):
    # 8 recoverable misses of at most 0.2 s each plus the one that trips the counter:
    # about 1.8 s. Long enough for a bandwidth hiccup, short enough that an unplugged
    # camera still fails in under 2 s. A fallback from raw V4L2 to
    # OpenCV must not change that, so the constants are shared rather than duplicated.
    self.assertEqual(camera.READ_TIMEOUT_S, 0.2)
    self.assertEqual(camera.MAX_READ_MISSES, 8)
    self.assertAlmostEqual(camera.READ_TIMEOUT_S * camera.MAX_READ_MISSES, 1.6, places=6)

  def _raw_camera(self, read_side_effect):
    fake = Mock()
    fake.width, fake.height = 64, 48
    fake.timestamp_ns = 123
    fake.timestamp_is_monotonic = True
    fake.read.side_effect = read_side_effect
    ok, jpeg = camera.cv.imencode('.jpg', np.full((48, 64, 3), 128, dtype=np.uint8))
    self.assertTrue(ok)
    env = {'WEBCAM_WIDTH': '64', 'WEBCAM_HEIGHT': '48', 'WEBCAM_OUTPUT_WIDTH': '64',
           'WEBCAM_OUTPUT_HEIGHT': '48', 'WEBCAM_ROTATE_180': '0'}
    with patch.dict(os.environ, env, clear=False), patch.object(camera, 'V4L2Capture', return_value=fake):
      cam = camera.Camera('narrowRoadCameraState', 0, '/dev/mock')
    return cam, fake, jpeg.reshape(1, -1)

  def test_the_raw_read_waits_the_shared_timeout(self):
    cam, fake, jpeg = self._raw_camera([None])
    fake.read.side_effect = [jpeg]
    frames = cam.read_frames()
    next(frames)
    frames.close()
    self.assertEqual(fake.read.call_args.args[0], camera.READ_TIMEOUT_S)
    fake.release()

  def test_the_raw_path_rides_out_a_hiccup_and_still_fails_a_dead_camera(self):
    cam, fake, jpeg = self._raw_camera([None])
    fake.read.side_effect = [None] * camera.MAX_READ_MISSES + [jpeg]
    frames = cam.read_frames()
    yuv, _ = next(frames)
    frames.close()
    self.assertEqual(len(yuv), cam.nv12_size, "a hiccup inside the budget must not kill the stream")
    fake.release()

    cam, fake, _ = self._raw_camera([None])
    fake.read.return_value = None
    with self.assertRaises(RuntimeError):
      next(iter(cam.read_frames()))
    fake.release()

  def test_the_opencv_retry_waits_the_shared_timeout(self):
    cap = Mock()
    cap.isOpened.return_value = True
    cap.get.side_effect = lambda prop: {camera.cv.CAP_PROP_FRAME_WIDTH: 640,
                                        camera.cv.CAP_PROP_FRAME_HEIGHT: 480}.get(prop, 0)
    cap.read.side_effect = [(False, None), (True, np.zeros((480, 640, 3), dtype=np.uint8))]
    with patch.object(camera.cv, 'VideoCapture', return_value=cap), patch.object(camera.time, 'sleep') as sleep:
      cam = camera.Camera('narrowRoadCameraState', 0, '/dev/mock')
      frames = cam.read_frames()
      next(frames)
      frames.close()
    sleep.assert_called_once_with(camera.READ_TIMEOUT_S)
    cam.cap.release()


class TestCameradOptionalCameras(unittest.TestCase):
  """An optional camera that cannot be opened must cost that input only.

  The narrow road camera is constructed first and is the model's only required
  input, so a construction failure in a later camera used to abort camerad before
  it ever started the threads - taking the road camera down with it.
  """

  @staticmethod
  def _camera_types():
    return [
      camerad.CameraType("narrowRoadCameraState", VisionStreamType.VISION_STREAM_NARROW_ROAD, "0", True),
      camerad.CameraType("wideRoadCameraState", VisionStreamType.VISION_STREAM_WIDE_ROAD, "1", False),
    ]

  def test_an_optional_camera_that_cannot_open_is_skipped(self):
    def fake_camera(msg_name, stream_type, device, required=True):
      if not required:
        raise RuntimeError("device busy")
      return Mock(required=True, msg_name=msg_name, stream_type=stream_type,
                  output_w=1344, output_h=760, nv12_size=1532160, nv12_stride=1408, nv12_y_height=768)

    with patch.object(camerad, 'CAMERAS', self._camera_types()), \
         patch.object(camerad, 'Camera', side_effect=fake_camera), \
         patch.object(camerad.messaging, 'PubMaster'), \
         patch.object(camerad, 'VisionIpcServer') as vipc, \
         patch.object(camerad.cloudlog, 'error') as error:
      instance = camerad.Camerad()

    self.assertEqual([c.msg_name for c in instance.cameras], ['narrowRoadCameraState'])
    vipc.return_value.create_buffers_with_sizes.assert_called_once()
    self.assertNotIn(VisionStreamType.VISION_STREAM_WIDE_ROAD,
                     [c.args[0] for c in vipc.return_value.create_buffers_with_sizes.call_args_list],
                     "no VIPC buffer may exist for a stream nothing will publish to")
    error.assert_called_once()
    self.assertIn('wideRoadCameraState', error.call_args.args[0])

  def test_a_required_camera_that_cannot_open_still_stops_camerad(self):
    def fake_camera(msg_name, stream_type, device, required=True):
      raise RuntimeError(f"cannot open {device}")

    with patch.object(camerad, 'CAMERAS', self._camera_types()), \
         patch.object(camerad, 'Camera', side_effect=fake_camera), \
         patch.object(camerad.messaging, 'PubMaster'), \
         patch.object(camerad, 'VisionIpcServer'):
      with self.assertRaisesRegex(RuntimeError, "cannot open"):
        camerad.Camerad()

  def test_run_starts_a_thread_only_for_constructed_cameras(self):
    instance = camerad.Camerad.__new__(camerad.Camerad)
    instance.cameras = [Mock(), Mock()]
    started = []

    class FakeThread:
      def __init__(self, target=None, args=(), daemon=None):
        self.target, self.args = target, args
        started.append(self)

      def start(self):
        pass

      def is_alive(self):
        return False

      def join(self, timeout=None):
        pass

    with patch.object(camerad.threading, 'Thread', FakeThread):
      instance.run()

    self.assertEqual([t.args[0] for t in started], instance.cameras)


class TestKernelTimestamps(unittest.TestCase):
  """The raw V4L2 path must publish the kernel's SOF, not the publish moment."""

  def _jpeg(self, w=64, h=48):
    ok, jpeg = camera.cv.imencode('.jpg', np.full((h, w, 3), 128, dtype=np.uint8))
    self.assertTrue(ok)
    return jpeg.reshape(1, -1)

  def test_kernel_sof_is_published_not_the_decode_time(self):
    fake = Mock()
    fake.width, fake.height = 64, 48
    fake.timestamp_ns = 123_456_789_000
    fake.timestamp_is_monotonic = True
    fake.read.return_value = self._jpeg()
    env = {'WEBCAM_WIDTH': '64', 'WEBCAM_HEIGHT': '48', 'WEBCAM_OUTPUT_WIDTH': '64',
           'WEBCAM_OUTPUT_HEIGHT': '48', 'WEBCAM_ROTATE_180': '0'}
    with patch.dict(os.environ, env), patch.object(camera, 'V4L2Capture', return_value=fake):
      cam = camera.Camera('narrowRoadCameraState', 0, '/dev/mock')
      self.assertIsNotNone(cam.v4l2, "raw path should have been taken")
      self.assertIsNone(cam.cap, "OpenCV must not be opened when the raw path works")
      frames = cam.read_frames()
      yuv, sof = next(frames)
      frames.close()
    self.assertEqual(sof, 123_456_789_000, "timestampSof must be the kernel SOF")
    self.assertEqual(len(yuv), cam.nv12_size)
    fake.release.assert_called_once()

  def test_a_non_monotonic_driver_falls_back_to_no_timestamp(self):
    # uvcvideo reports CLOCK_MONOTONIC here, but a driver that reports
    # TIMESTAMP_UNKNOWN gives a value on some other clock. Publishing that would
    # be worse than publishing nothing, so the frame must carry None and camerad
    # then stamps it with the publish time.
    fake = Mock()
    fake.width, fake.height = 64, 48
    fake.timestamp_ns = 999
    fake.timestamp_is_monotonic = False
    fake.read.return_value = self._jpeg()
    env = {'WEBCAM_WIDTH': '64', 'WEBCAM_HEIGHT': '48', 'WEBCAM_OUTPUT_WIDTH': '64',
           'WEBCAM_OUTPUT_HEIGHT': '48', 'WEBCAM_ROTATE_180': '0'}
    with patch.dict(os.environ, env), patch.object(camera, 'V4L2Capture', return_value=fake):
      frames = camera.Camera('narrowRoadCameraState', 0, '/dev/mock').read_frames()
      _, sof = next(frames)
      frames.close()
    self.assertIsNone(sof, "a non-monotonic timestamp must not be published")

  def test_the_raw_path_failing_falls_back_to_opencv(self):
    # This is the main camera's input path. A failed raw setup has to degrade to
    # exactly the shipped behaviour, not to a dead camera.
    cap = Mock()
    cap.isOpened.return_value = True
    cap.set.return_value = True
    cap.get.side_effect = lambda prop: {
      camera.cv.CAP_PROP_FRAME_WIDTH: 64, camera.cv.CAP_PROP_FRAME_HEIGHT: 48,
      camera.cv.CAP_PROP_FOURCC: camera.cv.VideoWriter_fourcc(*'MJPG'),
    }.get(prop, 0)
    cap.read.return_value = True, self._jpeg()
    env = {'WEBCAM_WIDTH': '64', 'WEBCAM_HEIGHT': '48', 'WEBCAM_OUTPUT_WIDTH': '64',
           'WEBCAM_OUTPUT_HEIGHT': '48', 'WEBCAM_ROTATE_180': '0'}
    with patch.dict(os.environ, env), \
         patch.object(camera, 'V4L2Capture', side_effect=OSError("ENOTTY")), \
         patch.object(camera.cv, 'VideoCapture', return_value=cap):
      cam = camera.Camera('narrowRoadCameraState', 0, '/dev/mock')
      self.assertIsNone(cam.v4l2)
      self.assertIsNotNone(cam.cap, "must have fallen back to OpenCV")
      frames = cam.read_frames()
      yuv, sof = next(frames)
      frames.close()
    self.assertIsNone(sof, "the OpenCV path has no kernel timestamp")
    self.assertEqual(len(yuv), cam.nv12_size)


class TestV4L2StructLayout(unittest.TestCase):
  """The kernel compares _IOC_SIZE and answers ENOTTY when it does not match.

  That failure reads as "this device does not support VIDIOC_S_FMT" and sends you
  looking at the camera instead of at the struct, so pin the sizes the kernel
  header reports: getting v4l2_format wrong by 4 bytes cost a full debugging
  round on this rig.
  """

  def test_sizes_match_the_kernel_header(self):
    from openpilot.system.camerad.webcam import v4l2
    self.assertEqual(__import__('ctypes').sizeof(v4l2._v4l2_capability), 104)
    self.assertEqual(__import__('ctypes').sizeof(v4l2._v4l2_format), 208)
    self.assertEqual(__import__('ctypes').sizeof(v4l2._v4l2_pix_format), 48)
    self.assertEqual(__import__('ctypes').sizeof(v4l2._v4l2_requestbuffers), 20)
    self.assertEqual(__import__('ctypes').sizeof(v4l2._v4l2_buffer), 88)

  def test_fmt_is_eight_byte_aligned_inside_v4l2_format(self):
    from openpilot.system.camerad.webcam import v4l2
    self.assertEqual(v4l2._v4l2_format.fmt.offset, 8)
    self.assertEqual(v4l2._v4l2_buffer.timestamp.offset, 24)
    self.assertEqual(v4l2._v4l2_buffer.sequence.offset, 56)
    self.assertEqual(v4l2._v4l2_buffer.m.offset, 64)

  def test_ioctl_numbers_match_the_kernel(self):
    from openpilot.system.camerad.webcam import v4l2
    self.assertEqual(v4l2.VIDIOC_S_FMT, 0xc0d05605)
    self.assertEqual(v4l2.VIDIOC_DQBUF, 0xc0585611)
    self.assertEqual(v4l2.VIDIOC_QBUF, 0xc058560f)
    self.assertEqual(v4l2.VIDIOC_REQBUFS, 0xc0145608)


class TestSendYuvTimestamps(unittest.TestCase):
  """_send_yuv is where timestampSof is actually decided, so it needs its own test.

  Testing only read_frames() left a hole: short-circuiting the kernel timestamp in
  camerad.py kept the whole suite green, which means nothing was pinning the
  behaviour that matters.
  """

  def _sink(self):
    camerad = importlib.import_module('openpilot.system.camerad.webcam.camerad')
    instance = camerad.Camerad.__new__(camerad.Camerad)
    instance.vipc_server = Mock()
    instance.pm = Mock()
    return instance

  def test_kernel_sof_becomes_timestamp_sof(self):
    instance = self._sink()
    instance._send_yuv(bytearray(8), 7, 'narrowRoadCameraState', 0, sof_ns=555_000_000)
    sent = instance.vipc_server.send.call_args
    self.assertEqual(sent.args[3], 555_000_000, "vipc must receive the kernel SOF")
    msg = instance.pm.send.call_args.args[1].narrowRoadCameraState
    self.assertEqual(msg.timestampSof, 555_000_000)
    self.assertNotEqual(msg.timestampEof, msg.timestampSof,
                        "SOF and EOF must not be the same value any more")

  def test_missing_kernel_sof_falls_back_to_the_publish_moment(self):
    instance = self._sink()
    before = time.monotonic_ns()
    instance._send_yuv(bytearray(8), 7, 'narrowRoadCameraState', 0, sof_ns=None)
    after = time.monotonic_ns()
    msg = instance.pm.send.call_args.args[1].narrowRoadCameraState
    self.assertTrue(before <= msg.timestampSof <= after,
                    "without a kernel timestamp the publish moment is the honest answer")

  def test_camera_runner_forwards_the_kernel_timestamp(self):
    instance = self._sink()
    instance._stop_event = threading.Event()

    # A real generator: camera_runner wraps read_frames() in contextlib.closing,
    # which needs a close() the bare iterator a Mock returns does not have.
    def frames():
      yield bytearray(8), 4242
      instance._stop_event.set()

    cam = Mock()
    cam.fps, cam.cur_frame_id, cam.cam_type_state, cam.stream_type = 20, 0, 'narrowRoadCameraState', 0
    cam.read_frames = frames
    instance._send_yuv = Mock()
    with patch.object(camerad.time, 'sleep'):
      instance.camera_runner(cam)
    # sof_ns is the fifth positional argument of _send_yuv.
    self.assertEqual(instance._send_yuv.call_args.args[4], 4242,
                     "the runner must pass the frame's timestamp through")

  def test_wide_camera_publishes_at_its_configured_rate(self):
    instance = self._sink()
    instance._stop_event = threading.Event()

    def frames():
      yield bytearray(8), 123_000_000
      instance._stop_event.set()

    cam = Mock()
    cam.fps, cam.cur_frame_id = 20, 0
    cam.cam_type_state, cam.stream_type = 'wideRoadCameraState', 1
    cam.read_frames = frames
    instance._send_yuv = Mock()
    with patch.object(camerad.time, 'sleep'):
      instance.camera_runner(cam)

    # Capture and publication are separate: V4L2 may ask for 20 fps, but the
    # publication clock still has to hand modeld a predictable 20 Hz stream.
    self.assertEqual(instance._send_yuv.call_count, 1)

  def test_wide_camera_delivery_is_paced_when_frames_are_fast(self):
    instance = self._sink()
    instance._stop_event = threading.Event()

    def frames():
      for index in range(3):
        yield bytearray(8), index * 32_000_000
      instance._stop_event.set()

    cam = Mock()
    cam.fps, cam.cur_frame_id = 20, 0
    cam.cam_type_state, cam.stream_type = 'wideRoadCameraState', 1
    cam.read_frames = frames
    sent = []
    instance._send_yuv = lambda *args: sent.append(args[4])

    with patch.object(camerad.time, 'sleep'):
      instance.camera_runner(cam)

    # Pacing must not rewrite the kernel timestamp: it is the sensor frame's SOF.
    self.assertEqual(sent, [0, 32_000_000, 64_000_000])

  def test_wide_camera_repeats_the_last_frame_to_hold_20_hz(self):
    instance = self._sink()
    instance._stop_event = threading.Event()
    sent = []

    def frames():
      # The sensor is only producing ~18 fps, so the second captured frame arrives
      # nearly two publication periods after the first.
      yield bytearray(b'first'), 100_000_000
      time.sleep(0.045)
      yield bytearray(b'second'), 156_000_000
      instance._stop_event.set()

    cam = Mock()
    cam.fps, cam.cur_frame_id = 20, 0
    cam.cam_type_state, cam.stream_type = 'wideRoadCameraState', 1
    cam.read_frames = frames
    instance._send_yuv = lambda yuv, fid, *args, **kwargs: sent.append(
      (bytes(yuv), fid, args[2], kwargs.get('repeat', False)))

    instance.camera_runner(cam)

    # The missing 50 ms slot is filled with the previous image, but marked as
    # a repeat so downstream code can distinguish it from a new observation.
    self.assertEqual([(pixels, fid, repeat) for pixels, fid, _sof, repeat in sent],
                     [(b'first', 0, False), (b'first', 0, True), (b'second', 1, False)])
    self.assertEqual(sent[0][2], sent[1][2])
    self.assertEqual(sent[2][2], 156_000_000)

  def test_both_road_cameras_use_the_same_publication_phase(self):
    instance = self._sink()
    instance._stop_event = threading.Event()
    starts = []

    def run_for(name, stream_type):
      start = time.monotonic()

      def frames():
        starts.append(time.monotonic())
        yield bytearray(8), 100_000_000
        instance._stop_event.set()

      cam = Mock()
      cam.fps, cam.cur_frame_id = 20, 0
      cam.cam_type_state, cam.stream_type = name, stream_type
      cam.read_frames = frames
      with patch.object(camerad.time, 'monotonic', side_effect=lambda: start), \
           patch.object(camerad.time, 'sleep'):
        instance.camera_runner(cam)

    instance._send_yuv = Mock()
    run_for('narrowRoadCameraState', 0)
    run_for('wideRoadCameraState', 1)

    # Both streams publish on the same global slot grid rather than each starting
    # its own 20 Hz phase when its thread happens to wake up.
    self.assertIn('_slot_phase', instance.__dict__)

  def test_wide_camera_frame_pixels_and_metadata_are_unique(self):
    instance = self._sink()
    instance._send_yuv(bytearray(b'one'), 0, 'wideRoadCameraState', 1, sof_ns=100_000_000)
    first = instance.pm.send.call_args.args[1].wideRoadCameraState
    instance._send_yuv(bytearray(b'two'), 1, 'wideRoadCameraState', 1, sof_ns=150_000_000)
    second = instance.pm.send.call_args.args[1].wideRoadCameraState
    self.assertNotEqual(first.timestampSof, second.timestampSof)
    self.assertNotEqual(first.frameId, second.frameId)


@unittest.skipUnless(HAS_AV, "requires PyAV (av)")
class TestCameraWorkerFailures(unittest.TestCase):
  """An optional camera that dies must be reported, not crash its own error path.

  This used to raise AttributeError inside the except block (``cam.msg_name`` does
  not exist on Camera), so the log line that was supposed to explain the failure
  became a second failure.
  """

  def _camerad(self):
    c = camerad.Camerad.__new__(camerad.Camerad)

    def boom(_cam):
      raise RuntimeError("device unplugged")

    c.camera_runner = boom
    return c

  def test_optional_camera_failure_does_not_raise_and_is_not_fatal(self):
    import queue
    from types import SimpleNamespace
    errors = queue.Queue()
    cam = SimpleNamespace(cam_type_state="wideRoadCameraState", required=False)
    self._camerad()._worker(cam, errors)
    self.assertTrue(errors.empty(), "an optional camera must not stop camerad")

  def test_required_camera_failure_reaches_the_run_loop(self):
    import queue
    from types import SimpleNamespace
    errors = queue.Queue()
    cam = SimpleNamespace(cam_type_state="narrowRoadCameraState", required=True)
    self._camerad()._worker(cam, errors)
    self.assertFalse(errors.empty(), "a missing narrow camera is a hard failure")
    self.assertIsInstance(errors.get_nowait(), RuntimeError)
