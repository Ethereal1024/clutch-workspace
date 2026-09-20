"""edit_file: single-occurrence replacement and its error codes."""

from clutch_workspace.exitcodes import EX_DATAERR, EX_NOINPUT, EX_USAGE


def test_unique_replacement(jok, tmp_path):
    (tmp_path / "f.txt").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    env = jok("edit_file", "--path", "f.txt", "--old-string", "beta", "--new-string", "BETA")
    assert env["content"] == "OK: edited f.txt (+1 -1 lines)"
    assert "-beta\n+BETA" in env["diff"]
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "alpha\nBETA\ngamma\n"


def test_multiline_old_and_new(jok, tmp_path):
    (tmp_path / "f.txt").write_text("a\nb\nc\n", encoding="utf-8")
    env = jok("edit_file", "--path", "f.txt", "--old-string", "a\nb", "--new-string", "x")
    assert env["diff"] == "--- a/f.txt\n+++ b/f.txt\n@@ -1,3 +1,2 @@\n-a\n-b\n+x\n c\n"
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "x\nc\n"


def test_zero_occurrences_is_dataerr_and_file_untouched(jerr, tmp_path):
    (tmp_path / "f.txt").write_text("keep\n", encoding="utf-8")
    code, env = jerr("edit_file", "--path", "f.txt", "--old-string", "zzz", "--new-string", "x")
    assert code == EX_DATAERR
    assert env["content"] == "old_string not found in f.txt"
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "keep\n"


def test_ambiguous_occurrences_is_dataerr(jerr, tmp_path):
    (tmp_path / "f.txt").write_text("x\nx\n", encoding="utf-8")
    code, env = jerr("edit_file", "--path", "f.txt", "--old-string", "x", "--new-string", "y")
    assert code == EX_DATAERR
    assert env["content"] == "old_string appears 2 times in f.txt"
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "x\nx\n"


def test_missing_file_is_noinput_with_hint(jerr):
    code, env = jerr("edit_file", "--path", "new.txt", "--old-string", "a", "--new-string", "b")
    assert code == EX_NOINPUT
    assert env["content"] == "file not found: new.txt — use write_file to create it"


def test_empty_old_string_is_usage(jerr, tmp_path):
    (tmp_path / "f.txt").write_text("x\n", encoding="utf-8")
    code, env = jerr("edit_file", "--path", "f.txt", "--old-string", "", "--new-string", "y")
    assert code == EX_USAGE
    assert env["content"] == "old_string is required"


def test_stdin_payload_via_dash(run, tmp_path):
    (tmp_path / "f.txt").write_text("count: 1\n", encoding="utf-8")
    proc = run("edit_file", "--path", "f.txt", "--old-string", "-", "--new-string", "N",
               stdin="count: 1")
    assert proc.returncode == 0
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "N\n"


def test_two_stdin_flags_rejected(run):
    proc = run("edit_file", "--path", "f.txt", "--old-string", "-", "--new-string", "-")
    assert proc.returncode == 64
    assert "only one of --old-string/--new-string may read stdin" in proc.stderr
