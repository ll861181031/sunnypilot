import hashlib
import json

import route_lifecycle as lifecycle
import route_snapshot as snapshot


def test_empty_window_records_absence_without_claiming_active():
  report, files = snapshot.gather_window(None, listing=lambda _: (), epoch=lambda _: "epoch")
  assert not files
  assert report["read_window_stable"]
  assert not report["active_guidance_verified"]


def test_lifecycle_keeps_exact_bytes_and_deduplicates(tmp_path):
  tick = [0.0]
  data = b"\xff\x00\n\r\x00A\n"
  digest = hashlib.sha256(data).hexdigest()

  def gather(_):
    return {"files": [{"source": snapshot.ALLOWED_PATH, "sha256": digest, "bytes": len(data)}]}, {snapshot.ALLOWED_PATH: data}

  def sleep(seconds):
    tick[0] += seconds

  out = tmp_path / "lifecycle"
  report = lifecycle.collect(None, out, 5, gather=gather, elapsed=lambda _: 123,
                             clock=lambda: tick[0], sleep=sleep)
  assert report["samples"] == 3
  assert report["unique_blobs"] == 1
  assert (out / ("blob-" + digest + ".bin")).read_bytes() == data
  assert (out.stat().st_mode & 0o777) == 0o700
  assert all(path.stat().st_mode & 0o777 == 0o600 for path in out.iterdir())
  rows = [json.loads(line) for line in (out / "observations.jsonl").read_text().splitlines()]
  assert all(row["status"] == "stable" for row in rows)


def test_persistent_paths_are_fixed_and_not_arbitrary():
  assert snapshot.allowed_path(snapshot.SELECTION_PATH)
  assert snapshot.allowed_path(snapshot.PERSIST_DIRECTORY + "/123.dat")
  assert not snapshot.allowed_path(snapshot.PERSIST_DIRECTORY + "/../secret.dat")
  assert not snapshot.allowed_path(snapshot.PERSIST_DIRECTORY + "/credentials.json")


def test_directory_disappearance_invalidates_window():
  import pytest
  names = iter([(snapshot.ALLOWED_PATH,), ()])
  with pytest.raises(ValueError, match="provider_or_cache_set_changed"):
    snapshot.gather_window(None, read=lambda *_: b"metadata", listing=lambda _: next(names), epoch=lambda _: "epoch")
