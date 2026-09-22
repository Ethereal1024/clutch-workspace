"""The workspace daemon: one long-lived process per workspace.

    python -m clutch_workspace.daemon --workspace DIR [--protect GLOB ...]
                                      [--idle SECONDS] [--port N]

Binds 127.0.0.1 on a random port, publishes port+token+pid to a discovery
file (see discovery.py), and serves the four commands over HTTP so repeated
CLI calls skip interpreter+filesystem warmup and share one undo stack.
Everything is stdlib; the zero-dependency promise holds.

Protocol — all JSON, every request token-gated via the X-Clutch-Token
header (anything else is 403):

    GET  /health      → {"ok": true, "workspace", "pid", "undo"}
    POST /read_file /grep /write_file /edit_file /undo /shutdown
                      → the command response

Every command response rides HTTP 200; the VERDICT is the envelope's
"code" field: 0 ok, else the BSD sysexits code the CLI contract uses
(64/65/66/74; 77 when a --protect glob fences the path; 70 for a daemon
bug). The sysexits numbers are NOT HTTP statuses — 2-digit codes are
illegal status lines — so they travel in the body. The other three fields
are exactly the CLI's {"content", "error", "diff"} envelope, so the thin
client re-emits byte-identically and just maps code → exit status. The
transport statuses (403 bad token, 400 bad JSON or missing field, 404
unknown path) carry no verdict: they mean no daemon is speaking the
protocol, which the client reports as a failure (74) — there is no
in-process path to fall back to.

Undo lives HERE (ratified: daemon memory, not disk): every write/edit that
succeeds pushes {path, before, existed}, capped at the 100 most recent;
/undo pops and restores (before=None → the file is removed). The stack
dies with the process — that is the feature, not a durability bug.

No reconfigure endpoint on purpose (ratified): the fence is whatever
--protect globs the daemon was STARTED with; to change it, /shutdown and
let the next CLI call lazy-start a fresh daemon (which also drops undo).

The fence has two faces, both from one matcher: a mutation of a fenced path
is refused with 77, and discovery of a fenced path is hidden (a directory
listing drops the entry; grep never searches a file it found by walking).
Naming a fenced path explicitly still serves it — the fence guards writes
and broad scans, not deliberate access.

Lifecycle: an idle watchdog exits the daemon after --idle seconds without
any request (default 600 ≈ 10 minutes), SIGTERM/SIGINT exit gracefully,
and both paths remove the discovery file. A crash leaves the file behind,
but the client's pid-liveness probe treats it as stale, so nothing hangs
on it.
"""

from __future__ import annotations

import argparse
import fnmatch
import hmac
import json
import os
import secrets
import signal
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import discovery, filesystem
from .envelope import CommandError, Result
from .exitcodes import EX_IOERR, EX_NOINPUT, EX_NOPERM, EX_SOFTWARE, EX_USAGE

DEFAULT_IDLE_SECONDS = 600.0
UNDO_MAX = 100

_COMMANDS = ("/read_file", "/grep", "/write_file", "/edit_file", "/undo")
_REQUIRED = {
    "/read_file": ("path",),
    "/grep": ("pattern",),
    "/write_file": ("path", "content"),
    "/edit_file": ("path", "old_string", "new_string"),
    "/undo": (),
}


class _BadRequest(Exception):
    """The request body does not match the protocol (client bug): 400."""


