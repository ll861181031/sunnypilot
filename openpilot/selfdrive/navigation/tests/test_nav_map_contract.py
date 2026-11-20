"""Schema compatibility and exact map-frame/route correlation, without starting services."""
from dataclasses import replace
import hashlib
from pathlib import Path
import re

import capnp
import pytest

from openpilot.cereal import custom, log
from openpilot.cereal import messaging
from openpilot.cereal.services import SERVICE_LIST
from openpilot.selfdrive.navigation import nav_map_contract as contract
from openpilot.selfdrive.navigation.phone_route import ActiveRouteBinding, RouteIdentity, SourceProvenance, bind_geometry


def route_data():
  raw = b"verified decoded geometry fixture"
  points = [(106.414900123456, 31.030100123456), (106.415000234567, 31.030100234567),
            (106.415100345678, 31.030200345678)]
  identity = RouteIdentity("amap", "source-1", "route-42", 3, hashlib.sha256(raw).hexdigest())
  provenance = SourceProvenance.from_decoded(identity=identity, source_bytes=raw, source_object_id="cache-1",
    decoder_id="fixture-v1", coordinate_system="WGS84", decoded_points=points, expected_point_count=3, complete=True)
  active = ActiveRouteBinding(identity, "transport-1", "cache-1", 7, 10.0, True)
  payload = {"provider": identity.provider, "source_session_id": identity.source_session_id, "route_id": identity.route_id,
             "revision": identity.revision, "cache_sha256": identity.cache_sha256, "coordinate_system": "WGS84",
             "coordinate_order": "lon_lat", "geometry_kind": "full_route", "complete": True, "points": points, "point_count": 3}
  snapshot = bind_geometry(payload, provenance=provenance, active_binding=active,
                           transport_session_id="transport-1", now=10.1)
  return snapshot, active


def frame_data():
  snapshot, active = route_data()
  pixels = bytes([128]) * (256 * 256)
  frame = contract.MapPixels(frame_id=42, timestamp_sof_ns=10_050_000_000, timestamp_eof_ns=10_100_000_000,
                             width=256, height=256, pixels=pixels)
  metadata = contract.make_map_frame(snapshot, active, frame, renderer_instance_id="mapsd-instance-1",
    location_mono_time_ns=10_050_000_000, projection=contract.ProjectionEvidence(2.5, 4.0, 2, 35.0))
  return snapshot, active, frame, metadata


def local_sm():
  snapshot, active = route_data()
  route = log.NavRoute.new_message(**contract.route_fields(snapshot, active, now_ns=10_100_000_000))
  instruction = log.NavInstruction.new_message(allManeuvers=[{"distance": 30.0, "type": "turn", "modifier": "left"}],
    routeBinding=contract.binding_from_snapshot(snapshot, active, now_ns=10_100_000_000).to_dict())

  class SubMasterFixture:
    valid = {"navRoute": True, "navInstruction": True}
    updated = {"navRoute": True, "navInstruction": True}
    logMonoTime = {"navRoute": 10_100_000_000, "navInstruction": 10_100_000_000}
    recv_time = {"navRoute": 10.11, "navInstruction": 10.11}

    def __getitem__(self, key):
      return {"navRoute": route, "navInstruction": instruction}[key]

  return SubMasterFixture()


def match(matcher, frame, snapshot, active, now_ns=10_200_000_000, instruction=None):
  if instruction is None:
    instruction = contract.binding_from_snapshot(snapshot, active, now_ns=now_ns)
  return matcher.match(frame, snapshot=snapshot, active_binding=active, instruction_binding=instruction, now_ns=now_ns)


def test_schema_registers_real_sidecar_service_without_moving_old_event_ordinals():
  fields = log.Event.schema.fields
  assert fields["navMapFrameSP"].proto.ordinal.explicit == 155
  assert fields["navRoute"].proto.ordinal.explicit == 83
  assert fields["navInstruction"].proto.ordinal.explicit == 82
  assert fields["customReserved10"].proto.ordinal.explicit == 136
  assert SERVICE_LIST["navMapFrameSP"].frequency == 20
  assert SERVICE_LIST["navMapFrameSP"].should_log
  assert SERVICE_LIST["navMapFrameSP"].decimation is None


