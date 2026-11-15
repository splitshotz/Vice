"""Background game-window enumeration without changing focus."""

import json
import os
import unittest
from types import SimpleNamespace
from unittest import mock

from vice import active_window as aw


class KdeWindowScanTests(unittest.TestCase):
    def setUp(self):
        self.adapter_patch = mock.patch.object(aw, "_ADAPTER", aw._get_active_window_kde)
        self.adapter_patch.start()
        self.addCleanup(self.adapter_patch.stop)
        self.display_patch = mock.patch.dict(os.environ, {'DISPLAY': ''})
        self.display_patch.start()
        self.addCleanup(self.display_patch.stop)

    def test_native_kde_scan_returns_windows_and_focused_id_from_one_command(self):
        payload = {
            "windows": [
                {"window_id": "11", "pid": 101, "class": "game-a"},
                {"window_id": "12", "pid": None, "class": "game-b"},
                {"window_id": "13", "pid": 0, "class": "game-c"},
            ],
            "focused_window_id": "12",
        }
        with mock.patch.object(aw, "host_which", return_value=True), \
             mock.patch.object(aw, "_run_scan_command", return_value=json.dumps(payload)) as run, \
             mock.patch.object(aw, "_read_proc_comm", return_value="game-process"):
            scan = aw.scan_game_windows()

        self.assertEqual(scan.focused_window_id, "12")
        self.assertEqual(scan.windows, [
            {"process": "game-process", "class": "game-a", "pid": 101, "window_id": "11"},
            {"process": "", "class": "game-b", "pid": 0, "window_id": "12"},
            {"process": "", "class": "game-c", "pid": 0, "window_id": "13"},
        ])
        self.assertEqual(run.call_count, 1)
        command = run.call_args.args[0]
        self.assertEqual(command[:3], ["kdotool", "kwinscript", "--inline"])
        self.assertIn("workspace.windowList()", command[3])
        self.assertIn("workspace.activeWindow", command[3])
        self.assertIn("w.internalId.toString()", command[3])
        self.assertIn("w.pid", command[3])
        self.assertIn("w.resourceClass", command[3])
        self.assertNotIn("caption", command[3].lower())

    def test_empty_successful_scan_is_distinct_from_failure(self):
        with mock.patch.object(aw, "host_which", return_value=True), \
             mock.patch.object(aw, "_run_scan_command", return_value='{"windows": [], "focused_window_id": null}'):
            scan = aw.scan_game_windows()
        self.assertEqual(scan, aw.WindowScan([], None))

    def test_command_error_and_malformed_payload_fail_safely(self):
        for output in (None, "not json", '{"windows": "broken"}'):
            with self.subTest(output=output), \
                 mock.patch.object(aw, "host_which", return_value=True), \
                 mock.patch.object(aw, "_run_scan_command", return_value=output):
                self.assertIsNone(aw.scan_game_windows())

    def test_kde_rejects_noninteger_or_boolean_pid_values(self):
        for pid in (True, 1.5, 1.0, 0.0, "123"):
            payload = json.dumps({
                "windows": [{"window_id": "11", "pid": pid, "class": "game"}],
                "focused_window_id": None,
            })
            with self.subTest(pid=pid), \
                 mock.patch.object(aw, "host_which", return_value=True), \
                 mock.patch.object(aw, "_run_scan_command", return_value=payload):
                self.assertIsNone(aw.scan_game_windows())

    def test_host_kdotool_uses_host_argv(self):
        payload = '{"windows": [], "focused_window_id": null}'
        seen = []

        def fake_run(command, **kwargs):
            seen.append(command)
            return SimpleNamespace(returncode=0, stdout=payload, stderr="")

        with mock.patch.object(aw, "host_which", return_value=True), \
             mock.patch.object(aw, "host_argv", side_effect=lambda argv: ["flatpak-spawn", "--host", *argv]), \
             mock.patch.object(aw.subprocess, "run", side_effect=fake_run):
            scan = aw.scan_game_windows()

        self.assertEqual(scan, aw.WindowScan([], None))
        self.assertEqual(seen[0][:3], ["flatpak-spawn", "--host", "kdotool"])

    def test_missing_kdotool_falls_back_to_x11_when_display_exists(self):
        with mock.patch.object(aw, "host_which", return_value=False), \
             mock.patch.object(aw, "_x11_scan_tools_available", return_value=True), \
             mock.patch.object(aw, "_scan_x11_game_windows", return_value=aw.WindowScan([], None)) as scan_x11, \
             mock.patch.dict(os.environ, {"DISPLAY": ":1"}):
            self.assertTrue(aw.auto_start_scan_supported())
            self.assertEqual(aw.scan_game_windows(), aw.WindowScan([], None))
        scan_x11.assert_called_once_with()

    def test_kde_scan_fallback_only_after_failure_with_display(self):
        fallback = aw.WindowScan([{'process': 'cs2', 'class': 'cs2',
                                   'pid': 42, 'window_id': '0x1'}], '0x1')
        empty = aw.WindowScan([], None)
        cases = [
            ('KDE fails, X11 succeeds', None, ':1', fallback, fallback, 1),
            ('both fail', None, ':1', None, None, 1),
            ('no DISPLAY', None, '', fallback, None, 0),
            ('empty KDE is success', empty, ':1', fallback, empty, 0),
            ('nonempty KDE is success', fallback, ':1', empty, fallback, 0),
        ]
        for label, kde_result, display, x11_result, expected, x11_calls in cases:
            with self.subTest(label=label), \
                 mock.patch.object(aw, 'host_which', return_value=True), \
                 mock.patch.object(aw, '_scan_kde_game_windows', return_value=kde_result) as kde, \
                 mock.patch.object(aw, '_scan_x11_game_windows', return_value=x11_result) as x11, \
                 mock.patch.dict(os.environ, {'DISPLAY': display}):
                self.assertEqual(aw.scan_game_windows(), expected)
                kde.assert_called_once_with()
                self.assertEqual(x11.call_count, x11_calls)