class Daemon:
    """State one workspace daemon owns: fence globs, undo stack, idle clock."""

    def __init__(self, workspace: Path, protect: list[str], idle: float) -> None:
        self.workspace = str(workspace)
        self.protect = list(protect)
        self.idle = idle
        self.token = secrets.token_hex(16)
        self.pid = os.getpid()
        self.server: ThreadingHTTPServer | None = None
        # one lock around whole-command execution (ratified): it serializes
        # filesystem work AND undo bookkeeping; RLock so the nested health
        # probe / watchdog reads don't deadlock against it
        self.lock = threading.RLock()
        self.undo: list[dict] = []  # newest last, capped at UNDO_MAX
        self.busy = 0  # commands currently executing (idle check defers to them)
        self._last_activity = time.monotonic()
        self._stop = threading.Event()

    # -- request plumbing ------------------------------------------------

    def touch(self) -> None:
        self._last_activity = time.monotonic()

    def route(self, command: str, payload: dict) -> Result:
        """Execute one command, serialized. One lock for everything that
        touches the filesystem or the undo stack (ratified: simple, correct)."""
        for field in _REQUIRED[command]:
            if field not in payload:
                raise _BadRequest(f"missing field: {field}")
        with self.lock:
            self.busy += 1
            try:
                return self._execute(command, payload)
            finally:
                self.busy -= 1

    def _execute(self, command: str, payload: dict) -> Result:
        if command == "/read_file":
            return self._read(payload)
        if command == "/grep":
            return self._grep(payload)
        if command == "/write_file":
            return self._write(payload)
        if command == "/edit_file":
            return self._edit(payload)
        if command == "/undo":
            return self._undo()
        raise _BadRequest(f"unknown command: {command}")  # route() pre-checks keys only

    def _read(self, a: dict) -> Result:
        try:
            return filesystem.read_file(
                a["path"],
                offset=int(a.get("offset") or 0),
                limit=int(a.get("limit") or 0),
                max_chars=int(a.get("max_chars") or 0),
                skip=self._hidden,
            )
        except (TypeError, ValueError):
            raise _BadRequest("offset/limit/max_chars must be integers") from None

    def _grep(self, a: dict) -> Result:
        return filesystem.grep(
            a["pattern"], path=a.get("path", "."), include=a.get("include", ""), skip=self._hidden
        )

    def _write(self, a: dict) -> Result:
        self._fence(a["path"])
        before = filesystem.previous_content(a["path"])
        result = filesystem.write_file(a["path"], content=a["content"])
        self._remember(a["path"], before)
        return result

    def _edit(self, a: dict) -> Result:
        self._fence(a["path"])
        before = filesystem.previous_content(a["path"])
        result = filesystem.edit_file(a["path"], old_string=a["old_string"], new_string=a["new_string"])
        self._remember(a["path"], before)
        return result

    def _undo(self) -> Result:
        # already serialized: route() holds the lock across execution
        if not self.undo:
            raise CommandError(EX_USAGE, "nothing to undo")
        entry = self.undo.pop()
        self._fence(entry["path"])
        return Result(filesystem.restore(entry["path"], entry["before"]))

    def _remember(self, path: str, before: str | None) -> None:
        self.undo.append({"path": path, "before": before, "existed": before is not None})
        if len(self.undo) > UNDO_MAX:
            self.undo.pop(0)

    # -- fence (daemon policy, not a filesystem capability) --------------
    #
    # One fence, two faces:
    #   * mutations of a fenced path are refused with 77 (--protect is a
    #     write barrier);
    #   * discovery of a fenced path is hidden — a directory listing drops
    #     the entry and grep never searches the file — so fenced content
    #     cannot leak through a broad scan.
    # Naming a fenced path explicitly still serves it (read_file/grep on the
    # path itself), which is the deliberate-access half of the same rule:
    # only expansion of a directory/walk consults the hide predicate. Both
    # faces come from _fence_hit, so a fence can never disagree with itself.

    def _fence_hit(self, path: str) -> str | None:
        """The first --protect glob matching `path`, or None. Matched against
        the path as given, its basename, its CWD-relative form in both
        separator flavors, and every directory suffix of that form, so
        'secret*', '*.env' and 'secrets/*' all behave as written
        ('secrets/*' also catches deep/secrets/x.txt)."""
        if not self.protect:
            return None
        for candidate in _fence_candidates(path):
            for glob in self.protect:
                if fnmatch.fnmatch(candidate, glob):
                    return glob
        return None

    def _fence(self, path: str) -> None:
        """Refuse a mutation of a fenced path (77) — the write barrier."""
        glob = self._fence_hit(path)
        if glob is not None:
            raise CommandError(EX_NOPERM, f"path is protected (--protect {glob}): {path}")

    def _hidden(self, rel: str) -> bool:
        """The hide predicate handed to filesystem's discovery walks: True
        when a CWD-relative path matches a fence glob."""
        return self._fence_hit(rel) is not None

    # -- lifecycle --------------------------------------------------------

    def shutdown_soon(self) -> None:
        """Ask the serve loop to wind down (signal handler / watchdog /
        /shutdown all land here; safe from any thread but serve_forever's)."""
        self._stop.set()
        threading.Thread(target=self._stop_serving, daemon=True).start()

    def _stop_serving(self) -> None:
        if self.server is not None:
            self.server.shutdown()

    def watchdog(self) -> None:
        """Idle suicide: after --idle seconds without any request, unpublish
        and exit. A command in flight (busy) defers the check to the next tick."""
        while not self._stop.wait(0.5):
            with self.lock:
                busy, idle_for = self.busy, time.monotonic() - self._last_activity
            if not busy and idle_for >= self.idle:
                self.shutdown_soon()
                return


def _fence_candidates(path: str) -> set[str]:
    """Every spelling a fence glob could plausibly be written against: the
    path as given, its basename, its CWD-relative form in both separator
    flavors, and every directory suffix of that form ('deep/secrets/x.txt'
    also offers 'secrets/x.txt' and 'x.txt')."""
    try:
        resolved = Path(path).resolve()
    except OSError:
        resolved = Path(os.path.abspath(path))
    out = {path, os.path.basename(path.replace("\\", "/"))}
    try:
        rel = str(resolved.relative_to(Path.cwd()))
    except ValueError:
        rel = str(resolved)
    parts = Path(rel.replace("\\", "/")).parts
    for i in range(len(parts)):
        suffix = "/".join(parts[i:])
        out |= {suffix, suffix.replace("/", "\\")}
    return out


