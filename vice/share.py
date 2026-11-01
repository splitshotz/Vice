"""
Vice share server, HTTP server that powers:
  • A local control UI/server  (/ → UI, /api/*, /ws, media)
  • A public share-only server  (/c/{token}, /v/{token}, /t/{token})

WebSocket event types (server → client):
  {"type": "clip_saved",   "clip":  <clip_json>}
  {"type": "clip_deleted", "slug":  "..."}
  {"type": "status",       "recording": bool, "backend": "..."}
  {"type": "tunnel_url",   "url":   "https://..."}
  {"type": "tunnel_error", "error": "..."}
  {"type": "playlists_changed", "playlists": [<playlist_json>]}
  {"type": "export_progress", "job_id": "...", "progress": 0.42}
  {"type": "export_done",  "job_id": "...", "path": "...", "clip": <clip_json>|null}
  {"type": "export_error", "job_id": "...", "error": "...", "canceled": bool}
  {"type": "editor_project_changed"}
"""

from __future__ import annotations

import asyncio
import copy
import glob
import html
import json
import logging
import os
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Callable, Coroutine, Optional
from urllib.parse import quote

from importlib.resources import files as _pkg_files

from aiohttp import WSMsgType, web

from . import __version__
from . import keys as keystore
from .editor import (DISCORD_MAX_BYTES, DiscordTooLarge, EditorProjectStore,
                     ExportBusy, ExportManager, KeyBurn, Source, build_export_cmd,
                     canvas_for,
                     default_export_name, discord_bitrates, project_extent,
                     sanitize_export_name, text_file_contents,
                     validate_project)
from .media import communicate_with_timeout, probe_media, probe_media_detailed
from .playlists import (IMAGE_PREFIX, PlaylistStore, build_tag_index,
                        image_slug)
from .recorder import (IMAGE_EXTS, KEEP_ALL_STREAMS, _available_encoders,
                       _is_nvidia, filename_tag, list_display_options,
                       list_gsr_audio_sources, next_image_path,
                       slugify_clip_name)
from .runtime import actual_home_dir, load_user_systemd_env, resolve_path

log = logging.getLogger("vice.share")

# Config maps whose keys are user data rather than field names.
FREE_MAP_KEYS = frozenset({"audio_track_volumes"})
UI_VERSION_TOKEN = "__VICE_VERSION__"


def _resolve_ui_index() -> Path | None:
    """Resolve the web UI index path across installed and source checkouts."""
    candidates: list[Path] = []

    # Preferred path for packaged installs.
    try:
        ui = _pkg_files("vice") / "ui" / "index.html"
        candidates.append(Path(str(ui)))
    except Exception as exc:
        log.debug("importlib.resources lookup for UI index failed: %s", exc)

    # Fallback for source checkouts / direct execution.
    candidates.append(Path(__file__).resolve().parent / "ui" / "index.html")

    for cand in candidates:
        try:
            if cand.exists() and cand.is_file():
                return cand
        except OSError:
            continue
    return None


# UI assets shipped inside the package (bundled, no network needed).
_UI_ASSET_KINDS = {"fonts", "styles", "scripts"}
_UI_CONTENT_TYPES = {
    "fonts":   "font/woff2",
    "styles":  "text/css; charset=utf-8",
    "scripts": "application/javascript; charset=utf-8",
}


_UI_ASSET_REV: Optional[str] = None


def _ui_asset_rev() -> str:
    """Cache key for the bundled UI assets: the version plus a fingerprint of
    the shipped files. Assets are served immutable for a year, so without the
    fingerprint a rebuild that keeps the same version (every test build during
    development) would keep serving the previous build's scripts."""
    global _UI_ASSET_REV
    if _UI_ASSET_REV is not None:
        return _UI_ASSET_REV
    latest = 0
    for kind in _UI_ASSET_KINDS:
        base = _resolve_ui_asset_dir(kind)
        if not base:
            continue
        try:
            for asset in base.iterdir():
                if asset.is_file():
                    st = asset.stat()
                    latest = max(latest, st.st_mtime_ns ^ st.st_size)
        except OSError:
            continue
    _UI_ASSET_REV = f"{__version__}-{latest & 0xffffffff:08x}" if latest else __version__
    return _UI_ASSET_REV


def _resolve_ui_asset_dir(kind: str) -> Path | None:
    if kind not in _UI_ASSET_KINDS:
        return None
    candidates: list[Path] = []
    try:
        candidates.append(Path(str(_pkg_files("vice") / "ui" / kind)))
    except Exception as exc:
        log.debug("importlib.resources lookup for UI dir %s failed: %s", kind, exc)
    candidates.append(Path(__file__).resolve().parent / "ui" / kind)
    for cand in candidates:
        try:
            if cand.is_dir():
                return cand
        except OSError:
            continue
    return None


def _resolve_ui_asset(kind: str, name: str) -> Path | None:
    """Resolve a bundled UI asset (only allows known kinds + simple filenames)."""
    if kind not in _UI_ASSET_KINDS:
        return None
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    candidates: list[Path] = []
    try:
        f = _pkg_files("vice") / "ui" / kind / name
        candidates.append(Path(str(f)))
    except Exception as exc:
        log.debug("importlib.resources lookup for UI asset %s/%s failed: %s", kind, name, exc)
    candidates.append(Path(__file__).resolve().parent / "ui" / kind / name)
    for cand in candidates:
        try:
            if cand.exists() and cand.is_file():
                return cand
        except OSError:
            continue
    return None

# Thumbnails go in the cache dir, separate from the clip files.
THUMB_DIR      = actual_home_dir() / ".cache" / "vice" / "thumbs"
# H.264 preview copies of clips the native WebEngine can't decode (H.265).
PROXY_DIR      = actual_home_dir() / ".cache" / "vice" / "proxies"
# Discord-sized copies live in a hidden folder beside the clip they came from,
# not in ~/.cache: the drag hands another app a path, and a sandboxed one
# (Discord ships as a Flatpak) can only read the folders it was granted. The
# clip's own directory is the one place the user has already opened up, so a
# copy there is readable wherever the original is.
DISCORD_SUBDIR = ".discord"
# Scratch space for editor export jobs (drawtext sidecar files).
EXPORT_WORK_DIR = actual_home_dir() / ".cache" / "vice" / "exports"
HIGHLIGHTS_DIR = actual_home_dir() / ".local" / "share" / "vice" / "highlights"


def _load_highlights(slug: str) -> list:
    f = HIGHLIGHTS_DIR / f"{slug}.json"
    if not f.exists():
        return []
    try:
        return json.loads(f.read_text())
    except Exception as exc:
        log.warning("Highlights file %s is unreadable: %s", f.name, exc)
        return []


def _save_highlights(slug: str, highlights: list) -> None:
    HIGHLIGHTS_DIR.mkdir(parents=True, exist_ok=True)
    (HIGHLIGHTS_DIR / f"{slug}.json").write_text(json.dumps(highlights))


# In-app view counts per slug. Like playlist membership, the counters are
# migrated on rename and dropped on delete so a reused clip number never
# inherits another clip's history.
VIEWS_PATH = actual_home_dir() / ".local" / "share" / "vice" / "views.json"


def _load_views() -> dict[str, int]:
    if not VIEWS_PATH.exists():
        return {}
    try:
        return {str(k): int(v) for k, v in json.loads(VIEWS_PATH.read_text()).items()}
    except Exception as exc:
        log.warning("Views file %s is unreadable: %s", VIEWS_PATH, exc)
        return {}


def _save_views(views: dict[str, int]) -> None:
    # Write-and-rename so a crash mid-write can't truncate the whole file and
    # lose every count (a single JSON file, unlike per-clip highlights).
    VIEWS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = VIEWS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(views))
    tmp.replace(VIEWS_PATH)


# Public links name a clip by a random token rather than its filename. Clip
# names are sequential, so anyone holding one link could reach every other
# clip by editing the number (#222). A token is all it takes to fetch a clip,
# so the file is kept readable by its owner only.
SHARE_TOKENS_PATH = actual_home_dir() / ".local" / "share" / "vice" / "share_tokens.json"


def _load_share_tokens() -> dict[str, str]:
    if not SHARE_TOKENS_PATH.exists():
        return {}
    try:
        data = json.loads(SHARE_TOKENS_PATH.read_text())
        return {str(k): v for k, v in data.items() if isinstance(v, str) and v}
    except Exception as exc:
        # Failing closed: every clip gets a fresh token, so links shared
        # before this point stop working rather than resolving to anything.
        log.warning("Share link file %s is unreadable, issuing new links: %s",
                    SHARE_TOKENS_PATH, exc)
        return {}


def _save_share_tokens(tokens: dict[str, str]) -> None:
    SHARE_TOKENS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = SHARE_TOKENS_PATH.with_suffix(".json.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    # A leftover temp file from a crash keeps its old mode through O_CREAT.
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(json.dumps(tokens))
    tmp.replace(SHARE_TOKENS_PATH)


# Small bag of UI state that must outlive the web view. The native window's
# localStorage does not reliably survive restarts on every QtWebEngine build,
# which made the first-run tutorial reappear every launch.
APP_STATE_PATH = actual_home_dir() / ".local" / "share" / "vice" / "ui_state.json"


def _load_app_state() -> dict:
    if not APP_STATE_PATH.exists():
        return {}
    try:
        data = json.loads(APP_STATE_PATH.read_text())
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        log.warning("UI state file %s is unreadable: %s", APP_STATE_PATH, exc)
        return {}


def _save_app_state(state: dict) -> None:
    APP_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    APP_STATE_PATH.write_text(json.dumps(state))


def _thumb_path(path: Path) -> Path:
    """Return cache path unique to this clip file content/version."""
    try:
        st = path.stat()
        key = f"{path.stem}_{st.st_size}_{st.st_mtime_ns}"
    except OSError:
        key = path.stem
    return THUMB_DIR / f"{key}.jpg"


def _purge_slug_thumbs(slug: str, keep: frozenset[Path] = frozenset()) -> None:
    """Remove any cached thumbs for a slug (legacy + versioned variants)."""
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    for t in THUMB_DIR.glob(f"{glob.escape(slug)}*.jpg"):
        if t not in keep:
            t.unlink(missing_ok=True)


def _proxy_path(path: Path) -> Path:
    """Cache path for a clip's H.264 preview, keyed by file identity so a trim
    or a reused clip number naturally invalidates it (same idea as _thumb_path)."""
    try:
        st = path.stat()
        key = f"{path.stem}_{st.st_size}_{st.st_mtime_ns}"
    except OSError:
        key = path.stem
    # Invalidate previews made before the eight-bit playback fix (#172).
    return PROXY_DIR / f"{key}_v2.mp4"


def _purge_slug_proxies(slug: str, keep: frozenset[Path] = frozenset()) -> None:
    """Remove any cached preview proxies for a slug (all file versions)."""
    PROXY_DIR.mkdir(parents=True, exist_ok=True)
    for pattern in (f"{glob.escape(slug)}*.mp4", f"{glob.escape(slug)}*_audio_*.m4a"):
        for p in PROXY_DIR.glob(pattern):
            if p not in keep:
                p.unlink(missing_ok=True)


def _current_cache_files(path: Path) -> list[Path]:
    """The thumbnail and previews already made for this exact file version."""
    proxy = _proxy_path(path)
    found = [_thumb_path(path), proxy, *PROXY_DIR.glob(f"{glob.escape(proxy.stem)}_audio_*.m4a")]
    return [p for p in found if p.exists()]


def _audio_preview_path(path: Path, index: int) -> Path:
    proxy = _proxy_path(path)
    return proxy.with_name(f"{proxy.stem}_audio_{index}.m4a")


async def _make_audio_preview(path: Path, index: int) -> Path:
    """Browser-readable audio for one recorded stream, keeping its timeline."""
    target = _audio_preview_path(path, index)
    if target.exists() and target.stat().st_size > 0:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix=".audio-", suffix=".m4a",
                                     dir=target.parent, delete=False) as handle:
        tmp = Path(handle.name)
    proc = None
    spawn = None
    try:
        spawn = asyncio.create_task(asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-threads", "2",
            "-i", str(path), "-map", f"0:a:{index}", "-vn",
            "-af", "aresample=48000:async=1:first_pts=0", "-ac", "2",
            "-c:a", "aac", "-b:a", "192k", "-threads", "2",
            "-movflags", "+faststart", "-y", str(tmp),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        ))
        proc = await asyncio.shield(spawn)
        _, stderr = await communicate_with_timeout(proc, timeout=300)
        if proc.returncode != 0 or tmp.stat().st_size == 0:
            reason = (stderr or b"").decode(errors="replace").strip()[-300:]
            raise RuntimeError(reason or "audio preview produced no output")
        if _audio_preview_path(path, index) != target or not path.exists():
            raise RuntimeError("clip changed while preparing its audio")
        tmp.replace(target)
        return target
    finally:
        if proc is None and spawn is not None:
            try:
                proc = await spawn
            except OSError:
                pass  # The spawn error is propagated by the main path.
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.communicate()
        tmp.unlink(missing_ok=True)


def _discord_path(path: Path) -> Path:
    """Where a clip's Discord-sized copy goes, keyed by file identity for the
    same reason as _proxy_path."""
    try:
        st = path.stat()
        key = f"{path.stem}_{st.st_size}_{st.st_mtime_ns}"
    except OSError:
        key = path.stem
    return path.parent / DISCORD_SUBDIR / f"{key}.mp4"


def _purge_clip_discord(path: Optional[Path]) -> None:
    """Remove any Discord copies of *path* (all file versions)."""
    if path is None:
        return
    folder = path.parent / DISCORD_SUBDIR
    if not folder.is_dir():
        return
    for p in folder.glob(f"{glob.escape(path.stem)}*.mp4"):
        p.unlink(missing_ok=True)


# WebEngine plays these without help; anything else gets an H.264 preview proxy.
_WEB_PLAYABLE_VCODECS = {"h264", "avc1", "vp8", "vp9", "av1"}

_IMAGE_MIME = {
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
}


