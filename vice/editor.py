"""
Timeline editor backend, project model, the ffmpeg render graph and the
export job.

A project is the JSON the editor UI autosaves: an ordered track list (text
on top, video lanes, audio at the bottom) plus items placed on those tracks.
Clip identity is the filename stem ("slug"), same as everywhere else, so the
share server calls on_clip_renamed / on_clip_deleted here just like it does
for playlists.

Rendering is a single ffmpeg filter_complex pass built by build_export_cmd,
which is pure (no I/O) so the graph can be golden-tested. The bottom video
track is the main program: segments and black gaps are concatenated, with
xfade joining segments across a transition. Upper video tracks are overlaid
at absolute times, text becomes drawtext, and audio is atrim + adelay + amix
over a silent full-length anchor.
"""

from __future__ import annotations

import asyncio
import logging
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional

from importlib.resources import files as _pkg_files

from .recorder import slugify_clip_name
from .runtime import actual_home_dir

log = logging.getLogger("vice.editor")

PROJECT_PATH = actual_home_dir() / ".local" / "share" / "vice" / "editor_project.json"

# Transition fx ids shared with the UI. xfade covers most; dipaccent has no
# xfade equivalent and is rendered as color fades around a hard cut.
FX_XFADE = {
    "crossfade": "fade",
    "fadeblack": "fadeblack",
    "fadewhite": "fadewhite",
    "blurdis":   "hblur",
    "slide":     "slideleft",
}
FX_NAMES = set(FX_XFADE) | {"dipaccent"}
# Fade color used when a transition starts a lane (nothing to fade from).
# xfade dips these through an opaque colour, which needs an alpha repair when
# the outgoing side of the transition is a transparent gap on an upper track.
FX_OPAQUE_DIP = {"fadeblack", "fadewhite"}

TRANS_MIN = 0.2
TRANS_MAX = 3.0

# UI font keys → shipped static TTFs (drawtext cannot read the woff2 the UI
# uses, so vice/fonts carries matching instances).
FONT_FILES = {
    "display": ("Geist-Regular.ttf", "Geist-Bold.ttf"),
    "body":    ("Inter-Regular.ttf", "Inter-Bold.ttf"),
    "mono":    ("JetBrainsMono-Regular.ttf", "JetBrainsMono-Bold.ttf"),
}

AUDIO_RATE = 48000
FPS = 60
MAX_EXTENT = 3600.0
GAP_EPS = 0.05

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def font_dir() -> Path:
    """Resolve the bundled TTF dir across installed and source checkouts."""
    try:
        cand = Path(str(_pkg_files("vice") / "fonts"))
        if cand.is_dir():
            return cand
    except Exception as exc:
        log.debug("importlib.resources lookup for fonts failed: %s", exc)
    return Path(__file__).resolve().parent / "fonts"


def font_file(fonts: Path, font: str, weight: int) -> Path:
    regular, bold = FONT_FILES.get(font, FONT_FILES["display"])
    return fonts / (bold if weight >= 600 else regular)


@dataclass
class KeyBurn:
    """The keyboard panel to burn into an export (#209): rendered images
    from key_overlay.render_burn_assets and each clip's saved keys."""
    panel: Path
    size: tuple[int, int]
    keys: dict[str, tuple[Path, int, int]]
    intervals: dict[str, list]


@dataclass(frozen=True)
class Source:
    """Per-clip facts the validator and graph builder need."""
    path: Path
    duration: float
    width: int
    height: int
    has_audio: bool
    audio_streams: Optional[int] = None


# ── validation ───────────────────────────────────────────────────────────────

def _r3(x: float) -> float:
    return round(float(x), 3)


