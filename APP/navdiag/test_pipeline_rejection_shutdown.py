"""Real probe entry-point shutdown and real adapter evidence wiring; no phone or GPU."""
import json
import time
from types import SimpleNamespace

import pytest

from APP.navdiag import live_pipeline_probe as probe
from APP.navdiag.phone_pipeline_bench import PhonePipelineBench
from openpilot.selfdrive.navigation.tests.test_amap_phone_pipeline import make_sample


@pytest.mark.parametrize("failure_point", ["start", "enter", "step"])
def test_final_drain_includes_close_event_even_on_partial_initialization(tmp_path, monkeypatch, failure_point):
  order = []

  class Producer:
    def __init__(self, **_kwargs):
      self.events = []
    def start(self):
      if failure_point == "start":
        raise RuntimeError("test_start_failure")
      return self
    def status(self):
      return SimpleNamespace(reason="accepted", sequence=1, connected=True, running=False,
                             cleanup_verified=True, cleanup_diagnostics=())
    def latest(self):
      return None
    def drain_rejection_events(self):
      order.append("drain")
      batch = {"events": self.events, "dropped_count": 2 if self.events else 0}
      self.events = []
      return batch
    def close(self):
      order.append("close")
      self.events.append({"event_id": 9, "reason": "test_last_rejection", "diagnosis": None})
      return True
    def summary(self):
      return {"collector_calls": 0, "recover_hits": 0}

  class Bench:
    prefix = "test_no_live_queues"
    def __enter__(self):
      if failure_point == "enter":
        raise RuntimeError("test_enter_failure")
      return self
    def step(self, _sample):
      raise RuntimeError("test_step_failure")
    def __exit__(self, *_args):
      order.append("bench_closed")

  monkeypatch.setattr(probe, "ObservedProducer", Producer)
  monkeypatch.setattr(probe, "PhonePipelineBench", Bench)
  out = tmp_path / failure_point
  assert probe.main(["--out", str(out), "--duration", "15"]) == 1
  report = json.loads((out / "report.json").read_text())
  assert [r["event"]["event_id"] for r in report["rejections"]] == [9]
  assert report["rejection_dropped_count"] == 2
  assert order[order.index("close") + 1] == "drain"
  assert order.count("close") == 1
  if failure_point == "step":
    assert order[-1] == "bench_closed"


def test_real_bench_adapter_decision_is_preserved_in_frame_record():
  with PhonePipelineBench() as bench:
    sample = make_sample(now_ns=time.monotonic_ns())
    inputs, details = bench.step(sample)
    assert details["source_reason"] == "accepted_strict"
    record = probe.frame_record(0, 0, sample, details, inputs)
    decision = record["source_decision"]
    assert decision == bench.navd.phone_adapter.decision_evidence()
    assert decision["decision_reason"] == "accepted_strict"
    assert decision["sample_observed_mono_s"] == pytest.approx(sample.host_start_ns / 1e9, abs=1e-8)
    assert decision["sample_observed_mono_s"] < decision["accept_called_mono_s"]
    assert decision["after_observe_withdrawn_at_mono_s"] == decision["accept_final_withdrawn_at_mono_s"]
    original_decision = dict(decision)
    inputs, details = bench.step(None)
    revoked = probe.frame_record(1, 1, None, details, inputs)["source_decision"]
    assert revoked["decision_reason"] == "phone_source_unavailable"
    assert revoked["sample_observed_mono_s"] == "unknown"
    assert revoked["accept_begin_withdrawn_at_mono_s"] == original_decision["accept_final_withdrawn_at_mono_s"]
    assert revoked["accept_final_withdrawn_at_mono_s"] == revoked["accept_called_mono_s"]
    assert record["source_decision"] == original_decision
