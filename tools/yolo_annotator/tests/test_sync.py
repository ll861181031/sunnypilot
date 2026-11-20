"""Unit tests for sync.py: config, manifest, scheduler, worker, mocked SFTP."""

from __future__ import annotations

import json
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import sync as sync_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def make_config(tmp_path: Path, **overrides) -> sync_mod.SyncConfig:
    payload = {
        "host": "192.168.123.70",
        "port": 22,
        "user": "lsr",
        "key_path": str(tmp_path / "id_ed25519_labeler"),
        "remote_dir": "/home/lsr/yolo_label_inbox/<batch>",
        "auto_upload": True,
        "timeout": 3,
    }
    payload.update(overrides)
    path = tmp_path / "sync.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return sync_mod.SyncConfig.load(path)


def make_batch(tmp_path: Path, ids=("000001", "000002")) -> Path:
    root = tmp_path / "batch_001"
    (root / "labels").mkdir(parents=True)
    for image_id in ids:
        (root / "labels" / f"{image_id}.txt").write_text(
            "0 0.5 0.5 0.2 0.2\n", encoding="utf-8")
    (root / "review_state.json").write_text(
        json.dumps({i: {"reviewed": True, "edited": False, "updated": 1.0}
                    for i in ids}), encoding="utf-8")
    return root


class FakeUploader:
    """Records uploads instead of talking SFTP."""

    instances: list[FakeUploader] = []

    def __init__(self, fail: bool = False) -> None:
        self.calls: list[dict] = []
        self.fail = fail
        self.closed = False
        FakeUploader.instances.append(self)

    def upload(self, items, remote_dir, manifest):
        if self.fail:
            raise sync_mod.SftpError("模拟网络故障")
        self.calls.append({
            "items": [(str(Path(local)), relative) for local, relative in items],
            "remote_dir": remote_dir,
            "manifest": manifest,
        })
        return {"uploaded": [relative for _local, relative in items] + ["sync_manifest.json"],
                "remote_dir": remote_dir}

    def close(self):
        self.closed = True


