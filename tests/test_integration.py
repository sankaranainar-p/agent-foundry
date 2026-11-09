import subprocess

import pytest

from foundry.cli import main
from foundry.orchestrator import AgentFailed, run_queue
from foundry.tasks import parse_tasks
from test_runners_cli import DIFF, TASKS, git, proj, runners, task  # noqa: F401  (proj is a fixture)

OK = lambda t, w: (True, "")  # noqa: E731


def new_file(path, body="x"):
    return f"```diff\ndiff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n+++ b/{path}\n@@ -0,0 +1 @@\n+{body}\n```"


def two_tasks(proj):
    (proj / "tasks.md").write_text(TASKS % "Claude" + "\n" + (TASKS % "Claude").split("\n\n", 1)[1].replace("T-001: Add hello", "T-002: Extend hello"))
    git(proj, "commit", "-qam", "two tasks")


EXTEND = """```diff
diff --git a/hello.txt b/hello.txt
--- a/hello.txt
+++ b/hello.txt
@@ -1 +1,2 @@
 hi
+there
```"""


def test_dependent_tasks_stack_on_integration_branch_and_base_is_untouched(proj):
    two_tasks(proj)
    dev_prompts = {}

    def claude(prompt):
        if "Agent 2: PR Reviewer" in prompt:
            return "APPROVE"
        tid = "T-002" if "=== task ===\nT-002" in prompt else "T-001"
        dev_prompts[tid] = prompt
        return EXTEND if tid == "T-002" else DIFF

    assert main(["run", str(proj), "--no-wait"], providers=(claude, lambda p: "", "m")) == 0
    assert "--- hello.txt\nhi" not in dev_prompts["T-001"]
    assert "--- hello.txt\nhi" in dev_prompts["T-002"]  # T-002 sees T-001's file
    assert git(proj, "show", "foundry/integration:hello.txt") == "hi\nthere\n"
    assert subprocess.run(["git", "cat-file", "-e", "main:hello.txt"], cwd=proj).returncode != 0
    assert git(proj, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"
    assert [t.status for t in parse_tasks((proj / "tasks.md").read_text())] == ["done", "done"]
    assert "- integration branch: foundry/integration (2 task merges)" in (proj / "logs" / "status.md").read_text()


def test_merge_conflict_blocks_task_with_conflict_text(proj):
    r, _ = runners(proj, lambda p: "APPROVE" if "Agent 2: PR Reviewer" in p else DIFF)

    def dev(t, notes):
        branch = r.developer(t, notes)
        git(proj, "checkout", "-q", "foundry/integration")  # integration moves under the task
        (proj / "hello.txt").write_text("conflicting\n")
        git(proj, "add", "hello.txt")
        git(proj, "commit", "-qm", "other work")
        git(proj, "checkout", "-q", "main")
        return branch

    task(proj)
    outs = run_queue(proj / "tasks.md", dev, r.reviewer, r.playwright, merger=r.merge)
    assert outs[0].status == "blocked" and "conflict" in outs[0].notes and "hello.txt" in outs[0].notes
    assert parse_tasks((proj / "tasks.md").read_text())[0].status == "blocked"
    assert git(proj, "show", "foundry/integration:hello.txt") == "conflicting\n"  # merge aborted cleanly
    assert git(proj, "status", "--porcelain", "--untracked-files=no").strip() in ("", "M tasks.md")


@pytest.mark.parametrize("patch", [
    new_file(".env.local", "KEY=1"),
    new_file("agents/evil.md"),
    new_file(".github/workflows/ci.yml"),
    new_file("sub/.env"),
    "```diff\ndiff --git a/spec.md b/spec.md\n--- a/spec.md\n+++ b/spec.md\n@@ -1 +1 @@\n-SPEC\n\\ No newline at end of file\n+HACKED\n\\ No newline at end of file\n```",
    "```diff\ndiff --git a/hello.txt b/.env\nsimilarity index 100%\nrename from hello.txt\nrename to .env\n```",
])
def test_protected_paths_rejected_before_any_branch_exists(proj, patch):
    r, _ = runners(proj, lambda p: patch)
    with pytest.raises(AgentFailed, match="protected paths"):
        r.developer(task(proj))
    assert git(proj, "branch", "--list", "t-001*").strip() == ""
    assert git(proj, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"


def test_protected_paths_come_from_project_yaml(proj):
    cfg = {"protected_paths": ["hello.txt"]}
    r, _ = runners(proj, lambda p: DIFF, cfg=cfg)
    with pytest.raises(AgentFailed, match="hello.txt"):
        r.developer(task(proj))
    r2, _ = runners(proj, lambda p: new_file("agents/x.md"), cfg=cfg)
    r2.developer(task(proj))  # defaults replaced, agents/ no longer listed


def test_retry_after_rejected_review_starts_fresh_branch_and_merges_once(proj):
    state = {"reviews": 0}
    dev_prompts = []

    def claude(prompt):
        if "Agent 2: PR Reviewer" in prompt:
            state["reviews"] += 1
            return "CHANGES REQUESTED\n1. Please write bye" if state["reviews"] == 1 else "APPROVE"
        dev_prompts.append(prompt)
        return new_file("hello.txt", "bye" if "Please write bye" in prompt else "hi")

    assert main(["run", str(proj), "--no-wait"], providers=(claude, lambda p: "", "m")) == 0
    assert len(dev_prompts) == 2 and "Please write bye" in dev_prompts[1] and "Please write bye" not in dev_prompts[0]
    assert git(proj, "show", "foundry/integration:hello.txt") == "bye\n"
    assert git(proj, "rev-list", "--merges", "--count", "main..foundry/integration").strip() == "1"
    assert parse_tasks((proj / "tasks.md").read_text())[0].status == "done"
