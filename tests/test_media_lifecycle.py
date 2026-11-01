import asyncio
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    import _sandbox  # noqa: F401  keeps every store out of the real home
except ModuleNotFoundError:
    from tests import _sandbox  # noqa: F401

from vice import share
from vice.editor import ExportBusy, ExportManager


class FiniteMediaLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def _interrupt(self, operation, *, cancel, filename="clip.mp4"):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / filename
            source.write_bytes(b"original recording")
            spawned = asyncio.Event()
            child = None
            task = None
            output = None
            real_spawn = asyncio.create_subprocess_exec
            real_wait = asyncio.wait_for

            async def spawn(*cmd, **kwargs):
                nonlocal child, output
                output = Path(cmd[-1])
                output.write_bytes(b"unfinished output")
                child = await real_spawn(sys.executable, "-c", "import time; time.sleep(60)", **kwargs)
                spawned.set()
                return child

            async def short_wait(awaitable, timeout):
                return await real_wait(awaitable, 0.05)

            try:
                with mock.patch.object(share, "THUMB_DIR", root / "thumbs"), \
                     mock.patch.object(asyncio, "create_subprocess_exec", spawn), \
                     mock.patch.object(asyncio, "wait_for", short_wait):
                    task = asyncio.create_task(operation(source))
                    await real_wait(spawned.wait(), 2)
                    if cancel:
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                    else:
                        await real_wait(task, 2)
                    self.assertIsNotNone(child.returncode, "ffmpeg outlived the operation")
                    self.assertFalse(output.exists(), "unfinished output survived cleanup")
                    self.assertFalse(share._thumb_path(source).exists())
                    self.assertEqual(source.read_bytes(), b"original recording")
            finally:
                if task and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                if child and child.returncode is None:
                    child.kill()
                    await child.communicate()

    async def test_thumbnail_timeout_reaps_child_and_discards_partial_image(self):
        await self._interrupt(share._make_thumb, cancel=False)

    async def test_thumbnail_cancel_reaps_child_and_discards_partial_image(self):
        await self._interrupt(share._make_thumb, cancel=True)

    async def test_remux_timeout_reaps_child_and_keeps_original(self):
        await self._interrupt(share._remux_moov, cancel=False)

    async def test_remux_cancel_reaps_child_and_keeps_original(self):
        await self._interrupt(share._remux_moov, cancel=True)

    # .mkv, so the "already small enough" shortcut never applies and the
    # clip actually goes through the encoder.
    async def test_discord_copy_timeout_reaps_child_and_keeps_original(self):
        await self._interrupt(lambda src: share._make_discord_copy(src, 10),
                              cancel=False, filename="clip.mkv")

    async def test_discord_copy_cancel_reaps_child_and_keeps_original(self):
        await self._interrupt(lambda src: share._make_discord_copy(src, 10),
                              cancel=True, filename="clip.mkv")

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is not installed")
    async def test_real_thumbnail_replaces_empty_cache_and_is_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "clip.mp4"
            subprocess.run([
                "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                "testsrc=size=128x72:rate=10:duration=0.2", "-threads", "1", str(source),
            ], check=True, timeout=10)
            with mock.patch.object(share, "THUMB_DIR", root / "thumbs"):
                share.THUMB_DIR.mkdir()
                thumb = share._thumb_path(source)
                thumb.touch()
                result = await share._make_thumb(source, duration=0.2)
                self.assertEqual(result, thumb)
                self.assertTrue(thumb.read_bytes().startswith(b"\xff\xd8"))
                self.assertEqual(list(share.THUMB_DIR.iterdir()), [thumb])
                with mock.patch.object(asyncio, "create_subprocess_exec") as spawn:
                    self.assertEqual(await share._make_thumb(source), thumb)
                    spawn.assert_not_called()

    async def test_failed_concurrent_thumbnail_does_not_remove_successful_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "clip.mp4"
            source.write_bytes(b"source")
            pending = asyncio.Event()
            release = asyncio.Event()
            outputs = []

            async def spawn(*cmd, **kwargs):
                output = Path(cmd[-1])
                outputs.append(output)
                first = len(outputs) == 1
                proc = mock.Mock(returncode=None)

                async def communicate():
                    if first:
                        pending.set()
                        await release.wait()
                        output.write_bytes(b"partial")
                        proc.returncode = 1
                    else:
                        output.write_bytes(b"complete")
                        proc.returncode = 0
                    return None, None

                proc.communicate = communicate
                return proc

            with mock.patch.object(share, "THUMB_DIR", root / "thumbs"), \
                 mock.patch.object(asyncio, "create_subprocess_exec", spawn):
                first = asyncio.create_task(share._make_thumb(source))
                try:
                    await asyncio.wait_for(pending.wait(), 1)
                    thumb = await share._make_thumb(source)
                finally:
                    release.set()
                    await first
                self.assertNotEqual(outputs[0], outputs[1])
                self.assertEqual(thumb.read_bytes(), b"complete")
                self.assertEqual(list(share.THUMB_DIR.iterdir()), [thumb])


class ExportLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        self.tmp = self.root / ".clip.export.mp4"
        self.final = self.root / "clip.mp4"
        self.cleanup = mock.Mock()
        self.broadcast = mock.AsyncMock()
        self.manager = ExportManager(self.broadcast)

    async def asyncTearDown(self):
        task = self.manager._task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        proc = self.manager._proc
        if proc is not None and proc.returncode is None:
            proc.kill()
            # The old implementation has an unowned stderr drainer. Drain only
            # stdout here so a failing reproduction still releases its child.
            await proc.stdout.read()
            await proc.wait()

    def start(self, cmd):
        self.manager.start("job", cmd, 10, self.tmp, self.final, cleanup=self.cleanup)

    async def wait_for_child(self):
        async def ready():
            while self.manager._proc is None:
                await asyncio.sleep(0.005)
        await asyncio.wait_for(ready(), 2)
        return self.manager._proc

    async def test_cancel_before_first_task_step_does_not_spawn_an_encoder(self):
        with mock.patch("vice.editor.asyncio.create_subprocess_exec") as spawn:
            self.start(["unused"])
            self.assertTrue(await self.manager.cancel("job"))
            self.assertFalse(self.manager.busy)
            spawn.assert_not_called()
        self.cleanup.assert_called_once_with()
        self.assertTrue(self.broadcast.call_args.args[0]["canceled"])

    async def test_canceling_export_task_reaps_child_and_removes_partial_file(self):
        self.tmp.write_bytes(b"unfinished")
        self.start([sys.executable, "-c", "import sys,time; sys.stderr.write('x'*200000); sys.stderr.flush(); time.sleep(60)"])
        proc = await self.wait_for_child()
        self.manager._task.cancel()
        await asyncio.gather(self.manager._task, return_exceptions=True)
        self.assertIsNotNone(proc.returncode)
        self.assertFalse(self.tmp.exists())
        self.assertFalse(self.final.exists())
        self.cleanup.assert_called_once_with()

    async def test_stop_finishes_export_cleanup_and_rejects_new_jobs(self):
        self.start([sys.executable, "-c", "import time; time.sleep(60)"])
        proc = await self.wait_for_child()
        await asyncio.wait_for(self.manager.stop(), 2)
        self.assertFalse(self.manager.busy)
        self.assertIsNotNone(proc.returncode)
        with self.assertRaises(ExportBusy):
            self.start(["unused"])
        self.cleanup.assert_called_once_with()

    async def test_repeated_cancel_during_spawn_keeps_ownership_until_child_is_reaped(self):
        started = asyncio.Event()
        release = asyncio.Event()
        real_spawn = asyncio.create_subprocess_exec
        child = None

        async def slow_spawn(*cmd, **kwargs):
            nonlocal child
            child = await real_spawn(*cmd, **kwargs)
            started.set()
            await release.wait()
            return child

        with mock.patch("vice.editor.asyncio.create_subprocess_exec", slow_spawn):
            self.start([sys.executable, "-c", "import time; time.sleep(60)"])
            await asyncio.wait_for(started.wait(), 2)
            requests = []
            try:
                requests.append(asyncio.create_task(self.manager.cancel("job")))
                await asyncio.sleep(0)
                requests.append(asyncio.create_task(self.manager.cancel("job")))
                await asyncio.sleep(0)
                self.assertTrue(self.manager.busy)
                self.assertIsNone(child.returncode)
            finally:
                release.set()
                await asyncio.wait_for(asyncio.gather(*requests), 2)
        self.assertIsNotNone(child.returncode)
        self.assertFalse(self.manager.busy)
        self.cleanup.assert_called_once_with()

    async def test_stop_waits_for_registration_after_file_is_committed(self):
        registering = asyncio.Event()
        release = asyncio.Event()

        async def register(path):
            self.assertEqual(path, self.final)
            registering.set()
            await release.wait()
            return {"slug": "clip"}

        self.manager.start("job", [sys.executable, "-c",
                           "import pathlib,sys; pathlib.Path(sys.argv[1]).write_bytes(b'complete')",
                           str(self.tmp)], 10, self.tmp, self.final,
                           on_done=register, cleanup=self.cleanup)
        await asyncio.wait_for(registering.wait(), 2)
        stop = asyncio.create_task(self.manager.stop())
        try:
            self.assertFalse(await self.manager.cancel("job"))
            await asyncio.sleep(0)
            self.assertFalse(stop.done())
        finally:
            release.set()
            await asyncio.wait_for(stop, 2)
        self.assertEqual(self.final.read_bytes(), b"complete")
        self.assertFalse(self.tmp.exists())
        self.assertEqual(self.broadcast.call_args.args[0]["type"], "export_done")
        self.cleanup.assert_called_once_with()

    async def test_an_oversized_export_is_not_committed(self):
        """A bitrate target is not a size guarantee, so a Discord export used
        to be published at whatever size the encoder happened to hit."""
        self.manager.start("job", [sys.executable, "-c",
                           "import pathlib,sys; pathlib.Path(sys.argv[1])"
                           ".write_bytes(b'x' * (21 * 1024 * 1024))",
                           str(self.tmp)], 10, self.tmp, self.final,
                           cleanup=self.cleanup, max_bytes=20 * 1024 * 1024)
        await asyncio.wait_for(asyncio.gather(self.manager._task), 10)
        self.assertFalse(self.final.exists(), "an oversized export was published")
        self.assertFalse(self.tmp.exists())
        event = self.broadcast.call_args.args[0]
        self.assertEqual(event["type"], "export_error")
        self.assertFalse(event["canceled"])
        self.assertIn("21.0 MB", event["error"])
        self.cleanup.assert_called_once_with()

    async def test_an_export_within_its_size_limit_is_committed(self):
        self.manager.start("job", [sys.executable, "-c",
                           "import pathlib,sys; pathlib.Path(sys.argv[1])"
                           ".write_bytes(b'small')",
                           str(self.tmp)], 10, self.tmp, self.final,
                           cleanup=self.cleanup, max_bytes=20 * 1024 * 1024)
        await asyncio.wait_for(asyncio.gather(self.manager._task), 10)
        self.assertEqual(self.final.read_bytes(), b"small")
        self.assertEqual(self.broadcast.call_args.args[0]["type"], "export_done")

    async def test_server_shutdown_reaps_export_before_closing_websockets(self):
        server = share.ShareServer.__new__(share.ShareServer)
        server._proxy_tasks = set()
        server._exports = self.manager
        server._local_runner = server._public_runner = server._tunnel_proc = None
        self.start([sys.executable, "-c", "import time; time.sleep(60)"])
        proc = await self.wait_for_child()

        async def close_socket():
            self.assertIsNotNone(proc.returncode)
            self.assertFalse(self.manager.busy)

        ws = mock.Mock(close=mock.AsyncMock(side_effect=close_socket))
        server._ws_clients = {ws}
        await asyncio.wait_for(server.stop(), 2)
        ws.close.assert_awaited_once()
        self.cleanup.assert_called_once_with()