def test_double_route_and_binding_round_trip_through_real_event():
  snapshot, active = route_data()
  fields = contract.route_fields(snapshot, active, now_ns=10_100_000_000)
  msg = messaging.new_message("navRoute", valid=True)
  msg.navRoute.from_dict(fields)
  with log.Event.from_bytes(msg.to_bytes()) as decoded:
    assert decoded.navRoute.coordinates64[0].longitude == snapshot.coordinates[0][0]
    assert decoded.navRoute.coordinates64[0].latitude == snapshot.coordinates[0][1]
    assert len(decoded.navRoute.coordinates) == 0
    binding = contract.WireRouteBinding.from_dict(decoded.navRoute.routeBinding.to_dict())
    assert binding.binding_id == snapshot.binding_id
    assert binding.identity == snapshot.identity
    assert binding.observed_mono_time_ns == 10_000_000_000
    assert not isinstance(binding, ActiveRouteBinding)


def test_binding_cannot_be_serialized_from_wire_or_self_selected_payload():
  snapshot, active = route_data()
  with pytest.raises(contract.RouteRejected):
    contract.binding_from_snapshot(snapshot, {"selected": True}, now_ns=10_100_000_000)
  with pytest.raises(contract.RouteRejected):
    contract.binding_from_snapshot(snapshot, replace(active, selected=False), now_ns=10_100_000_000)
  with pytest.raises(contract.RouteRejected):
    contract.binding_from_snapshot(snapshot, replace(active, transport_session_id="other"), now_ns=10_100_000_000)


def test_map_metadata_capnp_round_trip_preserves_pixels_and_projection_evidence():
  _, _, _, metadata = frame_data()
  msg = messaging.new_message("navMapFrameSP", valid=True)
  msg.navMapFrameSP.from_dict(metadata.to_dict())
  with log.Event.from_bytes(msg.to_bytes()) as decoded:
    restored = contract.MapFrameMetadata.from_dict(decoded.navMapFrameSP.to_dict())
    assert restored == metadata
    assert restored.projection.raw_gps_lateral_m == 2.5
    assert restored.pixel_format == "gray8"
    assert restored.width == restored.height == 256


@pytest.mark.parametrize("version", [0, 2, True, "1"])
def test_unknown_binding_version_never_implies_selected(version):
  snapshot, active = route_data()
  data = contract.binding_from_snapshot(snapshot, active, now_ns=10_100_000_000).to_dict()
  data["version"] = version
  with pytest.raises(contract.RouteRejected):
    contract.WireRouteBinding.from_dict(data)


def test_exact_frame_accepts_in_either_arrival_order():
  snapshot, active, frame, metadata = frame_data()
  matcher = contract.MapFrameMatcher("mapsd-instance-1")
  assert match(matcher, frame, snapshot, active).reason == "metadata_missing"
  matcher.add_metadata(metadata, received_mono_time_ns=10_120_000_000)
  assert match(matcher, frame, snapshot, active).accepted


@pytest.mark.parametrize("change,reason", [({"frame_id": 43}, "metadata_missing"),
  ({"timestamp_eof_ns": 10_100_000_001}, "metadata_missing"),
  ({"timestamp_sof_ns": 10_050_000_001}, "timestamp_mismatch"),
  ({"pixels": bytes([127]) * 65536}, "pixel_crc_mismatch"),
  ({"width": 128, "height": 512}, "pixel_shape_mismatch")])
def test_similar_but_different_frame_never_uses_latest_metadata(change, reason):
  snapshot, active, frame, metadata = frame_data()
  matcher = contract.MapFrameMatcher("mapsd-instance-1")
  matcher.add_metadata(metadata, received_mono_time_ns=10_120_000_000)
  assert match(matcher, replace(frame, **change), snapshot, active).reason == reason


def test_wrong_renderer_instance_is_rejected_and_instance_switch_clears_queue():
  snapshot, active, frame, metadata = frame_data()
  matcher = contract.MapFrameMatcher("mapsd-instance-2")
  assert not matcher.add_metadata(metadata, received_mono_time_ns=10_120_000_000)
  assert match(matcher, frame, snapshot, active).reason == "metadata_missing"
  matcher.switch_renderer("mapsd-instance-1")
  assert matcher.add_metadata(metadata, received_mono_time_ns=10_120_000_000)
  assert match(matcher, frame, snapshot, active).accepted
  matcher.switch_renderer("mapsd-instance-2")
  assert match(matcher, frame, snapshot, active).reason == "metadata_missing"


