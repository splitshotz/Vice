"""AWS Signature Version 4, standard library only.

Two forms are covered: signing a request through its Authorization header,
and presigning a URL through its query string. Both follow
https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_sigv-create-signed-request.html
and are checked against the published AWS test-suite vectors in
tests/test_uploaders.py.

Paths given here must already be URI-encoded exactly as they go on the wire;
S3 signs that encoded path as is, without encoding it a second time. Query
parameters are given decoded and are encoded here.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
from typing import Iterable, Mapping, Optional, Sequence, Union
from urllib.parse import quote

ALGORITHM = "AWS4-HMAC-SHA256"
UNSIGNED_PAYLOAD = "UNSIGNED-PAYLOAD"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
MAX_PRESIGN_SECONDS = 7 * 24 * 3600

QueryItems = Union[Mapping[str, str], Sequence[tuple[str, str]], None]


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def uri_encode(value: str, safe: str = "") -> str:
    """RFC 3986 encoding as SigV4 wants it: unreserved characters stay,
    everything else is percent-encoded with upper-case hex."""
    return quote(value, safe="-_.~" + safe)


def encode_path(path: str) -> str:
    """Encode a decoded object path (slashes kept) for the wire and for signing."""
    if not path.startswith("/"):
        path = "/" + path
    return uri_encode(path, safe="/")


def amz_date(now: Optional[_dt.datetime] = None) -> str:
    now = now or _dt.datetime.now(_dt.timezone.utc)
    if now.tzinfo is not None:
        now = now.astimezone(_dt.timezone.utc)
    return now.strftime("%Y%m%dT%H%M%SZ")


def _items(query: QueryItems) -> list[tuple[str, str]]:
    if not query:
        return []
    if isinstance(query, Mapping):
        return [(str(k), "" if v is None else str(v)) for k, v in query.items()]
    return [(str(k), "" if v is None else str(v)) for k, v in query]


def canonical_query(query: QueryItems) -> str:
    pairs = sorted((uri_encode(k), uri_encode(v)) for k, v in _items(query))
    return "&".join(f"{k}={v}" for k, v in pairs)


def _canonical_headers(headers: Mapping[str, str]) -> tuple[str, str]:
    merged: dict[str, list[str]] = {}
    for name, value in headers.items():
        merged.setdefault(name.strip().lower(), []).append(" ".join(str(value).split()))
    names = sorted(merged)
    block = "".join(f"{n}:{','.join(merged[n])}\n" for n in names)
    return block, ";".join(names)


def signing_key(secret_key: str, date: str, region: str, service: str) -> bytes:
    key = ("AWS4" + secret_key).encode("utf-8")
    for part in (date, region, service, "aws4_request"):
        key = hmac.new(key, part.encode("utf-8"), hashlib.sha256).digest()
    return key


def _signature(
    secret_key: str,
    timestamp: str,
    scope: str,
    region: str,
    service: str,
    canonical_request: str,
) -> str:
    to_sign = "\n".join(
        [ALGORITHM, timestamp, scope, sha256_hex(canonical_request.encode("utf-8"))]
    )
    key = signing_key(secret_key, timestamp[:8], region, service)
    return hmac.new(key, to_sign.encode("utf-8"), hashlib.sha256).hexdigest()


def canonical_request(
    method: str,
    path: str,
    query: QueryItems,
    headers: Mapping[str, str],
    payload_hash: str,
) -> tuple[str, str]:
    """Return (canonical_request, signed_headers)."""
    block, signed = _canonical_headers(headers)
    text = "\n".join(
        [method.upper(), path or "/", canonical_query(query), block, signed, payload_hash]
    )
    return text, signed


def sign_headers(
    method: str,
    host: str,
    path: str,
    *,
    access_key: str,
    secret_key: str,
    region: str,
    service: str = "s3",
    query: QueryItems = None,
    headers: Optional[Mapping[str, str]] = None,
    payload_hash: str = UNSIGNED_PAYLOAD,
    timestamp: Optional[str] = None,
    content_sha_header: bool = True,
    session_token: Optional[str] = None,
) -> dict[str, str]:
    """Return the full header set for a signed request.

    host includes the port when it is not the scheme's default, exactly as the
    Host header goes out. Every header passed in is signed. For S3 the
    x-amz-content-sha256 header is required; content_sha_header=False leaves
    it out, which is what the generic AWS test vectors expect.
    """
    timestamp = timestamp or amz_date()
    out: dict[str, str] = {}
    for name, value in (headers or {}).items():
        out[name] = value
    out["host"] = host
    out["x-amz-date"] = timestamp
    if content_sha_header:
        out["x-amz-content-sha256"] = payload_hash
    if session_token:
        out["x-amz-security-token"] = session_token
    lowered = {k.lower(): v for k, v in out.items()}
    creq, signed = canonical_request(method, path, query, lowered, payload_hash)
    scope = f"{timestamp[:8]}/{region}/{service}/aws4_request"
    sig = _signature(secret_key, timestamp, scope, region, service, creq)
    out["Authorization"] = (
        f"{ALGORITHM} Credential={access_key}/{scope}, "
        f"SignedHeaders={signed}, Signature={sig}"
    )
    return out


def presign_query(
    method: str,
    host: str,
    path: str,
    *,
    access_key: str,
    secret_key: str,
    region: str,
    expires: int,
    service: str = "s3",
    query: QueryItems = None,
    timestamp: Optional[str] = None,
    session_token: Optional[str] = None,
) -> str:
    """Return the encoded query string of a presigned URL (no leading '?')."""
    expires = int(expires)
    if expires < 1 or expires > MAX_PRESIGN_SECONDS:
        raise ValueError("presigned URLs last between 1 second and 7 days")
    timestamp = timestamp or amz_date()
    scope = f"{timestamp[:8]}/{region}/{service}/aws4_request"
    params = _items(query) + [
        ("X-Amz-Algorithm", ALGORITHM),
        ("X-Amz-Credential", f"{access_key}/{scope}"),
        ("X-Amz-Date", timestamp),
        ("X-Amz-Expires", str(expires)),
        ("X-Amz-SignedHeaders", "host"),
    ]
    if session_token:
        params.append(("X-Amz-Security-Token", session_token))
    creq, _ = canonical_request(method, path, params, {"host": host}, UNSIGNED_PAYLOAD)
    sig = _signature(secret_key, timestamp, scope, region, service, creq)
    return canonical_query(params) + "&X-Amz-Signature=" + sig


def presign_url(
    method: str,
    scheme: str,
    host: str,
    path: str,
    **kwargs,
) -> str:
    return f"{scheme}://{host}{path}?" + presign_query(method, host, path, **kwargs)


def join_query(items: Iterable[tuple[str, str]]) -> str:
    """Encode query items the same way they were signed."""
    return "&".join(f"{uri_encode(k)}={uri_encode(v)}" for k, v in items)
