"""Exercise full capture ordering and recurrent output lifetime with CPU fakes."""
import ctypes
import hashlib
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

BASE = Path(__file__).resolve().parent
ENGINE = Path(os.environ.get('TEST_CAPTURE_ENGINE', BASE / 'mgxd_engine.py'))
SPECS = {'new_img': ((2, 6, 128, 256), np.uint8), 'desire': ((8,), np.float32),
         'traffic_convention': ((1, 2), np.float32), 'action_t': ((1, 2), np.float32),
         'nav_map': ((1, 1, 256, 256), np.float32), 'navigation': ((1, 47), np.float32)}


class Shape:
  def __init__(self, lens, dtype):
    self.dimensions, self.dtype = tuple(lens), np.dtype(dtype)

  def lens(self):
    return self.dimensions

  def bytes(self):
    return int(np.prod(self.dimensions)) * self.dtype.itemsize


class Argument:
  def __init__(self, shape, device=False):
    self.array, self.device = np.empty(shape.lens(), shape.dtype), device

  def data_ptr(self):
    return self.array.ctypes.data

  def get_shape(self):
    return Shape(self.array.shape, self.array.dtype)

  def __array__(self, dtype=None, copy=None):
    assert not self.device, 'A device argument must never be read through NumPy'
    return np.asarray(self.array, dtype=dtype)


class Hip:
  def __init__(self, bad_graph=False):
    self.pending, self.captured, self.events = [], None, []
    self.graph_ops = []
    self.bad_graph = bad_graph

  def create_stream(self):
    self.events.append('create_stream')
    return 97

  def enqueue(self, action, label):
    self.events.append(label)
    (self.captured if self.captured is not None else self.pending).append(action)

  def zero(self, destination, size, stream=None):
    self.enqueue(lambda: ctypes.memset(destination, 0, size), 'zero')

  def copy(self, destination, source, size, kind, stream=None):
    self.enqueue(lambda: ctypes.memmove(destination, source, size), f'copy_{kind}')

  def synchronize(self, stream=None):
    assert self.captured is None, 'Synchronization is forbidden inside capture'
    self.events.append('sync')
    pending, self.pending = self.pending, []
    for action in pending:
      action()

  def begin_capture(self, stream):
    assert not self.pending and self.captured is None
    self.events.append('begin_capture')
    self.captured = []

  def end_capture(self, stream):
    assert self.captured is not None
    self.events.append('end_capture')
    self.graph_ops, self.captured = self.captured, None
    return 101

  def graph_topology(self, graph):
    types = [1] * 6 + [0] * (1 if self.bad_graph else 493) + [1] * 5
    return {'nodes': [{'id': i + 1, 'type': t} for i, t in enumerate(types)],
            'edges': [[i, i + 1] for i in range(1, len(types))]}

  def instantiate(self, graph):
    self.events.append('instantiate')
    return 103

  def launch(self, graph, stream):
    self.events.append('launch')
    self.pending.extend(self.graph_ops)

  def destroy_exec(self, graph):
    self.events.append('destroy_exec')

  def destroy_graph(self, graph):
    self.events.append('destroy_graph')
    self.graph_ops = []

  def destroy_stream(self, stream):
    self.events.append('destroy_stream')


