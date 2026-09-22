"""The four commands, ported 1:1 from the host's agent/tools/filesystem.py.

Deltas are exactly the frozen "pure capability" cuts — nothing else:
- no Workspace object: paths resolve against the process CWD (`_resolve`),
  so "the workspace" is whatever cwd the caller spawns us with
- no protected set / containment: every path is servable. The fence and the
  undo stack are daemon policy (daemon.py); this module only takes the
  policy's hooks — the two tiny snapshot primitives (`previous_content`,
  `restore`) and an optional `skip` hide predicate on the two discovery
  commands (read_file's directory listing, grep's walk)
- no error prose templates: failures raise CommandError(code, brief); the
  caller owns the long-form prose
- output-channel formats (hit grouping, continuation hints, the grep cap
  footer, "(no matches)", "(empty directory)") stay byte-identical to the
  host, so the host's prompt contract keeps holding verbatim
"""

from __future__ import annotations

import difflib
import fnmatch
import re
from pathlib import Path

from .envelope import CommandError, Result
from .exitcodes import EX_DATAERR, EX_NOINPUT, EX_USAGE

READ_MAX_CHARS = 20_000  # --max-chars default: the host's read budget
_GREP_MAX_HITS = 100
_GREP_LINE_MAX = 300


def read_file(
    path: str,
    offset: int = 0,
    limit: int = 0,
    max_chars: int = READ_MAX_CHARS,
    skip: Callable[[str], bool] | None = None,
) -> Result:
    """Read a file (or, with no range flags, list a directory).

    No range flags: raw text, truncated at the char budget with a hint that
    points at the first unread line. With range flags: 1-based numbered
    lines [offset, offset+limit) — a range that cannot fit the budget is an
    error, not a silent truncation. `skip` is the caller's hide predicate
    (the daemon's fence): a directory listing drops every entry it accepts,
    which is how a fenced file stays invisible to `read_file <dir>`.
    """
    limit_chars = max_chars or READ_MAX_CHARS  # --max-chars 0 means "the default", like the host
    p = _resolve(path)
    if p.is_dir():
        if offset > 0 or limit > 0:
            # usage-level: the flags contradict the input kind (64, not 65)
            raise CommandError(EX_USAGE, "cannot read a line range of a directory; list it without offset/limit")
        # byte-identical to the host: suffixed names, plain lexicographic sort,
        # dotfiles INCLUDED (only grep hides them). Directories carry their
        # trailing slash into the skip predicate, so a glob that fences a whole
        # tree ("secrets/*") also hides the directory entry itself.
        kept: list[str] = []
        for entry in p.iterdir():
            name = entry.name + ("/" if entry.is_dir() else "")
            rel = _rel(entry)
            if skip is not None and skip(rel + "/" if entry.is_dir() else rel):
                continue
            kept.append(name)
        entries = sorted(kept)
        return Result("\n".join(entries) if entries else "(empty directory)")
    try:
        text = _read_text(p)
    except FileNotFoundError:
        raise CommandError(EX_NOINPUT, f"file not found: {path}") from None
    if offset > 0 or limit > 0:
        return _read_range(text, offset, limit, limit_chars)
    if len(text) > limit_chars:
        # point the caller at the first unread line instead of re-serving the same head
        head = text[:limit_chars]
        next_offset = head.count("\n") + 1
        text = head + f"\n... [truncated, file is {len(text)} chars; use offset={next_offset} to continue]"
    return Result(text)


def _read_range(text: str, offset: int, limit: int, limit_chars: int) -> Result:
    """Lines [offset, offset+limit) (1-based) with line numbers; a range that
    cannot fit the char budget is an ERROR, not a silent truncation."""
    lines = text.splitlines()
    total = len(lines)
    start = max(0, offset - 1) if offset > 0 else 0
    end = start + limit if limit > 0 else total
    selected = lines[start:end]
    body = "\n".join(f"{i + 1}: {ln}" for i, ln in enumerate(selected, start=start))
    if len(body) > limit_chars:
        raise CommandError(
            EX_DATAERR,
            f"the requested range (lines {start + 1}-{end}) exceeds the read limit of "
            f"{limit_chars} chars. Use a smaller limit, or search with grep instead.",
        )
    shown = start + len(selected)
    if shown < total:
        body += f"\n... (showing lines {start + 1}-{shown} of {total}; use offset={shown + 1} to continue)"
    return Result(body)


def write_file(path: str, content: str) -> Result:
    """Create or overwrite a file (parents auto-created); summary plus a
    unified diff against the previous content, if there was one."""
    p = _resolve(path)
    old = ""
    try:
        old = _read_text(p)
    except FileNotFoundError:
        pass
    p.parent.mkdir(parents=True, exist_ok=True)
    # newline="\n": byte-exact on every host (the host's LocalWorkspace rule —
    # Windows text mode must not expand \n into \r\n)
    p.write_text(content, encoding="utf-8", newline="\n")
    rel = _rel(p)
    diff = _unified_diff(old, content, rel)
    adds, dels = _count_changes(diff)
    # the path in the summary is echoed as given (not absolutized) — see review notes
    if old:
        return Result(f"OK: wrote {path} (+{adds} -{dels} lines)", diff=diff)
    return Result(f"OK: wrote {path} ({len(content)} chars)", diff=diff)


