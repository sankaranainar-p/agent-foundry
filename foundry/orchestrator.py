"""Orchestrator loop.

Agents are callables supplied by you, so this loop is model and tool agnostic:

    developer(task) -> str          returns a branch name or summary
    reviewer(task, work) -> (bool, str)   approve flag and notes
    tester(task, work) -> (bool, str)     pass flag and evidence path or notes

A failure at any stage routes back to the developer with the notes, up to review_retry_cap times.
A task that exhausts its retries is marked blocked and the loop moves on, so one bad task
does not stall the queue. The human sees blocked tasks in the status report.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .tasks import Task, next_open_task, parse_tasks, set_status
from .wrapper import Paused

Developer = Callable[[Task, str], str]
Reviewer = Callable[[Task, str], tuple[bool, str]]
Tester = Callable[[Task, str], tuple[bool, str]]


@dataclass
class Outcome:
    task_id: str
    status: str  # done | blocked | paused
    attempts: int
    notes: str = ""


def run_task(
    task: Task,
    developer: Developer,
    reviewer: Reviewer,
    tester: Tester,
    retry_cap: int = 3,
) -> Outcome:
    notes = ""
    for attempt in range(1, retry_cap + 1):
        try:
            work = developer(task, notes)
            approved, notes = reviewer(task, work)
            if not approved:
                continue
            passed, notes = tester(task, work)
            if not passed:
                continue
            return Outcome(task.id, "done", attempt)
        except Paused as p:
            return Outcome(task.id, "paused", attempt, f"resume_at={p.resume_at}")
    return Outcome(task.id, "blocked", retry_cap, notes)


def run_queue(
    tasks_path: str | Path,
    developer: Developer,
    reviewer: Reviewer,
    tester: Tester,
    retry_cap: int = 3,
) -> list[Outcome]:
    """Work open tasks in order. Stops on a pause so the caller can sleep until resume_at."""
    path = Path(tasks_path)
    outcomes: list[Outcome] = []
    while True:
        text = path.read_text()
        task = next_open_task(parse_tasks(text))
        if task is None:
            return outcomes
        outcome = run_task(task, developer, reviewer, tester, retry_cap)
        outcomes.append(outcome)
        if outcome.status == "paused":
            return outcomes
        path.write_text(set_status(text, task.id, outcome.status))
