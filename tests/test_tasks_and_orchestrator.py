import subprocess
import sys
from pathlib import Path

from foundry import Paused, next_open_task, parse_tasks, set_status
from foundry.orchestrator import run_queue

SAMPLE = """# Tasks

## T-001: First thing
Satisfies: R-001, R-002
Inputs: a
Outputs: b
Done when: it works
Allowed models: Claude
Status: open

## T-002: Second thing
Satisfies: R-003
Inputs: c
Outputs: d
Done when: it also works
Allowed models: Claude, Gemini Flash
Status: open
"""


def test_parse_tasks():
    tasks = parse_tasks(SAMPLE)
    assert [t.id for t in tasks] == ["T-001", "T-002"]
    assert tasks[0].requirements == ["R-001", "R-002"]
    assert tasks[0].bounded is False
    assert tasks[1].bounded is True


def test_set_status_only_touches_one_line():
    out = set_status(SAMPLE, "T-001", "done")
    assert out.count("Status: done") == 1
    assert out.count("Status: open") == 1
    assert out.replace("Status: done", "Status: open") == SAMPLE
    assert next_open_task(parse_tasks(out)).id == "T-002"


def test_queue_marks_done_and_blocked(tmp_path):
    path = tmp_path / "tasks.md"
    path.write_text(SAMPLE)

    def developer(task, notes):
        return f"branch-{task.id.lower()}"

    def reviewer(task, work):
        # T-002 never gets approved
        return (task.id == "T-001", "needs work")

    def tester(task, work):
        return (True, "evidence/ok")

    outcomes = run_queue(path, developer, reviewer, tester, retry_cap=2)
    assert [(o.task_id, o.status) for o in outcomes] == [("T-001", "done"), ("T-002", "blocked")]
    final = parse_tasks(path.read_text())
    assert [t.status for t in final] == ["done", "blocked"]


def test_queue_stops_on_pause_and_leaves_task_open(tmp_path):
    path = tmp_path / "tasks.md"
    path.write_text(SAMPLE)

    def developer(task, notes):
        raise Paused(resume_at=5000.0)

    outcomes = run_queue(path, developer, lambda t, w: (True, ""), lambda t, w: (True, ""))
    assert outcomes[0].status == "paused"
    assert next_open_task(parse_tasks(path.read_text())).id == "T-001"


def test_init_project_copies_templates_and_never_overwrites(tmp_path):
    script = Path(__file__).resolve().parent.parent / "scripts" / "init_project.py"
    dest = tmp_path / "proj"
    run = lambda *a: subprocess.run([sys.executable, str(script), str(dest), *a], capture_output=True, text=True)

    first = run()
    assert first.returncode == 0
    for name in ["constitution.md", "spec.md", "plan.md", "tasks.md", "project.yaml"]:
        assert (dest / name).exists()

    (dest / "spec.md").write_text("my edited spec")
    second = run()
    assert "skipped" in second.stdout
    assert (dest / "spec.md").read_text() == "my edited spec"
