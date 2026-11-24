"""The remote upload providers: SigV4 signing, Zipline v4 and S3.

The signer is checked against vectors AWS publishes. The providers run against
small fake servers on 127.0.0.1 that check the requests the way the real
servers would, so these tests say nothing about a real Zipline or bucket, only
that we speak the protocol we coded to.
"""

import hashlib
import hmac
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

try:
    import _sandbox  # noqa: F401  keeps every store out of the real home
except ModuleNotFoundError:
    from tests import _sandbox  # noqa: F401

from vice.uploaders import sigv4

try:
    from aiohttp import ClientSession, web
    from aiohttp.test_utils import TestServer
    from yarl import URL

    from vice.uploaders import RemoteRef, UploadError, make_provider
    from vice.uploaders import s3 as s3mod
    from vice.uploaders.s3 import S3Provider
    from vice.uploaders.zipline import ZiplineProvider, parse_size
except ModuleNotFoundError:
    web = None


AWS_TEST_KEY = "AKIDEXAMPLE"
AWS_TEST_SECRET = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
S3_DOC_KEY = "AKIAIOSFODNN7EXAMPLE"
S3_DOC_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"


class SigV4VectorTests(unittest.TestCase):
    def test_get_vanilla(self) -> None:
        # aws-sig-v4-test-suite/get-vanilla
        headers = sigv4.sign_headers(
            "GET", "example.amazonaws.com", "/",
            access_key=AWS_TEST_KEY, secret_key=AWS_TEST_SECRET,
            region="us-east-1", service="service",
            payload_hash=sigv4.EMPTY_SHA256, timestamp="20150830T123600Z",
            content_sha_header=False,
        )
        self.assertEqual(
            headers["Authorization"],
            "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/service/aws4_request, "
            "SignedHeaders=host;x-amz-date, "
            "Signature=5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31",
        )

    def test_get_vanilla_canonical_request(self) -> None:
        creq, signed = sigv4.canonical_request(
            "GET", "/", None,
            {"host": "example.amazonaws.com", "x-amz-date": "20150830T123600Z"},
            sigv4.EMPTY_SHA256,
        )
        self.assertEqual(signed, "host;x-amz-date")
        self.assertEqual(
            creq,
            "GET\n/\n\nhost:example.amazonaws.com\nx-amz-date:20150830T123600Z\n\n"
            "host;x-amz-date\n" + sigv4.EMPTY_SHA256,
        )

    def test_get_vanilla_query_order(self) -> None:
        # aws-sig-v4-test-suite/get-vanilla-query-order-key-case
        headers = sigv4.sign_headers(
            "GET", "example.amazonaws.com", "/",
            access_key=AWS_TEST_KEY, secret_key=AWS_TEST_SECRET,
            region="us-east-1", service="service",
            query=[("Param2", "value2"), ("Param1", "value1")],
            payload_hash=sigv4.EMPTY_SHA256, timestamp="20150830T123600Z",
            content_sha_header=False,
        )
        self.assertTrue(headers["Authorization"].endswith(
            "Signature=b97d918cfa904a5beff61c982a1b6f458b799221646efd99d3219ec94cdf2500"
        ))

    def test_s3_get_object_with_range(self) -> None:
        # Example "GET Object" from the S3 SigV4 header-signing documentation.
        headers = sigv4.sign_headers(
            "GET", "examplebucket.s3.amazonaws.com", "/test.txt",
            access_key=S3_DOC_KEY, secret_key=S3_DOC_SECRET, region="us-east-1",
            headers={"Range": "bytes=0-9"},
            payload_hash=sigv4.EMPTY_SHA256, timestamp="20130524T000000Z",
        )
        self.assertEqual(
            headers["Authorization"],
            "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, "
            "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, "
            "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41",
        )

    def test_s3_presigned_get(self) -> None:
        # Example from the S3 query-string authentication documentation.
        url = sigv4.presign_url(
            "GET", "https", "examplebucket.s3.amazonaws.com", "/test.txt",
            access_key=S3_DOC_KEY, secret_key=S3_DOC_SECRET, region="us-east-1",
            expires=86400, timestamp="20130524T000000Z",
        )
        self.assertEqual(
            url,
            "https://examplebucket.s3.amazonaws.com/test.txt"
            "?X-Amz-Algorithm=AWS4-HMAC-SHA256"
            "&X-Amz-Credential=AKIAIOSFODNN7EXAMPLE%2F20130524%2Fus-east-1%2Fs3%2Faws4_request"
            "&X-Amz-Date=20130524T000000Z&X-Amz-Expires=86400&X-Amz-SignedHeaders=host"
            "&X-Amz-Signature=aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404",
        )

    def test_presign_refuses_more_than_seven_days(self) -> None:
        kwargs = dict(access_key="a", secret_key="b", region="auto")
        sigv4.presign_query("GET", "h", "/k", expires=7 * 86400, **kwargs)
        with self.assertRaises(ValueError):
            sigv4.presign_query("GET", "h", "/k", expires=7 * 86400 + 1, **kwargs)

    def test_path_encoding_keeps_slashes_and_escapes_spaces(self) -> None:
        self.assertEqual(sigv4.encode_path("clips/My Clip (1).mp4"), "/clips/My%20Clip%20%281%29.mp4")