@pytest.mark.parametrize("field,value", [("route_id", "different-route"), ("source_session_id", "source-2"),
  ("revision", 4), ("cache_sha256", "a" * 64)])
def test_new_route_context_cannot_accept_old_map(field, value):
  snapshot, active, frame, metadata = frame_data()
  matcher = contract.MapFrameMatcher("mapsd-instance-1")
  matcher.add_metadata(metadata, received_mono_time_ns=10_120_000_000)
  wrong = replace(metadata.route_binding, identity=replace(metadata.route_binding.identity, **{field: value}))
  assert not match(matcher, frame, snapshot, active, instruction=wrong).accepted


def test_missing_instruction_binding_only_rejects_map():
  snapshot, active, frame, metadata = frame_data()
  matcher = contract.MapFrameMatcher("mapsd-instance-1")
  matcher.add_metadata(metadata, received_mono_time_ns=10_120_000_000)
  result = matcher.match(frame, snapshot=snapshot, active_binding=active, instruction_binding=None, now_ns=10_200_000_000)
  assert result.reason == "instruction_binding_missing"
  assert not result.accepted


@pytest.mark.parametrize("field,value", [("binding_id", "a" * 64), ("geometry_sha256", "b" * 64),
  ("source_geometry_sha256", "c" * 64), ("transport_session_id", "transport-2"), ("source_object_id", "cache-2")])
def test_each_map_binding_component_must_match_independent_route(field, value):
  snapshot, active, frame, metadata = frame_data()
  metadata = replace(metadata, route_binding=replace(metadata.route_binding, **{field: value}))
  matcher = contract.MapFrameMatcher("mapsd-instance-1")
  matcher.add_metadata(metadata, received_mono_time_ns=10_120_000_000)
  assert match(matcher, frame, snapshot, active).reason == "map_route_mismatch"


@pytest.mark.parametrize("now,reason", [(10_000_000_000, "future_frame"), (10_600_000_000, "stale_frame"),
  (13_100_000_000, "stale_frame")])
def test_map_cannot_be_renewed_by_recent_sidecar_receipt(now, reason):
  snapshot, active, frame, metadata = frame_data()
  matcher = contract.MapFrameMatcher("mapsd-instance-1")
  matcher.add_metadata(metadata, received_mono_time_ns=10_120_000_000)
  instruction = contract.binding_from_snapshot(snapshot, active, now_ns=10_200_000_000)
  assert match(matcher, frame, snapshot, active, now_ns=now, instruction=instruction).reason == reason


def test_position_age_is_independent_of_fresh_render_time():
  snapshot, active, frame, metadata = frame_data()
  metadata = replace(metadata, location_mono_time_ns=8_000_000_000)
  matcher = contract.MapFrameMatcher("mapsd-instance-1")
  matcher.add_metadata(metadata, received_mono_time_ns=10_120_000_000)
  assert match(matcher, frame, snapshot, active).reason == "stale_position"


def test_new_sidecar_receipt_cannot_renew_stale_source_observation():
  snapshot, active, frame, metadata = frame_data()
  stale = replace(metadata.route_binding, observed_mono_time_ns=7_000_000_000)
  matcher = contract.MapFrameMatcher("mapsd-instance-1")
  matcher.add_metadata(replace(metadata, route_binding=stale), received_mono_time_ns=10_120_000_000)
  assert match(matcher, frame, snapshot, active).reason == "wire_observation_stale_or_future"


def test_wire_observation_cannot_be_newer_than_independent_active_context():
  snapshot, active, frame, metadata = frame_data()
  ahead = replace(metadata.route_binding, observed_sequence=8)
  matcher = contract.MapFrameMatcher("mapsd-instance-1")
  matcher.add_metadata(replace(metadata, route_binding=ahead), received_mono_time_ns=10_120_000_000)
  assert match(matcher, frame, snapshot, active).reason == "observation_ahead_of_active"


def test_new_active_heartbeat_accepts_same_route_frame_without_relabeling_it():
  snapshot, active, frame, metadata = frame_data()
  matcher = contract.MapFrameMatcher("mapsd-instance-1")
  matcher.add_metadata(metadata, received_mono_time_ns=10_120_000_000)
  active = replace(active, observed_sequence=8, observed_mono_time=10.15)
  result = match(matcher, frame, snapshot, active)
  assert result.accepted
  assert result.metadata.route_binding.observed_sequence == 7


