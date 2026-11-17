"""Desired recovery behavior for PR #303, intentionally red until fixed.

Drive the existing session-control/tick seams with the established capture
fixture. No real recorder, game process, or user suppression file is used.
"""

import json
import os
import unittest
from pathlib import Path
from unittest import mock

from tests import test_auto_start_sessions as sessions
from vice.active_window import WindowScan
from vice.session_auto_start import SuppressionStore

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
        # A scan failure alone did not damage or make storage unavailable.
        self.assertFalse(self.state_path.exists())

    async def assert_stopped_game_stays_stopped_then_new_game_starts(self):
        d = self.daemon
        self.scan.return_value = WindowScan([self.win], 'game')
        await self.detect()
        self.assertFalse(d._session_active,
                         'recovery must not restart the manually stopped game')
        # The original game exits; the next observation is a different game.
        old = self.identities[42]
        self.probe_game_process.side_effect = (
            lambda identity: 'exited' if identity == old else 'running'
        )
        self.scan.return_value = WindowScan([self.other], 'other')
        await self.detect()
        self.assertTrue(d._session_active,
                        'a failed manual-stop scan must not block future games')
        self.assertEqual(d._session_process_identity, self.identities[43])

    async def test_failed_manual_stop_scan_recovers_without_restarting_stopped_game(self):
        await self.stop_unbound_session_during_scan_failure()
        await self.assert_stopped_game_stays_stopped_then_new_game_starts()

    async def test_setting_toggle_recovers_failed_stop_without_restarting_stopped_game(self):
        await self.stop_unbound_session_during_scan_failure()
        await self.toggle_setting()
        await self.assert_stopped_game_stays_stopped_then_new_game_starts()

    async def test_successful_save_clears_pause_and_keeps_known_manual_stop(self):
        await self.stop_unbound_session_during_scan_failure()
        d = self.daemon
        known = {self.identities[42]}
        # This is a real successful atomic save through the existing
        # controller seam, not a test clearing its error/block flags.
        self.assertTrue(await d._save_auto_start_suppression(known))
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
