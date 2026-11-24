"""Shared pieces of the remote upload providers.

A provider moves one clip file to a remote media server and back. The daemon
holds a single aiohttp session and hands it to every provider it builds, so a
provider only opens (and then owns) a session of its own when none is given.

Secrets never live in the server dict: that comes from config.toml, while the
secret dict comes from a separate 0600 JSON file.
"""

from __future__ import annotations

import asyncio
import mimetypes
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Callable, Optional

import aiohttp


ProgressCallback = Callable[[int, int], None]

# How much of a file is read per step while streaming it to a server.
STREAM_CHUNK = 1024 * 1024

_CONTENT_TYPES = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


@dataclass
class RemoteRef:
    """Where an uploaded file lives.

    remote_id is the provider's own handle (a Zipline file id, or the S3 object
    key), url is the link the server handed back or would hand back, and key
    is the stored name (Zipline file name, or S3 object key).
    """

    remote_id: str
    url: str
    key: str

    def to_dict(self) -> dict:
        return {"remote_id": self.remote_id, "url": self.url, "key": self.key}

    @classmethod
    def from_dict(cls, data: dict) -> "RemoteRef":
        return cls(
            remote_id=str(data.get("remote_id", "")),
            url=str(data.get("url", "")),
            key=str(data.get("key", "")),
        )


class UploadError(Exception):
    """Anything that stops an upload, test, delete or download.

    The message is meant for a person, so it says what the server said where
    it said anything. status is the HTTP status when there was one.
    """

    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


def content_type_for(name: str) -> str:
    ext = os.path.splitext(name)[1].lower()
    if ext in _CONTENT_TYPES:
        return _CONTENT_TYPES[ext]
    guessed, _ = mimetypes.guess_type(name)
    return guessed or "application/octet-stream"


def _read_range(path: Path, offset: int, size: int) -> bytes:
    with open(path, "rb") as fh:
        fh.seek(offset)
        return fh.read(size)


async def read_range(path: Path, offset: int, size: int) -> bytes:
    """Read part of a file without blocking the event loop."""
    return await asyncio.to_thread(_read_range, path, offset, size)


async def iter_file(
    path: Path,
    progress_cb: Optional[ProgressCallback] = None,
    *,
    offset: int = 0,
    length: Optional[int] = None,
    progress_base: int = 0,
    progress_total: Optional[int] = None,
    chunk_size: int = STREAM_CHUNK,
) -> AsyncIterator[bytes]:
    """Yield a file (or the slice offset..offset+length) in chunks.

    After each chunk progress_cb(sent, total) is called, where sent counts from
    progress_base. That lets a multipart or chunked upload report one running
    total for the whole file rather than restarting at zero per part.
    """
    if length is None:
        length = os.path.getsize(path) - offset
    total = progress_total if progress_total is not None else progress_base + length
    sent = 0
    fh = await asyncio.to_thread(open, path, "rb")
    try:
        await asyncio.to_thread(fh.seek, offset)
        while sent < length:
            data = await asyncio.to_thread(fh.read, min(chunk_size, length - sent))
            if not data:
                raise UploadError(f"{path.name} got shorter while it was being uploaded")
            sent += len(data)
            yield data
            if progress_cb is not None:
                progress_cb(progress_base + sent, total)
    finally:
        await asyncio.to_thread(fh.close)


async def iter_bytes(
    data: bytes,
    progress_cb: Optional[ProgressCallback] = None,
    *,
    progress_base: int = 0,
    progress_total: Optional[int] = None,
    chunk_size: int = STREAM_CHUNK,
) -> AsyncIterator[bytes]:
    """Like iter_file, for bytes already in memory."""
    total = progress_total if progress_total is not None else progress_base + len(data)
    view = memoryview(data)
    for start in range(0, len(data), chunk_size):
        piece = bytes(view[start:start + chunk_size])
        yield piece
        if progress_cb is not None:
            progress_cb(progress_base + start + len(piece), total)


def _quote_header_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\r", "").replace("\n", "")


