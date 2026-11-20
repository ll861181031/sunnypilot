"""Run the validated nav graph with persistent buffers and HIP graph capture."""
import ctypes
import hashlib
import os
from pathlib import Path
import time

import numpy as np

from openpilot.selfdrive.modeld.nav_evidence_protocol import ERROR_GPU, ERROR_STAGING, crc32_buffer
from openpilot.selfdrive.modeld.nav_reset import NAV_STATE_KEYS

STATE_SPECS = {
  'state_img_q': ((2, 5, 6, 128, 256), np.dtype(np.uint8)),
  'state_desire_q': ((132, 1, 8), np.dtype(np.float32)),
  'state_feat_q': ((128, 1, 16384), np.dtype(np.float32)),
  'state_map_features_q': ((128, 1, 64), np.dtype(np.float32)),
  'state_navigation_features_q': ((128, 1, 64), np.dtype(np.float32)),
}
HOST_INPUTS = ('new_img', 'desire', 'traffic_convention', 'action_t', 'nav_map', 'navigation')
HOST_OUTPUT_PARAM = '__main_output_host'
MODEL_SHA256 = '085fe973f91d05fb5d7d63ca9c0d734b47e8d6298ae6fdc7fb7452870f74fa52'


class Hip:
  def __init__(self):
    root = Path(os.environ['MODELD_MGXD_ROCM_ROOT']).resolve()
    self.library = ctypes.CDLL(str(root / 'lib/libamdhip64.so'))
    p, size, integer = ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int
    signatures = {
      'hipStreamCreateWithFlags': [ctypes.POINTER(p), ctypes.c_uint],
      'hipStreamDestroy': [p], 'hipStreamSynchronize': [p],
      'hipMemcpyAsync': [p, p, size, integer, p], 'hipMemsetAsync': [p, integer, size, p],
      'hipStreamBeginCapture': [p, integer], 'hipStreamEndCapture': [p, ctypes.POINTER(p)],
      'hipGraphGetNodes': [p, ctypes.POINTER(p), ctypes.POINTER(size)],
      'hipGraphGetEdges': [p, ctypes.POINTER(p), ctypes.POINTER(p), ctypes.POINTER(size)],
      'hipGraphNodeGetType': [p, ctypes.POINTER(integer)],
      'hipGraphInstantiate': [ctypes.POINTER(p), p, ctypes.POINTER(p), ctypes.c_char_p, size],
      'hipGraphLaunch': [p, p], 'hipGraphExecDestroy': [p], 'hipGraphDestroy': [p],
    }
    for name, parameters in signatures.items():
      function = getattr(self.library, name)
      function.argtypes, function.restype = parameters, integer

  def call(self, name, *args):
    result = getattr(self.library, name)(*args)
    if result:
      raise RuntimeError(f'HIP {name} returned {result}')

  def create_stream(self):
    stream = ctypes.c_void_p()
    self.call('hipStreamCreateWithFlags', ctypes.byref(stream), 1)
    assert stream.value, 'An explicit nonblocking stream is required'
    return stream.value

  def zero(self, destination, size, stream):
    self.call('hipMemsetAsync', destination, 0, size, stream)

  def copy(self, destination, source, size, kind, stream):
    self.call('hipMemcpyAsync', destination, source, size, kind, stream)

  def synchronize(self, stream):
    self.call('hipStreamSynchronize', stream)

  def begin_capture(self, stream):
    self.call('hipStreamBeginCapture', stream, 0)

  def end_capture(self, stream):
    graph = ctypes.c_void_p()
    self.call('hipStreamEndCapture', stream, ctypes.byref(graph))
    assert graph.value, 'Captured graph is null'
    return graph.value

  def graph_topology(self, graph):
    count = ctypes.c_size_t()
    self.call('hipGraphGetNodes', graph, None, ctypes.byref(count))
    nodes = (ctypes.c_void_p * count.value)()
    self.call('hipGraphGetNodes', graph, nodes, ctypes.byref(count))
    result = []
    for node in nodes[:count.value]:
      kind = ctypes.c_int()
      self.call('hipGraphNodeGetType', node, ctypes.byref(kind))
      result.append({'id': int(node), 'type': kind.value})
    edge_count = ctypes.c_size_t()
    self.call('hipGraphGetEdges', graph, None, None, ctypes.byref(edge_count))
    sources = (ctypes.c_void_p * edge_count.value)()
    targets = (ctypes.c_void_p * edge_count.value)()
    self.call('hipGraphGetEdges', graph, sources, targets, ctypes.byref(edge_count))
    return {'nodes': result, 'edges': [[int(a), int(b)] for a, b in zip(
      sources[:edge_count.value], targets[:edge_count.value], strict=True)]}

  def instantiate(self, graph):
    executable = ctypes.c_void_p()
    self.call('hipGraphInstantiate', ctypes.byref(executable), graph, None, None, 0)
    assert executable.value
    return executable.value

  def launch(self, graph, stream):
    self.call('hipGraphLaunch', graph, stream)

  def destroy_exec(self, graph):
    self.call('hipGraphExecDestroy', graph)

  def destroy_graph(self, graph):
    self.call('hipGraphDestroy', graph)

  def destroy_stream(self, stream):
    self.call('hipStreamDestroy', stream)


