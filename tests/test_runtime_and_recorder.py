import asyncio
import json
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import wave
from datetime import datetime
from pathlib import Path
from unittest import mock

try:
    import _sandbox  # noqa: F401  keeps every store out of the real home
except ModuleNotFoundError:
    from tests import _sandbox  # noqa: F401

from vice import app as app_mod
from vice import audio as audio_mod
from vice import config as config_mod
from vice import main as main_mod
from vice import media as media_mod
from vice import recorder as recorder_mod
from vice import share as share_mod
from vice.main import _RECORDER_DEATH_BACKOFF_AFTER
from vice.config import Config, HotkeyClipPreset, HotkeyConfig, OutputConfig, RecordingConfig, SharingConfig
from vice.recorder import (
    CAPTURE_REGISTRY,
    GSRRecorder,
    SegmentRecorder,
    _classify_gsr_source,
    _process_argv,
    _read_capture_registry,
    _register_capture,
    _unregister_capture,
    _write_capture_registry,
    reap_orphaned_captures,
    _gsr_audio_args,
    _gsr_capture_args,
    _gsr_codec_args,
    _gsr_codec_choice,
    _gsr_supported_codecs,
    _gsr_wants_disk_replay,
    _is_wayland,
    _wf_audio_device,
    create_recorder,
    list_display_options,
    list_gsr_audio_sources,
    _wait_for_finalized_clip,
    _encoder_flags,
    _gsr_codec_for_encoder,
    _next_clip_path,
    _render_clip_name,
    slugify_clip_name,
)
from vice.runtime import (
    _wayland_runtime_dir_candidates,
    actual_home_dir,
    normalize_runtime_environment,
)


try:
    from vice.share import ShareServer
except ModuleNotFoundError:
    ShareServer = None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _stub_ffprobe(_: Path) -> dict:
    return {"width": 1920, "height": 1080, "duration": 4.2}


class RuntimeEnvironmentTests(unittest.TestCase):
    def test_normalize_runtime_environment_replaces_unexpanded_service_values(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"HOME": "${HOME}", "XDG_RUNTIME_DIR": "/run/user/$(id -u)"},
            clear=False,
        ):
            with mock.patch("vice.runtime.shutil.which", return_value=None):
                normalize_runtime_environment()
            self.assertEqual(os.environ["HOME"], str(actual_home_dir()))
            self.assertEqual(os.environ["XDG_RUNTIME_DIR"], f"/run/user/{os.getuid()}")

    def test_normalize_runtime_environment_loads_display_vars_from_systemd(self) -> None:
        systemd_env = "\n".join(
            [
                "WAYLAND_DISPLAY=wayland-1",
                f"XDG_RUNTIME_DIR=/run/user/{os.getuid()}",
                "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus",
                "XDG_SESSION_TYPE=wayland",
            ]
        )
        with mock.patch.dict(os.environ, {"HOME": "${HOME}"}, clear=True):
            with mock.patch("vice.runtime.shutil.which", return_value="/usr/bin/systemctl"):
                with mock.patch("vice.runtime.subprocess.check_output", return_value=systemd_env):
                    normalize_runtime_environment()
            self.assertEqual(os.environ["HOME"], str(actual_home_dir()))
            self.assertEqual(os.environ["WAYLAND_DISPLAY"], "wayland-1")
            self.assertEqual(os.environ["XDG_RUNTIME_DIR"], f"/run/user/{os.getuid()}")
            self.assertEqual(os.environ["XDG_SESSION_TYPE"], "wayland")

    def test_wayland_runtime_dir_candidates_include_tmp_fallback(self) -> None:
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}"}, clear=True):
            candidates = _wayland_runtime_dir_candidates()

        self.assertIn(Path(f"/run/user/{os.getuid()}"), candidates)
        self.assertIn(Path(f"/tmp/wayland-{os.getuid()}"), candidates)

    def test_normalize_runtime_environment_recovers_wayland_socket_without_systemd(self) -> None:
        runtime_dir = mock.MagicMock()
        runtime_dir.exists.return_value = True
        runtime_dir.__str__.return_value = "/tmp/vice-runtime"
        candidate = mock.MagicMock()
        candidate.name = "wayland-9"
        candidate.stat.return_value = mock.Mock(st_mode=stat.S_IFSOCK)
        runtime_dir.glob.return_value = [candidate]

        with mock.patch.dict(
            os.environ,
            {"HOME": "${HOME}", "XDG_RUNTIME_DIR": "/run/user/$(id -u)"},
            clear=True,
        ):
            with mock.patch("vice.runtime.shutil.which", return_value=None):
                with mock.patch(
                    "vice.runtime._wayland_runtime_dir_candidates",
                    return_value=[runtime_dir],
                ):
                    normalize_runtime_environment()
            self.assertEqual(os.environ["HOME"], str(actual_home_dir()))
            self.assertEqual(os.environ["WAYLAND_DISPLAY"], "wayland-9")
            self.assertEqual(os.environ["XDG_RUNTIME_DIR"], "/tmp/vice-runtime")

    def test_normalize_runtime_environment_leaves_display_unset_without_socket(self) -> None:
        runtime_dir = mock.MagicMock()
        runtime_dir.exists.return_value = True
        runtime_dir.glob.return_value = []

        with mock.patch.dict(os.environ, {"HOME": "${HOME}"}, clear=True):
            with mock.patch("vice.runtime.shutil.which", return_value=None):
                with mock.patch(
                    "vice.runtime._wayland_runtime_dir_candidates",
                    return_value=[runtime_dir],
                ):
                    normalize_runtime_environment()
            self.assertEqual(os.environ["HOME"], str(actual_home_dir()))
            self.assertNotIn("WAYLAND_DISPLAY", os.environ)
            self.assertNotIn("DISPLAY", os.environ)
            self.assertEqual(os.environ["XDG_RUNTIME_DIR"], f"/run/user/{os.getuid()}")

    def test_normalize_runtime_environment_repairs_runtime_dir_without_overwriting_valid_display(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "HOME": "${HOME}",
                "WAYLAND_DISPLAY": "wayland-3",
                "DISPLAY": ":1",
                "XDG_RUNTIME_DIR": "/run/user/$(id -u)",
            },
            clear=True,
        ):
            with mock.patch("vice.runtime.shutil.which", return_value=None):
                normalize_runtime_environment()
                self.assertEqual(os.environ["WAYLAND_DISPLAY"], "wayland-3")
                self.assertEqual(os.environ["DISPLAY"], ":1")
                self.assertEqual(os.environ["XDG_RUNTIME_DIR"], f"/run/user/{os.getuid()}")

    def test_normalize_runtime_environment_logs_before_and_after_snapshots(self) -> None:
        with mock.patch.dict(os.environ, {"HOME": "${HOME}"}, clear=True):
            with mock.patch("vice.runtime.shutil.which", return_value=None):
                with mock.patch("vice.runtime.log.debug") as debug_mock:
                    normalize_runtime_environment()

        debug_mock.assert_any_call("Runtime env before normalization: %s", mock.ANY)
        debug_mock.assert_any_call("Runtime env after normalization: %s", mock.ANY)


class WebviewEnvironmentTests(unittest.TestCase):
    def test_sets_default_chromium_flags(self) -> None:
        env = {"LANG": "en_US.UTF-8"}
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch("vice.app._is_nvidia", return_value=False):
                app_mod._prepare_webview_environment()
            flags = os.environ["QTWEBENGINE_CHROMIUM_FLAGS"]

        self.assertIn("--disable-accelerated-video-decode", flags)
        self.assertNotIn("Vulkan", flags)
        self.assertNotIn("--disable-gpu-compositing", flags)

    def test_qt_logging_forced_to_stderr(self) -> None:
        # Qt sends warnings to journald when stderr is not a TTY, which
        # blinded the compositor watcher for app-launcher starts (the
        # black-window failure only self-healed from a terminal).
        with mock.patch.dict(os.environ, {"LANG": "en_US.UTF-8"}, clear=True):
            with mock.patch("vice.app._is_nvidia", return_value=False):
                app_mod._prepare_webview_environment()
            self.assertEqual(os.environ["QT_LOGGING_TO_CONSOLE"], "1")

    def test_compositor_failure_triggers(self) -> None:
        # QtWebEngine prints the GBM line on every NVIDIA start and then
        # renders fine over Vulkan, so it must not condemn the run. Only
        # the repeating black-window markers do, once past the threshold.
        hits, relaunch = app_mod._compositor_failure_hit(
            b"GBM is not supported with the current configuration. "
            b"Fallback to Vulkan rendering in Chromium.", 0
        )
        self.assertFalse(relaunch)

        hits = 0
        for i in range(app_mod._COMPOSITOR_FAILURE_THRESHOLD):
            hits, relaunch = app_mod._compositor_failure_hit(
                b"Compositor returned null texture", hits
            )
            self.assertEqual(relaunch, i == app_mod._COMPOSITOR_FAILURE_THRESHOLD - 1)

        hits, relaunch = app_mod._compositor_failure_hit(b"some harmless line", 0)
        self.assertEqual((hits, relaunch), (0, False))

    def test_nvidia_gets_gpu_compositing_by_default(self) -> None:
        # GPU compositing is the default; software compositing only
        # applies to a run explicitly relaunched with
        # VICE_WEBVIEW_SOFTWARE=1. Nothing is persisted: the GBM failure
        # is intermittent, so every fresh launch tries the GPU first.
        with mock.patch.dict(os.environ, {"LANG": "en_US.UTF-8"}, clear=True):
            with mock.patch("vice.app._is_nvidia", return_value=True):
                with mock.patch("vice.app._nvidia_driver_major", return_value=610):
                    app_mod._prepare_webview_environment()
            flags = os.environ["QTWEBENGINE_CHROMIUM_FLAGS"]

        self.assertNotIn("--disable-gpu-compositing", flags)

    def test_vulkan_blocked_only_on_the_old_driver_series(self) -> None:
        # Vulkan is the only GPU path left once QtWebEngine rejects GBM, so
        # blocking it on every NVIDIA machine forced software compositing.
        def flags_for(major):
            with mock.patch.dict(os.environ, {"LANG": "en_US.UTF-8"}, clear=True):
                with mock.patch("vice.app._is_nvidia", return_value=True):
                    with mock.patch("vice.app._nvidia_driver_major", return_value=major):
                        app_mod._prepare_webview_environment()
                return os.environ["QTWEBENGINE_CHROMIUM_FLAGS"]

        self.assertIn("--disable-features=Vulkan", flags_for(535))
        self.assertNotIn("--disable-features=Vulkan", flags_for(610))
        # An unreadable version is not a reason to downgrade the machine.
        self.assertNotIn("--disable-features=Vulkan", flags_for(None))

    def test_nvidia_driver_major_parses_the_proc_file(self) -> None:
        text = ("NVRM version: NVIDIA UNIX Open Kernel Module for x86_64  "
                "610.43.02  Release Build\nGCC version: gcc 16.1.1\n")
        with mock.patch("pathlib.Path.read_text", return_value=text):
            self.assertEqual(app_mod._nvidia_driver_major(), 610)

    def test_software_env_var_enables_software_compositing_for_this_run(self) -> None:
        env = {"LANG": "en_US.UTF-8", "VICE_WEBVIEW_SOFTWARE": "1"}
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch("vice.app._is_nvidia", return_value=True):
                app_mod._prepare_webview_environment()
            flags = os.environ["QTWEBENGINE_CHROMIUM_FLAGS"]

        self.assertIn("--disable-gpu-compositing", flags)

    def test_qt_start_failure_relaunches_with_software_compositing(self) -> None:
        webview = mock.Mock()
        webview.start.side_effect = RuntimeError("QtWebEngine could not initialize")
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(app_mod, "_relaunch_with_software_compositing") as relaunch, \
             mock.patch.object(app_mod, "log"):
            app_mod._start_webview_with_fallback(webview, "qt", mock.Mock())
        relaunch.assert_called_once_with()
        webview.start.assert_called_once_with(gui="qt", debug=False, private_mode=False)

    def test_nvidia_on_wayland_prefers_xwayland_platform(self) -> None:
        # Chromium's native-Wayland GBM path is flaky on NVIDIA (same
        # machine accepts GBM on one launch, rejects it on the next);
        # the XWayland GL path is stable.
        env = {"LANG": "en_US.UTF-8", "WAYLAND_DISPLAY": "wayland-1",
               "QT_QPA_PLATFORM": "wayland;xcb"}
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch("vice.app._is_nvidia", return_value=True):
                app_mod._prepare_webview_environment()
            self.assertEqual(os.environ["QT_QPA_PLATFORM"], "xcb")

        # Explicit override wins.
        env["VICE_WEBVIEW_PLATFORM"] = "wayland"
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch("vice.app._is_nvidia", return_value=True):
                app_mod._prepare_webview_environment()
            self.assertEqual(os.environ["QT_QPA_PLATFORM"], "wayland")

        # Non-NVIDIA setups keep whatever Qt would pick.
        env = {"LANG": "en_US.UTF-8", "WAYLAND_DISPLAY": "wayland-1"}
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch("vice.app._is_nvidia", return_value=False):
                app_mod._prepare_webview_environment()
            self.assertNotIn("QT_QPA_PLATFORM", os.environ)

    def test_user_flags_are_respected(self) -> None:
        env = {"LANG": "en_US.UTF-8", "QTWEBENGINE_CHROMIUM_FLAGS": "--my-flag"}
        with mock.patch.dict(os.environ, env, clear=True):
            app_mod._prepare_webview_environment()
            self.assertEqual(os.environ["QTWEBENGINE_CHROMIUM_FLAGS"], "--my-flag")

    def test_extra_flags_are_appended(self) -> None:
        env = {"LANG": "en_US.UTF-8", "VICE_WEBVIEW_FLAGS": "--extra-flag"}
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch("vice.app._is_nvidia", return_value=False):
                app_mod._prepare_webview_environment()
            self.assertIn("--extra-flag", os.environ["QTWEBENGINE_CHROMIUM_FLAGS"])

    def test_c_locale_is_replaced_with_utf8(self) -> None:
        # Qt switches away from a C locale with loud warnings; systemd
        # user services often start without any locale at all (#82).
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch("vice.app._is_nvidia", return_value=False):
                app_mod._prepare_webview_environment()
            self.assertEqual(os.environ["LC_ALL"], "C.UTF-8")

        with mock.patch.dict(os.environ, {"LANG": "de_DE.UTF-8"}, clear=True):
            with mock.patch("vice.app._is_nvidia", return_value=False):
                app_mod._prepare_webview_environment()
            self.assertNotIn("LC_ALL", os.environ)


class AppStartupTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        for name in ("PID_FILE", "SOCKET_FILE", "DAEMON_STDERR_LOG_FILE"):
            patcher = mock.patch.object(app_mod, name, root / name.lower())
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_start_daemon_passes_normalized_environment_to_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            socket_path = Path(tmp) / "vice.sock"
            with mock.patch.object(app_mod, "SOCKET_FILE", socket_path):
                with mock.patch.dict(os.environ, {}, clear=True):
                    with mock.patch("vice.app._vice_cmd", return_value=["vice"]):
                        with mock.patch(
                            "vice.app.normalize_runtime_environment",
                            side_effect=lambda: os.environ.__setitem__("WAYLAND_DISPLAY", "wayland-7"),
                        ) as normalize_mock:
                            with mock.patch("vice.app.subprocess.Popen") as popen_mock:
                                app_mod._start_daemon()

        normalize_mock.assert_called_once()
        self.assertEqual(popen_mock.call_args.kwargs["env"]["WAYLAND_DISPLAY"], "wayland-7")

    def test_ensure_server_reuses_healthy_daemon_url(self) -> None:
        with mock.patch(
            "vice.app._daemon_status",
            return_value={"local_url": "http://127.0.0.1:9001", "ready": True},
        ):
            with mock.patch("vice.app._wait_for_server", return_value=True) as wait_mock:
                with mock.patch("vice.app._start_daemon") as start_mock:
                    url = app_mod._ensure_server("http://localhost:8765/")

        self.assertEqual(url, "http://127.0.0.1:9001/")
        self.assertEqual(wait_mock.call_args_list[0], mock.call("http://127.0.0.1:9001/", timeout=2.0))
        start_mock.assert_not_called()

    def test_ensure_server_waits_for_ready_daemon_after_http_responds(self) -> None:
        statuses = [
            None,
            {"local_url": "http://127.0.0.1:8765", "ready": False},
            {"local_url": "http://127.0.0.1:8765", "ready": True},
        ]
        with mock.patch("vice.app._daemon_status", side_effect=statuses):
            with mock.patch("vice.app._wait_for_server", return_value=True):
                with mock.patch("vice.app._start_daemon") as start_mock:
                    url = app_mod._ensure_server("http://127.0.0.1:8765/", startup_timeout=1.0)

        self.assertEqual(url, "http://127.0.0.1:8765/")
        start_mock.assert_called_once()

    def test_app_main_uses_ipv4_loopback_url(self) -> None:
        fake_cfg = mock.Mock()
        fake_cfg.sharing.port = 8765
        # The lock has to be pinned: unpinned this takes the real
        # single-window lock for the rest of the run, and it would take the
        # "a window is already open" exit whenever the suite runs on a
        # machine with Vice open.
        with mock.patch("vice.app._claim_app_lock", return_value=True), \
             mock.patch("vice.app.normalize_runtime_environment"):
            with mock.patch("vice.app._setup_logging"):
                with mock.patch("vice.app.signal.signal"):
                    with mock.patch("vice.config.load", return_value=fake_cfg):
                        with mock.patch("vice.app._ensure_server", return_value=None) as ensure_mock:
                            with mock.patch("vice.app._startup_failure_detail", return_value="detail") as detail_mock:
                                with mock.patch("vice.app._show_error"):
                                    with self.assertRaises(SystemExit):
                                        app_mod.main()

        ensure_mock.assert_called_once_with("http://127.0.0.1:8765/")
        detail_mock.assert_called_once_with("http://127.0.0.1:8765/")

    def test_ensure_server_restarts_when_ipc_is_alive_but_http_is_dead(self) -> None:
        with mock.patch("vice.app._daemon_status", side_effect=[
            {"local_url": "http://127.0.0.1:9001"},
            {"local_url": "http://127.0.0.1:8765", "ready": True},
        ]):
            with mock.patch("vice.app._wait_for_server", side_effect=[False, True]) as wait_mock:
                with mock.patch("vice.app._stop_daemon") as stop_mock:
                    with mock.patch("vice.app._wait_for_daemon_exit", return_value=True):
                        with mock.patch("vice.app._clear_stale_socket") as clear_mock:
                            with mock.patch("vice.app._start_daemon") as start_mock:
                                with mock.patch("vice.app.time.sleep"):
                                    url = app_mod._ensure_server("http://127.0.0.1:8765/")

        self.assertEqual(url, "http://127.0.0.1:8765/")
        stop_mock.assert_called_once()
        clear_mock.assert_called_once()
        start_mock.assert_called_once()
        self.assertEqual(wait_mock.call_args_list[0].kwargs["timeout"], 2.0)
        self.assertEqual(wait_mock.call_args_list[1].kwargs["timeout"], 1.0)

    def test_startup_failure_detail_includes_daemon_log_tail_when_ipc_never_appears(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            daemon_log = Path(tmp) / "vice.log"
            daemon_log.write_text("first line\nsecond line\n")
            with mock.patch.object(app_mod, "DAEMON_LOG_FILE", daemon_log):
                with mock.patch("vice.app._daemon_status", return_value=None):
                    detail = app_mod._startup_failure_detail("http://localhost:8765/")

        self.assertIn("Daemon IPC socket did not become ready.", detail)
        self.assertIn("second line", detail)

    def test_startup_failure_detail_reports_http_outage_when_ipc_is_alive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            daemon_log = Path(tmp) / "vice.log"
            daemon_log.write_text("backend failed\n")
            with mock.patch.object(app_mod, "DAEMON_LOG_FILE", daemon_log):
                with mock.patch(
                    "vice.app._daemon_status",
                    return_value={"local_url": "http://127.0.0.1:9001"},
                ):
                    detail = app_mod._startup_failure_detail("http://localhost:8765/")

        self.assertIn("Daemon IPC responded but HTTP UI is unavailable at http://127.0.0.1:9001/", detail)
        self.assertIn("backend failed", detail)

    def test_startup_failure_detail_explains_known_gsr_crash(self) -> None:
        """A raw 'failed to load opengl' traceback tells the user nothing about
        what to do next, so the dialog leads with the cause."""
        with tempfile.TemporaryDirectory() as tmp:
            daemon_log = Path(tmp) / "vice.log"
            daemon_log.write_text(
                "RuntimeError: gpu-screen-recorder failed to start: "
                "gsr error: failed to load opengl\n"
            )
            with mock.patch.object(app_mod, "DAEMON_LOG_FILE", daemon_log):
                with mock.patch("vice.app._daemon_status", return_value=None):
                    detail = app_mod._startup_failure_detail("http://localhost:8765/")

        self.assertTrue(detail.startswith("gpu-screen-recorder could not create"))
        self.assertIn("vice doctor", detail)
        # The raw log stays available underneath the explanation.
        self.assertIn("failed to load opengl", detail)

    def test_startup_failure_detail_stays_raw_for_unknown_crashes(self) -> None:
        self.assertIsNone(app_mod._diagnose_startup_failure("ZeroDivisionError: nope"))


class ConfigPathResolutionTests(unittest.TestCase):
    def test_default_config_enables_discord_rich_presence(self) -> None:
        self.assertTrue(Config().discord.enabled)

    def test_load_expands_home_placeholders_in_output_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / ".config" / "vice"
            config_dir.mkdir(parents=True)
            config_path = config_dir / "config.toml"
            config_path.write_text('[output]\ndirectory = "$HOME/Videos/Vice"\n')

            with mock.patch.object(config_mod, "CONFIG_DIR", config_dir):
                with mock.patch.object(config_mod, "CONFIG_PATH", config_path):
                    cfg = config_mod.load()

        self.assertEqual(cfg.output.directory, str(actual_home_dir() / "Videos" / "Vice"))

    def test_load_preserves_existing_discord_disabled_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / ".config" / "vice"
            config_dir.mkdir(parents=True)
            config_path = config_dir / "config.toml"
            config_path.write_text("[discord]\nenabled = false\n")

            with mock.patch.object(config_mod, "CONFIG_DIR", config_dir):
                with mock.patch.object(config_mod, "CONFIG_PATH", config_path):
                    cfg = config_mod.load()

        self.assertFalse(cfg.discord.enabled)

    def test_save_and_load_preserve_recording_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / ".config" / "vice"
            config_dir.mkdir(parents=True)
            config_path = config_dir / "config.toml"

            cfg = Config(
                recording=RecordingConfig(
                    capture_microphone=True,
                    microphone_source="device:alsa_input.usb-guitar",
                    wf_microphone_strategy="backend_fallback",
                    display="DP-1",
                    gsr_audio_source="app:firefox",
                    audio_tracks=["default_output", "app:Discord"],
                    audio_tracks_mix_first=True,
                )
            )

            with mock.patch.object(config_mod, "CONFIG_DIR", config_dir):
                with mock.patch.object(config_mod, "CONFIG_PATH", config_path):
                    config_mod.save(cfg)
                    loaded = config_mod.load()

        self.assertTrue(loaded.recording.capture_microphone)
        self.assertEqual(loaded.recording.microphone_source, "device:alsa_input.usb-guitar")
        self.assertEqual(loaded.recording.wf_microphone_strategy, "backend_fallback")
        self.assertEqual(loaded.recording.display, "DP-1")
        self.assertEqual(loaded.recording.gsr_audio_source, "app:firefox")
        self.assertEqual(loaded.recording.audio_tracks, ["default_output", "app:Discord"])
        self.assertTrue(loaded.recording.audio_tracks_mix_first)

    def test_load_ignores_unknown_config_keys(self) -> None:
        # A config written by a newer Vice must not crash an older daemon:
        # 1.2.x died at startup on the recording keys 1.3.0 added.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / ".config" / "vice"
            config_dir.mkdir(parents=True)
            config_path = config_dir / "config.toml"
            config_path.write_text(
                "[recording]\n"
                "fps = 30\n"
                "some_future_key = \"surprise\"\n"
                "[sharing]\n"
                "port = 9000\n"
                "another_future_key = 7\n"
            )

            with mock.patch.object(config_mod, "CONFIG_DIR", config_dir):
                with mock.patch.object(config_mod, "CONFIG_PATH", config_path):
                    loaded = config_mod.load()

        self.assertEqual(loaded.recording.fps, 30)
        self.assertEqual(loaded.sharing.port, 9000)

    def test_save_and_load_preserve_clip_presets_and_grow_buffer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / ".config" / "vice"
            config_dir.mkdir(parents=True)
            config_path = config_dir / "config.toml"

            cfg = Config(
                recording=RecordingConfig(buffer_duration=60, clip_duration=15),
                hotkeys=HotkeyConfig(
                    clip="KEY_F9",
                    clip_presets=[HotkeyClipPreset(key="KEY_F6", duration=120)],
                ),
            )

            with mock.patch.object(config_mod, "CONFIG_DIR", config_dir):
                with mock.patch.object(config_mod, "CONFIG_PATH", config_path):
                    config_mod.save(cfg)
                    loaded = config_mod.load()

        self.assertEqual(loaded.hotkeys.clip_presets[0].key, "KEY_F6")
        self.assertEqual(loaded.hotkeys.clip_presets[0].duration, 120)
        self.assertEqual(loaded.recording.buffer_duration, 120)

    def test_load_ignores_malformed_clip_presets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / ".config" / "vice"
            config_dir.mkdir(parents=True)
            config_path = config_dir / "config.toml"
            config_path.write_text(
                "[hotkeys]\n"
                "clip = \"KEY_F9\"\n"
                "[[hotkeys.clip_presets]]\n"
                "key = \"\"\n"
                "duration = 60\n"
                "[[hotkeys.clip_presets]]\n"
                "key = \"KEY_F6\"\n"
                "duration = 90\n"
            )

            with mock.patch.object(config_mod, "CONFIG_DIR", config_dir):
                with mock.patch.object(config_mod, "CONFIG_PATH", config_path):
                    loaded = config_mod.load()

        self.assertEqual(len(loaded.hotkeys.clip_presets), 1)
        self.assertEqual(loaded.hotkeys.clip_presets[0].key, "KEY_F6")


@unittest.skipUnless(ShareServer is not None, "aiohttp is not installed")
class ShareServerPathResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_preloads_clips_from_resolved_output_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "clips"
            output_dir.mkdir()
            clip_path = output_dir / "clip.mp4"
            clip_path.write_bytes(b"not-a-real-mp4")

            local_port = _free_port()
            public_port = _free_port()
            while public_port == local_port:
                public_port = _free_port()
            cfg = Config(
                output=OutputConfig(directory="$HOME/Videos/Vice"),
                sharing=SharingConfig(
                    port=local_port,
                    public_port=public_port,
                    cloudflare_tunnel=False,
                ),
            )
            server = ShareServer(cfg)

            with mock.patch("vice.share.resolve_path", return_value=output_dir):
                with mock.patch("vice.share._local_ip", return_value="127.0.0.1"):
                    with mock.patch("vice.share._ffprobe", new=_stub_ffprobe):
                        await server.start()
                        try:
                            self.assertIn("clip", server._clips)
                        finally:
                            await server.stop()


class RecorderEnvironmentTests(unittest.TestCase):
    def test_is_wayland_delegates_to_runtime_recovery(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch("vice.recorder.recover_wayland_display", return_value=True) as recover_mock:
                self.assertTrue(_is_wayland())
        recover_mock.assert_called_once()


class _FakeHotkeys:
    available = True

    def __init__(self) -> None:
        self.single: dict[str, list] = {}
        self.double: dict[str, list] = {}

    def clear_bindings(self) -> None:
        self.single.clear()
        self.double.clear()

    def on(self, key, callback) -> None:
        self.single.setdefault(key, []).append(callback)

    def on_double(self, key, callback) -> None:
        self.double.setdefault(key, []).append(callback)

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


class _FakeRecorder:
    def __init__(self, result=None) -> None:
        self.name = "fake"
        self._result = result
        self.save_calls = 0
        self.save_durations: list[int | None] = []
        self._cb = None
        self.healthy = True
        self.heal_on_start = False
        self.start_calls = 0
        self.stop_calls = 0
        self.start_error: Exception | None = None
        self.output = ""
        self.display_override: str | None = None

    def on_clip_saved(self, cb) -> None:
        self._cb = cb

    def is_healthy(self) -> bool:
        return self.healthy

    def last_output(self) -> str:
        return self.output

    async def start(self) -> None:
        self.start_calls += 1
        if self.start_error is not None:
            raise self.start_error
        if self.heal_on_start:
            self.healthy = True

    async def stop(self) -> None:
        self.stop_calls += 1

    async def save_clip(self, duration=None):
        self.save_calls += 1
        self.save_durations.append(duration)
        await asyncio.sleep(0)
        return self._result

    def session_elapsed(self) -> float:
        return 0.0


class _FakeShare:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def broadcast(self, msg: dict) -> None:
        self.messages.append(msg)


class ViceDaemonClipFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_clip_trigger_broadcasts_progress_and_error(self) -> None:
        recorder = _FakeRecorder(result=None)
        with mock.patch("vice.main.load_config", return_value=Config()):
            with mock.patch("vice.main.create_recorder", return_value=recorder):
                with mock.patch("vice.main.HotkeyListener", return_value=_FakeHotkeys()):
                    with mock.patch("vice.main.can_access_hotkeys", return_value=True):
                        daemon = main_mod.ViceDaemon()

        daemon.share = _FakeShare()

        with mock.patch("vice.main.audio.play_clip"):
            await daemon._handle_clip_hotkey()
            self.assertIsNotNone(daemon._clip_task)
            await daemon._clip_task

        self.assertEqual(recorder.save_calls, 1)
        self.assertEqual(
            [msg["type"] for msg in daemon.share.messages],
            ["clip_saving", "clip_error"],
        )

    async def test_clip_trigger_passes_requested_duration(self) -> None:
        recorder = _FakeRecorder(result=None)
        with mock.patch("vice.main.load_config", return_value=Config()):
            with mock.patch("vice.main.create_recorder", return_value=recorder):
                with mock.patch("vice.main.HotkeyListener", return_value=_FakeHotkeys()):
                    with mock.patch("vice.main.can_access_hotkeys", return_value=True):
                        daemon = main_mod.ViceDaemon()

        with mock.patch("vice.main.audio.play_clip"):
            await daemon._handle_clip_hotkey(90)
            await daemon._clip_task

        self.assertEqual(recorder.save_durations, [90])

    async def test_config_restart_rewires_game_tag_callback(self) -> None:
        """A settings change swaps in a new recorder. If it doesn't get the tag
        callback, filename tagging and auto playlists silently stop working."""
        first = _FakeRecorder()
        second = _FakeRecorder()
        with mock.patch("vice.main.load_config", return_value=Config()):
            with mock.patch("vice.main.create_recorder", return_value=first):
                with mock.patch("vice.main.HotkeyListener", return_value=_FakeHotkeys()):
                    with mock.patch("vice.main.can_access_hotkeys", return_value=True):
                        daemon = main_mod.ViceDaemon()

        # _wire_recorder must set both callbacks, not just on_clip_saved.
        daemon._wire_recorder(first)
        self.assertEqual(first._cb, daemon._on_clip_saved)
        self.assertEqual(first.clip_tag_cb, daemon._clip_game_tag)

        # A config change swaps in a new recorder; it must be wired the same way
        # or game tagging and auto playlists silently stop after any settings edit.
        daemon._session_active = False
        with mock.patch("vice.main.create_recorder", return_value=second):
            applied = await daemon._restart_recorder_for_config()

        self.assertTrue(applied)
        self.assertIs(daemon.recorder, second)
        self.assertEqual(second._cb, daemon._on_clip_saved)
        self.assertEqual(second.clip_tag_cb, daemon._clip_game_tag)

    async def _follow_mouse_daemon(self, samples: list) -> tuple:
        """Daemon with follow-the-pointer on and a scripted pointer sequence."""
        cfg = Config(recording=RecordingConfig(display="DP-1", follow_mouse_display=True))
        with mock.patch("vice.main.load_config", return_value=cfg), \
             mock.patch("vice.main.create_recorder", return_value=_FakeRecorder()), \
             mock.patch("vice.main.HotkeyListener", return_value=_FakeHotkeys()), \
             mock.patch("vice.main.can_access_hotkeys", return_value=True):
            daemon = main_mod.ViceDaemon()

        restarts: list = []

        async def fake_restart() -> bool:
            restarts.append(daemon._display_override)
            return True

        daemon._restart_recorder_for_config = fake_restart  # type: ignore[method-assign]
        readings = iter(samples)

        def next_reading():
            try:
                return next(readings)
            except StopIteration:
                raise asyncio.CancelledError

        with mock.patch("vice.main.FOLLOW_MOUSE_INTERVAL", 0), \
             mock.patch("vice.active_window.pointer_display", side_effect=next_reading):
            task = asyncio.create_task(daemon._follow_mouse_loop())
            await asyncio.wait_for(task, timeout=5)
        return daemon, restarts

    async def test_pointer_retargets_capture_after_two_agreeing_samples(self) -> None:
        daemon, restarts = await self._follow_mouse_daemon(
            ["HDMI-A-1", "HDMI-A-1", "HDMI-A-1"]
        )

        self.assertEqual(restarts, ["HDMI-A-1"])
        self.assertEqual(daemon._display_override, "HDMI-A-1")

    async def test_a_single_stray_sample_does_not_restart_the_recorder(self) -> None:
        """Dragging the pointer across a screen edge must not cost the buffer."""
        _, restarts = await self._follow_mouse_daemon(
            ["HDMI-A-1", "DP-2", "HDMI-A-1", "DP-2"]
        )

        self.assertEqual(restarts, [])

    async def test_undetectable_pointer_leaves_capture_alone(self) -> None:
        _, restarts = await self._follow_mouse_daemon([None, None, None])

        self.assertEqual(restarts, [])

    async def test_follow_mouse_task_starts_and_stops_with_the_setting(self) -> None:
        cfg = Config(recording=RecordingConfig(follow_mouse_display=True))
        recorder = _FakeRecorder()
        with mock.patch("vice.main.load_config", return_value=cfg), \
             mock.patch("vice.main.create_recorder", return_value=recorder), \
             mock.patch("vice.main.HotkeyListener", return_value=_FakeHotkeys()), \
             mock.patch("vice.main.can_access_hotkeys", return_value=True):
            daemon = main_mod.ViceDaemon()

        daemon._sync_follow_mouse_task()
        self.assertIsNotNone(daemon._follow_mouse_task)

        # Turning it off has to drop the override too, or the recorder keeps
        # capturing the last monitor the pointer visited.
        daemon._display_override = "HDMI-A-1"
        daemon.cfg.recording.follow_mouse_display = False
        daemon._sync_follow_mouse_task()
        await asyncio.sleep(0)

        self.assertIsNone(daemon._follow_mouse_task)
        self.assertIsNone(daemon._display_override)
        self.assertIsNone(recorder.display_override)

    async def test_bind_hotkeys_registers_primary_and_preset_keys(self) -> None:
        hotkeys = _FakeHotkeys()
        cfg = Config(
            recording=RecordingConfig(clip_duration=15),
            hotkeys=HotkeyConfig(
                clip="KEY_F9",
                clip_presets=[HotkeyClipPreset(key="KEY_F6", duration=60)],
            ),
        )
        with mock.patch("vice.main.load_config", return_value=cfg):
            with mock.patch("vice.main.create_recorder", return_value=_FakeRecorder()):
                with mock.patch("vice.main.HotkeyListener", return_value=hotkeys):
                    with mock.patch("vice.main.can_access_hotkeys", return_value=True):
                        daemon = main_mod.ViceDaemon()

        daemon._bind_hotkeys()

        self.assertEqual(set(hotkeys.single), {"KEY_F9", "KEY_F6"})
        self.assertEqual(set(hotkeys.double), {"KEY_F9", "KEY_F6"})

    async def test_clip_game_tag_matches_a_known_game_and_records_it(self) -> None:
        """End to end for the reporter's case: a focused Outer Wilds window
        yields a filename tag and sets the game used for the auto playlist."""
        cfg = Config(output=OutputConfig(tag_clips_with_game=True))
        with mock.patch("vice.main.load_config", return_value=cfg):
            with mock.patch("vice.main.create_recorder", return_value=_FakeRecorder()):
                with mock.patch("vice.main.HotkeyListener", return_value=_FakeHotkeys()):
                    with mock.patch("vice.main.can_access_hotkeys", return_value=True):
                        daemon = main_mod.ViceDaemon()

        win = {"process": "OuterWilds", "class": "OuterWilds", "pid": 1}
        with mock.patch("vice.active_window.get_active_window", return_value=win):
            tag = daemon._clip_game_tag()

        # _clip_game_tag returns the raw game name; the recorder's _clip_tag
        # sanitizes it into the filename.
        self.assertEqual(tag, "Outer Wilds")
        self.assertEqual(daemon._last_clip_game, "Outer Wilds")

    async def test_clip_game_tag_still_records_game_when_filename_tag_off(self) -> None:
        # Auto playlists must work even with filename tagging disabled.
        cfg = Config(output=OutputConfig(tag_clips_with_game=False))
        with mock.patch("vice.main.load_config", return_value=cfg):
            with mock.patch("vice.main.create_recorder", return_value=_FakeRecorder()):
                with mock.patch("vice.main.HotkeyListener", return_value=_FakeHotkeys()):
                    with mock.patch("vice.main.can_access_hotkeys", return_value=True):
                        daemon = main_mod.ViceDaemon()

        win = {"process": "OuterWilds", "class": "OuterWilds", "pid": 1}
        with mock.patch("vice.active_window.get_active_window", return_value=win):
            tag = daemon._clip_game_tag()

        self.assertIsNone(tag)
        self.assertEqual(daemon._last_clip_game, "Outer Wilds")


class RecorderDurationTests(unittest.IsolatedAsyncioTestCase):
    async def test_segment_save_clip_uses_requested_duration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seg = root / "seg0001.mp4"
            seg.write_bytes(b"segment")
            out = root / "out.mp4"
            recorder = SegmentRecorder(
                Config(
                    output=OutputConfig(directory=str(root)),
                    recording=RecordingConfig(clip_duration=15),
                ),
                use_wf_recorder=False,
            )
            recorder._segments = [(900.0, seg)]
            captured: dict[str, list[str]] = {}

            class _Proc:
                returncode = 0

                async def communicate(self):
                    out.write_bytes(b"clip")
                    return b"", b""

            async def _fake_exec(*cmd, **_kwargs):
                captured["cmd"] = list(cmd)
                return _Proc()

            with mock.patch("vice.recorder.time.time", return_value=1000.0):
                with mock.patch("vice.recorder._next_clip_path", return_value=out):
                    with mock.patch("vice.recorder.asyncio.create_subprocess_exec", new=_fake_exec):
                        saved = await recorder.save_clip(45)

        self.assertEqual(saved, out)
        self.assertEqual(captured["cmd"][captured["cmd"].index("-t") + 1], "45")


class RecorderStabilizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_wait_for_finalized_clip_waits_for_last_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "raw.mp4"

            async def _writer() -> None:
                await asyncio.sleep(0.02)
                clip.write_bytes(b"a")
                await asyncio.sleep(0.08)
                clip.write_bytes(b"ab")
                await asyncio.sleep(0.08)
                clip.write_bytes(b"abc")

            observed: list[bytes] = []

            async def _fake_duration(path: Path) -> float:
                observed.append(path.read_bytes())
                return 30.0

            writer = asyncio.create_task(_writer())
            start = time.monotonic()
            with mock.patch("vice.recorder._get_duration", new=_fake_duration):
                ready = await _wait_for_finalized_clip(
                    clip,
                    stable_polls=3,
                    poll_interval=0.03,
                    inactivity_timeout=1.0,
                    max_wait=5.0,
                )
            elapsed = time.monotonic() - start
            await writer

        self.assertTrue(ready)
        self.assertGreaterEqual(elapsed, 0.18)
        self.assertEqual(observed[-1], b"abc")

    async def test_wait_for_finalized_clip_gives_up_after_write_inactivity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "raw.mp4"
            clip.write_bytes(b"not a video")

            async def _zero_duration(_: Path) -> float:
                return 0.0

            start = time.monotonic()
            with mock.patch("vice.recorder._get_duration", new=_zero_duration):
                ready = await _wait_for_finalized_clip(
                    clip,
                    stable_polls=2,
                    poll_interval=0.02,
                    inactivity_timeout=0.15,
                    max_wait=5.0,
                )
            elapsed = time.monotonic() - start

        self.assertFalse(ready)
        self.assertLess(elapsed, 2.0)

    async def test_wait_for_finalized_clip_tolerates_slow_writes(self) -> None:
        """A file that keeps growing must not be abandoned, even when the
        total write time exceeds the inactivity timeout."""
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "raw.mp4"

            async def _writer() -> None:
                # Total write time (0.8 s) far exceeds the inactivity
                # timeout (0.3 s); only the gaps between writes count.
                data = b""
                for _ in range(16):
                    data += b"x" * 10
                    clip.write_bytes(data)
                    await asyncio.sleep(0.05)

            durations = iter([0.0] * 2)

            async def _fake_duration(_: Path) -> float:
                return next(durations, 30.0)

            writer = asyncio.create_task(_writer())
            with mock.patch("vice.recorder._get_duration", new=_fake_duration):
                ready = await _wait_for_finalized_clip(
                    clip,
                    stable_polls=2,
                    poll_interval=0.03,
                    inactivity_timeout=0.3,
                    max_wait=5.0,
                )
            await writer

        self.assertTrue(ready)

    async def test_trim_copy_command_avoids_negative_timestamps(self) -> None:
        commands: list = []

        async def _fake_duration(_: Path) -> float:
            return 100.0

        class _Proc:
            returncode = 0

            async def communicate(self):
                return b"", b""

        async def _fake_exec(*cmd, **_kwargs):
            commands.append(list(cmd))
            return _Proc()

        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "clip.mp4"
            clip.write_bytes(b"clip")
            with mock.patch("vice.recorder._get_duration", new=_fake_duration):
                with mock.patch(
                    "vice.recorder.asyncio.create_subprocess_exec", new=_fake_exec
                ):
                    from vice.recorder import _trim_to_last_n_seconds

                    await _trim_to_last_n_seconds(clip, 30)

        # The copy trim runs first; a re-encode only follows if it fails.
        cmd = commands[0]
        self.assertIn("-avoid_negative_ts", cmd)
        self.assertEqual(cmd[cmd.index("-avoid_negative_ts") + 1], "make_zero")
        self.assertIn("copy", cmd)

    async def test_gsr_save_clip_waits_for_finalized_file_before_trim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            recorder = GSRRecorder(
                Config(
                    output=OutputConfig(directory=str(out_dir)),
                    recording=RecordingConfig(clip_duration=30),
                )
            )
            recorder._proc = mock.Mock(pid=1234, returncode=None)

            raw_clip = out_dir / "gsr-auto.mp4"

            async def _writer() -> None:
                await asyncio.sleep(0.05)
                raw_clip.write_bytes(b"clip")

            async def _trim(path: Path, seconds: int) -> Path:
                self.assertEqual(seconds, 45)
                return path

            writer = asyncio.create_task(_writer())
            with mock.patch("vice.recorder.os.kill") as kill_mock:
                with mock.patch("vice.recorder._wait_for_finalized_clip", new=mock.AsyncMock(return_value=True)) as wait_mock:
                    with mock.patch("vice.recorder._trim_to_last_n_seconds", new=_trim):
                        saved = await recorder.save_clip(45)
            await writer

        kill_mock.assert_called_once_with(1234, mock.ANY)
        wait_mock.assert_awaited_once()
        self.assertIsNotNone(saved)
        self.assertEqual(saved.name, "Vice_Clip_1.mp4")

    async def test_gsr_save_clip_ignores_files_that_predate_the_trigger(self) -> None:
        # Regression: a session recording (or any file) that appeared after
        # recorder start used to be claimed as "the new clip" on the next
        # save, so the wrong video got renamed, trimmed, and shown.
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            recorder = GSRRecorder(
                Config(
                    output=OutputConfig(directory=str(out_dir)),
                    recording=RecordingConfig(clip_duration=30),
                )
            )
            recorder._proc = mock.Mock(pid=1234, returncode=None)

            session = out_dir / "Vice_Session_1.mp4"
            session.write_bytes(b"session recording")
            stray = out_dir / "renamed-by-user.mp4"
            stray.write_bytes(b"older clip")

            raw_clip = out_dir / "Replay_2026-06-10_12-00-00.mp4"

            async def _writer() -> None:
                await asyncio.sleep(0.05)
                raw_clip.write_bytes(b"new replay")

            async def _trim(path: Path, seconds: int) -> Path:
                return path

            writer = asyncio.create_task(_writer())
            with mock.patch("vice.recorder.os.kill"):
                with mock.patch(
                    "vice.recorder._wait_for_finalized_clip",
                    new=mock.AsyncMock(return_value=True),
                ):
                    with mock.patch("vice.recorder._trim_to_last_n_seconds", new=_trim):
                        saved = await recorder.save_clip()
            await writer

            self.assertIsNotNone(saved)
            self.assertEqual(saved.read_bytes(), b"new replay")
            self.assertTrue(session.exists())
            self.assertEqual(stray.read_bytes(), b"older clip")

    def test_gsr_replay_candidates_excludes_vice_artifacts(self) -> None:
        from vice.recorder import _gsr_replay_candidates

        current = {
            "Replay_2026-06-10_12-00-00.mp4",
            "Vice_Clip_4.mp4",
            "Vice_Session_2.mkv",
            "Vice_Clip_3.trim.mp4",
            "epic-headshot.trimming.mp4",
            "Vice_Clip_2.wm.mp4",
            "old-clip.fix.mkv",
            "already-there.mp4",
        }
        new = _gsr_replay_candidates(current, baseline={"already-there.mp4"})

        self.assertEqual(new, {"Replay_2026-06-10_12-00-00.mp4"})


class ClipNamingTests(unittest.IsolatedAsyncioTestCase):
    def test_next_clip_path_counts_tagged_and_mkv_clips(self) -> None:
        from vice.recorder import _next_clip_path

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "Vice_Clip_1.mp4").write_bytes(b"x")
            (out / "Vice_Clip_2_Overwatch-2.mkv").write_bytes(b"x")

            untagged = _next_clip_path(out)
            tagged = _next_clip_path(out, ext="mkv", tag="Deep-Rock-Galactic")

        self.assertEqual(untagged.name, "Vice_Clip_3.mp4")
        self.assertEqual(tagged.name, "Vice_Clip_3_Deep-Rock-Galactic.mkv")

    def test_next_image_path_numbers_screenshots_on_their_own(self) -> None:
        from vice.recorder import next_image_path

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            # Clips share the folder in nobody's setup, but they do share the
            # counter's regex, so a clip must not push a screenshot's number on.
            (out / "Vice_Clip_7.mp4").write_bytes(b"x")
            (out / "Vice_Shot_1.png").write_bytes(b"x")
            (out / "Vice_Shot_2_Overwatch-2.jpg").write_bytes(b"x")

            untagged = next_image_path(out)
            tagged = next_image_path(out, tag="Deep-Rock-Galactic")

        self.assertEqual(untagged.name, "Vice_Shot_3.png")
        self.assertEqual(tagged.name, "Vice_Shot_3_Deep-Rock-Galactic.png")

    def test_a_screenshot_name_keeps_its_extension_off_the_stem(self) -> None:
        from vice.recorder import slugify_clip_name

        self.assertEqual(slugify_clip_name("Nice shot.png"), "Nice-shot")
        self.assertEqual(slugify_clip_name("Nice shot.jpeg"), "Nice-shot")

    def test_next_session_path_uses_configured_container(self) -> None:
        from vice.recorder import _next_session_path

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)

            default = _next_session_path(out)
            mkv = _next_session_path(out, ext="mkv")

        self.assertEqual(default.name, "Vice_Session_1.mp4")
        self.assertEqual(mkv.name, "Vice_Session_1.mkv")

    async def test_clip_tag_is_sanitized_for_filenames(self) -> None:
        recorder = GSRRecorder(
            Config(output=OutputConfig(directory="/tmp/vice-test"))
        )
        recorder.clip_tag_cb = lambda: "Overwatch 2: Beta!"

        self.assertEqual(await recorder._clip_tag(), "Overwatch-2-Beta")

        recorder.clip_tag_cb = lambda: None
        self.assertIsNone(await recorder._clip_tag())

        def _boom() -> str:
            raise RuntimeError("window detection failed")

        recorder.clip_tag_cb = _boom
        self.assertIsNone(await recorder._clip_tag())