def construct(tmp_path, monkeypatch, mode, bad_graph=False):
  spec = importlib.util.spec_from_file_location('capture_under_test', ENGINE)
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  model = tmp_path / 'cpu-model.mxr'
  model.write_bytes(b'cpu-only-model')
  monkeypatch.setattr(module, 'MODEL_SHA256', hashlib.sha256(model.read_bytes()).hexdigest())
  monkeypatch.setenv('RUN30_CAPTURE', str(mode))
  shapes = {name: Shape(lens, dtype) for name, (lens, dtype) in module.STATE_SPECS.items()}
  shapes.update({name: Shape(lens, dtype) for name, (lens, dtype) in SPECS.items()})
  shapes['__main_output_host'] = Shape((1, 18580), np.float32)
  hip, allocations = Hip(bad_graph), []

  class Program:
    def __init__(self):
      self.outputs = [Argument(shapes[name], True) for name in module.STATE_SPECS]
      self.run_calls = 0

    def get_parameter_shapes(self):
      return shapes

    def run(self, args):
      raise AssertionError('The candidate must use run_async with its explicit stream')

    def run_async(self, args, stream, stream_name):
      assert stream == 97 and stream_name == 'ihipStream_t'
      self.run_calls += 1
      outputs = self.outputs

      def compute():
        delta = float(args['new_img'].array.flat[0]) + float(args['navigation'].array.flat[0]) + 1
        args['__main_output_host'].array.fill(float(args['state_feat_q'].array.flat[0]) + delta)
        for (name, _), output in zip(module.STATE_SPECS.items(), outputs, strict=True):
          output.array[:] = args[name].array + delta

      hip.enqueue(compute, 'run_async')
      return [args['__main_output_host']] + outputs

  program = Program()

  def allocate(shape, host=False):
    arg = Argument(shape, not host)
    allocations.append((host, arg))
    return arg

  runtime = SimpleNamespace(load=lambda _: program, allocate_gpu=allocate)
  return module, hip, allocations, program, lambda: module.ResidentEngine(runtime, model, hip=hip, capture=bool(mode))


@pytest.mark.parametrize('mode', [0, 1])
def test_complete_step_feedback_owned_output_and_cleanup(tmp_path, monkeypatch, mode):
  module, hip, allocations, program, create = construct(tmp_path, monkeypatch, mode)
  engine = create()
  try:
    assert all(not np.any(v) for v in engine.snapshot_state_host().values()), 'Warmup must reset all five states'
    inputs = {name: np.zeros(lens, dtype) for name, (lens, dtype) in SPECS.items()}
    inputs['new_img'].fill(2)
    inputs['navigation'].fill(3)
    first = engine.step(inputs)
    assert first.flags.owndata
    np.testing.assert_array_equal(first, np.full(18580, 6, np.float32))
    first_bytes = first.tobytes()
    inputs['navigation'].fill(8)
    second = engine.step(inputs)
    np.testing.assert_array_equal(second, np.full(18580, 17, np.float32))
    assert first.tobytes() == first_bytes, 'Previous result must survive a subsequent step'
    for value in engine.snapshot_state_host().values():
      np.testing.assert_array_equal(value, np.full_like(value, 17))
    assert len(allocations) == 18 and sum(host for host, _ in allocations) == 7
    assert program.run_calls == (2 if mode else 3), 'Capture replay must not rerun the Python graph traversal'
    assert hip.events.count('launch') == (2 if mode else 0)
    assert engine.audit_layout()['graph_outputs_disjoint_from_persistent_inputs']
    assert engine.last_timings_ns['graph_call_ns'] > 0
  finally:
    engine.close()
  assert hip.events[-1] == 'destroy_stream'
  if mode:
    assert hip.events.index('destroy_exec') < hip.events.index('destroy_graph') < hip.events.index('destroy_stream')
  events = list(hip.events)
  engine.close()
  assert hip.events == events


def test_incomplete_capture_is_rejected_and_cleaned(tmp_path, monkeypatch):
  _, hip, _, _, create = construct(tmp_path, monkeypatch, 1, bad_graph=True)
  with pytest.raises(AssertionError, match='kernel'):
    create()
  assert 'instantiate' not in hip.events and hip.events[-1] == 'destroy_stream'


def test_bad_host_dtype_rejected_before_work(tmp_path, monkeypatch):
  _, hip, _, _, create = construct(tmp_path, monkeypatch, 1)
  engine = create()
  try:
    inputs = {name: np.zeros(lens, dtype) for name, (lens, dtype) in SPECS.items()}
    inputs['navigation'] = inputs['navigation'].astype(np.float64)
    events = len(hip.events)
    with pytest.raises(AssertionError, match='dtype'):
      engine.step(inputs)
    assert len(hip.events) == events
  finally:
    engine.close()
