"""Regression contracts for unreadable state and disabled manual recording."""

import os
import asyncio
import errno
import threading
import unittest
from pathlib import Path
from unittest import mock

from tests import test_auto_start_sessions as sessions
from vice.session_auto_start import SuppressionStore
from vice.active_window import WindowScan


class AuditFixContracts(unittest.IsolatedAsyncioTestCase):
    setUp = sessions.AutoStartSessionTests.setUp
    cleanup_session = sessions.AutoStartSessionTests.cleanup_session
    detect = sessions.AutoStartSessionTests.detect

    @unittest.skipIf(os.geteuid() == 0, 'requires normal-user file permissions')
    async def test_unreadable_replaceable_state_recovers_and_keeps_memory(self):
        d = self.daemon
        store = SuppressionStore(sessions.BOOT, self.state_path)
        store.save({self.identities[43]})
        original = self.state_path.read_bytes()
        self.state_path.chmod(0)
        self.addCleanup(self.state_path.chmod, 0o600)
        with self.assertRaises(PermissionError):
            self.state_path.read_bytes()
        d._auto_start_suppressed = {self.identities[42]}

        with self.assertLogs('vice', level='WARNING'):
            recovered = await d._ensure_auto_start_store()

        self.assertTrue(recovered, 'a writable parent permits atomic replacement')
        self.assertEqual(store.load(), {self.identities[42]})
        self.assertEqual(self.state_path.stat().st_mode & 0o777, 0o600)
        self.assertIsNone(d._auto_start_error)
        backups = list(self.root.glob('.auto_start.json.unreadable.*.bak'))
        self.assertEqual(len(backups), 1)
        backups[0].chmod(0o600)
        self.assertEqual(backups[0].read_bytes(), original)

        # The repaired snapshot remains valid after a controller restart.
        del d._auto_start_task
        d._init_auto_start()
        await self.detect()
        self.assertFalse(d._session_active)
        self.scan.return_value = WindowScan([self.other], 'other')
        await self.detect()
        self.assertTrue(d._session_active)
        self.assertEqual(d._session_process_identity, self.identities[43])

    @unittest.skipIf(os.geteuid() == 0, 'requires normal-user file permissions')
    async def test_unreadable_nonreplaceable_state_blocks_then_recovers(self):
        d = self.daemon
        store = SuppressionStore(sessions.BOOT, self.state_path)
        store.save(set())
        self.state_path.chmod(0)
        self.root.chmod(0o500)
        self.addCleanup(self.state_path.chmod, 0o600)
        self.addCleanup(self.root.chmod, 0o700)
        d._auto_start_suppressed = {self.identities[42]}
        inode = self.state_path.stat().st_ino

        with self.assertLogs('vice', level='WARNING'):
            self.assertFalse(await d._ensure_auto_start_store())
        self.assertEqual(d._auto_start_error, 'state')
        self.assertTrue(d._auto_start_storage_failed)
        self.assertEqual(self.state_path.stat().st_ino, inode)
        self.assertEqual(d._auto_start_suppressed, {self.identities[42]})
        self.root.chmod(0o700)
        self.assertTrue(await d._ensure_auto_start_store())
        self.assertEqual(store.load(), {self.identities[42]})

    async def test_temporary_read_errors_do_not_replace_readable_state(self):
        d = self.daemon
        known = {self.identities[43]}
        SuppressionStore(sessions.BOOT, self.state_path).save(known)
        original = self.state_path.read_bytes()
        read = Path.read_text
        for code in (errno.EAGAIN, errno.EIO, errno.EACCES):
            with self.subTest(errno=code):
                def failing_read(path, *args, **kwargs):
                    if path == self.state_path:
                        raise OSError(code, 'injected transient read error', str(path))
                    return read(path, *args, **kwargs)
                with mock.patch.object(Path, 'read_text', new=failing_read), \
                     self.assertLogs('vice', level='WARNING'):
                    self.assertFalse(await d._ensure_auto_start_store())
                self.assertEqual(self.state_path.read_bytes(), original)
                self.assertEqual(list(self.root.glob('.*.bak')), [])
                self.assertIsNone(d._auto_start_store)
        self.assertTrue(await d._ensure_auto_start_store())
        self.assertEqual(d._auto_start_suppressed, known)

    @unittest.skipIf(os.geteuid() == 0, 'requires normal-user file permissions')
    async def test_failed_unreadable_replacement_retains_original_and_retries(self):
        d = self.daemon
        store = SuppressionStore(sessions.BOOT, self.state_path)
        store.save(set())
        original = self.state_path.read_bytes()
        inode = self.state_path.stat().st_ino
        self.state_path.chmod(0)
        self.addCleanup(self.state_path.chmod, 0o600)
        d._auto_start_suppressed = {self.identities[42]}
        with mock.patch('vice.session_auto_start.os.replace',
                        side_effect=OSError(errno.ENOSPC, 'injected disk full')), \
             self.assertLogs('vice', level='WARNING'):
            self.assertFalse(await d._ensure_auto_start_store())
        self.assertEqual(self.state_path.stat().st_ino, inode)
        self.assertEqual(self.state_path.stat().st_mode & 0o777, 0)
        self.assertEqual(d._auto_start_error, 'state')
        self.assertTrue(d._auto_start_dirty)
        self.assertTrue(await d._ensure_auto_start_store())
        self.assertEqual(store.load(), {self.identities[42]})
        backup, = self.root.glob('.auto_start.json.unreadable.*.bak')
        backup.chmod(0o600)
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(list(self.root.glob('*.tmp')), [])

    @unittest.skipIf(os.geteuid() == 0, 'requires normal-user file permissions')
    async def test_unreadable_symlink_is_not_replaced(self):
        target = self.root / 'target.json'
        SuppressionStore(sessions.BOOT, target).save({self.identities[43]})
        original = target.read_bytes()
        target.chmod(0)
        self.addCleanup(target.chmod, 0o600)
        self.state_path.symlink_to(target)
        with self.assertLogs('vice', level='WARNING'):
            self.assertFalse(await self.daemon._ensure_auto_start_store())
        self.assertTrue(self.state_path.is_symlink())
        self.assertEqual(list(self.root.glob('.*.bak')), [])
        target.chmod(0o600)
        self.assertEqual(target.read_bytes(), original)

    @unittest.skipIf(os.geteuid() == 0, 'requires normal-user file permissions')
    async def test_backup_failure_logs_and_replaces_unreadable_state(self):
        d = self.daemon
        store = SuppressionStore(sessions.BOOT, self.state_path)
        store.save(set())
        self.state_path.chmod(0)
        self.addCleanup(self.state_path.chmod, 0o600)
        with mock.patch('vice.session_auto_start.os.link',
                        side_effect=OSError(errno.EOPNOTSUPP, 'no hard links')), \
             self.assertLogs('vice', level='WARNING') as logs:
            self.assertTrue(await d._ensure_auto_start_store())
        self.assertEqual(store.load(), set())
        self.assertTrue(any('Could not preserve unreadable' in line for line in logs.output))

    async def test_disabled_manual_stop_does_not_scan_or_save_initialized_store(self):
        d = self.daemon
        known = {self.identities[42]}
        SuppressionStore(sessions.BOOT, self.state_path).save(known)
        self.assertTrue(await d._ensure_auto_start_store())
        original = self.state_path.read_bytes()
        d.cfg.recording.session_start_on_game_detect = False
        d.cfg.recording.session_stop_on_game_exit = False
        await d._sync_auto_start_task()
        self.focus.return_value = self.other
        await d._start_session()
        self.assertIsNone(d._session_process_identity)
        self.scan.reset_mock()

        with mock.patch.object(d._auto_start_store, 'save', wraps=d._auto_start_store.save) as save:
            await d._stop_session()

        self.scan.assert_not_called()
        save.assert_not_called()
        self.assertEqual(d._auto_start_suppressed, known)
        self.assertEqual(self.state_path.read_bytes(), original)

    async def test_repeated_toggles_preserve_stops_and_allow_other_game(self):
        d = self.daemon
        await self.detect()
        await d._stop_session()
        original = self.state_path.read_bytes()
        known = {self.identities[42]}
        for _ in range(3):
            d.cfg.recording.session_start_on_game_detect = False
            await d._sync_auto_start_task()
            self.scan.reset_mock()
            with mock.patch.object(d._auto_start_store, 'save', wraps=d._auto_start_store.save) as save:
                await d._start_session()
                await d._stop_session()
            self.scan.assert_not_called()
            save.assert_not_called()
            self.assertEqual(self.state_path.read_bytes(), original)
            d.cfg.recording.session_start_on_game_detect = True
            await self.detect()
            self.assertFalse(d._session_active)
        self.assertEqual(d._auto_start_suppressed, known)
        self.scan.return_value = WindowScan([self.other], 'other')
        await self.detect()
        self.assertTrue(d._session_active)
        self.assertEqual(d._session_process_identity, self.identities[43])

    async def test_disable_during_manual_stop_scan_discards_scan_without_error(self):
        d = self.daemon
        await d._ensure_auto_start_store()
        d.cfg.recording.session_start_on_game_detect = False
        await d._start_session()
        d.cfg.recording.session_start_on_game_detect = True
        entered, release = threading.Event(), threading.Event()
        def scan():
            entered.set()
            if not release.wait(3):
                raise TimeoutError('test did not release scanner')
            return None
        with mock.patch('vice.main.scan_game_windows', side_effect=scan), \
             mock.patch.object(d._auto_start_store, 'save', wraps=d._auto_start_store.save) as save:
            stopping = asyncio.create_task(d._stop_session())
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                d.cfg.recording.session_start_on_game_detect = False
                await d._sync_auto_start_task()
            finally:
                release.set()
            await asyncio.wait_for(stopping, 3)
        self.assertFalse(d._session_active)
        self.assertIsNone(d._auto_start_error)
        save.assert_not_called()
        self.assertEqual(d._auto_start_suppressed, set())

    async def test_disable_while_stop_waits_for_io_lock_does_not_mutate_state(self):
        d = self.daemon
        await d._ensure_auto_start_store()
        await d._start_session()
        self.assertEqual(d._session_process_identity, self.identities[42])
        entered = asyncio.Event()
        original_save = d._save_auto_start_suppression
        async def save(*args, **kwargs):
            entered.set()
            return await original_save(*args, **kwargs)
        generation = d._auto_start_generation
        await d._auto_start_io_lock.acquire()
        with mock.patch.object(d, '_save_auto_start_suppression', side_effect=save):
            stopping = asyncio.create_task(d._stop_session())
            try:
                await asyncio.wait_for(entered.wait(), 3)
                d.cfg.recording.session_start_on_game_detect = False
                await d._sync_auto_start_task()
            finally:
                d._auto_start_io_lock.release()
            await asyncio.wait_for(stopping, 3)
        self.assertEqual(d._auto_start_generation, generation)
        self.assertEqual(d._auto_start_suppressed, set())
        self.assertFalse(d._auto_start_dirty)
        self.assertFalse(self.state_path.exists())

    @unittest.skipIf(os.geteuid() == 0, 'requires normal-user file permissions')
    async def test_unreadable_recovery_concurrent_with_manual_stop_merges_identities(self):
        d = self.daemon
        SuppressionStore(sessions.BOOT, self.state_path).save(set())
        self.state_path.chmod(0)
        self.addCleanup(self.state_path.chmod, 0o600)
        d._auto_start_suppressed = {self.identities[42]}
        self.focus.return_value = self.other
        await d._start_session()
        entered, release = threading.Event(), threading.Event()
        original_backup = SuppressionStore.backup_unreadable
        def backup(store):
            entered.set()
            if not release.wait(3):
                raise TimeoutError('test did not release recovery')
            return original_backup(store)
        with mock.patch.object(SuppressionStore, 'backup_unreadable', new=backup), \
             self.assertLogs('vice', level='WARNING'):
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
        self.assertEqual(SuppressionStore(sessions.BOOT, self.state_path).load(),
                         set(self.identities.values()))


if __name__ == '__main__':
    unittest.main()
