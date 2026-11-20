import unittest

import numpy as np

from openpilot.selfdrive.modeld.extra_frame_selection import ExtraFrameChanged, ExtraFrameSelector, copy_stable_frame


class FakeExtraClient:
  def __init__(self, frames=()):
    self.frames = list(frames)
    self.calls = 0

  def recv(self, timeout_ms):
    assert timeout_ms == 0
    self.calls += 1
    if not self.frames:
      return None
    buffer, frame_id, sof, eof = self.frames.pop(0)
    self.frame_id, self.timestamp_sof, self.timestamp_eof = frame_id, sof, eof
    return buffer


class FakeVisionBuffer:
  def __init__(self, frame_id):
    self.frame_id = frame_id
    self.data = bytearray(8)


def frame(frame_id, sof_ms):
  return (FakeVisionBuffer(frame_id), frame_id, sof_ms * 1_000_000, sof_ms * 1_000_000 + 1000)


class TestExtraFrameSelection(unittest.TestCase):
  def test_binds_metadata_to_each_successful_receive(self):
    client = FakeExtraClient([frame(1, 100), frame(2, 132)])
    selector = ExtraFrameSelector(tolerance_ns=25_000_000)

    selected = selector.select(client, 125_000_000)

    self.assertEqual(selected.buffer.frame_id, 2)
    self.assertEqual(selected.meta.frame_id, 2)
    self.assertEqual(selected.meta.timestamp_sof, 132_000_000)

  def test_keeps_an_arrived_candidate_instead_of_only_using_the_newest(self):
    # Both frames arrived before this main-camera cycle. The older one is the
    # closest match; selecting only the newest would unnecessarily fall back.
    client = FakeExtraClient([frame(18, 96), frame(19, 132)])
    selector = ExtraFrameSelector(tolerance_ns=25_000_000)

    selected = selector.select(client, 100_000_000)

    self.assertEqual(selected.buffer.frame_id, 18)
    self.assertEqual(selected.meta.frame_id, 18)

  def test_retains_a_future_frame_for_the_next_main_cycle(self):
    client = FakeExtraClient([frame(1, 140)])
    selector = ExtraFrameSelector(tolerance_ns=25_000_000)

    self.assertIsNone(selector.select(client, 100_000_000))
    selected = selector.select(client, 140_000_000)

    self.assertEqual(selected.buffer.frame_id, 1)

  def test_never_uses_the_same_extra_frame_twice(self):
    client = FakeExtraClient([frame(1, 100)])
    selector = ExtraFrameSelector(tolerance_ns=25_000_000)

    self.assertIsNotNone(selector.select(client, 100_000_000))
    self.assertIsNone(selector.select(client, 101_000_000))

  def test_a_repeated_publication_is_not_a_new_model_observation(self):
    client = FakeExtraClient([frame(1, 100), frame(1, 100), frame(2, 150)])
    selector = ExtraFrameSelector(tolerance_ns=25_000_000)

    first = selector.select(client, 100_000_000)
    second = selector.select(client, 100_000_000)

    self.assertEqual(first.meta.frame_id, 1)
    self.assertIsNone(second)

  def test_rejects_frames_outside_the_sync_window(self):
    for delta_ms in (-26, 26):
      with self.subTest(delta_ms=delta_ms):
        client = FakeExtraClient([frame(1, 100 + delta_ms)])
        selector = ExtraFrameSelector(tolerance_ns=25_000_000)
        self.assertIsNone(selector.select(client, 100_000_000))

  def test_pending_candidates_are_bounded(self):
    selector = ExtraFrameSelector(tolerance_ns=25_000_000, max_pending=3)
    for index in range(6):
      # Future frames cannot be selected or aged out by this old main timestamp.
      selector.select(FakeExtraClient([frame(index, 1000 + index * 50)]), 0)

    self.assertLessEqual(len(selector._pending), 3)

  def test_receive_miss_counter_is_not_hidden_by_a_successful_pair(self):
    selector = ExtraFrameSelector(tolerance_ns=25_000_000)
    empty = FakeExtraClient()

    selector.select(empty, 100_000_000)
    selector.select(empty, 150_000_000)

    self.assertEqual(selector.consecutive_misses, 2)

    selector.select(FakeExtraClient([frame(1, 200)]), 200_000_000)
    self.assertEqual(selector.consecutive_misses, 0)

  def test_rejects_a_retained_frame_whose_shared_buffer_was_overwritten(self):
    buffer = FakeVisionBuffer(1)
    client = FakeExtraClient([(buffer, 1, 140_000_000, 141_000_000)])
    selector = ExtraFrameSelector(tolerance_ns=25_000_000)

    self.assertIsNone(selector.select(client, 100_000_000))
    buffer.frame_id = 2
    self.assertIsNone(selector.select(client, 140_000_000))

  def test_limits_receive_work_in_one_main_camera_cycle(self):
    client = FakeExtraClient([frame(index, 1000 + index) for index in range(10_000)])
    selector = ExtraFrameSelector(tolerance_ns=25_000_000)

    selector.select(client, 0)

    self.assertLessEqual(client.calls, 8)

  def test_retries_a_late_arrived_frame_once_without_waiting(self):
    buffer = FakeVisionBuffer(1)

    class ArrivesOnSecondCall(FakeExtraClient):
      def recv(self, timeout_ms):
        assert timeout_ms == 0
        self.calls += 1
        if self.calls < 5:
          return None
        self.frame_id, self.timestamp_sof, self.timestamp_eof = 1, 100_000_000, 101_000_000
        return buffer

    client = ArrivesOnSecondCall()
    selector = ExtraFrameSelector(tolerance_ns=25_000_000)

    selected = selector.select(client, 100_000_000)

    self.assertIs(selected.buffer, buffer)
    self.assertEqual(selected.meta.frame_id, 1)

  def test_rejects_pixels_overwritten_during_the_copy(self):
    buffer = FakeVisionBuffer(1)

    class OverwritingView:
      def __new__(cls, data, dtype, count):
        buffer.frame_id = 2
        return memoryview(data)[:count]

    class ChangingNumpy:
      uint8 = np.uint8
      array = staticmethod(np.array)

      @staticmethod
      def frombuffer(data, dtype, count):
        return OverwritingView(data, dtype, count)

    with self.assertRaises(ExtraFrameChanged):
      copy_stable_frame.__globals__['np'] = ChangingNumpy()
      try:
        copy_stable_frame(buffer, 1, 8)
      finally:
        copy_stable_frame.__globals__['np'] = __import__('numpy')


if __name__ == "__main__":
  unittest.main()
