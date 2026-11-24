"""Uploading clips to a media server, as the share server sees it.

The providers (Zipline, S3) have their own tests in test_uploaders.py; here a
fake stands in for them, so what is checked is the library: records, cloud
clips, links, deletes and renames.
"""

import asyncio
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    import _sandbox  # noqa: F401  keeps every store out of the real home
except ModuleNotFoundError:
    from tests import _sandbox  # noqa: F401

from vice import uploads as upstore
from vice.config import Config, normalize_upload

try:
    from aiohttp import web
    from vice.share import ShareServer
    from vice.uploaders import RemoteRef
except ModuleNotFoundError:
    ShareServer = None


async def _stub_ffprobe(_: Path) -> dict:
    return {"width": 1920, "height": 1080, "duration": 4.2, "vcodec": "h264"}


class FakeProvider:
    def __init__(self) -> None:
        self.uploaded: list[str] = []
        self.deleted: list[str] = []
        self.fail_upload = False

    async def test(self) -> str:
        return "river"

    async def upload(self, path, name, progress_cb=None):
        if self.fail_upload:
            raise RuntimeError("server said no")
        size = path.stat().st_size
        if progress_cb:
            progress_cb(size, size)
        self.uploaded.append(name)
        return RemoteRef(remote_id="abc123", url=f"https://zip.example/u/{name}", key=name)

    async def delete(self, ref) -> None:
        self.deleted.append(ref.remote_id)

    async def stream_url(self, ref) -> str:
        return ref.url

    async def download(self, ref, dest, progress_cb=None) -> None:
        Path(dest).write_bytes(b"from-the-server")


def _req(slug: str = "", query: dict | None = None, body: dict | None = None):
    req = mock.MagicMock()
    req.match_info = {"slug": slug, "ref": slug}
    req.query = query or {}
    req.json = mock.AsyncMock(return_value=body or {})
    return req


