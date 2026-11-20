"""Exercise the real navd entry point and existing logging path without a phone."""
from copy import deepcopy
import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from APP.navdiag import phone_route_producer
from openpilot.cereal import log, messaging
from openpilot.common.basedir import BASEDIR
from openpilot.common.hardware.hw import Paths
from openpilot.common.logging_extra import SwagFormatter, SwagLogger
from openpilot.common.prefix import OpenpilotPrefix
from openpilot.common.swaglog import UnixDomainSocketHandler
from openpilot.selfdrive.navigation import navd
from openpilot.system.loggerd.drive_index import read_log


def rejection(event_id):
  return {"event_id": event_id, "reason": "selection_changed_during_read", "generation": event_id,
          "sequence": 7, "host_monotonic_ns": 123000000 + event_id,
          "diagnosis": {"schema_version": 1, "stage": "A", "metadata_changed": True},
          "diagnosis_present": True}


class QueueSource:
  def __init__(self, events=(), dropped_count=0):
    self.events = deepcopy(list(events))
    self.dropped_count = dropped_count
    self.order = []

  def start(self):
    self.order.append("start")
    return self

  def drain_rejection_events(self):
    self.order.append("drain")
    batch = {"events": self.events, "dropped_count": self.dropped_count}
    self.events, self.dropped_count = [], 0
    return batch

  def close(self):
    self.order.append("close")
    self.events.append(rejection(3))
    return True

  def status(self):
    return SimpleNamespace(cleanup_verified=True)


def wire_main(monkeypatch, source, steps):
  def step(_now):
    steps.append("step")

  def stop():
    raise KeyboardInterrupt("test_done")

  monkeypatch.setattr(navd, "Ratekeeper", lambda _rate: SimpleNamespace(keep_time=stop))
  monkeypatch.setattr(navd.messaging, "SubMaster", lambda _services: SimpleNamespace(update=lambda _timeout: None))
  monkeypatch.setattr(navd.messaging, "PubMaster", lambda _services: object())
  monkeypatch.setattr(navd, "Navd", lambda *_args: SimpleNamespace(step=step, phone_source=None))
  monkeypatch.setattr(navd, "amap_root_nav_enabled", lambda: True)
  monkeypatch.setattr(phone_route_producer, "PhoneRouteProducer", lambda **_kwargs: source)


def test_main_logs_running_queue_then_close_generated_event(monkeypatch):
  source = QueueSource([rejection(1), rejection(2)], dropped_count=5)
  records, steps = [], []
  wire_main(monkeypatch, source, steps)
  monkeypatch.setattr(navd.cloudlog, "event", lambda event, **fields: records.append({"event": event, **fields}))
  with pytest.raises(KeyboardInterrupt, match="test_done"):
    navd.main()
  assert steps == ["step"]
  assert source.order == ["start", "drain", "close", "drain"]
  assert [r["event_id"] for r in records if r["event"] == "navd.phone_source_rejection"] == [1, 2, 3]
  assert [r["dropped_count"] for r in records if r["event"] == "navd.phone_source_rejections_dropped"] == [5]
  assert records[0]["diagnosis"] == rejection(1)["diagnosis"]


def test_repeated_drain_does_not_repeat_events_and_old_sources_are_supported(monkeypatch):
  source = QueueSource([rejection(1)])
  records = []
  monkeypatch.setattr(navd.cloudlog, "event", lambda event, **fields: records.append((event, fields)))
  navd.drain_phone_rejections(source)
  navd.drain_phone_rejections(source)
  navd.drain_phone_rejections(None)
  navd.drain_phone_rejections(SimpleNamespace(latest=lambda: None))
  assert len(records) == 1 and records[0][1]["event_id"] == 1


@pytest.mark.parametrize("failure", ["event", "drain"])
def test_logging_failure_does_not_interrupt_navigation_or_cleanup(monkeypatch, failure):
  source = QueueSource([rejection(1)])
  steps = []
  wire_main(monkeypatch, source, steps)

  def broken(*_args, **_kwargs):
    raise RuntimeError("test_log_failure")

  if failure == "drain":
    source.drain_rejection_events = broken
  monkeypatch.setattr(navd.cloudlog, "event", broken)
  monkeypatch.setattr(navd.cloudlog, "error", broken)
  with pytest.raises(KeyboardInterrupt, match="test_done"):
    navd.main()
  assert steps == ["step"]
  assert "close" in source.order


