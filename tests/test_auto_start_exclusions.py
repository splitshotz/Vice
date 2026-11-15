"""Exact process-name exclusions for automatic session starts."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import test_auto_start_sessions as sessions
from vice import config as config_mod
from vice.config import Config, RecordingConfig, normalize_session_auto_start_exclusions
from vice.active_window import WindowScan
from vice.main import AutoStartTarget
from vice.share import ShareServer


class AutoStartExclusionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Reuse its fixture setup and helpers without inheriting its test cases.
        sessions.AutoStartSessionTests.setUp(self)
        comms = {42: "cs2", 43: "overwatch.exe"}
        patcher = mock.patch(
            "vice.active_window._read_proc_comm",
            side_effect=lambda pid: comms.get(pid, ""),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    async def cleanup_session(self):
        self.daemon._auto_start_shutdown = True
        await self.daemon._stop_auto_start_task()
        await self.daemon._change_session(False, stop_reason="shutdown")

    async def detect(self):
        await self.daemon._auto_start_tick()
        await self.daemon._auto_start_tick()

    def test_exclusion_normalization(self):
        self.assertEqual(
            normalize_session_auto_start_exclusions(
                [" GameClient ", "gameclient", "", None, " Helper ", "helper"]
            ),
            ["GameClient", "Helper"],
        )
        self.assertEqual(Config().recording.session_auto_start_excluded_processes, [])

    async def test_filter_is_exact_process_only_and_keeps_two_scan_confirmation(self):
        d = self.daemon
        d.cfg.recording.session_auto_start_excluded_processes = ["CS2"]
        self.scan.return_value = WindowScan([self.win], None)
        await self.scan_tick_no_start()
        self.assertFalse(d._session_active)

        # A blocked window cannot outvote an eligible background game.
        d.cfg.recording.session_auto_start_excluded_processes = ["cs2"]
        self.scan.return_value = WindowScan([self.win, self.other], "game")
        await self.scan_tick_no_start()
        self.assertFalse(d._session_active)
        await d._auto_start_tick()
        self.assertTrue(d._session_active)
        self.assertEqual(d._session_game, "Overwatch 2")

        d.cfg.recording.session_auto_start_excluded_processes = ["over"]
        self.scan.return_value = WindowScan([self.other], None)
        targets, _ = d._scan_auto_start_targets()
        self.assertEqual([target.game for target in targets], ["Overwatch 2"])

    async def scan_tick_no_start(self):
        await self.daemon._auto_start_tick()
        self.assertFalse(self.daemon._session_active)

    async def test_comm_is_rechecked_before_start_and_unknown_comm_defers(self):
        d = self.daemon
        d.cfg.recording.session_auto_start_excluded_processes = ["CS2"]
        # The selection snapshot is stale and only its class resembles the
        # excluded name. The current process comm is authoritative.
        self.win["process"] = "launcher"
        self.win["class"] = "cs2"
        with mock.patch("vice.active_window._read_proc_comm", return_value="cs2") as read_comm:
            await self.detect()
        self.assertFalse(d._session_active)
        self.assertEqual(read_comm.call_count, 1)

        d._auto_start_pending = None
        with mock.patch("vice.active_window._read_proc_comm", return_value=""):
            await self.detect()
        self.assertFalse(d._session_active)

    async def test_pid_reuse_after_comm_read_blocks_start(self):
        d = self.daemon
        d.cfg.recording.session_auto_start_excluded_processes = ["other-name"]
        self.probe_game_process.side_effect = ["running", "exited"]
        with mock.patch("vice.active_window._read_proc_comm", return_value="cs2"):
            await self.detect()
        self.assertFalse(d._session_active)

    async def test_latest_exclusions_are_used_after_pending_process_probe(self):
        d = self.daemon
        d.cfg.recording.session_auto_start_excluded_processes = []

        def probe(_identity):
            d.cfg.recording.session_auto_start_excluded_processes = ["cs2"]
            return "running"

        self.probe_game_process.side_effect = probe
        with mock.patch("vice.active_window._read_proc_comm", return_value="cs2") as read_comm:
            await self.detect()
        self.assertEqual(read_comm.call_count, 1)
        self.assertFalse(d._session_active)

    async def test_latest_removed_exclusion_allows_selected_target(self):
        d = self.daemon
        d.cfg.recording.session_auto_start_excluded_processes = ["cs2"]
        self.win["process"] = "launcher"
        self.win["class"] = "cs2"
        with mock.patch(
            "vice.active_window._read_proc_comm",
            side_effect=lambda _pid: setattr(
                d.cfg.recording, "session_auto_start_excluded_processes", []
            ) or "cs2",
        ) as read_comm:
            await self.detect()
        self.assertEqual(read_comm.call_count, 1)
        self.assertTrue(d._session_active)

    async def test_removing_exclusion_requires_two_fresh_scans(self):
        d = self.daemon
        d.cfg.recording.session_auto_start_excluded_processes = ["cs2"]
        self.scan.return_value = WindowScan([self.win], None)
        await d._auto_start_tick()
        self.assertFalse(d._session_active)

        d.cfg.recording.session_auto_start_excluded_processes = []
        await d._auto_start_tick()
        self.assertFalse(d._session_active)
        await d._auto_start_tick()
        self.assertTrue(d._session_active)

    async def test_class_title_path_and_partial_names_do_not_match(self):
        d = self.daemon
        self.win.update({
            "process": "cs2",
            "class": "launcher",
            "title": "/usr/bin/cs2.exe",
        })
        d.cfg.recording.session_auto_start_excluded_processes = [
            "cs", "launcher", "/usr/bin/cs2.exe", "cs2.exe",
        ]
        targets, _ = d._scan_auto_start_targets()
        self.assertEqual([target.game for target in targets], ["Counter-Strike 2"])

    async def test_proc_comm_truncation_is_compared_as_observed(self):
        d = self.daemon
        observed = "abcdefghijklmnop"[:15]
        self.win["process"] = observed
        self.win["class"] = "cs2"
        d.cfg.recording.session_auto_start_excluded_processes = [observed]
        targets, _ = d._scan_auto_start_targets()
        self.assertEqual(targets, [])

    async def test_observed_15_character_comm_does_not_match_longer_configured_name(self):
        d = self.daemon
        observed = "abcdefghijklmnop"[:15]
        self.win["process"] = observed
        self.win["class"] = "cs2"
        d.cfg.recording.session_auto_start_excluded_processes = [observed + "q"]
        targets, _ = d._scan_auto_start_targets()
        self.assertEqual([target.game for target in targets], ["Counter-Strike 2"])

    async def test_exclusion_edit_does_not_interrupt_active_session(self):
        d = self.daemon
        await self.detect()
        token = d._session_token
        d.cfg.recording.session_auto_start_excluded_processes = ["cs2"]
        await d._auto_start_tick()
        self.assertTrue(d._session_active)
        self.assertIs(d._session_token, token)

    async def test_exclusion_added_during_inflight_start_does_not_cancel_it(self):
        d = self.daemon
        entered = asyncio.Event()
        release = asyncio.Event()
        original_start = d.recorder.start_session

        async def delayed_start(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original_start(*args, **kwargs)

        target = AutoStartTarget(
            "Counter-Strike 2", self.win, self.identities[42]
        )
        with mock.patch.object(d.recorder, "start_session", new=delayed_start):
            task = asyncio.create_task(d._change_session(True, automatic_target=target))
            await entered.wait()
            d.cfg.recording.session_auto_start_excluded_processes = ["cs2"]
            release.set()
            await task
        self.assertTrue(d._session_active)

    async def test_empty_exclusions_do_not_query_comm_and_manual_start_bypasses_them(self):
        d = self.daemon
        with mock.patch("vice.active_window._read_proc_comm") as read_comm:
            await self.detect()
        self.assertTrue(d._session_active)
        read_comm.assert_not_called()

        await d._stop_session()
        d._auto_start_pending = None
        d.cfg.recording.session_auto_start_excluded_processes = ["cs2"]
        await d._start_session()
        self.assertTrue(d._session_active)

    async def test_recording_signature_ignores_exclusion_edits(self):
        d = self.daemon
        before = d._recording_signature()
        d.cfg.recording.session_auto_start_excluded_processes = ["launcher"]
        self.assertEqual(d._recording_signature(), before)

    async def test_config_file_and_api_save_normalize_exclusions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "config.toml"
            with mock.patch.object(config_mod, "CONFIG_DIR", root), mock.patch.object(
                config_mod, "CONFIG_PATH", path
            ):
                config_mod.save(Config(recording=RecordingConfig(
                    session_auto_start_excluded_processes=["  launcher ", "LAUNCHER", "helper"]
                )))
                loaded = config_mod.load()
            self.assertEqual(
                loaded.recording.session_auto_start_excluded_processes,
                ["launcher", "helper"],
            )

        server = ShareServer(Config())
        request = mock.Mock()
        request.json = mock.AsyncMock(return_value={
            "recording": {"session_auto_start_excluded_processes": [" Game ", "game", "Tool"]}
        })
        with mock.patch("vice.config.load", return_value=server.cfg), mock.patch(
            "vice.config.save"
        ) as save_cfg:
            response = await server._api_set_config(request)
        self.assertEqual(response.status, 200)
        self.assertEqual(
            save_cfg.call_args.args[0].recording.session_auto_start_excluded_processes,
            ["Game", "Tool"],
        )


if __name__ == "__main__":
    unittest.main()
