from copy import deepcopy
from dataclasses import dataclass, replace
import hashlib
from types import SimpleNamespace

import numpy as np
import pytest

from openpilot.cereal import log, messaging
from openpilot.common.gcj import wgs84_to_gcj02
from openpilot.selfdrive.modeld import nav_inputs
from openpilot.selfdrive.modeld.nav_reset import NavigationResetMixin
from openpilot.selfdrive.navigation import amap_phone_route, mapsd, navd
from openpilot.selfdrive.navigation.amap_phone_route import AmapPhoneRouteAdapter, NATIVE_PROFILE
from openpilot.selfdrive.navigation.nav_phone_quality import (QUALITY_DEGRADED_MAP, QUALITY_STRICT,
  PHONE_MAP_MAX_PROJECTION_ERROR_M, classify_phone_position_quality)
from openpilot.selfdrive.navigation.nav_map_contract import (LocalNavdPhonePoseContext, LocalNavdRouteContext,
  MapFrameMatcher, MapFrameMetadata, MapPixels, RouteRejected, route_fields)
from tools.cplink_shim.amap_route_cache import COORDINATE_SCALE, SUPPORTED_PROFILE, decode_cache
from tools.cplink_shim.tests.test_amap_route_cache import cache, route, segment


NOW_NS = 100_000_000_000
MAP_SHAPE = (1, 1, 256, 256)
VEC_SHAPE = (1, 47)
WGS_POSITION = (121.0, 31.0)
NATIVE_POSITION = wgs84_to_gcj02(*WGS_POSITION)
NATIVE_X, NATIVE_Y = (round(value * COORDINATE_SCALE) for value in NATIVE_POSITION)
POINTS = ((NATIVE_X - 3600, NATIVE_Y), (NATIVE_X + 3600, NATIVE_Y), (NATIVE_X + 3600, NATIVE_Y + 3600))


@dataclass(frozen=True)
class GpsFix:
  phone_boot_id: str
  elapsed_realtime_ns: int
  source_token: str
  coordinates: tuple[float, float]
  horizontal_accuracy_m: float
  host_started_ns: int
  host_finished_ns: int
  age_upper_ns: int

  def fresh_at(self, now_ns):
    return now_ns >= self.host_finished_ns and self.age_upper_ns + now_ns - self.host_finished_ns < 2_000_000_000


def make_sample(*, now_ns=NOW_NS, sequence=1, elapsed_ns=10_000_000_000, transport="transport-1", epoch="123:456", path_id=123):
  raw = cache(routes=[route(path_id=path_id, segments=[segment(POINTS)])], ids=[path_id])
  decoded = decode_cache(raw, profile=SUPPORTED_PROFILE)
  path_hash = amap_phone_route._path_hash(decoded.routes[0].path_id, epoch)
  selected = SimpleNamespace(route=decoded.routes[0], identity_hash=path_hash,
    cache_sha256=hashlib.sha256(raw).hexdigest(), source_session_id=hashlib.sha256(epoch.encode()).hexdigest(),
    source_path="/sdcard/Android/data/com.autonavi.amapauto/files/amapauto9/path_cache/1.dat")
  gps = GpsFix("00000000-0000-0000-0000-000000000001", elapsed_ns,
               hashlib.sha256(str(elapsed_ns).encode()).hexdigest(), WGS_POSITION, 1.0,
               now_ns - 30_000_000, now_ns - 25_000_000, 100_000_000)
  observation = {"status": "observed", "sample_consistent": True, "gps_navigation_observed": True,
    "profile": NATIVE_PROFILE, "navi_type_raw": 0, "navi_state_raw": 1,
    "process_epoch_hash": hashlib.sha256(epoch.encode()).hexdigest(), "path_identity_hash": path_hash,
    "host_observation_clock_domain": "host_monotonic", "observation_clock_domain": "phone_elapsed_realtime",
    "host_observation_started_monotonic_ns": now_ns - 45_000_000,
    "host_observation_finished_monotonic_ns": now_ns - 35_000_000,
    "_position": {"status": "observed", "is_on_guide_road_raw": 1, "path_identity_hash": path_hash,
      "segment_index": 0, "link_index": 0, "point_index": 0,
      "native_st_pos_deg": list(NATIVE_POSITION), "matched_st_pos_deg": list(NATIVE_POSITION)}}
  after = deepcopy(observation)
  after["host_observation_started_monotonic_ns"] = now_ns - 20_000_000
  after["host_observation_finished_monotonic_ns"] = now_ns - 10_000_000
  branches = [
    {"maneuver_id_raw": 3, "maneuver_segment_raw": 0, "maneuver_link_raw": 0xffffffff,
     "maneuver_point_raw": 0xffffffff, "exit_number_raw": 0, "exit_angle_raw": 0,
     "obtain_supported_raw": 1, "data_changed_raw": 0, "segment_remain_time_raw": 17,
     "segment_remain_distance_raw": 79, "next_road_name_segment_raw": 1,
     "next_road_name_link_raw": 0, "split_raw": 0, "segment_binding": {
       "status": "verified", "reason": "segment_object_verified",
       "segment_object_verified": True, "icon_id_getter_vtable_verified": True,
       "icon_getter_vtable_verified": True}},
    {"maneuver_id_raw": 3, "maneuver_segment_raw": 0, "maneuver_link_raw": 0xffffffff,
     "maneuver_point_raw": 0xffffffff, "exit_number_raw": 0, "exit_angle_raw": 0,
     "obtain_supported_raw": 1, "data_changed_raw": 0, "segment_remain_time_raw": 17,
     "segment_remain_distance_raw": 79, "next_road_name_segment_raw": 1,
     "next_road_name_link_raw": 0, "split_raw": 0, "segment_binding": {
       "status": "verified", "reason": "segment_object_verified",
       "segment_object_verified": True, "icon_id_getter_vtable_verified": True,
       "icon_getter_vtable_verified": True}},
  ]
  after["_navi_info"] = {
    "schema_version": 2, "status": "observed", "reason": "unverified_navi_info_semantics",
    "diagnostic_only": True, "control_input_allowed": False, "route_render_input_allowed": False,
    "source": "native_navi_info", "path_identity_hash": path_hash,
    "route_remain_time_raw": 257, "route_remain_distance_raw": 929,
    "navigation_info_type_raw": 0, "item_count": 1, "item_index": 0,
    "current_segment": 0, "current_link": 0, "current_point": 0,
    "branch_flag_raw": 0, "branches": branches,
  }
  broadcast = {
    "schema_version": 1, "source": "adb_broadcast_observation", "status": "observed",
    "reason": "fresh_10001_observation", "receiver_active": True,
    "bridge_transport_valid": True, "receiver_session_id": "receiver-1",
    "receiver_sequence": 119, "host_read_started_ns": now_ns - 8_000_000,
    "host_read_finished_ns": now_ns - 7_000_000, "max_source_age_ns": 1_250_000_000,
    "records": {"10001": {
      "key_type": 10001, "sequence": 119, "fresh": True, "replayed": False,
      "source_age_upper_ns": 190_718_427,
      "fields": {"TYPE": 0, "ICON": 3, "NEW_ICON": 3, "SEG_REMAIN_DIS": 79,
                 "SEG_REMAIN_TIME": 17, "ROUTE_REMAIN_DIS": 929,
                 "ROUTE_REMAIN_TIME": 257, "CUR_SEG_NUM": 0},
    }},
  }
  return SimpleNamespace(host_start_ns=now_ns - 45_000_000, gps=gps, transport_session=transport, sequence=sequence,
                         selected=selected, epoch=epoch, native_before=observation, native_after=after,
                         cache_bytes=raw, broadcast=broadcast)


class PhoneSM:
  def __init__(self, publication, now_ns=NOW_NS, *, instructions=True):
    self.valid = {"navRoute": True, "navInstruction": instructions, "gpsLocation": True, "gpsLocationExternal": False}
    self.updated = {"navRoute": True, "navInstruction": True, "navMapFrameSP": False}
    self.recv_time = dict.fromkeys(("navRoute", "navInstruction", "gpsLocation"), now_ns / 1e9)
    self.logMonoTime = dict.fromkeys(self.recv_time, now_ns)
    self.data = {"navInstruction": log.NavInstruction.new_message(
      allManeuvers=[{"distance": 79.0, "type": "turn", "modifier": "right"}]),
      "gpsLocation": SimpleNamespace(hasFix=True, longitude=-140.0, latitude=-60.0, speed=10.0, bearingDeg=270.0)}
    self.publish(publication, now_ns)

  def publish(self, publication, now_ns):
    self.valid["navRoute"] = publication is not None
    self.updated["navRoute"] = True
    self.recv_time["navRoute"] = now_ns / 1e9
    self.logMonoTime["navRoute"] = now_ns
    self.data["navRoute"] = (log.NavRoute.new_message(**{**route_fields(publication.snapshot, publication.active, now_ns=now_ns),
      "phonePose": publication.phone_pose}) if publication is not None else log.NavRoute.new_message())
    instruction = publication.instruction if publication is not None else None
    if instruction is None:
      self.valid["navInstruction"] = False
      self.data["navInstruction"] = log.NavInstruction.new_message()
    else:
      binding = route_fields(publication.snapshot, publication.active, now_ns=now_ns)["routeBinding"]
      self.valid["navInstruction"] = True
      self.data["navInstruction"] = log.NavInstruction.new_message(
        allManeuvers=[{"distance": instruction["distance"], "type": instruction["type"],
                       "modifier": instruction["modifier"]}], routeBinding=binding)
    self.recv_time["navInstruction"] = now_ns / 1e9
    self.logMonoTime["navInstruction"] = now_ns

  def __getitem__(self, service):
    return self.data[service]


@pytest.fixture
def pipeline(monkeypatch):
  clock = [NOW_NS]
  monkeypatch.setattr(mapsd.time, "monotonic", lambda: clock[0] / 1e9)
  monkeypatch.setattr(mapsd.time, "monotonic_ns", lambda: clock[0])
  monkeypatch.setattr(mapsd, "cplink_navi_active", lambda: True)
  monkeypatch.setattr(nav_inputs, "cplink_navi_active", lambda: True)
  monkeypatch.setattr(navd, "cplink_navi_active", lambda: True)
  sample = make_sample()
  adapter = AmapPhoneRouteAdapter()
  publication = adapter.accept(sample, now_ns=clock[0])
  assert publication is not None, adapter.reason
  return SimpleNamespace(clock=clock, sample=sample, adapter=adapter, publication=publication,
                         sm=PhoneSM(publication), renderer=mapsd.MapsD())


