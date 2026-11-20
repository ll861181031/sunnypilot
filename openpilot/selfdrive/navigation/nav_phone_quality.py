"""Phone navigation position quality shared by publisher and local consumers."""

PHONE_STRICT_ACCURACY_M = 5.0
PHONE_MAP_MAX_ACCURACY_M = 25.0
PHONE_STRICT_POSE_ERROR_M = 5.0
PHONE_MAP_MAX_POSE_ERROR_M = 25.0
PHONE_MAP_MAX_PROJECTION_ERROR_M = 40.0

QUALITY_STRICT = "accepted_strict"
QUALITY_DEGRADED_MAP = "accepted_degraded_map"
QUALITY_REASONS = frozenset((QUALITY_STRICT, QUALITY_DEGRADED_MAP))


def classify_phone_position_quality(horizontal_accuracy_m: float, native_pose_error_m: float) -> str:
  """Classify already range-checked evidence; degraded remains map-only."""
  return (QUALITY_STRICT
          if horizontal_accuracy_m <= PHONE_STRICT_ACCURACY_M and native_pose_error_m <= PHONE_STRICT_POSE_ERROR_M
          else QUALITY_DEGRADED_MAP)
