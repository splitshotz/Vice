"""Session identity is captured at start, not borrowed from later replays."""

import asyncio
import json
import signal
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

try:
    import _sandbox  # noqa: F401
except ModuleNotFoundError:
    from tests import _sandbox  # noqa: F401

from vice.config import Config, DiscordCustomGame, OutputConfig
from vice.main import ViceDaemon
from vice.playlists import PlaylistStore, build_tag_index
from vice.recorder import (
    Recorder, GSRRecorder, SegmentRecorder, _next_session_path, filename_tag,
    _spawn_capture, _terminate_group, _next_clip_path,
)
from vice.share import ShareServer


class SessionGameTaggingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for target, value in (
            ("vice.playlists.PLAYLISTS_PATH", self.root / "playlists.json"),
            ("vice.share.SHARE_TOKENS_PATH", self.root / "tokens.json"),
            ("vice.share.VIEWS_PATH", self.root / "views.json"),
            ("vice.share.CLIP_GAMES_PATH", self.root / "clip_games.json"),
            ("vice.share.HIGHLIGHTS_DIR", self.root / "highlights"),
        ):
            patcher = mock.patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in ("play_session_start", "play_session_end", "play_highlight",
                     "play_screenshot"):
            patcher = mock.patch(f"vice.main.audio.{name}")
            patcher.start()
            self.addCleanup(patcher.stop)

        cfg = Config(output=OutputConfig(directory=str(self.root),
                                         tag_clips_with_game=True))
        cfg.recording.apply_watermark = False
        cfg.sharing.cloudflare_tunnel = False
        daemon = ViceDaemon.__new__(ViceDaemon)
        daemon.cfg = cfg
        daemon.recorder = GSRRecorder(cfg)
        daemon.share = ShareServer(cfg)
        daemon.share.broadcast = mock.AsyncMock()
        daemon.share._broadcast_clip = mock.AsyncMock()
        daemon.share._broadcast_playlists = mock.AsyncMock()
        daemon._session_lock = asyncio.Lock()
        daemon._session_active = False
        daemon._session_game = None
        daemon._session_path = None
        daemon._session_highlights = []
        daemon._last_clip_game = "Earlier replay"
        daemon._pending_recording_apply = False
        daemon._clip_count = 0
        daemon._ready = True
        daemon.hotkeys_available = True
        daemon._recorder_error = ""
        daemon.key_ring = None
        daemon._display_override = None
        daemon.recorder.on_clip_saved(daemon._on_clip_saved)
        self.daemon = daemon
        self.window = mock.patch("vice.active_window.get_active_window",
                                 return_value={"process": "cs2", "class": "cs2"})
        self.focus = self.window.start()
        self.addCleanup(self.window.stop)
        patcher = mock.patch.object(daemon, "_scan_visible_for_game", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

        # Exercise the real recorder path selection, startup, stop and callback
        # without starting a capture program or probing dummy media.
        def command(path):
            self.assertFalse(path.exists())
            path.write_bytes(b"session")
            return ["capture", str(path)]

        proc = mock.Mock()
        proc.wait = mock.AsyncMock(side_effect=asyncio.TimeoutError)
        proc.stderr = None
        for patcher in (
            mock.patch.object(daemon.recorder, "_build_session_cmd", side_effect=command),
            mock.patch("vice.recorder._spawn_capture", new=mock.AsyncMock(return_value=proc)),
            mock.patch("vice.recorder._terminate_group", new=mock.AsyncMock()),
            mock.patch("vice.recorder._read_stream_text", new=mock.AsyncMock(return_value="")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_session_uses_configured_clip_template(self) -> None:
        self.daemon.cfg.output.clip_name_template = "  recording_$n_$game  "
        await self.daemon._start_session()
        self.assertEqual(
            self.daemon._session_path.name, "recording_1_Counter-Strike-2.mp4"
        )
        with mock.patch.object(
            self.daemon.share, "add_clip", wraps=self.daemon.share.add_clip,
        ) as add:
            await self.daemon._stop_session()
        add.assert_called_once_with(
            self.root / "recording_1_Counter-Strike-2.mp4",
            game="Counter-Strike 2", session=True,
        )
        self.assertEqual(self.daemon._last_clip_game, "Earlier replay")

    def assert_cleared(self) -> None:
        self.assertFalse(self.daemon._session_active)
        self.assertIsNone(self.daemon._session_game)
        self.assertIsNone(self.daemon._session_path)
        self.assertEqual(self.daemon._session_highlights, [])

    async def clip_game(self, server: ShareServer, slug: str) -> str | None:
        req = mock.Mock(match_info={"slug": slug})
        with mock.patch.object(server, "_get_meta", new=mock.AsyncMock(return_value={})):
            response = await server._api_clip_info(req)
        return json.loads(response.text)["game"]

    async def test_tag_and_playlist_toggles_are_independent(self) -> None:
        for tagged, playlist in ((True, True), (False, True), (True, False), (False, False)):
            with self.subTest(tagged=tagged, playlist=playlist):
                d = self.daemon
                d.share.playlists = PlaylistStore(self.root / f"{tagged}-{playlist}.json")
                d.cfg.output.tag_clips_with_game = tagged
                d.cfg.output.auto_playlist_by_game = playlist
                await d._start_session()
                path = d._session_path
                self.assertEqual(path.name.endswith("_Counter-Strike-2.mp4"), tagged)
                self.assertEqual(d._session_game, "Counter-Strike 2")
                self.assertEqual(d._last_clip_game, "Earlier replay")
                with mock.patch.object(d.share, "add_clip", wraps=d.share.add_clip) as add:
                    await d._stop_session()
                add.assert_called_once_with(path, game="Counter-Strike 2", session=True)
                auto = d.share.playlists.get("auto:counter-strike-2")
                if playlist:
                    self.assertIn(path.stem, auto["clip_slugs"])
                else:
                    self.assertIsNone(auto)
                self.assertIn(path.stem, d.share._clips)
                self.assertEqual(await self.clip_game(d.share, path.stem), "Counter-Strike 2")
                restarted = ShareServer(d.cfg)
                with mock.patch("vice.share.web.TCPSite.start", new=mock.AsyncMock()), \
                     mock.patch("vice.share._local_ip", return_value="127.0.0.1"):
                    try:
                        await restarted.start()
                        self.assertEqual(await self.clip_game(restarted, path.stem),
                                         "Counter-Strike 2")
                    finally:
                        await restarted.stop()
                self.assert_cleared()
                await asyncio.sleep(0)

    async def test_sanitized_raw_game_and_deep_rock_galactic(self) -> None:
        for game in ("Deep Rock Galactic", "A / custom: Game!?", "x" * 70):
            with self.subTest(game=game), mock.patch.object(self.daemon, "_match_game", return_value=game):
                await self.daemon._start_session()
                path = self.daemon._session_path
                self.assertTrue(path.name.endswith(f"_{filename_tag(game)}.mp4"))
                self.assertEqual(self.daemon._session_game, game)
                await self.daemon._stop_session()
                self.assertEqual(self.daemon.share.playlists.game_for(path.stem), game)

    async def test_focus_replays_and_screenshots_do_not_change_session_identity_or_highlights(self) -> None:
        d = self.daemon
        await d._start_session()
        path = d._session_path
        self.focus.return_value = {"process": "overwatch.exe", "class": "overwatch.exe"}
        self.assertEqual(d._clip_game_tag(), "Overwatch 2")
        shot = self.root / "Vice_Shot_1_Overwatch-2.png"
        d.share.next_image_path = mock.Mock(return_value=shot)
        d.share.add_image = mock.Mock()
        with mock.patch("vice.main.capture_screenshot", new=mock.AsyncMock()), \
             mock.patch("vice.main.copy_image_to_clipboard", new=mock.AsyncMock(return_value=(True, ""))):
            await d._take_screenshot()
        d.share.add_image.assert_called_once_with(shot, game="Overwatch 2")
        with mock.patch.object(d.recorder, "session_elapsed", return_value=12.345):
            await d._handle_clip_hotkey()
        await d._stop_session()
        self.assertEqual(d.share.playlists.game_for(path.stem), "Counter-Strike 2")
        highlights = json.loads((self.root / "highlights" / f"{path.stem}.json").read_text())
        self.assertEqual(highlights[0]["time"], 12.345)
        self.assertEqual(highlights[0]["id"], "1")
        self.assertEqual(d._last_clip_game, "Overwatch 2")
        self.assert_cleared()

    async def test_consecutive_unknown_session_does_not_reuse_identity(self) -> None:
        d = self.daemon
        await d._start_session()
        await d._stop_session()
        self.focus.return_value = None
        await d._start_session()
        path = d._session_path
        self.assertEqual(path.name, "Vice_Session_2.mp4")
        await d._stop_session()
        self.assertIsNone(d.share.playlists.game_for(path.stem))
        self.assertIsNone(await self.clip_game(d.share, path.stem))
        self.assert_cleared()

    async def test_rename_delete_and_reuse_session_slug(self) -> None:
        d = self.daemon
        d.cfg.output.auto_playlist_by_game = False
        await d._start_session()
        old_slug = d._session_path.stem
        await d._stop_session()

        rename = mock.Mock(match_info={"slug": old_slug},
                           json=mock.AsyncMock(return_value={"name": "Renamed"}))
        body = json.loads((await d.share._api_rename(rename)).text)
        self.assertTrue(body["ok"])
        slug = body["slug"]
        self.assertEqual(await self.clip_game(d.share, slug), "Counter-Strike 2")

        reused = self.root / f"{old_slug}.mp4"
        reused.write_bytes(b"replacement replay")
        d.share.add_clip(reused)
        self.assertIsNone(await self.clip_game(d.share, old_slug))
        renamed = d.share._clips[slug]
        d.share.add_clip(renamed)
        self.assertIsNone(await self.clip_game(d.share, slug))

        d.share.add_clip(renamed, game="Counter-Strike 2", session=True)
        await d.share._api_delete(mock.Mock(match_info={"slug": slug}))
        self.assertEqual(json.loads((self.root / "clip_games.json").read_text()), {})

    async def test_sessions_keep_custom_steam_and_visible_window_detection(self) -> None:
        d = self.daemon
        d.cfg.discord.custom_games = [DiscordCustomGame(name="My Custom Game", matches=["cs2"])]
        await d._start_session()
        self.assertEqual(d._session_game, "My Custom Game")
        await d._stop_session()
        d.cfg.discord.custom_games = []
        self.focus.return_value = {"process": "unknown", "class": "steam_app_123456789"}
        with mock.patch("vice.main.steam_game_name", return_value="Steam-only Game"):
            await d._start_session()
        self.assertEqual(d._session_game, "Steam-only Game")
        await d._stop_session()
        self.focus.return_value = None
        with mock.patch.object(d, "_scan_visible_for_game", return_value=("Deep Rock Galactic", {})):
            await d._start_session()
        self.assertEqual(d._session_game, "Deep Rock Galactic")
        await d._stop_session()

    async def test_repeated_start_cannot_replace_captured_identity(self) -> None:
        d = self.daemon
        await d._start_session()
        path = d._session_path
        self.focus.return_value = None
        await d._start_session()
        self.assertEqual(d._session_path, path)
        self.assertEqual(d._session_game, "Counter-Strike 2")
        await d._stop_session()

    async def test_stop_without_share_still_clears_highlights_and_identity(self) -> None:
        d = self.daemon
        d.share = None
        await d._start_session()
        d._session_highlights = [{"time": 1}]
        await d._stop_session()
        self.assert_cleared()

    async def test_start_failures_and_cancellation_clear_stale_state(self) -> None:
        d = self.daemon
        for error in (None, RuntimeError("capture failed"), asyncio.CancelledError()):
            with self.subTest(error=error):
                d._session_game = "Stale game"
                d._session_highlights = [{"time": 1}]
                with mock.patch.object(d.recorder, "start_session", new=mock.AsyncMock(
                        return_value=None, side_effect=error)):
                    if error is None:
                        await d._start_session()
                    else:
                        with self.assertRaises(type(error)):
                            await d._start_session()
                self.assert_cleared()

    async def test_detection_exception_records_untagged_and_logs_traceback(self) -> None:
        d = self.daemon
        d._session_game = "Stale game"
        with mock.patch.object(d, "_detect_game", side_effect=RuntimeError("detection failed")), \
             self.assertLogs("vice", level="ERROR") as logs:
            await d._start_session()
        self.assertIsNotNone(logs.records[0].exc_info)
        self.assertTrue(d._session_active)
        self.assertIsNone(d._session_game)
        self.assertEqual(d._session_path.name, "Vice_Session_1.mp4")
        await d._stop_session()
        self.assert_cleared()
        # Normal adapter exceptions are already best-effort: capture continues
        # without a tag, and never inherits the previous session's identity.
        self.focus.side_effect = RuntimeError("adapter failed")
        await d._start_session()
        self.assertEqual(d._session_path.name, "Vice_Session_2.mp4")
        await d._stop_session()
        self.assert_cleared()

    async def test_detection_cancellation_is_not_swallowed(self) -> None:
        d = self.daemon
        entered, release = asyncio.Event(), asyncio.Event()

        async def detect(*args):
            entered.set()
            await release.wait()

        with mock.patch("vice.main.asyncio.to_thread", side_effect=detect), \
             mock.patch.object(d.recorder, "start_session") as start:
            task = asyncio.create_task(d._start_session())
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            start.assert_not_called()
        self.assert_cleared()

    async def test_toggle_queued_during_start_stops_instead_of_being_lost(self) -> None:
        d = self.daemon
        entered, release = asyncio.Event(), asyncio.Event()
        original_start = d.recorder.start_session

        async def slow_start(**kwargs):
            entered.set()
            await release.wait()
            return await original_start(**kwargs)

        with mock.patch.object(d.recorder, "start_session", side_effect=slow_start) as start, \
             mock.patch.object(d.share, "add_clip", wraps=d.share.add_clip) as add:
            first = asyncio.create_task(d._handle_session_toggle())
            await entered.wait()
            second = asyncio.create_task(d._handle_session_toggle())
            await asyncio.sleep(0)
            self.assertFalse(second.done())
            release.set()
            await asyncio.gather(first, second)
        start.assert_awaited_once()
        add.assert_called_once()
        self.assert_cleared()

    async def test_toggle_queued_during_stop_starts_after_finalization(self) -> None:
        d = self.daemon
        await d._start_session()
        old_path = d._session_path
        d._session_highlights = [{"time": 4}]
        entered, release = asyncio.Event(), asyncio.Event()

        async def watermark(path):
            entered.set()
            await release.wait()

        d.cfg.recording.apply_watermark = True
        with mock.patch("vice.recorder._apply_watermark", side_effect=watermark) as stamp, \
             mock.patch.object(d.share, "add_clip", wraps=d.share.add_clip) as add, \
             mock.patch.object(d.recorder, "_emit", wraps=d.recorder._emit) as emit:
            first = asyncio.create_task(d._handle_session_toggle())
            await entered.wait()
            second = asyncio.create_task(d._handle_session_toggle())
            await asyncio.sleep(0)
            self.assertFalse(second.done())
            self.assertEqual(d._session_game, "Counter-Strike 2")
            self.focus.return_value = {"process": "overwatch.exe", "class": "overwatch.exe"}
            release.set()
            await asyncio.gather(first, second)
            stamp.assert_awaited_once_with(old_path)
            emit.assert_called_once_with(old_path)
            add.assert_called_once_with(old_path, game="Counter-Strike 2", session=True)
        self.assertEqual(d._session_game, "Overwatch 2")
        self.assertNotEqual(d._session_path, old_path)
        self.assertEqual(d._session_highlights, [])
        self.assertTrue((self.root / "highlights" / f"{old_path.stem}.json").exists())
        d.cfg.recording.apply_watermark = False
        await d._stop_session()

    async def test_spawn_failure_does_not_try_to_terminate_none(self) -> None:
        with mock.patch("vice.recorder._spawn_capture", side_effect=OSError("spawn failed")), \
             mock.patch("vice.recorder._terminate_session_capture") as terminate, \
             mock.patch("vice.recorder._register_capture") as register:
            await self.daemon._start_session()
        terminate.assert_not_called()
        register.assert_not_called()
        self.assertIsNone(self.daemon.recorder._session_proc)
        self.assert_cleared()

    async def test_cleanup_exception_clears_state_without_registering_clip(self) -> None:
        d = self.daemon
        await d._start_session()
        d._session_highlights = [{"time": 6}]
        with mock.patch("vice.recorder._terminate_group", side_effect=RuntimeError("cleanup failed")), \
             mock.patch.object(d.share, "add_clip") as add:
            with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
                await d._stop_session()
            add.assert_not_called()
        self.assertIsNone(d.recorder._session_proc)
        self.assertFalse(d.recorder._session_active)
        self.assert_cleared()

    async def test_watchdog_replay_recovery_preserves_session_until_daemon_stop(self) -> None:
        d = self.daemon
        for cls in (GSRRecorder, SegmentRecorder):
            with self.subTest(backend=cls.__name__):
                # Initialize shared session state without backend hardware
                # probes. Exercise the real backend stop in the watchdog.
                recorder = cls.__new__(cls)
                Recorder.__init__(recorder, d.cfg)
                recorder._proc = recorder._watch_task = None
                recorder._current_proc = recorder._loop_task = None
                recorder._use_wf = False
                recorder._build_session_cmd = d.recorder._build_session_cmd
                recorder.on_clip_saved(d._on_clip_saved)
                d.recorder = recorder
                d._config_apply_lock = asyncio.Lock()
                d._clip_lock = asyncio.Lock()
                d._window_capture_id = None
                await d._start_session()
                path, proc = d._session_path, recorder._session_proc
                d._session_highlights = [{"time": 7}]
                restarting, finish = asyncio.Event(), asyncio.Event()

                async def restart():
                    restarting.set()
                    await finish.wait()

                with mock.patch.object(recorder, "is_healthy", return_value=False), \
                     mock.patch.object(recorder, "last_output", return_value="replay died"), \
                     mock.patch.object(recorder, "start", side_effect=restart), \
                     mock.patch.object(recorder, "stop_session", wraps=recorder.stop_session) as stop_session, \
                     mock.patch.object(d, "_output_dir_problem", return_value=None), \
                     mock.patch.object(d, "_broadcast_status"), \
                     mock.patch("vice.main.load_user_systemd_env", return_value=[]), \
                     mock.patch("vice.main.time.time", return_value=100), \
                     mock.patch("vice.main.asyncio.sleep", side_effect=[None, asyncio.CancelledError()]), \
                     mock.patch.object(d.share, "add_clip", wraps=d.share.add_clip) as add:
                    watchdog = asyncio.create_task(d._recorder_watchdog_loop())
                    try:
                        await asyncio.wait_for(restarting.wait(), timeout=5)
                        stop_session.assert_not_called()
                        self.assertTrue(recorder._session_active)
                        self.assertIs(recorder._session_proc, proc)
                        self.assertTrue(d._session_active)
                        self.assertEqual(d._session_game, "Counter-Strike 2")
                        self.assertEqual(d._session_highlights, [{"time": 7}])
                        add.assert_not_called()
                    finally:
                        finish.set()
                        with self.assertRaises(asyncio.CancelledError):
                            await watchdog
                    await d._stop_session()
                    stop_session.assert_awaited_once_with()
                    add.assert_called_once_with(path, game="Counter-Strike 2", session=True)
                self.assertEqual(d.share.playlists.game_for(path.stem), "Counter-Strike 2")
                highlights = json.loads((self.root / "highlights" / f"{path.stem}.json").read_text())
                self.assertEqual(highlights[0]["time"], 7)
                self.assert_cleared()

    async def test_shutdown_finalizes_active_session_before_stopping_recorder(self) -> None:
        d = self.daemon
        await d._start_session()
        path = d._session_path
        d._session_highlights = [{"time": 5}]
        d._pending_recording_apply = True
        for field in ("_watchdog_task", "_upgrade_task", "_update_task",
                      "_follow_mouse_task", "_window_capture_task", "_discord_task"):
            setattr(d, field, None)
        d.key_overlay = mock.Mock(stop=mock.AsyncMock())
        d.hotkeys = mock.Mock(stop=mock.AsyncMock())
        d.share.stop = mock.AsyncMock()
        d._clear_discord_presence = mock.AsyncMock()
        entered, release = asyncio.Event(), asyncio.Event()

        async def terminate(*args, **kwargs):
            entered.set()
            await release.wait()

        with mock.patch("vice.recorder._terminate_group", side_effect=terminate), \
             mock.patch.object(d, "_apply_live_config") as apply_config, \
             mock.patch.object(d.recorder, "stop", new=mock.AsyncMock()) as stop, \
             mock.patch.object(d.share, "add_clip", wraps=d.share.add_clip) as add, \
             mock.patch("vice.main.PID_FILE", self.root / "pid"), \
             mock.patch("vice.main.SOCKET_FILE", self.root / "socket"):
            shutdown = asyncio.create_task(d._shutdown(mock.Mock()))
            await entered.wait()
            self.assertTrue(d._session_lock.locked())
            stop.assert_not_called()
            release.set()
            await shutdown
            stop.assert_awaited_once_with()
            apply_config.assert_not_called()
            add.assert_called_once_with(path, game="Counter-Strike 2", session=True)
        self.assertTrue((self.root / "highlights" / f"{path.stem}.json").exists())
        self.assert_cleared()

    async def test_failed_or_interrupted_stop_clears_identity_and_highlights(self) -> None:
        d = self.daemon
        for error in (None, RuntimeError("stop failed"), asyncio.CancelledError()):
            with self.subTest(error=error):
                # Mock both boundaries here so a cancelled fake backend cannot
                # leave its internal active flag set for the next subtest.
                with mock.patch.object(d.recorder, "start_session", new=mock.AsyncMock(
                        return_value=self.root / "Vice_Session_1_Counter-Strike-2.mp4")):
                    await d._start_session()
                d._session_highlights = [{"time": 1}]
                with mock.patch.object(d.recorder, "stop_session", new=mock.AsyncMock(
                        return_value=None, side_effect=error)), \
                     mock.patch.object(d.share, "add_clip") as add:
                    if error is None:
                        await d._stop_session()
                    else:
                        with self.assertRaises(type(error)):
                            await d._stop_session()
                    add.assert_not_called()
                self.assert_cleared()

    async def test_start_waits_for_old_stop_and_keeps_highlights_with_old_slug(self) -> None:
        d = self.daemon
        await d._start_session()
        old_path = d._session_path
        d._session_highlights = [{"time": 3, "label": "Old", "color": "#ffffff"}]
        stopping = asyncio.Event()
        finish = asyncio.Event()
        original_stop = d.recorder.stop_session

        async def slow_stop():
            stopping.set()
            await finish.wait()
            return await original_stop()

        with mock.patch.object(d.recorder, "stop_session", side_effect=slow_stop):
            stop = asyncio.create_task(d._stop_session())
            await stopping.wait()
            self.focus.return_value = {"process": "overwatch.exe", "class": "overwatch.exe"}
            start = asyncio.create_task(d._start_session())
            await asyncio.sleep(0)
            self.assertFalse(start.done())
            finish.set()
            await asyncio.gather(stop, start)
        self.assertEqual(d._session_game, "Overwatch 2")
        self.assertEqual(d._session_highlights, [])
        self.assertEqual(d.share.playlists.game_for(old_path.stem), "Counter-Strike 2")
        self.assertTrue((self.root / "highlights" / f"{old_path.stem}.json").exists())
        await d._stop_session()

    async def _cancel_real_capture(self, *, during_start: bool) -> None:
        class Process:
            def __init__(self, pid):
                self.pid = pid
                self.returncode = None
                self.stderr = None
                self.waiting = asyncio.Event()
                self.exited = asyncio.Event()

            async def wait(self):
                self.waiting.set()
                await self.exited.wait()
                return self.returncode

            def finish(self):
                self.returncode = 0
                self.exited.set()

        first, second = Process(41001), Process(41002)
        terminating = asyncio.Event()

        def send_signal(proc, sig):
            if proc is first and sig == signal.SIGTERM:
                terminating.set()
            else:
                proc.finish()

        d = self.daemon
        with mock.patch("vice.recorder._spawn_capture", new=_spawn_capture), \
             mock.patch("vice.recorder._terminate_group", new=_terminate_group), \
             mock.patch("vice.recorder.asyncio.create_subprocess_exec",
                        new=mock.AsyncMock(side_effect=[first, second])), \
             mock.patch("vice.recorder._signal_group", side_effect=send_signal) as signals, \
             mock.patch("vice.recorder._register_capture") as register, \
             mock.patch("vice.recorder._unregister_capture") as unregister:
            if during_start:
                task = asyncio.create_task(d._start_session())
                await first.waiting.wait()
                old_path = next(self.root.glob("Vice_Session_*.mp4"))
                task.cancel()
                await terminating.wait()
            else:
                await d._start_session()
                old_path = d._session_path
                d._session_highlights = [{"time": 9}]
                task = asyncio.create_task(d._stop_session())
                await terminating.wait()
                task.cancel()

            await asyncio.sleep(0)
            self.assertFalse(task.done())
            self.assertTrue(d._session_lock.locked())
            self.assertIs(d.recorder._session_proc, first)
            unregister.assert_not_called()
            # Configuration replacement must defer throughout readiness and
            # termination, not just while the daemon reports active capture.
            recorder = d.recorder
            self.assertFalse(await d._restart_recorder_for_config())
            self.assertIs(d.recorder, recorder)
            self.assertTrue(d._pending_recording_apply)
            d._pending_recording_apply = False

            # Repeated cancellation cannot detach the reaper from its owner.
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            first.finish()
            with self.assertRaises(asyncio.CancelledError):
                await task
            unregister.assert_called_once_with(first.pid)
            self.assertIn(mock.call(first, signal.SIGKILL), signals.call_args_list)
            self.assertIsNone(recorder._session_proc)
            self.assertIsNone(recorder._session_path)
            self.assertEqual(recorder._session_program, "")
            self.assertFalse(recorder._session_active)
            self.assert_cleared()
            self.assertNotIn(old_path.stem, d.share._clips)
            self.assertFalse((self.root / "highlights" / f"{old_path.stem}.json").exists())

            self.focus.return_value = {"process": "overwatch.exe", "class": "overwatch.exe"}
            await d._start_session()
            new_path = d._session_path
            self.assertNotEqual(new_path, old_path)
            self.assertEqual(d._session_game, "Overwatch 2")
            self.assertEqual(d._session_highlights, [])
            await d._stop_session()
            self.assertEqual(d.share.playlists.game_for(new_path.stem), "Overwatch 2")
            self.assertEqual(register.call_count, 2)
            self.assertEqual(unregister.call_args_list,
                             [mock.call(first.pid), mock.call(second.pid)])
            self.assert_cleared()

    async def test_cancelled_readiness_reaps_capture_before_clearing_session(self) -> None:
        await self._cancel_real_capture(during_start=True)

    async def test_cancelled_stop_reaps_capture_before_clearing_session(self) -> None:
        await self._cancel_real_capture(during_start=False)

    async def test_session_toggle_waits_for_recorder_replacement(self) -> None:
        d = self.daemon
        replacement = GSRRecorder(d.cfg)
        stopping = asyncio.Event()
        finish = asyncio.Event()

        async def stop_replay():
            stopping.set()
            await finish.wait()

        d._window_capture_id = None
        with mock.patch("vice.main.create_recorder", return_value=replacement), \
             mock.patch.object(d.recorder, "stop", side_effect=stop_replay), \
             mock.patch.object(replacement, "start", new=mock.AsyncMock()), \
             mock.patch.object(replacement, "start_session", new=mock.AsyncMock(return_value=None)) as start_session:
            restart = asyncio.create_task(d._restart_recorder_for_config())
            await stopping.wait()
            start = asyncio.create_task(d._handle_session_toggle())
            await asyncio.sleep(0)
            start_session.assert_not_called()
            finish.set()
            self.assertTrue(await restart)
            await start
            start_session.assert_awaited_once_with(tag="Counter-Strike 2")
        self.assertIs(d.recorder, replacement)
        self.assert_cleared()

    async def test_deferred_config_apply_runs_after_releasing_session_lock(self) -> None:
        d = self.daemon
        await d._start_session()
        d._pending_recording_apply = True
        d._recording_sig = None

        async def apply():
            self.assertFalse(d._session_lock.locked())
            self.assert_cleared()
            # This must be able to acquire the same lock for replacement.
            async with d._session_lock:
                pass

        with mock.patch.object(d, "_apply_live_config", side_effect=apply) as apply_config:
            await d._stop_session()
        apply_config.assert_awaited_once_with()

    def test_mixed_extensions_and_tags_number_without_overwrite(self) -> None:
        names = ("Vice_Session_1.mp4", "Vice_Session_2_Deep-Rock-Galactic.mkv",
                 "Vice_Session_3.mkv", "Vice_Session_4_Counter-Strike-2.mp4")
        for name in names:
            (self.root / name).write_bytes(b"original")
        for ext, tag in (("mp4", None), ("mkv", "Deep Rock Galactic")):
            path = _next_session_path(self.root, ext, tag)
            self.assertTrue(path.name.startswith("Vice_Session_5"))
            self.assertFalse(path.exists())
        for name in names:
            self.assertEqual((self.root / name).read_bytes(), b"original")

    def test_tagged_sessions_backfill_after_restart(self) -> None:
        slugs = {"Vice_Session_1_Deep-Rock-Galactic", "Vice_Session_2_Counter-Strike-2",
                 "Vice_Session_3"}
        store = PlaylistStore(self.root / "restart.json")
        store.backfill(slugs, build_tag_index())
        reloaded = PlaylistStore(store.path)
        self.assertEqual(reloaded.game_for("Vice_Session_1_Deep-Rock-Galactic"), "Deep Rock Galactic")
        self.assertEqual(reloaded.game_for("Vice_Session_2_Counter-Strike-2"), "Counter-Strike 2")
        self.assertIsNone(reloaded.game_for("Vice_Session_3"))


class SessionNameTemplateTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_all_tokens_and_sanitized_game(self) -> None:
        with mock.patch("vice.recorder.datetime") as clock:
            clock.now.return_value = datetime(2026, 11, 15, 9, 5)
            path = _next_session_path(
                self.root, "mkv", "Deep Rock Galactic",
                template="recording_$n_$date_$time_$game",
            )
        self.assertEqual(
            path.name, "recording_1_2026-11-15_0905_Deep-Rock-Galactic.mkv"
        )

    def test_clips_and_sessions_share_numbering_across_containers(self) -> None:
        template = "recording_$n"
        clip = _next_clip_path(self.root, template=template)
        clip.touch()
        session = _next_session_path(self.root, "mkv", template=template)
        self.assertEqual(session.name, "recording_2.mkv")
        session.touch()
        self.assertEqual(
            _next_clip_path(self.root, template=template).name, "recording_3.mp4"
        )

    def test_collision_keeps_existing_file(self) -> None:
        existing = _next_clip_path(self.root, template="recording")
        existing.write_bytes(b"original")
        path = _next_session_path(self.root, template="recording")
        self.assertEqual(path.name, "recording_2.mp4")
        self.assertFalse(path.exists())
        self.assertEqual(existing.read_bytes(), b"original")

    def test_empty_and_unusable_templates_keep_session_default(self) -> None:
        (self.root / "Vice_Session_4.mkv").touch()
        for template in ("", "   ", "$game", "/\\"):
            with self.subTest(template=template):
                self.assertEqual(
                    _next_session_path(self.root, template=template).name,
                    "Vice_Session_5.mp4",
                )
        self.assertEqual(
            _next_session_path(self.root, tag="Deep Rock Galactic").name,
            "Vice_Session_5_Deep-Rock-Galactic.mp4",
        )

    def test_template_cannot_escape_output_directory(self) -> None:
        path = _next_session_path(self.root, template="../../recording_$n")
        self.assertEqual(path.parent, self.root)
        self.assertEqual(path.name, "recording_1.mp4")