class _Fixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def make_file(self, name: str, size: int) -> Path:
        path = self.root / name
        path.write_bytes(os.urandom(size))
        return path

    async def serve(self, app) -> str:
        server = TestServer(app, host="127.0.0.1")
        await server.start_server()
        self.addAsyncCleanup(server.close)
        return str(server.make_url("")).rstrip("/")


def _read_multipart_file(body: bytes, content_type: str) -> tuple[str, str, bytes]:
    boundary = content_type.split("boundary=", 1)[1].encode()
    parts = [p for p in body.split(b"--" + boundary) if p.strip(b"\r\n-")]
    assert len(parts) == 1, "exactly one part"
    head, _, data = parts[0].partition(b"\r\n\r\n")
    data = data[:-2] if data.endswith(b"\r\n") else data
    head_text = head.decode()
    name = head_text.split('name="', 1)[1].split('"', 1)[0]
    filename = head_text.split('filename="', 1)[1].split('"', 1)[0]
    return name, filename, data


class FakeZipline:
    TOKEN = "zl-token"

    def __init__(self, *, chunk_size="1kb", chunk_max="2kb", max_file="1mb", fail_processing=False):
        self.public = {"chunks": {"enabled": True, "size": chunk_size, "max": chunk_max},
                       "files": {"maxFileSize": max_file}}
        self.fail_processing = fail_processing
        self.files: dict[str, dict] = {}
        self.partial: dict = {}
        self.chunk_requests: list[dict] = []
        self.single_requests: list[dict] = []
        self.incomplete_polls = 0
        self.deleted: list[str] = []
        self.base = ""
        app = web.Application()
        app.router.add_get("/api/user", self.user)
        app.router.add_get("/api/server/public", self.server_public)
        app.router.add_post("/api/upload", self.upload)
        app.router.add_post("/api/upload/partial", self.upload_partial)
        app.router.add_get("/api/user/files/incomplete", self.incomplete)
        app.router.add_delete("/api/user/files/{id}", self.delete)
        app.router.add_get("/u/{name}", self.raw)
        self.app = app

    def _auth(self, request) -> None:
        if request.headers.get("authorization") != self.TOKEN:
            raise web.HTTPUnauthorized(text=json.dumps({"error": "unauthorized"}),
                                       content_type="application/json")

    async def user(self, request):
        self._auth(request)
        return web.json_response({"user": {"id": "u1", "username": "river"}})

    async def server_public(self, request):
        return web.json_response(self.public)

    def _store(self, name: str, data: bytes) -> dict:
        file_id = f"f{len(self.files) + 1}"
        self.files[file_id] = {"name": name, "data": data}
        return {"id": file_id, "name": name, "type": "video/mp4", "url": f"{self.base}/u/{name}"}

    async def upload(self, request):
        self._auth(request)
        if "content-range" in request.headers:
            return web.json_response({"error": "1001"}, status=400)
        body = await request.read()
        field, filename, data = _read_multipart_file(body, request.headers["Content-Type"])
        self.single_requests.append({"headers": dict(request.headers), "field": field})
        return web.json_response({"files": [self._store(filename, data)], "deletesAt": "never"})

    async def upload_partial(self, request):
        self._auth(request)
        h = request.headers
        rng = h["Content-Range"].replace("bytes ", "").replace("-", "/").split("/")
        start, end, total = (int(x) for x in rng)
        body = await request.read()
        _, _, data = _read_multipart_file(body, h["Content-Type"])
        self.chunk_requests.append({"start": start, "end": end, "headers": dict(h)})
        if len(data) != end - start + 1:
            return web.json_response({"error": "1002"}, status=400)
        if (h["x-zipline-p-lastchunk"] == "true") != (end == total - 1):
            return web.json_response({"error": "1002"}, status=400)
        if int(h["x-zipline-p-content-length"]) != total:
            return web.json_response({"error": "bad length"}, status=400)
        if start == 0:
            if "x-zipline-p-token" in h:
                return web.json_response({"error": "unexpected token"}, status=400)
            self.partial = {"data": bytearray(), "token": None}
        elif h.get("x-zipline-p-token") != self.partial.get("token"):
            return web.json_response({"error": "bad token"}, status=400)
        if start != len(self.partial["data"]):
            return web.json_response({"error": "out of order"}, status=400)
        self.partial["data"] += data
        if end == total - 1:
            info = self._store(h["x-zipline-p-filename"], bytes(self.partial["data"]))
            info["pending"] = True
            self.partial["file_id"] = info["id"]
            return web.json_response({"files": [info], "partialSuccess": True})
        self.partial["token"] = f"tok{start}"
        return web.json_response({"files": [], "partialSuccess": True, "partialToken": self.partial["token"]})

    async def incomplete(self, request):
        self._auth(request)
        self.incomplete_polls += 1
        if self.incomplete_polls == 1:
            status = "PROCESSING"
        else:
            status = "FAILED" if self.fail_processing else "COMPLETE"
        return web.json_response([{
            "id": "inc1", "status": status, "chunksTotal": 3, "chunksComplete": 1,
            "metadata": {"file": {"id": self.partial.get("file_id"), "filename": "x", "type": "video/mp4"}},
        }])

    async def delete(self, request):
        self._auth(request)
        file_id = request.match_info["id"]
        if file_id not in self.files:
            return web.json_response({"error": "not found"}, status=404)
        self.deleted.append(file_id)
        self.files.pop(file_id)
        return web.json_response({"id": file_id})

    async def raw(self, request):
        for info in self.files.values():
            if info["name"] == request.match_info["name"]:
                return web.Response(body=info["data"], content_type="video/mp4")
        raise web.HTTPNotFound()


