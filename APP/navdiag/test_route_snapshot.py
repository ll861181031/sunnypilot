import pytest

import route_snapshot as snapshot


CACHE = snapshot.CACHE_DIRECTORY + "/123.dat"


def fixture():
  return {snapshot.ALLOWED_PATH: b"synthetic-metadata", CACHE: b"\xff\x00synthetic-cache"}


def test_stable_snapshot_preserves_bytes_without_claiming_current_route():
  data = fixture()
  report, files = snapshot.gather(None, read=lambda _, name: data[name], listing=lambda _: (CACHE,), epoch=lambda _: "epoch")
  assert files == data
  assert report["read_window_stable"]
  assert not report["route_identity_verified"]
  assert not report["active_guidance_verified"]
  assert not report["complete_route_verified"]


@pytest.mark.parametrize("path", ["/etc/passwd", snapshot.CACHE_DIRECTORY + "/../123.dat",
  snapshot.CACHE_DIRECTORY + "/123.dat;id", snapshot.CACHE_DIRECTORY + "/name.dat", ""])
def test_only_known_navigation_file_paths_are_accepted(path):
  assert not snapshot.allowed_path(path)


def test_metadata_changes_reject_entire_snapshot():
  data = fixture()
  reads = [0]

  def read(_, name):
    reads[0] += name == snapshot.ALLOWED_PATH
    return b"changed-selection" if name == snapshot.ALLOWED_PATH and reads[0] >= 2 else data[name]

  with pytest.raises(ValueError, match="selection_changed"):
    snapshot.gather(None, read=read, listing=lambda _: (CACHE,), epoch=lambda _: "epoch")


def test_same_size_cache_replacement_is_detected():
  data = fixture()
  reads = [0]

  def read(_, name):
    reads[0] += name == CACHE
    return bytes(value ^ 1 for value in data[name]) if name == CACHE and reads[0] >= 2 else data[name]

  with pytest.raises(ValueError, match="cache_changed"):
    snapshot.gather(None, read=read, listing=lambda _: (CACHE,), epoch=lambda _: "epoch")


def test_provider_restart_invalidates_snapshot():
  epochs = iter(["before", "after"])
  data = fixture()
  with pytest.raises(ValueError, match="provider_or_cache_set_changed"):
    snapshot.gather(None, read=lambda _, name: data[name], listing=lambda _: (CACHE,), epoch=lambda _: next(epochs))


def test_new_cache_appearing_during_read_invalidates_snapshot():
  lists = iter([(CACHE,), (CACHE, snapshot.CACHE_DIRECTORY + "/456.dat")])
  data = fixture()
  with pytest.raises(ValueError, match="provider_or_cache_set_changed"):
    snapshot.gather(None, read=lambda _, name: data[name], listing=lambda _: next(lists), epoch=lambda _: "epoch")
