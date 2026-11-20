"""Phone-only mapsd integration using local verified routes and matching frame metadata."""
from dataclasses import replace
import hashlib
from types import SimpleNamespace
import zlib

import numpy as np
import pytest

from openpilot.cereal import log
from openpilot.selfdrive.navigation import mapsd
from openpilot.selfdrive.navigation.nav_map_contract import binding_from_snapshot, route_fields
from openpilot.selfdrive.navigation.phone_route import ActiveRouteBinding, RouteIdentity, SourceProvenance, bind_geometry


POINTS = ((121.0, 31.0), (121.0, 31.001), (121.001, 31.001))


def snapshot_for(revision=1, now=100.0):
  raw = b"complete phone route fixture"
  identity = RouteIdentity("amap", "amap-session-1", "route-1", revision, hashlib.sha256(raw).hexdigest())
  source = SourceProvenance.from_decoded(identity=identity, source_bytes=raw, source_object_id="cache-1",
    decoder_id="fixture-v1", coordinate_system="WGS84", decoded_points=POINTS, expected_point_count=3, complete=True)
  active = ActiveRouteBinding(identity, "transport-1", "cache-1", 1, now, True)
  payload = {"provider": identity.provider, "source_session_id": identity.source_session_id, "route_id": identity.route_id,
             "revision": identity.revision, "cache_sha256": identity.cache_sha256, "coordinate_system": "WGS84",
             "coordinate_order": "lon_lat", "geometry_kind": "full_route", "complete": True, "point_count": 3, "points": POINTS}
  snapshot = bind_geometry(payload, provenance=source, active_binding=active, transport_session_id="transport-1", now=now)
  return snapshot, active


class PhoneSM:
  def __init__(self, now=100.0):
    self.valid = {"navRoute": True, "navInstruction": True, "gpsLocation": True, "gpsLocationExternal": False}
    self.updated = {"navRoute": True, "navInstruction": True}
    self.recv_time = {"navRoute": now, "navInstruction": now, "gpsLocation": now}
    self.logMonoTime = {key: round(value * 1e9) for key, value in self.recv_time.items()}
    self.gps = SimpleNamespace(hasFix=True, longitude=121.0, latitude=31.0002, speed=5.0, bearingDeg=0.0)
    self.replace_route(now=now)

  def replace_route(self, revision=1, now=100.0):
    self.snapshot, self.active = snapshot_for(revision, now)
    self.route = log.NavRoute.new_message(**route_fields(self.snapshot, self.active, now_ns=round(now * 1e9)))
    self.updated["navRoute"] = True
    self.valid["navRoute"] = True
    self.recv_time["navRoute"] = now
    self.logMonoTime["navRoute"] = round(now * 1e9)
    self.refresh_instruction(now)

  def refresh_instruction(self, now, *, renew_observation=True):
    if renew_observation:
      self.active = replace(self.active, observed_sequence=self.active.observed_sequence + 1, observed_mono_time=now)
    # Preserve an intentionally stale observation when testing repeated publication.
    serialized = binding_from_snapshot(self.snapshot, self.active, now_ns=round(self.active.observed_mono_time * 1e9)).to_dict()
    self.instruction = log.NavInstruction.new_message(allManeuvers=[{"distance": 50.0, "type": "turn", "modifier": "right"}],
                                                      routeBinding=serialized)
    self.recv_time["navInstruction"] = now
    self.logMonoTime["navInstruction"] = round(now * 1e9)
    self.recv_time["gpsLocation"] = now
    self.logMonoTime["gpsLocation"] = round(now * 1e9)

  def __getitem__(self, service):
    return {"navRoute": self.route, "navInstruction": self.instruction, "gpsLocation": self.gps}[service]


@pytest.fixture
def setup(monkeypatch):
  clock, phone = [100.0], [True]
  monkeypatch.setattr(mapsd.time, "monotonic", lambda: clock[0])
  monkeypatch.setattr(mapsd.time, "monotonic_ns", lambda: round(clock[0] * 1e9))
  monkeypatch.setattr(mapsd, "cplink_navi_active", lambda: phone[0])
  monkeypatch.setattr(mapsd, "make_renderer", lambda: None)
  return mapsd.MapsD(), PhoneSM(), clock, phone