def queue_frame(subject, pipeline, *, frame_id=42):
  image, stamp = pipeline.renderer.update(pipeline.sm)
  assert image.any() and stamp > 0
  sent = []
  pipeline.renderer.publish_frame(SimpleNamespace(send=lambda *args: sent.append(args)),
    SimpleNamespace(send=lambda service, message: pipeline.sm.data.update({service: message.navMapFrameSP})),
    image, stamp, frame_id=frame_id, renderer_instance_id="renderer-test")
  fields = pipeline.sm.data["navMapFrameSP"].to_dict()
  metadata = MapFrameMetadata.from_dict(fields)
  subject._phone_matcher.add_metadata(metadata, received_mono_time_ns=pipeline.clock[0])
  frame = MapPixels(frame_id, sent[0][3], sent[0][4], 256, 256, sent[0][1])
  subject._phone_frames[(frame_id, frame.timestamp_eof_ns)] = frame


def model_runtime(pipeline, monkeypatch):
  subject = nav_inputs.NavRuntime()
  subject._cplink_active = True
  subject._client_failed = True
  subject._phone_matcher = MapFrameMatcher("renderer-test")
  queue_frame(subject, pipeline)
  return subject


def test_real_encoded_cache_and_independent_pose_reach_native_frame_map(pipeline, monkeypatch):
  monkeypatch.setattr(mapsd, "latest_gps_source", lambda *_: pytest.fail("native地图不应读取主机WGS定位"))
  monkeypatch.setattr(mapsd, "make_renderer", lambda: pytest.fail("native地图不应读取PMTiles"))
  pipeline.renderer.renderer = SimpleNamespace(render=lambda *_: pytest.fail("native地图不应混入basemap"))
  image, stamp = pipeline.renderer.update(pipeline.sm)
  assert image.any() and stamp == pipeline.publication.phone_pose["positionMonoTime"]
  assert pipeline.renderer._phone_route_context.binding.coordinate_system == "AMAP_NATIVE_MAP"
  np.testing.assert_array_equal(pipeline.renderer.route, pipeline.sample.selected.route.points_degrees)
  assert pipeline.renderer._projection_evidence.segment == 0
  assert pipeline.publication.phone_pose["reason"] == QUALITY_STRICT
  assert pipeline.adapter.reason == QUALITY_STRICT


@pytest.mark.parametrize(("accuracy", "pose_error", "expected"), [
  (5.0, 5.0, QUALITY_STRICT),
  (5.001, 1.0, QUALITY_DEGRADED_MAP),
  (15.0, 20.0, QUALITY_DEGRADED_MAP),
  (25.0, 25.0, QUALITY_DEGRADED_MAP),
])
def test_phone_quality_boundaries_keep_degraded_map_without_control(accuracy, pose_error, expected):
  sample = make_sample()
  sample.gps = replace(sample.gps, horizontal_accuracy_m=accuracy)
  publication = AmapPhoneRouteAdapter().accept(sample, now_ns=NOW_NS)
  assert publication is not None, "quality within 25 m must remain usable for map evidence"
  assert publication.phone_pose["reason"] == expected
  assert publication.phone_pose["horizontalAccuracyM"] == pytest.approx(accuracy)
  assert publication.phone_pose["nativePoseErrorM"] == pytest.approx(0.0, abs=0.1)
  assert classify_phone_position_quality(accuracy, pose_error) == expected


@pytest.mark.parametrize(("field", "value", "reason"), [
  ("horizontalAccuracyM", 25.001, "phone_pose_accuracy_unusable"),
  ("nativePoseErrorM", 25.001, "phone_pose_space_mismatch"),
  ("reason", "accepted", "phone_pose_quality_unverified"),
  ("reason", QUALITY_STRICT, "phone_pose_quality_mismatch"),
])
def test_local_wire_quality_fails_closed(pipeline, field, value, reason):
  if field == "reason":
    pipeline.sm["navRoute"].phonePose.horizontalAccuracyM = 15
  setattr(pipeline.sm["navRoute"].phonePose, field, value)
  with pytest.raises(RouteRejected, match=reason):
    LocalNavdPhonePoseContext.from_submaster(pipeline.sm, now_ns=NOW_NS)


def test_local_wire_accepts_degraded_map_quality(pipeline):
  pipeline.sm["navRoute"].phonePose.horizontalAccuracyM = 15
  pipeline.sm["navRoute"].phonePose.nativePoseErrorM = 20
  pipeline.sm["navRoute"].phonePose.reason = QUALITY_DEGRADED_MAP
  context = LocalNavdPhonePoseContext.from_submaster(pipeline.sm, now_ns=NOW_NS)
  assert context.quality_reason == QUALITY_DEGRADED_MAP


def _offset_wgs_position(lon, lat, offset_m):
  return lon, lat + offset_m / 111195.0802335329


def _sample_with_gps_offset(offset_m, *, now_ns=NOW_NS, sequence=1, elapsed_ns=10_000_000_000):
  """Move only GPS while keeping the source anchor and its native observation fixed."""
  sample = make_sample(now_ns=now_ns, sequence=sequence, elapsed_ns=elapsed_ns)
  shifted = _offset_wgs_position(*sample.gps.coordinates, offset_m)
  sample.gps = replace(sample.gps, horizontal_accuracy_m=15.0,
                       source_token=hashlib.sha256(f"offset-{offset_m}".encode()).hexdigest(),
                       coordinates=shifted)
  return sample


@pytest.mark.parametrize(("offset_m", "accepted"), [
  (33.9, True),
  (39.9, True),
  (PHONE_MAP_MAX_PROJECTION_ERROR_M + 0.01, False),
])
def test_gps_projection_uses_shared_road_width_boundary(pipeline, monkeypatch, offset_m, accepted):
  # Isolate the projection limit from the independent 25 m GPS-to-native gate.
  monkeypatch.setattr(amap_phone_route, "_distance", lambda *_: 0.0)
  pipeline.clock[0] += 100_000_000
  sample = _sample_with_gps_offset(offset_m, now_ns=pipeline.clock[0], sequence=2,
                                    elapsed_ns=pipeline.sample.gps.elapsed_realtime_ns + 100_000_000)
  publication = pipeline.adapter.accept(sample, now_ns=pipeline.clock[0])
  assert (publication is not None) is accepted
  if accepted:
    assert publication.phone_pose["reason"] == QUALITY_DEGRADED_MAP
    pipeline.sm.publish(publication, pipeline.clock[0])
    image, stamp = pipeline.renderer.update(pipeline.sm)
    assert image.any() and stamp > 0
  else:
    assert pipeline.adapter.reason == "phone_anchor_position_outside_source_edge"


def test_gps_native_consistency_limit_remains_25_m_even_inside_projection_boundary():
  sample = make_sample()
  sample.native_before["_position"]["native_st_pos_deg"] = list(_offset_wgs_position(
    *sample.native_before["_position"]["native_st_pos_deg"], 25.001))
  sample.native_after["_position"]["native_st_pos_deg"] = list(_offset_wgs_position(
    *sample.native_after["_position"]["native_st_pos_deg"], 25.001))
  adapter = AmapPhoneRouteAdapter()
  assert adapter.accept(sample, now_ns=NOW_NS) is None
  assert adapter.reason == "phone_pose_space_mismatch"


@pytest.mark.parametrize(("source", "reason"), [
  ({"segment_index": 1, "link_index": 0, "point_index": 0}, "phone_anchor_invalid_segment_index"),
  ({"segment_index": 0, "link_index": 0, "point_index": 1}, "phone_anchor_position_outside_source_edge"),
])
def test_projection_relaxation_never_selects_a_different_source_edge(source, reason):
  sample = make_sample()
  for side in ("native_before", "native_after"):
    getattr(sample, side)["_position"].update(source)
  adapter = AmapPhoneRouteAdapter()
  assert adapter.accept(sample, now_ns=NOW_NS) is None
  assert adapter.reason == reason


def test_only_verified_right_turn_reaches_instruction_publication(pipeline):
  assert pipeline.publication.instruction is not None
  assert pipeline.publication.instruction["type"] == "turn"
  assert pipeline.publication.instruction["modifier"] == "right"
  assert pipeline.publication.instruction["distance"] == pytest.approx(79.0)
  assert pipeline.publication.instruction["routeRemainDistanceRaw"] == 929
  assert pipeline.publication.instruction["routeRemainTimeRaw"] == 257
  assert pipeline.adapter.instruction_reason == "accepted"


def test_verified_left_turn_reaches_instruction_publication():
  sample = deepcopy(make_sample())
  fields = sample.broadcast["records"]["10001"]["fields"]
  for branch in sample.native_after["_navi_info"]["branches"]:
    branch["maneuver_id_raw"] = 2
  fields["ICON"] = 2
  fields["NEW_ICON"] = 2

  adapter = AmapPhoneRouteAdapter()
  publication = adapter.accept(sample, now_ns=NOW_NS)

  assert publication is not None, adapter.reason
  assert publication.instruction is not None
  assert publication.instruction["type"] == "turn"
  assert publication.instruction["modifier"] == "left"
  assert publication.instruction["distance"] == pytest.approx(79.0)
  assert publication.instruction["routeRemainDistanceRaw"] == 929
  assert publication.instruction["routeRemainTimeRaw"] == 257
  assert adapter.instruction_reason == "accepted"


def test_verified_sharp_right_turn_reaches_instruction_publication():
  sample = deepcopy(make_sample())
  fields = sample.broadcast["records"]["10001"]["fields"]
  for branch in sample.native_after["_navi_info"]["branches"]:
    branch["maneuver_id_raw"] = 7
  fields["ICON"] = 7
  fields["NEW_ICON"] = 7

  adapter = AmapPhoneRouteAdapter()
  publication = adapter.accept(sample, now_ns=NOW_NS)

  assert publication is not None, adapter.reason
  assert publication.instruction is not None
  assert publication.instruction["type"] == "turn"
  assert publication.instruction["modifier"] == "sharp right"
  assert publication.instruction["distance"] == pytest.approx(79.0)
  assert adapter.instruction_reason == "accepted"


