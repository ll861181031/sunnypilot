"""Background SFTP sync of labeler output to the Linux inbox.

Design notes
------------
* The network work runs in a daemon thread; the Tk main loop only reads a
  locked :class:`SyncStatus` snapshot (never blocks on sockets).
* :class:`SyncScheduler` is a pure state machine (debounce, periodic refresh,
  exponential backoff) so it can be unit tested with a fake clock.
* ``paramiko`` is optional.  Missing module, missing ``sync.json`` or
  ``auto_upload: false`` degrade to "local save only" instead of crashing.

Remote layout (agreed with the dataset pipeline)::

    <remote_dir>/labels/<id>.txt     # only files changed since last upload
    <remote_dir>/review_state.json
    <remote_dir>/sync_manifest.json  # written last, describes the batch
"""

from __future__ import annotations

import json
import os
import posixpath
import socket
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from collections.abc import Callable, Iterable, Sequence

try:  # pragma: no cover - exercised through monkeypatching in tests
    import paramiko
except Exception:  # pragma: no cover - optional dependency
    paramiko = None  # type: ignore[assignment]

APP_VERSION = "1.0.0"
SCHEMA_VERSION = 1

DEFAULT_DEBOUNCE = 3.0
DEFAULT_PERIOD = 60.0
DEFAULT_BACKOFF_BASE = 2.0
DEFAULT_BACKOFF_MAX = 120.0
DEFAULT_TIMEOUT = 10.0

MANIFEST_NAME = "sync_manifest.json"
REVIEW_STATE_NAME = "review_state.json"

#: (local path, path relative to the remote batch directory)
UploadItem = tuple[Path, str]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
@dataclass
class SyncConfig:
    """Contents of ``sync.json`` (kept next to ``labeler.py``)."""

    host: str = ""
    port: int = 22
    user: str = ""
    key_path: str = ""
    password: str = ""
    remote_dir: str = ""
    auto_upload: bool = True
    timeout: float = DEFAULT_TIMEOUT

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> SyncConfig:
        return cls(
            host=str(payload.get("host") or ""),
            port=int(payload.get("port") or 22),
            user=str(payload.get("user") or ""),
            key_path=str(payload.get("key_path") or ""),
            password=str(payload.get("password") or ""),
            remote_dir=str(payload.get("remote_dir") or ""),
            auto_upload=bool(payload.get("auto_upload", True)),
            timeout=float(payload.get("timeout") or DEFAULT_TIMEOUT),
        )

    @classmethod
    def load(cls, path: os.PathLike | str) -> SyncConfig:
        try:
            with open(path, encoding="utf-8") as handle:
                payload = json.load(handle)
        except FileNotFoundError:
            return cls()
        except (OSError, ValueError):
            return cls()
        if not isinstance(payload, dict):
            return cls()
        return cls.from_dict(payload)

    @classmethod
    def find(cls, app_dir: os.PathLike | str) -> tuple[SyncConfig, Path | None]:
        """Load ``sync.json`` from *app_dir*, falling back to the cwd."""
        app_dir = Path(app_dir)
        for candidate in (app_dir / "sync.json", Path.cwd() / "sync.json"):
            if candidate.is_file():
                return cls.load(candidate), candidate
        return cls(), None

    def is_configured(self) -> bool:
        return bool(self.host and self.user and self.remote_dir)

    def resolve_remote_dir(self, batch: str) -> str:
        """Substitute ``<batch>`` / ``{batch}`` in the configured remote dir."""
        remote = self.remote_dir or ""
        remote = remote.replace("<batch>", batch).replace("{batch}", batch)
        return remote

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "key_path": self.key_path,
            "remote_dir": self.remote_dir,
            "auto_upload": self.auto_upload,
            "timeout": self.timeout,
        }

    def sanitized(self) -> dict[str, Any]:
        """For logs: never leak the password."""
        payload = self.to_dict()
        payload["password"] = "<set>" if self.password else ""
        return payload


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------
@dataclass
class SyncStatus:
    """Thread-safe snapshot for the status bar."""

    state: str = "disabled"  # disabled|idle|pending|syncing|ok|error
    message: str = ""
    last_sync: float | None = None
    attempts: int = 0
    pending: int = 0
    uploaded: int = 0
    remote_dir: str = ""
    last_error: str = ""

    def label(self) -> str:
        if self.state == "disabled":
            return f"同步: 关闭（{self.message or '未配置'}）"
        if self.state == "syncing":
            return f"同步: 上传中… 待传 {self.pending}"
        if self.state == "error":
            return f"同步: 失败（重试 {self.attempts} 次）{self.last_error[:60]}"
        if self.state == "pending":
            return f"同步: 待上传 {self.pending} 张"
        if self.state == "ok":
            stamp = time.strftime("%H:%M:%S", time.localtime(self.last_sync or 0))
            return f"同步: 已上传 {self.uploaded} 个文件 · 上次 {stamp}"
        if self.last_sync is None:
            return "同步: 空闲（尚未上传）"
        stamp = time.strftime("%H:%M:%S", time.localtime(self.last_sync))
        return f"同步: 空闲 · 上次 {stamp}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "message": self.message,
            "last_sync": self.last_sync,
            "attempts": self.attempts,
            "pending": self.pending,
            "uploaded": self.uploaded,
            "remote_dir": self.remote_dir,
            "last_error": self.last_error,
        }