@unittest.skipIf(web is None, "aiohttp is not installed")
class ZiplineTests(_Fixture):
    async def provider(self, fake: FakeZipline, **server):
        fake.base = await self.serve(fake.app)
        cfg = {"kind": "zipline", "url": fake.base, **server}
        provider = make_provider(cfg, {"token": FakeZipline.TOKEN})
        self.addAsyncCleanup(provider.close)
        provider.poll_interval = 0.01
        return provider

    def test_parse_size(self) -> None:
        self.assertEqual(parse_size("25mb"), 25 * 1024 * 1024)
        self.assertEqual(parse_size("1GB"), 1024 ** 3)
        self.assertEqual(parse_size("512"), 512)
        self.assertEqual(parse_size(2048), 2048)
        self.assertIsNone(parse_size("lots"))

    async def test_test_returns_username(self) -> None:
        provider = await self.provider(FakeZipline())
        self.assertIsInstance(provider, ZiplineProvider)
        self.assertEqual(await provider.test(), "river")

    async def test_bad_token_is_an_upload_error(self) -> None:
        fake = FakeZipline()
        fake.base = await self.serve(fake.app)
        provider = make_provider({"kind": "zipline", "url": fake.base}, {"token": "wrong"})
        self.addAsyncCleanup(provider.close)
        with self.assertRaises(UploadError) as ctx:
            await provider.test()
        self.assertEqual(ctx.exception.status, 401)

    async def test_single_upload_then_download_and_delete(self) -> None:
        fake = FakeZipline()
        provider = await self.provider(fake, folder="fold1")
        path = self.make_file("clip.mp4", 1500)
        seen = []
        ref = await provider.upload(path, "clip.mp4", lambda s, t: seen.append((s, t)))

        self.assertEqual(ref, RemoteRef("f1", f"{fake.base}/u/clip.mp4", "clip.mp4"))
        self.assertEqual(fake.files["f1"]["data"], path.read_bytes())
        req = fake.single_requests[0]
        self.assertEqual(req["field"], "file")
        self.assertEqual(req["headers"]["x-zipline-original-name"], "true")
        self.assertEqual(req["headers"]["x-zipline-folder"], "fold1")
        self.assertNotIn("Transfer-Encoding", req["headers"])
        self.assertEqual(seen[-1], (1500, 1500))
        self.assertFalse(fake.chunk_requests)

        self.assertEqual(await provider.stream_url(ref), ref.url)
        dest = self.root / "back.mp4"
        await provider.download(ref, dest, None)
        self.assertEqual(dest.read_bytes(), path.read_bytes())

        await provider.delete(ref)
        self.assertEqual(fake.deleted, ["f1"])
        await provider.delete(ref)  # already gone is fine

    async def test_chunked_upload_follows_tokens_and_waits_for_processing(self) -> None:
        fake = FakeZipline(chunk_size="1kb", chunk_max="2kb")
        provider = await self.provider(fake)
        size = 2 * 1024 + 300
        path = self.make_file("big.mkv", size)
        seen = []
        ref = await provider.upload(path, "big.mkv", lambda s, t: seen.append((s, t)))

        self.assertEqual([(c["start"], c["end"]) for c in fake.chunk_requests],
                         [(0, 1023), (1024, 2047), (2048, size - 1)])
        first, second, last = (c["headers"] for c in fake.chunk_requests)
        self.assertNotIn("x-zipline-p-token", first)
        self.assertEqual(second["x-zipline-p-token"], "tok0")
        self.assertEqual(last["x-zipline-p-token"], "tok1024")
        self.assertEqual(last["x-zipline-p-lastchunk"], "true")
        self.assertEqual(first["x-zipline-p-lastchunk"], "false")
        self.assertEqual(first["x-zipline-p-content-type"], "video/x-matroska")
        self.assertEqual(first["Content-Range"], f"bytes 0-1023/{size}")
        self.assertEqual(fake.files[ref.remote_id]["data"], path.read_bytes())
        self.assertEqual(ref.url, f"{fake.base}/u/big.mkv")
        self.assertGreaterEqual(fake.incomplete_polls, 2)
        self.assertEqual(seen[-1], (size, size))
        self.assertEqual([s for s, _ in seen], sorted(s for s, _ in seen))

    async def test_chunked_upload_reports_failed_processing(self) -> None:
        fake = FakeZipline(fail_processing=True)
        provider = await self.provider(fake)
        path = self.make_file("big.mp4", 4096)
        with self.assertRaises(UploadError):
            await provider.upload(path, "big.mp4", None)

    async def test_file_over_server_limit_is_refused_before_sending(self) -> None:
        fake = FakeZipline(max_file="1kb")
        provider = await self.provider(fake)
        with self.assertRaises(UploadError):
            await provider.upload(self.make_file("c.mp4", 2048), "c.mp4", None)
        self.assertFalse(fake.single_requests or fake.chunk_requests)