def test_projection_must_use_finite_raw_values():
  with pytest.raises(contract.RouteRejected):
    contract.ProjectionEvidence(float("nan"), 0.0, 1, 3.0)
  with pytest.raises(contract.RouteRejected):
    contract.ProjectionEvidence(2.0, float("inf"), 1, 3.0)


def test_invalid_map_and_missing_projection_evidence_are_rejected():
  snapshot, active, frame, metadata = frame_data()
  for changed, reason in [(replace(metadata, valid=False, reason="off_route"), "map_invalid:off_route"),
                          (replace(metadata, projection=None), "projection_evidence_missing")]:
    matcher = contract.MapFrameMatcher("mapsd-instance-1")
    matcher.add_metadata(changed, received_mono_time_ns=10_120_000_000)
    assert match(matcher, frame, snapshot, active).reason == reason


def test_conflicting_metadata_same_frame_is_poisoned_not_last_write_wins():
  snapshot, active, frame, metadata = frame_data()
  matcher = contract.MapFrameMatcher("mapsd-instance-1")
  assert matcher.add_metadata(metadata, received_mono_time_ns=10_120_000_000)
  assert not matcher.add_metadata(replace(metadata, image_crc32=1), received_mono_time_ns=10_130_000_000)
  assert not matcher.add_metadata(metadata, received_mono_time_ns=10_140_000_000)
  assert match(matcher, frame, snapshot, active).reason == "metadata_conflict"


def test_metadata_queue_is_bounded():
  snapshot, active, frame, metadata = frame_data()
  matcher = contract.MapFrameMatcher("mapsd-instance-1", max_entries=2)
  for frame_id in (42, 43, 44):
    matcher.add_metadata(replace(metadata, frame_id=frame_id), received_mono_time_ns=10_120_000_000)
  assert matcher.pending_count == 2
  assert match(matcher, frame, snapshot, active).reason == "metadata_missing"


def test_local_navd_context_uses_real_message_metadata_without_rebuilding_provenance():
  sm = local_sm()
  route = contract.LocalNavdRouteContext.from_submaster(sm, now_ns=10_200_000_000)
  instruction = contract.LocalNavdInstructionContext.from_submaster(sm, now_ns=10_200_000_000)
  assert route.produced_mono_time_ns == sm.logMonoTime["navRoute"]
  assert route.received_mono_time_ns == 10_110_000_000
  assert len(route.coordinates) == 3
  assert isinstance(route.binding, contract.WireRouteBinding)
  _, _, frame, metadata = frame_data()
  matcher = contract.MapFrameMatcher("mapsd-instance-1")
  matcher.add_metadata(metadata, received_mono_time_ns=10_120_000_000)
  assert matcher.match_local(frame, route_context=route, instruction_context=instruction, now_ns=10_200_000_000).accepted


def test_mapsd_can_build_metadata_from_local_context_without_raw_decoder_objects():
  sm = local_sm()
  route = contract.LocalNavdRouteContext.from_submaster(sm, now_ns=10_200_000_000)
  instruction = contract.LocalNavdInstructionContext.from_submaster(sm, now_ns=10_200_000_000)
  _, _, frame, _ = frame_data()
  frame = replace(frame, timestamp_eof_ns=10_200_000_000)
  metadata = contract.make_local_map_frame(route, instruction, frame, renderer_instance_id="mapsd-instance-1",
    location_mono_time_ns=frame.timestamp_sof_ns, projection=contract.ProjectionEvidence(2.0, 0.0, 1, 30.0))
  assert metadata.route_binding == instruction.binding
  assert metadata.timestamp_eof_ns == frame.timestamp_eof_ns
  assert metadata.image_crc32 != 0


def test_unchanged_submaster_route_does_not_refresh_cached_receive_time():
  sm = local_sm()
  sm.updated["navRoute"] = False
  with pytest.raises(contract.RouteRejected, match="navRoute_not_updated"):
    contract.LocalNavdRouteContext.from_submaster(sm, now_ns=10_200_000_000)


