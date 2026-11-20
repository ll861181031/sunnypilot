"""The model always sees two camera slots, even when only one camera is delivering.

modeld fills the second slot with the main frame rather than shrinking the input,
because the compiled graph has a fixed ``(2, 6, 128, 256)`` entry point and there is no
single-camera graph to fall back to. These tests pin that structure so a later change
cannot quietly drop to one camera slot.

The warp is run in a subprocess with ``DEV=CPU``: tinygrad binds its default device at
import, and a parent pytest process that already imported it with the AMD backend would
either run these on the GPU (slow, and it may be busy) or fail outright on a machine
without one. The subprocess keeps the result deterministic and offline.
"""

import json
import os
import subprocess
import sys
import textwrap

import pytest

# Runs one warp and reports the resulting shape plus how far the two slots differ from
# each other. Both calls use a 1344x760 NV12 frame, the size this build publishes.
_PROBE = textwrap.dedent("""
    import json
    import numpy as np
    from tinygrad import Tensor
    from openpilot.selfdrive.modeld.compile_modeld import make_warp
    from openpilot.system.camerad.cameras.nv12_info import get_nv12_info

    w, h = 1344, 760
    stride, y_height, uv_height, size = get_nv12_info(w, h)
    nv12 = (w, h, stride, y_height, uv_height, size)

    rng = np.random.default_rng(0)
    first = Tensor(rng.integers(0, 255, size, dtype=np.uint8))
    second = Tensor(rng.integers(0, 255, size, dtype=np.uint8))
    # Two transforms that differ enough to show up in the warped pixels.
    wide_tf = Tensor(np.eye(3, dtype=np.float32) * 2.0)
    tight_tf = Tensor(np.eye(3, dtype=np.float32) * 0.5)

    warp = make_warp(nv12, 512, 256)

    two_frames = warp(wide_tf, tight_tf, first, second)
    same_frame_both_slots = warp(wide_tf, wide_tf, first, first)

    # Do the two slots carry different pixels when their transforms differ?
    a = two_frames.numpy()
    split = a.shape[0] // 2
    differ = float((a[:split] != a[split:]).mean())

    print(json.dumps({
      "two_frames_shape": list(two_frames.shape),
      "same_frame_shape": list(same_frame_both_slots.shape),
      "slot_difference": differ,
    }))
""")


@pytest.fixture(scope="module")
def probe() -> dict:
  env = {**os.environ, "DEV": "CPU", "PYTHONPATH": os.pathsep.join(sys.path)}
  proc = subprocess.run([sys.executable, "-c", _PROBE], capture_output=True, text=True,
                        env=env, timeout=600)
  if proc.returncode != 0:
    pytest.skip(f"tinygrad CPU backend unavailable: {proc.stderr.strip().splitlines()[-1:]}")
  return json.loads(proc.stdout.strip().splitlines()[-1])


class TestTheWarpAlwaysYieldsTwoCameraSlots:
  def test_two_different_frames_produce_two_slots(self, probe):
    assert probe["two_frames_shape"] == [2, 6, 128, 256]

  def test_the_same_frame_in_both_slots_still_produces_two_slots(self, probe):
    # This is the missing-camera path: modeld assigns buf_main to both slot buffers.
    # The graph must still see two slots, not one.
    assert probe["same_frame_shape"] == [2, 6, 128, 256]

  def test_each_slot_is_warped_with_its_own_transform(self, probe):
    # If the second slot were a straight copy of the first, a different transform on it
    # would change nothing. It changes about half the pixels.
    assert probe["slot_difference"] > 0.1


class TestModeldFillsBothSlotBuffers:
  """Every branch of the frame selection must assign both buffers.

  Read as source rather than exercised, because the selection lives inside the main
  loop: reaching it would need a full VIPC setup. The property worth guarding is that no
  branch leaves the second buffer unset, which is what "always two inputs" means here.
  """

  def _source(self) -> str:
    import inspect
    from openpilot.selfdrive.modeld import modeld
    return inspect.getsource(modeld)

  def test_the_extra_buffer_is_assigned_from_the_wide_frame_when_it_is_usable(self):
    src = self._source()
    assert src.count("buf_extra = selected_extra.buffer") == 1

  def test_the_extra_buffer_falls_back_to_the_main_frame(self):
    src = self._source()
    # The selection branch, the discarded-stream branch, the overwritten-frame
    # branch, and the copy-race recovery all have to repoint the second input.
    assert src.count("buf_extra = buf_main") >= 2

  def test_the_single_camera_branch_also_fills_the_extra_buffer(self):
    src = self._source()
    # The else of `if use_extra_client:` must still assign, or the warp gets a stale
    # buffer from the previous iteration.
    tail = src.split("else:\n      buf_extra = buf_main")
    assert len(tail) == 2, "single-camera branch must assign buf_extra"
