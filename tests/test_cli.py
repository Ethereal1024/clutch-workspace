"""CLI surface: envelope shape, --json placement, exit-code contract, exotic failures.

The black-box tests go through subprocesses; the failure paths that are awkward
to provoke in a child (broken pipe, SIGINT, an unreachable daemon, a daemon bug)
run main() in-process against a canned transport. There is no in-process
execution path left, so those tests patch the client, never the filesystem.
"""

import json
import sys

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


def _canned(monkeypatch, endpoint=None, forward=None):
    """Stand in for the daemon: these tests are about what the CLI does with a
    reply (or the lack of one), not about the transport. Patching the client
    module is the only seam left — the CLI itself never touches the filesystem."""
    monkeypatch.setattr(cli.client, "endpoint", endpoint or (lambda workspace: ("http://127.0.0.1:9", "tok")))
    if forward is not None:
        monkeypatch.setattr(cli.client, "forward", forward)


def _raises(exc: BaseException):
    def boom(*_args, **_kwargs):
        raise exc

    return boom


def test_broken_pipe_is_ioerr(monkeypatch):
    _canned(monkeypatch, forward=lambda *a, **k: (200, {"content": "x", "error": False, "diff": "", "code": 0}))
    monkeypatch.setattr(sys, "stdout", _DeadPipe())
    assert cli.main(["read_file", "--path", "f.txt"]) == EX_IOERR


def test_keyboard_interrupt_is_130(monkeypatch, capsys):
    _canned(monkeypatch, forward=_raises(KeyboardInterrupt()))
    assert cli.main(["read_file", "--path", "f.txt"]) == EX_SIGINT
    assert capsys.readouterr().err == "ERROR: interrupted\n"


def test_internal_bug_is_70_with_traceback(monkeypatch, capsys):
    _canned(monkeypatch, forward=_raises(RuntimeError("boom")))
    assert cli.main(["read_file", "--path", "f.txt"]) == EX_SOFTWARE
    assert "RuntimeError: boom" in capsys.readouterr().err


def test_unavailable_daemon_carries_its_own_code(monkeypatch, capsys):
    """No daemon, no execution: the client's verdict (66 for a missing
    workspace dir) is what the caller sees, in the usual envelope."""
    _canned(monkeypatch, endpoint=_raises(cli.client.DaemonUnavailable("workspace is not a directory: /nope", EX_NOINPUT)))
    assert cli.main(["--json", "read_file", "--path", "f.txt"]) == EX_NOINPUT
    assert json.loads(capsys.readouterr().out) == {
        "content": "workspace is not a directory: /nope",
        "error": True,
        "diff": "",
    }


def test_lost_connection_mid_command_is_74_and_never_retried(monkeypatch, capsys):
    calls = []

    def forward(base, token, command, payload):
        calls.append(command)
        raise cli.client.DaemonUnreachable("workspace daemon connection lost: gone")

    _canned(monkeypatch, forward=forward)
    assert cli.main(["--json", "write_file", "--path", "f.txt", "--content", "x"]) == EX_IOERR
    assert calls == ["write_file"], "a command that may have half-happened must NOT run again"
    assert json.loads(capsys.readouterr().out)["error"] is True


def test_non_200_from_the_daemon_is_74(monkeypatch, capsys):
    """403/400/404 mean no daemon spoke the protocol — transport trouble, not a
    verdict — and there is no other path to the filesystem."""
    _canned(monkeypatch, forward=lambda *a, **k: (403, {"content": "bad or missing token", "error": True, "diff": ""}))
    assert cli.main(["--json", "read_file", "--path", "f.txt"]) == EX_IOERR
    assert "bad or missing token" in json.loads(capsys.readouterr().out)["content"]
