"""Daemon mode: forwarding, lazy start, fallback, fence, undo, lifecycle.

Still black-box: behavior assertions go through the real CLI as a
subprocess (conftest's run). The client/discovery modules are imported
only as plumbing (spawning, probing, reading rendezvous state) and the
HTTP surface is exercised with raw requests where the CLI can't reach
(undo is HTTP-only by decision; 403/404/400 guards).
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import pytest

from clutch_workspace import client, discovery
from clutch_workspace.exitcodes import EX_NOPERM, EX_NOINPUT, EX_USAGE

_TIMEOUT = 15.0
_ROOT = Path(__file__).resolve().parents[1]


def _spawn_env() -> dict:
    """Built per-spawn (NOT at import): it must carry whatever the fixtures
    just set — the discovery-dir override above all."""
    return dict(os.environ, PYTHONPATH=str(_ROOT))


# -- fixtures ----------------------------------------------------------------


@dataclass
class Running:
    """A daemon this test process spawned (foreground pipes for diagnostics)."""

    workspace: Path
    proc: subprocess.Popen
    base: str = ""
    token: str = ""

    def record(self) -> dict:
        return discovery.read(str(self.workspace)) or {}

    def stop(self) -> None:
        if self.record():
            client.shutdown(self.base, self.token)
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)


@pytest.fixture
def daemon_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Rendezvous state repointed into the test's tmp dir, daemon mode ON."""
    disc = tmp_path / "discovery"
    monkeypatch.setenv("CLUTCH_WORKSPACE_DISCOVERY_DIR", str(disc))
    monkeypatch.setenv("CLUTCH_WORKSPACE_NO_SERVER", "0")
    disc.mkdir(parents=True, exist_ok=True)  # some tests write discovery files directly (stale/corrupt)
    yield disc
    for leftover in disc.glob("d-*.json"):  # sweep: no daemon may outlive a test
        try:
            payload = json.loads(leftover.read_text(encoding="utf-8"))
            client.shutdown(f"http://127.0.0.1:{payload['port']}", payload["token"])
        except (OSError, ValueError, KeyError):
            pass


@pytest.fixture
def spawn(daemon_env):
    started: list[Running] = []

    def _spawn(workspace: Path, *flags: str) -> Running:
        proc = subprocess.Popen(
            [sys.executable, "-m", "clutch_workspace.daemon", "--workspace", str(workspace), *flags],
            cwd=str(workspace),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_spawn_env(),
        )
        running = Running(workspace=workspace, proc=proc)
        record = _wait_record(workspace)
        running.base = f"http://127.0.0.1:{record['port']}"
        running.token = record["token"]
        started.append(running)
        return running

    yield _spawn
    for running in started:
        running.stop()


@pytest.fixture
def daemon(spawn, tmp_path: Path):
    return spawn(tmp_path)


def _wait_record(workspace: Path, timeout: float = _TIMEOUT) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = discovery.read(str(workspace))
        if record and client._healthy(record):
            return record
        time.sleep(0.05)
    raise AssertionError(f"daemon for {workspace} never became ready")


def _health(base: str, token: str) -> dict:
    request = urllib.request.Request(f"{base}/health", headers={"X-Clutch-Token": token})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


# -- forwarding: the daemon must be invisible --------------------------------