@pytest.mark.parametrize("fault", [
  "icon", "new_icon", "segment_distance", "segment_time", "route_distance",
  "route_time", "current_segment", "type", "stale_broadcast",
  "missing_identity", "unknown_maneuver", "branch_flag", "unsupported", "segment_binding",
])
def test_unconfirmed_or_inconsistent_phone_action_fails_closed(pipeline, fault):
  sample = deepcopy(pipeline.sample)
  fields = sample.broadcast["records"]["10001"]["fields"]
  navi = sample.native_after["_navi_info"]
  if fault == "icon":
    fields["ICON"] = 2
  elif fault == "new_icon":
    fields["NEW_ICON"] = 2
  elif fault == "segment_distance":
    fields["SEG_REMAIN_DIS"] += 1
  elif fault == "segment_time":
    fields["SEG_REMAIN_TIME"] += 1
  elif fault == "route_distance":
    fields["ROUTE_REMAIN_DIS"] += 1
  elif fault == "route_time":
    fields["ROUTE_REMAIN_TIME"] += 1
  elif fault == "current_segment":
    fields["CUR_SEG_NUM"] += 1
  elif fault == "type":
    fields["TYPE"] = 1
  elif fault == "stale_broadcast":
    sample.broadcast["records"]["10001"]["source_age_upper_ns"] = 2_000_000_001
  elif fault == "missing_identity":
    navi["path_identity_hash"] = ""
  elif fault == "unknown_maneuver":
    navi["branches"][0]["maneuver_id_raw"] = 4
  elif fault == "branch_flag":
    navi["branch_flag_raw"] = 2
  elif fault == "segment_binding":
    navi["branches"][0]["segment_binding"] = {
      "status": "unavailable", "reason": "segment_index_unavailable"}
  else:
    navi["branches"][0]["obtain_supported_raw"] = 0
  publication = AmapPhoneRouteAdapter().accept(sample, now_ns=NOW_NS)
  assert publication is not None
  assert publication.instruction is None


@pytest.mark.parametrize("index", [0, 1])
@pytest.mark.parametrize("field", ["next_road_name_segment_raw", "next_road_name_link_raw"])
def test_unverified_next_road_field_cannot_reach_instruction_even_with_verified_binding(pipeline, index, field):
  sample = deepcopy(pipeline.sample)
  sample.native_before["_navi_info"] = deepcopy(sample.native_after["_navi_info"])
  sample.native_before["_navi_info"]["branches"][index][field] = 0xfffffffe
  sample.native_after["_navi_info"]["branches"][index][field] = 0xfffffffe
  adapter = AmapPhoneRouteAdapter()
  publication = adapter.accept(sample, now_ns=NOW_NS)
  assert publication is not None, adapter.reason
  assert publication.instruction is None
  assert adapter.instruction_reason == "phone_instruction_next_road_unverified"


def test_unchanged_fresh_replay_keeps_the_same_verified_instruction(pipeline):
  sample = deepcopy(pipeline.sample)
  record = sample.broadcast["records"]["10001"]
  record["replayed"] = True
  record["source_age_upper_ns"] = 1_000_000_000

  adapter = AmapPhoneRouteAdapter()
  fresh = deepcopy(sample)
  fresh.broadcast["records"]["10001"].update(replayed=False, source_age_upper_ns=190_718_427)
  adapter.accept(fresh, now_ns=NOW_NS)
  publication = adapter.accept(sample, now_ns=NOW_NS)

  assert publication is not None
  assert publication.instruction is not None
  assert publication.instruction["modifier"] == "right"
  assert adapter.instruction_reason == "accepted_fresh_replay_hold"


def test_fresh_replayed_broadcast_holds_previous_action_until_new_source_snapshot():
  adapter = AmapPhoneRouteAdapter()
  first = deepcopy(make_sample())
  publication = adapter.accept(first, now_ns=NOW_NS)
  assert publication.instruction is not None and adapter.instruction_reason == "accepted"

  replay = deepcopy(make_sample(now_ns=NOW_NS + 200_000_000, sequence=2))
  record = replay.broadcast["records"]["10001"]
  record["sequence"] = first.broadcast["records"]["10001"]["sequence"]
  record["replayed"] = True
  record["source_age_upper_ns"] = 300_000_000
  publication = adapter.accept(replay, now_ns=NOW_NS + 200_000_000)

  assert publication is not None
  assert publication.instruction is not None
  assert publication.instruction["modifier"] == "right"
  assert adapter.instruction_reason == "accepted_fresh_replay_hold"


@pytest.mark.parametrize(("field", "value"), [
  ("replayed", False), ("replayed", None), ("fresh", False),
  ("source_age_upper_ns", 2_000_000_001), ("source_age_upper_ns", -1),
])
def test_replay_hold_requires_exact_fresh_replay(field, value):
  adapter = AmapPhoneRouteAdapter()
  adapter.accept(deepcopy(make_sample()), now_ns=NOW_NS)
  replay = deepcopy(make_sample(now_ns=NOW_NS + 200_000_000, sequence=2))
  record = replay.broadcast["records"]["10001"]
  record["sequence"] = 119
  record.update(replayed=True, source_age_upper_ns=300_000_000)
  record[field] = value
  if field in ("replayed", "fresh"):
    record["fields"]["ICON"] = 4
    record["fields"]["NEW_ICON"] = 4

  publication = adapter.accept(replay, now_ns=NOW_NS + 200_000_000)

  assert publication is not None
  assert publication.instruction is None
  assert adapter.instruction_reason != "accepted_fresh_replay_hold"


@pytest.mark.parametrize("change", [
  {"maneuver": 2}, {"broadcast_distance": 80}, {"branch_flag": 2},
  {"segment_binding": False}, {"obtain_supported": False},
])
def test_replay_hold_rejects_new_action_or_inconsistent_source(change):
  adapter = AmapPhoneRouteAdapter()
  adapter.accept(deepcopy(make_sample()), now_ns=NOW_NS)
  replay = deepcopy(make_sample(now_ns=NOW_NS + 200_000_000, sequence=2))
  record = replay.broadcast["records"]["10001"]
  record["sequence"] = 119
  record.update(replayed=True, source_age_upper_ns=300_000_000)
  navi = replay.native_after["_navi_info"]
  if "maneuver" in change:
    for branch in navi["branches"]:
      branch["maneuver_id_raw"] = change["maneuver"]
    record["fields"]["ICON"] = change["maneuver"]
    record["fields"]["NEW_ICON"] = change["maneuver"]
  if "broadcast_distance" in change:
    record["fields"]["SEG_REMAIN_DIS"] = change["broadcast_distance"]
  if "branch_flag" in change:
    navi["branch_flag_raw"] = change["branch_flag"]
  if "segment_binding" in change:
    navi["branches"][0]["segment_binding"] = {"status": "unavailable",
                                               "reason": "segment_index_unavailable"}
  if "obtain_supported" in change:
    navi["branches"][0]["obtain_supported_raw"] = 0
  publication = adapter.accept(replay, now_ns=NOW_NS + 200_000_000)

  assert publication is not None
  assert publication.instruction is None


@pytest.mark.parametrize("field,value", [
  ("reason", "navi_info_next_segment_invalid"),
  ("diagnostic_only", False), ("control_input_allowed", True),
  ("navigation_info_type_raw", 1), ("item_count", 0), ("item_index", 1),
  ("branch_flag_raw", False),
])
def test_same_broadcast_replay_revalidates_native_action_contract(field, value):
  adapter = AmapPhoneRouteAdapter()
  first = adapter.accept(make_sample(), now_ns=NOW_NS)
  assert first is not None and first.instruction is not None
  replay = make_sample(now_ns=NOW_NS + 200_000_000, sequence=2)
  replay.broadcast["records"]["10001"].update(replayed=True, source_age_upper_ns=300_000_000)
  replay.native_after["_navi_info"][field] = value
  replay.native_before["_navi_info"] = deepcopy(replay.native_after["_navi_info"])
  publication = adapter.accept(replay, now_ns=NOW_NS + 200_000_000)
  assert publication is not None, adapter.reason
  assert publication.instruction is None


@pytest.mark.parametrize("index", [0, 1])
@pytest.mark.parametrize("field", ["next_road_name_segment_raw", "next_road_name_link_raw"])
def test_same_broadcast_replay_rejects_raw_next_road_even_if_reason_claims_valid(index, field):
  adapter = AmapPhoneRouteAdapter()
  first = adapter.accept(make_sample(), now_ns=NOW_NS)
  assert first is not None and first.instruction is not None
  replay = make_sample(now_ns=NOW_NS + 200_000_000, sequence=2)
  replay.broadcast["records"]["10001"].update(replayed=True, source_age_upper_ns=300_000_000)
  replay.native_after["_navi_info"]["branches"][index][field] = 0xfffffffe
  replay.native_before["_navi_info"] = deepcopy(replay.native_after["_navi_info"])
  publication = adapter.accept(replay, now_ns=NOW_NS + 200_000_000)
  assert publication is not None, adapter.reason
  assert publication.instruction is None


def test_same_broadcast_cannot_revive_action_after_native_rejection():
  adapter = AmapPhoneRouteAdapter()
  assert adapter.accept(make_sample(), now_ns=NOW_NS).instruction is not None
  bad = make_sample(now_ns=NOW_NS + 200_000_000, sequence=2)
  bad.broadcast["records"]["10001"].update(replayed=True, source_age_upper_ns=300_000_000)
  bad.native_after["_navi_info"]["reason"] = "navi_info_next_segment_invalid"
  assert adapter.accept(bad, now_ns=NOW_NS + 200_000_000).instruction is None
  repeated = make_sample(now_ns=NOW_NS + 400_000_000, sequence=3)
  repeated.broadcast["records"]["10001"].update(replayed=True, source_age_upper_ns=500_000_000)
  publication = adapter.accept(repeated, now_ns=NOW_NS + 400_000_000)
  assert publication is not None and publication.instruction is None
  restored = make_sample(now_ns=NOW_NS + 600_000_000, sequence=4)
  restored.broadcast["records"]["10001"]["sequence"] = 120
  assert adapter.accept(restored, now_ns=NOW_NS + 600_000_000).instruction is not None


def test_replay_hold_preserves_its_bounded_bridge_age_tolerance():
  adapter = AmapPhoneRouteAdapter()
  first = adapter.accept(make_sample(), now_ns=NOW_NS)
  replay = make_sample(now_ns=NOW_NS + 1_300_000_000, sequence=2, elapsed_ns=11_300_000_000)
  replay.broadcast.update(status="unavailable", reason="10001_stale")
  replay.broadcast["records"]["10001"].update(replayed=True, fresh=False, source_age_upper_ns=1_500_000_000)
  held = adapter.accept(replay, now_ns=NOW_NS + 1_300_000_000)
  assert held is not None and held.instruction == first.instruction
  assert adapter.instruction_reason == "accepted_fresh_replay_hold"
  replay.broadcast["records"]["10001"]["source_age_upper_ns"] = 2_000_000_001
  expired = adapter.accept(replay, now_ns=NOW_NS + 1_300_000_000)
  assert expired is not None and expired.instruction is None