class ScanCommandTests(unittest.TestCase):
    def test_runner_distinguishes_successful_empty_stdout_from_command_error(self):
        with mock.patch.object(aw.subprocess, "run", return_value=SimpleNamespace(
            returncode=0, stdout="", stderr=""
        )):
            self.assertEqual(aw._run_scan_command(["tool"]), "")
        with mock.patch.object(aw.subprocess, "run", return_value=SimpleNamespace(
            returncode=1, stdout="", stderr="localized command error"
        )):
            self.assertIsNone(aw._run_scan_command(["tool"]))


class X11WindowScanTests(unittest.TestCase):
    def setUp(self):
        self.adapter_patch = mock.patch.object(aw, "_ADAPTER", aw._get_active_window_x11)
        self.adapter_patch.start()
        self.addCleanup(self.adapter_patch.stop)

    def test_wmctrl_scan_canonicalizes_window_and_focused_ids(self):
        output = "0x0000000a  0 1234  Game.Class  title\n"

        def run(command, timeout=3.0):
            if command == ["wmctrl", "-lpx"]:
                return output
            if command == ["xdotool", "getactivewindow"]:
                return "10\n"
            raise AssertionError(command)

        with mock.patch.object(aw.shutil, "which", side_effect=lambda name: f"/usr/bin/{name}"), \
             mock.patch.object(aw, "_run_scan_command", side_effect=run), \
             mock.patch.object(aw, "_read_proc_comm", return_value="game"):
            scan = aw.scan_game_windows()

        self.assertEqual(scan.windows, [{
            "process": "game", "class": "Class", "pid": 1234, "window_id": "0xa",
        }])
        self.assertEqual(scan.focused_window_id, "0xa")

    def test_successful_empty_wmctrl_scan_is_valid(self):
        with mock.patch.object(aw.shutil, "which", side_effect=lambda name: f"/usr/bin/{name}"), \
             mock.patch.object(aw, "_run_scan_command", return_value=""):
            self.assertEqual(aw.scan_game_windows(), aw.WindowScan([], None))

    def test_wmctrl_failure_is_not_misreported_as_empty(self):
        with mock.patch.object(aw.shutil, "which", side_effect=lambda name: "/usr/bin/wmctrl" if name == "wmctrl" else None), \
             mock.patch.object(aw, "_run_scan_command", return_value=None):
            self.assertIsNone(aw.scan_game_windows())

    def test_xdotool_fallback_includes_background_windows_beyond_thirty(self):
        ids = [str(index) for index in range(1, 36)]
        search_command = ["xdotool", "search", "--class", ".*"]

        def run(command, timeout=3.0):
            if command == search_command:
                return "\n".join(ids)
            if command == ["xdotool", "getactivewindow"]:
                return "35"
            if command[0] == "xdotool" and command[1] == "getwindowpid":
                return "0"
            if command[0] == "xprop":
                return f'WM_CLASS(STRING) = "game-{command[2]}", "game-{command[2]}"'
            raise AssertionError(command)

        with mock.patch.object(aw.shutil, "which", side_effect=lambda name: None if name == "wmctrl" else f"/usr/bin/{name}"), \
             mock.patch.object(aw, "_run_scan_command", side_effect=run):
            scan = aw.scan_game_windows()

        self.assertEqual(len(scan.windows), 35)
        self.assertEqual(scan.windows[0]["window_id"], "0x1")
        self.assertEqual(scan.windows[-1]["window_id"], "0x23")
        self.assertEqual(scan.focused_window_id, "0x23")
        self.assertNotIn("--onlyvisible", search_command)

    def test_xdotool_missing_pid_is_retained_when_class_is_available(self):
        def run(command, timeout=3.0):
            if command[:3] == ["xdotool", "search", "--class"]:
                return "7"
            if command == ["xdotool", "getwindowpid", "7"]:
                return None
            if command[0] == "xprop":
                return 'WM_CLASS(STRING) = "unknown", "Game"'
            if command == ["xdotool", "getactivewindow"]:
                return ""
            raise AssertionError(command)

        with mock.patch.object(aw.shutil, "which", side_effect=lambda name: None if name == "wmctrl" else f"/usr/bin/{name}"), \
             mock.patch.object(aw, "_run_scan_command", side_effect=run):
            scan = aw.scan_game_windows()
        self.assertEqual(scan.windows, [{
            "process": "", "class": "Game", "pid": 0, "window_id": "0x7",
        }])

    def test_xprop_transport_failure_fails_the_whole_scan(self):
        def run(command, timeout=3.0):
            if command[:3] == ["xdotool", "search", "--class"]:
                return "7 8"
            if command[:2] == ["xdotool", "getwindowpid"]:
                return "1234"
            if command[0] == "xprop" and command[2] == "7":
                return 'WM_CLASS(STRING) = "game", "Game"'
            if command[0] == "xprop" and command[2] == "8":
                return None
            raise AssertionError(command)

        with mock.patch.object(aw.shutil, "which", side_effect=lambda name: None if name == "wmctrl" else f"/usr/bin/{name}"), \
             mock.patch.object(aw, "_run_scan_command", side_effect=run):
            self.assertIsNone(aw.scan_game_windows())

    def test_xdotool_error_and_malformed_ids_are_failures(self):
        for output in (None, "bad-window-id"):
            with self.subTest(output=output), \
                 mock.patch.object(aw.shutil, "which", side_effect=lambda name: None if name == "wmctrl" else f"/usr/bin/{name}"), \
                 mock.patch.object(aw, "_run_scan_command", return_value=output):
                self.assertIsNone(aw.scan_game_windows())

    def test_supported_checks_adapter_and_tools(self):
        with mock.patch.object(aw.shutil, "which", side_effect=lambda name: "/usr/bin/wmctrl" if name == "wmctrl" else None):
            self.assertTrue(aw.auto_start_scan_supported())
        with mock.patch.object(aw.shutil, "which", return_value=None):
            self.assertFalse(aw.auto_start_scan_supported())
        with mock.patch.object(aw, "_ADAPTER", aw._get_active_window_sway):
            self.assertFalse(aw.auto_start_scan_supported())
            self.assertIsNone(aw.scan_game_windows())


if __name__ == "__main__":
    unittest.main()
