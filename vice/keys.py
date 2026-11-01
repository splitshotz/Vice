"""Keyboard visualiser: which keys it shows, and the keys kept beside clips.

Only the keys in LAYOUT are ever looked at. A chat message typed mid-game
cannot be read back out of a clip's sidecar, because the letters it would
need were never stored (#209).
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import deque
from pathlib import Path
from typing import Iterable, Optional

from .runtime import actual_home_dir

log = logging.getLogger(__name__)

KEYS_DIR = actual_home_dir() / ".local" / "share" / "vice" / "keys"

# Rows of (key id, label, width in key units). None is a gap. Every row is
# 5.5 units wide.
LAYOUT: list[list[tuple[Optional[str], str, float]]] = [
    [(None, "", 0.5), ("1", "1", 1), ("2", "2", 1), ("3", "3", 1), ("4", "4", 1), ("5", "5", 1)],
    [("TAB", "Tab", 1.5), ("Q", "Q", 1), ("W", "W", 1), ("E", "E", 1), ("R", "R", 1)],
    [("SHIFT", "Shift", 1.5), ("A", "A", 1), ("S", "S", 1), ("D", "D", 1), ("F", "F", 1)],
    [("CTRL", "Ctrl", 1.5), ("SPACE", "", 4)],
]

# evdev names onto layout ids. Left and right modifiers are one key on screen.
EVDEV_TO_KEY: dict[str, str] = {
    **{f"KEY_{n}": n for n in "12345"},
    **{f"KEY_{c}": c for c in "QWERASDF"},
    "KEY_TAB": "TAB",
    "KEY_LEFTSHIFT": "SHIFT",
    "KEY_RIGHTSHIFT": "SHIFT",
    "KEY_LEFTCTRL": "CTRL",
    "KEY_RIGHTCTRL": "CTRL",
    "KEY_SPACE": "SPACE",
}

LAYOUT_KEYS = frozenset(EVDEV_TO_KEY.values())

# Keyboard layouts the panel can be labelled for. Key ids, and so the keys
# saved beside clips, name the physical key evdev reports (KEY_Q is the key
# right of Tab on every keyboard), so a layout only changes what is printed
# on a key, never which key lights up. LAYOUT above is the QWERTY one.
KEY_LAYOUTS = ("qwerty", "azerty")
DEFAULT_KEY_LAYOUT = "qwerty"

# Labels that differ from LAYOUT's, by key id. On AZERTY the physical Q, W and
# A keys print A, Z and Q. The number row keeps 1 to 5, the names games and
# players use for those keys.
_LAYOUT_LABELS: dict[str, dict[str, str]] = {
    "azerty": {"Q": "A", "W": "Z", "A": "Q"},
}


def normalize_key_layout(name: object) -> str:
    """*name* when it is a known layout, the default otherwise."""
    value = str(name or "").strip().lower()
    return value if value in KEY_LAYOUTS else DEFAULT_KEY_LAYOUT


def layout_rows(name: object = DEFAULT_KEY_LAYOUT) -> list[list[tuple[Optional[str], str, float]]]:
    """LAYOUT with the labels of layout *name*. Same keys, same widths."""
    labels = _LAYOUT_LABELS.get(normalize_key_layout(name), {})
    return [[(key, labels.get(key, label) if key is not None else label, span)
             for key, label, span in row] for row in LAYOUT]


class KeyRing:
    """Presses and releases of layout keys over the last *horizon* seconds,
    in memory only."""

    def __init__(self, horizon: float) -> None:
        self.horizon = horizon
        self._events: deque[tuple[float, str, bool]] = deque()
        # Several physical keys can share one layout key (both shifts), so a
        # key is up only when every one of them is.
        self._held: dict[str, set[str]] = {}

    def record(self, evdev_name: str, down: bool, at: Optional[float] = None) -> Optional[tuple[str, bool]]:
        """Note one key event. Returns (layout key, now down) when what the
        layout shows changed, None otherwise."""
        key = EVDEV_TO_KEY.get(evdev_name)
        if key is None:
            return None
        now = time.monotonic() if at is None else at
        held = self._held.setdefault(key, set())
        was_down = bool(held)
        if down:
            held.add(evdev_name)
        else:
            held.discard(evdev_name)
        if bool(held) == was_down:
            return None
        self._events.append((now, key, bool(held)))
        self._trim(now)
        return key, bool(held)

    def _trim(self, now: float) -> None:
        cutoff = now - self.horizon
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

    def intervals(self, start: float, end: float) -> list[list]:
        """[[from, to, key], ...] for every press overlapping start..end,
        relative to start and clamped to the window."""
        open_at: dict[str, float] = {}
        out: list[list] = []
        for at, key, down in self._events:
            if at > end:
                break
            if down:
                open_at[key] = at
            elif key in open_at:
                begun = open_at.pop(key)
                if at >= start:
                    out.append([max(begun, start) - start, at - start, key])
        for key, begun in open_at.items():
            out.append([max(begun, start) - start, end - start, key])
        out.sort(key=lambda item: (item[0], item[2]))
        return [[round(a, 3), round(b, 3), k] for a, b, k in out if b > a]


def sidecar_path(slug: str) -> Path:
    return KEYS_DIR / f"{slug}.json"


def save_sidecar(slug: str, intervals: list[list]) -> None:
    """Write a clip's keys, readable by the user only."""
    KEYS_DIR.mkdir(parents=True, exist_ok=True)
    path = sidecar_path(slug)
    tmp = path.with_suffix(".json.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump({"version": 1, "keys": intervals}, fh)
    tmp.replace(path)


def load_sidecar(slug: str) -> Optional[list[list]]:
    try:
        data = json.loads(sidecar_path(slug).read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log.warning("Cannot read the keys saved with %s: %s", slug, exc)
        return None
    keys = data.get("keys") if isinstance(data, dict) else None
    if not isinstance(keys, list):
        return None
    return [k for k in keys if isinstance(k, list) and len(k) == 3 and k[2] in LAYOUT_KEYS]


def move_sidecar(old_slug: str, new_slug: str) -> None:
    try:
        sidecar_path(old_slug).replace(sidecar_path(new_slug))
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("Could not move the keys saved with %s: %s", old_slug, exc)


def delete_sidecar(slug: str) -> None:
    try:
        sidecar_path(slug).unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("Could not delete the keys saved with %s: %s", slug, exc)


def cut_intervals(intervals: Iterable[list], start: float, end: float) -> list[list]:
    """The part of *intervals* inside start..end, re-based to start, for a
    trim."""
    out = []
    for a, b, key in intervals:
        lo, hi = max(a, start), min(b, end)
        if hi > lo:
            out.append([round(lo - start, 3), round(hi - start, 3), key])
    return out
