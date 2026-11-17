"""Vice running as its own Flatpak (#97).

Inside the Flatpak the recorder, compositor tools and window pids are on the
host, reached through flatpak-spawn. These tests put a stand-in flatpak-spawn
on PATH that runs the host command locally, so the real code paths run end to
end, and check that nothing changes when Vice is not the Flatpak.
"""

import asyncio
import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

try:
    import _sandbox  # noqa: F401  keeps every store out of the real home
except ModuleNotFoundError:
    from tests import _sandbox  # noqa: F401

from vice import active_window, gsr_install, platform_info, recorder, runtime
from vice.config import Config

FLATPAK_ENV = {"FLATPAK_ID": runtime.VICE_FLATPAK_ID}

# Drops flatpak-spawn's own options and runs the rest, as the host would.
_FAKE_SPAWN = textwrap.dedent("""\
    #!/bin/sh
    while [ $# -gt 0 ]; do
      case "$1" in
        --host|--watch-bus|--env=*) shift ;;
        *) break ;;
      esac
    done
    exec "$@"
""")


class _FakeHost(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bin = Path(self.tmp.name) / "bin"
        self.bin.mkdir()
        spawn = self.bin / "flatpak-spawn"
        spawn.write_text(_FAKE_SPAWN)
        spawn.chmod(0o755)
        env = {**FLATPAK_ENV, "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}"}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake_tool(self, name: str, script: str) -> Path:
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + script)
        path.chmod(0o755)
        return path


class OutsideTheFlatpakTests(unittest.TestCase):
    """Every helper is a no-op for the AUR, install.sh and Nix installs."""

    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("FLATPAK_ID", None)

    def test_host_argv_is_unchanged(self) -> None:
        self.assertFalse(runtime.in_vice_flatpak())
        self.assertEqual(runtime.host_argv(["hyprctl", "activewindow"]), ["hyprctl", "activewindow"])
        self.assertEqual(runtime.host_argv(["x"], watch=True), ["x"])

    def test_another_flatpak_is_not_vice(self) -> None:
        with mock.patch.dict(os.environ, {"FLATPAK_ID": "org.example.Other"}):
            self.assertFalse(runtime.in_vice_flatpak())

    def test_proc_is_read_directly(self) -> None:
        self.assertEqual(runtime.read_host_proc(os.getpid(), "comm"),
                         Path(f"/proc/{os.getpid()}/comm").read_bytes())

    def test_the_gsr_flatpak_prefix_is_unchanged(self) -> None:
        recorder.set_gsr_source("flatpak")
        self.addCleanup(recorder.set_gsr_source, "auto")
        with mock.patch.object(recorder, "gsr_flatpak_installed", return_value=True):
            self.assertEqual(recorder._gsr_prefix(), recorder.GSR_FLATPAK_PREFIX)


class HostHelperTests(_FakeHost):
    def test_host_argv_carries_the_compositor_variables(self) -> None:
        with mock.patch.dict(os.environ, {"HYPRLAND_INSTANCE_SIGNATURE": "abc"}):
            argv = runtime.host_argv(["hyprctl", "activewindow"], watch=True)
        self.assertEqual(argv[:3], ["flatpak-spawn", "--host", "--watch-bus"])
        self.assertIn("--env=HYPRLAND_INSTANCE_SIGNATURE=abc", argv)
        self.assertEqual(argv[-2:], ["hyprctl", "activewindow"])

    def test_proc_is_read_on_the_host(self) -> None:
        self.assertEqual(runtime.read_host_proc(os.getpid(), "comm"),
                         Path(f"/proc/{os.getpid()}/comm").read_bytes())
        self.assertIsNone(runtime.read_host_proc(2 ** 22 + 7, "comm"))

    def test_host_which(self) -> None:
        self.fake_tool("kdotool", "exit 0\n")
        self.assertTrue(runtime.host_which("kdotool"))
        self.assertFalse(runtime.host_which("surely-not-a-real-tool-xyz"))

    def test_systemd_is_never_used_inside(self) -> None:
        self.assertFalse(runtime.systemd_unit_loaded())

    def test_compositor_tools_cross_to_the_host(self) -> None:
        self.fake_tool("hyprctl", 'echo "host:$*"\n')
        self.assertEqual(active_window._run(["hyprctl", "activewindow", "-j"]).strip(),
                         "host:activewindow -j")

    def test_window_process_names_come_from_the_host(self) -> None:
        self.assertEqual(active_window._read_proc_comm(os.getpid()),
                         Path(f"/proc/{os.getpid()}/comm").read_text().strip())