def test_only_queue_contract_fields_are_forwarded(monkeypatch):
  event = {**rejection(1), "coordinates": [1, 2], "private_source_path": "/private/ignored"}
  records = []
  monkeypatch.setattr(navd.cloudlog, "event", lambda name, **fields: records.append(fields))
  navd.drain_phone_rejections(QueueSource([event]))
  assert "coordinates" not in records[0] and "private_source_path" not in records[0]


def _navi_info_sample(sequence=1, changed_angle=False):
  binding = {"status": "verified", "reason": "segment_object_verified",
             "segment_object_verified": True, "icon_id_getter_vtable_verified": True,
             "icon_getter_vtable_verified": True}
  branch = {"maneuver_id_raw": 14, "maneuver_segment_raw": 2, "maneuver_link_raw": 3,
            "maneuver_point_raw": 4, "exit_number_raw": 2,
            "exit_angle_raw": 91 if changed_angle else 90, "obtain_supported_raw": 1,
            "data_changed_raw": 0, "segment_remain_time_raw": 40,
            "segment_remain_distance_raw": 500, "next_road_name_segment_raw": 5,
            "next_road_name_link_raw": 6, "split_raw": 0, "segment_binding": binding}
  return SimpleNamespace(sequence=sequence, native_after={
    "path_identity_hash": "a" * 64,
    "_navi_info": {"schema_version": 2, "status": "observed",
                   "reason": "unverified_navi_info_semantics", "diagnostic_only": True,
                   "control_input_allowed": False, "route_render_input_allowed": False,
                   "source": "native_navi_info", "path_identity_hash": "a" * 64,
                   "route_remain_time_raw": 100, "route_remain_distance_raw": 1000,
                   "navigation_info_type_raw": 0, "item_count": 3, "item_index": 1,
                   "current_segment": 2, "current_link": 3, "current_point": 4,
                   "branch_flag_raw": 1, "branches": [branch, dict(branch, maneuver_id_raw=15)]},
  })


def test_navi_info_logger_emits_changes_and_rate_limits_heartbeat(monkeypatch):
  source = SimpleNamespace(latest=lambda: _navi_info_sample(1))
  records = []
  monkeypatch.setattr(navd.cloudlog, "event",
                      lambda name, **fields: records.append((name, fields)))
  subject = navd.NaviInfoEvidenceLogger()
  subject.step(source, 10.0)
  subject.step(source, 11.0)
  source.latest = lambda: _navi_info_sample(2, changed_angle=True)
  subject.step(source, 12.0)
  source.latest = lambda: _navi_info_sample(3, changed_angle=True)
  subject.step(source, 14.0)
  subject.step(source, 17.0)
  assert [record[1]["changed"] for record in records] == [True, True, False]
  assert [record[1]["sequence"] for record in records] == [1, 2, 3]
  evidence = records[0][1]["evidence"]
  assert evidence["path_identity_hash"] == "a" * 64
  assert evidence["branches"][0]["exit_angle_raw"] == 90
  assert evidence["control_input_allowed"] is False
  assert "latitude" not in json.dumps(evidence) and "raw_address" not in json.dumps(evidence)


def test_navi_info_logger_fails_closed_and_never_breaks_navigation(monkeypatch):
  sample = _navi_info_sample()
  sample.native_after["_navi_info"]["control_input_allowed"] = True
  records = []
  monkeypatch.setattr(navd.cloudlog, "event",
                      lambda name, **fields: records.append(fields["evidence"]))
  navd.NaviInfoEvidenceLogger().step(SimpleNamespace(latest=lambda: sample), 1.0)
  assert records == [{"status": "unavailable", "reason": "unsafe_observation_contract"}]
  monkeypatch.setattr(navd.cloudlog, "event", lambda *_args, **_kwargs: (_ for _ in ()).throw(
    RuntimeError("broken log")))
  monkeypatch.setattr(navd.cloudlog, "error", lambda *_args, **_kwargs: None)
  navd.NaviInfoEvidenceLogger().step(SimpleNamespace(latest=lambda: _navi_info_sample()), 2.0)