async def copy_image_to_clipboard(path: Path) -> tuple[bool, str]:
    """Put the picture itself on the clipboard, not a link to it.

    Both tools keep owning the selection in the background after forking, so
    the clipboard outlives the request that filled it. Returns the reason on
    failure rather than raising: a screenshot that saved but could not be
    copied is still a screenshot, and the UI says so instead of losing it.
    """
    mime = _IMAGE_MIME.get(path.suffix.lower())
    if not mime:
        return False, f"{path.suffix} is not an image format Vice can copy."
    if shutil.which("wl-copy"):
        cmd = ["wl-copy", "--type", mime]
    elif shutil.which("xclip"):
        cmd = ["xclip", "-selection", "clipboard", "-t", mime]
    else:
        return False, "Copying images needs wl-clipboard (Wayland) or xclip (X11)."

    try:
        data = await asyncio.to_thread(path.read_bytes)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        proc.stdin.write(data)
        await proc.stdin.drain()
        proc.stdin.close()
    except Exception as exc:
        log.warning("Copying %s to the clipboard failed: %s", path.name, exc)
        return False, "Could not reach the clipboard."
    return True, ""


# Encoders to try when a trim has to re-encode, best first, per source codec.
# Software is last on every row, so a machine with no usable hardware encoder
# behaves exactly the way it always has.
_TRIM_ENCODERS = {
    "hevc": ("hevc_nvenc", "hevc_vaapi", "libx265", "libx264"),
    "h265": ("hevc_nvenc", "hevc_vaapi", "libx265", "libx264"),
    "av1":  ("av1_nvenc", "av1_vaapi", "libsvtav1", "libx264"),
}
_TRIM_ENCODERS_DEFAULT = ("h264_nvenc", "h264_vaapi", "libx264")
_VAAPI_RENDER_NODE = "/dev/dri/renderD128"

_trim_encoder_cache: dict[str, list[str]] = {}


def _trim_encoder_candidates(vcodec: str) -> list[str]:
    """Encoders worth trying for a trim of a *vcodec* clip, best first.

    Keeping the clip's own codec matters. Trimming used to rewrite every H.265
    recording as H.264, against the user's own Video codec setting and the file
    size they picked it for (#172). Probing is cached per codec because it
    shells out to ffmpeg and nvidia-smi.
    """
    key = (vcodec or "").lower()
    if key not in _trim_encoder_cache:
        names = _TRIM_ENCODERS.get(key, _TRIM_ENCODERS_DEFAULT)
        installed = _available_encoders()
        # An empty probe is no opinion, so try the whole list rather than none.
        picked = [n for n in names if n in installed] if installed else list(names)
        if not _is_nvidia():
            picked = [n for n in picked if not n.endswith("_nvenc")]
        _trim_encoder_cache[key] = picked or ["libx264"]
    return _trim_encoder_cache[key]


def _remember_trim_encoder(vcodec: str, encoder: str) -> None:
    """Move the encoder that worked to the front, so the next trim of the same
    codec does not open a device that already refused once. ffmpeg being built
    with an encoder says nothing about the driver accepting it."""
    picked = _trim_encoder_cache.get((vcodec or "").lower())
    if picked and encoder in picked:
        picked.remove(encoder)
        picked.insert(0, encoder)


def _trim_encoder_args(encoder: str,
                       bitrate: Optional[int] = None) -> tuple[list[str], list[str]]:
    """(arguments before -i, arguments after) for one trim encoder.

    Quality is per encoder because the scales are not comparable: x265 at 23
    and NVENC at 22 land near x264 at 20, which is what trimming has always
    used. Audio is copied whichever encoder wins, so every recorded track
    survives at its original quality.

    *bitrate* is the source's video bitrate in bits per second. When it is
    known the re-encode is capped at it, so a trim cannot come back larger
    per second than the recording the user tuned (#256). VAAPI's constant QP
    ignores a cap, so it targets the bitrate instead.
    """
    cap = ([] if not bitrate else
           ["-maxrate", str(bitrate), "-bufsize", str(bitrate)])
    if encoder.endswith("_nvenc"):
        return [], ["-c:v", encoder, "-rc", "vbr", "-cq", "22", *cap, "-preset", "p4",
                    "-c:a", "copy"]
    if encoder.endswith("_vaapi"):
        rate = (["-rc_mode", "VBR", "-b:v", str(bitrate), "-maxrate", str(bitrate)]
                if bitrate else ["-qp", "22"])
        return (["-vaapi_device", _VAAPI_RENDER_NODE],
                ["-vf", "format=nv12,hwupload", "-c:v", encoder, *rate,
                 "-c:a", "copy"])
    if encoder == "libsvtav1":
        return [], ["-c:v", encoder, "-crf", "30", *cap, "-preset", "8", "-c:a", "copy"]
    crf = "23" if encoder == "libx265" else "20"
    return [], ["-c:v", encoder, "-preset", "veryfast", "-crf", crf, *cap, "-c:a", "copy"]


def _trimmed_copy_path(path: Path) -> Path:
    """A free name beside *path* for a trimmed copy: "<name>-trim", then
    "<name>-trim-2" and so on."""
    stem = f"{path.stem}-trim"
    candidate = path.with_name(f"{stem}{path.suffix}")
    n = 2
    while candidate.exists():
        candidate = path.with_name(f"{stem}-{n}{path.suffix}")
        n += 1
    return candidate


def _key_burn_for(project: dict, sources: dict, work: Path, accent: str,
                  layout: str = "qwerty") -> Optional[KeyBurn]:
    """The keyboard panel for an export, or None when no clip in it has keys
    or the panel cannot be drawn here."""
    intervals = {}
    for cid in sources:
        kept = keystore.load_sidecar(cid)
        if kept is not None:
            intervals[cid] = kept
    if not intervals:
        return None
    _, height, _ = canvas_for(project, sources)
    try:
        from .key_overlay import render_burn_assets
        assets = render_burn_assets(work, accent, max(0.5, height / 1080), layout)
    except Exception as exc:  # pycairo missing, or a drawing failure
        log.warning("Exporting without the keyboard panel: %s", exc)
        return None
    return KeyBurn(panel=assets["panel"], size=assets["size"], keys=assets["keys"],
                   intervals=intervals)


async def _video_bitrate(path: Path) -> Optional[int]:
    """The first video stream's bitrate in bits per second, or None."""
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=bit_rate", "-of", "csv=p=0", str(path),
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await communicate_with_timeout(proc, timeout=60)
    except (asyncio.TimeoutError, OSError) as exc:
        log.debug("Bitrate probe of %s failed: %s", path.name, exc)
        return None
    try:
        rate = int((out or b"").decode(errors="replace").strip().split("\n")[0])
    except ValueError:
        return None
    return rate if rate > 0 else None


async def _first_video_packet(path: Path) -> Optional[tuple[float, str]]:
    """(timestamp, flags) of the first video packet, or None when ffprobe
    cannot say.

    Deliberately unanchored. Asking for the packet at timestamp zero misses it
    entirely on a stream copy, because ffmpeg expresses "start here" with a
    leading edit list and timestamps that begin below zero, and ffprobe then
    answers with nothing at all (#172).
    """
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0", str(path),
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await communicate_with_timeout(proc, timeout=60)
    except (asyncio.TimeoutError, OSError) as exc:
        log.debug("Packet probe of %s failed: %s", path.name, exc)
        return None
    if proc.returncode != 0:
        return None
    first = (out or b"").decode(errors="replace").strip().split("\n")[0]
    pts, _, flags = first.partition(",")
    try:
        return float(pts), flags
    except ValueError:
        return None


