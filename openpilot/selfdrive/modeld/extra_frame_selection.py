from dataclasses import dataclass
from collections import deque

import numpy as np


@dataclass(frozen=True)
class ExtraFrame:
  buffer: object
  frame_id: int
  timestamp_sof: int
  timestamp_eof: int


  @property
  def meta(self):
    return self


class ExtraFrameChanged(RuntimeError):
  pass


def copy_stable_frame(buf, frame_id: int, frame_copy_size: int) -> np.ndarray:
  """Copy NV12 pixels only when the shared VisionBuf still holds frame_id."""
  if buf.frame_id != frame_id:
    raise ExtraFrameChanged(f"buffer now holds frame {buf.frame_id}, expected {frame_id}")
  data = np.array(np.frombuffer(buf.data, dtype=np.uint8, count=frame_copy_size), copy=True)
  if buf.frame_id != frame_id:
    raise ExtraFrameChanged(f"buffer changed to frame {buf.frame_id} while copying {frame_id}")
  return data


class ExtraFrameSelector:
  """Select an arrived, unused wide frame without blocking the main camera."""

  def __init__(self, tolerance_ns: int, max_pending: int = 4, max_receive: int = 4):
    if max_pending < 1 or max_receive < 1:
      raise ValueError("max_pending and max_receive must be positive")
    self.tolerance_ns = tolerance_ns
    self.max_pending = max_pending
    self.max_receive = max_receive
    self._pending = deque()
    self.last_received_sof = 0
    self.last_accepted_frame_id = None
    self.consecutive_misses = 0

  def _discard_overwritten(self):
    self._pending = deque(candidate for candidate in self._pending
                          if candidate.buffer.frame_id == candidate.frame_id)

  def _attempt(self, client, main_timestamp_sof: int):
    received = False
    attempts = 0
    while attempts < self.max_receive:
      attempts += 1
      buffer = client.recv(0)
      if buffer is None:
        continue
      received = True
      if client.frame_id == self.last_accepted_frame_id:
        # camerad repeats the previous frame to fill a 20 Hz publication slot.
        # Recording sees the slot; the model does not see a fake new observation.
        continue
      self.last_accepted_frame_id = client.frame_id
      # A successful receive updates all three client properties together. Bind
      # them to this buffer immediately; another receive must not change them.
      self.last_received_sof = client.timestamp_sof
      self._pending.append(ExtraFrame(buffer, client.frame_id, client.timestamp_sof, client.timestamp_eof))

    # VisionIPC buffers are shared with the publisher. A retained reference can be
    # overwritten by a later frame, so its metadata stays correct while its pixels
    # have moved on. Never pair those pixels with the older timestamp.
    self._discard_overwritten()
    while len(self._pending) > self.max_pending:
      self._pending.popleft()

    best_index = None
    best_delta = None
    for index, candidate in enumerate(self._pending):
      delta = abs(main_timestamp_sof - candidate.timestamp_sof)
      if delta <= self.tolerance_ns and (best_delta is None or delta < best_delta):
        best_index, best_delta = index, delta

    if best_index is not None:
      selected = self._pending[best_index]
      del self._pending[best_index]
      return selected, True

    # Discard only frames that can no longer match a future main camera. A frame
    # after the current main SOF is retained for the next cycle.
    while self._pending and main_timestamp_sof - self._pending[0].timestamp_sof > self.tolerance_ns:
      self._pending.popleft()
    return None, received

  def select(self, client, main_timestamp_sof: int):
    selected, received = self._attempt(client, main_timestamp_sof)
    # A selected frame can be overwritten between the selector and inference. When
    # the first attempt has no usable frame, drain a second bounded batch before
    # giving up. If frames were merely late, this retries without ever waiting.
    if selected is None:
      selected, received_retry = self._attempt(client, main_timestamp_sof)
      received = received or received_retry
    self.consecutive_misses = 0 if received else self.consecutive_misses + 1
    return selected

  @staticmethod
  def is_current(frame: ExtraFrame) -> bool:
    return frame.buffer.frame_id == frame.frame_id
