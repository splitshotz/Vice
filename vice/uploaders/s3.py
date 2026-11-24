"""S3-compatible object storage: AWS S3, Cloudflare R2, MinIO, Backblaze B2.

server keys:
  endpoint         https://s3.us-east-1.amazonaws.com, https://<acct>.r2.cloudflarestorage.com,
                   http://127.0.0.1:9000, https://s3.us-west-004.backblazeb2.com.
                   Empty means AWS for the region.
  region           "auto" for R2. When empty it is read from an AWS or B2 endpoint
                   name, else "auto" for R2 and "us-east-1" for anything else.
  bucket           bucket name
  prefix           key prefix, e.g. "vice/"; the key is prefix + filename
  public_base_url  when set, links are public_base_url/key instead of presigned
  presign_days     1 to 7, how long a presigned link lasts
  path_style       true puts the bucket in the path (MinIO wants this)
secret keys: access_key, secret_key, and optionally session_token.

Request bodies are signed with their real SHA-256 rather than UNSIGNED-PAYLOAD,
because not every S3 clone accepts an unsigned payload over plain HTTP.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

import aiohttp
from yarl import URL

from . import sigv4
from .base import (
    Provider,
    ProgressCallback,
    RemoteRef,
    UploadError,
    content_type_for,
    error_text,
    iter_bytes,
    iter_file,
    read_range,
)

MiB = 1024 * 1024
SINGLE_PUT_MAX = 64 * MiB
PART_SIZE = 16 * MiB
MAX_PARTS = 10000

_AWS_HOST = re.compile(r"^s3[.-](?:dualstack\.)?([a-z0-9-]+)\.amazonaws\.com$")
_B2_HOST = re.compile(r"^s3\.([a-z0-9-]+)\.backblazeb2\.com$")


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _xml_find(text: str, name: str) -> Optional[str]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    for el in root.iter():
        if _strip_ns(el.tag) == name:
            return (el.text or "").strip()
    return None


def _xml_error(text: str) -> Optional[str]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    if _strip_ns(root.tag) != "Error":
        return None
    code = message = ""
    for el in root:
        if _strip_ns(el.tag) == "Code":
            code = (el.text or "").strip()
        elif _strip_ns(el.tag) == "Message":
            message = (el.text or "").strip()
    return ": ".join(p for p in (code, message) if p) or "error"


def _sha256_file_range(path: Path, offset: int, length: int) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        fh.seek(offset)
        left = length
        while left > 0:
            data = fh.read(min(MiB, left))
            if not data:
                break
            digest.update(data)
            left -= len(data)
    return digest.hexdigest()


class S3Provider(Provider):
    kind = "s3"

    def __init__(self, server: dict, secret: dict, session: Optional[aiohttp.ClientSession] = None) -> None:
        super().__init__(server, secret, session)
        self.bucket = str(self.server.get("bucket", "")).strip()
        self.prefix = str(self.server.get("prefix", "") or "").lstrip("/")
        self.path_style = bool(self.server.get("path_style", False))
        self.public_base_url = str(self.server.get("public_base_url", "") or "").strip().rstrip("/")
        try:
            raw = self.server.get("presign_days")
            days = 7 if raw in (None, "") else int(raw)
        except (TypeError, ValueError):
            days = 7
        self.presign_days = min(7, max(1, days))
        self.access_key = str(self.secret.get("access_key", "")).strip()
        self.secret_key = str(self.secret.get("secret_key", "")).strip()
        self.session_token = str(self.secret.get("session_token", "") or "").strip() or None

        endpoint = str(self.server.get("endpoint", "") or "").strip().rstrip("/")
        region = str(self.server.get("region", "") or "").strip()
        if not endpoint:
            endpoint = f"https://s3.{region or 'us-east-1'}.amazonaws.com"
        if "://" not in endpoint:
            endpoint = "https://" + endpoint
        parts = urlsplit(endpoint)
        self.scheme = parts.scheme or "https"
        self.endpoint_host = parts.netloc
        self.endpoint_path = parts.path.rstrip("/")
        self.region = region or self._guess_region(parts.hostname or "")

    @staticmethod
    def _guess_region(hostname: str) -> str:
        hostname = hostname.lower()
        for pattern in (_AWS_HOST, _B2_HOST):
            m = pattern.match(hostname)
            if m:
                return m.group(1)
        if hostname.endswith(".r2.cloudflarestorage.com"):
            return "auto"
        return "us-east-1"

    # ---- addressing -------------------------------------------------

    def object_key(self, name: str) -> str:
        return self.prefix + name

    def _host_and_path(self, key: str = "") -> tuple[str, str]:
        """(host, encoded path) for the bucket, or an object in it."""
        key_path = sigv4.uri_encode(key, safe="/") if key else ""
        if self.path_style:
            path = f"{self.endpoint_path}/{sigv4.uri_encode(self.bucket)}"
            if key:
                path += "/" + key_path
            return self.endpoint_host, path
        host = f"{self.bucket}.{self.endpoint_host}"
        return host, f"{self.endpoint_path}/{key_path}"

    def _check_config(self) -> None:
        if not self.bucket:
            raise UploadError("S3 bucket is not set")
        if not self.access_key or not self.secret_key:
            raise UploadError("S3 access key and secret key are not set")

    # ---- requests ---------------------------------------------------

    async def _request(
        self,
        method: str,
        key: str = "",
        *,
        query: Optional[list[tuple[str, str]]] = None,
        headers: Optional[dict] = None,
        body=None,
        payload_hash: str = sigv4.EMPTY_SHA256,
        content_length: Optional[int] = None,
        ok: tuple[int, ...] = (200,),
    ) -> tuple[int, dict, str]:
        """Send one signed request and return (status, headers, text).

        A status outside ok raises UploadError carrying S3's own error code.
        S3 can also answer 200 with an <Error> body (CompleteMultipartUpload
        does), and that raises too.
        """
        self._check_config()
        host, path = self._host_and_path(key)
        signed = sigv4.sign_headers(
            method,
            host,
            path,
            access_key=self.access_key,
            secret_key=self.secret_key,
            region=self.region,
            query=query,
            headers=headers or {},
            payload_hash=payload_hash,
            session_token=self.session_token,
        )
        if content_length is not None:
            signed["Content-Length"] = str(content_length)
        qs = sigv4.canonical_query(query)
        url = URL(f"{self.scheme}://{host}{path}" + (f"?{qs}" if qs else ""), encoded=True)
        try:
            async with self.session.request(method, url, headers=signed, data=body) as resp:
                text = "" if method == "HEAD" else await resp.text(errors="replace")
                if resp.status not in ok:
                    detail = _xml_error(text) or text.strip()[:300]
                    raise UploadError(
                        f"S3 {method} failed: HTTP {resp.status}" + (f" {detail}" if detail else ""),
                        resp.status,
                    )
                detail = _xml_error(text) if text else None
                if detail:
                    raise UploadError(f"S3 {method} failed: {detail}", resp.status)
                return resp.status, dict(resp.headers), text
        except aiohttp.ClientError as exc:
            raise UploadError(f"could not reach {self.endpoint_host}: {exc}") from exc

    # ---- Provider ---------------------------------------------------

    async def test(self) -> str:
        await self._request("GET", query=[("list-type", "2"), ("max-keys", "1")])
        return f"bucket {self.bucket}"

    async def upload(
        self,
        path: Path,
        name: str,
        progress_cb: Optional[ProgressCallback] = None,
    ) -> RemoteRef:
        path = Path(path)
        size = os.path.getsize(path)
        key = self.object_key(name)
        ctype = content_type_for(name)
        if size <= SINGLE_PUT_MAX:
            await self._put_single(path, key, size, ctype, progress_cb)
        else:
            await self._put_multipart(path, key, size, ctype, progress_cb)
        return RemoteRef(remote_id=key, url=self._link(key), key=key)

    async def _put_single(self, path, key, size, ctype, progress_cb) -> None:
        digest = await asyncio.to_thread(_sha256_file_range, path, 0, size)
        await self._request(
            "PUT",
            key,
            headers={"content-type": ctype},
            body=iter_file(path, progress_cb, length=size),
            payload_hash=digest,
            content_length=size,
        )

    async def _put_multipart(self, path, key, size, ctype, progress_cb) -> None:
        _, _, text = await self._request(
            "POST", key, query=[("uploads", "")], headers={"content-type": ctype}
        )
        upload_id = _xml_find(text, "UploadId")
        if not upload_id:
            raise UploadError("S3 did not return an upload id for the multipart upload")
        part_size = max(PART_SIZE, math.ceil(size / MAX_PARTS))
        etags: list[tuple[int, str]] = []
        try:
            offset = 0
            number = 1
            while offset < size:
                length = min(part_size, size - offset)
                data = await read_range(path, offset, length)
                if len(data) != length:
                    raise UploadError(f"{path.name} got shorter while it was being uploaded")
                _, headers, _ = await self._request(
                    "PUT",
                    key,
                    query=[("partNumber", str(number)), ("uploadId", upload_id)],
                    body=iter_bytes(data, progress_cb, progress_base=offset, progress_total=size),
                    payload_hash=sigv4.sha256_hex(data),
                    content_length=length,
                )
                etag = {k.lower(): v for k, v in headers.items()}.get("etag")
                if not etag:
                    raise UploadError(f"S3 returned no ETag for part {number}")
                etags.append((number, etag))
                offset += length
                number += 1
            body = "<CompleteMultipartUpload>" + "".join(
                f"<Part><PartNumber>{n}</PartNumber><ETag>{_xml_escape(e)}</ETag></Part>"
                for n, e in etags
            ) + "</CompleteMultipartUpload>"
            raw = body.encode("utf-8")
            await self._request(
                "POST",
                key,
                query=[("uploadId", upload_id)],
                headers={"content-type": "application/xml"},
                body=raw,
                payload_hash=sigv4.sha256_hex(raw),
            )
        except BaseException:
            await self._abort(key, upload_id)
            raise

    async def _abort(self, key: str, upload_id: str) -> None:
        try:
            await asyncio.shield(
                self._request("DELETE", key, query=[("uploadId", upload_id)], ok=(200, 204, 404))
            )
        except BaseException:
            # The upload already failed; a stuck part list is cleaned up by the
            # bucket's lifecycle rule if it has one, and is not worth masking
            # the real error for.
            pass

    async def delete(self, ref: RemoteRef) -> None:
        key = ref.key or ref.remote_id
        if not key:
            raise UploadError("nothing to delete: the upload has no key")
        await self._request("DELETE", key, ok=(200, 204, 404))

    def _link(self, key: str) -> str:
        if self.public_base_url:
            return f"{self.public_base_url}/{sigv4.uri_encode(key, safe='/')}"
        return self.presigned_url(key)

    def presigned_url(self, key: str, method: str = "GET", expires: Optional[int] = None) -> str:
        self._check_config()
        host, path = self._host_and_path(key)
        return sigv4.presign_url(
            method,
            self.scheme,
            host,
            path,
            access_key=self.access_key,
            secret_key=self.secret_key,
            region=self.region,
            expires=expires or self.presign_days * 86400,
            session_token=self.session_token,
        )

    async def stream_url(self, ref: RemoteRef) -> str:
        return self._link(ref.key or ref.remote_id)

    async def download(self, ref: RemoteRef, dest: Path, progress_cb: Optional[ProgressCallback] = None) -> None:
        # Always through a fresh presigned URL: a public base URL may be a CDN
        # with its own caching, and the bucket is the source of truth.
        url = self.presigned_url(ref.key or ref.remote_id, expires=3600)
        await self._download_url(URL(url, encoded=True), dest, progress_cb)


def _xml_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )
