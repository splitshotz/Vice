"""
Vice: Linux game clip recorder daemon + CLI.

Commands:
  vice start          Start the daemon (recorder + hotkey listener + share server)
  vice ui             Open the web UI in the default browser
  vice clip [SECS]    Save a clip right now, optionally of SECS seconds
  vice stop           Stop the daemon
  vice status         Show daemon status and recent clips
  vice doctor         Print startup diagnostics for environment/package issues
  vice config         Print the current config path and contents
  vice list-keys      Show available hotkey names (KEY_*)
  vice open-config    Open config in $EDITOR
  vice uninstall      Remove Vice cleanly (service, config, optionally clips)
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from dataclasses import asdict, dataclass
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

import click

from . import __version__
from .active_window import steam_game_name
from .session_process import ProcessIdentity, bind_game_process, probe_game_process
from .active_window import scan_game_windows, auto_start_scan_supported
from .session_auto_start import AutoStartStateError, MalformedAutoStartState, SuppressionStore, read_host_boot_id
from .config import (
    CLIP_DURATION_MIN,
    Config,
    CONFIG_DIR,
    CONFIG_PATH,
    effective_clip_bindings,
    normalize_combo,
    load as load_config,
    save as save_config,
)
from .hotkey import HotkeyListener, can_access_hotkeys, list_available_keys
from .keys import KeyRing, save_sidecar as save_key_sidecar
from .media import cleanup_temp_files, probe_media
from .overlay_host import KeyOverlayHost
from .platform_info import detect_platform
from . import gsr_install
from .voice import VoiceListener, get_model_path, is_vosk_available
from .recorder import (GSR_WINDOW_CAPTURE_WAYLAND_ERROR, GSRRecorder, capture_screenshot,
                       create_recorder, filename_tag, gsr_flatpak_installed, gsr_source,
                       refresh_gsr_probes,
                       gsr_window_capture_supported, reap_orphaned_captures)
from .runtime import (
    actual_home_dir,
    claim_daemon_lock,
    daemon_is_running,
    normalize_runtime_environment,
    resolve_path,
    has_display,
    installed_version,
    load_user_systemd_env,
    runtime_env_snapshot,
    running_under_systemd,
    in_vice_flatpak,
    VICE_FLATPAK_ID,
    systemd_unit_loaded,
    user_systemd_env_snapshot,
    wait_for_display,
)
from .share import ShareServer, copy_image_to_clipboard
from . import audio
from . import updates

log = logging.getLogger("vice")


def _load_default_games() -> list[dict]:
    """Load the bundled games.json. Returns [] if missing/corrupt rather
    than crashing the daemon."""
    try:
        from importlib.resources import files
        text = (files("vice") / "data" / "games.json").read_text(encoding="utf-8")
        data = json.loads(text)
        if isinstance(data, list):
            return [g for g in data if isinstance(g, dict) and g.get("name")]
    except Exception as exc:
        log.warning("Failed to load bundled games.json: %s", exc)
    return []


_DEFAULT_GAMES: list[dict] = _load_default_games()


_STEAM_ID_NEEDLE = re.compile(r"steam_app_\d+")
# The kernel keeps 15 characters of a process name (TASK_COMM_LEN).
_COMM_MAX = 15


def _needle_hits(needle: str, haystacks) -> bool:
    if _STEAM_ID_NEEDLE.fullmatch(needle):
        # An app id is a whole number: Half-Life's steam_app_70 is not
        # SCP: Secret Laboratory's steam_app_700330 (#264).
        tail = re.compile(re.escape(needle) + r"(?!\d)")
        return any(tail.search(h) for h in haystacks)
    return any(needle in h for h in haystacks)


def _best_game_match(entries, haystacks) -> Optional[str]:
    """Name of the entry whose longest needle matches, or None.

    Needles are substrings, so a short one swallows a longer one that is more
    specific: "hades" matches Hades II's "hades2.exe", and a generic
    "client-win64-shipping.exe" matches every Unreal game that ships under
    that name. Taking the longest match rather than the first in list order
    means the specific entry wins wherever the two overlap. Equal lengths keep
    list order, so nothing else moves.
    """
    best_name: Optional[str] = None
    best_len = 0
    for name, matches in entries:
        for needle in matches or []:
            n = (needle or "").lower()
            # Length first: it is cheap and skips most substring scans.
            if len(n) > best_len and _needle_hits(n, haystacks):
                best_name, best_len = name, len(n)
    return best_name


def _truncated_process_match(entries, process: str) -> Optional[str]:
    """The entry with the longest needle the kernel would cut down to
    *process*, when *process* is exactly as long as it keeps. Rocket League
    runs as "RocketLeague.ex", which its needle "RocketLeague.exe" never
    matched (#262). Only used once nothing else names the game, because a
    cut-down generic name ("client-win64-sh") is weaker evidence than any
    full match."""
    if len(process) != _COMM_MAX:
        return None
    best_name: Optional[str] = None
    best_len = 0
    for name, matches in entries:
        for needle in matches or []:
            n = (needle or "").lower()
            if len(n) > max(best_len, _COMM_MAX) and n.startswith(process):
                best_name, best_len = name, len(n)
    return best_name

PID_FILE    = Path("/tmp/vice/vice.pid")
SOCKET_FILE = Path("/tmp/vice/vice.sock")
USER_BIN_DIR = actual_home_dir() / ".local" / "bin"
INSTALL_VENV_DIR = actual_home_dir() / ".local" / "share" / "vice" / "venv"
USER_DESKTOP_FILE = actual_home_dir() / ".local" / "share" / "applications" / "vice.desktop"
USER_ICON_FILE = (
    actual_home_dir()
    / ".local"
    / "share"
    / "icons"
    / "hicolor"
    / "scalable"
    / "apps"
    / "vice.svg"
)
DAEMON_LOG_FILE = actual_home_dir() / ".local" / "share" / "vice" / "vice.log"

# Consecutive unexpected recorder deaths before the watchdog starts backing
# off. Two is normal turbulence (a driver reset, a suspend edge); a third in a
# row means the recorder is not coming back on its own.
_RECORDER_DEATH_BACKOFF_AFTER = 3

# How often follow-the-pointer capture samples which monitor the pointer is on.
# Two samples must agree before the recorder is retargeted, so a switch costs
# up to twice this.
FOLLOW_MOUSE_INTERVAL = 2.0

# How often window_capture re-checks the focused window against games.json.
# Checked once immediately too, so an already-focused game gets pinned right
# away at startup. Slower than FOLLOW_MOUSE_INTERVAL since a late pin/release
# just means the previous target keeps recording a bit longer, not a visible
# glitch.
WINDOW_CAPTURE_INTERVAL = 5.0
SESSION_GAME_POLL_INTERVAL = 1.0
SESSION_GAME_EXIT_DELAY = 5.0
SESSION_AUTO_START_INTERVAL = 2.0


@dataclass(frozen=True)
class AutoStartTarget:
    game: str
    window: dict
    identity: ProcessIdentity


# ──────────────────────────────────────────────────────────────────────────────
# Daemon
# ──────────────────────────────────────────────────────────────────────────────

class ViceDaemon:
    def __init__(self) -> None:
        self.cfg      = load_config()
        self.recorder = create_recorder(self.cfg)
        self.hotkeys  = HotkeyListener()
        self.share:   Optional[ShareServer] = None
        # Keyboard visualiser (#209): the ring holds the overlay's keys for
        # clips, the host runs the on-screen panel. Both idle until enabled.
        self.key_ring: Optional[KeyRing] = None
        self.key_overlay = KeyOverlayHost(self._on_key_overlay_moved, self._on_key_overlay_state)
        self.voice = VoiceListener(on_trigger=self._handle_clip_hotkey)
        self.hotkeys_available = can_access_hotkeys()
        self._clip_lock  = asyncio.Lock()
        self._clip_count = 0
        # Session recording state
        self._session_lock = asyncio.Lock()
        self._session_token: Optional[object] = None
        self._session_monitor_task: Optional[asyncio.Task] = None
        self._session_game: Optional[str] = None
        self._session_active   = False
        self._session_path:    Optional[Path] = None
        self._session_highlights: list[dict] = []  # {time, label, color}
        self._session_process_identity: Optional[ProcessIdentity] = None
        self._init_auto_start()
        self._recording_sig = self._recording_signature()
        self._pending_recording_apply = False
        self._config_apply_lock = asyncio.Lock()
        self._clip_task: Optional[asyncio.Task] = None
        self._screenshot_task: Optional[asyncio.Task] = None
        self._watchdog_task: Optional[asyncio.Task] = None
        self._upgrade_task: Optional[asyncio.Task] = None
        self._update_task: Optional[asyncio.Task] = None
        self._update: Optional[dict] = None
        self._ready = False
        # Why the recorder is not running, for the UI banner. Empty when it is.
        self._recorder_error = ""
        # Discord Rich Presence, default enabled, but only shown for matched games.
        self._discord_rpc = None  # type: ignore[var-annotated]
        self._discord_task: Optional[asyncio.Task] = None
        self._discord_client_id: Optional[str] = None
        self._discord_current_game: Optional[str] = None
        self._discord_started_at: float = 0.0
        self._discord_last_activity: Optional[dict] = None
        self._discord_current_pid = 0
        self._discord_game_comm = ""
        self._discord_scan_tick = 0
        self._discord_no_socket_logged = False
        self._discord_no_window_adapter_logged = False
        # Game detected while the most recent clip was being saved, consumed
        # by _on_clip_saved to file the clip into its auto playlist.
        self._last_clip_game: Optional[str] = None
        # How many windows the last visible-window scan could see.
        self._last_scan_candidates: int = 0
        # Monitor the pointer is on, when follow-the-pointer capture is on.
        # None means "use recording.display".
        self._display_override: Optional[str] = None
        self._follow_mouse_task: Optional[asyncio.Task] = None
        # Window id capture is pinned to, when window_capture is on and a
        # recognized game (games.json) is focused. None means full display.
        self._window_capture_id: Optional[str] = None
        self._window_capture_task: Optional[asyncio.Task] = None
        self._gsr_install_task: Optional[asyncio.Task] = None
        # Window ids a pin already failed on. Every attempt restarts the
        # recorder and empties the buffer, so one is never tried twice.
        self._window_capture_failed: set[str] = set()
        # "wayland" once GSR has refused window capture outright (#261, #274).
        self._window_capture_refused: Optional[str] = None

    @staticmethod
    def _output_dir_problem(out_dir: Path) -> str:
        """Why clips cannot be written to out_dir, or "" if they can."""
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            probe = out_dir / ".vice-write-test"
            probe.touch()
            probe.unlink()
        except OSError as exc:
            return (
                f"Clip output directory {out_dir} is not writable: {exc}. "
                "Fix permissions or change output.directory in "
                f"{CONFIG_PATH}."
            )
        return ""

    async def run(self) -> None:
        Path("/tmp/vice").mkdir(parents=True, exist_ok=True)
        out_dir = resolve_path(self.cfg.output.directory)

        # A capture process runs in its own session so its helper can be
        # reaped with it (#129), which also means kill -9 on the daemon
        # leaves it recording forever and the next start adds another one
        # next to it (#121). Clear any survivor before starting our own.
        try:
            reap_orphaned_captures()
        except Exception:
            log.exception("Could not check for a leftover recorder")

        # Share server (web UI + REST API + WebSocket)
        if self.cfg.sharing.enabled:
            self.share = ShareServer(self.cfg)
            self.share.trigger_clip_cb = self._handle_clip_hotkey
            self.share.check_update_cb = lambda: self.run_update_check(force=True)
            self.share.get_status_cb   = self._get_status
            self.share.apply_config_cb = self._apply_live_config
            self.share.key_overlay_move_cb = self.key_overlay.set_moving
            self.share.install_gsr_cb = self._install_gsr
            try:
                await self.share.start()
            except Exception:
                log.exception(
                    "Failed to start share server on 127.0.0.1:%s",
                    self.cfg.sharing.port,
                )
                raise

        # Wire the recorder's callbacks. Must also run when a settings change
        # swaps in a new recorder, or game tagging and auto playlists silently
        # stop working.
        self._wire_recorder(self.recorder)

        # Hotkeys
        self._bind_hotkeys()
        self.hotkeys.on_availability_change = self._on_hotkey_availability
        self.hotkeys.key_observer = self._on_key_event
        clip_key = self.cfg.hotkeys.clip

        PID_FILE.write_text(str(os.getpid()))

        server = await asyncio.start_unix_server(
            self._handle_ipc, path=str(SOCKET_FILE)
        )

        async def _abort_startup() -> None:
            try:
                server.close()
                await server.wait_closed()
            except Exception as exc:
                log.debug("IPC server did not close cleanly: %s", exc)
            try:
                await self.hotkeys.stop()
            except Exception as exc:
                log.debug("Hotkey listener did not stop cleanly: %s", exc)
            try:
                await self.recorder.stop()
            except Exception as exc:
                log.debug("Recorder did not stop cleanly: %s", exc)
            if self.share:
                try:
                    await self.share.stop()
                except Exception as exc:
                    log.debug("Share server did not stop cleanly: %s", exc)
            for p in (PID_FILE, SOCKET_FILE):
                try:
                    if p.exists():
                        p.unlink()
                except OSError:
                    pass

        try:
            await self.hotkeys.start()
            self.hotkeys_available = self.hotkeys.available
        except Exception as exc:
            log.error("Vice daemon failed during startup: %s", exc)
            log.exception("Startup traceback")
            await _abort_startup()
            raise

        # An output directory that has gone away (an unmounted drive, most
        # often) reads as a recorder problem to the user, so it goes through
        # the same banner rather than killing the daemon before it can say
        # anything. It used to be checked before the UI existed, so the
        # daemon died with the reason only ever reaching stderr (#142).
        dir_problem = self._output_dir_problem(out_dir)
        if dir_problem:
            self._ready = False
            self._recorder_error = dir_problem
            log.error("%s", dir_problem)
            if not self.share:
                await _abort_startup()
                raise RuntimeError(dir_problem)
        else:
            # Remove half-written temp files (trim/watermark/remux) from a
            # previous run that was interrupted mid-edit.
            cleanup_temp_files(out_dir)
            img_dir = resolve_path(getattr(self.cfg.output, "image_directory", "") or "")
            if img_dir and img_dir.exists() and img_dir != out_dir:
                cleanup_temp_files(img_dir)

        # A recorder that will not start is not a reason to take the UI down
        # with it. It used to be: the share server was stopped on the way out,
        # so the app reported "the UI server did not respond" and the user had
        # no way to reach Settings and pick an encoder that works, which is the
        # one thing that would have fixed it (#156).
        # Starting it on top of an unusable output directory only replaces a
        # clear message with a confusing one, so that case skips straight to
        # the watchdog, which retries once the directory comes back.
        if not dir_problem:
            try:
                await self.recorder.start()
                self._ready = True
                self._recorder_error = ""
            except Exception as exc:
                self._ready = False
                self._recorder_error = str(exc)
                log.error(
                    "Recorder failed to start (backend=%s): %s",
                    self.recorder.name, exc,
                )
                log.exception("Recorder startup traceback")
                if not self.share:
                    # No UI to explain it through, so this is still fatal.
                    await _abort_startup()
                    raise
        if not self._ready and self.share:
            log.error(
                "Vice is running without a recorder. Open the Vice window to "
                "see why and to change recording settings."
            )

        if self.share:
            log.info("Vice local control UI: %s", self.share.local_base_url())
        else:
            log.info("Vice local control UI disabled by config")
        log.info(
            "Vice daemon ready (backend=%s, share_enabled=%s, recording=%s)",
            self.recorder.name,
            bool(self.share),
            self._ready,
        )

        if self.share:
            asyncio.create_task(
                self.share.broadcast({
                    "type": "status", "recording": self._ready, "ready": self._ready,
                    "backend": self.recorder.name,
                    "session_active": self._session_active,
                    "clip_key": self.cfg.hotkeys.clip,
                    "hotkeys_available": self.hotkeys_available,
                    "recorder_error": self._recorder_error,
                    "cpu_fallback": bool(getattr(self.recorder, "cpu_fallback", False)),
                    "cpu_fallback_reason": getattr(self.recorder, "cpu_fallback_reason", None),
                    "codec_fallback": bool(getattr(self.recorder, "codec_fallback", False)),
                })
            )

        if self._ready:
            click.echo(f"[Vice {__version__}] Recording started.")
        else:
            click.echo(f"[Vice {__version__}] Started, but not recording.")
            click.echo(f"  Problem   : {self._recorder_error}")
        click.echo(f"  Backend   : {self.recorder.name}")
        click.echo(f"  Clip key  : {clip_key or '(none)'}")
        click.echo(f"  Output    : {self.cfg.output.directory}")
        if self.share and self.share.local_base_url():
            click.echo(f"  UI URL    : {self.share.local_base_url()}/")
        if self.share and self.share.public_base_url():
            click.echo(f"  Share URL : {self.share.public_base_url()}/")
        click.echo("Press Ctrl-C to stop.\n")

        if self.cfg.discord.enabled:
            self._discord_task = asyncio.create_task(self._discord_presence_loop())

        self._watchdog_task = asyncio.create_task(self._recorder_watchdog_loop())
        self._upgrade_task = asyncio.create_task(self._upgrade_watch_loop())
        self._sync_follow_mouse_task()
        self._sync_window_capture_task()
        await self._sync_key_overlay()
        if self.voice:
            await self.voice.sync(self.cfg.voice, self.cfg.recording.microphone_source)

        if self.cfg.updates.check_on_start:
            self._update_task = asyncio.create_task(self._update_check_soon())

        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()
        loop.add_signal_handler(signal.SIGTERM, stop_event.set)
        loop.add_signal_handler(signal.SIGINT,  stop_event.set)

        await self._sync_auto_start_task()
        await stop_event.wait()
        await self._shutdown(server)

    def _on_hotkey_availability(self, available: bool) -> None:
        """Keep the UI banner truthful when keyboards unplug/replug."""
        self.hotkeys_available = available
        if self.share:
            asyncio.create_task(
                self.share.broadcast({
                    "type": "status", "recording": self._ready, "ready": self._ready,
                    "backend": self.recorder.name,
                    "session_active": self._session_active,
                    "clip_key": self.cfg.hotkeys.clip,
                    "hotkeys_available": available,
                    "recorder_error": self._recorder_error,
                    "cpu_fallback": bool(getattr(self.recorder, "cpu_fallback", False)),
                    "cpu_fallback_reason": getattr(self.recorder, "cpu_fallback_reason", None),
                    "codec_fallback": bool(getattr(self.recorder, "codec_fallback", False)),
                })
            )

    def _recording_signature(self) -> str:
        """Stable representation of recording config for live-apply checks.

        Track volumes are applied when a clip is saved and never reach the
        recorder, so moving one must not restart it and empty the buffer."""
        recording = asdict(self.cfg.recording)
        recording.pop("audio_track_volumes", None)
        # Session lifecycle settings are captured at start, not capture options.
        recording.pop("session_stop_on_game_exit", None)
        recording.pop("session_start_on_game_detect", None)
        return json.dumps(recording, sort_keys=True)

    def _clips_in_library(self) -> int:
        """Size of the clip library.

        self._clip_count only ever counted saves made by this process, so a
        daemon that had been running a while reported a handful against a
        library of dozens. The share server scans the output directory and is
        the only thing that knows; the counter is the fallback for when the
        share server is not up.
        """
        return self.share.clip_count() if self.share else self._clip_count

    def _on_clip_saved(self, path: Path) -> None:
        self._clip_count += 1
        click.echo(f"\n[Vice] Clip saved: {path}")
        if self.share:
            # Session clips are added to the share server inside _stop_session;
            # only add here for regular replay-buffer clips (not sessions).
            if not path.name.startswith("Vice_Session_"):
                url = self.share.add_clip(path, game=self._last_clip_game)
                self._last_clip_game = None
                click.echo(f"[Vice] Share URL:  {url}\n")
                if self.key_ring is not None and self.cfg.overlay.keys_in_clips:
                    end = getattr(self.recorder, "last_save_signal_at", None) or time.monotonic()
                    asyncio.create_task(self._store_clip_keys(path, end))
            asyncio.create_task(
                self.share.broadcast({
                    "type": "status", "recording": self._ready, "ready": self._ready,
                    "backend": self.recorder.name,
                    "session_active": self._session_active,
                    "clip_key": self.cfg.hotkeys.clip,
                    "hotkeys_available": self.hotkeys_available,
                    "recorder_error": self._recorder_error,
                    "cpu_fallback": bool(getattr(self.recorder, "cpu_fallback", False)),
                    "cpu_fallback_reason": getattr(self.recorder, "cpu_fallback_reason", None),
                    "codec_fallback": bool(getattr(self.recorder, "codec_fallback", False)),
                })
            )

    def _broadcast_status(self, recording: bool) -> None:
        if not self.share:
            return
        asyncio.create_task(
            self.share.broadcast({
                "type": "status", "recording": recording, "ready": self._ready,
                "backend": self.recorder.name,
                "session_active": self._session_active,
                "clip_key": self.cfg.hotkeys.clip,
                "hotkeys_available": self.hotkeys_available,
                "recorder_error": self._recorder_error,
                "cpu_fallback": bool(getattr(self.recorder, "cpu_fallback", False)),
                "cpu_fallback_reason": getattr(self.recorder, "cpu_fallback_reason", None),
                "codec_fallback": bool(getattr(self.recorder, "codec_fallback", False)),
                "disk": self._disk_stats(),
            })
        )

    async def _upgrade_watch_loop(self) -> None:
        """Restart the daemon after its own files have been replaced on disk.

        An upgrade swaps the files, and this process keeps the old Python in
        memory because Python cannot reload it. Until now the only thing that
        noticed was vice-app at launch, so a user who upgraded without opening
        the window kept running the old daemon and had to know to type
        `systemctl --user restart vice.service`.

        Deliberately narrow. It only fires when systemd owns the unit, because
        systemd is what starts the replacement: exiting on our own where
        nothing supervises us would leave the user with no daemon at all,
        which is a far worse outcome than a stale one. Every probe that fails
        means "do nothing", so on any machine where this cannot work the
        behaviour is byte-identical to before.
        """
        interval = 30.0
        running = __version__
        unit_checked = False
        unit_present = False

        while True:
            await asyncio.sleep(interval)

            on_disk = installed_version()
            if not on_disk or on_disk == running:
                continue

            # Only ask systemd once there is something to ask about, so the
            # common case never shells out at all.
            if not unit_checked:
                unit_present = await asyncio.to_thread(systemd_unit_loaded)
                unit_checked = True
            if not unit_present:
                log.info(
                    "Vice %s is installed but this daemon is %s. No systemd unit here, "
                    "so restart it yourself to pick up the new code.",
                    on_disk, running,
                )
                return

            # Never in the middle of the user's recording. A restart here would
            # drop a session partway through or lose a clip that is still being
            # written, and the upgrade can wait as long as it needs to.
            if self._session_active or (self._clip_task and not self._clip_task.done()):
                log.debug("Upgrade to %s is waiting for the recorder to go idle", on_disk)
                continue

            log.info("Vice %s is installed, restarting this %s daemon to pick it up",
                     on_disk, running)
            if self.share:
                try:
                    await self.share.broadcast({"type": "daemon_upgrading", "version": on_disk})
                except Exception as exc:
                    log.debug("Could not announce the upgrade to the UI: %s", exc)

            await self._restart_via_systemd()
            return

    async def _restart_via_systemd(self) -> None:
        """Hand the restart to systemd and let it SIGTERM us.

        Detached with its own session on purpose: systemctl outlives the
        process it is restarting, and a child sharing our process group would
        be torn down with us before it could start the replacement.
        """
        try:
            await asyncio.create_subprocess_exec(
                "systemctl", "--user", "restart", "vice.service",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            # Nothing is restarting us, so stay up on the old code rather than
            # leaving the user with no daemon.
            log.warning("Could not ask systemd to restart Vice: %s", exc)

    async def _recorder_watchdog_loop(self) -> None:
        """Restart the recorder when its capture process dies (driver reset,
        crash) or after suspend/resume, which kills GPU encoder contexts even
        when the process survives (#116). asyncio.sleep runs on the monotonic
        clock, which stands still during suspend, so a wall-clock jump across
        one tick means the machine slept."""
        interval = 5.0
        backoff = interval
        deaths = 0
        last_wall = time.time()
        while True:
            await asyncio.sleep(interval)
            now = time.time()
            resumed = (now - last_wall) > interval + 30.0
            last_wall = now
            if self.recorder.is_healthy() and not resumed:
                backoff = interval
                deaths = 0
                continue
            if resumed:
                log.info("Resume from suspend detected. Restarting the recorder")
            else:
                deaths += 1
                # The capture process's own output is the only thing that says
                # why it died. Without it a fatal encoder error is invisible at
                # default log level and the UI still claims to be recording
                # (#129).
                tail = self.recorder.last_output()
                if tail:
                    log.error(
                        "Recorder process died unexpectedly, restarting. Last output from %s:\n%s",
                        self.recorder.name, tail,
                    )
                else:
                    log.error("Recorder process died unexpectedly, restarting")

            # An unmounted clip directory would otherwise surface as a bare
            # mkdir errno once a tick, losing the message that says what to
            # fix (#142).
            dir_problem = self._output_dir_problem(resolve_path(self.cfg.output.directory))
            if dir_problem:
                log.error("%s Retrying in %.0f s", dir_problem, backoff)
                self._ready = False
                self._recorder_error = dir_problem
                self._broadcast_status(recording=False)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300.0)
                last_wall = time.time()
                continue

            # A daemon started at boot can come up before the desktop has
            # exported its session, and a restart would otherwise reuse that
            # environment forever. Plasma's XAUTHORITY was the one missing in
            # #231: every retry failed until a manual restart picked it up.
            filled = await asyncio.to_thread(load_user_systemd_env)
            if filled:
                log.info("Picked up %s from the session before restarting the recorder",
                         ", ".join(filled))

            try:
                async with self._config_apply_lock:
                    async with self._clip_lock:
                        if not resumed and self.recorder.is_healthy():
                            continue  # a config apply already replaced it
                        if not resumed and self._window_capture_id:
                            # The dead process was pinned to a game window,
                            # relaunching against the same window id can come
                            # back "healthy" but capturing solid black (the
                            # window's NVFBC/capture session is wedged, not
                            # just the process). Drop the pin so the restart
                            # captures full-display, and let
                            # _window_capture_loop re-resolve and re-pin the
                            # window fresh on its next tick, the same
                            # recovery a manual Game Capture off/on does.
                            log.warning(
                                "Recorder died while pinned to a game window. "
                                "Dropping the pin so it re-acquires the window fresh"
                            )
                            self._window_capture_id = None
                            self.recorder.window_override = None
                        await self.recorder.stop()
                        await self.recorder.start()
            except Exception as exc:
                log.error("Recorder restart failed: %s. Retrying in %.0f s", exc, backoff)
                self._ready = False
                self._recorder_error = str(exc)
                self._broadcast_status(recording=False)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300.0)
                last_wall = time.time()
                continue
            log.info("Recorder restarted (backend=%s)", self.recorder.name)
            # Clears the banner when the watchdog recovers a recorder that
            # never came up in the first place (#156).
            self._ready = True
            self._recorder_error = ""
            self._broadcast_status(recording=True)
            # A process that clears the startup probe and then dies seconds
            # later never reaches the failed-start path above, so without this
            # an unrecoverably broken recorder is retried at full speed
            # forever (#129).
            if not resumed and deaths >= _RECORDER_DEATH_BACKOFF_AFTER:
                log.error(
                    "Recorder has died %d times in a row, waiting %.0f s before the next attempt",
                    deaths, backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300.0)
                last_wall = time.time()

    def _wire_recorder(self, recorder) -> None:
        """Attach the daemon's callbacks to a recorder. Fires for the initial
        recorder and for every replacement built on a config change, so game
        filename tagging and auto playlists survive settings changes."""
        recorder.on_clip_saved(self._on_clip_saved)
        # Tag clip filenames with the focused game (curated list, same
        # detection as Discord Rich Presence); also feeds the auto playlists.
        recorder.clip_tag_cb = self._clip_game_tag
        recorder.display_override = self._display_override
        recorder.window_override = self._window_capture_id

    async def _restart_recorder_for_config(self) -> bool:
        """Restart recorder without running two capture processes at once."""
        if self._session_active or self._session_lock.locked():
            self._pending_recording_apply = True
            log.info("Recording config changed during active session; applying after session ends")
            return False

        async with self._session_lock:
            return await self._restart_recorder_for_config_locked()

    async def _restart_recorder_for_config_locked(self) -> bool:
        old_recorder = self.recorder
        # Snapshot config before live-apply mutates recorder behavior.
        old_cfg = copy.deepcopy(self.cfg)

        new_recorder = create_recorder(self.cfg)
        self._wire_recorder(new_recorder)

        await old_recorder.stop()
        try:
            await new_recorder.start()
        except Exception:
            # Restore old config on the current recorder object before restart.
            for field in ("recording", "hotkeys", "output", "sharing", "discord"):
                setattr(self.cfg, field, getattr(old_cfg, field))

            # Try to restore the previous recorder so capture keeps running.
            try:
                await old_recorder.start()
                self._ready = True
                self._recorder_error = ""
            except Exception as restore_exc:
                log.error("Failed to restore previous recorder: %s", restore_exc)
                self._ready = False
                self._recorder_error = str(restore_exc)
            raise

        self.recorder = new_recorder
        self._recording_sig = self._recording_signature()
        self._pending_recording_apply = False
        # Covers recovering from a recorder that never came up at startup
        # (#156): changing settings is how the user gets out of that.
        self._ready = True
        self._recorder_error = ""
        return True

    # ── follow-the-pointer capture (#133) ─────────────────────────────────────
    # No capture backend can retarget a running replay buffer, so following the
    # pointer means restarting the recorder. Two agreeing samples in a row are
    # required so dragging the mouse across a screen edge does not restart
    # anything, and the restart is skipped while a clip or session is in flight.

    def _sync_follow_mouse_task(self) -> None:
        wanted = bool(self.cfg.recording.follow_mouse_display)
        running = self._follow_mouse_task is not None and not self._follow_mouse_task.done()
        if wanted and not running:
            self._follow_mouse_task = asyncio.create_task(self._follow_mouse_loop())
        elif not wanted and running:
            self._follow_mouse_task.cancel()
            self._follow_mouse_task = None
            self._display_override = None
            self.recorder.display_override = None

    async def _follow_mouse_loop(self) -> None:
        from .active_window import pointer_display

        pending: Optional[str] = None
        try:
            while True:
                await asyncio.sleep(FOLLOW_MOUSE_INTERVAL)
                if self._session_active or (self._clip_task and not self._clip_task.done()):
                    continue
                try:
                    current = await asyncio.to_thread(pointer_display)
                except Exception:
                    log.debug("Pointer display detection failed", exc_info=True)
                    continue
                if not current or current == self._display_override:
                    pending = None
                    continue
                if current != pending:
                    pending = current
                    continue
                pending = None
                log.info("Pointer moved to %s; retargeting capture", current)
                previous = self._display_override
                self._display_override = current
                async with self._config_apply_lock:
                    async with self._clip_lock:
                        try:
                            await self._restart_recorder_for_config()
                        except Exception as exc:
                            self._display_override = previous
                            log.warning("Could not retarget capture to %s: %s", current, exc)
        except asyncio.CancelledError:
            pass

    # ── game-window capture ─────────────────────────────────────────────────
    # Pins the replay buffer to a recognized game's window id (games.json),
    # instead of GSR's own `-w focused` which would jump to Discord/a browser
    # on alt-tab. A pin change means a recorder restart, so switching an
    # active pin (or releasing one) waits for two agreeing samples first,
    # to ride out the transient window id a game hands back during a
    # fullscreen/resolution change. Also releases the pin once the window is
    # confirmed gone, so quitting drops to full-display instead of GSR
    # crashing on a dead target.

    def _window_capture_unavailable(self) -> Optional[str]:
        """Why Game Capture cannot pin a window on this session, or None."""
        if self._window_capture_refused:
            return self._window_capture_refused
        return None if gsr_window_capture_supported() else "wayland"

    def _sync_window_capture_task(self) -> None:
        # Window pinning only exists on GSR (-w flag); segment backends would
        # just eat pointless recorder restarts for a pin that's never applied.
        wanted = bool(self.cfg.recording.window_capture) and isinstance(self.recorder, GSRRecorder)
        if wanted and self._window_capture_unavailable():
            log.info("Game Capture is on, but gpu-screen-recorder can only capture a "
                     "window in an X11 session. Recording the whole display")
            wanted = False
        running = self._window_capture_task is not None and not self._window_capture_task.done()
        if wanted and not running:
            self._window_capture_task = asyncio.create_task(self._window_capture_loop())
        elif not wanted and running:
            self._window_capture_task.cancel()
            self._window_capture_task = None
            self._window_capture_id = None
            self.recorder.window_override = None

    async def _window_capture_loop(self) -> None:
        from .active_window import get_active_window, get_window_geometry

        # Candidate window id for swapping an active pin, held for one tick
        # before it's acted on. None means no swap is pending.
        pending_switch: Optional[str] = None
        # Whether the last tick already saw the pinned window's geometry
        # lookup fail once. Only a second consecutive failure releases it.
        pending_release = False
        try:
            first = True
            while True:
                if first:
                    first = False
                else:
                    await asyncio.sleep(WINDOW_CAPTURE_INTERVAL)
                if self._session_active or (self._clip_task and not self._clip_task.done()):
                    continue
                try:
                    win = await asyncio.to_thread(get_active_window)
                except Exception:
                    log.debug("Active window detection failed", exc_info=True)
                    continue
                game = self._match_game(win) if win else None
                if game:
                    pending_release = False
                    # The id comes from the same lookup that matched the game,
                    # so the pin can only ever land on the window we matched.
                    # Asking X11 for focus again here would pin whatever the
                    # user tabbed to in between (Discord) under the game's name.
                    current = win.get("window_id")
                    if (not current or current == self._window_capture_id
                            or current in self._window_capture_failed):
                        pending_switch = None
                        continue
                    if self._window_capture_id and current != pending_switch:
                        # Already pinned to something else; confirm the new
                        # id sticks for a tick before paying for a restart.
                        pending_switch = current
                        continue
                    pending_switch = None
                    log.info("%s is focused; pinning capture to its window", game)
                    previous = self._window_capture_id
                    self._window_capture_id = current
                    async with self._config_apply_lock:
                        async with self._clip_lock:
                            try:
                                await self._restart_recorder_for_config()
                            except Exception as exc:
                                self._window_capture_id = previous
                                self._window_capture_failed.add(current)
                                log.warning("Could not pin capture to %s: %s", game, exc)
                                if GSR_WINDOW_CAPTURE_WAYLAND_ERROR.lower() in str(exc).lower():
                                    self._refuse_window_capture()
                                    return
                    continue

                # Focus is on something unrecognized (or nothing). That's
                # normal while the pinned game keeps running in the
                # background (Discord, a browser, etc). Only act if the
                # pinned window itself is actually gone, so we drop to
                # full-display capture before GSR notices and dies, instead
                # of after (the watchdog would otherwise catch a hard crash).
                pending_switch = None
                if not self._window_capture_id:
                    continue
                try:
                    still_open = await asyncio.to_thread(get_window_geometry, self._window_capture_id)
                except Exception:
                    log.debug("Pinned window liveness check failed", exc_info=True)
                    continue
                if still_open:
                    pending_release = False
                    continue
                if not pending_release:
                    # A window recreated by a fullscreen or resolution change
                    # can miss one lookup before the new id shows up as
                    # focused; confirm it's really gone before releasing.
                    pending_release = True
                    continue
                pending_release = False
                log.info("Pinned game window closed; releasing pin, capturing full display")
                previous = self._window_capture_id
                self._window_capture_id = None
                async with self._config_apply_lock:
                    async with self._clip_lock:
                        try:
                            await self._restart_recorder_for_config()
                        except Exception as exc:
                            self._window_capture_id = previous
                            log.warning("Could not release capture pin: %s", exc)
        except asyncio.CancelledError:
            pass

    def _install_gsr(self) -> bool:
        """Start installing the gpu-screen-recorder Flatpak, from the Vice
        Flatpak's setup card. False when it cannot start here."""
        if not in_vice_flatpak():
            return False
        if self._gsr_install_task and not self._gsr_install_task.done():
            return True
        self._gsr_install_task = asyncio.create_task(self._run_gsr_install())
        return True

    async def _run_gsr_install(self) -> None:
        async def progress(phase: str, percent: Optional[int]) -> None:
            if self.share:
                await self.share.broadcast(
                    {"type": "gsr_install", "phase": phase, "percent": percent})

        try:
            problem = await gsr_install.install_gsr_flatpak(progress)
        except Exception as exc:
            log.exception("Installing the gpu-screen-recorder Flatpak failed")
            problem = str(exc) or "the install failed"
        if problem is None:
            refresh_gsr_probes()
            try:
                async with self._config_apply_lock:
                    async with self._clip_lock:
                        await self._restart_recorder_for_config()
            except Exception as exc:
                log.warning("gpu-screen-recorder is installed, but recording did not start: %s", exc)
                problem = f"Installed, but recording did not start: {exc}"
        if self.share:
            await self.share.broadcast({
                "type": "gsr_install",
                "phase": "error" if problem else "done",
                "percent": None if problem else 100,
                "error": problem,
                "command": gsr_install.MANUAL_COMMAND if problem else None,
            })
        self._broadcast_status(recording=self._ready)
        if self.share:
            await self.share.broadcast({"type": "status", "gsr": {
                "source": gsr_source(), "flatpak": gsr_flatpak_installed()}})

    def _refuse_window_capture(self) -> None:
        """Stop pinning for the rest of this run. Retrying could only fail the
        same way, and each try empties the replay buffer (#261, #274)."""
        log.warning("gpu-screen-recorder refuses window capture on this session. "
                    "Game Capture is off until Vice restarts; recording the whole display")
        self._window_capture_refused = "wayland"
        self._window_capture_task = None
        if self.share:
            asyncio.create_task(self.share.broadcast(
                {"type": "status", "window_capture_unavailable": "wayland"}))

    async def _hotkeys_suppressed(self) -> bool:
        """Whether the focused app is one the user asked Vice to keep its hands
        off (#130). Only the keyboard path checks this, so the UI's clip button
        and the CLI stay live regardless."""
        matches = self.cfg.hotkeys.disable_while_focused
        if not matches:
            return False
        try:
            from .active_window import get_active_window
            win = await asyncio.to_thread(get_active_window)
        except Exception:
            log.debug("Focused-app check for hotkey suppression failed", exc_info=True)
            return False
        if not win:
            return False
        haystacks = ((win.get("process") or "").lower(), (win.get("class") or "").lower())
        for needle in matches:
            n = (needle or "").strip().lower()
            if n and any(n in h for h in haystacks):
                log.info("Hotkey ignored: %r is focused (matched %r)", haystacks[1] or haystacks[0], needle)
                return True
        return False

    def _bind_hotkeys(self) -> None:
        """(Re)bind runtime hotkeys from current config."""
        self.hotkeys.clear_bindings()
        for clip_key, duration in effective_clip_bindings(self.cfg):
            # Single tap → save clip (or add session highlight)
            async def _clip(duration=duration) -> None:
                if await self._hotkeys_suppressed():
                    return
                await self._handle_clip_hotkey(duration)

            async def _session_toggle() -> None:
                if await self._hotkeys_suppressed():
                    return
                await self._handle_session_toggle()

            self.hotkeys.on(clip_key, _clip)
            # Double tap → toggle session recording
            self.hotkeys.on_double(clip_key, _session_toggle)

        screenshot_key = normalize_combo((self.cfg.hotkeys.screenshot or "").strip())
        if screenshot_key:
            async def _screenshot() -> None:
                if await self._hotkeys_suppressed():
                    return
                await self._handle_screenshot_hotkey()

            self.hotkeys.on(screenshot_key, _screenshot)

        overlay_key = normalize_combo((self.cfg.hotkeys.key_overlay or "").strip())
        if overlay_key:
            async def _toggle_overlay() -> None:
                await self._toggle_key_overlay()

            self.hotkeys.on(overlay_key, _toggle_overlay)

    async def _apply_live_config(self) -> None:
        """Apply config changes and restart recorder when recording settings changed."""
        async with self._config_apply_lock:
            self._bind_hotkeys()
            # Before the restart check, so turning follow-the-pointer off drops
            # the override and the recorder goes back to the saved display.
            self._sync_follow_mouse_task()
            self._sync_window_capture_task()

            async with self._clip_lock:
                if self._recording_signature() != self._recording_sig:
                    await self._restart_recorder_for_config()
                    self._sync_window_capture_task()  # backend may have changed

            await self._sync_discord_presence_task()
            await self._sync_key_overlay()
            if self.voice:
                await self.voice.sync(self.cfg.voice, self.cfg.recording.microphone_source)

            await self._sync_auto_start_task()
            if self.share:
                await self.share.broadcast({
                    "type": "status",
                    "recording": self._ready,
                    "ready": self._ready,
                    "backend": self.recorder.name,
                    "session_active": self._session_active,
                    "clip_key": self.cfg.hotkeys.clip,
                    "hotkeys_available": self.hotkeys_available,
                    "recorder_error": self._recorder_error,
                    "cpu_fallback": bool(getattr(self.recorder, "cpu_fallback", False)),
                    "cpu_fallback_reason": getattr(self.recorder, "cpu_fallback_reason", None),
                    "codec_fallback": bool(getattr(self.recorder, "codec_fallback", False)),
                })

    # ── Discord Rich Presence ────────────────────────────────────────────
    def _discord_configured_client_id(self) -> str:
        from .discord_rpc import DEFAULT_CLIENT_ID
        return self.cfg.discord.client_id_override or DEFAULT_CLIENT_ID

    async def _stop_discord_presence_task(self) -> None:
        if self._discord_task and not self._discord_task.done():
            self._discord_task.cancel()
            try:
                await self._discord_task
            except (asyncio.CancelledError, Exception):
                pass
        self._discord_task = None
        await self._clear_discord_presence()

    async def _sync_discord_presence_task(self) -> None:
        desired_client_id = self._discord_configured_client_id()
        if not self.cfg.discord.enabled:
            await self._stop_discord_presence_task()
            return

        if (
            self._discord_task
            and not self._discord_task.done()
            and self._discord_client_id != desired_client_id
        ):
            await self._stop_discord_presence_task()

        if self._discord_task is None or self._discord_task.done():
            self._discord_task = asyncio.create_task(self._discord_presence_loop())

    def _discord_activity(self, game: str) -> dict:
        icon = None
        if self.cfg.discord.game_icons:
            from .game_icons import discord_icon_url
            icon = discord_icon_url(game)
        if icon:
            assets = {
                "large_image": icon,
                "large_text": game,
                "small_image": "vice_logo",
                "small_text": "Vice",
            }
        else:
            assets = {
                "large_image": "vice_logo",
                "large_text": "Vice, Linux clip recorder",
            }
        return {
            "details": f"Clipping {game} with Vice",
            "state": game,
            "timestamps": {"start": int(self._discord_started_at)},
            "assets": assets,
        }

    async def _discord_presence_loop(self) -> None:
        """Poll the active window every 5s. When a configured game is focused,
        push "Clipping <Game> with Vice" to Discord. Clear when no game is
        focused. Exits when discord.enabled flips off."""
        from .active_window import (
            detection_tools_status,
            get_active_window,
            supported_compositor,
            uses_x11_adapter,
        )
        from .discord_rpc import DiscordRPC
        cid = self._discord_configured_client_id()
        if not cid:
            log.info("Discord RPC enabled but no client_id is set; presence disabled.")
            return
        self._discord_client_id = cid
        self._discord_rpc = DiscordRPC(cid)
        backoff = 5.0
        if not supported_compositor() and not self._discord_no_window_adapter_logged:
            log.info(
                "Discord Rich Presence is enabled, but active-window detection "
                "is unavailable on this Wayland session (no XWayland/DISPLAY). "
                "RPC still connects; games launched via XWayland are detected "
                "when DISPLAY is set."
            )
            self._discord_no_window_adapter_logged = True
        if uses_x11_adapter():
            tools = detection_tools_status()
            if not (tools["xdotool"] and tools["xprop"]):
                log.warning(
                    "Game detection on this compositor needs xdotool and xprop "
                    "(wmctrl helps too), install them for Discord RPC and "
                    "game-tagged clips."
                )
        try:
            while True:
                if not self.cfg.discord.enabled:
                    await self._clear_discord_presence()
                    return
                connected_now = False
                if not self._discord_rpc.is_connected:
                    if not await self._discord_rpc.connect():
                        if not self._discord_no_socket_logged:
                            log.info("Discord Rich Presence is enabled, but no Discord IPC socket is reachable.")
                            self._discord_no_socket_logged = True
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, 60.0)
                        continue
                    backoff = 5.0
                    connected_now = True
                    self._discord_no_socket_logged = False
                try:
                    win = get_active_window()
                    matched = self._match_game(win) if win else None
                    if matched is not None:
                        self._remember_discord_game_process(win)
                    else:
                        matched = await self._discord_unfocused_game()
                    if matched is None:
                        self._discord_current_game = None
                        self._discord_current_pid = 0
                        self._discord_game_comm = ""
                        if connected_now or self._discord_last_activity is not None:
                            if await self._discord_rpc.set_activity(None):
                                self._discord_last_activity = None
                    else:
                        if matched != self._discord_current_game:
                            self._discord_current_game = matched
                            self._discord_started_at = time.time()
                        activity = self._discord_activity(matched)
                        if connected_now or activity != self._discord_last_activity:
                            if await self._discord_rpc.set_activity(activity):
                                self._discord_last_activity = activity
                except Exception as exc:
                    log.warning("Discord presence tick failed: %s", exc)
                await asyncio.sleep(5.0)
        except asyncio.CancelledError:
            await self._clear_discord_presence()
            raise

    def _remember_discord_game_process(self, win: Optional[dict]) -> None:
        """Snapshot the matched window's pid and comm so presence can outlive
        focus. The comm comparison guards against pid reuse."""
        from .active_window import _read_proc_comm
        pid = int((win or {}).get("pid") or 0)
        if pid <= 0:
            return
        comm = _read_proc_comm(pid)
        if comm:
            self._discord_current_pid = pid
            self._discord_game_comm = comm

    async def _discord_unfocused_game(self) -> Optional[str]:
        """The game to keep showing when no matched window is focused: the
        remembered one while its process lives (#112), else a visible-window
        scan for compositors that can't report focus reliably (#102)."""
        from .active_window import _read_proc_comm
        if not self.cfg.discord.persist_while_running:
            return None
        if self._discord_current_game and self._discord_current_pid > 0:
            comm = _read_proc_comm(self._discord_current_pid)
            if comm and comm == self._discord_game_comm:
                return self._discord_current_game
        self._discord_scan_tick += 1
        if self._discord_scan_tick % 3:
            return None
        hit = await asyncio.to_thread(self._scan_visible_for_game)
        if hit:
            matched, win = hit
            self._remember_discord_game_process(win)
            return matched
        return None

    def _scan_visible_for_game(self) -> Optional[tuple[str, dict]]:
        """First visible window matching the games list, with the window.

        Sync: the scan shells out to the compositor. KWin only partly mirrors
        focus into XWayland's EWMH properties, so on KDE Wayland asking for the
        focused window comes back empty and this is the only thing that works
        (#102, #152).
        """
        from .active_window import list_candidate_windows
        windows = list_candidate_windows()
        # Recorded for the clip detection log: no candidates at all means the
        # compositor gave us nothing, which is a different fault from a game
        # that simply is not on the list (#176).
        self._last_scan_candidates = len(windows)
        for win in windows:
            matched = self._match_game(win)
            if matched:
                return matched, win
        return None

    async def _clear_discord_presence(self) -> None:
        if self._discord_rpc is None:
            return
        try:
            if self._discord_last_activity is not None:
                await self._discord_rpc.set_activity(None)
            await self._discord_rpc.close()
        except Exception as exc:
            log.debug("Discord clear/close raised: %s", exc)
        finally:
            self._discord_rpc = None
            self._discord_client_id = None
            self._discord_current_game = None
            self._discord_last_activity = None
            self._discord_current_pid = 0
            self._discord_game_comm = ""

    def _detect_game(self) -> Optional[str]:
        """Recognized game label for existing replay/tagging callers."""
        target = self._detect_game_target()
        return target[0] if target else None

    def _detect_game_target(self, *, focused_only: bool = False) -> Optional[tuple[str, dict]]:
        """Recognized game and the matched window from the same detection.

        Sync (callers run it in a thread; window detection shells out to the
        compositor). Matches configured, bundled or Steam games, never
        arbitrary window titles.

        Shared by replays and sessions; callers own their captured identity.
        focused_only excludes background matches when binding a manual session.
        """
        game = None
        win = None
        adapter = "none"
        scanned = False
        candidates = 0
        try:
            from .active_window import adapter_name, get_active_window
            adapter = adapter_name()
            win = get_active_window()
            game = self._match_game(win) if win else None
            if game is None and not focused_only:
                # Focus detection is unreliable on KDE and GNOME under Wayland,
                # where it goes through XWayland. Discord presence has fallen
                # back to a visible-window scan since #102; without the same
                # fallback here, clips on those sessions were never tagged and
                # never landed in an auto playlist (#152).
                scanned = True
                hit = self._scan_visible_for_game()
                candidates = getattr(self, "_last_scan_candidates", 0)
                if hit:
                    game, win = hit
        except Exception:
            log.debug("Game detection for clip tagging failed", exc_info=True)
        # One line per detection so an unmatched game or a compositor miss is
        # diagnosable from vice.log. A focused window with no match means the
        # game is not on the curated list; adapter=none means the session never
        # gave Vice a way to look, which is a different problem (#176). Local
        # only, never leaves the machine, and no window title goes in it.
        log.info(
            "Clip game detection: adapter=%s process=%r class=%r matched=%r scanned=%s candidates=%d",
            adapter, (win or {}).get("process"), (win or {}).get("class"),
            game, scanned, candidates,
        )
        return (game, win) if game is not None and win is not None else None

    def _clip_game_tag(self) -> Optional[str]:
        """Capture replay identity even when filename tagging is disabled."""
        game = self._detect_game()
        self._last_clip_game = game
        if not getattr(self.cfg.output, "tag_clips_with_game", False):
            return None
        return game

    def _match_game(self, win: dict) -> Optional[str]:
        from .active_window import read_steam_app_id

        proc = (win.get("process") or "").lower()
        cls  = (win.get("class") or "").lower()
        haystacks = [proc, cls]
        # Source games share one launcher binary, so the process name alone
        # tags Half-Life 2 and Left 4 Dead 2 as Team Fortress 2 (#162). The
        # app id is exact; without it nothing changes.
        app_id = read_steam_app_id(win.get("pid"))
        if app_id:
            haystacks.append(f"steam_app_{app_id}")
        haystacks = tuple(haystacks)
        # User custom games first, explicit user intent beats the bundled list.
        custom = [(g.name, g.matches) for g in self.cfg.discord.custom_games]
        bundled = [(g["name"], g.get("matches")) for g in _DEFAULT_GAMES]
        matched = _best_game_match(custom, haystacks) or _best_game_match(bundled, haystacks)
        if matched:
            return matched
        # Not on either list. The lists stay the source of truth, since tags and
        # auto playlists are keyed on their names, so Steam's own name only
        # fills the gaps for games that aren't listed.
        if not app_id:
            from_class = re.match(r"steam_app_(\d+)$", cls)
            app_id = from_class.group(1) if from_class else None
        named = steam_game_name(app_id) if app_id else None
        return named or _truncated_process_match(custom, proc) or _truncated_process_match(bundled, proc)

    def _disk_stats(self) -> Optional[dict]:
        """Free space where clips land, for the Home readout.

        A recorder that quietly runs out of room is the failure this is here to
        make visible, so a drive that cannot be measured reports nothing rather
        than a zero that would read as full.
        """
        try:
            usage = shutil.disk_usage(resolve_path(self.cfg.output.directory))
        except OSError as exc:
            log.debug("Could not measure free space: %s", exc)
            return None
        return {"free": usage.free, "total": usage.total}

    # ── keyboard visualiser ──────────────────────────────────────────────────

    def _on_key_event(self, evdev_name: str, down: bool) -> None:
        if self.key_ring is None:
            return
        change = self.key_ring.record(evdev_name, down)
        if change is not None:
            self.key_overlay.key(*change)

    async def _sync_key_overlay(self) -> None:
        ov = self.cfg.overlay
        if ov.keys or ov.keys_in_clips:
            # Long enough to cover a clip cut from the end of the buffer.
            horizon = float(self.cfg.recording.buffer_duration) + 10 if ov.keys_in_clips else 0.0
            if self.key_ring is None:
                self.key_ring = KeyRing(horizon)
            self.key_ring.horizon = horizon
            self.key_ring.hide(ov.keys_hidden)
        else:
            self.key_ring = None
        place = {"position": ov.keys_position, "x": ov.keys_x, "y": ov.keys_y,
                 "scale": ov.keys_scale}
        await self.key_overlay.sync(ov.keys, place, self.cfg.sharing.embed_color, ov.keys_hidden,
                                    ov.keys_layout)

    async def _toggle_key_overlay(self) -> None:
        self.cfg.overlay.keys = not self.cfg.overlay.keys
        self._save_overlay_config()
        await self._sync_key_overlay()

    def _save_overlay_config(self) -> None:
        """Persist the overlay section only, on top of what is on disk, so a
        drag or the toggle key cannot write back stale settings."""
        try:
            on_disk = load_config()
            on_disk.overlay = copy.deepcopy(self.cfg.overlay)
            save_config(on_disk)
        except Exception:
            log.exception("Could not save the keyboard overlay settings")
        if self.share:
            asyncio.create_task(self.share.broadcast({"type": "config_changed"}))

    def _on_key_overlay_moved(self, x: int, y: int) -> None:
        ov = self.cfg.overlay
        ov.keys_position, ov.keys_x, ov.keys_y = "custom", x, y
        self._save_overlay_config()

    def _on_key_overlay_state(self) -> None:
        if self.share:
            asyncio.create_task(self.share.broadcast(
                {"type": "key_overlay", **self.key_overlay.state()}))

    async def _store_clip_keys(self, path: Path, end: float) -> None:
        """Keep the overlay's keys from the clip's own span beside it."""
        ring = self.key_ring
        if ring is None:
            return
        try:
            meta = await probe_media(path)
            duration = float(meta.get("duration") or 0)
            if duration <= 0:
                return
            intervals = ring.intervals(end - duration, end)
            await asyncio.to_thread(save_key_sidecar, path.stem, intervals)
        except Exception:
            log.exception("Could not keep the keys for %s", path.name)
            return
        if self.share:
            await self.share.refresh_clip(path.stem)

    def _get_status(self) -> dict:
        return {
            "ready":          self._ready,
            "recording":      self._ready,
            "recorder_error": self._recorder_error,
            "cpu_fallback":   bool(getattr(self.recorder, "cpu_fallback", False)),
            "cpu_fallback_reason":   getattr(self.recorder, "cpu_fallback_reason", None),
            "codec_fallback": bool(getattr(self.recorder, "codec_fallback", False)),
            "backend":          self.recorder.name,
            "clips":            self._clips_in_library(),
            "session_active":   self._session_active,
            "session_auto_start_error": getattr(self, "_auto_start_error", None),
            "clip_key":         self.cfg.hotkeys.clip,
            "hotkeys_available": self.hotkeys_available,
            "disk":             self._disk_stats(),
            # None unless a newer release is known, so a UI opened long after
            # the check still learns about it.
            "update":           self._update,
            "key_overlay":      self.key_overlay.state() if hasattr(self, "key_overlay") else None,
            "voice":            self.voice.state() if hasattr(self, "voice") and self.voice else None,
            # Which gpu-screen-recorder runs, and whether the Flatpak one is
            # there to choose in Settings.
            "gsr":              {"source": gsr_source(), "flatpak": gsr_flatpak_installed()},
            # Game Capture pins a window, which GSR only does on X11.
            "window_capture_unavailable": self._window_capture_unavailable(),
            "platform":         detect_platform(_install_method),
        }

    # ── update check ─────────────────────────────────────────────────────────

    def _update_install_hint(self) -> dict:
        """How this machine should update, so the notice can say it exactly."""
        if in_vice_flatpak():
            return {"method": "flatpak", "command": f"flatpak update {VICE_FLATPAK_ID}"}
        if _installed_via_aur():
            return {"method": "aur", "command": "yay -Syu vice-clipper"}
        if _installed_via_nix():
            return {"method": "nix", "command": "nix flake update vice && sudo nixos-rebuild switch"}
        if _using_install_script_venv():
            return {"method": "script", "command": "cd Vice && git pull && ./install.sh"}
        return {"method": "unknown", "command": ""}

    async def run_update_check(self, force: bool = False) -> Optional[dict]:
        """Ask GitHub whether there is a newer release. Silent about
        everything: no network, a rate limit or a malformed reply all just
        leave the last known answer in place."""
        cache = updates.UpdateCache()
        stored = cache.load()
        if force or cache.stale():
            fetched = await asyncio.to_thread(updates.fetch_latest, stored.get("etag"))
            if fetched:
                stored = dict(fetched, checked_at=time.time())
            else:
                stored["checked_at"] = time.time()
            cache.save(stored)

        found = updates.available(stored)
        if found:
            found["install"] = await asyncio.to_thread(self._update_install_hint)
        self._update = found
        if found and self.share:
            await self.share.broadcast(dict(found, type="update_available"))
        return found

    async def _update_check_soon(self) -> None:
        # Well clear of startup: the window, the recorder and the first clip
        # scan all matter more than this.
        await asyncio.sleep(20)
        try:
            await self.run_update_check()
        except Exception as exc:
            log.debug("Update check failed: %s", exc)

    async def _shutdown(self, server) -> None:
        click.echo("\n[Vice] Shutting down…")
        self._init_auto_start()
        self._auto_start_shutdown = True
        await self._stop_auto_start_task()
        try:
            await self.key_overlay.stop()
        except Exception:
            log.exception("Stopping the keyboard overlay failed")
        if hasattr(self, "voice") and self.voice:
            try:
                await self.voice.stop()
            except Exception:
                log.exception("Stopping voice clipping failed")
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._upgrade_task and not self._upgrade_task.done():
            self._upgrade_task.cancel()
            try:
                await self._upgrade_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._update_task and not self._update_task.done():
            self._update_task.cancel()
            try:
                await self._update_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._follow_mouse_task and not self._follow_mouse_task.done():
            self._follow_mouse_task.cancel()
            try:
                await self._follow_mouse_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._window_capture_task and not self._window_capture_task.done():
            self._window_capture_task.cancel()
            try:
                await self._window_capture_task
            except (asyncio.CancelledError, Exception):
                pass
        if self.share:
            try:
                await self.share.broadcast({"type": "status", "recording": False, "ready": False, "backend": ""})
            except Exception as exc:
                log.warning("Failed to broadcast shutdown status: %s", exc)

        if self._discord_task and not self._discord_task.done():
            self._discord_task.cancel()
            try:
                await self._discord_task
            except (asyncio.CancelledError, Exception):
                pass
        await self._clear_discord_presence()

        server.close()

        # There is no reason to restart capture for deferred settings on exit.
        self._pending_recording_apply = False
        try:
            await self._change_session(False, stop_reason="shutdown")
        except Exception:
            log.exception("Session stop failed during shutdown")

        try:
            await self.recorder.stop()
        except Exception as exc:
            log.error("Recorder stop failed during shutdown: %s", exc)

        try:
            await self.hotkeys.stop()
        except Exception as exc:
            log.warning("Hotkey stop failed during shutdown: %s", exc)

        if self.share:
            try:
                await self.share.stop()
            except Exception as exc:
                log.warning("Share server stop failed during shutdown: %s", exc)

        for p in (PID_FILE, SOCKET_FILE):
            try:
                if p.exists():
                    p.unlink()
            except OSError as exc:
                log.warning("Failed to remove %s during shutdown: %s", p, exc)

        click.echo("[Vice] Stopped.")

    async def _handle_clip_hotkey(self, duration: Optional[int] = None) -> None:
        if self._session_active:
            # During a session, single tap = add a highlight at current timestamp
            elapsed = self.recorder.session_elapsed()
            label   = f"Highlight {len(self._session_highlights) + 1}" if self._session_highlights else "Highlight"
            color   = "#f59e0b"
            entry   = {"time": round(elapsed, 3), "label": label, "color": color}
            self._session_highlights.append(entry)
            click.echo(f"[Vice] Session highlight at {elapsed:.1f}s", err=True)
            audio.play_highlight(self.cfg.notifications.sound_volume,
                                 self.cfg.notifications.highlight_sound,
                                 self.cfg.notifications.sound_pack)
            if self.share:
                asyncio.create_task(
                    self.share.broadcast({
                        "type": "session_highlight",
                        "time": entry["time"],
                        "label": entry["label"],
                        "color": entry["color"],
                    })
                )
        else:
            if self._clip_task and not self._clip_task.done():
                log.info("Clip save already in progress; ignoring new trigger")
                return
            self._clip_task = asyncio.create_task(self._save_clip(duration))
            self._clip_task.add_done_callback(self._clip_task_done)

    async def _save_clip(self, duration: Optional[int] = None) -> None:
        async with self._clip_lock:
            click.echo("[Vice] Clip triggered!", err=True)
            if self.share:
                await self.share.broadcast({"type": "clip_saving"})
            # The sound stays here rather than moving to the success path:
            # flushing a long buffer takes seconds and immediate feedback is
            # the point of a clipper. Failure gets its own tone instead, so
            # the confirmation is never a lie (#154).
            audio.play_clip(self.cfg.notifications.sound_volume,
                            self.cfg.notifications.clip_sound,
                            self.cfg.notifications.sound_pack)
            saved = await self.recorder.save_clip(duration)
            if saved is None:
                audio.play_clip_failed(self.cfg.notifications.sound_volume,
                                       self.cfg.notifications.clip_failed_sound,
                                       self.cfg.notifications.sound_pack)
                if self.share:
                    await self.share.broadcast({
                        "type": "clip_error",
                        "error": self._clip_error_text(),
                    })

    async def _handle_screenshot_hotkey(self) -> None:
        """Save a still of the screen and put it on the clipboard (#171).

        Deliberately independent of the recorder: gpu-screen-recorder takes the
        picture in its own short-lived process, so pressing this never disturbs
        the replay buffer and works whichever backend is recording.
        """
        if self._screenshot_task and not self._screenshot_task.done():
            log.info("Screenshot already in progress; ignoring new trigger")
            return
        self._screenshot_task = asyncio.create_task(self._take_screenshot())
        self._screenshot_task.add_done_callback(self._screenshot_task_done)

    async def _take_screenshot(self) -> None:
        if not self.share:
            log.warning("Screenshot requested before the share server was up")
            return
        # Same detection the clip path uses, so a screenshot files itself into
        # the same auto playlist a clip taken at that moment would.
        game = await asyncio.to_thread(self._clip_game_tag)
        tag = filename_tag(game) if self.cfg.output.tag_clips_with_game else None
        out = self.share.next_image_path(tag)
        try:
            await capture_screenshot(out, self.cfg.recording, self._display_override)
        except RuntimeError as exc:
            click.echo(f"[Vice] Screenshot failed: {exc}", err=True)
            audio.play_clip_failed(self.cfg.notifications.sound_volume,
                                   self.cfg.notifications.clip_failed_sound,
                                   self.cfg.notifications.sound_pack)
            await self.share.broadcast({"type": "image_error", "error": str(exc)})
            return

        click.echo(f"[Vice] Screenshot saved: {out.name}", err=True)
        audio.play_screenshot(self.cfg.notifications.sound_volume,
                              self.cfg.notifications.screenshot_sound,
                              self.cfg.notifications.sound_pack)
        self.share.add_image(out, game=self._last_clip_game)
        copied, copy_error = await copy_image_to_clipboard(out)
        if not copied:
            # Saved but not copied is worth saying out loud: the user is about
            # to paste and would otherwise get whatever was there before.
            log.warning("Screenshot %s could not be copied: %s", out.name, copy_error)
            await self.share.broadcast({"type": "image_copy_failed", "error": copy_error})

    def _screenshot_task_done(self, task: asyncio.Task) -> None:
        if self._screenshot_task is task:
            self._screenshot_task = None
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            log.exception("Screenshot task failed")

    def _clip_error_text(self) -> str:
        """What to show the user when a clip did not save."""
        reason = getattr(self.recorder, "last_clip_error", "") or ""
        return reason or "Clip save failed. Check vice.log for details."

    def _clip_task_done(self, task: asyncio.Task) -> None:
        if self._clip_task is task:
            self._clip_task = None
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            log.exception("Clip save task failed")
            if self.share:
                asyncio.create_task(self.share.broadcast({
                    "type": "clip_error",
                    "error": self._clip_error_text(),
                }))

    # Automatic starts share the session lock, but never own session stops.
    def _init_auto_start(self) -> None:
        if hasattr(self, "_auto_start_task"):
            return
        self._auto_start_task: Optional[asyncio.Task] = None
        self._auto_start_wake = asyncio.Event()
        self._auto_start_shutdown = False
        self._auto_start_error: Optional[str] = None
        self._auto_start_storage_failed = False
        self._auto_start_storage_failure_epoch = 0
        self._auto_start_io_lock = asyncio.Lock()
        self._auto_start_dirty = False
        self._auto_start_generation = 0
        self._auto_start_store: Optional[SuppressionStore] = None
        self._auto_start_suppressed: set[ProcessIdentity] = set()
        self._auto_start_attempts: dict[ProcessIdentity, tuple[int, float]] = {}
        self._auto_start_pending: Optional[ProcessIdentity] = None

    async def _set_auto_start_error(self, error: Optional[str]) -> None:
        if error == "state":
            self._auto_start_storage_failed = True
            self._auto_start_storage_failure_epoch += 1
        if self._auto_start_storage_failed:
            error = "state"
        if self._auto_start_error == error:
            return
        self._auto_start_error = error
        if error:
            log.warning("Automatic session starts paused: %s", error)
        if self.share:
            await self.share.broadcast({"type": "status", "session_auto_start_error": error})

    async def _refresh_auto_start_store_locked(self) -> bool:
        """Called with the persistence lock; never acquires the session lock."""
        observed = self._auto_start_storage_failure_epoch
        try:
            if self._auto_start_store is None:
                boot_id = await asyncio.to_thread(read_host_boot_id)
                store = SuppressionStore(boot_id)
                try:
                    saved, version = await asyncio.to_thread(store.load_snapshot)
                except MalformedAutoStartState:
                    log.exception('Malformed automatic-session suppression state')
                    try:
                        backup = await asyncio.to_thread(store.backup_corrupt)
                        log.warning('Preserved malformed suppression state at %s', backup)
                    except (AutoStartStateError, OSError):
                        log.exception('Could not preserve malformed suppression state; attempting replacement')
                    saved, version = set(), 1
                    self._auto_start_dirty = True
                changed = bool(saved - self._auto_start_suppressed)
                self._auto_start_suppressed.update(saved)
                if changed:
                    self._auto_start_generation += 1
                    self._auto_start_pending = None
                self._auto_start_store = store
                if version == 2:
                    self._auto_start_dirty = True
            if self._auto_start_dirty:
                await asyncio.to_thread(
                    self._auto_start_store.save, self._auto_start_suppressed,
                )
                self._auto_start_dirty = False
        except (AutoStartStateError, OSError):
            log.exception('Cannot load or save automatic session suppression state')
            await self._set_auto_start_error('state')
            return False
        if observed != self._auto_start_storage_failure_epoch:
            # Only a fresh successful write may release a newer storage failure.
            self._auto_start_dirty = True
            return False
        self._auto_start_storage_failed = False
        if self._auto_start_error == 'state':
            await self._set_auto_start_error(None)
        return True

    async def _ensure_auto_start_store(self) -> bool:
        async with self._auto_start_io_lock:
            return await self._refresh_auto_start_store_locked()

    async def _save_auto_start_suppression(
        self, identities: set[ProcessIdentity], *, merge: bool = False,
    ) -> bool:
        async with self._auto_start_io_lock:
            # Manual stops can wait behind an initial load. Merge under the
            # same lock so newly loaded identities survive.
            if merge:
                self._auto_start_suppressed.update(identities)
            else:
                self._auto_start_suppressed = set(identities)
            self._auto_start_dirty = True
            self._auto_start_generation += 1
            self._auto_start_pending = None
            return await self._refresh_auto_start_store_locked()

    def _scan_auto_start_targets(
        self, *, require_identities: bool = False,
    ) -> Optional[tuple[list[AutoStartTarget], Optional[ProcessIdentity]]]:
        scan = scan_game_windows()
        if scan is None:
            return None
        targets = {}
        focused = None
        for win in scan.windows:
            game = self._match_game(win)
            if not game:
                continue
            identity = bind_game_process(win)
            if identity is None:
                # A partial manual-stop scan cannot identify every game.
                if require_identities:
                    return None
                continue
            targets.setdefault(identity, AutoStartTarget(game, win, identity))
            if win.get("window_id") == scan.focused_window_id:
                focused = identity
        return list(targets.values()), focused

    async def _suppress_manually_stopped_session(self) -> None:
        if not (self.cfg.recording.session_start_on_game_detect or self._auto_start_store):
            return
        # Capture the identity before any storage I/O can fail or suspend us.
        identity = getattr(self, "_session_process_identity", None)
        if identity is not None:
            stopped = {identity}
        else:
            try:
                scan = await asyncio.to_thread(self._scan_auto_start_targets, require_identities=True)
            except Exception:
                scan = None
                log.exception("Cannot identify games during manual session stop")
            if scan is None:
                # Invalidate an in-flight observation, but remember no stop intent.
                self._auto_start_generation += 1
                self._auto_start_pending = None
                await self._set_auto_start_error('detection')
                return
            stopped = {target.identity for target in scan[0]}
        await self._save_auto_start_suppression(stopped, merge=True)

    async def _sync_auto_start_task(self) -> None:
        self._init_auto_start()
        if not self.cfg.recording.session_start_on_game_detect or self._auto_start_shutdown:
            await self._stop_auto_start_task()
            if not self._auto_start_shutdown:
                # Keep unsaved identities for retry, while releasing the setting's
                # visible error. Re-enabling still requires successful persistence.
                self._auto_start_dirty |= self._auto_start_storage_failed
                self._auto_start_storage_failed = False
                await self._set_auto_start_error(None)
            return
        if self._auto_start_task is None or self._auto_start_task.done():
            self._auto_start_wake.clear()
            self._auto_start_pending = None
            self._auto_start_task = asyncio.create_task(self._auto_start_loop())

    async def _stop_auto_start_task(self) -> None:
        task = self._auto_start_task
        if task is not None and task is not asyncio.current_task():
            self._auto_start_wake.set()
            # Do not cancel a start which is already creating a capture child.
            await task
            self._auto_start_task = None
        self._auto_start_pending = None

    async def _auto_start_loop(self) -> None:
        try:
            while (self.cfg.recording.session_start_on_game_detect
                   and not self._auto_start_shutdown):
                try:
                    await self._auto_start_tick()
                except Exception:
                    self._auto_start_pending = None
                    log.exception("Automatic game detection failed")
                    await self._set_auto_start_error("detection")
                try:
                    await asyncio.wait_for(self._auto_start_wake.wait(), SESSION_AUTO_START_INTERVAL)
                    break
                except asyncio.TimeoutError:
                    pass
        finally:
            self._auto_start_pending = None

    async def _auto_start_tick(self) -> None:
        if not self.cfg.recording.session_start_on_game_detect or self._auto_start_shutdown:
            self._auto_start_pending = None
            return
        if not await self._ensure_auto_start_store():
            self._auto_start_pending = None
            return
        # A disappearing window is not process exit. Only the process probe
        # may release suppression or attempt limits, including across scans.
        probe_generation = self._auto_start_generation
        tracked = self._auto_start_suppressed | self._auto_start_attempts.keys()
        expired = set()
        for identity in tracked:
            if await asyncio.to_thread(probe_game_process, identity) == "exited":
                expired.add(identity)
        if expired:
            async with self._session_lock:
                if probe_generation != self._auto_start_generation:
                    self._auto_start_pending = None
                    return
                for identity in expired:
                    self._auto_start_attempts.pop(identity, None)
                remaining = self._auto_start_suppressed - expired
                if remaining != self._auto_start_suppressed:
                    if not await self._save_auto_start_suppression(
                        remaining,
                    ):
                        self._auto_start_pending = None
                        return
        if self._session_active or not self._ready:
            self._auto_start_pending = None
            return
        if self._auto_start_storage_failed or self._auto_start_dirty:
            self._auto_start_pending = None
            return
        if not await asyncio.to_thread(auto_start_scan_supported):
            self._auto_start_pending = None
            await self._set_auto_start_error("unsupported")
            return
        scan_generation = self._auto_start_generation
        scan = await asyncio.to_thread(self._scan_auto_start_targets)
        async with self._session_lock:
            if (scan_generation != self._auto_start_generation
                    or not self.cfg.recording.session_start_on_game_detect
                    or self._auto_start_shutdown or self._session_active or not self._ready):
                self._auto_start_pending = None
                return
            if self._auto_start_storage_failed or self._auto_start_dirty:
                self._auto_start_pending = None
                return
            if scan is None:
                self._auto_start_pending = None
                await self._set_auto_start_error('detection')
                return
            targets, focused = scan
            await self._set_auto_start_error(None)
            selected = targets[0] if len(targets) == 1 else next(
                (target for target in targets if target.identity == focused), None)
            identity = selected.identity if selected else None
            previous = self._auto_start_pending
            self._auto_start_pending = identity
        if selected is not None and identity == previous:
            await self._change_session(True, automatic_target=selected)

    def _automatic_start_allowed(self, target: AutoStartTarget) -> bool:
        attempts, retry_at = self._auto_start_attempts.get(target.identity, (0, 0.0))
        return (self.cfg.recording.session_start_on_game_detect
                and not self._auto_start_shutdown and self._ready
                and not self._session_active and self._auto_start_error is None
                and self._auto_start_store is not None
                and not self._auto_start_storage_failed
                and not self._auto_start_dirty
                and target.identity not in self._auto_start_suppressed
                and attempts < 3 and time.monotonic() >= retry_at)

    async def _attempt_automatic_start_locked(self, target: AutoStartTarget) -> None:
        attempts, _ = self._auto_start_attempts.get(target.identity, (0, 0.0))
        try:
            await self._start_session_locked(target)
        except Exception:
            log.exception("Automatic session recording failed to start")
        if self._session_active:
            # One successful automatic session per instance; never loop if its
            # capture child subsequently exits unexpectedly.
            self._auto_start_attempts[target.identity] = (3, float("inf"))
            return
        attempts += 1
        delay = 5.0 if attempts == 1 else 15.0
        self._auto_start_attempts[target.identity] = (attempts, time.monotonic() + delay)
        if attempts == 3:
            log.error("Automatic session start failed three times; waiting for game relaunch")
            if self.share:
                await self.share.broadcast({"type": "session_auto_start_failed"})

    async def _handle_session_toggle(self) -> None:
        await self._change_session(None)

    async def _start_session(self) -> None:
        await self._change_session(True)

    async def _stop_session(self) -> None:
        await self._change_session(False)

    async def _change_session(
        self, start: Optional[bool], *, expected_token: Optional[object] = None,
        automatic_target: Optional[AutoStartTarget] = None, stop_reason: str = "manual",
    ) -> None:
        # Serialize start/stop through finalization: a new session must not
        # replace the identity or highlights of one still being saved.
        self._init_auto_start()
        async with self._session_lock:
            if automatic_target is not None:
                if not self._automatic_start_allowed(automatic_target):
                    return
                live = await asyncio.to_thread(probe_game_process, automatic_target.identity)
                if live != "running" or not self._automatic_start_allowed(automatic_target):
                    return
            if expected_token is not None and expected_token is not self._session_token:
                return
            if start is None:
                start = not self._session_active
            try:
                if start:
                    if not self._session_active:
                        if automatic_target is None:
                            await self._start_session_locked()
                        else:
                            await self._attempt_automatic_start_locked(automatic_target)
                elif self._session_active:
                    if stop_reason == "manual":
                        await self._suppress_manually_stopped_session()
                    await self._stop_session_locked()
            finally:
                if not self._session_active:
                    self._session_token = None
                    self._session_process_identity = None
                    await self._cancel_session_monitor()
                    self._session_game = None
                    self._session_path = None
                    self._session_highlights = []

        # Live apply takes the config/clip locks before the session lock.
        if (not start and self._pending_recording_apply
                and self._recording_signature() != self._recording_sig):
            try:
                await self._apply_live_config()
            except Exception as exc:
                log.error("Deferred recording config apply failed: %s", exc)

    async def _start_session_locked(self, target: Optional[AutoStartTarget] = None) -> None:
        click.echo("[Vice] Starting session recording…", err=True)
        self._session_game = None
        self._session_highlights = []
        auto_stop_enabled = self.cfg.recording.session_stop_on_game_exit
        identity = None
        try:
            if target is not None:
                self._session_game = target.game
                identity = target.identity
            elif auto_stop_enabled or self.cfg.recording.session_start_on_game_detect:
                detected = await asyncio.to_thread(self._detect_game_target, focused_only=True)
                if detected:
                    self._session_game, win = detected
                    identity = await asyncio.to_thread(bind_game_process, win)
                else:
                    # Background detection remains useful for tags, not binding.
                    self._session_game = await asyncio.to_thread(self._detect_game)
            else:
                self._session_game = await asyncio.to_thread(self._detect_game)
        except Exception:
            if auto_stop_enabled:
                log.exception("Session game detection failed; automatic stopping unavailable")
            else:
                log.exception("Session game detection failed; recording without a game tag")
        tag = self._session_game if self.cfg.output.tag_clips_with_game else None
        path = await self.recorder.start_session(tag=tag)
        if path is None:
            click.echo("[Vice] Session recording failed to start", err=True)
            return
        self._session_active = True
        self._session_token = object()
        self._session_process_identity = identity
        self._session_path   = path
        auto_stop = "off" if not auto_stop_enabled else ("armed" if identity else "unavailable")
        audio.play_session_start(self.cfg.notifications.sound_volume,
                                 self.cfg.notifications.session_start_sound,
                                 self.cfg.notifications.sound_pack)
        click.echo(f"[Vice] Session recording started → {path}", err=True)
        if auto_stop == "armed":
            message = "Session will stop five seconds after the original game exits."
        elif auto_stop == "unavailable":
            message = "Automatic stopping is unavailable for this session. Stop it manually."
        else:
            message = None
        if message is not None:
            log.info(message)
            click.echo(f"[Vice] {message}", err=True)
        if self.share:
            asyncio.create_task(
                self.share.broadcast({
                    "type": "session_start",
                    "path": str(path),
                    "auto_stop": auto_stop,
                    "origin": "automatic" if target is not None else "manual",
                })
            )

        if auto_stop_enabled and identity is not None:
            self._session_monitor_task = asyncio.create_task(
                self._monitor_session_game(self._session_token, identity)
            )

    async def _cancel_session_monitor(self) -> None:
        task = getattr(self, "_session_monitor_task", None)
        self._session_monitor_task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _monitor_session_game(self, token: object, identity: ProcessIdentity) -> None:
        exited_since = None
        try:
            while self._session_active and self._session_token is token:
                await asyncio.sleep(SESSION_GAME_POLL_INTERVAL)
                if self.recorder.session_capture_exited():
                    reason = "Session capture process exited"
                    break
                try:
                    state = await asyncio.to_thread(probe_game_process, identity)
                except Exception:
                    log.exception("Session game process probe failed")
                    state = "unknown"
                now = time.monotonic()
                if state != "exited":
                    exited_since = None
                    continue
                if exited_since is None:
                    exited_since = now
                if now - exited_since >= SESSION_GAME_EXIT_DELAY:
                    reason = "Original session game exited"
                    break
            else:
                return
            log.info("%s; finalizing session", reason)
            await self._change_session(False, expected_token=token, stop_reason="automatic")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Automatic session stop failed")
        finally:
            if getattr(self, "_session_monitor_task", None) is asyncio.current_task():
                self._session_monitor_task = None

    async def _stop_session_locked(self) -> None:
        click.echo("[Vice] Stopping session recording…", err=True)
        self._session_active = False
        game = self._session_game
        path = await self.recorder.stop_session()
        self._session_path = None

        audio.play_session_end(self.cfg.notifications.sound_volume,
                               self.cfg.notifications.session_end_sound,
                               self.cfg.notifications.sound_pack)
        if path and self.share:
            slug = path.stem
            url  = self.share.add_clip(path, game=game, session=True)
            click.echo(f"[Vice] Session clip saved: {path}", err=True)
            click.echo(f"[Vice] Share URL: {url}", err=True)
            # Persist the highlights that were collected during the session
            if self._session_highlights:
                from .share import HIGHLIGHTS_DIR, _save_highlights
                HIGHLIGHTS_DIR.mkdir(parents=True, exist_ok=True)
                # Assign IDs
                hl_with_ids = [
                    {**h, "id": str(i + 1)}
                    for i, h in enumerate(self._session_highlights)
                ]
                _save_highlights(slug, hl_with_ids)
                click.echo(
                    f"[Vice] {len(hl_with_ids)} highlight(s) saved for {slug}", err=True
                )
            self._session_highlights = []

        if self.share:
            asyncio.create_task(
                self.share.broadcast({
                    "type": "session_stop",
                })
            )


    def _ipc_clip_duration(self, arg: str) -> tuple[Optional[int], str]:
        """The duration asked for by `vice clip <seconds>` (#285), or a reason
        to refuse it. No argument means the clip key's own duration. A request
        longer than the buffer gets the whole buffer, which is all there is."""
        if not arg:
            return None, ""
        if not arg.isdigit():
            return None, f"{arg!r} is not a whole number of seconds"
        seconds = int(arg)
        if seconds < CLIP_DURATION_MIN:
            return None, f"a clip is at least {CLIP_DURATION_MIN} seconds"
        buffer = int(self.cfg.recording.buffer_duration)
        if seconds > buffer:
            log.info("Clip of %d s asked for, the buffer holds %d s; saving %d s",
                     seconds, buffer, buffer)
            seconds = buffer
        return seconds, ""

    async def _handle_ipc(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=5)
            cmd = raw.decode().strip()
            if cmd == "clip" or cmd.startswith("clip "):
                duration, problem = self._ipc_clip_duration(cmd[len("clip"):].strip())
                if problem:
                    writer.write(f"error: {problem}\n".encode())
                else:
                    asyncio.create_task(self._handle_clip_hotkey(duration))
                    writer.write(b"ok\n" if duration is None else f"ok {duration}\n".encode())
            elif cmd == "stop":
                try:
                    writer.write(b"ok\n")
                    await writer.drain()
                finally:
                    # A client closing early must not discard an accepted stop.
                    os.kill(os.getpid(), signal.SIGTERM)
                return
            elif cmd == "status":
                writer.write(json.dumps({
                    "running":        True,
                    "ready":          self._ready,
                    "version":        __version__,
                    "backend":        self.recorder.name,
                    "clips":          self._clips_in_library(),
                    "output":         self.cfg.output.directory,
                    "local_url":      self.share.local_base_url() if self.share else None,
                    "public_url":     self.share.public_base_url() if self.share else None,
                    "share_url":      self.share.public_base_url() if self.share else None,
                    "session_active":  self._session_active,
                    "clip_key":        self.cfg.hotkeys.clip,
                    "hotkeys_available": self.hotkeys_available,
                }).encode() + b"\n")
            elif cmd == "url":
                url = self.share.local_base_url() if self.share else ""
                writer.write((url or "").encode() + b"\n")
            else:
                writer.write(b"unknown command\n")
            await writer.drain()
        except Exception as exc:
            log.debug("IPC error: %s", exc)
        finally:
            writer.close()


# ──────────────────────────────────────────────────────────────────────────────
# IPC client
# ──────────────────────────────────────────────────────────────────────────────

async def _ipc(command: str, timeout: float = 5.0) -> Optional[str]:
    if not SOCKET_FILE.exists():
        return None
    writer = None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(SOCKET_FILE)), timeout=timeout,
        )
        writer.write(command.encode() + b"\n")
        await asyncio.wait_for(writer.drain(), timeout=timeout)
        response = await asyncio.wait_for(reader.readline(), timeout=timeout)
        return response.decode().strip()
    except Exception as exc:
        log.debug("IPC failed: %s", exc)
        return None
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError as exc:
                log.debug("Closing the IPC connection failed: %s", exc)


