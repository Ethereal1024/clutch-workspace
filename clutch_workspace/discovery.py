"""Discovery files: how a thin client finds the daemon serving a workspace.

One daemon per workspace (ratified). The rendezvous is a JSON file in a
user-level directory, named by a hash of the normalized+resolved workspace
path so two spellings of the same directory (different drive-letter case,
relative vs absolute, 8.3 short names) land on the same file:

    <discovery_dir>/d-<sha256(workspace)[:32]>.json
    {"version": 1, "workspace": str, "port": int, "token": hex, "pid": int,
     "started": iso8601}

The default directory is %LOCALAPPDATA%/clutch-workspace, falling back to
~/.clutch-workspace where LOCALAPPDATA is not set (POSIX). Tests and
parallel checkouts can repoint it with CLUTCH_WORKSPACE_DISCOVERY_DIR.

Daemons idle out after ~10 minutes, so discovery files routinely outlive
their daemon; a read that finds a dead pid removes the stale file and
returns None (the client then lazy-starts a fresh daemon). A daemon removes
the file on its way out only while it still names it (remove(..., pid=…)):
a daemon that was replaced cannot unpublish its replacement. Everything here
is stdlib only — the zero-dependency promise holds.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import tempfile
from datetime import datetime, timezone
from pathlib import Path

VERSION = 1
_ENV_DISCOVERY_DIR = "CLUTCH_WORKSPACE_DISCOVERY_DIR"


def discovery_dir() -> Path:
    """Where discovery files live (overridable for tests / parallel checkouts)."""
    override = os.environ.get(_ENV_DISCOVERY_DIR)
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "clutch-workspace"
    return Path.home() / ".clutch-workspace"


def workspace_key(workspace: str) -> str:
    """32 hex chars identifying the workspace, stable across path spellings:
    resolve (symlinks + relative parts + case), then normcase, then hash."""
    normalized = os.path.normcase(str(Path(workspace).resolve()))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


def discovery_file(workspace: str) -> Path:
    return discovery_dir() / f"d-{workspace_key(workspace)}.json"


def write(workspace: str, port: int, token: str | None = None) -> Path:
    """Atomically publish the daemon's coordinates; returns the file written."""
    payload = {
        "version": VERSION,
        "workspace": str(Path(workspace).resolve()),
        "port": int(port),
        "token": token or secrets.token_hex(16),
        "pid": os.getpid(),
        "started": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    final = discovery_file(workspace)
    final.parent.mkdir(parents=True, exist_ok=True)
    # write-then-replace: a client probing mid-write must never parse a half file
    fd, tmp_name = tempfile.mkstemp(dir=str(final.parent), prefix=final.name, suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh)
        os.replace(tmp, final)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return final


def read(workspace: str) -> dict | None:
    """The daemon record for this workspace, or None (with stale-file
    cleanup) when it is missing, corrupt, or its daemon's pid is dead."""
    path = discovery_file(workspace)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        path.unlink(missing_ok=True)
        return None
    if not _wellformed(payload) or not pid_alive(payload["pid"]):
        path.unlink(missing_ok=True)
        return None
    return payload


def remove(workspace: str, pid: int | None = None) -> None:
    """Unpublish this workspace's record.

    `pid` guards the removal on whose record this is: a daemon that was replaced
    while it was still running must not delete its SUCCESSOR's record on the way
    out (the file now names the replacement, and the next client would find
    nothing and start yet another one). Callers that are the daemon itself pass
    its own pid; a caller that owns the record another way passes nothing.
    """
    path = discovery_file(workspace)
    if pid is not None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(payload, dict) or payload.get("pid") != pid:
            return
    path.unlink(missing_ok=True)


def _wellformed(payload: object) -> bool:
    """Just enough shape to be usable: anything else is treated as a stale
    file, never as an error — a client must not die on someone else's junk."""
    if not isinstance(payload, dict) or payload.get("version") != VERSION:
        return False
    port, pid, token = payload.get("port"), payload.get("pid"), payload.get("token")
    return (
        isinstance(port, int)
        and 0 < port <= 65535
        and isinstance(pid, int)
        and pid > 0
        and isinstance(token, str)
        and bool(token)
    )


def pid_alive(pid: int) -> bool:
    """Liveness of a daemon pid. POSIX: signal 0 probes without delivering.
    Windows: os.kill(pid, 0) would TERMINATE the process (it maps to
    TerminateProcess), so query the kernel instead via ctypes."""
    if pid <= 0:
        return False
    if os.name == "nt":
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # exists but belongs to someone else
        return True
    except OSError:
        return False
    return True


def _pid_alive_windows(pid: int) -> bool:
    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:  # ERROR_INVALID_PARAMETER etc. — no such process
        return False
    exit_code = ctypes.c_ulong()
    try:
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
    finally:
        kernel32.CloseHandle(handle)
    return exit_code.value == STILL_ACTIVE
