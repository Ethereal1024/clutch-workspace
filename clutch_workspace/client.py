"""Thin-client plumbing: find (or lazily start) the workspace daemon, forward
one command over HTTP, and map the response back onto the CLI's contract.

Policy (ratified):
- Lazy start: with no live daemon for the workspace, the first CLI call
  spawns one detached and waits up to LAZY_START_SECONDS for it to publish
  itself; the command then rides the daemon.
- Silent fallback: if there is no daemon and one cannot be started —
  workspace dir missing, spawn failed, port lost — the command simply runs
  in-process, byte-identically. The daemon is an accelerator, never a
  dependency; only `--no-server` / CLUTCH_WORKSPACE_NO_SERVER=1 force that
  path from the start.
- A daemon that dies MID-command is NOT retried locally: the command may
  have half-happened and the commands are not all idempotent (edit_file
  is not), so a lost connection surfaces as exit 74 instead.

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

LAZY_START_SECONDS = 10.0  # how long a client waits for a daemon it just spawned
PROBE_TIMEOUT = 2.0  # /health budget — a live daemon answers instantly
COMMAND_TIMEOUT = 300.0  # command budget; the daemon itself idles out at 600
_TOKEN_HEADER = "X-Clutch-Token"


class DaemonUnreachable(Exception):
    """The daemon accepted the connection but died before answering."""


def disabled() -> bool:
    """The environment kill switch: CLUTCH_WORKSPACE_NO_SERVER=1 forces the
    direct path everywhere (tests set this so the default suite stays
    in-process; --no-server is the per-call spelling)."""
    return os.environ.get("CLUTCH_WORKSPACE_NO_SERVER", "").strip().lower() in ("1", "true", "yes", "on")


def endpoint(workspace: str) -> tuple[str, str] | None:
    """(base_url, token) for this workspace's daemon — probing first, lazy-
    starting once when absent, None when the direct path should take over."""
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


def forget(workspace: str) -> None:
    """Drop the discovery entry (403/404/5xx: the daemon is not usable as
    published — the next call lazy-starts a fresh one)."""
    discovery.remove(workspace)


def _healthy(record: dict) -> bool:
    try:
        request = urllib.request.Request(_base(record) + "/health", headers={_TOKEN_HEADER: record["token"]})
        with urllib.request.urlopen(request, timeout=PROBE_TIMEOUT) as response:
            return response.status == 200 and bool(json.loads(response.read().decode("utf-8")).get("ok"))
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _base(record: dict) -> str:
    return f"http://127.0.0.1:{record['port']}"


def _spawn_and_wait(workspace: str) -> tuple[str, str] | None:
    """Start one detached daemon for this workspace and poll for its
    discovery entry. Any failure here is 'no daemon', never an error — the
    caller falls back to direct execution without a word."""
    try:
        child = _spawn(workspace)
    except OSError:
        return None
    deadline = time.monotonic() + LAZY_START_SECONDS
    while time.monotonic() < deadline:
        if child.poll() is not None:
            return None  # died on startup (bad flags, missing dir): don't wait out the clock
        record = discovery.read(workspace)
        if record and _healthy(record):
            return _base(record), record["token"]
        time.sleep(0.05)
    return None


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
