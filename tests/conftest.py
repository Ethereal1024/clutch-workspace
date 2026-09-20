"""Black-box harness: every test drives the real CLI as a subprocess
(`python -m clutch_workspace`), the way the host transport will — no imports
of the module under test for behavior, only for the exit-code table."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # lets tests import clutch_workspace for constants
    sys.path.insert(0, str(ROOT))

# daemon OFF for the whole pytest PROCESS, not just spawned children: tests
# that drive cli.main in-process would otherwise lazy-start real daemons and
# dodge their monkeypatches. Daemon tests opt back in per-test with
# CLUTCH_WORKSPACE_NO_SERVER=0 (monkeypatch restores this afterwards).
os.environ.setdefault("CLUTCH_WORKSPACE_NO_SERVER", "1")


def _run(*args: object, cwd: Path | str, stdin: str | None = None):
    # daemon OFF by default here so the whole suite exercises the direct
    # path; daemon tests opt back in with CLUTCH_WORKSPACE_NO_SERVER=0
    env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONUTF8="1")
    env.setdefault("CLUTCH_WORKSPACE_NO_SERVER", "1")
    return subprocess.run(
        [sys.executable, "-m", "clutch_workspace", *(str(a) for a in args)],
        cwd=str(cwd),
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


@pytest.fixture
def run(tmp_path: Path):
    """Raw runner, cwd defaults to the test's tmp workspace."""

    def _run_cwd(*args: object, cwd: Path | str | None = None, stdin: str | None = None):
        return _run(*args, cwd=cwd if cwd is not None else tmp_path, stdin=stdin)

    return _run_cwd


@pytest.fixture
def jok(run):
    """--json run that must succeed: returns the parsed envelope dict."""

    def _jok(*args: object, **kw):
        proc = run("--json", *args, **kw)
        assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
        return json.loads(proc.stdout)

    return _jok


@pytest.fixture
def jerr(run):
    """--json run that must fail with an envelope: returns (exit_code, env)."""

    def _jerr(*args: object, **kw):
        proc = run("--json", *args, **kw)
        assert proc.returncode != 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
        assert proc.stdout.count("\n") == 1, f"envelope must be one line, got {proc.stdout!r}"
        return proc.returncode, json.loads(proc.stdout)

    return _jerr