def _vice_command_path() -> Optional[Path]:
    exe = shutil.which("vice")
    if not exe:
        return None
    try:
        return Path(exe).resolve()
    except OSError:
        return Path(exe)


def _installed_via_aur() -> bool:
    pacman = shutil.which("pacman")
    vice_path = _vice_command_path()
    if not pacman or not vice_path:
        return False

    try:
        query = subprocess.run(
            [pacman, "-Q", "vice-clipper"],
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        log.debug("Could not ask pacman about vice-clipper: %s", exc)
        return False
    if query.returncode != 0:
        return False

    owner = subprocess.run(
        [pacman, "-Qo", str(vice_path)],
        capture_output=True,
        text=True,
    )
    if owner.returncode != 0:
        return False
    return "vice-clipper" in owner.stdout


def _installed_via_nix() -> bool:
    vice_path = _vice_command_path()
    return vice_path is not None and str(vice_path).startswith("/nix/store/")


def _install_method() -> str:
    """aur, nix, script or unknown. The Flatpak is told apart before this."""
    if _installed_via_aur():
        return "aur"
    if _installed_via_nix():
        return "nix"
    if _using_install_script_venv():
        return "script"
    return "unknown"


def _using_install_script_venv() -> bool:
    for name in ("vice", "vice-app"):
        cmd = USER_BIN_DIR / name
        if not cmd.exists():
            continue
        try:
            resolved = cmd.resolve()
        except OSError:
            continue
        if INSTALL_VENV_DIR == resolved or INSTALL_VENV_DIR in resolved.parents:
            return True
    return INSTALL_VENV_DIR.exists()


def _remove_local_install_artifacts() -> list[Path]:
    removed: list[Path] = []
    for path in (
        USER_BIN_DIR / "vice",
        USER_BIN_DIR / "vice-app",
        USER_DESKTOP_FILE,
        USER_ICON_FILE,
    ):
        if not path.exists() and not path.is_symlink():
            continue
        path.unlink(missing_ok=True)
        removed.append(path)
    return removed


def _remove_legacy_user_site_artifacts() -> list[Path]:
    removed: list[Path] = []
    user_lib = actual_home_dir() / ".local" / "lib"
    if not user_lib.exists():
        return removed

    for pattern in (
        "python*/site-packages/vice",
        "python*/site-packages/vice-*.dist-info",
        "python*/site-packages/vice.egg-info",
    ):
        for path in sorted(user_lib.glob(pattern)):
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
            removed.append(path)
    return removed


def _refresh_desktop_caches() -> None:
    commands = [
        ["update-desktop-database", str(USER_DESKTOP_FILE.parent)],
        ["gtk-update-icon-cache", "-f", "-t", str(USER_ICON_FILE.parents[2])],
    ]
    for cmd in commands:
        exe = shutil.which(cmd[0])
        if not exe:
            continue
        subprocess.run([exe, *cmd[1:]], capture_output=True)


def _setup_daemon_logging(debug: bool) -> None:
    DAEMON_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [logging.FileHandler(DAEMON_LOG_FILE)]
    if sys.stderr.isatty():
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
        force=True,
    )


