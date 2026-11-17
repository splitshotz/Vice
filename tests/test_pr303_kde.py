"""PR #303 KDE contracts, plus controls for pre-existing focus behavior.

Capability tests load a fresh module so future caches cannot leak between
cases. External processes and compositor responses are always mocked.
"""

import importlib.util
import os
import subprocess
import sys
import unittest
from contextlib import ExitStack
from unittest import mock

from tests import test_auto_start_sessions as sessions
from vice import active_window as aw, runtime
from vice.config import Config
from vice.main import ViceDaemon

KDE_ENV = {'DISPLAY': ':1', 'WAYLAND_DISPLAY': 'wayland-0',
           'XDG_CURRENT_DESKTOP': 'KDE'}


class KdeFocusContracts(unittest.TestCase):
    def setUp(self):
        for patcher in (
            mock.patch.object(aw, '_ADAPTER', aw._get_active_window_kde),
            mock.patch.dict(os.environ, KDE_ENV),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_kde_focus_preserves_pre_pr_two_command_budget(self):
        # Both real CLI forms are understood, so the test fails for the
        # changed command budget, not because its mock rejects the PR form.
        responses = {
            ('kdotool', 'getactivewindow', 'getwindowpid'): '712',
            ('kdotool', 'getactivewindow', 'getwindowclassname'): 'game-class',
            ('kdotool', 'getactivewindow'): '17',
            ('kdotool', 'getwindowpid', '17'): '712',
            ('kdotool', 'getwindowclassname', '17'): 'game-class',
        }
        with (
            mock.patch.object(aw, '_run', side_effect=lambda cmd: responses.get(tuple(cmd), '')) as run,
            mock.patch.object(aw, '_read_proc_comm', return_value='game'),
        ):
            self.assertEqual(aw.get_active_window(), {
                'process': 'game', 'class': 'game-class', 'pid': 712,
            })
        self.assertEqual(run.call_count, 2, run.call_args_list)

    def test_no_kde_focus_can_return_stale_xwayland_focus_as_on_base(self):
        # Characterization, not a newly introduced PR regression: base
        # b165982 already falls back when both KDE process and class are empty.
        stale = {'process': 'stale', 'class': 'old', 'pid': 888,
                 'window_id': '91'}
        responses = {
            ('xdotool', 'getactivewindow'): '91',
            ('xdotool', 'getwindowpid', '91'): '888',
            ('xprop', '-id', '91', 'WM_CLASS'): 'WM_CLASS(STRING) = "old", "old"',
        }
        with (
            mock.patch.object(aw, '_run', side_effect=lambda cmd: responses.get(tuple(cmd), '')),
            mock.patch.object(aw, '_read_proc_comm', return_value='stale'),
        ):
            self.assertEqual(aw.get_active_window(), stale)

    def test_no_kde_focus_without_xwayland_returns_none(self):
        with (
            mock.patch.dict(os.environ, {'DISPLAY': ''}),
            mock.patch.object(aw, '_run', return_value=''),
        ):
            self.assertIsNone(aw.get_active_window())


class KdeOffControl(unittest.IsolatedAsyncioTestCase):
    async def test_default_off_does_not_change_no_focus_detection_or_start_scanning(self):
        d = ViceDaemon.__new__(ViceDaemon)
        d.cfg = Config()
        d.share = None
        self.assertFalse(d.cfg.recording.session_start_on_game_detect)
        stale = {'process': 'stale', 'class': 'old', 'pid': 888}
        with (
            mock.patch.object(aw, '_ADAPTER', aw._get_active_window_kde),
            mock.patch.dict(os.environ, KDE_ENV),
            mock.patch.object(aw, '_run', return_value=''),
            mock.patch.object(aw, '_get_active_window_x11', return_value=stale),
            mock.patch('vice.main.scan_game_windows') as scan,
            mock.patch.object(aw, '_kde_scan_supported') as probe,
        ):
            before = aw.get_active_window()
            await d._sync_auto_start_task()
            await d._auto_start_tick()
            self.assertEqual(before, stale)
            self.assertEqual(aw.get_active_window(), before)
            scan.assert_not_called()
            probe.assert_not_called()
            self.assertIsNone(d._auto_start_task)
            self.assertIsNone(d._auto_start_store)


class FakeKdotool:
    """Public CLI responses based on the v0.2.2 and v0.3.0 command sets."""
    def __init__(self, mode):
        self.mode = mode
        self.calls = []

    def __call__(self, argv, **kwargs):
        args = tuple(argv)
        self.calls.append(args)
        if args[0] != 'kdotool':
            raise AssertionError('unexpected subprocess: ' + repr(args))
        if self.mode == 'oserror':
            raise OSError('probe inaccessible')
        if self.mode == 'timeout':
            raise subprocess.TimeoutExpired(argv, kwargs.get('timeout', 1))
        if self.mode == 'nonzero':
            return subprocess.CompletedProcess(args, 1, '', 'probe failed')
        if args[1:] in (('--version',), ('-v',)):
            version = '0.2.2' if self.mode == 'old' else '0.3.0'
            return subprocess.CompletedProcess(args, 0, 'kdotool v' + version, '')
        if args[1:] in (('--help',), ('-h',)):
            help_text = 'getactivewindow getwindowpid getwindowclassname'
            if self.mode == 'compatible':
                help_text += '\nkwinscript --file <path> | --inline <code>'
            return subprocess.CompletedProcess(args, 0, help_text, '')
        if args[1:] == ('kwinscript', '--help') and self.mode == 'compatible':
            return subprocess.CompletedProcess(args, 0, 'kwinscript --file <path> | --inline <code>', '')
        if self.mode == 'old':
            return subprocess.CompletedProcess(args, 1, '', 'Error: Unknown command: kwinscript')
        if len(args) == 4 and args[:3] == ('kdotool', 'kwinscript', '--inline'):
            payload = '{"windows":[{"window_id":"11","pid":42,"class":"cs2"}],"focused_window_id":"11"}'
            return subprocess.CompletedProcess(args, 0, payload, '')
        return subprocess.CompletedProcess(args, 1, '', 'unknown command')


def fresh_window_module(test):
    # Isolate caches without assuming their name or introducing production APIs.
    name = 'vice._pr303_probe_' + str(id(test))
    spec = importlib.util.spec_from_file_location(name, aw.__file__)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    test.addCleanup(sys.modules.pop, name, None)
    spec.loader.exec_module(module)
    module._ADAPTER = module._get_active_window_kde
    return module


def install_cli(test, native, mode, *, installed=True):
    fake = FakeKdotool(mode)
    stack = ExitStack()
    test.addCleanup(stack.close)
    stack.enter_context(mock.patch.dict(os.environ, KDE_ENV))
    stack.enter_context(mock.patch.object(native, 'host_which', return_value=installed))
    stack.enter_context(mock.patch.object(native, 'host_argv', side_effect=lambda args: args))
    stack.enter_context(mock.patch.object(native.shutil, 'which',
                                         side_effect=lambda name: '/usr/bin/wmctrl' if name == 'wmctrl' else None))
    stack.enter_context(mock.patch.object(native.subprocess, 'run', side_effect=fake))
    stack.enter_context(mock.patch.object(native, '_read_proc_comm', return_value='cs2'))
    return fake


class KdeInlineCapabilityContracts(unittest.TestCase):
    def setUp(self):
        self.native = fresh_window_module(self)

    def test_missing_kdotool_is_not_supported_despite_x11_tools(self):
        fake = install_cli(self, self.native, 'compatible', installed=False)
        self.assertFalse(self.native.auto_start_scan_supported())
        self.assertEqual(fake.calls, [])

    def test_installed_incompatible_kdotool_reports_unsupported(self):
        install_cli(self, self.native, 'old')
        self.assertFalse(self.native.auto_start_scan_supported())

    def test_missing_tool_is_unsupported_when_focus_adapter_falls_back_to_x11(self):
        install_cli(self, self.native, 'old', installed=False)
        self.native._ADAPTER = self.native._get_active_window_x11
        with mock.patch.object(self.native, '_scan_x11_game_windows') as x11:
            self.assertFalse(self.native.auto_start_scan_supported())
            self.assertIsNone(self.native.scan_game_windows())
            x11.assert_not_called()

    def test_missing_tool_without_xwayland_does_not_repeat_host_presence_probes(self):
        install_cli(self, self.native, 'old', installed=False)
        self.native._ADAPTER = None
        with (
            mock.patch.dict(os.environ, {'DISPLAY': '', 'HYPRLAND_INSTANCE_SIGNATURE': '',
                                         'SWAYSOCK': '', 'NIRI_SOCKET': ''}),
            mock.patch.object(self.native, 'host_which', return_value=False) as which,
        ):
            self.assertEqual([self.native.auto_start_scan_supported() for _ in range(3)],
                             [False, False, False])
            self.assertIsNone(self.native.scan_game_windows())
        which.assert_called_once_with('kdotool')

    def test_flatpak_probes_and_scans_host_kdotool_without_local_tools(self):
        fake = FakeKdotool('compatible')
        commands = []

        def run_host(command, **kwargs):
            commands.append(command)
            self.assertEqual(command[:2], ['flatpak-spawn', '--host'])
            if command[-2:] == ['sh', 'kdotool']:
                return subprocess.CompletedProcess(command, 0, b'', b'')
            # host_argv can insert --env flags before the executable.
            return fake(command[command.index('kdotool'):], **kwargs)

        with (
            mock.patch.dict(os.environ, KDE_ENV),
            mock.patch.object(runtime, 'in_vice_flatpak', return_value=True),
            mock.patch.object(self.native.shutil, 'which', return_value=None),
            mock.patch.object(self.native.subprocess, 'run', side_effect=run_host),
            mock.patch.object(self.native, '_read_proc_comm', return_value='cs2'),
        ):
            self.assertTrue(self.native.auto_start_scan_supported())
            self.assertTrue(self.native.auto_start_scan_supported())
            self.assertIsNotNone(self.native.scan_game_windows())
        self.assertEqual(len(commands), 3)  # host presence, help probe, scan
        self.assertEqual(fake.calls[0], ('kdotool', '--help'))
        self.assertEqual(fake.calls[1][:3], ('kdotool', 'kwinscript', '--inline'))

    def test_unsupported_capability_result_is_probed_and_cached(self):
        fake = install_cli(self, self.native, 'old')
        results = [self.native.auto_start_scan_supported()]
        first_calls = list(fake.calls)
        results.extend(self.native.auto_start_scan_supported() for _ in range(2))
        self.assertTrue(first_calls, 'binary presence alone is not a capability probe')
        self.assertEqual(fake.calls, first_calls, 'repeat support checks must use the cached result')
        self.assertEqual(results, [False, False, False])

    def test_polling_does_not_repeat_known_unsupported_inline_command(self):
        fake = install_cli(self, self.native, 'old')
        with mock.patch.object(self.native, '_scan_x11_game_windows', return_value=None):
            self.native.auto_start_scan_supported()
            for _ in range(3):
                self.native.scan_game_windows()
        inlines = [c for c in fake.calls if c[:3] == ('kdotool', 'kwinscript', '--inline')]
        self.assertLessEqual(len(inlines), 1,
                             'an unsupported inline attempt must not repeat every poll')

    def test_failed_native_kde_scan_does_not_masquerade_as_complete_x11_scan(self):
        install_cli(self, self.native, 'old')
        incomplete = self.native.WindowScan([], None)
        with mock.patch.object(self.native, '_scan_x11_game_windows', return_value=incomplete) as x11:
            result = self.native.scan_game_windows()
        self.assertIsNone(result, 'empty XWayland results cannot establish no native Wayland games')
        x11.assert_not_called()

    def test_compatible_kdotool_capability_is_probed_and_cached(self):
        fake = install_cli(self, self.native, 'compatible')
        self.assertTrue(self.native.auto_start_scan_supported())
        probes = list(fake.calls)
        self.assertTrue(probes, 'support must be established by a capability probe')
        self.assertTrue(self.native.auto_start_scan_supported())
        self.assertEqual(fake.calls, probes)

    def test_compatible_kdotool_native_scan_works(self):
        fake = install_cli(self, self.native, 'compatible')
        self.assertTrue(self.native.auto_start_scan_supported())
        self.assertEqual(self.native.scan_game_windows(), self.native.WindowScan(
            [{'process': 'cs2', 'class': 'cs2', 'pid': 42, 'window_id': '11'}], '11'))
        self.assertTrue(any(c[:3] == ('kdotool', 'kwinscript', '--inline') for c in fake.calls))

    def assert_probe_failure(self, mode):
        fake = install_cli(self, self.native, mode)
        results = [self.native.auto_start_scan_supported()]
        probes = list(fake.calls)
        results.append(self.native.auto_start_scan_supported())
        self.assertEqual(results, [False, False])
        self.assertTrue(probes)
        self.assertEqual(fake.calls, probes)

    def test_nonzero_probe_result_reports_unsupported(self):
        self.assert_probe_failure('nonzero')

    def test_oserror_probe_reports_unsupported(self):
        self.assert_probe_failure('oserror')

    def test_timeout_probe_reports_unsupported(self):
        self.assert_probe_failure('timeout')


class KdeUnsupportedStatusContract(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        sessions.AutoStartSessionTests.setUp(self)
        self.native = fresh_window_module(self)
        install_cli(self, self.native, 'old')
        # Keep the real capability and scanner functions connected to the
        # daemon, replacing the fixture's permissive support/scan responses.
        self.auto_start_scan_supported.side_effect = self.native.auto_start_scan_supported
        self.scan.side_effect = self.native.scan_game_windows
        patcher = mock.patch.object(self.native, '_scan_x11_game_windows',
                                    return_value=self.native.WindowScan([], None))
        patcher.start()
        self.addCleanup(patcher.stop)

    cleanup_session = sessions.AutoStartSessionTests.cleanup_session
    detect = sessions.AutoStartSessionTests.detect

    async def test_daemon_reports_unsupported_for_installed_incompatible_kdotool(self):
        await self.detect()
        self.assertFalse(self.daemon._session_active)
        self.assertEqual(self.daemon._auto_start_error, 'unsupported')
