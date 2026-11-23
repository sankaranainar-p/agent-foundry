import subprocess
from foundry.runners import _git_apply, recount_patch


def _repo(tmp_path, files=None):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    for n, t in (files or {}).items():
        (tmp_path / n).write_text(t)
    return tmp_path


def _apply(tmp_path, patch):
    fixed, warns = recount_patch(patch)
    (tmp_path / "p.patch").write_text(fixed)
    _git_apply(tmp_path, "p.patch")
    return warns


def test_two_new_files_without_diff_git(tmp_path):
    _repo(tmp_path)
    assert _apply(tmp_path, "--- /dev/null\n+++ b/a.txt\n@@ -0,0 +1,1 @@\n+a\n--- /dev/null\n+++ b/b.txt\n@@ -0,0 +1,2 @@\n+b1\n+b2\n") == []
    assert (tmp_path / "a.txt").read_text() == "a\n" and (tmp_path / "b.txt").read_text() == "b1\nb2\n"


def test_too_small_count_not_truncated(tmp_path):
    _repo(tmp_path)
    warns = _apply(tmp_path, "--- /dev/null\n+++ b/a.txt\n@@ -0,0 +1,3 @@\n+1\n+2\n+3\n+4\n")
    assert (tmp_path / "a.txt").read_text() == "1\n2\n3\n4\n" and len(warns) == 1


def test_too_large_count_applies(tmp_path):
    _repo(tmp_path)
    assert _apply(tmp_path, "--- /dev/null\n+++ b/a.txt\n@@ -0,0 +1,9 @@\n+1\n+2\n")
    assert (tmp_path / "a.txt").read_text() == "1\n2\n"


def test_removed_dashdash_line_is_not_a_header(tmp_path):
    _repo(tmp_path, {"a.txt": "x\n-- y\nz\n"})
    patch = "--- a/a.txt\n+++ b/a.txt\n@@ -1,3 +1,2 @@\n x\n--- y\n z\n"
    assert _apply(tmp_path, patch) == []
    assert (tmp_path / "a.txt").read_text() == "x\nz\n"


def test_modify_with_context(tmp_path):
    _repo(tmp_path, {"a.txt": "1\n2\n3\n4\n5\n"})
    assert _apply(tmp_path, "--- a/a.txt\n+++ b/a.txt\n@@ -2,3 +2,3 @@\n 2\n-3\n+three\n 4\n") == []
    assert (tmp_path / "a.txt").read_text() == "1\n2\nthree\n4\n5\n"


def test_unprefixed_blank_in_new_file_is_added_line(tmp_path):
    _repo(tmp_path)
    patch = ("--- /dev/null\n+++ b/a.txt\n@@ -0,0 +1,3 @@\n+a\n\n+c\n"
             "--- /dev/null\n+++ b/b.txt\n@@ -0,0 +1,1 @@\n+b\n")
    warns = _apply(tmp_path, patch)
    assert (tmp_path / "a.txt").read_text() == "a\n\nc\n" and (tmp_path / "b.txt").read_text() == "b\n"
    assert len(warns) == 1 and "unprefixed empty line" in warns[0]


def test_added_lines_without_trailing_context_mid_file(tmp_path):
    src = "type A struct{}\n}\n\ntype B struct{}\n"
    _repo(tmp_path, {"a.go": src})
    patch = "--- a/a.go\n+++ b/a.go\n@@ -2,1 +2,3 @@\n }\n+\n+type New struct{}\n"
    used = []
    fixed, _ = recount_patch(patch)
    (tmp_path / "p.patch").write_text(fixed)
    _git_apply(tmp_path, "p.patch", used=used)
    got = (tmp_path / "a.go").read_text()
    assert got == "type A struct{}\n}\n\ntype New struct{}\n\ntype B struct{}\n" and used == ["--unidiff-zero"]
