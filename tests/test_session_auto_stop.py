"""Process-bound session lifetime and finalization races."""
import asyncio
import json
import time
import unittest
from unittest import mock

from tests import test_session_game_tagging as tagging
from vice.main import AutoStartTarget
from vice.session_process import ProcessIdentity


class SessionAutoStopTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tagging.SessionGameTaggingTests.setUp(self)
        self.daemon.recorder._session_proc = None
        # Capture children made by the shared fixture are alive until stopped.
        self.capture = self.daemon.recorder
        self.alive = mock.patch.object(self.capture, 'session_capture_exited', return_value=False)
        self.capture_exited = self.alive.start()
        self.addCleanup(self.alive.stop)
        self.identity = ProcessIdentity(42, 100)
        self.window = {'process': 'cs2', 'class': 'cs2', 'pid': 42}
        self.target_patch = mock.patch.object(self.daemon, '_detect_game_target',
                                              return_value=('Counter-Strike 2', self.window))
        self.target = self.target_patch.start()
        self.addCleanup(self.target_patch.stop)
        self.bind_patch = mock.patch('vice.main.bind_game_process', return_value=self.identity)
        self.bind = self.bind_patch.start()
        self.addCleanup(self.bind_patch.stop)
        self.poll_patch = mock.patch('vice.main.SESSION_GAME_POLL_INTERVAL', 0.001)
        self.poll_patch.start()
        self.addCleanup(self.poll_patch.stop)
        self.addAsyncCleanup(self.daemon._stop_session)

    async def start_armed(self):
        self.daemon.cfg.recording.session_stop_on_game_exit = True
        await self.daemon._start_session()
        await asyncio.sleep(0)
        self.assertTrue(self.daemon._session_active)
        self.assertIsNotNone(self.daemon._session_monitor_task)
        return self.daemon._session_monitor_task

    async def _check_unfocused_background_tag(self, focus, windows, *, auto_start_only=False):
        d = self.daemon
        d.cfg.recording.session_stop_on_game_exit = not auto_start_only
        d.cfg.recording.session_start_on_game_detect = auto_start_only
        d.cfg.output.tag_clips_with_game = True
        names = {'cs2': 'Counter-Strike 2', 'dota2': 'Dota 2'}
        self.target.side_effect = lambda **kwargs: type(d)._detect_game_target(d, **kwargs)
        with mock.patch('vice.active_window.get_active_window', return_value=focus), \
             mock.patch('vice.active_window.list_candidate_windows', return_value=windows), \
             mock.patch.object(d, '_match_game', side_effect=lambda win: names.get(win['class'])), \
             mock.patch.object(d, '_scan_visible_for_game', side_effect=lambda: type(d)._scan_visible_for_game(d)), \
             mock.patch.object(d.recorder, 'start_session', wraps=d.recorder.start_session) as start, \
             mock.patch.object(d.share, 'add_clip', wraps=d.share.add_clip) as add, \
             mock.patch.object(d, '_suppress_manually_stopped_session', new=mock.AsyncMock()):
            await d._start_session()
            await asyncio.sleep(0)
            path = d._session_path
            self.assertTrue(d._session_active)
            self.assertEqual(d._session_game, 'Counter-Strike 2')
            self.assertIsNone(d._session_process_identity)
            self.assertIsNone(getattr(d, '_session_monitor_task', None))
            self.bind.assert_not_called()
            events = [call.args[0] for call in d.share.broadcast.call_args_list]
            event = next(event for event in events if event['type'] == 'session_start')
            self.assertEqual(event['auto_stop'], 'off' if auto_start_only else 'unavailable')
            self.assertEqual(event['origin'], 'manual')
            start.assert_awaited_once_with(tag='Counter-Strike 2')
            d._session_highlights = [{'time': 1.25, 'label': 'Moment', 'color': '#fff'}]
            await d._stop_session()
            add.assert_called_once_with(path, game='Counter-Strike 2', session=True)
        highlights = json.loads((self.root / 'highlights' / f'{path.stem}.json').read_text())
        self.assertEqual(highlights[0]['time'], 1.25)

    async def test_manual_unrecognized_focus_does_not_bind_background(self):
        await self._check_unfocused_background_tag(
            {'process': 'browser', 'class': 'firefox', 'pid': 99}, [self.window])

    async def test_manual_absent_focus_with_two_games_avoids_ambiguous_binding(self):
        await self._check_unfocused_background_tag(
            None, [self.window, {'process': 'dota2', 'class': 'dota2', 'pid': 88}])

    async def test_manual_auto_start_setting_does_not_bind_background(self):
        await self._check_unfocused_background_tag(
            {'process': 'browser', 'class': 'firefox', 'pid': 99},
            [self.window], auto_start_only=True)

    async def test_recognized_focus_binds_even_with_background_game(self):
        d = self.daemon
        d.cfg.recording.session_stop_on_game_exit = True
        self.target.side_effect = lambda **kwargs: type(d)._detect_game_target(d, **kwargs)
        with mock.patch('vice.active_window.get_active_window', return_value=self.window), \
             mock.patch('vice.active_window.list_candidate_windows', return_value=[
                 {'process': 'dota2', 'class': 'dota2', 'pid': 88}]) as scan, \
             mock.patch.object(d, '_match_game', return_value='Counter-Strike 2'), \
             mock.patch('vice.main.probe_game_process', return_value='running'):
            task = await self.start_armed()
            self.assertEqual(d._session_game, 'Counter-Strike 2')
            self.assertEqual(d._session_process_identity, self.identity)
            self.bind.assert_called_once_with(self.window)
            scan.assert_not_called()
            await d._stop_session()
        self.assertTrue(task.done())

    async def test_automatic_background_target_keeps_selected_identity(self):
        d = self.daemon
        d._ready = True
        d.cfg.recording.session_stop_on_game_exit = True
        d.cfg.recording.session_start_on_game_detect = True
        d.cfg.output.tag_clips_with_game = True
        selected = AutoStartTarget('Counter-Strike 2', self.window, self.identity)
        with mock.patch('vice.main.probe_game_process', return_value='running'), \
             mock.patch('vice.active_window.get_active_window') as focused, \
             mock.patch.object(d.recorder, 'start_session', wraps=d.recorder.start_session) as start, \
             mock.patch.object(d.share, 'add_clip', wraps=d.share.add_clip) as add:
            await d._change_session(True, automatic_target=selected)
            self.assertTrue(d._session_active)
            self.assertEqual(d._session_game, selected.game)
            self.assertEqual(d._session_process_identity, selected.identity)
            self.assertIsNotNone(d._session_monitor_task)
            start.assert_awaited_once_with(tag=selected.game)
            self.target.assert_not_called()
            self.bind.assert_not_called()
            focused.assert_not_called()
            path = d._session_path
            d._session_highlights = [{'time': 2.5, 'label': 'Selected game', 'color': '#fff'}]
            await d._change_session(False, stop_reason='automatic')
            add.assert_called_once_with(path, game=selected.game, session=True)
        highlights = json.loads((self.root / 'highlights' / f'{path.stem}.json').read_text())
        self.assertEqual(highlights[0]['time'], 2.5)

    async def test_confirmed_exit_saves_once_with_original_identity_and_highlights(self):
        d = self.daemon
        ticks = []
        def exited(identity):
            self.assertEqual(identity, self.identity)
            ticks.append(len(ticks) + 1)
            return 'exited'
        clock = mock.Mock(wraps=time)
        clock.monotonic.side_effect = lambda: ticks[-1]
        with mock.patch('vice.main.probe_game_process', side_effect=exited), \
             mock.patch('vice.main.time', clock), \
             mock.patch.object(d.share, 'add_clip', wraps=d.share.add_clip) as add:
            task = await self.start_armed()
            path = d._session_path
            d._session_highlights = [{'time': 1.25, 'label': 'Moment', 'color': '#fff'}]
            self.target.return_value = ('Other game', {'pid': 99})
            d.cfg.recording.session_stop_on_game_exit = False
            await asyncio.wait_for(task, 3)
            add.assert_called_once_with(path, game='Counter-Strike 2', session=True)
        self.assertEqual(len(ticks), 6)
        highlights = json.loads((self.root / 'highlights' / f'{path.stem}.json').read_text())
        self.assertEqual(highlights[0]['time'], 1.25)
        self.assertFalse(d._session_active)
        self.assertIsNone(d._session_token)
        self.assertIsNone(d._session_monitor_task)
        self.target.assert_called_once()

    async def test_unknown_and_running_reset_continuous_exit_delay(self):
        # First two apparent exits, then an unknown read and a running sample.
        states = ['exited', 'exited', 'unknown', 'exited', 'running'] + ['exited'] * 6
        seen = []
        def probe(_):
            state = states[len(seen)]
            seen.append(state)
            return state
        clock = mock.Mock(wraps=time)
        clock.monotonic.side_effect = lambda: len(seen)
        with mock.patch('vice.main.probe_game_process', side_effect=probe), \
             mock.patch('vice.main.time', clock):
            task = await self.start_armed()
            await asyncio.wait_for(task, 3)
        self.assertEqual(seen, states)
        self.assertFalse(self.daemon._session_active)

    async def test_focus_changes_do_not_rebind_or_stop_running_process(self):
        sampled = asyncio.Event()
        loop = asyncio.get_running_loop()
        seen = []
        def probe(identity):
            seen.append(identity)
            if len(seen) >= 4:
                loop.call_soon_threadsafe(sampled.set)
            return 'running'
        with mock.patch('vice.main.probe_game_process', side_effect=probe):
            task = await self.start_armed()
            # Unfocused/minimized/no current game, followed by another game.
            self.focus.return_value = None
            self.target.return_value = ('Other game', {'pid': 99})
            await asyncio.wait_for(sampled.wait(), 3)
            self.assertTrue(self.daemon._session_active)
            await self.daemon._stop_session()
        self.assertTrue(task.done())
        self.assertTrue(all(identity == self.identity for identity in seen))
        self.target.assert_called_once()

    async def test_capture_exit_finalizes_without_game_exit_delay(self):
        with mock.patch('vice.main.probe_game_process') as probe:
            self.capture_exited.return_value = True
            task = await self.start_armed()
            await asyncio.wait_for(task, 3)
        probe.assert_not_called()
        self.assertFalse(self.daemon._session_active)

    async def test_unavailable_identity_and_missing_game_record_with_notice(self):
        for target in (None, ('Counter-Strike 2', self.window)):
            with self.subTest(target=target):
                self.target.return_value = target
                self.bind.return_value = None
                self.daemon.cfg.recording.session_stop_on_game_exit = True
                self.daemon.share.broadcast.reset_mock()
                await self.daemon._start_session()
                await asyncio.sleep(0)
                self.assertTrue(self.daemon._session_active)
                self.assertIsNone(getattr(self.daemon, '_session_monitor_task', None))
                events = [call.args[0] for call in self.daemon.share.broadcast.call_args_list]
                start = next(event for event in events if event['type'] == 'session_start')
                self.assertEqual(start['auto_stop'], 'unavailable')
                await self.daemon._stop_session()

    async def test_default_off_and_enabling_mid_session_only_affects_next_session(self):
        d = self.daemon
        # Off sessions use the compatibility label-only entry point.
        self.assertFalse(d.cfg.recording.session_stop_on_game_exit)
        signature = d._recording_signature()
        await d._start_session()
        d.cfg.recording.session_stop_on_game_exit = True
        self.assertEqual(signature, d._recording_signature())
        self.assertIsNone(getattr(d, '_session_monitor_task', None))
        self.bind.assert_not_called()
        await d._stop_session()
        with mock.patch('vice.main.probe_game_process', return_value='running'):
            task = await self.start_armed()
            await d._stop_session()
        self.assertTrue(task.done())

    async def test_stale_token_cannot_stop_new_session(self):
        d = self.daemon
        with mock.patch('vice.main.probe_game_process', return_value='running'):
            await self.start_armed()
            stale = d._session_token
            await d._stop_session()
            await self.start_armed()
            current = d._session_token
            await d._change_session(False, expected_token=stale)
            self.assertTrue(d._session_active)
            self.assertIs(d._session_token, current)
            await d._stop_session()

    async def test_manual_stop_and_queued_auto_stop_finalize_once(self):
        d = self.daemon
        with mock.patch('vice.main.probe_game_process', return_value='running'), \
             mock.patch.object(d.share, 'add_clip', wraps=d.share.add_clip) as add:
            await self.start_armed()
            token = d._session_token
            await asyncio.gather(d._stop_session(), d._change_session(False, expected_token=token))
        add.assert_called_once()
        self.assertFalse(d._session_active)
        self.assertIsNone(d._session_monitor_task)

    async def test_failed_start_never_starts_monitor(self):
        self.daemon.cfg.recording.session_stop_on_game_exit = True
        with mock.patch.object(self.capture, 'start_session', new=mock.AsyncMock(return_value=None)):
            await self.daemon._start_session()
        self.assertFalse(self.daemon._session_active)
        self.assertIsNone(getattr(self.daemon, '_session_monitor_task', None))
        self.assertIsNone(self.daemon._session_token)

    async def test_probe_exception_keeps_session_active(self):
        sampled = asyncio.Event()
        loop = asyncio.get_running_loop()
        def probe(_):
            loop.call_soon_threadsafe(sampled.set)
            raise OSError('probe unavailable')
        with mock.patch('vice.main.probe_game_process', side_effect=probe), \
             self.assertLogs('vice', level='ERROR'):
            task = await self.start_armed()
            await asyncio.wait_for(sampled.wait(), 3)
            self.assertTrue(self.daemon._session_active)
            await self.daemon._stop_session()
        self.assertTrue(task.done())

    async def test_shutdown_cancels_monitor_and_finalizes_before_recorder_stop(self):
        d = self.daemon
        for field in ('_watchdog_task', '_upgrade_task', '_update_task',
                      '_follow_mouse_task', '_window_capture_task', '_discord_task'):
            setattr(d, field, None)
        d.key_overlay = mock.Mock(stop=mock.AsyncMock())
        d.hotkeys = mock.Mock(stop=mock.AsyncMock())
        d.share.stop = mock.AsyncMock()
        d._clear_discord_presence = mock.AsyncMock()
        order = []
        original_stop = d.recorder.stop_session
        async def stop_session():
            order.append('session')
            return await original_stop()
        async def stop_recorder():
            self.assertFalse(d._session_active)
            self.assertIsNone(d._session_monitor_task)
            order.append('recorder')
        with mock.patch('vice.main.probe_game_process', return_value='running'), \
             mock.patch.object(d.recorder, 'stop_session', side_effect=stop_session), \
             mock.patch.object(d.recorder, 'stop', side_effect=stop_recorder), \
             mock.patch.object(d.share, 'add_clip', wraps=d.share.add_clip) as add, \
             mock.patch('vice.main.PID_FILE', self.root / 'pid'), \
             mock.patch('vice.main.SOCKET_FILE', self.root / 'socket'):
            task = await self.start_armed()
            await d._shutdown(mock.Mock())
        self.assertEqual(order, ['session', 'recorder'])
        self.assertTrue(task.done())
        add.assert_called_once()

    async def test_manual_stop_during_inflight_probe_does_not_stop_next_session(self):
        import threading
        entered, release = threading.Event(), threading.Event()
        def probe(_):
            entered.set()
            release.wait(3)
            return 'exited'
        d = self.daemon
        with mock.patch('vice.main.probe_game_process', side_effect=probe):
            old = await self.start_armed()
            self.assertTrue(await asyncio.to_thread(entered.wait, 3))
            await asyncio.wait_for(d._stop_session(), 2)
            self.assertTrue(old.done())
            d.cfg.recording.session_stop_on_game_exit = False
            await d._start_session()
            token = d._session_token
            release.set()
            await asyncio.sleep(0.01)
            self.assertTrue(d._session_active)
            self.assertIs(d._session_token, token)
            await d._stop_session()
        release.set()

    async def test_manual_stop_waits_for_automatic_finalization_once(self):
        d = self.daemon
        entered, release = asyncio.Event(), asyncio.Event()
        async def terminate(*args, **kwargs):
            entered.set()
            await release.wait()
        self.capture_exited.return_value = True
        with mock.patch('vice.recorder._terminate_group', side_effect=terminate), \
             mock.patch.object(d.share, 'add_clip', wraps=d.share.add_clip) as add:
            monitor = await self.start_armed()
            await asyncio.wait_for(entered.wait(), 3)
            manual = asyncio.create_task(d._stop_session())
            await asyncio.sleep(0)
            self.assertFalse(manual.done())
            release.set()
            await asyncio.wait_for(asyncio.gather(monitor, manual), 3)
        add.assert_called_once()
        self.assertFalse(d._session_active)
        self.assertIsNone(d._session_monitor_task)
