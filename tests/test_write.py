"""write_file: creation, overwrite diffs, newline law, error surface."""

from clutch_workspace.exitcodes import EX_IOERR


def test_create_new_file(jok, tmp_path):
    env = jok("write_file", "--path", "new.txt", "--content", "hello\n")
    assert env["content"] == "OK: wrote new.txt (6 chars)"
    assert env["diff"] == "--- a/new.txt\n+++ b/new.txt\n@@ -0,0 +1 @@\n+hello\n"
    assert (tmp_path / "new.txt").read_bytes() == b"hello\n"


def test_parents_auto_created(jok, tmp_path):
    env = jok("write_file", "--path", "sub/dir/new.txt", "--content", "x")
    assert env["content"] == "OK: wrote sub/dir/new.txt (1 chars)"
    assert (tmp_path / "sub" / "dir" / "new.txt").read_text(encoding="utf-8") == "x"


def test_overwrite_reports_added_removed_lines(jok, tmp_path):
    (tmp_path / "f.txt").write_text("old\n", encoding="utf-8")
    env = jok("write_file", "--path", "f.txt", "--content", "new\n")
    assert env["content"] == "OK: wrote f.txt (+1 -1 lines)"
    assert env["diff"] == "--- a/f.txt\n+++ b/f.txt\n@@ -1 +1 @@\n-old\n+new\n"
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "new\n"


def test_noop_rewrite_has_empty_diff(jok, tmp_path):
    (tmp_path / "f.txt").write_text("same\n", encoding="utf-8")
    env = jok("write_file", "--path", "f.txt", "--content", "same\n")
    assert env["content"] == "OK: wrote f.txt (+0 -0 lines)"
    assert env["diff"] == ""


def test_empty_content_new_file(jok, tmp_path):
    env = jok("write_file", "--path", "empty.txt", "--content", "")
    assert env["content"] == "OK: wrote empty.txt (0 chars)"
    assert env["diff"] == ""
    assert (tmp_path / "empty.txt").read_bytes() == b""


def test_content_writes_bytes_verbatim_no_crlf_expansion(jok, tmp_path):
    # newline="\n" means NO translation: \n must not expand to \r\n on Windows,
    # and a literal \r in the payload is stored as-is (the host's byte-exact rule)
    jok("write_file", "--path", "f.txt", "--content", "a\r\nb\nc")
    assert (tmp_path / "f.txt").read_bytes() == b"a\r\nb\nc"


def test_trailing_newline_preserved_exactly(jok, tmp_path):
    jok("write_file", "--path", "f.txt", "--content", "no-newline")
    assert (tmp_path / "f.txt").read_bytes() == b"no-newline"


def test_human_mode_prints_summary_then_diff(run, tmp_path):
    (tmp_path / "f.txt").write_text("old\n", encoding="utf-8")
    proc = run("write_file", "--path", "f.txt", "--content", "new\n")
    assert proc.returncode == 0
    assert proc.stdout == "OK: wrote f.txt (+1 -1 lines)\n--- a/f.txt\n+++ b/f.txt\n@@ -1 +1 @@\n-old\n+new\n"


def test_stdin_payload_via_dash(run, tmp_path):
    proc = run("write_file", "--path", "f.txt", "--content", "-", stdin="from stdin\n")
    assert proc.returncode == 0
    assert "OK: wrote f.txt (11 chars)" in proc.stdout
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "from stdin\n"


def test_parent_is_a_file_is_io_error(jerr, tmp_path):
    (tmp_path / "blocker").write_text("just a file", encoding="utf-8")
    code, env = jerr("write_file", "--path", "blocker/child.txt", "--content", "x")
    assert code == EX_IOERR
    assert env["error"] is True


def test_utf8_body_survives_json_roundtrip(jok, tmp_path):
    jok("write_file", "--path", "cn.txt", "--content", "中文内容\n")
    env = jok("read_file", "--path", "cn.txt")
    assert env["content"] == "中文内容\n"
