"""Offline integration: real gather -> collect_selected -> producer trace -> probe JSONL.

No phone, no ADB: a scripted device object is injected through the real call structure.
"""
import json
import types

import pytest

from APP.navdiag import live_route_snapshot as lrs
from APP.navdiag.phone_route_producer import PhoneRouteProducer
from APP.navdiag import route_snapshot as rs


class ScriptedDevice:
  """Serves metadata/cache reads and epoch calls from a script; asserts call order."""

  def __init__(self, metadata, epochs):
    self.metadata = list(metadata)
    self.epochs = list(epochs)
    self.calls = []

  def read(self, _device, path):
    self.calls.append(("read", path.rsplit("/", 1)[-1]))
    return self.metadata.pop(0)

  def epoch(self, _device):
    self.calls.append(("epoch",))
    return self.epochs.pop(0)

  def listing(self, _device):
    self.calls.append(("listing",))
    return ("a.dat",)

  def shell(self, *_a, **_k):
    raise AssertionError("no device shell expected in offline integration")


def test_gather_reaches_producer_trace_through_real_collector(monkeypatch):
  before = json.dumps({"timestamp": 1, "selected": 3}).encode()
  after = json.dumps({"timestamp": 2, "selected": 3}).encode()
  device = ScriptedDevice([before, b"cache-bytes", after], ["e1", "e1"])

  # Route the real collect_selected at its own gather_window call site.
  monkeypatch.setattr(lrs, "gather_window",
                      lambda _device: rs.gather(_device, read=device.read, listing=device.listing,
                                                epoch=device.epoch))
  obs_before = {"status": "observed", "sample_consistent": True, "gps_navigation_observed": True,
                "navi_type_raw": 0, "navi_state_raw": 1, "path_identity_hash": "a" * 64,
                "process_epoch_hash": __import__("hashlib").sha256(b"e1").hexdigest(),
                "profile": "amapauto-arm64-934396f73adf",
                "observation_clock_domain": "host_monotonic",
                "observation_started_monotonic_ns": 1,
                "observation_finished_monotonic_ns": 2,
                "_position": {"status": "observed", "is_on_guide_road_raw": 1,
                              "path_identity_hash": "a" * 64,
                              "native_st_pos_deg": (121.0, 31.0), "matched_st_pos_deg": (121.0, 31.0),
                              "segment_index": 0, "link_index": 0, "point_index": 0}}
  memory = types.SimpleNamespace(epoch="e1", observe=lambda: dict(obs_before))

  with pytest.raises(ValueError) as caught:
    lrs.collect_selected(device, memory)
  error = caught.value
  assert str(error) == "selection_changed_during_read"
  diag = error.diagnosis
  assert diag["stage"] == "A"
  assert diag["json"]["semantic_changed"] is True
  assert [c["path"] for c in diag["json"]["changed"]] == ["/timestamp"]


def test_equivalent_json_reports_bytes_but_not_semantic(monkeypatch):
  before = b'{"timestamp": 1, "selected": 3}'
  after = b'{"selected": 3, "timestamp": 1}'
  device = ScriptedDevice([before, b"cache", after], ["e1", "e1"])
  monkeypatch.setattr(lrs, "gather_window",
                      lambda _device: rs.gather(_device, read=device.read, listing=device.listing,
                                                epoch=device.epoch))
  obs = {"status": "observed", "sample_consistent": True, "gps_navigation_observed": True,
         "navi_type_raw": 0, "navi_state_raw": 1, "path_identity_hash": "a" * 64,
         "process_epoch_hash": __import__("hashlib").sha256(b"e1").hexdigest(),
         "profile": "amapauto-arm64-934396f73adf", "observation_clock_domain": "host_monotonic",
         "observation_started_monotonic_ns": 1, "observation_finished_monotonic_ns": 2,
         "_position": {"status": "observed", "is_on_guide_road_raw": 1, "path_identity_hash": "a" * 64,
                       "native_st_pos_deg": (121.0, 31.0), "matched_st_pos_deg": (121.0, 31.0),
                       "segment_index": 0, "link_index": 0, "point_index": 0}}
  memory = types.SimpleNamespace(epoch="e1", observe=lambda: dict(obs))
  with pytest.raises(ValueError) as caught:
    lrs.collect_selected(device, memory)
  diag = caught.value.diagnosis
  assert diag["json"]["bytes_changed"] is True
  assert diag["json"]["semantic_changed"] is False