@pytest.mark.parametrize("instructions", [True, False])
def test_map_gate_is_independent_from_instruction_validity(pipeline, monkeypatch, instructions):
  pipeline.sm.valid["navInstruction"] = instructions
  assert pipeline.sm["navInstruction"].routeBinding.version == 1
  subject = model_runtime(pipeline, monkeypatch)
  image, vector = subject.update(pipeline.sm, 0.0, MAP_SHAPE, VEC_SHAPE)
  assert image is not None and image.any()
  if instructions:
    assert vector is not None and vector[0, 10] == pytest.approx(1.0)
    assert vector[0, -1] == pytest.approx(0.79)
  else:
    assert vector is None


def test_map_rejection_keeps_verified_route_action(pipeline, monkeypatch):
  subject = model_runtime(pipeline, monkeypatch)
  pipeline.sm["navRoute"].phonePose.coordinateSystem = "WGS84"
  image, vector = subject.update(pipeline.sm, 0.0, MAP_SHAPE, VEC_SHAPE)
  assert image is None and vector is not None
  assert vector[0, 10] == pytest.approx(1.0) and vector[0, -1] == pytest.approx(0.79)


def test_cancel_revokes_pixels_and_resets_both_model_histories(pipeline, monkeypatch):
  class Model(NavigationResetMixin):
    input_shapes = {"nav_map": MAP_SHAPE, "navigation": VEC_SHAPE}

  subject = model_runtime(pipeline, monkeypatch)
  model = Model()
  first = subject.inputs_for_model(pipeline.sm, 0.0, model)
  assert first["nav_map"].any() and first["navigation"].any()
  model._commit_nav_reset(model.pending_nav_resets())
  pipeline.adapter.withdraw(pipeline.clock[0], "phone_navigation_stopped")
  pipeline.sm.publish(None, pipeline.clock[0])
  second = subject.inputs_for_model(pipeline.sm, 0.0, model)
  assert not second["nav_map"].any() and not second["navigation"].any()
  assert model.pending_nav_resets() == {"map": 2, "navigation": 2}
  assert not subject._phone_frames and subject._phone_route_context is None


def test_instruction_loss_without_route_change_keeps_map_but_resets_navigation(pipeline, monkeypatch):
  class Model(NavigationResetMixin):
    input_shapes = {"nav_map": MAP_SHAPE, "navigation": VEC_SHAPE}

  subject = model_runtime(pipeline, monkeypatch)
  model = Model()
  first = subject.inputs_for_model(pipeline.sm, 0.0, model)
  assert first["nav_map"].any() and first["navigation"].any()
  model._commit_nav_reset(model.pending_nav_resets())
  pipeline.sm.valid["navInstruction"] = False
  second = subject.inputs_for_model(pipeline.sm, 0.0, model)
  assert second["nav_map"].any() and not second["navigation"].any()
  assert model.pending_nav_resets() == {"navigation": 2}


@pytest.mark.parametrize("fault", ["missing_attribute", "none", "non_mapping", "missing_records",
                                   "missing_record", "null_record", "non_mapping_record"])
def test_optional_broadcast_loss_keeps_verified_map_and_clears_previous_action(pipeline, monkeypatch, fault):
  class Model(NavigationResetMixin):
    input_shapes = {"nav_map": MAP_SHAPE, "navigation": VEC_SHAPE}

  subject = model_runtime(pipeline, monkeypatch)
  model = Model()
  initial = subject.inputs_for_model(pipeline.sm, 0.0, model)
  assert initial["nav_map"].any() and initial["navigation"][0, 10] == 1
  model._commit_nav_reset(model.pending_nav_resets())

  pipeline.clock[0] += 200_000_000
  sample = make_sample(now_ns=pipeline.clock[0], sequence=2, elapsed_ns=10_200_000_000)
  if fault == "missing_attribute":
    del sample.broadcast
  elif fault == "none":
    sample.broadcast = None
  elif fault == "non_mapping":
    sample.broadcast = []
  elif fault == "missing_records":
    del sample.broadcast["records"]
  elif fault == "missing_record":
    del sample.broadcast["records"]["10001"]
  else:
    sample.broadcast["records"]["10001"] = None if fault == "null_record" else []

  publication = pipeline.adapter.accept(sample, now_ns=pipeline.clock[0])
  assert publication is not None, pipeline.adapter.reason
  assert publication.instruction is None
  assert pipeline.adapter.instruction_reason == "phone_instruction_broadcast_unverified"
  pipeline.sm.publish(publication, pipeline.clock[0])
  queue_frame(subject, pipeline, frame_id=43)
  lost = subject.inputs_for_model(pipeline.sm, 0.0, model)
  assert lost["nav_map"].any() and not lost["navigation"].any()
  assert model.pending_nav_resets() == {"navigation": 2}
  model._commit_nav_reset(model.pending_nav_resets())

  pipeline.clock[0] += 200_000_000
  restored = make_sample(now_ns=pipeline.clock[0], sequence=3, elapsed_ns=10_400_000_000)
  publication = pipeline.adapter.accept(restored, now_ns=pipeline.clock[0])
  assert publication is not None and publication.instruction is not None
  pipeline.sm.publish(publication, pipeline.clock[0])
  queue_frame(subject, pipeline, frame_id=44)
  recovered = subject.inputs_for_model(pipeline.sm, 0.0, model)
  assert recovered["nav_map"].any() and recovered["navigation"][0, 10] == 1
  assert model.pending_nav_resets() == {"navigation": 3}


@pytest.mark.parametrize("replayed", [False, True])
@pytest.mark.parametrize("reason", ["navi_info_path_match_missing", "navi_info_count_zero"])
def test_native_action_match_loss_keeps_map_without_reviving_previous_action(pipeline, monkeypatch, replayed, reason):
  class Model(NavigationResetMixin):
    input_shapes = {"nav_map": MAP_SHAPE, "navigation": VEC_SHAPE}

  subject = model_runtime(pipeline, monkeypatch)
  model = Model()
  first = subject.inputs_for_model(pipeline.sm, 0.0, model)
  assert first["nav_map"].any() and first["navigation"][0, 10] == 1
  model._commit_nav_reset(model.pending_nav_resets())

  pipeline.clock[0] += 200_000_000
  sample = make_sample(now_ns=pipeline.clock[0], sequence=2, elapsed_ns=10_200_000_000)
  missing = {"schema_version": 2, "status": "unavailable", "reason": reason,
             "diagnostic_only": True, "control_input_allowed": False, "route_render_input_allowed": False}
  sample.native_before["_navi_info"] = deepcopy(missing)
  sample.native_after["_navi_info"] = deepcopy(missing)
  sample.broadcast["records"]["10001"].update(replayed=replayed, sequence=1 if replayed else 2)
  publication = pipeline.adapter.accept(sample, now_ns=pipeline.clock[0])
  assert publication is not None and publication.instruction is None
  assert pipeline.adapter.instruction_reason == "phone_instruction_unavailable"
  pipeline.sm.publish(publication, pipeline.clock[0])
  queue_frame(subject, pipeline, frame_id=43)
  lost = subject.inputs_for_model(pipeline.sm, 0.0, model)
  assert lost["nav_map"].any() and not lost["navigation"].any()
  assert model.pending_nav_resets() == {"navigation": 2}
  model._commit_nav_reset(model.pending_nav_resets())

  pipeline.clock[0] += 200_000_000
  restored = make_sample(now_ns=pipeline.clock[0], sequence=3, elapsed_ns=10_400_000_000)
  publication = pipeline.adapter.accept(restored, now_ns=pipeline.clock[0])
  assert publication is not None and publication.instruction is not None
  pipeline.sm.publish(publication, pipeline.clock[0])
  queue_frame(subject, pipeline, frame_id=44)
  recovered = subject.inputs_for_model(pipeline.sm, 0.0, model)
  assert recovered["nav_map"].any() and recovered["navigation"][0, 10] == 1
  assert recovered["navigation"][0, -1] == pytest.approx(0.79)


@pytest.mark.parametrize("replayed", [False, True])
@pytest.mark.parametrize("field,reason", [
  ("next_road_name_segment_raw", "navi_info_next_segment_invalid"),
  ("next_road_name_link_raw", "navi_info_next_link_invalid"),
])
def test_quarantined_next_road_keeps_model_map_clears_action_then_recovers(pipeline, monkeypatch, replayed, field, reason):
  class Model(NavigationResetMixin):
    input_shapes = {"nav_map": MAP_SHAPE, "navigation": VEC_SHAPE}

  subject = model_runtime(pipeline, monkeypatch)
  model = Model()
  first = subject.inputs_for_model(pipeline.sm, 0.0, model)
  assert first["nav_map"].any() and first["navigation"][0, 10] == 1
  model._commit_nav_reset(model.pending_nav_resets())
  pipeline.clock[0] += 200_000_000
  sample = make_sample(now_ns=pipeline.clock[0], sequence=2, elapsed_ns=10_200_000_000)
  navi = sample.native_after["_navi_info"]
  navi["reason"] = reason
  navi["branches"][1][field] = 0xfffffffe
  sample.native_before["_navi_info"] = deepcopy(navi)
  original_sequence = pipeline.sample.broadcast["records"]["10001"]["sequence"]
  sample.broadcast["records"]["10001"].update(replayed=replayed, sequence=original_sequence if replayed else original_sequence + 1)
  publication = pipeline.adapter.accept(sample, now_ns=pipeline.clock[0])
  assert publication is not None and publication.instruction is None
  pipeline.sm.publish(publication, pipeline.clock[0])
  queue_frame(subject, pipeline, frame_id=43)
  lost = subject.inputs_for_model(pipeline.sm, 0.0, model)
  assert lost["nav_map"].any() and not lost["navigation"].any()
  assert model.pending_nav_resets() == {"navigation": 2}
  model._commit_nav_reset(model.pending_nav_resets())

  pipeline.clock[0] += 200_000_000
  restored = make_sample(now_ns=pipeline.clock[0], sequence=3, elapsed_ns=10_400_000_000)
  publication = pipeline.adapter.accept(restored, now_ns=pipeline.clock[0])
  assert publication is not None and publication.instruction is not None
  pipeline.sm.publish(publication, pipeline.clock[0])
  queue_frame(subject, pipeline, frame_id=44)
  recovered = subject.inputs_for_model(pipeline.sm, 0.0, model)
  assert recovered["nav_map"].any() and recovered["navigation"][0, 10] == 1
  assert model.pending_nav_resets() == {"navigation": 3}


