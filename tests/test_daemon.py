"""Daemon mode — the CLI's ONLY execution path: forwarding, lazy start,
workspace keying, fence, undo, lifecycle.

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
def spawn():
    """Start a daemon by hand (the tests that need flags the CLI never passes,
    like --protect). Per-test rendezvous isolation comes from conftest's
    autouse daemon_home, which also reaps whatever survives."""
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


_ENVELOPE_KEYS = ("content", "error", "diff")


def _envelope_line(env: dict) -> str:
    """The daemon envelope as the thin client must print it: same fields, same
    order, same encoding — only the transport-only "code" is dropped."""
    return json.dumps({k: env[k] for k in _ENVELOPE_KEYS}, ensure_ascii=False) + "\n"


# -- forwarding: the CLI is a transport, not a renderer -----------------------


def test_cli_reemits_the_daemon_envelope_byte_for_byte(daemon, run, tmp_path):
    """The CLI adds and drops nothing: its --json stdout is the daemon's own
    envelope minus the transport-only "code" field, and its exit status is that
    same code. Failures travel the same road (the daemon's verdict 66 becomes
    the exit status; the human mode prose is "ERROR: " + that same content)."""
    f = tmp_path / "f.txt"

    def seed(text: str | None):
        f.unlink(missing_ok=True)
        if text is not None:
            f.write_text(text, encoding="utf-8")

    def forwarded(command: str, payload: dict) -> dict:
        status, env = client.forward(daemon.base, daemon.token, command, payload)
        assert status == 200, (status, env)
        return env

    def check(command: str, payload: dict, *flags: str) -> dict:
        """Read-only command: the daemon leg cannot disturb the CLI leg."""
        env = forwarded(command, payload)
        proc = run("--json", command, *flags)
        assert (proc.returncode, proc.stdout) == (env["code"], _envelope_line(env)), flags
        return env

    def check_mutating(state: str | None, command: str, payload: dict, *flags: str) -> dict:
        """Same state before each leg: the daemon runs the command first, so the
        CLI gets a second helping of identical input."""
        seed(state)
        env = forwarded(command, payload)
        seed(state)
        proc = run("--json", command, *flags)
        assert (proc.returncode, proc.stdout) == (env["code"], _envelope_line(env)), flags
        return env

    check("read_file", {"path": "missing.txt", "offset": 0, "limit": 0, "max_chars": 0}, "--path", "missing.txt")
    check_mutating(None, "write_file", {"path": "f.txt", "content": "v1\n"}, "--path", "f.txt", "--content", "v1\n")
    assert f.read_text(encoding="utf-8") == "v1\n"
    check_mutating(
        "v1\n",
        "edit_file",
        {"path": "f.txt", "old_string": "v1", "new_string": "v2"},
        "--path",
        "f.txt",
        "--old-string",
        "v1",
        "--new-string",
        "v2",
    )
    assert f.read_text(encoding="utf-8") == "v2\n"
    check("read_file", {"path": "f.txt", "offset": 0, "limit": 0, "max_chars": 0}, "--path", "f.txt")
    check("read_file", {"path": "f.txt", "offset": 1, "limit": 1, "max_chars": 0}, "--path", "f.txt", "--offset", "1", "--limit", "1")
    check("read_file", {"path": ".", "offset": 0, "limit": 0, "max_chars": 0}, "--path", ".")  # directory listing
    check("grep", {"pattern": "v2", "path": ".", "include": "*.txt"}, "--pattern", "v2", "--include", "*.txt")
    check("read_file", {"path": "missing.txt", "offset": 0, "limit": 0, "max_chars": 0}, "--path", "missing.txt")

    # human mode: stdout AND the error prose on stderr are rendered from that
    # same envelope — never re-derived from a local attempt
    env = forwarded("read_file", {"path": "missing.txt", "offset": 0, "limit": 0, "max_chars": 0})
    proc = run("read_file", "--path", "missing.txt")
    assert (proc.returncode, proc.stdout, proc.stderr) == (env["code"], "", f"ERROR: {env['content']}\n")


def test_lazy_start_spawns_one_daemon_and_reuses_it(run, tmp_path):
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


def test_there_is_no_in_process_path_anymore(run, tmp_path, monkeypatch):
    """The ratchet of this refactor: --no-server is gone and no environment
    variable can talk the CLI into touching the filesystem itself."""
    proc = run("--no-server", "--json", "write_file", "--path", "f.txt", "--content", "x")
    assert proc.returncode == EX_USAGE, "--no-server must no longer parse"
    assert not (tmp_path / "f.txt").exists()

    monkeypatch.setenv("CLUTCH_WORKSPACE_NO_SERVER", "1")  # the old kill switch
    proc = run("--json", "write_file", "--path", "g.txt", "--content", "x")
    assert proc.returncode == 0
    assert (tmp_path / "g.txt").read_text(encoding="utf-8") == "x"
    assert discovery.discovery_file(str(tmp_path)).exists(), "every call rides a daemon, kill switch or not"


def _stop_daemon(workspace: Path) -> None:
    """Shut down whatever daemon currently serves this workspace, if its record
    is readable. The tests below wreck that record on purpose, so the autouse
    sweep (which needs a parseable record) cannot be relied on."""
    record = discovery.read(str(workspace))
    if record:
        client.shutdown(f"http://127.0.0.1:{record['port']}", record["token"])


def _wait_pid_gone(pid: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and discovery.pid_alive(pid):
        time.sleep(0.05)
    assert not discovery.pid_alive(pid), f"daemon {pid} did not stop"


def test_stale_and_corrupt_discovery_self_heal(run, tmp_path):
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

    try:
        proc = run("--json", "read_file", "--path", "f.txt")
        assert proc.returncode == 0
        healed = discovery.read(str(tmp_path))
        assert healed and healed["pid"] != victim.pid and healed["port"] != 1, (
            "a dead pid must be replaced by a live daemon"
        )

        # stop the healed daemon BEFORE wrecking its record: the corrupt write
        # below destroys its only coordinates, and a record-less daemon can
        # never be shut down again (it would linger until its idle watchdog)
        _stop_daemon(tmp_path)
        _wait_pid_gone(healed["pid"])

        discovery.discovery_file(str(tmp_path)).write_text(":{not json", encoding="utf-8")
        proc = run("--json", "read_file", "--path", "f.txt")
        assert proc.returncode == 0, "a corrupt discovery file must never take the CLI down"
        assert discovery.read(str(tmp_path)), "corrupt entry must be rebuilt"
    finally:
        _stop_daemon(tmp_path)


def test_workspace_flag_keys_the_daemon_on_that_directory(run, tmp_path):
    target = tmp_path / "target"
    target.mkdir()

    proc = run("--json", "--workspace", str(target), "write_file", "--path", "f.txt", "--content", "w")
    assert proc.returncode == 0
    assert (target / "f.txt").read_text(encoding="utf-8") == "w", "the daemon chdirs into --workspace"
    assert discovery.discovery_file(str(target)).exists(), "the daemon is keyed on the --workspace dir, not the CWD"
    assert not discovery.discovery_file(str(tmp_path)).exists(), "the CWD must not get a daemon of its own"

    record = discovery.read(str(target))
    proc = run("--json", "--workspace", str(target), "read_file", "--path", "f.txt")
    assert proc.returncode == 0
    assert json.loads(proc.stdout)["content"] == "w"
    assert discovery.read(str(target))["pid"] == record["pid"], "the second call rides the SAME daemon"

    ghost = tmp_path / "ghost"
    proc = run("--json", "--workspace", str(ghost), "read_file", "--path", "f.txt")
    assert proc.returncode == EX_NOINPUT, "a missing --workspace dir is a hard 66: there is nothing to spawn for"
    assert json.loads(proc.stdout)["error"] is True


# -- fence --------------------------------------------------------------------


def test_fence_refuses_mutations_with_77(spawn, run, tmp_path):
    spawn(tmp_path, "--protect", "secret*", "--protect", "secrets/*")

    code, env = _forward_json(run, "write_file", "--path", "secret.txt", "--content", "x")
    assert code == EX_NOPERM
    assert env["error"] is True and "--protect secret*" in env["content"]
    assert not (tmp_path / "secret.txt").exists()

    code, env = _forward_json(run, "write_file", "--path", "deep/secrets/x.txt", "--content", "x")
    assert code == EX_NOPERM

    code, _ = _forward_json(run, "write_file", "--path", "open.txt", "--content", "x")
    assert code == 0, "unfenced paths keep working"

    # an existing fenced file: edit refused too; naming the path explicitly
    # still serves it (the fence guards mutation + discovery, not deliberate
    # access — the hide half is test_fence_hides_from_listings_and_grep)
    (tmp_path / "secret.txt").write_text("v1\n", encoding="utf-8")
    code, _ = _forward_json(run, "edit_file", "--path", "secret.txt", "--old-string", "v1", "--new-string", "v2")
    assert code == EX_NOPERM
    assert (tmp_path / "secret.txt").read_text(encoding="utf-8") == "v1\n"
    code, env = _forward_json(run, "read_file", "--path", "secret.txt")
    assert code == 0 and "v1" in env["content"]


def test_fence_hides_from_listings_and_grep(spawn, run, tmp_path):
    """The discovery half of the fence: a directory listing drops fenced
    entries and a workspace-wide grep never searches them, so content cannot
    leak through a broad scan — while naming the path explicitly still serves
    it (the same deliberate-access rule read_file keeps)."""
    spawn(tmp_path, "--protect", "secret*", "--protect", "secrets/*")
    (tmp_path / "secret.txt").write_text("top secret\n", encoding="utf-8")
    (tmp_path / "open.txt").write_text("hello\n", encoding="utf-8")
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "x.txt").write_text("deep secret\n", encoding="utf-8")

    # a listing of the workspace hides the fenced file AND the fenced tree
    code, env = _forward_json(run, "read_file", "--path", ".")
    assert code == 0
    listing = env["content"].splitlines()
    assert "open.txt" in listing
    assert "secret.txt" not in listing, "a fenced file must not appear in a listing"
    assert "secrets/" not in listing, "'secrets/*' fences the directory entry itself"

    # a workspace-wide grep never searches fenced content
    code, env = _forward_json(run, "grep", "--pattern", "secret", "--path", ".")
    assert code == 0 and env["content"] == "(no matches)", env["content"]

    # explicit access is deliberate: the file itself is still served
    code, env = _forward_json(run, "grep", "--pattern", "secret", "--path", "secret.txt")
    assert code == 0 and "top secret" in env["content"]

    # unfenced content is untouched
    code, env = _forward_json(run, "grep", "--pattern", "hello", "--path", ".")
    assert code == 0 and "open.txt:" in env["content"]


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


def test_idle_suicide_unpublishes(tmp_path):
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


def test_shutdown_via_flag_stops_cleanly(tmp_path):
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