def _installed_service_file() -> Optional[Path]:
    """Where vice.service actually is, in the order systemd resolves it.

    Only the user's own directory used to be checked, so a package install
    that put the unit in /usr/lib reported "(not installed)" while the service
    was sitting there enabled (#139).
    """
    candidates = (
        actual_home_dir() / ".config" / "systemd" / "user" / "vice.service",
        Path("/etc/systemd/user/vice.service"),
        Path("/usr/lib/systemd/user/vice.service"),
        Path("/usr/local/lib/systemd/user/vice.service"),
    )
    for path in candidates:
        if path.exists():
            return path
    return None


def _systemctl_user_query(verb: str) -> str:
    """`systemctl --user <verb> vice.service`, as a single word for doctor."""
    if shutil.which("systemctl") is None:
        return "(systemctl not found)"
    try:
        result = subprocess.run(
            ["systemctl", "--user", verb, "vice.service"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception as exc:
        return f"(query failed: {exc})"
    return (result.stdout or result.stderr or "").strip() or "(unknown)"


def _tail_text_file(path: Path, lines: int = 20) -> str:
    try:
        content = path.read_text(errors="replace").splitlines()
    except Exception:
        return ""
    if not content:
        return ""
    return "\n".join(content[-lines:])


def _doctor_log_tail(path: Path, lines: int = 20) -> str:
    """The end of the daemon log for a report: without the recorder's
    once-a-second throughput lines, and with the last recorder command, which
    is what says which audio was asked for (#309)."""
    try:
        content = path.read_text(errors="replace").splitlines()
    except OSError:
        return ""
    kept = [line for line in content if "fps:" not in line or "gsr:" not in line]
    tail = kept[-lines:]
    for line in reversed(kept):
        if "Starting GSR:" in line:
            if line not in tail:
                tail.insert(0, line)
            break
    return "\n".join(tail)


def _doctor_run(cmd: list[str], timeout: float = 15.0) -> str:
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"({exc})"
    return (done.stdout.strip() or done.stderr.strip())


def _doctor_audio_lines(cfg) -> list[str]:
    """What audio Vice asks the recorder for, and what the newest clip holds.

    "No sound in my clips" is a wrong source, a muted one or a track the
    player does not pick, and nothing else in the report tells them apart.
    """
    from .recorder import _gsr_audio_args
    rc = cfg.recording
    lines = [
        f"  Desktop audio: {'on' if rc.capture_audio else 'off'} "
        f"(source {rc.gsr_audio_source or 'default_output'}, volume {rc.desktop_volume})",
        f"  Microphone: {'on' if rc.capture_microphone else 'off'} "
        f"(source {rc.microphone_source or 'default_input'}, volume {rc.microphone_volume})",
        f"  Separate tracks: {list(rc.audio_tracks) or 'none'}"
        f"{' with a combined track first' if rc.audio_tracks and rc.audio_tracks_mix_first else ''}",
        f"  Recorder audio arguments: {' '.join(_gsr_audio_args(rc)) or '(none)'}",
    ]
    if shutil.which("pactl"):
        lines.append(f"  Default output: {_doctor_run(['pactl', 'get-default-sink'], 3) or '(none)'}")
        lines.append(f"  Default input: {_doctor_run(['pactl', 'get-default-source'], 3) or '(none)'}")
    folder = resolve_path(cfg.output.directory)
    try:
        clips = sorted(
            (f for f in folder.iterdir()
             if f.is_file() and f.suffix.lower() in (".mp4", ".mkv") and not f.name.startswith(".")),
            key=lambda f: f.stat().st_mtime)
    except OSError as exc:
        lines.append(f"  Clips folder: {exc}")
        return lines
    if not clips or not shutil.which("ffprobe") or not shutil.which("ffmpeg"):
        lines.append("  Newest clip: (none to check)" if not clips else "  Newest clip: (ffmpeg not found)")
        return lines
    newest = clips[-1]
    streams = _doctor_run([
        "ffprobe", "-v", "error", "-select_streams", "a",
        "-show_entries", "stream=index,codec_name,channels:stream_tags=title",
        "-of", "csv=p=0", str(newest)])
    rows = [row for row in streams.splitlines() if row.strip()]
    lines.append(f"  Newest clip: {newest.name}, {len(rows)} audio track(s)")
    for number, row in enumerate(rows):
        # The loudest sample of the last ten seconds: -91 dB is digital silence.
        heard = _doctor_run([
            "ffmpeg", "-nostdin", "-hide_banner", "-sseof", "-10", "-i", str(newest),
            "-map", f"0:a:{number}", "-af", "volumedetect", "-f", "null", "-"], 30)
        peak = next((part.split("max_volume:")[1].strip() for part in heard.splitlines()
                     if "max_volume:" in part), "not measured")
        lines.append(f"    track {number + 1}: {row} (loudest {peak})")
    return lines


def _http_probe(url: str, timeout: float = 2.0) -> tuple[bool, str]:
    try:
        with urlopen(url, timeout=timeout) as resp:
            status = getattr(resp, "status", 200)
            return 200 <= status < 400, f"HTTP {status}"
    except HTTPError as exc:
        return 200 <= exc.code < 400, f"HTTP {exc.code}"
    except URLError as exc:
        return False, str(exc.reason)
    except Exception as exc:
        return False, str(exc)


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

@click.group(invoke_without_command=True)
@click.version_option(__version__, prog_name="vice")
@click.pass_context
def cli(ctx: click.Context) -> None:
    """Vice, Linux game clip recorder (Medal.tv for Linux)."""
    normalize_runtime_environment()
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


def _running_daemon_version(status_line: Optional[str]) -> Optional[str]:
    """Version reported by the daemon already holding the socket."""
    try:
        return str(json.loads(status_line or "").get("version") or "") or None
    except (ValueError, AttributeError):
        return None


def _take_over_outdated_daemon(status_line: Optional[str]) -> bool:
    """Stop a daemon running different code so this one can replace it.

    Returns True when replacement can proceed under the ownership lock.
    Same version means the user
    simply started Vice twice, which stays an error: silently killing a
    healthy daemon would be worse than refusing.
    """
    running = _running_daemon_version(status_line)
    if not running or running == __version__:
        return False

    log.warning(
        "Daemon on the socket reports v%s but this binary is v%s; "
        "stopping it so the upgraded code can take over",
        running, __version__,
    )
    click.echo(f":: Replacing the running Vice {running} daemon with {__version__}", err=True)
    asyncio.run(_ipc("stop", timeout=5.0))

    # The daemon closes its socket on the way out. Waiting on that rather than
    # on the process means this works whoever started it.
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if not SOCKET_FILE.exists():
            return True
        if not daemon_is_running(SOCKET_FILE, PID_FILE):
            break
        time.sleep(0.1)

    if daemon_is_running(SOCKET_FILE, PID_FILE):
        log.error("The previous Vice daemon is still shutting down; refusing to replace it")
        return False
    # start() clears leftover state after acquiring the ownership lock. A
    # competing launcher may have claimed the socket while this one waited.
    return True


@cli.command()
@click.option("--debug", is_flag=True, help="Enable verbose logging.")
@click.option("--open-ui/--no-open-ui", default=True,
              help="Open the web UI in the browser on start.")
def start(debug: bool, open_ui: bool) -> None:
    """Start the Vice recording daemon."""
    _setup_daemon_logging(debug)
    log.info("Vice daemon startup requested (python=%s)", sys.executable)
    log.info("Runtime environment at daemon start: %s", runtime_env_snapshot())

    # The unit is wanted by default.target, which the user manager can reach
    # before the compositor has exported anything (#139). Only under systemd:
    # a terminal start must never sit there waiting.
    if running_under_systemd() and not has_display():
        wait_for_display()
        log.info("Runtime environment after session wait: %s", runtime_env_snapshot())

    replacing = False
    if SOCKET_FILE.exists():
        resp = asyncio.run(_ipc("status", timeout=1.5))
        if resp is not None:
            # A daemon left over from before a package upgrade is still running
            # the old code, and Python cannot hot-reload it. Refusing to start
            # leaves the user on the old version with no sign of it, and under
            # systemd it turns into an endless restart loop because retrying
            # can never clear the condition. Take over instead.
            if _take_over_outdated_daemon(resp):
                replacing = True
            else:
                click.echo("Vice is already running. Use `vice stop` or `vice status`.", err=True)
                sys.exit(1)

    try:
        lock = claim_daemon_lock(SOCKET_FILE, timeout=2.0 if replacing else 0.0)
    except (OSError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc
    with lock:
        if daemon_is_running(SOCKET_FILE, PID_FILE):
            raise click.ClickException(
                "Vice is already starting, running, or shutting down. "
                "Its control socket did not answer; wait before trying again."
            )
        SOCKET_FILE.unlink(missing_ok=True)
        _run_daemon(open_ui)


def _run_daemon(open_ui: bool) -> None:
    try:
        daemon = ViceDaemon()
    except Exception:
        log.exception("Vice daemon failed during startup")
        raise

    if open_ui and daemon.cfg.sharing.enabled:
        port = daemon.cfg.sharing.port
        from threading import Timer
        def _open():
            subprocess.Popen(
                ["xdg-open", f"http://localhost:{port}/"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        Timer(1.5, _open).start()

    try:
        asyncio.run(daemon.run())
    except KeyboardInterrupt:
        pass
    except Exception:
        # Without this the reason only ever reaches stderr, so a user reading
        # vice.log sees it stop mid-startup with nothing after it (#142).
        log.exception("Vice daemon failed during startup")
        raise


@cli.command()
def ui() -> None:
    """Open the Vice web UI in your browser."""
    raw = asyncio.run(_ipc("url"))
    if raw and raw.startswith("http"):
        url = raw
    else:
        cfg = load_config()
        url = f"http://localhost:{cfg.sharing.port}/"
        if not raw:
            click.echo("Daemon may not be running, opening default port anyway.")
    subprocess.Popen(
        ["xdg-open", url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    click.echo(f"Opening {url}")


@cli.command()
@click.argument("seconds", required=False, type=click.IntRange(min=1))
@click.option("--duration", "-d", "duration", type=click.IntRange(min=1),
              help="Seconds to save, instead of the clip key's duration.")
def clip(seconds: Optional[int], duration: Optional[int]) -> None:
    """Save a clip right now (daemon must be running).

    `vice clip 30` saves the last 30 seconds, for a Stream Deck button or a
    script; without a number it saves what the clip key does.
    """
    if seconds is not None and duration is not None and seconds != duration:
        raise click.UsageError("Give the duration once, as SECONDS or --duration.")
    wanted = seconds if seconds is not None else duration
    resp = asyncio.run(_ipc("clip" if wanted is None else f"clip {wanted}"))
    if resp is None:
        click.echo("Vice is not running. Start it with `vice start`.", err=True)
        sys.exit(1)
    if resp.startswith("error:"):
        click.echo(f"Clip not saved: {resp[len('error:'):].strip()}", err=True)
        sys.exit(1)
    if resp != "ok" and not resp.startswith("ok "):
        # A daemon from before durations existed answers "unknown command".
        click.echo("The running Vice is older than this command. Restart it with "
                   "`vice stop` and `vice start`, or reopen the Vice app.", err=True)
        sys.exit(1)
    saved = resp[len("ok "):].strip()
    click.echo(f"Clip of the last {saved} s triggered!" if saved else "Clip triggered!")


@cli.command()
def stop() -> None:
    """Stop the Vice daemon."""
    resp = asyncio.run(_ipc("stop"))
    if resp is None:
        click.echo("Vice is not running.", err=True)
        sys.exit(1)
    click.echo("Stopped.")


@cli.command()
def status() -> None:
    """Show daemon status."""
    raw = asyncio.run(_ipc("status"))
    if raw is None:
        click.echo("Vice is not running.")
        return
    try:
        info = json.loads(raw)
        click.echo(f"Status   : {'running' if info['running'] else 'stopped'}")
        click.echo(f"Backend  : {info['backend']}")
        click.echo(f"Clips    : {info['clips']}")
        click.echo(f"Output   : {info['output']}")
        if info.get("local_url"):
            click.echo(f"UI URL   : {info['local_url']}/")
        if info.get("public_url"):
            click.echo(f"Share URL: {info['public_url']}/")
    except Exception:
        click.echo(raw)


@cli.command()
def doctor() -> None:
    """Print startup diagnostics for environment, install, and service issues."""
    cfg_error = ""
    try:
        cfg = load_config()
    except Exception as exc:
        cfg = Config()
        cfg_error = str(exc)
    vice_cmd = shutil.which("vice") or "(not found)"
    vice_app_cmd = shutil.which("vice-app") or "(not found)"
    package_file = Path(sys.modules["vice"].__file__).resolve()
    systemd_env = user_systemd_env_snapshot()
    service_file = _installed_service_file()
    running_status = asyncio.run(_ipc("status"))

    click.echo("Vice doctor")
    click.echo(f"Version         : {__version__}")
    click.echo(f"Python          : {sys.executable}")
    click.echo(f"Package         : {package_file}")
    click.echo(f"vice            : {vice_cmd}")
    click.echo(f"vice-app        : {vice_app_cmd}")
    click.echo(f"Config          : {CONFIG_PATH}")
    if cfg_error:
        click.echo(f"Config error    : {cfg_error}")
    click.echo(f"Daemon log      : {DAEMON_LOG_FILE}")
    info = detect_platform(_install_method)
    click.echo(f"Platform        : {info['os_id'] or '?'} ({info['device']}, "
               f"{'immutable' if info['immutable'] else 'mutable'}, "
               f"installed via {info['packaging']}, session {info['session'] or '?'})")
    if in_vice_flatpak():
        click.echo(f"Flatpak         : {VICE_FLATPAK_ID}; gpu-screen-recorder Flatpak "
                   f"{'installed' if gsr_flatpak_installed() else 'NOT installed'}")
    click.echo("")

    click.echo("Environment")
    for key, value in runtime_env_snapshot().items():
        click.echo(f"  {key}={value or '(unset)'}")
    click.echo("")

    click.echo("User systemd environment")
    if systemd_env:
        for key in sorted(systemd_env):
            click.echo(f"  {key}={systemd_env[key]}")
    else:
        click.echo("  (unavailable)")
    click.echo("")

    click.echo("Service")
    if service_file is not None:
        click.echo(f"  File: {service_file}")
        click.echo(f"  Enabled: {_systemctl_user_query('is-enabled')}")
        click.echo(f"  Active: {_systemctl_user_query('is-active')}")
        service_tail = _tail_text_file(service_file, lines=30)
        if service_tail:
            for line in service_tail.splitlines():
                click.echo(f"    {line}")
    else:
        click.echo("  File: (not installed)")
    click.echo("")

    click.echo("Recorder probe")
    try:
        recorder = create_recorder(cfg)
        click.echo(f"  OK: {type(recorder).__name__} ({recorder.name})")
    except Exception as exc:
        click.echo(f"  ERROR: {exc}")
    click.echo("")

    click.echo("Daemon status")
    if running_status is None:
        click.echo("  IPC: not running")
        local_url = f"http://localhost:{cfg.sharing.port}/"
    else:
        click.echo(f"  IPC: {running_status}")
        try:
            info = json.loads(running_status)
            local_url = f"{str(info.get('local_url') or f'http://localhost:{cfg.sharing.port}').rstrip('/')}/"
        except Exception:
            local_url = f"http://localhost:{cfg.sharing.port}/"
    ok, detail = _http_probe(local_url)
    click.echo(f"  HTTP: {'ok' if ok else 'error'} ({detail}) {local_url}")
    click.echo("")

    click.echo("Dependencies")
    for tool in ("gpu-screen-recorder", "wf-recorder", "ffmpeg", "xdg-open", "systemctl",
                 "xdotool", "xprop", "wmctrl"):
        click.echo(f"  {tool}: {shutil.which(tool) or '(not found)'}")
    from .recorder import GSR_FLATPAK_APP, set_gsr_source
    set_gsr_source(cfg.recording.gsr_source)
    click.echo(f"  gpu-screen-recorder Flatpak ({GSR_FLATPAK_APP}): "
               f"{'installed' if gsr_flatpak_installed() else '(not installed)'}")
    click.echo(f"  gpu-screen-recorder in use: {gsr_source() or '(none)'} "
               f"(recording.gsr_source = {cfg.recording.gsr_source})")
    click.echo(f"  vosk (offline speech): {'installed' if is_vosk_available() else '(not installed)'}")
    voice_cfg = getattr(cfg, "voice", None)
    raw_model_path = getattr(voice_cfg, "model_path", "") if voice_cfg else ""
    model_path = get_model_path(raw_model_path)
    click.echo(f"  vosk model: {model_path or '(not found - see README)'}")
    voice_enabled = getattr(voice_cfg, "enabled", False) is True
    click.echo(f"  voice clipping: {'enabled' if voice_enabled else 'disabled'}")
    click.echo("")

    click.echo("Audio")
    try:
        for line in _doctor_audio_lines(cfg):
            click.echo(line)
    except Exception as exc:
        click.echo(f"  ERROR: {exc}")
    click.echo("")

    # From this shell, which is not necessarily what the daemon's environment
    # looks like. When this says x11 and the log line below says adapter=none,
    # the daemon started before the session exported DISPLAY (#176).
    from .active_window import adapter_name
    click.echo(f"Window detection (this shell): {adapter_name()}")
    click.echo("")

    click.echo("Recent daemon log")
    log_tail = _doctor_log_tail(DAEMON_LOG_FILE, lines=20)
    if log_tail:
        for line in log_tail.splitlines():
            click.echo(f"  {line}")
    else:
        click.echo("  (no log output yet)")


@cli.command("config")
def show_config() -> None:
    """Print the config file path and its contents."""
    click.echo(f"Config: {CONFIG_PATH}\n")
    if CONFIG_PATH.exists():
        click.echo(CONFIG_PATH.read_text())
    else:
        click.echo("(no config file yet, will be created on first `vice start`)")


@cli.command("open-config")
def open_config() -> None:
    """Open the config file in $EDITOR."""
    if not CONFIG_PATH.exists():
        from .config import Config
        save_config(Config())
        click.echo(f"Created default config at {CONFIG_PATH}")
    editor = os.environ.get("EDITOR", "nano")
    os.execlp(editor, editor, str(CONFIG_PATH))


@cli.command()
def matugen() -> None:
    """Set up matugen to colour Vice from your wallpaper."""
    from importlib.resources import files as pkg_files
    from .share import MATUGEN_PATH
    # Copied out of the package so matugen can read it from any install,
    # the Flatpak included.
    template = MATUGEN_PATH.with_name("matugen-template.json")
    template.parent.mkdir(parents=True, exist_ok=True)
    template.write_text((pkg_files("vice") / "data" / "matugen.json").read_text())
    click.echo("Add this to ~/.config/matugen/config.toml:\n")
    click.echo("[templates.vice]")
    click.echo(f"input_path = '{template}'")
    click.echo(f"output_path = '{MATUGEN_PATH}'")
    click.echo("\nThen run matugen and pick Matugen under Settings, Appearance.")


@cli.command("list-keys")
@click.option("--filter", "filt", default="", help="Filter by substring.")
def list_keys(filt: str) -> None:
    """List available hotkey names for use in config."""
    keys = list_available_keys()
    if filt:
        keys = [k for k in keys if filt.upper() in k]
    for k in keys:
        click.echo(k)


@cli.command()
def clips() -> None:
    """List saved clips in the output directory."""
    cfg = load_config()
    out_dir = resolve_path(cfg.output.directory)
    if not out_dir.exists():
        click.echo("No clips directory found.")
        return
    files = sorted(out_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        click.echo("No clips saved yet.")
        return
    for f in files:
        size_mb = f.stat().st_size / (1024 * 1024)
        click.echo(f"{f.name}  ({size_mb:.1f} MB)")


@cli.command()
@click.option("--yes", "-y", is_flag=True, help="Skip all confirmation prompts.")
def uninstall(yes: bool) -> None:
    """Remove Vice cleanly, config, service, and optionally clips."""
    click.echo("Vice uninstaller\n")

    if in_vice_flatpak():
        click.echo("Vice is installed as a Flatpak.")
        click.echo(f"Run: flatpak uninstall {VICE_FLATPAK_ID}")
        click.echo("Add --delete-data to also remove its settings. Clips stay where they are.")
        return

    if _installed_via_aur():
        click.echo("Vice was installed via AUR.")
        click.echo("Run: yay -Rns vice-clipper")
        return

    if _installed_via_nix():
        click.echo("Vice is installed by Nix and cannot remove itself.")
        click.echo("Drop services.vice.enable (or the package) and rebuild.")
        return

    # 1. Stop daemon
    if SOCKET_FILE.exists():
        click.echo("Stopping daemon…")
        asyncio.run(_ipc("stop"))

    # 2. Disable systemd user service
    service = actual_home_dir() / ".config" / "systemd" / "user" / "vice.service"
    if service.exists():
        if yes or click.confirm("Disable and remove the systemd user service?", default=True):
            subprocess.run(
                ["systemctl", "--user", "disable", "--now", "vice"],
                capture_output=True,
            )
            service.unlink()
            click.echo("  Removed systemd service.")

    # 3. Remove config
    if CONFIG_DIR.exists():
        if yes or click.confirm(f"Remove config directory {CONFIG_DIR}?", default=False):
            shutil.rmtree(CONFIG_DIR)
            click.echo(f"  Removed {CONFIG_DIR}.")

    # 4. Offer to remove clips
    try:
        cfg = load_config() if CONFIG_PATH.exists() else None
        clips_dir = resolve_path(cfg.output.directory) if cfg else actual_home_dir() / "Videos" / "Vice"
    except Exception:
        clips_dir = actual_home_dir() / "Videos" / "Vice"

    if clips_dir.exists():
        n = len(list(clips_dir.glob("*.mp4")))
        if n > 0 and (yes or click.confirm(
            f"Delete {n} saved clip(s) in {clips_dir}?", default=False
        )):
            shutil.rmtree(clips_dir)
            click.echo(f"  Deleted {n} clip(s).")

    using_venv = _using_install_script_venv()

    # 5. Remove the Python package or the dedicated install.sh virtualenv
    if using_venv:
        click.echo("\nRemoving Vice virtual environment…")
        shutil.rmtree(INSTALL_VENV_DIR, ignore_errors=True)
        click.echo(f"  Removed {INSTALL_VENV_DIR}.")
    else:
        click.echo("\nUninstalling Python package…")
        subprocess.run([sys.executable, "-m", "pip", "uninstall", "vice", "-y"])

    removed = _remove_local_install_artifacts()
    removed.extend(_remove_legacy_user_site_artifacts())
    if using_venv and INSTALL_VENV_DIR not in removed:
        removed.append(INSTALL_VENV_DIR)
    if removed:
        click.echo("\nRemoved local Vice install files:")
        for path in removed:
            click.echo(f"  {path}")
        _refresh_desktop_caches()

    click.echo("\nVice has been removed. Goodbye!")


if __name__ == "__main__":
    cli()
