"""Automatic recording selection, retries, durable manual control and races."""
import asyncio
import json
import time
import unittest
from unittest import mock

from tests import test_session_game_tagging as tagging
from vice.active_window import WindowScan
from vice.main import AutoStartTarget
from vice.session_auto_start import AutoStartStateError, SuppressionStore
from vice.session_process import ProcessIdentity

BOOT = '11111111-1111-1111-1111-111111111111'


class AutoStartSessionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tagging.SessionGameTaggingTests.setUp(self)
        d = self.daemon
        d._init_auto_start()
        d.cfg.recording.session_start_on_game_detect = True
        self.state_path = self.root / 'auto_start.json'
        self.identities = {42: ProcessIdentity(42, 100), 43: ProcessIdentity(43, 200)}
        self.win = {'pid': 42, 'process': 'cs2', 'class': 'cs2', 'window_id': 'game'}
        self.focus.return_value = self.win
        self.other = {'pid': 43, 'process': 'overwatch.exe', 'class': 'overwatch.exe', 'window_id': 'other'}
        for name, value in (
            ('vice.main.read_host_boot_id', BOOT),
            ('vice.main.auto_start_scan_supported', True),
            ('vice.main.probe_game_process', 'running'),
        ):
            patcher = mock.patch(name, return_value=value)
            setattr(self, name.rsplit('.', 1)[1], patcher.start())
            self.addCleanup(patcher.stop)
        patcher = mock.patch('vice.main.SuppressionStore',
                             side_effect=lambda boot: SuppressionStore(boot, self.state_path))
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch('vice.main.scan_game_windows', return_value=WindowScan([self.win], None))
        self.scan = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch('vice.main.bind_game_process',
                             side_effect=lambda win: self.identities.get(win.get('pid')))
        self.bind = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(d.recorder, 'session_capture_exited', return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addAsyncCleanup(self.cleanup_session)

    async def cleanup_session(self):
        self.daemon._auto_start_shutdown = True
        await self.daemon._stop_auto_start_task()
        await self.daemon._change_session(False, stop_reason='shutdown')

    async def detect(self):
        await self.daemon._auto_start_tick()
        await self.daemon._auto_start_tick()

    async def test_already_running_background_game_starts_after_two_scans(self):
        d = self.daemon
        await d._auto_start_tick()
        self.assertFalse(d._session_active)
        await d._auto_start_tick()
        self.assertTrue(d._session_active)
        self.assertEqual(d._session_game, 'Counter-Strike 2')
        self.assertEqual(d._session_process_identity, self.identities[42])
        self.assertIsNone(getattr(d, '_session_monitor_task', None))
        await asyncio.sleep(0)
        events = [call.args[0] for call in d.share.broadcast.call_args_list]
        start = next(event for event in events if event['type'] == 'session_start')
        self.assertEqual(start['origin'], 'automatic')
        self.assertEqual(start['auto_stop'], 'off')

    async def test_default_off_does_not_scan_or_read_state(self):
        self.daemon.cfg.recording.session_start_on_game_detect = False
        await self.daemon._sync_auto_start_task()
        await self.daemon._auto_start_tick()
        self.scan.assert_not_called()
        self.read_host_boot_id.assert_not_called()
        self.assertIsNone(self.daemon._auto_start_task)

    async def test_transient_empty_failed_or_changed_scan_resets_confirmation(self):
        d = self.daemon
        for interrupted in (None, WindowScan([], None), WindowScan([self.other], None)):
            with self.subTest(interrupted=interrupted):
                self.scan.return_value = WindowScan([self.win], None)
                await d._auto_start_tick()
                self.scan.return_value = interrupted
                await d._auto_start_tick()
                self.scan.return_value = WindowScan([self.win], None)
                await d._auto_start_tick()
                self.assertFalse(d._session_active)
                d._auto_start_pending = None
        await d._auto_start_tick()
        await d._auto_start_tick()
        self.assertTrue(d._session_active)

    async def test_multiple_games_wait_for_focus_before_suppression_filter(self):
        d = self.daemon
        await d._ensure_auto_start_store()
        await d._save_auto_start_suppression({self.identities[42]})
        self.scan.return_value = WindowScan([self.win, self.other], None)
        await self.detect()
        self.assertFalse(d._session_active)
        self.scan.return_value = WindowScan([self.win, self.other], 'game')
        await self.detect()
        self.assertFalse(d._session_active)
        self.scan.return_value = WindowScan([self.win, self.other], 'other')
        await self.detect()
        self.assertEqual(d._session_game, 'Overwatch 2')

    async def test_duplicate_windows_and_missing_identity(self):
        duplicate = dict(self.win, window_id='second')
        missing = {'pid': 0, 'process': 'cs2', 'class': 'cs2', 'window_id': 'missing'}
        self.scan.return_value = WindowScan([self.win, duplicate, missing], None)
        await self.detect()
        self.assertTrue(self.daemon._session_active)
        self.assertEqual(self.daemon._session_process_identity, self.identities[42])

    async def test_untrackable_game_is_never_started(self):
        self.bind.return_value = None
        self.bind.side_effect = None
        await self.detect()
        self.assertFalse(self.daemon._session_active)

    async def test_active_session_is_preserved_without_queueing_another_game(self):
        d = self.daemon
        await self.detect()
        token = d._session_token
        self.scan.reset_mock()
        self.scan.return_value = WindowScan([self.other], 'other')
        await self.detect()
        self.scan.assert_not_called()
        self.assertIs(d._session_token, token)
        await d._stop_session()
        await d._auto_start_tick()
        self.assertFalse(d._session_active)
        await d._auto_start_tick()
        self.assertEqual(d._session_game, 'Overwatch 2')

    async def test_manual_stop_persists_across_restart_and_setting_toggle(self):
        d = self.daemon
        await self.detect()
        await d._stop_session()
        self.assertEqual(SuppressionStore(BOOT, self.state_path).load(), {self.identities[42]})
        # Simulate a new controller in the same boot, as after daemon restart.
        del d._auto_start_task
        d._init_auto_start()
        await self.detect()
        self.assertFalse(d._session_active)
        d.cfg.recording.session_start_on_game_detect = False
        await d._sync_auto_start_task()
        d.cfg.recording.session_start_on_game_detect = True
        await self.detect()
        self.assertFalse(d._session_active)
        # Explicit manual start bypasses automatic suppression.
        await d._start_session()
        self.assertTrue(d._session_active)

    async def test_unknown_or_disappearing_window_does_not_release_manual_stop(self):
        d = self.daemon
        await self.detect()
        await d._stop_session()
        self.probe_game_process.return_value = 'unknown'
        self.scan.return_value = WindowScan([], None)
        await self.detect()
        self.assertIn(self.identities[42], d._auto_start_suppressed)
        self.scan.return_value = WindowScan([self.win], None)
        self.probe_game_process.return_value = 'running'
        await self.detect()
        self.assertFalse(d._session_active)

    async def test_game_relaunch_pid_reuse_is_a_new_eligible_instance(self):
        d = self.daemon
        await self.detect()
        await d._stop_session()
        old = self.identities[42]
        new = ProcessIdentity(42, 300)
        self.identities[42] = new
        self.probe_game_process.side_effect = lambda identity: 'exited' if identity == old else 'running'
        await self.detect()
        self.assertEqual(d._session_process_identity, new)
        self.assertNotIn(old, SuppressionStore(BOOT, self.state_path).load())

    async def test_boot_change_releases_persisted_suppression(self):
        SuppressionStore('22222222-2222-2222-2222-222222222222', self.state_path).save({self.identities[42]})
        await self.detect()
        self.assertTrue(self.daemon._session_active)

    async def test_session_without_binding_suppresses_all_current_instances(self):
        d = self.daemon
        d.cfg.recording.session_start_on_game_detect = False
        await d._start_session()
        self.assertIsNone(d._session_process_identity)
        d.cfg.recording.session_start_on_game_detect = True
        self.scan.return_value = WindowScan([self.win, self.other], None)
        await d._stop_session()
        self.assertEqual(SuppressionStore(BOOT, self.state_path).load(), set(self.identities.values()))

    async def test_retry_delays_three_attempt_limit_and_single_failure_notice(self):
        d = self.daemon
        clock = mock.Mock(wraps=time)
        now = [0.0]
        clock.monotonic.side_effect = lambda: now[0]
        with mock.patch('vice.main.time', clock), \
             mock.patch.object(d.recorder, 'start_session', new=mock.AsyncMock(return_value=None)) as start:
            await self.detect()
            self.assertEqual(start.await_count, 1)
            now[0] = 4.9
            await d._auto_start_tick()
            self.assertEqual(start.await_count, 1)
            now[0] = 5.0
            await d._auto_start_tick()
            self.assertEqual(start.await_count, 2)
            now[0] = 19.9
            await d._auto_start_tick()
            self.assertEqual(start.await_count, 2)
            now[0] = 20.0
            await d._auto_start_tick()
            self.assertEqual(start.await_count, 3)
            now[0] = 100
            await d._auto_start_tick()
            self.assertEqual(start.await_count, 3)
        events = [call.args[0] for call in d.share.broadcast.call_args_list]
        self.assertEqual(sum(event['type'] == 'session_auto_start_failed' for event in events), 1)

    async def test_storage_read_and_write_errors_pause_without_preventing_manual_stop(self):
        d = self.daemon
        with mock.patch('vice.main.read_host_boot_id', side_effect=AutoStartStateError('no boot ID')):
            await self.detect()
        self.assertEqual(d._auto_start_error, 'state')
        self.assertFalse(d._session_active)
        # A fresh daemon starts normally, then its durable write fails.
        del d._auto_start_task
        d._init_auto_start()
        await self.detect()
        with mock.patch.object(d._auto_start_store, 'save', side_effect=AutoStartStateError('read only')):
            await d._stop_session()
        self.assertFalse(d._session_active)
        self.assertEqual(d._auto_start_error, 'state')
        self.assertIn(self.identities[42], d._auto_start_suppressed)
        await self.detect()
        self.assertFalse(d._session_active)

    async def test_unsupported_and_scan_failure_show_status_and_recover(self):
        d = self.daemon
        self.auto_start_scan_supported.return_value = False
        await self.detect()
        self.assertEqual(d._auto_start_error, 'unsupported')
        self.auto_start_scan_supported.return_value = True
        self.scan.return_value = None
        await self.detect()
        self.assertEqual(d._auto_start_error, 'detection')
        self.scan.return_value = WindowScan([self.win], None)
        await self.detect()
        self.assertIsNone(d._auto_start_error)
        self.assertTrue(d._session_active)

    async def test_not_ready_waits_without_spending_attempts(self):
        d = self.daemon
        d._ready = False
        await self.detect()
        self.scan.assert_not_called()
        self.assertEqual(d._auto_start_attempts, {})
        d._ready = True
        await self.detect()
        self.assertTrue(d._session_active)

    async def test_simultaneous_manual_and_automatic_start_only_capture_once(self):
        d = self.daemon
        await d._auto_start_tick()
        with mock.patch.object(d.recorder, 'start_session', wraps=d.recorder.start_session) as start:
            await asyncio.gather(d._auto_start_tick(), d._start_session())
        start.assert_awaited_once()
        self.assertTrue(d._session_active)

    async def test_manual_stop_prevents_previously_selected_target_from_starting(self):
        d = self.daemon
        await self.detect()
        target = AutoStartTarget('Counter-Strike 2', self.win, self.identities[42])
        await d._stop_session()
        await d._change_session(True, automatic_target=target)
        self.assertFalse(d._session_active)

    async def test_disable_during_liveness_probe_prevents_start(self):
        d = self.daemon
        await d._auto_start_tick()
        def probe(_):
            d.cfg.recording.session_start_on_game_detect = False
            return 'running'
        self.probe_game_process.side_effect = probe
        await d._auto_start_tick()
        self.assertFalse(d._session_active)

    async def test_disable_waits_for_inflight_start_without_cancelling_capture(self):
        d = self.daemon
        entered, release = asyncio.Event(), asyncio.Event()
        original = d.recorder.start_session
        async def start(**kwargs):
            entered.set()
            await release.wait()
            return await original(**kwargs)
        with mock.patch('vice.main.SESSION_AUTO_START_INTERVAL', .001), \
             mock.patch.object(d.recorder, 'start_session', side_effect=start):
            await d._sync_auto_start_task()
            await asyncio.wait_for(entered.wait(), 3)
            d.cfg.recording.session_start_on_game_detect = False
            disabling = asyncio.create_task(d._sync_auto_start_task())
            await asyncio.sleep(0)
            self.assertFalse(disabling.done())
            release.set()
            await asyncio.wait_for(disabling, 3)
        self.assertTrue(d._session_active)
        self.assertIsNone(d._auto_start_task)

    async def test_all_four_start_stop_setting_combinations(self):
        d = self.daemon
        for auto_start, auto_stop in ((False, False), (False, True), (True, False), (True, True)):
            with self.subTest(auto_start=auto_start, auto_stop=auto_stop):
                del d._auto_start_task
                d._init_auto_start()
                d.cfg.recording.session_start_on_game_detect = auto_start
                d.cfg.recording.session_stop_on_game_exit = auto_stop
                if auto_start:
                    await self.detect()
                else:
                    await d._start_session()
                self.assertTrue(d._session_active)
                self.assertEqual(getattr(d, '_session_monitor_task', None) is not None, auto_stop)
                await d._change_session(False, stop_reason='shutdown')

    async def test_automatic_finalization_preserves_original_tag_and_highlights(self):
        d = self.daemon
        d.cfg.output.tag_clips_with_game = False
        d.cfg.discord.enabled = False
        await self.detect()
        path = d._session_path
        self.assertEqual(path.name, 'Vice_Session_1.mp4')
        d._session_highlights = [{'time': 2.5, 'label': 'Moment', 'color': '#fff'}]
        self.scan.return_value = WindowScan([self.other], 'other')
        await d._change_session(False, stop_reason='automatic')
        self.assertEqual(d.share.playlists.game_for(path.stem), 'Counter-Strike 2')
        saved = json.loads((self.root / 'highlights' / f'{path.stem}.json').read_text())
        self.assertEqual(saved[0]['time'], 2.5)
        # Capture ending cannot cause another automatic session for the same instance.
        self.scan.return_value = WindowScan([self.win], None)
        await self.detect()
        self.assertFalse(d._session_active)

    async def test_inflight_scan_cannot_clear_storage_failure_pause(self):
        import threading
        d = self.daemon
        entered, release = threading.Event(), threading.Event()
        target = AutoStartTarget('Counter-Strike 2', self.win, self.identities[42])
        def scan():
            entered.set()
            release.wait(3)
            return [target], None
        d._auto_start_pending = target.identity
        with mock.patch.object(d, '_scan_auto_start_targets', side_effect=scan):
            scanning = asyncio.create_task(d._auto_start_tick())
            self.assertTrue(await asyncio.to_thread(entered.wait, 3))
            # A manual session started and stopped while detection was pending.
            d._session_active = True
            d._session_process_identity = self.identities[42]
            with mock.patch.object(d._auto_start_store, 'save', side_effect=AutoStartStateError('disk failed')), \
                 mock.patch.object(d.recorder, 'stop_session', new=mock.AsyncMock(return_value=None)):
                await d._stop_session()
            release.set()
            await asyncio.wait_for(scanning, 3)
        self.assertFalse(d._session_active)
        self.assertEqual(d._auto_start_error, 'state')
        self.assertTrue(d._auto_start_state_blocked)
        await d._set_auto_start_error('unsupported')
        self.assertEqual(d._auto_start_error, 'state')
        d.cfg.recording.session_start_on_game_detect = False
        await d._sync_auto_start_task()
        d.cfg.recording.session_start_on_game_detect = True
        await self.detect()
        self.assertFalse(d._session_active)
        self.assertEqual(d._auto_start_error, 'state')

    async def test_scan_after_manual_stop_preserves_new_suppression_during_cleanup(self):
        d = self.daemon
        await self.detect()
        await d._stop_session()
        self.probe_game_process.return_value = 'unknown'
        await d._auto_start_tick()
        self.assertEqual(SuppressionStore(BOOT, self.state_path).load(), {self.identities[42]})

    async def test_shutdown_finishes_inflight_start_then_saves_session(self):
        d = self.daemon
        for field in ('_watchdog_task', '_upgrade_task', '_update_task',
                      '_follow_mouse_task', '_window_capture_task', '_discord_task'):
            setattr(d, field, None)
        d.key_overlay = mock.Mock(stop=mock.AsyncMock())
        d.hotkeys = mock.Mock(stop=mock.AsyncMock())
        d.share.stop = mock.AsyncMock()
        d._clear_discord_presence = mock.AsyncMock()
        entered, release = asyncio.Event(), asyncio.Event()
        original = d.recorder.start_session
        async def start(**kwargs):
            entered.set()
            await release.wait()
            return await original(**kwargs)
        async def recorder_stop():
            self.assertFalse(d._session_active)
            self.assertIsNone(d._auto_start_task)
        with mock.patch('vice.main.SESSION_AUTO_START_INTERVAL', .001), \
             mock.patch.object(d.recorder, 'start_session', side_effect=start), \
             mock.patch.object(d.recorder, 'stop', side_effect=recorder_stop), \
             mock.patch.object(d.share, 'add_clip', wraps=d.share.add_clip) as add, \
             mock.patch('vice.main.PID_FILE', self.root / 'pid'), \
             mock.patch('vice.main.SOCKET_FILE', self.root / 'socket'):
            await d._sync_auto_start_task()
            await asyncio.wait_for(entered.wait(), 3)
            shutdown = asyncio.create_task(d._shutdown(mock.Mock()))
            await asyncio.sleep(0)
            self.assertFalse(shutdown.done())
            release.set()
            await asyncio.wait_for(shutdown, 3)
        add.assert_called_once()
        self.assertEqual(d._auto_start_suppressed, set())
        self.assertTrue(d._auto_start_shutdown)

    async def test_enabling_auto_start_does_not_change_capture_signature(self):
        d = self.daemon
        d.cfg.recording.session_start_on_game_detect = False
        signature = d._recording_signature()
        d.cfg.recording.session_start_on_game_detect = True
        self.assertEqual(d._recording_signature(), signature)

    async def test_concurrent_store_load_cannot_unpause_failed_state(self):
        import threading
        d = self.daemon
        entered, release = threading.Event(), threading.Event()
        def load():
            entered.set()
            release.wait(3)
            return set()
        with mock.patch.object(SuppressionStore, 'load', side_effect=load):
            loading = asyncio.create_task(d._ensure_auto_start_store())
            self.assertTrue(await asyncio.to_thread(entered.wait, 3))
            await d._set_auto_start_error('state')
            release.set()
            self.assertFalse(await asyncio.wait_for(loading, 3))
        self.assertEqual(d._auto_start_error, 'state')
        self.assertTrue(d._auto_start_state_blocked)