def validate_topology(topology):
  nodes, edges = topology['nodes'], topology['edges']
  ids = {node['id'] for node in nodes}
  assert len(ids) == len(nodes) and nodes, 'Empty or duplicate captured graph nodes'
  counts = {kind: sum(node['type'] == kind for node in nodes) for kind in {node['type'] for node in nodes}}
  assert counts.get(0, 0) == 493, ('captured_kernel_count', counts)
  assert counts.get(1, 0) == 11, ('captured_memcpy_count', counts)
  assert set(counts) <= {0, 1, 5, 6, 7}, ('unexpected_captured_node_type', counts)
  adjacent = {node: set() for node in ids}
  outgoing = {node: set() for node in ids}
  indegree = dict.fromkeys(ids, 0)
  assert len({tuple(edge) for edge in edges}) == len(edges), 'Duplicate graph edges'
  for source, target in edges:
    assert source in ids and target in ids and source != target
    adjacent[source].add(target)
    adjacent[target].add(source)
    outgoing[source].add(target)
    indegree[target] += 1
  visited, pending = set(), [next(iter(ids))]
  while pending:
    node = pending.pop()
    if node not in visited:
      visited.add(node)
      pending.extend(adjacent[node] - visited)
  assert visited == ids, 'Captured graph has disconnected work'
  pending = [node for node in ids if indegree[node] == 0]
  ordered = []
  while pending:
    node = pending.pop()
    ordered.append(node)
    for target in outgoing[node]:
      indegree[target] -= 1
      if indegree[target] == 0:
        pending.append(target)
  assert len(ordered) == len(nodes), 'Captured graph has cyclic dependencies'
  return {'node_count': len(nodes), 'edge_count': len(edges), 'node_types': counts,
          'connected': True, 'acyclic': True, 'expected_compute_kernels': 492,
          'expected_main_copy_kernels': 1, 'expected_host_uploads': 6, 'expected_state_commits': 5,
          'topology': topology}


def overlaps(left, right):
  return left['address'] < right['address'] + right['nbytes'] and right['address'] < left['address'] + left['nbytes']