def validate_project(raw: dict, sources: dict[str, Source]) -> tuple[dict, list[str]]:
    """Normalize a project for rendering. Returns (project, errors); the
    project is only renderable when errors is empty. Structural problems are
    errors; cosmetic text styling falls back to defaults instead so a stale
    autosave can't brick an export."""
    errors: list[str] = []
    tracks: list[dict] = []
    seen_tracks: set[str] = set()

    for t in raw.get("tracks") or []:
        if not isinstance(t, dict):
            continue
        tid, ttype = str(t.get("id", "")), str(t.get("type", ""))
        if not _ID_RE.match(tid) or ttype not in {"text", "video", "audio"}:
            errors.append(f"track {tid or '?'}: invalid id or type")
            continue
        if tid in seen_tracks:
            errors.append(f"track {tid}: duplicate id")
            continue
        seen_tracks.add(tid)
        tracks.append({"id": tid, "type": ttype,
                       "label": str(t.get("label", tid))[:8] or tid})

    if sum(1 for t in tracks if t["type"] == "text") != 1:
        errors.append("project needs exactly one text track")
    if not any(t["type"] == "video" for t in tracks):
        errors.append("project needs at least one video track")

    track_type = {t["id"]: t["type"] for t in tracks}
    kind_track = {"clip": "video", "audio": "audio", "text": "text"}
    items: list[dict] = []
    seen_items: set[str] = set()

    for it in raw.get("items") or []:
        if not isinstance(it, dict):
            continue
        iid = str(it.get("id", ""))
        kind = str(it.get("kind", ""))
        tid = str(it.get("trackId", ""))
        if not _ID_RE.match(iid) or iid in seen_items:
            errors.append(f"item {iid or '?'}: invalid or duplicate id")
            continue
        seen_items.add(iid)
        if kind not in kind_track or tid not in track_type:
            errors.append(f"item {iid}: unknown kind or track")
            continue
        if track_type[tid] != kind_track[kind]:
            errors.append(f"item {iid}: {kind} does not belong on a {track_type[tid]} track")
            continue
        try:
            start = _r3(max(0.0, float(it.get("start", 0))))
            dur = _r3(float(it.get("dur", 0)))
        except (TypeError, ValueError):
            errors.append(f"item {iid}: bad timing values")
            continue
        if dur <= 0:
            errors.append(f"item {iid}: duration must be positive")
            continue
        out = {"id": iid, "kind": kind, "trackId": tid, "start": start, "dur": dur}

        if kind in ("clip", "audio"):
            cid = str(it.get("clipId", ""))
            src = sources.get(cid)
            if src is None:
                errors.append(f"item {iid}: clip {cid or '?'} is missing")
                continue
            try:
                offset = _r3(max(0.0, float(it.get("offset", 0))))
            except (TypeError, ValueError):
                offset = 0.0
            if src.duration > 0 and offset + dur > src.duration + GAP_EPS:
                errors.append(f"item {iid}: uses more of {cid} than it has")
                continue
            out["clipId"] = cid
            out["offset"] = offset
            if kind == "audio":
                stream = it.get("audioStream", 0)
                count = src.audio_streams if src.audio_streams is not None else int(src.has_audio)
                if type(stream) is not int or not 0 <= stream < count:
                    errors.append(f"item {iid}: audio stream is missing or invalid")
                    continue
                volume = it.get("volume", 1)
                if type(volume) not in (int, float) or not 0 <= volume <= 1:
                    errors.append(f"item {iid}: volume must be between 0 and 1")
                    continue
                if "muted" in it and type(it["muted"]) is not bool:
                    errors.append(f"item {iid}: muted must be a boolean")
                    continue
                for key in ("audioStream", "volume", "muted"):
                    if key in it:
                        out[key] = it[key]
            if kind == "clip":
                out["muted"] = bool(it.get("muted", False))
                trans = it.get("trans")
                if isinstance(trans, dict) and trans.get("fx") in FX_NAMES:
                    try:
                        length = float(trans.get("len", 0))
                    except (TypeError, ValueError):
                        length = TRANS_MIN
                    cap = min(TRANS_MAX, _r3(dur * 0.8))
                    out["trans"] = {"fx": trans["fx"],
                                    "len": _r3(min(max(TRANS_MIN, length), cap))}

        if kind == "text":
            font = it.get("font") if it.get("font") in FONT_FILES else "display"
            color = it.get("color") if _COLOR_RE.match(str(it.get("color", ""))) else "#f2f5fa"
            try:
                size = int(it.get("size", 64))
            except (TypeError, ValueError):
                size = 64
            try:
                weight = int(it.get("weight", 600))
            except (TypeError, ValueError):
                weight = 600
            out.update({
                "text": str(it.get("text", ""))[:200],
                "font": font,
                "size": min(1000, max(8, size)),
                "weight": min(800, max(100, weight)),
                "color": color,
                "x": _r3(min(100.0, max(0.0, float(it.get("x", 50) or 0)))),
                "y": _r3(min(100.0, max(0.0, float(it.get("y", 50) or 0)))),
            })
        items.append(out)

    order = {t["id"]: i for i, t in enumerate(tracks)}
    items.sort(key=lambda i: (order.get(i["trackId"], 99), i["start"], i["id"]))

    by_track: dict[str, list[dict]] = {}
    for it in items:
        by_track.setdefault(it["trackId"], []).append(it)
    for tid, lane in by_track.items():
        prev = None
        for it in lane:
            if prev and it["start"] < prev["start"] + prev["dur"] - 1e-6:
                errors.append(f"item {it['id']}: overlaps {prev['id']} on {tid}")
            prev = it

    project = {"version": 1, "tracks": tracks, "items": items}
    extent = project_extent(project)
    if not items:
        errors.append("timeline is empty")
    elif extent > MAX_EXTENT:
        errors.append("timeline is longer than an hour")
    return project, errors