class FlatpakRecorderTests(_FakeHost):
    def setUp(self) -> None:
        super().setUp()
        recorder.refresh_gsr_probes()
        self.addCleanup(recorder.refresh_gsr_probes)

    def test_only_the_gsr_flatpak_can_record(self) -> None:
        with mock.patch.object(recorder, "gsr_flatpak_installed", return_value=True), \
             mock.patch.object(recorder, "_has", return_value=True):
            self.assertEqual(recorder.gsr_source(), "flatpak")
            prefix = recorder._gsr_prefix()
        self.assertEqual(prefix[:3], ["flatpak-spawn", "--host", "--watch-bus"])
        self.assertEqual(prefix[-len(recorder.GSR_FLATPAK_PREFIX):], recorder.GSR_FLATPAK_PREFIX)
        with mock.patch.object(recorder, "gsr_flatpak_installed", return_value=False):
            self.assertIsNone(recorder.gsr_source())

    def test_the_gsr_flatpak_is_asked_for_on_the_host(self) -> None:
        self.fake_tool("flatpak", '[ "$1" = info ] && [ "$2" = com.dec05eba.gpu_screen_recorder ]\n')
        self.assertTrue(recorder.gsr_flatpak_installed())

    def test_commands_still_name_the_recorder(self) -> None:
        cmd = [*runtime.host_argv(recorder.GSR_FLATPAK_PREFIX, watch=True), "-w", "screen"]
        self.assertEqual(recorder._program_name(cmd), "gpu-screen-recorder")
        self.assertTrue(recorder._is_flatpak_capture(cmd))
        self.assertEqual(recorder._gsr_args_after_prefix(cmd), ["-w", "screen"])

    def test_the_host_recorder_is_found_by_its_exact_command_line(self) -> None:
        # A symlink, not a copy: the kernel names the process after the link
        # (comm "gpu-screen-reco"), and a copied coreutils multi-call binary,
        # as in the Nix sandbox, would not know what to run.
        fake = Path(self.tmp.name) / "gpu-screen-recorder"
        fake.symlink_to(sys.executable)
        # The comment makes the command line this run's own: two suites on
        # one machine each found the other's process.
        args = ["-c", f"import time; time.sleep(30)  # {os.getpid()} {self.tmp.name}"]
        proc = subprocess.Popen(["gpu-screen-recorder", *args], executable=str(fake))
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        found = None
        for _ in range(100):
            found = recorder._host_recorder_pid(args)
            if found:
                break
            time.sleep(0.05)
        self.assertEqual(found, proc.pid)
        self.assertIsNone(recorder._host_recorder_pid(["-c", "something else"]))

    def test_signals_go_through_the_host(self) -> None:
        child = subprocess.Popen(["sleep", "30"])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        proc = mock.Mock(vice_host_args=["-w", "screen"])
        self.assertTrue(recorder._recorder_alive(proc, child.pid))
        recorder._kill_recorder(proc, child.pid, signal.SIGTERM)
        child.wait(timeout=5)
        self.assertFalse(recorder._recorder_alive(proc, child.pid))
        with self.assertRaises(ProcessLookupError):
            recorder._kill_recorder(proc, child.pid, signal.SIGUSR1)


class MissingGsrInsideTests(_FakeHost, unittest.IsolatedAsyncioTestCase):
    """Found by running the real Flatpak: without the GSR Flatpak the daemon
    died while starting, so the setup step that installs it never showed."""

    async def test_the_daemon_keeps_a_recorder_that_says_why(self) -> None:
        with mock.patch.object(recorder, "gsr_flatpak_installed", return_value=False):
            rec = recorder.create_recorder(Config())
            self.assertIsInstance(rec, recorder.GSRRecorder)
            with self.assertRaisesRegex(RuntimeError, "gpu-screen-recorder Flatpak is not installed"):
                await rec.start()

    def test_outside_the_flatpak_it_is_still_an_error(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "FLATPAK_ID"}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(recorder, "gsr_source", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "required by Vice's auto-backend"):
                recorder.create_recorder(Config())


class SpawnMarksHostCapturesTests(_FakeHost, unittest.IsolatedAsyncioTestCase):
    async def test_a_capture_started_inside_is_marked_as_a_host_process(self) -> None:
        with mock.patch.object(recorder, "CAPTURE_REGISTRY", Path(self.tmp.name) / "capture.json"):
            cmd = [*runtime.host_argv(recorder.GSR_FLATPAK_PREFIX, watch=True), "-o", "/x"]
            # The fake host has no flatpak, so the command fails at once; the
            # marking is what is under test.
            proc = await recorder._spawn_capture(cmd, asyncio.subprocess.DEVNULL)
            await proc.wait()
            recorder._unregister_capture(proc.pid)
        self.assertTrue(proc.vice_flatpak)
        self.assertEqual(proc.vice_host_args, ["-o", "/x"])