# ---------------------------------------------------------------------------
# Scheduler (pure, unit tested with a fake clock)
# ---------------------------------------------------------------------------
class SyncScheduler:
    """Decides *when* to upload.

    The scheduler never touches the network: :meth:`take_due` returns
    ``"dirty"`` / ``"periodic"`` when an upload should start, and the caller
    reports back through :meth:`success` / :meth:`failure`.
    """

    def __init__(self, now: float, debounce: float = DEFAULT_DEBOUNCE,
                 period: float = DEFAULT_PERIOD,
                 backoff_base: float = DEFAULT_BACKOFF_BASE,
                 backoff_max: float = DEFAULT_BACKOFF_MAX) -> None:
        self.debounce = max(0.0, float(debounce))
        self.period = max(0.0, float(period))
        self.backoff_base = max(0.1, float(backoff_base))
        self.backoff_max = max(self.backoff_base, float(backoff_max))

        self.pending: set[str] = set()
        self.due_at: float | None = None
        self.next_period_at: float | None = None
        self.in_flight = False
        self.attempts = 0
        self.last_error = ""
        self.last_sync: float | None = None
        self.changed_since_sync = False
        self._now = float(now)
        if self.period > 0.0:
            self.next_period_at = self._now + self.period

    # -- events -----------------------------------------------------------
    def mark_dirty(self, image_ids: Iterable[str], now: float,
                   schedule: bool = True) -> None:
        """Record changed images.

        *schedule* is False when automatic uploads are disabled
        (``auto_upload: false``): the ids are still tracked so a manual
        "sync now" can push them, but no debounce timer is armed.
        """
        ids = {str(i) for i in image_ids}
        if not ids:
            return
        self.pending |= ids
        self.changed_since_sync = True
        if not schedule:
            return
        due = float(now) + self.debounce
        if self.due_at is None or due < self.due_at:
            self.due_at = due

    def force(self, now: float) -> None:
        """Manual / shutdown upload: ignore the debounce delay."""
        self.due_at = float(now)

    # -- driver -----------------------------------------------------------
    def take_due(self, now: float) -> str | None:
        """Return ``"dirty"``/``"periodic"`` when an upload should start now."""
        now = float(now)
        self._now = now
        if self.in_flight:
            return None
        if self.pending and self.due_at is not None and now >= self.due_at:
            self.in_flight = True
            return "dirty"
        if self.period > 0.0 and self.next_period_at is not None \
                and now >= self.next_period_at:
            self.next_period_at = now + self.period
            if self.changed_since_sync:
                self.in_flight = True
                return "periodic"
        return None

    def success(self, now: float, uploaded: int = 0) -> None:
        now = float(now)
        self.pending.clear()
        self.due_at = None
        self.in_flight = False
        self.attempts = 0
        self.last_error = ""
        self.last_sync = now
        self.changed_since_sync = False
        if self.period > 0.0:
            self.next_period_at = now + self.period

    def failure(self, now: float, error: str) -> None:
        now = float(now)
        self.attempts += 1
        self.in_flight = False
        self.last_error = str(error)[:300]
        delay = min(self.backoff_base * (2 ** (self.attempts - 1)), self.backoff_max)
        self.due_at = now + delay

    def next_wake(self, now: float) -> float:
        """Seconds until the next scheduled action (``0`` = right now)."""
        now = float(now)
        if self.in_flight:
            return 0.25
        candidates: list[float] = []
        if self.pending and self.due_at is not None:
            candidates.append(max(0.0, self.due_at - now))
        if self.period > 0.0 and self.next_period_at is not None:
            candidates.append(max(0.0, self.next_period_at - now))
        if not candidates:
            return 1.0
        return max(0.01, min(candidates))

    # -- introspection ----------------------------------------------------
    @property
    def state(self) -> str:
        if self.in_flight:
            return "syncing"
        if self.last_error:
            return "error"
        if self.pending:
            return "pending"
        if self.last_sync is not None:
            return "ok"
        return "idle"


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------
def _iso_utc(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp))


