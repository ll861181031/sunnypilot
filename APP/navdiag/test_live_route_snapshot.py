import hashlib
import json
from contextlib import contextmanager

import pytest

from amap_memory_probe import identity_hash
import live_route_snapshot as live
import native_route_source
from route_snapshot import CACHE_DIRECTORY
from tools.cplink_shim.tests.test_amap_route_cache import cache


def fixture():
  epoch = "123:456"
  raw = cache()
  path = CACHE_DIRECTORY + "/123.dat"
  before = {"status": "observed", "sample_consistent": True, "gps_navigation_observed": True,
            "navi_type_raw": 0, "navi_state_raw": 1, "process_epoch_hash": hashlib.sha256(epoch.encode()).hexdigest(),
            "path_identity_hash": identity_hash(123, epoch),
            "observation_started_monotonic_ns": 1_000_000_000, "observation_finished_monotonic_ns": 1_100_000_000}
  after = {**before, "observation_started_monotonic_ns": 1_300_000_000, "observation_finished_monotonic_ns": 1_400_000_000}
  snapshot = {"read_window_stable": True, "source_app_session_id": "a" * 64,
              "read_started_mono_ns": 1_200_000_000, "read_finished_mono_ns": 1_250_000_000,
              "files": [{"source": path, "sha256": hashlib.sha256(raw).hexdigest()}]}
  return before, snapshot, {path: raw}, after, epoch


def associate(data):
  before, snapshot, files, after, epoch = data
  return live.associate(before, snapshot, files, after, epoch=epoch)


def test_native_path_id_binds_exact_decoded_route_but_not_map_readiness():
  selected = associate(fixture())
  assert selected.route.path_id == 123
  summary = selected.summary()
  assert summary["route_identity_verified"] and summary["complete_route_verified"]
  assert summary["cache_route_index"] == 0
  assert not summary["map_input_ready"] and not summary["coordinate_datum_verified"]


def native_fixture():
  epoch = "123:456"
  geometry_hash = "f" * 64
  value = {
    "status": "observed", "geometry_kind": "current_native_path_window",
    "source_geometry_sha256": geometry_hash, "path_identity_hash": identity_hash(123, epoch),
    "epoch_sha256": hashlib.sha256(epoch.encode()).hexdigest(),
    "point_count": 3, "segment_count": 1, "link_count": 1,
    "points_native": ((10_000_000, 20_000_000), (10_000_100, 20_000_000),
                      (10_000_100, 20_000_100)),
    "segments": (((0, 3),),), "main_index": 0, "path_count": 1,
    "navi_type_raw": 0, "navi_state_raw": 1,
    "host_observation_clock_domain": "host_monotonic",
    "host_observation_started_monotonic_ns": 1_000_000_000,
    "host_observation_finished_monotonic_ns": 1_100_000_000,
    "complete_current_path_window_verified": True, "active_route_verified": False,
    "coordinate_frame": "amap_native_map", "coordinate_datum_verified": False,
    "control_input_allowed": False,
  }
  return value, epoch


def test_native_window_binds_current_path_without_claiming_destination_coverage():
  value, epoch = native_fixture()
  selected = live.select_native(value, epoch=epoch)
  assert selected.identity_hash == value["path_identity_hash"]
  assert selected.source_geometry_sha256 == "f" * 64
  assert selected.cache_sha256 == "f" * 64
  assert selected.geometry_kind == "current_native_path_window"
  assert selected.complete_current_path_window_verified is True
  assert selected.complete_route_verified is False
  summary = selected.summary()
  assert summary["complete_current_path_window_verified"] is True
  assert summary["complete_route_verified"] is False
  assert summary["points"] == 3 and summary["links"] == 1
  assert summary["map_input_ready"] is False and summary["control_input_allowed"] is False


def native_observation(geometry):
  return {
    "status": "observed", "sample_consistent": True, "gps_navigation_observed": True,
    "navi_type_raw": 0, "navi_state_raw": 1,
    "process_epoch_hash": geometry["epoch_sha256"],
    "path_identity_hash": geometry["path_identity_hash"],
    "observation_started_monotonic_ns": 900_000_000,
    "observation_finished_monotonic_ns": 950_000_000,
  }


