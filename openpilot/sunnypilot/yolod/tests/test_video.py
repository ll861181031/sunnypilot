"""Video reader tests: PyAV, the ffmpeg pipe fallback and route directory lookup."""

import shutil
import subprocess

import numpy as np
import pytest

from openpilot.sunnypilot.yolod.video import VideoDecodeError, find_video_path, iter_frames, probe_video

FFMPEG = shutil.which("ffmpeg")


@pytest.fixture(scope="module")
def sample_video(tmp_path_factory) -> str:
  if FFMPEG is None:
    pytest.skip("ffmpeg is not installed")
  path = tmp_path_factory.mktemp("yolo_video") / "sample.mp4"
  command = [FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=64x48:rate=10:duration=0.8",
             "-pix_fmt", "yuv420p", "-c:v", "libx264", str(path)]
  result = subprocess.run(command, capture_output=True, text=True, check=False)
  if result.returncode != 0:
    pytest.skip(f"ffmpeg could not generate a test video: {result.stderr[-200:]}")
  return str(path)


def test_probe_video(sample_video):
  info = probe_video(sample_video)
  assert (info.width, info.height) == (64, 48)
  assert info.fps == pytest.approx(10.0)
  assert info.frame_count in (None, 8)


def test_av_backend_decodes_frames(sample_video):
  frames = list(iter_frames(sample_video, backend="av"))
  assert len(frames) == 8
  assert [index for index, _ in frames] == list(range(8))
  assert frames[0][1].shape == (48, 64, 3)
  assert frames[0][1].dtype == np.uint8


def test_every_and_max_frames(sample_video):
  every = list(iter_frames(sample_video, every=2, backend="av"))
  assert len(every) == 4
  assert [index for index, _ in every] == [0, 2, 4, 6]

  limited = list(iter_frames(sample_video, max_frames=3, backend="av"))
  assert len(limited) == 3


def test_ffmpeg_pipe_fallback_matches_av(sample_video):
  from_av = [frame for _, frame in iter_frames(sample_video, backend="av")]
  from_ffmpeg = [frame for _, frame in iter_frames(sample_video, backend="ffmpeg")]
  assert len(from_ffmpeg) == len(from_av)
  for expected, actual in zip(from_av, from_ffmpeg, strict=True):
    assert actual.shape == expected.shape
    # Both decode the same bitstream; the pixel values must agree.
    assert np.array_equal(actual, expected)


def test_auto_backend_uses_av_when_available(sample_video):
  frames = list(iter_frames(sample_video))
  assert len(frames) == 8


def test_route_directory_lookup(tmp_path):
  (tmp_path / "fcamera.hevc").write_bytes(b"")
  assert find_video_path(str(tmp_path)) == str(tmp_path / "fcamera.hevc")
  other = tmp_path / "video.mp4"
  other.write_bytes(b"")
  (tmp_path / "fcamera.hevc").unlink()
  assert find_video_path(str(tmp_path)) == str(other)
  (tmp_path / "video.mp4").unlink()
  with pytest.raises(FileNotFoundError):
    find_video_path(str(tmp_path))


def test_missing_file_raises():
  with pytest.raises(FileNotFoundError):
    find_video_path("/nonexistent/video.mp4")
  with pytest.raises((VideoDecodeError, OSError)):
    list(iter_frames("/nonexistent/video.mp4", backend="av"))


def test_rejects_invalid_options(sample_video):
  with pytest.raises(ValueError):
    list(iter_frames(sample_video, every=0))
  with pytest.raises(ValueError):
    list(iter_frames(sample_video, max_frames=-1))
  with pytest.raises(ValueError):
    list(iter_frames(sample_video, backend="magic"))