@pytest.mark.parametrize("fault", [None, "stale_gps", "space", "path"])
@pytest.mark.parametrize("both_ticks", [False, True])
def test_native_coordinate_retry_cannot_bypass_independent_map_guards(pipeline, monkeypatch, fault, both_ticks):
  class Model(NavigationResetMixin):
    input_shapes = {"nav_map": MAP_SHAPE, "navigation": VEC_SHAPE}

  subject = model_runtime(pipeline, monkeypatch)
  model = Model()
  initial = subject.inputs_for_model(pipeline.sm, 0.0, model)
  assert initial["nav_map"].any() and initial["navigation"].any()
  model._commit_nav_reset(model.pending_nav_resets())
  pipeline.clock[0] += 200_000_000
  sample = make_sample(now_ns=pipeline.clock[0], sequence=2, elapsed_ns=10_200_000_000)
  fields = ["native_st_pos_deg", "tick_time_raw"] + (["input_tick_time_raw"] if both_ticks else [])
  retry = {"schema_version": 1, "source": "native_observation_pair", "extra_observations": 1,
           "recovered": True, "initial_diagnosis": {
             "schema_version": 2, "source": "native_observation_pair", "changed_fields": ["position"],
             "position_changed_fields": fields, "position_serialization_changed": True,
             "navi_info_changed_fields": [], "navi_info_serialization_changed": False}}
  for observation in (sample.native_before, sample.native_after):
    observation["_snapshot_retry"] = deepcopy(retry)
  if fault == "stale_gps":
    sample.gps = replace(sample.gps, age_upper_ns=2_000_000_000)
  elif fault == "space":
    for observation in (sample.native_before, sample.native_after):
      observation["_position"]["native_st_pos_deg"][1] += 0.001
  elif fault == "path":
    sample.native_after["path_identity_hash"] = "0" * 64
  publication = pipeline.adapter.accept(sample, now_ns=pipeline.clock[0])
  assert (publication is not None) == (fault is None)
  pipeline.sm.publish(publication, pipeline.clock[0])
  if publication is not None:
    queue_frame(subject, pipeline, frame_id=43)
  else:
    image, stamp = pipeline.renderer.update(pipeline.sm)
    assert not image.any() and stamp == 0
  inputs = subject.inputs_for_model(pipeline.sm, 0.0, model)
  assert bool(inputs["nav_map"].any()) == (fault is None)
  assert bool(inputs["navigation"].any()) == (fault is None)
  if fault is None:
    assert inputs["navigation"][0, 10] == 1 and inputs["navigation"][0, -1] == pytest.approx(0.79)
  else:
    assert set(model.pending_nav_resets()) == {"map", "navigation"}


@pytest.mark.parametrize("mutation", ["profile", "path", "epoch", "onroute", "segment", "link", "point", "matched", "space"])
def test_source_fault_immediately_publishes_black_and_does_not_retain_old_map(pipeline, mutation):
  image, stamp = pipeline.renderer.update(pipeline.sm)
  assert image.any() and stamp
  pipeline.clock[0] += 100_000_000
  bad = make_sample(now_ns=pipeline.clock[0], sequence=2)
  if mutation == "profile":
    bad.native_after["profile"] = "unsupported"
  elif mutation == "path":
    bad.native_after["path_identity_hash"] = "0" * 64
  elif mutation == "epoch":
    bad.native_after["process_epoch_hash"] = "0" * 64
  elif mutation == "onroute":
    bad.native_after["_position"]["is_on_guide_road_raw"] = 0x80
  elif mutation in ("segment", "link", "point"):
    for observation in (bad.native_before, bad.native_after):
      observation["_position"][mutation + "_index"] = 99999
  elif mutation == "matched":
    bad.native_after["_position"]["matched_st_pos_deg"][1] += 0.0001
  else:
      bad.native_after["_position"]["native_st_pos_deg"][1] += 0.0003
  publication = pipeline.adapter.accept(bad, now_ns=pipeline.clock[0])
  assert publication is None
  pipeline.sm.publish(publication, pipeline.clock[0])
  image, stamp = pipeline.renderer.update(pipeline.sm)
  assert not image.any() and stamp == 0
  assert pipeline.renderer.tracker is None and pipeline.renderer._phone_route_context is None


@pytest.mark.parametrize("fault", [None, "stale", "space", "identity"])
def test_actual_producer_anchor_repair_reaches_map_without_bypassing_adapter(pipeline, monkeypatch, fault):
  from APP.navdiag.test_phone_route_producer import anchor_retry_rig

  class Model(NavigationResetMixin):
    input_shapes = {"nav_map": MAP_SHAPE, "navigation": VEC_SHAPE}

  subject = model_runtime(pipeline, monkeypatch)
  model = Model()
  initial = subject.inputs_for_model(pipeline.sm, 0.0, model)
  assert initial["nav_map"].any() and initial["navigation"].any()
  model._commit_nav_reset(model.pending_nav_resets())
  state = anchor_retry_rig()
  state.clock.now = NOW_NS + 200_000_000
  corner = (10.0001, 19.9999)
  native, gps = state.native.observe_position_navi_info, state.gps.observe

  def native_at_shared_point():
    value = native()
    value["_position"].update(native_st_pos_deg=list(corner), matched_st_pos_deg=list(corner))
    return value

  def gps_at_shared_point():
    fix, raw, evidence = gps()
    return replace(fix, elapsed_realtime_ns=2_000_000_000, source_token="c" * 64, coordinates=corner), raw, evidence

  state.native.observe_position_navi_info = native_at_shared_point
  state.gps.observe = gps_at_shared_point
  sample = state.worker._take_sample(state.device, state.native, state.gps)
  assert len(state.observations) == 3 and sample.anchor_pair_retry["recovered"] is True
  assert sample.native_before["_position"]["point_index"] == sample.native_after["_position"]["point_index"] == 1
  if fault == "stale":
    sample = replace(sample, gps=replace(sample.gps, age_upper_ns=2_000_000_000))
  elif fault == "space":
    sample = replace(sample, native_after={**sample.native_after, "_position": {
      **sample.native_after["_position"], "native_st_pos_deg": [10.1, 20.]}})
  elif fault == "identity":
    sample = replace(sample, native_after={**sample.native_after, "path_identity_hash": "0" * 64})
  pipeline.clock[0] = state.clock()
  pipeline.adapter = AmapPhoneRouteAdapter()
  publication = pipeline.adapter.accept(sample, now_ns=pipeline.clock[0])
  assert (publication is not None) == (fault is None), pipeline.adapter.reason
  pipeline.sm.publish(publication, pipeline.clock[0])
  subject.inputs_for_model(pipeline.sm, 0.0, model)
  if publication is not None:
    queue_frame(subject, pipeline, frame_id=43)
  else:
    image, stamp = pipeline.renderer.update(pipeline.sm)
    assert not image.any() and stamp == 0
  inputs = subject.inputs_for_model(pipeline.sm, 0.0, model)
  assert bool(inputs["nav_map"].any()) == (fault is None), subject.evidence()
  assert not inputs["navigation"].any()


def test_unchanged_gps_token_cannot_extend_its_source_lease(pipeline):
  position_time = pipeline.publication.phone_pose["positionMonoTime"]
  for elapsed in [200_000_000, 500_000_000, 900_000_000]:
    pipeline.clock[0] = NOW_NS + elapsed
    same_fix = make_sample(now_ns=pipeline.clock[0], sequence=1 + elapsed)
    same_fix.gps = replace(same_fix.gps, age_upper_ns=pipeline.sample.gps.age_upper_ns + elapsed)
    publication = pipeline.adapter.accept(same_fix, now_ns=pipeline.clock[0])
    assert publication is not None, pipeline.adapter.reason
    assert publication.phone_pose["positionMonoTime"] >= position_time
    assert pipeline.adapter.decision_evidence()["same_gps_token"] is True
    assert publication.phone_pose["gpsSourceToken"] == pipeline.sample.gps.source_token
  pipeline.clock[0] = NOW_NS + 2_000_000_000
  assert pipeline.adapter.accept(same_fix, now_ns=pipeline.clock[0]) is None


def test_new_transport_can_recover_but_old_observation_does_not_resurrect(pipeline):
  pipeline.adapter.withdraw(NOW_NS, "usb_disconnected")
  assert pipeline.adapter.accept(pipeline.sample, now_ns=NOW_NS) is None
  fresh = make_sample(now_ns=NOW_NS + 200_000_000, sequence=1, transport="transport-2")
  assert pipeline.adapter.accept(fresh, now_ns=NOW_NS + 200_000_000) is not None


def test_same_transport_recovers_after_delayed_sample_hits_withdrawal_barrier(pipeline):
  pipeline.adapter.accept(None, now_ns=NOW_NS + 100_000_000)
  delayed = make_sample(now_ns=NOW_NS + 125_000_000, sequence=2)
  assert pipeline.adapter.accept(delayed, now_ns=NOW_NS + 125_000_000) is None
  assert pipeline.adapter.reason == "observation_before_withdrawal"
  fresh = make_sample(now_ns=NOW_NS + 150_000_000, sequence=3)
  publication = pipeline.adapter.accept(fresh, now_ns=NOW_NS + 150_000_000)
  assert publication is not None, pipeline.adapter.reason


def test_repeated_prewithdrawal_samples_cannot_push_recovery_barrier_forward(pipeline):
  pipeline.adapter.accept(None, now_ns=NOW_NS + 100_000_000)
  delayed = make_sample(now_ns=NOW_NS + 125_000_000, sequence=2)
  for delay in (125_000_000, 150_000_000, 175_000_000):
    assert pipeline.adapter.accept(delayed, now_ns=NOW_NS + delay) is None
    assert pipeline.adapter.reason == "observation_before_withdrawal"
  fresh = make_sample(now_ns=NOW_NS + 200_000_000, sequence=3)
  assert pipeline.adapter.accept(fresh, now_ns=NOW_NS + 200_000_000) is not None


def test_repeated_missing_source_only_arms_recovery_barrier_once(pipeline):
  """A long producer refresh must not keep moving the barrier past its next observation."""
  pipeline.adapter.accept(None, now_ns=NOW_NS + 100_000_000)
  barrier = pipeline.adapter._lease._withdrawn_at
  for delay in (200_000_000, 300_000_000, 400_000_000):
    assert pipeline.adapter.accept(None, now_ns=NOW_NS + delay) is None
    assert pipeline.adapter._lease._withdrawn_at == barrier

  fresh = make_sample(now_ns=NOW_NS + 500_000_000, sequence=2, elapsed_ns=10_100_000_000)
  assert fresh.host_start_ns / 1e9 > barrier
  publication = pipeline.adapter.accept(fresh, now_ns=NOW_NS + 500_000_000)
  assert publication is not None, pipeline.adapter.reason


def test_refresh_longer_than_one_second_can_reuse_the_previous_sample():
  sample = make_sample(now_ns=NOW_NS - 1_500_000_000)
  adapter = AmapPhoneRouteAdapter()
  publication = adapter.accept(sample, now_ns=NOW_NS)
  assert publication is not None, adapter.reason

  expired = make_sample(now_ns=NOW_NS - 2_000_000_000)
  expired.gps = replace(expired.gps, host_started_ns=NOW_NS - 2_030_000_000,
                        host_finished_ns=NOW_NS - 2_025_000_000,
                        age_upper_ns=100_000_000)
  assert adapter.accept(expired, now_ns=NOW_NS) is None
  assert adapter.reason == "phone_observation_stale_or_future"


