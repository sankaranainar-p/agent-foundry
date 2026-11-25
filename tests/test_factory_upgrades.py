"""Tests for the factory upgrades:
- Isolated git worktrees (dirty main working tree is never touched)
- Tool-using developer mode (`agents.developer.mode: agent`)
- Deterministic build/test verification stage (`commands.build`, `commands.test`)
- Context filtering (`context.include`, `context.exclude`, `context.docs`) and local role prompt overrides
- Reviewer sees touched file contents alongside the diff
- Task validation (`validate_tasks`), dependencies (`Depends on:`), and milestone filtering (`--milestone`)
- CLI `audit` command (`logs/gap-report.md` and `logs/parking-lot.md`)
- Crash recovery when a task branch is already merged into `foundry/integration`
- Cost and token telemetry in `logs/calls.jsonl` and `logs/status.md`
"""

import json
from pathlib import Path

import pytest

from foundry import validate_tasks
from foundry.adapters import Reply, claude_agent, claude_cli
from foundry.cli import main
from foundry.orchestrator import AgentFailed
from foundry.tasks import next_open_task, parse_tasks
from test_integration import new_file
from test_runners_cli import DIFF, TASKS, git, proj, runners, task  # noqa: F401


def test_worktree_isolation_preserves_dirty_working_tree(proj):
    # Dirty tracked and untracked files in the user's working tree must survive a full run untouched.
    (proj / "constitution.md").write_text("DIRTY WORKING TREE")
    (proj / "untracked_scratch.txt").write_text("keep me")

    def claude(prompt):
        return "VERDICT: APPROVE\nok" if "Agent 2: PR Reviewer" in prompt else DIFF

    assert main(["run", str(proj), "--no-wait"], providers=(claude, lambda p: "", "m")) == 0
    assert (proj / "constitution.md").read_text() == "DIRTY WORKING TREE"
    assert (proj / "untracked_scratch.txt").read_text() == "keep me"
    assert git(proj, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"
    assert git(proj, "show", "foundry/integration:hello.txt") == "hi\n"


def test_verify_stage_runs_build_and_test_and_routes_failure_to_developer(proj):
    attempts = []

    def claude(prompt):
        if "Agent 2: PR Reviewer" in prompt:
            return "VERDICT: APPROVE\nok"
        attempts.append(prompt)
        # First attempt writes "bad", second attempt (after test failure notes) writes "good".
        body = "good" if "commands.test failed" in prompt else "bad"
        return new_file("hello.txt", body)

    cfg = (
        'name: p\ncommands:\n  build: "test -f hello.txt"\n  test: "grep -q good hello.txt"\n'
        '  smoke: "echo smoke-ok"\nhas_ui: false\nmodels:\n  fallback: m\nlimits:\n  review_retry_cap: 2\n'
    )
    (proj / "project.yaml").write_text(cfg)
    git(proj, "commit", "-qam", "add build and test")

    assert main(["run", str(proj), "--no-wait"], providers=(claude, lambda p: "", "m")) == 0
    assert len(attempts) == 2
    assert "commands.test failed" in attempts[1]
    assert (proj / "logs" / "T-001" / "attempt-1" / "verify.txt").exists()
    assert (proj / "logs" / "T-001" / "attempt-2" / "verify.txt").exists()
    assert git(proj, "show", "foundry/integration:hello.txt") == "good\n"


def test_developer_agent_mode_edits_files_in_worktree_and_enforces_protected_paths(proj):
    cfg = {
        "name": "p",
        "commands": {"smoke": "echo ok"},
        "has_ui": False,
        "models": {"fallback": "m"},
        "agents": {"developer": {"mode": "agent"}},
    }

    def good_agent(prompt, cwd):
        wt = Path(cwd)
        assert wt != proj and (wt / "constitution.md").exists()
        (wt / "created_by_agent.py").write_text("x = 42\n")
        return "Created created_by_agent.py for R-001."

    r, _ = runners(proj, lambda p: "APPROVE", cfg=cfg)
    r.agent_runner = good_agent
    branch = r.developer(task(proj))
    assert branch == "t-001-add-hello"
    assert not (proj / "created_by_agent.py").exists()  # main tree untouched
    assert git(proj, "show", f"{branch}:created_by_agent.py") == "x = 42\n"

    # Protected path touched in agent mode is rejected and branch is cleaned up.
    def bad_agent(prompt, cwd):
        (Path(cwd) / "spec.md").write_text("hacked\n")
        return "Edited spec."

    r.agent_runner = bad_agent
    with pytest.raises(AgentFailed, match="protected paths"):
        r.developer(task(proj))


def test_claude_agent_adapter_passes_permission_and_tools_and_cwd(tmp_path):
    seen = {}

    def fake_run(cmd, capture_output, text, timeout, cwd):
        seen["cmd"] = cmd
        seen["cwd"] = cwd

        class P:
            returncode = 0
            stdout = json.dumps({
                "result": "done",
                "is_error": False,
                "total_cost_usd": 0.125,
                "num_turns": 4,
                "usage": {"input_tokens": 100, "output_tokens": 50},
            })
            stderr = ""

        return P()

    out = claude_agent("do work", str(tmp_path), tools=("Read", "Edit"), run=fake_run)
    assert out == "done"
    assert isinstance(out, Reply) and out.meta["cost_usd"] == 0.125 and out.meta["num_turns"] == 4
    assert "--permission-mode" in seen["cmd"] and "acceptEdits" in seen["cmd"]
    assert "Read,Edit" in seen["cmd"]
    assert seen["cwd"] == str(tmp_path)


def test_context_filtering_custom_docs_and_role_override(proj):
    (proj / "uv.lock").write_text("huge lockfile " * 500)
    (proj / "src").mkdir()
    (proj / "src" / "keep.py").write_text("KEEP_ME = 1\n")
    (proj / "src" / "skip.txt").write_text("SKIP_ME = 1\n")
    (proj / "CLAUDE.md").write_text("CUSTOM_CONSTITUTION_FROM_CLAUDE_MD")
    (proj / "agents").mkdir()
    (proj / "agents" / "developer.md").write_text("# Custom Developer Role Prompt")
    git(proj, "add", "-A")
    git(proj, "commit", "-qm", "add context files")

    cfg = {
        "context": {
            "include": ["src/*.py"],
            "docs": {"constitution": ["CLAUDE.md", "constitution.md"]},
        }
    }
    seen = []

    def prim(prompt):
        seen.append(prompt)
        return DIFF

    r, _ = runners(proj, prim, cfg=cfg)
    r.developer(task(proj))
    prompt = seen[0]
    assert "# Custom Developer Role Prompt" in prompt
    assert "CUSTOM_CONSTITUTION_FROM_CLAUDE_MD" in prompt
    assert "KEEP_ME = 1" in prompt
    assert "SKIP_ME" not in prompt
    assert "huge lockfile" not in prompt


def test_reviewer_receives_touched_file_contents(proj):
    dev, _ = runners(proj, lambda p: DIFF)
    branch = dev.developer(task(proj))
    seen = []
    r, _ = runners(proj, lambda p: (seen.append(p), "VERDICT: APPROVE\nok")[1])
    ok, _ = r.reviewer(task(proj), branch)
    assert ok
    assert "=== touched files ===" in seen[0]
    assert "--- hello.txt\nhi" in seen[0]


def test_validate_tasks_dependencies_and_milestone_filtering(proj):
    spec = "# Spec\n| R-001 | req 1 | ok |\n| R-002 | req 2 | ok |\n"
    bad_tasks = """# Tasks

## T-001: Missing done and bad req
Satisfies: R-999
Depends on: T-002
Status: open

## T-002: No satisfies
Done when: works
Status: mystery
"""
    problems = validate_tasks(bad_tasks, spec)
    assert any("R-999" in p for p in problems)
    assert any("Done when" in p for p in problems)
    assert any("depends on unknown or later" in p for p in problems)
    assert any("no `Satisfies:`" in p for p in problems)
    assert any("status 'mystery'" in p for p in problems)

    good = """# Tasks

## T-001: Base task
Satisfies: R-001
Done when: hello.txt exists
Milestone: W5
Status: open

## T-002: Later milestone task
Satisfies: R-002
Depends on: T-001
Done when: world.txt exists
Milestone: W6
Status: open
"""
    assert validate_tasks(good, spec) == []
    parsed = parse_tasks(good)
    assert next_open_task(parsed, milestone="W6") is None  # T-001 not done yet
    assert next_open_task(parsed, milestone="W5").id == "T-001"

    (proj / "spec.md").write_text(spec)
    (proj / "tasks.md").write_text(good)
    git(proj, "commit", "-qam", "good tasks")

    prov = (lambda p: "VERDICT: APPROVE\nok" if "Agent 2: PR Reviewer" in p else DIFF, lambda p: "", "m")
    assert main(["run", str(proj), "--milestone", "W5", "--no-wait"], providers=prov) == 0
    after = parse_tasks((proj / "tasks.md").read_text())
    assert [t.status for t in after] == ["done", "open"]


def test_crash_recovery_marks_already_merged_task_done_without_rerunning(proj):
    r, _ = runners(proj, lambda p: DIFF)
    t = task(proj)
    branch = r.developer(t)
    assert r.merge(t, branch)[0] is True
    # Simulate a crash before tasks.md was updated: task is still Status: open, but branch is merged.
    assert r.is_merged(t) is True

    prov = (lambda p: pytest.fail("should not call model for already-merged task"), lambda p: "", "m")
    assert main(["run", str(proj), "--no-wait"], providers=prov) == 0
    assert parse_tasks((proj / "tasks.md").read_text())[0].status == "done"


def test_branch_with_no_commits_is_not_already_merged(proj):
    r, _ = runners(proj, lambda p: DIFF)
    t = task(proj)
    r.ensure_integration()
    git(proj, "branch", r.task_branch(t), r.integ)
    # integration tip is a non-task merge commit and the empty task branch sits on it (the T-016 case)
    git(proj, "checkout", "-q", r.integ)
    git(proj, "commit", "-q", "--allow-empty", "-m", "Merge main into foundry/integration")
    git(proj, "branch", "-f", r.task_branch(t), r.integ)
    git(proj, "checkout", "-q", r.base)
    assert r.is_merged(t) is False


def test_cli_audit_splits_gap_report_and_parking_lot(proj):
    reply = "# Gap Report\n| R-001 | met | hello.txt |\n\n# Parking Lot\n- extra feature: low cost\n"
    prov = (lambda p: reply, lambda p: "", "m")
    assert main(["audit", str(proj)], providers=prov) == 0
    assert "R-001" in (proj / "logs" / "gap-report.md").read_text()
    assert "Parking Lot" not in (proj / "logs" / "gap-report.md").read_text()
    assert "extra feature" in (proj / "logs" / "parking-lot.md").read_text()


def test_cost_and_usage_logged_and_reported_in_status(proj):
    def claude(prompt):
        if "Agent 2: PR Reviewer" in prompt:
            return Reply("VERDICT: APPROVE\nok", cost_usd=0.05, input_tokens=200, output_tokens=20, num_turns=1)
        return Reply(DIFF, cost_usd=0.10, input_tokens=500, output_tokens=80, num_turns=2)

    assert main(["run", str(proj), "--no-wait"], providers=(claude, lambda p: "", "m")) == 0
    calls = [json.loads(line) for line in (proj / "logs" / "calls.jsonl").read_text().splitlines()]
    assert [c["role"] for c in calls] == ["developer", "reviewer"]
    assert calls[0]["cost_usd"] == 0.10 and calls[0]["input_tokens"] == 500 and "duration_s" in calls[0]
    status = (proj / "logs" / "status.md").read_text()
    assert "- cost so far: $0.15" in status


def test_claude_cli_is_tool_free_in_an_empty_temp_cwd():
    seen = {}

    def fake_run(cmd, capture_output, text, timeout, cwd):
        seen["cmd"], seen["cwd"] = cmd, cwd
        seen["empty"] = not __import__("os").listdir(cwd)

        class P:
            returncode = 0
            stdout = json.dumps({"result": "ok"})
            stderr = ""

        return P()

    assert claude_cli("hi", run=fake_run) == "ok"
    cmd = seen["cmd"]
    assert cmd[cmd.index("--tools") + 1] == ""
    assert "--allowedTools" not in cmd and "--permission-mode" not in cmd
    assert seen["empty"] and not __import__("os").path.exists(seen["cwd"])  # fresh dir, removed afterwards


def test_claude_agent_keeps_tools_unrestricted_by_tools_flag(tmp_path):
    seen = {}

    def fake_run(cmd, capture_output, text, timeout, cwd):
        seen["cmd"], seen["cwd"] = cmd, cwd

        class P:
            returncode = 0
            stdout = json.dumps({"result": "ok"})
            stderr = ""

        return P()

    claude_agent("x", str(tmp_path), run=fake_run)
    assert "--tools" not in seen["cmd"] and seen["cwd"] == str(tmp_path)
    assert seen["cmd"][seen["cmd"].index("--allowedTools") + 1] == "Read,Edit,Write,Glob,Grep"


def test_plan_with_zero_tasks_exits_1_writes_no_proposal_and_saves_reply(proj, capsys):
    prov = (lambda p: "I wrote it to /tmp/tasks.md, please review.", lambda p: "x", "m")
    assert main(["plan", str(proj)], providers=prov) == 1
    assert not (proj / "tasks.proposed.md").exists()
    assert (proj / "logs" / "planner-reply.md").read_text() == "I wrote it to /tmp/tasks.md, please review."
    assert "no tasks" in capsys.readouterr().err


def test_plan_with_validation_problems_still_writes_file_but_exits_1(proj):
    reply = TASKS.replace("Satisfies: R-001\n", "") % "Claude"
    assert validate_tasks(reply)  # precondition: the reply has problems
    prov = (lambda p: reply, lambda p: "x", "m")
    assert main(["plan", str(proj)], providers=prov) == 1
    assert (proj / "tasks.proposed.md").read_text() == reply
    assert (proj / "logs" / "planner-reply.md").read_text() == reply


def test_planner_prompt_demands_reply_only_no_files(proj):
    from foundry.runners import AGENTS

    text = (AGENTS / "planner.md").read_text()
    assert "complete contents of tasks.md and nothing else" in text and "do not use /tmp" in text


def test_gap_analyzer_prompt_has_inline_evidence_and_outputs_files(proj):
    ev = proj / "logs" / "evidence" / "T-001"
    ev.mkdir(parents=True)
    (ev / "stdout.txt").write_text("X" * 2000 + "TAIL-OUT")
    (ev / "stderr.txt").write_text("TAIL-ERR")
    (ev / "exit_code.txt").write_text("3\n")
    prompts = []
    r, _ = runners(proj, lambda p: prompts.append(p) or "")
    r.gap_analyzer()
    p = prompts[0]
    assert "exit code: 3" in p and "TAIL-OUT" in p and "TAIL-ERR" in p
    assert "X" * 1492 + "TAIL-OUT" in p and "X" * 1493 not in p  # exactly the last 1500 chars


def test_snapshot_puts_named_files_first_and_lists_omitted(proj):
    (proj / "a.txt").write_text("a" * 60)
    (proj / "core.go").write_text("c" * 60)
    git(proj, "add", "-A")
    git(proj, "commit", "-q", "-m", "x")
    r, _ = runners(proj, lambda p: "")
    r.cfg["context"] = {"max_chars": 80, "exclude": []}
    snap = r._snapshot(first=["core.go"])
    assert "--- core.go\n" + "c" * 60 in snap
    assert "Files omitted for size" in snap and "a.txt" in snap.splitlines()[0]


def test_cli_audit_rejects_tool_call_reply(proj, capsys):
    gap = proj / "logs" / "gap-report.md"
    gap.parent.mkdir(exist_ok=True)
    gap.write_text("OLD")
    bad = '# Gap Report\n| R-001 | met |\n<invoke name="Read"><parameter name="p">x</parameter></invoke>'
    assert main(["audit", str(proj)], providers=(lambda p: bad, lambda p: "", "m")) == 1
    assert gap.read_text() == "OLD"
    assert (proj / "logs" / "gap-analyzer-reply.md").read_text() == bad
    assert "tool call" in capsys.readouterr().err
    for bad in ("just prose", "# Gap Report\nno table here"):
        assert main(["audit", str(proj)], providers=(lambda p: bad, lambda p: "", "m")) == 1
        assert gap.read_text() == "OLD"


def test_snapshot_focused_mode_includes_full_for_task_siblings_and_helpers_and_decls_for_rest(proj):
    (proj / "core").mkdir()
    (proj / "other").mkdir()
    (proj / "core" / "index.go").write_text(
        "package core\n\n// IndexSpans indexes spans.\nfunc IndexSpans() {\n\t_ = BuildFixtureSpan()\n\t FOCUS_BODY_LINE := 1\n}\n"
    )
    (proj / "core" / "sibling.go").write_text(
        "package core\n\n// SiblingType is in the same dir.\ntype SiblingType struct {\n\tSIBLING_BODY_FIELD int\n}\n"
    )
    (proj / "core" / "helpers_test.go").write_text(
        "package core\n\n// BuildFixtureSpan builds a test span used by focus files.\nfunc BuildFixtureSpan() int {\n\tHELPER_BODY_MARKER := 42\n\treturn HELPER_BODY_MARKER\n}\n"
    )
    (proj / "core" / "unrelated_test.go").write_text(
        "package core\n\n// TestUnrelated tests something else.\nfunc TestUnrelated() {\n\tUNRELATED_TEST_BODY := 99\n}\n"
    )
    (proj / "other" / "util.py").write_text(
        "# OtherUtility comment\ndef OtherUtility():\n    OTHER_PY_BODY = 'secret'\n    return OTHER_PY_BODY\n"
    )
    (proj / "other" / "util.py").write_text((proj / "other" / "util.py").read_text() + "# pad\n" * 2000)
    git(proj, "add", "-A")
    git(proj, "commit", "-q", "-m", "add files for focused snapshot")

    t = parse_tasks(
        "# Tasks\n\n## T-001: Update index\nSatisfies: R-001\nInputs: core/index.go\n"
        "Outputs: core/index.go\nDone when: works\nAllowed models: Claude\nStatus: open\n"
    )[0]
    r, _ = runners(proj, lambda p: "")
    r.cfg["context"] = {"mode": "focused", "max_chars": 60000}
    snap = r._snapshot(task=t)

    # Notice at top explaining focused mode and declaration-only files.
    assert "Focused snapshot:" in snap and "declaration lines only" in snap
    # 1. Named file in Inputs/Outputs has full body.
    assert "FOCUS_BODY_LINE := 1" in snap
    # 2. Sibling non-test source file in the same directory has full body.
    assert "SIBLING_BODY_FIELD int" in snap
    # 3. Test file defining a helper identifier (BuildFixtureSpan) used by focus files has full body.
    assert "HELPER_BODY_MARKER := 42" in snap
    # 4. Unrelated test file in same dir & file in other dir show declaration + preceding comment only, no body.
    # Every file in a focus dir stays full, tests included.
    assert "UNRELATED_TEST_BODY := 99" in snap
    assert "--- other/util.py (declarations only; non-focus file)" in snap
    assert "# OtherUtility comment\ndef OtherUtility():" in snap
    assert "OTHER_PY_BODY" not in snap


def test_snapshot_focused_keeps_gomod_and_focus_dir_full_and_reduces_large_unrelated(proj):
    (proj / "cmd").mkdir()
    (proj / "far").mkdir()
    (proj / "go.mod").write_text("module x\n\ngo 1.22\n" + "// pad\n" * 2000)
    (proj / "cmd" / "other.go").write_text("package main\n")
    (proj / "cmd" / "main.go").write_text("package main\n\nfunc main() {\n\tMAIN_BODY := 1\n}\n" + "// pad\n" * 2000)
    (proj / "far" / "big.go").write_text("package far\n\nfunc Big() {\n\tBIG_BODY := 1\n}\n" + "// pad\n" * 2000)
    git(proj, "add", "-A")
    git(proj, "commit", "-q", "-m", "files")
    t = parse_tasks(
        "# Tasks\n\n## T-001: Edit cmd\nSatisfies: R-001\nInputs: cmd/other.go\n"
        "Outputs: cmd/other.go\nDone when: works\nAllowed models: Claude\nStatus: open\n"
    )[0]
    r, _ = runners(proj, lambda p: "")
    r.cfg["context"] = {"mode": "focused", "max_chars": 200000}
    snap = r._snapshot(task=t)
    assert "--- go.mod\n" in snap
    assert "--- cmd/main.go\n" in snap and "MAIN_BODY := 1" in snap
    assert "--- far/big.go (declarations only" in snap and "BIG_BODY" not in snap
    assert "Reduced to declarations:" in snap and "far/big.go" in snap


def test_prompts_put_fixed_sections_first_and_task_and_retry_notes_last_and_log_section_sizes(proj):
    prompts = []

    def claude(prompt):
        prompts.append(prompt)
        if "Agent 2: PR Reviewer" in prompt:
            return "VERDICT: CHANGES REQUESTED\n1. hello.txt:1 missing test" if len(prompts) == 2 else "VERDICT: APPROVE\nok"
        return DIFF

    assert main(["run", str(proj), "--no-wait"], providers=(claude, lambda p: "", "m")) == 0
    # Attempt 2 developer prompt has constitution, spec, plan, repository, output format BEFORE task and retry notes.
    dev_retry_prompt = prompts[2]
    order = [
        dev_retry_prompt.index("=== constitution ==="),
        dev_retry_prompt.index("=== spec ==="),
        dev_retry_prompt.index("=== plan ==="),
        dev_retry_prompt.index("=== repository ==="),
        dev_retry_prompt.index("=== output format ==="),
        dev_retry_prompt.index("=== task ==="),
        dev_retry_prompt.index("=== notes from previous attempt ==="),
        dev_retry_prompt.index("=== Rejected attempt:"),
    ]
    assert order == sorted(order)

    # Reviewer prompt has constitution, spec, touched files, diff BEFORE task.
    rev_prompt = prompts[1]
    rev_order = [
        rev_prompt.index("=== constitution ==="),
        rev_prompt.index("=== spec ==="),
        rev_prompt.index("=== touched files ==="),
        rev_prompt.index("=== diff ==="),
        rev_prompt.index("=== task ==="),
    ]
    assert rev_order == sorted(rev_order)

    # calls.jsonl logs prompt size by section (`section_chars`) on every call.
    calls = [json.loads(line) for line in (proj / "logs" / "calls.jsonl").read_text().splitlines()]
    assert len(calls) == 4
    dev_sec = calls[2]["section_chars"]
    for key in ("role", "constitution", "spec", "plan", "repository", "task", "notes from previous attempt"):
        assert dev_sec[key] > 0
    rev_sec = calls[1]["section_chars"]
    for key in ("role", "constitution", "spec", "touched files", "diff", "task"):
        assert rev_sec[key] > 0

