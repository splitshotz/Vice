"""Uploading clips to a media server (Zipline or S3-compatible storage).

Three stores live here, all keyed so a rename or delete in share.py can keep
them in step with the clips folder:

- ``uploads.json``: what is on a server, per clip slug. A record whose
  ``local`` is False is a cloud clip: the file is gone from disk and the
  library shows it from the record and a saved thumbnail.
- ``upload_secrets.json``: tokens and keys per server id. Kept out of
  config.toml, which the UI reads in full, and readable by its owner only.
- ``cloud_thumbs/``: the thumbnail of each cloud clip, copied before its
  local file is removed, since the thumbnail cache is purged with the file.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from .runtime import actual_home_dir

if TYPE_CHECKING:  # pragma: no cover
    from .share import ShareServer

log = logging.getLogger(__name__)

_DATA = actual_home_dir() / ".local" / "share" / "vice"
UPLOADS_PATH = _DATA / "uploads.json"
CLOUD_THUMB_DIR = _DATA / "cloud_thumbs"
UPLOAD_SECRETS_PATH = actual_home_dir() / ".config" / "vice" / "upload_secrets.json"

# Secret fields per server kind. Anything else sent with a secret is dropped.
SECRET_FIELDS = {
    "zipline": ("token",),
    "s3": ("access_key", "secret_key"),
}


def _write_private(path: Path, data) -> None:
    """Write-and-rename, owner-only, so a crash never truncates the file and
    nobody else on the machine can read a token or a link."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(json.dumps(data))
    tmp.replace(path)


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("%s is unreadable: %s", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def load_records() -> dict[str, dict]:
    return {str(k): v for k, v in _read_json(UPLOADS_PATH).items() if isinstance(v, dict)}


def save_records(records: dict[str, dict]) -> None:
    _write_private(UPLOADS_PATH, records)


def load_secrets() -> dict[str, dict]:
    return {str(k): v for k, v in _read_json(UPLOAD_SECRETS_PATH).items() if isinstance(v, dict)}


def save_secrets(secrets: dict[str, dict]) -> None:
    _write_private(UPLOAD_SECRETS_PATH, secrets)


def clean_secret(kind: str, raw) -> dict:
    if not isinstance(raw, dict):
        return {}
    return {k: str(raw[k]).strip() for k in SECRET_FIELDS.get(kind, ()) if raw.get(k)}


def has_secret(kind: str, secret: Optional[dict]) -> bool:
    secret = secret or {}
    return all(secret.get(k) for k in SECRET_FIELDS.get(kind, ()))


def reserved_names() -> set[str]:
    """Filenames held by cloud clips. A new recording must not take one, or
    the reused clip number would land on top of a clip that is on a server."""
    return {
        str(rec.get("name") or "")
        for rec in load_records().values()
        if not rec.get("local") and rec.get("name")
    }


def cloud_thumb_path(slug: str) -> Path:
    return CLOUD_THUMB_DIR / f"{slug}.jpg"


def snapshot_thumb(slug: str, source: Optional[Path]) -> None:
    if source is None or not source.exists():
        return
    try:
        CLOUD_THUMB_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, cloud_thumb_path(slug))
    except OSError as exc:
        log.warning("Could not keep the thumbnail of %s: %s", slug, exc)


def move_cloud_thumb(slug: str, new_slug: str) -> None:
    old = cloud_thumb_path(slug)
    if old.exists():
        try:
            old.replace(cloud_thumb_path(new_slug))
        except OSError as exc:
            log.debug("Could not move the cloud thumbnail of %s: %s", slug, exc)


def drop_cloud_thumb(slug: str) -> None:
    cloud_thumb_path(slug).unlink(missing_ok=True)


def remote_ref(record: dict):
    from .uploaders import RemoteRef
    return RemoteRef(
        remote_id=str(record.get("remote_id") or ""),
        url=str(record.get("url") or ""),
        key=str(record.get("key") or ""),
    )


# ── upload queue ──────────────────────────────────────────────────────────────

@dataclass
class UploadJob:
    slug: str
    server_id: str
    state: str = "queued"  # queued | uploading
    sent: int = 0
    total: int = 0
    task: Optional[asyncio.Task] = field(default=None, repr=False)


