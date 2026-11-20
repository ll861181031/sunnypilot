"""Report semantics, never extra activation or weaker replay checks."""

import pytest
from openpilot.cereal import log

from openpilot.tools.car_porting.byd_longcontrol_replay import replay_events
from openpilot.tools.car_porting.tests.test_byd_longcontrol_replay import cp_event, pid_bundle
from openpilot.tools.car_porting.tests.test_byd_longitudinal_replay import can

from openpilot.tools.car_porting.runtime_support import (
  PLAN_TARGET_HELP, plan_target_fields_present,
)

pytestmark = pytest.mark.skipif(
  not plan_target_fields_present(), reason=PLAN_TARGET_HELP)



def run(events):
  return replay_events(sorted((e.as_reader() for e in events), key=lambda e: e.logMonoTime))


def test_enabled_denominator_is_not_the_whole_log_or_recorded_long_active():
  out = run([cp_event(), *pid_bundle(1_000_000_000, enabled=False), *pid_bundle(1_020_000_000, 1),
             *pid_bundle(1_040_000_000, 2)])
  assert 'diagnostics' in out, 'Missing explanatory coverage diagnostics'
  d = out['diagnostics']
  assert d['request_partitions']['not_requested']['ticks'] == 2
  assert d['request_partitions']['requested']['ticks'] == 3
  assert d['request_partitions']['requested']['states'] == {'arming': 1, 'tracking': 2}
  assert d['request_partitions']['requested']['output_ticks'] == 2
  assert d['output_run_tick_histogram'] == {'2': 1}
  assert d['max_contiguous_output_ticks'] == 2
  assert out['pid_output_ticks'] == 2 and out['recorded_long_active_ticks'] == 0


def test_latch_trigger_and_later_inherited_block_are_attributed_separately():
  bad = pid_bundle(1_040_000_000, 2)
  bad[2].valid = False
  out = run([cp_event(), *pid_bundle(1_000_000_000), *pid_bundle(1_020_000_000, 1), *bad,
             *pid_bundle(1_060_000_000, 3), *pid_bundle(1_080_000_000, 4, enabled=False),
             *pid_bundle(1_100_000_000, 5), *pid_bundle(1_120_000_000, 6)])
  assert 'diagnostics' in out, 'Missing latch origin trace'
  d = out['diagnostics']
  assert d['latch_trigger_counts'] == {'invalid_longitudinalPlan': 1}
  assert d['inherited_latch_ticks_by_trigger'] == {'invalid_longitudinalPlan': 2}
  episode, = d['latch_episode_examples']
  assert episode['start_ns'] == 1_041_000_000
  assert episode['end_ns_exclusive'] == 1_081_000_000
  assert episode['latched_grid_ticks'] == 4
  assert episode['inherited_block_ticks'] == 2
  assert episode['end_reason'] == 'healthy_disengagement'
  assert out['pid_output_ticks'] == 5


def test_latch_that_reaches_segment_end_is_not_called_recovered():
  bad = pid_bundle(1_020_000_000, 1)
  bad[3].carState.brakePressed = True
  out = run([cp_event(), *pid_bundle(1_000_000_000), *bad, *pid_bundle(1_040_000_000, 2)])
  assert 'diagnostics' in out, 'Missing segment-end latch reporting'
  d = out['diagnostics']
  assert d['latch_episode_examples'][0]['end_reason'] == 'segment_end_not_recovered'
  assert d['latch_episode_examples'][0]['latched_grid_ticks'] == 3
  assert d['latch_episode_examples'][0]['inherited_block_ticks'] == 1


def test_stale_enable_is_unknown_not_a_disabled_or_eligible_tick():
  events = [cp_event()]
  for i in range(11):
    b = pid_bundle(1_000_000_000 + i * 20_000_000, i)
    events.extend(b if i in (0, 10) else b[:-1])
  out = run(events)
  assert 'diagnostics' in out, 'Missing stale request partition'
  d = out['diagnostics']
  assert d['request_partitions']['requested']['ticks'] == 11
  assert d['request_partitions']['unknown']['ticks'] == 10
  assert d['request_partitions']['not_requested']['ticks'] == 0
  assert out['pid_output_ticks'] == 9


def test_negative_filtered_speed_is_reported_separately_from_nonfinite_and_brake():
  b = pid_bundle(1_000_000_000)
  b[3].carState.vEgo = -.01
  b[3].carState.vEgoRaw = 0
  b[3].carState.brakePressed = True
  out = run([cp_event(), *b])
  assert 'diagnostics' in out, 'Missing raw vehicle evidence classification'
  v = out['diagnostics']['vehicle_observations']
  assert v['valid_samples'] == 1
  assert v['flags']['negative_filtered_speed'] == 1
  assert v['flags']['negative_filtered_with_zero_raw'] == 1
  assert v['flags'].get('nonfinite_speed', 0) == 0
  assert v['flags'].get('nonfinite_acceleration', 0) == 0
  assert v['negative_speed_range_mps'] == pytest.approx([-.01, -.01])
  assert out['pid_output_ticks'] == 0


@pytest.mark.parametrize('identical', [True, False])
def test_duplicate_batch_diagnostics_do_not_pick_a_frame_to_enable_replay(identical):
  b = pid_bundle(1_020_000_000, 1)
  frames = [f.to_dict() for f in b[5].can]
  duplicate = frames[3].copy()
  if not identical:
    duplicate['dat'] = bytes(can(1_020_000_000, 2).can[3].dat)
  b[5].can = frames + [duplicate]
  out = run([cp_event(), *pid_bundle(1_000_000_000), *b, *pid_bundle(1_040_000_000, 2)])
  assert 'diagnostics' in out, 'Missing batch ambiguity classification'
  stats = out['diagnostics']['can_batch_observations']['template']
  assert stats['ambiguous_batches'] == 1 and stats['ambiguous_frames'] == 2
  assert stats['identical_payload_batches' if identical else 'distinct_payload_batches'] == 1
  assert stats['valid_payload_ambiguous_batches'] == 1
  assert out['pid_output_ticks'] == 1