def build_manifest(batch: str, items: Sequence[UploadItem] = (),
                   counts: dict[str, Any] | None = None,
                   last_sync: float | None = None,
                   app_version: str = APP_VERSION, pending: int = 0,
                   remote_dir: str = "", extra: dict[str, Any] | None = None
                   ) -> dict[str, Any]:
    """Build the ``sync_manifest.json`` payload."""
    now = time.time()  # noqa: TID251 — manifest timestamps are wall clock
    files: list[dict[str, Any]] = []
    for local, relative in items:
        entry: dict[str, Any] = {"path": relative}
        try:
            stat = Path(local).stat()
            entry["size"] = int(stat.st_size)
            entry["mtime"] = round(float(stat.st_mtime), 3)
        except OSError:
            entry["size"] = None
            entry["mtime"] = None
        files.append(entry)
    payload: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "app_version": app_version,
        "batch": str(batch),
        "last_sync": _iso_utc(last_sync if last_sync is not None else now),
        "last_sync_unix": float(last_sync if last_sync is not None else now),
        "host": socket.gethostname(),
        "pending": int(pending),
        "remote_dir": remote_dir,
        "counts": counts or {},
        "files": files,
    }
    if extra:
        payload.update(extra)
    return payload


# ---------------------------------------------------------------------------
# SFTP uploader
# ---------------------------------------------------------------------------
class SftpError(RuntimeError):
    """Upload failed (network, auth, permissions, ...)."""


