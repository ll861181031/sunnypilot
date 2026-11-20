"""Bounded diagnosis for selection_changed_during_read; no real ADB access."""
import json

import pytest

from APP.navdiag import route_snapshot as rs


def fake_device(payloads, epochs):
  """Serve scripted reads/epochs; raises if more reads are requested than scripted."""
  reads, epoch_iter = iter(payloads), iter(epochs)
  calls = {"read": 0}

  def read(_device, _path):
    calls["read"] += 1
    try:
      return next(reads)
    except StopIteration:
      raise AssertionError("unexpected extra read") from None

  def epoch(_device):
    try:
      return next(epoch_iter)
    except StopIteration:
      raise AssertionError("unexpected extra epoch") from None

  def listing(_device):
    return ("a.dat",)

  return read, epoch, listing, calls


def run_gather(payloads, epochs, *, listing_names=("a.dat",)):
  read, epoch, listing, calls = fake_device(payloads, epochs)

  def scripted_listing(_device):
    return listing_names

  with pytest.raises(ValueError) as caught:
    rs.gather(object(), read=read, listing=scripted_listing, epoch=epoch)
  return caught.value, calls


def test_stage_a_metadata_change_records_both_hashes():
  before = json.dumps({"pathId": 1, "state": 1}).encode()
  after = json.dumps({"pathId": 1, "state": 2}).encode()
  error, calls = run_gather([before, b"cache", after], ["e1", "e1"])
  assert str(error) == "selection_changed_during_read"
  diag = error.diagnosis
  assert diag["stage"] == "A"
  assert diag["metadata_changed"] is True
  assert diag["metadata_before_sha256"] != diag["metadata_after_sha256"]
  assert diag["epoch_changed"] == "unknown"
  assert diag["json"]["status"] == "parsed"
  assert any(entry["path"].endswith("/state") for entry in diag["json"]["changed"])
  assert calls["read"] == 3


def test_stage_b_epoch_short_circuit_leaves_metadata_unknown():
  before = json.dumps({"pathId": 1}).encode()
  # reads: metadata, cache, metadata(A), cache(:142), then epoch(:144) short-circuits
  error, _ = run_gather([before, b"cache", before, b"cache"], ["e1", "e1", "e2"])
  assert str(error) == "selection_changed_during_read"
  diag = error.diagnosis
  assert diag["stage"] == "B"
  assert diag["epoch_changed"] is True
  assert diag["metadata_read"] is False
  assert diag["metadata_after_sha256"] is None
  assert diag["json"] == "unknown"


def test_stage_b_metadata_change_with_stable_epoch():
  before = json.dumps({"pathId": 1}).encode()
  after = json.dumps({"pathId": 2}).encode()
  error, _ = run_gather([before, b"cache", before, b"cache", after], ["e1", "e1", "e1"])
  diag = error.diagnosis
  assert diag["stage"] == "B"
  assert diag["epoch_changed"] is False
  assert diag["metadata_changed"] is True


def test_unparsed_metadata_reports_type_only():
  error, _ = run_gather([b"not-json", b"cache", b"also-not-json"], ["e1", "e1"])
  diag = error.diagnosis
  assert diag["json"]["status"] == "unparsed"
  assert "error" in diag["json"]
  assert "not-json" not in json.dumps(diag)


def test_diagnosis_is_bounded_and_never_leaks_raw_strings():
  before = {"pathId": 1, "name": "secret-place", "nested": {"deep": {"deeper": {"x": 1}}},
            **{f"k{i}": i for i in range(50)}}
  after = dict(before, name="other-place", pathId=2)
  error, _ = run_gather([json.dumps(before).encode(), b"cache", json.dumps(after).encode()],
                        ["e1", "e1"])
  diag = error.diagnosis
  text = json.dumps(diag)
  assert "secret-place" not in text and "other-place" not in text
  assert len(diag["json"]["changed"]) <= rs.SELECTION_DIAG_MAX_KEYS
  assert diag["stage"] == "A"


def test_diagnosis_failure_never_masks_reason(monkeypatch):
  before = b'{"a": 1}'
  after = b'{"a": 2}'
  def boom(*_a, **_k):
    raise RuntimeError("boom")

  monkeypatch.setattr(rs, "selection_diagnosis", boom)
  error, _ = run_gather([before, b"cache", after], ["e1", "e1"])
  assert str(error) == "selection_changed_during_read"
  assert error.diagnosis["status"] == "diagnosis_failed"


def test_other_rejections_keep_their_original_reason():
  error, _ = run_gather([b"{}", b"cache", b"{}"], ["e1", "e2"])
  assert str(error) == "provider_or_cache_set_changed"
  assert not hasattr(error, "diagnosis")


def test_diagnosis_reaches_producer_status_without_changing_reason():
  """The exact failing read's diagnosis survives into status; the safe reason is untouched."""
  from APP.navdiag.phone_route_producer import PhoneRouteProducer, _reason
  error, _ = run_gather([b'{"a": 1}', b"cache", b'{"a": 2}'], ["e1", "e1"])
  assert _reason(error) == "selection_changed_during_read"
  producer = PhoneRouteProducer(port=5039)
  producer._last_rejection_diagnosis = getattr(error, "diagnosis", None)
  status = producer.status()
  assert status.rejection_diagnosis["stage"] == "A"
  assert status.reason != "phone_producer_source_failed"


def test_producer_keeps_chain_when_diagnosis_absent():
  from APP.navdiag.phone_route_producer import PhoneRouteProducer
  producer = PhoneRouteProducer(port=5039)
  assert producer.status().rejection_diagnosis is None
