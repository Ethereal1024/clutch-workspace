"""read_file: raw reads, numbered ranges, truncation hints, directory listings."""

from clutch_workspace.exitcodes import EX_DATAERR, EX_NOINPUT, EX_USAGE

DATA = "l1\nl2\nl3\nl4\nl5\n"


def test_raw_read_has_no_line_numbers(jok, tmp_path):
    (tmp_path / "f.txt").write_text(DATA, encoding="utf-8")
    env = jok("read_file", "--path", "f.txt")
    assert env["content"] == DATA
    assert env["error"] is False and env["diff"] == ""


def test_human_read_prints_content_plus_newline(run, tmp_path):
    (tmp_path / "f.txt").write_text("abc", encoding="utf-8")
    proc = run("read_file", "--path", "f.txt")
    assert proc.returncode == 0
    assert proc.stdout == "abc\n"
    assert proc.stderr == ""


def test_range_is_numbered_with_continuation_hint(jok, tmp_path):
    (tmp_path / "f.txt").write_text(DATA, encoding="utf-8")
    env = jok("read_file", "--path", "f.txt", "--offset", 2, "--limit", 2)
    assert env["content"] == "2: l2\n3: l3\n... (showing lines 2-3 of 5; use offset=4 to continue)"


def test_range_from_middle_to_eof_has_no_hint(jok, tmp_path):
    (tmp_path / "f.txt").write_text(DATA, encoding="utf-8")
    assert jok("read_file", "--path", "f.txt", "--offset", 4)["content"] == "4: l4\n5: l5"


def test_limit_only_starts_at_line_one(jok, tmp_path):
    (tmp_path / "f.txt").write_text(DATA, encoding="utf-8")
    env = jok("read_file", "--path", "f.txt", "--limit", 2)
    assert env["content"] == "1: l1\n2: l2\n... (showing lines 1-2 of 5; use offset=3 to continue)"


def test_range_over_budget_is_dataerr_not_truncation(jerr, tmp_path):
    (tmp_path / "f.txt").write_text(DATA, encoding="utf-8")
    code, env = jerr("read_file", "--path", "f.txt", "--offset", 1, "--limit", 4, "--max-chars", 10)
    assert code == EX_DATAERR
    assert env["error"] is True
    assert env["content"] == (
        "the requested range (lines 1-4) exceeds the read limit of 10 chars. "
        "Use a smaller limit, or search with grep instead."
    )


def test_offset_past_eof_returns_empty_ok(jok, tmp_path):
    (tmp_path / "f.txt").write_text(DATA, encoding="utf-8")
    assert jok("read_file", "--path", "f.txt", "--offset", 99)["content"] == ""


def test_whole_file_truncation_points_at_next_line(jok, tmp_path):
    text = "x" * 30000
    (tmp_path / "f.txt").write_text(text, encoding="utf-8")
    env = jok("read_file", "--path", "f.txt")
    assert env["content"] == "x" * 20000 + "\n... [truncated, file is 30000 chars; use offset=1 to continue]"


def test_truncation_hint_uses_first_unread_line(jok, tmp_path):
    text = "0123456789\n" * 3000  # 33000 chars, 3000 lines
    (tmp_path / "f.txt").write_text(text, encoding="utf-8")
    head = text[:20000]
    env = jok("read_file", "--path", "f.txt")
    assert env["content"] == head + f"\n... [truncated, file is {len(text)} chars; use offset={head.count(chr(10)) + 1} to continue]"


def test_max_chars_flag(jok, tmp_path):
    (tmp_path / "f.txt").write_text("0123456789ABCDEF", encoding="utf-8")
    env = jok("read_file", "--path", "f.txt", "--max-chars", 10)
    assert env["content"] == "0123456789\n... [truncated, file is 16 chars; use offset=1 to continue]"


def test_directory_lists_entries_dirs_get_slash(jok, tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "b.txt").write_text("x", encoding="utf-8")
    (tmp_path / "A.txt").write_text("x", encoding="utf-8")
    (tmp_path / ".hidden").write_text("x", encoding="utf-8")  # read shows dotfiles; only grep hides them
    assert jok("read_file", "--path", ".")["content"] == ".hidden\nA.txt\nb.txt\nsub/"


def test_empty_directory(jok, tmp_path):
    (tmp_path / "empty").mkdir()
    assert jok("read_file", "--path", "empty")["content"] == "(empty directory)"


def test_directory_with_range_flags_is_usage_error(jerr, tmp_path):
    (tmp_path / "sub").mkdir()
    code, env = jerr("read_file", "--path", "sub", "--offset", 1)
    assert code == EX_USAGE
    assert env["content"] == "cannot read a line range of a directory; list it without offset/limit"


def test_missing_file_is_noinput(jerr, tmp_path):
    code, env = jerr("read_file", "--path", "nope.txt")
    assert code == EX_NOINPUT
    assert env["content"] == "file not found: nope.txt"


def test_undecodable_bytes_read_with_replacement(jok, tmp_path):
    (tmp_path / "latin.txt").write_bytes(b"caf\xe9\n")
    assert jok("read_file", "--path", "latin.txt")["content"] == "caf\ufffd\n"


def test_crlf_reads_as_lf(jok, tmp_path):
    (tmp_path / "win.txt").write_bytes(b"a\r\nb\r\n")
    assert jok("read_file", "--path", "win.txt")["content"] == "a\nb\n"


def test_negative_flags_are_usage(jerr, tmp_path):
    code, env = jerr("read_file", "--path", "f.txt", "--offset", -1)
    assert code == EX_USAGE
    assert env["content"] == "--offset must be >= 0"
