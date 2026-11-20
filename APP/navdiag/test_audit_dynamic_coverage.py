import copy
import json

import pytest

from APP.navdiag.audit_dynamic_coverage import anchor, audit, scan_records


def frame(index, point=0, segment=0, link=0, identity='a', sequence=None):
  source = {'segment_index': segment, 'link_index': link, 'point_index': point}
  seq = index + 1 if sequence is None else sequence
  return {'index': index, 'since_start_ms': index * 250.0, 'sequence': seq,
          'has_sample': True, 'map_valid': True, 'transport_session_hash': 'b' * 64,
          'sample_position_diagnostics': {'source_anchors': {'native_before': source,
                                                             'native_after': dict(source)}},
          'route_identity_diagnostics': {'source_consumer_binding_match': True,
                                         'selected': {'path_identity_hash': identity * 64,
                                                      'transport_session_hash': 'b' * 64,
                                                      'source_object_hash': 'c' * 64,
                                                      'observed_sequence': seq}}}


def test_static_nonzero_segment_is_not_dynamic():
  result = scan_records([frame(0, segment=1), frame(1, segment=1)])
  assert result['indices'] == [{'indices': [1, 0, 0], 'frames': 2}]
  assert result['counts']['observed_index_change_events'] == 0


@pytest.mark.parametrize('field,value,category', [('point', 1, 'point'), ('link', 1, 'link'), ('segment', 1, 'segment')])
def test_same_identity_new_sequence_observes_index_change(field, value, category):
  result = scan_records([frame(0), frame(1, **{field: value})])
  assert result['counts']['observed_index_change_events'] == 1
  assert result['events'][0]['category'] == category


def test_route_change_does_not_imply_motion():
  result = scan_records([frame(0), frame(1, segment=1, identity='d')])
  assert result['counts']['observed_index_change_events'] == 0
  assert result['counts']['identity_gap_or_order_breaks'] == 1


@pytest.mark.parametrize('kind', ['missing', 'binding_unknown', 'read_unstable', 'index_gap', 'time_backwards',
                                 'sequence_backwards', 'clock_nan', 'clock_inf', 'boolean_observed_sequence'])
def test_unknown_or_discontinuous_frame_cuts_comparison(kind):
  middle = frame(1, point=1)
  if kind == 'missing':
    middle['sample_position_diagnostics'] = None
  elif kind == 'binding_unknown':
    middle['route_identity_diagnostics']['source_consumer_binding_match'] = 'unknown'
  elif kind == 'read_unstable':
    middle['sample_position_diagnostics']['source_anchors']['native_after']['point_index'] = 2
  elif kind == 'index_gap':
    middle['index'] = 4
  elif kind == 'time_backwards':
    middle['since_start_ms'] = -1
  elif kind == 'sequence_backwards':
    middle['sequence'] = middle['route_identity_diagnostics']['selected']['observed_sequence'] = 0
  elif kind == 'clock_nan':
    middle['since_start_ms'] = float('nan')
  elif kind == 'clock_inf':
    middle['since_start_ms'] = float('inf')
  elif kind == 'boolean_observed_sequence':
    middle['sequence'] = 1
    middle['route_identity_diagnostics']['selected']['observed_sequence'] = True
  result = scan_records([frame(0), middle])
  assert result['counts']['observed_index_change_events'] == 0


def test_same_sequence_changed_anchor_is_inconsistency():
  result = scan_records([frame(0, sequence=1), frame(1, point=1, sequence=1)])
  assert result['counts']['same_sequence_anchor_inconsistencies'] == 1
  assert result['counts']['observed_index_change_events'] == 0


@pytest.mark.parametrize('value', [True, -1, 'unknown', None, 0.0])
def test_anchor_rejects_unknown_or_noninteger(value):
  assert anchor({'segment_index': 0, 'link_index': 0, 'point_index': value}) is None


def test_file_audit_uses_only_raw_rows_and_binds_sha(tmp_path):
  first, second = tmp_path / 'one.jsonl', tmp_path / 'two.jsonl'
  first.write_text(json.dumps(frame(0)) + '\n')
  second.write_text(json.dumps(frame(1, point=1)) + '\n')
  result = audit([first, second, first])
  assert result['files_scanned'] == 2
  assert result['counts']['observed_index_change_events'] == 0
  assert all(len(row['sha256']) == 64 for row in result['files'])
  for key in ('actual_motion_verified', 'terminal_behavior_verified', 'roundabout_behavior_verified', 'target_complete'):
    assert result[key] is False
  original = copy.deepcopy(result)
  first.write_text(json.dumps(frame(0, point=2)) + '\n')
  assert audit([first, second]) != original