def project_extent(project: dict) -> float:
    return _r3(max((i["start"] + i["dur"] for i in project.get("items", [])), default=0.0))


def canvas_for(project: dict, sources: dict[str, Source]) -> tuple[int, int, int]:
    """Canvas follows the first clip on the main (bottom) video track so the
    common single-source edit exports at native resolution; mixed sources get
    scaled and padded to that canvas."""
    video_tracks = [t["id"] for t in project["tracks"] if t["type"] == "video"]
    main = video_tracks[-1] if video_tracks else None
    w, h = 1920, 1080
    for it in project["items"]:
        if it["trackId"] == main and it["kind"] == "clip":
            src = sources.get(it.get("clipId", ""))
            if src and src.width > 0 and src.height > 0:
                w, h = src.width, src.height
            break
    return w - w % 2, h - h % 2, FPS


# ── graph builder ────────────────────────────────────────────────────────────

def _n(x: float) -> str:
    s = f"{x:.3f}".rstrip("0").rstrip(".")
    return s or "0"


def _q(value: str) -> str:
    """Quote a filter option value for the filtergraph parser."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _hexcolor(color: str) -> str:
    return "0x" + color.lstrip("#").lower()


def text_file_path(text_dir: Path, item_id: str) -> Path:
    return text_dir / f"text_{item_id}.txt"


def text_file_contents(project: dict, text_dir: Path) -> dict[Path, str]:
    """Title texts go through drawtext textfile=, which sidesteps the whole
    filtergraph escaping mess and keeps arbitrary user text safe."""
    return {
        text_file_path(text_dir, it["id"]): it.get("text", "")
        for it in project["items"] if it["kind"] == "text"
    }


def _segment(idx: int, offset: float, dur: float, w: int, h: int,
             extra: str = "", pix: str = "yuv420p") -> str:
    return (f"[{idx}:v]trim=start={_n(offset)}:end={_n(offset + dur)},"
            f"setpts=PTS-STARTPTS,fps={FPS},"
            f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,settb=AVTB,"
            f"format={pix}{extra}")


def _gap(dur: float, w: int, h: int, *, alpha: bool = False) -> str:
    """Filler for the empty stretches of a track. Upper tracks fill with
    transparent frames so the tracks below show through the overlay."""
    return (f"color=c={'black@0' if alpha else 'black'}:s={w}x{h}:r={FPS}:"
            f"d={_n(dur)},settb=AVTB,"
            f"format={'yuva420p' if alpha else 'yuv420p'}")


class _GraphCtx:
    """Shared output buffer and label counter for one export graph."""

    def __init__(self, w: int, h: int, accent: str,
                 sources: dict[str, Source], input_idx: dict[str, int]) -> None:
        self.w, self.h, self.accent = w, h, accent
        self.sources, self.input_idx = sources, input_idx
        self.lines: list[str] = []
        self._seq = 0

    def label(self, prefix: str) -> str:
        lbl = f"{prefix}{self._seq}"
        self._seq += 1
        return lbl


class _VideoChain:
    """One video track rendered as a single stream: clip segments, gap
    fillers and real transitions in timeline order.

    Every track is built this way. The program track fills its gaps with
    opaque black; upper tracks fill them with transparent frames and are
    then composited with overlay, which is what gives clips on an upper
    track the same transitions the program track has always had.
    """

    def __init__(self, ctx: _GraphCtx, *, transparent: bool) -> None:
        self.ctx = ctx
        self.transparent = transparent
        self.pix = "yuva420p" if transparent else "yuv420p"
        self.cur: Optional[str] = None
        self.pending: list[dict] = []
        self.end = 0.0
        self.tail: Optional[str] = None   # kind of the last element added

    # ── emitters ─────────────────────────────────────────────────────────

    def _emit(self, el: dict, ext: float = 0.0,
              prefix: str = "", suffix: str = "") -> str:
        ctx = self.ctx
        lbl = ctx.label("s")
        if el["kind"] == "gap":
            ctx.lines.append(_gap(el["dur"] + ext, ctx.w, ctx.h,
                                  alpha=self.transparent) + f"{suffix}[{lbl}]")
            return lbl
        it = el["it"]
        src = ctx.sources[it["clipId"]]
        dur = it["dur"]
        media_ext = 0.0
        if ext > 0:
            media_ext = min(ext, max(0.0, _r3(src.duration - it["offset"] - dur)))
        extra = prefix + suffix
        if ext - media_ext > 1e-3:
            extra += f",tpad=stop_mode=clone:stop_duration={_n(ext - media_ext)}"
        ctx.lines.append(_segment(ctx.input_idx[it["clipId"]], it["offset"],
                                  dur + media_ext, ctx.w, ctx.h, extra,
                                  self.pix) + f"[{lbl}]")
        return lbl

    def _pipe(self, expr: str) -> None:
        lbl = self.ctx.label("s")
        self.ctx.lines.append(f"[{self.cur}]{expr}[{lbl}]")
        self.cur = lbl

    def _flush(self) -> None:
        emitted = [self._emit(el, el.get("_ext", 0.0),
                              el.get("_pre", ""), el.get("_suf", ""))
                   for el in self.pending]
        self.pending = []
        parts = ([self.cur] if self.cur else []) + emitted
        if not parts:
            return
        if len(parts) == 1:
            self.cur = parts[0]
            return
        lbl = self.ctx.label("c")
        self.ctx.lines.append("".join(f"[{p}]" for p in parts)
                              + f"concat=n={len(parts)}:v=1:a=0[{lbl}]")
        self.cur = lbl

    def _xfade(self, left: str, right: str, fx: str,
               length: float, offset: float) -> None:
        expr = (f"[{left}][{right}]xfade=transition={FX_XFADE[fx]}:"
                f"duration={_n(length)}:offset={_n(offset)}")
        # xfade dips through opaque black/white by design. Over a transparent
        # left side that would punch a hole in an upper track, so the alpha is
        # ramped back in.
        if self.transparent and fx in FX_OPAQUE_DIP and self.tail != "clip":
            expr += f",fade=t=in:st={_n(offset)}:d={_n(length)}:alpha=1"
        lbl = self.ctx.label("x")
        self.ctx.lines.append(expr + f"[{lbl}]")
        self.cur = lbl

    # ── junctions ────────────────────────────────────────────────────────

    def _junction(self, el: dict, trans: dict) -> None:
        """A transition with something already to its left."""
        fx, length = trans["fx"], trans["len"]
        it = el["it"]
        if fx == "dipaccent":
            half = _r3(length / 2)
            dip = _hexcolor(self.ctx.accent)
            if self.pending:
                left = self.pending[-1]
                left_dur = left["dur"] if left["kind"] == "gap" else left["it"]["dur"]
                left["_suf"] = (left.get("_suf", "")
                                + f",fade=t=out:st={_n(left_dur - half)}:"
                                  f"d={_n(half)}:color={dip}")
            else:
                self._pipe(f"fade=t=out:st={_n(it['start'] - half)}:"
                           f"d={_n(half)}:color={dip}")
            el["_pre"] = f",fade=t=in:st=0:d={_n(half)}:color={dip}"
            self.pending.append(el)
            return
        # The left side is extended by the transition length (real media when
        # the source has it, frozen last frame otherwise) so absolute timeline
        # positions never shift.
        if self.pending:
            self.pending[-1]["_ext"] = length
        else:
            self._pipe(f"tpad=stop_mode=clone:stop_duration={_n(length)}")
        left_kind = self.tail
        self._flush()
        right = self._emit(el)
        self.tail = left_kind
        self._xfade(self.cur, right, fx, length, it["start"])

    def _lead(self, el: dict, trans: dict) -> None:
        """A transition with nothing to its left: xfade against a synthesised
        lead-in so slide and blur keep their motion instead of collapsing
        into a plain fade."""
        fx, length = trans["fx"], trans["len"]
        if fx == "dipaccent":
            el["_pre"] = (f",fade=t=in:st=0:d={_n(length)}"
                          f":color={_hexcolor(self.ctx.accent)}")
            self.pending.append(el)
            return
        lead = self.ctx.label("s")
        self.ctx.lines.append(_gap(length, self.ctx.w, self.ctx.h,
                                   alpha=self.transparent) + f"[{lead}]")
        right = self._emit(el)
        self.tail = "gap"
        self._xfade(lead, right, fx, length, 0.0)

    # ── build ────────────────────────────────────────────────────────────

    def build(self, items: list[dict], extent: float,
              *, pad_tail: bool) -> Optional[str]:
        """Return the label of this track's stream, or None when it is empty."""
        elements: list[dict] = []
        cursor = 0.0
        for it in items:
            if it["start"] - cursor > GAP_EPS:
                elements.append({"kind": "gap", "dur": _r3(it["start"] - cursor)})
            elements.append({"kind": "clip", "it": it})
            cursor = it["start"] + it["dur"]
        if not elements:
            return None

        for el in elements:
            it = el.get("it")
            trans = it.get("trans") if it else None
            if trans and (self.pending or self.cur):
                self._junction(el, trans)
            elif trans:
                self._lead(el, trans)
            else:
                self.pending.append(el)
            self.end = (it["start"] + it["dur"]) if it else self.end + el["dur"]
            self.tail = el["kind"]

        self._flush()
        if pad_tail and extent - self.end > 1e-3:
            self._pipe(f"tpad=stop_mode=add:stop_duration={_n(extent - self.end)}")
        return self.cur