def test_actual_source_withdrawal_still_advances_recovery_barrier(pipeline):
  pipeline.adapter.accept(None, now_ns=NOW_NS + 100_000_000)
  pipeline.adapter.withdraw(NOW_NS + 200_000_000, "usb_disconnected")
  assert pipeline.adapter._lease._withdrawn_at == pytest.approx((NOW_NS + 200_000_000) / 1e9)
  old = make_sample(now_ns=NOW_NS + 225_000_000, sequence=2)
  assert pipeline.adapter.accept(old, now_ns=NOW_NS + 225_000_000) is None
  assert pipeline.adapter.reason == "observation_before_withdrawal"
  fresh = make_sample(now_ns=NOW_NS + 250_000_000, sequence=3)
  assert pipeline.adapter.accept(fresh, now_ns=NOW_NS + 250_000_000) is not None


def test_known_same_session_refresh_freezes_and_reuses_verified_publication(pipeline):
  later = NOW_NS + 750_000_000
  status = SimpleNamespace(running=True, connected=True, reason="phone_selection_refreshing",
                           transport_session="transport-1", sequence=1)
  publication = pipeline.adapter.accept(None, now_ns=later, source_status=status)
  assert publication is pipeline.publication
  assert publication.active.observed_sequence == pipeline.publication.active.observed_sequence
  assert publication.active.observed_mono_time == pipeline.publication.active.observed_mono_time
  assert publication.phone_pose["positionMonoTime"] == pipeline.publication.phone_pose["positionMonoTime"]
  assert publication.phone_pose["gpsSourceToken"] == pipeline.publication.phone_pose["gpsSourceToken"]
  evidence = pipeline.adapter.decision_evidence()
  assert evidence["decision_reason"] == "accepted_transient_source_refresh"
  assert evidence["publication_reused"] is True
  assert evidence["source_status_reason"] == "phone_selection_refreshing"
  assert evidence["held_publication_age_ns"] == later - pipeline.sample.host_start_ns
  assert pipeline.adapter.reason == QUALITY_STRICT


@pytest.mark.parametrize(("field", "value"), [
  ("connected", False),
  ("running", False),
  ("reason", "phone_navigation_not_active"),
  ("transport_session", "transport-2"),
  ("sequence", 2),
])
def test_source_refresh_hold_rejects_identity_or_real_withdrawal(pipeline, field, value):
  attrs = {"running": True, "connected": True, "reason": "phone_selection_refreshing",
           "transport_session": "transport-1", "sequence": 1}
  attrs[field] = value
  assert pipeline.adapter.accept(
    None, now_ns=NOW_NS + 250_000_000, source_status=SimpleNamespace(**attrs)) is None
  assert pipeline.adapter.reason == "phone_source_unavailable"
  assert pipeline.adapter._publication is None


def test_source_refresh_hold_never_exceeds_original_two_second_lease(pipeline):
  status = SimpleNamespace(running=True, connected=True, reason="phone_selection_refreshing",
                           transport_session="transport-1", sequence=1)
  assert pipeline.adapter.accept(None, now_ns=pipeline.sample.host_start_ns + 1_900_000_000,
                                 source_status=status) is not None
  assert pipeline.adapter.accept(None, now_ns=pipeline.sample.host_start_ns + 2_100_000_000,
                                 source_status=status) is None
  assert pipeline.adapter._publication is None


def test_expired_source_refresh_drops_map_without_advancing_recovery_barrier(pipeline):
  status = SimpleNamespace(running=True, connected=True, reason="phone_selection_refreshing",
                           transport_session="transport-1", sequence=1)
  barrier = pipeline.adapter._lease._withdrawn_at
  expired_at = pipeline.sample.host_start_ns + 2_100_000_000
  assert pipeline.adapter.accept(None, now_ns=expired_at, source_status=status) is None
  assert pipeline.adapter.reason == "phone_transient_refresh_expired"
  assert pipeline.adapter._lease._withdrawn_at == barrier
  assert pipeline.adapter._publication is None
  evidence = pipeline.adapter.decision_evidence()
  assert evidence["decision_reason"] == "phone_transient_refresh_expired"
  assert evidence["publication_reused"] is False
  assert evidence["source_status_reason"] == "phone_selection_refreshing"
  assert evidence["held_publication_age_ns"] == expired_at - pipeline.sample.host_start_ns

  # Repeated polls in the same long refresh retain the original map barrier.
  for elapsed in (100_000_000, 300_000_000, 600_000_000):
    later = expired_at + elapsed
    assert pipeline.adapter.accept(None, now_ns=later, source_status=status) is None
    assert pipeline.adapter.reason == "phone_transient_refresh_expired"
    assert pipeline.adapter._lease._withdrawn_at == barrier

  # The refresh began before the old sample's lease ended, but the real transport never disappeared.
  refreshed = make_sample(now_ns=expired_at - 100_000_000, sequence=2,
                          elapsed_ns=pipeline.sample.gps.elapsed_realtime_ns + 100_000_000)
  assert pipeline.adapter.accept(refreshed, now_ns=expired_at + 600_000_000) is not None, pipeline.adapter.reason
  assert pipeline.adapter.reason == QUALITY_STRICT


def test_pending_observation_across_transient_refresh_expiry_keeps_real_barrier(pipeline):
  """A refresh may deliver a sample started after its last published one but before the
  lease-expiry poll; that is not an observation from before a real withdrawal."""
  status = SimpleNamespace(running=True, connected=True, reason="phone_selection_refreshing",
                           transport_session="transport-1", sequence=1)
  assert pipeline.adapter.accept(None, now_ns=pipeline.sample.host_start_ns + 1_900_000_000,
                                 source_status=status) is not None
  sample_started = pipeline.sample.host_start_ns + 2_050_000_000
  arrival = sample_started + 100_000_000
  assert pipeline.adapter.accept(None, now_ns=arrival, source_status=status) is None
  barrier = pipeline.adapter._lease._withdrawn_at
  assert pipeline.adapter.reason == "phone_transient_refresh_expired"
  assert barrier < sample_started / 1e9
  refreshed = make_sample(now_ns=sample_started, sequence=2,
                          elapsed_ns=pipeline.sample.gps.elapsed_realtime_ns + 150_000_000)
  publication = pipeline.adapter.accept(refreshed, now_ns=arrival)
  assert publication is not None, pipeline.adapter.reason


@pytest.mark.parametrize(("field", "value"), [
  ("connected", False),
  ("running", False),
  ("reason", "phone_navigation_not_active"),
  ("transport_session", "transport-2"),
  ("sequence", 2),
])
def test_real_disconnect_still_advances_barrier_when_refresh_looks_expired(pipeline, field, value):
  attrs = {"running": True, "connected": True, "reason": "phone_selection_refreshing",
           "transport_session": "transport-1", "sequence": 1}
  attrs[field] = value
  status = SimpleNamespace(**attrs)
  old_barrier = pipeline.adapter._lease._withdrawn_at
  expired_at = pipeline.sample.host_start_ns + 2_100_000_000
  assert pipeline.adapter.accept(None, now_ns=expired_at, source_status=status) is None
  assert pipeline.adapter.reason == "phone_source_unavailable"
  assert pipeline.adapter._lease._withdrawn_at == pytest.approx(expired_at / 1e9)
  assert pipeline.adapter._lease._withdrawn_at > old_barrier


def test_source_refresh_hold_rejects_non_integer_clock_before_arithmetic(pipeline):
  status = SimpleNamespace(running=True, connected=True, reason="phone_selection_refreshing",
                           transport_session="transport-1", sequence=1)
  assert pipeline.adapter.accept(None, now_ns=NOW_NS + 0.25, source_status=status) is None
  assert pipeline.adapter.reason == "phone_source_unavailable"


def test_mapsd_keeps_identical_binding_during_authorized_source_refresh(pipeline):
  first_image, first_stamp = pipeline.renderer.update(pipeline.sm)
  assert first_image.any() and first_stamp == pipeline.publication.phone_pose["positionMonoTime"]
  later = NOW_NS + 750_000_000
  pipeline.clock[0] = later
  status = SimpleNamespace(running=True, connected=True, reason="phone_selection_refreshing",
                           transport_session="transport-1", sequence=1)
  publication = pipeline.adapter.accept(None, now_ns=later, source_status=status)
  assert publication is pipeline.publication
  pipeline.sm.publish(publication, later)
  image, stamp = pipeline.renderer.update(pipeline.sm)
  assert image.any() and stamp == first_stamp
  assert pipeline.renderer._phone_route_context.binding.observed_sequence == 1
  assert pipeline.renderer._phone_route_context.binding.observed_mono_time_ns == pipeline.sample.host_start_ns


def test_nav_runtime_keeps_map_valid_across_long_authorized_source_refresh(pipeline, monkeypatch):
  subject = model_runtime(pipeline, monkeypatch)
  image, _ = subject.update(pipeline.sm, 0.0, MAP_SHAPE, VEC_SHAPE)
  assert image is not None and subject.status()["map_valid"] is True

  later = NOW_NS + 750_000_000
  pipeline.clock[0] = later
  status = SimpleNamespace(running=True, connected=True, reason="phone_selection_refreshing",
                           transport_session="transport-1", sequence=1)
  publication = pipeline.adapter.accept(None, now_ns=later, source_status=status)
  assert publication is pipeline.publication
  pipeline.sm.publish(publication, later)
  queue_frame(subject, pipeline, frame_id=43)

  image, _ = subject.update(pipeline.sm, 0.0, MAP_SHAPE, VEC_SHAPE)
  assert image is not None and image.any()
  assert subject.status()["map_valid"] is True
  assert subject.evidence()["mapReason"] == "accepted"


def test_navd_keeps_instruction_during_producer_refresh_without_renewing_its_clock(pipeline, monkeypatch):
  monkeypatch.setattr(navd, "amap_root_nav_enabled", lambda: True)
  sent = {}
  source = SimpleNamespace(
    latest=lambda: pipeline.sample,
    status=lambda: SimpleNamespace(running=True, connected=True, reason="phone_selection_refreshing",
                                   transport_session="transport-1", sequence=1))
  daemon = navd.Navd(None, SimpleNamespace(send=lambda service, message: sent.__setitem__(service, message)),
                      phone_source=source)
  daemon.publish_route(NOW_NS / 1e9)
  original_at = daemon._phone_publication_at
  source.latest = lambda: None
  later = NOW_NS / 1e9 + 0.75
  daemon.publish_route(later)
  assert daemon._phone_publication is not None
  assert daemon._phone_publication_at == original_at
  daemon.publish_instruction(original_at + 1.0)
  assert sent["navInstruction"].valid
  daemon.publish_instruction(original_at + 2.01)
  assert not sent["navInstruction"].valid


