"""Zipline v4 (https://github.com/diced/zipline, the trunk branch).

server keys: url, folder (optional folder id), embed (bool)
secret keys: token

Written against the v4 source rather than its docs, which are thin. The facts
this relies on, and where they differ from what one might guess:

* Auth is the raw token in an `authorization` header, with no "Bearer".
* POST /api/upload takes every multipart file part whatever its field name;
  "file" is what Zipline's own clients send. The answer is
  {"files": [{"id", "name", "type", "url"}], "deletesAt"?, ...} and url is
  absolute (scheme from core.returnHttpsUrls, host from core.defaultDomain or
  the Host header).
* x-zipline-original-name is a boolean ("true") that keeps the original name
  on the file record; it does not pick the URL name. That is x-zipline-format.
* Large files go to a separate endpoint, POST /api/upload/partial, one file
  part per request, with:
      Content-Range: bytes <start>-<end inclusive>/<total>
      x-zipline-p-filename, x-zipline-p-content-type,
      x-zipline-p-content-length (the whole file's size),
      x-zipline-p-lastchunk: "true" only on the chunk that ends at total-1,
      x-zipline-p-token: the partialToken from the previous answer (absent on
      the first chunk, which must start at 0).
  There is no x-zipline-p-identifier in v4; the single-use token replaced it.
  Every answer but the last is {"files": [], "partialSuccess": true,
  "partialToken": "..."}; the last carries the file with "pending": true and
  no token.
* The last chunk only means the chunks were accepted: Zipline joins them in a
  background worker. Its progress is GET /api/user/files/incomplete, a list
  whose entries have status PENDING, PROCESSING, COMPLETE or FAILED and
  metadata.file.id equal to the file id. Until COMPLETE the file has no bytes,
  so upload() waits for it, or the caller could delete the only copy.
* The chunk size and the size above which clients should chunk are config
  strings such as "25mb" (chunks.size, chunks.max; defaults 25mb and 95mb),
  published without auth at GET /api/server/public along with
  files.maxFileSize (default 100mb), which caps chunked uploads as well.
* DELETE /api/user/files/{id} takes no body and answers with the deleted file.

embed is not sent to Zipline (v4 has no per-upload embed switch; embeds are a
per-user setting). It is kept in the server dict for the caller, which decides
whether a share link should be the /u/ page or the file.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import aiohttp

from .base import (
    Provider,
    ProgressCallback,
    RemoteRef,
    UploadError,
    content_type_for,
    error_text,
    iter_bytes,
    iter_file,
    multipart_body,
    read_range,
)

MiB = 1024 * 1024
DEFAULT_CHUNK_SIZE = 25 * MiB
DEFAULT_CHUNK_THRESHOLD = 95 * MiB

_SIZE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmgtp]?b?)?\s*$", re.IGNORECASE)
_UNITS = {"": 1, "b": 1, "k": 1024, "kb": 1024, "m": MiB, "mb": MiB,
          "g": 1024 ** 3, "gb": 1024 ** 3, "t": 1024 ** 4, "tb": 1024 ** 4,
          "p": 1024 ** 5, "pb": 1024 ** 5}


def parse_size(value) -> Optional[int]:
    """Turn a Zipline size ("25mb", "1gb", 1048576) into bytes.

    Zipline parses these with the `bytes` npm package, which counts in 1024s.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    m = _SIZE.match(str(value))
    if not m:
        return None
    unit = (m.group(2) or "").lower()
    if unit not in _UNITS:
        return None
    return int(float(m.group(1)) * _UNITS[unit])