# Discord's plain (non-Nitro) upload cap. The margin covers container
# overhead and single-pass ABR variance so the file lands under it in one
# encode rather than needing a slower two-pass run.
DISCORD_MAX_MB = 20
DISCORD_MAX_BYTES = DISCORD_MAX_MB * 1024 * 1024
DISCORD_SIZE_MARGIN = 0.92
DISCORD_AUDIO_KBPS = 128
DISCORD_MIN_VIDEO_KBPS = 150


class DiscordTooLarge(Exception):
    """No bitrate fits this clip under the cap at a watchable quality."""


def discord_bitrates(duration: float, audio_streams: int = 1,
                     max_mb: int = DISCORD_MAX_MB) -> tuple[int, int]:
    """(video kbps, per-track audio kbps) that fit *max_mb*.

    Raises DiscordTooLarge when audio plus the video floor already overruns
    the cap. Clamping to the floor and encoding anyway produced files far
    over it: 1800s with one audio track budgets 62 MB at the floor.
    """
    audio_kbps = DISCORD_AUDIO_KBPS * max(audio_streams, 1)
    budget_kbps = max_mb * 8192 * DISCORD_SIZE_MARGIN / max(duration, 1)
    video_kbps = round(budget_kbps - audio_kbps)
    if video_kbps < DISCORD_MIN_VIDEO_KBPS:
        raise DiscordTooLarge(
            f"{duration:.0f}s with {max(audio_streams, 1)} audio track(s) "
            f"cannot fit {max_mb} MB. Trim it shorter first.")
    return video_kbps, DISCORD_AUDIO_KBPS