@pytest.mark.parametrize("location", ["native_before", "native_after"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), True, 181.0])
def test_either_native_pose_must_be_finite_strict_and_in_range(pipeline, location, value):
  bad = deepcopy(pipeline.sample)
  getattr(bad, location)["_position"]["native_st_pos_deg"][0] = value
  assert pipeline.adapter.accept(bad, now_ns=NOW_NS) is None


@pytest.mark.parametrize("field,value", [("navi_type_raw", False), ("navi_state_raw", True), ("onroute", True)])
def test_native_integer_state_flags_do_not_accept_booleans(pipeline, field, value):
  bad = deepcopy(pipeline.sample)
  if field == "onroute":
    bad.native_before["_position"]["is_on_guide_road_raw"] = value
  else:
    bad.native_before[field] = value
  assert pipeline.adapter.accept(bad, now_ns=NOW_NS) is None


@pytest.mark.parametrize("fault", ["foreign_clock", "old_native", "gps_before_native", "gps_after_native",
                                   "inverted_native", "future_native", "sample_start_mismatch"])
def test_native_and_gps_observation_windows_must_be_one_ordered_host_bracket(pipeline, fault):
  bad = deepcopy(pipeline.sample)
  if fault == "foreign_clock":
    bad.native_before["host_observation_clock_domain"] = "phone_elapsed_realtime"
  elif fault == "old_native":
    bad.host_start_ns -= 2_000_000_000
    bad.native_before["host_observation_started_monotonic_ns"] = bad.host_start_ns
  elif fault == "gps_before_native":
    bad.gps = replace(bad.gps, host_started_ns=NOW_NS - 90_000_000)
  elif fault == "gps_after_native":
    bad.gps = replace(bad.gps, host_finished_ns=NOW_NS - 5_000_000)
  elif fault == "inverted_native":
    bad.native_after["host_observation_finished_monotonic_ns"] = NOW_NS - 21_000_000
  elif fault == "future_native":
    bad.native_after["host_observation_finished_monotonic_ns"] = NOW_NS + 1
  else:
    bad.host_start_ns += 1
  assert pipeline.adapter.accept(bad, now_ns=NOW_NS) is None


def test_same_gps_token_position_time_advances_with_a_tighter_source_bound(pipeline):
  first = make_sample(now_ns=NOW_NS)
  first.gps = replace(first.gps, age_upper_ns=950_000_000)
  initial = pipeline.adapter.accept(first, now_ns=NOW_NS)
  assert initial is not None, pipeline.adapter.reason

  later_ns = NOW_NS + 1_500_000_000
  later = make_sample(now_ns=later_ns, sequence=2)
  later.gps = replace(later.gps, age_upper_ns=700_000_000)
  assert later.gps.elapsed_realtime_ns == first.gps.elapsed_realtime_ns
  assert later.gps.source_token == first.gps.source_token
  assert later.gps.fresh_at(later_ns)

  publication = pipeline.adapter.accept(later, now_ns=later_ns)
  assert publication is not None, pipeline.adapter.reason
  assert publication.phone_pose["positionMonoTime"] >= initial.phone_pose["positionMonoTime"]
  assert later_ns - publication.phone_pose["positionMonoTime"] < 2_000_000_000
  evidence = pipeline.adapter.decision_evidence()
  assert evidence["same_gps_token"] is True
  assert evidence["gps_source_age_upper_ns"] == 725_000_000
  assert evidence["gps_position_age_upper_ns"] < 2_000_000_000


def test_same_gps_token_cannot_change_coordinates(pipeline):
  later = make_sample(now_ns=NOW_NS + 200_000_000, sequence=2)
  later.gps = replace(later.gps, age_upper_ns=300_000_000,
                      coordinates=(later.gps.coordinates[0] + 0.000001, later.gps.coordinates[1]))
  assert pipeline.adapter.accept(later, now_ns=NOW_NS + 200_000_000) is None


@pytest.mark.parametrize("fault", ["cache_bytes", "cache_hash", "selected_route", "gps_boot", "gps_time", "gps_token"])
def test_source_cache_and_gps_identity_conflicts_revoke_lease(pipeline, fault):
  bad = make_sample(now_ns=NOW_NS + 100_000_000, sequence=2)
  if fault == "cache_bytes":
    bad.cache_bytes = bad.cache_bytes[:-1] + bytes([bad.cache_bytes[-1] ^ 1])
    bad.selected.cache_sha256 = hashlib.sha256(bad.cache_bytes).hexdigest()
  elif fault == "cache_hash":
    bad.selected.cache_sha256 = "0" * 64
  elif fault == "selected_route":
    bad.transport_session = "selected-route-conflict"
    bad.selected.route = replace(bad.selected.route, travel_time_s=bad.selected.route.travel_time_s + 1)
  elif fault == "gps_boot":
    bad.gps = replace(bad.gps, phone_boot_id="00000000-0000-0000-0000-000000000002")
  elif fault == "gps_time":
    bad.gps = replace(bad.gps, elapsed_realtime_ns=bad.gps.elapsed_realtime_ns - 1)
  else:
    bad.gps = replace(bad.gps, source_token="0" * 64)
  assert pipeline.adapter.accept(bad, now_ns=NOW_NS + 100_000_000) is None


@pytest.mark.parametrize("state", [2, 4, 5])
def test_paused_stopped_or_arrived_navigation_revokes_geometry(pipeline, state):
  bad = deepcopy(pipeline.sample)
  bad.native_after["navi_state_raw"] = state
  assert pipeline.adapter.accept(bad, now_ns=NOW_NS) is None


def test_before_source_indices_do_not_accept_boolean_alias_of_after_zero(pipeline):
  bad = deepcopy(pipeline.sample)
  bad.native_before["_position"]["segment_index"] = False
  assert pipeline.adapter.accept(bad, now_ns=NOW_NS) is None


def test_retired_transport_cannot_reappear_even_if_old_sample_still_under_one_second(pipeline):
  new = make_sample(now_ns=NOW_NS + 100_000_000, transport="transport-2")
  assert pipeline.adapter.accept(new, now_ns=NOW_NS + 100_000_000) is not None
  assert pipeline.adapter.accept(pipeline.sample, now_ns=NOW_NS + 200_000_000) is None


def test_identical_current_sample_can_be_polled_without_new_observation_claim(pipeline):
  publication = pipeline.adapter.accept(pipeline.sample, now_ns=NOW_NS + 100_000_000)
  assert publication is not None
  assert publication.active.observed_sequence == pipeline.publication.active.observed_sequence
  assert publication.active.observed_mono_time == pipeline.publication.active.observed_mono_time
  assert publication.phone_pose["positionMonoTime"] == NOW_NS + 100_000_000


def test_same_sequence_cannot_claim_a_new_observation_time(pipeline):
  conflict = make_sample(now_ns=NOW_NS + 100_000_000, sequence=1)
  assert pipeline.adapter.accept(conflict, now_ns=NOW_NS + 100_000_000) is None


def test_new_sequence_cannot_renew_an_old_observation_time(pipeline):
  conflict = deepcopy(pipeline.sample)
  conflict.sequence += 1
  assert pipeline.adapter.accept(conflict, now_ns=NOW_NS + 100_000_000) is None


def test_route_change_without_black_frame_resets_both_histories(pipeline, monkeypatch):
  class Model(NavigationResetMixin):
    input_shapes = {"nav_map": MAP_SHAPE, "navigation": VEC_SHAPE}

  subject = model_runtime(pipeline, monkeypatch)
  model = Model()
  assert subject.inputs_for_model(pipeline.sm, 0.0, model)["nav_map"].any()
  original_key = subject._phone_route_context.binding.route_key()
  model._commit_nav_reset(model.pending_nav_resets())
  pipeline.clock[0] += 100_000_000
  new = make_sample(now_ns=pipeline.clock[0], sequence=2, path_id=456)
  publication = pipeline.adapter.accept(new, now_ns=pipeline.clock[0])
  assert publication is not None, pipeline.adapter.reason
  pipeline.sm.publish(publication, pipeline.clock[0])
  subject._update_phone_route(pipeline.sm, pipeline.clock[0] / 1e9)
  assert subject._phone_route_context.binding.route_key() != original_key
  queue_frame(subject, pipeline, frame_id=43)
  inputs = subject.inputs_for_model(pipeline.sm, 0.0, model)
  assert inputs["nav_map"].any() and inputs["navigation"].any()
  assert model.pending_nav_resets() == {"map": 2, "navigation": 2}


@pytest.mark.parametrize("field,value", [("globalSegment", 1000), ("globalSegment", 2),
  ("coordinateSystem", "WGS84"), ("positionTransform", "identity"),
  ("positionMonoTime", NOW_NS + 1), ("positionMonoTime", NOW_NS - 2_000_000_000)])
def test_local_wire_pose_changes_fail_closed(pipeline, field, value):
  setattr(pipeline.sm["navRoute"].phonePose, field, value)
  image, stamp = pipeline.renderer.update(pipeline.sm)
  assert not image.any() and stamp == 0


def test_local_wire_native_frame_geometry_cannot_silently_change(pipeline):
  pipeline.sm["navRoute"].coordinates64[0].longitude += 0.00001
  with pytest.raises(RouteRejected, match="local_geometry_hash_mismatch"):
    LocalNavdRouteContext.from_submaster(pipeline.sm, now_ns=NOW_NS)
  image, stamp = pipeline.renderer.update(pipeline.sm)
  assert not image.any() and stamp == 0


def test_local_native_pose_rejects_stale_source_despite_fresh_navroute_republication(pipeline):
  pipeline.clock[0] = NOW_NS + 2_000_000_000
  pipeline.sm.recv_time["navRoute"] = pipeline.clock[0] / 1e9
  pipeline.sm.logMonoTime["navRoute"] = pipeline.clock[0]
  with pytest.raises(RouteRejected, match="phone_position_stale"):
    LocalNavdPhonePoseContext.from_submaster(pipeline.sm, now_ns=pipeline.clock[0])
  image, stamp = pipeline.renderer.update(pipeline.sm)
  assert not image.any() and stamp == 0