class SftpUploader:
    """Thin paramiko SFTP wrapper used by :class:`SyncWorker`."""

    def __init__(self, config: SyncConfig, logger: Callable[[str], None] | None = None):
        if paramiko is None:  # pragma: no cover - guarded by SyncWorker
            raise SftpError("paramiko 未安装")
        self.config = config
        self.logger = logger or (lambda message: None)
        self._client = None
        self._sftp = None

    # -- connection -------------------------------------------------------
    def _connect(self):
        config = self.config
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kwargs: dict[str, Any] = {
            "hostname": config.host,
            "port": config.port,
            "username": config.user,
            "timeout": config.timeout,
            "banner_timeout": config.timeout,
            "auth_timeout": config.timeout,
            "look_for_keys": False,
            "allow_agent": False,
        }
        if config.key_path:
            key_path = os.path.expanduser(os.path.expandvars(config.key_path))
            if not os.path.isfile(key_path):
                raise SftpError(f"私钥不存在: {key_path}")
            kwargs["key_filename"] = key_path
        if config.password:
            kwargs["password"] = config.password
        try:
            client.connect(**kwargs)
        except Exception as exc:  # paramiko raises many types
            raise SftpError(f"SSH 连接失败: {exc}") from exc
        try:
            sftp = client.open_sftp()
        except Exception as exc:
            client.close()
            raise SftpError(f"打开 SFTP 失败: {exc}") from exc
        self._client = client
        self._sftp = sftp
        self.logger(f"已连接 {config.user}@{config.host}:{config.port}")
        return sftp

    def close(self) -> None:
        for handle in (self._sftp, self._client):
            try:
                if handle is not None:
                    handle.close()
            except Exception:
                pass
        self._sftp = None
        self._client = None

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def ensure_remote_dir(sftp, remote_dir: str) -> None:
        """``mkdir -p`` over SFTP (paramiko has no recursive helper)."""
        if not remote_dir or remote_dir in ("/", "."):
            return
        parts = [p for p in remote_dir.split("/") if p]
        current = "/" if remote_dir.startswith("/") else ""
        for part in parts:
            current = posixpath.join(current, part) if current else part
            try:
                sftp.stat(current)
            except OSError:
                try:
                    sftp.mkdir(current)
                except OSError as exc:
                    # Someone may have created it in between; only fail if it
                    # still does not exist.
                    try:
                        sftp.stat(current)
                    except OSError:
                        raise SftpError(f"无法创建远端目录 {current}: {exc}") from exc

    @staticmethod
    def write_remote_text(sftp, remote_path: str, text: str) -> None:
        with sftp.open(remote_path, "w") as handle:
            handle.write(text)

    # -- public -----------------------------------------------------------
    def upload(self, items: Sequence[UploadItem], remote_dir: str,
               manifest: dict[str, Any]) -> dict[str, Any]:
        """Upload *items* then the manifest; returns a small result dict."""
        sftp = self._sftp or self._connect()
        try:
            self.ensure_remote_dir(sftp, remote_dir)
            uploaded: list[str] = []
            for local, relative in items:
                local_path = Path(local)
                if not local_path.is_file():
                    continue
                remote_path = posixpath.join(remote_dir, relative)
                parent = posixpath.dirname(remote_path)
                if parent and parent != remote_dir:
                    self.ensure_remote_dir(sftp, parent)
                sftp.put(str(local_path), remote_path)
                uploaded.append(relative)
            payload = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
            self.write_remote_text(sftp, posixpath.join(remote_dir, MANIFEST_NAME),
                                   payload)
            uploaded.append(MANIFEST_NAME)
            self.logger(f"已上传 {len(uploaded)} 个文件 -> {remote_dir}")
            return {"uploaded": uploaded, "remote_dir": remote_dir}
        except SftpError:
            self.close()
            raise
        except Exception as exc:
            self.close()
            raise SftpError(f"上传失败: {exc}") from exc