def build_export_cmd(project: dict, sources: dict[str, Source], out_path: Path,
                     *, accent: str = "#0099ff", fonts: Optional[Path] = None,
                     text_dir: Optional[Path] = None,
                     discord_optimized: bool = False,
                     key_burn: Optional[KeyBurn] = None,
                     discord_max_mb: int = DISCORD_MAX_MB) -> list[str]:
    """Build the full ffmpeg argv for a validated project. Pure: callers
    write out the text files (text_file_contents) before running it."""
    fonts = fonts or font_dir()
    text_dir = text_dir or PROJECT_PATH.parent
    w, h, _ = canvas_for(project, sources)
    extent = project_extent(project)
    if not _COLOR_RE.match(accent):
        accent = "#0099ff"

    video_tracks = [t["id"] for t in project["tracks"] if t["type"] == "video"]
    main_id = video_tracks[-1]
    lane = lambda tid: [i for i in project["items"] if i["trackId"] == tid]

    input_order: list[str] = []
    for it in project["items"]:
        cid = it.get("clipId")
        if cid and cid not in input_order:
            input_order.append(cid)
    input_idx = {cid: i for i, cid in enumerate(input_order)}

    ctx = _GraphCtx(w, h, accent, sources, input_idx)
    lines = ctx.lines

    # The program (bottom) track is a full-length opaque stream.
    cur = _VideoChain(ctx, transparent=False).build(lane(main_id), extent,
                                                    pad_tail=True)
    if cur is None:
        cur = ctx.label("s")
        lines.append(_gap(extent, w, h) + f"[{cur}]")

    # Upper tracks are the same chain with transparent gaps, composited over
    # the program. Alpha does the masking, so no enable window is needed. The
    # earliest listed track is overlaid last so it wins, matching the preview.
    for tid in reversed(video_tracks[:-1]):
        upper = _VideoChain(ctx, transparent=True).build(lane(tid), extent,
                                                         pad_tail=False)
        if upper is None:
            continue
        lbl = ctx.label("o")
        lines.append(f"[{cur}][{upper}]overlay=eof_action=pass[{lbl}]")
        cur = lbl

    # Titles: drawtext over the composited program, reading the text from a
    # sidecar file per item (no filtergraph escaping of user text).
    for it in project["items"]:
        if it["kind"] != "text":
            continue
        ff = font_file(fonts, it["font"], it["weight"])
        fs = max(8, round(it["size"] * h / 1080))
        lbl = ctx.label("t")
        lines.append(
            f"[{cur}]drawtext=expansion=none:fontfile={_q(str(ff))}:"
            f"textfile={_q(str(text_file_path(text_dir, it['id'])))}:"
            f"fontsize={fs}:fontcolor={_hexcolor(it['color'])}:"
            f"shadowcolor=black@0.5:shadowx=2:shadowy=2:"
            f"x=(w*{_n(it['x'])}/100)-(text_w/2):"
            f"y=(h*{_n(it['y'])}/100)-(text_h/2):"
            f"enable='between(t,{_n(it['start'])},{_n(it['start'] + it['dur'])})'"
            f"[{lbl}]")
        cur = lbl

    image_inputs: list[Path] = []
    if key_burn is not None:
        cur = _burn_keys(lines, ctx, cur, key_burn, lane(main_id), w, h,
                         len(input_order), image_inputs)

    vout = cur

    # Audio: every unmuted clip item plus every audio item mixes over a
    # silent anchor that pins the output length.
    contribs = [it for it in project["items"]
                if it.get("clipId") and sources[it["clipId"]].has_audio
                and it["kind"] in ("audio", "clip") and not it.get("muted")]
    contribs.sort(key=lambda i: (i["start"], i["id"]))
    lines.append(f"anullsrc=r={AUDIO_RATE}:cl=stereo,atrim=0:{_n(extent)}[ab]")
    alabels = ["ab"]
    for k, it in enumerate(contribs):
        lines.append(
            f"[{input_idx[it['clipId']]}:a:{it.get('audioStream', 0)}]"
            + (f"aresample={AUDIO_RATE}:async=1:first_pts=0," if it["kind"] == "audio" else "") +
            f"atrim=start={_n(it['offset'])}:end={_n(it['offset'] + it['dur'])},"
            f"asetpts=PTS-STARTPTS,"
            f"aformat=sample_rates={AUDIO_RATE}:channel_layouts=stereo,"
            + (f"volume={_n(it['volume'])}," if it.get("volume", 1) != 1 else "") +
            f"adelay={round(it['start'] * 1000)}:all=1[a{k}]")
        alabels.append(f"a{k}")
    if len(alabels) == 1:
        lines.append("[ab]anull[aout]")
    else:
        lines.append("".join(f"[{a}]" for a in alabels)
                     + f"amix=inputs={len(alabels)}:duration=longest:normalize=0,"
                     f"atrim=0:{_n(extent)},asetpts=PTS-STARTPTS[aout]")

    if discord_optimized:
        # One mixed track out, whatever the sources had.
        video_kbps, audio_kbps = discord_bitrates(extent, 1, discord_max_mb)
        video_args = ["-c:v", "libx264", "-preset", "medium",
                     "-b:v", f"{video_kbps}k",
                     "-maxrate", f"{round(video_kbps * 1.45)}k",
                     "-bufsize", f"{video_kbps * 2}k"]
        audio_args = ["-c:a", "aac", "-b:a", f"{audio_kbps}k"]
    else:
        video_args = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]
        audio_args = ["-c:a", "aac", "-b:a", "192k"]

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats",
           "-progress", "pipe:1"]
    for cid in input_order:
        cmd += ["-i", str(sources[cid].path)]
    for image in image_inputs:
        cmd += ["-loop", "1", "-t", _n(extent), "-i", str(image)]
    cmd += [
        "-filter_complex", ";".join(lines),
        "-map", f"[{vout}]", "-map", "[aout]",
        *video_args,
        "-pix_fmt", "yuv420p",
        *audio_args,
        "-movflags", "+faststart",
        "-t", _n(extent),
        "-f", "mp4",
        "-y", str(out_path),
    ]
    return cmd


