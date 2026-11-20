"""Capability probes for the offline BYD car-porting tooling.

Part of this toolchain was written against a newer sunnypilot than the one in
this checkout: its `LongitudinalPlan` carries `vTargetNow`/`jTargetNow` and its
panda exposes `CAN_HEALTH_PACKET_VERSION`. Neither exists here, so the affected
tools fail with a cryptic capnp / AttributeError instead of saying what is
wrong. These probes turn that into one actionable message, and the accompanying
tests skip (rather than fail) until the capability appears — so they start
running automatically once the schema/panda is synced.
"""

from openpilot.cereal import log


REQUIRED_PLAN_TARGET_FIELDS = ("vTargetNow", "jTargetNow")

# Built with explicit joins: ruff's ISC002 forbids implicit multi-line concatenation here.
PLAN_TARGET_HELP = " ".join([
  "This tool needs",
  " and ".join(f"LongitudinalPlan.{name}" for name in REQUIRED_PLAN_TARGET_FIELDS) + ",",
  "which this checkout's cereal schema does not define.",
  "The tool targets a newer sunnypilot: either sync this fork's cereal,",
  "or re-derive the tool's data model (the nearest available fields are",
  "LongitudinalPlan.speeds/jerks, which are NOT equivalent).",
])

CAN_HEALTH_HELP = " ".join([
  "This tool needs panda.Panda.CAN_HEALTH_PACKET_VERSION, which this checkout's panda does not expose",
  "(only HEALTH_PACKET_VERSION). It targets a newer panda submodule.",
])


def plan_target_fields_present() -> bool:
  fields = log.LongitudinalPlan.schema.fields
  names = set(fields.keys()) if hasattr(fields, "keys") else {f.name for f in fields}
  return all(name in names for name in REQUIRED_PLAN_TARGET_FIELDS)


def can_health_packet_version_present() -> bool:
  try:
    from panda import Panda
  except Exception:
    return False
  return hasattr(Panda, "CAN_HEALTH_PACKET_VERSION")


def require_plan_target_fields() -> None:
  if not plan_target_fields_present():
    raise RuntimeError(PLAN_TARGET_HELP)


def require_can_health_packet_version() -> None:
  if not can_health_packet_version_present():
    raise RuntimeError(CAN_HEALTH_HELP)
