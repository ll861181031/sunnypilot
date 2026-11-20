from types import SimpleNamespace

import pytest

from openpilot.selfdrive.controls.lib.turn_speed_guard import TurnSpeedGuard


def diagnostics(now_ns, **changes):
  values = {'valid': True, 'sampleMonoTime': now_ns, 'lastSubmittedMonoTime': now_ns - 10_000_000,
            'requestedActive': True, 'active': True, 'submitted': False, 'desiredAngleDeg': 30.0,
            'measuredAngleDeg': 10.0, 'finalAngleDeg': 14.0, 'leadBoundDeg': 5.0, 'limitMask': 1}
  values.update(changes)
  return SimpleNamespace(bydLateral=SimpleNamespace(**values), epsTooFast=False)


def update(guard, now_ns, diag=None, **changes):
  args = {'now_ns': now_ns, 'message_alive': True, 'message_valid': True, 'long_active': True,
          'lateral_active': True, 'overriding': False, 'v_ego': 8.0}
  args.update(changes)
  return guard.update(diag if diag is not None else diagnostics(now_ns), **args)


def confirm(guard, start=1_000_000_000, **changes):
  for frame in range(8):
    result = update(guard, start + frame * 50_000_000, **changes)
  return result


def test_shadow_records_without_changing_acceleration_limit():
  result = confirm(TurnSpeedGuard())
  assert result.valid and result.active
  assert result.proposed_accel_max == 0.0
  assert result.accel_max == float('inf')
  assert result.error_deg == 20.0
  assert result.pressure_seconds >= 0.3
  assert 'lead' in result.reason


def test_enforced_limit_only_suppresses_positive_acceleration():
  result = confirm(TurnSpeedGuard(enforce=True))
  assert result.accel_max == 0.0
  assert min(0.8, result.accel_max) == 0.0
  assert min(-1.2, result.accel_max) == -1.2
  assert min(-2.0, -0.5, result.accel_max) == -2.0


def test_pressure_requires_elapsed_new_samples():
  guard = TurnSpeedGuard()
  assert not update(guard, 1_000_000_000).active
  for _ in range(20):
    result = update(guard, 1_050_000_000, diagnostics(1_000_000_000))
    assert not result.active
    assert result.pressure_seconds == 0.0
  assert not update(guard, 1_100_000_000).active


def test_non_submitted_sample_keeps_recent_actual_submission_usable():
  guard = TurnSpeedGuard()
  assert confirm(guard).active
  result = update(guard, 1_400_000_000, diagnostics(1_400_000_000, submitted=False))
  assert result.valid and result.active


@pytest.mark.parametrize('change', [
  {'message_alive': False}, {'message_valid': False}, {'long_active': False},
  {'lateral_active': False}, {'overriding': True}, {'v_ego': 0.0}, {'v_ego': float('nan')},
])
def test_invalid_and_driver_override_reset_persistence(change):
  guard = TurnSpeedGuard()
  assert confirm(guard).active
  result = update(guard, 1_400_000_000, **change)
  assert not result.active and result.accel_max == float('inf')
  assert not update(guard, 1_450_000_000).active


@pytest.mark.parametrize('change', [
  {'valid': False}, {'requestedActive': False}, {'active': False},
  {'sampleMonoTime': 0}, {'sampleMonoTime': 1_500_000_000},
  {'lastSubmittedMonoTime': 0}, {'lastSubmittedMonoTime': 1_500_000_000},
  {'lastSubmittedMonoTime': 1_000_000_000}, {'leadBoundDeg': 0.0},
  {'desiredAngleDeg': float('nan')}, {'finalAngleDeg': float('inf')},
])
def test_unusable_diagnostics_reset_persistence(change):
  guard = TurnSpeedGuard()
  assert confirm(guard).active
  result = update(guard, 1_400_000_000, diagnostics(1_400_000_000, **change))
  assert not result.valid and not result.active
  assert not update(guard, 1_450_000_000).active


def test_stale_sample_and_clock_reversal_reset_persistence():
  guard = TurnSpeedGuard()
  assert confirm(guard).active
  assert not update(guard, 1_700_000_000, diagnostics(1_350_000_000)).valid
  assert not update(guard, 1_750_000_000).active
  assert not update(guard, 1_700_000_000).valid


@pytest.mark.parametrize('angles', [
  (10.0, 8.0, 9.0), (30.0, -10.0, -5.0), (10.0, 30.0, 25.0),
  (-10.0, -30.0, -25.0), (30.0, 10.0, 8.0),
])
def test_small_error_unwinding_and_direction_change_are_not_pressure(angles):
  guard = TurnSpeedGuard()
  for frame in range(20):
    now = 1_000_000_000 + frame * 50_000_000
    diag = diagnostics(now, desiredAngleDeg=angles[0], measuredAngleDeg=angles[1], finalAngleDeg=angles[2])
    assert not update(guard, now, diag).active


def test_negative_turn_and_below_scc_minimum_speed_still_record_pressure():
  guard = TurnSpeedGuard()
  for frame in range(8):
    now = 1_000_000_000 + frame * 50_000_000
    diag = diagnostics(now, desiredAngleDeg=-30.0, measuredAngleDeg=-10.0, finalAngleDeg=-14.0, limitMask=2)
    result = update(guard, now, diag, v_ego=2.0)
  assert result.active and 'rate' in result.reason


def test_error_without_limiter_or_eps_evidence_never_activates():
  guard = TurnSpeedGuard()
  for frame in range(20):
    now = 1_000_000_000 + frame * 50_000_000
    assert not update(guard, now, diagnostics(now, limitMask=0)).active


def test_eps_pulse_needs_persistent_same_direction_error():
  guard = TurnSpeedGuard()
  diag = diagnostics(1_000_000_000, limitMask=0)
  diag.epsTooFast = True
  assert not update(guard, 1_000_000_000, diag).active
  for frame in range(1, 8):
    now = 1_000_000_000 + frame * 50_000_000
    diag = diagnostics(now, limitMask=0)
    diag.epsTooFast = frame == 7
    result = update(guard, now, diag)
  assert result.active and 'eps_too_fast' in result.reason


def test_straight_driving_releases_and_new_turn_needs_confirmation():
  guard = TurnSpeedGuard()
  assert confirm(guard).active
  diag = diagnostics(1_400_000_000, desiredAngleDeg=0.0, measuredAngleDeg=0.0, finalAngleDeg=0.0, limitMask=0)
  assert not update(guard, 1_400_000_000, diag).active
  assert not update(guard, 1_450_000_000).active


def test_lateral_only_mads_does_not_record_longitudinal_constraint():
  guard = TurnSpeedGuard()
  result = confirm(guard, long_active=False, lateral_active=True)
  assert not result.active and result.proposed_accel_max == float('inf')


def test_opposite_turn_requires_a_new_confirmation_interval():
  guard = TurnSpeedGuard()
  assert confirm(guard).active
  diag = diagnostics(1_400_000_000, desiredAngleDeg=-30.0, measuredAngleDeg=-10.0, finalAngleDeg=-14.0)
  result = update(guard, 1_400_000_000, diag)
  assert result.valid and not result.active and result.pressure_seconds == 0.0