def test_transport_echo_and_rejection_are_never_ecu_acceptance_evidence():
  e = log.Event.new_message(logMonoTime=1_000_000_000, valid=True)
  raw = bytes.fromhex('5366669937dcf044')
  e.can = [{'address': 0x32E, 'src': src, 'dat': raw} for src in (0, 2, 128, 192)]
  tx = log.Event.new_message(logMonoTime=1_000_000_001, valid=True)
  tx.sendcan = [{'address': 0x32E, 'src': 0, 'dat': raw}]
  out = run([e, tx])
  assert 'diagnostics' in out, 'Missing transport evidence accounting'
  d = out['diagnostics']
  assert d['longitudinal_transport_frames'] == {
    'can:src0:0x32e': 1, 'can:src2:0x32e': 1, 'can:src128:0x32e': 1,
    'can:src192:0x32e': 1, 'sendcan:src0:0x32e': 1}
  assert d['ecu_acceptance_established'] is False and out['pid_output_ticks'] == 0


def test_invalid_vehicle_and_can_envelopes_do_not_become_valid_evidence():
  b = pid_bundle(1_000_000_000)
  b[3].valid = b[5].valid = False
  out = run([cp_event(), *b])
  assert 'diagnostics' in out, 'Missing invalid evidence accounting'
  d = out['diagnostics']
  assert d['vehicle_observations']['invalid_samples'] == 1
  assert d['vehicle_observations']['valid_samples'] == 0
  assert not d['longitudinal_transport_frames']
  assert d['invalid_can_envelopes'] == 1


def test_diagnostic_state_and_latch_origin_do_not_leak_between_segments():
  bad = pid_bundle(1_020_000_000, 1)
  bad[2].valid = False
  first = run([cp_event(), *pid_bundle(1_000_000_000), *bad])
  second = run([cp_event(), *pid_bundle(1_040_000_000), *pid_bundle(1_060_000_000, 1)])
  assert 'diagnostics' in first and 'diagnostics' in second, 'Missing independent diagnostics'
  assert first['diagnostics']['latch_trigger_counts']
  assert second['diagnostics']['latch_trigger_counts'] == {}
  assert second['pid_output_ticks'] == 2


@pytest.mark.parametrize('reverse', [False, True])
def test_ambiguous_enable_envelope_is_not_a_known_request(reverse):
  b = pid_bundle(1_000_000_000)
  duplicate = log.Event.new_message(logMonoTime=b[6].logMonoTime, valid=True)
  duplicate.init('carControl')
  duplicate.carControl.enabled = False
  control = [b[6], duplicate]
  if reverse:
    control.reverse()
  out = run([cp_event(), *b[:-1], *control])
  d = out['diagnostics']
  assert d['request_partitions']['unknown']['ticks'] == 1
  assert d['request_partitions']['requested']['ticks'] == 0


def test_payload_counter_difference_is_distinguished_from_decoded_field_disagreement():
  b = pid_bundle(1_020_000_000, 1)
  frames = [f.to_dict() for f in b[5].can]
  # Captured same host batch: physical pedal values agree but raw counter differs.
  frames[2]['dat'] = bytes.fromhex('000017d4262800c6')
  extra = dict(frames[2], dat=bytes.fromhex('000017d4262810b6'))
  b[5].can = frames + [extra]
  out = run([cp_event(), *pid_bundle(1_000_000_000), *b])
  stats = out['diagnostics']['can_batch_observations']['pedals']
  assert stats.get('same_declared_fields_ambiguous_batches', 0) == 1
  assert out['diagnostics']['latch_trigger_counts'] == {'ambiguous_pedals_batch': 1}
  assert out['pid_output_ticks'] == 1


def test_bad_payload_duplicate_never_becomes_field_equivalence_evidence():
  b = pid_bundle(1_020_000_000, 1)
  frames = [f.to_dict() for f in b[5].can]
  extra = dict(frames[3], dat=b'\x00')
  b[5].can = frames + [extra]
  out = run([cp_event(), *pid_bundle(1_000_000_000), *b])
  stats = out['diagnostics']['can_batch_observations']['template']
  assert stats['invalid_payload_ambiguous_batches'] == 1
  assert stats.get('same_declared_fields_ambiguous_batches', 0) == 0
  assert out['pid_output_ticks'] == 1


def test_newer_normal_request_does_not_hide_ambiguity_before_grid_tick():
  events = [cp_event(), *pid_bundle(1_000_000_000)]
  for enabled in (False, True):
    duplicate = log.Event.new_message(logMonoTime=1_005_000_000, valid=True)
    duplicate.init('carControl')
    duplicate.carControl.enabled = enabled
    events.append(duplicate)
  good = log.Event.new_message(logMonoTime=1_006_000_000, valid=True)
  good.init('carControl')
  good.carControl.enabled = True
  events.append(good)
  events.extend(pid_bundle(1_020_000_000, 1))
  out = run(events)
  assert out['diagnostics']['request_partitions']['unknown']['ticks'] == 1
  assert out['diagnostics']['request_partitions']['requested']['ticks'] == 2
  assert out['pid_output_ticks'] == 0