class _MediaCase(unittest.IsolatedAsyncioTestCase):
    """A share server with one clip and a Zipline server whose provider is
    the fake above. Holds no tests of its own."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.clips = self.root / "clips"
        self.clips.mkdir()
        self.thumbs = self.root / "thumbs"
        self.thumbs.mkdir()

        async def _stub_thumb(path: Path, duration: float = 0.0) -> Path:
            from vice.share import _thumb_path
            target = _thumb_path(path)
            target.write_bytes(b"jpeg")
            return target

        for target, value in (
            ("vice.share.THUMB_DIR", self.thumbs),
            ("vice.share.HIGHLIGHTS_DIR", self.root / "highlights"),
            ("vice.share.VIEWS_PATH", self.root / "views.json"),
            ("vice.share.CLIP_GAMES_PATH", self.root / "clip_games.json"),
            ("vice.share.SHARE_TOKENS_PATH", self.root / "share_tokens.json"),
            ("vice.playlists.PLAYLISTS_PATH", self.root / "playlists.json"),
            ("vice.uploads.UPLOADS_PATH", self.root / "uploads.json"),
            ("vice.uploads.CLOUD_THUMB_DIR", self.root / "cloud_thumbs"),
            ("vice.uploads.UPLOAD_SECRETS_PATH", self.root / "upload_secrets.json"),
            ("vice.share._ffprobe", _stub_ffprobe),
            ("vice.share._make_thumb", _stub_thumb),
        ):
            patcher = mock.patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.fake = FakeProvider()

        async def _provider(_manager, _server_cfg):
            return self.fake

        patcher = mock.patch.object(upstore.UploadManager, "provider", _provider)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _server(self, *, delete_after: bool = False) -> "ShareServer":
        cfg = Config()
        cfg.output.directory = str(self.clips)
        cfg.upload.servers = [{"id": "zip", "kind": "zipline", "name": "Home Zipline",
                               "url": "https://zip.example"}]
        cfg.upload.delete_after_upload = delete_after
        normalize_upload(cfg)
        upstore.save_secrets({"zip": {"token": "secret-token"}})
        path = self.clips / "Vice_Clip_1.mp4"
        path.write_bytes(b"clip-bytes")
        server = ShareServer(cfg)
        server._clips["Vice_Clip_1"] = path
        server.broadcast = mock.AsyncMock()
        return server

    async def _upload(self, server, slug: str = "Vice_Clip_1") -> None:
        resp = await server._api_uploads_enqueue(_req(body={"slugs": [slug]}))
        self.assertEqual(json.loads(resp.text)["queued"], [slug])
        job = server.uploader.jobs.get(slug)
        if job and job.task:
            await asyncio.gather(job.task, return_exceptions=True)

    def _events(self, server, kind: str) -> list[dict]:
        return [c.args[0] for c in server.broadcast.await_args_list if c.args[0].get("type") == kind]



@unittest.skipUnless(ShareServer is not None, "aiohttp is not installed")
class MediaUploadTests(_MediaCase):
    async def test_an_upload_is_recorded_and_its_link_is_the_servers(self) -> None:
        server = self._server()
        await self._upload(server)
        self.assertEqual(self.fake.uploaded, ["Vice_Clip_1.mp4"])
        record = upstore.load_records()["Vice_Clip_1"]
        self.assertEqual(record["server_id"], "zip")
        self.assertTrue(record["local"])
        self.assertEqual(record["meta"]["duration"], 4.2)
        self.assertEqual(server.share_url("Vice_Clip_1"), "https://zip.example/u/Vice_Clip_1.mp4")
        self.assertTrue(self._events(server, "upload_done"))
        clip = server._any_clip_json("Vice_Clip_1")
        self.assertEqual(clip["cloud"]["server_name"], "Home Zipline")
        self.assertTrue(clip["cloud"]["local"])
        self.assertTrue((self.clips / "Vice_Clip_1.mp4").exists())

    async def test_a_failed_upload_says_why_and_records_nothing(self) -> None:
        server = self._server()
        self.fake.fail_upload = True
        await self._upload(server)
        errors = self._events(server, "upload_error")
        self.assertEqual(errors[-1]["message"], "server said no")
        self.assertFalse(errors[-1]["canceled"])
        self.assertEqual(upstore.load_records(), {})

    async def test_a_clip_already_on_the_server_is_not_sent_twice(self) -> None:
        server = self._server()
        await self._upload(server)
        resp = await server._api_uploads_enqueue(_req(body={"slugs": ["Vice_Clip_1"]}))
        self.assertEqual(json.loads(resp.text)["queued"], [])
        self.assertEqual(len(self.fake.uploaded), 1)

    async def test_no_upload_without_a_saved_token(self) -> None:
        server = self._server()
        upstore.save_secrets({})
        resp = await server._api_uploads_enqueue(_req(body={"slugs": ["Vice_Clip_1"]}))
        self.assertEqual(resp.status, 409)

    async def test_delete_after_upload_leaves_a_cloud_clip(self) -> None:
        server = self._server(delete_after=True)
        await server._broadcast_clip("Vice_Clip_1", server._clips["Vice_Clip_1"])  # makes the thumb
        await self._upload(server)
        self.assertFalse((self.clips / "Vice_Clip_1.mp4").exists())
        self.assertTrue(server._is_cloud_only("Vice_Clip_1"))
        self.assertTrue(upstore.cloud_thumb_path("Vice_Clip_1").exists())

        listing = json.loads((await server._api_clips(_req())).text)["clips"]
        self.assertEqual([c["slug"] for c in listing], ["Vice_Clip_1"])
        clip = listing[0]
        self.assertFalse(clip["cloud"]["local"])
        self.assertEqual(clip["duration"], 4.2)
        self.assertIsNotNone(clip["thumb_url"])
        self.assertEqual(clip["share_url"], "https://zip.example/u/Vice_Clip_1.mp4")

        thumb = await server._serve_thumb(_req("Vice_Clip_1"), public=False)
        self.assertEqual(Path(thumb._path), upstore.cloud_thumb_path("Vice_Clip_1"))

    async def test_a_cloud_clip_plays_and_old_links_land_on_the_server(self) -> None:
        server = self._server(delete_after=True)
        token = server._share_tokens.get("Vice_Clip_1") or server._issue_share_token("Vice_Clip_1")
        await self._upload(server)
        with self.assertRaises(web.HTTPFound) as local:
            await server._serve_video(_req("Vice_Clip_1"), public=False)
        self.assertEqual(local.exception.location, "https://zip.example/u/Vice_Clip_1.mp4")
        req = _req(token)
        with self.assertRaises(web.HTTPFound):
            await server._serve_embed_page(req, public=True)
        # The public server still never takes a filename.
        with self.assertRaises(web.HTTPNotFound):
            await server._serve_video(_req("Vice_Clip_1"), public=True)

    async def test_keep_cloud_delete_and_download_back(self) -> None:
        server = self._server()
        await self._upload(server)
        await server._api_delete(_req("Vice_Clip_1", {"keep_cloud": "1"}))
        self.assertTrue(server._is_cloud_only("Vice_Clip_1"))
        self.assertEqual(self.fake.deleted, [])

        resp = await server._api_cloud_download(_req("Vice_Clip_1"))
        self.assertEqual(json.loads(resp.text), {"ok": True})
        self.assertEqual((self.clips / "Vice_Clip_1.mp4").read_bytes(), b"from-the-server")
        self.assertFalse(server._is_cloud_only("Vice_Clip_1"))
        self.assertTrue(upstore.load_records()["Vice_Clip_1"]["local"])

    async def test_delete_with_remote_removes_the_server_copy_too(self) -> None:
        server = self._server()
        await self._upload(server)
        await server._api_delete(_req("Vice_Clip_1", {"remote": "1"}))
        self.assertEqual(self.fake.deleted, ["abc123"])
        self.assertEqual(upstore.load_records(), {})
        self.assertNotIn("Vice_Clip_1", server._clips)

    async def test_a_plain_delete_leaves_the_server_alone(self) -> None:
        server = self._server()
        await self._upload(server)
        await server._api_delete(_req("Vice_Clip_1"))
        self.assertEqual(self.fake.deleted, [])
        self.assertEqual(upstore.load_records(), {})

    async def test_removing_from_the_server_keeps_a_local_clip(self) -> None:
        server = self._server()
        await self._upload(server)
        await server._api_cloud_remove(_req("Vice_Clip_1"))
        self.assertEqual(self.fake.deleted, ["abc123"])
        self.assertIn("Vice_Clip_1", server._clips)
        self.assertIsNone(server._any_clip_json("Vice_Clip_1")["cloud"])
        self.assertIn("/c/", server.share_url("Vice_Clip_1"))

    async def test_a_rename_carries_the_record(self) -> None:
        server = self._server()
        await self._upload(server)
        resp = await server._api_rename(_req("Vice_Clip_1", body={"name": "Ace"}))
        self.assertEqual(json.loads(resp.text)["slug"], "Ace")
        records = upstore.load_records()
        self.assertNotIn("Vice_Clip_1", records)
        self.assertEqual(records["Ace"]["name"], "Ace.mp4")
        self.assertEqual(server.share_url("Ace"), "https://zip.example/u/Vice_Clip_1.mp4")

    async def test_a_rename_cannot_take_a_cloud_clips_name(self) -> None:
        server = self._server(delete_after=True)
        await self._upload(server)
        other = self.clips / "Other.mp4"
        other.write_bytes(b"x")
        server._clips["Other"] = other
        resp = await server._api_rename(_req("Other", body={"name": "Vice_Clip_1"}))
        self.assertFalse(json.loads(resp.text)["ok"])

    async def test_a_new_recording_skips_a_cloud_clips_number(self) -> None:
        from vice.recorder import _next_clip_path
        server = self._server(delete_after=True)
        await self._upload(server)
        self.assertEqual(_next_clip_path(self.clips).name, "Vice_Clip_2.mp4")
        self.assertEqual(
            _next_clip_path(self.clips, template="Vice_Clip_1").name, "Vice_Clip_1_2.mp4")

    async def test_secrets_stay_out_of_the_config_and_the_page(self) -> None:
        server = self._server()
        mode = stat.S_IMODE(os.stat(upstore.UPLOAD_SECRETS_PATH).st_mode)
        self.assertEqual(mode, 0o600)
        payload = server._servers_payload()
        self.assertTrue(payload["servers"][0]["has_secret"])
        self.assertNotIn("secret-token", json.dumps(payload))
        self.assertNotIn("token", payload["servers"][0])

    async def test_a_server_save_keeps_the_saved_token(self) -> None:
        server = self._server()
        with mock.patch("vice.config.CONFIG_DIR", self.root), \
                mock.patch("vice.config.CONFIG_PATH", self.root / "config.toml"):
            resp = await server._api_upload_server_save(_req(body={
                "server": {"id": "zip", "kind": "zipline", "name": "Renamed",
                           "url": "https://zip.example/"},
            }))
            body = json.loads(resp.text)
        self.assertTrue(body["ok"])
        self.assertEqual(body["servers"][0]["name"], "Renamed")
        self.assertEqual(body["servers"][0]["url"], "https://zip.example")
        self.assertEqual(upstore.load_secrets()["zip"], {"token": "secret-token"})


@unittest.skipUnless(ShareServer is not None, "aiohttp is not installed")
class ShareFallbackTests(_MediaCase):
    """Copying the link of an uploaded clip checks the server first: a server
    that is down must not get a dead link handed out when this PC could
    serve the clip itself."""

    async def _resolve(self, server, slug: str = "Vice_Clip_1") -> dict:
        return json.loads((await server._api_resolve_share(_req(slug))).text)

    async def test_a_reachable_server_gives_its_link(self) -> None:
        server = self._server()
        await self._upload(server)
        server._server_health.clear()
        with mock.patch.object(server, "_server_reachable", mock.AsyncMock(return_value=True)):
            body = await self._resolve(server)
        self.assertEqual(body["via"], "server")
        self.assertEqual(body["share_url"], "https://zip.example/u/Vice_Clip_1.mp4")
        self.assertEqual(body["server_name"], "Home Zipline")

    async def test_a_down_server_falls_back_to_this_pcs_link(self) -> None:
        server = self._server()
        await self._upload(server)
        with mock.patch.object(server, "_server_reachable", mock.AsyncMock(return_value=False)):
            body = await self._resolve(server)
        self.assertEqual(body["via"], "fallback")
        self.assertIn("/c/", body["share_url"])
        self.assertNotIn("zip.example", body["share_url"])
        # The fallback link resolves to the clip on this PC.
        token = body["share_url"].rsplit("/c/", 1)[1]
        self.assertEqual(server._slug_for_ref(token, allow_slug=False), "Vice_Clip_1")

    async def test_a_cloud_only_clip_has_nothing_to_fall_back_to(self) -> None:
        server = self._server(delete_after=True)
        await self._upload(server)
        with mock.patch.object(server, "_server_reachable", mock.AsyncMock(return_value=False)):
            body = await self._resolve(server)
        self.assertEqual(body["via"], "server_down")
        self.assertEqual(body["share_url"], "https://zip.example/u/Vice_Clip_1.mp4")

    async def test_a_clip_never_uploaded_is_not_checked(self) -> None:
        server = self._server()
        with mock.patch.object(server, "_server_reachable", mock.AsyncMock()) as check:
            body = await self._resolve(server)
        check.assert_not_awaited()
        self.assertEqual(body["via"], "local")

    async def test_the_check_is_remembered_briefly(self) -> None:
        server = self._server()
        await self._upload(server)
        # A finished upload already counts as the server answering.
        self.assertTrue(server._server_health["zip"][1])
        server._server_health.clear()

        class Boom:
            async def __aenter__(self):
                raise OSError("connection refused")

            async def __aexit__(self, *exc):
                return False

        session = mock.MagicMock()
        session.get = mock.MagicMock(return_value=Boom())
        with mock.patch.object(server.uploader, "session", mock.AsyncMock(return_value=session)):
            self.assertFalse(await server._server_reachable("zip", "https://zip.example/u/x"))
            self.assertFalse(await server._server_reachable("zip", "https://zip.example/u/x"))
        self.assertEqual(session.get.call_count, 1)


class UploadConfigTests(unittest.TestCase):
    def test_servers_are_cleaned_up(self) -> None:
        cfg = Config()
        cfg.upload.servers = [
            {"kind": "S3", "name": "", "bucket": "b", "presign_days": 99, "token": "leak"},
            {"kind": "ftp"},
            "nonsense",
        ]
        cfg.upload.concurrency = 12
        normalize_upload(cfg)
        self.assertEqual(len(cfg.upload.servers), 1)
        server = cfg.upload.servers[0]
        self.assertEqual(server["kind"], "s3")
        self.assertEqual(server["presign_days"], 7)
        self.assertNotIn("token", server)
        self.assertEqual(cfg.upload.active_server, server["id"])
        self.assertEqual(cfg.upload.concurrency, 4)


if __name__ == "__main__":
    unittest.main()