def test_forwarded_output_is_byte_identical_to_direct(daemon, run, tmp_path):
    f = tmp_path / "f.txt"

    def seed_absent():
        f.unlink(missing_ok=True)

    def seed_v1():
        f.write_text("v1\n", encoding="utf-8")

    def seed_v2():
        f.write_text("v2\n", encoding="utf-8")

    def both(seed, *args: str):
        seed()  # identical state before each leg: only the transport differs
        via_daemon = run("--json", *args)
        seed()
        direct = run("--no-server", "--json", *args)
        assert (via_daemon.returncode, via_daemon.stdout) == (direct.returncode, direct.stdout), args

    both(seed_absent, "write_file", "--path", "f.txt", "--content", "v1\n")
    assert f.read_text(encoding="utf-8") == "v1\n"
    both(seed_v1, "edit_file", "--path", "f.txt", "--old-string", "v1", "--new-string", "v2")
    both(seed_v2, "read_file", "--path", "f.txt")
    both(seed_v2, "read_file", "--path", "f.txt", "--offset", "1", "--limit", "1")
    both(seed_v2, "read_file", "--path", ".")  # directory listing
    both(seed_v2, "grep", "--pattern", "v2", "--include", "*.txt")
    both(seed_absent, "read_file", "--path", "missing.txt")  # error verdict: 66 both ways

    # human mode too: stdout AND the error prose on stderr must match byte-for-byte
    seed_absent()
    a = run("read_file", "--path", "missing.txt")
    b = run("--no-server", "read_file", "--path", "missing.txt")
    assert (a.returncode, a.stdout, a.stderr) == (b.returncode, b.stdout, b.stderr)


def test_lazy_start_spawns_one_daemon_and_reuses_it(daemon_env, run, tmp_path):
    assert not discovery.discovery_file(str(tmp_path)).exists()
    first = run("--json", "write_file", "--path", "a.txt", "--content", "one\n")
    assert first.returncode == 0

    record = discovery.read(str(tmp_path))
    assert record, "lazy start must publish discovery"
    health = _health(f"http://127.0.0.1:{record['port']}", record["token"])
    assert health["ok"] is True and health["pid"] == record["pid"]
    assert health["undo"] == 1, "the write must have pushed onto the daemon's undo stack"

    second = run("--json", "read_file", "--path", "a.txt")
    assert json.loads(second.stdout)["content"] == "one\n"
    again = _health(f"http://127.0.0.1:{record['port']}", record["token"])
    assert again["pid"] == record["pid"], "the second call must ride the SAME daemon"
    assert again["undo"] == 1

    client.shutdown(f"http://127.0.0.1:{record['port']}", record["token"])
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and discovery.discovery_file(str(tmp_path)).exists():
        time.sleep(0.05)
    assert not discovery.discovery_file(str(tmp_path)).exists(), "/shutdown must unpublish"


def test_no_server_flag_and_env_kill_switch_stay_direct(daemon_env, run, tmp_path, monkeypatch):
    proc = run("--no-server", "--json", "write_file", "--path", "f.txt", "--content", "x")
    assert proc.returncode == 0
    assert (tmp_path / "f.txt").exists()
    assert not discovery.discovery_file(str(tmp_path)).exists(), "--no-server must never publish"

    monkeypatch.setenv("CLUTCH_WORKSPACE_NO_SERVER", "1")
    proc = run("--json", "write_file", "--path", "g.txt", "--content", "x")
    assert proc.returncode == 0
    assert not discovery.discovery_file(str(tmp_path)).exists(), "the env kill switch must hold"


def test_stale_and_corrupt_discovery_self_heal(daemon_env, run, tmp_path):
    (tmp_path / "f.txt").write_text("here\n", encoding="utf-8")
    victim = subprocess.Popen([sys.executable, "-c", "pass"])
    victim.wait()
    stale = {
        "version": 1,
        "workspace": str(tmp_path),
        "port": 1,
        "token": "cafebabe",
        "pid": victim.pid,  # dead: the daemon exited before publishing
        "started": "1970-01-01T00:00:00+00:00",
    }
    discovery.discovery_file(str(tmp_path)).write_text(json.dumps(stale), encoding="utf-8")

    proc = run("--json", "read_file", "--path", "f.txt")
    assert proc.returncode == 0
    healed = discovery.read(str(tmp_path))
    assert healed and healed["pid"] != victim.pid and healed["port"] != 1, "a dead pid must be replaced by a live daemon"

    discovery.discovery_file(str(tmp_path)).write_text(":{not json", encoding="utf-8")
    proc = run("--json", "read_file", "--path", "f.txt")
    assert proc.returncode == 0, "a corrupt discovery file must never take the CLI down"
    assert discovery.read(str(tmp_path)), "corrupt entry must be rebuilt"