def _verdict(result: Result) -> dict:
    """The CLI's envelope plus the verdict code — "code" is transport-only:
    sysexits numbers are illegal HTTP statuses, so they ride in the body."""
    return {"content": result.content, "error": False, "diff": result.diff, "code": 0}


def _verdict_err(code: int, message: str) -> dict:
    return {"content": message, "error": True, "diff": "", "code": code}


def _transport_err(message: str) -> dict:
    return {"content": message, "error": True, "diff": ""}


class _Handler(BaseHTTPRequestHandler):
    """One small HTTP skin over Daemon.route. Never logs (spawned daemons
    point stdio at the void anyway); the banner is the daemon's voice."""

    daemon: Daemon  # wired in serve()

    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        try:
            self._get()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self) -> None:  # noqa: N802 - http.server naming
        try:
            self._post()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _get(self) -> None:
        self.daemon.touch()
        if self.path != "/health":
            return self._respond(404, _transport_err(f"unknown path: {self.path}"))
        if not self._authorized():
            return
        d = self.daemon
        # lock-free: a plain len() can't be corrupted by a concurrent append
        self._respond(200, {"ok": True, "workspace": d.workspace, "pid": d.pid, "undo": len(d.undo)})

    def _post(self) -> None:
        d = self.daemon
        d.touch()
        if not self._authorized():
            return
        if self.path == "/shutdown":
            self._respond(200, {"ok": True})
            d.shutdown_soon()  # respond first, then wind down
            return
        if self.path not in _COMMANDS:
            return self._respond(404, _transport_err(f"unknown path: {self.path}"))
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (OSError, ValueError):
            return self._respond(400, _transport_err("request body must be one JSON object"))
        if not isinstance(payload, dict):
            return self._respond(400, _transport_err("request body must be one JSON object"))
        try:
            result = d.route(self.path, payload)
        except _BadRequest as err:  # a client bug, not a verdict: no "code"
            return self._respond(400, _transport_err(str(err)))  # field, so the client fails loudly
        except CommandError as err:  # the command's verdict rides 200 in "code"
            return self._respond(200, _verdict_err(err.code, err.message))
        except OSError as err:  # a filesystem-level failure is the CLI's 74 (EX_IOERR)
            return self._respond(200, _verdict_err(EX_IOERR, str(err)))
        except Exception as err:  # noqa: BLE001 - name it a bug, keep serving
            traceback.print_exc()
            return self._respond(200, _verdict_err(EX_SOFTWARE, f"internal error: {err}"))
        self._respond(200, _verdict(result))

    def _authorized(self) -> bool:
        header = self.headers.get("X-Clutch-Token", "")
        ok = hmac.compare_digest(header.encode("utf-8", "replace"), self.daemon.token.encode())
        if not ok:
            self._respond(403, _transport_err("bad or missing token"))
        return ok

    def _respond(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib signature
        pass  # silence per-request logging; stderr is a void for spawned daemons


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="clutch-workspace-daemon",
        description="Per-workspace HTTP daemon for the clutch-workspace CLI (usually started lazily by the client, not by hand).",
        allow_abbrev=False,
    )
    parser.add_argument("--workspace", required=True, metavar="DIR", help="directory to serve; the daemon chdirs here")
    parser.add_argument("--protect", action="append", default=[], metavar="GLOB", help="fence: refuse mutations of matching paths with exit 77 and hide them from directory listings/grep (repeatable)")
    parser.add_argument("--idle", type=float, default=DEFAULT_IDLE_SECONDS, metavar="SECONDS", help=f"exit after this much inactivity (default {DEFAULT_IDLE_SECONDS:g})")
    parser.add_argument("--port", type=int, default=0, metavar="N", help="port to bind (default: a random free port)")
    return parser


def serve(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).resolve()
    if not workspace.is_dir():
        print(f"clutch-workspace daemon: workspace is not a directory: {workspace}", file=sys.stderr)
        return EX_NOINPUT
    os.chdir(workspace)  # the four commands resolve against the CWD; the workspace IS the CWD

    daemon = Daemon(workspace, protect=args.protect, idle=args.idle)
    _Handler.daemon = daemon
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), _Handler)
    daemon.server = httpd
    port = httpd.server_address[1]
    discovery.write(str(workspace), port, daemon.token)

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, lambda *_: daemon.shutdown_soon())
        except (OSError, ValueError):  # not the main thread / unsupported
            pass
    threading.Thread(target=daemon.watchdog, daemon=True).start()

    print(f"clutch-workspace daemon ready: workspace={workspace} port={port} pid={daemon.pid} idle={args.idle:g}s protect={args.protect}", flush=True)
    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        httpd.server_close()
        discovery.remove(str(workspace))
        print(f"clutch-workspace daemon stopped: workspace={workspace}", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    return serve(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
