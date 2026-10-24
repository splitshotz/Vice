"""Running the Flatpak gpu-screen-recorder (#247).

The process behaviour these guard was measured against the real Flatpak in a
container: flatpak run execs bwrap, SIGUSR1 to bwrap kills the sandbox, and
bwrap exits on SIGTERM while the recorder inside is still finishing.
"""

import asyncio
import os
import signal
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

try:
    import _sandbox  # noqa: F401  keeps every store out of the real home
except ModuleNotFoundError:
    from tests import _sandbox  # noqa: F401

from vice import recorder
from vice.config import Config


class SourceSelectionTests(unittest.TestCase):
    def tearDown(self) -> None:
        recorder.set_gsr_source("auto")
        recorder.gsr_flatpak_installed.cache_clear()

    def _select(self, pref, native, flatpak):
        recorder.set_gsr_source(pref)
        recorder.gsr_flatpak_installed.cache_clear()
        has = lambda tool: {"gpu-screen-recorder": native, "flatpak": flatpak}.get(tool, False)
        run = lambda cmd, timeout: (0 if flatpak else 1, "")
        with mock.patch.object(recorder, "_has", side_effect=has), \
             mock.patch.object(recorder, "_run_command_capture", side_effect=run):
            return recorder.gsr_source(), recorder._gsr_prefix()

    def test_auto_keeps_the_distro_build_when_it_is_there(self):
        self.assertEqual(self._select("auto", True, True), ("native", ["gpu-screen-recorder"]))

    def test_auto_falls_back_to_the_flatpak(self):
        self.assertEqual(self._select("auto", False, True),
                         ("flatpak", recorder.GSR_FLATPAK_PREFIX))

    def test_an_explicit_choice_is_honoured_or_reports_nothing(self):
        self.assertEqual(self._select("flatpak", True, True)[0], "flatpak")
        self.assertEqual(self._select("flatpak", True, False), (None, None))
        self.assertEqual(self._select("native", False, True), (None, None))

    def test_the_recorder_command_runs_through_flatpak(self):
        recorder.set_gsr_source("flatpak")
        with mock.patch.object(recorder, "gsr_source", return_value="flatpak"), \
             mock.patch.object(recorder, "_gsr_help_text", return_value=""), \
             mock.patch.object(recorder, "_gsr_supported_codecs", return_value=frozenset()):
            cmd = recorder.GSRRecorder(Config())._build_cmd()
        self.assertEqual(cmd[:4], recorder.GSR_FLATPAK_PREFIX)
        self.assertEqual(recorder._program_name(cmd), "gpu-screen-recorder")


class ReaperMatchTests(unittest.TestCase):
    def test_bwrap_command_line_matches_the_registered_flatpak_run(self):
        argv = [*recorder.GSR_FLATPAK_PREFIX, "-w", "screen", "-r", "60", "-o", "/v"]
        running = ["bwrap", "--args", "70", "--", "gpu-screen-recorder",
                   "-w", "screen", "-r", "60", "-o", "/v"]
        self.assertTrue(recorder._argv_matches(running, argv))
        self.assertFalse(recorder._argv_matches(running[:-1] + ["/elsewhere"], argv))
        self.assertFalse(recorder._argv_matches(["bwrap", "--", "something-else"], argv))
        # A native capture still has to match exactly.
        native = ["gpu-screen-recorder", "-w", "screen"]
        self.assertTrue(recorder._argv_matches(native, native))
        self.assertFalse(recorder._argv_matches(["gpu-screen-recorder"], native))


# A stand-in for bwrap: it starts a child called gpu-screen-recorder and dies
# on SIGTERM straight away, while the child takes a while to finish its file.
_SANDBOX = textwrap.dedent('''
    import os, signal, subprocess, sys, time
    child = subprocess.Popen([sys.argv[1], sys.argv[2]])
    signal.signal(signal.SIGTERM, lambda *_: os._exit(143))
    signal.signal(signal.SIGUSR1, lambda *_: os._exit(138))
    child.wait()
''')
_RECORDER = textwrap.dedent('''\
    #!{python}
    import signal, sys, time
    out = sys.argv[1]
    def finish(*_):
        time.sleep(1.0)
        open(out, "a").write("finished\\n")
        sys.exit(0)
    signal.signal(signal.SIGTERM, finish)
    signal.signal(signal.SIGUSR1, lambda *_: open(out, "a").write("saved\\n"))
    while True:
        time.sleep(0.05)
''')


class SandboxedRecorderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.out = root / "events.txt"
        fake = root / "gpu-screen-recorder"
        fake.write_text(_RECORDER.format(python=sys.executable))
        fake.chmod(0o755)
        sandbox = root / "bwrap.py"
        sandbox.write_text(_SANDBOX)
        registry = mock.patch.object(recorder, "CAPTURE_REGISTRY", root / "capture.json")
        registry.start()
        self.addCleanup(registry.stop)
        self.proc = await recorder._spawn_capture(
            [sys.executable, str(sandbox), str(fake), str(self.out)], asyncio.subprocess.DEVNULL)
        self.proc.vice_flatpak = True
        self.addAsyncCleanup(self._kill)

    async def _kill(self) -> None:
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    async def test_save_signals_the_recorder_not_the_sandbox(self):
        inner = await recorder._recorder_pid(self.proc)
        self.assertIsNotNone(inner)
        self.assertNotEqual(inner, self.proc.pid)
        os.kill(inner, signal.SIGUSR1)
        for _ in range(50):
            await asyncio.sleep(0.05)
            if self.out.exists():
                break
        self.assertEqual(self.out.read_text(), "saved\n")
        self.assertIsNone(self.proc.returncode)

    async def test_stop_waits_for_the_recorder_to_finish(self):
        await recorder._recorder_pid(self.proc)
        await recorder._terminate_group(self.proc, timeout=5)
        self.assertEqual(self.out.read_text(), "finished\n")