@pytest.mark.parametrize("service", ["navRoute", "navInstruction"])
@pytest.mark.parametrize("defect", ["invalid", "future", "stale", "future_receipt", "stale_receipt"])
def test_local_context_rejects_invalid_or_stale_actual_submaster_state(service, defect):
  sm = local_sm()
  if defect == "invalid":
    sm.valid[service] = False
  elif defect == "future":
    sm.logMonoTime[service] = 11_000_000_000
  elif defect == "stale":
    sm.logMonoTime[service] = 1
  elif defect == "future_receipt":
    sm.recv_time[service] = 11.0
  else:
    sm.recv_time[service] = 0.0
  factory = contract.LocalNavdRouteContext if service == "navRoute" else contract.LocalNavdInstructionContext
  with pytest.raises(contract.RouteRejected):
    factory.from_submaster(sm, now_ns=40_200_000_000 if defect == "stale" and service == "navRoute" else 10_200_000_000)


def test_local_route_requires_double_geometry_and_matching_hash():
  sm = local_sm()
  sm["navRoute"].coordinates64[0].longitude += 0.0001
  with pytest.raises(contract.RouteRejected, match="local_geometry_hash_mismatch"):
    contract.LocalNavdRouteContext.from_submaster(sm, now_ns=10_200_000_000)
  sm = local_sm()
  sm["navRoute"].coordinates64 = []
  with pytest.raises(contract.RouteRejected):
    contract.LocalNavdRouteContext.from_submaster(sm, now_ns=10_200_000_000)


def test_cached_route_identity_and_new_instruction_observation_have_separate_lifetimes():
  sm = local_sm()
  route = contract.LocalNavdRouteContext.from_submaster(sm, now_ns=10_200_000_000)
  sm["navInstruction"].routeBinding.observedSequence = 8
  sm["navInstruction"].routeBinding.observedMonoTime = 11_000_000_000
  sm.logMonoTime["navInstruction"] = 11_100_000_000
  sm.recv_time["navInstruction"] = 11.11
  instruction = contract.LocalNavdInstructionContext.from_submaster(sm, now_ns=11_200_000_000)
  _, _, frame, metadata = frame_data()
  frame = replace(frame, timestamp_sof_ns=11_050_000_000, timestamp_eof_ns=11_100_000_000)
  metadata = replace(metadata, timestamp_sof_ns=frame.timestamp_sof_ns, timestamp_eof_ns=frame.timestamp_eof_ns,
    location_mono_time_ns=11_050_000_000, route_binding=instruction.binding)
  matcher = contract.MapFrameMatcher("mapsd-instance-1")
  matcher.add_metadata(metadata, received_mono_time_ns=11_120_000_000)
  assert matcher.match_local(frame, route_context=route, instruction_context=instruction, now_ns=11_200_000_000).accepted


def test_local_current_instruction_cannot_select_different_route_from_navd_anchor():
  sm = local_sm()
  route = contract.LocalNavdRouteContext.from_submaster(sm, now_ns=10_200_000_000)
  sm["navInstruction"].routeBinding.routeId = "other-route"
  instruction = contract.LocalNavdInstructionContext.from_submaster(sm, now_ns=10_200_000_000)
  _, _, frame, metadata = frame_data()
  matcher = contract.MapFrameMatcher("mapsd-instance-1")
  matcher.add_metadata(metadata, received_mono_time_ns=10_120_000_000)
  result = matcher.match_local(frame, route_context=route, instruction_context=instruction, now_ns=10_200_000_000)
  assert result.reason == "instruction_route_mismatch"


def test_local_context_rejects_missing_route_even_with_self_consistent_frame_and_instruction():
  sm = local_sm()
  instruction = contract.LocalNavdInstructionContext.from_submaster(sm, now_ns=10_200_000_000)
  _, _, frame, metadata = frame_data()
  matcher = contract.MapFrameMatcher("mapsd-instance-1")
  matcher.add_metadata(metadata, received_mono_time_ns=10_120_000_000)
  assert matcher.match_local(frame, route_context=None, instruction_context=instruction,
                              now_ns=10_200_000_000).reason == "local_route_missing"


def test_visionipc_link_binds_instance_only_after_full_frame_validation():
  snapshot, active, frame, metadata = frame_data()
  matcher = contract.MapFrameMatcher.for_link("local-connection-1", connected_mono_time_ns=10_040_000_000)
  matcher.add_metadata(metadata, received_mono_time_ns=10_120_000_000)
  assert matcher.renderer_instance_id is None
  assert not match(matcher, replace(frame, pixels=bytes([127]) * 65536), snapshot, active).accepted
  assert matcher.renderer_instance_id is None
  assert match(matcher, frame, snapshot, active).accepted
  assert matcher.renderer_instance_id == "mapsd-instance-1"


