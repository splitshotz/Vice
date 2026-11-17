"""Persistent suppression state and host boot identity for game auto-start."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

from .runtime import actual_home_dir, host_argv
from .session_process import ProcessIdentity

log = logging.getLogger(__name__)

STATE_PATH = actual_home_dir() / ".local" / "share" / "vice" / "session_auto_start.json"
_BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"
_STATE_VERSION = 1
_BOOT_ID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


class AutoStartStateError(RuntimeError):
    """The auto-start suppression state could not be read or saved safely."""


class MalformedAutoStartState(AutoStartStateError):
    """State is structurally invalid, rather than inaccessible."""


def read_host_boot_id() -> str:
    """Return the Linux host boot UUID, including when Vice runs in Flatpak."""
    try:
        result = subprocess.run(
            host_argv(["cat", _BOOT_ID_PATH]),
            capture_output=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AutoStartStateError(f"Could not read host boot ID: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise AutoStartStateError(detail or "Could not read host boot ID")
    try:
        raw = result.stdout.decode("ascii", "strict").strip()
    except UnicodeError as exc:
        raise AutoStartStateError("Host boot ID is not valid ASCII") from exc
    if not _BOOT_ID_RE.fullmatch(raw):
        raise AutoStartStateError("Host boot ID is invalid")
    try:
        return str(uuid.UUID(raw))
    except ValueError as exc:
        raise AutoStartStateError("Host boot ID is invalid") from exc


class SuppressionStore:
    """Read and atomically write suppressed process identities for one boot."""

    def __init__(self, boot_id: str, path: Path | None = None) -> None:
        if not isinstance(boot_id, str) or not _BOOT_ID_RE.fullmatch(boot_id):
            raise AutoStartStateError("boot_id must be a valid host boot UUID")
        self.boot_id = str(uuid.UUID(boot_id))
        self.path = Path(path) if path is not None else STATE_PATH

    def load(self) -> set[ProcessIdentity]:
        identities, _ = self.load_snapshot()
        return identities

    def load_snapshot(self) -> tuple[set[ProcessIdentity], int]:
        """Return identities and source version; v2 stop intent is discarded."""
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return set(), _STATE_VERSION
        except UnicodeError as exc:
            raise MalformedAutoStartState(f"Auto-start state is malformed: {exc}") from exc
        except OSError as exc:
            raise AutoStartStateError(f"Could not read auto-start state: {exc}") from exc

        try:
            data = json.loads(raw)
            if not isinstance(data, dict) or "version" not in data:
                raise ValueError("state must be an object with a version")
            version = data["version"]
            if type(version) is not int or version not in (1, 2):
                raise ValueError("unsupported state version")
            fields = {"version", "boot_id", "suppressed"}
            if version == 2:
                fields.add("pending")
            if set(data) != fields:
                raise ValueError("state contains unexpected or missing fields")
            if version == 2 and type(data["pending"]) is not bool:
                raise ValueError("pending must be a boolean")
            if (
                not isinstance(data["boot_id"], str)
                or not _BOOT_ID_RE.fullmatch(data["boot_id"])
                or not isinstance(data["suppressed"], list)
            ):
                raise ValueError("state fields have invalid types")
            identities: set[ProcessIdentity] = set()
            for item in data["suppressed"]:
                if not isinstance(item, dict) or set(item) != {"pid", "start_time"}:
                    raise ValueError("suppressed entry is malformed")
                pid, start_time = item["pid"], item["start_time"]
                if type(pid) is not int or pid <= 0 or type(start_time) is not int or start_time < 0:
                    raise ValueError("suppressed identity is invalid")
                identities.add(ProcessIdentity(pid=pid, start_time=start_time))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise MalformedAutoStartState(f"Auto-start state is malformed: {exc}") from exc

        if str(uuid.UUID(data["boot_id"])) != self.boot_id:
            return set(), version
        return identities, version

    def backup_corrupt(self) -> Path:
        """Preserve invalid bytes privately before replacing a malformed file."""
        backup: Path | None = None
        try:
            fd, name = tempfile.mkstemp(
                prefix=f".{self.path.name}.corrupt.", suffix=".bak",
                dir=self.path.parent,
            )
            backup = Path(name)
            with os.fdopen(fd, "wb") as output, self.path.open("rb") as source:
                shutil.copyfileobj(source, output)
                output.flush()
                os.fsync(output.fileno())
            return backup
        except OSError as exc:
            if backup is not None:
                try:
                    backup.unlink(missing_ok=True)
                except OSError:
                    log.debug("Could not remove incomplete diagnostic backup", exc_info=True)
            raise AutoStartStateError(f"Could not preserve malformed state: {exc}") from exc

    def save(self, identities: set[ProcessIdentity]) -> None:
        if not isinstance(identities, set) or any(
            not isinstance(item, ProcessIdentity)
            or type(item.pid) is not int
            or item.pid <= 0
            or type(item.start_time) is not int
            or item.start_time < 0
            for item in identities
        ):
            raise AutoStartStateError("Cannot save malformed auto-start identities")

        payload = {
            "version": _STATE_VERSION,
            "boot_id": self.boot_id,
            "suppressed": [
                {"pid": item.pid, "start_time": item.start_time}
                for item in sorted(identities, key=lambda identity: (identity.pid, identity.start_time))
            ],
        }
        encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        temp_path: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
            temp_path = Path(temp_name)
            try:
                os.fchmod(fd, 0o600)
            except OSError:
                os.close(fd)
                raise
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, self.path)
            temp_path = None
            try:
                dir_fd = os.open(self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                log.debug("Could not fsync auto-start state directory %s", self.path.parent, exc_info=True)
        except OSError as exc:
            raise AutoStartStateError(f"Could not save auto-start state: {exc}") from exc
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    log.debug("Could not remove temporary auto-start state %s", temp_path, exc_info=True)