def test_navi_info_logger_accepts_frozen_branch_tuples():
  sample = _navi_info_sample()
  sample.native_after["_navi_info"]["branches"] = tuple(
    sample.native_after["_navi_info"]["branches"])
  evidence = navd.navi_info_evidence(sample)
  assert evidence["status"] == "observed"
  assert isinstance(evidence["branches"], list)
  assert evidence["control_input_allowed"] is False


def _stop_owned(process):
  if process.poll() is None:
    assert os.getpgid(process.pid) == process.pid
    os.killpg(process.pid, signal.SIGTERM)
    try:
      process.wait(timeout=5)
    except subprocess.TimeoutExpired:
      os.killpg(process.pid, signal.SIGKILL)
      process.wait(timeout=5)


def test_production_rejection_crosses_swaglog_logmessage_and_native_logger(tmp_path, monkeypatch):
  binary = Path(BASEDIR) / "openpilot/system/loggerd/loggerd"
  if not binary.is_file():
    pytest.skip("build loggerd before running native logging integration")
  monkeypatch.setenv("LOGGERD_TEST", "1")
  monkeypatch.setenv("LOGGERD_SEGMENT_LENGTH", "60")
  with OpenpilotPrefix():
    output = tmp_path / "processes.log"
    custom_log = SwagLogger()
    custom_log.setLevel(logging.INFO)
    handler = UnixDomainSocketHandler(SwagFormatter(custom_log))
    custom_log.addHandler(handler)
    monkeypatch.setattr(navd, "cloudlog", custom_log)
    subscription = messaging.sub_sock("logMessage", timeout=100, conflate=False)
    processes = []
    try:
      with output.open("w") as stdout:
        processes.append(subprocess.Popen([sys.executable, "-m", "openpilot.system.logmessaged"],
                                          cwd=BASEDIR, stdout=stdout, stderr=subprocess.STDOUT,
                                          start_new_session=True))
        deadline = time.monotonic() + 5
        ipc_path = Path(Paths.swaglog_ipc().removeprefix("ipc://"))
        while not ipc_path.exists() and time.monotonic() < deadline:
          time.sleep(0.02)
        assert ipc_path.exists(), output.read_text()
        # Establish the publisher before loggerd subscribes to its current queue.
        while time.monotonic() < deadline:
          custom_log.info("nav_rejection_test_ready")
          if messaging.recv_one_or_none(subscription) is not None:
            break
          time.sleep(0.02)
        else:
          pytest.fail(output.read_text())
        processes.append(subprocess.Popen([str(binary)], cwd=binary.parent, stdout=stdout,
                                          stderr=subprocess.STDOUT, start_new_session=True))
        while not list(Path(Paths.log_root()).glob("*/rlog.zst")) and time.monotonic() < deadline:
          time.sleep(0.02)
        assert list(Path(Paths.log_root()).glob("*/rlog.zst")), output.read_text()
        navd.drain_phone_rejections(QueueSource([rejection(1), rejection(2)], dropped_count=4))
        seen = []
        while len(seen) < 3 and time.monotonic() < deadline:
          record = messaging.recv_one_or_none(subscription)
          if record is None:
            continue
          body = json.loads(record.logMessage).get("msg", {})
          if isinstance(body, dict) and body.get("event", "").startswith("navd.phone_source_"):
            seen.append(body)
        assert len(seen) == 3, output.read_text()
        # Drive the existing bounded zstd checkpoint without resending source events.
        time.sleep(1.05)
        custom_log.info("nav_rejection_test_checkpoint")
        time.sleep(0.1)
    finally:
      handler.close()
      for process in reversed(processes):
        _stop_owned(process)
      Path(Paths.swaglog_ipc().removeprefix("ipc://")).unlink(missing_ok=True)
      del subscription
    recorded = []
    debug_records = []
    for path in Path(Paths.log_root()).glob("*/rlog.zst"):
      raw, _, issues = read_log(path)
      assert not issues
      for item in log.Event.read_multiple_bytes(raw):
        if item.which() == "logMessage":
          body = json.loads(item.logMessage).get("msg", {})
          debug_records.append(body)
          if isinstance(body, dict) and body.get("event", "").startswith("navd.phone_source_"):
            recorded.append(body)
    assert [item["event_id"] for item in recorded if item["event"] == "navd.phone_source_rejection"] == [1, 2], debug_records
    assert [item["dropped_count"] for item in recorded if item["event"] == "navd.phone_source_rejections_dropped"] == [4]
    assert all(process.poll() is not None for process in processes)