# ---------------------------------------------------------------------------
# Worker thread
# ---------------------------------------------------------------------------
class SyncWorker:
    """Owns the background thread, the scheduler and the uploader."""

    def __init__(self, config: SyncConfig, dataset_root: os.PathLike | str,
                 batch: str,
                 extra_files: Sequence[str] = (REVIEW_STATE_NAME,),
                 uploader_factory: Callable[[SyncConfig], Any] | None = None,
                 counts_provider: Callable[[], dict[str, Any]] | None = None,
                 debounce: float = DEFAULT_DEBOUNCE,
                 period: float = DEFAULT_PERIOD,
                 backoff_base: float = DEFAULT_BACKOFF_BASE,
                 backoff_max: float = DEFAULT_BACKOFF_MAX,
                 clock: Callable[[], float] = time.monotonic,
                 logger: Callable[[str], None] | None = None,
                 app_version: str = APP_VERSION) -> None:
        self.config = config
        self.dataset_root = Path(dataset_root)
        self.batch = str(batch)
        self.extra_files = tuple(extra_files)
        self.uploader_factory = uploader_factory or (lambda cfg: SftpUploader(cfg))
        self.counts_provider = counts_provider or (dict)
        self.clock = clock
        self.logger = logger or (lambda message: None)
        self.app_version = app_version

        self.remote_dir = config.resolve_remote_dir(self.batch) if config.is_configured() else ""
        # No periodic timer when automatic uploads are switched off: pending
        # ids are then pushed only by the manual button or at shutdown.
        scheduler_period = period if config.auto_upload else 0.0
        self._scheduler = SyncScheduler(now=clock(), debounce=debounce,
                                        period=scheduler_period,
                                        backoff_base=backoff_base,
                                        backoff_max=backoff_max)
        self._uploader = None
        self._lock = threading.Lock()
        self._upload_lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._uploaded_total = 0
        # Scheduler times use the monotonic clock; the status bar and manifest
        # need wall clock, so keep one explicit timestamp for them.
        self._last_sync_wall: float | None = None
        self._status = SyncStatus(state="idle", remote_dir=self.remote_dir)

        # Decide up front whether syncing is possible at all.
        self.disabled_reason = ""
        if paramiko is None:
            self.disabled_reason = "paramiko 未安装"
        elif not config.is_configured():
            self.disabled_reason = "未配置 sync.json"
        if self.disabled_reason:
            self._status = SyncStatus(state="disabled", message=self.disabled_reason,
                                      remote_dir="")
            self.logger(f"同步已禁用: {self.disabled_reason}")

    # -- properties -------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return not self.disabled_reason

    @property
    def auto_enabled(self) -> bool:
        return self.enabled and bool(self.config.auto_upload)

    @property
    def pending(self) -> int:
        return len(self._scheduler.pending)

    # -- lifecycle --------------------------------------------------------
    def disable(self, reason: str) -> None:
        """Force-disable syncing with an explicit reason (e.g. ``--no-sync``)."""
        self.disabled_reason = str(reason)
        self._status = SyncStatus(state="disabled", message=self.disabled_reason)

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="labeler-sync",
                                        daemon=True)
        self._thread.start()
        self.logger("同步线程已启动")

    def shutdown(self, flush: bool = True, timeout: float = 15.0) -> bool:
        """Stop the thread and optionally push pending work synchronously."""
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
            self._thread = None
        ok = True
        if flush and self.enabled and self._scheduler.pending:
            ok = self.flush()
        self._close_uploader()
        return ok

    def flush(self) -> bool:
        """Try one upload right now, in the calling thread."""
        if not self.enabled:
            return False
        now = self.clock()
        if not self._scheduler.pending and not self._scheduler.changed_since_sync:
            return True
        if self._scheduler.in_flight:
            # Let the worker finish; do not race it.
            thread = self._thread
            if thread is not None:
                thread.join(timeout=5.0)
        self._scheduler.force(now)
        action = self._scheduler.take_due(self.clock())
        if action is None:
            return False
        return self._do_upload(action)

    # -- triggers ---------------------------------------------------------
    def mark_dirty(self, image_ids: Iterable[str]) -> None:
        if not self.enabled:
            return
        ids = [str(i) for i in image_ids]
        self._scheduler.mark_dirty(ids, self.clock(), schedule=self.auto_enabled)
        self._refresh_status()
        self._wake.set()

    def request_sync(self) -> None:
        """Manual "sync now" button (works even with ``auto_upload: false``)."""
        if not self.enabled:
            return
        self._scheduler.force(self.clock())
        self._refresh_status()
        self._wake.set()
        if self._thread is None:
            self.start()

    # -- status -----------------------------------------------------------
    def status(self) -> SyncStatus:
        with self._lock:
            return SyncStatus(**self._status.__dict__)

    def _refresh_status(self) -> None:
        scheduler = self._scheduler
        state = scheduler.state
        if not self.enabled:
            state = "disabled"
        message = self.disabled_reason
        status = SyncStatus(
            state=state,
            message=message,
            last_sync=self._last_sync_wall,
            attempts=scheduler.attempts,
            pending=len(scheduler.pending),
            uploaded=self._uploaded_total,
            remote_dir=self.remote_dir,
            last_error=scheduler.last_error,
        )
        with self._lock:
            if self._status.state == "disabled" and self.disabled_reason:
                return
            self._status = status

    # -- internals --------------------------------------------------------
    def _run(self) -> None:
        while not self._stop.is_set():
            now = self.clock()
            action = self._scheduler.take_due(now)
            if action is not None:
                self._do_upload(action)
                continue
            timeout = min(self._scheduler.next_wake(now), 1.0)
            self._wake.wait(max(0.05, timeout))
            self._wake.clear()

    def _items_for(self, image_ids: Sequence[str]) -> list[UploadItem]:
        items: list[UploadItem] = []
        for image_id in sorted(image_ids):
            local = self.dataset_root / "labels" / f"{image_id}.txt"
            items.append((local, f"labels/{image_id}.txt"))
        for name in self.extra_files:
            local = self.dataset_root / name
            if local.is_file():
                items.append((local, name))
        return items

    def _do_upload(self, action: str) -> bool:
        with self._upload_lock:
            scheduler = self._scheduler
            pending_ids = sorted(scheduler.pending)
            items = self._items_for(pending_ids)
            manifest = build_manifest(
                batch=self.batch,
                items=items,
                counts=self.counts_provider(),
                last_sync=self._last_sync_wall,
                app_version=self.app_version,
                pending=len(pending_ids),
                remote_dir=self.remote_dir,
                extra={"trigger": action},
            )
            try:
                uploader = self._get_uploader()
                result = uploader.upload(items, self.remote_dir, manifest)
            except Exception as exc:  # network errors must never kill the thread
                scheduler.failure(self.clock(), str(exc))
                self.logger(f"同步失败: {exc}")
                self._refresh_status()
                return False
            uploaded = int(len(result.get("uploaded", []))) if isinstance(result, dict) else 0
            scheduler.success(self.clock(), uploaded=uploaded)
            self._last_sync_wall = time.time()  # noqa: TID251 — display only
            self._uploaded_total += uploaded
            self._refresh_status()
            return True

    def _get_uploader(self):
        if self._uploader is None:
            self._uploader = self.uploader_factory(self.config)
        return self._uploader

    def _close_uploader(self) -> None:
        uploader = self._uploader
        self._uploader = None
        if uploader is not None:
            try:
                close = getattr(uploader, "close", None)
                if callable(close):
                    close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Optional: pull a new batch from the inbox
