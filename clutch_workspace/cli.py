"""Flag wiring: argparse -> filesystem commands -> envelope out.

  clutch-workspace [--json] <command> [flags]

- human (default): content on stdout (plus the unified diff for
  write/edit), `ERROR: <brief>` on stderr, sysexits code as exit status
- --json: ONE compact {"content", "error", "diff"} line on stdout for
  BOTH outcomes; callers parse stdout plus the exit code, never prose.
  It is a global flag and must precede the subcommand.
- daemon mode (default): the command rides a per-workspace HTTP daemon
  when one is alive (or can be lazy-started); its envelope is re-emitted
  byte-identically. When the daemon is absent AND cannot be started the
  command runs in-process, same bytes, same code — the daemon is an
  accelerator, not a dependency. `--no-server` (or
  CLUTCH_WORKSPACE_NO_SERVER=1) forces the direct path; `--workspace DIR`
  serves DIR instead of the CWD (the daemon is keyed on it, and direct
  execution chdirs there first).
A payload flag value of `-` reads the value from stdin (write_file
--content, edit_file --old-string/--new-string) so bodies beyond the
Windows 32K argv limit still get through. Only ONE flag per invocation
may use `-` — two would drain the same stream twice.

Usage errors (bad flags, unknown command, contradictory arguments —
argparse's own included) exit 64; I/O failures 74; a bug that escapes is
70, never masquerading as caller error.
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback

from . import __version__, client, filesystem
from .envelope import CommandError, Result, emit_error, emit_result
from .exitcodes import EX_IOERR, EX_SIGINT, EX_SOFTWARE, EX_USAGE
from .filesystem import READ_MAX_CHARS

class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # argparse's default is 2; our contract is 64
        self.print_usage(sys.stderr)
        self.exit(EX_USAGE, f"{self.prog}: error: {message}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="clutch-workspace",
        description="Standalone file-inspection CLI: read_file / grep / write_file / edit_file.",
        allow_abbrev=False,
    )
    parser.add_argument("--json", action="store_true", help="emit one JSON envelope line on stdout instead of human text")
    parser.add_argument("--version", action="version", version=f"clutch-workspace {__version__}")
    parser.add_argument("--workspace", default="", metavar="DIR", help="serve DIR instead of the CWD (keys the daemon; direct execution chdirs there)")
    parser.add_argument("--no-server", action="store_true", help="skip the workspace daemon and always execute in-process (also CLUTCH_WORKSPACE_NO_SERVER=1)")
    sub = parser.add_subparsers(dest="command", metavar="<command>", required=True)

    read = sub.add_parser("read_file", help="read a file (or list a directory); numbered ranges with continuation hints")
    read.add_argument("--path", required=True, help="file or directory, relative to the process CWD")
    read.add_argument("--offset", type=int, default=0, metavar="N", help="1-based first line")
    read.add_argument("--limit", type=int, default=0, metavar="N", help="max lines to return")
    read.add_argument("--max-chars", type=int, default=READ_MAX_CHARS, metavar="N", help=f"char budget before truncation (default {READ_MAX_CHARS})")

    search = sub.add_parser("grep", help="regex search over text files, capped at 100 hits")
    search.add_argument("--pattern", required=True, help="Python regex")
    search.add_argument("--path", default=".", help="file or directory to search (default: the CWD)")
    search.add_argument("--include", default="", metavar="GLOB", help="fnmatch filter on file name or CWD-relative path")

    write = sub.add_parser("write_file", help="create or overwrite a file (parents auto-created); prints a unified diff")
    write.add_argument("--path", required=True)
    write.add_argument("--content", required=True, help="new file body; `-` reads stdin")

    edit = sub.add_parser("edit_file", help="replace exactly one occurrence of old_string; prints a unified diff")
    edit.add_argument("--path", required=True)
    edit.add_argument("--old-string", required=True, dest="old_string", help="exact text to find; `-` reads stdin")
    edit.add_argument("--new-string", required=True, dest="new_string", help="replacement text; `-` reads stdin")
    return parser


def _payload(value: str) -> str:
    """`-` = the value arrives on stdin (Windows argv caps at ~32K)."""
    return sys.stdin.read() if value == "-" else value


def _check_args(args: argparse.Namespace) -> None:
    """Flag combos argparse cannot express (negative numbers are caller bugs,
    not data problems — usage fault, 64)."""
    if args.command != "read_file":
        return
    for name in ("offset", "limit", "max_chars"):
        if getattr(args, name) < 0:
            raise CommandError(EX_USAGE, f"--{name.replace('_', '-')} must be >= 0")


def _prepare(args: argparse.Namespace) -> tuple[str, dict]:
    """The command name plus its payload — with stdin payloads drained
    EXACTLY once, before any forwarding, so a daemon fallback cannot find
    an already-drained stdin."""
    if args.command == "read_file":
        return "read_file", {"path": args.path, "offset": args.offset, "limit": args.limit, "max_chars": args.max_chars}
    if args.command == "grep":
        return "grep", {"pattern": args.pattern, "path": args.path, "include": args.include}
    if args.command == "write_file":
        return "write_file", {"path": args.path, "content": _payload(args.content)}
    if args.command == "edit_file":
        if args.old_string == "-" and args.new_string == "-":
            raise CommandError(EX_USAGE, "only one of --old-string/--new-string may read stdin ('-')")
        return "edit_file", {
            "path": args.path,
            "old_string": _payload(args.old_string),
            "new_string": _payload(args.new_string),
        }
    raise CommandError(EX_USAGE, f"unknown command: {args.command}")  # unreachable: subparsers required


_EXECUTE = {
    "read_file": lambda p: filesystem.read_file(p["path"], offset=p["offset"], limit=p["limit"], max_chars=p["max_chars"]),
    "grep": lambda p: filesystem.grep(p["pattern"], path=p["path"], include=p["include"]),
    "write_file": lambda p: filesystem.write_file(p["path"], content=p["content"]),
    "edit_file": lambda p: filesystem.edit_file(p["path"], old_string=p["old_string"], new_string=p["new_string"]),
}


def _execute(args: argparse.Namespace) -> Result:
    """One command: through the daemon when possible, direct otherwise."""
    command, payload = _prepare(args)
    workspace = os.path.abspath(args.workspace or os.getcwd())
    if _server_enabled(args):
        served = _via_daemon(workspace, command, payload)
        if served is not None:
            return served
    os.chdir(workspace)  # the direct path honors --workspace too (74 if it cannot)
    return _EXECUTE[command](payload)


def _server_enabled(args: argparse.Namespace) -> bool:
    return not args.no_server and not client.disabled()


def _via_daemon(workspace: str, command: str, payload: dict) -> Result | None:
    """Forward one command; None means 'no daemon for you, run it directly'.
    The daemon answers EVERY command with HTTP 200 and carries the verdict
    in the envelope's transport-only "code" field (0 ok, else the sysexits
    code); content/error/diff are exactly the CLI's envelope, so re-emitting
    it is byte-identical to local execution."""
    if not os.path.isdir(workspace):
        return None  # nothing to serve: keep direct semantics (missing input stays 66/74, etc.)
    endpoint = client.endpoint(workspace)
    if endpoint is None:
        return None  # absent and not startable: silent fallback (ratified)
    base, token = endpoint
    try:
        status, env = client.forward(base, token, command, payload)
    except client.DaemonUnreachable as err:
        # it was alive a moment ago; the command may have half-happened, so
        # re-running locally is NOT safe (edit_file is not idempotent)
        raise CommandError(EX_IOERR, str(err)) from None
    if status != 200:
        # a non-200 is transport trouble (403 bad token / 400 bad JSON / 404
        # unknown path), never the command's verdict: this daemon is not what
        # its discovery entry promises — forget it and run directly, exactly
        # as if it had never been there
        client.forget(workspace)
        return None
    if env.get("error"):
        code = env.get("code")  # a non-int code is a daemon bug, not caller error
        raise CommandError(code if isinstance(code, int) else EX_SOFTWARE, env.get("content", "command failed"))
    return Result(env.get("content", ""), env.get("diff", ""))


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError, ValueError):
            pass
    args = build_parser().parse_args(argv)  # usage errors exit 64 straight from here
    try:
        _check_args(args)
        result = _execute(args)
        emit_result(result, as_json=args.json)  # the pipe can die mid-write too — same contract
    except CommandError as err:
        emit_error(err, as_json=args.json)
        return err.code
    except BrokenPipeError:
        return _silence_broken_pipe()
    except KeyboardInterrupt:
        emit_error(CommandError(EX_SIGINT, "interrupted"), as_json=args.json)
        return EX_SIGINT
    except OSError as err:
        emit_error(CommandError(EX_IOERR, str(err)), as_json=args.json)
        return EX_IOERR
    except Exception as err:  # noqa: BLE001 - the one catch-all: name it a bug
        traceback.print_exc()
        emit_error(CommandError(EX_SOFTWARE, f"internal error: {err}"), as_json=args.json)
        return EX_SOFTWARE
    return 0


def _silence_broken_pipe() -> int:
    """Keep the interpreter's shutdown flush from re-raising on the dead pipe."""
    try:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    except (OSError, ValueError, AttributeError):
        pass
    return EX_IOERR