class GsrInstallProgressTests(unittest.TestCase):
    def test_steps_fold_into_one_percent(self) -> None:
        state: dict = {}
        self.assertIsNone(gsr_install.parse_progress("Looking for matches…", state))
        self.assertEqual(gsr_install.parse_progress("Installing 1/2… ███ 50%", state), 25)
        self.assertEqual(gsr_install.parse_progress("Installing 1/2… 100%", state), 50)
        self.assertEqual(gsr_install.parse_progress("Installing 2/2… 40%", state), 70)
        self.assertEqual(gsr_install.parse_progress("Installing 2/2… 100%", state), 100)

    def test_progress_never_goes_backwards(self) -> None:
        state: dict = {}
        gsr_install.parse_progress("Installing 1/1 80%", state)
        self.assertEqual(gsr_install.parse_progress("Installing 1/1 10%", state), 80)


class GsrInstallTests(_FakeHost, unittest.IsolatedAsyncioTestCase):
    async def _install(self):
        seen = []

        async def progress(phase, percent):
            seen.append((phase, percent))

        return await gsr_install.install_gsr_flatpak(progress), seen

    async def test_a_successful_install_reports_progress(self) -> None:
        self.fake_tool("flatpak", textwrap.dedent("""\
            case "$1" in
              remotes) echo "https://dl.flathub.org/repo/" ;;
              install) printf 'Installing 1/1\\r 30%%\\r 100%%\\n'; echo "$*" > "$0.args" ;;
            esac
        """))
        problem, seen = await self._install()
        self.assertIsNone(problem)
        self.assertEqual(seen[0], ("preparing", None))
        self.assertEqual(seen[-1], ("installing", 100))
        args = (self.bin / "flatpak.args").read_text()
        self.assertIn("--system", args)
        self.assertIn("com.dec05eba.gpu_screen_recorder", args)

    async def test_flathub_is_added_when_missing(self) -> None:
        self.fake_tool("flatpak", textwrap.dedent("""\
            echo "$1" >> "$0.calls"
            exit 0
        """))
        problem, _ = await self._install()
        self.assertIsNone(problem)
        self.assertEqual((self.bin / "flatpak.calls").read_text().split(),
                         ["remotes", "remote-add", "install"])

    async def test_a_failure_returns_flatpaks_reason(self) -> None:
        self.fake_tool("flatpak", textwrap.dedent("""\
            case "$1" in
              remotes) echo "https://dl.flathub.org/repo/" ;;
              install) echo "error: Unable to connect to dl.flathub.org"; exit 1 ;;
            esac
        """))
        problem, _ = await self._install()
        self.assertEqual(problem, "error: Unable to connect to dl.flathub.org")


class PlatformInfoTests(unittest.TestCase):
    def _detect(self, files: dict, env: dict | None = None, packaging=None) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for rel, text in files.items():
                path = root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
            platform_info.detect_platform.cache_clear()
            self.addCleanup(platform_info.detect_platform.cache_clear)
            base_env = {k: v for k, v in os.environ.items()
                        if k not in ("FLATPAK_ID", "GAMESCOPE_WAYLAND_DISPLAY", "XDG_CURRENT_DESKTOP")}
            with mock.patch.object(platform_info, "_ROOT", root), \
                 mock.patch.dict(os.environ, {**base_env, **(env or {})}, clear=True):
                return platform_info.detect_platform(packaging)

    def test_steam_deck_in_game_mode_through_the_flatpak(self) -> None:
        info = self._detect({
            "sys/class/dmi/id/board_vendor": "Valve\n",
            "sys/class/dmi/id/product_name": "Jupiter\n",
            "etc/os-release": 'ID=org.freedesktop.platform\n',
            "run/host/os-release": 'ID=steamos\nVARIANT_ID=steamdeck\n',
        }, {**FLATPAK_ENV, "GAMESCOPE_WAYLAND_DISPLAY": "gamescope-0"})
        self.assertEqual(info, {"device": "steamdeck", "os_id": "steamos", "variant": "steamdeck",
                                "immutable": True, "packaging": "flatpak", "session": "gamescope"})

    def test_oled_deck(self) -> None:
        info = self._detect({"sys/class/dmi/id/board_vendor": "Valve",
                             "sys/class/dmi/id/product_name": "Galileo"})
        self.assertEqual(info["device"], "steamdeck")

    def test_bazzite_and_fedora_atomic_are_immutable(self) -> None:
        self.assertTrue(self._detect({"etc/os-release": "ID=bazzite\nID_LIKE=fedora\n"})["immutable"])
        info = self._detect({"etc/os-release": "ID=fedora\nVARIANT_ID=kinoite\n",
                             "run/ostree-booted": ""})
        self.assertTrue(info["immutable"])
        self.assertEqual(info["variant"], "kinoite")

    def test_arch_desktop(self) -> None:
        info = self._detect({"etc/os-release": 'ID=arch\n',
                             "sys/class/dmi/id/board_vendor": "ASUSTeK"},
                            {"XDG_CURRENT_DESKTOP": "Hyprland"}, packaging=lambda: "aur")
        self.assertEqual(info, {"device": "desktop", "os_id": "arch", "variant": "",
                                "immutable": False, "packaging": "aur", "session": "Hyprland"})


