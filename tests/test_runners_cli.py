import json
import subprocess

import pytest

from foundry import ModelWrapper, RateLimited
from foundry.cli import main
from foundry.orchestrator import AgentFailed
from foundry.runners import Runners
from foundry.tasks import parse_tasks

TASKS = """# Tasks

## T-001: Add hello
Satisfies: R-001
Inputs: a
Outputs: hello.txt
Done when: hello.txt exists
Allowed models: %s
Status: open
"""
DIFF = "```diff\ndiff --git a/hello.txt b/hello.txt\nnew file mode 100644\n--- /dev/null\n+++ b/hello.txt\n@@ -0,0 +1 @@\n+hi\n```\nAdded hello."


def git(d, *a):
    return subprocess.run(["git", *a], cwd=d, capture_output=True, text=True, check=True).stdout


@pytest.fixture
def proj(tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    git(d, "init", "-q", "-b", "main")
    git(d, "config", "user.name", "t")
    git(d, "config", "user.email", "t@e.x")
    (d / "constitution.md").write_text("CONSTITUTION")
    (d / "spec.md").write_text("SPEC")
    (d / "plan.md").write_text("PLAN")
    (d / "project.yaml").write_text(
        'name: p\ncommands:\n  smoke: "echo smoke-ok"\nhas_ui: false\nmodels:\n  fallback: m\n'
        "limits:\n  review_retry_cap: 2\n"
    )
    (d / "tasks.md").write_text(TASKS % "Claude")
    git(d, "add", "-A")
    git(d, "commit", "-q", "-m", "init")
    return d


class Recorder:
    def __init__(self, reply):
        self.reply, self.prompts = reply, []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        return self.reply(prompt) if callable(self.reply) else self.reply


def runners(proj, primary, fallback=lambda p: "flash says " + DIFF, cfg=None):
    import yaml

    w = ModelWrapper(primary, fallback, proj / "logs" / "state.json")
    return Runners(proj, w, cfg or yaml.safe_load((proj / "project.yaml").read_text())), w


def task(proj, models="Claude"):
    (proj / "tasks.md").write_text(TASKS % models)
    return parse_tasks((proj / "tasks.md").read_text())[0]


def test_planner_prompt_and_unbounded(proj):
    prim = Recorder("TASKS")
    r, _ = runners(proj, prim)
    assert r.planner() == "TASKS"
    assert all(s in prim.prompts[0] for s in ("Agent 0: Planner", "CONSTITUTION", "SPEC", "PLAN"))


def test_developer_branches_commits_never_merges_and_labels_flash(proj):
    def limited(p):
        raise RateLimited(60)

    r, w = runners(proj, limited)
    w.begin_task("T-001")
    with pytest.raises(Exception):  # TierChanged on first mid-task limit
        r.developer(task(proj, "Claude, Gemini Flash"))
    w.begin_task("T-001")
    branch = r.developer(task(proj, "Claude, Gemini Flash"))
    assert branch == "t-001-add-hello"
    assert git(proj, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"  # never left on / merged into
    assert not (proj / "hello.txt").exists()
    msg = git(proj, "log", "-1", "--format=%B", branch)
    assert msg.strip() == "T-001: Add hello [flash]"
    assert "Co-Authored" not in msg


def test_developer_claude_commit_has_no_label_and_bad_reply_fails_softly(proj):
    r, _ = runners(proj, lambda p: DIFF)
    r.developer(task(proj))
    assert git(proj, "log", "-1", "--format=%s", "t-001-add-hello").strip() == "T-001: Add hello"
    r2, _ = runners(proj, lambda p: "no diff here")
    with pytest.raises(AgentFailed):
        r2.developer(task(proj))


def test_developer_unbounded_for_claude_only_task_pauses_instead_of_flash(proj):
    from foundry import Paused

    def limited(p):
        raise RateLimited(60)

    r, w = runners(proj, limited, fallback=lambda p: pytest.fail("flash used"))
    w.begin_task("T-001")
    with pytest.raises(Exception):
        r.developer(task(proj))
    w.begin_task("T-001")
    with pytest.raises(Paused):
        r.developer(task(proj))


def test_reviewer_sees_diff_not_notes_and_parses_verdict(proj):
    dev, _ = runners(proj, lambda p: DIFF)
    branch = dev.developer(task(proj))
    prim = Recorder("APPROVE: fine")
    r, _ = runners(proj, prim)
    ok, notes = r.reviewer(task(proj), branch)
    assert ok and notes.startswith("APPROVE")
    p = prim.prompts[0]
    assert "+hi" in p and "CONSTITUTION" in p and "SPEC" in p and "Added hello." not in p and "PLAN" not in p
    bad, _ = runners(proj, lambda p: "CHANGES REQUESTED\n1. x")
    assert bad.reviewer(task(proj), branch)[0] is False


def test_playwright_smoke_saves_evidence_and_ui_raises(proj):
    dev, _ = runners(proj, lambda p: DIFF)
    branch = dev.developer(task(proj))
    r, _ = runners(proj, lambda p: "")
    ok, _ = r.playwright(task(proj), branch)
    assert ok
    ev = proj / "logs" / "evidence" / "T-001"
    assert "smoke-ok" in (ev / "stdout.txt").read_text() and (ev / "exit_code.txt").read_text() == "0\n"
    r.cfg["commands"]["smoke"] = "exit 3"
    assert r.playwright(task(proj), branch)[0] is False
    r.cfg["has_ui"] = True
    with pytest.raises(NotImplementedError):
        r.playwright(task(proj), branch)


def test_gap_analyzer_is_unbounded_claude(proj):
    prim = Recorder("GAPS")
    r, _ = runners(proj, prim, fallback=lambda p: pytest.fail("flash used"))
    assert r.gap_analyzer() == "GAPS"
    assert "Gap Analyzer" in prim.prompts[0] and "SPEC" in prim.prompts[0]


def test_cli_plan_and_run_end_to_end(proj, capsys):
    def claude(prompt):
        if "Agent 0: Planner" in prompt:
            return TASKS % "Claude"
        if "Agent 2: PR Reviewer" in prompt:
            return "APPROVE: ok"
        return DIFF

    prov = (claude, lambda p: "flash", "m")
    assert main(["plan", str(proj)], providers=prov) == 0
    assert (proj / "tasks.proposed.md").read_text().startswith("# Tasks")
    assert main(["run", str(proj), "--no-wait"], providers=prov) == 0
    assert parse_tasks((proj / "tasks.md").read_text())[0].status == "done"
    assert "- done: 1" in (proj / "logs" / "status.md").read_text()


def test_cli_no_wait_exits_2_on_pause_and_persists_state(proj):
    def limited(p):
        raise RateLimited(500)

    prov = (limited, lambda p: pytest.fail("flash"), "m")
    assert main(["run", str(proj), "--no-wait"], providers=prov) == 2
    st = json.loads((proj / "logs" / "state.json").read_text())
    assert st["paused_task"] == "T-001" and st["resume_at"] > 0 and st["tier"] == "fallback"
    assert parse_tasks((proj / "tasks.md").read_text())[0].status == "open"
    assert "- paused: 1 (T-001)" in (proj / "logs" / "status.md").read_text()