class FakeS3:
    def __init__(self, bucket="clips", fail_part=None, access_key="AK", secret_key="SK"):
        self.bucket = bucket
        self.access_key = access_key
        self.secret_key = secret_key
        self.fail_part = fail_part
        self.objects: dict[str, bytes] = {}
        self.uploads: dict[str, dict] = {}
        self.aborted: list[str] = []
        self.requests: list[tuple[str, str, dict]] = []
        app = web.Application(client_max_size=64 * 1024 * 1024)
        app.router.add_route("*", "/{bucket}", self.bucket_route)
        app.router.add_route("*", "/{bucket}/{key:.+}", self.object_route)
        self.app = app

    def _error(self, status, code, message=""):
        body = f"<Error><Code>{code}</Code><Message>{message}</Message></Error>"
        return web.Response(status=status, body=body, content_type="application/xml")

    async def _check(self, request) -> tuple:
        body = await request.read()
        self.requests.append((request.method, request.path_qs, dict(request.headers)))
        if "X-Amz-Signature" in request.query:
            if not request.query.get("X-Amz-Credential", "").startswith(self.access_key + "/"):
                return body, self._error(403, "InvalidAccessKeyId")
            if request.match_info["bucket"] != self.bucket:
                return body, self._error(404, "NoSuchBucket")
            return body, None
        auth = request.headers.get("Authorization", "")
        if f"Credential={self.access_key}/" not in auth:
            return body, self._error(403, "InvalidAccessKeyId")
        if not self._signature_matches(request, auth):
            return body, self._error(403, "SignatureDoesNotMatch")
        claimed = request.headers.get("x-amz-content-sha256")
        if claimed != hashlib.sha256(body).hexdigest():
            return body, self._error(400, "XAmzContentSHA256Mismatch")
        if request.match_info["bucket"] != self.bucket:
            return body, self._error(404, "NoSuchBucket")
        return body, None

    def _signature_matches(self, request, auth: str) -> bool:
        # Recompute the signature from what actually arrived on the wire: the
        # raw path, the query and the headers the client said it signed.
        signed_names = auth.split("SignedHeaders=", 1)[1].split(",", 1)[0].split(";")
        scope = auth.split("Credential=", 1)[1].split(",", 1)[0].split("/")
        headers = {n: request.headers.get(n, "") for n in signed_names}
        creq, _ = sigv4.canonical_request(
            request.method, request.raw_path.split("?", 1)[0], list(request.query.items()),
            headers, request.headers["x-amz-content-sha256"],
        )
        to_sign = "\n".join([sigv4.ALGORITHM, request.headers["x-amz-date"], "/".join(scope[1:]),
                             hashlib.sha256(creq.encode()).hexdigest()])
        key = sigv4.signing_key(self.secret_key, scope[1], scope[2], scope[3])
        expected = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
        return auth.endswith("Signature=" + expected)

    async def bucket_route(self, request):
        _, err = await self._check(request)
        if err:
            return err
        if request.method == "GET" and request.query.get("list-type") == "2":
            return web.Response(body="<ListBucketResult><KeyCount>0</KeyCount></ListBucketResult>",
                                content_type="application/xml")
        return self._error(405, "MethodNotAllowed")

    async def object_route(self, request):
        body, err = await self._check(request)
        if err:
            return err
        key = request.match_info["key"]
        q = request.query
        if request.method == "POST" and "uploads" in q:
            upload_id = f"up{len(self.uploads) + 1}"
            self.uploads[upload_id] = {"key": key, "parts": {}, "type": request.headers.get("content-type")}
            return web.Response(body=f"<InitiateMultipartUploadResult><UploadId>{upload_id}</UploadId>"
                                     "</InitiateMultipartUploadResult>", content_type="application/xml")
        if request.method == "PUT" and "uploadId" in q:
            number = int(q["partNumber"])
            if number == self.fail_part:
                return self._error(500, "InternalError", "part broke")
            self.uploads[q["uploadId"]]["parts"][number] = body
            return web.Response(headers={"ETag": f'"etag{number}"'})
        if request.method == "POST" and "uploadId" in q:
            up = self.uploads.pop(q["uploadId"])
            listed = body.decode().count("<PartNumber>")
            if listed != len(up["parts"]):
                return self._error(400, "InvalidPart")
            self.objects[key] = b"".join(up["parts"][n] for n in sorted(up["parts"]))
            return web.Response(body="<CompleteMultipartUploadResult/>", content_type="application/xml")
        if request.method == "DELETE" and "uploadId" in q:
            self.aborted.append(q["uploadId"])
            self.uploads.pop(q["uploadId"], None)
            return web.Response(status=204)
        if request.method == "PUT":
            if "Content-Length" not in request.headers:
                return self._error(411, "MissingContentLength")
            self.objects[key] = body
            return web.Response(headers={"ETag": '"x"'})
        if request.method == "DELETE":
            self.objects.pop(key, None)
            return web.Response(status=204)
        if request.method == "GET":
            if key not in self.objects:
                return self._error(404, "NoSuchKey")
            return web.Response(body=self.objects[key])
        return self._error(405, "MethodNotAllowed")