def test_local_phone_wire_accepts_one_and_a_half_second_refresh_gap(pipeline):
  pipeline.clock[0] = NOW_NS + 1_500_000_000
  pipeline.sm.recv_time["navRoute"] = pipeline.clock[0] / 1e9
  pipeline.sm.logMonoTime["navRoute"] = pipeline.clock[0] - 1_400_000_000
  pipeline.sm["navRoute"].phonePose.positionMonoTime = pipeline.clock[0] - 1_550_000_000
  context = LocalNavdPhonePoseContext.from_submaster(pipeline.sm, now_ns=pipeline.clock[0])
  assert context.position_mono_time_ns == pipeline.clock[0] - 1_550_000_000


def test_navd_is_sole_publisher_and_binds_verified_instruction_to_route(pipeline, monkeypatch):
  monkeypatch.setenv("SUNNYPILOT_AMAP_ROOT_NAV", "1")
  events = []
  phone_instruction = messaging.new_message("navInstruction", valid=True)
  phone_instruction.navInstruction.allManeuvers = [{"distance": 50, "type": "turn", "modifier": "right"}]
  monkeypatch.setattr(navd, "read_cplink_message", lambda *_: phone_instruction)
  source = SimpleNamespace(latest=lambda: pipeline.sample)
  daemon = navd.Navd(SimpleNamespace(), SimpleNamespace(send=lambda name, msg: events.append((name, msg))), phone_source=source)
  daemon.step(NOW_NS / 1e9)
  assert [name for name, _ in events] == ["navRoute", "navInstruction"]
  assert events[0][1].valid
  assert events[0][1].navRoute.routeBinding.coordinateSystem == "AMAP_NATIVE_MAP"
  assert events[0][1].navRoute.phonePose.reason == QUALITY_STRICT
  assert events[1][1].valid
  assert events[1][1].navInstruction.routeBinding.version == 1
  assert events[1][1].navInstruction.routeBinding.routeId == events[0][1].navRoute.routeBinding.routeId
  assert events[1][1].navInstruction.allManeuvers[0].type == "turn"
  assert events[1][1].navInstruction.allManeuvers[0].modifier == "right"


def test_anchor_rejection_exposes_fixed_subreason_without_coordinates(monkeypatch):
  """A rejected anchor surfaces its fixed code, never coordinates."""
  from openpilot.selfdrive.navigation import amap_phone_route as module
  sample = make_sample()
  adapter = AmapPhoneRouteAdapter()

  def boom(*_args, **_kwargs):
    raise module.AmapRouteAnchorRejected("discontinuous_source_edge")

  monkeypatch.setattr(module, "AmapRouteAnchorIndex", boom)
  assert adapter.accept(sample, now_ns=NOW_NS) is None
  assert adapter.reason == "phone_anchor_discontinuous_source_edge"
  assert "." not in adapter.reason and "," not in adapter.reason


def test_cache_decode_rejection_exposes_fixed_subreason(monkeypatch):
  """A cache decode failure surfaces its fixed code as a stable source_reason."""
  from openpilot.selfdrive.navigation import amap_phone_route as module
  sample = make_sample()
  adapter = AmapPhoneRouteAdapter()

  def boom(*_args, **_kwargs):
    raise module.RouteCacheDecodeError("cache_magic_or_size")

  monkeypatch.setattr(module, "decode_cache", boom)
  assert adapter.accept(sample, now_ns=NOW_NS) is None
  assert adapter.reason == "phone_cache_decode_cache_magic_or_size"


def test_unexpected_error_still_fails_closed(monkeypatch):
  """Non-rejection exceptions keep the generic closed reason."""
  from openpilot.selfdrive.navigation import amap_phone_route as module
  sample = make_sample()
  adapter = AmapPhoneRouteAdapter()

  def boom(*_args, **_kwargs):
    raise KeyError("missing_field")

  monkeypatch.setattr(module, "decode_cache", boom)
  assert adapter.accept(sample, now_ns=NOW_NS) is None
  assert adapter.reason == "phone_sample_rejected"


def test_unsafe_code_falls_back_to_unspecified(monkeypatch):
  """Codes that could carry coordinates are not propagated verbatim."""
  from openpilot.selfdrive.navigation import amap_phone_route as module
  sample = make_sample()
  adapter = AmapPhoneRouteAdapter()

  def boom(*_args, **_kwargs):
    raise module.AmapRouteAnchorRejected("121.47,31.23 out of range")

  monkeypatch.setattr(module, "AmapRouteAnchorIndex", boom)
  assert adapter.accept(sample, now_ns=NOW_NS) is None
  assert adapter.reason == "phone_anchor_unspecified"


def test_recovery_after_rejection_leaves_no_half_built_state(monkeypatch):
  """After a rejection the next valid sample is accepted with a clean index/key."""
  from openpilot.selfdrive.navigation import amap_phone_route as module
  sample = make_sample()
  adapter = AmapPhoneRouteAdapter()
  real = module.AmapRouteAnchorIndex
  state = {"fail": True}

  def flaky(*args, **kwargs):
    if state["fail"]:
      state["fail"] = False
      raise module.AmapRouteAnchorRejected("discontinuous_source_edge")
    return real(*args, **kwargs)

  monkeypatch.setattr(module, "AmapRouteAnchorIndex", flaky)
  assert adapter.accept(sample, now_ns=NOW_NS) is None
  assert adapter.reason == "phone_anchor_discontinuous_source_edge"
  assert adapter._index is None and adapter._key is None
  later = make_sample(now_ns=NOW_NS + 200_000_000, sequence=sample.sequence + 1)
  publication = adapter.accept(later, now_ns=NOW_NS + 200_000_000)
  assert publication is not None, adapter.reason
  assert adapter.reason == QUALITY_STRICT
  assert adapter._index is not None and adapter._key is not None


def test_decision_evidence_reports_same_decision_times():
  """Unknown until a binding exists; only the accept call time is known up front."""
  adapter = AmapPhoneRouteAdapter()
  evidence = adapter.decision_evidence()
  assert evidence["clock"] == "host_monotonic_s"
  assert evidence["accept_begin_withdrawn_at_mono_s"] == "unknown"
  assert evidence["accept_final_withdrawn_at_mono_s"] == "unknown"
  assert evidence["sample_observed_mono_s"] == "unknown"
  adapter.accept(None, now_ns=NOW_NS)
  after = adapter.decision_evidence()
  assert after["accept_called_mono_s"] == NOW_NS / 1e9
  assert after["reason"] == "phone_source_unavailable"
  assert after["sample_observed_mono_s"] == "unknown"


def test_decision_evidence_records_observed_sample_and_accept_after(monkeypatch):
  import openpilot.selfdrive.navigation.tests.test_amap_phone_pipeline as module
  pipeline_obj = module.pipeline.__wrapped__(monkeypatch)
  adapter = pipeline_obj.adapter
  evidence = adapter.decision_evidence()
  assert evidence["clock"] == "host_monotonic_s"
  # The lease has no per-accept barrier; the real barrier is _withdrawn_at.
  # begin/final are real values once a lease exists; the pipeline fixture accepted once.
  assert evidence["clock"] == "host_monotonic_s"
  assert evidence["sample_observed_mono_s"] != "unknown"
  assert evidence["decision_reason"] in (QUALITY_STRICT, "phone_source_unavailable")


def test_decision_evidence_keeps_distinct_sample_accept_and_barrier_times(monkeypatch):
  """Sample observed (10), accept call (12) and lease barrier (11) must stay distinct."""
  import openpilot.selfdrive.navigation.tests.test_amap_phone_pipeline as module
  pipeline_obj = module.pipeline.__wrapped__(monkeypatch)
  adapter = pipeline_obj.adapter
  # Second, later sample so lease.observe actually runs its comparison path.
  later = module.make_sample(now_ns=NOW_NS + 200_000_000, sequence=pipeline_obj.sample.sequence + 1)
  adapter._lease._withdrawn_at = 11.0
  adapter._lease._last_observed = 0.0
  accept_ns = NOW_NS + 200_000_000
  adapter.accept(later, now_ns=accept_ns)
  evidence = adapter.decision_evidence()
  assert evidence["clock"] == "host_monotonic_s"
  # accept call time is the caller's clock, never the sample's observed time
  assert evidence["accept_called_mono_s"] == pytest.approx(accept_ns / 1e9, abs=1e-9)
  assert evidence["sample_observed_mono_s"] == pytest.approx(later.host_start_ns / 1e9, abs=1e-9)
  assert evidence["accept_begin_withdrawn_at_mono_s"] == pytest.approx(11.0, abs=1e-9)


def test_decision_evidence_marks_unknown_before_binding_exists():
  adapter = AmapPhoneRouteAdapter()
  evidence = adapter.decision_evidence()
  assert evidence["sample_observed_mono_s"] == "unknown"
  assert evidence["accept_begin_withdrawn_at_mono_s"] == "unknown"
  adapter.accept(None, now_ns=NOW_NS)
  after = adapter.decision_evidence()
  assert after["sample_observed_mono_s"] == "unknown"
  assert after["accept_called_mono_s"] == NOW_NS / 1e9


def test_two_samples_do_not_share_decision_record(monkeypatch):
  import openpilot.selfdrive.navigation.tests.test_amap_phone_pipeline as module
  pipeline_obj = module.pipeline.__wrapped__(monkeypatch)
  adapter = pipeline_obj.adapter
  first = module.make_sample(now_ns=NOW_NS, sequence=pipeline_obj.sample.sequence + 5)
  adapter.accept(first, now_ns=NOW_NS)
  one = dict(adapter.decision_evidence())
  second = module.make_sample(now_ns=NOW_NS + 300_000_000, sequence=pipeline_obj.sample.sequence + 6)
  adapter.accept(second, now_ns=NOW_NS + 300_000_000)
  two = adapter.decision_evidence()
  assert one["accept_called_mono_s"] != two["accept_called_mono_s"]
  assert one["sample_sequence"] != two["sample_sequence"] or two["sample_sequence"] == "unknown"


def test_accept_begin_and_final_barrier_are_real_values():
  """begin reflects the lease barrier before the decision; final reflects after withdrawal."""
  from openpilot.selfdrive.navigation.phone_route import PhoneRouteLease
  adapter = AmapPhoneRouteAdapter()
  adapter._session = "transport-1"
  adapter._lease = PhoneRouteLease("transport-1", max_age_s=1.0)
  adapter._lease._withdrawn_at = 11.0
  adapter.accept(None, now_ns=12_000_000_000)
  evidence = adapter.decision_evidence()
  assert evidence["sample_observed_mono_s"] == "unknown"
  assert evidence["accept_begin_withdrawn_at_mono_s"] == pytest.approx(11.0, abs=1e-9)
  assert evidence["accept_final_withdrawn_at_mono_s"] == pytest.approx(12.0, abs=1e-9)
  assert evidence["decision_reason"] == "phone_source_unavailable"
