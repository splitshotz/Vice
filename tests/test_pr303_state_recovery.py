"""Recovery behavior for PR #303 automatic session recording.

Drive the existing session-control/tick seams with the established capture
fixture. No real recorder, game process, or user suppression file is used.
"""

import json
import os
import asyncio
import threading
import unittest
from pathlib import Path
from unittest import mock

from tests import test_auto_start_sessions as sessions
from vice.active_window import WindowScan
from vice.session_auto_start import AutoStartStateError, SuppressionStore

BOOT = sessions.BOOT


class RecoveryContracts(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Import the fixture module, not its TestCase, to avoid collecting its
        # existing tests a second time in this module.
        sessions.AutoStartSessionTests.setUp(self)
        self.state_path = self.root / 'session_auto_start.json'

    cleanup_session = sessions.AutoStartSessionTests.cleanup_session
    detect = sessions.AutoStartSessionTests.detect

    async def restart_controller(self):
        """Recreate the controller's volatile state, retaining its state file."""
        await self.daemon._stop_auto_start_task()
        del self.daemon._auto_start_task
        self.daemon._init_auto_start()

    async def toggle_setting(self):
        d = self.daemon
        d.cfg.recording.session_start_on_game_detect = False
        await d._sync_auto_start_task()
        self.assertIsNone(d._auto_start_error)
        self.assertFalse(d._auto_start_storage_failed)
        d.cfg.recording.session_start_on_game_detect = True
        # Exercise settings synchronization without a concurrently polling
        # loop. All detection ticks are explicitly driven by this test.
        with mock.patch.object(d, '_auto_start_loop', new=mock.AsyncMock()):
            await d._sync_auto_start_task()
            await d._auto_start_task
        await d._stop_auto_start_task()

    async def stop_unbound_session_during_scan_failure(self):
        d = self.daemon
        d.cfg.recording.session_start_on_game_detect = False
        await d._start_session()
        self.assertTrue(d._session_active)
        self.assertIsNone(d._session_process_identity)
        d.cfg.recording.session_start_on_game_detect = True
        self.scan.return_value = None
        await d._stop_session()
        self.assertFalse(d._session_active)
        self.assertEqual(SuppressionStore(BOOT, self.state_path).load(), set())
        self.assertFalse(self.state_path.exists(), 'failed identification must not save pending intent')
        self.assertFalse(hasattr(d, '_auto_start_pending_suppression'))
        self.assertEqual(d._auto_start_error, 'detection')

    async def assert_game_starts_without_suppression(self, window=None):
        window = self.win if window is None else window
        self.scan.return_value = WindowScan([window], window['window_id'])
        await self.detect()
        self.assertTrue(self.daemon._session_active,
                        'failed manual-stop identification must not suppress later observations')
        self.assertEqual(self.daemon._session_process_identity, self.identities[window['pid']])
        self.assertEqual(SuppressionStore(BOOT, self.state_path).load(), set())

    async def test_failed_manual_stop_scan_suppresses_nothing_and_recovers(self):
        await self.stop_unbound_session_during_scan_failure()
        await self.assert_game_starts_without_suppression()

    async def test_manual_stop_scan_exception_does_not_suppress_unrelated_game(self):
        self.scan.side_effect = RuntimeError('temporary scanner failure')
        with self.assertLogs('vice', level='WARNING'):
            await self.stop_unbound_session_during_scan_failure()
        self.scan.side_effect = None
        await self.assert_game_starts_without_suppression(self.other)

    async def test_unbound_failed_stop_then_empty_scan_allows_returning_game_to_restart(self):
        await self.stop_unbound_session_during_scan_failure()
        self.scan.return_value = WindowScan([], None)
        await self.daemon._auto_start_tick()
        self.assertEqual(SuppressionStore(BOOT, self.state_path).load_snapshot(),
                         (set(), 1))
        self.assertFalse(self.daemon._session_active)
        self.scan.return_value = WindowScan([self.win], 'game')
        await self.daemon._auto_start_tick()
        self.assertFalse(self.daemon._session_active)
        await self.daemon._auto_start_tick()
        self.assertTrue(self.daemon._session_active)
        self.assertEqual(self.daemon._session_process_identity, self.identities[42])

    async def test_bound_manual_stop_remains_suppressed_across_empty_scan_and_return(self):
        await self.detect()
        self.assertEqual(self.daemon._session_process_identity, self.identities[42])
        self.scan.return_value = None
        self.scan.reset_mock()
        await self.daemon._stop_session()
        self.scan.assert_not_called()  # A bound stop already knows its identity.
        self.scan.return_value = WindowScan([], None)
        await self.daemon._auto_start_tick()
        self.scan.return_value = WindowScan([self.win], 'game')
        await self.detect()
        self.assertFalse(self.daemon._session_active)
        self.assertEqual(SuppressionStore(BOOT, self.state_path).load_snapshot(),
                         ({self.identities[42]}, 1))

    async def test_setting_toggle_recovers_failed_stop_without_suppressing_unrelated_game(self):
        await self.stop_unbound_session_during_scan_failure()
        await self.toggle_setting()
        await self.assert_game_starts_without_suppression(self.other)

    async def test_successful_save_clears_pause_and_keeps_known_manual_stop(self):
        await self.stop_unbound_session_during_scan_failure()
        d = self.daemon
        known = {self.identities[42]}
        with mock.patch.object(SuppressionStore, 'save', side_effect=AutoStartStateError('temporary write failure')):
            self.assertFalse(await d._save_auto_start_suppression(known))
        self.assertEqual(d._auto_start_error, 'state')
        # A real successful save releases the storage block without a restart.
        self.assertTrue(await d._save_auto_start_suppression(known))
        self.assertFalse(d._auto_start_storage_failed)
        self.assertIsNone(d._auto_start_error)
        self.assertEqual(SuppressionStore(BOOT, self.state_path).load(), known)
        self.scan.return_value = WindowScan([self.win], 'game')
        await self.detect()
        self.assertFalse(d._session_active)
        self.scan.return_value = WindowScan([self.other], 'other')
        await self.detect()
        self.assertTrue(d._session_active)
        self.assertEqual(d._session_process_identity, self.identities[43])

    async def assert_bad_state_recovers(self, payload):
        self.state_path.write_text(payload, encoding='utf-8')
        observed = []
        for _ in range(2):
            await self.restart_controller()
            await self.detect()
            observed.append(self.daemon._session_active)
            await self.daemon._change_session(False, stop_reason='shutdown')
        # No test-side rewrite: both starts must recover themselves. Capture
        # both observations before asserting to demonstrate restart behavior.
        self.assertEqual(observed, [True, True],
                         'malformed state must not poison every controller restart')
        self.assertEqual(SuppressionStore(BOOT, self.state_path).load(), set())
        self.assertEqual(json.loads(self.state_path.read_text())['version'], 1)

    async def test_malformed_state_is_repaired_across_controller_restarts(self):
        await self.assert_bad_state_recovers('{not-json')

    async def test_unsupported_state_version_is_repaired_across_controller_restarts(self):
        await self.assert_bad_state_recovers(json.dumps({
            'version': 999, 'boot_id': BOOT, 'suppressed': [],
        }))

    async def test_temporary_read_failure_recovers_and_loads_known_manual_stop(self):
        d = self.daemon
        known = {self.identities[42]}
        SuppressionStore(BOOT, self.state_path).save(known)
        original_read = Path.read_text

        def read(path, *args, **kwargs):
            if path == self.state_path:
                raise PermissionError('temporary read denial')
            return original_read(path, *args, **kwargs)

        with mock.patch.object(Path, 'read_text', new=read):
            await self.detect()
            self.assertFalse(d._session_active)
            self.assertEqual(d._auto_start_error, 'state')
        # Read permission is restored; the same game remains stopped, while
        # another game must be allowed after the successful reload.
        await self.detect()
        self.assertFalse(d._session_active)
        self.scan.return_value = WindowScan([self.other], 'other')
        await self.detect()
        self.assertTrue(d._session_active)
        self.assertEqual(d._session_process_identity, self.identities[43])
        self.assertEqual(d._auto_start_suppressed, known)

    async def test_temporary_write_failure_recovers_and_persists_known_manual_stop(self):
        d = self.daemon
        await self.detect()
        known = {self.identities[42]}
        # Fail the filesystem boundary, exercising the real store's atomic
        # write/error handling instead of substituting a fake store.
        original_replace = os.replace

        def replace(source, destination, *args, **kwargs):
            if Path(destination) == self.state_path:
                raise PermissionError('temporary write denial')
            return original_replace(source, destination, *args, **kwargs)

        with mock.patch('vice.session_auto_start.os.replace',
                        side_effect=replace):
            await d._stop_session()
            self.assertFalse(d._session_active)
            self.assertEqual(d._auto_start_error, 'state')
            self.assertEqual(d._auto_start_suppressed, known)
            await self.toggle_setting()
            self.scan.return_value = WindowScan([self.other], 'other')
            await self.detect()
            self.assertFalse(d._session_active,
                             'storage failure must pause automatic starts')

        self.scan.return_value = WindowScan([self.win], 'game')
        await self.detect()
        self.assertFalse(d._session_active,
                         'restored storage must not restart the stopped game')
        self.scan.return_value = WindowScan([self.other], 'other')
        await self.detect()
        self.assertTrue(d._session_active,
                        'restored storage must allow a different game')
        self.assertEqual(d._session_process_identity, self.identities[43])
        self.assertEqual(SuppressionStore(BOOT, self.state_path).load(), known)
        await d._change_session(False, stop_reason='shutdown')
        await self.restart_controller()
        self.scan.return_value = WindowScan([self.win], 'game')
        await self.detect()
        self.assertFalse(d._session_active)
        self.assertEqual(d._auto_start_suppressed, known)
        # This last start makes the post-restart safety check non-vacuous.
        self.scan.return_value = WindowScan([self.other], 'other')
        await self.detect()
        self.assertTrue(d._session_active)

    async def test_ordinary_poll_scan_failure_recovers_without_manual_stop(self):
        self.scan.return_value = None
        await self.detect()
        self.assertFalse(self.daemon._session_active)
        self.assertEqual(self.daemon._auto_start_error, 'detection')
        self.scan.return_value = WindowScan([self.win], 'game')
        await self.detect()
        self.assertTrue(self.daemon._session_active)
        self.assertIsNone(self.daemon._auto_start_error)

    async def test_stop_concurrent_with_initial_load_retains_legacy_identities_without_intent(self):
        d = self.daemon
        known = {self.identities[42]}
        self.state_path.write_text(json.dumps({
            'version': 2, 'boot_id': BOOT, 'pending': True,
            'suppressed': [{'pid': 42, 'start_time': 100}],
        }))
        self.focus.return_value = self.other
        await d._start_session()
        self.assertEqual(d._session_process_identity, self.identities[43])
        entered, release = threading.Event(), threading.Event()
        original_load = SuppressionStore.load_snapshot

        def load(store):
            snapshot = original_load(store)
            entered.set()
            if not release.wait(3):
                raise TimeoutError('test did not release initial load')
            return snapshot

        with mock.patch.object(SuppressionStore, 'load_snapshot', new=load):
            loading = asyncio.create_task(d._ensure_auto_start_store())
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                stopping = asyncio.create_task(d._stop_session())
                await asyncio.sleep(0)
                self.assertFalse(stopping.done())
            finally:
                release.set()
            self.assertTrue(await asyncio.wait_for(loading, 3))
            await asyncio.wait_for(stopping, 3)
        identities, version = SuppressionStore(BOOT, self.state_path).load_snapshot()
        self.assertEqual(identities, known | {self.identities[43]})
        self.assertEqual(version, 1)
        self.assertNotIn('pending', json.loads(self.state_path.read_text()))

    async def test_failed_stop_has_no_deferred_intent_across_restarts(self):
        await self.stop_unbound_session_during_scan_failure()
        await self.restart_controller()
        await self.assert_game_starts_without_suppression()
        await self.daemon._change_session(False, stop_reason='shutdown')
        await self.restart_controller()
        await self.assert_game_starts_without_suppression(self.other)

    async def test_failed_stop_then_untrackable_window_does_not_defer_suppression(self):
        await self.stop_unbound_session_during_scan_failure()
        self.scan.return_value = WindowScan([dict(self.win, pid=0)], 'game')
        await self.detect()
        self.assertEqual(SuppressionStore(BOOT, self.state_path).load(), set())
        await self.assert_game_starts_without_suppression(self.other)

    async def test_partial_manual_stop_identification_suppresses_nothing(self):
        d = self.daemon
        d.cfg.recording.session_start_on_game_detect = False
        await d._start_session()
        self.assertIsNone(d._session_process_identity)
        d.cfg.recording.session_start_on_game_detect = True
        self.scan.return_value = WindowScan([self.win, dict(self.other, pid=0)], 'game')
        await d._stop_session()
        self.assertFalse(d._session_active)
        self.assertEqual(SuppressionStore(BOOT, self.state_path).load(), set())
        await self.assert_game_starts_without_suppression(self.other)

    async def test_failed_stop_keeps_prior_valid_suppression_unchanged(self):
        d = self.daemon
        known = {self.identities[42]}
        SuppressionStore(BOOT, self.state_path).save(known)
        original = self.state_path.read_bytes()
        d.cfg.recording.session_start_on_game_detect = False
        await d._start_session()
        d.cfg.recording.session_start_on_game_detect = True
        self.scan.return_value = None
        await d._stop_session()
        self.assertEqual(self.state_path.read_bytes(), original)
        self.scan.return_value = WindowScan([self.other], 'other')
        await self.detect()
        self.assertTrue(d._session_active)
        self.assertEqual(d._session_process_identity, self.identities[43])
        self.assertEqual(SuppressionStore(BOOT, self.state_path).load(), known)

    async def test_legacy_v2_discards_pending_but_retains_valid_identities_across_restart(self):
        self.state_path.write_text(json.dumps({
            'version': 2, 'boot_id': BOOT, 'pending': True,
            'suppressed': [{'pid': 42, 'start_time': 100}],
        }))
        self.scan.return_value = WindowScan([self.other], 'other')
        await self.detect()
        self.assertTrue(self.daemon._session_active)
        self.assertEqual(self.daemon._session_process_identity, self.identities[43])
        self.assertEqual(SuppressionStore(BOOT, self.state_path).load(), {self.identities[42]})
        self.assertEqual(json.loads(self.state_path.read_text())['version'], 1)
        await self.daemon._change_session(False, stop_reason='shutdown')
        await self.restart_controller()
        self.scan.return_value = WindowScan([self.win], 'game')
        await self.detect()
        self.assertFalse(self.daemon._session_active)
        self.probe_game_process.return_value = 'exited'
        self.scan.return_value = WindowScan([], None)
        await self.daemon._auto_start_tick()
        self.assertEqual(SuppressionStore(BOOT, self.state_path).load(), set())

    async def test_corrupt_backup_failure_does_not_prevent_atomic_repair(self):
        self.state_path.write_text('{not-json')
        with mock.patch.object(SuppressionStore, 'backup_corrupt', side_effect=AutoStartStateError('no backup space')), \
             self.assertLogs('vice', level='WARNING') as logs:
            await self.detect()
        self.assertTrue(self.daemon._session_active)
        self.assertEqual(SuppressionStore(BOOT, self.state_path).load(), set())
        self.assertTrue(any('Malformed' in item for item in logs.output))

    async def test_initial_read_failure_retains_known_stop_and_merges_after_recovery(self):
        d = self.daemon
        known = {self.identities[42]}
        SuppressionStore(BOOT, self.state_path).save(known)
        self.focus.return_value = self.other
        await d._start_session()
        self.assertEqual(d._session_process_identity, self.identities[43])
        with mock.patch.object(SuppressionStore, 'load_snapshot',
                               side_effect=PermissionError('temporary read denial')):
            await d._stop_session()
        self.assertFalse(d._session_active)
        self.assertTrue(d._auto_start_dirty)
        self.assertEqual(d._auto_start_suppressed, {self.identities[43]})
        self.assertEqual(SuppressionStore(BOOT, self.state_path).load(), known)
        await d._auto_start_tick()
        self.assertFalse(d._auto_start_dirty)
        self.assertIsNone(d._auto_start_error)
        self.assertEqual(SuppressionStore(BOOT, self.state_path).load(),
                         known | {self.identities[43]})

    async def test_corrupt_repair_write_failure_preserves_original_and_diagnostic_copy(self):
        d = self.daemon
        original = b'{not-json\n'
        self.state_path.write_bytes(original)
        original_replace = os.replace

        def replace(source, destination, *args, **kwargs):
            if Path(destination) == self.state_path:
                raise PermissionError('temporary repair denial')
            return original_replace(source, destination, *args, **kwargs)

        with mock.patch('vice.session_auto_start.os.replace', side_effect=replace):
            await self.detect()
            self.assertFalse(d._session_active)
            self.assertEqual(d._auto_start_error, 'state')
            self.assertEqual(self.state_path.read_bytes(), original)
        backups = list(self.state_path.parent.glob(f'.{self.state_path.name}.corrupt.*.bak'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
        await self.detect()
        self.assertTrue(d._session_active)
        self.assertEqual(SuppressionStore(BOOT, self.state_path).load(), set())

    async def test_persistence_serializes_overlapping_updates(self):
        d = self.daemon
        await d._ensure_auto_start_store()
        entered, release = threading.Event(), threading.Event()
        original_save = SuppressionStore.save
        calls = []

        def save(store, identities):
            calls.append(set(identities))
            if len(calls) == 1:
                entered.set()
                if not release.wait(3):
                    raise TimeoutError('test did not release save')
            return original_save(store, identities)

        first_identity, second_identity = self.identities.values()
        with mock.patch.object(SuppressionStore, 'save', new=save):
            first = asyncio.create_task(d._save_auto_start_suppression({first_identity}))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                second = asyncio.create_task(d._save_auto_start_suppression(
                    {first_identity, second_identity}))
                await asyncio.sleep(0)
                self.assertFalse(second.done())
                self.assertEqual(len(calls), 1)
            finally:
                release.set()
            self.assertEqual(await asyncio.gather(first, second), [True, True])
        self.assertEqual(SuppressionStore(BOOT, self.state_path).load(),
                         {first_identity, second_identity})

    async def test_stale_scan_after_failed_manual_stop_does_not_create_suppression(self):
        d = self.daemon
        await d._auto_start_tick()
        entered, release = threading.Event(), threading.Event()
        calls = []

        def scan(*, require_identities=False):
            calls.append(True)
            if len(calls) == 1:
                entered.set()
                if not release.wait(3):
                    raise TimeoutError('test did not release scan')
                # This observation predates the manual stop and must be discarded.
                return [], None
            return None  # Manual stop's scan fails.

        with mock.patch.object(d, '_scan_auto_start_targets', side_effect=scan):
            scanning = asyncio.create_task(d._auto_start_tick())
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                d.cfg.recording.session_start_on_game_detect = False
                await d._start_session()
                self.assertIsNone(d._session_process_identity)
                d.cfg.recording.session_start_on_game_detect = True
                await d._stop_session()
            finally:
                release.set()
            await asyncio.wait_for(scanning, 3)
        self.assertEqual(SuppressionStore(BOOT, self.state_path).load(), set())
        self.assertFalse(d._session_active)
        await self.assert_game_starts_without_suppression(self.other)
