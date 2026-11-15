"""
Vice recorder, manages the continuous capture buffer and clip extraction.

Backend priority (auto mode):
  1. gpu-screen-recorder (gsr):  best: native replay buffer, NVIDIA NVENC, Wayland + X11
  2. wf-recorder:                good: Wayland (wlroots, GNOME portal, KDE portal)
  3. ffmpeg x11grab:             fallback: X11 only

Environment detection
---------------------
* Wayland  : $WAYLAND_DISPLAY is set
* Hyprland : $HYPRLAND_INSTANCE_SIGNATURE is set (subset of Wayland)
* GNOME    : $XDG_CURRENT_DESKTOP contains "GNOME"
* KDE      : $XDG_CURRENT_DESKTOP contains "KDE"
* NVIDIA   : /proc/driver/nvidia/version exists or nvidia-smi succeeds
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import time
import unicodedata
from abc import ABC, abstractmethod
from collections import deque
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Callable, List, Optional

from . import active_window
from .config import Config
from .media import get_duration as _get_duration
from .media import probe_media_detailed
from .runtime import host_argv, in_vice_flatpak, recover_wayland_display, resolve_path

log = logging.getLogger("vice.recorder")

# ──────────────────────────────────────────────────────────────────────────────
# Environment helpers
# ──────────────────────────────────────────────────────────────────────────────

def _is_wayland() -> bool:
    return recover_wayland_display()


def _is_x11() -> bool:
    return bool(os.environ.get("DISPLAY")) and not _is_wayland()


# What gpu-screen-recorder prints when asked for a window on a Wayland
# session (src/recorder/capture_setup.c).
GSR_WINDOW_CAPTURE_WAYLAND_ERROR = "window capture only works in a pure X11 session"


def gsr_window_capture_supported() -> bool:
    """Whether gpu-screen-recorder can pin capture to a window here.

    GSR treats an XWayland server as Wayland and refuses every window target
    on it, so Game Capture only works in a pure X11 session (#261, #274).
    Only the session's own variables count: a leftover wayland socket on an
    X11 desktop must not switch the feature off, and a session this misses is
    caught by the first refused pin instead."""
    if os.environ.get("WAYLAND_DISPLAY"):
        return False
    return os.environ.get("XDG_SESSION_TYPE", "").lower() != "wayland"


def _is_nvidia() -> bool:
    if Path("/proc/driver/nvidia/version").exists():
        return True
    return shutil.which("nvidia-smi") is not None and _run_ok(["nvidia-smi", "-L"])


def _run_ok(cmd: list[str]) -> bool:
    try:
        subprocess.run(cmd, capture_output=True, timeout=5)
        return True
    except Exception:
        return False


def _has(tool: str) -> bool:
    return shutil.which(tool) is not None


def _combine_process_output(*chunks) -> str:
    parts: list[str] = []
    for chunk in chunks:
        if not chunk:
            continue
        if isinstance(chunk, bytes):
            text = chunk.decode(errors="replace")
        else:
            text = str(chunk)
        text = text.strip()
        if text:
            parts.append(text)
    return "\n".join(parts)


def _run_command_capture(cmd: list[str], timeout: float = 5.0) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, _combine_process_output(proc.stdout, proc.stderr)
    except subprocess.TimeoutExpired as exc:
        return 124, _combine_process_output(exc.stdout, exc.stderr)
    except Exception as exc:
        return 1, str(exc)


def _wf_runtime_error(raw: str) -> Optional[str]:
    lowered = raw.lower()
    if "compositor doesn't support wlr-screencopy-unstable-v1" in lowered:
        return "compositor doesn't support wlr-screencopy-unstable-v1"
    if "failed to start screencopy" in lowered:
        return "failed to start screencopy"
    if "xdg-desktop-portal" in lowered and "failed" in lowered:
        return "failed to use xdg-desktop-portal screencast"
    return None


@lru_cache(maxsize=1)
def _wf_help_text() -> str:
    if not _has("wf-recorder"):
        return ""
    outputs: list[str] = []
    for args in (["wf-recorder", "--help"], ["wf-recorder", "-h"]):
        _, out = _run_command_capture(args, timeout=2.0)
        if out:
            outputs.append(out)
        if outputs:
            break
    return "\n".join(outputs)


def _wf_supports_flag(flag: str) -> bool:
    return flag in _wf_help_text()


def _wf_list_outputs() -> tuple[list[dict], Optional[str]]:
    if not _has("wf-recorder"):
        return [], None

    _, out = _run_command_capture(["wf-recorder", "-L"], timeout=3.0)
    runtime_error = _wf_runtime_error(out)
    if runtime_error:
        return [], runtime_error

    lowered = out.lower()
    if (
        "invalid option -- 'l'" in lowered
        or "unsupported command line argument" in lowered
        or "unrecognized option" in lowered
    ):
        return [], "installed wf-recorder does not support output listing (-L)"

    return _parse_wf_display_lines(out), None


def _extra_gsr_args(raw: str) -> list[str]:
    """Parse user-provided gpu-screen-recorder CLI args."""
    if not raw.strip():
        return []

    # Allow env var + tilde expansion so users can reference shell-style values.
    expanded = os.path.expanduser(os.path.expandvars(raw))
    monitor = _desktop_audio_source("default")
    expanded = expanded.replace("$(pactl get-default-sink).monitor", monitor)
    expanded = expanded.replace("(pactl get-default-sink).monitor", monitor)

    try:
        args = shlex.split(expanded)
    except ValueError as exc:
        log.warning("Invalid recording.gsr_args ignored: %s", exc)
        return []

    # Convenience placeholder for desktop monitor source.
    replaced: list[str] = []
    for arg in args:
        val = arg.replace("{default_sink_monitor}", monitor)
        val = val.replace("$(pactl get-default-sink).monitor", monitor)
        val = val.replace("(pactl get-default-sink).monitor", monitor)
        replaced.append(val)
    return replaced




def _gsr_has_any_flag(args: list[str], *flags: str) -> bool:
    for arg in args:
        for f in flags:
            if arg == f or arg.startswith(f + "="):
                return True
    return False


def _color_depth(rc) -> str:
    """Validated bits per channel ("8" or "10")."""
    value = str(getattr(rc, "color_depth", "") or "8").strip()
    if value not in {"8", "10"}:
        log.warning("Unknown recording.color_depth=%r, using 8", value)
        return "8"
    return value


def _gsr_codec_for_encoder(encoder: str, depth: str = "8") -> Optional[str]:
    """The gpu-screen-recorder -k value for an encoder choice, or None to let
    GSR pick. 10-bit only exists for HEVC and AV1, so a 10-bit request with
    H.264 or auto resolves to HEVC."""
    if encoder in {"h264_vulkan", "hevc_vulkan", "av1_vulkan"}:
        if depth == "10":
            return "av1_10bit_vulkan" if encoder == "av1_vulkan" else "hevc_10bit_vulkan"
        return encoder
    if depth == "10":
        if encoder in {"av1", "av1_nvenc", "av1_vaapi", "libaom-av1", "libsvtav1"}:
            return "av1_10bit"
        if encoder not in {"hevc", "hevc_nvenc", "hevc_vaapi", "libx265"}:
            log.info(
                "10-bit colour needs HEVC or AV1 (encoder=%s); recording HEVC 10-bit",
                encoder,
            )
        return "hevc_10bit"
    if encoder == "auto":
        return None
    if encoder in {"h264", "h264_nvenc", "h264_vaapi", "libx264"}:
        return "h264"
    if encoder in {"hevc", "hevc_nvenc", "hevc_vaapi", "libx265"}:
        return "hevc"
    if encoder in {"av1", "av1_nvenc", "av1_vaapi", "libaom-av1", "libsvtav1"}:
        return "av1"
    return None


# ── which gpu-screen-recorder ────────────────────────────────────────────────
#
# Besides the distro's gpu-screen-recorder, Vice can run the Flatpak one. It is
# built against its own FFmpeg with the patch that negotiates the NVENC API at
# runtime, so it still records on the GPU where the distro's FFmpeg demands a
# newer NVIDIA driver than the card can have (#247).

GSR_FLATPAK_APP = "com.dec05eba.gpu_screen_recorder"
GSR_FLATPAK_PREFIX = ["flatpak", "run", "--command=gpu-screen-recorder", GSR_FLATPAK_APP]
GSR_SOURCES = ("auto", "native", "flatpak")
# Set from recording.gsr_source by create_recorder.
_gsr_source_pref = "auto"


def set_gsr_source(pref: str) -> None:
    """Choose which gpu-screen-recorder to run: auto, native or flatpak."""
    global _gsr_source_pref
    pref = pref if pref in GSR_SOURCES else "auto"
    if pref != _gsr_source_pref:
        _gsr_source_pref = pref
        _gsr_help_text.cache_clear()
        _gsr_supported_codecs.cache_clear()


@lru_cache(maxsize=1)
def gsr_flatpak_installed() -> bool:
    if not in_vice_flatpak() and not _has("flatpak"):
        return False
    code, _ = _run_command_capture(host_argv(["flatpak", "info", GSR_FLATPAK_APP]), timeout=5.0)
    return code == 0


def refresh_gsr_probes() -> None:
    """Forget what was learned about gpu-screen-recorder, after it has been
    installed while Vice was running."""
    gsr_flatpak_installed.cache_clear()
    _gsr_help_text.cache_clear()
    _gsr_supported_codecs.cache_clear()


def gsr_source() -> Optional[str]:
    """"native" or "flatpak", whichever will run, or None when neither can.

    Auto keeps the distro's build whenever it is installed, so nothing changes
    for anyone who did not ask for the Flatpak.
    """
    if in_vice_flatpak():
        # Nothing records inside the sandbox; the GSR Flatpak runs on the host.
        return "flatpak" if gsr_flatpak_installed() else None
    native = _has("gpu-screen-recorder")
    if _gsr_source_pref == "native":
        return "native" if native else None
    if _gsr_source_pref == "flatpak":
        return "flatpak" if gsr_flatpak_installed() else None
    if native:
        return "native"
    return "flatpak" if gsr_flatpak_installed() else None


def _gsr_prefix() -> Optional[list[str]]:
    """The argv that runs gpu-screen-recorder, or None when there is none."""
    source = gsr_source()
    if source == "native":
        return ["gpu-screen-recorder"]
    if source == "flatpak":
        return host_argv(GSR_FLATPAK_PREFIX, watch=True)
    return None


def _gsr_args_after_prefix(cmd: list[str]) -> Optional[list[str]]:
    """The recorder's own arguments in a command that runs the GSR Flatpak,
    with or without the host prefix in front, or None if it does not."""
    n = len(GSR_FLATPAK_PREFIX)
    for start in range(len(cmd) - n + 1):
        if cmd[start:start + n] == GSR_FLATPAK_PREFIX:
            return cmd[start + n:]
    return None


def _program_name(cmd: list[str]) -> str:
    """The recorder a command runs, seeing through flatpak run."""
    if _gsr_args_after_prefix(cmd) is not None:
        return "gpu-screen-recorder"
    return cmd[0] if cmd else ""


def _is_flatpak_capture(cmd: list[str]) -> bool:
    return _gsr_args_after_prefix(cmd) is not None


@lru_cache(maxsize=None)
def _gsr_help_text() -> str:
    prefix = _gsr_prefix()
    if prefix is None:
        return ""
    _, out = _run_command_capture([*prefix, "--help"], timeout=3.0)
    return out


def _gsr_supports_flag(flag: str) -> bool:
    return flag in _gsr_help_text()


@lru_cache(maxsize=None)
def _gsr_supported_codecs() -> frozenset[str]:
    """The video codecs this GPU actually offers, per gpu-screen-recorder.

    ffmpeg's encoder list only says how ffmpeg was built, so it happily claims
    h264_nvenc on a card that will not open it (#156). GSR asks the driver.

    An empty set means "could not tell", never "nothing is supported": every
    caller has to keep its old behaviour in that case.
    """
    prefix = _gsr_prefix()
    if prefix is None:
        return frozenset()
    code, out = _run_command_capture([*prefix, "--info"], timeout=5.0)
    if code != 0 or not out:
        return frozenset()
    codecs: set[str] = set()
    in_section = False
    for line in out.splitlines():
        value = line.strip()
        if value.startswith("section="):
            in_section = value == "section=video_codecs"
            continue
        if in_section and re.fullmatch(r"[a-z0-9_]+", value):
            codecs.add(value)
    return frozenset(codecs)


_NVENC_API_MISMATCH = re.compile(
    r"only supports nvenc api version (\d+(?:\.\d+)*).*?requires nvenc api version (\d+(?:\.\d+)*)",
    re.IGNORECASE,
)


def _gsr_nvenc_api_mismatch() -> Optional[dict]:
    """The NVENC API versions, when GSR says the driver is too old for its FFmpeg.

    FFmpeg 9 needs NVENC API 13.1, which needs driver 610, and the 580 branch
    is the last one for Maxwell and Pascal cards (#206, #247). A reboot does
    not fix that, so the CPU fallback has to say so. None means no mismatch,
    or GSR could not be asked.
    """
    prefix = _gsr_prefix()
    if prefix is None:
        return None
    _, out = _run_command_capture([*prefix, "--info"], timeout=5.0)
    found = _NVENC_API_MISMATCH.search(out or "")
    if not found:
        return None
    return {"kind": "nvenc_api", "driver": found.group(1), "required": found.group(2)}


def _gsr_codec_unsupported(codec: Optional[str]) -> bool:
    """Whether GSR has told us this -k value will not work on this GPU."""
    if not codec:
        return False
    supported = _gsr_supported_codecs()
    return bool(supported) and codec not in supported


# Order to reach for when the configured codec is out. HEVC first: every GPU
# with an AV1 encoder also has HEVC, and HEVC is the wider bet for players.
_GSR_CODEC_PREFERENCE = ("hevc", "av1", "h264")
_GSR_CODEC_PREFERENCE_10BIT = ("hevc_10bit", "av1_10bit")
_GSR_VULKAN_PREFERENCE = ("h264_vulkan", "hevc_vulkan", "av1_vulkan")
_GSR_VULKAN_PREFERENCE_10BIT = ("hevc_10bit_vulkan", "av1_10bit_vulkan")


def _gsr_codec_choice(rc, avoid: Optional[str] = None) -> Optional[str]:
    """The -k value for a GSR command, or None to leave it to GSR.

    avoid names a codec already known not to work here, either because
    --info does not list it or because it just refused to open. Leaving the
    flag off is not a way to avoid one: GSR's own default resolves to h264.
    """
    depth = _color_depth(rc)
    codec = _gsr_codec_for_encoder(rc.encoder, depth)
    supported = _gsr_supported_codecs()
    # GSR's automatic selection does not consider Vulkan encoders (#206).
    # Leave its normal choice alone unless only the Vulkan path is available.
    if (rc.encoder == "auto" and depth == "8" and supported
            and not supported.intersection(_GSR_CODEC_PREFERENCE)):
        codec = next((c for c in _GSR_VULKAN_PREFERENCE if c in supported), None)
    rejected = {c for c in (avoid,) if c}
    if codec and _gsr_codec_unsupported(codec):
        rejected.add(codec)

    if not rejected:
        # encoder=auto resolves to None, and has always meant "no -k".
        return codec
    if codec and codec not in rejected:
        return codec

    if not supported:
        return None
    order = _GSR_CODEC_PREFERENCE_10BIT if depth == "10" else _GSR_CODEC_PREFERENCE
    vulkan = _GSR_VULKAN_PREFERENCE_10BIT if depth == "10" else _GSR_VULKAN_PREFERENCE
    order += vulkan
    for candidate in order:
        if candidate in supported and candidate not in rejected:
            return candidate
    return None


def _gsr_codec_args(rc, extra: list[str], avoid: Optional[str] = None) -> list[str]:
    """The -k arguments for a GSR command, honouring a user-supplied -k."""
    if _gsr_has_any_flag(extra, "-k"):
        return []
    if any(arg == "-encoder=cpu" or (arg == "-encoder" and extra[i + 1:i + 2] == ["cpu"])
           for i, arg in enumerate(extra)):
        return []
    configured = _gsr_codec_for_encoder(rc.encoder, _color_depth(rc))
    codec = _gsr_codec_choice(rc, avoid)
    if configured and codec != configured and avoid is None:
        log.warning(
            "This GPU does not list %s (it offers %s); recording %s instead",
            configured,
            ", ".join(sorted(_gsr_supported_codecs())) or "nothing",
            codec or "whatever gpu-screen-recorder picks",
        )
    return ["-k", codec] if codec else []


# What GSR says when the GPU encoder itself is the problem, as opposed to a
# bad monitor name or a missing output directory. A driver that stops
# offering NVENC after a kernel update reports "Could not open video codec:
# Function not implemented" (#156).
_ENCODER_FAILURE_MARKERS = (
    "could not open video codec",
    "failed to create encoder",
    "failed to load libnvidia-encode",
    "libcuda",
    "nvenc",
    "vaapi",
    "no encoder",
    "no video encoder",
    "neither h264",
    "neither hevc",
    "encoder not supported",
    "gpu encoding is not supported",
)


def _looks_like_encoder_failure(detail: str) -> bool:
    """Whether a GSR startup failure is worth retrying on the CPU encoder."""
    text = (detail or "").lower()
    return any(marker in text for marker in _ENCODER_FAILURE_MARKERS)


def _gsr_wants_disk_replay(rc) -> bool:
    storage = (getattr(rc, "gsr_replay_storage", "") or "auto").strip().lower()
    if storage == "disk":
        return True
    try:
        buffer_duration = int(rc.buffer_duration)
    except (TypeError, ValueError):
        return False
    return storage == "auto" and buffer_duration > 600


def _classify_gsr_source(source: str) -> str:
    """Rough kind of a GSR -a value: "monitor" (desktop audio), "input"
    (microphone), "app", or "unknown"."""
    value = (source or "").strip()
    if value == "default_output":
        return "monitor"
    if value == "default_input":
        return "input"
    if value.startswith(("app:", "app-inverse:")):
        return "app"
    if value.startswith("device:"):
        return "monitor" if value.endswith(".monitor") else "input"
    return "unknown"


def _container(rc) -> str:
    """Validated clip container ("mp4" or "mkv")."""
    value = (getattr(rc, "container", "") or "mp4").strip().lower()
    if value not in {"mp4", "mkv"}:
        log.warning("Unknown recording.container=%r, using mp4", value)
        return "mp4"
    return value


def _with_mic_and_mix(rc, tracks: list[str]) -> tuple[list[str], bool]:
    """The separate tracks as GSR will write them: the microphone appended
    when it is captured but not listed, and the combined track first when one
    is wanted. Returns (tracks, whether track 0 is the combined one)."""
    tracks = list(tracks)
    mixed = False
    if _captures_microphone(rc):
        mic = _gsr_mic_source(rc)
        if not any(mic in track.split("|") for track in tracks):
            tracks.append(mic)
    if getattr(rc, "audio_tracks_mix_first", False) and len(tracks) > 1:
        mix: list[str] = []
        parts = [part for track in tracks for part in track.split("|") if part]
        # GSR rejects one track that contains both app: and app-inverse:
        # sources. A monitor already contains application audio, so it is
        # also the correct combined source and avoids recording it twice.
        has_monitor = any(_classify_gsr_source(part) == "monitor" for part in parts)
        has_app = any(part.startswith("app:") for part in parts)
        has_inverse_app = any(part.startswith("app-inverse:") for part in parts)
        for part in parts:
            if has_monitor and _classify_gsr_source(part) == "app":
                continue
            if part not in mix:
                mix.append(part)
        if has_app and has_inverse_app and not has_monitor:
            # GSR cannot express both application directions in one
            # track. Keep the individual tracks, which still preserve
            # the requested sources, instead of starting a bad command.
            log.warning(
                "Cannot create a combined audio track from app and "
                "app-inverse sources without a desktop monitor; keeping "
                "the separate tracks"
            )
        elif mix:
            tracks.insert(0, "|".join(mix))
            mixed = True
    return tracks, mixed


def _separate_track_layout(rc) -> tuple[list[str], bool]:
    """What _gsr_audio_args writes for separate audio_tracks, or ([], False)
    when the clip is recorded with a mixed track."""
    tracks = [
        str(t).strip()
        for t in (getattr(rc, "audio_tracks", None) or [])
        if str(t).strip()
    ]
    if not tracks or not _captures_desktop_audio(rc):
        return [], False
    return _with_mic_and_mix(rc, tracks)


def _gsr_audio_args(rc, *, split_for_volume: bool = True) -> list[str]:
    """GSR audio flags: one -a per configured track, or one mixed input.

    gpu-screen-recorder records each -a flag as its own audio track;
    sources joined with "|" inside one flag are mixed together.
    split_for_volume=False keeps a single mixed track even when the volume
    sliders are active (session recordings get no save-time mix pass).
    """
    tracks = [
        str(t).strip()
        for t in (getattr(rc, "audio_tracks", None) or [])
        if str(t).strip()
    ]
    if not _captures_desktop_audio(rc):
        # The desktop-audio toggle silences desktop/app sources but must not
        # take the microphone down with them (#110).
        kept: list[str] = []
        for track in tracks:
            parts = [
                p for p in track.split("|")
                if p and _classify_gsr_source(p) == "input"
            ]
            if parts:
                kept.append("|".join(parts))
        if tracks and len(kept) != len(tracks):
            # Silently dropping the tracks a user built by hand looks like
            # multi-track being broken (#137), so say what happened.
            log.warning(
                "Capture desktop audio is off, so %d of your %d separate audio "
                "tracks will not be recorded. Turn it back on under Settings → "
                "Audio to keep them.",
                len(tracks) - len(kept), len(tracks),
            )
        tracks = kept
    if tracks:
        tracks, _ = _with_mic_and_mix(rc, tracks)
        args: list[str] = []
        for track in tracks:
            args += ["-a", track]
        return args
    if (
        split_for_volume
        and _captures_desktop_audio(rc)
        and _captures_microphone(rc)
        and _save_audio_pass_wanted(rc)
    ):
        # Record desktop and mic as separate tracks so the save-time audio
        # pass can balance and downmix them before mixing down.
        desktop_source = (getattr(rc, "gsr_audio_source", "") or "default_output").strip() or "default_output"
        _warn_if_desktop_source_is_mic(desktop_source)
        return ["-a", desktop_source, "-a", _gsr_mic_source(rc)]
    merged = _gsr_audio_input(rc)
    return ["-a", merged] if merged else []


def _gsr_resolution_args(rc, extra: list[str]) -> list[str]:
    """GSR `-s WxH` flag for the configured output resolution, if any."""
    resolution = (getattr(rc, "resolution", None) or "").strip()
    if not resolution or _gsr_has_any_flag(extra, "-s"):
        return []
    if not re.fullmatch(r"\d+x\d+", resolution):
        log.warning(
            "Ignoring recording.resolution=%r, expected WIDTHxHEIGHT (e.g. 1920x1080)",
            resolution,
        )
        return []
    return ["-s", resolution]


def _gsr_sanitize_args(args: list[str], blocked_flags: set[str]) -> list[str]:
    """Drop flags that Vice manages internally to avoid conflicting values."""
    out: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in blocked_flags:
            i += 2
            continue
        if any(arg.startswith(f + "=") for f in blocked_flags):
            i += 1
            continue
        out.append(arg)
        i += 1
    return out


def _selected_display_id(rc, override: Optional[str] = None) -> Optional[str]:
    """The display to capture. `override` is the live follow-the-pointer pick,
    which wins over the saved choice without overwriting it."""
    value = override if override is not None else getattr(rc, "display", None)
    if value is None:
        return None
    selected = str(value).strip()
    return selected or None


def _detect_x11_resolution() -> Optional[str]:
    try:
        out = subprocess.check_output(
            ["xdpyinfo"], text=True, stderr=subprocess.DEVNULL
        )
        for line in out.splitlines():
            if "dimensions:" in line:
                return line.split()[1]  # e.g. "1920x1080"
    except Exception as exc:
        log.debug("xdpyinfo could not report the screen size: %s", exc)
    return None


def _unquote_gsr_ident(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1].strip()
    return value


def _parse_gsr_display_lines(raw: str) -> list[dict]:
    displays: list[dict] = []
    generic_targets = {
        "window",
        "focused",
        "screen",
        "screen-direct-force",
        "portal",
    }
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        line = line.lstrip("-*• ").strip()
        lowered = line.lower()
        # Filter GSR diagnostic noise, `--list-capture-options` is known to
        # print error strings (e.g. "gsr error: for_each_active_monitor_output_drm
        # failed, ...") on systems where DRM enumeration fails. They share stdout
        # with the real options because _run_command_capture merges stdout+stderr.
        if (
            lowered.startswith("gsr error")
            or lowered.startswith("error:")
            or "for_each_active_monitor" in lowered
            or "failed to open" in lowered
        ):
            continue
        if not line or lowered.startswith("monitor") or lowered in generic_targets:
            continue
        ident = line
        label = line
        if "|" in line:
            head, tail = line.split("|", 1)
            ident = _unquote_gsr_ident(head)
            tail = tail.strip()
            label = f"{ident} ({tail})" if tail else ident
        elif match := re.match(r'^"([^"]+)"\s*(.*)$', line):
            ident = match.group(1).strip()
            detail = match.group(2).strip()
            label = f"{ident} {detail}".strip()
        elif ":" in line:
            head, _ = line.split(":", 1)
            if head and " " not in head:
                ident = _unquote_gsr_ident(head)
        else:
            ident = _unquote_gsr_ident(line.split()[0])
            label = ident if ident != line else label
        if ident and ident.lower() not in generic_targets:
            displays.append({"id": ident, "label": label})
    return displays


def _parse_wf_display_lines(raw: str) -> list[dict]:
    displays: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        line = line.lstrip("-*• ").strip()
        if not line:
            continue
        ident = line
        if ":" in line:
            head, _ = line.split(":", 1)
            if head and " " not in head:
                ident = head.strip()
        else:
            ident = line.split()[0]
        displays.append({"id": ident, "label": line})
    return displays


def _parse_xrandr_display_lines(raw: str) -> list[dict]:
    displays: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("Monitors:"):
            continue
        match = re.match(
            r"^\d+:\s+([+*]*)(\S+)\s+(\d+)/\d+x(\d+)/\d+\+(-?\d+)\+(-?\d+)\s+(\S+)$",
            line,
        )
        if not match:
            continue
        flags, _, width, height, x, y, ident = match.groups()
        displays.append(
            {
                "id": ident,
                "label": f"{ident} ({width}x{height})",
                "width": int(width),
                "height": int(height),
                "x": int(x),
                "y": int(y),
                "primary": "*" in flags,
            }
        )
    return displays


def _display_options(backend: str) -> list[dict]:
    if backend == "gsr":
        prefix = _gsr_prefix()
        if prefix is None:
            return []
        for cmd in (
            [*prefix, "--list-monitors"],
            [*prefix, "--list-capture-options"],
        ):
            code, out = _run_command_capture(cmd, timeout=3.0)
            if code == 0 and out:
                displays = _parse_gsr_display_lines(out)
                if displays:
                    return displays
            lowered = out.lower()
            if "--list-capture-options" in cmd and (
                "unrecognized option" in lowered
                or "unknown option" in lowered
                or "invalid option" in lowered
            ):
                continue
        return []

    if backend == "wf-recorder":
        displays, _ = _wf_list_outputs()
        return displays

    if backend == "ffmpeg":
        if not _has("xrandr"):
            return []
        try:
            out = subprocess.check_output(
                ["xrandr", "--listactivemonitors"],
                text=True,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            return []
        return _parse_xrandr_display_lines(out)

    return []


def resolve_display_backend(preferred: str = "auto") -> str:
    if preferred == "gsr" or (preferred == "auto" and gsr_source() is not None):
        return "gsr"
    if preferred == "wf-recorder" or (preferred == "auto" and _is_wayland() and _has("wf-recorder")):
        return "wf-recorder"
    if preferred == "ffmpeg" or _is_x11():
        return "ffmpeg"
    return preferred


def list_display_options(preferred: str = "auto") -> dict:
    backend = resolve_display_backend(preferred)
    warning = None
    if backend == "wf-recorder":
        displays, warning = _wf_list_outputs()
    else:
        displays = _display_options(backend)
    if preferred in {"gsr", "wf-recorder", "ffmpeg"} and not displays:
        warning = warning or f"Could not list displays for {backend}."
    return {"backend": backend, "displays": displays, "warning": warning}


def _parse_gsr_audio_lines(raw: str, prefix: str) -> list[dict]:
    sources: list[dict] = []
    seen: set[str] = set()
    for line in raw.splitlines():
        value = line.strip().lstrip("-*• ").strip()
        if not value:
            continue
        lowered = value.lower()
        if (
            lowered.startswith("gsr error")
            or lowered.startswith("error:")
            or lowered.startswith("warning:")
            or "failed to" in lowered
        ):
            continue
        # GSR lists devices as "name|Human description"; application
        # entries are bare names. Only the name is a valid -a value.
        name, _, description = value.partition("|")
        name = name.strip()
        description = description.strip()
        if not name:
            continue
        if name in {"default_output", "default_input"}:
            continue  # covered by the hardcoded entries with friendly labels
        source_id = name if name.startswith(("device:", "app:", "app-inverse:")) else f"{prefix}:{name}"
        if source_id in seen:
            continue
        seen.add(source_id)
        label_prefix = "Application" if prefix == "app" else "Device"
        label_value = description or (source_id.split(":", 1)[1] if ":" in source_id else source_id)
        sources.append({
            "id": source_id,
            "label": f"{label_prefix}: {label_value}",
            "kind": _classify_gsr_source(source_id),
        })
    return sources


def list_gsr_audio_sources() -> dict:
    sources = [
        {"id": "default_output", "label": "Default output", "kind": "monitor"},
        {"id": "default_input", "label": "Default input", "kind": "input"},
    ]
    warning = None
    prefix = _gsr_prefix()
    if prefix is None:
        return {"sources": sources, "warning": "gpu-screen-recorder is not installed."}

    code, out = _run_command_capture([*prefix, "--list-audio-devices"], timeout=3.0)
    if code == 0:
        sources.extend(_parse_gsr_audio_lines(out, "device"))
    elif out:
        warning = out.splitlines()[-1].strip()

    code, out = _run_command_capture([*prefix, "--list-application-audio"], timeout=3.0)
    if code == 0:
        apps = _parse_gsr_audio_lines(out, "app")
        sources.extend(apps)
        for app in apps:
            name = app["id"].split(":", 1)[1]
            sources.append({"id": f"app-inverse:{name}", "label": f"All except: {name}", "kind": "app"})
    elif out and not warning:
        warning = out.splitlines()[-1].strip()

    deduped: list[dict] = []
    seen: set[str] = set()
    for source in sources:
        source_id = str(source.get("id") or "")
        if not source_id or source_id in seen:
            continue
        seen.add(source_id)
        deduped.append(source)
    return {"sources": deduped, "warning": warning}


def _resolve_display_option(rc, backend: str, override: Optional[str] = None) -> Optional[dict]:
    selected = _selected_display_id(rc, override)
    if not selected:
        return None
    normalized_selected = selected.split("|", 1)[0].strip()
    for opt in _display_options(backend):
        ident = str(opt.get("id") or "").strip()
        label = str(opt.get("label") or "").strip()
        if selected == ident or selected == label or normalized_selected == ident:
            return opt
    log.warning("Configured display %r is unavailable for backend %s; using auto capture", selected, backend)
    return None


def _default_gsr_capture_target() -> str:
    return "screen"


def _gsr_capture_target(rc, override: Optional[str] = None) -> str:
    selected = _resolve_display_option(rc, "gsr", override)
    return str(selected["id"]) if selected else _default_gsr_capture_target()


def _gsr_capture_args(
    rc, extra: list[str], override: Optional[str] = None, window_override: Optional[str] = None
) -> list[str]:
    """`-w` (and `-s` when needed) flags for gpu-screen-recorder when
    recording.window_capture is on, else the configured/auto monitor target.

    `window_override` is a specific window id the daemon has pinned capture
    to, set only once it has confirmed (via games.json matching) that a
    recognized game is focused, see Daemon._window_capture_loop. This is
    deliberately not GSR's own `-w focused` mode, which tracks whichever
    window currently has input focus and would jump to Discord/a browser/etc.
    the moment you alt-tab away from the game. Pinning by id needs a pure X11
    session: GSR refuses window capture on XWayland too, see
    gsr_window_capture_supported. Falls back to full-display capture when no
    game is pinned yet or geometry can't be resolved.
    """
    if _gsr_has_any_flag(extra, "-w"):
        return []
    if getattr(rc, "window_capture", False) and window_override:
        geometry = active_window.get_window_geometry(window_override)
        if geometry:
            args = ["-w", window_override]
            if not (getattr(rc, "resolution", None) or _gsr_has_any_flag(extra, "-s")):
                args += ["-s", f"{geometry[0]}x{geometry[1]}"]
            return args
        log.warning(
            "recording.window_capture: pinned window %s is no longer "
            "available; capturing the full display until a game is detected again",
            window_override,
        )
    return ["-w", _gsr_capture_target(rc, override)]


def _wf_capture_target(rc, override: Optional[str] = None) -> Optional[str]:
    selected = _resolve_display_option(rc, "wf-recorder", override)
    return str(selected["id"]) if selected else None


def _resolution_scale_filter(resolution: Optional[str]) -> Optional[str]:
    if not resolution:
        return None
    if "x" not in resolution:
        return None
    width, height = resolution.lower().split("x", 1)
    if not width.isdigit() or not height.isdigit():
        return None
    return f"scale={width}:{height}"


def _merge_ffmpeg_filters(flags: list[str], extra_filter: Optional[str]) -> list[str]:
    if not extra_filter:
        return flags
    out: list[str] = []
    i = 0
    merged = False
    while i < len(flags):
        arg = flags[i]
        if arg == "-vf" and i + 1 < len(flags):
            out += ["-vf", f"{extra_filter},{flags[i + 1]}"]
            merged = True
            i += 2
            continue
        out.append(arg)
        i += 1
    if not merged:
        return ["-vf", extra_filter, *out]
    return out


def _ffmpeg_x11_input_args(rc, override: Optional[str] = None) -> tuple[list[str], Optional[str]]:
    display = os.environ.get("DISPLAY", ":0")
    selected = _resolve_display_option(rc, "ffmpeg", override)
    if selected:
        video_size = f"{selected['width']}x{selected['height']}"
        input_display = f"{display}+{selected['x']},{selected['y']}"
        return (
            ["-f", "x11grab", "-framerate", str(rc.fps), "-video_size", video_size, "-i", input_display],
            _resolution_scale_filter(rc.resolution),
        )

    res = rc.resolution or _detect_x11_resolution()
    args = ["-f", "x11grab", "-framerate", str(rc.fps)]
    if res:
        args += ["-s", res]
    args += ["-i", display]
    return args, None

def _pactl_audio_source(kind: str, preferred: str = "default") -> str:
    """Resolve a Pulse/PipeWire source name via pactl.

    kind="desktop": the default sink's monitor source, so clips contain
    system/game audio. Leaving this to "default" can make ffmpeg/wf-recorder
    record the default *input* (microphone) on some setups.
    kind="microphone": the default input source.

    Falls back to `preferred` (logged at debug) when pactl is missing or
    fails, gpu-screen-recorder resolves "default" itself, so this only
    degrades the ffmpeg/wf-recorder paths.
    """
    if preferred and preferred != "default":
        return preferred
    if not _has("pactl"):
        log.debug("pactl not found; using %r for %s audio", preferred, kind)
        return preferred

    get_cmd = "get-default-sink" if kind == "desktop" else "get-default-source"
    try:
        name = subprocess.check_output(
            ["pactl", get_cmd], text=True, stderr=subprocess.DEVNULL
        ).strip()
        if name:
            return f"{name}.monitor" if kind == "desktop" else name
    except Exception as exc:
        log.debug("pactl %s failed: %s", get_cmd, exc)

    # Fallback: walk the source list for a (non-)monitor entry.
    try:
        out = subprocess.check_output(
            ["pactl", "list", "short", "sources"], text=True, stderr=subprocess.DEVNULL
        )
        for line in out.splitlines():
            cols = re.split(r"\s+", line.strip())
            if len(cols) > 1 and (cols[1].endswith(".monitor") == (kind == "desktop")):
                return cols[1]
    except Exception as exc:
        log.debug("pactl list sources failed: %s", exc)

    return preferred


def _desktop_audio_source(preferred: str) -> str:
    return _pactl_audio_source("desktop", preferred)


def _microphone_audio_source(preferred: str = "default") -> str:
    return _pactl_audio_source("microphone", preferred)


def _captures_desktop_audio(rc) -> bool:
    return bool(rc.capture_audio)


def _captures_microphone(rc) -> bool:
    return bool(rc.capture_microphone)


def _gsr_mic_source(rc) -> str:
    """Configured microphone as a GSR -a value ("default_input" or "device:…")."""
    return (getattr(rc, "microphone_source", "") or "default_input").strip() or "default_input"


def _pactl_mic_preferred(rc) -> str:
    """Configured microphone as a pactl source name for wf-recorder/ffmpeg."""
    source = _gsr_mic_source(rc)
    if source == "default_input":
        return "default"
    if source.startswith("device:"):
        return source.split(":", 1)[1]
    return source


def _desktop_volume(rc) -> float:
    try:
        return max(0.0, min(float(getattr(rc, "desktop_volume", 1.0)), 2.0))
    except (TypeError, ValueError):
        return 1.0


def _mic_volume(rc) -> float:
    try:
        return max(0.0, min(float(getattr(rc, "microphone_volume", 1.0)), 2.0))
    except (TypeError, ValueError):
        return 1.0


def _mic_mono(rc) -> bool:
    """Whether the microphone should be downmixed to the centre (#146)."""
    return bool(getattr(rc, "microphone_mono", False)) and _captures_microphone(rc)


def _save_audio_pass_wanted(rc) -> bool:
    """Whether clips need a save-time audio pass. Off at the defaults so the
    recording pipeline stays byte-identical for everyone who never touches
    the sliders. Separate audio_tracks keep full control instead."""
    if getattr(rc, "audio_tracks", None):
        return False
    if _mic_mono(rc):
        return True
    return abs(_desktop_volume(rc) - 1.0) > 0.01 or abs(_mic_volume(rc) - 1.0) > 0.01


_warned_desktop_sources: set[str] = set()


def _warn_if_desktop_source_is_mic(desktop_source: str) -> None:
    parts = [p for p in desktop_source.split("|") if p]
    if not parts or any(_classify_gsr_source(p) != "input" for p in parts):
        return
    if desktop_source in _warned_desktop_sources:
        return
    _warned_desktop_sources.add(desktop_source)
    log.warning(
        "Desktop audio source %r is a microphone input, clips will have no "
        "desktop audio. Pick a monitor source under Settings → Audio.",
        desktop_source,
    )


def _gsr_audio_input(rc) -> Optional[str]:
    desktop = _captures_desktop_audio(rc)
    mic = _captures_microphone(rc)
    desktop_source = (getattr(rc, "gsr_audio_source", "") or "default_output").strip() or "default_output"
    if desktop:
        _warn_if_desktop_source_is_mic(desktop_source)
    if desktop and mic:
        mic_source = _gsr_mic_source(rc)
        parts = [p for p in desktop_source.split("|") if p]
        if mic_source not in parts:
            parts.append(mic_source)
        return "|".join(parts)
    if desktop:
        return desktop_source
    if mic:
        return _gsr_mic_source(rc)
    return None


def _wf_audio_device(rc) -> Optional[str]:
    desktop = _captures_desktop_audio(rc)
    mic = _captures_microphone(rc)
    if desktop and mic:
        if rc.wf_microphone_strategy == "mic_only":
            return _microphone_audio_source(_pactl_mic_preferred(rc))
        return _desktop_audio_source(rc.audio_sink)
    if desktop:
        return _desktop_audio_source(rc.audio_sink)
    if mic:
        return _microphone_audio_source(_pactl_mic_preferred(rc))
    return None


def _ffmpeg_audio_input_args(rc) -> list[str]:
    args: list[str] = []
    if _captures_desktop_audio(rc):
        args += ["-f", "pulse", "-i", _desktop_audio_source(rc.audio_sink)]
    if _captures_microphone(rc):
        args += ["-f", "pulse", "-i", _microphone_audio_source(_pactl_mic_preferred(rc))]
    return args


def _ffmpeg_audio_output_args(rc) -> list[str]:
    desktop = _captures_desktop_audio(rc)
    mic = _captures_microphone(rc)
    if not desktop and not mic:
        return []
    if desktop and mic:
        return [
            "-filter_complex", "[1:a][2:a]amix=inputs=2:normalize=0[aout]",
            "-map", "0:v",
            "-map", "[aout]",
            "-c:a", "aac",
            "-b:a", "128k",
        ]
    return ["-c:a", "aac", "-b:a", "128k"]


def _gsr_monitor_listing_line(line: str) -> bool:
    value = line.strip()
    if not value:
        return False
    if re.match(r'^"[^"]+"\s+\(\d+x\d+[+-]\d+[+-]\d+\)$', value):
        return True
    if re.match(r"^\S+\|\d+x\d+", value):
        return True
    return False


def _gsr_progress_line(line: str) -> bool:
    """GSR prints a throughput line every second. Keeping those would push the
    one line that explains a death straight out of the tail."""
    return bool(re.match(r"^\s*(update|damage)\s+fps:", line, re.IGNORECASE))


def _gsr_runtime_error(raw: str) -> Optional[str]:
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    # X answered but refused the connection. GSR then assumes there is no X
    # server, fails on Wayland, and its last word is "failed to create
    # window", which hides the cause entirely (#231).
    if any("authorization required" in line.lower() for line in lines):
        return ("the X session refused gpu-screen-recorder (authorization "
                "required), so XAUTHORITY is missing or out of date")
    for line in reversed(lines):
        lowered = line.lower()
        if lowered.startswith(("gsr error:", "error:", "gpu-screen-recorder:")):
            return line
        if "failed to" in lowered:
            return line
        if "invalid" in lowered and (
            "capture" in lowered or "target" in lowered or "-w" in lowered
        ):
            return line

    for line in reversed(lines):
        lowered = line.lower()
        if lowered.startswith(("available", "monitors")):
            continue
        if _gsr_monitor_listing_line(line):
            continue
        return line
    return None


def _summarize_process_error(program: str, returncode: Optional[int], stderr_text: str) -> str:
    if program == "wf-recorder":
        runtime_error = _wf_runtime_error(stderr_text)
        if runtime_error:
            return runtime_error
    if program == "gpu-screen-recorder":
        runtime_error = _gsr_runtime_error(stderr_text)
        if runtime_error:
            return runtime_error

    lines = [line.strip() for line in stderr_text.splitlines() if line.strip()]
    if lines:
        return lines[-1]
    if returncode is not None:
        return f"exit code {returncode}"
    return "unknown error"


async def _read_stream_text(stream) -> str:
    if stream is None:
        return ""
    try:
        data = await asyncio.wait_for(stream.read(), timeout=1.0)
    except Exception:
        return ""
    return _combine_process_output(data)


async def _spawn_capture(cmd: list[str], stderr) -> asyncio.subprocess.Process:
    """Start a capture process as its own process-group leader.

    gpu-screen-recorder forks a privileged gsr-kms-server helper. Signalling
    GSR alone leaves that helper running, and because it inherited GSR's
    stderr it holds the write end of our pipe open forever, so asyncio never
    tears the transport down. That leaked one process and one fd per restart
    until the daemon hit EMFILE and stopped clipping (#129). Own group means
    the helper can be reaped with the recorder.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=stderr,
        start_new_session=True,
    )
    proc.vice_flatpak = _is_flatpak_capture(cmd)  # type: ignore[attr-defined]
    # Inside the Vice Flatpak the recorder is a host process, out of reach of
    # this pid namespace, so it is found and signalled on the host.
    proc.vice_host_args = (  # type: ignore[attr-defined]
        _gsr_args_after_prefix(cmd) if proc.vice_flatpak and in_vice_flatpak() else None)
    _register_capture(proc.pid, cmd)
    return proc


# flatpak run execs bwrap, so the pid Vice holds is the sandbox, not the
# recorder. Signalling it is wrong both ways: SIGUSR1 kills bwrap and the
# whole sandbox with it, and once bwrap has gone a SIGKILL to the group would
# land while gpu-screen-recorder is still finishing its file. Checked against
# the real Flatpak in a container (#247).

_GSR_COMM = "gpu-screen-reco"  # /proc comm is cut at 15 characters


def _descendants(pid: int) -> list[int]:
    found: list[int] = []
    try:
        tasks = os.listdir(f"/proc/{pid}/task")
    except OSError:
        return found
    for tid in tasks:
        try:
            kids = Path(f"/proc/{pid}/task/{tid}/children").read_text().split()
        except OSError:
            continue
        for kid in kids:
            found.append(int(kid))
            found.extend(_descendants(int(kid)))
    return found


def _flatpak_recorder_pid(pid: int) -> Optional[int]:
    """The sandboxed gpu-screen-recorder under a flatpak run pid."""
    for child in _descendants(pid):
        try:
            if Path(f"/proc/{child}/comm").read_text().strip() == _GSR_COMM:
                return child
        except OSError:
            continue
    return None


# Finds the host gpu-screen-recorder whose command line is exactly the one
# Vice started, so a recorder the user runs themselves is never touched.
_HOST_FIND_RECORDER = (
    'want=$(printf "%s\\n" "$@"); '
    'for d in /proc/[0-9]*; do '
    '[ "$(cat "$d/comm" 2>/dev/null)" = gpu-screen-reco ] || continue; '
    '[ "$(tr "\\000" "\\n" < "$d/cmdline" 2>/dev/null)" = "$want" ] '
    '&& { echo "${d#/proc/}"; exit 0; }; '
    'done; exit 1'
)


def _host_recorder_pid(gsr_args: list[str]) -> Optional[int]:
    try:
        result = subprocess.run(
            host_argv(["sh", "-c", _HOST_FIND_RECORDER, "sh", "gpu-screen-recorder", *gsr_args]),
            capture_output=True, text=True, timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("Could not look for the recorder on the host: %s", exc)
        return None
    pid = result.stdout.strip()
    return int(pid) if result.returncode == 0 and pid.isdigit() else None


def _host_args(proc) -> Optional[list[str]]:
    """The recorder's arguments when it runs on the host, else None."""
    args = getattr(proc, "vice_host_args", None)
    return args if isinstance(args, list) else None


def _inner_recorder_pid(proc: asyncio.subprocess.Process) -> Optional[int]:
    host_args = _host_args(proc)
    if host_args is not None:
        return _host_recorder_pid(host_args)
    return _flatpak_recorder_pid(proc.pid)


def _kill_recorder(proc: asyncio.subprocess.Process, pid: int, sig: int) -> None:
    """os.kill for the pid _recorder_pid returned, on the host when that is
    where it lives. Raises ProcessLookupError when it has gone."""
    if _host_args(proc) is None:
        os.kill(pid, sig)
        return
    try:
        result = subprocess.run(host_argv(["kill", f"-{int(sig)}", str(pid)]),
                                capture_output=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProcessLookupError(f"could not signal host pid {pid}: {exc}") from exc
    if result.returncode != 0:
        raise ProcessLookupError(f"host pid {pid} is gone")


def _recorder_alive(proc: asyncio.subprocess.Process, pid: int) -> bool:
    if _host_args(proc) is None:
        return Path(f"/proc/{pid}").exists()
    try:
        _kill_recorder(proc, pid, 0)
    except ProcessLookupError:
        return False
    return True


async def _recorder_pid(proc: asyncio.subprocess.Process, wait: float = 3.0) -> Optional[int]:
    """The pid that takes the recorder's signals. The sandbox needs a moment
    to start the recorder inside it, so a fresh one is waited for."""
    if not getattr(proc, "vice_flatpak", False) is True:
        return proc.pid
    deadline = time.monotonic() + wait
    while True:
        inner = await asyncio.to_thread(_inner_recorder_pid, proc)
        if inner is not None or time.monotonic() >= deadline or proc.returncode is not None:
            return inner
        await asyncio.sleep(0.1)


# ── Orphaned capture processes ────────────────────────────────────────────────
#
# Its own session is what keeps the recorder alive when the daemon is killed
# outright: SIGTERM is handled and tears the group down, but `kill -9` and a
# hard crash are not, so gpu-screen-recorder carries on recording with nothing
# supervising it and the next daemon start adds a second one (#121). The
# session cannot be given up, it is the #129 fix, so instead every capture
# process is written down and a stale one is reaped at startup.

CAPTURE_REGISTRY = Path("/tmp/vice/capture.json")


def _register_capture(pid: int, cmd: list[str]) -> None:
    entries = [e for e in _read_capture_registry() if e.get("pid") != pid]
    entries.append({"pid": pid, "argv": list(cmd), "owner": os.getpid()})
    _write_capture_registry(entries)


def _unregister_capture(pid: int) -> None:
    entries = [e for e in _read_capture_registry() if e.get("pid") != pid]
    _write_capture_registry(entries)


def _read_capture_registry() -> list[dict]:
    try:
        data = json.loads(CAPTURE_REGISTRY.read_text())
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        log.debug("Could not read %s: %s", CAPTURE_REGISTRY, exc)
        return []
    return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []


def _write_capture_registry(entries: list[dict]) -> None:
    try:
        CAPTURE_REGISTRY.parent.mkdir(parents=True, exist_ok=True)
        CAPTURE_REGISTRY.write_text(json.dumps(entries))
    except OSError as exc:
        # Losing the registry costs a reap, never a recording, so this is
        # not worth failing a clip over.
        log.debug("Could not write %s: %s", CAPTURE_REGISTRY, exc)


def _process_argv(pid: int) -> Optional[list[str]]:
    """The argv of a live process, or None when it is gone or unreadable."""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except (FileNotFoundError, ProcessLookupError):
        return None
    except OSError as exc:
        log.debug("Could not read the command line of pid %d: %s", pid, exc)
        return None
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def _argv_matches(running: list[str], argv: list[str]) -> bool:
    """Whether a live command line is the capture that was registered. A
    Flatpak capture runs as bwrap, with the recorder's own argv after --."""
    if running == argv:
        return True
    if argv[:len(GSR_FLATPAK_PREFIX)] != GSR_FLATPAK_PREFIX or not running:
        return False
    if Path(running[0]).name != "bwrap" or "--" not in running:
        return False
    tail = running[running.index("--") + 1:]
    return tail == ["gpu-screen-recorder", *argv[len(GSR_FLATPAK_PREFIX):]]


def reap_orphaned_captures() -> int:
    """Kill capture processes left behind by a daemon that did not shut down.

    A pid on its own proves nothing, because pids get recycled and killing
    the wrong process would be far worse than the orphan. The recorded argv
    has to still match what is running, or the entry is dropped untouched.
    Returns how many were killed.
    """
    entries = _read_capture_registry()
    if not entries:
        return 0

    killed = 0
    kept: list[dict] = []
    for entry in entries:
        pid = entry.get("pid")
        argv = entry.get("argv")
        if not isinstance(pid, int) or not isinstance(argv, list):
            continue
        if pid == os.getpid():
            continue
        # A recorder whose daemon is still alive belongs to that daemon. This
        # machine has already had two daemons running at once, and taking the
        # working one's recorder away would be a far worse bug than the
        # orphan this is here to clear.
        owner = entry.get("owner")
        if isinstance(owner, int) and owner != os.getpid() and _process_argv(owner) is not None:
            log.info(
                "Leaving pid %d alone, the daemon that started it (pid %d) is still running",
                pid, owner,
            )
            kept.append(entry)
            continue
        running = _process_argv(pid)
        if running is None:
            continue
        if not _argv_matches(running, [str(a) for a in argv]):
            log.debug(
                "Pid %d is alive but is not the recorder we started, leaving it alone",
                pid,
            )
            continue
        # Capture processes are always started with start_new_session, so a
        # real one leads its own group. If this pid does not, the group is
        # somebody else's and killpg would signal every process in it.
        try:
            if os.getpgid(pid) != pid:
                log.warning(
                    "Pid %d is not its own process group leader, so it is not "
                    "one of ours. Leaving it alone.", pid,
                )
                continue
        except (ProcessLookupError, PermissionError, OSError) as exc:
            log.debug("Could not read the process group of pid %d: %s", pid, exc)
            continue

        log.warning(
            "Found a recorder left over from a previous run (pid %d, %s). "
            "Stopping it before starting a new one.",
            pid, Path(argv[0]).name if argv else "?",
        )
        try:
            os.killpg(pid, signal.SIGTERM)
            killed += 1
        except (ProcessLookupError, PermissionError, OSError) as exc:
            log.warning("Could not stop the leftover recorder pid %d: %s", pid, exc)

    _write_capture_registry(kept)
    return killed


def _signal_group(proc: asyncio.subprocess.Process, sig: int) -> None:
    """Signal a capture process's whole group, helpers included.

    An empty group is the ordinary outcome once everything has exited, so
    ProcessLookupError is not an error here.
    """
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


async def _terminate_group(proc: asyncio.subprocess.Process, timeout: float = 5.0) -> None:
    """Ask a capture process group to stop, then make sure it did.

    The SIGKILL still reaches surviving members after the leader has been
    reaped, which is exactly the case that used to strand gsr-kms-server.
    """
    inner = (await asyncio.to_thread(_inner_recorder_pid, proc)
             if getattr(proc, "vice_flatpak", False) is True else None)
    if inner is not None and _host_args(proc) is not None:
        # flatpak-spawn would hand SIGTERM to bwrap on the host, so the
        # recorder itself is asked to finish its file.
        try:
            await asyncio.to_thread(_kill_recorder, proc, inner, signal.SIGTERM)
        except ProcessLookupError:
            inner = None
    else:
        _signal_group(proc, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        pass
    except ProcessLookupError:
        pass
    except Exception as exc:
        log.warning("Error while stopping capture process: %s", exc)
    # bwrap goes at once, and the recorder inside carries on finishing the
    # file. The SIGKILL has to wait for it, not for bwrap.
    while (inner is not None and time.monotonic() < deadline
           and await asyncio.to_thread(_recorder_alive, proc, inner)):
        await asyncio.sleep(0.1)
    _signal_group(proc, signal.SIGKILL)
    try:
        await proc.wait()
    except Exception as exc:
        log.debug("Capture process %d was already reaped: %s", proc.pid, exc)
    _unregister_capture(proc.pid)


async def _terminate_session_capture(proc: asyncio.subprocess.Process) -> None:
    """Finish reaping before propagating cancellation to the session owner."""
    cleanup = asyncio.create_task(_terminate_group(proc, timeout=8))
    cancelled = False
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            # A second stop/shutdown cancellation must not orphan cleanup or
            # let the daemon release its session lock while capture is alive.
            cancelled = True
    cleanup.result()
    if cancelled:
        raise asyncio.CancelledError


# ──────────────────────────────────────────────────────────────────────────────
# Encoder selection
# ──────────────────────────────────────────────────────────────────────────────

def _available_encoders() -> set[str]:
    """Return the set of ffmpeg video encoders available on this system."""
    try:
        out = subprocess.check_output(
            ["ffmpeg", "-hide_banner", "-encoders"], stderr=subprocess.DEVNULL, text=True
        )
        return {line.split()[1] for line in out.splitlines() if line.startswith(" V")}
    except Exception:
        return set()


def choose_encoder(preferred: str) -> str:
    """
    Resolve 'auto' or validate a user-specified encoder.
    Returns the ffmpeg encoder name to use.
    """
    if preferred != "auto":
        return preferred

    enc = _available_encoders()
    if _is_nvidia():
        if "h264_nvenc" in enc:
            log.info("NVIDIA GPU detected → using h264_nvenc")
            return "h264_nvenc"
    # AMD/Intel VAAPI (Wayland/Mesa)
    if "h264_vaapi" in enc and _is_wayland():
        log.info("VAAPI available → using h264_vaapi")
        return "h264_vaapi"
    log.info("Falling back to software encoder libx264")
    return "libx264"


def _encoder_flags(encoder: str, crf: int, depth: str = "8") -> list[str]:
    """Return ffmpeg flags for a given encoder."""
    # No hardware encoder does 10-bit H.264, and x264 10-bit needs a separate
    # build, so those keep 8-bit whatever the setting says.
    ten_bit = depth == "10" and encoder not in ("h264_nvenc", "h264_vaapi", "libx264")
    if depth == "10" and not ten_bit:
        log.info("10-bit colour needs HEVC or AV1 (encoder=%s); recording 8-bit", encoder)
    if encoder in ("h264_nvenc", "hevc_nvenc", "av1_nvenc"):
        # NVENC: use CQ mode (similar to CRF) and tuning for low-latency
        flags = ["-c:v", encoder, "-rc", "vbr", "-cq", str(crf), "-preset", "p4", "-tune", "hq"]
        return flags + (["-pix_fmt", "p010le"] if ten_bit else [])
    if encoder in ("h264_vaapi", "hevc_vaapi", "av1_vaapi"):
        upload = "format=p010,hwupload" if ten_bit else "format=nv12,hwupload"
        return ["-vf", upload, "-c:v", encoder, "-qp", str(crf)]
    # libx264 / libx265 software
    flags = ["-c:v", encoder, "-crf", str(crf), "-preset", "fast"]
    return flags + (["-pix_fmt", "yuv420p10le"] if ten_bit else [])


# ──────────────────────────────────────────────────────────────────────────────
# Abstract base
# ──────────────────────────────────────────────────────────────────────────────

class Recorder(ABC):
    """Base class for recording backends."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._running = False
        self._clip_callbacks: list[Callable[[Path], None]] = []
        # time.monotonic() when the last save was asked for, which is where
        # that clip ends. The keyboard visualiser lines its keys up to it.
        self.last_save_signal_at: Optional[float] = None
        # Optional sync callback returning the focused game's name (or None);
        # used to tag clip filenames. Runs in a thread (it shells out).
        self.clip_tag_cb: Optional[Callable[[], Optional[str]]] = None
        # Display to capture instead of recording.display, set by the daemon
        # when follow-the-pointer capture is on. Applied at start(), so the
        # daemon restarts the recorder after changing it.
        self.display_override: Optional[str] = None
        # Window id to pin capture to instead of a display, set by the daemon
        # when window_capture is on and it has confirmed a recognized game is
        # focused (see games.json matching). Applied at start(), so the
        # daemon restarts the recorder after changing it.
        self.window_override: Optional[str] = None
        # Session recording state (shared across all backends)
        self._session_active = False
        self._session_proc: Optional[asyncio.subprocess.Process] = None
        self._session_path: Optional[Path] = None
        self._session_start: float = 0.0
        self._session_program = ""
        # Why the last clip failed, for the toast. A generic "check the log"
        # is no use to somebody whose clips are silently unreadable (#154).
        self.last_clip_error = ""

    def on_clip_saved(self, cb: Callable[[Path], None]) -> None:
        """Register a callback invoked with the clip Path once it's ready."""
        self._clip_callbacks.append(cb)

    def last_output(self) -> str:
        """Recent output from the capture process, for diagnosing a death.
        Backends that do not capture stderr return nothing."""
        return ""

    async def _clip_tag(self) -> Optional[str]:
        """Sanitized filename tag for the clip being saved, or None."""
        if not self.clip_tag_cb:
            return None
        try:
            tag = await asyncio.to_thread(self.clip_tag_cb)
        except Exception:
            log.exception("Clip tag callback raised")
            return None
        return filename_tag(tag)

    def _clip_name_template(self) -> str:
        return (getattr(self.cfg.output, "clip_name_template", "") or "").strip()

    def _emit(self, path: Path) -> None:
        for cb in self._clip_callbacks:
            try:
                cb(path)
            except Exception:
                log.exception("Clip callback raised")

    def is_healthy(self) -> bool:
        """Whether capture is believed to be live right now."""
        return self._running

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def save_clip(self, duration: Optional[int] = None) -> Optional[Path]:
        """
        Trigger saving the requested number of seconds, or the configured
        default clip duration when duration is omitted.
        Returns the saved path, or None on failure.
        """
        ...

    @property
    def name(self) -> str:
        return type(self).__name__

    # ── Session recording (shared implementation) ──────────────────────────

    def session_elapsed(self) -> float:
        """Return seconds elapsed since the session started (0 if not active)."""
        if not self._session_active:
            return 0.0
        return time.time() - self._session_start

    def session_capture_exited(self) -> bool:
        """Whether the current session's capture child has already exited."""
        return self._session_proc is not None and self._session_proc.returncode is not None

    async def start_session(self, tag: Optional[str] = None) -> Optional[Path]:
        """
        Begin a continuous session recording directly to a file.
        Returns the output path, or None on failure.
        Session recording uses ffmpeg regardless of the replay-buffer backend
        so that we get a single contiguous output file to stamp highlights into.
        """
        if self._session_active:
            log.warning("Session already active")
            return None

        out_dir = resolve_path(self.cfg.output.directory)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = _next_session_path(out_dir, _container(self.cfg.recording), tag)

        cmd = self._build_session_cmd(out_path)
        if cmd is None:
            log.error("Cannot build session recording command for this environment")
            return None

        log.info("Starting session recording: %s", " ".join(cmd))
        stderr_target = (
            asyncio.subprocess.PIPE
            if cmd and cmd[0] in {"wf-recorder", "ffmpeg"}
            else asyncio.subprocess.DEVNULL
        )
        try:
            self._session_proc = await _spawn_capture(cmd, stderr_target)
        except Exception as exc:
            log.error("Failed to start session recording: %s", exc)
            return None

        self._session_program = _program_name(cmd)
        try:
            try:
                await asyncio.wait_for(self._session_proc.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                self._session_active = True
                self._session_path = out_path
                self._session_start = time.time()
                return out_path
            stderr_text = await _read_stream_text(self._session_proc.stderr)
            detail = _summarize_process_error(
                self._session_program,
                self._session_proc.returncode,
                stderr_text,
            )
            log.error("Session recorder failed to start: %s", detail)
            return None
        finally:
            if not self._session_active:
                try:
                    await _terminate_session_capture(self._session_proc)
                finally:
                    self._session_proc = None
                    self._session_path = None
                    self._session_program = ""

    async def stop_session(self) -> Optional[Path]:
        """
        Stop the active session recording, apply the watermark, and emit the
        clip via the normal on_clip_saved callbacks.
        Returns the saved path, or None on failure.
        """
        if not self._session_active or not self._session_proc:
            log.warning("No active session to stop")
            return None

        path = self._session_path
        proc = self._session_proc
        program = self._session_program
        self._session_active = False

        # Ask the recorder to stop gracefully, then reap anything it forked.
        # The SIGKILL only lands after the leader has exited, so it can never
        # cut a container short.
        try:
            await _terminate_session_capture(proc)
        finally:
            self._session_proc = None
            self._session_path = None
            self._session_program = ""

        stderr_text = await _read_stream_text(proc.stderr)
        if not path or not path.exists():
            detail = _summarize_process_error(program, proc.returncode, stderr_text)
            log.error("Session file not found after stop: %s (%s)", path, detail)
            return None

        if self.cfg.recording.apply_watermark:
            await _apply_watermark(path)
        log.info("Session clip saved: %s", path)
        self._emit(path)
        return path

    def _build_session_cmd(self, out_path: Path) -> Optional[list[str]]:
        """Build a direct-to-file ffmpeg command for session recording."""
        rc = self.cfg.recording
        encoder = choose_encoder(rc.encoder)

        if _is_wayland():
            # Prefer gpu-screen-recorder on Wayland (especially smoother on NVIDIA).
            if gsr_source() is not None:
                return self._gsr_session_cmd(out_path, rc, self.display_override, self.window_override)

            # Fallback: wf-recorder direct-to-file on Wayland.
            if _has("wf-recorder"):
                cmd = ["wf-recorder"]
                if _wf_supports_flag("--force-yuv"):
                    cmd.append("--force-yuv")
                cmd += ["-f", str(out_path)]
                if rc.capture_audio:
                    cmd += [f"--audio={_desktop_audio_source(rc.audio_sink)}"]
                if encoder in ("h264_nvenc", "hevc_nvenc"):
                    cmd += ["-c", encoder]
                elif encoder == "h264_vaapi":
                    cmd += ["-c", "h264_vaapi", "-d", "/dev/dri/renderD128"]
                else:
                    cmd += ["-c", "libx264"]
                return cmd

            # Last resort on XWayland sessions.
            if os.environ.get("DISPLAY") and _has("ffmpeg"):
                return self._ffmpeg_session_cmd(out_path, encoder, rc, self.display_override)
            return None

        if _is_x11() and _has("ffmpeg"):
            return self._ffmpeg_session_cmd(out_path, encoder, rc, self.display_override)

        return None

    @staticmethod
    def _gsr_session_cmd(
        out_path: Path, rc, override: Optional[str] = None, window_override: Optional[str] = None
    ) -> list[str]:
        extra = _gsr_sanitize_args(_extra_gsr_args(rc.gsr_args), {"-o", "-r"})
        cmd = _gsr_prefix() or ["gpu-screen-recorder"]

        cmd += _gsr_capture_args(rc, extra, override, window_override)
        if not _gsr_has_any_flag(extra, "-f"):
            cmd += ["-f", str(rc.fps)]
        cmd += _gsr_resolution_args(rc, extra)
        if not _gsr_has_any_flag(extra, "-c"):
            cmd += ["-c", _container(rc)]
        cmd += _gsr_codec_args(rc, extra)
        if not _gsr_has_any_flag(extra, "-a"):
            cmd += _gsr_audio_args(rc, split_for_volume=False)

        cmd += extra
        cmd += ["-o", str(out_path)]
        return cmd

    @staticmethod
    def _ffmpeg_session_cmd(
        out_path: Path, encoder: str, rc, override: Optional[str] = None
    ) -> list[str]:
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning"]
        input_args, extra_filter = _ffmpeg_x11_input_args(rc, override)
        cmd += input_args
        cmd += _ffmpeg_audio_input_args(rc)
        cmd += _merge_ffmpeg_filters(_encoder_flags(encoder, rc.crf, _color_depth(rc)), extra_filter)
        cmd += _ffmpeg_audio_output_args(rc)
        cmd += ["-y", str(out_path)]
        return cmd


# ──────────────────────────────────────────────────────────────────────────────
# Clip trimming helper (used by GSR backend)
# ──────────────────────────────────────────────────────────────────────────────

def _media_file_names(out_dir: Path) -> set[str]:
    """Names of clip media files (mp4 + mkv) currently in out_dir."""
    return {f.name for f in out_dir.glob("*.mp4")} | {f.name for f in out_dir.glob("*.mkv")}


def _gsr_replay_candidates(current: set[str], baseline: set[str]) -> set[str]:
    """New media files that could be a replay GSR just flushed.

    GSR names its replay files itself (date-based); anything Vice creates,
    sequential clips, session recordings, in-place-edit temp files, can
    never be the flushed replay, so those names are never claimed even if
    they appear in the output directory mid-save.
    """
    return {
        name
        for name in current - baseline
        if not name.startswith(("Vice_Clip_", "Vice_Session_"))
        and not any(t in name for t in (".trim.", ".trimming.", ".wm.", ".fix."))
    }


def filename_tag(game: Optional[str]) -> Optional[str]:
    """The game part of a Vice filename, or None when there is nothing to add.

    Kept in one place because playlists.game_key lowercases the same rule to
    build auto-playlist ids: if the two ever disagree, a clip's tag stops
    matching its own playlist.
    """
    tag = re.sub(r"[^A-Za-z0-9]+", "-", (game or "")).strip("-")
    return tag[:48] or None


# Every extension a given kind of file can be numbered under. Counting has to
# span all of them or switching container mid-library restarts the numbering
# and clobbers what is already there.
VIDEO_EXTS = ("mp4", "mkv")
IMAGE_EXTS = ("png", "jpg", "jpeg")


def _next_numbered_path(
    out_dir: Path,
    stem: str,
    ext: str,
    tag: Optional[str] = None,
    known_exts: tuple[str, ...] = VIDEO_EXTS,
) -> Path:
    """Next available <stem>_N[_Tag].<ext> path. Numbering counts every
    container and tag variant so tagged clips never collide."""
    max_n = 0
    alternatives = "|".join(known_exts)
    pattern = re.compile(rf"^{stem}_(\d+)(?:_.+)?\.(?:{alternatives})$")
    for f in out_dir.glob(f"{stem}_*"):
        m = pattern.match(f.name)
        if m:
            n = int(m.group(1))
            if n > max_n:
                max_n = n
    suffix = f"_{tag}" if tag else ""
    return out_dir / f"{stem}_{max_n + 1}{suffix}.{ext}"


def _sanitize_clip_name(name: str) -> str:
    """Strip anything that cannot live in a filename, then tidy the separators
    an unresolved token leaves behind."""
    name = re.sub(r"[/\\\x00-\x1f]", "", name)
    name = re.sub(r"[_-]{2,}", lambda m: m.group(0)[0], name)
    return name.strip("_- .")


# Anything outside this set is dropped from a user-chosen clip name. Slugs are
# filename stems *and* URL path segments *and* arguments to inline UI handlers,
# so a name is only safe once it survives all three: spaces break share links,
# quotes break the handlers, and "#?%&" break both (#138).
_SLUG_KEEP = re.compile(r"[^\w.\-]", re.UNICODE)


def slugify_clip_name(name: str) -> Optional[str]:
    """Turn a user-typed clip name into a filename stem.

    Whitespace runs become a single dash and unsupported punctuation is
    dropped; casing is left alone. Returns None when nothing usable is left.
    """
    name = unicodedata.normalize("NFC", (name or "").strip())
    for suffix in (".mp4", ".mkv", ".png", ".jpg", ".jpeg"):
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
            break
    name = re.sub(r"\s+", "-", name)
    name = _SLUG_KEEP.sub("", name)
    return _sanitize_clip_name(name) or None


def _render_clip_name(template: str, n: int, game: Optional[str], now: datetime) -> str:
    values = {
        "$n":    str(n),
        "$date": now.strftime("%Y-%m-%d"),
        "$time": now.strftime("%H%M"),
        "$game": game or "",
    }
    rendered = template
    for token, value in values.items():
        rendered = rendered.replace(token, value)
    return _sanitize_clip_name(rendered)


# Private-use codepoints stand in for each token while the template is
# sanitized: they survive it and cannot occur in a real filename.
_TOKEN_MARKERS = {"$n": "\ue000", "$date": "\ue001", "$time": "\ue002", "$game": "\ue003"}
_MARKER_PATTERNS = {
    "\ue000": r"(\d+)",
    "\ue001": r"\d{4}-\d{2}-\d{2}",
    "\ue002": r"\d{4}",
    "\ue003": r".*?",
}


def _template_number_pattern(template: str) -> Optional[re.Pattern]:
    """Matches any clip this template has named, capturing its $n.

    Date, time and game match whatever they were when that clip was saved, so
    the count carries on across minutes, days and games. Matching the current
    values instead restarted $n at 1 every minute (#243). Separator runs are
    loose because sanitizing collapses the ones an empty $game leaves behind.
    """
    skeleton = template
    for token, marker in _TOKEN_MARKERS.items():
        skeleton = skeleton.replace(token, marker)
    skeleton = _sanitize_clip_name(skeleton)
    if _TOKEN_MARKERS["$n"] not in skeleton:
        return None
    parts: list[str] = []
    for char in skeleton:
        if char in _MARKER_PATTERNS:
            parts.append(_MARKER_PATTERNS[char])
        elif char in "_-":
            if not parts or parts[-1] != "[_-]*":
                parts.append("[_-]*")
        else:
            parts.append(re.escape(char))
    return re.compile("^[_-]*" + "".join(parts) + r"[_-]*\.(?:mp4|mkv)$")


def _next_templated_path(
    out_dir: Path, template: str, ext: str, tag: Optional[str], now: datetime
) -> Optional[Path]:
    """Path for a user-defined clip name, or None if the template is unusable.

    $n continues the highest number already on disk for this template. Without
    $n a template can repeat, so a numeric suffix is added to avoid clobbering
    an existing clip."""
    template = (template or "").strip()
    if not template:
        return None

    n = 1
    if "$n" in template:
        pattern = _template_number_pattern(template)
        if pattern is None:
            return None
        max_n = 0
        for f in out_dir.glob("*"):
            m = pattern.match(f.name)
            if m:
                max_n = max(max_n, int(m.group(1)))
        n = max_n + 1

    name = _render_clip_name(template, n, tag, now)
    if not name:
        return None

    candidate = out_dir / f"{name}.{ext}"
    if not candidate.exists():
        return candidate
    for extra in range(2, 1000):
        candidate = out_dir / f"{name}_{extra}.{ext}"
        if not candidate.exists():
            return candidate
    return None


def _next_clip_path(
    out_dir: Path,
    ext: str = "mp4",
    tag: Optional[str] = None,
    template: str = "",
) -> Path:
    """Return the next available clip path in out_dir. Uses the user's filename
    template when set, otherwise Vice_Clip_N[_Game].<ext>."""
    if template:
        path = _next_templated_path(out_dir, template, ext, tag, datetime.now())
        if path is not None:
            return path
        log.warning(
            "Clip name template %r produced no usable filename, using default naming",
            template,
        )
    return _next_numbered_path(out_dir, "Vice_Clip", ext, tag)


def _next_session_path(
    out_dir: Path, ext: str = "mp4", tag: Optional[str] = None,
) -> Path:
    """Return the next available Vice_Session_N[_Game].<ext> path."""
    return _next_numbered_path(out_dir, "Vice_Session", ext, filename_tag(tag))


def next_image_path(out_dir: Path, tag: Optional[str] = None, ext: str = "png") -> Path:
    """Return the next available Vice_Shot_N[_Game].<ext> path in out_dir."""
    return _next_numbered_path(out_dir, "Vice_Shot", ext, tag, known_exts=IMAGE_EXTS)


async def capture_screenshot(
    out_path: Path, rc, override: Optional[str] = None, timeout: float = 20.0
) -> Path:
    """Save a still of the captured screen to *out_path* with GSR.

    gpu-screen-recorder takes screenshots natively when handed an image path,
    so this needs no second capture tool and works on every compositor and GPU
    Vice already supports. It runs as its own short-lived process alongside the
    one holding the replay buffer, which is why nothing here touches the
    recorder's state.
    """
    prefix = _gsr_prefix()
    if prefix is None:
        raise RuntimeError("gpu-screen-recorder is not installed, so screenshots cannot be taken.")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        *prefix,
        "-w", _gsr_capture_target(rc, override),
        "-o", str(out_path),
    ]
    log.debug("Screenshot command: %s", " ".join(cmd))
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        raise RuntimeError("gpu-screen-recorder did not return a screenshot in time.")
    except OSError as exc:
        raise RuntimeError(f"Could not run gpu-screen-recorder: {exc}") from exc

    text = (stderr or b"").decode(errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(
            _summarize_process_error("gpu-screen-recorder", proc.returncode, text)
            or "gpu-screen-recorder could not take a screenshot."
        )
    # GSR exits 0 having written nothing when the capture target is gone. Left
    # unchecked that reaches the UI as a screenshot the user cannot find.
    if not out_path.exists() or out_path.stat().st_size == 0:
        out_path.unlink(missing_ok=True)
        raise RuntimeError(
            _summarize_process_error("gpu-screen-recorder", proc.returncode, text)
            or "gpu-screen-recorder wrote no image."
        )
    return out_path


# Without an explicit map, ffmpeg keeps only one audio stream per output, so
# any pass over a clip recorded with separate desktop and mic tracks silently
# threw the mic away (#119). The "?" makes each map optional for clips that
# have no audio at all. The watermark pass filters video, which needs exactly
# one video input, so it pins the first video stream instead of all of them.
KEEP_ALL_STREAMS = ["-map", "0:v?", "-map", "0:a?"]
KEEP_ALL_AUDIO = ["-map", "0:v:0?", "-map", "0:a?"]


async def _trim_to_last_n_seconds(path: Path, seconds: int) -> Path:
    """Trim a clip to its last `seconds` seconds in-place. Returns the path."""
    total = await _get_duration(path)
    if total <= 0 or total <= seconds:
        return path  # already short enough

    start = total - seconds
    ext = path.suffix.lstrip(".") or "mp4"
    tmp = path.with_suffix(f".trim.{ext}")
    faststart = ["-movflags", "+faststart"] if ext == "mp4" else []

    def _copy_trim_cmd() -> list[str]:
        return [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-ss", str(start), "-i", str(path),
            "-t", str(seconds),
            *KEEP_ALL_STREAMS,
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            *faststart,
            "-y", str(tmp),
        ]

    def _reencode_trim_cmd() -> list[str]:
        return [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-ss", str(start), "-i", str(path),
            "-t", str(seconds),
            *KEEP_ALL_STREAMS,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            *faststart,
            "-y", str(tmp),
        ]

    async def _run_trim(cmd: list[str], timeout: int) -> tuple[bool, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            err = stderr.decode() if stderr else ""
            return proc.returncode == 0, err
        except asyncio.TimeoutError:
            return False, "trim command timed out"

    # ffmpeg exiting cleanly is not proof the trim worked. A file whose frames
    # all share one timestamp trims "successfully" to a single frame, and this
    # replaces the recording in place, so the result has to be checked first
    # (#154). Half the expected length is well clear of keyframe rounding.
    async def _trim_problem() -> Optional[str]:
        if not tmp.exists():
            return "it wrote no file"
        got = await _get_duration(tmp)
        if got < seconds / 2:
            return f"the result is {got:.2f}s long instead of {seconds}s"
        return None

    ok, err = await _run_trim(_copy_trim_cmd(), 60)
    problem = await _trim_problem() if ok else err
    if problem:
        log.warning("ffmpeg copy trim failed, retrying with re-encode: %s", problem)
        ok, err = await _run_trim(_reencode_trim_cmd(), 120)
        problem = await _trim_problem() if ok else err
        if problem:
            log.error("Could not trim %s, keeping the whole clip: %s", path.name, problem)
            tmp.unlink(missing_ok=True)
            return path

    tmp.replace(path)
    return path


async def _wait_for_finalized_clip(
    path: Path,
    *,
    stable_polls: int = 3,
    poll_interval: float = 0.25,
    inactivity_timeout: float = 15.0,
    max_wait: float = 600.0,
) -> bool:
    """Wait until a replay clip stops changing and ffprobe can read it.

    The timeout is based on write inactivity, not total elapsed time:
    flushing a long replay buffer can legitimately take minutes on slow
    storage, and a fixed deadline used to abandon clips that were still
    being written (and would have finished fine). We only give up when
    the file has stopped growing for `inactivity_timeout` seconds and
    still cannot be probed, or after `max_wait` as a hard safety cap.
    """
    deadline = time.monotonic() + max_wait
    last_sig: tuple[int, int] | None = None
    last_change = time.monotonic()
    stable = 0

    while time.monotonic() < deadline:
        try:
            st = path.stat()
            sig = (st.st_size, st.st_mtime_ns)
        except OSError:
            sig = None

        if sig != last_sig:
            stable = 0
            last_sig = sig
            last_change = time.monotonic()
        elif sig is not None and sig[0] > 0:
            stable += 1
        else:
            stable = 0

        if stable >= stable_polls and await _get_duration(path) > 0:
            return True

        if time.monotonic() - last_change > inactivity_timeout:
            return False

        await asyncio.sleep(poll_interval)

    return False


_WATERMARK = (
    "drawtext=text='Clipped with Vice'"
    ":x=w-tw-12:y=h-th-12"
    ":fontsize=17"
    ":fontcolor=white@0.55"
    ":shadowcolor=black@0.7:shadowx=1:shadowy=1"
    ":box=1:boxcolor=black@0.25:boxborderw=7"
)


async def _apply_watermark(path: Path) -> None:
    """Burn the Vice watermark into *path* in-place (re-encodes with libx264)."""
    ext = path.suffix.lstrip(".") or "mp4"
    tmp = path.with_suffix(f".wm.{ext}")
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", str(path),
        "-vf", _WATERMARK,
        *KEEP_ALL_AUDIO,
        "-c:v", "libx264", "-crf", "23", "-preset", "fast",
        "-c:a", "copy",
        *(["-movflags", "+faststart"] if ext == "mp4" else []),
        "-y", str(tmp),
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        if proc.returncode != 0:
            log.warning("watermark encode failed: %s", stderr.decode())
            tmp.unlink(missing_ok=True)
            return
    except asyncio.TimeoutError:
        log.warning("watermark encode timed out")
        tmp.unlink(missing_ok=True)
        return
    tmp.replace(path)


async def _count_audio_streams(path: Path) -> int:
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-select_streams", "a",
            "-show_entries", "stream=index", "-of", "csv=p=0", str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        if proc.returncode != 0:
            return 0
        return len([line for line in out.decode().splitlines() if line.strip()])
    except (asyncio.TimeoutError, OSError):
        return 0


# Both channels get the same summed signal, so a mic that only carries one
# channel lands in the centre instead of one ear (#146). Staying stereo keeps
# amix happy: it refuses inputs with mismatched channel layouts.
_MIC_MONO_PAN = "pan=stereo|c0=0.5*c0+0.5*c1|c1=0.5*c0+0.5*c1"


def _volume_mix_cmd(
    path: Path, tmp: Path, streams: int, dv: float, mv: float, mic_mono: bool = False
) -> list[str]:
    ext = path.suffix.lstrip(".") or "mp4"
    audio_codec = ["-c:a", "libopus", "-b:a", "128k"] if ext == "mkv" else ["-c:a", "aac", "-b:a", "160k"]
    faststart = ["-movflags", "+faststart"] if ext == "mp4" else []
    mono = f"{_MIC_MONO_PAN}," if mic_mono else ""
    if streams >= 2:
        filter_graph = (
            f"[0:a:0]volume={dv}[a0];[0:a:1]{mono}volume={mv}[a1];"
            f"[a0][a1]amix=inputs=2:normalize=0[aout]"
        )
        mapping = ["-filter_complex", filter_graph, "-map", "0:v", "-map", "[aout]"]
    else:
        mapping = ["-af", f"{mono}volume={dv}"]
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", str(path),
        *mapping,
        "-c:v", "copy",
        *audio_codec,
        *faststart,
        "-y", str(tmp),
    ]


async def _apply_volume_mix(path: Path, rc) -> None:
    """Balance desktop vs mic loudness and downmix the mic in-place. Video is
    stream-copied, only audio re-encodes. Any failure leaves the clip
    untouched."""
    if not _save_audio_pass_wanted(rc):
        return
    if [t for t in (getattr(rc, "audio_tracks", None) or []) if str(t).strip()]:
        # User-defined tracks are never split for volume, and the mix graph
        # below only handles the first two, so leave them alone.
        return
    dv, mv = _desktop_volume(rc), _mic_volume(rc)
    mic_mono = _mic_mono(rc)
    streams = await _count_audio_streams(path)
    if streams == 0:
        return
    if streams == 1:
        if _captures_desktop_audio(rc) and _captures_microphone(rc):
            # Both sources share one track (clip recorded before the setting
            # change took effect); can't touch the mic on its own.
            log.debug("Skipping audio pass: single mixed audio track in %s", path.name)
            return
        # A lone desktop track must not be downmixed as if it were the mic.
        if _captures_desktop_audio(rc):
            mic_mono = False
        else:
            dv = mv

    ext = path.suffix.lstrip(".") or "mp4"
    tmp = path.with_suffix(f".mix.{ext}")
    cmd = _volume_mix_cmd(path, tmp, streams, dv, mv, mic_mono)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        if proc.returncode != 0:
            log.warning("audio pass failed, keeping clip as recorded: %s", stderr.decode())
            tmp.unlink(missing_ok=True)
            return
    except asyncio.TimeoutError:
        log.warning("audio pass timed out, keeping clip as recorded")
        tmp.unlink(missing_ok=True)
        return
    tmp.replace(path)


def _track_volumes(rc, layout: list[str], mixed: bool) -> list[float]:
    """Gain for each separate track in *layout*, 1.0 for the combined one."""
    listed = {str(t).strip() for t in (getattr(rc, "audio_tracks", None) or [])}
    volumes = getattr(rc, "audio_track_volumes", None) or {}
    out: list[float] = []
    for i, track in enumerate(layout):
        if mixed and i == 0:
            out.append(1.0)
        elif track not in listed:
            # The microphone added because capture_microphone is on.
            out.append(_mic_volume(rc))
        else:
            try:
                out.append(max(0.0, min(float(volumes.get(track, 1.0)), 2.0)))
            except (TypeError, ValueError):
                out.append(1.0)
    return out


async def _audio_stream_codecs(path: Path) -> list[tuple[str, int]]:
    """(codec name, bitrate or 0) for each audio stream, in order."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-select_streams", "a",
            "-show_entries", "stream=codec_name,bit_rate", "-of", "csv=p=0", str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
    except (asyncio.TimeoutError, OSError):
        return []
    if proc.returncode != 0:
        return []
    streams = []
    for line in out.decode().splitlines():
        name, _, rate = line.strip().partition(",")
        if not name:
            continue
        try:
            streams.append((name, int(rate)))
        except ValueError:
            streams.append((name, 0))
    return streams


_AUDIO_ENCODERS = {"opus": "libopus", "aac": "aac", "mp3": "libmp3lame", "flac": "flac"}


def _track_volume_cmd(
    path: Path, tmp: Path, layout: list[str], mixed: bool,
    volumes: list[float], codecs: list[tuple[str, int]],
) -> list[str]:
    """Re-level the separate tracks in place of the originals and, when the
    first track is the combined one, rebuild it from the re-levelled tracks
    so what players and Discord play is balanced too (#251). Video and any
    untouched track are stream-copied."""
    changed = [abs(v - 1.0) > 0.01 and not (mixed and i == 0)
               for i, v in enumerate(volumes)]
    members: set[int] = set()
    if mixed and any(changed):
        # The combined track leaves out app-only tracks when a monitor is
        # recorded, because the monitor already carries that audio. Same rule
        # GSR's combined track was built with.
        parts = [p for t in layout[1:] for p in t.split("|") if p]
        has_monitor = any(_classify_gsr_source(p) == "monitor" for p in parts)
        members = {
            i for i in range(1, len(layout))
            if not (has_monitor and all(
                _classify_gsr_source(p) == "app" for p in layout[i].split("|") if p))
        }
        if not any(changed[i] for i in members):
            members = set()
    graph: list[str] = []
    labels: dict[int, str] = {}
    for i, volume in enumerate(volumes):
        stereo = f"[0:a:{i}]aformat=channel_layouts=stereo"
        if changed[i]:
            outs = f"asplit=2[v{i}][m{i}]" if i in members else ""
            graph.append(f"{stereo},volume={volume:g}" + (f",{outs}" if outs else f"[v{i}]"))
            labels[i] = f"v{i}"
        elif i in members:
            graph.append(f"{stereo}[m{i}]")
    if members:
        inputs = "".join(f"[m{i}]" for i in sorted(members))
        graph.append(f"{inputs}amix=inputs={len(members)}:normalize=0[mix]")
        labels[0] = "mix"
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path),
            "-filter_complex", ";".join(graph), "-map", "0:v"]
    for i in range(len(layout)):
        args += ["-map", f"[{labels[i]}]" if i in labels else f"0:a:{i}"]
    args += ["-c:v", "copy"]
    for i in range(len(layout)):
        name, rate = codecs[i]
        if i not in labels:
            args += [f"-c:a:{i}", "copy"]
            continue
        args += [f"-c:a:{i}", _AUDIO_ENCODERS.get(name, "aac")]
        if rate:
            args += [f"-b:a:{i}", str(rate)]
    if path.suffix.lower() == ".mp4":
        args += ["-movflags", "+faststart"]
    return args + ["-y", str(tmp)]


async def _apply_track_volumes(path: Path, rc) -> None:
    """Apply the per-track volumes from Settings to a clip with separate
    audio tracks, in place. Nothing runs at the defaults, and any failure,
    or a clip whose tracks no longer match the settings, keeps the clip as
    recorded."""
    layout, mixed = _separate_track_layout(rc)
    if not layout:
        return
    volumes = _track_volumes(rc, layout, mixed)
    if all(abs(v - 1.0) <= 0.01 for v in volumes):
        return
    codecs = await _audio_stream_codecs(path)
    if len(codecs) != len(layout):
        log.info("Not applying track volumes to %s: it has %d audio tracks, "
                 "the settings describe %d", path.name, len(codecs), len(layout))
        return
    ext = path.suffix.lstrip(".") or "mp4"
    tmp = path.with_suffix(f".levels.{ext}")
    cmd = _track_volume_cmd(path, tmp, layout, mixed, volumes, codecs)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
    except asyncio.TimeoutError:
        log.warning("Track volume pass timed out, keeping %s as recorded", path.name)
        tmp.unlink(missing_ok=True)
        return
    if proc.returncode != 0:
        log.warning("Track volume pass failed, keeping %s as recorded: %s",
                    path.name, stderr.decode(errors="replace").strip()[:300])
        tmp.unlink(missing_ok=True)
        return
    tmp.replace(path)


# ──────────────────────────────────────────────────────────────────────────────
# gpu-screen-recorder backend
# ──────────────────────────────────────────────────────────────────────────────

class GSRRecorder(Recorder):
    """
    Uses gpu-screen-recorder (https://git.dec05eba.com/gpu-screen-recorder).
    Supports: NVIDIA (NVENC), AMD (VAAPI), Intel (VAAPI), Wayland KMS, X11.
    Replay-buffer mode: sends SIGUSR1 to flush the buffer to a file.
    """

    def __init__(self, cfg: Config) -> None:
        super().__init__(cfg)
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._out_dir = resolve_path(cfg.output.directory)
        self._watch_task: Optional[asyncio.Task] = None
        # Kept so an unexpected death can say why instead of just "died".
        self._stderr_tail: deque[str] = deque(maxlen=12)
        # How many routine GSR lines still go to the log at INFO. The useful
        # ones are all at startup, so a small budget covers them.
        self._stderr_info_budget = 40
        # Set when the GPU encoder was unusable this run and CPU encoding
        # took over. Never persisted, every start tries the GPU first, so a
        # driver that gets fixed is picked up without the user doing anything.
        self.cpu_fallback = False
        self.cpu_fallback_reason: Optional[dict] = None
        # Set when the configured codec would not open and GSR was left to
        # pick one. Still GPU encoding, so it is worth telling the user apart
        # from the CPU fallback (#156).
        self.codec_fallback = False

    @property
    def name(self) -> str:
        return "gpu-screen-recorder"

    def is_healthy(self) -> bool:
        return self._running and self._proc is not None and self._proc.returncode is None

    def _build_cmd(self, cpu_encoder: bool = False, avoid_codec: Optional[str] = None) -> list[str]:
        rc = self.cfg.recording
        extra = _gsr_sanitize_args(_extra_gsr_args(rc.gsr_args), {"-o"})
        cmd = _gsr_prefix() or ["gpu-screen-recorder"]

        # Allow manual overrides through recording.gsr_args.
        cmd += _gsr_capture_args(rc, extra, self.display_override, self.window_override)

        if not _gsr_has_any_flag(extra, "-f"):
            cmd += ["-f", str(rc.fps)]

        cmd += _gsr_resolution_args(rc, extra)

        if not _gsr_has_any_flag(extra, "-r"):
            cmd += ["-r", str(rc.buffer_duration)]

        if not _gsr_has_any_flag(extra, "-replay-storage") and _gsr_wants_disk_replay(rc):
            if _gsr_supports_flag("-replay-storage"):
                cmd += ["-replay-storage", "disk"]
            else:
                log.info(
                    "This gpu-screen-recorder build has no -replay-storage flag; "
                    "keeping the replay buffer in RAM"
                )

        if not _gsr_has_any_flag(extra, "-c"):
            cmd += ["-c", _container(rc)]
        # GSR only does H.264 on the CPU, and that is its default, so asking
        # for anything else here would fail the retry that is meant to rescue
        # the recording.
        if not cpu_encoder:
            cmd += _gsr_codec_args(rc, extra, avoid_codec)

        if not _gsr_has_any_flag(extra, "-a"):
            cmd += _gsr_audio_args(rc)

        if cpu_encoder and not _gsr_has_any_flag(extra, "-encoder"):
            cmd += ["-encoder", "cpu"]

        cmd += extra

        # Output directory (gsr writes files here on SIGUSR1)
        self._out_dir.mkdir(parents=True, exist_ok=True)
        cmd += ["-o", str(self._out_dir)]
        return cmd

    async def _try_start(self, cpu_encoder: bool, avoid_codec: Optional[str] = None) -> Optional[str]:
        """Spawn GSR. Returns None when it stayed up, else why it did not."""
        cmd = self._build_cmd(cpu_encoder, avoid_codec)
        log.info("Starting GSR: %s", " ".join(cmd))
        self._stderr_tail.clear()
        self._proc = await _spawn_capture(cmd, asyncio.subprocess.PIPE)
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            return None
        stderr_text = await _read_stream_text(self._proc.stderr)
        detail = _summarize_process_error(
            "gpu-screen-recorder",
            self._proc.returncode,
            stderr_text,
        )
        # GSR may have forked its helper before giving up.
        _signal_group(self._proc, signal.SIGKILL)
        _unregister_capture(self._proc.pid)
        self._proc = None
        return detail

    async def start(self) -> None:
        self._running = True
        self.cpu_fallback = False
        self.cpu_fallback_reason = None
        self.codec_fallback = False
        self._stderr_info_budget = 40

        if in_vice_flatpak() and gsr_source() is None:
            # The UI's setup step installs it; until then say so plainly.
            raise RuntimeError("The gpu-screen-recorder Flatpak is not installed")

        rc = self.cfg.recording
        detail = await self._try_start(cpu_encoder=False)

        # Staying on the GPU is worth one more try. A card can list a codec
        # and still refuse it, H.264 above 4096 pixels wide on NVIDIA being
        # the common one, and another codec handles it (#156). Dropping the
        # flag is not the retry: GSR's own default is H.264 too.
        first_codec = _gsr_codec_choice(rc) if detail is not None else None
        alternative = _gsr_codec_choice(rc, avoid=first_codec) if first_codec else None
        if (
            detail is not None
            and _looks_like_encoder_failure(detail)
            and alternative
            and alternative != first_codec
        ):
            log.warning(
                "The GPU would not encode %s (%s). Retrying with %s.",
                first_codec, detail, alternative,
            )
            retry_detail = await self._try_start(cpu_encoder=False, avoid_codec=first_codec)
            if retry_detail is None:
                self.codec_fallback = True
                detail = None
            else:
                detail = retry_detail

        if detail is not None and _looks_like_encoder_failure(detail) and _gsr_supports_flag("-encoder"):
            mismatch = await asyncio.to_thread(_gsr_nvenc_api_mismatch)
            if mismatch:
                log.warning(
                    "The GPU encoder would not open (%s). The NVIDIA driver supports "
                    "NVENC API %s but this FFmpeg needs %s, so retrying on CPU.",
                    detail, mismatch["driver"], mismatch["required"],
                )
            else:
                log.warning(
                    "The GPU encoder would not open (%s). Retrying on CPU. "
                    "This is usually a driver that needs a reboot after an update.",
                    detail,
                )
            retry_detail = await self._try_start(cpu_encoder=True)
            if retry_detail is None:
                self.cpu_fallback = True
                if mismatch is not None:
                    # The Flatpak build negotiates the NVENC API at runtime,
                    # so it is the way back to the GPU on this driver (#247).
                    mismatch = {**mismatch, "flatpak": gsr_flatpak_installed(),
                                "source": gsr_source()}
                self.cpu_fallback_reason = mismatch
                detail = None
            else:
                log.error("CPU encoding did not work either: %s", retry_detail)

        if detail is not None:
            self._running = False
            raise RuntimeError(f"gpu-screen-recorder failed to start: {detail}")
        self._watch_task = asyncio.create_task(self._stderr_reader())

    async def _stderr_reader(self) -> None:
        assert self._proc and self._proc.stderr
        async for line in self._proc.stderr:
            text = line.decode(errors="replace").rstrip()
            if not text or _gsr_progress_line(text):
                log.debug("gsr: %s", text)
                continue
            self._stderr_tail.append(text)
            # GSR's own complaints used to only exist at debug level, which
            # made an audio source it could not find or a stream it dropped
            # invisible in a normal vice.log. Chatter is capped so a build
            # that prints something we do not recognise as progress cannot
            # fill the log over a long session; complaints never are.
            lowered = text.lower()
            if any(w in lowered for w in ("error", "failed", "not found", "unable", "invalid")):
                log.warning("gsr: %s", text)
            elif self._stderr_info_budget > 0:
                self._stderr_info_budget -= 1
                log.info("gsr: %s", text)
            else:
                log.debug("gsr: %s", text)

    def last_output(self) -> str:
        """GSR's last non-routine stderr, for explaining an unexpected death.
        Empty when it said nothing, which is the honest answer for a process
        that was simply killed."""
        return "\n".join(self._stderr_tail)

    async def stop(self) -> None:
        self._running = False
        if self._proc:
            proc = self._proc
            self._proc = None
            await _terminate_group(proc)
        if self._watch_task:
            self._watch_task.cancel()
            self._watch_task = None

    async def save_clip(self, duration: Optional[int] = None) -> Optional[Path]:
        self.last_clip_error = ""
        if not self._proc or self._proc.returncode is not None:
            self.last_clip_error = "The recorder is not running. Check Settings for an encoder error."
            log.error("GSR process is not running")
            return None
        clip_duration = int(duration or self.cfg.recording.clip_duration)

        # Snapshot the directory immediately before triggering the flush.
        # Diffing against a baseline captured at recorder start misattributed
        # files that appeared in between (a finished session recording, a clip
        # renamed in the UI, a late flush from a timed-out save) as the new
        # replay, the wrong clip then got renamed, trimmed, and shown.
        baseline = _media_file_names(self._out_dir)

        target = await _recorder_pid(self._proc)
        if target is None:
            self.last_clip_error = "The Flatpak recorder has not started inside its sandbox."
            log.error("No gpu-screen-recorder found inside the Flatpak sandbox (pid=%d)",
                      self._proc.pid)
            return None
        log.info("Sending SIGUSR1 to GSR (pid=%d) to save replay", target)
        try:
            await asyncio.to_thread(_kill_recorder, self._proc, target, signal.SIGUSR1)
            self.last_save_signal_at = time.monotonic()
        except ProcessLookupError:
            self.last_clip_error = "The recorder stopped before the clip could be saved."
            log.error("GSR process not found")
            return None

        def _mtime(p: Path) -> float:
            try:
                return p.stat().st_mtime
            except OSError:
                return 0.0

        # Wait for the new file to appear (GSR creates it almost immediately
        # after SIGUSR1), then wait for GSR to finish writing it.
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            await asyncio.sleep(0.25)
            current = _media_file_names(self._out_dir)
            new = _gsr_replay_candidates(current, baseline)
            if new:
                newest = max((self._out_dir / n for n in new), key=_mtime)
                if not await _wait_for_finalized_clip(newest):
                    # The file has stopped changing by now, so one more probe
                    # is cheap and is the only way to learn why it cannot be
                    # read. Without it the reporter and I both get nothing
                    # more than "clip save failed" (#154).
                    _, why = await probe_media_detailed(newest)
                    # An empty reason means ffprobe read the file and simply
                    # found no duration, which is not the same as corrupt.
                    why = why or "it reads, but reports no duration"
                    self.last_clip_error = (
                        f"{newest.name} was written but cannot be read: {why}."
                        " The file is still there, nothing was deleted."
                    )
                    log.error(
                        "GSR clip %s stopped being written but is unreadable (%s). "
                        "Leaving the file in place for inspection.",
                        newest, why,
                    )
                    return None
                # Rename GSR's auto-generated filename to a sequential
                # Vice_Clip_N name, tagged with the focused game if known.
                seq_path = _next_clip_path(
                    self._out_dir,
                    ext=newest.suffix.lstrip(".") or "mp4",
                    tag=await self._clip_tag(),
                    template=self._clip_name_template(),
                )
                newest.rename(seq_path)
                newest = seq_path
                # GSR saves the entire buffer; trim to the requested clip duration.
                trimmed = await _trim_to_last_n_seconds(newest, clip_duration)
                await _apply_volume_mix(trimmed, self.cfg.recording)
                await _apply_track_volumes(trimmed, self.cfg.recording)
                if self.cfg.recording.apply_watermark:
                    await _apply_watermark(trimmed)
                log.info("Clip saved: %s", trimmed)
                self._emit(trimmed)
                return trimmed

        self.last_clip_error = (
            "gpu-screen-recorder did not write a clip within 20 seconds. "
            "The log has its output."
        )
        log.error("Timed out waiting for GSR to write clip")
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Segment-based backend (wf-recorder or ffmpeg x11grab)
# ──────────────────────────────────────────────────────────────────────────────

SEGMENT_DURATION = 30  # seconds per segment
MAX_SEGMENTS = 20      # 20 × 30 s = 10 min max buffer


class SegmentRecorder(Recorder):
    """
    Rolling-segment recording: records 30-second chunks in a temp directory,
    keeping the most recent MAX_SEGMENTS. On clip request, concatenates the
    segments covering the last `clip_duration` seconds using ffmpeg.

    This backend works with any capture tool that writes to a file:
    wf-recorder (Wayland) or ffmpeg -f x11grab (X11).
    """

    def __init__(self, cfg: Config, use_wf_recorder: bool) -> None:
        super().__init__(cfg)
        self._use_wf = use_wf_recorder
        self._seg_dir = Path("/tmp/vice/segs")
        self._seg_dir.mkdir(parents=True, exist_ok=True)
        self._seg_index = 0
        self._segments: list[tuple[float, Path]] = []  # (start_time, path)
        self._loop_task: Optional[asyncio.Task] = None
        self._current_proc: Optional[asyncio.subprocess.Process] = None
        self._encoder = choose_encoder(cfg.recording.encoder)
        self._out_dir = resolve_path(cfg.output.directory)
        self._out_dir.mkdir(parents=True, exist_ok=True)
        self._last_segment_error: Optional[str] = None

    @property
    def name(self) -> str:
        return "wf-recorder" if self._use_wf else "ffmpeg"

    def is_healthy(self) -> bool:
        return self._running and self._loop_task is not None and not self._loop_task.done()

    # ── Capture commands ──────────────────────────────────────────────────────

    def _wf_recorder_cmd(self, out: Path) -> list[str]:
        rc = self.cfg.recording
        cmd = ["wf-recorder"]
        if _wf_supports_flag("--force-yuv"):
            cmd.append("--force-yuv")
        cmd += ["-f", str(out)]
        if rc.resolution:
            # wf-recorder geometry flag
            pass  # resolution is auto by default; geometry can be set with -g
        target = _wf_capture_target(rc, self.display_override)
        if target:
            cmd += ["-o", target]
        audio_device = _wf_audio_device(rc)
        if audio_device:
            cmd += [f"--audio={audio_device}"]
        # Use ffmpeg codec flags via wf-recorder's -c option
        codec = self._encoder
        if codec in ("h264_nvenc", "hevc_nvenc"):
            cmd += ["-c", codec]
        elif codec == "h264_vaapi":
            cmd += ["-c", "h264_vaapi", "-d", "/dev/dri/renderD128"]
        else:
            cmd += ["-c", "libx264"]
        # H.264 is the only codec wf-recorder gets here, and it has no 10-bit
        # hardware path, so only ask for it on the HEVC encoder.
        if _color_depth(rc) == "10" and codec == "hevc_nvenc" and _wf_supports_flag("--pixel-format"):
            cmd += ["-x", "p010le"]
        return cmd

    def _ffmpeg_x11_cmd(self, out: Path) -> list[str]:
        rc = self.cfg.recording
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning"]
        input_args, extra_filter = _ffmpeg_x11_input_args(rc, self.display_override)
        cmd += input_args
        cmd += _ffmpeg_audio_input_args(rc)

        enc_flags = _merge_ffmpeg_filters(
            _encoder_flags(self._encoder, rc.crf, _color_depth(rc)), extra_filter
        )
        cmd += enc_flags

        cmd += _ffmpeg_audio_output_args(rc)

        cmd += ["-y", str(out)]
        return cmd

    @staticmethod
    def _detect_x11_resolution() -> Optional[str]:
        return _detect_x11_resolution()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        log.info(
            "Starting segment recorder (backend=%s, encoder=%s)",
            "wf-recorder" if self._use_wf else "ffmpeg-x11grab",
            self._encoder,
        )
        self._running = True
        self._last_segment_error = None
        self._loop_task = asyncio.create_task(self._record_loop())
        deadline = time.monotonic() + 1.2
        while time.monotonic() < deadline:
            await asyncio.sleep(0.05)
            if self._last_segment_error:
                await self.stop()
                raise RuntimeError(f"{self.name} failed to start: {self._last_segment_error}")
            if self._loop_task and self._loop_task.done():
                exc = self._loop_task.exception()
                await self.stop()
                detail = str(exc) if exc else "recorder loop exited during startup"
                raise RuntimeError(f"{self.name} failed to start: {detail}")

    async def stop(self) -> None:
        self._running = False
        if self._current_proc:
            proc = self._current_proc
            self._current_proc = None
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                    await proc.wait()
                except ProcessLookupError:
                    pass
            except ProcessLookupError:
                pass
            except Exception as exc:
                log.warning("Error while stopping segment recorder process: %s", exc)
        if self._loop_task:
            self._loop_task.cancel()
            self._loop_task = None

    async def _record_loop(self) -> None:
        while self._running:
            idx = self._seg_index % MAX_SEGMENTS
            seg_path = self._seg_dir / f"seg{idx:04d}.mp4"
            # Remove old file at this slot before overwriting
            if seg_path.exists():
                seg_path.unlink()

            start_ts = time.time()

            if self._use_wf:
                cmd = self._wf_recorder_cmd(seg_path)
            else:
                cmd = self._ffmpeg_x11_cmd(seg_path)

            log.debug("Segment %d: %s", self._seg_index, " ".join(cmd))

            timed_out = False
            stderr_text = ""
            try:
                self._current_proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE if self._use_wf else asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(
                    self._current_proc.wait(), timeout=SEGMENT_DURATION
                )
            except asyncio.TimeoutError:
                timed_out = True
                # Normal: segment duration elapsed, kill and move on
                if self._current_proc:
                    self._current_proc.terminate()
                    try:
                        await asyncio.wait_for(self._current_proc.wait(), timeout=3)
                    except asyncio.TimeoutError:
                        try:
                            self._current_proc.kill()
                        except ProcessLookupError:
                            pass
            except asyncio.CancelledError:
                if self._current_proc:
                    try:
                        self._current_proc.terminate()
                    except ProcessLookupError:
                        pass
                return
            except Exception as exc:
                log.error("Recorder error: %s", exc)
                await asyncio.sleep(1)
                continue

            proc = self._current_proc
            self._current_proc = None
            if proc:
                stderr_text = await _read_stream_text(proc.stderr)

            if seg_path.exists():
                self._segments.append((start_ts, seg_path))
                # Prune old slots that have been overwritten
                self._segments = [
                    (t, p) for t, p in self._segments if p.exists()
                ]
                self._last_segment_error = None
            else:
                detail = _summarize_process_error(
                    "wf-recorder" if self._use_wf else "ffmpeg",
                    proc.returncode if proc else None,
                    stderr_text,
                )
                if detail or not timed_out:
                    self._last_segment_error = detail
                    log.error("Segment recorder did not produce output: %s", detail)
                    if self._seg_index == 0:
                        self._running = False
                        return

            self._seg_index += 1

    # ── Clip extraction ───────────────────────────────────────────────────────

    async def save_clip(self, duration: Optional[int] = None) -> Optional[Path]:
        rc = self.cfg.recording
        clip_duration = int(duration or rc.clip_duration)
        now = time.time()
        clip_start = now - clip_duration

        # Also stop the current segment so we capture up to "now"
        if self._current_proc and self._current_proc.returncode is None:
            self._current_proc.send_signal(signal.SIGINT)
            try:
                await asyncio.wait_for(self._current_proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._current_proc.kill()

        # Refresh segment list
        self._segments = [
            (t, p) for t, p in self._segments if p.exists()
        ]
        if not self._segments:
            if self._last_segment_error:
                log.error("No segments available to clip from (%s)", self._last_segment_error)
            else:
                log.error("No segments available to clip from")
            return None

        # Find segments that overlap the desired clip window
        relevant = [
            (t, p) for t, p in sorted(self._segments)
            if t + SEGMENT_DURATION >= clip_start
        ]
        if not relevant:
            relevant = [self._segments[-1]]

        log.info(
            "Clipping from %d segment(s), target window: last %d s",
            len(relevant),
            clip_duration,
        )

        # Write concat list for ffmpeg
        concat_list = Path("/tmp/vice/concat.txt")
        with concat_list.open("w") as fh:
            for _, seg in relevant:
                fh.write(f"file '{seg}'\n")

        # Calculate offset into the first segment
        first_ts = relevant[0][0]
        skip = max(0.0, clip_start - first_ts)

        out_path = _next_clip_path(
            self._out_dir,
            tag=await self._clip_tag(),
            template=self._clip_name_template(),
        )

        ffmpeg_cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning",
            "-f", "concat", "-safe", "0",
            "-i", str(concat_list),
            "-ss", str(skip),
            "-t", str(clip_duration),
            *KEEP_ALL_STREAMS,
            "-c:v", "copy",
            "-c:a", "copy",
            "-movflags", "+faststart",
            "-y", str(out_path),
        ]

        log.info("Extracting clip: %s", " ".join(ffmpeg_cmd))
        try:
            proc = await asyncio.create_subprocess_exec(
                *ffmpeg_cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            if proc.returncode != 0:
                log.error("ffmpeg clip error: %s", stderr.decode())
                return None
        except asyncio.TimeoutError:
            log.error("ffmpeg timed out during clip extraction")
            return None

        if self.cfg.recording.apply_watermark:
            await _apply_watermark(out_path)
        log.info("Clip saved: %s", out_path)
        self._emit(out_path)
        return out_path


# ──────────────────────────────────────────────────────────────────────────────
# Factory
# ──────────────────────────────────────────────────────────────────────────────

def _wf_requires_compat_backend(cfg: Config) -> bool:
    rc = cfg.recording
    return bool(rc.capture_audio and rc.capture_microphone and rc.wf_microphone_strategy == "backend_fallback")


def _wf_requires_user_choice(cfg: Config) -> bool:
    rc = cfg.recording
    return bool(rc.capture_audio and rc.capture_microphone and rc.wf_microphone_strategy == "prompt")


def _create_wf_compatible_recorder(cfg: Config) -> Recorder:
    if gsr_source() is not None:
        log.info("wf-recorder mic mode requested compatible backend; using gpu-screen-recorder")
        return GSRRecorder(cfg)
    if _is_x11() and _has("ffmpeg"):
        log.info("wf-recorder mic mode requested compatible backend; using ffmpeg x11grab")
        return SegmentRecorder(cfg, use_wf_recorder=False)
    raise RuntimeError(
        "Microphone capture with desktop audio is not supported by wf-recorder on this system. "
        "Install gpu-screen-recorder, switch backend, or choose mic-only mode."
    )


def create_recorder(cfg: Config) -> Recorder:
    """
    Instantiate the best available recorder for this system.
    Respects cfg.recording.backend if not 'auto'.
    """
    pref = cfg.recording.backend
    on_wayland = _is_wayland()
    on_x11 = _is_x11()
    set_gsr_source(getattr(cfg.recording, "gsr_source", "auto"))
    has_gsr = gsr_source() is not None
    has_wf = _has("wf-recorder")
    has_ffmpeg = _has("ffmpeg")

    if pref in ("wf-recorder", "ffmpeg"):
        if _container(cfg.recording) != "mp4":
            log.warning(
                "recording.container=%s applies to the gpu-screen-recorder "
                "backend; %s clips stay mp4",
                _container(cfg.recording), pref,
            )
        if getattr(cfg.recording, "audio_tracks", None):
            log.warning(
                "recording.audio_tracks applies to the gpu-screen-recorder "
                "backend; %s records a single mixed track", pref,
            )

    if pref == "wf-recorder" and not on_wayland and not on_x11:
        # wf-recorder is the only backend whose selection actually depends on
        # on_wayland downstream (auto + gsr return GSRRecorder regardless;
        # ffmpeg is X11-explicit). Briefly retry Wayland detection in case a
        # packaged launch raced desktop-session startup env exports.
        for _ in range(5):
            time.sleep(0.2)
            on_wayland = _is_wayland()
            on_x11 = _is_x11()
            if on_wayland or on_x11:
                break

    if pref == "gsr":
        if not has_gsr and not in_vice_flatpak():
            raise RuntimeError(
                "gpu-screen-recorder is selected as the backend, but it is not installed or not on PATH."
            )
        log.info("Selected backend: gpu-screen-recorder")
        return GSRRecorder(cfg)

    if pref == "wf-recorder":
        if not on_wayland:
            raise RuntimeError("wf-recorder requires a Wayland session.")
        if _wf_requires_user_choice(cfg):
            raise RuntimeError(
                "wf-recorder cannot combine desktop audio and microphone until you choose a compatibility mode."
            )
        if _wf_requires_compat_backend(cfg):
            return _create_wf_compatible_recorder(cfg)
        if not has_wf:
            raise RuntimeError(
                "wf-recorder is selected as the backend, but it is not installed or not on PATH."
            )
        log.info("Selected backend: wf-recorder (Wayland segment mode)")
        return SegmentRecorder(cfg, use_wf_recorder=True)

    if pref == "ffmpeg":
        if not on_x11:
            raise RuntimeError("ffmpeg x11grab requires an X11 session.")
        if not has_ffmpeg:
            raise RuntimeError(
                "No supported screen-capture backend found.\n"
                "Install gpu-screen-recorder, wf-recorder, or ffmpeg."
            )
        log.info("Selected backend: ffmpeg x11grab")
        return SegmentRecorder(cfg, use_wf_recorder=False)

    if pref == "auto":
        if has_gsr:
            if not on_wayland and not on_x11:
                log.warning(
                    "Display server env vars not detected (WAYLAND_DISPLAY and DISPLAY both unset). "
                    "GSR will try its own session detection, but if recording fails see "
                    "README troubleshooting for systemd-launched daemons on Hyprland/Sway."
                )
            log.info("Selected backend: gpu-screen-recorder")
            return GSRRecorder(cfg)
        if in_vice_flatpak():
            # Inside the Flatpak the recorder is the GSR Flatpak, which Vice
            # installs from its setup step. The daemon has to stay up for that,
            # so the recorder is created and reports why it cannot start.
            log.warning("The gpu-screen-recorder Flatpak is not installed yet")
            return GSRRecorder(cfg)
        raise RuntimeError(
            "gpu-screen-recorder is required by Vice's auto-backend mode but is not "
            "installed or not on PATH. Rerun ./install.sh, or set "
            "recording.backend in ~/.config/vice/config.toml to 'wf-recorder' or "
            "'ffmpeg' if you specifically need an alternate backend."
        )

    raise RuntimeError(
        f"Invalid recording.backend value: {pref!r}. "
        "Must be one of: 'auto', 'gsr', 'wf-recorder', 'ffmpeg'."
    )
