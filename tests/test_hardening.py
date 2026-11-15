import os
import time

import pytest

from foundry.orchestrator import AgentFailed
from foundry.runners import REJECTED_HEADING, Runners
from foundry.tasks import parse_tasks
from test_integration import new_file
from test_runners_cli import DIFF, TASKS, Recorder, git, proj, runners, task  # noqa: F401


# 1. protected_paths union and normalization

def test_protected_union_keeps_defaults_when_project_lists_only_docs(proj):
    r, _ = runners(proj, lambda p: "", cfg={"protected_paths": ["docs/*"]})
    for path in ("constitution.md", "spec.md", "plan.md", "tasks.md", "project.yaml", "agents/x.md",
                 ".github/workflows/t.yml", ".env", ".env.local", "docs/a.md"):
        assert r._filter_protected([path]) == [path], path
    assert r._filter_protected(["src/main.go"]) == []


@pytest.mark.parametrize("path", ["./agents/../.env", "./spec.md", "src/../plan.md", "agents\\x.md", "docs/./a.md"])
def test_protected_paths_are_normalized_before_matching(proj, path):
    r, _ = runners(proj, lambda p: "", cfg={"protected_paths": ["docs/*"]})
    assert r._filter_protected([path]) == [path]


def test_protected_dir_pattern_with_trailing_slash(proj):
    r, _ = runners(proj, lambda p: "", cfg={"protected_paths": ["./catalog/"]})
    assert r._filter_protected(["catalog/x/y.json"]) == ["catalog/x/y.json"]


# 2. command timeouts

def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def _timeout_cfg(proj, **cmds):
    return {"commands": cmds, "limits": {"command_timeout_seconds": 1}}


def test_verify_timeout_kills_child_and_returns_timeout_notes(proj):
    git(proj, "branch", "br")
    pidfile = proj / "child.pid"
    cmd = f"echo started; sleep 60 & echo $! > {pidfile}; wait"
    r, _ = runners(proj, lambda p: "", cfg=_timeout_cfg(proj, test=cmd))
    r.set_attempt("T-001", 1)
    t0 = time.time()
    ok, notes = r.verify(task(proj), "br")
    assert not ok and notes.startswith("timeout") and "started" in notes
    assert time.time() - t0 < 15
    pid = int(pidfile.read_text())
    for _ in range(20):
        if not _alive(pid):
            break
        time.sleep(0.1)
    assert not _alive(pid)


def test_timeout_notes_are_last_200_lines(proj):
    git(proj, "branch", "br")
    r, _ = runners(proj, lambda p: "", cfg=_timeout_cfg(proj, test="seq 1 500; sleep 60"))
    ok, notes = r.verify(task(proj), "br")
    assert notes.splitlines()[-1] == "500" and "\n301\n" in notes and "\n300\n" not in notes


def test_smoke_timeout_fails_with_timeout_reason(proj):
    git(proj, "branch", "br")
    r, _ = runners(proj, lambda p: "", cfg=_timeout_cfg(proj, smoke="sleep 60"))
    ok, notes = r.playwright(task(proj), "br")
    assert not ok and notes.startswith("timeout")


def test_timeout_default_is_300_and_overridable(proj):
    assert runners(proj, lambda p: "", cfg={})[0]._timeout() == 300
    assert runners(proj, lambda p: "", cfg={"limits": {"command_timeout_seconds": 7}})[0]._timeout() == 7


# 3. stateful retries

def test_attempt_two_prompt_has_rejected_diff_and_output_attempt_one_does_not(proj):
    prim = Recorder(lambda p: new_file("hello.txt", "one"))
    r, _ = runners(proj, prim)
    r.set_attempt("T-001", 1)
    r.developer(task(proj))
    r.record("notes.txt", "reviewer says: bad\nplease fix X")
    r.set_attempt("T-001", 2)
    r.developer(task(proj), "reviewer says: bad")
    assert REJECTED_HEADING not in prim.prompts[0]
    assert REJECTED_HEADING in prim.prompts[1]
    assert "+one" in prim.prompts[1] and "please fix X" in prim.prompts[1]
    assert "hello.txt" not in git(proj, "ls-tree", "-r", "--name-only", "foundry/integration")  # not merged