def wait_for(predicate, timeout: float = 5.0, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def test_config_load_and_placeholder(tmp_path):
    config = make_config(tmp_path)
    assert config.is_configured()
    assert config.resolve_remote_dir("batch_001") == \
        "/home/lsr/yolo_label_inbox/batch_001"
    assert config.auto_upload is True
    assert config.port == 22


def test_config_placeholders_both_styles(tmp_path):
    config = make_config(tmp_path, remote_dir="/in/{batch}/labels")
    assert config.resolve_remote_dir("pilot_200") == "/in/pilot_200/labels"


def test_config_missing_or_invalid_degrades(tmp_path):
    assert sync_mod.SyncConfig.load(tmp_path / "nope.json").is_configured() is False
    bad = tmp_path / "bad.json"
    bad.write_text("{oops", encoding="utf-8")
    assert sync_mod.SyncConfig.load(bad).is_configured() is False
    empty = sync_mod.SyncConfig()
    assert not empty.is_configured()
    assert empty.sanitized()["password"] == ""


def test_config_find_prefers_app_dir(tmp_path, monkeypatch):
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    make_config(app_dir)
    config, path = sync_mod.SyncConfig.find(app_dir)
    assert path == app_dir / "sync.json" and config.is_configured()

    # No sync.json next to the app: fall back to the cwd, so run the check in
    # an empty working directory (otherwise a deployed sync.json would win).
    other = tmp_path / "no_config"
    other.mkdir()
    cwd = tmp_path / "empty_cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    config, path = sync_mod.SyncConfig.find(other)
    assert path is None and not config.is_configured()


def test_config_password_is_not_exposed_in_sanitized():
    config = sync_mod.SyncConfig(host="h", user="u", remote_dir="/r",
                                 password="secret")
    assert "secret" not in json.dumps(config.sanitized())


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------
def test_build_manifest_contents(tmp_path):
    batch = make_batch(tmp_path)
    items = [(batch / "labels" / "000001.txt", "labels/000001.txt"),
             (batch / "review_state.json", "review_state.json")]
    manifest = sync_mod.build_manifest(
        batch="batch_001", items=items, counts={"total": 2, "reviewed": 1},
        last_sync=1000.0, pending=1, remote_dir="/remote/batch_001")
    assert manifest["schema"] == 1
    assert manifest["batch"] == "batch_001"
    assert manifest["app_version"] == sync_mod.APP_VERSION
    assert manifest["counts"] == {"total": 2, "reviewed": 1}
    assert manifest["last_sync"] == "1970-01-01T00:16:40Z"
    assert manifest["pending"] == 1
    assert manifest["remote_dir"] == "/remote/batch_001"
    paths = [entry["path"] for entry in manifest["files"]]
    assert paths == ["labels/000001.txt", "review_state.json"]
    assert all(entry["size"] is not None for entry in manifest["files"])
    json.dumps(manifest)  # must be serialisable


def test_build_manifest_handles_missing_files(tmp_path):
    manifest = sync_mod.build_manifest(
        "b", items=[(tmp_path / "gone.txt", "labels/gone.txt")])
    assert manifest["files"][0]["size"] is None


# ---------------------------------------------------------------------------
# Scheduler (pure state machine, fake clock)
# ---------------------------------------------------------------------------
def test_scheduler_debounces_saves():
    scheduler = sync_mod.SyncScheduler(now=100.0, debounce=3.0, period=60.0)
    scheduler.mark_dirty(["000001"], now=100.0)
    assert scheduler.take_due(101.0) is None
    scheduler.mark_dirty(["000002"], now=101.5)  # earlier due stays
    assert scheduler.take_due(103.1) == "dirty"
    assert scheduler.take_due(103.2) is None  # already in flight
    scheduler.success(now=103.3)
    assert scheduler.pending == set()
    assert scheduler.last_sync == 103.3
    assert scheduler.take_due(104.0) is None


def test_scheduler_force_ignores_debounce():
    scheduler = sync_mod.SyncScheduler(now=0.0, debounce=3.0, period=0.0)
    scheduler.mark_dirty(["a"], now=0.0)
    scheduler.force(now=0.1)
    assert scheduler.take_due(0.2) == "dirty"


def test_scheduler_periodic_tick_only_when_changed():
    scheduler = sync_mod.SyncScheduler(now=0.0, debounce=1000.0, period=60.0)
    # Nothing changed: no periodic upload.
    assert scheduler.take_due(60.5) is None
    # Something changed (debounce is still pending) -> the periodic tick fires.
    scheduler.mark_dirty(["a"], now=61.0)
    assert scheduler.take_due(61.1) is None  # debounce not elapsed yet
    assert scheduler.take_due(121.0) == "periodic"
    scheduler.success(now=121.0)
    # Nothing pending any more -> no periodic upload either.
    assert scheduler.take_due(200.0) is None


def test_scheduler_exponential_backoff():
    scheduler = sync_mod.SyncScheduler(now=0.0, debounce=3.0, period=60.0,
                                       backoff_base=2.0, backoff_max=10.0)
    scheduler.mark_dirty(["a"], now=0.0)
    assert scheduler.take_due(3.1) == "dirty"
    scheduler.failure(now=3.1, error="boom")
    assert scheduler.attempts == 1
    assert scheduler.due_at == pytest.approx(3.1 + 2.0)
    assert scheduler.take_due(4.0) is None
    assert scheduler.take_due(5.2) == "dirty"
    scheduler.failure(now=5.2, error="boom")
    assert scheduler.due_at == pytest.approx(5.2 + 4.0)
    assert scheduler.take_due(9.3) == "dirty"
    scheduler.failure(now=9.3, error="boom")
    assert scheduler.due_at == pytest.approx(9.3 + 8.0)
    assert scheduler.take_due(17.4) == "dirty"
    scheduler.failure(now=17.4, error="boom")
    assert scheduler.due_at == pytest.approx(17.4 + 10.0)  # capped
    assert scheduler.state == "error"
    assert scheduler.last_error == "boom"


def test_scheduler_success_resets_backoff():
    scheduler = sync_mod.SyncScheduler(now=0.0)
    scheduler.mark_dirty(["a"], now=0.0)
    scheduler.take_due(3.5)
    scheduler.failure(now=3.5, error="x")
    assert scheduler.attempts == 1
    scheduler.take_due(5.6)
    scheduler.success(now=5.6)
    assert scheduler.attempts == 0 and scheduler.last_error == ""
    assert scheduler.state == "ok"


def test_scheduler_next_wake_bounds():
    scheduler = sync_mod.SyncScheduler(now=0.0, debounce=3.0, period=60.0)
    assert scheduler.next_wake(0.0) == pytest.approx(60.0)
    scheduler.mark_dirty(["a"], now=0.0)
    assert scheduler.next_wake(0.0) == pytest.approx(3.0)
    assert scheduler.next_wake(2.0) == pytest.approx(1.0)
    scheduler.take_due(3.1)
    assert scheduler.next_wake(3.1) == pytest.approx(0.25)


# ---------------------------------------------------------------------------
# Worker (thread)
# ---------------------------------------------------------------------------
def test_worker_disabled_without_config(tmp_path):
    worker = sync_mod.SyncWorker(sync_mod.SyncConfig(), tmp_path, "batch_001",
                                 uploader_factory=lambda cfg: FakeUploader())
    assert worker.enabled is False
    assert "sync.json" in worker.disabled_reason
    status = worker.status()
    assert status.state == "disabled"
    assert "同步: 关闭" in status.label()
    worker.mark_dirty(["000001"])  # must not raise
    assert worker.flush() is False
    assert worker.shutdown() is True


def test_worker_disabled_without_paramiko(tmp_path, monkeypatch):
    monkeypatch.setattr(sync_mod, "paramiko", None)
    config = make_config(tmp_path)
    worker = sync_mod.SyncWorker(config, tmp_path, "batch_001")
    assert worker.enabled is False
    assert "paramiko" in worker.disabled_reason
    assert worker.status().state == "disabled"
    worker.shutdown()


def test_worker_uploads_only_dirty_labels(tmp_path):
    batch = make_batch(tmp_path)
    config = make_config(tmp_path)
    uploader = FakeUploader()
    worker = sync_mod.SyncWorker(config, dataset_root=batch,
                                 batch="batch_001", debounce=0.05, period=0.0,
                                 uploader_factory=lambda _cfg: uploader,
                                 counts_provider=lambda: {"total": 2})
    worker.start()
    try:
        worker.mark_dirty(["000001"])
        assert wait_for(lambda: len(uploader.calls) == 1), "first upload did not happen"
        first = uploader.calls[0]
        relatives = [relative for _local, relative in first["items"]]
        assert relatives == ["labels/000001.txt", "review_state.json"]
        assert first["remote_dir"] == "/home/lsr/yolo_label_inbox/batch_001"
        assert first["manifest"]["batch"] == "batch_001"
        assert first["manifest"]["pending"] == 1
        assert first["manifest"]["trigger"] == "dirty"
        assert first["manifest"]["counts"] == {"total": 2}

        worker.mark_dirty(["000002"])
        assert wait_for(lambda: len(uploader.calls) == 2), "second upload did not happen"
        relatives = [relative for _local, relative in uploader.calls[1]["items"]]
        assert relatives == ["labels/000002.txt", "review_state.json"]
        assert wait_for(lambda: worker.status().state == "ok")
        status = worker.status()
        assert status.pending == 0
        assert status.last_sync is not None
    finally:
        worker.shutdown()


def test_worker_reports_failure_and_recovers(tmp_path):
    batch = make_batch(tmp_path)
    config = make_config(tmp_path)
    uploader = FakeUploader(fail=True)
    worker = sync_mod.SyncWorker(config, batch, "batch_001", debounce=0.05,
                                 period=0.0, backoff_base=0.05, backoff_max=0.2,
                                 uploader_factory=lambda _cfg: uploader)
    worker.start()
    try:
        worker.mark_dirty(["000001"])
        assert wait_for(lambda: worker.status().state == "error"), "no error reported"
        status = worker.status()
        assert status.attempts >= 1
        assert "模拟网络故障" in status.last_error
        assert "同步: 失败" in status.label()

        uploader.fail = False
        assert wait_for(lambda: worker.status().state == "ok", timeout=5.0), \
            "worker did not recover"
    finally:
        worker.shutdown()


def test_worker_shutdown_flushes_pending(tmp_path):
    batch = make_batch(tmp_path)
    config = make_config(tmp_path)
    uploader = FakeUploader()
    worker = sync_mod.SyncWorker(config, batch, "batch_001", debounce=30.0,
                                 period=0.0, uploader_factory=lambda _cfg: uploader)
    # No thread started: shutdown must still push the pending batch synchronously.
    worker.mark_dirty(["000001"])
    assert uploader.calls == []
    assert worker.shutdown(flush=True, timeout=2.0) is True
    assert len(uploader.calls) == 1


def test_worker_manual_sync_works_with_auto_upload_disabled(tmp_path):
    batch = make_batch(tmp_path)
    config = make_config(tmp_path, auto_upload=False)
    uploader = FakeUploader()
    worker = sync_mod.SyncWorker(config, batch, "batch_001", debounce=0.05,
                                 period=0.0, uploader_factory=lambda _cfg: uploader)
    worker.start()
    try:
        worker.mark_dirty(["000001"])
        time.sleep(0.2)
        assert uploader.calls == []  # automatic uploads disabled
        worker.request_sync()
        assert wait_for(lambda: len(uploader.calls) == 1), "manual sync did nothing"
        assert worker.status().state == "ok"
    finally:
        worker.shutdown()


# ---------------------------------------------------------------------------
# SFTP uploader with a fake paramiko module (no network)
# ---------------------------------------------------------------------------
class FakeSFTP:
    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.dirs: set[str] = {"/"}
        self.puts: list[tuple[str, str]] = []
        self.mkdirs: list[str] = []

    def stat(self, path):
        if path in self.dirs or path in self.files:
            return SimpleNamespace(st_size=len(self.files.get(path, b"")))
        raise OSError(f"no such file: {path}")

    def mkdir(self, path):
        self.dirs.add(path)
        self.mkdirs.append(path)

    def put(self, local, remote):
        self.puts.append((local, remote))
        self.files[remote] = Path(local).read_bytes()

    def open(self, path, mode="r"):
        sftp = self

        class _Handle:
            def __enter__(self):
                self.buffer = []
                return self

            def __exit__(self, *exc):
                sftp.files[path] = "".join(self.buffer).encode("utf-8")
                return False

            def write(self, text):
                self.buffer.append(text)

        return _Handle()

    def close(self):
        pass


class FakeSSHClient:
    def __init__(self):
        self.sftp = FakeSFTP()
        self.connected = False
        self.closed = False
        self.connect_kwargs: dict = {}

    def set_missing_host_key_policy(self, _policy):
        pass

    def connect(self, **kwargs):
        self.connected = True
        self.connect_kwargs = kwargs

    def open_sftp(self):
        return self.sftp

    def close(self):
        self.closed = True


@pytest.fixture()
def fake_paramiko(monkeypatch):
    client = FakeSSHClient()
    fake_module = SimpleNamespace(SSHClient=lambda: client,
                                 AutoAddPolicy=lambda: None)
    monkeypatch.setattr(sync_mod, "paramiko", fake_module)
    return client


def test_sftp_uploader_creates_remote_dirs_and_manifest_last(tmp_path, fake_paramiko):
    key = tmp_path / "id_ed25519_labeler"
    key.write_text("fake key", encoding="utf-8")
    batch = make_batch(tmp_path)
    config = make_config(tmp_path)
    uploader = sync_mod.SftpUploader(config)

    items = [(batch / "labels" / "000001.txt", "labels/000001.txt"),
             (batch / "review_state.json", "review_state.json")]
    manifest = sync_mod.build_manifest("batch_001", items, counts={"total": 2})
    result = uploader.upload(items, "/home/lsr/yolo_label_inbox/batch_001", manifest)

    sftp = fake_paramiko.sftp
    assert fake_paramiko.connected
    assert fake_paramiko.connect_kwargs["hostname"] == "192.168.123.70"
    assert fake_paramiko.connect_kwargs["key_filename"] == str(key)
    assert fake_paramiko.connect_kwargs["look_for_keys"] is False

    # Nested remote directories were created.
    assert "/home/lsr/yolo_label_inbox/batch_001" in sftp.dirs
    assert "/home/lsr/yolo_label_inbox/batch_001/labels" in sftp.dirs

    # Both files plus the manifest landed remotely; manifest content parses.
    assert sftp.puts[0][1] == "/home/lsr/yolo_label_inbox/batch_001/labels/000001.txt"
    assert sftp.puts[1][1] == "/home/lsr/yolo_label_inbox/batch_001/review_state.json"
    manifest_path = "/home/lsr/yolo_label_inbox/batch_001/sync_manifest.json"
    assert manifest_path in sftp.files
    payload = json.loads(sftp.files[manifest_path].decode("utf-8"))
    assert payload["batch"] == "batch_001"
    assert result["uploaded"] == ["labels/000001.txt", "review_state.json",
                                  "sync_manifest.json"]
    uploader.close()
    assert fake_paramiko.closed


def test_sftp_uploader_reports_missing_key(tmp_path, fake_paramiko):
    config = make_config(tmp_path)  # key file does not exist
    uploader = sync_mod.SftpUploader(config)
    with pytest.raises(sync_mod.SftpError, match="私钥不存在"):
        uploader.upload([], "/remote", {})


def test_sftp_uploader_skips_missing_local_files(tmp_path, fake_paramiko):
    key = tmp_path / "id_ed25519_labeler"
    key.write_text("fake key", encoding="utf-8")
    config = make_config(tmp_path)
    uploader = sync_mod.SftpUploader(config)
    manifest = sync_mod.build_manifest("b", [])
    result = uploader.upload([(tmp_path / "gone.txt", "labels/gone.txt")],
                             "/remote/b", manifest)
    assert result["uploaded"] == ["sync_manifest.json"]


# ---------------------------------------------------------------------------
# zip helpers
# ---------------------------------------------------------------------------
def test_safe_extract_zip_extracts_normally(tmp_path):
    archive = tmp_path / "batch.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("batch_001/dataset.json", "{}")
        zf.writestr("batch_001/images/000001.jpg", b"jpeg")
    dest = sync_mod.safe_extract_zip(archive, tmp_path / "out")
    assert (dest / "batch_001" / "dataset.json").is_file()


def test_safe_extract_zip_rejects_path_traversal(tmp_path):
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../escaped.txt", "boom")
    with pytest.raises(sync_mod.SftpError):
        sync_mod.safe_extract_zip(archive, tmp_path / "out")


def test_pull_batch_requires_paramiko_and_config(tmp_path, monkeypatch):
    monkeypatch.setattr(sync_mod, "paramiko", None)
    with pytest.raises(sync_mod.SftpError):
        sync_mod.pull_batch(make_config(tmp_path), "batch_001.zip", tmp_path)
    with pytest.raises(sync_mod.SftpError):
        sync_mod.pull_batch(sync_mod.SyncConfig(), "batch_001.zip", tmp_path)


# ---------------------------------------------------------------------------
# Status formatting
# ---------------------------------------------------------------------------
def test_status_labels():
    assert "关闭" in sync_mod.SyncStatus(state="disabled", message="未配置").label()
    assert "待上传 3" in sync_mod.SyncStatus(state="pending", pending=3).label()
    assert "上传中" in sync_mod.SyncStatus(state="syncing", pending=1).label()
    assert "失败" in sync_mod.SyncStatus(state="error", attempts=2,
                                        last_error="x").label()
    assert "已上传 5 个文件" in sync_mod.SyncStatus(state="ok", uploaded=5,
                                                   last_sync=1.0).label()
    assert "尚未上传" in sync_mod.SyncStatus(state="idle").label()
    assert "上次" in sync_mod.SyncStatus(state="idle", last_sync=1.0).label()