def _windows(spans: list[tuple[float, float]]) -> str:
    return "+".join(f"between(t,{_n(a)},{_n(b)})" for a, b in spans)


def _burn_keys(lines: list[str], ctx: "_GraphCtx", cur: str, burn: KeyBurn,
               program: list[dict], w: int, h: int, first_input: int,
               image_inputs: list[Path]) -> str:
    """Lay the keyboard panel over the program while a clip with saved keys
    is on it, and each held key over the panel while it was held. Only the
    program track counts, since that is the footage being watched."""
    panel_spans: list[tuple[float, float]] = []
    held: dict[str, list[tuple[float, float]]] = {}
    for it in program:
        if it["kind"] != "clip" or it.get("clipId") not in burn.intervals:
            continue
        start, end = it["start"], it["start"] + it["dur"]
        panel_spans.append((start, end))
        for a, b, key in burn.intervals[it["clipId"]]:
            ta = max(start, start + a - it["offset"])
            tb = min(end, start + b - it["offset"])
            if tb > ta and key in burn.keys:
                held.setdefault(key, []).append((_r3(ta), _r3(tb)))
    if not panel_spans:
        return cur
    # Bottom left, the overlay's own default, sized to the export.
    pw, ph = burn.size
    px, py = round(32 * h / 1080), h - ph - round(32 * h / 1080)
    image_inputs.append(burn.panel)
    lbl = ctx.label("k")
    lines.append(f"[{cur}][{first_input}:v]overlay=x={px}:y={py}:"
                 f"enable='{_windows(panel_spans)}'[{lbl}]")
    cur = lbl
    for key in sorted(held):
        path, kx, ky = burn.keys[key]
        image_inputs.append(path)
        lbl = ctx.label("k")
        lines.append(f"[{cur}][{first_input + len(image_inputs) - 1}:v]"
                     f"overlay=x={px + kx}:y={py + ky}:enable='{_windows(held[key])}'[{lbl}]")
        cur = lbl
    return cur