_GSR_INFO = "\n".join([
    "section=system_info",
    "display_server|wayland",
    "gsr_version|5.15.3",
    "section=gpu_info",
    "vendor|nvidia",
    "section=video_codecs",
    "hevc",
    "hevc_10bit",
    "av1",
    "section=image_formats",
    "jpeg",
])


class GSRCodecCapabilityTests(unittest.TestCase):
    """Vice must ask gpu-screen-recorder what the GPU encodes rather than
    trusting ffmpeg's build-time encoder list (#156)."""

    def setUp(self) -> None:
        _gsr_supported_codecs.cache_clear()
        self.addCleanup(_gsr_supported_codecs.cache_clear)

    def test_info_video_codecs_section_is_parsed(self) -> None:
        with mock.patch("vice.recorder._has", return_value=True):
            with mock.patch("vice.recorder._run_command_capture", return_value=(0, _GSR_INFO)):
                self.assertEqual(_gsr_supported_codecs(), frozenset({"hevc", "hevc_10bit", "av1"}))

    def test_missing_section_reads_as_no_opinion(self) -> None:
        info = "section=system_info\nvendor|nvidia\nsection=image_formats\njpeg"
        with mock.patch("vice.recorder._has", return_value=True):
            with mock.patch("vice.recorder._run_command_capture", return_value=(0, info)):
                self.assertEqual(_gsr_supported_codecs(), frozenset())

    def test_failed_probe_reads_as_no_opinion(self) -> None:
        with mock.patch("vice.recorder._has", return_value=True):
            with mock.patch("vice.recorder._run_command_capture", return_value=(1, "gsr error: no")):
                self.assertEqual(_gsr_supported_codecs(), frozenset())

    def test_missing_binary_reads_as_no_opinion(self) -> None:
        with mock.patch("vice.recorder._has", return_value=False):
            self.assertEqual(_gsr_supported_codecs(), frozenset())

    def test_unsupported_codec_is_swapped_for_one_the_gpu_has(self) -> None:
        # Dropping -k would land on GSR's default, which is h264, the very
        # codec that is missing. It has to name a replacement.
        rc = RecordingConfig(encoder="h264_nvenc")
        with mock.patch("vice.recorder._gsr_supported_codecs", return_value=frozenset({"hevc", "av1"})):
            self.assertEqual(_gsr_codec_args(rc, []), ["-k", "hevc"])

    def test_supported_codec_is_still_forced(self) -> None:
        rc = RecordingConfig(encoder="h264_nvenc")
        with mock.patch("vice.recorder._gsr_supported_codecs", return_value=frozenset({"h264", "hevc"})):
            self.assertEqual(_gsr_codec_args(rc, []), ["-k", "h264"])

    def test_unknown_capabilities_change_nothing(self) -> None:
        """The regression guard: a probe that tells us nothing must leave the
        command exactly as it was before this check existed."""
        rc = RecordingConfig(encoder="h264_nvenc")
        with mock.patch("vice.recorder._gsr_supported_codecs", return_value=frozenset()):
            self.assertEqual(_gsr_codec_args(rc, []), ["-k", "h264"])

    def test_auto_encoder_still_sends_no_codec_flag(self) -> None:
        # encoder=auto has always left the choice to GSR, and a machine with
        # nothing wrong with it must not start seeing -k appear.
        rc = RecordingConfig(encoder="auto")
        with mock.patch("vice.recorder._gsr_supported_codecs", return_value=frozenset({"hevc", "av1", "h264"})):
            self.assertEqual(_gsr_codec_args(rc, []), [])

    def test_user_supplied_k_flag_always_wins(self) -> None:
        rc = RecordingConfig(encoder="h264_nvenc")
        with mock.patch("vice.recorder._gsr_supported_codecs", return_value=frozenset({"hevc"})):
            self.assertEqual(_gsr_codec_args(rc, ["-k", "av1"]), [])

    def test_avoiding_a_codec_picks_another_supported_one(self) -> None:
        rc = RecordingConfig(encoder="h264_nvenc")
        with mock.patch("vice.recorder._gsr_supported_codecs", return_value=frozenset({"h264", "hevc", "av1"})):
            self.assertEqual(_gsr_codec_choice(rc, avoid="h264"), "hevc")

    def test_avoiding_a_codec_with_no_capability_info_gives_up(self) -> None:
        # Guessing blind could pick something worse than the CPU fallback.
        rc = RecordingConfig(encoder="h264_nvenc")
        with mock.patch("vice.recorder._gsr_supported_codecs", return_value=frozenset()):
            self.assertIsNone(_gsr_codec_choice(rc, avoid="h264"))

    def test_ten_bit_stays_ten_bit_when_swapping(self) -> None:
        rc = RecordingConfig(encoder="hevc_nvenc", color_depth="10")
        with mock.patch(
            "vice.recorder._gsr_supported_codecs",
            return_value=frozenset({"h264", "hevc", "av1", "av1_10bit"}),
        ):
            self.assertEqual(_gsr_codec_choice(rc, avoid="hevc_10bit"), "av1_10bit")

    def test_cpu_encoding_carries_no_codec_flag(self) -> None:
        # GSR only encodes H.264 on the CPU, which is also its default, so
        # forcing anything here would break the retry meant to rescue it.
        recorder = GSRRecorder(
            Config(
                output=OutputConfig(directory="/tmp/vice-test"),
                recording=RecordingConfig(encoder="hevc_nvenc"),
            )
        )
        with mock.patch("vice.recorder._gsr_supported_codecs", return_value=frozenset({"hevc"})):
            self.assertNotIn("-k", recorder._build_cmd(cpu_encoder=True))
            self.assertIn("-k", recorder._build_cmd(cpu_encoder=False))

    def test_build_cmd_swaps_an_unsupported_codec(self) -> None:
        recorder = GSRRecorder(
            Config(
                output=OutputConfig(directory="/tmp/vice-test"),
                recording=RecordingConfig(encoder="h264_nvenc"),
            )
        )
        with mock.patch("vice.recorder._gsr_supported_codecs", return_value=frozenset({"hevc", "av1"})):
            cmd = recorder._build_cmd()
        self.assertEqual(cmd[cmd.index("-k") + 1], "hevc")
        with mock.patch("vice.recorder._gsr_supported_codecs", return_value=frozenset()):
            cmd = recorder._build_cmd()
        self.assertEqual(cmd[cmd.index("-k") + 1], "h264")


async def _no_stderr_reader() -> None:
    return None


class GSRStartFallbackTests(unittest.IsolatedAsyncioTestCase):
    """The GPU is worth one more try before dropping to CPU encoding (#156)."""

    def _recorder(self, encoder: str = "h264_nvenc") -> GSRRecorder:
        return GSRRecorder(
            Config(
                output=OutputConfig(directory="/tmp/vice-test"),
                recording=RecordingConfig(encoder=encoder),
            )
        )

    @staticmethod
    def _tracker(attempts, succeed_on):
        async def fake_try_start(cpu_encoder: bool, avoid_codec=None):
            attempts.append((cpu_encoder, avoid_codec))
            if succeed_on(cpu_encoder, avoid_codec):
                return None
            return "gsr error: Could not open video codec"
        return fake_try_start

    async def _run(self, recorder, fake_try_start, supported, mismatch=None):
        with mock.patch("vice.recorder._gsr_supported_codecs", return_value=supported), \
             mock.patch("vice.recorder._gsr_nvenc_api_mismatch", return_value=mismatch), \
             mock.patch("vice.recorder._gsr_supports_flag", return_value=True), \
             mock.patch.object(recorder, "_try_start", side_effect=fake_try_start), \
             mock.patch.object(recorder, "_stderr_reader", new=_no_stderr_reader):
            await recorder.start()

    async def test_a_driver_too_old_for_ffmpeg_is_named_as_the_cause(self) -> None:
        # #247: a GTX 970 on the 580 driver against Arch's FFmpeg 9.
        recorder = self._recorder()
        mismatch = {"kind": "nvenc_api", "driver": "13.0", "required": "13.1"}
        # The banner also says whether the Flatpak build, which still works
        # on this driver, is there to switch to.
        with mock.patch("vice.recorder.gsr_flatpak_installed", return_value=True), \
             mock.patch("vice.recorder.gsr_source", return_value="native"):
            await self._run(recorder, self._tracker([], lambda cpu, avoid: cpu), frozenset(), mismatch)

        self.assertTrue(recorder.cpu_fallback)
        self.assertEqual(recorder.cpu_fallback_reason,
                         {**mismatch, "flatpak": True, "source": "native"})

    async def test_an_ordinary_cpu_fallback_carries_no_reason(self) -> None:
        recorder = self._recorder()
        await self._run(recorder, self._tracker([], lambda cpu, avoid: cpu), frozenset())

        self.assertTrue(recorder.cpu_fallback)
        self.assertIsNone(recorder.cpu_fallback_reason)

    def test_the_nvenc_api_warning_is_read_from_gsr_info(self) -> None:
        # Verbatim from gpu-screen-recorder 6.1.0 --info in #206.
        info = (
            "section=video_codecs\n"
            "gsr warning: gsr_get_supported_video_codecs_nvenc: your nvidia driver only "
            "supports nvenc api version 13.0, but the FFmpeg version that GPU Screen "
            "Recorder uses requires nvenc api version 13.1. Update your nvidia driver "
            "or use an older FFmpeg version to record with nvenc\nh264_software\n"
        )
        from vice.recorder import _gsr_nvenc_api_mismatch
        with mock.patch("vice.recorder._has", return_value=True), \
             mock.patch("vice.recorder._run_command_capture", return_value=(0, info)):
            self.assertEqual(_gsr_nvenc_api_mismatch(),
                             {"kind": "nvenc_api", "driver": "13.0", "required": "13.1"})
        with mock.patch("vice.recorder._has", return_value=True), \
             mock.patch("vice.recorder._run_command_capture", return_value=(0, "section=video_codecs\nh264\n")):
            self.assertIsNone(_gsr_nvenc_api_mismatch())

    async def test_another_codec_is_tried_before_the_cpu(self) -> None:
        recorder = self._recorder()
        attempts: list = []
        await self._run(
            recorder,
            self._tracker(attempts, lambda cpu, avoid: avoid is not None),
            frozenset({"h264", "hevc", "av1"}),
        )

        self.assertEqual(attempts, [(False, None), (False, "h264")])
        self.assertTrue(recorder.codec_fallback)
        self.assertFalse(recorder.cpu_fallback)

    async def test_cpu_fallback_still_happens_when_no_codec_works(self) -> None:
        recorder = self._recorder()
        attempts: list = []
        await self._run(
            recorder,
            self._tracker(attempts, lambda cpu, avoid: cpu),
            frozenset({"h264", "hevc", "av1"}),
        )

        self.assertEqual(attempts, [(False, None), (False, "h264"), (True, None)])
        self.assertTrue(recorder.cpu_fallback)
        self.assertFalse(recorder.codec_fallback)

    async def test_no_capability_info_goes_straight_to_the_cpu(self) -> None:
        """Guessing a codec blind could be worse than CPU encoding, and this
        is the path every machine without gpu-screen-recorder --info takes."""
        recorder = self._recorder()
        attempts: list = []
        await self._run(
            recorder,
            self._tracker(attempts, lambda cpu, avoid: cpu),
            frozenset(),
        )

        self.assertEqual(attempts, [(False, None), (True, None)])
        self.assertTrue(recorder.cpu_fallback)

    async def test_auto_encoder_avoids_the_default_codec_on_retry(self) -> None:
        # encoder=auto sends no -k, and GSR's default is h264, so a failure
        # there still has somewhere better to go.
        recorder = self._recorder(encoder="auto")
        attempts: list = []
        await self._run(
            recorder,
            self._tracker(attempts, lambda cpu, avoid: cpu),
            frozenset({"h264", "hevc"}),
        )

        # Nothing was forced, so there is no codec to avoid and no retry.
        self.assertEqual(attempts, [(False, None), (True, None)])

    async def test_no_hardware_codec_error_reaches_cpu_fallback(self) -> None:
        recorder = self._recorder(encoder="auto")
        attempts: list = []

        async def fail_gpu_then_start_cpu(cpu_encoder: bool, avoid_codec=None):
            attempts.append((cpu_encoder, avoid_codec))
            if cpu_encoder:
                return None
            return (
                "gsr error: no video encoder was specified and neither h264, "
                "hevc nor av1 are supported on your system"
            )

        await self._run(recorder, fail_gpu_then_start_cpu, frozenset({"h264_software", "vp9"}))

        self.assertEqual(attempts, [(False, None), (True, None)])
        self.assertTrue(recorder.cpu_fallback)

    async def test_a_healthy_start_tries_once(self) -> None:
        recorder = self._recorder()
        attempts: list = []
        await self._run(
            recorder,
            self._tracker(attempts, lambda cpu, avoid: True),
            frozenset({"h264", "hevc"}),
        )

        self.assertEqual(attempts, [(False, None)])
        self.assertFalse(recorder.cpu_fallback)
        self.assertFalse(recorder.codec_fallback)