def test_verified_phone_route_renders_and_retains_double_geometry(setup):
  instance, sm, _, _ = setup
  image, stamp = instance.update(sm)
  assert stamp and image.any()
  assert tuple(map(tuple, instance.route)) == POINTS
  assert instance._phone_route_context.binding.identity == sm.snapshot.identity


def test_legacy_phone_route_without_binding_stays_black(setup):
  instance, sm, _, _ = setup
  sm.route = log.NavRoute.new_message(coordinates=[{"longitude": lon, "latitude": lat} for lon, lat in POINTS])
  image, stamp = instance.update(sm)
  assert stamp == 0 and not image.any()
  assert instance.tracker is None


def test_invalid_route_update_clears_previous_context_before_parsing(setup):
  instance, sm, clock, _ = setup
  assert instance.update(sm)[1]
  clock[0] += 0.1
  sm.route.coordinates64[1].longitude += 0.0001
  sm.recv_time["navRoute"] = clock[0]
  sm.logMonoTime["navRoute"] = round(clock[0] * 1e9)
  sm.refresh_instruction(clock[0])
  image, stamp = instance.update(sm)
  assert stamp == 0 and not image.any()
  assert instance._phone_route_context is None and instance.tracker is None
  sm.updated["navRoute"] = False
  assert instance.update(sm)[1] == 0


def test_identical_geometry_with_new_binding_resets_tracker_and_pose(setup):
  instance, sm, clock, _ = setup
  assert instance.update(sm)[1]
  first_tracker = instance.tracker
  instance.tracker.progress = 160.0
  clock[0] += 0.1
  sm.replace_route(revision=2, now=clock[0])
  image, stamp = instance.update(sm)
  assert stamp and image.any()
  assert instance.tracker is not first_tracker
  assert instance.progress < 30.0


def test_same_route_resend_preserves_tracker_with_fresh_context(setup):
  instance, sm, clock, _ = setup
  assert instance.update(sm)[1]
  tracker = instance.tracker
  clock[0] += 0.1
  sm.recv_time["navRoute"] = clock[0]
  sm.logMonoTime["navRoute"] = round(clock[0] * 1e9)
  sm.refresh_instruction(clock[0])
  assert instance.update(sm)[1]
  assert instance.tracker is tracker


@pytest.mark.parametrize("defect", ["route_cancelled", "instruction_cancelled", "instruction_stale", "observation_stale", "route_mismatch"])
def test_cancellation_or_lease_expiry_immediately_renders_black(setup, defect):
  instance, sm, clock, _ = setup
  assert instance.update(sm)[1]
  sm.updated["navRoute"] = False
  clock[0] += 0.1
  sm.refresh_instruction(clock[0])
  if defect == "route_cancelled":
    sm.valid["navRoute"] = False
    sm.updated["navRoute"] = True
  elif defect == "instruction_cancelled":
    sm.valid["navInstruction"] = False
  elif defect == "instruction_stale":
    clock[0] += 0.6
  elif defect == "observation_stale":
    clock[0] += 3.1
    sm.refresh_instruction(clock[0], renew_observation=False)
  else:
    sm.instruction.routeBinding.routeId = "different-route"
  image, stamp = instance.update(sm)
  assert stamp == 0 and not image.any()


def test_source_switch_discards_cached_phone_geometry_and_old_queued_route(setup):
  instance, sm, clock, phone = setup
  assert instance.update(sm)[1]
  sm.updated["navRoute"] = False
  phone[0] = False
  clock[0] += 0.1
  assert instance.update(sm)[1] == 0
  assert instance._phone_route_context is None
  phone[0] = True
  clock[0] += 0.1
  sm.updated["navRoute"] = True
  sm.refresh_instruction(clock[0])
  image, stamp = instance.update(sm)
  assert stamp == 0 and not image.any()
  sm.replace_route(now=clock[0])
  assert instance.update(sm)[1]


def test_phone_raw_gps_offroute_gate_runs_before_tracker_progress(setup):
  instance, sm, _, _ = setup
  sm.gps.longitude = 121.01
  image, stamp = instance.update(sm)
  assert stamp == 0 and not image.any()
  assert instance.tracker.progress == -np.inf


