"""grep: hit format, filters, skips, the 100 cap, error cases."""

import os

from clutch_workspace.exitcodes import EX_NOINPUT, EX_USAGE


def test_hits_grouped_per_file_with_blank_line_between(jok, tmp_path):
    (tmp_path / "g1.txt").write_text("hit_a\nhit_b\nnope\n", encoding="utf-8")
    (tmp_path / "g2.txt").write_text("x\nhit_c\n", encoding="utf-8")
    assert (
        jok("grep", "--pattern", "hit")["content"]
        == "g1.txt:\n  Line 1: hit_a\n  Line 2: hit_b\n\ng2.txt:\n  Line 2: hit_c"
    )


def test_no_matches(jok, tmp_path):
    (tmp_path / "f.txt").write_text("nothing here\n", encoding="utf-8")
    env = jok("grep", "--pattern", "zzz")
    assert env["content"] == "(no matches)"


def test_python_regex_dialect(jok, tmp_path):
    (tmp_path / "f.py").write_text("def one():\nreturn 1\n", encoding="utf-8")
    assert jok("grep", "--pattern", r"^def \w+\(")["content"] == "f.py:\n  Line 1: def one():"


def test_include_filters_by_name(jok, tmp_path):
    (tmp_path / "a.py").write_text("needle\n", encoding="utf-8")
    (tmp_path / "a.txt").write_text("needle\n", encoding="utf-8")
    content = jok("grep", "--pattern", "needle", "--include", "*.py")["content"]
    assert "a.py:" in content
    assert "a.txt" not in content


def test_include_filters_by_relative_path(jok, tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.py").write_text("needle\n", encoding="utf-8")
    (tmp_path / "a.py").write_text("needle\n", encoding="utf-8")
    content = jok("grep", "--pattern", "needle", "--include", "sub/*")["content"]
    assert f"sub{os.sep}b.py:" in content
    assert "a.py" not in content


def test_path_subdir_limits_search(jok, tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "f.txt").write_text("needle\n", encoding="utf-8")
    (tmp_path / "top.txt").write_text("needle\n", encoding="utf-8")
    content = jok("grep", "--pattern", "needle", "--path", "sub")["content"]
    assert f"sub{os.sep}f.txt:" in content
    assert "top.txt" not in content


def test_single_file_as_path(jok, tmp_path):
    (tmp_path / "f.txt").write_text("one\ntwo needle\n", encoding="utf-8")
    assert jok("grep", "--pattern", "needle", "--path", "f.txt")["content"] == "f.txt:\n  Line 2: two needle"


def test_hidden_and_pycache_are_skipped(jok, tmp_path):
    (tmp_path / ".secrets.txt").write_text("needle\n", encoding="utf-8")
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / "x.py").write_text("needle\n", encoding="utf-8")
    assert jok("grep", "--pattern", "needle")["content"] == "(no matches)"


def test_explicit_dotfile_path_is_searched(jok, tmp_path):
    (tmp_path / ".secrets.txt").write_text("needle\n", encoding="utf-8")
    content = jok("grep", "--pattern", "needle", "--path", ".secrets.txt")["content"]
    assert content == ".secrets.txt:\n  Line 1: needle"


def test_binary_file_skipped(jok, tmp_path):
    (tmp_path / "bin.dat").write_bytes(b"ok\x00binary" + b"\nneedle\n")
    assert jok("grep", "--pattern", "needle")["content"] == "(no matches)"


def test_nul_after_first_1024_bytes_is_not_binary(jok, tmp_path):
    (tmp_path / "late.bin").write_bytes(b"z" * 1024 + b"\x00 and needle\n")
    content = jok("grep", "--pattern", "needle")["content"]
    assert "  Line 1: " in content


def test_capped_at_100_with_footer(jok, tmp_path):
    (tmp_path / "big.txt").write_text("hit\n" * 120, encoding="utf-8")
    content = jok("grep", "--pattern", "hit")["content"]
    assert content.endswith("(Results capped at 100; use a more specific pattern or path.)")
    assert "  Line 100: hit" in content
    assert "  Line 101:" not in content


def test_lines_truncated_at_300_chars(jok, tmp_path):
    (tmp_path / "long.txt").write_text("y" * 400 + "\n", encoding="utf-8")
    assert jok("grep", "--pattern", "y")["content"] == "long.txt:\n  Line 1: " + "y" * 300


def test_bad_regex_is_usage_error(jerr):
    code, env = jerr("grep", "--pattern", "(")
    assert code == EX_USAGE
    assert env["content"].startswith("invalid regex: ")


def test_missing_search_root_is_noinput(jerr):
    code, env = jerr("grep", "--pattern", "x", "--path", "nope_dir")
    assert code == EX_NOINPUT
    assert env["content"] == "file not found: nope_dir"