class FlatpakDaemonTests(unittest.TestCase):
    def test_the_update_hint_names_flatpak(self) -> None:
        from vice.main import ViceDaemon
        daemon = ViceDaemon.__new__(ViceDaemon)
        with mock.patch.dict(os.environ, FLATPAK_ENV):
            hint = daemon._update_install_hint()
        self.assertEqual(hint, {"method": "flatpak",
                                "command": f"flatpak update {runtime.VICE_FLATPAK_ID}"})

    def test_install_gsr_only_starts_inside(self) -> None:
        from vice.main import ViceDaemon
        daemon = ViceDaemon.__new__(ViceDaemon)
        daemon._gsr_install_task = None
        env = {k: v for k, v in os.environ.items() if k != "FLATPAK_ID"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertFalse(daemon._install_gsr())

    def test_uninstall_points_at_flatpak(self) -> None:
        from click.testing import CliRunner
        from vice import main as main_mod
        with mock.patch.dict(os.environ, FLATPAK_ENV), \
             mock.patch.object(main_mod, "normalize_runtime_environment"):
            result = CliRunner().invoke(main_mod.cli, ["uninstall", "--yes"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn(f"flatpak uninstall {runtime.VICE_FLATPAK_ID}", result.output)


try:
    from vice.share import ShareServer
except ImportError:  # aiohttp missing
    ShareServer = None


@unittest.skipUnless(ShareServer is not None, "aiohttp missing")
class InstallGsrEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_refused_outside_the_flatpak_and_started_inside(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config()
            cfg.output.directory = tmp
            server = ShareServer(cfg)
            resp = await server._api_install_gsr(None)
            self.assertEqual(resp.status, 409)
            server.install_gsr_cb = lambda: True
            resp = await server._api_install_gsr(None)
            self.assertEqual(resp.status, 200)


# Refuses Wayland the way the real overlay does when the compositor hides
# layer-shell from the sandbox, and records the environment it ran under.
_NO_LAYER_SHELL_OVERLAY = textwrap.dedent("""\
    import json, os, sys, time
    log = sys.argv[1]
    if os.environ.get("WAYLAND_DISPLAY"):
        print(json.dumps({"t": "error", "reason": "no-layer-shell"}), flush=True)
        sys.exit(3)
    with open(log, "w") as fh:
        json.dump({"gdk": os.environ.get("GDK_BACKEND")}, fh)
    while True:
        time.sleep(0.05)
""")


class OverlayFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def _host(self, env: dict):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        script = Path(tmp.name) / "overlay.py"
        script.write_text(_NO_LAYER_SHELL_OVERLAY)
        self.log = Path(tmp.name) / "env.json"
        from vice.overlay_host import KeyOverlayHost
        host = KeyOverlayHost(lambda x, y: None, lambda: None,
                              command=[sys.executable, str(script), str(self.log)])
        self.addAsyncCleanup(host.stop)
        patches = [mock.patch("vice.overlay_host.has_display", return_value=True),
                   mock.patch.dict(os.environ, env)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        await host.sync(True, {"position": "bottom-left"}, "")
        for _ in range(100):
            await asyncio.sleep(0.03)
            if self.log.exists() or host.state()["error"]:
                break
        return host

    async def test_inside_the_flatpak_it_falls_back_to_xwayland(self) -> None:
        host = await self._host({**FLATPAK_ENV, "WAYLAND_DISPLAY": "wayland-1", "DISPLAY": ":0"})
        self.assertTrue(self.log.exists(), host.state())
        self.assertEqual(json.loads(self.log.read_text()), {"gdk": "x11"})
        self.assertIsNone(host.state()["error"])

    async def test_outside_the_flatpak_it_still_reports_no_layer_shell(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "FLATPAK_ID"}
        env.update({"WAYLAND_DISPLAY": "wayland-1", "DISPLAY": ":0"})
        with mock.patch.dict(os.environ, env, clear=True):
            host = await self._host({})
        self.assertFalse(self.log.exists())
        self.assertEqual(host.state()["error"], "no-layer-shell")


if __name__ == "__main__":
    unittest.main()