async def _decode_complaint(path: Path) -> Optional[str]:
    """What a decoder says when asked for a picture from the start of *path*,
    or None when it produces one without complaining. ffmpeg reports a missing
    reference picture on stderr and still exits zero, so the output is the
    answer, not the exit code."""
    cmd = [
        "ffmpeg", "-v", "error", "-i", str(path),
        "-map", "0:v:0", "-frames:v", "1", "-f", "null", "-",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await communicate_with_timeout(proc, timeout=60)
    except (asyncio.TimeoutError, OSError) as exc:
        log.debug("Decode probe of %s failed: %s", path.name, exc)
        return None
    complaint = (stderr or b"").decode(errors="replace").strip()
    if proc.returncode == 0 and not complaint:
        return None
    return complaint.splitlines()[0] if complaint else "ffmpeg could not read it"


async def _xdg_open(target: Path) -> None:
    """Hand *target* to the desktop's default handler, and log why when it
    fails.

    The daemon can outlive the environment it started with, so the desktop's
    variables are refreshed from the session first. xdg-open picks its opener
    from them, and on Plasma a missing KDE_SESSION_VERSION sends it to a
    kfmclient that no longer exists (#252).
    """
    try:
        await asyncio.to_thread(load_user_systemd_env)
        proc = await asyncio.create_subprocess_exec(
            "xdg-open", str(target),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await communicate_with_timeout(proc, timeout=30)
    except (asyncio.TimeoutError, OSError) as exc:
        log.warning("xdg-open %s failed: %s", target, exc)
        return
    complaint = (stderr or b"").decode(errors="replace").strip()
    if proc.returncode or complaint:
        log.warning("xdg-open %s exited %s: %s", target, proc.returncode,
                    complaint or "no output")


async def _trim_result_problem(path: Path) -> str:
    """Why the trimmed *path* would not play in the app window, or "" when it
    will.

    A trimmed clip has to start the way an untrimmed one does, and two shapes
    get past every other check. A stream copy that begins between keyframes
    leaves a track whose frames refer back to a picture the file does not
    contain. A copy that begins on a leading edit list only plays where the
    player honours edit lists. Both probe fine, both play in mpv and VLC, and
    both are a black frame with the duration filled in and no error at all in
    the app window (#172).

    An unanswerable probe returns "", because keeping the copy is better than
    re-encoding on a guess.
    """
    packet = await _first_video_packet(path)
    if packet is None:
        return ""
    pts, flags = packet
    if "K" not in flags:
        return "starts between keyframes"
    if pts < 0:
        return "starts on an edit list before zero"
    complaint = await _decode_complaint(path)
    if complaint:
        return f"cannot decode its first frame ({complaint})"
    return ""


_PREVIEW_TIMEOUT = 300


async def _make_preview_proxy(path: Path, vcodec: str) -> Optional[Path]:
    """Return an H.264 copy of *path* for in-app playback, transcoding once and
    caching it. Returns None when the source is already web-playable or the
    transcode fails, so the caller can just serve the original."""
    if vcodec and vcodec in _WEB_PLAYABLE_VCODECS:
        return None
    proxy = _proxy_path(path)
    if proxy.exists() and proxy.stat().st_size > 0:
        return proxy

    PROXY_DIR.mkdir(parents=True, exist_ok=True)
    tmp = proxy.with_suffix(".mp4.tmp")
    # Same duration and fps as the source so trim in/out points map 1:1 to the
    # original file, which is what the trim endpoint actually cuts.
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-threads", "2", "-filter_threads", "1",
        "-i", str(path),
        "-map", "0:v:0?", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-threads:v", "2",
        "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart",
        # The temp name ends in .tmp, so name the container explicitly.
        "-f", "mp4",
        "-y", str(tmp),
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await communicate_with_timeout(proc, timeout=_PREVIEW_TIMEOUT)
        if proc.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
            log.warning("preview proxy for %s failed: %s", path.name,
                        (stderr or b"").decode(errors="replace")[:200])
            return None
        tmp.replace(proxy)
        return proxy
    except asyncio.TimeoutError:
        log.warning("preview proxy for %s timed out after %ss", path.name, _PREVIEW_TIMEOUT)
        return None
    except OSError as exc:
        log.warning("preview proxy for %s errored: %s", path.name, exc)
        return None
    finally:
        tmp.unlink(missing_ok=True)


_DISCORD_TIMEOUT = 600


async def _make_discord_copy(path: Path, duration: float,
                            audio_streams: int = 1) -> Optional[Path]:
    """Return a copy of *path* small enough to drop into a Discord chat.

    The source itself when it already fits, a cached H.264 transcode when it
    does not, and None when the transcode fails. Raises DiscordTooLarge when
    no bitrate fits the clip under the cap. Same bitrate budget as the
    editor's "optimize for Discord" export, so the two produce comparable
    files.
    """
    try:
        already_small = (path.suffix.lower() == ".mp4"
                         and path.stat().st_size <= DISCORD_MAX_BYTES)
    except OSError:
        return None
    if already_small:
        return path

    out = _discord_path(path)
    if out.exists() and 0 < out.stat().st_size <= DISCORD_MAX_BYTES:
        return out

    video_kbps, audio_kbps = discord_bitrates(duration, audio_streams)

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".mp4.tmp")
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", str(path),
        "-map", "0:v:0?", "-map", "0:a?",
        # 1440p at a Discord-sized bitrate looks worse than the same bits
        # spent on 1080p, and Discord's inline player tops out there anyway.
        # The pad rounds both sides up: yuv420p has no odd dimension, and
        # libx264 refuses the frame rather than fixing it (a 127x73 source
        # scaled to 128x73 failed outright).
        "-vf", "scale=-2:min(1080\\,ih),pad=ceil(iw/2)*2:ceil(ih/2)*2",
        "-c:v", "libx264", "-preset", "veryfast",
        "-b:v", f"{video_kbps}k",
        "-maxrate", f"{round(video_kbps * 1.45)}k",
        "-bufsize", f"{video_kbps * 2}k",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", f"{audio_kbps}k",
        "-movflags", "+faststart",
        "-f", "mp4",
        "-y", str(tmp),
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await communicate_with_timeout(proc, timeout=_DISCORD_TIMEOUT)
        if proc.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
            log.warning("Discord copy of %s failed: %s", path.name,
                        (stderr or b"").decode(errors="replace")[:200])
            return None
        # Single-pass ABR aims at the budget, it does not promise it. Never
        # hand out or cache a copy Discord would reject.
        size = tmp.stat().st_size
        if size > DISCORD_MAX_BYTES:
            log.warning("Discord copy of %s came out at %.1f MB, over the cap",
                        path.name, size / 1048576)
            return None
        tmp.replace(out)
        return out
    except asyncio.TimeoutError:
        log.warning("Discord copy of %s timed out after %ss", path.name,
                    _DISCORD_TIMEOUT)
        return None
    except OSError as exc:
        log.warning("Discord copy of %s errored: %s", path.name, exc)
        return None
    finally:
        # communicate_with_timeout has already killed and reaped the encoder,
        # so nothing is still writing to this file.
        tmp.unlink(missing_ok=True)


# ── helpers ──────────────────────────────────────────────────────────────────

def _local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


_PROBE_DEFAULTS = {"width": 1920, "height": 1080, "duration": 0, "vcodec": ""}


async def _remux_moov(path: Path) -> bool:
    """Try to recover `path` via `ffmpeg -c copy -movflags +faststart`.

    Used for clips whose MP4 container is damaged (no moov atom), happens
    when the encoder was killed mid-finalize. The original file is only
    replaced when the remuxed copy probes as a sane video of comparable
    size; a remux that produces a near-empty file means the input was
    *not* a simple moov-atom problem, and replacing would destroy data
    (this used to truncate healthy clips to 0.02 s). Returns True when
    the remuxed file replaced the original.
    """
    tmp = path.with_suffix(path.suffix + ".fix.mp4")
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(path), *KEEP_ALL_STREAMS, "-c", "copy",
            "-movflags", "+faststart", str(tmp),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await communicate_with_timeout(proc, timeout=60)
        if proc.returncode == 0 and tmp.exists():
            remuxed = await probe_media(tmp)
            orig_size = path.stat().st_size
            if (
                remuxed
                and remuxed["duration"] > 0
                and tmp.stat().st_size >= orig_size * 0.5
            ):
                tmp.replace(path)
                return True
            log.warning(
                "Remux of %s produced an invalid or truncated file "
                "(duration=%.2fs, %d → %d bytes), keeping the original",
                path.name,
                remuxed["duration"] if remuxed else 0.0,
                orig_size,
                tmp.stat().st_size,
            )
    except Exception as exc:
        log.warning("Remux of %s failed: %s", path.name, exc)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError as exc:
            log.debug("Could not remove the remux temp file %s: %s", tmp.name, exc)
    return False


async def _ffprobe(path: Path) -> dict:
    """Return {"width", "height", "duration"} via ffprobe.

    If the file cannot be probed at all, its container is probably missing
    the moov atom, so try one (validated, non-destructive) remux and re-probe
    before giving up.

    A file that still cannot be read comes back carrying "unreadable" and the
    reason, so the gallery can say so. It used to fall back to the defaults
    and be listed as an ordinary clip of 0:00 with no thumbnail, which is
    what a broken clip looks like to the person who recorded it (#154).
    """
    meta, why = await probe_media_detailed(path)
    if meta and meta["duration"] > 0:
        return meta
    if path.suffix.lower() != ".mp4":
        # The remux below repairs MP4 moov atoms; other containers are
        # served as-is.
        return meta or _unreadable_meta(why)
    log.warning("ffprobe cannot read %s (%s), attempting moov remux",
                path.name, why or "it reads, but reports no duration")
    if await _remux_moov(path):
        meta, why = await probe_media_detailed(path)
        log.info(
            "Remuxed %s, duration now %.2fs",
            path.name, meta["duration"] if meta else 0.0,
        )
    return meta or _unreadable_meta(why)


def _unreadable_meta(reason: str) -> dict:
    meta = dict(_PROBE_DEFAULTS)
    meta["unreadable"] = True
    meta["unreadable_reason"] = reason or "ffprobe could not read this file"
    return meta


async def _make_thumb(path: Path, duration: float = 0.0) -> Path:
    """Lazily generate a 640px-wide JPEG thumbnail stored in THUMB_DIR.

    Short clips (< 1 s) used to come back blank because `-ss 0.75` seeks
    past EOF and `-vf thumbnail` needs a 100-frame lookahead. Now we seek
    to `min(duration/2, 0.75)` and use a plain scale filter, which works
    on sub-second clips too.
    """
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    thumb = _thumb_path(path)
    if thumb.exists() and thumb.stat().st_size > 0:
        return thumb
    if duration and duration > 0:
        seek_ts = min(duration / 2.0, 0.75)
    else:
        seek_ts = 0.0
    # Publish only complete images. Concurrent requests get separate temporary
    # files, so cancellation cannot delete another request's finished thumbnail.
    with tempfile.NamedTemporaryFile(
        prefix=f".{thumb.stem}.", suffix=".jpg", dir=THUMB_DIR, delete=False,
    ) as handle:
        tmp = Path(handle.name)
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-ss", f"{seek_ts:.3f}",
            "-i", str(path),
            "-frames:v", "1",
            "-vf", "scale=640:-2",
            "-q:v", "4",
            "-y", str(tmp),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await communicate_with_timeout(proc, timeout=20)
        if proc.returncode == 0 and tmp.stat().st_size > 0:
            tmp.replace(thumb)
    except Exception as exc:
        log.debug("Thumbnail generation failed for %s: %s", path.name, exc)
    finally:
        tmp.unlink(missing_ok=True)
    return thumb


# Discord can use the Twitter player metadata to skip its thumbnailing pass
# for direct video links. Keep the OpenGraph metadata as the fallback used by
# other unfurlers (issues #77, #100, #207).
_EMBED_PAGE = """\
<!DOCTYPE html>
<html><head>
  <meta charset="utf-8">
  <meta name="theme-color"              content="{color}">
  <meta name="twitter:card"             content="player">
  <meta name="twitter:player"           content="{video_url}">
  <meta name="twitter:player:stream"    content="{video_url}">
  <meta name="twitter:player:stream:content_type" content="{video_type}">
  <meta name="twitter:image"            content="{thumb_url}">
  <meta property="og:site_name"         content="Vice">
  <meta property="og:type"              content="video.other">
  <meta property="og:url"               content="{page_url}">
  <meta property="og:title"             content="{title}">
  <meta property="og:description"       content="Clipped with Vice on Linux">
  <meta property="og:video"             content="{video_url}">
  <meta property="og:video:url"         content="{video_url}">
  <meta property="og:video:secure_url"  content="{video_url}">
  <meta property="og:video:type"        content="{video_type}">
  <meta property="og:video:width"       content="{width}">
  <meta property="og:video:height"      content="{height}">
  <meta property="og:image"             content="{thumb_url}">
  <title>{title}</title>
  <style>
    body{{margin:0;background:#000;display:flex;align-items:center;
         justify-content:center;min-height:100vh}}
    video{{max-width:100%;max-height:100vh}}
  </style>
</head>
<body>
  <video src="{video_url}" controls autoplay muted loop></video>
</body></html>
"""


# cloudflared prints its own infrastructure hostnames alongside the tunnel
# address. api.trycloudflare.com winning the match meant every share link
# pointed at Cloudflare's API, which answers "Method Not Allowed" (#143).
_TRYCLOUDFLARE_RE = re.compile(r"https://([a-zA-Z0-9-]+)\.trycloudflare\.com")
_NOT_A_TUNNEL = {"api", "www", "dash", "developers", "blog"}
_TUNNEL_RETRY_INITIAL = 5.0
_TUNNEL_RETRY_MAX = 300.0


def _cloudflared_failure_detail(lines: list[str]) -> str:
    """Return a short actionable cloudflared diagnostic from its output."""
    relevant = [
        line.strip()
        for line in lines
        if any(marker in line.lower() for marker in ("err", "error", "failed", "unable"))
    ]
    if not relevant:
        return ""
    detail = " | ".join(relevant[-3:])
    return " ".join(detail.split())[:600]


def _quick_tunnel_url(line: str) -> Optional[str]:
    """The quick-tunnel address in a line of cloudflared output, or None.

    Quick tunnels get multi-word hyphenated subdomains, so a hyphenless host
    is ranked last rather than rejected: a naming change at Cloudflare should
    cost a worse guess, not a broken feature.
    """
    fallback: Optional[str] = None
    for match in _TRYCLOUDFLARE_RE.finditer(line):
        if match.group(1).lower() in _NOT_A_TUNNEL:
            continue
        if "-" in match.group(1):
            return match.group(0)
        if fallback is None:
            fallback = match.group(0)
    return fallback


# ── share server ─────────────────────────────────────────────────────────────

class ShareServer:
    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self._local_app = web.Application()
        self._public_app = web.Application()
        self._local_runner: Optional[web.AppRunner] = None
        self._local_site: Optional[web.TCPSite] = None
        self._public_runner: Optional[web.AppRunner] = None
        self._public_site: Optional[web.TCPSite] = None
        self._legacy_public_site: Optional[web.TCPSite] = None

        # slug → Path  (populated from disk on start + runtime additions)
        self._clips: dict[str, Path] = {}
        # slug → {width, height, duration}
        self._meta:  dict[str, dict] = {}
        # Screenshots, kept in their own index. Slugs are filename stems like a
        # clip's, and they can collide with one, so nothing may look an image
        # up in _clips or the other way round.
        self._images: dict[str, Path] = {}
        # Slugs with a trim in flight. A trim can run for minutes when it has
        # to re-encode, and a second one on the same clip would write the same
        # .trimming file and read the original while the first replaces it,
        # which leaves a corrupt clip (#281).
        self._trimming: set[str] = set()

        self.playlists = PlaylistStore()
        self._views = _load_views()
        self._share_tokens = _load_share_tokens()
        self._share_slugs = {token: slug for slug, token in self._share_tokens.items()}
        self.editor_project = EditorProjectStore()
        self._exports = ExportManager(self.broadcast)

        # One encoder across the library, shared by preview proxies and
        # Discord copies. Different clips used to spawn independent encoders,
        # each allocating its own frame queues (#193).
        self._proxy_lock = asyncio.Lock()
        self._proxy_tasks: set[asyncio.Task] = set()
        self._proxy_stopping = False

        self._tunnel_proc: Optional[asyncio.subprocess.Process] = None
        self._tunnel_task: Optional[asyncio.Task] = None
        self._tunnel_stopping = False
        self._tunnel_url:  Optional[str] = None
        self._local_base_url: Optional[str] = None
        self._public_bind_url: Optional[str] = None
        self._legacy_public_bind_url: Optional[str] = None

        # Connected WebSocket clients
        self._ws_clients: set[web.WebSocketResponse] = set()

        # Injected by ViceDaemon so /api/trigger works
        self.trigger_clip_cb: Optional[Callable[[], Coroutine]] = None
        # Injected so the Settings "Check now" button can skip the daily wait.
        self.check_update_cb: Optional[Callable[[], Coroutine]] = None
        # Injected so /api/status can report live state
        self.get_status_cb: Optional[Callable[[], dict]] = None
        # Injected so config changes can be applied without restart when possible.
        self.apply_config_cb: Optional[Callable[[], Coroutine]] = None
        # Injected so Settings can put the keyboard overlay into move mode.
        self.key_overlay_move_cb: Optional[Callable[[bool], None]] = None

        self._setup_local_routes()
        self._setup_public_routes()

    # ── routes ───────────────────────────────────────────────────────────────

    def _setup_local_routes(self) -> None:
        r = self._local_app.router

        # Web UI
        r.add_get("/",            self._ui)
        for _kind in _UI_ASSET_KINDS:
            r.add_get(f"/{_kind}/{{name}}",
                      lambda req, k=_kind: self._ui_asset(req, kind=k))

        # Discord embed pages
        r.add_get("/c/{ref}",     self._embed_page)

        # Media
        r.add_get("/v/{ref}",     self._video)
        r.add_get("/t/{ref}",     self._thumb)
        # Images, local only. There is no share link for a screenshot, so
        # these have no counterpart on the public app below.
        r.add_get("/i/{slug}",    self._image_file)
        r.add_get("/it/{slug}",   self._image_thumb)

        # REST
        r.add_get("/api/clips",              self._api_clips)
        r.add_get("/api/clips/{slug}",       self._api_clip_info)
        r.add_delete("/api/clips/{slug}",    self._api_delete)
        r.add_post("/api/clips/{slug}/trim",              self._api_trim)
        r.add_post("/api/clips/{slug}/rename",            self._api_rename)
        r.add_post("/api/clips/{slug}/reveal",            self._api_reveal)
        r.add_post("/api/clips/{slug}/open",              self._api_open)
        r.add_post("/api/clips/{slug}/copy-file",         self._api_copy_file)
        r.add_get("/api/clips/{slug}/discord",            self._api_discord_copy)
        r.add_post("/api/clips/{slug}/frame",             self._api_save_frame)
        r.add_get("/api/clips/{slug}/audio/{index}",      self._audio_track)
        r.add_get("/api/app-state",                       self._api_get_app_state)
        r.add_post("/api/app-state",                      self._api_set_app_state)
        r.add_post("/api/clips/{slug}/view",              self._api_view)
        r.add_get("/api/clips/{slug}/highlights",         self._api_get_highlights)
        r.add_get("/api/clips/{slug}/keys",               self._api_get_keys)
        r.add_post("/api/overlay/move",                   self._api_overlay_move)
        r.add_post("/api/clips/{slug}/highlights",        self._api_add_highlight)
        r.add_patch("/api/clips/{slug}/highlights/{hid}", self._api_patch_highlight)
        r.add_delete("/api/clips/{slug}/highlights/{hid}",self._api_del_highlight)
        r.add_get("/api/editor/project",              self._api_editor_get_project)
        r.add_post("/api/editor/project",             self._api_editor_save_project)
        r.add_post("/api/editor/export",              self._api_editor_export)
        r.add_post("/api/editor/export/{jid}/cancel", self._api_editor_export_cancel)
        r.add_get("/api/images",                self._api_images)
        r.add_delete("/api/images/{slug}",      self._api_image_delete)
        r.add_post("/api/images/{slug}/rename", self._api_image_rename)
        r.add_post("/api/images/{slug}/reveal", self._api_image_reveal)
        r.add_post("/api/images/{slug}/open",   self._api_image_open)
        r.add_post("/api/images/{slug}/copy",   self._api_image_copy)
        r.add_post("/api/images/{slug}/annotate", self._api_image_annotate)
        r.add_get("/api/playlists",            self._api_playlists)
        r.add_post("/api/playlists",           self._api_create_playlist)
        r.add_patch("/api/playlists/{pid}",    self._api_patch_playlist)
        r.add_delete("/api/playlists/{pid}",   self._api_delete_playlist)
        r.add_post("/api/playlists/{pid}/clips",           self._api_playlist_add_clip)
        r.add_delete("/api/playlists/{pid}/clips/{slug}",  self._api_playlist_remove_clip)
        r.add_get("/api/config",               self._api_get_config)
        r.add_get("/api/displays",             self._api_get_displays)
        r.add_get("/api/audio-sources",        self._api_get_audio_sources)
        r.add_post("/api/config",              self._api_set_config)
        r.add_get("/api/status",               self._api_status)
        r.add_post("/api/update/check",         self._api_check_update)
        r.add_post("/api/trigger",             self._api_trigger)
        r.add_post("/api/quit",                self._api_quit)
        r.add_post("/api/uninstall",           self._api_uninstall)

        # WebSocket
        r.add_get("/ws", self._ws_handler)

    def _setup_public_routes(self) -> None:
        r = self._public_app.router
        r.add_get("/c/{ref}", self._public_embed_page)
        r.add_get("/v/{ref}", self._public_video)
        r.add_get("/t/{ref}", self._public_thumb)

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        # Pre-populate from output dir
        out_dir = resolve_path(self.cfg.output.directory)
        if out_dir.exists():
            media = list(out_dir.glob("*.mp4")) + list(out_dir.glob("*.mkv"))
            for clip in sorted(media, key=lambda p: p.stat().st_mtime):
                self._clips[clip.stem] = clip
        self._ensure_share_tokens()
        self.rescan_images()
        # Both indexes in one call. backfill drops membership for anything not
        # in the set it is given, so handing it the clips alone would empty
        # every playlist of its screenshots on the next start.
        self.playlists.backfill(
            set(self._clips) | {image_slug(s) for s in self._images},
            build_tag_index(self.cfg.discord.custom_games),
            seed_auto=self.cfg.output.auto_playlist_by_game,
        )
        # View counts persist like highlights: they are only dropped when a
        # clip is deleted (in _api_delete) or its number is reused by a new
        # recording (in add_clip). They are never purged against the startup
        # scan, which wiped valid counts when the output dir was slow to mount.

        local_port = self.cfg.sharing.port
        public_port = self.cfg.sharing.public_port or (local_port + 1)
        public_host = _local_ip()

        self._local_runner = web.AppRunner(self._local_app, access_log=None)
        await self._local_runner.setup()
        self._local_site = web.TCPSite(self._local_runner, "127.0.0.1", local_port)
        await self._local_site.start()

        self._public_runner = web.AppRunner(self._public_app, access_log=None)
        await self._public_runner.setup()
        self._public_site = web.TCPSite(self._public_runner, "0.0.0.0", public_port)
        await self._public_site.start()

        self._local_base_url = f"http://127.0.0.1:{local_port}"
        self._public_bind_url = f"http://{public_host}:{public_port}"
        log.info("Vice local control UI: %s", self._local_base_url)
        log.info("Vice public share server: %s", self._public_bind_url)
        if self.cfg.sharing.base_url:
            log.info("Vice public base URL override: %s", self.cfg.sharing.base_url)
        elif public_host not in {"127.0.0.1", "0.0.0.0"}:
            try:
                self._legacy_public_site = web.TCPSite(self._public_runner, public_host, local_port)
                await self._legacy_public_site.start()
                self._legacy_public_bind_url = f"http://{public_host}:{local_port}"
                log.info(
                    "Vice legacy share compatibility URL: %s",
                    self._legacy_public_bind_url,
                )
            except OSError as exc:
                log.warning(
                    "Failed to enable legacy share compatibility on %s:%d: %s",
                    public_host,
                    local_port,
                    exc,
                )

        if self.cfg.sharing.cloudflare_tunnel:
            await self._start_tunnel(public_port)

    async def stop(self) -> None:
        self._proxy_stopping = True
        tasks = list(self._proxy_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self._exports.stop()
        for ws in list(self._ws_clients):
            try:
                await ws.close()
            except Exception as exc:
                log.debug("A websocket client did not close cleanly: %s", exc)
        self._tunnel_stopping = True
        tunnel_task = getattr(self, "_tunnel_task", None)
        if tunnel_task:
            tunnel_task.cancel()
            await asyncio.gather(tunnel_task, return_exceptions=True)
            self._tunnel_task = None
        elif getattr(self, "_tunnel_proc", None):
            await self._stop_tunnel_process(self._tunnel_proc)
        if self._local_runner:
            await self._local_runner.cleanup()
        if self._public_runner:
            await self._public_runner.cleanup()

    # ── public helpers (called by ViceDaemon) ─────────────────────────────────

    def add_clip(self, path: Path, game: Optional[str] = None) -> str:
        """Register a new clip and return its share URL."""
        slug = path.stem
        self._clips[slug] = path
        self._meta.pop(slug, None)
        # A fresh recording under a reused clip number must not inherit the
        # old clip's view count, nor its link: anyone still holding the old
        # one would be shown the new recording.
        if self._views.pop(slug, None) is not None:
            _save_views(self._views)
        self._issue_share_token(slug)
        if (game and self.cfg.output.auto_playlist_by_game
                and self.playlists.record_auto(game, slug)):
            asyncio.create_task(self._broadcast_playlists())
        asyncio.create_task(self.broadcast({
            "type": "clip_saved",
            "clip": self._clip_json(slug, path, {}),
        }))
        asyncio.create_task(self._broadcast_clip(slug, path))
        return self.share_url(slug)

    # ── share tokens ──────────────────────────────────────────────────────────

    def _new_share_token(self) -> str:
        token = secrets.token_urlsafe(9)
        while token in self._share_slugs:
            token = secrets.token_urlsafe(9)
        return token

    def _ensure_share_tokens(self) -> None:
        """Give every clip in the library a token, in one write.

        Tokens for clips that are missing are kept, not pruned: a clips folder
        on a drive that is not mounted yet would otherwise lose every link.
        A recording that reuses a clip number gets a fresh token in add_clip,
        which is the case that matters.
        """
        missing = [slug for slug in self._clips if slug not in self._share_tokens]
        for slug in missing:
            token = self._new_share_token()
            self._share_tokens[slug] = token
            self._share_slugs[token] = slug
        if missing:
            _save_share_tokens(self._share_tokens)

    def _issue_share_token(self, slug: str) -> str:
        """Give *slug* a new token, retiring any link it had before."""
        old = self._share_tokens.get(slug)
        if old is not None:
            self._share_slugs.pop(old, None)
        token = self._new_share_token()
        self._share_tokens[slug] = token
        self._share_slugs[token] = slug
        _save_share_tokens(self._share_tokens)
        return token

    def _move_share_token(self, slug: str, new_slug: str) -> None:
        """Keep a shared link working when its clip is renamed."""
        token = self._share_tokens.pop(slug, None)
        if token is None:
            return
        stale = self._share_tokens.get(new_slug)
        if stale is not None:
            self._share_slugs.pop(stale, None)
        self._share_tokens[new_slug] = token
        self._share_slugs[token] = new_slug
        _save_share_tokens(self._share_tokens)

    def _forget_share_token(self, slug: str) -> None:
        token = self._share_tokens.pop(slug, None)
        if token is not None:
            self._share_slugs.pop(token, None)
            _save_share_tokens(self._share_tokens)

    def share_url(self, slug: str) -> str:
        base = self.public_base_url() or self.local_base_url() or ""
        token = self._share_tokens.get(slug) or self._issue_share_token(slug)
        return f"{base}/c/{token}"

    def _slug_for_ref(self, ref: str, *, allow_slug: bool) -> Optional[str]:
        """The clip a URL segment names, or None.

        The public server resolves tokens only. The local server keeps
        accepting slugs, which is what the app itself uses, and also takes
        tokens so a share link that fell back to the local address still
        opens on this machine.
        """
        if allow_slug and ref in self._clips:
            return ref
        return self._share_slugs.get(ref)

    def local_base_url(self) -> Optional[str]:
        return self._local_base_url

    def public_base_url(self) -> Optional[str]:
        if self.cfg.sharing.base_url:
            return self.cfg.sharing.base_url.rstrip("/")
        return (self._tunnel_url or self._public_bind_url or "").rstrip("/") or None

    def public_is_reachable(self) -> bool:
        """Whether share links work outside the local network. False means we
        fell back to a LAN address because there is no tunnel (#105)."""
        return bool(self.cfg.sharing.base_url or self._tunnel_url)

    def _share_links_update(self) -> dict:
        return {
            "type": "share_links_changed",
            "links": {slug: self.share_url(slug) for slug in self._clips},
            "share_is_public": self.public_is_reachable(),
        }

    async def _broadcast_share_links(self) -> None:
        if self._clips:
            await self.broadcast(self._share_links_update())

    async def broadcast(self, msg: dict) -> None:
        if not self._ws_clients:
            return
        text = json.dumps(msg)
        dead: set[web.WebSocketResponse] = set()
        for ws in self._ws_clients:
            try:
                await ws.send_str(text)
            except Exception:
                dead.add(ws)
        self._ws_clients -= dead

    # ── internal broadcast helpers ────────────────────────────────────────────

    async def _broadcast_playlists(self) -> None:
        await self.broadcast({
            "type": "playlists_changed",
            "playlists": self.playlists.list_playlists(),
        })

    async def refresh_clip(self, slug: str) -> None:
        """Tell the UI a clip's details changed."""
        path = self._clips.get(slug)
        if path is not None and path.exists():
            await self._broadcast_clip(slug, path)

    async def _api_get_keys(self, req: web.Request) -> web.Response:
        slug = req.match_info["slug"]
        if slug not in self._clips:
            raise web.HTTPNotFound()
        return web.json_response({"keys": keystore.load_sidecar(slug)})

    async def _api_overlay_move(self, req: web.Request) -> web.Response:
        body = await req.json()
        if self.key_overlay_move_cb is None:
            return web.json_response({"ok": False})
        self.key_overlay_move_cb(bool(body.get("on")))
        return web.json_response({"ok": True})

    async def _broadcast_clip(self, slug: str, path: Path) -> None:
        meta = await self._get_meta(slug, path)
        if not _thumb_path(path).exists():
            await _make_thumb(path, duration=meta.get("duration", 0))
        # A rename or delete can land while the probe runs, as when a trim
        # saves and the dialog applies its new name straight after. That path
        # is gone by now, so announcing it would put an unreadable copy of
        # the old name back in the gallery (#246).
        if self._clips.get(slug) != path or not path.exists():
            self._meta.pop(slug, None)
            return
        await self.broadcast({"type": "clip_saved", "clip": self._clip_json(slug, path, meta)})

    async def _get_meta(self, slug: str, path: Path) -> dict:
        if slug not in self._meta:
            self._meta[slug] = await _ffprobe(path)
        return self._meta[slug]

    def _clip_json(self, slug: str, path: Path, meta: dict) -> dict:
        try:
            st = path.stat()
            size = st.st_size
            mtime_ns = st.st_mtime_ns
            created_at = datetime.fromtimestamp(st.st_mtime).isoformat()
        except OSError:
            size, mtime_ns, created_at = 0, 0, ""

        # The slug is a filename, so it can hold spaces and punctuation that
        # would truncate or corrupt a URL (#138). Encode it in every link.
        enc = quote(slug, safe="")
        thumb_rev = f"{size}-{mtime_ns}"
        thumb_url = f"/t/{enc}?v={thumb_rev}" if _thumb_path(path).exists() else None
        return {
            "slug":       slug,
            "name":       path.name,
            "size":       size,
            "created_at": created_at,
            "game":       self.playlists.game_for(slug),
            "views":      self._views.get(slug, 0),
            # Keys were kept beside this clip for the viewer's overlay.
            "keys":       keystore.sidecar_path(slug).exists(),
            "duration":   meta.get("duration", 0),
            "width":      meta.get("width",    0),
            "height":     meta.get("height",   0),
            # Lets the UI request an H.264 preview proxy for codecs the native
            # WebEngine can't decode (H.265).
            "vcodec":     meta.get("vcodec",   ""),
            "audio_tracks": meta.get("audio_tracks", []),
            # ffmpeg cannot read this file. It is still listed, because it is
            # the user's recording and may be recoverable by hand, but the
            # card says so instead of showing a 0:00 clip that will not play.
            "unreadable": bool(meta.get("unreadable")),
            "unreadable_reason": meta.get("unreadable_reason", ""),
            # Keep share links public, but serve media via local relative URLs
            # so the app UI never fetches video through an external tunnel.
            "share_url":  self.share_url(slug),
            "share_is_public": self.public_is_reachable(),
            # Cache-bust media URLs by clip file identity: deleted clip numbers
            # get reused (Vice_Clip_5 can name a brand-new file), and a trim
            # rewrites the file under the same slug, without the version the
            # browser may play a cached older video for the clip it shows.
            "video_url":  f"/v/{enc}?v={thumb_rev}",
            "thumb_url":  thumb_url,
        }

    # ── images ────────────────────────────────────────────────────────────────

    def _image_dir(self) -> Path:
        return resolve_path(
            getattr(self.cfg.output, "image_directory", "")
            or str(actual_home_dir() / "Pictures" / "Vice")
        )

    def next_image_path(self, tag: Optional[str] = None) -> Path:
        """Where the next screenshot should be written."""
        out_dir = self._image_dir()
        out_dir.mkdir(parents=True, exist_ok=True)
        return next_image_path(out_dir, tag)

    def add_image(self, path: Path, game: Optional[str] = None) -> str:
        """Register a new screenshot and return its slug."""
        slug = path.stem
        self._images[slug] = path
        if (game and self.cfg.output.auto_playlist_by_game
                and self.playlists.record_auto(game, image_slug(slug))):
            asyncio.create_task(self._broadcast_playlists())
        asyncio.create_task(self._broadcast_image(slug, path))
        return slug

    def image_count(self) -> int:
        return len(self._images)

    def rescan_images(self) -> None:
        """Rebuild the image index from whatever the image directory holds now.

        Playlist membership is deliberately left alone: pointing Vice at a
        second folder and back must not throw away which playlists the first
        folder's screenshots were in.
        """
        found: dict[str, Path] = {}
        img_dir = self._image_dir()
        if img_dir.exists():
            stills = [p for ext in IMAGE_EXTS for p in img_dir.glob(f"*.{ext}")]
            for shot in sorted(stills, key=lambda p: p.stat().st_mtime):
                found[shot.stem] = shot
        self._images = found

    async def _broadcast_image(self, slug: str, path: Path) -> None:
        # Thumbnails are what the grid loads. Without one it would pull the
        # full screenshot, which for a 4K library is hundreds of megabytes.
        if not _thumb_path(path).exists():
            await _make_thumb(path)
        await self.broadcast({"type": "image_saved", "image": await self._image_json(slug, path)})

    async def _image_json(self, slug: str, path: Path) -> dict:
        try:
            st = path.stat()
            size = st.st_size
            mtime_ns = st.st_mtime_ns
            created_at = datetime.fromtimestamp(st.st_mtime).isoformat()
        except OSError:
            size, mtime_ns, created_at = 0, 0, ""

        # probe_media, never _ffprobe: _ffprobe reads a duration of zero as a
        # damaged container and goes off to attempt a moov remux, which means
        # nothing for a still and would rewrite the user's screenshot.
        width = height = 0
        try:
            probed = await probe_media(path) or {}
            width = int(probed.get("width") or 0)
            height = int(probed.get("height") or 0)
        except Exception as exc:
            log.debug("Could not read the dimensions of %s: %s", path.name, exc)

        enc = quote(slug, safe="")
        rev = f"{size}-{mtime_ns}"
        return {
            "slug":       slug,
            "name":       path.name,
            "size":       size,
            "created_at": created_at,
            "game":       self.playlists.game_for(image_slug(slug)),
            "width":      width,
            "height":     height,
            # Annotating rewrites the file under the same slug, so both URLs
            # carry the file's identity or the window shows the pre-edit copy.
            "image_url":  f"/i/{enc}?v={rev}",
            "thumb_url":  f"/it/{enc}?v={rev}" if _thumb_path(path).exists() else None,
        }

    # ── route handlers ────────────────────────────────────────────────────────

    async def _ui(self, _: web.Request) -> web.Response:
        ui_index = _resolve_ui_index()
        if not ui_index:
            log.error("Vice UI not found (missing vice/ui/index.html)")
            return web.Response(
                text="<h1>Vice UI not found</h1><p>Reinstall Vice from this checkout or AUR package.</p>",
                content_type="text/html",
                status=500,
            )

        try:
            content = ui_index.read_text(encoding="utf-8")
            # Asset URLs are versioned by content, the visible version string
            # stays the plain version.
            content = content.replace(f"?v={UI_VERSION_TOKEN}", f"?v={_ui_asset_rev()}")
            content = content.replace(UI_VERSION_TOKEN, __version__)
            return web.Response(
                text=content,
                content_type="text/html",
                # The page itself must never be cached, or a stale copy keeps
                # pointing at the previous build's assets.
                headers={"Cache-Control": "no-store"},
            )
        except Exception as exc:
            log.error("Failed reading UI file %s: %s", ui_index, exc)
            return web.Response(
                text="<h1>Vice UI failed to load</h1><p>Check vice logs for details.</p>",
                content_type="text/html",
                status=500,
            )

    async def _ui_asset(self, req: web.Request, *, kind: str) -> web.Response:
        path = _resolve_ui_asset(kind, req.match_info["name"])
        if not path:
            raise web.HTTPNotFound()
        return web.FileResponse(
            path,
            headers={
                "Content-Type": _UI_CONTENT_TYPES[kind],
                "Cache-Control": "public, max-age=31536000, immutable",
            },
        )

    async def _embed_page(self, req: web.Request) -> web.Response:
        return await self._serve_embed_page(req, public=False)

    async def _public_embed_page(self, req: web.Request) -> web.Response:
        return await self._serve_embed_page(req, public=True)

    def _clip_for_ref(self, ref: str, *, public: bool) -> tuple[str, Path]:
        slug = self._slug_for_ref(ref, allow_slug=not public)
        path = self._clips.get(slug) if slug else None
        if not path or not path.exists():
            raise web.HTTPNotFound()
        return slug, path

    async def _serve_embed_page(self, req: web.Request, *, public: bool) -> web.Response:
        ref = req.match_info["ref"]
        slug, path = self._clip_for_ref(ref, public=public)
        meta = await self._get_meta(slug, path)
        # cloudflared terminates TLS and forwards plain HTTP, so req.scheme
        # is "http" even when the visitor came in over https. Discord and
        # other scrapers reject non-https og:video URLs, which breaks
        # embeds for tunnel links (issue #100).
        scheme = req.headers.get("X-Forwarded-Proto", req.scheme)
        base = f"{scheme}://{req.host}"
        # Direct file URL with the real container suffix; some unfurlers
        # sniff the extension. _video strips it back off.
        suffix = path.suffix.lower() or ".mp4"
        # The media links reuse whatever named the page, so a page reached by
        # token never hands out the filename that would lead to other clips.
        enc = quote(ref, safe="")
        page = _EMBED_PAGE.format(
            title=html.escape(f"Vice clip, {slug}", quote=True),
            page_url=f"{base}/c/{enc}",
            video_url=f"{base}/v/{enc}{suffix}",
            video_type="video/x-matroska" if suffix == ".mkv" else "video/mp4",
            thumb_url=f"{base}/t/{enc}",
            width=meta.get("width", 1920),
            height=meta.get("height", 1080),
            color=self._embed_color(),
        )
        return web.Response(text=page, content_type="text/html")

    def _embed_color(self) -> str:
        """Validated embed accent color (guards against HTML injection)."""
        color = getattr(self.cfg.sharing, "embed_color", "") or ""
        if re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            return color
        return "#0099ff"

    async def _video(self, req: web.Request) -> web.Response:
        return await self._serve_video(req, public=False)

    async def _public_video(self, req: web.Request) -> web.Response:
        return await self._serve_video(req, public=True)

    async def _serve_video(self, req: web.Request, *, public: bool) -> web.Response:
        ref = req.match_info["ref"]
        slug = self._slug_for_ref(ref, allow_slug=not public)
        if slug is None and ref.lower().endswith((".mp4", ".mkv")):
            # Embed pages link the file with its container suffix. Exact
            # match first so slugs that themselves contain dots keep working.
            slug = self._slug_for_ref(ref.rsplit(".", 1)[0], allow_slug=not public)
        path = self._clips.get(slug) if slug else None
        if not path or not path.exists():
            raise web.HTTPNotFound()

        # The UI asks for proxy=1 when the clip's codec (H.265) can't play in
        # the native WebEngine. Serve a cached H.264 copy instead; the original
        # is never touched. Falls through to the source if it's already
        # web-playable or the transcode fails. Local only: over a share link
        # it would let a stranger make this machine transcode on request.
        if not public and req.query.get("proxy") == "1":
            served = await self._serve_preview_proxy(slug, path)
            if served is not None:
                return served

        # no-cache = revalidate before reuse. Slugs are not stable identities
        # (clip numbers get reused after deletes, trims rewrite in place), so
        # a cached response may belong to a different video than the slug
        # currently names.
        content_type = ("video/x-matroska" if path.suffix.lower() == ".mkv"
                        else "video/mp4")
        return web.FileResponse(
            path,
            headers={
                "Content-Type": content_type,
                "Accept-Ranges": "bytes",
                "Cache-Control": "no-cache",
            },
        )

    async def _serve_preview_proxy(self, slug: str, path: Path):
        """Return a FileResponse for the clip's H.264 preview proxy, or None to
        fall back to serving the original."""
        if self._proxy_stopping:
            raise web.HTTPServiceUnavailable()
        task = asyncio.current_task()
        self._proxy_tasks.add(task)
        try:
            proxy = _proxy_path(path)
            if not proxy.exists() or proxy.stat().st_size == 0:
                async with self._proxy_lock:
                    meta = await self._get_meta(slug, path)
                    proxy = await _make_preview_proxy(path, meta.get("vcodec", ""))
        finally:
            self._proxy_tasks.discard(task)
        if proxy is None or not proxy.exists():
            return None
        return web.FileResponse(
            proxy,
            headers={
                "Content-Type": "video/mp4",
                "Accept-Ranges": "bytes",
                "Cache-Control": "no-cache",
            },
        )

    async def _prepare_discord_copy(self, slug: str, path: Path) -> Optional[Path]:
        """The clip's Discord-sized file, transcoding it once if needed.

        Raises DiscordTooLarge when the clip cannot fit the cap at all.
        """
        if self._proxy_stopping:
            raise web.HTTPServiceUnavailable()
        task = asyncio.current_task()
        self._proxy_tasks.add(task)
        try:
            # A drag that is already encoded must not queue behind someone
            # else's encode: the whole library shares one encoder lock.
            cached = _discord_path(path)
            if cached.exists() and 0 < cached.stat().st_size <= DISCORD_MAX_BYTES:
                return cached
            async with self._proxy_lock:
                meta = await self._get_meta(slug, path)
                return await _make_discord_copy(
                    path,
                    meta.get("duration", 0),
                    meta.get("audio_streams", 1),
                )
        finally:
            self._proxy_tasks.discard(task)

    async def _audio_track(self, req: web.Request) -> web.Response:
        slug = req.match_info["slug"]
        path = self._clips.get(slug)
        raw_index = req.match_info["index"]
        if not path or not path.exists() or not re.fullmatch(r"[0-9]{1,4}", raw_index):
            raise web.HTTPNotFound()
        index = int(raw_index)
        meta = await self._get_meta(slug, path)
        if index >= meta.get("audio_streams", 0):
            raise web.HTTPNotFound()
        if self._proxy_stopping:
            raise web.HTTPServiceUnavailable()
        task = asyncio.current_task()
        self._proxy_tasks.add(task)
        try:
            target = _audio_preview_path(path, index)
            if not target.exists() or target.stat().st_size == 0:
                async with self._proxy_lock:
                    target = await _make_audio_preview(path, index)
        except (OSError, RuntimeError, asyncio.TimeoutError) as exc:
            log.warning("Audio preview failed for %s track %d: %s", slug, index, exc)
            raise web.HTTPServiceUnavailable(text="Could not prepare this audio track") from exc
        finally:
            self._proxy_tasks.discard(task)
        return web.FileResponse(target, headers={
            "Content-Type": "audio/mp4", "Accept-Ranges": "bytes", "Cache-Control": "no-cache",
        })

    async def _thumb(self, req: web.Request) -> web.Response:
        return await self._serve_thumb(req, public=False)

    async def _public_thumb(self, req: web.Request) -> web.Response:
        return await self._serve_thumb(req, public=True)

    async def _serve_thumb(self, req: web.Request, *, public: bool) -> web.Response:
        slug, path = self._clip_for_ref(req.match_info["ref"], public=public)
        meta = await self._get_meta(slug, path)
        t = await _make_thumb(path, duration=meta.get("duration", 0))
        if not t.exists() or t.stat().st_size == 0:
            raise web.HTTPNotFound()
        return web.FileResponse(t, headers={"Content-Type": "image/jpeg"})

    # ── REST handlers ─────────────────────────────────────────────────────────

    async def _api_clips(self, _: web.Request) -> web.Response:
        items = [
            (slug, path) for slug, path in sorted(
                self._clips.items(),
                key=lambda kv: kv[1].stat().st_mtime if kv[1].exists() else 0,
                reverse=True,
            )
            if path.exists()
        ]
        metas = {slug: await self._get_meta(slug, path) for slug, path in items}

        sem = asyncio.Semaphore(3)
        async def _ensure(slug: str, path: Path) -> None:
            thumb = _thumb_path(path)
            if thumb.exists() and thumb.stat().st_size > 0:
                return
            async with sem:
                await _make_thumb(path, duration=metas[slug].get("duration", 0))
        await asyncio.gather(
            *[_ensure(slug, path) for slug, path in items],
            return_exceptions=True,
        )

        result = [self._clip_json(slug, path, metas[slug]) for slug, path in items]
        return web.json_response({"clips": result})

    async def _api_clip_info(self, req: web.Request) -> web.Response:
        slug = req.match_info["slug"]
        path = self._clips.get(slug)
        if not path or not path.exists():
            raise web.HTTPNotFound()
        meta = await self._get_meta(slug, path)
        return web.json_response(self._clip_json(slug, path, meta))

    async def _api_delete(self, req: web.Request) -> web.Response:
        slug = req.match_info["slug"]
        path = self._clips.pop(slug, None)
        _purge_clip_discord(path)
        if path and path.exists():
            path.unlink()
        _purge_slug_thumbs(slug)
        _purge_slug_proxies(slug)
        (HIGHLIGHTS_DIR / f"{slug}.json").unlink(missing_ok=True)
        keystore.delete_sidecar(slug)
        self._meta.pop(slug, None)
        if self._views.pop(slug, None) is not None:
            _save_views(self._views)
        self._forget_share_token(slug)
        if self.playlists.on_clip_deleted(slug):
            await self._broadcast_playlists()
        if self.editor_project.on_clip_deleted(slug):
            await self.broadcast({"type": "editor_project_changed"})
        await self.broadcast({"type": "clip_deleted", "slug": slug})
        return web.json_response({"ok": True})

    async def _api_trim(self, req: web.Request) -> web.Response:
        slug = req.match_info["slug"]
        path = self._clips.get(slug)
        if not path or not path.exists():
            raise web.HTTPNotFound()

        # No await between the check and the add, so two requests cannot both
        # get past it. Refused rather than queued: the second cut was chosen
        # against a file the first one is about to replace (#281).
        if slug in self._trimming:
            return web.json_response(
                {"ok": False,
                 "error": "A trim is already running on this clip. "
                          "Wait for it to finish."},
                status=409)
        self._trimming.add(slug)
        try:
            return await self._trim_clip(req, slug, path)
        finally:
            self._trimming.discard(slug)

    async def _trim_clip(self, req: web.Request, slug: str, path: Path) -> web.Response:
        body  = await req.json()
        start = float(body.get("start", 0))
        end   = float(body.get("end",   0))
        # A copy keeps the original. Replacing it lost the rest of a session
        # recording for someone who expected Save trim to make a new clip (#255).
        as_copy = bool(body.get("copy"))
        if end <= start:
            return web.json_response({"ok": False, "error": "end must be after start"})

        ext = path.suffix.lstrip(".") or "mp4"
        tmp = path.with_suffix(f".trimming.{ext}")
        faststart = ["-movflags", "+faststart"] if ext == "mp4" else []
        source_codec = (await self._get_meta(slug, path)).get("vcodec", "")

        def _trim_cmd(encoder: Optional[str], bitrate: Optional[int] = None) -> list[str]:
            """Stream copy when *encoder* is None, otherwise re-encode the video
            with it. Audio is copied either way, so every recorded track
            survives at its original quality.

            A re-encode starts the video exactly on the in-point, but copied
            audio still reaches back to the keyframe before it, as packets
            marked for discard under a leading edit list. When that lead-in is
            not a whole number of audio packets, Chromium before about 107
            refuses the file with "unfixable negative timestamp", which is the
            QtWebEngine 6.4 that Ubuntu 24.04 and Pop!_OS ship (#246). The
            output-side -ss 0 drops those packets instead.
            """
            if encoder is None:
                pre, out, drop_lead_in = [], ["-c", "copy"], []
            else:
                pre, out = _trim_encoder_args(encoder, bitrate)
                drop_lead_in = ["-ss", "0"]
            return [
                "ffmpeg", "-hide_banner", "-loglevel", "error", *pre,
                "-ss", str(start), "-i", str(path), *drop_lead_in,
                "-t",  str(end - start),
                *KEEP_ALL_STREAMS,
                *out,
                *faststart,
                "-y",  str(tmp),
            ]

        async def _run(cmd: list[str], timeout: int) -> tuple[bool, str]:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await communicate_with_timeout(proc, timeout=timeout)
                return proc.returncode == 0, (stderr or b"").decode()[:300]
            except asyncio.TimeoutError:
                return False, "ffmpeg timed out"

        # Copy first: it is instant and lossless, and a cut that lands on a
        # keyframe needs nothing else. When the copy would not play, re-encode
        # into the clip's own codec, which is the only way to give the exact
        # frame the user asked for. Seeking to the previous keyframe instead
        # would play, but it would silently hand back a clip that starts before
        # the chosen in-point (#172).
        ok, err = await _run(_trim_cmd(None), 120)
        if ok:
            problem = await _trim_result_problem(tmp)
            if problem:
                log.info("Copy trim of %s %s, re-encoding", path.name, problem)
        else:
            problem = "could not be stream copied"
            log.warning("Copy trim of %s failed, re-encoding: %s", path.name, err)

        if problem:
            candidates = await asyncio.to_thread(_trim_encoder_candidates, source_codec)
            bitrate = await _video_bitrate(path)
            for encoder in candidates:
                ok, err = await _run(_trim_cmd(encoder, bitrate), 900)
                if not ok:
                    log.info("Trim of %s with %s failed: %s", path.name, encoder, err)
                    continue
                problem = await _trim_result_problem(tmp)
                if problem:
                    err = f"the trimmed clip {problem}"
                    log.warning("Trim of %s with %s %s", path.name, encoder, problem)
                    continue
                _remember_trim_encoder(source_codec, encoder)
                log.info("Trimmed %s with %s", path.name, encoder)
                break

        # Never replace the user's recording with a file Vice has not decoded.
        # Failing here leaves the original on disk and says why, which beats
        # handing back a clip that opens black and explains nothing (#172).
        if problem or not ok or not tmp.exists():
            tmp.unlink(missing_ok=True)
            return web.json_response(
                {"ok": False, "error": err or problem or "trim failed"})

        kept_keys = keystore.load_sidecar(slug)
        if as_copy:
            target = _trimmed_copy_path(path)
            tmp.replace(target)
            if kept_keys is not None:
                keystore.save_sidecar(target.stem, keystore.cut_intervals(kept_keys, start, end))
            self.add_clip(target, self.playlists.game_for(slug))
            return web.json_response({"ok": True, "slug": target.stem, "copy": True})

        tmp.replace(path)
        if kept_keys is not None:
            keystore.save_sidecar(slug, keystore.cut_intervals(kept_keys, start, end))
        # Clear cached thumbnail and metadata so they regenerate on next access
        _purge_slug_thumbs(slug)
        _purge_slug_proxies(slug)
        _purge_clip_discord(path)
        self._meta.pop(slug, None)
        asyncio.create_task(self._broadcast_clip(slug, path))
        return web.json_response({"ok": True, "slug": slug})

    async def _api_rename(self, req: web.Request) -> web.Response:
        slug = req.match_info["slug"]
        path = self._clips.get(slug)
        if not path or not path.exists():
            raise web.HTTPNotFound()

        body     = await req.json()
        requested = str(body.get("name") or "").strip()
        if not requested:
            return web.json_response({"ok": False, "error": "name is required"})

        # Spaces and punctuation are normalised away rather than rejected, so
        # "Insane wallbang" saves as Insane-wallbang.mp4. Keep the clip's own
        # container.
        ext = path.suffix.lower() or ".mp4"
        stem = slugify_clip_name(requested)
        if not stem:
            return web.json_response({"ok": False, "error": "that name will not work as a file"})

        new_path = path.parent / f"{stem}{ext}"
        if new_path.exists() and new_path != path:
            return web.json_response({"ok": False, "error": "A clip with that name already exists"})

        # A rename keeps the file's size and modification time, so the cached
        # thumbnail and previews are still valid. Purging them made an H.265
        # clip transcode its preview again after every rename (#226).
        cached = frozenset(_current_cache_files(path))
        old_stem = path.stem
        path.rename(new_path)
        new_slug = new_path.stem

        # Update internal state
        self._clips.pop(slug, None)
        self._clips[new_slug] = new_path
        _purge_slug_thumbs(slug, keep=cached)
        _purge_slug_proxies(slug, keep=cached)
        _purge_clip_discord(path)
        for old in cached:
            try:
                old.replace(old.with_name(new_slug + old.name[len(old_stem):]))
            except OSError as exc:
                log.debug("Could not carry %s across the rename: %s", old.name, exc)
        self._meta.pop(slug, None)

        # Rename highlights file if it exists
        old_hl = HIGHLIGHTS_DIR / f"{slug}.json"
        if old_hl.exists():
            HIGHLIGHTS_DIR.mkdir(parents=True, exist_ok=True)
            old_hl.rename(HIGHLIGHTS_DIR / f"{new_slug}.json")
        keystore.move_sidecar(slug, new_slug)

        # Playlist membership, the view counter and the editor project follow
        # the clip
        if self.playlists.on_clip_renamed(slug, new_slug):
            await self._broadcast_playlists()
        if self.editor_project.on_clip_renamed(slug, new_slug):
            await self.broadcast({"type": "editor_project_changed"})
        if slug in self._views:
            self._views[new_slug] = self._views.pop(slug)
            _save_views(self._views)
        self._move_share_token(slug, new_slug)

        # Tell the UI: old card gone, new card appears
        await self.broadcast({"type": "clip_deleted", "slug": slug})
        asyncio.create_task(self._broadcast_clip(new_slug, new_path))
        return web.json_response({"ok": True, "slug": new_slug, "name": new_path.name})

    async def _api_reveal(self, req: web.Request) -> web.Response:
        slug = req.match_info["slug"]
        path = self._clips.get(slug)
        if not path or not path.exists():
            raise web.HTTPNotFound()
        asyncio.create_task(_xdg_open(path.parent))
        return web.json_response({"ok": True})

    async def _api_get_app_state(self, _: web.Request) -> web.Response:
        return web.json_response(_load_app_state())

    async def _api_set_app_state(self, req: web.Request) -> web.Response:
        try:
            body = await req.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"ok": False, "error": "expected an object"}, status=400)
        state = _load_app_state()
        state.update(body)
        _save_app_state(state)
        return web.json_response({"ok": True})

    async def _api_copy_file(self, req: web.Request) -> web.Response:
        """Put the clip file itself on the clipboard so it can be pasted
        straight into Discord instead of shared as a link (#117).

        With ``?discord=1`` the file is the Discord-sized copy, built first if
        it does not exist yet. That is what the Share button copies when the
        user chose Discord files over links.
        """
        slug = req.match_info["slug"]
        path = self._clips.get(slug)
        if not path or not path.exists():
            raise web.HTTPNotFound()

        if req.query.get("discord") == "1":
            try:
                copy_path = await self._prepare_discord_copy(slug, path)
            except DiscordTooLarge as exc:
                return web.json_response({"ok": False, "error": str(exc)})
            if copy_path is None or not copy_path.exists():
                return web.json_response(
                    {"ok": False, "error": "Could not build a Discord-sized copy"})
            path = copy_path

        uri = path.resolve().as_uri()
        # Chromium and Electron read pasted files from text/uri-list. Both
        # tools keep owning the selection in the background after forking,
        # so the clipboard survives this request returning.
        if shutil.which("wl-copy"):
            cmd = ["wl-copy", "--type", "text/uri-list"]
        elif shutil.which("xclip"):
            cmd = ["xclip", "-selection", "clipboard", "-t", "text/uri-list"]
        else:
            return web.json_response({
                "ok": False,
                "error": "Copying files needs wl-clipboard (Wayland) or xclip (X11).",
            })

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            proc.stdin.write(f"{uri}\r\n".encode())
            await proc.stdin.drain()
            proc.stdin.close()
        except Exception as exc:
            log.warning("Copying %s to the clipboard failed: %s", path.name, exc)
            return web.json_response({"ok": False, "error": "Could not reach the clipboard."})
        return web.json_response({"ok": True})

    # ── image handlers ────────────────────────────────────────────────────────

    def _image_or_404(self, req: web.Request) -> tuple[str, Path]:
        slug = req.match_info["slug"]
        path = self._images.get(slug)
        if not path or not path.exists():
            raise web.HTTPNotFound()
        return slug, path

    async def _image_file(self, req: web.Request) -> web.Response:
        _, path = self._image_or_404(req)
        return web.FileResponse(path, headers={
            "Content-Type": _IMAGE_MIME.get(path.suffix.lower(), "application/octet-stream"),
            # An annotation rewrites this path, so the URL's revision is what
            # makes the new picture appear. Never cache past that.
            "Cache-Control": "no-cache",
        })

    async def _image_thumb(self, req: web.Request) -> web.Response:
        _, path = self._image_or_404(req)
        t = await _make_thumb(path)
        if not t.exists():
            raise web.HTTPNotFound()
        return web.FileResponse(t, headers={"Content-Type": "image/jpeg"})

    async def _api_discord_copy(self, req: web.Request) -> web.Response:
        """Build (or reuse) the clip's Discord-sized copy and say where it is.

        The share modal calls this when it opens so the file is on disk before
        anyone tries to drag it: a drag cannot wait for an encode.
        """
        slug = req.match_info["slug"]
        path = self._clips.get(slug)
        if not path or not path.exists():
            raise web.HTTPNotFound()
        try:
            copy_path = await self._prepare_discord_copy(slug, path)
        except DiscordTooLarge as exc:
            return web.json_response({"ok": False, "error": str(exc)})
        if copy_path is None or not copy_path.exists():
            return web.json_response(
                {"ok": False, "error": "Could not build a Discord-sized copy"})
        st = copy_path.stat()
        return web.json_response({
            "ok": True,
            # A path, not a URL: a drop target outside the window takes files
            # as a file:// uri-list. There is deliberately no HTTP route for
            # the copy, so nothing can fall back to the full-size original
            # while claiming to serve a Discord-sized one.
            "path": str(copy_path),
            "filename": f"{slug}.mp4",
            "size": st.st_size,
            "limit": DISCORD_MAX_BYTES,
        })

    async def _api_images(self, _: web.Request) -> web.Response:
        items = [
            (slug, path) for slug, path in sorted(
                self._images.items(),
                key=lambda kv: kv[1].stat().st_mtime if kv[1].exists() else 0,
                reverse=True,
            )
            if path.exists()
        ]

        sem = asyncio.Semaphore(3)
        async def _ensure(path: Path) -> None:
            if _thumb_path(path).exists():
                return
            async with sem:
                await _make_thumb(path)
        await asyncio.gather(*[_ensure(path) for _, path in items], return_exceptions=True)

        result = [await self._image_json(slug, path) for slug, path in items]
        return web.json_response({"images": result})

    async def _api_image_delete(self, req: web.Request) -> web.Response:
        slug = req.match_info["slug"]
        path = self._images.pop(slug, None)
        if path and path.exists():
            path.unlink()
        _purge_slug_thumbs(slug)
        if self.playlists.on_clip_deleted(image_slug(slug)):
            await self._broadcast_playlists()
        await self.broadcast({"type": "image_deleted", "slug": slug})
        return web.json_response({"ok": True})

    async def _api_image_rename(self, req: web.Request) -> web.Response:
        slug, path = self._image_or_404(req)
        try:
            body = await req.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        stem = slugify_clip_name(str(body.get("name", "")))
        if not stem:
            return web.json_response({"ok": False, "error": "that name has nothing usable in it"})
        if stem == slug:
            return web.json_response(await self._image_json(slug, path))

        target = path.with_name(f"{stem}{path.suffix}")
        if target.exists():
            return web.json_response({"ok": False, "error": "an image by that name already exists"})
        try:
            path.rename(target)
        except OSError as exc:
            log.warning("Renaming %s failed: %s", path.name, exc)
            return web.json_response({"ok": False, "error": str(exc)})

        self._images.pop(slug, None)
        self._images[stem] = target
        _purge_slug_thumbs(slug)
        if self.playlists.on_clip_renamed(image_slug(slug), image_slug(stem)):
            await self._broadcast_playlists()
        await self.broadcast({"type": "image_deleted", "slug": slug})
        await self._broadcast_image(stem, target)
        return web.json_response(await self._image_json(stem, target))

    async def _api_image_reveal(self, req: web.Request) -> web.Response:
        _, path = self._image_or_404(req)
        asyncio.create_task(_xdg_open(path.parent))
        return web.json_response({"ok": True})

    async def _api_image_open(self, req: web.Request) -> web.Response:
        _, path = self._image_or_404(req)
        asyncio.create_task(_xdg_open(path))
        return web.json_response({"ok": True})

    async def _api_image_copy(self, req: web.Request) -> web.Response:
        _, path = self._image_or_404(req)
        ok, error = await copy_image_to_clipboard(path)
        return web.json_response({"ok": ok} if ok else {"ok": False, "error": error})

    async def _api_image_annotate(self, req: web.Request) -> web.Response:
        """Replace an image with the annotated version the window composited.

        The body is the PNG itself rather than JSON: it is already megabytes,
        and base64 in a JSON envelope would make it a third bigger for nothing.
        """
        slug, path = self._image_or_404(req)
        data = await req.read()
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            return web.json_response({"ok": False, "error": "expected a PNG body"}, status=400)

        # Written beside the original and moved into place, so an interrupted
        # save cannot leave the user with half a screenshot.
        tmp = path.with_suffix(f".annotating{path.suffix}")
        target = path.with_suffix(".png")
        try:
            tmp.write_bytes(data)
            tmp.replace(target)
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            log.warning("Saving the annotated %s failed: %s", path.name, exc)
            return web.json_response({"ok": False, "error": str(exc)})

        # A jpg screenshot annotated once becomes a png, so the index has to
        # follow the file rather than keep pointing at a path that is gone.
        if target != path:
            path.unlink(missing_ok=True)
        self._images[slug] = target
        _purge_slug_thumbs(slug)
        await self._broadcast_image(slug, target)
        return web.json_response(await self._image_json(slug, target))

    async def _api_save_frame(self, req: web.Request) -> web.Response:
        """Save the frame at `time` of a clip as a screenshot (#171)."""
        slug = req.match_info["slug"]
        path = self._clips.get(slug)
        if not path or not path.exists():
            raise web.HTTPNotFound()
        try:
            body = await req.json()
        except Exception:
            body = {}
        try:
            at = max(0.0, float(body.get("time", 0)))
        except (TypeError, ValueError):
            at = 0.0

        # The frame inherits the clip's game, so a still pulled out of a
        # Deadlock clip files itself under Deadlock like a screenshot would.
        game = self.playlists.game_for(slug)
        out = self.next_image_path(filename_tag(game))
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-ss", f"{at:.3f}", "-i", str(path),
            "-frames:v", "1", "-update", "1",
            "-y", str(out),
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await communicate_with_timeout(proc, timeout=60)
        except asyncio.TimeoutError:
            out.unlink(missing_ok=True)
            return web.json_response({"ok": False, "error": "ffmpeg timed out reading that frame"})
        except OSError as exc:
            return web.json_response({"ok": False, "error": str(exc)})
        if proc.returncode != 0 or not out.exists():
            out.unlink(missing_ok=True)
            reason = (stderr or b"").decode(errors="replace").strip().splitlines()
            return web.json_response({
                "ok": False,
                "error": reason[-1] if reason else "ffmpeg could not read that frame",
            })

        self.add_image(out, game=game)
        copied, copy_error = await copy_image_to_clipboard(out)
        payload = await self._image_json(out.stem, out)
        payload["ok"] = True
        payload["copied"] = copied
        if not copied:
            payload["copy_error"] = copy_error
        return web.json_response(payload)

    async def _api_open(self, req: web.Request) -> web.Response:
        slug = req.match_info["slug"]
        path = self._clips.get(slug)
        if not path or not path.exists():
            raise web.HTTPNotFound()
        # Playback escape hatch for WebEngine builds that cannot decode the
        # clip (PyPI wheels ship without H.264 support): hand the file to the
        # system default video player.
        asyncio.create_task(_xdg_open(path))
        return web.json_response({"ok": True})

    async def _api_playlists(self, _: web.Request) -> web.Response:
        return web.json_response({"playlists": self.playlists.list_playlists()})

    async def _api_create_playlist(self, req: web.Request) -> web.Response:
        body = await req.json()
        try:
            playlist = self.playlists.create_custom(
                name=str(body.get("name", "")),
                emoji=str(body.get("emoji", "") or ""),
                color1=str(body.get("color1", "") or ""),
                color2=str(body.get("color2", "") or ""),
            )
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        await self._broadcast_playlists()
        return web.json_response({"ok": True, "playlist": playlist})

    async def _api_patch_playlist(self, req: web.Request) -> web.Response:
        pid = req.match_info["pid"]
        body = await req.json()
        try:
            playlist = self.playlists.update_playlist(pid, body)
        except KeyError:
            raise web.HTTPNotFound()
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        await self._broadcast_playlists()
        return web.json_response({"ok": True, "playlist": playlist})

    async def _api_delete_playlist(self, req: web.Request) -> web.Response:
        pid = req.match_info["pid"]
        try:
            self.playlists.delete(pid)
        except KeyError:
            raise web.HTTPNotFound()
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        await self._broadcast_playlists()
        return web.json_response({"ok": True})

    async def _api_playlist_add_clip(self, req: web.Request) -> web.Response:
        pid = req.match_info["pid"]
        body = await req.json()
        slug = str(body.get("slug", ""))
        # Clips and screenshots share one membership list, so this accepts
        # either namespace and refuses anything that is neither.
        known = (
            slug[len(IMAGE_PREFIX):] in self._images
            if slug.startswith(IMAGE_PREFIX)
            else slug in self._clips
        )
        if not known:
            raise web.HTTPNotFound()
        try:
            playlist = self.playlists.add_clip(pid, slug)
        except KeyError:
            raise web.HTTPNotFound()
        await self._broadcast_playlists()
        return web.json_response({"ok": True, "playlist": playlist})

    async def _api_playlist_remove_clip(self, req: web.Request) -> web.Response:
        pid = req.match_info["pid"]
        slug = req.match_info["slug"]
        try:
            self.playlists.remove_clip(pid, slug)
        except KeyError:
            raise web.HTTPNotFound()
        await self._broadcast_playlists()
        return web.json_response({"ok": True})

    async def _api_view(self, req: web.Request) -> web.Response:
        slug = req.match_info["slug"]
        if slug not in self._clips:
            raise web.HTTPNotFound()
        self._views[slug] = self._views.get(slug, 0) + 1
        _save_views(self._views)
        return web.json_response({"ok": True, "views": self._views[slug]})

    async def _api_get_highlights(self, req: web.Request) -> web.Response:
        slug = req.match_info["slug"]
        return web.json_response({"highlights": _load_highlights(slug)})

    async def _api_add_highlight(self, req: web.Request) -> web.Response:
        slug = req.match_info["slug"]
        body  = await req.json()
        time_ = round(float(body.get("time", 0)), 3)
        label = (body.get("label") or "Highlight").strip() or "Highlight"
        color = body.get("color") or "#f59e0b"
        hl = _load_highlights(slug)
        next_id = str(max((int(h["id"]) for h in hl if str(h.get("id","")).isdigit()), default=0) + 1)
        entry = {"id": next_id, "time": time_, "label": label, "color": color}
        hl.append(entry)
        hl.sort(key=lambda h: h["time"])
        _save_highlights(slug, hl)
        return web.json_response({"ok": True, "highlight": entry})

    async def _api_patch_highlight(self, req: web.Request) -> web.Response:
        slug = req.match_info["slug"]
        hid  = req.match_info["hid"]
        body  = await req.json()
        hl = _load_highlights(slug)
        for h in hl:
            if str(h.get("id")) == hid:
                if "label" in body:
                    h["label"] = (body["label"] or "Highlight").strip() or "Highlight"
                if "color" in body:
                    h["color"] = body["color"]
                if "time" in body:
                    try:
                        h["time"] = round(max(0.0, float(body["time"])), 3)
                    except (TypeError, ValueError):
                        pass
                hl.sort(key=lambda x: float(x.get("time", 0)))
                _save_highlights(slug, hl)
                return web.json_response({"ok": True})
        return web.json_response({"ok": False, "error": "highlight not found"})

    async def _api_del_highlight(self, req: web.Request) -> web.Response:
        slug = req.match_info["slug"]
        hid  = req.match_info["hid"]
        hl = [h for h in _load_highlights(slug) if str(h.get("id")) != hid]
        _save_highlights(slug, hl)
        return web.json_response({"ok": True})

    # ── editor ───────────────────────────────────────────────────────────────

    async def _api_editor_get_project(self, _: web.Request) -> web.Response:
        project = self.editor_project.load()
        missing: list[str] = []
        if project:
            refs = {it.get("clipId") for it in project.get("items", [])
                    if isinstance(it, dict) and it.get("clipId")}
            missing = sorted(c for c in refs if c not in self._clips)
        return web.json_response({"project": project, "missing": missing})

    async def _api_editor_save_project(self, req: web.Request) -> web.Response:
        # Autosave is lenient on purpose: items may reference clips that no
        # longer exist and still get persisted, so a slow-mounting output dir
        # can't eat the project.
        try:
            body = await req.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        if (not isinstance(body, dict)
                or not isinstance(body.get("tracks"), list)
                or not isinstance(body.get("items"), list)):
            return web.json_response({"ok": False, "error": "expected a project"},
                                     status=400)
        self.editor_project.save({"version": 1, "tracks": body["tracks"],
                                  "items": body["items"]})
        return web.json_response({"ok": True})

    async def _editor_sources(self, project: dict) -> dict[str, Source]:
        sources: dict[str, Source] = {}
        for it in project.get("items", []):
            cid = it.get("clipId") if isinstance(it, dict) else None
            if not cid or cid in sources:
                continue
            path = self._clips.get(cid)
            if not path or not path.exists():
                continue
            meta = await self._get_meta(cid, path)
            sources[cid] = Source(
                path=path,
                duration=meta.get("duration", 0),
                width=meta.get("width", 0),
                height=meta.get("height", 0),
                has_audio=meta.get("audio_streams", 0) > 0,
                audio_streams=meta.get("audio_streams", 0),
            )
        return sources

    async def _api_editor_export(self, req: web.Request) -> web.Response:
        try:
            body = await req.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        raw = body.get("project")
        if not isinstance(raw, dict):
            return web.json_response({"ok": False, "error": "expected a project"},
                                     status=400)
        if self._exports.busy:
            return web.json_response(
                {"ok": False, "error": "an export is already running"}, status=409)

        sources = await self._editor_sources(raw)
        project, errors = validate_project(raw, sources)
        if errors:
            return web.json_response({"ok": False, "errors": errors}, status=400)

        out_dir = resolve_path(self.cfg.output.directory)
        location = body.get("location", "library")
        if location == "videos":
            dest = actual_home_dir() / "Videos"
        elif location == "custom":
            custom = str(body.get("path", "")).strip()
            if not custom:
                return web.json_response(
                    {"ok": False, "error": "a folder is required"}, status=400)
            dest = resolve_path(custom)
        else:
            location, dest = "library", out_dir
        try:
            dest.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return web.json_response(
                {"ok": False, "error": f"cannot use that folder: {exc}"}, status=400)

        requested = body.get("filename")
        if requested:
            name = sanitize_export_name(str(requested))
            if not name:
                return web.json_response(
                    {"ok": False, "error": "that name will not work as a file"},
                    status=400)
        else:
            name = default_export_name(dest)
        final = dest / name
        if final.exists():
            return web.json_response(
                {"ok": False, "error": "a file with that name already exists"},
                status=400)

        job_id = f"exp-{int(datetime.now().timestamp() * 1000)}"
        work = EXPORT_WORK_DIR / job_id
        work.mkdir(parents=True, exist_ok=True)
        for path, text in text_file_contents(project, work).items():
            path.write_text(text)

        tmp = dest / f".{final.stem}.export.mp4"
        discord_optimized = bool(body.get("discord_optimized"))
        accent = str(body.get("accent", "")) or "#0099ff"
        key_burn = None
        if body.get("keys"):
            key_burn = await asyncio.to_thread(
                _key_burn_for, project, sources, work / "keys", accent,
                self.cfg.overlay.keys_layout)
        try:
            cmd = build_export_cmd(project, sources, tmp,
                                   accent=accent,
                                   text_dir=work,
                                   discord_optimized=discord_optimized,
                                   key_burn=key_burn)
        except DiscordTooLarge as exc:
            shutil.rmtree(work, ignore_errors=True)
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        add_to_library = bool(body.get("add_to_library"))

        async def on_done(path: Path) -> Optional[dict]:
            target = path
            if location != "library" and add_to_library:
                copy = out_dir / name
                if copy.exists():
                    copy = out_dir / default_export_name(out_dir)
                out_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, copy)
                target = copy
            elif location != "library":
                return None
            self.add_clip(target)
            meta = await self._get_meta(target.stem, target)
            return self._clip_json(target.stem, target, meta)

        def cleanup() -> None:
            shutil.rmtree(work, ignore_errors=True)

        try:
            self._exports.start(job_id, cmd, project_extent(project), tmp, final,
                                on_done=on_done, cleanup=cleanup,
                                max_bytes=DISCORD_MAX_BYTES if discord_optimized else None)
        except ExportBusy:
            cleanup()
            return web.json_response(
                {"ok": False, "error": "an export is already running"}, status=409)
        log.info("Editor export %s started: %s", job_id, final)
        return web.json_response({"ok": True, "job_id": job_id, "path": str(final)})

    async def _api_editor_export_cancel(self, req: web.Request) -> web.Response:
        if await self._exports.cancel(req.match_info["jid"]):
            return web.json_response({"ok": True})
        raise web.HTTPNotFound()

    def _require_same_origin(self, req: web.Request) -> None:
        expected_origin = self.local_base_url()
        if expected_origin is None or req.headers.get("Origin") != expected_origin:
            raise web.HTTPForbidden(text="Request origin is not allowed")

    async def _api_uninstall(self, req: web.Request) -> web.Response:
        """Launch a detached uninstall process, then exit the daemon cleanly."""
        self._require_same_origin(req)
        import os
        import signal as _sig
        import subprocess as _sp
        import sys

        # Use a shell subprocess in a *new session* so it survives after we
        # send SIGTERM to this daemon process.  The `sleep 2` delay lets the
        # daemon finish shutting down (and the Unix socket disappear) before
        # the uninstall script tries to stop it via IPC, avoiding a deadlock
        # where the uninstall blocks waiting to talk to a daemon that is
        # waiting for the uninstall to finish.
        exe = sys.executable.replace("'", r"\'")
        cmd = f"sleep 2 && '{exe}' -m vice.main uninstall --yes"
        try:
            _sp.Popen(
                ["bash", "-c", cmd],
                start_new_session=True,
                stdin=_sp.DEVNULL,
                stdout=_sp.DEVNULL,
                stderr=_sp.DEVNULL,
            )
        except Exception as exc:
            log.error("Failed to launch uninstall subprocess: %s", exc)

        # Stop the daemon after giving the HTTP response time to reach the client.
        async def _exit() -> None:
            await asyncio.sleep(0.4)
            os.kill(os.getpid(), _sig.SIGTERM)

        asyncio.create_task(_exit())
        return web.json_response({"ok": True})

    async def _api_get_config(self, _: web.Request) -> web.Response:
        from .config import load as load_cfg
        return web.json_response(asdict(load_cfg()))

    async def _api_get_displays(self, req: web.Request) -> web.Response:
        backend = (req.query.get("backend") or self.cfg.recording.backend or "auto").strip() or "auto"
        # Enumeration shells out (with timeouts); keep it off the event loop.
        from .active_window import pointer_display_supported
        payload = await asyncio.to_thread(list_display_options, backend)
        payload["selected"] = self.cfg.recording.display
        payload["follow_mouse_supported"] = pointer_display_supported()
        return web.json_response(payload)

    async def _api_get_audio_sources(self, _: web.Request) -> web.Response:
        payload = await asyncio.to_thread(list_gsr_audio_sources)
        payload["selected"] = self.cfg.recording.gsr_audio_source
        return web.json_response(payload)

    async def _api_set_config(self, req: web.Request) -> web.Response:
        from .config import (
            Config, RecordingConfig, HotkeyConfig, OutputConfig, SharingConfig,
            DiscordConfig, DiscordCustomGame, UpdatesConfig, NotificationsConfig,
            UIConfig, OverlayConfig,
            clamp_recording_limits, ensure_buffer_covers_clip_presets, normalize_overlay,
            normalize_clip_presets, normalize_combo, normalize_focus_blocklist,
            validate_hotkeys,
            load as load_cfg, save as save_cfg,
        )

        body = await req.json()

        def _merge(base: dict, over: dict) -> dict:
            for k, v in over.items():
                # Keyed by source name, so the keys are data: a new source has
                # no field to merge into and must be stored whole (#263).
                if k in FREE_MAP_KEYS and isinstance(v, dict):
                    base[k] = dict(v)
                elif k in base and isinstance(base[k], dict) and isinstance(v, dict):
                    _merge(base[k], v)
                elif k in base:
                    base[k] = v
            return base

        # Merge onto the persisted config so partial saves never depend on
        # transient in-memory rollback state.
        persisted_cfg = load_cfg()
        merged = _merge(asdict(persisted_cfg), body)

        discord_raw = dict(merged.get("discord", {}))
        custom_games_raw = discord_raw.pop("custom_games", []) or []
        discord_custom_games = [
            DiscordCustomGame(
                name=str(g.get("name", "")),
                matches=[str(m) for m in (g.get("matches") or [])],
            )
            for g in custom_games_raw
            if isinstance(g, dict)
        ]
        hotkeys_raw = dict(merged.get("hotkeys", {}))
        if hotkeys_raw.get("clip"):
            hotkeys_raw["clip"] = normalize_combo(str(hotkeys_raw["clip"]).strip())
        # Cleared in the UI means unbound, not bound to the empty string.
        shot = str(hotkeys_raw.get("screenshot") or "").strip()
        hotkeys_raw["screenshot"] = normalize_combo(shot) if shot else None
        toggle = str(hotkeys_raw.get("key_overlay") or "").strip()
        hotkeys_raw["key_overlay"] = normalize_combo(toggle) if toggle else None
        try:
            hotkeys_raw["clip_presets"] = normalize_clip_presets(
                hotkeys_raw.get("clip_presets", []),
                strict=True,
            )
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        hotkeys_raw["disable_while_focused"] = normalize_focus_blocklist(
            hotkeys_raw.get("disable_while_focused")
        )

        new_cfg = Config(
            recording=RecordingConfig(**{
                k: v for k, v in merged["recording"].items()
                if k in RecordingConfig.__dataclass_fields__
            }),
            hotkeys=HotkeyConfig(**{
                k: v for k, v in hotkeys_raw.items()
                if k in HotkeyConfig.__dataclass_fields__
            }),
            output=OutputConfig(**{
                k: v for k, v in merged["output"].items()
                if k in OutputConfig.__dataclass_fields__
            }),
            sharing=SharingConfig(**{
                k: v for k, v in merged["sharing"].items()
                if k in SharingConfig.__dataclass_fields__
            }),
            discord=DiscordConfig(
                **{k: v for k, v in discord_raw.items()
                   if k in DiscordConfig.__dataclass_fields__ and k != "custom_games"},
                custom_games=discord_custom_games,
            ),
            updates=UpdatesConfig(**{
                k: v for k, v in merged.get("updates", {}).items()
                if k in UpdatesConfig.__dataclass_fields__
            }),
            notifications=NotificationsConfig(**{
                k: v for k, v in merged.get("notifications", {}).items()
                if k in NotificationsConfig.__dataclass_fields__
            }),
            ui=UIConfig(**{
                k: v for k, v in merged.get("ui", {}).items()
                if k in UIConfig.__dataclass_fields__
            }),
            overlay=OverlayConfig(**{
                k: v for k, v in merged.get("overlay", {}).items()
                if k in OverlayConfig.__dataclass_fields__
            }),
        )
        try:
            validate_hotkeys(new_cfg.hotkeys)
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        ensure_buffer_covers_clip_presets(new_cfg)
        clamp_recording_limits(new_cfg)
        normalize_overlay(new_cfg)

        old_cfg = copy.deepcopy(self.cfg)
        image_dir_changed = (
            getattr(old_cfg.output, "image_directory", "")
            != getattr(new_cfg.output, "image_directory", "")
        )
        # embed_color is read per-request (the UI syncs it on theme switches)
        # and share_discord_file only by the UI, so neither demands a daemon
        # restart.
        old_sharing = copy.deepcopy(old_cfg.sharing)
        new_sharing = copy.deepcopy(new_cfg.sharing)
        old_sharing.embed_color = new_sharing.embed_color = ""
        old_sharing.share_discord_file = new_sharing.share_discord_file = False
        restart_required = (
            old_sharing != new_sharing
            or old_cfg.recording.gsr_args != new_cfg.recording.gsr_args
        )

        # Apply live (some settings still require daemon restart, e.g. recorder
        # backend). "ui" is only read by vice-app when the window opens, but it
        # is carried across so self.cfg still matches what is on disk.
        for field in ("recording", "hotkeys", "output", "sharing", "discord", "ui", "overlay"):
            setattr(self.cfg, field, getattr(new_cfg, field))

        apply_error: str | None = None
        if self.apply_config_cb:
            try:
                await self.apply_config_cb()
            except Exception as exc:
                # Keep runtime state stable and reject invalid live changes.
                for field in ("recording", "hotkeys", "output", "sharing", "discord"):
                    setattr(self.cfg, field, getattr(old_cfg, field))

                try:
                    await self.apply_config_cb()
                except Exception as rollback_exc:
                    log.warning("Rollback apply failed: %s", rollback_exc)

                apply_error = str(exc) or exc.__class__.__name__
                log.warning("Live config apply failed; settings saved for next restart: %s", exc)

        # Always persist validated settings, even when live apply fails.
        # This keeps restart-intended config changes from being lost.
        save_cfg(new_cfg)

        # Pointing Vice at a different pictures folder has to show that
        # folder's contents now, not after a restart.
        if image_dir_changed and not apply_error:
            self.rescan_images()

        if apply_error:
            return web.json_response({
                "ok": True,
                "applied": False,
                "restart_required": True,
                "warning": "Settings saved for next restart. Restart Vice to apply them.",
                "error": apply_error,
            })

        payload = {"ok": True, "applied": True, "restart_required": restart_required}
        if restart_required:
            payload["warning"] = "Some sharing settings require a full app restart to take effect."
        return web.json_response(payload)

    def clip_count(self) -> int:
        """How many clips are in the library right now."""
        return len(self._clips)

    async def _api_status(self, _: web.Request) -> web.Response:
        extra = self.get_status_cb() if self.get_status_cb else {}
        public_url = self.public_base_url()
        return web.json_response({
            "running":  True,
            "version":  __version__,
            "clips":    self.clip_count(),
            "images":   self.image_count(),
            "local_url": self.local_base_url(),
            "public_url": public_url,
            "base_url": public_url,
            "public_is_tunnel": self.public_is_reachable(),
            **extra,
        })

    async def _api_trigger(self, _: web.Request) -> web.Response:
        if self.trigger_clip_cb:
            asyncio.create_task(self.trigger_clip_cb())
        return web.json_response({"ok": True})

    async def _api_check_update(self, _: web.Request) -> web.Response:
        """Check now, ignoring the daily interval. Returns the newer release
        or null; a failed check is reported as null, never as an error."""
        if not self.check_update_cb:
            return web.json_response({"ok": True, "update": None})
        try:
            found = await self.check_update_cb()
        except Exception as exc:
            log.debug("Manual update check failed: %s", exc)
            found = None
        return web.json_response({"ok": True, "update": found})

    async def _api_quit(self, req: web.Request) -> web.Response:
        """Stop the daemon (browser-mode quit, native window uses pywebview API)."""
        self._require_same_origin(req)
        import os, signal as _sig
        response = web.json_response({"ok": True})
        asyncio.get_event_loop().call_later(0.2, lambda: os.kill(os.getpid(), _sig.SIGTERM))
        return response

    # ── WebSocket ─────────────────────────────────────────────────────────────

    async def _ws_handler(self, req: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(req)
        self._ws_clients.add(ws)
        try:
            async for msg in ws:
                if msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                    break
        finally:
            self._ws_clients.discard(ws)
        return ws

    # ── Tunnel (Cloudflare quick tunnel) ──────────────────────────────────────
    #
    # cloudflared is the only supported tunnel. There used to be an SSH
    # fallback via serveo.net, but it produced broken links (serveo prints
    # promotional URLs that got parsed as the tunnel address), is operated
    # by an unaccountable third party, and silently man-in-the-middles all
    # traffic. Failing loudly with an install hint is strictly better.

    async def _start_tunnel(self, port: int) -> None:
        if not shutil.which("cloudflared"):
            await self._tunnel_failed(
                "cloudflared is not installed. Install it to get public share "
                "links (https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/), "
                "or turn off the public tunnel in Settings."
            )
            return
        log.info("Starting Cloudflare Tunnel on port %d", port)
        if self._tunnel_task and not self._tunnel_task.done():
            return
        self._tunnel_stopping = False
        self._tunnel_task = asyncio.create_task(self._run_tunnel(port))

    async def _run_tunnel(self, port: int) -> None:
        delay = _TUNNEL_RETRY_INITIAL
        while not self._tunnel_stopping:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "cloudflared", "tunnel", "--url", f"http://localhost:{port}",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
            except OSError as exc:
                await self._tunnel_failed(f"cloudflared failed to start: {exc}")
                connected = False
            else:
                self._tunnel_proc = proc
                try:
                    _, connected = await self._read_cloudflare_url(proc)
                except asyncio.CancelledError:
                    await self._stop_tunnel_process(proc)
                    raise
                finally:
                    if self._tunnel_proc is proc:
                        self._tunnel_proc = None

            if self._tunnel_stopping:
                return
            if connected:
                delay = _TUNNEL_RETRY_INITIAL
            log.warning("Retrying Cloudflare Tunnel in %.0f seconds", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, _TUNNEL_RETRY_MAX)

    @staticmethod
    async def _stop_tunnel_process(proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            return
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=5)
        except ProcessLookupError:
            return
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                return
            await proc.wait()

    async def _tunnel_failed(self, reason: str) -> None:
        log.error("Public share tunnel unavailable: %s", reason)
        self._tunnel_url = None
        await self.broadcast({"type": "tunnel_error", "error": reason})
        await self._broadcast_share_links()

    async def _read_cloudflare_url(
        self, proc: Optional[asyncio.subprocess.Process] = None,
    ) -> tuple[str, bool]:
        proc = proc or self._tunnel_proc
        assert proc and proc.stdout
        diagnostics: list[str] = []
        connected = False
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").strip()
            # Keep draining stdout after the URL so process exit is still
            # detected.
            url = _quick_tunnel_url(line) if self._tunnel_url is None else None
            if url:
                self._tunnel_url = url
                connected = True
                log.info("Cloudflare Tunnel URL: %s", self._tunnel_url)
                await self.broadcast({"type": "tunnel_url", "url": self._tunnel_url})
                await self._broadcast_share_links()
            if not url and line and any(
                marker in line.lower() for marker in ("err", "error", "failed", "unable")
            ):
                diagnostics.append(line[-600:])
                diagnostics = diagnostics[-20:]

        rc = proc.returncode if proc.returncode is not None else await proc.wait()
        if connected:
            reason = f"cloudflared exited (code {rc}) after providing a tunnel URL"
        else:
            reason = f"cloudflared exited (code {rc}) before providing a tunnel URL"
        detail = _cloudflared_failure_detail(diagnostics)
        if detail:
            reason += f": {detail}"
        if proc is self._tunnel_proc:
            await self._tunnel_failed(reason)
        return reason, connected