def test_workspace_flag_serves_that_directory(daemon_env, run, tmp_path):
    target = tmp_path / "target"
    target.mkdir()

    proc = run("--no-server", "--json", "--workspace", str(target), "write_file", "--path", "f.txt", "--content", "w")
    assert proc.returncode == 0
    assert (target / "f.txt").read_text(encoding="utf-8") == "w", "direct execution must chdir into --workspace"

    proc = run("--json", "--workspace", str(target), "read_file", "--path", "f.txt")
    assert proc.returncode == 0
    assert json.loads(proc.stdout)["content"] == "w"
    assert discovery.discovery_file(str(target)).exists(), "the daemon is keyed on the --workspace dir, not the CWD"


# -- fence --------------------------------------------------------------------


def test_fence_refuses_mutations_with_77(spawn, run, tmp_path):
    d = spawn(tmp_path, "--protect", "secret*", "--protect", "secrets/*")

    code, env = _forward_json(run, "write_file", "--path", "secret.txt", "--content", "x")
    assert code == EX_NOPERM
    assert env["error"] is True and "--protect secret*" in env["content"]
    assert not (tmp_path / "secret.txt").exists()

    code, env = _forward_json(run, "write_file", "--path", "deep/secrets/x.txt", "--content", "x")
    assert code == EX_NOPERM

    code, _ = _forward_json(run, "write_file", "--path", "open.txt", "--content", "x")
    assert code == 0, "unfenced paths keep working"

    # an existing fenced file: edit refused too; reads still pass (mutation-only
    # fence — which fences move is still carry-over ①)
    (tmp_path / "secret.txt").write_text("v1\n", encoding="utf-8")
    code, _ = _forward_json(run, "edit_file", "--path", "secret.txt", "--old-string", "v1", "--new-string", "v2")
    assert code == EX_NOPERM
    assert (tmp_path / "secret.txt").read_text(encoding="utf-8") == "v1\n"
    code, env = _forward_json(run, "read_file", "--path", "secret.txt")
    assert code == 0 and "v1" in env["content"]


def _forward_json(run, *args: str):
    proc = run("--json", *args)
    assert proc.stdout.count("\n") == 1, proc.stdout
    return proc.returncode, json.loads(proc.stdout)


# -- undo (HTTP-only by decision: no CLI subcommand, no agent tool) -----------

def test_undo_restores_then_removes(daemon, run):
    assert run("--json", "write_file", "--path", "a.txt", "--content", "one\n").returncode == 0
    assert run("--json", "write_file", "--path", "a.txt", "--content", "two\n").returncode == 0

    status, env = client.forward(daemon.base, daemon.token, "undo", {})
    assert status == 200 and env["error"] is False
    assert (daemon.workspace / "a.txt").read_text(encoding="utf-8") == "one\n"

    status, env = client.forward(daemon.base, daemon.token, "undo", {})
    assert status == 200 and "removed" in env["content"]
    assert not (daemon.workspace / "a.txt").exists(), "undo of a create deletes the file"

    status, env = client.forward(daemon.base, daemon.token, "undo", {})
    assert status == 200 and env["code"] == EX_USAGE and "nothing to undo" in env["content"]


def test_fenced_refusal_pushes_nothing_onto_undo(spawn, run, tmp_path):
    d = spawn(tmp_path, "--protect", "fenced.txt")
    (tmp_path / "open.txt").write_text("v1\n", encoding="utf-8")
    proc = run("--json", "write_file", "--path", "open.txt", "--content", "v2\n")
    assert proc.returncode == 0

    code, _ = _forward_json(run, "write_file", "--path", "fenced.txt", "--content", "x")
    assert code == EX_NOPERM, "the fenced write is refused"

    status, _ = client.forward(d.base, d.token, "undo", {})
    assert status == 200
    assert (tmp_path / "open.txt").read_text(encoding="utf-8") == "v1\n", "undo pops the last SUCCESSFUL write"

    status, env = client.forward(d.base, d.token, "undo", {})
    assert status == 200 and env["code"] == EX_USAGE and "nothing to undo" in env["content"], (
        "a refused write must leave no entry"
    )


