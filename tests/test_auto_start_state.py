"""Durable manual-stop suppression for automatic session start."""

import json
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vice import session_auto_start
from vice.session_auto_start import AutoStartStateError, SuppressionStore, read_host_boot_id
from vice.session_process import ProcessIdentity


BOOT_ID = "12345678-1234-5678-9abc-def012345678"


class HostBootIdTests(unittest.TestCase):
    def test_reads_and_validates_boot_id_through_flatpak_host_argv(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=(BOOT_ID.upper() + "\n").encode(), stderr=b""
        )
        with mock.patch("vice.runtime.in_vice_flatpak", return_value=True):
            with mock.patch.object(session_auto_start.subprocess, "run", return_value=completed) as run:
                self.assertEqual(read_host_boot_id(), BOOT_ID)

        command = run.call_args.args[0]
        self.assertEqual(command[0:2], ["flatpak-spawn", "--host"])
        self.assertEqual(command[-2:], ["cat", "/proc/sys/kernel/random/boot_id"])
        self.assertEqual(run.call_args.kwargs["timeout"], 3)

    def test_invalid_or_unavailable_host_boot_id_raises(self) -> None:
        for completed in (
            subprocess.CompletedProcess(args=[], returncode=0, stdout=b"not-a-uuid\n", stderr=b""),
            subprocess.CompletedProcess(args=[], returncode=1, stdout=b"", stderr=b"denied"),
        ):
            with self.subTest(result=completed.returncode):
                with mock.patch.object(session_auto_start.subprocess, "run", return_value=completed):
                    with self.assertRaises(AutoStartStateError):
                        read_host_boot_id()


class SuppressionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.json"
        self.store = SuppressionStore(BOOT_ID, self.path)

    def test_missing_state_is_empty_and_save_round_trips_private_file(self) -> None:
        self.assertEqual(self.store.load(), set())
        entries = {ProcessIdentity(321, 7654), ProcessIdentity(42, 1234)}

        self.store.save(entries)

        self.assertEqual(self.store.load(), entries)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        document = json.loads(self.path.read_text())
        self.assertEqual(document["version"], 1)
        self.assertEqual(document["boot_id"], BOOT_ID)
        self.assertEqual(
            document["suppressed"],
            [{"pid": 42, "start_time": 1234}, {"pid": 321, "start_time": 7654}],
        )

    def test_a_different_boot_id_returns_empty_without_changing_file(self) -> None:
        self.store.save({ProcessIdentity(42, 1234)})
        original = self.path.read_bytes()

        self.assertEqual(SuppressionStore("22222222-2222-2222-2222-222222222222", self.path).load(), set())
        self.assertEqual(self.path.read_bytes(), original)

    def test_malformed_state_is_rejected_and_preserved(self) -> None:
        malformed = (
            "{",
            json.dumps({"version": 2, "boot_id": BOOT_ID, "suppressed": []}),
            json.dumps({"version": 1, "boot_id": "", "suppressed": []}),
            json.dumps({"version": 1, "boot_id": "corrupt", "suppressed": []}),
            json.dumps({"version": 1, "boot_id": BOOT_ID, "suppressed": [{"pid": True, "start_time": 5}]}),
            json.dumps({"version": 1, "boot_id": BOOT_ID, "suppressed": [{"pid": 7}]}),
            "\xff",
        )
        for content in malformed:
            with self.subTest(content=content):
                self.path.write_bytes(content.encode("latin-1"))
                original = self.path.read_bytes()
                with self.assertRaises(AutoStartStateError):
                    self.store.load()
                self.assertEqual(self.path.read_bytes(), original)

    def test_read_error_is_not_treated_as_empty(self) -> None:
        self.path.write_text("{}")
        with mock.patch.object(Path, "read_text", side_effect=PermissionError("denied")):
            with self.assertRaises(AutoStartStateError):
                self.store.load()
        self.assertTrue(self.path.exists())

    def test_failed_atomic_write_keeps_previous_state(self) -> None:
        self.store.save({ProcessIdentity(42, 1234)})
        original = self.path.read_bytes()
        with mock.patch.object(session_auto_start.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(AutoStartStateError):
                self.store.save({ProcessIdentity(88, 2345)})

        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.glob(".state.json.*.tmp")), [])

    def test_invalid_identity_is_rejected_without_overwriting(self) -> None:
        self.store.save({ProcessIdentity(42, 1234)})
        original = self.path.read_bytes()
        with self.assertRaises(AutoStartStateError):
            self.store.save({ProcessIdentity(0, 1234)})
        self.assertEqual(self.path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