class NativeSource:
  def __init__(self, observation, geometry):
    self.epoch = "123:456"
    self.observation = observation
    self.geometry = geometry
    self.calls = []

  def observe(self):
    self.calls.append("observe")
    return self.observation

  def observe_geometry(self):
    self.calls.append("observe_geometry")
    return self.geometry


def test_collect_native_uses_current_route_identity_without_cache_discovery(monkeypatch):
  geometry, _ = native_fixture()
  source = NativeSource(native_observation(geometry), geometry)
  monkeypatch.setattr(live, "gather_window", lambda _device: pytest.fail("cache scan must not run"))
  selected, evidence, files = live.collect_native(source)
  assert source.calls == ["observe", "observe_geometry"]
  assert selected.identity_hash == geometry["path_identity_hash"]
  assert evidence == {"observation": source.observation, "geometry": geometry,
                      "provider_epoch": source.epoch}
  assert files == {}


def test_collect_native_rejects_selected_route_change_between_reads():
  geometry, _ = native_fixture()
  observation = native_observation(geometry)
  observation["path_identity_hash"] = identity_hash(456, "123:456")
  with pytest.raises(live.LiveRouteRejected, match="native_selected_identity_changed"):
    live.collect_native(NativeSource(observation, geometry))


@pytest.mark.parametrize("field,value", [
  ("geometry_kind", "full_route"), ("complete_current_path_window_verified", False),
  ("active_route_verified", True), ("coordinate_frame", "wgs84"),
  ("coordinate_datum_verified", True), ("control_input_allowed", True),
  ("source_geometry_sha256", "not-a-hash"), ("path_identity_hash", "not-a-hash"),
])
def test_native_window_rejects_relabelled_or_unsafe_claims(field, value):
  geometry, epoch = native_fixture()
  geometry[field] = value
  with pytest.raises(live.LiveRouteRejected, match="native_geometry_identity_invalid"):
    live.select_native(geometry, epoch=epoch)


@pytest.mark.parametrize("field,value", [
  ("point_count", 2), ("segment_count", 2), ("link_count", 2),
])
def test_native_window_shape_must_match_the_actual_claims(field, value):
  geometry, epoch = native_fixture()
  geometry[field] = value
  with pytest.raises(live.LiveRouteRejected, match="native_geometry_shape_invalid"):
    live.select_native(geometry, epoch=epoch)


@pytest.mark.parametrize("change", [{"gps_navigation_observed": False}, {"navi_type_raw": 1},
  {"navi_state_raw": 2}, {"navi_state_raw": 4}, {"sample_consistent": False}, {"status": "unavailable"}])
def test_non_active_or_simulated_state_never_selects_cache(change):
  data = fixture()
  data[3].update(change)
  with pytest.raises(live.LiveRouteRejected, match="gps_navigation_not_observed"):
    associate(data)


def test_same_distance_without_same_path_id_is_not_selection_proof():
  data = fixture()
  for report in (data[0], data[3]):
    report["path_identity_hash"] = identity_hash(456, data[4])
  with pytest.raises(live.LiveRouteRejected, match="selected_cache_missing_or_ambiguous"):
    associate(data)


def test_selected_path_change_invalidates_bracketed_cache():
  data = fixture()
  data[3]["path_identity_hash"] = identity_hash(456, data[4])
  with pytest.raises(live.LiveRouteRejected, match="main_route_changed_during_read"):
    associate(data)


def test_provider_restart_invalidates_selection():
  data = fixture()
  data[3]["process_epoch_hash"] = "b" * 64
  with pytest.raises(live.LiveRouteRejected, match="provider_epoch_mismatch"):
    associate(data)


def test_modified_cache_is_rejected():
  data = fixture()
  path = next(iter(data[2]))
  data[2][path] += b"x"
  with pytest.raises(live.LiveRouteRejected, match="cache_hash_mismatch"):
    associate(data)


def test_long_read_window_is_rejected_instead_of_retimed():
  data = fixture()
  data[3]["observation_finished_monotonic_ns"] += live.MAX_OBSERVATION_NS
  with pytest.raises(live.LiveRouteRejected, match="observation_window_too_long"):
    associate(data)