# ── export naming ────────────────────────────────────────────────────────────

def sanitize_export_name(name: str) -> Optional[str]:
    """Same rules as clip rename. Returns the final filename (always .mp4) or
    None when nothing usable is left."""
    stem = slugify_clip_name(name)
    return f"{stem}.mp4" if stem else None


def default_export_name(out_dir: Path) -> str:
    n = 1
    while (out_dir / f"Vice_Edit_{n}.mp4").exists() or \
          (out_dir / f"Vice_Edit_{n}.mkv").exists():
        n += 1
    return f"Vice_Edit_{n}.mp4"


# ── project store ────────────────────────────────────────────────────────────

class EditorProjectStore:
    """The single autosaved editor project. Follows clips through renames and
    drops their items on delete, mirroring the playlist store hooks."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or PROJECT_PATH

    def load(self) -> Optional[dict]:
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text())
            return data if isinstance(data, dict) else None
        except Exception as exc:
            log.warning("Editor project file %s is unreadable: %s", self.path, exc)
            return None

    def save(self, project: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(project))
        tmp.replace(self.path)

    def _mutate_items(self, fn) -> bool:
        project = self.load()
        if not project or not isinstance(project.get("items"), list):
            return False
        changed = fn(project["items"])
        if changed:
            self.save(project)
        return changed

    def on_clip_renamed(self, old_slug: str, new_slug: str) -> bool:
        def rename(items: list) -> bool:
            changed = False
            for it in items:
                if isinstance(it, dict) and it.get("clipId") == old_slug:
                    it["clipId"] = new_slug
                    changed = True
            return changed
        return self._mutate_items(rename)

    def on_clip_deleted(self, slug: str) -> bool:
        def drop(items: list) -> bool:
            kept = [it for it in items
                    if not (isinstance(it, dict) and it.get("clipId") == slug)]
            if len(kept) == len(items):
                return False
            items[:] = kept
            return True
        return self._mutate_items(drop)


# ── export job ───────────────────────────────────────────────────────────────

def parse_progress(line: str, total: float) -> Optional[float]:
    """Progress fraction from one `ffmpeg -progress` line, or None."""
    if line.startswith("progress=") and line.split("=", 1)[1] == "end":
        return 1.0
    if line.startswith("out_time_us=") and total > 0:
        try:
            return min(1.0, int(line.split("=", 1)[1]) / 1_000_000 / total)
        except ValueError:
            return None
    return None


class ExportBusy(Exception):
    pass


class ExportManager:
    """Runs at most one export at a time, streaming progress to the UI over
    the share server's WebSocket."""

    def __init__(self, broadcast: Callable[[dict], Awaitable[None]]) -> None:
        self._broadcast = broadcast
        self._job_id: Optional[str] = None
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._task: Optional[asyncio.Task] = None
        self._canceled = False
        self._started = False
        self._committed = False
        self._stopping = False

    @property
    def busy(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self, job_id: str, cmd: list[str], total: float,
              tmp_path: Path, final_path: Path,
              on_done: Optional[Callable[[Path], Awaitable[Optional[dict]]]] = None,
              cleanup: Optional[Callable[[], None]] = None,
              max_bytes: Optional[int] = None) -> None:
        if self.busy or self._stopping:
            raise ExportBusy()
        self._job_id = job_id
        self._canceled = False
        self._started = False
        self._committed = False
        self._proc = None
        self._task = asyncio.create_task(
            self._run(job_id, cmd, total, tmp_path, final_path, on_done, cleanup,
                      max_bytes))

    async def _run(self, job_id: str, cmd: list[str], total: float,
                   tmp: Path, final: Path, on_done, cleanup,
                   max_bytes: Optional[int] = None) -> None:
        self._started = True
        proc = None
        spawn_task = None
        err_task = None
        try:
            if self._canceled:
                return
            try:
                # Cancellation must not lose ownership between spawning the
                # process and receiving its handle. Cleanup awaits this task.
                spawn_task = asyncio.create_task(asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                ))
                proc = await asyncio.shield(spawn_task)
            except FileNotFoundError:
                await self._broadcast({"type": "export_error", "job_id": job_id,
                                       "error": "ffmpeg not found", "canceled": False})
                return
            self._proc = proc

            stderr_tail = bytearray()

            async def drain_stderr() -> None:
                while True:
                    chunk = await proc.stderr.read(4096)
                    if not chunk:
                        return
                    stderr_tail.extend(chunk)
                    del stderr_tail[:-4096]

            err_task = asyncio.create_task(drain_stderr())
            last_pct, last_sent = -1, 0.0
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                frac = parse_progress(line.decode(errors="replace").strip(), total)
                if frac is None:
                    continue
                pct, now = int(frac * 100), time.monotonic()
                if pct != last_pct and (now - last_sent >= 0.25 or pct >= 100):
                    last_pct, last_sent = pct, now
                    await self._broadcast({"type": "export_progress",
                                           "job_id": job_id,
                                           "progress": round(frac, 3)})
            await proc.wait()
            await err_task

            if proc.returncode != 0 or not tmp.exists():
                tmp.unlink(missing_ok=True)
                err = bytes(stderr_tail).decode(errors="replace").strip()[-300:]
                await self._broadcast({
                    "type": "export_error", "job_id": job_id,
                    "error": err or f"ffmpeg exited with {proc.returncode}",
                    "canceled": False,
                })
                return

            # A bitrate target is a request, not a guarantee: check what the
            # encoder actually produced before it becomes the export.
            size = tmp.stat().st_size if max_bytes is not None else 0
            if max_bytes is not None and size > max_bytes:
                await self._broadcast({
                    "type": "export_error", "job_id": job_id,
                    "error": f"the export came out at {size / 1048576:.1f} MB, "
                             f"over the {max_bytes // 1048576} MB limit",
                    "canceled": False,
                })
                return

            tmp.replace(final)
            self._committed = True
            clip = None
            if on_done:
                try:
                    clip = await on_done(final)
                except Exception as exc:
                    log.warning("Export post-processing failed: %s", exc)
            await self._broadcast({"type": "export_done", "job_id": job_id,
                                   "path": str(final), "clip": clip})
        except asyncio.CancelledError:
            self._canceled = True
            raise
        except Exception as exc:
            log.warning("Export %s failed: %s", job_id, exc)
            await self._broadcast({"type": "export_error", "job_id": job_id,
                                   "error": str(exc), "canceled": False})
        finally:
            if proc is None and spawn_task is not None:
                try:
                    proc = await spawn_task
                    self._proc = proc
                except OSError:
                    pass  # Spawn failures are reported by the main path.
            if proc is not None and proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            if err_task is not None:
                err_task.cancel()
                await asyncio.gather(err_task, return_exceptions=True)
            if proc is not None:
                # The progress reader has stopped. Drain both pipes while
                # reaping, including when cancellation interrupted a read.
                await proc.communicate()
            try:
                tmp.unlink(missing_ok=True)
            except OSError as exc:
                log.debug("Could not remove export temporary file %s: %s", tmp, exc)
            if cleanup:
                try:
                    cleanup()
                except Exception as exc:
                    log.debug("Export cleanup failed: %s", exc)
            if self._canceled:
                await self._broadcast({"type": "export_error", "job_id": job_id,
                                       "error": "export canceled", "canceled": True})

    async def cancel(self, job_id: str) -> bool:
        if job_id != self._job_id or not self.busy or self._committed:
            return False
        if not self._canceled:
            self._canceled = True
            # Let a task that has not run enter its cleanup block. Once started,
            # cancellation interrupts progress reads and reaps the owned child.
            # Repeated requests must not interrupt cleanup with another cancel.
            if self._started:
                self._task.cancel()
        await asyncio.shield(asyncio.gather(self._task, return_exceptions=True))
        return True

    async def stop(self) -> None:
        """Finish owned work before the server closes and reject new exports."""
        self._stopping = True
        if self.busy:
            await self.cancel(self._job_id)
            # A file already committed to its final name must finish its
            # registration callback instead of being reported as canceled.
            await asyncio.shield(asyncio.gather(self._task, return_exceptions=True))