def test_publish_sidecar_matches_exact_visionipc_bytes_and_timestamps(setup):
  instance, sm, clock, _ = setup
  image, stamp = instance.update(sm)
  sent, events = [], []
  server = SimpleNamespace(send=lambda *args: sent.append(args))
  pm = SimpleNamespace(send=lambda name, msg: events.append((name, msg)))
  clock[0] += 0.01
  instance.publish_frame(server, pm, image, stamp, frame_id=42, renderer_instance_id="renderer-1")
  _, pixels, frame_id, sof, eof = sent[0]
  metadata = events[0][1].navMapFrameSP
  assert events[0][0] == "navMapFrameSP"
  assert metadata.valid
  assert metadata.frameId == frame_id == 42
  assert metadata.timestampSof == sof == stamp
  assert metadata.timestampEof == eof == round(clock[0] * 1e9)
  assert metadata.rendererInstanceId == "renderer-1"
  assert metadata.imageCrc32 == zlib.crc32(pixels)
  assert metadata.routeBinding.bindingId == sm.snapshot.binding_id
  assert metadata.hasProjectionEvidence and metadata.rawGpsLateralM < 1e-6


def test_publish_rechecks_lease_after_slow_render_and_sends_black(setup):
  instance, sm, clock, _ = setup
  image, stamp = instance.update(sm)
  sent, events = [], []
  clock[0] += 0.6
  instance.publish_frame(SimpleNamespace(send=lambda *args: sent.append(args)),
                         SimpleNamespace(send=lambda name, msg: events.append(msg)), image, stamp,
                         frame_id=42, renderer_instance_id="renderer-1")
  assert not any(sent[0][1]) and sent[0][3] == 0
  assert not events[0].navMapFrameSP.valid
  assert events[0].navMapFrameSP.imageCrc32 == zlib.crc32(sent[0][1])


def test_unknown_gps_heading_is_explicit_and_does_not_disable_map(setup):
  instance, sm, _, _ = setup
  sm.gps.speed = 0.0
  sm.gps.bearingDeg = float("nan")
  image, stamp = instance.update(sm)
  assert stamp and image.any()
  sent = []
  instance.publish_frame(SimpleNamespace(send=lambda *args: None), SimpleNamespace(send=lambda name, msg: sent.append(msg)),
                         image, stamp, frame_id=42, renderer_instance_id="renderer-1")
  assert sent[0].navMapFrameSP.valid
  assert not sent[0].navMapFrameSP.rawGpsHeadingValid


def test_source_change_between_update_and_publish_never_emits_old_phone_image(setup):
  instance, sm, _, phone = setup
  image, stamp = instance.update(sm)
  assert image.any()
  phone[0] = False
  sent, events = [], []
  instance.publish_frame(SimpleNamespace(send=lambda *args: sent.append(args)),
                         SimpleNamespace(send=lambda name, msg: events.append(msg)), image, stamp,
                         frame_id=42, renderer_instance_id="renderer-1")
  assert sent[0][3] == 0 and not any(sent[0][1])
  assert not events[0].navMapFrameSP.valid
  assert events[0].navMapFrameSP.reason == "source_changed_before_publish"


@pytest.mark.parametrize("position_stamp", [97_000_000_000, 101_000_000_000])
def test_publish_does_not_mask_stale_or_future_gps_with_fresh_render_time(setup, position_stamp):
  instance, sm, _, _ = setup
  image, _ = instance.update(sm)
  sent, events = [], []
  instance.publish_frame(SimpleNamespace(send=lambda *args: sent.append(args)),
                         SimpleNamespace(send=lambda name, msg: events.append(msg)), image, position_stamp,
                         frame_id=42, renderer_instance_id="renderer-1")
  assert not any(sent[0][1]) and sent[0][3] == 0
  assert events[0].navMapFrameSP.reason == "position_stale_or_future"


def test_native_path_keeps_existing_image_and_does_not_publish_phone_sidecar(setup):
  instance, sm, _, phone = setup
  phone[0] = False
  sm.route.coordinates = [{"longitude": lon, "latitude": lat} for lon, lat in POINTS]
  image, stamp = instance.update(sm)
  assert stamp and image.any()
  sent, events = [], []
  instance.publish_frame(SimpleNamespace(send=lambda *args: sent.append(args)),
                         SimpleNamespace(send=lambda name, msg: events.append(msg)), image, stamp,
                         frame_id=42, renderer_instance_id="renderer-1")
  assert not events
  assert sent[0][1] == image.tobytes() and sent[0][3] == stamp