class ZiplineProvider(Provider):
    kind = "zipline"

    # How often and how long to wait for Zipline to finish joining a chunked
    # upload. Tests shorten these.
    poll_interval = 1.0
    process_timeout = 30 * 60.0

    def __init__(self, server: dict, secret: dict, session: Optional[aiohttp.ClientSession] = None) -> None:
        super().__init__(server, secret, session)
        url = str(self.server.get("url", "") or "").strip().rstrip("/")
        if url and "://" not in url:
            url = "https://" + url
        self.base_url = url
        self.folder = str(self.server.get("folder", "") or "").strip()
        self.embed = bool(self.server.get("embed", False))
        self.token = str(self.secret.get("token", "") or "").strip()

    # ---- helpers ----------------------------------------------------

    def _check_config(self) -> None:
        if not self.base_url:
            raise UploadError("Zipline URL is not set")
        if not self.token:
            raise UploadError("Zipline token is not set")

    def _auth(self) -> dict:
        return {"authorization": self.token}

    def _upload_headers(self) -> dict:
        headers = self._auth()
        headers["x-zipline-original-name"] = "true"
        if self.folder:
            headers["x-zipline-folder"] = self.folder
        return headers

    def _absolute(self, url: str) -> str:
        if url.startswith("/"):
            return self.base_url + url
        return url

    async def _json(self, method: str, path: str, *, ok=(200,), **kwargs):
        url = self.base_url + path
        try:
            async with self.session.request(method, url, **kwargs) as resp:
                if resp.status not in ok:
                    raise UploadError(await self._describe_error(resp), resp.status)
                if resp.status != 200:
                    return None
                try:
                    return await resp.json(content_type=None)
                except ValueError as exc:
                    raise UploadError(f"Zipline sent a reply that is not JSON for {path}") from exc
        except aiohttp.ClientError as exc:
            raise UploadError(f"could not reach {self.base_url}: {exc}") from exc

    @staticmethod
    async def _describe_error(resp: aiohttp.ClientResponse) -> str:
        text = await error_text(resp)
        message = ""
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                message = str(data.get("error") or data.get("message") or "")
        except ValueError:
            message = text
        if resp.status == 401:
            return "Zipline rejected the token (HTTP 401)"
        return f"Zipline answered HTTP {resp.status}" + (f": {message}" if message else "")

    async def server_limits(self) -> dict:
        """Chunking settings the server publishes; defaults when it does not."""
        limits = {
            "chunks_enabled": True,
            "chunk_size": DEFAULT_CHUNK_SIZE,
            "chunk_threshold": DEFAULT_CHUNK_THRESHOLD,
            "max_file_size": None,
        }
        try:
            data = await self._json("GET", "/api/server/public")
        except UploadError:
            return limits
        if not isinstance(data, dict):
            return limits
        chunks = data.get("chunks") or {}
        if isinstance(chunks, dict):
            if "enabled" in chunks:
                limits["chunks_enabled"] = bool(chunks.get("enabled"))
            size = parse_size(chunks.get("size"))
            if size and size > 0:
                limits["chunk_size"] = size
            threshold = parse_size(chunks.get("max"))
            if threshold and threshold > 0:
                limits["chunk_threshold"] = threshold
        files = data.get("files") or {}
        if isinstance(files, dict):
            limits["max_file_size"] = parse_size(files.get("maxFileSize"))
        return limits

    # ---- Provider ---------------------------------------------------

    async def test(self) -> str:
        self._check_config()
        data = await self._json("GET", "/api/user", headers=self._auth())
        user = (data or {}).get("user") if isinstance(data, dict) else None
        if not isinstance(user, dict):
            raise UploadError("Zipline did not return a user for this token")
        return str(user.get("username") or user.get("id") or "connected")

    async def upload(
        self,
        path: Path,
        name: str,
        progress_cb: Optional[ProgressCallback] = None,
    ) -> RemoteRef:
        self._check_config()
        path = Path(path)
        size = os.path.getsize(path)
        limits = await self.server_limits()
        max_size = limits["max_file_size"]
        if max_size and size > max_size:
            raise UploadError(
                f"{name} is {size // MiB} MiB, over this Zipline server's "
                f"{max_size // MiB} MiB limit"
            )
        if limits["chunks_enabled"] and size > limits["chunk_threshold"]:
            return await self._upload_chunked(path, name, size, limits["chunk_size"], progress_cb)
        return await self._upload_single(path, name, size, progress_cb)

    @staticmethod
    def _first_file(data) -> dict:
        files = data.get("files") if isinstance(data, dict) else None
        if not files or not isinstance(files[0], dict):
            raise UploadError("Zipline accepted the upload but returned no file")
        return files[0]

    def _ref(self, info: dict) -> RemoteRef:
        file_id = str(info.get("id") or "")
        url = self._absolute(str(info.get("url") or ""))
        if not file_id or not url:
            raise UploadError("Zipline returned a file without an id or url")
        return RemoteRef(remote_id=file_id, url=url, key=str(info.get("name") or ""))

    async def _upload_single(self, path, name, size, progress_cb) -> RemoteRef:
        ctype = content_type_for(name)
        body, form_type, length = multipart_body(
            "file", name, ctype, iter_file(path, progress_cb, length=size), size
        )
        headers = self._upload_headers()
        headers["Content-Type"] = form_type
        headers["Content-Length"] = str(length)
        data = await self._json("POST", "/api/upload", headers=headers, data=body)
        return self._ref(self._first_file(data))

    async def _upload_chunked(self, path, name, size, chunk_size, progress_cb) -> RemoteRef:
        ctype = content_type_for(name)
        token: Optional[str] = None
        offset = 0
        data = None
        while offset < size:
            length = min(chunk_size, size - offset)
            end = offset + length - 1
            chunk = await read_range(path, offset, length)
            if len(chunk) != length:
                raise UploadError(f"{path.name} got shorter while it was being uploaded")
            body, form_type, body_len = multipart_body(
                "file",
                name,
                "application/octet-stream",
                iter_bytes(chunk, progress_cb, progress_base=offset, progress_total=size),
                length,
            )
            headers = self._upload_headers()
            headers.update({
                "Content-Type": form_type,
                "Content-Length": str(body_len),
                "Content-Range": f"bytes {offset}-{end}/{size}",
                "x-zipline-p-filename": name,
                "x-zipline-p-content-type": ctype,
                "x-zipline-p-content-length": str(size),
                "x-zipline-p-lastchunk": "true" if end == size - 1 else "false",
            })
            if token:
                headers["x-zipline-p-token"] = token
            data = await self._json("POST", "/api/upload/partial", headers=headers, data=body)
            if not isinstance(data, dict):
                raise UploadError("Zipline sent an unexpected reply to a chunk")
            offset = end + 1
            if offset < size:
                token = data.get("partialToken")
                if not token:
                    raise UploadError("Zipline did not return a token to continue the upload")
        ref = self._ref(self._first_file(data))
        await self._wait_processed(ref.remote_id)
        return ref

    async def _wait_processed(self, file_id: str) -> None:
        """Wait for Zipline to finish joining a chunked upload.

        A server that keeps no record of the job (or an older build without
        the endpoint) is taken at its word after a few looks.
        """
        deadline = time.monotonic() + self.process_timeout
        missing = 0
        while True:
            try:
                entries = await self._json("GET", "/api/user/files/incomplete", headers=self._auth())
            except UploadError as exc:
                if exc.status == 404:
                    return
                raise
            entry = None
            for item in entries if isinstance(entries, list) else []:
                meta = item.get("metadata") if isinstance(item, dict) else None
                f = meta.get("file") if isinstance(meta, dict) else None
                if isinstance(f, dict) and str(f.get("id")) == file_id:
                    entry = item
                    break
            if entry is None:
                missing += 1
                if missing >= 3:
                    return
            else:
                status = str(entry.get("status", "")).upper()
                if status == "COMPLETE":
                    return
                if status == "FAILED":
                    raise UploadError("Zipline could not put the uploaded chunks back together")
            if time.monotonic() >= deadline:
                raise UploadError("Zipline is still processing the upload; gave up waiting")
            await asyncio.sleep(self.poll_interval)

    async def delete(self, ref: RemoteRef) -> None:
        self._check_config()
        if not ref.remote_id:
            raise UploadError("nothing to delete: the upload has no Zipline id")
        await self._json(
            "DELETE",
            "/api/user/files/" + quote(ref.remote_id, safe=""),
            headers=self._auth(),
            ok=(200, 204, 404),
        )

    async def stream_url(self, ref: RemoteRef) -> str:
        return ref.url

    def _download_headers(self) -> dict:
        return self._auth()