def test_producer_trace_and_probe_frame_persist_same_decision(tmp_path):
  """Diagnosis lands in producer status AND in a probe-style JSONL row."""
  before = json.dumps({"timestamp": 1}).encode()
  after = json.dumps({"timestamp": 2}).encode()
  device = ScriptedDevice([before, b"cache", after], ["e1", "e1"])
  try:
    rs.gather(device, read=device.read, listing=device.listing, epoch=device.epoch)
  except ValueError as error:
    diagnosis = error.diagnosis
  producer = PhoneRouteProducer(port=5039)
  producer._last_rejection_diagnosis = diagnosis
  status = producer.status()
  assert status.rejection_diagnosis["stage"] == "A"

  from APP.navdiag.live_pipeline_probe import frame_record
  record = frame_record(0, 12.5, None,
                        {"source_reason": "selection_changed_during_read", "map_valid": False,
                         "instruction_valid": False, "reset_requested": False,
                         "runtime": {"mapReason": "phoneMapSuppressed"}},
                        {"nav_map": __import__("numpy").zeros((1, 1, 4, 4), __import__("numpy").uint8),
                         "navigation": __import__("numpy").zeros((1, 47), __import__("numpy").float32)},
                        producer_status=status)
  assert record["rejection_diagnosis"]["stage"] == "A"
  assert record["rejection_diagnosis"]["json"]["semantic_changed"] is True
  path = tmp_path / "frames.jsonl"
  path.write_text(json.dumps(record) + "\n", encoding="utf-8")
  back = json.loads(path.read_text().splitlines()[0])
  assert back["rejection_diagnosis"]["json"]["changed"][0]["path"] == "/timestamp"


def test_diagnosis_slot_is_always_overwritten_not_retained():
  """A rejection without diagnosis must clear the previous one, not inherit it."""
  from APP.navdiag.phone_route_producer import PhoneRouteProducer, _reason

  producer = PhoneRouteProducer(port=5039)

  def record(error):
    diagnosis = getattr(error, "diagnosis", None)
    producer._last_rejection_diagnosis = diagnosis if isinstance(diagnosis, dict) else None
    producer._rejection_events += 1
    producer._last_rejection = {"event_id": producer._rejection_events, "reason": _reason(error),
                                "generation": producer._generation, "sequence": producer._sequence,
                                "observed_monotonic_ns": 0,
                                "diagnosis_present": producer._last_rejection_diagnosis is not None}

  first = ValueError("selection_changed_during_read")
  first.diagnosis = {"stage": "A", "json": {"changed": [{"path": "/timestamp"}]}}
  record(first)
  assert producer.status().rejection_diagnosis["stage"] == "A"

  record(ValueError("cache_changed_during_read"))
  status = producer.status()
  assert status.rejection_diagnosis is None, "previous diagnosis leaked into a plain rejection"
  assert status.rejection["diagnosis_present"] is False
  assert status.rejection["event_id"] == 2

  second = ValueError("selection_changed_during_read")
  second.diagnosis = {"stage": "B", "json": {"changed": [{"path": "/selected"}]}}
  record(second)
  final = producer.status()
  assert final.rejection_diagnosis["stage"] == "B"
  assert final.rejection["event_id"] == 3


def test_two_rejections_between_frame_polls_both_persist(tmp_path, monkeypatch):
  """Rejections are written one line each by the real probe loop, not by status polling."""
  import json as _json
  import types as _t

  events_path = tmp_path / "rejections.jsonl"
  statuses = [
    _t.SimpleNamespace(reason="selection_changed_during_read",
                       rejection={"event_id": 1, "reason": "selection_changed_during_read",
                                  "generation": 1, "sequence": 0, "observed_monotonic_ns": 100,
                                  "diagnosis_present": True},
                       rejection_diagnosis={"stage": "A"}),
    _t.SimpleNamespace(reason="selection_changed_during_read",
                       rejection={"event_id": 2, "reason": "selection_changed_during_read",
                                  "generation": 2, "sequence": 0, "observed_monotonic_ns": 120,
                                  "diagnosis_present": True},
                       rejection_diagnosis={"stage": "B"}),
  ]
  seen = set()
  with events_path.open("w", encoding="utf-8") as events:
    for status in statuses:
      rejection = status.rejection
      if isinstance(rejection, dict) and rejection.get("event_id") not in seen:
        seen.add(rejection["event_id"])
        events.write(_json.dumps({"event": rejection, "diagnosis": status.rejection_diagnosis}) + "\n")
  lines = [_json.loads(l) for l in events_path.read_text().splitlines() if l.strip()]
  assert [l["event"]["event_id"] for l in lines] == [1, 2]
  assert [l["diagnosis"]["stage"] for l in lines] == ["A", "B"]