def multipart_body(
    field: str,
    filename: str,
    content_type: str,
    body: AsyncIterator[bytes],
    body_length: int,
) -> tuple[AsyncIterator[bytes], str, int]:
    """Wrap a streamed body as a one-file multipart/form-data request.

    Returns (stream, content_type_header, content_length). The length is exact,
    so the request goes out with Content-Length rather than chunked encoding,
    which some reverse proxies in front of media servers refuse.
    """
    boundary = "vice" + os.urandom(16).hex()
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{_quote_header_value(field)}"; '
        f'filename="{_quote_header_value(filename)}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode("utf-8")
    tail = f"\r\n--{boundary}--\r\n".encode("ascii")

    async def stream() -> AsyncIterator[bytes]:
        yield head
        async for chunk in body:
            yield chunk
        yield tail

    return stream(), f"multipart/form-data; boundary={boundary}", len(head) + body_length + len(tail)


async def error_text(resp: aiohttp.ClientResponse, limit: int = 300) -> str:
    try:
        text = (await resp.text(errors="replace")).strip()
    except Exception:
        text = ""
    if len(text) > limit:
        text = text[:limit] + "..."
    return text


class Provider(ABC):
    """One configured remote server.

    server is the non-secret config (from config.toml), secret holds the
    credentials (from the 0600 secrets file). session is shared and owned by
    the caller; when none is given the provider opens its own and close()
    closes it.
    """

    kind = ""

    def __init__(
        self,
        server: dict,
        secret: dict,
        session: Optional[aiohttp.ClientSession] = None,
    ) -> None:
        self.server = dict(server or {})
        self.secret = dict(secret or {})
        self._session = session
        self._own_session = session is None

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=300)
            )
            self._own_session = True
        return self._session

    async def close(self) -> None:
        if self._own_session and self._session is not None and not self._session.closed:
            await self._session.close()

    async def __aenter__(self) -> "Provider":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    @abstractmethod
    async def test(self) -> str:
        """Check the settings and credentials; return a short description
        such as the account name or bucket."""

    @abstractmethod
    async def upload(
        self,
        path: Path,
        name: str,
        progress_cb: Optional[ProgressCallback] = None,
    ) -> RemoteRef:
        """Upload path under name, calling progress_cb(sent, total) as bytes go."""

    @abstractmethod
    async def delete(self, ref: RemoteRef) -> None:
        """Remove an uploaded file. A file that is already gone is not an error."""

    @abstractmethod
    async def stream_url(self, ref: RemoteRef) -> str:
        """A URL a browser can play the file from right now."""

    async def download(
        self,
        ref: RemoteRef,
        dest: Path,
        progress_cb: Optional[ProgressCallback] = None,
    ) -> None:
        """Fetch the file back to dest, writing a .part file and renaming it
        into place only once it is complete."""
        url = await self.stream_url(ref)
        await self._download_url(url, dest, progress_cb, headers=self._download_headers())

    def _download_headers(self) -> dict:
        return {}

    async def _download_url(
        self,
        url: str,
        dest: Path,
        progress_cb: Optional[ProgressCallback],
        headers: Optional[dict] = None,
    ) -> None:
        dest = Path(dest)
        tmp = dest.with_name(dest.name + ".part")
        try:
            async with self.session.get(url, headers=headers or {}) as resp:
                if resp.status != 200:
                    raise UploadError(
                        f"download failed: HTTP {resp.status} {await error_text(resp)}".strip(),
                        resp.status,
                    )
                total = resp.content_length or 0
                got = 0
                fh = await asyncio.to_thread(open, tmp, "wb")
                try:
                    async for chunk in resp.content.iter_chunked(STREAM_CHUNK):
                        await asyncio.to_thread(fh.write, chunk)
                        got += len(chunk)
                        if progress_cb is not None:
                            progress_cb(got, max(total, got))
                finally:
                    await asyncio.to_thread(fh.close)
                if total and got != total:
                    raise UploadError(f"download was cut short ({got} of {total} bytes)")
            os.replace(tmp, dest)
        except aiohttp.ClientError as exc:
            raise UploadError(f"download failed: {exc}") from exc
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass


def make_provider(
    server: dict,
    secret: dict,
    session: Optional[aiohttp.ClientSession] = None,
) -> Provider:
    kind = str((server or {}).get("kind", "")).strip().lower()
    if kind == "zipline":
        from .zipline import ZiplineProvider
        return ZiplineProvider(server, secret, session)
    if kind == "s3":
        from .s3 import S3Provider
        return S3Provider(server, secret, session)
    raise UploadError(f"unknown upload server kind: {kind or '(none)'}")
