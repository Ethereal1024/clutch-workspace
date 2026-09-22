"""Black-box harness: every test drives the real CLI as a subprocess
(`python -m clutch_workspace`), the way the host transport will — no imports
of the module under test for behavior, only for the exit-code table.

The daemon is the CLI's only execution path, so every test needs one: the
autouse fixture below gives each test its own discovery directory and shuts
down whatever daemons it lazy-started.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # lets tests import clutch_workspace for constants
    sys.path.insert(0, str(ROOT))

from clutch_workspace import client  # noqa: E402


@pytest.fixture(autouse=True)
def daemon_home(tmp_path_factory, monkeypatch):
    """Isolate rendezvous state per test and reap the daemons it started."""
    disc = tmp_path_factory.mktemp("discovery")
    monkeypatch.setenv("CLUTCH_WORKSPACE_DISCOVERY_DIR", str(disc))
    yield disc
    # sweep: no daemon may outlive a test
    for leftover in disc.glob("d-*.json"):
        try:
            payload = json.loads(leftover.read_text(encoding="utf-8"))
            client.shutdown(f"http://127.0.0.1:{payload['port']}", payload["token"])
        except (OSError, ValueError, KeyError):
            pass


def _run(*args: object, cwd: Path | str, stdin: str | None = None):
    env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONUTF8="1")
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
