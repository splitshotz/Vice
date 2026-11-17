import re
import shutil
import subprocess
import unittest
import tempfile
from pathlib import Path
from unittest import mock

try:
    import _sandbox  # noqa: F401  keeps every store out of the real home
except ModuleNotFoundError:
    from tests import _sandbox  # noqa: F401

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10, the version Vice still supports
    import tomli as tomllib

from click.testing import CliRunner

try:
    from vice import main as main_mod
except ModuleNotFoundError:
    import sys
    import types

    stub_share = types.ModuleType("vice.share")
    stub_share.ShareServer = object
    sys.modules["vice.share"] = stub_share
    from vice import main as main_mod

from vice import __version__
from vice.main import cli


class _FakeStartDaemon:
    def __init__(self) -> None:
        self.cfg = mock.Mock()
        self.cfg.sharing.enabled = True
        self.cfg.sharing.port = 8765
        self.run_called = False

    async def run(self) -> None:
        self.run_called = True


class CliVersionTests(unittest.TestCase):
    def test_version_flag_reports_current_release(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["--version"])

        self.assertEqual(result.exit_code, 0)
        self.assertIn(f"vice, version {__version__}", result.output)

    def test_python_and_packaging_versions_stay_in_sync(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        pyproject = tomllib.loads((repo_root / "pyproject.toml").read_text())
        pkgbuild = (repo_root / "PKGBUILD").read_text()
        srcinfo = (repo_root / ".SRCINFO").read_text()

        # pyproject reads its version dynamically from vice.__version__, so
        # the Python module is the single source of truth for the wheel.
        self.assertIn("version", pyproject["project"].get("dynamic", []))
        self.assertEqual(
            pyproject["tool"]["setuptools"]["dynamic"]["version"]["attr"],
            "vice.__version__",
        )
        # PKGBUILD/.SRCINFO are a separate ecosystem; this catches drift.
        self.assertIn(f"pkgver={__version__}", pkgbuild)
        self.assertIn(f"pkgver = {__version__}", srcinfo)
        # The Flatpak's newest release entry is what Flatpak front ends show
        # as its version, so it moves with every release too.
        metainfo = (repo_root / "packaging" / "flatpak"
                    / "io.github.splitshotz.Vice.metainfo.xml").read_text()
        newest = re.search(r'<release version="([^"]+)"', metainfo)
        self.assertIsNotNone(newest, "metainfo has no <release> entry")
        self.assertEqual(newest.group(1), __version__)


class StartCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.socket_path = Path(tmp.name) / "vice.sock"
        patcher = mock.patch("vice.main.PID_FILE", Path(tmp.name) / "vice.pid")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_start_help_documents_no_open_ui_flag(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["start", "--help"])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("--open-ui / --no-open-ui", result.output)
        self.assertIn("Open the web UI in the browser on start.", result.output)

    def test_start_no_open_ui_does_not_spawn_browser(self) -> None:
        runner = CliRunner()
        daemon = _FakeStartDaemon()

        # wait_for_display has to be pinned: unpinned it shells out on a
        # headless machine, and patching subprocess.Popen here patches it for
        # the whole interpreter, so those calls land on this mock.
        with mock.patch("vice.main.normalize_runtime_environment"), \
             mock.patch("vice.main._setup_daemon_logging"), \
             mock.patch("vice.main.runtime_env_snapshot", return_value={}), \
             mock.patch("vice.main.wait_for_display"), \
             mock.patch("vice.main.SOCKET_FILE", self.socket_path), \
             mock.patch("vice.main.ViceDaemon", return_value=daemon), \
             mock.patch("vice.main.subprocess.Popen") as popen_mock:
            result = runner.invoke(cli, ["start", "--no-open-ui"])

        self.assertEqual(result.exit_code, 0)
        self.assertTrue(daemon.run_called)
        popen_mock.assert_not_called()


class DoctorCommandTests(unittest.TestCase):
    def test_doctor_reports_key_diagnostics(self) -> None:
        runner = CliRunner()
        fake_cfg = mock.Mock()
        fake_cfg.sharing.port = 8765
        fake_recorder = mock.Mock(name="wf-recorder")
        fake_recorder.name = "wf-recorder"

        with mock.patch("vice.main.load_config", return_value=fake_cfg):
            with mock.patch("vice.main.create_recorder", return_value=fake_recorder):
                with mock.patch("vice.main.runtime_env_snapshot", return_value={"WAYLAND_DISPLAY": "wayland-0"}):
                    with mock.patch(
                        "vice.main.user_systemd_env_snapshot",
                        return_value={"WAYLAND_DISPLAY": "wayland-0", "XDG_RUNTIME_DIR": "/run/user/1000"},
                    ):
                        with mock.patch("vice.main._ipc", return_value=None):
                            with mock.patch("vice.main._http_probe", return_value=(False, "connection refused")):
                                with mock.patch("vice.main._tail_text_file", return_value="line one\nline two"), \
                                        mock.patch("vice.main._doctor_log_tail", return_value="line one\nline two"), \
                                        mock.patch("vice.main._doctor_audio_lines", return_value=["  Desktop audio: on"]):
                                    with mock.patch("vice.main.shutil.which", side_effect=lambda cmd: f"/usr/bin/{cmd}"):
                                        result = runner.invoke(cli, ["doctor"])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("Vice doctor", result.output)
        self.assertIn(f"Version         : {__version__}", result.output)
        self.assertIn("Environment", result.output)
        self.assertIn("User systemd environment", result.output)
        self.assertIn("Recorder probe", result.output)
        self.assertIn("OK: Mock (wf-recorder)", result.output)
        self.assertIn("HTTP: error (connection refused) http://localhost:8765/", result.output)
        self.assertIn("Recent daemon log", result.output)
        self.assertIn("line two", result.output)


class DoctorAudioTests(unittest.TestCase):
    """A "no sound in my clips" report has to carry what was asked of the
    recorder and what the clip holds (#309)."""

    def test_the_log_tail_drops_throughput_lines_and_keeps_the_command(self) -> None:
        from vice.main import _doctor_log_tail
        with tempfile.TemporaryDirectory() as tmp:
            log_file = Path(tmp) / "vice.log"
            lines = ["10:00:00 [vice.recorder] INFO: Starting GSR: gpu-screen-recorder -a default_output"]
            lines += [f"10:00:{i:02d} [vice.recorder] INFO: gsr: gsr info: update fps: 60, damage fps: 60"
                      for i in range(1, 40)]
            lines += [f"10:01:{i:02d} [vice] INFO: event {i}" for i in range(30)]
            log_file.write_text("\n".join(lines))
            tail = _doctor_log_tail(log_file, lines=20).splitlines()
        self.assertIn("Starting GSR:", tail[0])
        self.assertEqual(len(tail), 21)
        self.assertFalse([line for line in tail if "update fps" in line])
        self.assertTrue(tail[-1].endswith("event 29"))

    def test_gsr_throughput_lines_are_recognised_with_their_prefix(self) -> None:
        from vice.recorder import _gsr_progress_line
        self.assertTrue(_gsr_progress_line("update fps: 60, damage fps: 60"))
        self.assertTrue(_gsr_progress_line("gsr info: update fps: 61, damage fps: 61"))
        self.assertFalse(_gsr_progress_line("gsr error: failed to open audio device"))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg not installed")
    def test_a_silent_track_in_the_newest_clip_is_named(self) -> None:
        from vice.config import Config
        from vice.main import _doctor_audio_lines
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "Vice_Clip_1.mp4"
            subprocess.run(
                ["ffmpeg", "-v", "error", "-y",
                 "-f", "lavfi", "-i", "color=c=black:s=64x64:r=10:d=2",
                 "-f", "lavfi", "-i", "sine=f=440:d=2",
                 "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo:d=2",
                 "-map", "0:v", "-map", "1:a", "-map", "2:a", "-t", "2",
                 "-c:v", "mpeg4", "-c:a", "aac", str(clip)], check=True)
            cfg = Config()
            cfg.output.directory = tmp
            cfg.recording.capture_microphone = True
            real_which = shutil.which
            with mock.patch("vice.main.shutil.which",
                            side_effect=lambda cmd: None if cmd == "pactl" else real_which(cmd)):
                text = "\n".join(_doctor_audio_lines(cfg))
        self.assertIn("Desktop audio: on", text)
        self.assertIn("Microphone: on", text)
        self.assertIn("Recorder audio arguments: -a ", text)
        self.assertIn("Vice_Clip_1.mp4, 2 audio track(s)", text)
        loud, silent = [line for line in text.splitlines() if "track " in line and "loudest" in line]
        self.assertNotIn("-91", loud)
        self.assertIn("loudest -91.0 dB", silent)


class SessionWaitTests(unittest.TestCase):
    """The service is wanted by default.target now, which the user manager can
    reach before the compositor exports anything (#139). Waiting for that is
    only ever right under systemd."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.socket_path = Path(tmp.name) / "vice.sock"
        patcher = mock.patch("vice.main.PID_FILE", Path(tmp.name) / "vice.pid")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_terminal_start_never_waits(self) -> None:
        runner = CliRunner()
        daemon = _FakeStartDaemon()

        with mock.patch("vice.main.normalize_runtime_environment"), \
             mock.patch("vice.main._setup_daemon_logging"), \
             mock.patch("vice.main.runtime_env_snapshot", return_value={}), \
             mock.patch("vice.main.running_under_systemd", return_value=False), \
             mock.patch("vice.main.has_display", return_value=False), \
             mock.patch("vice.main.SOCKET_FILE", self.socket_path), \
             mock.patch("vice.main.ViceDaemon", return_value=daemon), \
             mock.patch("vice.main.wait_for_display") as wait_mock:
            runner.invoke(cli, ["start", "--no-open-ui"])

        wait_mock.assert_not_called()

    def test_systemd_start_without_a_display_waits(self) -> None:
        runner = CliRunner()
        daemon = _FakeStartDaemon()

        with mock.patch("vice.main.normalize_runtime_environment"), \
             mock.patch("vice.main._setup_daemon_logging"), \
             mock.patch("vice.main.runtime_env_snapshot", return_value={}), \
             mock.patch("vice.main.running_under_systemd", return_value=True), \
             mock.patch("vice.main.has_display", return_value=False), \
             mock.patch("vice.main.SOCKET_FILE", self.socket_path), \
             mock.patch("vice.main.ViceDaemon", return_value=daemon), \
             mock.patch("vice.main.wait_for_display") as wait_mock:
            runner.invoke(cli, ["start", "--no-open-ui"])

        wait_mock.assert_called_once()

    def test_systemd_start_with_a_display_does_not_wait(self) -> None:
        runner = CliRunner()
        daemon = _FakeStartDaemon()

        with mock.patch("vice.main.normalize_runtime_environment"), \
             mock.patch("vice.main._setup_daemon_logging"), \
             mock.patch("vice.main.runtime_env_snapshot", return_value={}), \
             mock.patch("vice.main.running_under_systemd", return_value=True), \
             mock.patch("vice.main.has_display", return_value=True), \
             mock.patch("vice.main.SOCKET_FILE", self.socket_path), \
             mock.patch("vice.main.ViceDaemon", return_value=daemon), \
             mock.patch("vice.main.wait_for_display") as wait_mock:
            runner.invoke(cli, ["start", "--no-open-ui"])

        wait_mock.assert_not_called()


class ServiceFileLookupTests(unittest.TestCase):
    """Doctor used to look only in ~/.config/systemd/user, so a package install
    that put the unit in /usr/lib reported "(not installed)" while the service
    sat there enabled (#139)."""

    def test_finds_system_installed_unit(self) -> None:
        from vice.main import _installed_service_file

        system_path = Path("/usr/lib/systemd/user/vice.service")
        with mock.patch.object(Path, "exists", lambda self: self == system_path):
            self.assertEqual(_installed_service_file(), system_path)

    def test_prefers_the_users_own_unit(self) -> None:
        from vice.main import _installed_service_file
        from vice.runtime import actual_home_dir

        user_path = actual_home_dir() / ".config" / "systemd" / "user" / "vice.service"
        with mock.patch.object(Path, "exists", lambda self: True):
            self.assertEqual(_installed_service_file(), user_path)

    def test_none_when_nothing_is_installed(self) -> None:
        from vice.main import _installed_service_file

        with mock.patch.object(Path, "exists", lambda self: False):
            self.assertIsNone(_installed_service_file())


class ServiceUnitTests(unittest.TestCase):
    def test_unit_is_wanted_by_default_target(self) -> None:
        # graphical-session.target alone left the unit enabled but never
        # started on compositors that do not activate it (#139).
        unit = (Path(__file__).resolve().parent.parent / "packaging" / "vice.service").read_text()
        wanted = [l for l in unit.splitlines() if l.startswith("WantedBy=")]
        self.assertEqual(len(wanted), 1)
        self.assertIn("default.target", wanted[0])
        self.assertIn("graphical-session.target", wanted[0])


class UninstallCommandTests(unittest.TestCase):
    """Every test here that goes through CliRunner pins
    normalize_runtime_environment. The group callback runs it on every command,
    and on a machine with no display it asks systemd for one, which shows up as
    an extra subprocess.run these tests then count."""

    def test_aur_detection_checks_package_ownership_of_vice_binary(self) -> None:
        with mock.patch("vice.main.shutil.which", side_effect=["/usr/bin/pacman", "/usr/bin/vice"]):
            with mock.patch("vice.main.subprocess.run") as run_mock:
                run_mock.side_effect = [
                    mock.Mock(returncode=0, stdout="vice-clipper 1.0.17-1\n"),
                    mock.Mock(returncode=0, stdout="/usr/bin/vice is owned by vice-clipper 1.0.17-1\n"),
                ]
                detected = main_mod._installed_via_aur()

        self.assertTrue(detected)
        self.assertEqual(run_mock.call_count, 2)

    def test_aur_install_returns_early_with_instruction(self) -> None:
        runner = CliRunner()
        with mock.patch("vice.main.normalize_runtime_environment"):
            with mock.patch("vice.main._installed_via_aur", return_value=True):
                with mock.patch("vice.main._ipc") as ipc_mock:
                    with mock.patch("vice.main.subprocess.run") as run_mock:
                        result = runner.invoke(cli, ["uninstall", "--yes"])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("Vice was installed via AUR.", result.output)
        self.assertIn("yay -Rns vice-clipper", result.output)
        ipc_mock.assert_not_called()
        run_mock.assert_not_called()

    def test_nix_detection_follows_the_vice_binary_into_the_store(self) -> None:
        store = Path("/nix/store/abc123-vice-clipper-2.14.1/bin/vice")
        with mock.patch("vice.main._vice_command_path", return_value=store):
            self.assertTrue(main_mod._installed_via_nix())
        with mock.patch("vice.main._vice_command_path", return_value=Path("/usr/bin/vice")):
            self.assertFalse(main_mod._installed_via_nix())
        with mock.patch("vice.main._vice_command_path", return_value=None):
            self.assertFalse(main_mod._installed_via_nix())

    def test_nix_install_returns_early_without_touching_the_store(self) -> None:
        runner = CliRunner()
        with mock.patch("vice.main.normalize_runtime_environment"), \
             mock.patch("vice.main._installed_via_aur", return_value=False), \
             mock.patch("vice.main._installed_via_nix", return_value=True), \
             mock.patch("vice.main._ipc") as ipc_mock, \
             mock.patch("vice.main.subprocess.run") as run_mock:
            result = runner.invoke(cli, ["uninstall", "--yes"])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("installed by Nix", result.output)
        ipc_mock.assert_not_called()
        run_mock.assert_not_called()

    def test_user_site_uninstall_uses_pip_and_skips_desktop_cache_refresh_without_files(self) -> None:
        runner = CliRunner()
        with mock.patch("vice.main.normalize_runtime_environment"), \
             mock.patch("vice.main._installed_via_aur", return_value=False), \
             mock.patch("vice.main._installed_via_nix", return_value=False), \
             mock.patch("vice.main.SOCKET_FILE", Path("/tmp/does-not-exist.sock")), \
             mock.patch("vice.main.actual_home_dir", return_value=Path("/tmp/vice-test-home")), \
             mock.patch("vice.main.CONFIG_DIR", Path("/tmp/does-not-exist-config")), \
             mock.patch("vice.main.CONFIG_PATH", Path("/tmp/does-not-exist-config.toml")), \
             mock.patch("vice.main.load_config") as load_config_mock, \
             mock.patch("vice.main._using_install_script_venv", return_value=False), \
             mock.patch("vice.main._remove_local_install_artifacts", return_value=[]), \
             mock.patch("vice.main._refresh_desktop_caches") as refresh_mock, \
             mock.patch("vice.main.subprocess.run") as run_mock:
            result = runner.invoke(cli, ["uninstall", "--yes"])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("Uninstalling Python package", result.output)
        load_config_mock.assert_not_called()
        run_mock.assert_called_once()
        self.assertEqual(run_mock.call_args.args[0][1:], ["-m", "pip", "uninstall", "vice", "-y"])
        refresh_mock.assert_not_called()

    def test_install_script_venv_uninstall_removes_venv_without_pip(self) -> None:
        runner = CliRunner()
        with mock.patch("vice.main.normalize_runtime_environment"), \
             mock.patch("vice.main._installed_via_aur", return_value=False), \
             mock.patch("vice.main._installed_via_nix", return_value=False), \
             mock.patch("vice.main.SOCKET_FILE", Path("/tmp/does-not-exist.sock")), \
             mock.patch("vice.main.actual_home_dir", return_value=Path("/tmp/vice-test-home")), \
             mock.patch("vice.main.CONFIG_DIR", Path("/tmp/does-not-exist-config")), \
             mock.patch("vice.main.CONFIG_PATH", Path("/tmp/does-not-exist-config.toml")), \
             mock.patch("vice.main.load_config") as load_config_mock, \
             mock.patch("vice.main._using_install_script_venv", return_value=True), \
             mock.patch("vice.main.shutil.rmtree") as rmtree_mock, \
             mock.patch(
                 "vice.main._remove_local_install_artifacts",
                 return_value=[Path("/home/test/.local/bin/vice")],
             ), \
             mock.patch("vice.main._refresh_desktop_caches") as refresh_mock, \
             mock.patch("vice.main.subprocess.run") as run_mock:
            result = runner.invoke(cli, ["uninstall", "--yes"])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("Removing Vice virtual environment", result.output)
        self.assertIn("Removed local Vice install files", result.output)
        load_config_mock.assert_not_called()
        rmtree_mock.assert_called_once_with(mock.ANY, ignore_errors=True)
        run_mock.assert_not_called()
        refresh_mock.assert_called_once_with()


class InstallMethodTests(unittest.TestCase):
    def test_a_pacman_that_cannot_run_is_not_the_aur(self) -> None:
        # CI and the Nix sandbox have no pacman; a which() that claims one
        # used to crash vice doctor there.
        from vice import main as main_mod
        with mock.patch("vice.main.shutil.which", side_effect=lambda cmd: f"/nonexistent/{cmd}"):
            self.assertFalse(main_mod._installed_via_aur())