def edit_file(path: str, old_string: str, new_string: str) -> Result:
    """Targeted string replacement: exactly one occurrence of old_string
    becomes new_string (tiny diffs keep the context small instead of
    re-emitting the whole file)."""
    p = _resolve(path)
    try:
        text = _read_text(p)
    except FileNotFoundError:
        raise CommandError(EX_NOINPUT, f"file not found: {path} — use write_file to create it") from None
    if not old_string:
        raise CommandError(EX_USAGE, "old_string is required")
    count = text.count(old_string)
    if count == 0:
        raise CommandError(EX_DATAERR, f"old_string not found in {path}")
    if count > 1:
        raise CommandError(EX_DATAERR, f"old_string appears {count} times in {path}")
    new = text.replace(old_string, new_string, 1)
    p.write_text(new, encoding="utf-8", newline="\n")
    diff = _unified_diff(text, new, _rel(p))
    adds, dels = _count_changes(diff)
    return Result(f"OK: edited {path} (+{adds} -{dels} lines)", diff=diff)


def grep(pattern: str, path: str = ".", include: str = "", skip: Callable[[str], bool] | None = None) -> Result:
    """Regex search over text files under path, capped at 100 hits, grouped
    per file with a blank line between groups. `skip` is the caller's hide
    predicate (the daemon's fence): a file discovered by the walk is never
    searched when it matches, so fenced content cannot leak through a
    workspace-wide grep. A file named explicitly as `path` is served — the
    predicate guards discovery, not deliberate access (same rule as
    read_file's directory listing)."""
    try:
        rx = re.compile(pattern)
    except re.error as e:
        # the pattern is caller-built input; a syntax error in it is a usage
        # fault ("bad syntax in a parameter" in sysexits terms) — 64, not 65
        raise CommandError(EX_USAGE, f"invalid regex: {e}") from None
    root = _resolve(path)
    if not root.is_file() and not root.is_dir():
        raise CommandError(EX_NOINPUT, f"file not found: {path}")
    explicit = root.is_file()
    files = [root] if explicit else _grep_files(root)
    out: list[tuple[str, int, str]] = []
    for f in files:
        rel = _rel(f)
        if not explicit and skip is not None and skip(rel):
            continue
        if include and not (fnmatch.fnmatch(f.name, include) or fnmatch.fnmatch(rel, include)):
            continue
        if _is_binary(f):
            continue
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                for i, line in enumerate(fh, 1):
                    if rx.search(line):
                        out.append((rel, i, line.rstrip("\n")))
                        if len(out) >= _GREP_MAX_HITS:
                            break
        except OSError:
            continue
        if len(out) >= _GREP_MAX_HITS:
            break
    if not out:
        return Result("(no matches)")
    lines: list[str] = []
    current: str | None = None
    for fpath, lineno, text in out:
        if current != fpath:
            if current is not None:
                lines.append("")
            current = fpath
            lines.append(f"{fpath}:")
        lines.append(f"  Line {lineno}: {text[:_GREP_LINE_MAX]}")
    content = "\n".join(lines)
    if len(out) >= _GREP_MAX_HITS:
        content += "\n\n(Results capped at 100; use a more specific pattern or path.)"
    return Result(content)


def previous_content(path: str) -> str | None:
    """The file's current text, or None when it does not exist (daemon undo:
    the daemon records this before a write/edit and hands it back to
    `restore` on undo). Same read discipline as the commands themselves."""
    try:
        return _read_text(_resolve(path))
    except FileNotFoundError:
        return None


def restore(path: str, content: str | None) -> str:
    """The undo half of the pair: put `content` back (None = the file did not
    exist, so remove it). Same write discipline as write_file — utf-8,
    newline='\\n', parents auto-created — so a restored file is byte-exact."""
    p = _resolve(path)
    if content is None:
        p.unlink(missing_ok=True)
        return f"OK: undid {path} (created file removed)"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8", newline="\n")
    return f"OK: undid {path} (restored {len(content)} chars)"


def _grep_files(dirpath: Path) -> list[Path]:
    """Recursive walk, sorted, skipping dot-entries and __pycache__ (the
    host's rule: hidden files are invisible to grep but visible to read)."""
    files: list[Path] = []
    for ent in sorted(dirpath.iterdir()):
        if ent.name.startswith(".") or ent.name == "__pycache__":
            continue
        if ent.is_dir():
            files.extend(_grep_files(ent))
        elif ent.is_file():
            files.append(ent)
    return files


def _is_binary(p: Path) -> bool:
    try:
        with open(p, "rb") as f:
            return b"\x00" in f.read(1024)
    except OSError:
        return True


def _resolve(path: str) -> Path:
    """cwd-flavored mirror of LocalWorkspace.resolve: anchor relative paths at
    the process CWD, resolve symlinks in the parent chain, keep the final
    component unresolved (a project-local symlink must still open as itself).
    No containment: whatever this returns is the file — the caller picked
    the cwd, so the caller picked the fence."""
    base = Path.cwd().resolve() / path
    try:
        if base.name:
            return base.parent.resolve() / base.name
    except OSError:
        pass
    return base.resolve()


def _read_text(p: Path) -> str:
    """The host's LocalWorkspace.read: a non-file (dir, missing) is
    FileNotFoundError; text is utf-8 with replacement, never an exception."""
    if not p.is_file():
        raise FileNotFoundError(str(p))
    return p.read_text(encoding="utf-8", errors="replace")


def _rel(p: Path) -> str:
    """CWD-relative display form (the host shows workspace-relative paths in
    diff headers and grep groups); outside the CWD it falls back to absolute."""
    try:
        return str(p.relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(p)


def _unified_diff(old: str, new: str, rel: str) -> str:
    """Unified diff between old and new file contents (difflib, n=3 context)."""
    diff_lines = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"a/{rel}",
        tofile=f"b/{rel}",
        n=3,
    )
    return "".join(diff_lines)


def _count_changes(diff: str) -> tuple[int, int]:
    adds = sum(1 for ln in diff.splitlines() if ln.startswith("+") and not ln.startswith("+++"))
    dels = sum(1 for ln in diff.splitlines() if ln.startswith("-") and not ln.startswith("---"))
    return adds, dels
