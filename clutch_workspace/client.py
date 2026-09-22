"""Thin-client plumbing: find (or lazily start) the workspace daemon and
forward one command over HTTP.

Policy (ratified): the daemon IS the CLI's execution engine. Every command
rides the per-workspace daemon; when none is alive the first CLI call spawns
one detached and waits up to LAZY_START_SECONDS for it to publish itself.
There is NO in-process fallback — a command that cannot reach or start a
daemon fails with a sysexits code (66 for a missing workspace dir, 74 for
any other unavailable daemon) instead of quietly executing locally.
A daemon that dies MID-command is NOT retried either: the command may have
half-happened and the commands are not all idempotent (edit_file is not), so
a lost connection surfaces as exit 74.

Stdlib only (urllib, subprocess, secrets) — the zero-dependency promise
holds; the daemon must never import the host repo's procmgr.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import discovery
from .exitcodes import EX_IOERR, EX_NOINPUT

LAZY_START_SECONDS = 10.0  # how long a client waits for a daemon it just spawned
PROBE_TIMEOUT = 2.0  # /health budget — a live daemon answers instantly
COMMAND_TIMEOUT = 300.0  # command budget; the daemon itself idles out at 600
_TOKEN_HEADER = "X-Clutch-Token"


class DaemonUnreachable(Exception):
    """The daemon accepted the connection but died before answering."""


class DaemonUnavailable(Exception):
    """No usable daemon for this workspace and none could be started. The CLI
    has no other way to execute a command, so this is a hard failure carrying
    the sysexits code the CLI exits with."""

    def __init__(self, message: str, code: int = EX_IOERR) -> None:
        super().__init__(message)
        self.code = code


def endpoint(workspace: str) -> tuple[str, str]:
    """(base_url, token) for this workspace's daemon, lazy-starting one when
    absent. Raises DaemonUnavailable when there is no daemon and none can be
    started — the daemon is the CLI's only execution path."""
    if not os.path.isdir(workspace):
        raise DaemonUnavailable(f"workspace is not a directory: {workspace}", EX_NOINPUT)
    record = discovery.read(workspace)
    if record and _healthy(record):
        return _base(record), record["token"]
    if record:
        # published but not answering: a daemon that died in the last
        # moments — drop the entry so the fresh spawn below owns the file
        discovery.remove(workspace)
    return _spawn_and_wait(workspace)


def forward(base: str, token: str, command: str, payload: dict) -> tuple[int, dict]:
    """POST one command (e.g. 'write_file'); returns (http_status, envelope).
    The command's verdict rides HTTP 200 INSIDE the envelope ("code": 0 ok,
    else sysexits) — 2-digit sysexits numbers are illegal HTTP status lines.
    A non-200 is transport trouble only (403 bad token / 400 bad JSON / 404
    unknown path) and carries no verdict; a lost connection raises
    DaemonUnreachable (the CLI must not blindly re-run the command locally,
    see the module docstring)."""
    request = urllib.request.Request(
        f"{base}/{command.strip('/')}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", _TOKEN_HEADER: token},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=COMMAND_TIMEOUT) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as err:  # transport status, never a verdict
        with err:
            body = err.read().decode("utf-8", "replace")
        try:
            return err.code, json.loads(body)
        except ValueError:
            return err.code, {"content": body or str(err.reason), "error": True, "diff": ""}
    except (urllib.error.URLError, OSError, ValueError) as err:
        raise DaemonUnreachable(f"workspace daemon connection lost: {err}") from None


def shutdown(base: str, token: str) -> None:
    """Ask the daemon to unpublish and exit; best-effort (tests and tooling)."""
    request = urllib.request.Request(
        f"{base}/shutdown", data=b"{}", headers={"Content-Type": "application/json", _TOKEN_HEADER: token}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=PROBE_TIMEOUT):
            pass
    except (urllib.error.URLError, OSError):
        pass


def _healthy(record: dict) -> bool:
    try:
        request = urllib.request.Request(_base(record) + "/health", headers={_TOKEN_HEADER: record["token"]})
        with urllib.request.urlopen(request, timeout=PROBE_TIMEOUT) as response:
            return response.status == 200 and bool(json.loads(response.read().decode("utf-8")).get("ok"))
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _base(record: dict) -> str:
    return f"http://127.0.0.1:{record['port']}"


def _spawn_and_wait(workspace: str) -> tuple[str, str]:
    """Start one detached daemon for this workspace and poll for its
    discovery entry. Any failure here is fatal: with no daemon there is no
    way to run the command."""
    try:
        child = _spawn(workspace)
    except OSError as err:
        raise DaemonUnavailable(f"could not start the workspace daemon: {err}") from None
    deadline = time.monotonic() + LAZY_START_SECONDS
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise DaemonUnavailable(f"the workspace daemon exited during startup (status {child.returncode})")
        record = discovery.read(workspace)
        if record and _healthy(record):
            return _base(record), record["token"]
        time.sleep(0.05)
    raise DaemonUnavailable(f"the workspace daemon did not become ready within {LAZY_START_SECONDS:g}s")


def _spawn(workspace: str) -> subprocess.Popen:
    """Detached daemon start: its own session on POSIX, DETACHED_PROCESS on
    Windows, stdio to the void either way — the daemon outlives this CLI
    call by design (that is where the undo stack lives)."""
    kwargs: dict = dict(
        cwd=workspace,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    cmd = [sys.executable, "-m", "clutch_workspace.daemon", "--workspace", os.path.abspath(workspace)]
    # the child chdirs to the workspace, so its own `-m` import must not rely
    # on the parent's sys.path: carry the package's parent dir explicitly
    # (no-op for an installed package, life support for a source checkout)
    kwargs["env"] = _child_env()
    return subprocess.Popen(cmd, **kwargs)


def _child_env() -> dict:
    env = dict(os.environ)
    package_parent = str(Path(__file__).resolve().parent.parent)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = f"{package_parent}{os.pathsep}{existing}" if existing else package_parent
    return env