class ResidentEngine:
  def __init__(self, migraphx_module, model_path, hip=None, capture=True):
    assert os.environ.get('MIGRAPHX_TRACE_EVAL', '0') == '0', 'MIGRAPHX_TRACE_EVAL would synchronize capture'
    assert os.environ.get('MIGRAPHX_ENABLE_NULL_STREAM', '0') == '0', 'Null-stream MIGraphX is not supported'
    self.capture = capture
    self.migraphx = migraphx_module
    self.model_path = str(Path(model_path).resolve())
    with Path(self.model_path).open('rb') as source:
      assert hashlib.file_digest(source, 'sha256').hexdigest() == MODEL_SHA256, 'Unreviewed capture model'
    self.hip = hip if hip is not None else Hip()
    self.program = None
    self.stream = self.graph = self.executable = None
    self.capturing = self.closed = False
    self.arguments, self.staging_arguments, self.staging_views = {}, {}, {}
    self.state_layout, self.input_layout = {}, {}
    self.last_output_layout, self.output_owners = [], []
    self.main_output_argument = self.main_output_view = None
    self.last_timings_ns, self.graph_audit = {}, None
    self.nav_readback_arguments, self.nav_readback_views = {}, {}
    self.frames = 0
    try:
      self.program = self.migraphx.load(self.model_path)
      self.shapes = self.program.get_parameter_shapes()
      assert set(self.shapes) == set(STATE_SPECS) | set(HOST_INPUTS) | {HOST_OUTPUT_PARAM}
      self.stream = self.hip.create_stream()
      for name in (*STATE_SPECS, *HOST_INPUTS):
        shape = self.shapes[name]
        argument = self.migraphx.allocate_gpu(shape)
        self.arguments[name] = argument
        dtype = np.dtype(np.uint8 if name in ('new_img', 'state_img_q') else np.float32)
        layout = {'address': int(argument.data_ptr()), 'nbytes': int(shape.bytes()),
                  'shape': list(shape.lens()), 'dtype': str(dtype)}
        (self.state_layout if name in STATE_SPECS else self.input_layout)[name] = layout
        if name in STATE_SPECS:
          lens, expected_dtype = STATE_SPECS[name]
          assert tuple(shape.lens()) == lens and shape.bytes() == int(np.prod(lens)) * expected_dtype.itemsize
        else:
          staging = self.migraphx.allocate_gpu(shape, host=True)
          self.staging_arguments[name] = staging
          view = np.asarray(staging)
          assert view.dtype == dtype and view.flags.c_contiguous and view.nbytes == shape.bytes()
          view.fill(0)
          self.staging_views[name] = view
      shape = self.shapes[HOST_OUTPUT_PARAM]
      assert tuple(shape.lens()) == (1, 18580) and shape.bytes() == 74320
      self.main_output_argument = self.migraphx.allocate_gpu(shape, host=True)
      self.arguments[HOST_OUTPUT_PARAM] = self.main_output_argument
      self.main_output_view = np.asarray(self.main_output_argument, np.float32).reshape(-1)
      layouts = list(self.state_layout.values()) + list(self.input_layout.values())
      assert all(not overlaps(a, b) for i, a in enumerate(layouts) for b in layouts[i + 1:])
      self._reset_states()
      self._enqueue_body()
      self.hip.synchronize(self.stream)
      self._reset_states()
      if self.capture:
        self.hip.begin_capture(self.stream)
        self.capturing = True
        self._enqueue_body()
        self.graph = self.hip.end_capture(self.stream)
        self.capturing = False
        self.graph_audit = validate_topology(self.hip.graph_topology(self.graph))
        self.executable = self.hip.instantiate(self.graph)
    except BaseException:
      try:
        self.close()
      except BaseException:
        pass
      raise

  def _reset_states(self):
    for state in self.state_layout.values():
      self.hip.zero(state['address'], state['nbytes'], self.stream)
    self.hip.synchronize(self.stream)

  def reset_navigation(self, *, map_reset=False, navigation_reset=False):
    """Clear only selected history buffers, preserving every captured address."""
    assert not self.closed and not self.capturing, 'Cannot reset navigation in a closed or capturing engine'
    for channel, selected in (('map', map_reset), ('navigation', navigation_reset)):
      if selected:
        state = self.state_layout[NAV_STATE_KEYS[channel]]
        self.hip.zero(state['address'], state['nbytes'], self.stream)
    if map_reset or navigation_reset:
      self.hip.synchronize(self.stream)

  def _enqueue_body(self):
    for name in HOST_INPUTS:
      layout = self.input_layout[name]
      self.hip.copy(layout['address'], self.staging_arguments[name].data_ptr(), layout['nbytes'], 1, self.stream)
    values = self.program.run_async(self.arguments, self.stream, 'ihipStream_t')
    outputs = list(values.values()) if isinstance(values, dict) else list(values)
    assert len(outputs) == 6 and outputs[0].data_ptr() == self.main_output_argument.data_ptr()
    layout = []
    for (name, (lens, dtype)), output in zip(STATE_SPECS.items(), outputs[1:], strict=True):
      shape = output.get_shape()
      assert tuple(shape.lens()) == lens and shape.bytes() == self.state_layout[name]['nbytes']
      item = {'name': name, 'address': int(output.data_ptr()), 'nbytes': int(shape.bytes()),
              'shape': list(lens), 'dtype': str(dtype)}
      assert all(not overlaps(item, own) for own in (*self.state_layout.values(), *self.input_layout.values()))
      layout.append(item)
    if self.last_output_layout:
      assert layout == self.last_output_layout, 'Scratch output pointers changed after warmup'
    self.last_output_layout = layout
    self.output_owners = outputs
    for item in layout:
      self.hip.copy(self.state_layout[item['name']]['address'], item['address'], item['nbytes'], 3, self.stream)

  def step(self, host_feeds, audit=False):
    assert not self.closed, 'ResidentEngine is closed'
    start = time.perf_counter_ns()
    assert set(HOST_INPUTS) <= set(host_feeds)
    for name in HOST_INPUTS:
      value, staging = host_feeds[name], self.staging_views[name]
      assert isinstance(value, np.ndarray) and value.flags.c_contiguous, ('host_input_not_contiguous', name)
      assert value.shape == staging.shape, ('host_input_shape', name)
      assert value.dtype == staging.dtype, ('host_input_dtype', name)
    for name in HOST_INPUTS:
      np.copyto(self.staging_views[name], host_feeds[name], casting='no')
    ready = time.perf_counter_ns()
    if self.capture:
      self.hip.launch(self.executable, self.stream)
    else:
      self._enqueue_body()
    self.hip.synchronize(self.stream)
    finished = time.perf_counter_ns()
    main = self.main_output_view.copy()
    assert main.flags.owndata
    copied = time.perf_counter_ns()
    self.frames += 1
    self.last_timings_ns = {'input_prepare_ns': ready - start, 'graph_call_ns': finished - ready,
                            'output_view_ns': copied - finished}
    if audit:
      assert np.isfinite(main).all(), 'nonfinite_main_output'
    return main

  def snapshot_state_host(self):
    assert not self.closed, 'ResidentEngine is closed'
    arrays = {name: np.empty(lens, dtype=dtype) for name, (lens, dtype) in STATE_SPECS.items()}
    for name, array in arrays.items():
      self.hip.copy(array.ctypes.data, self.state_layout[name]['address'], array.nbytes, 2, self.stream)
    self.hip.synchronize(self.stream)
    return arrays

  def navigation_evidence(self, sample_gpu=False):
    """Inspect a completed step; optional D2H copies stay outside the captured graph."""
    result = {'error_code': 0, 'staging_valid': False, 'gpu_sampled': False, 'gpu_match': False,
              'gpu_error': False, 'diagnostic_error': False, 'gpu_sample_mono_time': 0,
              'gpu_sample_duration_ns': 0, 'gpu_nav_crc32': 0, 'gpu_map_crc32': 0}
    try:
      result['staging_nav_crc32'] = crc32_buffer(self.staging_views['navigation'])
      result['staging_map_crc32'] = crc32_buffer(self.staging_views['nav_map'])
      result['staging_valid'] = True
    except Exception:
      result.update(diagnostic_error=True, error_code=ERROR_STAGING)
    if not sample_gpu:
      return result
    started = time.monotonic_ns()
    try:
      assert not self.closed and not self.capturing and self.frames > 0
      for name in ('navigation', 'nav_map'):
        if name not in self.nav_readback_arguments:
          argument = self.migraphx.allocate_gpu(self.shapes[name], host=True)
          view = np.asarray(argument)
          staging = self.staging_views[name]
          assert view.shape == staging.shape and view.dtype == staging.dtype and view.flags.c_contiguous
          assert view.nbytes == self.input_layout[name]['nbytes']
          self.nav_readback_arguments[name] = argument
          self.nav_readback_views[name] = view
      for name in ('navigation', 'nav_map'):
        layout = self.input_layout[name]
        self.hip.copy(self.nav_readback_arguments[name].data_ptr(), layout['address'], layout['nbytes'], 2, self.stream)
      self.hip.synchronize(self.stream)
      result['gpu_sample_mono_time'] = time.monotonic_ns()
      result['gpu_nav_crc32'] = crc32_buffer(self.nav_readback_views['navigation'])
      result['gpu_map_crc32'] = crc32_buffer(self.nav_readback_views['nav_map'])
      result['gpu_sampled'] = True
      result['gpu_match'] = (result['staging_valid'] and result['gpu_nav_crc32'] == result['staging_nav_crc32']
                             and result['gpu_map_crc32'] == result['staging_map_crc32'])
    except Exception:
      result.update(gpu_error=True, diagnostic_error=True, error_code=result['error_code'] | ERROR_GPU)
    result['gpu_sample_duration_ns'] = time.monotonic_ns() - started
    return result

  def audit_layout(self):
    return {'persistent_state_buffers': self.state_layout, 'persistent_input_buffers': self.input_layout,
            'last_graph_state_outputs': self.last_output_layout,
            'graph_outputs_disjoint_from_persistent_inputs': all(not overlaps(output, own)
              for output in self.last_output_layout for own in (*self.state_layout.values(), *self.input_layout.values()))}

  def metadata(self):
    return {'model_path': self.model_path, 'capture_enabled': self.capture,
            'parameters': {name: str(shape) for name, shape in self.shapes.items()},
            'state_order': list(STATE_SPECS), 'state_bytes': sum(x['nbytes'] for x in self.state_layout.values()),
            'host_input_bytes': sum(x['nbytes'] for x in self.input_layout.values()), 'main_output_bytes': 74320,
            'persistent_pinned_staging': True, 'returned_main_output_owned_copy': True,
            'state_commit_in_step': True, 'state_device_to_host_in_step': False,
            'warmup_state_reset': True, 'explicit_nonblocking_stream': True,
            'graph_call_scope': '6 H2D uploads + 492 compute kernels + main copy + 5 state D2D commits + final stream sync',
            'graph_call_not_directly_comparable_to_program_run': True,
            'completed_step_scope': 'staging copies + complete device chain + owned main output copy',
            'graph_audit': self.graph_audit}

  def close(self):
    if self.closed:
      return
    self.closed = True
    errors = []

    def attempt(function, *args):
      try:
        return function(*args)
      except BaseException as error:
        errors.append(error)
        return None

    if self.capturing:
      self.graph = attempt(self.hip.end_capture, self.stream)
      self.capturing = False
    if self.stream is not None:
      attempt(self.hip.synchronize, self.stream)
    if self.executable is not None:
      attempt(self.hip.destroy_exec, self.executable)
    if self.graph is not None:
      attempt(self.hip.destroy_graph, self.graph)
    self.executable = self.graph = None
    self.output_owners.clear()
    self.main_output_view = None
    self.staging_views.clear()
    self.nav_readback_views.clear()
    self.arguments.clear()
    self.staging_arguments.clear()
    self.nav_readback_arguments.clear()
    self.main_output_argument = None
    self.program = None
    if self.stream is not None:
      attempt(self.hip.destroy_stream, self.stream)
    self.stream = None
    if errors:
      raise errors[0]
