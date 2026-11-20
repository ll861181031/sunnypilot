import json
from pathlib import Path
import stat
import subprocess
from types import SimpleNamespace

import pytest

import root_probe as probe


def frame(payload=b"", code=0):
  return probe.PREFIX + payload + f"\nNAVDIAG_PROBE_RC={code:03d}\n".encode()


@pytest.mark.parametrize("path", ["/data/user/0/other/files/naviPathInfo.json", "/sdcard/naviPathInfo.json",
                                 probe.ALLOWED_PATH + "; id", probe.ALLOWED_PATH + "/../other", ""])
def test_remote_read_rejects_every_non_allowlisted_path(path):
  with pytest.raises(ValueError):
    probe.remote_command(path)


def test_read_command_is_bounded_and_checks_file_kind():
  command = probe.remote_command()
  assert probe.ALLOWED_PATH in command
  assert f"head -c {probe.MAX_FILE_BYTES + 1}" in command
  assert "[ -L " in command
  assert "[ ! -f " in command
  assert "NAVDIAG_PROBE_RC=" in command


def test_non_utf8_binary_and_embedded_marker_are_preserved():
  payload = b"\x00\xff\xfe\x80\nNAVDIAG_PROBE_RC=044\n\x00"
  event, body = probe.parse_sample(0, frame(payload))
  assert event["status"] == "read"
  assert body == payload


@pytest.mark.parametrize("local_code,remote_code,status", [(0, 44, "missing"), (44, 44, "missing"),
  (0, 45, "not_regular_file"), (0, 46, "symlink_rejected"), (0, 47, "unreadable"),
  (0, 1, "read_error"), (1, 44, "transport_error")])
def test_exit_codes_are_interpreted_without_saving_error_text(local_code, remote_code, status):
  event, body = probe.parse_sample(local_code, frame(b"private-error-text", remote_code))
  assert event["status"] == status
  assert body is None
  assert "private-error-text" not in json.dumps(event)


@pytest.mark.parametrize("raw,code,status", [(b"", 0, "protocol_error"), (b"permission denied", 1, "command_error"),
  (probe.PREFIX + b"incomplete", 0, "protocol_error")])
def test_missing_or_incomplete_envelope_is_not_a_sample(raw, code, status):
  event, body = probe.parse_sample(code, raw)
  assert event["status"] == status
  assert body is None


def test_size_limit_reads_sentinel_but_never_stores_truncated_body():
  event, body = probe.parse_sample(0, frame(b"a" * (probe.MAX_FILE_BYTES + 1)))
  assert event["status"] == "oversize"
  assert body is None
  event, body = probe.parse_sample(0, frame(b"a" * probe.MAX_FILE_BYTES))
  assert event["status"] == "read"
  assert len(body) == probe.MAX_FILE_BYTES


def test_empty_file_is_distinguished_from_missing():
  event, body = probe.parse_sample(0, frame())
  assert event["status"] == "empty"
  assert body is None


def test_adb_disables_pty_and_preserves_binary_without_text_conversion(monkeypatch):
  calls = []

  def run(command, **kwargs):
    calls.append((command, kwargs))
    return subprocess.CompletedProcess(command, 0, frame(b"\xff\x00"), b"")

  monkeypatch.setattr(probe.subprocess, "run", run)
  device = SimpleNamespace(adb="/fake/adb", port=5039, serial="fixture-device")
  event, body = probe.read_once(device, 2.5)
  assert event["status"] == "read"
  assert body == b"\xff\x00"
  assert calls[0][0] == ["/fake/adb", "-H", "127.0.0.1", "-P", "5039", "-s", "fixture-device", "shell", "-T", probe.remote_command()]
  assert calls[0][1]["text"] is False
  assert calls[0][1]["timeout"] == 2.5
  assert calls[0][1]["check"] is False


def test_store_deduplicates_and_keeps_private_modes_and_total_limit(tmp_path):
  store = probe.SnapshotStore(tmp_path / "out", max_total_bytes=probe.REPORT_RESERVE + 5)
  first = store.save(b"\xff\x00abc")
  assert first["status"] == "saved"
  assert store.save(b"\xff\x00abc")["status"] == "duplicate"
  assert store.save(b"x")["status"] == "total_limit"
  assert store.total_bytes == 5
  saved = store.output / first["file"]
  assert saved.suffix == ".bin"
  assert saved.read_bytes() == b"\xff\x00abc"
  assert stat.S_IMODE(store.output.stat().st_mode) == 0o700
  assert stat.S_IMODE(saved.stat().st_mode) == 0o600
  report = store.report({"saved_bytes": 5})
  assert stat.S_IMODE(report.stat().st_mode) == 0o600


@pytest.mark.parametrize("duration", [0, -1, 181, 1.5, True])
def test_duration_rejects_out_of_bounds_or_non_integer(duration):
  with pytest.raises(ValueError):
    probe.validate_duration(duration)


def test_sampling_is_one_per_second_and_records_missing_without_content(tmp_path):
  now = [0.0]
  read_times = []

  def reader(_device, timeout):
    read_times.append(now[0])
    assert 0 < timeout <= 3
    if len(read_times) == 1:
      return {"status": "missing", "returncode": 0, "remote_returncode": 44}, None
    return {"status": "read", "returncode": 0, "remote_returncode": 0}, b"\xff\x00"

  result = probe.collect(None, tmp_path / "out", 3, reader=reader, clock=lambda: now[0],
                         sleep=lambda seconds: now.__setitem__(0, now[0] + seconds))
  assert read_times == [0.0, 1.0, 2.0]
  assert result["counts"] == {"missing": 1, "saved": 1, "duplicate": 1}
  assert result["distinct_samples"] == 1
  report = json.loads(Path(result["report"]).read_text())
  assert report["complete_route_verified"] is False
  assert report["source_complete_verified"] is False
  assert [entry["elapsed_ms"] for entry in report["events"]] == [0, 1000, 2000]


def test_existing_output_directory_is_never_overwritten(tmp_path):
  output = tmp_path / "existing"
  output.mkdir()
  with pytest.raises(FileExistsError):
    probe.SnapshotStore(output)