def test_native_phone_clock_requires_explicit_host_brackets():
  data = fixture()
  for observation in (data[0], data[3]):
    observation["observation_clock_domain"] = "phone_elapsed_realtime"
  with pytest.raises(live.LiveRouteRejected, match="unsupported_observation_clock"):
    associate(data)
  for observation in (data[0], data[3]):
    observation["host_observation_clock_domain"] = "host_monotonic"
    for part in ("started", "finished"):
      name = "observation_" + part + "_monotonic_ns"
      observation["host_" + name] = observation[name]
      observation[name] += 99_000_000_000
  assert associate(data).summary()["route_identity_verified"]


def test_expected_collection_error_still_writes_private_failure_report(tmp_path, monkeypatch, capsys):
  output = tmp_path / "result"

  @contextmanager
  def device(*args, **kwargs):
    yield object()

  def source(*args):
    raise ValueError("private command arguments must not leak")

  monkeypatch.setattr(live, "Device", device)
  monkeypatch.setattr(live, "choose_adb", lambda value: "unused")
  monkeypatch.setattr(live, "NativeMemorySource", source)
  monkeypatch.setattr("sys.argv", ["live_route_snapshot.py", "--out", str(output)])
  live.main()
  report = json.loads((output / "report.json").read_bytes())
  assert report["reason"] == "ValueError"
  assert not report["route_identity_verified"] and not report["map_input_ready"]
  assert (output / "report.json").stat().st_mode & 0o777 == 0o600
  assert "private command" not in capsys.readouterr().out


@pytest.mark.parametrize("argument,expected", [
  (["--adb-ownership", "attach"], "attach"),
  ([], "dedicated"),
])
def test_cli_passes_adb_ownership_to_diagnostic_device(tmp_path, monkeypatch, argument, expected):
  output = tmp_path / "result"
  seen = {}

  @contextmanager
  def device(*args, **kwargs):
    seen["args"] = args
    seen["kwargs"] = kwargs
    yield object()

  def source(*args):
    raise ValueError("adb_server_unavailable")

  monkeypatch.setattr(live, "Device", device)
  monkeypatch.setattr(live, "choose_adb", lambda value: "unused")
  monkeypatch.setattr(live, "NativeMemorySource", source)
  monkeypatch.setattr("sys.argv", ["live_route_snapshot.py", "--port", "5039", "--serial", "phone",
                                   "--out", str(output), *argument])
  live.main()
  assert seen["args"] == ("unused", 5039, "phone")
  assert seen["kwargs"] == {"ownership": expected}


def test_native_cli_writes_private_evidence_without_coordinates_in_public_report(tmp_path, monkeypatch):
  output = tmp_path / "native"
  geometry, _ = native_fixture()
  observation = native_observation(geometry)
  seen = {}

  @contextmanager
  def device(*args, **kwargs):
    seen["device_args"], seen["device_kwargs"] = args, kwargs
    yield object()

  class Source(NativeSource):
    def __init__(self, _device):
      super().__init__(observation, geometry)

    def __enter__(self):
      return self

    def __exit__(self, *_):
      return None

  def no_cache(_device):
    raise AssertionError("native CLI must not scan cache files")

  monkeypatch.setattr(live, "Device", device)
  monkeypatch.setattr(live, "choose_adb", lambda value: "unused")
  monkeypatch.setattr(live, "gather_window", no_cache)
  monkeypatch.setattr(native_route_source, "NativeRouteProbeSource", Source)
  monkeypatch.setattr("sys.argv", ["live_route_snapshot.py", "--backend", "native",
                                   "--adb-ownership", "attach", "--port", "5039",
                                   "--serial", "phone", "--out", str(output)])
  live.main()

  evidence = json.loads((output / "evidence.json").read_bytes())
  report = json.loads((output / "report.json").read_bytes())
  assert evidence["geometry"]["points_native"] == [list(point) for point in geometry["points_native"]]
  assert not {"points_native", "route", "geometry"} & report.keys()
  assert isinstance(report["segments"], int) and report["segments"] == 1
  assert report["route_identity_verified"] is True
  assert report["complete_current_path_window_verified"] is True
  assert report["complete_route_verified"] is False
  assert (output / "evidence.json").stat().st_mode & 0o777 == 0o600
  assert (output / "report.json").stat().st_mode & 0o777 == 0o600
  assert seen["device_args"] == ("unused", 5039, "phone")
  assert seen["device_kwargs"] == {"ownership": "attach"}