def test_visionipc_link_requires_reconnect_for_new_verified_renderer_instance():
  snapshot, active, frame, metadata = frame_data()
  matcher = contract.MapFrameMatcher.for_link("local-connection-1", connected_mono_time_ns=10_040_000_000)
  matcher.add_metadata(metadata, received_mono_time_ns=10_120_000_000)
  assert match(matcher, frame, snapshot, active).accepted
  new_frame = replace(frame, frame_id=0, timestamp_sof_ns=10_150_000_000, timestamp_eof_ns=10_160_000_000)
  new_metadata = replace(metadata, renderer_instance_id="mapsd-instance-2", frame_id=0,
    timestamp_sof_ns=new_frame.timestamp_sof_ns, timestamp_eof_ns=new_frame.timestamp_eof_ns, location_mono_time_ns=10_150_000_000)
  matcher.add_metadata(new_metadata, received_mono_time_ns=10_170_000_000)
  assert match(matcher, new_frame, snapshot, active).reason == "renderer_instance_changed"
  assert matcher.requires_reconnect
  assert matcher.renderer_instance_id == "mapsd-instance-1"


def test_frame_queued_before_visionipc_connection_cannot_bind_instance():
  snapshot, active, frame, metadata = frame_data()
  matcher = contract.MapFrameMatcher.for_link("local-connection-2", connected_mono_time_ns=10_150_000_000)
  matcher.add_metadata(metadata, received_mono_time_ns=10_160_000_000)
  assert match(matcher, frame, snapshot, active).reason == "frame_before_connection"
  assert matcher.renderer_instance_id is None


def test_two_renderer_claims_for_same_exact_frame_are_ambiguous():
  snapshot, active, frame, metadata = frame_data()
  matcher = contract.MapFrameMatcher.for_link("local-connection-1", connected_mono_time_ns=10_040_000_000)
  matcher.add_metadata(metadata, received_mono_time_ns=10_120_000_000)
  matcher.add_metadata(replace(metadata, renderer_instance_id="mapsd-instance-2"), received_mono_time_ns=10_120_000_000)
  assert match(matcher, frame, snapshot, active).reason == "renderer_frame_ambiguous"
  assert matcher.renderer_instance_id is None


LEGACY_SCHEMA = """
@0x9f0448c9164779ab;
struct NavRoute {
  coordinates @0 :List(Coordinate);
  struct Coordinate { latitude @0 :Float32; longitude @1 :Float32; }
}
struct NavInstruction {
  maneuverPrimaryText @0 :Text;
  maneuverSecondaryText @1 :Text;
  maneuverDistance @2 :Float32;
  maneuverType @3 :Text;
  maneuverModifier @4 :Text;
  distanceRemaining @5 :Float32;
  timeRemaining @6 :Float32;
  timeRemainingTypical @7 :Float32;
  lanes @8 :List(Lane);
  showFull @9 :Bool;
  speedLimit @10 :Float32;
  speedLimitSign @11 :SpeedLimitSign;
  allManeuvers @12 :List(Maneuver);
  struct Lane { directions @0 :List(Direction); active @1 :Bool; activeDirection @2 :Direction; }
  enum Direction { none @0; left @1; right @2; straight @3; slightLeft @4; slightRight @5; }
  enum SpeedLimitSign { mutcd @0; vienna @1; }
  struct Maneuver { distance @0 :Float32; type @1 :Text; modifier @2 :Text; }
}
"""


@pytest.fixture
def legacy_schema(tmp_path):
  path = tmp_path / "legacy_nav.capnp"
  # The pre-extension evidence uses exactly the existing ordinals 0..66. Keep its
  # actual field types/defaults so this exercises old wire data, not only new defaults.
  source = Path(log.__file__).with_name("custom.capnp").read_text()
  evidence = re.search(r"struct NavModelEvidence \{(.*?)\n\}", source, re.S).group(1)
  legacy_evidence = "\n".join(line for line in evidence.splitlines()
                              if not (ordinal := re.search(r"@(\d+)\s*:", line)) or int(ordinal[1]) <= 66)
  path.write_text(LEGACY_SCHEMA + "\nstruct NavModelEvidence {" + legacy_evidence + "\n}\n")
  parser = capnp.SchemaParser()
  return parser.load(str(path))