class UploadManager:
    """Runs uploads a few at a time and reports them over the WebSocket.

    Events: ``upload_queue`` whenever the queue changes, ``upload_progress``
    (throttled) while bytes go up, ``upload_done`` with the new record and
    ``upload_error`` with ``canceled`` set when the user stopped it.
    """

    PROGRESS_INTERVAL = 0.25

    def __init__(self, server: "ShareServer") -> None:
        self.server = server
        self.jobs: dict[str, UploadJob] = {}
        self._sem: Optional[asyncio.Semaphore] = None
        self._sem_size = 0
        self._session = None
        self._stopping = False

    # ── helpers ───────────────────────────────────────────────────────────────

    async def session(self):
        import aiohttp
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=300),
            )
        return self._session

    async def provider(self, server_cfg: dict):
        from .uploaders import make_provider
        secret = load_secrets().get(server_cfg.get("id", ""), {})
        return make_provider(server_cfg, secret, await self.session())

    def _semaphore(self) -> asyncio.Semaphore:
        size = int(getattr(self.server.cfg.upload, "concurrency", 2) or 2)
        # A new size takes effect once the queue has drained; swapping the
        # semaphore under running jobs would let more run than asked.
        if self._sem is None or (size != self._sem_size and not self.jobs):
            self._sem = asyncio.Semaphore(size)
            self._sem_size = size
        return self._sem

    def queue_state(self) -> list[dict]:
        return [
            {"slug": j.slug, "state": j.state, "sent": j.sent, "total": j.total,
             "server_id": j.server_id}
            for j in self.jobs.values()
        ]

    async def _broadcast_queue(self) -> None:
        await self.server.broadcast({"type": "upload_queue", "items": self.queue_state()})

    # ── public API ────────────────────────────────────────────────────────────

    async def enqueue(self, slugs: list[str], server_id: str) -> list[str]:
        accepted: list[str] = []
        for slug in slugs:
            if slug in self.jobs or slug not in self.server._clips:
                continue
            path = self.server._clips[slug]
            if not path.exists():
                continue
            job = UploadJob(slug=slug, server_id=server_id)
            self.jobs[slug] = job
            job.task = asyncio.create_task(self._run(job))
            accepted.append(slug)
        if accepted:
            await self._broadcast_queue()
        return accepted

    def cancel(self, slug: str) -> bool:
        job = self.jobs.get(slug)
        if job is None or job.task is None:
            return False
        job.task.cancel()
        return True

    def busy(self, slug: str) -> bool:
        return slug in self.jobs

    async def stop(self) -> None:
        self._stopping = True
        tasks = [j.task for j in self.jobs.values() if j.task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self._session is not None and not self._session.closed:
            await self._session.close()

    # ── the job ───────────────────────────────────────────────────────────────

    async def _run(self, job: UploadJob) -> None:
        srv = self.server
        try:
            async with self._semaphore():
                server_cfg = srv.upload_server(job.server_id)
                if server_cfg is None:
                    raise RuntimeError("That media server is no longer set up.")
                path = srv._clips.get(job.slug)
                if path is None or not path.exists():
                    raise RuntimeError("The clip file is gone.")
                job.state = "uploading"
                job.total = path.stat().st_size
                await self._broadcast_queue()

                last = 0.0

                def progress(sent: int, total: int) -> None:
                    nonlocal last
                    job.sent, job.total = sent, total or job.total
                    now = time.monotonic()
                    if now - last < self.PROGRESS_INTERVAL and sent < job.total:
                        return
                    last = now
                    asyncio.get_running_loop().create_task(srv.broadcast({
                        "type": "upload_progress", "slug": job.slug,
                        "sent": job.sent, "total": job.total,
                    }))

                provider = await self.provider(server_cfg)
                ref = await provider.upload(path, path.name, progress)
                # A rename while the bytes went up moves the job's clip.
                slug = srv.slug_for_path(path) or job.slug
                record = {
                    "server_id": server_cfg["id"],
                    "kind": server_cfg["kind"],
                    "remote_id": ref.remote_id,
                    "key": ref.key,
                    "url": ref.url,
                    "url_at": time.time(),
                    "name": path.name,
                    "size": job.total,
                    "uploaded_at": datetime.now().isoformat(timespec="seconds"),
                    "local": True,
                }
                await srv.on_uploaded(slug, record)
                if srv.cfg.upload.delete_after_upload:
                    await srv.delete_local(slug, keep_cloud=True)
                await srv.broadcast({"type": "upload_done", "slug": slug,
                                     "server_name": server_cfg.get("name", "")})
        except asyncio.CancelledError:
            if not self._stopping:
                await srv.broadcast({"type": "upload_error", "slug": job.slug,
                                     "message": "", "canceled": True})
            raise
        except Exception as exc:
            log.warning("Uploading %s failed: %s", job.slug, exc)
            await srv.broadcast({"type": "upload_error", "slug": job.slug,
                                 "message": str(exc) or exc.__class__.__name__,
                                 "canceled": False})
        finally:
            self.jobs.pop(job.slug, None)
            if not self._stopping:
                await self._broadcast_queue()
