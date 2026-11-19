"""Bind and probe the process that owns a focused game window."""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .runtime import host_argv, in_vice_flatpak

log = logging.getLogger(__name__)

ProcessStatus = Literal["running", "exited", "unknown"]
_PROXY_COMMS = frozenset(
    name[:15]
    for name in (
        "xwayland-satellite", "Xwayland", "steam", "steamwebhelper", "wineserver",
    )
)
_DEAD_STATES = frozenset({"Z", "X", "x"})
_PROC_STATES = frozenset({"R", "S", "D", "Z", "T", "t", "W", "X", "x", "K", "P", "I"})
_HOST_MISSING_MARKER = b"__VICE_PROC_STAT_MISSING__"
_HOST_PROC_STAT_SCRIPT = (
    'if [ ! -r /proc/self/stat ]; then exit 2; fi; '
    'found=0; for entry in /proc/[0-9]*; do '
    '[ "${entry##*/}" = "$1" ] && found=1 && break; done; '
    'if [ "$found" -ne 1 ]; then printf "%s\\n" "__VICE_PROC_STAT_MISSING__"; exit 0; fi; '
    'IFS= read -r line < "/proc/$1/stat" || exit 3; printf "%s\\n" "$line"'
)


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    start_time: int


def _parse_stat(raw: bytes) -> tuple[int, str, str, int] | None:
    """Return (pid, comm, state, starttime), tolerating ')' in comm."""
    try:
        line = raw.decode("utf-8", "strict").strip()
        pid_text, remainder = line.split(" ", 1)
        pid = int(pid_text)
        # comm is parenthesized but may itself contain spaces and parentheses.
        if not remainder.startswith("("):
            return None
        comm, fields_text = remainder[1:].rsplit(")", 1)
        fields = fields_text.split()
        state = fields[0]
        start_time = int(fields[19])  # field 22; fields begin at field 3.
    except (UnicodeError, ValueError, IndexError):
        return None
    if (pid <= 0 or not comm or len(fields) < 20 or state not in _PROC_STATES
            or start_time < 0):
        return None
    return pid, comm, state, start_time


def _read_native_stat(pid: int) -> tuple[bytes | None, bool]:
    """Return (contents, confirmed_missing); other read errors stay unknown."""
    try:
        return Path(f"/proc/{pid}/stat").read_bytes(), False
    except FileNotFoundError:
        return None, True
    except OSError as exc:
        log.debug("Cannot read /proc/%s/stat: %s", pid, exc)
        return None, False


def _read_host_stat(pid: int) -> tuple[bytes | None, bool]:
    """Read host proc stat while preserving missing vs command/permission errors."""
    try:
        result = subprocess.run(
            host_argv(["sh", "-c", _HOST_PROC_STAT_SCRIPT, "sh", str(pid)]),
            capture_output=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("Cannot query host /proc/%s/stat: %s", pid, exc)
        return None, False
    if result.returncode == 0:
        if result.stdout.strip() == _HOST_MISSING_MARKER:
            return None, True
        return result.stdout, False
    log.debug("Host /proc/%s/stat query failed: %s", pid, result.stderr)
    return None, False


def _read_stat(pid: int) -> tuple[bytes | None, bool]:
    if in_vice_flatpak():
        return _read_host_stat(pid)
    return _read_native_stat(pid)


def bind_game_process(win: dict) -> ProcessIdentity | None:
    """Bind a window's PID to its current Linux process start time.

    Proxy/compositor processes cannot identify the game, so they are rejected.
    """
    if not isinstance(win, dict):
        return None
    pid_value = win.get("pid")
    if isinstance(pid_value, bool):
        return None
    if isinstance(pid_value, int):
        pid = pid_value
    elif isinstance(pid_value, str) and pid_value.strip().isdigit():
        pid = int(pid_value.strip())
    else:
        return None
    if pid <= 0:
        return None
    raw, missing = _read_stat(pid)
    if raw is None or missing:
        return None
    parsed = _parse_stat(raw)
    if parsed is None:
        return None
    parsed_pid, comm, _state, start_time = parsed
    if parsed_pid != pid or _state in _DEAD_STATES or comm[:15] in _PROXY_COMMS:
        return None
    return ProcessIdentity(pid=pid, start_time=start_time)


def probe_game_process(identity: ProcessIdentity) -> ProcessStatus:
    """Report whether the bound PID still names the same live process."""
    if not isinstance(identity, ProcessIdentity) or identity.pid <= 0:
        return "unknown"
    raw, missing = _read_stat(identity.pid)
    if missing:
        return "exited"
    if raw is None:
        return "unknown"
    parsed = _parse_stat(raw)
    if parsed is None:
        return "unknown"
    pid, _comm, state, start_time = parsed
    if pid != identity.pid:
        return "unknown"
    if start_time != identity.start_time:
        return "exited"
    return "exited" if state in _DEAD_STATES else "running"