# -- HTTP surface guards -------------------------------------------------------


def test_http_guards(daemon):
    """403/404/400 are TRANSPORT answers: they carry no verdict ("code")."""

    def post(path: str, body: bytes) -> tuple[int, dict]:
        request = urllib.request.Request(
            f"{daemon.base}{path}",
            data=body,
            headers={"Content-Type": "application/json", "X-Clutch-Token": daemon.token},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(request, timeout=10)
        return err.value.code, json.loads(err.value.read().decode("utf-8"))

    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(f"{daemon.base}/health", timeout=10)
    assert err.value.code == 403
    assert json.loads(err.value.read().decode("utf-8"))["error"] is True

    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(
            urllib.request.Request(f"{daemon.base}/health", headers={"X-Clutch-Token": "wrong"}), timeout=10
        )
    assert err.value.code == 403

    code, env = post("/nope", b"{}")
    assert code == 404, "unknown path"
    code, env = post("/read_file", b"{}")  # missing required field: a client bug,
    assert code == 400 and "code" not in env, "protocol trouble carries no verdict"
    code, env = post("/read_file", b"{broken")  # not JSON: the same
    assert code == 400 and "code" not in env


def test_verdict_rides_http_200(daemon):
    """A sysexits verdict (66 here) must arrive as HTTP 200 + "code" in the
    body: a 2-digit number is an illegal HTTP status line (BadStatusLine)."""
    request = urllib.request.Request(
        f"{daemon.base}/read_file",
        data=json.dumps({"path": "missing.txt"}).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Clutch-Token": daemon.token},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        assert response.status == 200
        env = json.loads(response.read().decode("utf-8"))
    assert env["error"] is True and env["code"] == EX_NOINPUT and "missing.txt" in env["content"]


# -- lifecycle -----------------------------------------------------------------


def test_idle_suicide_unpublishes(daemon_env, tmp_path):
    proc = subprocess.Popen(
        [sys.executable, "-m", "clutch_workspace.daemon", "--workspace", str(tmp_path), "--idle", "1"],
        cwd=str(tmp_path),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=_spawn_env(),
    )
    try:
        _wait_record(tmp_path)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and proc.poll() is None:
            time.sleep(0.1)
        assert proc.poll() == 0, "idle exit is a clean exit"
        assert not discovery.discovery_file(str(tmp_path)).exists()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def test_shutdown_via_flag_stops_cleanly(daemon_env, tmp_path):
    proc = subprocess.Popen(
        [sys.executable, "-m", "clutch_workspace.daemon", "--workspace", str(tmp_path), "--port", "0"],
        cwd=str(tmp_path),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=_spawn_env(),
    )
    try:
        record = _wait_record(tmp_path)
        client.shutdown(f"http://127.0.0.1:{record['port']}", record["token"])
        proc.wait(timeout=10)
        assert proc.returncode == 0
        assert not discovery.discovery_file(str(tmp_path)).exists()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def test_missing_workspace_dir_is_a_66(tmp_path):
    ghost = tmp_path / "ghost"
    proc = subprocess.Popen(
        [sys.executable, "-m", "clutch_workspace.daemon", "--workspace", str(ghost)],
        cwd=str(tmp_path),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=_spawn_env(),
    )
    _, stderr = proc.communicate(timeout=10)
    assert proc.returncode == EX_NOINPUT
    assert b"not a directory" in stderr


# -- discovery primitives -------------------------------------------------------


def test_workspace_key_is_spelling_stable(tmp_path):
    via_relative = discovery.workspace_key(str(tmp_path))
    via_dots = discovery.workspace_key(str(tmp_path / "sub" / ".."))
    assert via_relative == via_dots
    assert discovery.discovery_file(str(tmp_path)).name.startswith("d-")


def test_pid_alive_probe():
    assert discovery.pid_alive(os.getpid()) is True
    assert discovery.pid_alive(-1) is False
    victim = subprocess.Popen([sys.executable, "-c", "pass"])
    victim.wait()
    discovery.pid_alive(victim.pid)  # must not raise, whatever the OS reports (pid reuse is legal)