class RecorderAudioCommandTests(unittest.TestCase):
    def test_list_display_options_parses_gsr_capture_options(self) -> None:
        gsr_out = "\n".join(
            [
                "window",
                "screen",
                "DP-1|2560x1440",
                "HDMI-A-1|1920x1080",
            ]
        )

        with mock.patch("vice.recorder._has", side_effect=lambda tool: tool == "gpu-screen-recorder"):
            with mock.patch("vice.recorder._run_command_capture", return_value=(0, gsr_out)):
                info = list_display_options("gsr")

        self.assertEqual(info["backend"], "gsr")
        self.assertEqual([d["id"] for d in info["displays"]], ["DP-1", "HDMI-A-1"])
        self.assertEqual(info["displays"][0]["label"], "DP-1 (2560x1440)")

    def test_list_display_options_parses_quoted_gsr_monitors(self) -> None:
        gsr_out = "\n".join(
            [
                '"DP-4" (1920x1080+1920+0)',
                '"HDMI-A-1" (1920x1080+0+0)',
            ]
        )

        with mock.patch("vice.recorder._has", side_effect=lambda tool: tool == "gpu-screen-recorder"):
            with mock.patch("vice.recorder._run_command_capture", return_value=(0, gsr_out)):
                info = list_display_options("gsr")

        self.assertEqual(info["backend"], "gsr")
        self.assertEqual([d["id"] for d in info["displays"]], ["DP-4", "HDMI-A-1"])
        self.assertEqual(info["displays"][0]["label"], "DP-4 (1920x1080+1920+0)")

    def test_list_display_options_parses_xrandr_monitors(self) -> None:
        xrandr_out = "\n".join(
            [
                "Monitors: 2",
                " 0: +*DP-1 2560/600x1440/340+1920+0  DP-1",
                " 1: +HDMI-1 1920/520x1080/290+0+0  HDMI-1",
            ]
        )

        with mock.patch("vice.recorder._has", side_effect=lambda tool: tool == "xrandr"):
            with mock.patch("vice.recorder.subprocess.check_output", return_value=xrandr_out):
                info = list_display_options("ffmpeg")

        self.assertEqual(info["backend"], "ffmpeg")
        self.assertEqual([d["id"] for d in info["displays"]], ["DP-1", "HDMI-1"])
        self.assertEqual(info["displays"][0]["x"], 1920)
        self.assertEqual(info["displays"][0]["width"], 2560)

    def test_gsr_build_cmd_includes_desktop_and_microphone_audio(self) -> None:
        recorder = GSRRecorder(
            Config(
                output=OutputConfig(directory="/tmp/vice-test"),
                recording=RecordingConfig(
                    capture_audio=True,
                    capture_microphone=True,
                ),
            )
        )

        cmd = recorder._build_cmd()

        self.assertIn("-a", cmd)
        idx = cmd.index("-a")
        self.assertEqual(cmd[idx + 1], "default_output|default_input")

    def test_gsr_build_cmd_uses_selected_audio_source(self) -> None:
        recorder = GSRRecorder(
            Config(
                output=OutputConfig(directory="/tmp/vice-test"),
                recording=RecordingConfig(
                    capture_audio=True,
                    gsr_audio_source="app:firefox",
                ),
            )
        )

        cmd = recorder._build_cmd()

        self.assertEqual(cmd[cmd.index("-a") + 1], "app:firefox")

    def test_gsr_build_cmd_combines_selected_audio_source_with_microphone(self) -> None:
        recorder = GSRRecorder(
            Config(
                output=OutputConfig(directory="/tmp/vice-test"),
                recording=RecordingConfig(
                    capture_audio=True,
                    capture_microphone=True,
                    gsr_audio_source="device:game.monitor",
                ),
            )
        )

        cmd = recorder._build_cmd()

        self.assertEqual(cmd[cmd.index("-a") + 1], "device:game.monitor|default_input")

    def test_gsr_build_cmd_passes_configured_resolution(self) -> None:
        recorder = GSRRecorder(
            Config(
                output=OutputConfig(directory="/tmp/vice-test"),
                recording=RecordingConfig(resolution="1280x720"),
            )
        )

        cmd = recorder._build_cmd()

        self.assertEqual(cmd[cmd.index("-s") + 1], "1280x720")

    def test_gsr_build_cmd_ignores_invalid_resolution(self) -> None:
        recorder = GSRRecorder(
            Config(
                output=OutputConfig(directory="/tmp/vice-test"),
                recording=RecordingConfig(resolution="720p"),
            )
        )

        cmd = recorder._build_cmd()

        self.assertNotIn("-s", cmd)

    def test_gsr_build_cmd_keeps_user_resolution_override(self) -> None:
        recorder = GSRRecorder(
            Config(
                output=OutputConfig(directory="/tmp/vice-test"),
                recording=RecordingConfig(
                    resolution="1280x720",
                    gsr_args="-s 640x360",
                ),
            )
        )

        cmd = recorder._build_cmd()

        self.assertEqual(cmd.count("-s"), 1)
        self.assertEqual(cmd[cmd.index("-s") + 1], "640x360")

    def test_gsr_session_cmd_passes_configured_resolution(self) -> None:
        rc = RecordingConfig(resolution="1920x1080")

        cmd = GSRRecorder._gsr_session_cmd(Path("/tmp/vice-test/out.mp4"), rc)

        self.assertEqual(cmd[cmd.index("-s") + 1], "1920x1080")

    def test_gsr_session_cmd_uses_configured_container(self) -> None:
        rc = RecordingConfig(container="mkv")

        cmd = GSRRecorder._gsr_session_cmd(Path("/tmp/vice-test/out.mkv"), rc)

        self.assertEqual(cmd[cmd.index("-c") + 1], "mkv")

    def test_gsr_build_cmd_uses_configured_container(self) -> None:
        recorder = GSRRecorder(
            Config(
                output=OutputConfig(directory="/tmp/vice-test"),
                recording=RecordingConfig(container="mkv"),
            )
        )

        cmd = recorder._build_cmd()

        self.assertEqual(cmd[cmd.index("-c") + 1], "mkv")

    def test_gsr_build_cmd_falls_back_to_mp4_for_unknown_container(self) -> None:
        recorder = GSRRecorder(
            Config(
                output=OutputConfig(directory="/tmp/vice-test"),
                recording=RecordingConfig(container="avi"),
            )
        )

        cmd = recorder._build_cmd()

        self.assertEqual(cmd[cmd.index("-c") + 1], "mp4")

    def test_gsr_build_cmd_emits_one_audio_flag_per_track(self) -> None:
        recorder = GSRRecorder(
            Config(
                output=OutputConfig(directory="/tmp/vice-test"),
                recording=RecordingConfig(
                    capture_audio=True,
                    audio_tracks=["default_output", "default_input", "app:Discord"],
                ),
            )
        )

        cmd = recorder._build_cmd()

        audio_values = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-a"]
        self.assertEqual(
            audio_values, ["default_output", "default_input", "app:Discord"]
        )

    def test_gsr_build_cmd_appends_microphone_track_when_mic_enabled(self) -> None:
        recorder = GSRRecorder(
            Config(
                output=OutputConfig(directory="/tmp/vice-test"),
                recording=RecordingConfig(
                    capture_audio=True,
                    capture_microphone=True,
                    audio_tracks=["default_output", "app:Discord"],
                ),
            )
        )

        cmd = recorder._build_cmd()

        audio_values = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-a"]
        self.assertEqual(
            audio_values, ["default_output", "app:Discord", "default_input"]
        )

    def test_gsr_build_cmd_does_not_duplicate_microphone_track(self) -> None:
        recorder = GSRRecorder(
            Config(
                output=OutputConfig(directory="/tmp/vice-test"),
                recording=RecordingConfig(
                    capture_audio=True,
                    capture_microphone=True,
                    audio_tracks=["default_output", "default_input"],
                ),
            )
        )

        cmd = recorder._build_cmd()

        audio_values = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-a"]
        self.assertEqual(audio_values, ["default_output", "default_input"])

    def test_gsr_build_cmd_uses_configured_microphone_source_for_tracks(self) -> None:
        recorder = GSRRecorder(
            Config(
                output=OutputConfig(directory="/tmp/vice-test"),
                recording=RecordingConfig(
                    capture_audio=True,
                    capture_microphone=True,
                    microphone_source="device:alsa_input.usb-guitar",
                    audio_tracks=["default_output"],
                ),
            )
        )

        cmd = recorder._build_cmd()

        audio_values = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-a"]
        self.assertEqual(
            audio_values, ["default_output", "device:alsa_input.usb-guitar"]
        )

    def test_gsr_build_cmd_mixed_audio_uses_configured_microphone_source(self) -> None:
        recorder = GSRRecorder(
            Config(
                output=OutputConfig(directory="/tmp/vice-test"),
                recording=RecordingConfig(
                    capture_audio=True,
                    capture_microphone=True,
                    microphone_source="device:alsa_input.usb-guitar",
                ),
            )
        )

        cmd = recorder._build_cmd()

        self.assertEqual(
            cmd[cmd.index("-a") + 1], "default_output|device:alsa_input.usb-guitar"
        )

    def test_gsr_build_cmd_drops_tracks_when_desktop_audio_disabled(self) -> None:
        recorder = GSRRecorder(
            Config(
                output=OutputConfig(directory="/tmp/vice-test"),
                recording=RecordingConfig(
                    capture_audio=False,
                    capture_microphone=True,
                    audio_tracks=["default_output", "app:Discord"],
                ),
            )
        )

        cmd = recorder._build_cmd()

        audio_values = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-a"]
        self.assertEqual(audio_values, ["default_input"])

    def test_gsr_build_cmd_mix_first_prepends_combined_track(self) -> None:
        recorder = GSRRecorder(
            Config(
                output=OutputConfig(directory="/tmp/vice-test"),
                recording=RecordingConfig(
                    capture_audio=True,
                    capture_microphone=True,
                    audio_tracks=["default_output", "app:Discord"],
                    audio_tracks_mix_first=True,
                ),
            )
        )

        cmd = recorder._build_cmd()

        audio_values = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-a"]
        self.assertEqual(
            audio_values,
            [
                "default_output|default_input",
                "default_output",
                "app:Discord",
                "default_input",
            ],
        )

    def test_mix_first_does_not_put_app_and_inverse_app_in_one_gsr_track(self) -> None:
        from vice.recorder import _gsr_audio_args

        rc = RecordingConfig(
            capture_audio=True,
            audio_tracks=["default_output", "app:Discord", "app-inverse:Discord"],
            audio_tracks_mix_first=True,
        )

        self.assertEqual(
            _gsr_audio_args(rc),
            [
                "-a", "default_output",
                "-a", "default_output",
                "-a", "app:Discord",
                "-a", "app-inverse:Discord",
            ],
        )

    def test_mix_first_keeps_app_directions_separate_without_monitor(self) -> None:
        from vice.recorder import _gsr_audio_args

        rc = RecordingConfig(
            capture_audio=True,
            audio_tracks=["app:Discord", "app-inverse:Discord", "default_input"],
            audio_tracks_mix_first=True,
        )

        with self.assertLogs("vice.recorder", level="WARNING") as logs:
            args = _gsr_audio_args(rc)
        self.assertEqual(
            args,
            ["-a", "app:Discord", "-a", "app-inverse:Discord", "-a", "default_input"],
        )
        self.assertIn("Cannot create a combined audio track", "\n".join(logs.output))

    def test_gsr_build_cmd_mix_first_skipped_for_single_track(self) -> None:
        recorder = GSRRecorder(
            Config(
                output=OutputConfig(directory="/tmp/vice-test"),
                recording=RecordingConfig(
                    capture_audio=True,
                    audio_tracks=["default_output"],
                    audio_tracks_mix_first=True,
                ),
            )
        )

        cmd = recorder._build_cmd()

        audio_values = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-a"]
        self.assertEqual(audio_values, ["default_output"])

    def test_wf_audio_device_uses_configured_microphone_source(self) -> None:
        rc = RecordingConfig(
            capture_audio=False,
            capture_microphone=True,
            microphone_source="device:alsa_input.usb-guitar",
        )

        self.assertEqual(_wf_audio_device(rc), "alsa_input.usb-guitar")

    def test_gsr_build_cmd_maps_hevc_encoder_to_gsr_codec(self) -> None:
        recorder = GSRRecorder(
            Config(
                output=OutputConfig(directory="/tmp/vice-test"),
                recording=RecordingConfig(encoder="hevc_vaapi"),
            )
        )

        cmd = recorder._build_cmd()

        self.assertEqual(cmd[cmd.index("-k") + 1], "hevc")

    def test_gsr_build_cmd_respects_user_codec_arg(self) -> None:
        recorder = GSRRecorder(
            Config(
                output=OutputConfig(directory="/tmp/vice-test"),
                recording=RecordingConfig(encoder="hevc_vaapi", gsr_args="-k av1"),
            )
        )

        cmd = recorder._build_cmd()

        self.assertEqual(cmd.count("-k"), 1)
        self.assertEqual(cmd[cmd.index("-k") + 1], "av1")

    def test_gsr_session_cmd_maps_hevc_encoder_to_gsr_codec(self) -> None:
        cmd = GSRRecorder._gsr_session_cmd(
            Path("/tmp/session.mp4"),
            RecordingConfig(encoder="hevc_nvenc"),
        )

        self.assertEqual(cmd[cmd.index("-k") + 1], "hevc")

    def test_hevc_vaapi_encoder_flags(self) -> None:
        flags = _encoder_flags("hevc_vaapi", 23)

        self.assertIn("-c:v", flags)
        self.assertEqual(flags[flags.index("-c:v") + 1], "hevc_vaapi")

    def test_gsr_build_cmd_maps_av1_encoders_to_gsr_codec(self) -> None:
        for encoder in ("av1_nvenc", "av1_vaapi", "av1"):
            recorder = GSRRecorder(
                Config(
                    output=OutputConfig(directory="/tmp/vice-test"),
                    recording=RecordingConfig(encoder=encoder),
                )
            )

            cmd = recorder._build_cmd()

            self.assertEqual(cmd[cmd.index("-k") + 1], "av1", encoder)

    def test_av1_encoder_flags_use_hardware_branches(self) -> None:
        nvenc = _encoder_flags("av1_nvenc", 23)
        vaapi = _encoder_flags("av1_vaapi", 23)

        self.assertIn("-cq", nvenc)
        self.assertEqual(nvenc[nvenc.index("-c:v") + 1], "av1_nvenc")
        self.assertIn("-qp", vaapi)
        self.assertEqual(vaapi[vaapi.index("-c:v") + 1], "av1_vaapi")

    def test_list_gsr_audio_sources_parses_devices_and_apps(self) -> None:
        def fake_run(cmd, timeout=5.0):
            if "--list-audio-devices" in cmd:
                # GSR format: "name|Human description". The default entries
                # must be deduped against the hardcoded friendly ones.
                return 0, (
                    "default_output|Default output\n"
                    "default_input|Default input\n"
                    "alsa_output.game.monitor|Monitor of Game Audio\n"
                )
            if "--list-application-audio" in cmd:
                return 0, "Firefox\nDiscord\n"
            return 1, ""

        with mock.patch("vice.recorder._has", side_effect=lambda tool: tool == "gpu-screen-recorder"):
            with mock.patch("vice.recorder._run_command_capture", side_effect=fake_run):
                payload = list_gsr_audio_sources()

        ids = [source["id"] for source in payload["sources"]]
        self.assertIn("default_output", ids)
        self.assertIn("device:alsa_output.game.monitor", ids)
        self.assertIn("app:Firefox", ids)
        self.assertIn("app-inverse:Firefox", ids)
        # The description must land in the label, never in the id.
        self.assertEqual(ids.count("default_output"), 1)
        self.assertNotIn("device:default_output", ids)
        by_id = {s["id"]: s["label"] for s in payload["sources"]}
        self.assertEqual(by_id["device:alsa_output.game.monitor"], "Device: Monitor of Game Audio")
        for source_id in ids:
            self.assertNotIn("|", source_id)

    def test_gsr_build_cmd_defaults_to_screen_on_x11(self) -> None:
        recorder = GSRRecorder(
            Config(output=OutputConfig(directory="/tmp/vice-test"))
        )

        with mock.patch("vice.recorder._is_wayland", return_value=False):
            with mock.patch.dict(os.environ, {"DISPLAY": ":0"}, clear=False):
                cmd = recorder._build_cmd()

        self.assertIn("-w", cmd)
        self.assertEqual(cmd[cmd.index("-w") + 1], "screen")

    def test_gsr_build_cmd_uses_selected_display(self) -> None:
        recorder = GSRRecorder(
            Config(
                output=OutputConfig(directory="/tmp/vice-test"),
                recording=RecordingConfig(display="DP-1"),
            )
        )

        with mock.patch("vice.recorder._display_options", return_value=[{"id": "DP-1", "label": "DP-1"}]):
            cmd = recorder._build_cmd()

        self.assertIn("-w", cmd)
        self.assertEqual(cmd[cmd.index("-w") + 1], "DP-1")

    def test_gsr_build_cmd_accepts_legacy_pipe_form_display_value(self) -> None:
        recorder = GSRRecorder(
            Config(
                output=OutputConfig(directory="/tmp/vice-test"),
                recording=RecordingConfig(display="DP-1|2560x1440"),
            )
        )

        with mock.patch(
            "vice.recorder._display_options",
            return_value=[{"id": "DP-1", "label": "DP-1 (2560x1440)"}],
        ):
            cmd = recorder._build_cmd()

        self.assertIn("-w", cmd)
        self.assertEqual(cmd[cmd.index("-w") + 1], "DP-1")

    def test_ffmpeg_segment_cmd_mixes_desktop_and_microphone_audio(self) -> None:
        recorder = SegmentRecorder(
            Config(
                recording=RecordingConfig(
                    capture_audio=True,
                    capture_microphone=True,
                )
            ),
            use_wf_recorder=False,
        )

        with mock.patch("vice.recorder._desktop_audio_source", return_value="desk.monitor"):
            with mock.patch("vice.recorder._microphone_audio_source", return_value="mic.input"):
                cmd = recorder._ffmpeg_x11_cmd(Path("/tmp/out.mp4"))

        self.assertIn("desk.monitor", cmd)
        self.assertIn("mic.input", cmd)
        self.assertIn("-filter_complex", cmd)
        self.assertIn("[1:a][2:a]amix=inputs=2:normalize=0[aout]", cmd)

    def test_ffmpeg_segment_cmd_uses_selected_monitor_geometry(self) -> None:
        recorder = SegmentRecorder(
            Config(
                recording=RecordingConfig(
                    display="DP-1",
                    resolution="1920x1080",
                )
            ),
            use_wf_recorder=False,
        )

        displays = [{"id": "DP-1", "label": "DP-1", "width": 2560, "height": 1440, "x": 1920, "y": 0}]
        with mock.patch("vice.recorder._display_options", return_value=displays):
            cmd = recorder._ffmpeg_x11_cmd(Path("/tmp/out.mp4"))

        self.assertIn("-video_size", cmd)
        self.assertEqual(cmd[cmd.index("-video_size") + 1], "2560x1440")
        self.assertIn("-i", cmd)
        self.assertEqual(cmd[cmd.index("-i") + 1], ":0+1920,0")
        self.assertIn("-vf", cmd)
        self.assertIn("scale=1920:1080", cmd[cmd.index("-vf") + 1])

    def test_wf_recorder_uses_microphone_only_strategy(self) -> None:
        recorder = SegmentRecorder(
            Config(
                recording=RecordingConfig(
                    capture_audio=True,
                    capture_microphone=True,
                    wf_microphone_strategy="mic_only",
                )
            ),
            use_wf_recorder=True,
        )

        with mock.patch("vice.recorder._microphone_audio_source", return_value="mic.input"):
            cmd = recorder._wf_recorder_cmd(Path("/tmp/out.mp4"))

        self.assertIn("--audio=mic.input", cmd)

    def test_wf_recorder_uses_selected_display(self) -> None:
        recorder = SegmentRecorder(
            Config(recording=RecordingConfig(display="DP-1")),
            use_wf_recorder=True,
        )

        with mock.patch("vice.recorder._display_options", return_value=[{"id": "DP-1", "label": "DP-1"}]):
            with mock.patch("vice.recorder._wf_supports_flag", return_value=False):
                cmd = recorder._wf_recorder_cmd(Path("/tmp/out.mp4"))

        self.assertIn("-o", cmd)
        self.assertEqual(cmd[cmd.index("-o") + 1], "DP-1")
        self.assertNotIn("--force-yuv", cmd)

    def test_wf_recorder_includes_force_yuv_when_supported(self) -> None:
        recorder = SegmentRecorder(
            Config(recording=RecordingConfig()),
            use_wf_recorder=True,
        )

        with mock.patch("vice.recorder._wf_supports_flag", return_value=True):
            cmd = recorder._wf_recorder_cmd(Path("/tmp/out.mp4"))

        self.assertIn("--force-yuv", cmd)

    def test_list_display_options_warns_for_legacy_wf_recorder_listing(self) -> None:
        proc = subprocess.CompletedProcess(
            ["wf-recorder", "-L"],
            1,
            "",
            "wf-recorder: invalid option -- 'L'\nUnsupported command line argument (null)\n",
        )

        with mock.patch("vice.recorder._has", side_effect=lambda tool: tool == "wf-recorder"):
            with mock.patch("vice.recorder.subprocess.run", return_value=proc):
                info = list_display_options("wf-recorder")

        self.assertEqual(info["backend"], "wf-recorder")
        self.assertEqual(info["displays"], [])
        self.assertEqual(
            info["warning"],
            "installed wf-recorder does not support output listing (-L)",
        )

    def test_create_recorder_uses_compat_backend_for_wf_microphone_mode(self) -> None:
        cfg = Config(
            recording=RecordingConfig(
                backend="wf-recorder",
                capture_audio=True,
                capture_microphone=True,
                wf_microphone_strategy="backend_fallback",
            )
        )

        with mock.patch("vice.recorder._has") as has_mock:
            with mock.patch("vice.recorder._is_wayland", return_value=True):
                with mock.patch("vice.recorder._is_x11", return_value=False):
                    has_mock.side_effect = lambda tool: tool == "gpu-screen-recorder"
                    recorder = create_recorder(cfg)

        self.assertIsInstance(recorder, GSRRecorder)

    def test_create_recorder_rejects_wf_microphone_prompt_mode(self) -> None:
        cfg = Config(
            recording=RecordingConfig(
                backend="wf-recorder",
                capture_audio=True,
                capture_microphone=True,
                wf_microphone_strategy="prompt",
            )
        )

        with mock.patch("vice.recorder._has", side_effect=lambda tool: tool == "wf-recorder"):
            with mock.patch("vice.recorder._is_wayland", return_value=True):
                with mock.patch("vice.recorder._is_x11", return_value=False):
                    with self.assertRaises(RuntimeError):
                        create_recorder(cfg)

    def test_create_recorder_reports_missing_wayland_backend(self) -> None:
        cfg = Config(recording=RecordingConfig(backend="auto"))

        with mock.patch("vice.recorder._has", return_value=False):
            with mock.patch("vice.recorder._is_wayland", return_value=True):
                with mock.patch("vice.recorder._is_x11", return_value=False):
                    with self.assertRaises(RuntimeError) as ctx:
                        create_recorder(cfg)

        self.assertIn("gpu-screen-recorder is required", str(ctx.exception))
        self.assertIn("recording.backend", str(ctx.exception))


class _FakeStream:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def read(self) -> bytes:
        return self._data

    # A real StreamReader iterates line by line, which is how the stderr
    # reader consumes it.
    def __aiter__(self):
        async def _lines():
            for line in self._data.splitlines(keepends=True):
                yield line
        return _lines()


class _FakeProcess:
    # A pid is part of the interface now: capture processes are killed by
    # group, so anything standing in for one needs to be addressable (#129).
    def __init__(self, returncode: int, stderr: bytes = b"", pid: int = 424242) -> None:
        self.returncode = returncode
        self.stderr = _FakeStream(stderr)
        self.pid = pid

    async def wait(self) -> int:
        return self.returncode

    def terminate(self) -> None:
        return None

    def kill(self) -> None:
        return None


def _capture_killpg():
    """Patch os.killpg and record (pgid, signal) instead of signalling a real
    process group. Tests must never send signals outside themselves."""
    calls: list[tuple[int, int]] = []
    patcher = mock.patch(
        "vice.recorder.os.killpg",
        side_effect=lambda pgid, sig: calls.append((pgid, sig)),
    )
    return patcher, calls


class RecorderSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_gsr_start_raises_when_process_exits_immediately(self) -> None:
        recorder = GSRRecorder(
            Config(output=OutputConfig(directory="/tmp/vice-test"))
        )
        proc = _FakeProcess(
            2,
            b"gpu-screen-recorder: invalid capture target DP-1|2560x1440\n",
        )

        killpg, kills = _capture_killpg()
        with killpg, mock.patch(
            "vice.recorder.asyncio.create_subprocess_exec",
            new=mock.AsyncMock(return_value=proc),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                await recorder.start()

        self.assertIn("gpu-screen-recorder failed to start", str(ctx.exception))
        # GSR can fork gsr-kms-server before giving up, so a failed start has
        # to take the group with it (#129).
        self.assertIn((proc.pid, signal.SIGKILL), kills)

    async def test_gsr_start_reports_error_line_instead_of_monitor_listing(self) -> None:
        recorder = GSRRecorder(
            Config(output=OutputConfig(directory="/tmp/vice-test"))
        )
        proc = _FakeProcess(
            2,
            b"gpu-screen-recorder: invalid capture target :0\n"
            b"Available monitors:\n"
            b'"DP-4" (1920x1080+1920+0)\n',
        )

        killpg, _ = _capture_killpg()
        with killpg, mock.patch(
            "vice.recorder.asyncio.create_subprocess_exec",
            new=mock.AsyncMock(return_value=proc),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                await recorder.start()

        message = str(ctx.exception)
        self.assertIn("invalid capture target :0", message)
        self.assertNotIn('"DP-4"', message)

    async def test_segment_start_raises_when_first_segment_exits_immediately(self) -> None:
        recorder = SegmentRecorder(
            Config(output=OutputConfig(directory="/tmp/vice-test")),
            use_wf_recorder=True,
        )
        proc = _FakeProcess(
            2,
            b"wf-recorder: unknown output DP-1\n",
        )

        with mock.patch("vice.recorder._wf_supports_flag", return_value=False):
            with mock.patch(
                "vice.recorder.asyncio.create_subprocess_exec",
                new=mock.AsyncMock(return_value=proc),
            ):
                with self.assertRaises(RuntimeError) as ctx:
                    await recorder.start()

        self.assertIn("wf-recorder failed to start", str(ctx.exception))

    async def test_start_session_returns_none_when_recorder_exits_immediately(self) -> None:
        recorder = SegmentRecorder(
            Config(output=OutputConfig(directory="/tmp/vice-test")),
            use_wf_recorder=True,
        )
        proc = _FakeProcess(
            2,
            b"wf-recorder: unrecognized option '--force-yuv'\n",
        )

        killpg, _ = _capture_killpg()
        with killpg, mock.patch("vice.recorder._is_wayland", return_value=True):
            with mock.patch("vice.recorder._has", side_effect=lambda tool: tool == "wf-recorder"):
                with mock.patch("vice.recorder._wf_supports_flag", return_value=False):
                    with mock.patch(
                        "vice.recorder.asyncio.create_subprocess_exec",
                        new=mock.AsyncMock(return_value=proc),
                    ):
                        path = await recorder.start_session()

        self.assertIsNone(path)

    async def test_gsr_runs_in_its_own_process_group(self) -> None:
        recorder = GSRRecorder(
            Config(output=OutputConfig(directory="/tmp/vice-test"))
        )
        proc = _FakeProcess(None)

        async def _never_exits() -> int:
            await asyncio.sleep(3600)
            return 0

        proc.wait = _never_exits  # type: ignore[method-assign]
        spawn = mock.AsyncMock(return_value=proc)
        with mock.patch("vice.recorder.asyncio.create_subprocess_exec", new=spawn):
            await recorder.start()
        if recorder._watch_task:
            recorder._watch_task.cancel()

        # Without its own session, signalling GSR leaves gsr-kms-server holding
        # the stderr pipe open and the fd is never reclaimed (#129).
        self.assertTrue(spawn.call_args.kwargs.get("start_new_session"))

    async def test_gsr_stop_kills_the_whole_group(self) -> None:
        recorder = GSRRecorder(
            Config(output=OutputConfig(directory="/tmp/vice-test"))
        )
        proc = _FakeProcess(0)
        recorder._proc = proc
        recorder._running = True

        killpg, kills = _capture_killpg()
        with killpg:
            await recorder.stop()

        # Terminate the group, then make sure any forked helper is gone.
        self.assertEqual(kills, [(proc.pid, signal.SIGTERM), (proc.pid, signal.SIGKILL)])
        self.assertIsNone(recorder._proc)

    async def test_gsr_keeps_stderr_for_diagnosing_a_death(self) -> None:
        recorder = GSRRecorder(
            Config(output=OutputConfig(directory="/tmp/vice-test"))
        )
        proc = _FakeProcess(
            None,
            b"gsr error: no encoder found\n"
            + b"update fps: 60, damage fps: 60\n" * 30
            + b"fatal: giving up\n",
        )
        recorder._proc = proc
        await recorder._stderr_reader()

        # The watchdog logs this; without it a fatal encoder error is invisible
        # at the default log level (#129). GSR's per-second throughput line
        # would otherwise evict the only line that matters.
        self.assertIn("no encoder found", recorder.last_output())
        self.assertIn("giving up", recorder.last_output())
        self.assertNotIn("damage fps", recorder.last_output())

    async def test_gsr_reports_nothing_when_it_said_nothing(self) -> None:
        recorder = GSRRecorder(
            Config(output=OutputConfig(directory="/tmp/vice-test"))
        )
        recorder._proc = _FakeProcess(None, b"update fps: 60, damage fps: 60\n" * 5)
        await recorder._stderr_reader()

        # A process that was simply killed has nothing to explain, and an
        # empty tail keeps the watchdog's log line short.
        self.assertEqual(recorder.last_output(), "")


class RecordingLimitTests(unittest.TestCase):
    def test_load_clamps_oversized_durations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / ".config" / "vice"
            config_dir.mkdir(parents=True)
            config_path = config_dir / "config.toml"
            config_path.write_text(
                "[recording]\n"
                "buffer_duration = 999999\n"
                "clip_duration = 99999\n"
                "gsr_replay_storage = \"floppy\"\n"
            )

            with mock.patch.object(config_mod, "CONFIG_DIR", config_dir):
                with mock.patch.object(config_mod, "CONFIG_PATH", config_path):
                    loaded = config_mod.load()

        self.assertEqual(loaded.recording.clip_duration, 1800)
        self.assertEqual(loaded.recording.buffer_duration, 1800)
        self.assertEqual(loaded.recording.gsr_replay_storage, "auto")

    def test_clamp_falls_back_on_non_numeric_values(self) -> None:
        cfg = Config(recording=RecordingConfig())
        cfg.recording.buffer_duration = "lots"
        cfg.recording.clip_duration = None
        config_mod.clamp_recording_limits(cfg)

        self.assertEqual(cfg.recording.clip_duration, 20)
        self.assertEqual(cfg.recording.buffer_duration, 120)

    def test_clamp_keeps_buffer_covering_clip(self) -> None:
        cfg = Config(recording=RecordingConfig(buffer_duration=30, clip_duration=300))
        config_mod.clamp_recording_limits(cfg)

        self.assertEqual(cfg.recording.clip_duration, 300)
        self.assertGreaterEqual(cfg.recording.buffer_duration, 300)

    def test_replay_storage_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / ".config" / "vice"
            config_dir.mkdir(parents=True)
            config_path = config_dir / "config.toml"

            with mock.patch.object(config_mod, "CONFIG_DIR", config_dir):
                with mock.patch.object(config_mod, "CONFIG_PATH", config_path):
                    config_mod.save(Config(recording=RecordingConfig(gsr_replay_storage="disk")))
                    loaded = config_mod.load()

        self.assertEqual(loaded.recording.gsr_replay_storage, "disk")


class ReplayStorageCommandTests(unittest.TestCase):
    def _cmd(self, rc: RecordingConfig) -> list[str]:
        recorder = GSRRecorder(
            Config(output=OutputConfig(directory="/tmp/vice-test"), recording=rc)
        )
        return recorder._build_cmd()

    def test_wants_disk_replay_matrix(self) -> None:
        self.assertTrue(_gsr_wants_disk_replay(RecordingConfig(gsr_replay_storage="disk")))
        self.assertTrue(_gsr_wants_disk_replay(
            RecordingConfig(gsr_replay_storage="auto", buffer_duration=601)))
        self.assertFalse(_gsr_wants_disk_replay(
            RecordingConfig(gsr_replay_storage="auto", buffer_duration=600)))
        self.assertFalse(_gsr_wants_disk_replay(
            RecordingConfig(gsr_replay_storage="ram", buffer_duration=1800)))

    def test_default_config_emits_no_storage_flag(self) -> None:
        with mock.patch("vice.recorder._gsr_supports_flag", return_value=True):
            cmd = self._cmd(RecordingConfig())
        self.assertNotIn("-replay-storage", cmd)

    def test_long_auto_buffer_uses_disk_when_supported(self) -> None:
        rc = RecordingConfig(buffer_duration=1200)
        with mock.patch("vice.recorder._gsr_supports_flag", return_value=True):
            cmd = self._cmd(rc)
        self.assertEqual(cmd[cmd.index("-replay-storage") + 1], "disk")

    def test_storage_flag_omitted_when_gsr_lacks_it(self) -> None:
        rc = RecordingConfig(buffer_duration=1200)
        with mock.patch("vice.recorder._gsr_supports_flag", return_value=False):
            cmd = self._cmd(rc)
        self.assertNotIn("-replay-storage", cmd)

    def test_user_gsr_args_storage_flag_wins(self) -> None:
        rc = RecordingConfig(buffer_duration=1200, gsr_args="-replay-storage ram")
        with mock.patch("vice.recorder._gsr_supports_flag", return_value=True):
            cmd = self._cmd(rc)
        self.assertEqual(cmd.count("-replay-storage"), 1)
        self.assertEqual(cmd[cmd.index("-replay-storage") + 1], "ram")


class AudioSourceClassificationTests(unittest.TestCase):
    def test_classify_matrix(self) -> None:
        cases = {
            "default_output": "monitor",
            "default_input": "input",
            "device:alsa_output.pci-0000.analog-stereo.monitor": "monitor",
            "device:alsa_input.usb-Focusrite_Scarlett-00.pro-input-0": "input",
            "app:Discord": "app",
            "app-inverse:firefox": "app",
            "": "unknown",
            "garbage": "unknown",
        }
        for source, expected in cases.items():
            self.assertEqual(_classify_gsr_source(source), expected, source)

    def test_desktop_toggle_off_keeps_microphone_tracks(self) -> None:
        rc = RecordingConfig(
            capture_audio=False,
            capture_microphone=True,
            audio_tracks=["default_output|default_input", "app:Discord"],
        )

        args = _gsr_audio_args(rc)

        self.assertEqual(args, ["-a", "default_input"])

    def test_desktop_toggle_off_without_tracks_still_records_mic(self) -> None:
        rc = RecordingConfig(capture_audio=False, capture_microphone=True)

        self.assertEqual(_gsr_audio_args(rc), ["-a", "default_input"])

    def test_audio_sources_report_kind(self) -> None:
        def fake_run(cmd, timeout=5.0):
            if "--list-audio-devices" in cmd:
                return 0, (
                    "alsa_output.pci-0000.analog-stereo.monitor|Speakers\n"
                    "alsa_input.usb-mic|USB Mic\n"
                )
            return 0, "Discord\n"

        with mock.patch("vice.recorder._has", return_value=True):
            with mock.patch("vice.recorder._run_command_capture", side_effect=fake_run):
                info = list_gsr_audio_sources()

        kinds = {s["id"]: s.get("kind") for s in info["sources"]}
        self.assertEqual(kinds["default_output"], "monitor")
        self.assertEqual(kinds["default_input"], "input")
        self.assertEqual(kinds["device:alsa_output.pci-0000.analog-stereo.monitor"], "monitor")
        self.assertEqual(kinds["device:alsa_input.usb-mic"], "input")
        self.assertEqual(kinds["app:Discord"], "app")
        self.assertEqual(kinds["app-inverse:Discord"], "app")


class GSRHealthTests(unittest.TestCase):
    def _recorder(self) -> GSRRecorder:
        return GSRRecorder(Config(output=OutputConfig(directory="/tmp/vice-test")))

    def test_healthy_requires_running_live_process(self) -> None:
        class _Proc:
            returncode = None

        recorder = self._recorder()
        self.assertFalse(recorder.is_healthy())

        recorder._running = True
        self.assertFalse(recorder.is_healthy())

        recorder._proc = _Proc()
        self.assertTrue(recorder.is_healthy())

        recorder._proc.returncode = 1
        self.assertFalse(recorder.is_healthy())


class SessionEnvironmentTests(unittest.TestCase):
    """What a daemon started before the desktop finishes logging in needs (#231)."""

    def test_xauthority_is_taken_from_the_session_when_missing(self) -> None:
        from vice import runtime
        session = {"DISPLAY": ":0", "XAUTHORITY": "/tmp/xauth_abc"}
        with mock.patch.dict(os.environ, {"DISPLAY": ":0"}, clear=True), \
                mock.patch.object(runtime, "user_systemd_env_snapshot", return_value=session):
            filled = runtime.load_user_systemd_env()
            self.assertEqual(filled, ["XAUTHORITY"])
            self.assertEqual(os.environ["XAUTHORITY"], "/tmp/xauth_abc")

    def test_a_value_already_set_is_never_replaced(self) -> None:
        from vice import runtime
        session = {"XAUTHORITY": "/tmp/xauth_other"}
        with mock.patch.dict(os.environ, {"XAUTHORITY": "/home/u/.Xauthority"}, clear=True), \
                mock.patch.object(runtime, "user_systemd_env_snapshot", return_value=session):
            self.assertEqual(runtime.load_user_systemd_env(), [])
            self.assertEqual(os.environ["XAUTHORITY"], "/home/u/.Xauthority")

    def test_an_x_refusal_names_the_cause_not_the_symptom(self) -> None:
        from vice.recorder import _gsr_runtime_error
        # gpu-screen-recorder 6.1.2's output against an X server that wants a
        # cookie it was not given. The last line is all the reporter saw.
        stderr = (
            "Authorization required, but no authorization protocol specified\n"
            "gsr warning: failed to connect to the X server. Assuming wayland is running without Xwayland\n"
            "gsr error: gsr_window_wayland_init failed: failed to connect to the Wayland server\n"
            "gsr error: failed to create window\n"
        )
        self.assertIn("XAUTHORITY", _gsr_runtime_error(stderr))
        self.assertEqual(
            _gsr_runtime_error("gsr error: failed to create window\n"),
            "gsr error: failed to create window",
        )


class RecorderWatchdogTests(unittest.IsolatedAsyncioTestCase):
    def _daemon(self, recorder: _FakeRecorder) -> main_mod.ViceDaemon:
        with mock.patch("vice.main.load_config", return_value=Config()):
            with mock.patch("vice.main.create_recorder", return_value=recorder):
                with mock.patch("vice.main.HotkeyListener", return_value=_FakeHotkeys()):
                    with mock.patch("vice.main.can_access_hotkeys", return_value=True):
                        daemon = main_mod.ViceDaemon()
        daemon.share = _FakeShare()
        return daemon

    async def _run_watchdog(self, daemon, max_sleeps: int, wall_times=None, env_loader=None):
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            if len(sleeps) >= max_sleeps:
                raise asyncio.CancelledError

        # The real loader asks this machine's systemd for its session and
        # copies it into the test process, which would undo any test that
        # clears DISPLAY to stand in for a headless runner.
        loader = env_loader or mock.Mock(return_value=[])
        patches = [mock.patch("vice.main.asyncio.sleep", fake_sleep)]
        if wall_times is not None:
            patches.append(mock.patch("vice.main.time.time", side_effect=wall_times))
        with patches[0], mock.patch("vice.main.load_user_systemd_env", loader):
            ctx = patches[1] if len(patches) > 1 else None
            try:
                if ctx:
                    with ctx:
                        await daemon._recorder_watchdog_loop()
                else:
                    await daemon._recorder_watchdog_loop()
            except asyncio.CancelledError:
                pass
        return sleeps

    async def test_a_restart_picks_up_a_session_exported_after_boot(self) -> None:
        # #231: the daemon started at boot before Plasma exported XAUTHORITY,
        # so every restart reused that environment and failed until a manual
        # restart. Each attempt has to look at the session again first.
        recorder = _FakeRecorder()
        recorder.healthy = False
        recorder.heal_on_start = True
        recorder.start_error = RuntimeError("gpu-screen-recorder failed to start: failed to create window")
        daemon = self._daemon(recorder)
        refreshes: list[int] = []

        def session_env() -> list:
            refreshes.append(recorder.start_calls)
            if len(refreshes) < 2:
                return []
            recorder.start_error = None  # the desktop has exported its session
            return ["XAUTHORITY"]

        with self.assertLogs("vice", level="INFO") as caught:
            await self._run_watchdog(daemon, max_sleeps=5, env_loader=mock.Mock(side_effect=session_env))
        await asyncio.sleep(0)

        self.assertEqual(recorder.start_calls, 2)
        # Looked before each attempt, not after.
        self.assertEqual(refreshes[:2], [0, 1])
        self.assertIn("Picked up XAUTHORITY from the session", "\n".join(caught.output))
        self.assertTrue(any(m.get("recording") for m in daemon.share.messages))

    async def test_dead_recorder_is_restarted(self) -> None:
        recorder = _FakeRecorder()
        recorder.healthy = False
        recorder.heal_on_start = True
        daemon = self._daemon(recorder)

        await self._run_watchdog(daemon, max_sleeps=3)
        await asyncio.sleep(0)

        self.assertEqual(recorder.stop_calls, 1)
        self.assertEqual(recorder.start_calls, 1)
        self.assertTrue(
            any(m.get("recording") for m in daemon.share.messages),
            daemon.share.messages,
        )

    async def test_repeated_deaths_start_backing_off(self) -> None:
        # A recorder that clears the 1 s startup probe and then dies never
        # reaches the failed-start path, so before #129 it was restarted at
        # full speed forever. One reporter logged 1019 restarts in under two
        # hours, each leaking a process and an fd.
        recorder = _FakeRecorder()
        recorder.healthy = False   # dies again right after every restart
        daemon = self._daemon(recorder)

        sleeps = await self._run_watchdog(daemon, max_sleeps=12)

        interval_sleeps = [s for s in sleeps if s == 5.0]
        backoff_sleeps = [s for s in sleeps if s > 5.0]
        self.assertTrue(backoff_sleeps, sleeps)
        # Doubling, not a flat retry.
        self.assertEqual(backoff_sleeps, sorted(backoff_sleeps))
        self.assertGreaterEqual(backoff_sleeps[-1], 10.0)
        # The first couple of deaths are still retried promptly.
        self.assertGreaterEqual(len(interval_sleeps), _RECORDER_DEATH_BACKOFF_AFTER)

    async def test_death_logs_the_recorder_output(self) -> None:
        recorder = _FakeRecorder()
        recorder.healthy = False
        recorder.heal_on_start = True
        recorder.output = "gsr error: no encoder found"
        daemon = self._daemon(recorder)

        with self.assertLogs("vice", level="ERROR") as logs:
            await self._run_watchdog(daemon, max_sleeps=3)

        self.assertTrue(
            any("no encoder found" in line for line in logs.output),
            logs.output,
        )

    async def test_healthy_recorder_is_left_alone(self) -> None:
        recorder = _FakeRecorder()
        daemon = self._daemon(recorder)

        await self._run_watchdog(daemon, max_sleeps=4)

        self.assertEqual(recorder.start_calls, 0)
        self.assertEqual(recorder.stop_calls, 0)

    async def test_wall_clock_jump_restarts_healthy_recorder(self) -> None:
        recorder = _FakeRecorder()
        daemon = self._daemon(recorder)

        wall = [1000.0, 2000.0] + [2000.0 + i * 5 for i in range(1, 20)]
        await self._run_watchdog(daemon, max_sleeps=3, wall_times=wall)

        self.assertEqual(recorder.stop_calls, 1)
        self.assertEqual(recorder.start_calls, 1)

    async def test_recovered_before_lock_skips_restart(self) -> None:
        recorder = _FakeRecorder()
        recorder.is_healthy = mock.Mock(side_effect=[False, True, True, True, True])
        daemon = self._daemon(recorder)

        await self._run_watchdog(daemon, max_sleeps=3)

        self.assertEqual(recorder.start_calls, 0)

    async def test_failed_restart_backs_off(self) -> None:
        recorder = _FakeRecorder()
        recorder.healthy = False
        recorder.start_error = RuntimeError("driver gone")
        daemon = self._daemon(recorder)

        sleeps = await self._run_watchdog(daemon, max_sleeps=5)
        await asyncio.sleep(0)

        self.assertEqual(sleeps, [5.0, 5.0, 5.0, 10.0, 5.0])
        self.assertTrue(
            any(m.get("recording") is False for m in daemon.share.messages),
            daemon.share.messages,
        )


_TEST_GAME = config_mod.DiscordCustomGame(name="TestGame", matches=["testgame"])
_UNRECOGNIZED_WINDOW = {"process": "explorer", "class": "Explorer"}
_GAME_WINDOW = {"process": "testgame", "class": "TestGame"}


def _game_window(window_id):
    """The game's window as one lookup reports it: the id that gets pinned
    belongs to the same window whose process/class matched."""
    return {**_GAME_WINDOW, "window_id": window_id}


class WindowCaptureLoopTests(unittest.IsolatedAsyncioTestCase):
    """_window_capture_loop: pinning, releasing, and the debounce that keeps
    a window recreated by a fullscreen/resolution change from costing a
    restart on every intermediate id (see WINDOW_CAPTURE_INTERVAL)."""

    def _daemon(self, recorder: _FakeRecorder, *, window_capture_id=None) -> main_mod.ViceDaemon:
        cfg = Config(
            recording=RecordingConfig(window_capture=True),
            discord=config_mod.DiscordConfig(custom_games=[_TEST_GAME]),
        )
        with mock.patch("vice.main.load_config", return_value=cfg):
            with mock.patch("vice.main.create_recorder", return_value=recorder):
                with mock.patch("vice.main.HotkeyListener", return_value=_FakeHotkeys()):
                    with mock.patch("vice.main.can_access_hotkeys", return_value=True):
                        daemon = main_mod.ViceDaemon()
        daemon.share = _FakeShare()
        daemon._window_capture_id = window_capture_id
        return daemon

    async def _run_loop(self, daemon, recorder: _FakeRecorder, ticks: int, *, wins=(), geoms=(), focus_id=None) -> None:
        """Run daemon._window_capture_loop for exactly `ticks` processing
        iterations (the immediate first check, then `ticks - 1` sleeps),
        then cancel it. Queues that run out return None (no active window /
        lookup failed) rather than raising. Each window in `wins` carries the
        id it was matched on, the way the real lookup returns it.

        `focus_id` is what a fresh focus lookup would answer, standing in for
        the user tabbing away mid-tick; the loop must never consult it.

        create_recorder needs to stay patched for the whole run, not just
        daemon construction: a real pin/release restart calls it again (see
        _restart_recorder_for_config), and without this it would build and
        start a real backend against the live environment.
        """
        win_q, geom_q = list(wins), list(geoms)
        sleeps = 0

        async def fake_sleep(_seconds: float) -> None:
            nonlocal sleeps
            sleeps += 1
            if sleeps >= ticks:
                raise asyncio.CancelledError

        def fake_get_active_window():
            return win_q.pop(0) if win_q else None

        def fake_get_window_geometry(_window_id):
            return geom_q.pop(0) if geom_q else None

        with mock.patch("vice.main.create_recorder", return_value=recorder), \
             mock.patch("vice.main.asyncio.sleep", fake_sleep), \
             mock.patch("vice.active_window.get_active_window", side_effect=fake_get_active_window), \
             mock.patch("vice.active_window.get_focused_window_id", return_value=focus_id) as focus_lookup, \
             mock.patch("vice.active_window.get_window_geometry", side_effect=fake_get_window_geometry):
            try:
                await daemon._window_capture_loop()
            except asyncio.CancelledError:
                pass
        self.focus_lookup = focus_lookup

    async def test_unrecognized_window_does_not_move_an_unset_pin(self) -> None:
        recorder = _FakeRecorder()
        daemon = self._daemon(recorder, window_capture_id=None)

        await self._run_loop(daemon, recorder, ticks=2, wins=[_UNRECOGNIZED_WINDOW, _UNRECOGNIZED_WINDOW])

        self.assertIsNone(daemon._window_capture_id)
        self.assertEqual(recorder.start_calls, 0)
        self.assertEqual(recorder.stop_calls, 0)

    async def test_unrecognized_window_does_not_release_an_active_pin(self) -> None:
        # The pinned game keeps running in the background (Discord, a
        # browser, etc focused instead); only a dead pinned window releases.
        recorder = _FakeRecorder()
        daemon = self._daemon(recorder, window_capture_id="0xOLD")

        await self._run_loop(
            daemon, recorder,
            ticks=2,
            wins=[_UNRECOGNIZED_WINDOW, _UNRECOGNIZED_WINDOW],
            geoms=[(800, 600), (800, 600)],
        )

        self.assertEqual(daemon._window_capture_id, "0xOLD")
        self.assertEqual(recorder.start_calls, 0)
        self.assertEqual(recorder.stop_calls, 0)

    async def test_first_pin_is_instant(self) -> None:
        recorder = _FakeRecorder()
        daemon = self._daemon(recorder, window_capture_id=None)

        await self._run_loop(daemon, recorder, ticks=1, wins=[_game_window("0xNEW")])

        self.assertEqual(daemon._window_capture_id, "0xNEW")
        self.assertEqual(recorder.start_calls, 1)
        self.assertEqual(recorder.stop_calls, 1)

    async def test_switching_an_active_pin_waits_for_a_second_agreeing_sample(self) -> None:
        recorder = _FakeRecorder()
        daemon = self._daemon(recorder, window_capture_id="0xOLD")

        await self._run_loop(
            daemon, recorder,
            ticks=1, wins=[_game_window("0xNEW")],
        )

        self.assertEqual(daemon._window_capture_id, "0xOLD")
        self.assertEqual(recorder.start_calls, 0)

    async def test_switching_an_active_pin_completes_after_two_agreeing_samples(self) -> None:
        recorder = _FakeRecorder()
        daemon = self._daemon(recorder, window_capture_id="0xOLD")

        await self._run_loop(
            daemon, recorder,
            ticks=2,
            wins=[_game_window("0xNEW"), _game_window("0xNEW")],
        )

        self.assertEqual(daemon._window_capture_id, "0xNEW")
        self.assertEqual(recorder.start_calls, 1)

    async def test_a_recreated_window_that_keeps_churning_never_switches(self) -> None:
        # Two different ids in a row, neither ever confirmed twice.
        recorder = _FakeRecorder()
        daemon = self._daemon(recorder, window_capture_id="0xOLD")

        await self._run_loop(
            daemon, recorder,
            ticks=2,
            wins=[_game_window("0xNEW1"), _game_window("0xNEW2")],
        )

        self.assertEqual(daemon._window_capture_id, "0xOLD")
        self.assertEqual(recorder.start_calls, 0)

    async def test_pins_the_matched_window_even_if_focus_moves_away(self) -> None:
        # Tabbing from the game to Discord between the match and the pin used
        # to attach the pin to Discord's window under the game's name; the id
        # now travels with the match, so a stale focus answer can't be used.
        recorder = _FakeRecorder()
        daemon = self._daemon(recorder, window_capture_id=None)

        await self._run_loop(
            daemon, recorder,
            ticks=1,
            wins=[_game_window("0xGAME")],
            focus_id="0xDISCORD",
        )

        self.assertEqual(daemon._window_capture_id, "0xGAME")
        self.assertEqual(recorder.start_calls, 1)
        self.focus_lookup.assert_not_called()

    async def test_a_matched_window_with_no_id_is_not_pinned(self) -> None:
        # Native-Wayland windows (and a failed id lookup) report no window id;
        # GSR can only pin X11 ids, so capture stays on the full display.
        recorder = _FakeRecorder()
        daemon = self._daemon(recorder, window_capture_id=None)

        await self._run_loop(
            daemon, recorder,
            ticks=2,
            wins=[_game_window(None), _GAME_WINDOW],
            focus_id="0xDISCORD",
        )

        self.assertIsNone(daemon._window_capture_id)
        self.assertEqual(recorder.start_calls, 0)
        self.assertEqual(recorder.stop_calls, 0)

    async def test_release_waits_for_a_second_failed_check(self) -> None:
        recorder = _FakeRecorder()
        daemon = self._daemon(recorder, window_capture_id="0xOLD")

        await self._run_loop(
            daemon, recorder,
            ticks=1,
            wins=[_UNRECOGNIZED_WINDOW], geoms=[None],
        )

        self.assertEqual(daemon._window_capture_id, "0xOLD")
        self.assertEqual(recorder.stop_calls, 0)

    async def test_release_completes_after_two_failed_checks(self) -> None:
        recorder = _FakeRecorder()
        daemon = self._daemon(recorder, window_capture_id="0xOLD")

        await self._run_loop(
            daemon, recorder,
            ticks=2,
            wins=[_UNRECOGNIZED_WINDOW, _UNRECOGNIZED_WINDOW],
            geoms=[None, None],
        )

        self.assertIsNone(daemon._window_capture_id)
        self.assertEqual(recorder.stop_calls, 1)

    async def test_release_recovers_after_a_transient_miss(self) -> None:
        recorder = _FakeRecorder()
        daemon = self._daemon(recorder, window_capture_id="0xOLD")

        await self._run_loop(
            daemon, recorder,
            ticks=2,
            wins=[_UNRECOGNIZED_WINDOW, _UNRECOGNIZED_WINDOW],
            geoms=[None, (800, 600)],
        )

        self.assertEqual(daemon._window_capture_id, "0xOLD")
        self.assertEqual(recorder.stop_calls, 0)


    async def test_a_failed_pin_is_not_retried_on_the_same_window(self) -> None:
        # Each attempt restarts the recorder and empties the buffer, so
        # retrying a window that already failed only throws clips away.
        recorder = _PinFailingRecorder("gsr error: failed to capture window 0xGAME")
        daemon = self._daemon(recorder, window_capture_id=None)

        await self._run_loop(daemon, recorder, ticks=3, wins=[_game_window("0xGAME")] * 3)

        self.assertIsNone(daemon._window_capture_id)
        self.assertEqual(recorder.start_calls, 2)  # the pin, then the restore
        self.assertIsNone(daemon._window_capture_refused)

    async def test_a_new_window_is_still_tried_after_a_failed_one(self) -> None:
        recorder = _PinFailingRecorder("gsr error: failed to capture window 0xGAME")
        daemon = self._daemon(recorder, window_capture_id=None)

        await self._run_loop(daemon, recorder, ticks=2,
                             wins=[_game_window("0xGAME"), _game_window("0xRELAUNCHED")])

        self.assertEqual(daemon._window_capture_id, "0xRELAUNCHED")
        self.assertEqual(recorder.start_calls, 3)

    async def test_a_wayland_refusal_stops_game_capture(self) -> None:
        # #261, #274: on XWayland every pin failed this way and was retried
        # every five seconds, so the buffer never held more than a few.
        recorder = _PinFailingRecorder(
            "gsr error: GPU Screen Recorder window capture only works in a pure X11 "
            "session. Xwayland is not supported. You can record a monitor instead on wayland")
        daemon = self._daemon(recorder, window_capture_id=None)

        await self._run_loop(daemon, recorder, ticks=3,
                             wins=[_game_window("0xGAME"), _game_window("0xOTHER"), _game_window("0xOTHER")])
        await asyncio.sleep(0)

        self.assertIsNone(daemon._window_capture_id)
        self.assertEqual(recorder.start_calls, 2)
        self.assertEqual(daemon._window_capture_unavailable(), "wayland")
        self.assertIn({"type": "status", "window_capture_unavailable": "wayland"},
                      daemon.share.messages)


class _PinFailingRecorder(_FakeRecorder):
    """Refuses the first start, the pin, and comes back when restored."""

    def __init__(self, message: str) -> None:
        super().__init__()
        self._message = message

    async def start(self) -> None:
        self.start_calls += 1
        if self.start_calls == 1:
            raise RuntimeError(self._message)


class GSRWindowCaptureSupportTests(unittest.TestCase):
    def _supported(self, env: dict) -> bool:
        with mock.patch.dict(os.environ, env, clear=True):
            return recorder_mod.gsr_window_capture_supported()

    def test_x11_session(self) -> None:
        self.assertTrue(self._supported({"DISPLAY": ":0", "XDG_SESSION_TYPE": "x11"}))

    def test_x11_without_a_session_type(self) -> None:
        self.assertTrue(self._supported({"DISPLAY": ":0"}))

    def test_wayland_display_means_xwayland(self) -> None:
        self.assertFalse(self._supported({"DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-1"}))

    def test_wayland_session_type(self) -> None:
        self.assertFalse(self._supported({"DISPLAY": ":0", "XDG_SESSION_TYPE": "wayland"}))


class WindowCaptureTaskGatingTests(unittest.IsolatedAsyncioTestCase):
    """_sync_window_capture_task: window pinning is GSR-only, so segment
    backends (wf-recorder, ffmpeg) must never start the polling loop, and GSR
    only pins a window on X11."""

    def setUp(self) -> None:
        patcher = mock.patch("vice.main.gsr_window_capture_supported", return_value=True)
        self.supported = patcher.start()
        self.addCleanup(patcher.stop)

    def _daemon(self, cfg: Config, recorder) -> main_mod.ViceDaemon:
        with mock.patch("vice.main.load_config", return_value=cfg), \
             mock.patch("vice.main.create_recorder", return_value=recorder), \
             mock.patch("vice.main.HotkeyListener", return_value=_FakeHotkeys()), \
             mock.patch("vice.main.can_access_hotkeys", return_value=True):
            return main_mod.ViceDaemon()

    async def test_starts_for_gsr_backend(self) -> None:
        cfg = Config(recording=RecordingConfig(window_capture=True))
        daemon = self._daemon(cfg, GSRRecorder(cfg))

        daemon._sync_window_capture_task()
        self.assertIsNotNone(daemon._window_capture_task)

        daemon.cfg.recording.window_capture = False
        daemon._sync_window_capture_task()

    async def test_does_not_start_for_segment_backend(self) -> None:
        cfg = Config(recording=RecordingConfig(window_capture=True))
        daemon = self._daemon(cfg, SegmentRecorder(cfg, use_wf_recorder=True))

        daemon._sync_window_capture_task()

        self.assertIsNone(daemon._window_capture_task)

    async def test_switching_off_stops_a_running_task(self) -> None:
        cfg = Config(recording=RecordingConfig(window_capture=True))
        daemon = self._daemon(cfg, GSRRecorder(cfg))
        daemon._sync_window_capture_task()

        daemon.cfg.recording.window_capture = False
        daemon._sync_window_capture_task()

        self.assertIsNone(daemon._window_capture_task)
        self.assertIsNone(daemon._window_capture_id)

    async def test_does_not_start_on_a_wayland_session(self) -> None:
        self.supported.return_value = False
        cfg = Config(recording=RecordingConfig(window_capture=True))
        daemon = self._daemon(cfg, GSRRecorder(cfg))

        daemon._sync_window_capture_task()

        self.assertIsNone(daemon._window_capture_task)
        self.assertEqual(daemon._get_status()["window_capture_unavailable"], "wayland")


class VolumeBalanceTests(unittest.IsolatedAsyncioTestCase):
    def test_default_volumes_keep_audio_args_identical(self) -> None:
        rc = RecordingConfig(capture_audio=True, capture_microphone=True)

        self.assertEqual(_gsr_audio_args(rc), ["-a", "default_output|default_input"])

    def test_non_default_volume_splits_desktop_and_mic_tracks(self) -> None:
        rc = RecordingConfig(
            capture_audio=True, capture_microphone=True, microphone_volume=0.5
        )

        self.assertEqual(
            _gsr_audio_args(rc),
            ["-a", "default_output", "-a", "default_input"],
        )

    def test_session_commands_never_split_for_volume(self) -> None:
        rc = RecordingConfig(
            capture_audio=True, capture_microphone=True, microphone_volume=0.5
        )

        self.assertEqual(
            _gsr_audio_args(rc, split_for_volume=False),
            ["-a", "default_output|default_input"],
        )

    def test_separate_tracks_ignore_volume_split(self) -> None:
        rc = RecordingConfig(
            capture_audio=True,
            capture_microphone=True,
            microphone_volume=0.5,
            audio_tracks=["default_output", "app:Discord"],
        )

        args = _gsr_audio_args(rc)

        self.assertEqual(args.count("-a"), 3)  # tracks + auto-appended mic

    def test_volume_mix_cmd_two_streams(self) -> None:
        from vice.recorder import _volume_mix_cmd

        cmd = _volume_mix_cmd(Path("/tmp/c.mp4"), Path("/tmp/c.mix.mp4"), 2, 1.0, 0.5)

        joined = " ".join(cmd)
        self.assertIn("volume=1.0[a0]", joined)
        self.assertIn("volume=0.5[a1]", joined)
        self.assertIn("amix=inputs=2:normalize=0", joined)
        self.assertIn("-c:v copy", joined)
        self.assertIn("-c:a aac", joined)
        self.assertIn("+faststart", joined)

    def test_volume_mix_cmd_mkv_uses_opus(self) -> None:
        from vice.recorder import _volume_mix_cmd

        cmd = _volume_mix_cmd(Path("/tmp/c.mkv"), Path("/tmp/c.mix.mkv"), 2, 1.0, 0.5)

        joined = " ".join(cmd)
        self.assertIn("-c:a libopus", joined)
        self.assertNotIn("+faststart", joined)

    async def test_apply_volume_mix_noop_at_defaults(self) -> None:
        from vice.recorder import _apply_volume_mix

        rc = RecordingConfig(capture_audio=True, capture_microphone=True)
        with mock.patch("vice.recorder._count_audio_streams") as probe:
            await _apply_volume_mix(Path("/tmp/c.mp4"), rc)

        probe.assert_not_called()

    async def test_apply_volume_mix_skips_single_mixed_track(self) -> None:
        from vice.recorder import _apply_volume_mix

        rc = RecordingConfig(
            capture_audio=True, capture_microphone=True, microphone_volume=0.5
        )
        with mock.patch("vice.recorder._count_audio_streams", return_value=1):
            with mock.patch("asyncio.create_subprocess_exec") as spawn:
                await _apply_volume_mix(Path("/tmp/c.mp4"), rc)

        spawn.assert_not_called()

    async def test_apply_volume_mix_skips_user_defined_tracks(self) -> None:
        from vice.recorder import _apply_volume_mix

        rc = RecordingConfig(
            capture_audio=True,
            capture_microphone=True,
            microphone_volume=0.5,
            audio_tracks=["default_output", "app:Discord"],
        )
        with mock.patch("vice.recorder._count_audio_streams") as probe:
            await _apply_volume_mix(Path("/tmp/c.mp4"), rc)

        probe.assert_not_called()

    def test_mic_mono_downmixes_only_the_mic_track(self) -> None:
        from vice.recorder import _volume_mix_cmd

        cmd = _volume_mix_cmd(Path("/tmp/c.mp4"), Path("/tmp/c.mix.mp4"), 2, 1.0, 1.0, True)

        joined = " ".join(cmd)
        # Both channels carry the same summed signal, so a one-channel mic
        # lands centred instead of in one ear (#146).
        self.assertIn("[0:a:1]pan=stereo|c0=0.5*c0+0.5*c1|c1=0.5*c0+0.5*c1,volume=1.0[a1]", joined)
        self.assertIn("[0:a:0]volume=1.0[a0]", joined)
        self.assertNotIn("[0:a:0]pan=", joined)

    def test_mic_mono_off_leaves_the_graph_alone(self) -> None:
        from vice.recorder import _volume_mix_cmd

        cmd = _volume_mix_cmd(Path("/tmp/c.mp4"), Path("/tmp/c.mix.mp4"), 2, 1.0, 0.5)

        self.assertNotIn("pan=", " ".join(cmd))

    def test_mic_mono_alone_triggers_the_save_time_pass(self) -> None:
        from vice.recorder import _save_audio_pass_wanted

        rc = RecordingConfig(
            capture_audio=True, capture_microphone=True, microphone_mono=True
        )
        self.assertTrue(_save_audio_pass_wanted(rc))

        # Without mic capture there is no mic to downmix.
        rc_no_mic = RecordingConfig(capture_audio=True, microphone_mono=True)
        self.assertFalse(_save_audio_pass_wanted(rc_no_mic))

    def test_mic_mono_splits_desktop_and_mic_into_tracks(self) -> None:
        from vice.recorder import _gsr_audio_args

        rc = RecordingConfig(
            capture_audio=True, capture_microphone=True, microphone_mono=True
        )
        args = _gsr_audio_args(rc)

        # Two -a flags: the pass can only touch the mic if it has its own track.
        self.assertEqual(args, ["-a", "default_output", "-a", "default_input"])

    async def test_mic_mono_not_applied_to_a_lone_desktop_track(self) -> None:
        from vice.recorder import _apply_volume_mix

        rc = RecordingConfig(
            capture_audio=True, capture_microphone=False, microphone_mono=True
        )
        with mock.patch("vice.recorder._count_audio_streams") as probe:
            await _apply_volume_mix(Path("/tmp/c.mp4"), rc)

        # microphone_mono without capture_microphone is not a reason to
        # re-encode anything.
        probe.assert_not_called()

    def test_encoder_failure_detection(self) -> None:
        from vice.recorder import _looks_like_encoder_failure

        self.assertTrue(_looks_like_encoder_failure(
            "gsr error: Could not open video codec: Function not implemented"
        ))
        self.assertTrue(_looks_like_encoder_failure("failed to load libnvidia-encode.so"))
        self.assertTrue(_looks_like_encoder_failure(
            "gsr error: no video encoder was specified and neither h264, hevc nor av1 are supported"
        ))
        # A bad monitor name is not worth retrying on the CPU.
        self.assertFalse(_looks_like_encoder_failure(
            "gsr error: monitor DP-9 not found"
        ))
        self.assertFalse(_looks_like_encoder_failure(""))

    def test_gsr_cmd_cpu_encoder_flag(self) -> None:
        from vice.recorder import GSRRecorder

        rec = GSRRecorder(Config(recording=RecordingConfig()))
        self.assertNotIn("-encoder", rec._build_cmd())
        cpu = rec._build_cmd(cpu_encoder=True)
        self.assertIn("-encoder", cpu)
        self.assertEqual(cpu[cpu.index("-encoder") + 1], "cpu")

    def test_audio_tracks_dropped_by_desktop_toggle_are_reported(self) -> None:
        from vice.recorder import _gsr_audio_args

        rc = RecordingConfig(
            capture_audio=False,
            capture_microphone=True,
            audio_tracks=["default_output", "app:Discord"],
        )
        with self.assertLogs("vice.recorder", level="WARNING") as logs:
            args = _gsr_audio_args(rc)

        # Only the mic survives, which looks exactly like multi-track being
        # broken unless Vice says why (#137).
        self.assertEqual(args, ["-a", "default_input"])
        self.assertIn("Capture desktop audio is off", "\n".join(logs.output))

    def test_clamp_bounds_volumes(self) -> None:
        cfg = Config(recording=RecordingConfig(desktop_volume=9.0, microphone_volume=-1))
        config_mod.clamp_recording_limits(cfg)

        self.assertEqual(cfg.recording.desktop_volume, 2.0)
        self.assertEqual(cfg.recording.microphone_volume, 0.0)


class ClipSlugTests(unittest.TestCase):
    """#138: a clip name the user types has to survive the filesystem, the
    share URL and the inline handlers in the clip grid."""

    def test_spaces_become_dashes_and_case_survives(self) -> None:
        self.assertEqual(slugify_clip_name("Insane wallbang"), "Insane-wallbang")
        self.assertEqual(slugify_clip_name("why did   this? #2"), "why-did-this-2")

    def test_apostrophes_and_url_punctuation_are_dropped(self) -> None:
        # The reported break: an apostrophe closed the JS string in every
        # inline handler on the card.
        self.assertEqual(slugify_clip_name("Bob's clip"), "Bobs-clip")
        self.assertEqual(slugify_clip_name("100% ownage!"), "100-ownage")
        self.assertEqual(slugify_clip_name("a&b?c#d"), "abcd")

    def test_extension_and_separators_are_stripped(self) -> None:
        self.assertEqual(slugify_clip_name("clip.mp4"), "clip")
        self.assertEqual(slugify_clip_name("clip.MKV"), "clip")
        self.assertEqual(slugify_clip_name("../../evil"), "evil")

    def test_nothing_usable_returns_none(self) -> None:
        self.assertIsNone(slugify_clip_name("   "))
        self.assertIsNone(slugify_clip_name("..."))
        self.assertIsNone(slugify_clip_name("???"))

    def test_existing_clip_names_are_left_alone(self) -> None:
        self.assertEqual(
            slugify_clip_name("Vice_Clip_4_Overwatch-2"), "Vice_Clip_4_Overwatch-2"
        )


class ColorDepthTests(unittest.TestCase):
    """#131: 10-bit capture, which only HEVC and AV1 can actually do."""

    def test_gsr_codec_gains_the_10bit_variant(self) -> None:
        self.assertEqual(_gsr_codec_for_encoder("hevc_nvenc", "10"), "hevc_10bit")
        self.assertEqual(_gsr_codec_for_encoder("av1_vaapi", "10"), "av1_10bit")

    def test_gsr_falls_back_to_hevc_when_the_encoder_cannot_do_10bit(self) -> None:
        # No GPU encoder does 10-bit H.264, so asking for it must not silently
        # produce an 8-bit clip under a 10-bit setting.
        self.assertEqual(_gsr_codec_for_encoder("h264_nvenc", "10"), "hevc_10bit")
        self.assertEqual(_gsr_codec_for_encoder("auto", "10"), "hevc_10bit")

    def test_8bit_keeps_the_previous_mapping(self) -> None:
        self.assertIsNone(_gsr_codec_for_encoder("auto"))
        self.assertEqual(_gsr_codec_for_encoder("h264_nvenc"), "h264")
        self.assertEqual(_gsr_codec_for_encoder("av1_nvenc"), "av1")

    def test_gsr_command_carries_the_10bit_codec(self) -> None:
        cfg = Config(recording=RecordingConfig(encoder="hevc_nvenc", color_depth="10"))
        cmd = GSRRecorder(cfg)._build_cmd()

        self.assertIn("-k", cmd)
        self.assertEqual(cmd[cmd.index("-k") + 1], "hevc_10bit")

    def test_ffmpeg_pixel_format_follows_the_setting(self) -> None:
        self.assertIn("p010le", _encoder_flags("hevc_nvenc", 23, "10"))
        self.assertIn("yuv420p10le", _encoder_flags("libx265", 23, "10"))
        self.assertIn("format=p010,hwupload", _encoder_flags("hevc_vaapi", 23, "10"))
        # Software and hardware H.264 have no 10-bit path here.
        self.assertNotIn("yuv420p10le", _encoder_flags("libx264", 23, "10"))
        self.assertNotIn("p010le", _encoder_flags("h264_nvenc", 23, "10"))


class FollowMouseDisplayTests(unittest.TestCase):
    """#133: capture whichever monitor the pointer is on."""

    def test_override_beats_the_saved_display(self) -> None:
        cfg = Config(recording=RecordingConfig(display="DP-1"))
        recorder = GSRRecorder(cfg)
        recorder.display_override = "HDMI-A-1"

        with mock.patch(
            "vice.recorder._display_options",
            return_value=[{"id": "DP-1", "label": "DP-1"}, {"id": "HDMI-A-1", "label": "HDMI-A-1"}],
        ):
            cmd = recorder._build_cmd()

        self.assertEqual(cmd[cmd.index("-w") + 1], "HDMI-A-1")

    def test_no_override_keeps_the_saved_display(self) -> None:
        cfg = Config(recording=RecordingConfig(display="DP-1"))
        with mock.patch(
            "vice.recorder._display_options",
            return_value=[{"id": "DP-1", "label": "DP-1"}],
        ):
            cmd = GSRRecorder(cfg)._build_cmd()

        self.assertEqual(cmd[cmd.index("-w") + 1], "DP-1")


class WindowCaptureArgsTests(unittest.TestCase):
    """_gsr_capture_args: the -w/-s flags window_capture adds for GSR."""

    def test_pinned_window_produces_w_id_and_s_geometry(self) -> None:
        rc = RecordingConfig(window_capture=True)
        with mock.patch("vice.recorder.active_window.get_window_geometry", return_value=(1920, 1080)):
            args = _gsr_capture_args(rc, extra=[], window_override="0x123")

        self.assertEqual(args, ["-w", "0x123", "-s", "1920x1080"])

    def test_manual_resolution_suppresses_the_s_flag(self) -> None:
        rc = RecordingConfig(window_capture=True, resolution="1280x720")
        with mock.patch("vice.recorder.active_window.get_window_geometry", return_value=(1920, 1080)):
            args = _gsr_capture_args(rc, extra=[], window_override="0x123")

        self.assertEqual(args, ["-w", "0x123"])

    def test_dead_window_falls_back_to_full_display(self) -> None:
        rc = RecordingConfig(window_capture=True)
        with mock.patch("vice.recorder.active_window.get_window_geometry", return_value=None):
            with mock.patch("vice.recorder._display_options", return_value=[]):
                args = _gsr_capture_args(rc, extra=[], window_override="0x123")

        self.assertEqual(args, ["-w", "screen"])

    def test_window_capture_off_changes_nothing(self) -> None:
        rc = RecordingConfig(window_capture=False)
        with mock.patch("vice.recorder.active_window.get_window_geometry", return_value=(1920, 1080)):
            with mock.patch("vice.recorder._display_options", return_value=[]):
                args = _gsr_capture_args(rc, extra=[], window_override="0x123")

        self.assertEqual(args, ["-w", "screen"])

    def test_no_window_override_uses_the_configured_display(self) -> None:
        rc = RecordingConfig(window_capture=True)
        with mock.patch("vice.recorder._display_options", return_value=[]):
            args = _gsr_capture_args(rc, extra=[], window_override=None)

        self.assertEqual(args, ["-w", "screen"])


class ClipNameTemplateTests(unittest.TestCase):
    """#118: let users name clips themselves without ever losing one."""

    NOW = datetime(2026, 7, 19, 16, 0)

    def test_renders_every_token(self) -> None:
        self.assertEqual(
            _render_clip_name("clip_$date_$time", 1, None, self.NOW),
            "clip_2026-07-19_1600",
        )
        self.assertEqual(
            _render_clip_name("$game_$n", 4, "Overwatch-2", self.NOW),
            "Overwatch-2_4",
        )

    def test_missing_game_leaves_no_dangling_separators(self) -> None:
        self.assertEqual(_render_clip_name("clip_$game_$n", 4, None, self.NOW), "clip_4")
        self.assertEqual(_render_clip_name("$game-$n", 2, None, self.NOW), "2")

    def test_template_cannot_escape_the_output_directory(self) -> None:
        name = _render_clip_name("../../etc/passwd_$n", 1, None, self.NOW)

        self.assertNotIn("/", name)
        self.assertNotIn("..", name)

    def test_empty_template_keeps_default_naming(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)

            self.assertEqual(_next_clip_path(out).name, "Vice_Clip_1.mp4")
            self.assertEqual(_next_clip_path(out, template="").name, "Vice_Clip_1.mp4")

    def test_numbering_continues_across_saves(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            names = []
            for _ in range(3):
                path = _next_clip_path(out, template="Rec-$n")
                path.touch()
                names.append(path.name)

            self.assertEqual(names, ["Rec-1.mp4", "Rec-2.mp4", "Rec-3.mp4"])

    def test_numbering_counts_other_containers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "Rec-7.mkv").touch()

            self.assertEqual(_next_clip_path(out, template="Rec-$n").name, "Rec-8.mp4")

    def test_numbering_carries_on_when_the_minute_changes(self) -> None:
        # #243: $n restarted at 1 for every new minute of $time.
        from datetime import datetime
        from vice.recorder import _next_templated_path
        template = "clip_$n_$game_$date_$time"
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            names = []
            for minute, game in ((5, "CS2"), (6, "CS2"), (7, "Deep-Rock-Galactic"), (8, None)):
                now = datetime(2026, 10, 11, 13, minute)
                path = _next_templated_path(out, template, "mp4", game, now)
                path.touch()
                names.append(path.name)

            self.assertEqual(names, [
                "clip_1_CS2_2026-10-11_1305.mp4",
                "clip_2_CS2_2026-10-11_1306.mp4",
                "clip_3_Deep-Rock-Galactic_2026-10-11_1307.mp4",
                "clip_4_2026-10-11_1308.mp4",
            ])

    def test_numbering_carries_on_across_days(self) -> None:
        from datetime import datetime
        from vice.recorder import _next_templated_path
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "Rec-2026-10-10-4.mp4").touch()
            path = _next_templated_path(out, "Rec-$date-$n", "mp4", None, datetime(2026, 10, 11, 9, 0))

            self.assertEqual(path.name, "Rec-2026-10-11-5.mp4")

    def test_numbering_ignores_clips_another_template_named(self) -> None:
        from datetime import datetime
        from vice.recorder import _next_templated_path
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            for name in ("Vice_Clip_9.mp4", "other_7_2026-10-11.mp4", "clip_3_notes.txt"):
                (out / name).touch()
            path = _next_templated_path(out, "clip_$n_$date", "mp4", None, datetime(2026, 10, 11, 9, 0))

            self.assertEqual(path.name, "clip_1_2026-10-11.mp4")

    def test_template_without_number_never_overwrites(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            first = _next_clip_path(out, template="clip_$date")
            first.touch()
            second = _next_clip_path(out, template="clip_$date")

            self.assertNotEqual(first, second)
            self.assertTrue(second.name.endswith("_2.mp4"))

    def test_unusable_template_falls_back_to_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            # $game is empty when no game is detected, leaving nothing at all.
            self.assertEqual(
                _next_clip_path(out, template="$game").name, "Vice_Clip_1.mp4"
            )


class AudioTrackPreservationTests(unittest.IsolatedAsyncioTestCase):
    """Every ffmpeg pass over a clip must keep all audio tracks. Without an
    explicit map, ffmpeg keeps one, which silently dropped the mic from clips
    recorded with split desktop/mic tracks (#119)."""

    @staticmethod
    def _make_two_track_clip(path: Path) -> None:
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc=size=320x240:rate=30:duration=6",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
                "-f", "lavfi", "-i", "sine=frequency=880:duration=6",
                "-map", "0:v", "-map", "1:a", "-map", "2:a",
                "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
                "-y", str(path),
            ],
            check=True,
        )

    @staticmethod
    def _audio_stream_count(path: Path) -> int:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "a",
                "-show_entries", "stream=index", "-of", "csv=p=0", str(path),
            ],
            capture_output=True, text=True, check=True,
        ).stdout
        return len([line for line in out.splitlines() if line.strip()])

    def test_trim_and_watermark_commands_map_every_stream(self) -> None:
        from vice.recorder import KEEP_ALL_AUDIO, KEEP_ALL_STREAMS

        self.assertEqual(KEEP_ALL_STREAMS, ["-map", "0:v?", "-map", "0:a?"])
        # The watermark pass filters video, which needs a single video input.
        self.assertEqual(KEEP_ALL_AUDIO, ["-map", "0:v:0?", "-map", "0:a?"])

    @unittest.skipUnless(
        shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg not installed"
    )
    async def test_trim_keeps_both_audio_tracks(self) -> None:
        from vice.recorder import _trim_to_last_n_seconds

        with tempfile.TemporaryDirectory() as tmpdir:
            clip = Path(tmpdir) / "Vice_Clip_1.mp4"
            self._make_two_track_clip(clip)
            self.assertEqual(self._audio_stream_count(clip), 2)

            await _trim_to_last_n_seconds(clip, 3)

            self.assertEqual(self._audio_stream_count(clip), 2)

    @unittest.skipUnless(
        shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg not installed"
    )
    async def test_volume_mix_balances_both_tracks_into_one(self) -> None:
        from vice.recorder import _apply_volume_mix

        rc = RecordingConfig(
            capture_audio=True, capture_microphone=True, microphone_volume=0.5
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            clip = Path(tmpdir) / "Vice_Clip_1.mp4"
            self._make_two_track_clip(clip)

            await _apply_volume_mix(clip, rc)

            self.assertEqual(self._audio_stream_count(clip), 1)



class UpdateNoticeTests(unittest.IsolatedAsyncioTestCase):
    """The daemon side of the update check. Installed version is the real
    __version__, so the fixtures are chosen relative to it."""

    def _daemon(self):
        from vice.config import Config
        from vice.main import ViceDaemon

        daemon = main_mod.ViceDaemon.__new__(main_mod.ViceDaemon)
        daemon.cfg = Config()
        daemon._update = None
        daemon.share = mock.MagicMock()
        daemon.share.broadcast = mock.AsyncMock()
        return daemon

    async def _run(self, daemon, tmp, latest):
        from vice import updates as up
        from vice.main import ViceDaemon

        cache = up.UpdateCache(Path(tmp) / "update.json")
        with mock.patch.object(up, "UpdateCache", return_value=cache):
            with mock.patch.object(up, "fetch_latest", return_value=latest) as fetch:
                with mock.patch.object(
                        ViceDaemon, "_update_install_hint",
                        return_value={"method": "aur",
                                      "command": "yay -Syu vice-clipper"}):
                    found = await daemon.run_update_check()
        return found, fetch, cache

    @staticmethod
    def _bump(version, by=1):
        parts = [int(p) for p in version.split(".")]
        parts[1] += by
        return ".".join(str(p) for p in parts)

    async def test_newer_release_is_broadcast_with_the_install_command(self) -> None:
        from vice import __version__

        with tempfile.TemporaryDirectory() as tmp:
            daemon = self._daemon()
            latest = {"version": self._bump(__version__), "url": "https://x/rel",
                      "notes": ["Something good"], "etag": 'W/"e"'}
            found, _, _ = await self._run(daemon, tmp, latest)

        self.assertEqual(found["version"], self._bump(__version__))
        self.assertEqual(found["install"]["command"], "yay -Syu vice-clipper")
        msg = daemon.share.broadcast.await_args.args[0]
        self.assertEqual(msg["type"], "update_available")
        self.assertEqual(msg["notes"], ["Something good"])

    async def test_an_older_release_says_nothing(self) -> None:
        from vice import __version__

        with tempfile.TemporaryDirectory() as tmp:
            daemon = self._daemon()
            latest = {"version": self._bump(__version__, -1), "url": "https://x",
                      "notes": [], "etag": None}
            found, _, _ = await self._run(daemon, tmp, latest)

        self.assertIsNone(found)
        self.assertIsNone(daemon._update)
        daemon.share.broadcast.assert_not_awaited()

    async def test_a_failed_fetch_says_nothing_but_still_marks_the_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            daemon = self._daemon()
            found, _, cache = await self._run(daemon, tmp, None)

            self.assertIsNone(found)
            daemon.share.broadcast.assert_not_awaited()
            # The timestamp still moves, so a dead network is not retried on
            # every launch.
            self.assertIn("checked_at", cache.load())

    async def test_a_fresh_cache_is_not_refetched(self) -> None:
        from vice import __version__, updates as up
        from vice.main import ViceDaemon

        with tempfile.TemporaryDirectory() as tmp:
            daemon = self._daemon()
            cache = up.UpdateCache(Path(tmp) / "update.json")
            cache.save({"version": self._bump(__version__), "url": "https://x",
                        "notes": [], "checked_at": time.time()})
            with mock.patch.object(up, "UpdateCache", return_value=cache):
                with mock.patch.object(up, "fetch_latest") as fetch:
                    with mock.patch.object(
                            ViceDaemon, "_update_install_hint",
                            return_value={"method": "aur",
                                          "command": "yay -Syu vice-clipper"}):
                        found = await daemon.run_update_check()

        fetch.assert_not_called()
        self.assertEqual(found["version"], self._bump(__version__))


class NotificationVolumeTests(unittest.IsolatedAsyncioTestCase):
    """Issue #127: the clip ping had no volume control and no way off."""

    def _peak(self, wav: bytes) -> int:
        import io
        import struct
        import wave
        with wave.open(io.BytesIO(wav), "rb") as w:
            frames = w.readframes(w.getnframes())
        samples = struct.unpack(f"<{len(frames) // 2}h", frames)
        return max(abs(s) for s in samples)

    def test_volume_scales_the_tone(self) -> None:
        loud = audio_mod._wav_for("clip", 1.0)
        quiet = audio_mod._wav_for("clip", 0.25)
        self.assertLess(self._peak(quiet), self._peak(loud))
        # Same sound, just quieter: identical length.
        self.assertEqual(len(loud), len(quiet))

    def test_out_of_range_volumes_are_clamped(self) -> None:
        self.assertEqual(audio_mod._clamp_volume(5.0), 1.0)
        self.assertEqual(audio_mod._clamp_volume(-2.0), 0.0)
        # A hand-edited config should never crash the daemon.
        self.assertEqual(audio_mod._clamp_volume("loud"), 1.0)

    def test_zero_volume_plays_nothing(self) -> None:
        with mock.patch.object(audio_mod, "_play") as play:
            audio_mod.play_clip(0.0)
            audio_mod.play_session_start(0.0)
            audio_mod.play_session_end(0.0)
            audio_mod.play_highlight(0.0)
        # No temp file, no player process, no device wake-up.
        play.assert_not_called()

    async def test_nonzero_volume_still_plays(self) -> None:
        with mock.patch.object(audio_mod, "_play", new=mock.AsyncMock()) as play:
            audio_mod.play_clip(0.4)
            await asyncio.sleep(0)
        play.assert_awaited_once_with("clip", 0.4, None, "classic")

    async def test_custom_sound_is_passed_through(self) -> None:
        with mock.patch.object(audio_mod, "_play", new=mock.AsyncMock()) as play:
            audio_mod.play_clip(0.4, "~/sounds/ping.wav")
            await asyncio.sleep(0)
        play.assert_awaited_once_with("clip", 0.4, "~/sounds/ping.wav", "classic")

    def test_daemon_passes_the_configured_volume(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "vice" / "main.py").read_text()
        for fn in ("play_clip", "play_session_start", "play_session_end", "play_highlight"):
            self.assertIn(f"audio.{fn}(self.cfg.notifications.sound_volume,", source)

    def test_daemon_passes_the_configured_custom_sounds(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "vice" / "main.py").read_text()
        for setting in ("clip_sound", "clip_failed_sound", "session_start_sound",
                        "session_end_sound", "highlight_sound"):
            self.assertIn(f"self.cfg.notifications.{setting}", source)


class NotificationWavFormatTests(unittest.TestCase):
    """Mitigation for #163: a notification that matches the usual PipeWire
    graph needs no resampling and no channel remix, which is the work that
    was running in libspa-audioconvert when pipewire-pulse aborted."""

    def test_tones_are_48k_stereo(self) -> None:
        import io
        import wave

        for name in ("clip", "clip_failed", "session_start",
                     "session_end", "highlight"):
            with self.subTest(sound=name):
                with wave.open(io.BytesIO(audio_mod._wav_for(name, 1.0))) as w:
                    self.assertEqual(w.getframerate(), 48000)
                    self.assertEqual(w.getnchannels(), 2)
                    self.assertEqual(w.getsampwidth(), 2)
                    self.assertGreater(w.getnframes(), 0)

    def test_each_volume_gets_its_own_file(self) -> None:
        """A shared path was rewritten under a player that still had it open."""
        source = (Path(__file__).resolve().parents[1] / "vice" / "audio.py").read_text()
        self.assertIn('f"snd_{name}_{level}.wav"', source)
        self.assertNotIn('f"snd_{name}.wav"', source)


class ProbeFailureReasonTests(unittest.IsolatedAsyncioTestCase):
    """ffprobe's own explanation has to survive.

    It used to run with -v quiet and stderr to /dev/null, so a clip that
    could not be read produced a log line about JSON parsing and nothing
    about the file. That is why #154 took three round trips.
    """

    @classmethod
    def setUpClass(cls) -> None:
        if shutil.which("ffprobe") is None or shutil.which("ffmpeg") is None:
            raise unittest.SkipTest("ffmpeg/ffprobe not installed")

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _good_clip(self) -> Path:
        path = self.dir / "good.mp4"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
             "-i", "testsrc=size=64x64:rate=15", "-t", "1", str(path)],
            check=True,
        )
        return path

    async def test_healthy_clip_reports_no_reason(self) -> None:
        meta, why = await media_mod.probe_media_detailed(self._good_clip())
        self.assertIsNotNone(meta)
        self.assertEqual(why, "")
        self.assertGreater(meta["duration"], 0)

    async def test_unreadable_file_reports_ffprobes_own_message(self) -> None:
        junk = self.dir / "junk.mp4"
        junk.write_bytes(os.urandom(4096))
        meta, why = await media_mod.probe_media_detailed(junk)
        self.assertIsNone(meta)
        self.assertTrue(why)
        # The message is ffprobe's, not a generic one of ours.
        self.assertIn("Invalid data", why)
        # And it does not repeat the path the log line already names.
        self.assertNotIn(str(junk), why)

    async def test_missing_file_reports_a_reason(self) -> None:
        meta, why = await media_mod.probe_media_detailed(self.dir / "nope.mp4")
        self.assertIsNone(meta)
        self.assertIn("No such file", why)

    async def test_probe_media_keeps_its_old_signature(self) -> None:
        # Every existing caller still gets a dict or None, never a tuple.
        self.assertIsInstance(await media_mod.probe_media(self._good_clip()), dict)
        junk = self.dir / "junk2.mp4"
        junk.write_bytes(os.urandom(2048))
        self.assertIsNone(await media_mod.probe_media(junk))

    def test_zero_duration_mp4_can_use_video_samples(self) -> None:
        stream = {
            "nb_frames": "3641",
            "avg_frame_rate": "728200000/12136677",
        }
        self.assertAlmostEqual(
            media_mod._duration_from_video_samples(stream),
            60.6834,
            places=3,
        )
        self.assertEqual(
            media_mod._duration_from_video_samples({"nb_frames": "0", "avg_frame_rate": "60/1"}),
            0.0,
        )

    @staticmethod
    def _zero_duration_probe(frames: int, readable) -> mock.AsyncMock:
        """ffprobe as it answers for an MP4 whose duration fields are zero:
        first the metadata, then the packet count, or a failed count."""
        payload = {
            "format": {"duration": "N/A"},
            "streams": [{
                "codec_type": "video",
                "width": 1920,
                "height": 1080,
                "codec_name": "h264",
                "duration": "0",
                "nb_frames": str(frames),
                "avg_frame_rate": "728200000/12136677",
            }],
        }

        def answer(stdout: bytes) -> mock.Mock:
            process = mock.Mock(returncode=0)
            process.communicate = mock.AsyncMock(return_value=(stdout, b""))
            return process

        count = answer(f"{readable}\n".encode()) if readable is not None else answer(b"")
        return mock.AsyncMock(side_effect=[answer(json.dumps(payload).encode()), count])

    async def test_probe_uses_samples_when_mp4_duration_fields_are_zero(self) -> None:
        with mock.patch.object(media_mod.asyncio, "create_subprocess_exec",
                               new=self._zero_duration_probe(3641, readable=3641)):
            meta, why = await media_mod.probe_media_detailed(self.dir / "gsr.mp4")
        self.assertEqual(why, "")
        self.assertIsNotNone(meta)
        self.assertAlmostEqual(meta["duration"], 60.6834, places=3)

    async def test_probe_refuses_the_estimate_when_the_frames_cannot_be_read(self) -> None:
        # #154's real file: 3655 frames in the index, all stamped at zero, and
        # FFmpeg reads one of them. Calling it 60 seconds let the trim replace
        # the recording with a single frame.
        with mock.patch.object(media_mod.asyncio, "create_subprocess_exec",
                               new=self._zero_duration_probe(3655, readable=1)):
            meta, why = await media_mod.probe_media_detailed(self.dir / "gsr.mp4")
        self.assertIsNone(meta)
        self.assertIn("1 of its 3655 frames", why)

    async def test_probe_keeps_the_estimate_when_the_count_is_unavailable(self) -> None:
        # No count is no opinion, so behaviour is exactly what it was without it.
        with mock.patch.object(media_mod.asyncio, "create_subprocess_exec",
                               new=self._zero_duration_probe(3641, readable=None)):
            meta, why = await media_mod.probe_media_detailed(self.dir / "gsr.mp4")
        self.assertEqual(why, "")
        self.assertAlmostEqual(meta["duration"], 60.6834, places=3)

    def _long_clip(self, seconds: int) -> Path:
        path = self.dir / "long.mp4"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
             "-i", "testsrc=size=64x64:rate=15", "-t", str(seconds),
             "-g", "15", str(path)],
            check=True,
        )
        return path

    async def test_a_healthy_trim_still_replaces_the_clip(self) -> None:
        clip = self._long_clip(6)
        await recorder_mod._trim_to_last_n_seconds(clip, 2)
        self.assertLess(await media_mod.get_duration(clip), 3)
        self.assertFalse(list(self.dir.glob("*.trim.*")))

    async def test_a_trim_that_comes_out_broken_keeps_the_whole_clip(self) -> None:
        # ffmpeg exits cleanly on the collapsed file and writes one frame. The
        # original must survive, byte for byte, with nothing left behind.
        clip = self._long_clip(6)
        original = clip.read_bytes()

        async def duration(path: Path) -> float:
            return 60.0 if path == clip else 0.017

        with mock.patch.object(recorder_mod, "_get_duration", new=duration):
            with self.assertLogs("vice.recorder", level="ERROR") as caught:
                await recorder_mod._trim_to_last_n_seconds(clip, 20)
        self.assertEqual(clip.read_bytes(), original)
        self.assertFalse(list(self.dir.glob("*.trim.*")))
        self.assertIn("keeping the whole clip", "\n".join(caught.output))

    async def test_failure_is_logged_at_warning_with_the_file_name(self) -> None:
        junk = self.dir / "broken.mp4"
        junk.write_bytes(os.urandom(2048))
        with self.assertLogs("vice.media", level="WARNING") as caught:
            await media_mod.probe_media_detailed(junk)
        joined = "\n".join(caught.output)
        self.assertIn("broken.mp4", joined)
        self.assertIn("Invalid data", joined)