def test_large_rejected_diff_is_truncated(proj):
    r, _ = runners(proj, lambda p: "", cfg={})
    r.set_attempt("T-001", 1)
    r.record("diff.patch", "x" * 50_000)
    r.set_attempt("T-001", 2)
    section = r._rejected_attempt()[REJECTED_HEADING]
    assert "[diff truncated at 20 KB]" in section and len(section) < 21_000


# 4. per-task Verify line

def _vtask(proj, verify):
    (proj / "tasks.md").write_text((TASKS % "Claude").replace("Status: open", f"Verify: {verify}\nStatus: open"))
    return parse_tasks((proj / "tasks.md").read_text())[0]


def test_task_parses_verify_line(proj):
    assert _vtask(proj, "go test ./core/...").verify == "go test ./core/..."
    assert task(proj).verify == ""


def test_allowed_verify_command_runs_without_shell(proj):
    git(proj, "branch", "br")
    r, _ = runners(proj, lambda p: "", cfg={"verify_allowlist": ["echo"]})
    assert r.verify(_vtask(proj, "echo hi"), "br")[0]
    ok, _ = r.verify(_vtask(proj, "echo hi>marker $(touch marker2)"), "br")
    assert ok and not (proj / "marker").exists() and not (proj / "marker2").exists()  # not interpreted


def test_failing_allowed_verify_command_fails_attempt(proj):
    git(proj, "branch", "br")
    r, _ = runners(proj, lambda p: "", cfg={"verify_allowlist": ["false"]})
    ok, notes = r.verify(_vtask(proj, "false"), "br")
    assert not ok and "task verify failed" in notes


@pytest.mark.parametrize("line", [
    "rm -rf /", "bash -c 'echo hi'", "go test ./core/... ; rm x", "/bin/rm x", "./go test", "sh", "go 'unterminated",
])
def test_disallowed_verify_commands_are_rejected(proj, line):
    git(proj, "branch", "br")
    r, _ = runners(proj, lambda p: "", cfg={})
    ok, notes = r.verify(_vtask(proj, line), "br")
    assert not ok and "verify command not allowed" in notes


def test_semicolon_token_rejected_even_if_allowlisted_and_nothing_runs(proj):
    git(proj, "branch", "br")
    (proj / "x").write_text("keep")
    r, _ = runners(proj, lambda p: "", cfg={"verify_allowlist": ["echo", ";"]})
    ok, notes = r.verify(_vtask(proj, "echo a ; rm x"), "br")
    assert not ok and "verify command not allowed" in notes and (proj / "x").exists()


def test_full_test_suite_still_runs_after_task_verify_passes(proj):
    git(proj, "branch", "br")
    r, _ = runners(proj, lambda p: "", cfg={"verify_allowlist": ["echo"], "commands": {"test": "exit 3"}})
    ok, notes = r.verify(_vtask(proj, "echo ok"), "br")
    assert not ok and "commands.test failed (exit 3)" in notes


@pytest.mark.parametrize("line", ["rm -rf /", "go 'unterminated", "go test ./... ; rm x"])
def test_rejected_verify_blocks_task_with_zero_developer_calls(proj, line):
    from foundry.cli import main

    _vtask(proj, line)
    git(proj, "commit", "-qam", "verify line")
    calls = []

    def claude(prompt):
        calls.append(prompt)
        return new_file("hello.txt")

    assert main(["run", str(proj), "--no-wait"], providers=(claude, lambda p: "", "m")) != 0
    assert calls == []
    assert "Status: blocked" in (proj / "tasks.md").read_text()
    assert "config: verify command not allowed" in (proj / "logs" / "T-001" / "attempt-1" / "notes.txt").read_text()
