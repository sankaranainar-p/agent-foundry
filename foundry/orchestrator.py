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

import datetime as dt
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .tasks import Task, next_open_task, parse_tasks, set_status
from .wrapper import Paused, TierChanged


class AgentFailed(Exception):
    """An agent produced unusable output (no diff, patch does not apply). Routes back to the
    developer as notes, like a review failure."""

Developer = Callable[[Task, str], str]
Reviewer = Callable[[Task, str], tuple[bool, str]]
Tester = Callable[[Task, str], tuple[bool, str]]


@dataclass
class Outcome:
    task_id: str
    status: str  # done | blocked | paused | restart (tier changed mid-task, rerun it)
    attempts: int
    notes: str = ""


def run_task(
    task: Task,
    developer: Developer,
    reviewer: Reviewer,
    tester: Tester,
    retry_cap: int = 3,
    merger: Optional[Callable[[Task, str], tuple[bool, str]]] = None,
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
            if merger:  # the orchestrator merges, and only after review and tests pass
                merged, merge_notes = merger(task, work)
                if not merged:
                    return Outcome(task.id, "blocked", attempt, merge_notes)
            return Outcome(task.id, "done", attempt)
        except AgentFailed as e:
            notes = str(e)
        except TierChanged:
            return Outcome(task.id, "restart", attempt)
        except Paused as p:
            return Outcome(task.id, "paused", attempt, f"resume_at={p.resume_at}")
    return Outcome(task.id, "blocked", retry_cap, notes)


def run_queue(
    tasks_path: str | Path,
    developer: Developer,
    reviewer: Reviewer,
    tester: Tester,
    retry_cap: int = 3,
    begin_task: Optional[Callable[[str], None]] = None,
    after_task: Optional[Callable[[Task, Outcome], None]] = None,
    merger: Optional[Callable[[Task, str], tuple[bool, str]]] = None,
) -> list[Outcome]:
    """Work open tasks in order. Stops on a pause so the caller can sleep until resume_at.

    begin_task(task_id) runs before each task (wrapper.begin_task: the only place tiers change).
    after_task(task, outcome) runs after each task, including a pause (status report)."""
    path = Path(tasks_path)
    outcomes: list[Outcome] = []
    while True:
        text = path.read_text()
        task = next_open_task(parse_tasks(text))
        if task is None:
            return outcomes
        if begin_task:
            begin_task(task.id)
        outcome = run_task(task, developer, reviewer, tester, retry_cap, merger)
        if outcome.status == "restart":
            continue  # tier switched; begin_task applies it on the rerun
        outcomes.append(outcome)
        if outcome.status != "paused":
            path.write_text(set_status(text, task.id, outcome.status))
        if after_task:
            after_task(task, outcome)
        if outcome.status == "paused":
            return outcomes


def run_until_done(
    run_once: Callable[[], list[Outcome]],
    get_resume_at: Callable[[], float],
    probe_interval: float,
    wait: bool = True,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
) -> list[Outcome]:
    """Run the queue; on a pause sleep until resume_at (re-read every probe interval), then rerun.
    wait=False returns at the first pause. The queue itself is the resume state: paused tasks
    stay open, and the wrapper state file holds the tier and resume_at."""
    all_outcomes: list[Outcome] = []
    while True:
        outcomes = run_once()
        all_outcomes += outcomes
        if not outcomes or outcomes[-1].status != "paused" or not wait:
            return all_outcomes
        slept = False
        while True:
            left = get_resume_at() - clock()
            if left <= 0 and slept:
                break
            sleep(min(left, probe_interval) if left > 0 else probe_interval)
            slept = True


def write_status(path: str | Path, tasks_text: str, state: dict, extra: tuple[str, ...] = ()) -> None:
    """logs/status.md: counts, current task, active tier, resume_at."""
    tasks = parse_tasks(tasks_text)
    count = lambda st: sum(t.status == st for t in tasks)  # noqa: E731
    resume = state.get("resume_at") or 0
    resume_s = dt.datetime.fromtimestamp(resume).astimezone().isoformat(timespec="seconds") if resume else "none"
    lines = [
        "# Factory status",
        "",
        f"- done: {count('done')}",
        f"- blocked: {count('blocked')}",
        f"- paused: {1 if state.get('paused_task') else 0}" + (f" ({state['paused_task']})" if state.get("paused_task") else ""),
        f"- open: {count('open')}",
        f"- current task: {state.get('current_task') or 'none'}",
        f"- active tier: {state.get('tier')}",
        f"- resume_at: {resume_s}",
        *[f"- {line}" for line in extra],
        "",
    ]
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines))
