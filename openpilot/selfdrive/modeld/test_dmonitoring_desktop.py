from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from openpilot.selfdrive.modeld import dmonitoringmodeld as dm


def test_gpu_upload_refreshes_reused_host_frame_without_treating_it_as_gpu_memory():
  model = dm.ModelState.__new__(dm.ModelState)
  model.DEV = 'AMD'
  data = bytearray([10, 20, 30, 40])
  snapshots = []

  def copy_frame(array, device):
    assert device == 'AMD'
    snapshots.append(array.copy())
    return Mock()

  with patch.object(dm, 'Tensor', side_effect=copy_frame) as tensor:
    model._frame_tensor(SimpleNamespace(data=data))
    data[:] = bytes([50, 60, 70, 80])
    model._frame_tensor(SimpleNamespace(data=data))
    tensor.from_blob.assert_not_called()
  np.testing.assert_array_equal(snapshots, [[10, 20, 30, 40], [50, 60, 70, 80]])


def test_stock_host_buffer_cache_is_preserved():
  model = dm.ModelState.__new__(dm.ModelState)
  model.DEV = 'CPU'
  model.frame_buf_params = (4, 1, 0, 4)
  model._blob_cache = {}
  frame = SimpleNamespace(data=bytearray(4))
  with patch.object(dm, 'Tensor') as tensor:
    first = model._frame_tensor(frame)
    assert model._frame_tensor(frame) is first
    tensor.from_blob.assert_called_once()