# ---------------------------------------------------------------------------
def safe_extract_zip(zip_path: os.PathLike | str, dest_dir: os.PathLike | str) -> Path:
    """Extract *zip_path* into *dest_dir*, refusing path traversal entries."""
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.infolist():
            name = member.filename
            if name.startswith(("/", "\\")) or ".." in Path(name).parts:
                raise SftpError(f"压缩包包含非法路径: {name}")
        archive.extractall(dest)
    return dest


def pull_batch(config: SyncConfig, remote_zip: str, dest_root: os.PathLike | str,
               batch: str | None = None) -> Path:
    """Download ``<remote_dir>/<remote_zip>`` and extract it locally.

    Returns the extracted batch directory.  Implemented as a plain
    synchronous helper (call it from a worker thread if the UI must stay
    responsive).
    """
    if paramiko is None:
        raise SftpError("paramiko 未安装，无法拉取批次")
    if not config.is_configured():
        raise SftpError("未配置 sync.json，无法拉取批次")
    remote_dir = config.resolve_remote_dir(batch or Path(remote_zip).stem)
    uploader = SftpUploader(config)
    sftp = uploader._connect()
    import tempfile

    tmp_dir = Path(tempfile.mkdtemp(prefix="labeler_pull_"))
    local_zip = tmp_dir / Path(remote_zip).name
    try:
        sftp.get(posixpath.join(remote_dir, remote_zip), str(local_zip))
    except Exception as exc:
        raise SftpError(f"下载 {remote_zip} 失败: {exc}") from exc
    finally:
        uploader.close()
    return safe_extract_zip(local_zip, dest_root)