def test_legacy_route_decodes_with_unbound_defaults(legacy_schema):
  old = legacy_schema.NavRoute.new_message(coordinates=[{"latitude": 31.03, "longitude": 106.41}])
  with log.NavRoute.from_bytes(old.to_bytes()) as new:
    assert len(new.coordinates) == 1
    assert len(new.coordinates64) == 0
    assert new.routeBinding.version == 0
    assert new.phonePose.version == 0
    assert new.phonePose.positionMonoTime == 0
    with pytest.raises(contract.RouteRejected, match="legacy_binding"):
      contract.WireRouteBinding.from_dict(new.routeBinding.to_dict())


def test_new_route_remains_readable_by_old_schema(legacy_schema):
  snapshot, active = route_data()
  fields = contract.route_fields(snapshot, active, now_ns=10_100_000_000)
  fields["coordinates"] = [{"latitude": 31.03, "longitude": 106.41}]
  new = log.NavRoute.new_message(**fields)
  with legacy_schema.NavRoute.from_bytes(new.to_bytes()) as old:
    assert len(old.coordinates) == 1
    assert old.coordinates[0].longitude == pytest.approx(106.41, abs=1e-5)


def test_old_and_new_instruction_schemas_keep_maneuvers_independent(legacy_schema):
  maneuvers = [{"distance": 12.5, "type": "turn", "modifier": "left"}]
  old = legacy_schema.NavInstruction.new_message(allManeuvers=maneuvers)
  with log.NavInstruction.from_bytes(old.to_bytes()) as new:
    assert new.allManeuvers[0].modifier == "left"
    assert new.routeBinding.version == 0
  snapshot, active = route_data()
  new = log.NavInstruction.new_message(allManeuvers=maneuvers,
    routeBinding=contract.binding_from_snapshot(snapshot, active, now_ns=10_100_000_000).to_dict())
  with legacy_schema.NavInstruction.from_bytes(new.to_bytes()) as old:
    assert old.allManeuvers[0].distance == 12.5
    assert old.allManeuvers[0].modifier == "left"


def test_legacy_evidence_gets_unknown_map_binding_without_changing_vector(legacy_schema):
  vector = [0.0] * 47
  vector[15], vector[-1] = 1.0, 0.3
  old = legacy_schema.NavModelEvidence.new_message(version=1, frameId=123, navigation=vector, rpcNs=456)
  with custom.NavModelEvidence.from_bytes(old.to_bytes()) as evidence:
    assert evidence.mapBinding.version == 0
    assert evidence.mapRendererInstanceId == ""
    assert evidence.mapCorrelationReason == ""
    assert evidence.mapPixelCrc32 == 0
    assert evidence.phonePositionQuality == ""
    assert evidence.phoneGpsAccuracyM == 0
    assert evidence.phoneNativePoseErrorM == 0
    assert evidence.controlInputAllowed is False
    assert len(evidence.navigation) == 47
    assert evidence.navigation[15] == 1.0
    assert evidence.rpcNs == 456


def test_new_map_evidence_remains_readable_by_old_schema(legacy_schema):
  snapshot, active = route_data()
  binding = contract.binding_from_snapshot(snapshot, active, now_ns=10_100_000_000).to_dict()
  current = custom.NavModelEvidence.new_message(version=1, frameId=123, navigation=[0.0] * 47,
    mapBinding=binding, mapRendererInstanceId="mapsd-instance-1", mapCorrelationReason="accepted", mapPixelCrc32=1234)
  with legacy_schema.NavModelEvidence.from_bytes(current.to_bytes()) as old:
    assert old.frameId == 123
    assert len(old.navigation) == 47


def test_phone_quality_evidence_round_trips_and_old_reader_keeps_core_fields(legacy_schema):
  current = custom.NavModelEvidence.new_message(
    version=1, frameId=124, navigation=[0.0] * 47,
    phonePositionQuality="accepted_degraded_map", phoneGpsAccuracyM=15,
    phoneNativePoseErrorM=20, controlInputAllowed=False)
  with custom.NavModelEvidence.from_bytes(current.to_bytes()) as decoded:
    assert decoded.phonePositionQuality == "accepted_degraded_map"
    assert decoded.phoneGpsAccuracyM == 15
    assert decoded.phoneNativePoseErrorM == 20
    assert decoded.controlInputAllowed is False
  with legacy_schema.NavModelEvidence.from_bytes(current.to_bytes()) as old:
    assert old.frameId == 124 and len(old.navigation) == 47
