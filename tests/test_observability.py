import pytest

from foundry import RateLimited
from foundry.cli import main
from foundry.tasks import parse_tasks
from test_integration import new_file, two_tasks
from test_runners_cli import DIFF, TASKS, proj  # noqa: F401  (proj is a fixture)

NO = "VERDICT: CHANGES REQUESTED\n1. hello.txt: wrong"
YES = "VERDICT: APPROVE\nfine"


def provider(review, dev=lambda prompt: DIFF):
    def claude(prompt):
        return review(prompt) if "Agent 2: PR Reviewer" in prompt else dev(prompt)

    return claude, lambda p: "flash", "m"


def statuses(proj):
    return [t.status for t in parse_tasks((proj / "tasks.md").read_text())]


def test_every_attempt_is_persisted_and_blocked_reason_is_last_notes(proj, capsys):
    assert main(["run", str(proj), "--no-wait"], providers=provider(lambda p: NO)) == 1
    for n in (1, 2):  # review_retry_cap is 2 in the fixture
        d = proj / "logs" / "T-001" / f"attempt-{n}"
        assert "Added hello." in (d / "developer-reply.md").read_text()
        assert "+hi" in (d / "diff.patch").read_text()
        assert (d / "reviewer-reply.md").read_text() == NO
        assert (d / "notes.txt").read_text() == NO  # the reason, even when no exception happened
        assert not (d / "smoke.txt").exists()
    assert statuses(proj) == ["blocked"]
    assert "T-001 blocked: VERDICT: CHANGES REQUESTED" in capsys.readouterr().err


def test_smoke_output_and_agent_failure_reasons_are_saved(proj):
    (proj / "project.yaml").write_text((proj / "project.yaml").read_text().replace('"echo smoke-ok"', '"echo boom; exit 3"'))
    main(["run", str(proj), "--no-wait"], providers=provider(lambda p: YES))
    smoke = (proj / "logs" / "T-001" / "attempt-2" / "smoke.txt").read_text()
    assert "exit code: 3" in smoke and "boom" in smoke
    assert "boom" in (proj / "logs" / "T-001" / "attempt-2" / "notes.txt").read_text()  # stdout is in the notes
    # reopen the blocked task for the protected path case
    (proj / "tasks.md").write_text(TASKS % "Claude")
    main(["run", str(proj), "--no-wait"], providers=provider(lambda p: YES, dev=lambda p: new_file("spec.md")))
    d = proj / "logs" / "T-001" / "attempt-1"
    assert "protected paths" in (d / "failure.txt").read_text() and "spec.md" in (d / "failure.txt").read_text()
    assert "spec.md" in (d / "diff.patch").read_text()


def test_blocked_task_halts_queue_exit_1_and_later_tasks_stay_open(proj, capsys):
    two_tasks(proj)
    assert main(["run", str(proj), "--no-wait"], providers=provider(lambda p: NO)) == 1
    assert statuses(proj) == ["blocked", "open"]
    err = capsys.readouterr().err
    assert "T-002 start" not in err and not (proj / "logs" / "T-002").exists()


def test_keep_going_continues_past_blocked(proj, capsys):
    two_tasks(proj)
    dev = lambda p: new_file("world.txt") if "=== task ===\nT-002" in p else DIFF  # noqa: E731
    review = lambda p: YES if "world.txt" in p else NO  # noqa: E731
    assert main(["run", str(proj), "--no-wait", "--keep-going"], providers=provider(review, dev)) == 1
    assert statuses(proj) == ["blocked", "done"]
    assert "T-002 done" in capsys.readouterr().err


def test_progress_output_and_quiet(proj, capsys):
    assert main(["run", str(proj), "--no-wait"], providers=provider(lambda p: YES)) == 0
    err = capsys.readouterr().err.splitlines()
    assert err[0] == "integration branch: foundry/integration (0 task merges)"
    assert "T-001 start (tier: primary, attempt 1)" in err
    for line in ("  developer ok (t-001-add-hello)", "  reviewer: approved", "  smoke: pass", "T-001 done"):
        assert line in err
    assert err[-1].startswith("summary: 1 done, 0 blocked, 0 paused. Status: ") and err[-1].endswith("status.md")


def test_progress_reports_rejection_and_second_attempt(proj, capsys):
    replies = iter([NO, YES])
    assert main(["run", str(proj), "--no-wait"], providers=provider(lambda p: next(replies))) == 0
    err = capsys.readouterr().err
    assert "  reviewer: not approved (VERDICT: CHANGES REQUESTED)" in err
    assert "T-001 start (tier: primary, attempt 2)" in err


def test_quiet_and_empty_queue(proj, capsys):
    assert main(["run", str(proj), "--no-wait", "--quiet"], providers=provider(lambda p: YES)) == 0
    assert capsys.readouterr().err == ""
    assert main(["run", str(proj), "--no-wait"], providers=provider(lambda p: YES)) == 0
    assert f"no open tasks in {proj / 'tasks.md'}" in capsys.readouterr().err


def test_pause_and_tier_change_report_reason_and_resume_at(proj, capsys):
    def limited(prompt):
        raise RateLimited(500)

    assert main(["run", str(proj), "--no-wait"], providers=(limited, lambda p: "flash", "m")) == 2
    err = capsys.readouterr().err
    assert "T-001 tier changed (primary rate limited mid-task; resume_at=20" in err
    assert "T-001 paused: unbounded task, primary limited; resume_at=20" in err