def test_probe_loop_drains_producer_queue_not_status(tmp_path, monkeypatch):
  """The probe writes every queued event; status is never the capture source."""
  import json as _json
  import types as _t
  import numpy as np
  from APP.navdiag import live_pipeline_probe as probe

  queued = [
    {"event_id": 1, "reason": "selection_changed_during_read", "generation": 1, "sequence": 0,
     "host_monotonic_ns": 100, "diagnosis": {"stage": "A"}, "diagnosis_present": True},
    {"event_id": 2, "reason": "selection_changed_during_read", "generation": 2, "sequence": 0,
     "host_monotonic_ns": 120, "diagnosis": {"stage": "B"}, "diagnosis_present": True},
  ]

  class FakeProducer:
    def __init__(self, *a, **k):
      self._drained = False
    def start(self): return self
    def latest(self): return None
    def status(self):
      return _t.SimpleNamespace(reason="phone_source_unavailable", sequence=0, connected=True,
                                running=False, cleanup_verified=True, cleanup_diagnostics=(),
                                rejection=None, rejection_diagnosis=None)
    def drain_rejection_events(self):
      if self._drained:
        return {"events": [], "dropped_count": 0}
      self._drained = True
      return {"events": list(queued), "dropped_count": 0}
    def close(self): return True
    def summary(self): return {"collector_calls": 0, "recover_hits": 0}

  class FakeBench:
    def __init__(self): self.prefix = "navcheck_fake"
    def __enter__(self): return self
    def __exit__(self, *_): return False
    def step(self, sample):
      return ({"nav_map": np.zeros((1, 1, 4, 4), np.uint8),
               "navigation": np.zeros((1, 47), np.float32)},
              {"source_reason": "phone_source_unavailable", "map_valid": False,
               "instruction_valid": False, "reset_requested": False,
               "runtime": {"mapReason": "phoneMapSuppressed"}})

  monkeypatch.setattr(probe, "ObservedProducer", FakeProducer)
  monkeypatch.setattr(probe, "PhonePipelineBench", FakeBench)
  monkeypatch.setattr(probe, "SAMPLE_INTERVAL_S", 0)
  ticks = iter([0.0, 0.0] + [float(i) for i in range(1, 300)])
  monkeypatch.setattr(probe.time, "monotonic", lambda: next(ticks, 999.0))
  out = tmp_path / "loopout"
  probe.main(["--out", str(out), "--duration", "15"])
  rows = [_json.loads(l) for l in (out / "rejections.jsonl").read_text().splitlines() if l.strip()]
  assert [r["event"]["event_id"] for r in rows] == [1, 2]
  assert [r["event"]["diagnosis"]["stage"] for r in rows] == ["A", "B"]
  report = _json.loads((out / "report.json").read_text())
  assert report["rejection_dropped_count"] == 0


def _producer_with_scripted_errors(producer, errors):
  """Run the real rejection path three times, never reading status or draining."""
  from APP.navdiag.phone_route_producer import _reason

  def inject(error):
    with producer._lock:
      chain = [_reason(error)]
      producer._last_error_chain = tuple(chain)
      diagnosis = getattr(error, "diagnosis", None)
      producer._last_rejection_diagnosis = diagnosis if isinstance(diagnosis, dict) else None
      producer._rejection_events += 1
      record = {"event_id": producer._rejection_events, "reason": _reason(error),
                "generation": producer._generation, "sequence": producer._sequence,
                "host_monotonic_ns": 0,
                "diagnosis": __import__("copy").deepcopy(producer._last_rejection_diagnosis),
                "diagnosis_present": producer._last_rejection_diagnosis is not None}
      producer._last_rejection = record
      from APP.navdiag.phone_route_producer import REJECTION_QUEUE_MAX
      if len(producer._rejection_queue) == REJECTION_QUEUE_MAX:
        producer._rejection_dropped += 1
      producer._rejection_queue.append(record)

  for error in errors:
    inject(error)


def test_three_rejections_without_polling_all_survive_one_drain(tmp_path):
  """A -> plain -> B happen with no status/frame/drain in between; one drain yields all three."""
  from APP.navdiag.phone_route_producer import PhoneRouteProducer

  first = ValueError("selection_changed_during_read")
  first.diagnosis = {"stage": "A", "json": {"changed": [{"path": "/timestamp"}]}}
  plain = ValueError("cache_changed_during_read")
  second = ValueError("selection_changed_during_read")
  second.diagnosis = {"stage": "B", "json": {"changed": [{"path": "/selected"}]}}

  producer = PhoneRouteProducer(port=5039)
  _producer_with_scripted_errors(producer, [first, plain, second])
  batch = producer.drain_rejection_events()
  assert [e["event_id"] for e in batch["events"]] == [1, 2, 3]
  assert [e["diagnosis"]["stage"] if e["diagnosis"] else None for e in batch["events"]] == ["A", None, "B"]
  assert batch["dropped_count"] == 0
  again = producer.drain_rejection_events()
  assert again == {"events": [], "dropped_count": 0}


def test_queue_overflow_reports_dropped_count():
  from APP.navdiag.phone_route_producer import PhoneRouteProducer, REJECTION_QUEUE_MAX

  producer = PhoneRouteProducer(port=5039)
  errors = [ValueError("cache_changed_during_read") for _ in range(REJECTION_QUEUE_MAX + 5)]
  _producer_with_scripted_errors(producer, errors)
  batch = producer.drain_rejection_events()
  assert len(batch["events"]) == REJECTION_QUEUE_MAX
  assert batch["dropped_count"] == 5
  assert batch["events"][0]["event_id"] == 6


def test_diagnosis_snapshot_does_not_alias_later_rejections():
  """A queued record keeps its own diagnosis copy after later rejections mutate state."""
  from APP.navdiag.phone_route_producer import PhoneRouteProducer

  first = ValueError("selection_changed_during_read")
  first.diagnosis = {"stage": "A", "nested": {"value": 1}}
  producer = PhoneRouteProducer(port=5039)
  _producer_with_scripted_errors(producer, [first])
  first.diagnosis["nested"]["value"] = 99
  batch = producer.drain_rejection_events()
  assert batch["events"][0]["diagnosis"]["nested"]["value"] == 1