class UnreadableClipListingTests(unittest.IsolatedAsyncioTestCase):
    """A clip ffmpeg cannot read is listed as broken, not as a 0:00 clip."""

    @classmethod
    def setUpClass(cls) -> None:
        if shutil.which("ffprobe") is None or shutil.which("ffmpeg") is None:
            raise unittest.SkipTest("ffmpeg/ffprobe not installed")

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    async def test_broken_mp4_is_flagged_with_a_reason(self) -> None:
        junk = self.dir / "Vice_Clip_9.mp4"
        junk.write_bytes(os.urandom(8192))
        meta = await share_mod._ffprobe(junk)
        self.assertTrue(meta.get("unreadable"))
        self.assertTrue(meta.get("unreadable_reason"))

    async def test_healthy_clip_is_not_flagged(self) -> None:
        path = self.dir / "Vice_Clip_1.mp4"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
             "-i", "testsrc=size=64x64:rate=15", "-t", "1", str(path)],
            check=True,
        )
        meta = await share_mod._ffprobe(path)
        self.assertFalse(meta.get("unreadable"))
        self.assertGreater(meta["duration"], 0)


async def _wait_until_exec(pid: int, argv: list) -> None:
    """Wait for the child to become what it was told to run.

    create_subprocess_exec returns once the fork has happened, so
    /proc/<pid>/cmdline can still be empty or still the runner's own argv.
    The reaper identifies a capture by its argv and leaves anything it
    cannot identify alone, so racing the exec reads as "nothing was
    reaped" (CI, Python 3.10, 19 Sept 2026).
    """
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if _process_argv(pid) == argv:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"pid {pid} never exec'd {argv}")


