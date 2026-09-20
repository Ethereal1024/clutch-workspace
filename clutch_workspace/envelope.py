"""Output contract: human text vs the --json machine envelope.

Two modes, one Result shape:
- human:  content (+ diff) on stdout; errors go to stderr as `ERROR: <brief>`
- --json: ONE compact JSON line on stdout, always
          {"content": str, "error": bool, "diff": str}
          errors use the same envelope with error=true, diff="", and a
          non-zero exit code — callers parse stdout, never prose.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass


@dataclass
class Result:
    """A successful command outcome: content plus an optional unified diff."""

    content: str
    diff: str = ""


class CommandError(Exception):
    """A failed command: sysexits code (see exitcodes) plus a brief message."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _write_line(text: str) -> None:
    sys.stdout.write(text + "\n")


def emit_result(res: Result, as_json: bool) -> None:
    if as_json:
        _write_line(json.dumps({"content": res.content, "error": False, "diff": res.diff}, ensure_ascii=False))
        return
    sys.stdout.write(res.content + "\n")
    if res.diff:
        sys.stdout.write(res.diff if res.diff.endswith("\n") else res.diff + "\n")


def emit_error(err: CommandError, as_json: bool) -> None:
    if as_json:
        _write_line(json.dumps({"content": err.message, "error": True, "diff": ""}, ensure_ascii=False))
        return
    sys.stderr.write(f"ERROR: {err.message}\n")
