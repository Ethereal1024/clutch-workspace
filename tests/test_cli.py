"""CLI surface: envelope shape, --json placement, exit-code contract, exotic failures.

The black-box tests go through subprocesses; the three failure paths that are
awkward to provoke in a child (broken pipe, SIGINT, internal bug) run main()
in-process with patched streams/collaborators.
"""

import json
import sys

import pytest

from clutch_workspace import cli
from clutch_workspace.exitcodes import EX_IOERR, EX_NOINPUT, EX_SIGINT, EX_SOFTWARE, EX_USAGE


def test_version(run):
    proc = run("--version")
    assert proc.returncode == 0
    assert proc.stdout == "clutch-workspace 0.2.0\n"


def test_no_args_prints_usage_and_exits_64(run):
    proc = run()
    assert proc.returncode == EX_USAGE
    assert proc.stderr.startswith("usage:")
    assert "error:" in proc.stderr


def test_unknown_command_is_64(run):
    assert run("frobnicate").returncode == EX_USAGE


def test_missing_required_flag_is_64(run):
    assert run("read_file").returncode == EX_USAGE


def test_json_must_precede_the_subcommand(run, tmp_path):
    (tmp_path / "f.txt").write_text("x", encoding="utf-8")
    assert run("read_file", "--path", "f.txt", "--json").returncode == EX_USAGE
    assert run("--json", "read_file", "--path", "f.txt").returncode == 0


def test_envelope_success_shape(run, tmp_path):
    (tmp_path / "f.txt").write_text("x", encoding="utf-8")
    proc = run("--json", "read_file", "--path", "f.txt")
    assert proc.stdout.count("\n") == 1  # ONE compact line
    env = json.loads(proc.stdout)
    assert set(env) == {"content", "error", "diff"}
    assert env == {"content": "x", "error": False, "diff": ""}


def test_envelope_error_shape_and_exit_code(run):
    proc = run("--json", "read_file", "--path", "nope.txt")
    assert proc.returncode == EX_NOINPUT
    assert json.loads(proc.stdout) == {"content": "file not found: nope.txt", "error": True, "diff": ""}


def test_human_errors_go_to_stderr(run):
    proc = run("read_file", "--path", "nope.txt")
    assert proc.returncode == EX_NOINPUT
    assert proc.stdout == ""
    assert proc.stderr == "ERROR: file not found: nope.txt\n"


def test_json_body_is_not_ascii_escaped(run, tmp_path):
    (tmp_path / "cn.txt").write_text("中文", encoding="utf-8")
    proc = run("--json", "read_file", "--path", "cn.txt")
    assert "中文" in proc.stdout  # ensure_ascii=False
    assert json.loads(proc.stdout)["content"] == "中文"


def test_help_lists_all_four_commands(run):
    proc = run("--help")
    assert proc.returncode == 0
    for command in ("read_file", "grep", "write_file", "edit_file"):
        assert command in proc.stdout


class _DeadPipe:
    """stdout stand-in whose writes fail like a closed pipe downstream."""

    def write(self, *_args):
        raise BrokenPipeError

    def fileno(self):
        raise ValueError  # keeps the dup2 cleanup away from the real fd 1

    def reconfigure(self, **_kw):
        pass


def test_broken_pipe_is_ioerr(monkeypatch, tmp_path):
    (tmp_path / "f.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(sys, "stdout", _DeadPipe())
    monkeypatch.chdir(tmp_path)
    assert cli.main(["read_file", "--path", "f.txt"]) == EX_IOERR


def test_keyboard_interrupt_is_130(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.filesystem, "read_file", lambda *_a, **_k: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert cli.main(["read_file", "--path", "f.txt"]) == EX_SIGINT


def test_internal_bug_is_70_with_traceback(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.filesystem, "read_file", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert cli.main(["read_file", "--path", "f.txt"]) == EX_SOFTWARE
    assert "RuntimeError: boom" in capsys.readouterr().err