class OrphanedCaptureTests(unittest.IsolatedAsyncioTestCase):
    """A capture process runs in its own session so its helper dies with it
    (#129), which also means kill -9 on the daemon leaves it recording with
    nothing supervising it (#121). It gets reaped at the next start."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch(
            "vice.recorder.CAPTURE_REGISTRY", Path(self.tmp.name) / "capture.json"
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def _running(pid: int) -> bool:
        """Running, not merely present: a killed child stays as a zombie
        until it is waited on, and signal 0 succeeds on those."""
        try:
            stat_line = Path(f"/proc/{pid}/stat").read_text()
        except (FileNotFoundError, ProcessLookupError):
            return False
        except OSError:
            return True
        return stat_line.rsplit(")", 1)[1].split()[0] != "Z"

    async def test_a_leftover_recorder_is_stopped(self) -> None:
        proc = await asyncio.create_subprocess_exec(
            "sleep", "60", stdout=asyncio.subprocess.DEVNULL, start_new_session=True
        )
        self.addCleanup(lambda: proc.kill() if proc.returncode is None else None)
        await _wait_until_exec(proc.pid, ["sleep", "60"])
        _write_capture_registry([{"pid": proc.pid, "argv": ["sleep", "60"]}])

        self.assertEqual(reap_orphaned_captures(), 1)
        await asyncio.wait_for(proc.wait(), timeout=5)
        self.assertFalse(self._running(proc.pid))
        self.assertEqual(_read_capture_registry(), [])

    async def test_a_recycled_pid_is_left_completely_alone(self) -> None:
        # The regression guard. Pids get reused, and killing an unrelated
        # process would be far worse than the orphan this is here to clear.
        # Its own session even though the guard should stop the reap dead:
        # the code under test calls killpg, so a regression here would take
        # the test runner's whole process group with it.
        proc = await asyncio.create_subprocess_exec(
            "sleep", "60", stdout=asyncio.subprocess.DEVNULL, start_new_session=True
        )
        self.addCleanup(lambda: proc.kill() if proc.returncode is None else None)
        _write_capture_registry([
            {"pid": proc.pid, "argv": ["gpu-screen-recorder", "-w", "screen"]}
        ])

        self.assertEqual(reap_orphaned_captures(), 0)
        await asyncio.sleep(0.2)
        self.assertTrue(self._running(proc.pid))

    async def test_a_recorder_whose_daemon_is_alive_is_left_alone(self) -> None:
        # Two Vice daemons have run at once on a real machine. Taking the
        # working one's recorder away would be worse than the bug.
        recorder = await asyncio.create_subprocess_exec(
            "sleep", "60", stdout=asyncio.subprocess.DEVNULL, start_new_session=True
        )
        daemon = await asyncio.create_subprocess_exec(
            "sleep", "60", stdout=asyncio.subprocess.DEVNULL, start_new_session=True
        )
        for p in (recorder, daemon):
            self.addCleanup(lambda p=p: p.kill() if p.returncode is None else None)
        _write_capture_registry([
            {"pid": recorder.pid, "argv": ["sleep", "60"], "owner": daemon.pid}
        ])

        self.assertEqual(reap_orphaned_captures(), 0)
        await asyncio.sleep(0.2)
        self.assertTrue(self._running(recorder.pid))
        # And the entry survives, so the live daemon can still clean up.
        self.assertEqual(len(_read_capture_registry()), 1)

    def test_a_dead_pid_is_not_an_error(self) -> None:
        _write_capture_registry([{"pid": 0x7FFFFFFF, "argv": ["sleep", "60"]}])
        self.assertEqual(reap_orphaned_captures(), 0)

    def test_a_corrupt_registry_is_survivable(self) -> None:
        from vice import recorder as recorder_mod
        recorder_mod.CAPTURE_REGISTRY.parent.mkdir(parents=True, exist_ok=True)
        recorder_mod.CAPTURE_REGISTRY.write_text("{not json")
        self.assertEqual(reap_orphaned_captures(), 0)

    def test_no_registry_at_all_is_not_an_error(self) -> None:
        self.assertEqual(reap_orphaned_captures(), 0)

    def test_register_and_unregister_round_trip(self) -> None:
        _register_capture(4242, ["gpu-screen-recorder", "-w", "screen"])
        entries = _read_capture_registry()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["pid"], 4242)
        self.assertEqual(entries[0]["owner"], os.getpid())
        _unregister_capture(4242)
        self.assertEqual(_read_capture_registry(), [])


class SingleAppWindowTests(unittest.TestCase):
    """Two Vice windows drive the same daemon and disagree with each other
    (#121), so the second one raises the first instead of opening."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.lock = Path(self.tmp.name) / "vice-app.pid"
        patcher = mock.patch("vice.app.APP_LOCK_FILE", self.lock)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._release_lock)
        app_mod._app_lock_handle = None

    @staticmethod
    def _release_lock() -> None:
        # The real app holds this open for its whole life; a test must not.
        handle, app_mod._app_lock_handle = app_mod._app_lock_handle, None
        if handle is not None:
            handle.close()

    def test_the_first_window_takes_the_lock(self) -> None:
        self.assertTrue(app_mod._claim_app_lock())
        self.assertEqual(self.lock.read_text().strip(), str(os.getpid()))

    def test_a_second_process_is_refused_while_it_is_held(self) -> None:
        self.assertTrue(app_mod._claim_app_lock())
        script = (
            "import sys; sys.path.insert(0, %r)\n"
            "import vice.app as app\n"
            "from pathlib import Path\n"
            "app.APP_LOCK_FILE = Path(%r)\n"
            "print('GOT' if app._claim_app_lock() else 'REFUSED')\n"
        ) % (str(Path(__file__).resolve().parents[1]), str(self.lock))
        out = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
        )
        self.assertEqual(out.stdout.strip(), "REFUSED", out.stderr)

    def test_an_unusable_lock_path_never_blocks_the_window(self) -> None:
        # A duplicate window is annoying. No window at all is a broken app.
        with mock.patch("vice.app.APP_LOCK_FILE", Path("/proc/nope/vice-app.pid")):
            self.assertTrue(app_mod._claim_app_lock())