@unittest.skipIf(web is None, "aiohttp is not installed")
class S3Tests(_Fixture):
    async def provider(self, fake: FakeS3, **server):
        base = await self.serve(fake.app)
        cfg = {"kind": "s3", "endpoint": base, "bucket": fake.bucket, "region": "us-east-1",
               "path_style": True, "prefix": "vice/", **server}
        provider = make_provider(cfg, {"access_key": "AK", "secret_key": "SK"})
        self.addAsyncCleanup(provider.close)
        return provider

    async def test_test_lists_the_bucket(self) -> None:
        provider = await self.provider(FakeS3())
        self.assertIsInstance(provider, S3Provider)
        self.assertEqual(await provider.test(), "bucket clips")

    async def test_test_reports_s3_error_code(self) -> None:
        provider = await self.provider(FakeS3(bucket="other"), bucket="clips")
        with self.assertRaises(UploadError) as ctx:
            await provider.test()
        self.assertIn("NoSuchBucket", str(ctx.exception))
        self.assertEqual(ctx.exception.status, 404)

    async def test_single_put_with_content_type_and_progress(self) -> None:
        fake = FakeS3()
        provider = await self.provider(fake)
        path = self.make_file("My Clip.mp4", 3000)
        seen = []
        ref = await provider.upload(path, "My Clip.mp4", lambda s, t: seen.append((s, t)))

        self.assertEqual(ref.key, "vice/My Clip.mp4")
        self.assertEqual(ref.remote_id, "vice/My Clip.mp4")
        self.assertEqual(fake.objects["vice/My Clip.mp4"], path.read_bytes())
        method, path_qs, headers = fake.requests[-1]
        self.assertEqual((method, path_qs), ("PUT", "/clips/vice/My%20Clip.mp4"))
        self.assertEqual(headers["Content-Type"], "video/mp4")
        self.assertNotIn("Transfer-Encoding", headers)
        self.assertEqual(seen[-1], (3000, 3000))

        dest = self.root / "back.mp4"
        await provider.download(ref, dest, None)
        self.assertEqual(dest.read_bytes(), path.read_bytes())

        await provider.delete(ref)
        self.assertNotIn("vice/My Clip.mp4", fake.objects)

    async def test_multipart_upload_reports_one_running_total(self) -> None:
        fake = FakeS3()
        provider = await self.provider(fake)
        size = 2500
        path = self.make_file("long.mkv", size)
        seen = []
        with mock.patch.object(s3mod, "SINGLE_PUT_MAX", 1000), mock.patch.object(s3mod, "PART_SIZE", 1000):
            ref = await provider.upload(path, "long.mkv", lambda s, t: seen.append((s, t)))

        self.assertEqual(fake.objects[ref.key], path.read_bytes())
        part_puts = [r for r in fake.requests if r[0] == "PUT" and "partNumber" in r[1]]
        self.assertEqual(len(part_puts), 3)
        self.assertEqual(seen[-1], (size, size))
        self.assertEqual([s for s, _ in seen], sorted(s for s, _ in seen))
        self.assertTrue(all(t == size for _, t in seen))
        self.assertFalse(fake.aborted)
        init = next(r for r in fake.requests if "uploads" in r[1])
        self.assertEqual(init[2]["Content-Type"], "video/x-matroska")

    async def test_multipart_failure_aborts_the_upload(self) -> None:
        fake = FakeS3(fail_part=2)
        provider = await self.provider(fake)
        path = self.make_file("long.mp4", 2500)
        with mock.patch.object(s3mod, "SINGLE_PUT_MAX", 1000), mock.patch.object(s3mod, "PART_SIZE", 1000):
            with self.assertRaises(UploadError) as ctx:
                await provider.upload(path, "long.mp4", None)
        self.assertIn("InternalError", str(ctx.exception))
        self.assertEqual(fake.aborted, ["up1"])
        self.assertEqual(fake.objects, {})

    async def test_presigned_stream_url_is_fetchable(self) -> None:
        fake = FakeS3()
        provider = await self.provider(fake, presign_days=2)
        ref = await provider.upload(self.make_file("a.mp4", 10), "a.mp4", None)
        url = await provider.stream_url(ref)
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(query["X-Amz-Expires"], ["172800"])
        self.assertEqual(query["X-Amz-SignedHeaders"], ["host"])
        self.assertTrue(urlsplit(url).path.endswith("/clips/vice/a.mp4"))
        self.assertEqual(ref.url.split("?")[0], url.split("?")[0])

        async with ClientSession() as session:
            async with session.get(URL(url, encoded=True)) as resp:
                self.assertEqual(resp.status, 200)
                self.assertEqual(len(await resp.read()), 10)

    async def test_public_base_url_wins_over_presigning(self) -> None:
        fake = FakeS3()
        provider = await self.provider(fake, public_base_url="https://cdn.example.com/")
        ref = await provider.upload(self.make_file("b c.mp4", 10), "b c.mp4", None)
        self.assertEqual(await provider.stream_url(ref), "https://cdn.example.com/vice/b%20c.mp4")
        self.assertEqual(ref.url, "https://cdn.example.com/vice/b%20c.mp4")

    def test_presign_days_is_clamped(self) -> None:
        p = S3Provider({"bucket": "b", "endpoint": "https://x", "presign_days": 30}, {})
        self.assertEqual(p.presign_days, 7)
        p = S3Provider({"bucket": "b", "endpoint": "https://x", "presign_days": 0}, {})
        self.assertEqual(p.presign_days, 1)

    def test_region_is_read_from_known_endpoints(self) -> None:
        cases = {
            "https://s3.eu-west-2.amazonaws.com": "eu-west-2",
            "https://s3.us-west-004.backblazeb2.com": "us-west-004",
            "https://abc123.r2.cloudflarestorage.com": "auto",
            "http://127.0.0.1:9000": "us-east-1",
        }
        for endpoint, region in cases.items():
            self.assertEqual(S3Provider({"endpoint": endpoint, "bucket": "b"}, {}).region, region, endpoint)

    def test_virtual_host_addressing(self) -> None:
        p = S3Provider({"endpoint": "https://abc.r2.cloudflarestorage.com", "bucket": "clips"}, {})
        self.assertEqual(p._host_and_path("vice/a b.mp4"), ("clips.abc.r2.cloudflarestorage.com", "/vice/a%20b.mp4"))
        p = S3Provider({"endpoint": "http://127.0.0.1:9000", "bucket": "clips", "path_style": True}, {})
        self.assertEqual(p._host_and_path("k.mp4"), ("127.0.0.1:9000", "/clips/k.mp4"))


@unittest.skipIf(web is None, "aiohttp is not installed")
class MakeProviderTests(unittest.TestCase):
    def test_unknown_kind_is_refused(self) -> None:
        with self.assertRaises(UploadError):
            make_provider({"kind": "ftp"}, {})
        with self.assertRaises(UploadError):
            make_provider({}, {})

    def test_remote_ref_round_trips(self) -> None:
        ref = RemoteRef("id", "https://x/u/a", "a")
        self.assertEqual(RemoteRef.from_dict(ref.to_dict()), ref)


if __name__ == "__main__":
    unittest.main()
