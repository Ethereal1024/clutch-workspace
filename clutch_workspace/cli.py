"""Flag wiring: argparse -> filesystem commands -> envelope out.

  clutch-workspace [--json] <command> [flags]

- human (default): content on stdout (plus the unified diff for
  write/edit), `ERROR: <brief>` on stderr, sysexits code as exit status
- --json: ONE compact {"content", "error", "diff"} line on stdout for
  BOTH outcomes; callers parse stdout plus the exit code, never prose.
  It is a global flag and must precede the subcommand.
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

from . import __version__, filesystem
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


def _dispatch(args: argparse.Namespace) -> Result:
    if args.command == "read_file":
        return filesystem.read_file(args.path, offset=args.offset, limit=args.limit, max_chars=args.max_chars)
    if args.command == "grep":
        return filesystem.grep(args.pattern, path=args.path, include=args.include)
    if args.command == "write_file":
        return filesystem.write_file(args.path, content=_payload(args.content))
    if args.command == "edit_file":
        if args.old_string == "-" and args.new_string == "-":
            raise CommandError(EX_USAGE, "only one of --old-string/--new-string may read stdin ('-')")
        return filesystem.edit_file(
            args.path,
            old_string=_payload(args.old_string),
            new_string=_payload(args.new_string),
        )
    raise CommandError(EX_USAGE, f"unknown command: {args.command}")  # unreachable: subparsers required


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError, ValueError):
            pass
    args = build_parser().parse_args(argv)  # usage errors exit 64 straight from here
    try:
        _check_args(args)
        result = _dispatch(args)
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