class CustomNotificationSoundTests(unittest.TestCase):
    """A mistyped path must never turn into silence: the sound is how you
    know the clip landed (#123)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_a_readable_file_is_used(self) -> None:
        sound = self.dir / "ping.wav"
        sound.write_bytes(b"RIFF....WAVEfmt ")
        self.assertEqual(audio_mod.resolve_custom_sound(str(sound)), sound)

    def test_blank_settings_fall_back_to_the_tone(self) -> None:
        for value in (None, "", "   "):
            self.assertIsNone(audio_mod.resolve_custom_sound(value))

    def test_a_missing_file_falls_back_to_the_tone(self) -> None:
        self.assertIsNone(audio_mod.resolve_custom_sound(str(self.dir / "nope.wav")))

    def test_an_empty_file_falls_back_to_the_tone(self) -> None:
        empty = self.dir / "empty.wav"
        empty.touch()
        self.assertIsNone(audio_mod.resolve_custom_sound(str(empty)))

    def test_a_directory_falls_back_to_the_tone(self) -> None:
        self.assertIsNone(audio_mod.resolve_custom_sound(str(self.dir)))

    def test_an_unreadable_file_falls_back_to_the_tone(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root can read anything")
        locked = self.dir / "locked.wav"
        locked.write_bytes(b"RIFF")
        os.chmod(locked, 0o000)
        try:
            self.assertIsNone(audio_mod.resolve_custom_sound(str(locked)))
        finally:
            os.chmod(locked, 0o644)

    def test_a_home_relative_path_is_expanded(self) -> None:
        # Settings takes a typed path, and people type ~.
        with mock.patch.dict(os.environ, {"HOME": str(self.dir)}):
            sound = self.dir / "tilde.wav"
            sound.write_bytes(b"RIFF")
            self.assertEqual(audio_mod.resolve_custom_sound("~/tilde.wav"), sound)

    def test_a_path_pasted_from_a_file_manager_is_understood(self) -> None:
        # #297: file managers copy a location quoted, or as a file:// URI.
        sound = self.dir / "medal clip.wav"
        sound.write_bytes(b"RIFF")
        for pasted in (
            f'"{sound}"',
            f"'{sound}'",
            f"  {sound}\n",
            sound.as_uri(),
            f'"{sound.as_uri()}"',
        ):
            with self.subTest(pasted=pasted):
                self.assertEqual(audio_mod.resolve_custom_sound(pasted), sound)

    def test_every_sound_has_a_synthesised_tone_to_fall_back_on(self) -> None:
        for name in ("clip", "clip_failed", "session_start", "session_end", "highlight"):
            self.assertTrue(audio_mod._wav_for(name, 1.0))


class SoundPackTests(unittest.TestCase):
    """#300: a built-in pack of recorded sounds as well as the tones."""

    def test_every_pack_file_matches_the_tone_format(self) -> None:
        from importlib.resources import files
        import wave
        for pack in audio_mod.SOUND_PACKS[1:]:
            folder = files("vice") / "sounds" / pack
            names = [p.name for p in folder.iterdir() if p.name.endswith(".wav")]
            self.assertTrue(names, pack)
            for name in names:
                with self.subTest(pack=pack, name=name), wave.open(str(folder / name)) as w:
                    self.assertEqual((w.getsampwidth(), w.getnchannels(), w.getframerate()), (2, 2, 48000))
                    self.assertIn(name[:-4], audio_mod._SPECS)

    def test_a_pack_sound_is_used_and_scaled(self) -> None:
        loud = audio_mod._wav_for("clip", 1.0, "futuristic")
        quiet = audio_mod._wav_for("clip", 0.5, "futuristic")
        self.assertNotEqual(loud, audio_mod._wav_for("clip", 1.0))
        self.assertEqual(len(loud), len(quiet))
        self.assertLess(max(abs(v) for v in memoryview(quiet[44:]).cast("h")),
                        max(abs(v) for v in memoryview(loud[44:]).cast("h")))

    def test_highlight_borrows_the_clip_sound(self) -> None:
        self.assertEqual(audio_mod._wav_for("highlight", 1.0, "futuristic"),
                         audio_mod._wav_for("clip", 1.0, "futuristic"))

    def test_an_unknown_pack_is_the_classic_tones(self) -> None:
        self.assertEqual(audio_mod.normalize_pack("nope"), "classic")
        from vice.config import Config, normalize_notifications
        cfg = Config()
        cfg.notifications.sound_pack = "Futuristic"
        normalize_notifications(cfg)
        self.assertEqual(cfg.notifications.sound_pack, "futuristic")


class CustomSoundPlaybackTests(unittest.IsolatedAsyncioTestCase):
    """A file that exists but will not play must not turn into silence (#297)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.played = self.dir / "played.txt"
        self.custom = self.dir / "custom.wav"
        self.custom.write_bytes(b"not really audio")
        player = self.dir / "player"
        player.write_text(
            "#!/bin/sh\n"
            f'echo "$1" >> "{self.played}"\n'
            f'if [ "$1" = "{self.custom}" ]; then echo "Failed to open audio file." >&2; exit 1; fi\n'
        )
        player.chmod(0o755)
        for target, value in (
            ("vice.audio._find_player", mock.Mock(return_value=str(player))),
            ("vice.audio._TMP_DIR", self.dir / "tones"),
            ("vice.audio._converted", {}),
        ):
            patcher = mock.patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_an_unplayable_file_falls_back_to_the_tone_and_says_why(self) -> None:
        with self.assertLogs("vice.audio", level="WARNING") as logs:
            await audio_mod._play("clip", 1.0, str(self.custom))
        lines = self.played.read_text().splitlines()
        self.assertEqual(lines[0], str(self.custom))
        self.assertEqual(len(lines), 2)
        self.assertIn("snd_clip_", lines[1])
        self.assertIn("Failed to open audio file", "\n".join(logs.output))

    async def test_a_file_that_cannot_be_used_is_only_tried_once(self) -> None:
        with self.assertLogs("vice.audio", level="WARNING"):
            await audio_mod._play("clip", 1.0, str(self.custom))
        await audio_mod._play("clip", 1.0, str(self.custom))
        lines = self.played.read_text().splitlines()
        self.assertEqual(lines.count(str(self.custom)), 1)
        self.assertEqual(len(lines), 3)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
    async def test_a_file_the_player_cannot_read_is_converted_and_played(self) -> None:
        # AAC saved with a .wav name: what paplay answers "Failed to open
        # audio file." to (#297).
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=600:d=0.3",
             "-c:a", "aac", "-f", "mp4", str(self.custom)], check=True)
        with self.assertLogs("vice.audio", level="INFO") as logs:
            await audio_mod._play("clip", 1.0, str(self.custom))
        lines = self.played.read_text().splitlines()
        self.assertEqual(lines[0], str(self.custom))
        self.assertEqual(len(lines), 2, lines)
        self.assertIn("snd_custom_", lines[1])
        with wave.open(lines[1]) as converted:
            self.assertEqual(
                (converted.getnchannels(), converted.getframerate(), converted.getsampwidth()),
                (2, 48000, 2))
            self.assertGreater(converted.getnframes(), 48000 * 0.2)
        self.assertIn("converted copy", "\n".join(logs.output))
        self.assertNotIn("WARNING", "\n".join(logs.output))

        # Next time the copy plays straight away.
        await audio_mod._play("clip", 1.0, str(self.custom))
        self.assertEqual(self.played.read_text().splitlines()[2], lines[1])

        # A changed file is looked at again.
        self.custom.write_bytes(b"not really audio any more")
        with self.assertLogs("vice.audio", level="WARNING"):
            await audio_mod._play("clip", 1.0, str(self.custom))
        self.assertIn("snd_clip_", self.played.read_text().splitlines()[-1])

    async def test_a_tone_that_fails_is_not_retried(self) -> None:
        with mock.patch("vice.audio._wav_for", return_value=b"tone"):
            player = self.dir / "failing"
            player.write_text(f'#!/bin/sh\necho "$1" >> "{self.played}"\nexit 1\n')
            player.chmod(0o755)
            with mock.patch("vice.audio._find_player", return_value=str(player)):
                await audio_mod._play("clip", 1.0, None)
        self.assertEqual(len(self.played.read_text().splitlines()), 1)


class ClipErrorReasonTests(unittest.TestCase):
    """"Clip save failed. Check vice.log" is no use to somebody whose clips
    are silently unreadable (#154)."""

    def test_the_recorders_reason_is_used_when_there_is_one(self) -> None:
        daemon = main_mod.ViceDaemon.__new__(main_mod.ViceDaemon)
        daemon.recorder = mock.Mock()
        daemon.recorder.last_clip_error = "Vice_Clip_3.mp4 was written but cannot be read"
        self.assertIn("cannot be read", daemon._clip_error_text())

    def test_it_falls_back_when_the_recorder_has_nothing_to_say(self) -> None:
        daemon = main_mod.ViceDaemon.__new__(main_mod.ViceDaemon)
        daemon.recorder = mock.Mock()
        daemon.recorder.last_clip_error = ""
        self.assertIn("vice.log", daemon._clip_error_text())

    def test_a_recorder_without_the_attribute_does_not_crash(self) -> None:
        # Third-party or older recorder objects must not break the toast.
        daemon = main_mod.ViceDaemon.__new__(main_mod.ViceDaemon)
        daemon.recorder = object()
        self.assertIn("vice.log", daemon._clip_error_text())


class ReapGroupSafetyTests(unittest.IsolatedAsyncioTestCase):
    """reap_orphaned_captures signals a process *group*. Capture processes
    always lead their own group, so anything that does not is somebody
    else's group and must never be signalled."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch(
            "vice.recorder.CAPTURE_REGISTRY", Path(self.tmp.name) / "capture.json"
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_a_pid_that_does_not_lead_its_group_is_not_signalled(self) -> None:
        # Shares this process's group, so a killpg here would take the test
        # runner down with it.
        proc = await asyncio.create_subprocess_exec(
            "sleep", "60", stdout=asyncio.subprocess.DEVNULL
        )
        self.addCleanup(lambda: proc.kill() if proc.returncode is None else None)
        self.assertNotEqual(os.getpgid(proc.pid), proc.pid)
        # Matching argv and no live owner, so only the group check can save us.
        # Without the wait the argv would not match yet and the test would pass
        # for the wrong reason.
        await _wait_until_exec(proc.pid, ["sleep", "60"])
        _write_capture_registry([{"pid": proc.pid, "argv": ["sleep", "60"]}])

        with mock.patch("vice.recorder.os.killpg") as killpg:
            self.assertEqual(reap_orphaned_captures(), 0)
        killpg.assert_not_called()
        self.assertIsNone(proc.returncode)
