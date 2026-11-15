"""Focused tests for binding a window PID to a stable Linux process identity."""

import subprocess
import unittest
from types import SimpleNamespace
from unittest import mock

from vice.session_process import (
    ProcessIdentity,
    bind_game_process,
    probe_game_process,
)


def proc_stat(pid=1234, comm="game (client)", state="S", start_time=9876):
    fields = [state, *(["0"] * 18), str(start_time)]
    return f"{pid} ({comm}) {' '.join(fields)}\n".encode()


class BindGameProcessTests(unittest.TestCase):
    def test_binds_pid_and_start_time_from_stat_with_parens_in_comm(self):
        with mock.patch("vice.session_process.Path.read_bytes", return_value=proc_stat()):
            self.assertEqual(bind_game_process({"pid": 1234}), ProcessIdentity(1234, 9876))

    def test_rejects_invalid_pids(self):
        for pid in (None, "bad", 0, -5, 1234.0, True):
            with self.subTest(pid=pid):
                self.assertIsNone(bind_game_process({"pid": pid}))

    def test_rejects_unreadable_or_malformed_stat(self):
        for failure in (PermissionError("denied"), FileNotFoundError()):
            with self.subTest(failure=type(failure).__name__), mock.patch(
                "vice.session_process.Path.read_bytes", side_effect=failure
            ):
                self.assertIsNone(bind_game_process({"pid": 1234}))
        with mock.patch("vice.session_process.Path.read_bytes", return_value=b"not proc stat"):
            self.assertIsNone(bind_game_process({"pid": 1234}))

    def test_rejects_invalid_stat_shape_state_starttime_and_dead_process(self):
        malformed = (
            b"1234 game S " + b"0 " * 18 + b"9876\n",
            b"1234 (game) ? " + b"0 " * 18 + b"9876\n",
            proc_stat(start_time=-1),
            proc_stat(state="Z"),
            proc_stat(state="X"),
        )
        for raw in malformed:
            with self.subTest(raw=raw[:32]), mock.patch(
                "vice.session_process.Path.read_bytes", return_value=raw
            ):
                self.assertIsNone(bind_game_process({"pid": 1234}))

    def test_rejects_proxy_comms_including_linux_truncation(self):
        for comm in ("xwayland-satellite", "Xwayland", "steam", "steamwebhelper", "wineserver"):
            with self.subTest(comm=comm), mock.patch(
                "vice.session_process.Path.read_bytes",
                return_value=proc_stat(comm=comm),
            ):
                self.assertIsNone(bind_game_process({"pid": 1234}))


class ProbeGameProcessTests(unittest.TestCase):
    def test_same_live_process_is_running(self):
        with mock.patch("vice.session_process.Path.read_bytes", return_value=proc_stat()):
            self.assertEqual(probe_game_process(ProcessIdentity(1234, 9876)), "running")

    def test_missing_process_pid_reuse_and_zombie_are_exited(self):
        identity = ProcessIdentity(1234, 9876)
        with mock.patch("vice.session_process.Path.read_bytes", side_effect=FileNotFoundError):
            self.assertEqual(probe_game_process(identity), "exited")
        with mock.patch("vice.session_process.Path.read_bytes", return_value=proc_stat(start_time=9999)):
            self.assertEqual(probe_game_process(identity), "exited")
        with mock.patch("vice.session_process.Path.read_bytes", return_value=proc_stat(state="Z")):
            self.assertEqual(probe_game_process(identity), "exited")

    def test_read_errors_and_malformed_stat_are_unknown(self):
        identity = ProcessIdentity(1234, 9876)
        with mock.patch("vice.session_process.Path.read_bytes", side_effect=PermissionError):
            self.assertEqual(probe_game_process(identity), "unknown")
        with mock.patch("vice.session_process.Path.read_bytes", return_value=b"broken"):
            self.assertEqual(probe_game_process(identity), "unknown")

    def test_flatpak_probe_reads_host_proc_and_distinguishes_missing(self):
        identity = ProcessIdentity(1234, 9876)
        with mock.patch("vice.session_process.in_vice_flatpak", return_value=True), \
             mock.patch("vice.session_process.host_argv", side_effect=lambda argv: ["flatpak-spawn", "--host", *argv]) as host_argv, \
             mock.patch("vice.session_process.subprocess.run", return_value=SimpleNamespace(
                 returncode=0, stdout=proc_stat(), stderr=b""
             )):
            self.assertEqual(probe_game_process(identity), "running")
            command = host_argv.call_args.args[0]
            self.assertEqual(command[:2], ["sh", "-c"])
            self.assertEqual(command[-2:], ["sh", "1234"])

        with mock.patch("vice.session_process.in_vice_flatpak", return_value=True), \
             mock.patch("vice.session_process.host_argv", side_effect=lambda argv: argv), \
             mock.patch("vice.session_process.subprocess.run", return_value=SimpleNamespace(
                 returncode=0, stdout=b"__VICE_PROC_STAT_MISSING__\n", stderr=b""
             )):
            self.assertEqual(probe_game_process(identity), "exited")

    def test_flatpak_command_and_permission_errors_are_unknown(self):
        identity = ProcessIdentity(1234, 9876)
        with mock.patch("vice.session_process.in_vice_flatpak", return_value=True), \
             mock.patch("vice.session_process.host_argv", side_effect=lambda argv: argv), \
             mock.patch("vice.session_process.subprocess.run", return_value=SimpleNamespace(
                 returncode=1, stdout=b"", stderr=b"Permission denied"
             )):
            self.assertEqual(probe_game_process(identity), "unknown")

        for stderr in (b"Permission denied", b"Datei oder Verzeichnis nicht gefunden"):
            with self.subTest(stderr=stderr), \
                 mock.patch("vice.session_process.in_vice_flatpak", return_value=True), \
                 mock.patch("vice.session_process.host_argv", side_effect=lambda argv: argv), \
                 mock.patch("vice.session_process.subprocess.run", return_value=SimpleNamespace(
                     returncode=1, stdout=b"", stderr=stderr
                 )):
                self.assertEqual(probe_game_process(identity), "unknown")

        with mock.patch("vice.session_process.in_vice_flatpak", return_value=True), \
             mock.patch("vice.session_process.host_argv", side_effect=lambda argv: argv), \
             mock.patch("vice.session_process.subprocess.run", side_effect=subprocess.TimeoutExpired("cat", 2)):
            self.assertEqual(probe_game_process(identity), "unknown")

        with mock.patch("vice.session_process.in_vice_flatpak", return_value=True), \
             mock.patch("vice.session_process.host_argv", side_effect=lambda argv: argv), \
             mock.patch("vice.session_process.subprocess.run", side_effect=FileNotFoundError("flatpak-spawn")):
            self.assertEqual(probe_game_process(identity), "unknown")

    def test_mismatched_pid_in_stat_reply_is_unknown(self):
        with mock.patch("vice.session_process.Path.read_bytes", return_value=proc_stat(pid=1235)):
            self.assertEqual(probe_game_process(ProcessIdentity(1234, 9876)), "unknown")


if __name__ == "__main__":
    unittest.main()
