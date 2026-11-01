"""Runs the keyboard overlay process for the daemon and talks to it.

The overlay is a separate process because it needs a GTK main loop and the
daemon is asyncio. It dies with its stdin, so a daemon that goes away takes
the overlay with it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from typing import Callable, Optional

from .keys import DEFAULT_KEY_LAYOUT, normalize_key_layout
from .runtime import has_display

log = logging.getLogger(__name__)

# Reasons the overlay cannot run that a restart will not fix.
PERMANENT = {"no-layer-shell", "no-gtk"}


class KeyOverlayHost:
    def __init__(self, on_moved: Callable[[int, int], None],
                 on_state: Callable[[], None],
                 command: Optional[list[str]] = None) -> None:
        self._command = command or [sys.executable, "-m", "vice.key_overlay"]
        self._on_moved = on_moved
        self._on_state = on_state
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._reader: Optional[asyncio.Task] = None
        self._wanted = False
        self._place: dict = {}
        self._accent = ""
        self._layout = DEFAULT_KEY_LAYOUT
        self._moving = False
        self._starts: list[float] = []
        self.error: Optional[str] = None

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    def state(self) -> dict:
        return {"running": self.running, "error": self.error, "moving": self._moving}

    async def sync(self, enabled: bool, place: dict, accent: str,
                   layout: str = DEFAULT_KEY_LAYOUT) -> None:
        """Bring the overlay in line with the settings."""
        self._wanted = enabled
        layout = normalize_key_layout(layout)
        changed_place = place != self._place
        changed_accent = accent != self._accent
        changed_layout = layout != self._layout
        self._place, self._accent, self._layout = dict(place), accent, layout
        if not enabled:
            self._moving = False
            await self.stop()
            return
        if not self.running:
            await self._start()
            return
        if changed_place:
            self._send({"t": "place", **self._place})
        if changed_accent:
            self._send({"t": "style", "accent": self._accent})
        if changed_layout:
            self._send({"t": "layout", "name": self._layout})

    def key(self, key: str, down: bool) -> None:
        if self.running:
            self._send({"t": "key", "k": key, "d": down})

    def set_moving(self, on: bool) -> None:
        self._moving = bool(on) and self.running
        self._send({"t": "move", "on": self._moving})
        self._on_state()

    async def _start(self) -> None:
        if self.error in PERMANENT:
            return
        if not has_display():
            self.error = "no-display"
            self._on_state()
            return
        now = time.monotonic()
        self._starts = [t for t in self._starts if now - t < 60] + [now]
        if len(self._starts) > 3:
            self.error = "crashed"
            log.warning("The keyboard overlay keeps exiting, not starting it again")
            self._on_state()
            return
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self._command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            self.error = "crashed"
            log.warning("Could not start the keyboard overlay: %s", exc)
            self._on_state()
            return
        self.error = None
        self._send({"t": "style", "accent": self._accent})
        if self._layout != DEFAULT_KEY_LAYOUT:
            # The overlay starts on the default, so only a choice needs saying.
            self._send({"t": "layout", "name": self._layout})
        self._send({"t": "place", **self._place})
        self._reader = asyncio.create_task(self._read(self._proc))
        self._on_state()

    async def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        if proc.returncode is None:
            try:
                if proc.stdin:
                    proc.stdin.close()
                await asyncio.wait_for(proc.wait(), timeout=2)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        if self._reader:
            self._reader.cancel()
            self._reader = None
        self._on_state()

    def _send(self, message: dict) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.returncode is not None:
            return
        try:
            proc.stdin.write((json.dumps(message) + "\n").encode())
        except (BrokenPipeError, ConnectionResetError, RuntimeError) as exc:
            log.debug("Keyboard overlay pipe closed: %s", exc)

    async def _read(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if message.get("t") == "moved":
                self._on_moved(int(message.get("x", 0)), int(message.get("y", 0)))
            elif message.get("t") == "error":
                self.error = str(message.get("reason") or "crashed")
        code = await proc.wait()
        stderr = (await proc.stderr.read()).decode(errors="replace").strip() if proc.stderr else ""
        if proc is not self._proc:
            return
        self._proc = None
        self._moving = False
        if code != 0 and not self.error:
            self.error = "no-gtk" if "No module named 'gi'" in stderr or "Namespace Gtk" in stderr \
                else "crashed"
        if self.error:
            log.warning("Keyboard overlay stopped (%s)%s", self.error,
                        f": {stderr.splitlines()[-1]}" if stderr else "")
        self._on_state()
        if self._wanted and self.error not in PERMANENT:
            await asyncio.sleep(2)
            if self._wanted and not self.running:
                await self._start()
